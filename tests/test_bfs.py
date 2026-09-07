import pytest

from backend.bfs import find_path
from backend.graph_backend import GraphBackend
from backend.models import Connection


class MockBackend(GraphBackend):
    def __init__(self, graph: dict[str, list[Connection]]):
        self._graph = graph

    async def get_neighbors(self, author_id: str) -> list[Connection]:
        return self._graph.get(author_id, [])


class RecordingBackend(MockBackend):
    def __init__(self, graph, incomplete_ids=()):
        super().__init__(graph)
        self.calls = []
        self.incomplete_ids = set(incomplete_ids)

    async def get_neighbors_batch(self, author_ids, cached_only=False):
        self.calls.append(set(author_ids))
        batch = await super().get_neighbors_batch(author_ids, cached_only=cached_only)
        batch.complete_ids.difference_update(self.incomplete_ids)
        return batch


def edge(to_id, to_name, conn_type="coauthor", label="Test Paper", direction=None):
    return Connection(
        target_author_id=to_id,
        target_name=to_name,
        connection_type=conn_type,
        label=label,
        direction=direction,
    )


async def collect(gen):
    events = []
    async for e in gen:
        events.append(e)
    return events


async def test_same_person():
    backend = MockBackend({})
    events = await collect(find_path(backend, "A1", "Alice", "A1", "Alice"))
    result = events[-1]
    assert result["type"] == "result"
    assert result["found"] is True
    assert result["hops"] == 0
    assert result["path"][0]["author_id"] == "A1"


async def test_direct_connection():
    graph = {
        "A1": [edge("A2", "Bob")],
        "A2": [edge("A1", "Alice")],
    }
    backend = MockBackend(graph)
    events = await collect(find_path(backend, "A1", "Alice", "A2", "Bob"))
    result = events[-1]
    assert result["found"] is True
    assert result["hops"] == 1
    path_ids = [s["author_id"] for s in result["path"]]
    assert path_ids[0] == "A1"
    assert path_ids[-1] == "A2"


async def test_two_hop_path():
    graph = {
        "A1": [edge("B1", "Bob")],
        "B1": [edge("A1", "Alice"), edge("C1", "Carol")],
        "C1": [edge("B1", "Bob")],
    }
    backend = MockBackend(graph)
    events = await collect(find_path(backend, "A1", "Alice", "C1", "Carol"))
    result = events[-1]
    assert result["found"] is True
    assert result["hops"] == 2
    path_ids = [s["author_id"] for s in result["path"]]
    assert path_ids == ["A1", "B1", "C1"]


async def test_no_path_found():
    graph = {
        "A1": [],
        "B1": [],
    }
    backend = MockBackend(graph)
    events = await collect(find_path(backend, "A1", "Alice", "B1", "Bob", max_depth=2))
    result = events[-1]
    assert result["found"] is False
    assert "reason" in result


@pytest.mark.parametrize("empty_side", ["forward", "backward"])
async def test_search_continues_from_nonempty_side_after_partial_ring(empty_side):
    if empty_side == "forward":
        graph = {
            "A1": [],
            "A3": [edge("A2", "Bob")],
            "A2": [edge("A1", "Alice")],
        }
        incomplete_ids = {"A1"}
    else:
        graph = {
            "A1": [edge("A2", "Bob"), edge("A9", "Other")],
            "A3": [],
            "A2": [edge("A3", "Carol")],
        }
        incomplete_ids = {"A3"}
    backend = RecordingBackend(graph, incomplete_ids=incomplete_ids)

    events = await collect(find_path(backend, "A1", "Alice", "A3", "Carol", max_depth=3))

    assert events[-1]["found"] is True
    assert events[-1]["hops"] == 2
    assert events[-1]["search_complete"] is False
    assert [s["author_id"] for s in events[-1]["path"]] == ["A1", "A2", "A3"]
    assert len(backend.calls) == 3
    assert all(backend.calls)


async def test_exhausted_frontiers_stop_without_empty_batch_fetches():
    backend = RecordingBackend({})

    events = await collect(find_path(backend, "A1", "Alice", "A2", "Bob", max_depth=6))

    assert events[-1]["found"] is False
    assert backend.calls == [{"A1"}, {"A2"}]


@pytest.mark.parametrize("max_depth", [0, 1, 2, 3])
async def test_search_keeps_total_expansion_and_path_hop_bounds(max_depth):
    graph = {
        "A1": [edge("A2", "Bob")],
        "A2": [edge("A1", "Alice"), edge("A3", "Carol")],
        "A3": [edge("A2", "Bob"), edge("A4", "Dave")],
        "A4": [edge("A3", "Carol")],
    }
    backend = RecordingBackend(graph)

    events = await collect(find_path(backend, "A1", "Alice", "A4", "Dave", max_depth=max_depth))

    assert len(backend.calls) <= max_depth
    assert events[-1]["found"] is (max_depth >= 3)
    if events[-1]["found"]:
        assert events[-1]["hops"] <= max_depth


