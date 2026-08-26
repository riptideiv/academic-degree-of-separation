import asyncio
import json
import logging
import math
import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from dotenv import load_dotenv

# Load local env vars from .env.local so local runs mirror
# Render. override=False means real environment vars (Render's) always win.
load_dotenv(Path(__file__).parent.parent / ".env.local", override=False)

from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from backend.affiliation_overrides import (
    AffiliationOverride,
    apply_reviewed_identity,
    get_effective_affiliation_overrides,
    get_verified_work_ids,
)
from backend.bfs import find_path
from backend.graph_backend import (
    ALL_EDGE_TYPES,
    ALL_WORK_EDGE_TYPES,
    GraphBackend,
    IdentityScopedBackend,
    OpenAlexBackend,
    _NeighborBatch,
    _is_work_id,
)
from backend.institution_ranking import (
    choose_deeper_search_shortlist,
    extract_topic_metadata,
    merge_balanced_candidate_pool,
    stable_result_sort_key,
    topic_similarity,
)
from backend.local_cache_index import LocalCacheIndex
from backend.models import (
    AuthorResult,
    AuthorWork,
    Connection,
    PaginatedAuthors,
    PaginatedWorks,
)
from backend.neighbor_store import (
    JsonNeighborStore,
    NeighborCache,
    NeighborStore,
    SupabaseNeighborStore,
)
from backend.openalex_client import OpenAlexClient, _short_id
from backend.path_evidence import (
    evaluate_edge_profile_compatibility,
    evaluate_intermediate_coherence,
)

log = logging.getLogger(__name__)

app = FastAPI(title="Researcher Degree of Separation")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)

_client = OpenAlexClient()
_BACKEND = os.environ.get("BACKEND", "openalex")

# ── Neighbor cache ─────────────────────────────────────────────────────────────
# A bounded in-memory LRU (`NeighborCache`) fronts a durable NeighborStore, so the
# process footprint stays flat under load instead of holding the whole table
# resident. On an LRU miss the store is consulted per-id; only a true miss hits
# OpenAlex. Setting SUPABASE_POOLER_CONNECTION_STRING
# selects the Postgres-backed store (survives Render's ephemeral FS); otherwise a
# local JSON file is used. NEIGHBOR_CACHE_MAX caps the resident entry count.

# Cache v2 fails closed on partial OpenAlex responses and validates displayed
# coauthor paths from exact works.  Keep it physically separate from legacy rings
# so a deploy cannot silently reuse empty/partial entries written by older code.
_CACHE_FILE = Path(__file__).parent.parent / "neighbor_cache_v2.json"
_LEGACY_INDEX_FILE = Path(__file__).parent.parent / "neighbor_cache.json"
_CACHE_MAX = int(os.environ.get("NEIGHBOR_CACHE_MAX", "10000"))
_local_index = LocalCacheIndex(
    _LEGACY_INDEX_FILE if _LEGACY_INDEX_FILE.exists() else _CACHE_FILE
)


def _make_store() -> NeighborStore:
    dsn = os.environ.get("SUPABASE_POOLER_CONNECTION_STRING")
    if dsn:
        return SupabaseNeighborStore(dsn)
    return JsonNeighborStore(_CACHE_FILE)


_store: NeighborStore = _make_store()
_cache = NeighborCache(_store, max_size=_CACHE_MAX)


RANK_EFFECTIVE_POOL_MAX = 80
RANK_FETCH_LIMIT_MAX = 80
RANK_TOPIC_FETCH_LIMIT = 50
RANK_TOPIC_ID_MAX = 20
RANK_HIERARCHY_FETCH_LIMIT = 80
RANK_SUBFIELD_ID_MAX = 12
RANK_FIELD_ID_MAX = 6
RANK_TOTAL_TIMEOUT_S = 12
RANK_INSTITUTION_LOOKUP_TIMEOUT_S = 2.5
RANK_DISCOVERY_TIMEOUT_S = 4.5
RANK_ORIGIN_PROFILE_TIMEOUT_S = 2.5
RANK_AFFILIATION_FETCH_TIMEOUT_S = 1.0
RANK_CANDIDATE_TIMEOUT_S = 5.5
# Two deep candidates at a time keeps the free OpenAlex budget focused enough
# that the highest-priority (reviewed/topic-near) candidates finish reliably.
RANK_CONCURRENCY = 2
RANK_PATH_CONCURRENCY = 8
RANK_MAX_DEPTH = 4
RANK_FAST_TIMEOUT_S = 8
RANK_FAST_BRIDGE_MAX = 240
RANK_DEEP_SHORTLIST_MAX = 8
RANK_ORIGIN_MAX = 10


@asynccontextmanager
async def lifespan(app: FastAPI):
    await _store.open()
    yield
    await _store.close()


app.router.lifespan_context = lifespan


def _make_backend(
    edge_types: set[str],
    work_edge_types: set[str] | None = None,
) -> GraphBackend:
    if _BACKEND == "bigquery":
        from backend.bigquery_backend import BigQueryBackend
        project = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
        if not project:
            raise RuntimeError(
                "BigQuery backend requires the GOOGLE_CLOUD_PROJECT environment variable"
            )
        backend: GraphBackend = BigQueryBackend(project, edge_types=edge_types)
    else:
        backend = OpenAlexBackend(
            _client,
            edge_types=edge_types,
            work_edge_types=work_edge_types,
            neighbor_cache=_cache,
        )
    return IdentityScopedBackend(
        backend,
        _client,
        edge_types=edge_types,
    )


class _SummarySeededBackend(GraphBackend):
    """Use complete-ish group-by rings for bounded deep-search roots.

    The general graph backend batches top works for speed. Institution Explorer's
    ten-person deep shortlist can cheaply afford one coauthor group-by request per
    root, avoiding the old top-50-work blind spot while all deeper frontier nodes
    still use the shared batched/cached backend.
    """

    def __init__(
        self,
        backend: GraphBackend,
        seeds: dict[str, list[Connection]],
        *,
        complete_seed_ids: set[str] | None = None,
    ):
        self._backend = backend
        self._seeds = seeds
        self._complete_seed_ids = set(complete_seed_ids or ())

    async def get_neighbors(self, author_id: str) -> list[Connection]:
        if author_id in self._seeds:
            return self._seeds[author_id]
        return await self._backend.get_neighbors(author_id)

    async def get_neighbors_batch(
        self, author_ids: list[str], cached_only: bool = False
    ) -> dict[str, list[Connection]]:
        seeded = {
            author_id: self._seeds[author_id]
            for author_id in author_ids
            if author_id in self._seeds
        }
        remaining = [author_id for author_id in author_ids if author_id not in seeded]
        delegated = (
            await self._backend.get_neighbors_batch(remaining, cached_only=cached_only)
            if remaining else {}
        )
        values = {
            author_id: seeded.get(author_id, delegated.get(author_id, []))
            for author_id in author_ids
        }
        delegated_complete = getattr(delegated, "complete_ids", set(remaining))
        complete_ids = (
            set(seeded) & self._complete_seed_ids
        ) | (set(remaining) & set(delegated_complete))
        return _NeighborBatch(values, complete_ids=complete_ids)


def _get_inst(author: dict) -> str | None:
    override = author.get("_affiliation_override")
    if override is not None:
        return override.institution_name
    insts = author.get("last_known_institutions", [])
    return insts[0].get("display_name") if insts else None


def _get_matching_inst(author: dict, institution_id: str, fallback_name: str) -> str:
    override = author.get("_affiliation_override")
    if override is not None and override.institution_id == institution_id:
        return override.institution_name
    for inst in author.get("last_known_institutions", []) or []:
        if inst.get("id") and _short_id(inst["id"]) == institution_id:
            return inst.get("display_name") or fallback_name
    return fallback_name


def _has_current_inst(author: dict, institution_id: str) -> bool:
    """Whether OpenAlex lists the institution among the author's last-known ones.

    ``last_known_institutions`` is a set-like list; OpenAlex does not document its
    first item as a unique primary affiliation.  This is a discovery predicate only:
    it is not evidence of current employment, and response payloads label it as such.
    """
    insts = author.get("last_known_institutions", []) or []
    return any(
        inst.get("id") and _short_id(inst["id"]) == institution_id
        for inst in insts
    )


