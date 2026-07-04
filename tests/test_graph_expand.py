from unittest.mock import AsyncMock

from backend.graph_backend import GraphBackend
from backend.graph_expand import expand_graph
from backend.models import Connection


class RecordingBackend(GraphBackend):
    """Serves canned rings and records every batch call's cached_only flag."""

    def __init__(self, rings):
        self.rings = rings
        self.calls = []  # (sorted_ids, cached_only)

    async def get_neighbors(self, author_id):
        return self.rings.get(author_id, [])

    async def get_neighbors_batch(self, ids, cached_only=False):
        self.calls.append((sorted(ids), cached_only))
        return {i: self.rings.get(i, []) for i in ids}


def coauthor(target, name):
    return Connection(
        target_author_id=target, target_name=name,
        connection_type="coauthor", label="P1",
    )


async def test_stitch_pass_is_cache_only():
    backend = RecordingBackend({
        "A1": [coauthor("A2", "Bob")],
        "A2": [coauthor("A1", "Alice")],
    })
    client = AsyncMock()
    client.get_authors_batch.return_value = []

    events = [e async for e in expand_graph(backend, client, ["A1"], max_depth=1, top_k=5)]

    assert any(e["type"] == "expansion" for e in events)
    # Expansion levels fetch normally; the final stitch read must be cache-only.
    assert backend.calls[-1] == (["A1", "A2"], True)
    assert all(cached is False for _, cached in backend.calls[:-1])
