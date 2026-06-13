import logging
from typing import AsyncIterator

from backend.graph_backend import GraphBackend
from backend.openalex_client import OpenAlexClient, _short_id

log = logging.getLogger(__name__)


def _inst_name(author: dict) -> str | None:
    insts = author.get("last_known_institutions", [])
    return insts[0].get("display_name") if insts else None


async def expand_graph(
    backend: GraphBackend,
    client: OpenAlexClient,
    start_node_ids: list[str],
    max_depth: int = 3,
    top_k: int = 10,
) -> AsyncIterator[dict]:
    """
    BFS expansion from start_node_ids up to max_depth levels.

    At each depth the full ring of every frontier node is read (from the backend's
    cache when available, fetched from the API only when not).  All candidate
    neighbours are collected, ranked by cited_by_count, and pruned to top_k.  The
    top_k survivors become the next frontier.

    Yields dicts with keys:
      {"type": "progress", "message": str}
      {"type": "expansion", "depth": int, "nodes": [...], "edges": [...]}
    """
    all_seen: set[str] = set(start_node_ids)
    frontier: set[str] = set(start_node_ids)

    for depth in range(1, max_depth + 1):
        if not frontier:
            break

        yield {"type": "progress", "message": f"Expanding network (depth {depth}/{max_depth})…"}

        # get_neighbors_batch reads from backend's ring cache where possible,
        # fetching from OpenAlex only for authors whose rings are not yet cached.
        neighbor_map = await backend.get_neighbors_batch(list(frontier))

        candidates: dict[str, str] = {}   # id → name (first connection seen)
        freq: dict[str, int] = {}         # id → number of frontier nodes that reach it
        raw_edges: list[dict] = []

        for src_id, connections in neighbor_map.items():
            for conn in connections:
                tgt = conn.target_author_id
                if tgt not in all_seen:
                    freq[tgt] = freq.get(tgt, 0) + 1
                    if tgt not in candidates:
                        candidates[tgt] = conn.target_name
                    raw_edges.append({
                        "source": src_id,
                        "target": tgt,
                        "type": conn.connection_type,
                        "label": conn.label,
                    })

        if not candidates:
            break

        # Pre-filter by connection frequency before the expensive metadata fetch.
        # Authors connected to more frontier nodes are more central; this cap keeps
        # get_authors_batch to at most top_k*5 IDs (one API chunk) regardless of
        # how many raw candidates the ring queries returned.
        pre_filter_limit = top_k * 5
        if len(candidates) > pre_filter_limit:
            top_by_freq = sorted(candidates, key=lambda aid: freq.get(aid, 0), reverse=True)[:pre_filter_limit]
            candidates = {aid: candidates[aid] for aid in top_by_freq}
            raw_edges = [e for e in raw_edges if e["target"] in candidates]

        candidate_ids = list(candidates.keys())
        author_list = await client.get_authors_batch(candidate_ids)
        meta: dict[str, dict] = {_short_id(a["id"]): a for a in author_list}

        ranked = sorted(
            candidate_ids,
            key=lambda aid: meta.get(aid, {}).get("cited_by_count", 0),
            reverse=True,
        )[:top_k]
        kept = set(ranked)

        nodes = [
            {
                "id": aid,
                "name": candidates[aid],
                "institution": _inst_name(meta.get(aid, {})),
                "works_count": meta.get(aid, {}).get("works_count", 0),
                "cited_by_count": meta.get(aid, {}).get("cited_by_count", 0),
                "type": "expansion",
                "depth": depth,
            }
            for aid in ranked
        ]
        kept_edges = [e for e in raw_edges if e["target"] in kept]

        yield {"type": "expansion", "depth": depth, "nodes": nodes, "edges": kept_edges}

        # Mark all candidates as seen so they are never re-discovered as expansion
        # nodes in a later depth (even the pruned ones — their rings may still get
        # populated as a side-effect of fetching the kept nodes' rings later).
        all_seen |= set(candidate_ids)
        frontier = kept