def _affiliation_evidence(author: dict, institution_id: str) -> dict | None:
    override: AffiliationOverride | None = author.get("_affiliation_override")
    if override is not None and override.institution_id == institution_id:
        return {
            "institution_id": institution_id,
            "display_name": override.institution_name,
            "years": [],
            "source": override.source,
            "source_url": override.evidence_url,
            "reviewed_at": override.reviewed_at,
            "ror_url": override.institution_ror_url,
            "status": "verified_current",
            "verified_current": True,
        }
    for aff in author.get("affiliations", []) or []:
        inst = aff.get("institution") or {}
        if inst.get("id") and _short_id(inst["id"]) == institution_id:
            return {
                "institution_id": institution_id,
                "display_name": inst.get("display_name"),
                "years": sorted(aff.get("years") or [], reverse=True),
                "openalex_url": f"https://openalex.org/{institution_id}",
                "status": "openalex_last_known",
                "verified_current": False,
            }
    for inst in author.get("last_known_institutions", []) or []:
        if inst.get("id") and _short_id(inst["id"]) == institution_id:
            return {
                "institution_id": institution_id,
                "display_name": inst.get("display_name"),
                "years": [],
                "openalex_url": f"https://openalex.org/{institution_id}",
                "status": "openalex_last_known",
                "verified_current": False,
            }
    return None


def _is_rate_limited(exc: Exception) -> bool:
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429


def _rate_limit_message() -> str:
    if not _client.has_api_key:
        return (
            "OpenAlex search is temporarily unavailable. Open Advanced settings at the "
            "bottom of the menu to add your own free OpenAlex API key. Showing saved results when available."
        )
    return (
        "The configured OpenAlex API key was rejected or reached its limit. Open Advanced "
        "settings at the bottom of the menu to add your own free key. Showing saved results when available."
    )


def _override_action(override: AffiliationOverride) -> str:
    """Compatibility helper while keeping the reviewed data schema explicit."""
    return getattr(override, "action", "include")


async def _merge_affiliation_overrides(
    authors: list[dict],
    institution_id: str,
    effective_pool: int,
    excluded_author_ids: set[str] | None = None,
    fetch_timeout_s: float | None = None,
) -> list[dict]:
    """Apply exact-ID reviewed membership corrections and reserve their slots.

    Reviewed inclusions are placed before the citation-ranked OpenAlex slice so a
    low-citation correction cannot be discarded by the candidate cutoff.  Exclusions
    win, and every record is copied before private metadata is attached so the shared
    OpenAlex author cache is never mutated.
    """
    overrides = get_effective_affiliation_overrides(institution_id)
    excluded_ids = {
        entry.author_id for entry in overrides if _override_action(entry) == "exclude"
    }
    excluded_ids |= excluded_author_ids or set()
    included = {
        entry.author_id: entry
        for entry in overrides
        if _override_action(entry) == "include" and entry.author_id not in excluded_ids
    }

    openalex_by_id = {
        _short_id(author["id"]): author
        for author in authors
        if author.get("id") and _short_id(author["id"]) not in excluded_ids
    }
    missing_ids = [author_id for author_id in included if author_id not in openalex_by_id]
    if missing_ids:
        try:
            request = _client.get_authors_batch(missing_ids)
            fetched = (
                await asyncio.wait_for(request, timeout=fetch_timeout_s)
                if fetch_timeout_s is not None
                else await request
            )
        except Exception:
            fetched = []
        for author in fetched:
            if author.get("id"):
                openalex_by_id[_short_id(author["id"])] = author

    reserved: list[dict] = []
    for author_id, override in included.items():
        author = dict(openalex_by_id.get(author_id) or {
            "id": f"https://openalex.org/{author_id}",
            "display_name": override.display_name,
            "last_known_institutions": [],
            "affiliations": [],
            "works_count": 0,
            "cited_by_count": 0,
        })
        reserved.append(apply_reviewed_identity(author, override))

    regular = [
        dict(author)
        for author_id, author in openalex_by_id.items()
        if author_id not in included and _has_current_inst(author, institution_id)
    ]
    regular_slots = max(0, effective_pool - len(reserved))
    # Reviewed inclusions are correctness records, not ranking candidates; never
    # drop them merely because their count exceeds the performance-oriented pool.
    return reserved + regular[:regular_slots]


def _ranked_author_payload(
    author: dict,
    institution: dict,
    *,
    found: bool,
    hops: int | None,
    steps: list[dict],
    closest_origin_id: str | None,
    reachable_origin_count: int,
    path_verified: bool = False,
    evidence_quality: float = 0.0,
) -> dict:
    author_id = _short_id(author["id"])
    author_name = author.get("display_name", author_id)
    topics = []
    for topic in (author.get("topics") or author.get("x_concepts") or [])[:3]:
        if isinstance(topic, dict):
            name = topic.get("display_name") or topic.get("name")
        else:
            name = topic if isinstance(topic, str) else None
        if name:
            topics.append(name)
    similarity = float(author.get("_topic_similarity") or 0.0)
    return {
        "matched_institution": _get_matching_inst(
            author, institution["id"], institution["display_name"]
        ),
        "affiliation_evidence": _affiliation_evidence(author, institution["id"]),
        "author": {
            "id": author_id,
            "display_name": author_name,
            "institution": _get_inst(author),
            "works_count": author.get("works_count", 0),
            "cited_by_count": author.get("cited_by_count", 0),
            "metrics_scoped": bool(author.get("_verified_identity_scope")),
            "openalex_url": f"https://openalex.org/{author_id}",
            "orcid": author.get("orcid"),
            "topics": topics,
        },
        "found": found,
        "hops": hops,
        "verified_hops": hops if found and path_verified else None,
        "path_verified": bool(found and path_verified),
        "evidence_quality": evidence_quality if found and path_verified else 0.0,
        "topic_similarity": similarity,
        "steps": steps,
        "closest_origin_id": closest_origin_id,
        "reachable_origin_count": reachable_origin_count,
    }


def _profile_for_path(author: dict | None, author_id: str) -> dict:
    """Return an endpoint profile with reviewed identity evidence attached."""
    profile = apply_reviewed_identity(dict(author or {"id": author_id}))
    profile.setdefault("id", author_id)
    scope = get_verified_work_ids(author_id)
    if scope is not None:
        profile["work_ids"] = sorted(scope)
    return profile


def _orient_coauthor_edge(edge: dict, from_id: str, to_id: str) -> dict | None:
    """Orient exact work evidence along a displayed path hop."""
    from_id, to_id = _short_id(from_id), _short_id(to_id)
    left_id, right_id = _short_id(edge.get("left_id", "")), _short_id(edge.get("right_id", ""))
    if (left_id, right_id) == (from_id, to_id):
        return dict(edge)
    if (left_id, right_id) != (to_id, from_id):
        return None
    oriented = dict(edge)
    for suffix in ("id", "name", "authorship"):
        oriented[f"left_{suffix}"], oriented[f"right_{suffix}"] = (
            edge.get(f"right_{suffix}"),
            edge.get(f"left_{suffix}"),
        )
    return oriented


def _edge_in_reviewed_scopes(edge: dict, *author_ids: str) -> bool:
    work_id = _short_id(edge.get("work_id", ""))
    if not work_id:
        return False
    return all(
        scope is None or work_id in scope
        for scope in (get_verified_work_ids(_short_id(author_id)) for author_id in author_ids)
    )


def _edge_step(edge: dict) -> dict:
    work_id = _short_id(edge.get("work_id", ""))
    title = edge.get("title") or edge.get("label") or "(untitled)"
    return {
        "from_id": _short_id(edge.get("left_id", "")),
        "from_name": edge.get("left_name") or _short_id(edge.get("left_id", "")),
        "to_id": _short_id(edge.get("right_id", "")),
        "to_name": edge.get("right_name") or _short_id(edge.get("right_id", "")),
        "type": "coauthor",
        "label": title,
        "title": title,
        "direction": None,
        "work_id": work_id,
        "work_url": f"https://openalex.org/{work_id}",
        "publication_year": edge.get("publication_year"),
        "evidence_verified": True,
    }


def _edge_order(edge: dict) -> tuple:
    return (
        int(edge.get("author_count") or 10_000),
        -(int(edge.get("publication_year") or 0)),
        _short_id(edge.get("work_id", "")),
    )


