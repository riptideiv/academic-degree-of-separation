import asyncio
from typing import AsyncIterator

from backend.graph_backend import GraphBackend
from backend.models import Connection


async def find_path(
    backend: GraphBackend,
    source_id: str,
    source_name: str,
    target_id: str,
    target_name: str,
    max_depth: int = 6,
) -> AsyncIterator[dict]:
    if source_id == target_id:
        yield {
            "type": "result",
            "found": True,
            "path": [{"author_id": source_id, "author_name": source_name,
                      "connection_to_next": None, "label": None}],
            "hops": 0,
        }
        return

    # parent maps: node_id -> (parent_id, Connection) | None for BFS roots
    forward_parents: dict[str, tuple[str, Connection] | None] = {source_id: None}
    backward_parents: dict[str, tuple[str, Connection] | None] = {target_id: None}
    names: dict[str, str] = {source_id: source_name, target_id: target_name}
    forward_frontier: set[str] = {source_id}
    backward_frontier: set[str] = {target_id}

    # Depth tracking for each visited node
    forward_depth_map: dict[str, int] = {source_id: 0}
    backward_depth_map: dict[str, int] = {target_id: 0}
    forward_depth: int = 0
    backward_depth: int = 0

    for depth in range(max_depth):
        if not forward_frontier and not backward_frontier:
            break

        if len(forward_frontier) <= len(backward_frontier):
            direction = "forward"
            frontier = forward_frontier
            visited = forward_parents
            other_visited = backward_parents
            current_depth = forward_depth
            current_depth_map = forward_depth_map
        else:
            direction = "backward"
            frontier = backward_frontier
            visited = backward_parents
            other_visited = forward_parents
            current_depth = backward_depth
            current_depth_map = backward_depth_map

        yield {
            "type": "progress",
            "message": (
                f"Expanding {direction} frontier "
                f"({len(frontier)} authors at depth {depth + 1})..."
            ),
        }

        tasks = [backend.get_neighbors(nid) for nid in frontier]
        all_neighbors = await asyncio.gather(*tasks, return_exceptions=True)

        new_frontier: set[str] = set()
        meetings: list[str] = []  # all meeting candidates found in this level

        for node_id, neighbors_or_exc in zip(frontier, all_neighbors):
            if isinstance(neighbors_or_exc, Exception):
                continue
            for conn in neighbors_or_exc:
                nid = conn.target_author_id
                names[nid] = conn.target_name

                if nid in other_visited:
                    if nid not in visited:
                        visited[nid] = (node_id, conn)
                        current_depth_map[nid] = current_depth + 1
                    meetings.append(nid)
                    # Don't add to new_frontier; it's already reachable from both sides
                    continue

                if nid not in visited:
                    visited[nid] = (node_id, conn)
                    current_depth_map[nid] = current_depth + 1
                    new_frontier.add(nid)

        if meetings:
            # Pick the candidate with the minimum combined forward + backward depth
            best_meeting = min(
                meetings,
                key=lambda m: forward_depth_map.get(m, 0) + backward_depth_map.get(m, 0),
            )
            path = _reconstruct_path(best_meeting, forward_parents, backward_parents, names)
            yield {"type": "result", "found": True, "path": path, "hops": len(path) - 1}
            return

        yield {
            "type": "progress",
            "message": f"Found {len(new_frontier)} new authors to explore",
        }

        if direction == "forward":
            forward_frontier = new_frontier
            forward_depth += 1
        else:
            backward_frontier = new_frontier
            backward_depth += 1

    yield {
        "type": "result",
        "found": False,
        "reason": f"No path found within {max_depth} hops",
    }


def _reconstruct_path(
    meeting_id: str,
    forward_parents: dict[str, tuple | None],
    backward_parents: dict[str, tuple | None],
    names: dict[str, str],
) -> list[dict]:
    # Forward half: trace from meeting back to source, then reverse
    # forward_parents[node] = (parent, conn) means parent->node via conn
    forward_steps: list[tuple[str, Connection, str]] = []
    node = meeting_id
    while forward_parents[node] is not None:
        parent_id, conn = forward_parents[node]
        forward_steps.append((parent_id, conn, node))
        node = parent_id
    forward_steps.reverse()

    # Backward half: meeting -> target
    # backward_parents[node] = (parent, conn) means parent->node in backward BFS
    # so real direction is node -> parent -> ... -> target
    backward_steps: list[tuple[str, Connection, str]] = []
    node = meeting_id
    while backward_parents[node] is not None:
        parent_id, conn = backward_parents[node]
        backward_steps.append((node, conn, parent_id))
        node = parent_id

    all_steps = forward_steps + backward_steps

    if not all_steps:
        return [{"author_id": meeting_id, "author_name": names.get(meeting_id, meeting_id),
                 "connection_to_next": None, "label": None}]

    first_id = all_steps[0][0]
    path: list[dict] = [
        {"author_id": first_id, "author_name": names.get(first_id, first_id),
         "connection_to_next": None, "label": None}
    ]

    for from_id, conn, to_id in all_steps:
        if path[-1]["author_id"] == from_id:
            path[-1]["connection_to_next"] = conn.connection_type
            path[-1]["label"] = conn.label
        path.append({
            "author_id": to_id,
            "author_name": names.get(to_id, to_id),
            "connection_to_next": None,
            "label": None,
        })

    return path
