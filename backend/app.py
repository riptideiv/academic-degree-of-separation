import asyncio
import json
import inspect
import logging
import math
import os
import time
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

from backend.bfs import find_path
from backend.graph_backend import ALL_EDGE_TYPES, ALL_WORK_EDGE_TYPES, OpenAlexBackend, _is_work_id
from backend.local_cache_index import LocalCacheIndex
from backend.models import AuthorResult, AuthorWork, PaginatedAuthors, PaginatedWorks
from backend.neighbor_store import (
    JsonNeighborStore,
    NeighborCache,
    NeighborStore,
    SupabaseNeighborStore,
)
from backend.openalex_client import OpenAlexClient, _short_id

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

_CACHE_FILE = Path(__file__).parent.parent / "neighbor_cache.json"
_CACHE_MAX = int(os.environ.get("NEIGHBOR_CACHE_MAX", "10000"))
_local_index = LocalCacheIndex(_CACHE_FILE)


def _make_store() -> NeighborStore:
    dsn = os.environ.get("SUPABASE_POOLER_CONNECTION_STRING")
    if dsn:
        return SupabaseNeighborStore(dsn)
    return JsonNeighborStore(_CACHE_FILE)


_store: NeighborStore = _make_store()
_cache = NeighborCache(_store, max_size=_CACHE_MAX)


RANK_EFFECTIVE_POOL_MAX = 30
RANK_FETCH_LIMIT_MAX = 80
RANK_TOTAL_TIMEOUT_S = 28
RANK_CANDIDATE_TIMEOUT_S = 8
RANK_CONCURRENCY = 8
RANK_MAX_DEPTH = 4


@asynccontextmanager
async def lifespan(app: FastAPI):
    await _store.open()
    yield
    await _store.close()


app.router.lifespan_context = lifespan


def _make_backend(edge_types: set[str], work_edge_types: set[str] | None = None) -> OpenAlexBackend:
    if _BACKEND == "bigquery":
        from backend.bigquery_backend import BigQueryBackend
        project = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
        if not project:
            raise RuntimeError(
                "BigQuery backend requires the GOOGLE_CLOUD_PROJECT environment variable"
            )
        return BigQueryBackend(project, edge_types=edge_types)
    return OpenAlexBackend(
        _client,
        edge_types=edge_types,
        work_edge_types=work_edge_types,
        neighbor_cache=_cache,
    )


def _get_inst(author: dict) -> str | None:
    insts = author.get("last_known_institutions", [])
    return insts[0].get("display_name") if insts else None


def _get_matching_inst(author: dict, institution_id: str, fallback_name: str) -> str:
    for inst in author.get("last_known_institutions", []) or []:
        if inst.get("id") and _short_id(inst["id"]) == institution_id:
            return inst.get("display_name") or fallback_name
    return fallback_name


def _has_primary_inst(author: dict, institution_id: str) -> bool:
    insts = author.get("last_known_institutions", []) or []
    return bool(insts and insts[0].get("id") and _short_id(insts[0]["id"]) == institution_id)