async def _short_coauthor_paths(
    candidates: list[dict],
    origins: list[str],
    max_depth: int,
) -> tuple[dict[str, dict], int, bool]:
    """Find evidence-backed one/two-hop paths with bounded set joins.

    Group-by summaries are proposal indexes only. Every displayed hop is fetched
    from an exact OpenAlex work, checked against reviewed work scopes, and tested
    for endpoint/intermediate identity continuity before it can become a result.
    """
    if max_depth < 1 or not candidates or not origins:
        return {}, 0, True

    origins = list(dict.fromkeys(_short_id(value) for value in origins if value))
    candidate_by_id = {
        _short_id(author["id"]): author
        for author in candidates
        if author.get("id") and _short_id(author["id"]) not in origins
    }
    candidate_ids = sorted(candidate_by_id)
    if not candidate_ids or not origins:
        return {}, 0, True

    errors = 0
    complete = True
    origin_scopes = {origin_id: get_verified_work_ids(origin_id) for origin_id in origins}

    # These three independent operations overlap: author metadata validates the
    # endpoints, summaries propose bridges, and the set join proves direct edges.
    summaries_task = asyncio.gather(*[
        _client.get_coauthor_summary(
            origin_id, verified_work_ids=origin_scopes[origin_id]
        )
        for origin_id in origins
    ], return_exceptions=True)
    initial = await asyncio.gather(
        summaries_task,
        _client.get_authors_batch(origins),
        _client.get_coauthor_links(candidate_ids, origins),
        return_exceptions=True,
    )
    summary_results, origin_records_result, direct_links_result = initial

    origin_summaries: dict[str, dict[str, dict]] = {}
    for origin_id, result in zip(origins, summary_results):
        if isinstance(result, Exception) or not isinstance(result, dict):
            errors += 1
            complete = False
            continue
        origin_summaries[origin_id] = result
        complete = complete and bool(getattr(result, "complete", True))

    origin_records: list[dict] = []
    if isinstance(origin_records_result, Exception) or not isinstance(origin_records_result, list):
        errors += 1
        complete = False
    else:
        origin_records = origin_records_result
    origin_record_by_id = {
        _short_id(record["id"]): record
        for record in origin_records
        if isinstance(record, dict) and record.get("id")
    }
    if set(origins) - set(origin_record_by_id):
        complete = False

    origin_profiles = {
        # Summary coauthors are proposals from the same potentially merged
        # OpenAlex record, not independent identity evidence.  Feeding them
        # back into endpoint validation would make a bogus edge self-validate.
        origin_id: _profile_for_path(origin_record_by_id.get(origin_id), origin_id)
        for origin_id in origins
    }
    candidate_profiles = {
        candidate_id: _profile_for_path(author, candidate_id)
        for candidate_id, author in candidate_by_id.items()
    }

    # candidate id -> origin id -> best verified path
    paths: dict[str, dict[str, dict]] = {}
    origin_order = {origin_id: index for index, origin_id in enumerate(origins)}

    def keep_path(candidate_id: str, origin_id: str, edges: list[dict]) -> None:
        steps = [_edge_step(edge) for edge in edges]
        evidence_key = tuple(_edge_order(edge) for edge in edges)
        proposed = {
            "hops": len(steps),
            "steps": steps,
            "evidence_key": evidence_key,
        }
        current = paths.setdefault(candidate_id, {}).get(origin_id)
        if current is None or (proposed["hops"], evidence_key) < (
            current["hops"], current["evidence_key"]
        ):
            paths[candidate_id][origin_id] = proposed

    if isinstance(direct_links_result, Exception) or not isinstance(direct_links_result, list):
        direct_links: list[dict] = []
        errors += 1
        complete = False
    else:
        direct_links = direct_links_result
        complete = complete and bool(getattr(direct_links_result, "complete", True))

    for raw_edge in sorted(
        (edge for edge in direct_links if isinstance(edge, dict)), key=_edge_order
    ):
        for candidate_id in (
            _short_id(raw_edge.get("left_id", "")),
            _short_id(raw_edge.get("right_id", "")),
        ):
            if candidate_id not in candidate_by_id:
                continue
            for origin_id in origins:
                edge = _orient_coauthor_edge(raw_edge, candidate_id, origin_id)
                if edge is None or not _edge_in_reviewed_scopes(edge, candidate_id, origin_id):
                    continue
                candidate_check = evaluate_edge_profile_compatibility(
                    edge, candidate_profiles[candidate_id], candidate_id
                )
                origin_check = evaluate_edge_profile_compatibility(
                    edge, origin_profiles[origin_id], origin_id
                )
                if candidate_check["compatible"] and origin_check["compatible"]:
                    keep_path(candidate_id, origin_id, [edge])

    if max_depth >= 2 and origin_summaries:
        bridge_origins: dict[str, set[str]] = {}
        bridge_strength: dict[str, int] = {}
        for origin_id, summary in origin_summaries.items():
            for bridge_id, proposal in summary.items():
                bridge_id = _short_id(bridge_id)
                if not bridge_id or bridge_id == origin_id or not isinstance(proposal, dict):
                    continue
                bridge_origins.setdefault(bridge_id, set()).add(origin_id)
                bridge_strength[bridge_id] = max(
                    bridge_strength.get(bridge_id, 0),
                    int(proposal.get("works_count") or 0),
                )
        all_bridge_count = len(bridge_origins)
        bridge_ids = sorted(
            bridge_origins,
            key=lambda bridge_id: (
                -bridge_strength[bridge_id],
                -len(bridge_origins[bridge_id]),
                bridge_id,
            ),
        )[:RANK_FAST_BRIDGE_MAX]
        if all_bridge_count > len(bridge_ids):
            complete = False

        try:
            candidate_bridge_links = await _client.get_coauthor_links(candidate_ids, bridge_ids)
            if not isinstance(candidate_bridge_links, list):
                raise TypeError("coauthor link query did not return a list")
            complete = complete and bool(getattr(candidate_bridge_links, "complete", True))
        except Exception:
            candidate_bridge_links = []
            errors += 1
            complete = False

        candidate_edges_by_bridge: dict[str, list[tuple[str, dict]]] = {}
        bridge_set = set(bridge_ids)
        for raw_edge in sorted(
            (edge for edge in candidate_bridge_links if isinstance(edge, dict)),
            key=_edge_order,
        ):
            for candidate_id in (
                _short_id(raw_edge.get("left_id", "")),
                _short_id(raw_edge.get("right_id", "")),
            ):
                if candidate_id not in candidate_by_id:
                    continue
                other_id = (
                    _short_id(raw_edge.get("right_id", ""))
                    if _short_id(raw_edge.get("left_id", "")) == candidate_id
                    else _short_id(raw_edge.get("left_id", ""))
                )
                if other_id not in bridge_set or other_id == candidate_id:
                    continue
                edge = _orient_coauthor_edge(raw_edge, candidate_id, other_id)
                if edge is None or not _edge_in_reviewed_scopes(edge, candidate_id):
                    continue
                check = evaluate_edge_profile_compatibility(
                    edge, candidate_profiles[candidate_id], candidate_id
                )
                if check["compatible"]:
                    candidate_edges_by_bridge.setdefault(other_id, []).append(
                        (candidate_id, edge)
                    )

        used_bridge_ids = sorted(candidate_edges_by_bridge)
        try:
            bridge_origin_links = await _client.get_coauthor_links(used_bridge_ids, origins)
            if not isinstance(bridge_origin_links, list):
                raise TypeError("coauthor link query did not return a list")
            complete = complete and bool(getattr(bridge_origin_links, "complete", True))
        except Exception:
            bridge_origin_links = []
            if used_bridge_ids:
                errors += 1
                complete = False

        origin_edges_by_bridge: dict[str, list[tuple[str, dict]]] = {}
        for raw_edge in sorted(
            (edge for edge in bridge_origin_links if isinstance(edge, dict)),
            key=_edge_order,
        ):
            for bridge_id in used_bridge_ids:
                for origin_id in bridge_origins.get(bridge_id, set()):
                    edge = _orient_coauthor_edge(raw_edge, bridge_id, origin_id)
                    if edge is None or not _edge_in_reviewed_scopes(edge, origin_id):
                        continue
                    check = evaluate_edge_profile_compatibility(
                        edge, origin_profiles[origin_id], origin_id
                    )
                    if check["compatible"]:
                        origin_edges_by_bridge.setdefault(bridge_id, []).append(
                            (origin_id, edge)
                        )

        for bridge_id, candidate_edges in candidate_edges_by_bridge.items():
            for candidate_id, candidate_edge in candidate_edges:
                for origin_id, origin_edge in origin_edges_by_bridge.get(bridge_id, []):
                    continuity = evaluate_intermediate_coherence(
                        candidate_edge, origin_edge, bridge_id
                    )
                    if continuity["compatible"]:
                        keep_path(candidate_id, origin_id, [candidate_edge, origin_edge])

    result: dict[str, dict] = {}
    for candidate_id, by_origin in paths.items():
        closest_origin_id, best = min(
            by_origin.items(),
            key=lambda item: (
                item[1]["hops"],
                item[1]["evidence_key"],
                origin_order.get(item[0], len(origin_order)),
            ),
        )
        result[candidate_id] = {
            "found": True,
            "hops": best["hops"],
            "steps": best["steps"],
            "closest_origin_id": closest_origin_id,
            "reachable_origin_count": len(by_origin),
            "path_verified": True,
            "evidence_quality": 1.0,
        }
    return result, errors, complete


