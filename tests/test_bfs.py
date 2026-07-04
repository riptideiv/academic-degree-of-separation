import pytest
from backend.bfs import find_path
from backend.graph_backend import GraphBackend
from backend.models import Connection


class MockBackend(GraphBackend):
    def __init__(self, graph: dict[str, list[Connection]]):
        self._graph = graph

    async def get_neighbors(self, author_id: str) -> list[Connection]:
        return self._graph.get(author_id, [])


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


# --- fast mode (frontier_cap) ---

class RecordingBackend(MockBackend):
    """MockBackend that also records every batch call's id list."""

    def __init__(self, graph):
        super().__init__(graph)
        self.batch_calls: list[list[str]] = []

    async def get_neighbors_batch(self, author_ids, cached_only=False):
        self.batch_calls.append(sorted(author_ids))
        return {i: self._graph.get(i, []) for i in author_ids}


def _hub_graph():
    """S fans out to 30 authors, T to 20; both fan-outs funnel into C."""
    graph = {
        "S": [edge(f"a{i}", f"A{i}") for i in range(30)],
        "T": [edge(f"b{i}", f"B{i}") for i in range(20)],
        "C": [edge(f"a{i}", f"A{i}") for i in range(30)]
           + [edge(f"b{i}", f"B{i}") for i in range(20)],
    }
    for i in range(30):
        graph[f"a{i}"] = [edge("C", "Common")]
    for i in range(20):
        graph[f"b{i}"] = [edge("C", "Common")]
    return graph


async def test_uncapped_search_expands_full_frontier():
    backend = RecordingBackend(_hub_graph())
    events = await collect(find_path(backend, "S", "Source", "T", "Target"))
    result = events[-1]
    assert result["found"] is True
    assert result["hops"] == 4  # S - a? - C - b? - T
    # The bidirectional search expands the smaller (backward) fan-out whole.
    assert max(len(c) for c in backend.batch_calls) == 20


async def test_frontier_cap_bounds_every_level():
    backend = RecordingBackend(_hub_graph())
    events = await collect(
        find_path(backend, "S", "Source", "T", "Target", frontier_cap=5)
    )
    result = events[-1]
    assert result["found"] is True
    assert result["hops"] == 4  # pruning is harmless: every a/b funnels into C
    assert max(len(c) for c in backend.batch_calls) == 5


async def test_frontier_cap_keeps_best_connected():
    # HUB is reachable from both frontier parents (freq 2); the leaves are
    # freq 1. With cap=2 the beam must keep HUB, and only HUB leads onward.
    graph = {
        "S": [edge("p1", "P1"), edge("p2", "P2")],
        "p1": [edge("HUB", "Hub"), edge("l1", "L1"), edge("l2", "L2")],
        "p2": [edge("HUB", "Hub"), edge("l3", "L3"), edge("l4", "L4")],
        "HUB": [edge("c1", "C1")],
        "T": [edge(f"c{i}", f"C{i}") for i in range(1, 10)],
    }
    backend = RecordingBackend(graph)
    events = await collect(
        find_path(backend, "S", "Source", "T", "Target", frontier_cap=2)
    )
    result = events[-1]
    assert result["found"] is True
    assert result["hops"] == 4  # S - p? - HUB - c1 - T survives the beam
    path_ids = [s["author_id"] for s in result["path"]]
    assert "HUB" in path_ids
