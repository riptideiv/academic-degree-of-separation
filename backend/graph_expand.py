import logging
from typing import AsyncIterator

from backend.graph_backend import GraphBackend
from backend.openalex_client import OpenAlexClient, _short_id

log = logging.getLogger(__name__)


def _inst_name(author: dict) -> str | None:
    insts = author.get("last_known_institutions", [])
    return insts[0].get("display_name") if insts else None


def _edge_key(a: str, b: str, etype: str) -> tuple:
    """Canonical (undirected) key so A->B and B->A dedupe to one edge."""
    return (a, b, etype) if a <= b else (b, a, etype)


async def expand_graph(
    backend: GraphBackend,
    client: OpenAlexClient,
    root_ids: list[str],
    max_depth: int = 2,
    top_k: int = 10,
    bridge_ids: list[str] | None = None,
    bridge_top_k: int | None = None,
) -> AsyncIterator[dict]:
    """
    Balanced BFS expansion around each root (a researcher of interest).

    Each root grows its own neighborhood with its own per-level budget, so a
    highly-connected researcher can't crowd out a sparse one in a single global
    ranking. Neighbors shared between roots are deduplicated and bridge them.

    bridge_ids (the connecting-path nodes) are also expanded, but with a smaller
    budget, so each middle node gains a little neighborhood of its own instead of
    sitting as an isolated chain link.

    After expansion, a stitch pass adds the real edges among all visible nodes (no new
    nodes). Together with the bridge expansion this keeps the middle nodes connected
    and lets the neighborhoods interconnect.

    Yields:
      {"type": "progress", "message": str}
      {"type": "expansion", "depth": int, "nodes": [...], "edges": [...]}
    """
    # Origins expand with the full budget; bridges (path nodes) with a smaller one.
    bridges = [b for b in dict.fromkeys(bridge_ids or []) if b not in set(root_ids)]
    btk = bridge_top_k if bridge_top_k is not None else max(3, top_k // 3)
    budget: dict[str, int] = {r: top_k for r in root_ids}
    for b in bridges:
        budget[b] = btk
    all_roots = list(budget.keys())

    all_seen: set[str] = set(all_roots)
    graph_nodes: set[str] = set(all_roots)   # everything visible in the graph
    emitted: set[tuple] = set()              # canonical edges already sent
    frontiers: dict[str, set[str]] = {r: {r} for r in all_roots}

    for depth in range(1, max_depth + 1):
        active = [r for r in all_roots if frontiers.get(r)]
        if not active:
            break

        yield {"type": "progress", "message": f"Expanding network (depth {depth}/{max_depth})…"}

        # One batched ring read for the union of every root's frontier.
        union_frontier: set[str] = set()
        for r in active:
            union_frontier |= frontiers[r]
        neighbor_map = await backend.get_neighbors_batch(list(union_frontier))

        # Collect candidates per root (so each gets its own budget below).
        per_root: dict[str, dict] = {}
        for r in active:
            cands: dict[str, str] = {}
            freq: dict[str, int] = {}
            edges: list[dict] = []
            for src in frontiers[r]:
                for conn in neighbor_map.get(src, []):
                    tgt = conn.target_author_id
                    if tgt in all_seen:
                        continue
                    freq[tgt] = freq.get(tgt, 0) + 1
                    cands.setdefault(tgt, conn.target_name)
                    edges.append({
                        "source": src,
                        "target": tgt,
                        "type": conn.connection_type,
                        "label": conn.label,
                    })
            per_root[r] = {"cands": cands, "freq": freq, "edges": edges}

        # Pre-filter each root's candidates by connection frequency before the
        # metadata fetch (bounds how many author records we look up).
        pre_filter = top_k * 5
        fetch_ids: set[str] = set()
        for r in active:
            cands, freq = per_root[r]["cands"], per_root[r]["freq"]
            fetch_ids |= set(sorted(cands, key=lambda a: freq.get(a, 0), reverse=True)[:pre_filter])

        if not fetch_ids:
            break

        author_list = await client.get_authors_batch(list(fetch_ids))
        meta: dict[str, dict] = {_short_id(a["id"]): a for a in author_list}

        # Rank each root's candidates by citations and keep its own budget per root.
        next_frontiers: dict[str, set[str]] = {r: set() for r in all_roots}
        kept_all: set[str] = set()
        for r in active:
            cands = per_root[r]["cands"]
            ranked = sorted(
                cands.keys(),
                key=lambda a: meta.get(a, {}).get("cited_by_count", 0),
                reverse=True,
            )[:budget[r]]
            next_frontiers[r] = set(ranked)
            kept_all |= set(ranked)

        # Deduped nodes for everything kept this level.
        nodes = []
        for aid in kept_all:
            name = next(
                (per_root[r]["cands"][aid] for r in active if aid in per_root[r]["cands"]),
                aid,
            )
            m = meta.get(aid, {})
            nodes.append({
                "id": aid,
                "name": name,
                "institution": _inst_name(m),
                "works_count": m.get("works_count", 0),
                "cited_by_count": m.get("cited_by_count", 0),
                "type": "expansion",
                "depth": depth,
                "expand_owners": [r for r in active if aid in per_root[r]["cands"]],
            })

        # Every edge into a kept node, from any root (so bridges between roots show),
        # deduplicated.
        edges = []
        for r in active:
            for e in per_root[r]["edges"]:
                if e["target"] not in kept_all:
                    continue
                k = _edge_key(e["source"], e["target"], e["type"])
                if k not in emitted:
                    emitted.add(k)
                    edges.append(e)

        yield {"type": "expansion", "depth": depth, "nodes": nodes, "edges": edges}

        graph_nodes |= kept_all
        # Mark all candidates seen (even pruned ones) so they aren't rediscovered.
        for r in active:
            all_seen |= set(per_root[r]["cands"].keys())
        frontiers = next_frontiers

    # Stitch pass: add the real edges among the visible nodes (no new nodes). This
    # links the connecting/middle nodes into the graph and interconnects the
    # neighborhoods, instead of leaving thin chains between two bushy hubs.
    if graph_nodes:
        neighbor_map = await backend.get_neighbors_batch(list(graph_nodes))
        stitch = []
        for src, conns in neighbor_map.items():
            for conn in conns:
                tgt = conn.target_author_id
                if tgt == src or tgt not in graph_nodes:
                    continue
                k = _edge_key(src, tgt, conn.connection_type)
                if k in emitted:
                    continue
                emitted.add(k)
                stitch.append({
                    "source": src,
                    "target": tgt,
                    "type": conn.connection_type,
                    "label": conn.label,
                })
        if stitch:
            yield {"type": "expansion", "depth": max_depth, "nodes": [], "edges": stitch}