async def _collect_path(
    backend: GraphBackend,
    from_id: str,
    from_name: str,
    to_id: str,
    max_depth: int = 6,
) -> dict:
    """Run bidirectional BFS; return the found path's nodes/edges plus hop count.

    The returned dict carries the graph elements as well as the degree-of-separation
    metadata (found, hops, and both endpoint names) so the caller can emit a `path`
    SSE event without re-deriving any of it.
    """
    if _is_work_id(to_id):
        to_obj = await _client.get_work(to_id)
        to_name = to_obj.get("title", to_id)
    else:
        try:
            to_obj = apply_reviewed_identity(await _client.get_author(to_id))
        except Exception as exc:
            if not _is_rate_limited(exc):
                raise
            to_obj = _local_index.author_record(to_id)
        to_obj = apply_reviewed_identity(to_obj)
        to_name = to_obj.get("display_name", to_id)

    nodes: list[dict] = []
    edges: list[dict] = []
    steps: list[dict] = []   # ordered hops along the path (names + paper/label)
    found = False
    hops: int | None = None
    search_complete = True

    async for event in find_path(backend, from_id, from_name, to_id, to_name, max_depth=max_depth):
        if event.get("type") == "result":
            search_complete = bool(event.get("search_complete", True))
        if event.get("type") == "result" and event.get("found"):
            found = True
            hops = event.get("hops")
            path = event["path"]
            for i, step in enumerate(path):
                sid = step["author_id"]
                is_endpoint = sid in (from_id, to_id)
                # A work can only ever be a path endpoint (its only edges are to
                # its own authors/citers, never to another work) — never a mid-path node.
                node_type = "work" if _is_work_id(sid) else ("origin" if is_endpoint else "path")
                nodes.append({
                    "id": sid,
                    "name": step["author_name"],
                    "institution": None,
                    "works_count": 0,
                    "cited_by_count": 0,
                    "publication_year": None,
                    "type": node_type,
                    "depth": 0,
                })
                if i < len(path) - 1 and step.get("connection_to_next"):
                    nxt = path[i + 1]
                    edges.append({
                        "source": step["author_id"],
                        "target": nxt["author_id"],
                        "type": step["connection_to_next"],
                        "label": step.get("label", ""),
                        "direction": step.get("direction"),
                    })
                    steps.append({
                        "from_id": step["author_id"],
                        "from_name": step["author_name"],
                        "to_id": nxt["author_id"],
                        "to_name": nxt["author_name"],
                        "type": step["connection_to_next"],
                        "label": step.get("label", ""),
                        "direction": step.get("direction"),
                    })

    # The BFS only knows ids + names, so path nodes would otherwise render as
    # "0 works · 0 citations" with no institution. Backfill real metadata in a
    # single batched lookup before returning — authors via get_authors_batch,
    # and any work-typed endpoint (at most from_id/to_id, never a mid-path node)
    # via get_work, reusing the to_id fetch already done above where possible.
    author_node_ids = [n["id"] for n in nodes if n["type"] != "work"]
    if author_node_ids:
        try:
            authors = await _client.get_authors_batch(author_node_ids)
        except Exception as exc:
            if not _is_rate_limited(exc):
                raise
            authors = [
                _local_index.author_record(n["id"], n["name"])
                for n in nodes
                if n["type"] != "work"
            ]
        meta = {
            _short_id(author["id"]): apply_reviewed_identity(author)
            for author in authors
        }
        for n in nodes:
            a = meta.get(n["id"])
            if a:
                n["institution"] = _get_inst(a)
                n["works_count"] = a.get("works_count", 0)
                n["cited_by_count"] = a.get("cited_by_count", 0)

    async def _work_details(n: dict) -> dict:
        return to_obj if n["id"] == to_id else await _client.get_work(n["id"])

    work_nodes = [n for n in nodes if n["type"] == "work"]
    if work_nodes:
        details = await asyncio.gather(*[_work_details(n) for n in work_nodes])
        for n, w in zip(work_nodes, details):
            n["cited_by_count"] = w.get("cited_by_count", 0)
            n["publication_year"] = w.get("publication_year")
            n["name"] = w.get("title", n["name"])

    return {
        "nodes": nodes,
        "edges": edges,
        "steps": steps,
        "found": found,
        "hops": hops,
        "from_id": from_id,
        "from_name": from_name,
        "to_id": to_id,
        "to_name": to_name,
        "search_complete": search_complete,
    }


async def _collect_path_proposal(
    backend: GraphBackend,
    from_id: str,
    from_name: str,
    to_id: str,
    max_depth: int = 6,
    *,
    to_name: str | None = None,
) -> dict:
    """Collect only the BFS IDs needed for exact path verification.

    Institution ranking discards the hydrated graph payload produced by
    ``_collect_path``.  Keeping this proposal collector metadata-free avoids an
    extra author request for every candidate/origin path that BFS happens to find.
    """
    resolved_to_name = to_name or to_id
    async for event in find_path(
        backend,
        from_id,
        from_name,
        to_id,
        resolved_to_name,
        max_depth=max_depth,
    ):
        if event.get("type") != "result":
            continue
        if not event.get("found"):
            return {
                "found": False,
                "hops": None,
                "steps": [],
                "search_complete": bool(event.get("search_complete", True)),
            }
        path = event.get("path") or []
        steps = []
        for index, step in enumerate(path[:-1]):
            next_step = path[index + 1]
            if not step.get("connection_to_next"):
                continue
            steps.append({
                "from_id": step["author_id"],
                "from_name": step.get("author_name") or step["author_id"],
                "to_id": next_step["author_id"],
                "to_name": next_step.get("author_name") or next_step["author_id"],
                "type": step["connection_to_next"],
                "label": step.get("label", ""),
                "direction": step.get("direction"),
            })
        return {
            "found": True,
            "hops": event.get("hops"),
            "steps": steps,
            "from_id": from_id,
            "from_name": from_name,
            "to_id": to_id,
            "to_name": resolved_to_name,
            "search_complete": bool(event.get("search_complete", True)),
        }
    return {
        "found": False,
        "hops": None,
        "steps": [],
        "search_complete": False,
    }


async def _verify_deep_coauthor_path(
    path: dict,
    candidate_profile: dict,
    origin_profile: dict,
) -> dict | None:
    """Replace a BFS proposal with a coherent chain of exact publication edges."""
    raw_steps = path.get("steps") or []
    if not path.get("found") or not raw_steps:
        return None
    if any(
        step.get("type") != "coauthor" or not step.get("from_id") or not step.get("to_id")
        for step in raw_steps
    ):
        return None

    left_ids = list(dict.fromkeys(_short_id(step["from_id"]) for step in raw_steps))
    right_ids = list(dict.fromkeys(_short_id(step["to_id"]) for step in raw_steps))
    links = await _client.get_coauthor_links(left_ids, right_ids)
    if not isinstance(links, list):
        raise TypeError("coauthor evidence query did not return a list")

    options: list[list[dict]] = []
    for index, step in enumerate(raw_steps):
        from_id, to_id = _short_id(step["from_id"]), _short_id(step["to_id"])
        hop_options: list[dict] = []
        seen: set[tuple[str, str, str]] = set()
        for raw_edge in links:
            if not isinstance(raw_edge, dict):
                continue
            edge = _orient_coauthor_edge(raw_edge, from_id, to_id)
            if edge is None or not _edge_in_reviewed_scopes(edge, from_id, to_id):
                continue
            dedup_key = (from_id, to_id, _short_id(edge.get("work_id", "")))
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            if index == 0 and not evaluate_edge_profile_compatibility(
                edge, candidate_profile, from_id
            )["compatible"]:
                continue
            if index == len(raw_steps) - 1 and not evaluate_edge_profile_compatibility(
                edge, origin_profile, to_id
            )["compatible"]:
                continue
            hop_options.append(edge)
        hop_options.sort(key=_edge_order)
        if not hop_options:
            return None
        # A few representative works per hop are enough to find a coherent chain;
        # bounding here prevents combinatorial work on large team-science records.
        options.append(hop_options[:12])

    chains: list[list[dict]] = [[edge] for edge in options[0]]
    for hop_index in range(1, len(options)):
        intermediate_id = _short_id(raw_steps[hop_index]["from_id"])
        next_chains: list[list[dict]] = []
        for chain in chains:
            for edge in options[hop_index]:
                if evaluate_intermediate_coherence(
                    chain[-1], edge, intermediate_id
                )["compatible"]:
                    next_chains.append([*chain, edge])
        if not next_chains:
            return None
        next_chains.sort(key=lambda chain: tuple(_edge_order(edge) for edge in chain))
        chains = next_chains[:48]

    best = min(chains, key=lambda chain: tuple(_edge_order(edge) for edge in chain))
    verified = dict(path)
    verified["steps"] = [_edge_step(edge) for edge in best]
    verified["path_verified"] = True
    verified["evidence_quality"] = 1.0
    verified["evidence_query_complete"] = bool(getattr(links, "complete", True))
    return verified