def _affiliation_evidence(author: dict, institution_id: str) -> dict | None:
    for aff in author.get("affiliations", []) or []:
        inst = aff.get("institution") or {}
        if inst.get("id") and _short_id(inst["id"]) == institution_id:
            return {
                "institution_id": institution_id,
                "display_name": inst.get("display_name"),
                "years": sorted(aff.get("years") or [], reverse=True),
                "openalex_url": f"https://openalex.org/{institution_id}",
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


async def _collect_path(
    backend: OpenAlexBackend,
    from_id: str,
    from_name: str,
    to_id: str,
    max_depth: int = 6,
    hydrate: bool = True,
    to_name: str | None = None,
) -> dict:
    """Run bidirectional BFS; return the found path's nodes/edges plus hop count.

    The returned dict carries the graph elements as well as the degree-of-separation
    metadata (found, hops, and both endpoint names) so the caller can emit a `path`
    SSE event without re-deriving any of it.
    """
    to_obj: dict | None = None
    if to_name is None:
        if _is_work_id(to_id):
            to_obj = await _client.get_work(to_id)
            to_name = to_obj.get("title", to_id)
        else:
            try:
                to_obj = await _client.get_author(to_id)
            except Exception as exc:
                if not _is_rate_limited(exc):
                    raise
                to_obj = _local_index.author_record(to_id)
            to_name = to_obj.get("display_name", to_id)

    nodes: list[dict] = []
    edges: list[dict] = []
    steps: list[dict] = []   # ordered hops along the path (names + paper/label)
    found = False
    hops: int | None = None

    async for event in find_path(backend, from_id, from_name, to_id, to_name, max_depth=max_depth):
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
                        "from_name": step["author_name"],
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
    author_node_ids = [n["id"] for n in nodes if n["type"] != "work"] if hydrate else []
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
        meta = {_short_id(a["id"]): a for a in authors}
        for n in nodes:
            a = meta.get(n["id"])
            if a:
                n["institution"] = _get_inst(a)
                n["works_count"] = a.get("works_count", 0)
                n["cited_by_count"] = a.get("cited_by_count", 0)

    async def _work_details(n: dict) -> dict:
        return to_obj if n["id"] == to_id else await _client.get_work(n["id"])

    work_nodes = [n for n in nodes if n["type"] == "work"] if hydrate else []
    if work_nodes:
        if to_obj is None and any(n["id"] == to_id for n in work_nodes):
            to_obj = await _client.get_work(to_id)
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
    }


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
    candidate_pool: int = Query(default=30, ge=1, le=75),
    max_depth: int = Query(default=4, ge=1, le=6),
):
    """Suggest current institution researchers near any author in the graph.

    Discovery distance deliberately uses co-authorship only. The graph's display
    edge settings do not affect this relevance signal.
    """
    if institution_id:
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
            institution_results, _ = await _client.search_institutions(institution, page=1, per_page=1)
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

    origins = list(dict.fromkeys(_short_id(i) for i in origin_ids if i and not _is_work_id(i)))
    if not origins:
        return {"institution": inst, "results": [], "message": "Add a researcher you like to the graph first."}

    rank_started = time.perf_counter()
    call_count_start = _client.request_count
    cache_hits_start, cache_misses_start = _cache.stats()
    backend = _make_backend({"coauthor"})
    effective_pool = min(candidate_pool, RANK_EFFECTIVE_POOL_MAX)
    fetch_limit = min(
        RANK_FETCH_LIMIT_MAX,
        max(effective_pool * 4, effective_pool),
    )
    try:
        candidates = await _client.get_institution_authors(
            inst["id"], limit=fetch_limit, sort="cited_by_count:desc"
        )
        candidate_source_message = None
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 429:
            candidates = _local_index.institution_authors(
                inst["id"], inst.get("display_name"), limit=fetch_limit
            )
            candidate_source_message = _rate_limit_message()
        else:
            raise
    candidates = [a for a in candidates if _has_primary_inst(a, inst["id"])]
    candidates = candidates[:effective_pool]
    candidate_ids = [_short_id(a["id"]) for a in candidates if _short_id(a["id"]) not in origins]
    if candidate_ids:
        prefetch = backend.get_neighbors_batch(candidate_ids)
        if inspect.isawaitable(prefetch):
            await prefetch
    origin_records = await _client.get_authors_batch(origins)
    origin_names = {
        _short_id(author["id"]): author.get("display_name", _short_id(author["id"]))
        for author in origin_records
    }
    started_count = len([a for a in candidates if _short_id(a["id"]) not in origins])
    rank_errors = 0
    rank_timeouts = 0
    semaphore = asyncio.Semaphore(RANK_CONCURRENCY)

    async def rank_candidate(author: dict) -> dict:
        author_id = _short_id(author["id"])
        author_name = author.get("display_name", author_id)
        paths = await asyncio.gather(*[
            _collect_path(
                backend, author_id, author_name, origin_id, max_depth=max_depth,
                hydrate=False, to_name=origin_names.get(origin_id, origin_id),
            )
            for origin_id in origins
        ])
        found_paths = [(origin_id, path) for origin_id, path in zip(origins, paths) if path["found"]]
        closest_id, result = min(found_paths, key=lambda pair: pair[1]["hops"]) if found_paths else (None, {"found": False, "hops": None, "steps": []})
        topics = [t.get("display_name") for t in (author.get("topics") or author.get("x_concepts") or [])[:3] if t.get("display_name")]
        return {
            "matched_institution": _get_matching_inst(author, inst["id"], inst["display_name"]),
            "affiliation_evidence": _affiliation_evidence(author, inst["id"]),
            "author": {
                "id": author_id,
                "display_name": author_name,
                "institution": _get_inst(author),
                "works_count": author.get("works_count", 0),
                "cited_by_count": author.get("cited_by_count", 0),
                "openalex_url": f"https://openalex.org/{author_id}",
                "orcid": author.get("orcid"),
                "topics": topics,
            },
            "found": result["found"],
            "hops": result["hops"],
            "steps": result["steps"],
            "closest_origin_id": closest_id,
            "reachable_origin_count": len(found_paths),
        }

    async def rank_candidate_safely(author: dict) -> dict | None:
        nonlocal rank_errors, rank_timeouts
        async with semaphore:
            try:
                return await asyncio.wait_for(
                    rank_candidate(author),
                    timeout=RANK_CANDIDATE_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                rank_timeouts += 1
                return None
            except Exception:
                rank_errors += 1
                return None

    tasks = [
        asyncio.create_task(rank_candidate_safely(a))
        for a in candidates
        if _short_id(a["id"]) not in origins
    ]
    if tasks:
        done, pending = await asyncio.wait(tasks, timeout=RANK_TOTAL_TIMEOUT_S)
        for task in pending:
            task.cancel()
        rank_timeouts += len(pending)
        ranked = [
            task.result()
            for task in done
            if not task.cancelled() and task.exception() is None and task.result() is not None
        ]
    else:
        ranked = []
    ranked.sort(key=lambda r: (
        r["hops"] is None,
        r["hops"] if r["hops"] is not None else 999,
        -r["reachable_origin_count"],
        -r["author"]["cited_by_count"],
        r["author"]["display_name"].casefold(),
    ))
    visible = [r for r in ranked if r["found"]]
    unconnected_count = sum(1 for r in ranked if not r["found"])

    response = {
        "institution": inst,
        "origin_ids": origins,
        "searched_count": len(ranked),
        "started_count": started_count,
        "unconnected_count": unconnected_count,
        "timeout_count": rank_timeouts,
        "error_count": rank_errors,
        "results": visible[:limit],
    }
    if candidate_source_message:
        response["message"] = candidate_source_message
    elif not started_count:
        response["message"] = "No matching institution candidates found."
    elif not ranked and started_count:
        response["message"] = (
            "The explorer could not finish checking researchers this time. "
            "Please refresh to try again."
        )
    elif not visible:
        response["message"] = (
            "We could not find a coauthor connection between researchers at your school "
            "and the researchers in your graph yet. Try adding more researchers you like."
        )
    elif rank_timeouts or rank_errors:
        response["message"] = (
            f"Ranked {len(ranked)} of {started_count} candidates within the time budget."
        )
    call_count = _client.request_count - call_count_start
    log.info(
        "institution_rank institution=%s origins=%d candidates=%d ranked=%d calls=%d cache_hits=%d cache_misses=%d wall_ms=%d",
        inst["id"], len(origins), started_count, len(ranked), call_count,
        _cache.stats()[0] - cache_hits_start, _cache.stats()[1] - cache_misses_start,
        round((time.perf_counter() - rank_started) * 1000),
    )
    return response


@app.get("/api/authors/{author_id}/works", response_model=list[AuthorWork])
async def get_author_top_works(author_id: str, limit: int = Query(default=10, ge=1, le=25)):
    works = await _client.get_author_works(author_id, limit=limit)
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
            from_author = await _client.get_author(from_id)
            to_author = await _client.get_author(to_id)
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
