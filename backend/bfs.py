from typing import AsyncIterator

from backend.graph_backend import GraphBackend
from backend.models import Connection

# Fast mode: expand only this many candidates per BFS level (ranked by how many
# frontier nodes connect to them). Heuristic — the found path can occasionally
# be one hop longer than the true shortest, or missed for very obscure pairs.
FAST_FRONTIER_CAP = 200


async def find_path(
    backend: GraphBackend,
    source_id: str,
    source_name: str,
    target_id: str,
    target_name: str,
    max_depth: int = 6,
    frontier_cap: int | None = None,
) -> AsyncIterator[dict]:
    if source_id == target_id:
        yield {
            "type": "result",
            "found": True,
            "path": [{"author_id": source_id, "author_name": source_name,
                      "connection_to_next": None, "label": None, "direction": None}],
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

        neighbor_map = await backend.get_neighbors_batch(list(frontier))

        new_frontier: set[str] = set()
        meetings: list[str] = []  # all meeting candidates found in this level
        freq: dict[str, int] = {}  # frontier connections per candidate (fast-mode rank)

        for node_id in frontier:
            for conn in neighbor_map.get(node_id, []):
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
                    freq[nid] = 1
                elif nid in new_frontier:
                    freq[nid] += 1

        if meetings:
            # Pick the candidate with the minimum combined forward + backward depth
            best_meeting = min(
                meetings,
                key=lambda m: forward_depth_map.get(m, 0) + backward_depth_map.get(m, 0),
            )
            path = _reconstruct_path(best_meeting, forward_parents, backward_parents, names)
            yield {"type": "result", "found": True, "path": path, "hops": len(path) - 1}
            return

        # Fast mode: keep only the best-connected candidates. Pruned nodes stay
        # in `visited` (their parent link is still valid if the other side meets
        # them); they just never get expanded from this side.
        if frontier_cap is not None and len(new_frontier) > frontier_cap:
            ranked = sorted(new_frontier, key=lambda n: freq[n], reverse=True)
            new_frontier = set(ranked[:frontier_cap])
            yield {
                "type": "progress",
                "message": (
                    f"Fast mode: narrowing {len(ranked)} candidates "
                    f"to the {frontier_cap} best-connected"
                ),
            }

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
                 "connection_to_next": None, "label": None, "direction": None}]

    first_id = all_steps[0][0]
    path: list[dict] = [
        {"author_id": first_id, "author_name": names.get(first_id, first_id),
         "connection_to_next": None, "label": None, "direction": None}
    ]

    for from_id, conn, to_id in all_steps:
        if path[-1]["author_id"] == from_id:
            path[-1]["connection_to_next"] = conn.connection_type
            path[-1]["label"] = conn.label
            path[-1]["direction"] = conn.direction
        path.append({
            "author_id": to_id,
            "author_name": names.get(to_id, to_id),
            "connection_to_next": None,
            "label": None,
            "direction": None,
        })

    return path
