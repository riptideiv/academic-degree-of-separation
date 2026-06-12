import pytest
from backend.bfs import find_path
from backend.graph_backend import GraphBackend
from backend.models import Connection


class MockBackend(GraphBackend):
    def __init__(self, graph: dict[str, list[Connection]]):
        self._graph = graph

    async def get_neighbors(self, author_id: str) -> list[Connection]:
        return self._graph.get(author_id, [])


def edge(to_id, to_name, conn_type="coauthor", label="Test Paper"):
    return Connection(
        target_author_id=to_id,
        target_name=to_name,
        connection_type=conn_type,
        label=label,
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