@app.api_route("/health", methods=["GET", "HEAD"])
async def health():
    store = "supabase" if isinstance(_store, SupabaseNeighborStore) else "json"
    return {"status": "ok", "store": store}


@app.get("/api/openalex-key")
async def openalex_key_status():
    return {"configured": _client.has_api_key}


@app.post("/api/openalex-key")
async def set_openalex_key(request: Request):
    api_key = request.query_params.get("api_key", "").strip()
    if not api_key:
        body = await request.body()
        if body:
            try:
                data = json.loads(body)
                api_key = str(data.get("api_key", "")).strip()
            except json.JSONDecodeError:
                api_key = body.decode("utf-8", errors="ignore").strip()
    _client.set_api_key(api_key)
    return {"configured": _client.has_api_key}


@app.get("/api/authors", response_model=PaginatedAuthors)
async def search_authors(
    q: str = Query(..., min_length=2),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=20, ge=1, le=50),
):
    try:
        results, total = await _client.search_authors(q, page=page, per_page=per_page)
    except Exception as exc:
        if _is_rate_limited(exc):
            results, total = _local_index.search_authors(q, page=page, per_page=per_page)
            total_pages = max(1, math.ceil(total / per_page))
            return PaginatedAuthors(
                results=results, page=page, per_page=per_page,
                total=total, total_pages=total_pages,
                message=_rate_limit_message(),
            )
        raise
    total_pages = max(1, math.ceil(total / per_page))
    return PaginatedAuthors(
        results=results, page=page, per_page=per_page,
        total=total, total_pages=total_pages,
    )


@app.get("/api/works", response_model=PaginatedWorks)
async def search_works(
    q: str = Query(..., min_length=2),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=20, ge=1, le=50),
):
    try:
        results, total = await _client.search_works(q, page=page, per_page=per_page)
    except Exception as exc:
        if _is_rate_limited(exc):
            return PaginatedWorks(
                results=[], page=page, per_page=per_page,
                total=0, total_pages=1,
            ).model_dump() | {"message": _rate_limit_message()}
        raise
    total_pages = max(1, math.ceil(total / per_page))
    return PaginatedWorks(
        results=results, page=page, per_page=per_page,
        total=total, total_pages=total_pages,
    )


@app.get("/api/institutions")
async def search_institutions(
    q: str = Query(..., min_length=2),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=20, ge=1, le=50),
):
    try:
        results, total = await _client.search_institutions(q, page=page, per_page=per_page)
    except Exception as exc:
        if _is_rate_limited(exc):
            results, total = _local_index.search_institutions(q, page=page, per_page=per_page)
            total_pages = max(1, math.ceil(total / per_page))
            return {
                "results": results,
                "page": page,
                "per_page": per_page,
                "total": total,
                "total_pages": total_pages,
                "message": _rate_limit_message(),
            }
        raise
    total_pages = max(1, math.ceil(total / per_page))
    return {
        "results": results,
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
    }