async def test_no_path_from_incomplete_ring_is_not_reported_complete():
    class NeighborMap(dict):
        pass

    class IncompleteBackend(MockBackend):
        async def get_neighbors_batch(self, author_ids, cached_only=False):
            values = NeighborMap({author_id: [] for author_id in author_ids})
            values.complete_ids = set()
            return values

    backend = IncompleteBackend({})
    events = await collect(
        find_path(backend, "A1", "Alice", "B1", "Bob", max_depth=2)
    )

    assert events[-1]["found"] is False
    assert events[-1]["search_complete"] is False


async def test_progress_events_emitted():
    graph = {
        "A1": [edge("B1", "Bob")],
        "B1": [edge("A1", "Alice"), edge("C1", "Carol")],
        "C1": [edge("B1", "Bob")],
    }
    backend = MockBackend(graph)
    events = await collect(find_path(backend, "A1", "Alice", "C1", "Carol"))
    progress = [e for e in events if e["type"] == "progress"]
    assert len(progress) >= 1


async def test_path_has_connection_labels():
    graph = {
        "A1": [Connection(target_author_id="A2", target_name="Bob",
                          connection_type="coauthor", label="Famous Paper")],
        "A2": [Connection(target_author_id="A1", target_name="Alice",
                          connection_type="coauthor", label="Famous Paper")],
    }
    backend = MockBackend(graph)
    events = await collect(find_path(backend, "A1", "Alice", "A2", "Bob"))
    result = events[-1]
    assert result["found"] is True
    first_step = result["path"][0]
    assert first_step["connection_to_next"] == "coauthor"
    assert first_step["label"] == "Famous Paper"
    assert first_step["direction"] is None


async def test_path_propagates_citation_direction():
    graph = {
        "A1": [edge("A2", "Bob", conn_type="citation", direction="outgoing")],
        "A2": [edge("A1", "Alice", conn_type="citation", direction="incoming")],
    }
    backend = MockBackend(graph)
    events = await collect(find_path(backend, "A1", "Alice", "A2", "Bob"))
    result = events[-1]
    assert result["found"] is True
    first_step = result["path"][0]
    assert first_step["connection_to_next"] == "citation"
    assert first_step["direction"] == "outgoing"


@pytest.mark.parametrize(
    ("backward_direction", "path_direction"),
    [("incoming", "outgoing"), ("outgoing", "incoming"), ("mutual", "mutual")],
)
async def test_backward_path_reverses_citation_direction_without_mutating_rings(
    backward_direction, path_direction,
):
    forward_edge = edge("A2", "Bob", conn_type="citation", direction="outgoing")
    backward_edge = edge(
        "A2", "Bob", conn_type="citation", direction=backward_direction,
    )
    graph = {
        # The larger source frontier forces the second expansion from A3.
        "A1": [forward_edge, edge("A9", "Other")],
        "A3": [backward_edge],
    }
    backend = MockBackend(graph)

    events = await collect(find_path(backend, "A1", "Alice", "A3", "Carol"))

    path = events[-1]["path"]
    assert [step["author_id"] for step in path] == ["A1", "A2", "A3"]
    assert path[0]["direction"] == "outgoing"
    assert path[1]["direction"] == path_direction
    assert forward_edge.direction == "outgoing"
    assert backward_edge.direction == backward_direction


async def test_interior_meeting_point():
    # Graph: A1 -> M1, C1 -> M1 (M1 is a middle node both sides reach)
    graph = {
        "A1": [edge("M1", "Middleman")],
        "M1": [edge("A1", "Alice"), edge("C1", "Carol")],
        "C1": [edge("M1", "Middleman")],
    }
    backend = MockBackend(graph)
    events = await collect(find_path(backend, "A1", "Alice", "C1", "Carol"))
    result = events[-1]
    assert result["found"] is True
    assert result["hops"] == 2
    path_ids = [s["author_id"] for s in result["path"]]
    assert "M1" in path_ids
    assert path_ids[0] == "A1"
    assert path_ids[-1] == "C1"


async def test_bfs_tolerates_neighbor_fetch_exception():
    # A1's neighbor fetch fails, but graph is otherwise disconnected
    class PartialFailBackend(GraphBackend):
        async def get_neighbors(self, author_id: str) -> list[Connection]:
            if author_id == "A1":
                raise RuntimeError("fetch failed")
            if author_id == "C1":
                return []
            return []

    backend = PartialFailBackend()
    events = await collect(find_path(backend, "A1", "Alice", "C1", "Carol", max_depth=2))
    result = events[-1]
    # Should not crash; either found=False or found=True (depending on graph)
    assert result["type"] == "result"
    assert "found" in result