@app.get("/api/institution-suggestions")
async def institution_rank(
    institution: str | None = Query(default=None, min_length=2),
    institution_id: str | None = Query(default=None),
    origin_ids: list[str] = Query(default=[]),
    limit: int = Query(default=10, ge=1, le=20),
    candidate_pool: int = Query(default=80, ge=1, le=120),
    max_depth: int = Query(default=4, ge=1, le=6),
    diagnostics: bool = Query(default=False),
):
    """Suggest institution-associated researchers near any author in the graph.

    Discovery distance deliberately uses co-authorship only. Candidate discovery
    balances topic and citation lanes; a result is shown only after every displayed
    hop has exact publication evidence and passes identity-continuity checks.
    """
    loop = asyncio.get_running_loop()
    search_started = loop.time()
    total_deadline = search_started + RANK_TOTAL_TIMEOUT_S
    rank_errors = 0
    rank_timeouts = 0

    def total_remaining() -> float:
        return max(0.0, total_deadline - loop.time())

    if institution_id:
        institution_id = _short_id(institution_id)
        inst = {
            "id": institution_id,
            "display_name": institution or institution_id,
            "country_code": None,
            "works_count": 0,
            "cited_by_count": 0,
        }
    else:
        if not institution:
            return {"institution": None, "results": [], "message": "Select a home institution."}
        try:
            lookup_timeout = min(
                RANK_INSTITUTION_LOOKUP_TIMEOUT_S, total_remaining()
            )
            if lookup_timeout <= 0:
                raise asyncio.TimeoutError
            institution_results, _ = await asyncio.wait_for(
                _client.search_institutions(institution, page=1, per_page=1),
                timeout=lookup_timeout,
            )
        except asyncio.TimeoutError:
            rank_timeouts += 1
            institution_results, _ = _local_index.search_institutions(
                institution, page=1, per_page=1
            )
        except Exception as exc:
            if not _is_rate_limited(exc):
                raise
            institution_results, _ = _local_index.search_institutions(institution, page=1, per_page=1)
        if not institution_results:
            return {
                "institution": None,
                "results": [],
                "message": f"No institution found for {institution!r}",
            }
        inst = institution_results[0]
    inst = {**inst, "id": _short_id(inst["id"])}

    all_origins = list(dict.fromkeys(
        _short_id(origin_id)
        for origin_id in origin_ids
        if origin_id and not _is_work_id(origin_id)
    ))
    omitted_origin_count = max(0, len(all_origins) - RANK_ORIGIN_MAX)
    origins = all_origins[:RANK_ORIGIN_MAX]
    if not origins:
        return {"institution": inst, "results": [], "message": "Add a researcher you like to the graph first."}

    effective_pool_cap = min(candidate_pool, RANK_EFFECTIVE_POOL_MAX)
    fetch_limit = min(RANK_FETCH_LIMIT_MAX, effective_pool_cap)
    candidate_source_message = None
    candidate_discovery_complete = True
    discovery_deadline = min(
        total_deadline, loop.time() + RANK_DISCOVERY_TIMEOUT_S
    )

    def discovery_remaining() -> float:
        return max(0.0, discovery_deadline - loop.time())

    # Citation discovery does not depend on the origin profiles, so overlap it
    # with the metadata request that supplies topic IDs and identity evidence.
    citation_task = asyncio.create_task(_client.get_institution_authors(
        inst["id"], limit=fetch_limit, sort="cited_by_count:desc"
    ))
    try:
        origin_timeout = min(
            RANK_ORIGIN_PROFILE_TIMEOUT_S,
            discovery_remaining(),
            total_remaining(),
        )
        if origin_timeout <= 0:
            raise asyncio.TimeoutError
        origin_records = await asyncio.wait_for(
            _client.get_authors_batch(origins), timeout=origin_timeout
        )
        if not isinstance(origin_records, list):
            raise TypeError("author metadata query did not return a list")
    except asyncio.TimeoutError:
        origin_records = []
        rank_timeouts += 1
        candidate_discovery_complete = False
    except Exception:
        origin_records = []
        rank_errors += 1
        candidate_discovery_complete = False
    origin_by_id = {
        _short_id(record["id"]): apply_reviewed_identity(record)
        for record in origin_records
        if isinstance(record, dict) and record.get("id")
    }
    if set(origins) - set(origin_by_id):
        candidate_discovery_complete = False
    origin_profiles = [
        _profile_for_path(origin_by_id.get(origin_id), origin_id)
        for origin_id in origins
    ]
    topic_ids: list[str] = []
    subfield_ids: list[str] = []
    field_ids: list[str] = []
    for profile in origin_profiles:
        metadata = extract_topic_metadata(profile)
        for topic_id in sorted(metadata.topic_ids):
            if topic_id not in topic_ids:
                topic_ids.append(topic_id)
            if len(topic_ids) >= RANK_TOPIC_ID_MAX:
                break
        for subfield_id in sorted(metadata.subfield_ids):
            if subfield_id not in subfield_ids:
                subfield_ids.append(subfield_id)
            if len(subfield_ids) >= RANK_SUBFIELD_ID_MAX:
                break
        for field_id in sorted(metadata.field_ids):
            if field_id not in field_ids:
                field_ids.append(field_id)
            if len(field_ids) >= RANK_FIELD_ID_MAX:
                break
        if len(topic_ids) >= RANK_TOPIC_ID_MAX:
            # Continue only while hierarchy lists still need values.
            if (
                len(subfield_ids) >= RANK_SUBFIELD_ID_MAX
                and len(field_ids) >= RANK_FIELD_ID_MAX
            ):
                break

    topic_tasks: list[asyncio.Task] = []
    if topic_ids:
        topic_tasks.append(asyncio.create_task(_client.get_institution_authors_by_topics(
            inst["id"], topic_ids, limit=RANK_TOPIC_FETCH_LIMIT
        )))
    if subfield_ids or field_ids:
        topic_tasks.append(asyncio.create_task(
            _client.get_institution_authors_by_hierarchy(
                inst["id"],
                subfield_ids=subfield_ids,
                field_ids=field_ids,
                limit=RANK_HIERARCHY_FETCH_LIMIT,
            )
        ))

    try:
        citation_timeout = min(discovery_remaining(), total_remaining())
        if citation_timeout <= 0:
            raise asyncio.TimeoutError
        citation_candidates = await asyncio.wait_for(
            citation_task, timeout=citation_timeout
        )
        if not isinstance(citation_candidates, list):
            raise TypeError("institution author query did not return a list")
    except asyncio.TimeoutError:
        if not citation_task.done():
            citation_task.cancel()
            await asyncio.gather(citation_task, return_exceptions=True)
        citation_candidates = []
        rank_timeouts += 1
        candidate_discovery_complete = False
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code != 429:
            citation_candidates = []
            rank_errors += 1
            candidate_discovery_complete = False
        else:
            citation_candidates = _local_index.institution_authors(
                inst["id"], inst.get("display_name"), limit=fetch_limit
            )
            candidate_source_message = _rate_limit_message()
            candidate_discovery_complete = False
    except Exception:
        citation_candidates = []
        rank_errors += 1
        candidate_discovery_complete = False

    if not topic_tasks:
        topic_candidates: list[dict] = []
    else:
        topic_candidates = []
        topic_timeout = min(discovery_remaining(), total_remaining())
        if topic_timeout > 0:
            done, pending = await asyncio.wait(topic_tasks, timeout=topic_timeout)
        else:
            done, pending = set(), set(topic_tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
            rank_timeouts += len(pending)
            candidate_discovery_complete = False
        topic_results = []
        for task in done:
            if task.cancelled():
                continue
            try:
                topic_results.append(task.result())
            except Exception as exc:
                topic_results.append(exc)
        for result in topic_results:
            if isinstance(result, Exception) or not isinstance(result, list):
                rank_errors += 1
                candidate_discovery_complete = False
                if isinstance(result, Exception) and _is_rate_limited(result):
                    candidate_source_message = _rate_limit_message()
                continue
            topic_candidates.extend(result)

    overrides = get_effective_affiliation_overrides(inst["id"])
    excluded_ids = {
        entry.author_id for entry in overrides if _override_action(entry) == "exclude"
    } | set(origins)
    reviewed_candidates = await _merge_affiliation_overrides(
        [],
        inst["id"],
        0,
        excluded_author_ids=set(origins),
        fetch_timeout_s=min(
            RANK_AFFILIATION_FETCH_TIMEOUT_S,
            max(0.001, total_remaining()),
        ),
    )

    def valid_discovered_lane(values: list[dict]) -> list[dict]:
        return [
            dict(author)
            for author in values
            if (
                isinstance(author, dict)
                and author.get("id")
                and _short_id(author["id"]) not in excluded_ids
                and _has_current_inst(author, inst["id"])
            )
        ]

    citation_candidates = valid_discovered_lane(citation_candidates)
    topic_candidates = valid_discovered_lane(topic_candidates)
    candidates = merge_balanced_candidate_pool(
        reviewed_candidates,
        topic_candidates,
        citation_candidates,
        origins=origin_profiles,
        origin_ids=origins,
        pool_size=effective_pool_cap,
        topical_fraction=0.4,
    )
    started_count = len(candidates)
    candidate_source_counts = {
        source: sum(
            source in set(author.get("_candidate_sources") or ())
            for author in candidates
        )
        for source in ("reviewed", "topic", "citation")
    }

    # Resolve every candidate's one/two-hop possibilities in shared set joins.
    # Group-by data proposes bridges; exact works alone are allowed into results.
    short_scan_returned = False
    try:
        short_timeout = min(RANK_FAST_TIMEOUT_S, total_remaining())
        if short_timeout <= 0:
            raise asyncio.TimeoutError
        short_paths, short_errors, short_complete = await asyncio.wait_for(
            _short_coauthor_paths(candidates, origins, max_depth),
            timeout=short_timeout,
        )
        short_scan_returned = True
        rank_errors += short_errors
    except asyncio.TimeoutError:
        short_paths = {}
        short_complete = False
        rank_timeouts += 1
    except Exception:
        short_paths = {}
        short_complete = False
        rank_errors += 1
    short_checked_count = started_count if candidates and short_scan_returned else 0

    short_ranked = []
    for author in candidates:
        author_id = _short_id(author["id"])
        path = short_paths.get(author_id)
        if path is None or author_id in origins:
            continue
        short_ranked.append(_ranked_author_payload(
            author,
            inst,
            found=True,
            hops=path["hops"],
            steps=path["steps"],
            closest_origin_id=path["closest_origin_id"],
            reachable_origin_count=path["reachable_origin_count"],
            path_verified=True,
            evidence_quality=float(path.get("evidence_quality") or 1.0),
        ))

    # Do not stop after an arbitrary two results. When the result page is not full,
    # spend the remaining budget on a deterministic, topic-first unmatched subset.
    matched_ids = set(short_paths)
    reviewed_ids = {
        _short_id(author["id"])
        for author in candidates
        if author.get("_reviewed_candidate")
    }
    deeper_shortlist = []
    if max_depth > 2 and len(short_ranked) < limit:
        deeper_shortlist = choose_deeper_search_shortlist(
            candidates,
            origins=origin_profiles,
            origin_ids=origins,
            matched_ids=matched_ids,
            reviewed_ids=reviewed_ids,
            limit=RANK_DEEP_SHORTLIST_MAX,
        )
    deeper_search_started_count = len(deeper_shortlist)
    unmatched_count = max(0, started_count - len(matched_ids))
    deeper_search_skipped_count = max(
        0, unmatched_count - deeper_search_started_count
    )

    deeper_ranked: list[dict] = []
    deeper_search_completed_count = 0
    deep_evidence_complete = True
    if deeper_shortlist:
        base_backend = _make_backend({"coauthor"})
        seed_ids = list(dict.fromkeys([
            *origins,
            *(_short_id(author["id"]) for author in deeper_shortlist),
        ]))
        seed_semaphore = asyncio.Semaphore(RANK_PATH_CONCURRENCY)

        async def fetch_seed(author_id: str):
            async with seed_semaphore:
                return await _client.get_coauthor_summary(
                    author_id,
                    verified_work_ids=get_verified_work_ids(author_id),
                )

        seed_tasks = {
            asyncio.create_task(fetch_seed(author_id)): author_id
            for author_id in seed_ids
        }
        seed_timeout = total_remaining()
        if seed_tasks and seed_timeout > 0:
            seed_done, seed_pending = await asyncio.wait(
                seed_tasks, timeout=seed_timeout
            )
        else:
            seed_done, seed_pending = set(), set(seed_tasks)
        for task in seed_pending:
            task.cancel()
        if seed_pending:
            await asyncio.gather(*seed_pending, return_exceptions=True)
            rank_timeouts += len(seed_pending)
            deep_evidence_complete = False
        seed_results_by_id: dict[str, object] = {}
        for task in seed_done:
            author_id = seed_tasks[task]
            try:
                seed_results_by_id[author_id] = task.result()
            except Exception as exc:
                seed_results_by_id[author_id] = exc
        seed_rings: dict[str, list[Connection]] = {}
        complete_seed_ids: set[str] = set()
        for author_id in seed_ids:
            summary = seed_results_by_id.get(author_id, asyncio.TimeoutError())
            if isinstance(summary, Exception) or not isinstance(summary, dict):
                if author_id in seed_results_by_id:
                    rank_errors += 1
                deep_evidence_complete = False
                continue
            summary_complete = bool(getattr(summary, "complete", True))
            deep_evidence_complete = deep_evidence_complete and summary_complete
            if summary_complete:
                complete_seed_ids.add(author_id)
            seed_rings[author_id] = [
                Connection(
                    target_author_id=_short_id(coauthor_id),
                    target_name=proposal.get("name") or _short_id(coauthor_id),
                    connection_type="coauthor",
                    label=proposal.get("label") or "",
                )
                for coauthor_id, proposal in summary.items()
                if (
                    isinstance(proposal, dict)
                    and _short_id(coauthor_id)
                    and _short_id(coauthor_id) != author_id
                )
            ]
        backend = _SummarySeededBackend(
            base_backend,
            seed_rings,
            complete_seed_ids=complete_seed_ids,
        )
        semaphore = asyncio.Semaphore(RANK_CONCURRENCY)
        path_semaphore = asyncio.Semaphore(RANK_PATH_CONCURRENCY)

        async def rank_candidate(author: dict) -> tuple[dict, int, bool]:
            author_id = _short_id(author["id"])
            author_name = author.get("display_name", author_id)

            async def collect_origin(origin_id: str) -> dict:
                origin_name = (origin_by_id.get(origin_id) or {}).get(
                    "display_name", origin_id
                )
                async with path_semaphore:
                    return await _collect_path_proposal(
                        backend,
                        author_id,
                        author_name,
                        origin_id,
                        max_depth=max_depth,
                        to_name=origin_name,
                    )

            paths = await asyncio.gather(*[
                collect_origin(origin_id)
                for origin_id in origins
            ], return_exceptions=True)
            local_errors = sum(isinstance(path, Exception) for path in paths)
            verified_paths: list[tuple[str, dict]] = []
            evidence_complete = True
            for origin_id, raw_path in zip(origins, paths):
                if isinstance(raw_path, Exception):
                    evidence_complete = False
                    continue
                evidence_complete = evidence_complete and bool(
                    raw_path.get("search_complete", True)
                )
                if not raw_path.get("found"):
                    continue
                try:
                    verified = await _verify_deep_coauthor_path(
                        raw_path,
                        _profile_for_path(author, author_id),
                        _profile_for_path(origin_by_id.get(origin_id), origin_id),
                    )
                except Exception:
                    local_errors += 1
                    evidence_complete = False
                    continue
                if verified is not None:
                    verified_paths.append((origin_id, verified))
                    evidence_complete = (
                        evidence_complete
                        and bool(verified.get("evidence_query_complete", True))
                    )
            if verified_paths:
                closest_id, result = min(
                    verified_paths,
                    key=lambda pair: (
                        pair[1]["hops"],
                        tuple(step.get("work_id", "") for step in pair[1]["steps"]),
                    ),
                )
            else:
                closest_id = None
                result = {"found": False, "hops": None, "steps": []}
            payload = _ranked_author_payload(
                author,
                inst,
                found=result["found"],
                hops=result["hops"],
                steps=result["steps"],
                closest_origin_id=closest_id,
                reachable_origin_count=len(verified_paths),
                path_verified=bool(result.get("path_verified")),
                evidence_quality=float(result.get("evidence_quality") or 0.0),
            )
            payload["_search_complete"] = local_errors == 0 and evidence_complete
            return payload, local_errors, evidence_complete

        async def rank_candidate_safely(author: dict) -> dict | None:
            nonlocal rank_errors, rank_timeouts, deep_evidence_complete
            async with semaphore:
                try:
                    payload, local_errors, evidence_complete = await asyncio.wait_for(
                        rank_candidate(author),
                        timeout=RANK_CANDIDATE_TIMEOUT_S,
                    )
                    rank_errors += local_errors
                    deep_evidence_complete = deep_evidence_complete and evidence_complete
                    return payload
                except asyncio.TimeoutError:
                    rank_timeouts += 1
                    return None
                except Exception:
                    rank_errors += 1
                    return None

        tasks = [
            asyncio.create_task(rank_candidate_safely(author))
            for author in deeper_shortlist
        ]
        elapsed = asyncio.get_running_loop().time() - search_started
        remaining = max(0.0, RANK_TOTAL_TIMEOUT_S - elapsed)
        if tasks and remaining > 0:
            done, pending = await asyncio.wait(tasks, timeout=remaining)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            rank_timeouts += len(pending)
            deeper_ranked = [
                task.result()
                for task in done
                if (
                    not task.cancelled()
                    and task.exception() is None
                    and task.result() is not None
                )
            ]
        else:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            rank_timeouts += len(tasks)
            deeper_ranked = []
        deeper_search_completed_count = sum(
            bool(result.pop("_search_complete", False)) for result in deeper_ranked
        )

    ranked = short_ranked + deeper_ranked
    ranked.sort(key=stable_result_sort_key)
    visible = [
        result for result in ranked
        if result["found"] and result.get("path_verified") is True
    ]
    searched_count = (
        started_count if short_checked_count else deeper_search_completed_count
    )
    unconnected_count = sum(
        1 for result in deeper_ranked if not result["found"]
    )
    deep_coverage_complete = (
        max_depth <= 2
        or (
            deeper_search_skipped_count == 0
            and deeper_search_completed_count == unmatched_count
        )
    )
    coverage_complete = bool(
        candidate_discovery_complete
        and short_complete
        and deep_coverage_complete
        and deep_evidence_complete
        and omitted_origin_count == 0
        and rank_timeouts == 0
        and rank_errors == 0
    )
    if coverage_complete:
        coverage_note = (
            f"Checked all {started_count} researchers in this balanced candidate pool "
            f"through {max_depth} coauthor steps. This is not an exhaustive roster."
        )
    else:
        short_coverage_note = (
            "all received the bounded exact one/two-hop scan"
            if short_checked_count == started_count
            else "the exact one/two-hop scan did not complete"
        )
        coverage_note = (
            f"Checked a balanced pool of {started_count} institution-associated researchers; "
            f"{short_coverage_note}, and {deeper_search_completed_count} "
            "unmatched candidates completed the bounded deeper scan. Results are verified, "
            "but absence from this list is not proof that no connection exists."
        )

    response = {
        "institution": inst,
        "origin_ids": origins,
        "omitted_origin_count": omitted_origin_count,
        "searched_count": searched_count,
        "started_count": started_count,
        "unconnected_count": unconnected_count,
        "timeout_count": rank_timeouts,
        "error_count": rank_errors,
        "short_search_complete": short_complete,
        "short_checked_count": short_checked_count,
        "deeper_search_started_count": deeper_search_started_count,
        "deeper_search_completed_count": deeper_search_completed_count,
        "deeper_search_skipped_count": deeper_search_skipped_count,
        "candidate_pool_requested": candidate_pool,
        "candidate_pool_cap": effective_pool_cap,
        "candidate_pool_effective": started_count,
        "candidate_source_counts": candidate_source_counts,
        "coverage_scope": "balanced_candidate_pool",
        "coverage_complete": coverage_complete,
        "coverage_note": coverage_note,
        "search_strategy": (
            "balanced_verified_two_hop_plus_bounded_bfs"
            if deeper_search_started_count
            else "balanced_verified_two_hop_join"
        ),
        "results": visible[:limit],
    }
    if diagnostics:
        deep_ids = {_short_id(author["id"]) for author in deeper_shortlist}
        response["candidate_diagnostics"] = [
            {
                "id": _short_id(author["id"]),
                "display_name": author.get("display_name"),
                "sources": list(author.get("_candidate_sources") or ()),
                "topic_similarity": float(author.get("_topic_similarity") or 0.0),
                "selected_for_deeper_search": _short_id(author["id"]) in deep_ids,
                "verified_path_found": _short_id(author["id"]) in {
                    result["author"]["id"] for result in visible
                },
            }
            for author in candidates
        ]
    if candidate_source_message:
        response["message"] = candidate_source_message
    elif not started_count:
        response["message"] = "No matching institution candidates found."
    elif not ranked and started_count:
        response["message"] = (
            "The explorer could not finish checking researchers this time. "
            "Please refresh to try again."
        )
    elif not visible and (rank_timeouts or rank_errors):
        response["message"] = (
            "The explorer could not finish checking every coauthor connection this time. "
            "Please refresh to try again."
        )
    elif not visible:
        response["message"] = (
            "No publication-verified path was found in the balanced candidate pool "
            "within this search budget. This is not an exhaustive university-wide negative."
        )
    elif rank_timeouts or rank_errors:
        response["message"] = (
            f"Ranked {len(ranked)} of {started_count} candidates within the time budget."
        )
    return response


@app.get("/api/authors/{author_id}/works", response_model=list[AuthorWork])
async def get_author_top_works(author_id: str, limit: int = Query(default=10, ge=1, le=25)):
    author_id = _short_id(author_id)
    verified_work_ids = get_verified_work_ids(author_id)
    if verified_work_ids is None:
        works = await _client.get_author_works(author_id, limit=limit)
    else:
        works = [
            work
            for work in await _client.get_works_batch(sorted(verified_work_ids))
            if (
                work.get("id")
                and _short_id(work["id"]) in verified_work_ids
                and not work.get("is_authors_truncated")
                and author_id in {
                    _short_id(authorship["author"]["id"])
                    for authorship in work.get("authorships", [])
                    if (
                        authorship.get("author")
                        and authorship["author"].get("id")
                    )
                }
            )
        ]
        works.sort(
            key=lambda work: (
                -int(work.get("cited_by_count", 0)),
                -(work.get("publication_year") or 0),
                _short_id(work.get("id", "")),
            )
        )
        works = works[:limit]
    return [
        AuthorWork(
            id=_short_id(w["id"]),
            title=w.get("title") or "(untitled)",
            cited_by_count=w.get("cited_by_count", 0),
            publication_year=w.get("publication_year"),
            doi=w.get("doi"),
        )
        for w in works
    ]


@app.delete("/api/cache")
async def clear_cache():
    """Wipe the server-side caches (neighbor LRU + persisted store + author LRU)."""
    await _cache.clear()
    _client.clear_author_cache()
    return {"cleared": True}


@app.get("/api/path")
async def get_path(
    from_id: str = Query(..., alias="from"),
    to_id: str = Query(..., alias="to"),
    edges: list[str] = Query(default=list(ALL_EDGE_TYPES)),
):
    edge_types = {e for e in edges if e in ALL_EDGE_TYPES} or ALL_EDGE_TYPES

    async def event_stream():
        try:
            from_author = apply_reviewed_identity(await _client.get_author(from_id))
            to_author = apply_reviewed_identity(await _client.get_author(to_id))
            from_name = from_author.get("display_name", from_id)
            to_name = to_author.get("display_name", to_id)
        except Exception as exc:
            yield f"event: app_error\ndata: {json.dumps({'message': str(exc)})}\n\n"
            return

        backend = _make_backend(edge_types)
        try:
            async for event in find_path(backend, from_id, from_name, to_id, to_name):
                event_type = event.get("type", "progress")
                yield f"event: {event_type}\ndata: {json.dumps(event)}\n\n"
        except Exception as exc:
            yield f"event: app_error\ndata: {json.dumps({'message': str(exc)})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/graph/expand")
async def graph_expand(
    new_id: str = Query(...),
    origin_ids: str = Query(default=""),   # comma-sep existing origin IDs
    path_ids: str = Query(default=""),     # comma-sep existing path node IDs from client
    edges: list[str] = Query(default=list(ALL_EDGE_TYPES)),
    work_edges: list[str] = Query(default=list(ALL_WORK_EDGE_TYPES)),
    depth: int = Query(default=2, ge=0, le=4),   # neighborhood expansion depth (0 = path only)
    top_k: int = Query(default=8, ge=1, le=25),  # neighbors kept per expansion level
):
    from backend.graph_expand import _edge_key, expand_graph, stitch_edges

    edge_types = {e for e in edges if e in ALL_EDGE_TYPES} or ALL_EDGE_TYPES
    work_edge_types = {e for e in work_edges if e in ALL_WORK_EDGE_TYPES} or ALL_WORK_EDGE_TYPES
    existing_origins = [x.strip() for x in origin_ids.split(",") if x.strip()]
    existing_path_ids = [x.strip() for x in path_ids.split(",") if x.strip()]

    async def event_stream():
        # Fetch the new origin's metadata — a work (paper) or an author.
        try:
            new_obj = await (
                _client.get_work(new_id) if _is_work_id(new_id) else _client.get_author(new_id)
            )
        except Exception as exc:
            yield f"event: app_error\ndata: {json.dumps({'message': str(exc)})}\n\n"
            return

        if _is_work_id(new_id):
            new_name = new_obj.get("title", new_id)
            new_node = {
                "id": new_id,
                "name": new_name,
                "cited_by_count": new_obj.get("cited_by_count", 0),
                "publication_year": new_obj.get("publication_year"),
                "type": "work",
                "depth": 0,
            }
        else:
            new_obj = apply_reviewed_identity(new_obj)
            new_name = new_obj.get("display_name", new_id)
            new_node = {
                "id": new_id,
                "name": new_name,
                "institution": _get_inst(new_obj),
                "works_count": new_obj.get("works_count", 0),
                "cited_by_count": new_obj.get("cited_by_count", 0),
                "type": "origin",
                "depth": 0,
            }
        yield f"event: node\ndata: {json.dumps(new_node)}\n\n"

        backend = _make_backend(edge_types, work_edge_types)
        new_path_node_ids: list[str] = []
        visible_ids: set[str] = {new_id} | set(existing_origins) | set(existing_path_ids)
        all_origins = [new_id] + existing_origins
        emitted_edge_keys: set[tuple] = set()

        def _path_events(path_results):
            """SSE frames for gathered path results: nodes, edges, then one path event per pair."""
            for result in path_results:
                if isinstance(result, Exception):
                    log.warning("Path finding failed: %s", result)
                    continue
                pair_key = "||".join(sorted([result["from_id"], result["to_id"]]))
                for n in result["nodes"]:
                    if n["type"] == "path":
                        n = {**n, "path_pair": pair_key}
                        new_path_node_ids.append(n["id"])
                    visible_ids.add(n["id"])
                    yield f"event: node\ndata: {json.dumps(n)}\n\n"
                for e in result["edges"]:
                    emitted_edge_keys.add(_edge_key(e["source"], e["target"], e["type"]))
                    yield f"event: edge\ndata: {json.dumps(e)}\n\n"
                path_event = {
                    k: result[k]
                    for k in ("from_id", "from_name", "to_id", "to_name", "hops", "found", "steps")
                }
                yield f"event: path\ndata: {json.dumps(path_event)}\n\n"

        # Overlap the two expensive phases: the path BFS runs as a background
        # gather while the origin expansion streams, so the user watches the
        # graph grow during the search instead of staring at a progress line.
        # Both phases share the ring cache, so no work is duplicated.
        path_task = None
        if existing_origins:
            yield f"event: progress\ndata: {json.dumps({'message': f'Finding connections to {len(existing_origins)} existing researcher(s)…'})}\n\n"
            path_task = asyncio.gather(*[
                _collect_path(backend, new_id, new_name, oid)
                for oid in existing_origins
            ], return_exceptions=True)

        try:
            # Phase 1: expand the origins (existing path nodes get their small
            # bridge budget here since they're known upfront). Stitching waits
            # until every phase's nodes are on the canvas. The path gather is
            # polled between events so the degrees answer lands as soon as the
            # search finishes, not after the whole expansion.
            if depth > 0:
                yield f"event: progress\ndata: {json.dumps({'message': 'Building neighborhood graph…'})}\n\n"
                async for event in expand_graph(
                    backend, _client, all_origins,
                    max_depth=depth, top_k=top_k, bridge_ids=existing_path_ids,
                    do_stitch=False,
                ):
                    if event.get("type") == "expansion":
                        visible_ids.update(n["id"] for n in event.get("nodes", []))
                        for e in event.get("edges", []):
                            emitted_edge_keys.add(_edge_key(e["source"], e["target"], e["type"]))
                    event_type = event.get("type", "progress")
                    yield f"event: {event_type}\ndata: {json.dumps(event)}\n\n"
                    if path_task is not None and path_task.done():
                        path_results = path_task.result()
                        path_task = None
                        for frame in _path_events(path_results):
                            yield frame

            # Phase 2: paths (skipped when they already flushed during phase 1).
            if path_task is not None:
                path_results = await path_task
                path_task = None
                for frame in _path_events(path_results):
                    yield frame

            # Phase 3: give the fresh path nodes their small bridge neighborhoods
            # (they only exist now). One ring level only — bridges just need a
            # little halo, and every extra level here is a serial API round that
            # the old merged flow got for free by batching bridges with origin
            # frontiers. May rediscover a phase-1 node; the client merges by id.
            new_bridges = [b for b in dict.fromkeys(new_path_node_ids) if b not in set(all_origins)]
            if depth > 0 and new_bridges:
                async for event in expand_graph(
                    backend, _client, [],
                    max_depth=1, top_k=top_k, bridge_ids=new_bridges,
                    do_stitch=False,
                ):
                    if event.get("type") == "expansion":
                        visible_ids.update(n["id"] for n in event.get("nodes", []))
                        for e in event.get("edges", []):
                            emitted_edge_keys.add(_edge_key(e["source"], e["target"], e["type"]))
                    event_type = event.get("type", "progress")
                    yield f"event: {event_type}\ndata: {json.dumps(event)}\n\n"

            # One stitch across everything now visible (cache-only); the
            # accumulated keys keep it from re-sending every edge already streamed.
            if depth > 0:
                stitch = await stitch_edges(backend, visible_ids, emitted_edge_keys)
                if stitch:
                    expansion = {"type": "expansion", "depth": depth, "nodes": [], "edges": stitch}
                    yield f"event: expansion\ndata: {json.dumps(expansion)}\n\n"
        except Exception as exc:
            # Degraded-path behavior: an expansion failure shouldn't discard the
            # path search — flush whatever it found before surfacing the error.
            if path_task is not None:
                path_results = await path_task
                path_task = None
                for frame in _path_events(path_results):
                    yield frame
            yield f"event: app_error\ndata: {json.dumps({'message': str(exc)})}\n\n"
            return
        finally:
            # Client disconnect closes this generator mid-stream; don't leave
            # the path gather running as an orphan.
            if path_task is not None:
                path_task.cancel()

        yield "event: done\ndata: {}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


_frontend_dir = Path(__file__).parent.parent / "frontend"
if _frontend_dir.exists():
    app.mount("/", StaticFiles(directory=str(_frontend_dir), html=True), name="frontend")
