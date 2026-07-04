import asyncio

import pytest
from unittest.mock import AsyncMock
from backend.graph_backend import OpenAlexBackend
from backend.models import Connection
from backend.neighbor_store import NeighborCache, NeighborStore


def make_work(work_id, title, authors):
    return {
        "id": f"https://openalex.org/{work_id}",
        "title": title,
        "authorships": [
            {"author": {"id": f"https://openalex.org/{aid}", "display_name": name}}
            for aid, name in authors
        ],
    }


async def test_get_coauthors():
    mock_client = AsyncMock()
    mock_client.get_author_works.return_value = [
        make_work("W1", "Paper AB", [("A1", "Alice"), ("A2", "Bob")])
    ]
    mock_client.get_citing_works.return_value = []
    mock_client.get_author.return_value = {"last_known_institutions": []}

    backend = OpenAlexBackend(mock_client)
    neighbors = await backend.get_neighbors("A1")

    coauthors = [n for n in neighbors if n.connection_type == "coauthor"]
    assert any(c.target_author_id == "A2" for c in coauthors)
    assert any(c.label == "Paper AB" for c in coauthors)


async def test_excludes_self_from_coauthors():
    mock_client = AsyncMock()
    mock_client.get_author_works.return_value = [
        make_work("W1", "Solo paper", [("A1", "Alice")])
    ]
    mock_client.get_citing_works.return_value = []
    mock_client.get_author.return_value = {"last_known_institutions": []}

    backend = OpenAlexBackend(mock_client)
    neighbors = await backend.get_neighbors("A1")
    assert not any(n.target_author_id == "A1" for n in neighbors)


async def test_institution_neighbors():
    mock_client = AsyncMock()
    mock_client.get_author_works.return_value = []
    mock_client.get_author.return_value = {
        "last_known_institutions": [
            {"id": "https://openalex.org/I1", "display_name": "MIT"}
        ]
    }
    mock_client.get_institution_authors.return_value = [
        {"id": "https://openalex.org/A2", "display_name": "Bob"}
    ]

    backend = OpenAlexBackend(mock_client)
    neighbors = await backend.get_neighbors("A1")

    inst = [n for n in neighbors if n.connection_type == "institution"]
    assert any(c.target_author_id == "A2" for c in inst)
    assert any(c.label == "MIT" for c in inst)


async def test_deduplicates_neighbors():
    mock_client = AsyncMock()
    # Two works both list A2 as coauthor
    mock_client.get_author_works.return_value = [
        make_work("W1", "Paper 1", [("A1", "Alice"), ("A2", "Bob")]),
        make_work("W2", "Paper 2", [("A1", "Alice"), ("A2", "Bob")]),
    ]
    mock_client.get_citing_works.return_value = []
    mock_client.get_author.return_value = {"last_known_institutions": []}

    backend = OpenAlexBackend(mock_client)
    neighbors = await backend.get_neighbors("A1")

    ids = [n.target_author_id for n in neighbors]
    assert ids.count("A2") == 1


async def test_failed_sub_query_does_not_crash():
    mock_client = AsyncMock()
    mock_client.get_author_works.side_effect = Exception("API error")
    mock_client.get_author.return_value = {"last_known_institutions": []}

    backend = OpenAlexBackend(mock_client)
    neighbors = await backend.get_neighbors("A1")
    assert isinstance(neighbors, list)


async def test_edge_type_filtering():
    mock_client = AsyncMock()
    mock_client.get_author_works.return_value = [
        make_work("W1", "Paper AB", [("A1", "Alice"), ("A2", "Bob")])
    ]
    mock_client.get_author.return_value = {"last_known_institutions": []}

    backend = OpenAlexBackend(mock_client, edge_types={"coauthor"})
    neighbors = await backend.get_neighbors("A1")

    assert all(n.connection_type == "coauthor" for n in neighbors)
    # citation and institution methods should not have been called
    mock_client.get_institution_authors.assert_not_called()


# --- get_neighbors_batch tests ---

async def test_batch_coauthors():
    mock_client = AsyncMock()
    mock_client.get_works_by_authors.return_value = [
        make_work("W1", "Paper AB", [("A1", "Alice"), ("A2", "Bob"), ("A3", "Carol")])
    ]
    mock_client.get_authors_batch.return_value = []

    backend = OpenAlexBackend(mock_client, edge_types={"coauthor"})
    result = await backend.get_neighbors_batch(["A1", "A2"])

    # A1 should see A3 as coauthor (A2 is in the frontier, filtered out)
    a1_ids = {c.target_author_id for c in result["A1"]}
    assert "A3" in a1_ids
    assert "A2" not in a1_ids


async def test_batch_citations():
    mock_client = AsyncMock()
    mock_client.get_works_by_authors.return_value = [
        make_work("W1", "Original Paper", [("A1", "Alice")])
    ]
    mock_client.get_citing_works_for_works.return_value = [{
        "id": "https://openalex.org/W2",
        "authorships": [{"author": {"id": "https://openalex.org/A3", "display_name": "Carol"}}],
        "referenced_works": ["https://openalex.org/W1"],
    }]

    backend = OpenAlexBackend(mock_client, edge_types={"citation"})
    result = await backend.get_neighbors_batch(["A1"])

    citations = [c for c in result["A1"] if c.connection_type == "citation"]
    assert any(c.target_author_id == "A3" for c in citations)
    assert any(c.label == "Original Paper" for c in citations)


async def test_batch_institutions():
    mock_client = AsyncMock()
    mock_client.get_authors_batch.return_value = [{
        "id": "https://openalex.org/A1",
        "display_name": "Alice",
        "last_known_institutions": [{"id": "https://openalex.org/I1", "display_name": "MIT"}],
    }]
    mock_client.get_institution_authors_batch.return_value = [{
        "id": "https://openalex.org/A4",
        "display_name": "Dave",
        "last_known_institutions": [{"id": "https://openalex.org/I1", "display_name": "MIT"}],
    }]

    backend = OpenAlexBackend(mock_client, edge_types={"institution"})
    result = await backend.get_neighbors_batch(["A1"])

    inst = [c for c in result["A1"] if c.connection_type == "institution"]
    assert any(c.target_author_id == "A4" for c in inst)
    assert any(c.label == "MIT" for c in inst)


async def test_batch_excludes_frontier_authors():
    mock_client = AsyncMock()
    # A2 (also in frontier) co-authors with A1 — should be excluded
    mock_client.get_works_by_authors.return_value = [
        make_work("W1", "Paper", [("A1", "Alice"), ("A2", "Bob"), ("A3", "Carol")])
    ]
    mock_client.get_authors_batch.return_value = []

    backend = OpenAlexBackend(mock_client, edge_types={"coauthor"})
    result = await backend.get_neighbors_batch(["A1", "A2"])

    a1_ids = {c.target_author_id for c in result["A1"]}
    assert "A2" not in a1_ids
    assert "A3" in a1_ids


async def test_batch_failed_subquery_returns_empty():
    mock_client = AsyncMock()
    mock_client.get_works_by_authors.side_effect = Exception("API error")
    mock_client.get_authors_batch.return_value = []

    backend = OpenAlexBackend(mock_client, edge_types={"coauthor"})
    result = await backend.get_neighbors_batch(["A1"])
    assert result["A1"] == []


async def test_concurrent_overlapping_batches_share_cache_miss():
    mock_client = AsyncMock()
    fetch_started = asyncio.Event()
    release_fetch = asyncio.Event()

    async def slow_works(_author_ids):
        fetch_started.set()
        await release_fetch.wait()
        return []

    mock_client.get_works_by_authors.side_effect = slow_works
    mock_client.get_authors_batch.return_value = []

    backend = OpenAlexBackend(mock_client)
    first = asyncio.create_task(backend.get_neighbors_batch(["A1"]))
    await fetch_started.wait()
    second = asyncio.create_task(backend.get_neighbors_batch(["A1"]))
    await asyncio.sleep(0)
    release_fetch.set()

    assert await first == {"A1": []}
    assert await second == {"A1": []}
    mock_client.get_works_by_authors.assert_awaited_once_with(["A1"])
    mock_client.get_authors_batch.assert_awaited_once_with(["A1"])


async def test_citation_neighbors():
    mock_client = AsyncMock()
    citing_work = {
        "id": "https://openalex.org/W2",
        "title": "Citing Paper",
        "authorships": [
            {"author": {"id": "https://openalex.org/A3", "display_name": "Carol"}}
        ],
    }
    mock_client.get_author_works.return_value = [
        make_work("W1", "Original Paper", [("A1", "Alice")])
    ]
    mock_client.get_citing_works.return_value = [citing_work]
    mock_client.get_author.return_value = {"last_known_institutions": []}

    backend = OpenAlexBackend(mock_client)
    neighbors = await backend.get_neighbors("A1")

    citations = [n for n in neighbors if n.connection_type == "citation"]
    assert any(c.target_author_id == "A3" for c in citations)
    assert any(c.label == "Original Paper" for c in citations)
    assert all(c.direction == "incoming" for c in citations)


async def test_citation_neighbors_outgoing():
    """The author's own paper cites someone else's work."""
    mock_client = AsyncMock()
    work = make_work("W1", "Original Paper", [("A1", "Alice")])
    work["referenced_works"] = ["https://openalex.org/W9"]
    mock_client.get_author_works.return_value = [work]
    mock_client.get_citing_works.return_value = []
    mock_client.get_works_batch.return_value = [{
        "id": "https://openalex.org/W9",
        "title": "Earlier Paper",
        "authorships": [{"author": {"id": "https://openalex.org/A3", "display_name": "Carol"}}],
    }]
    mock_client.get_author.return_value = {"last_known_institutions": []}

    backend = OpenAlexBackend(mock_client)
    neighbors = await backend.get_neighbors("A1")

    citations = [n for n in neighbors if n.connection_type == "citation"]
    assert len(citations) == 1
    assert citations[0].target_author_id == "A3"
    assert citations[0].direction == "outgoing"
    assert citations[0].label == "Earlier Paper"


async def test_citation_neighbors_mutual():
    """Both an incoming and outgoing citation exist for the same pair -> merged."""
    mock_client = AsyncMock()
    work = make_work("W1", "Original Paper", [("A1", "Alice")])
    work["referenced_works"] = ["https://openalex.org/W9"]
    mock_client.get_author_works.return_value = [work]
    mock_client.get_citing_works.return_value = [{
        "id": "https://openalex.org/W2",
        "title": "Citing Paper",
        "authorships": [{"author": {"id": "https://openalex.org/A3", "display_name": "Carol"}}],
    }]
    mock_client.get_works_batch.return_value = [{
        "id": "https://openalex.org/W9",
        "title": "Earlier Paper",
        "authorships": [{"author": {"id": "https://openalex.org/A3", "display_name": "Carol"}}],
    }]
    mock_client.get_author.return_value = {"last_known_institutions": []}

    backend = OpenAlexBackend(mock_client)
    neighbors = await backend.get_neighbors("A1")

    citations = [n for n in neighbors if n.connection_type == "citation"]
    assert len(citations) == 1
    assert citations[0].target_author_id == "A3"
    assert citations[0].direction == "mutual"


async def test_batch_citations_outgoing_and_mutual():
    mock_client = AsyncMock()
    work = make_work("W1", "Original Paper", [("A1", "Alice")])
    work["referenced_works"] = ["https://openalex.org/W9"]
    mock_client.get_works_by_authors.return_value = [work]
    mock_client.get_citing_works_for_works.return_value = []
    mock_client.get_works_batch.return_value = [{
        "id": "https://openalex.org/W9",
        "title": "Earlier Paper",
        "authorships": [{"author": {"id": "https://openalex.org/A3", "display_name": "Carol"}}],
    }]

    backend = OpenAlexBackend(mock_client, edge_types={"citation"})
    result = await backend.get_neighbors_batch(["A1"])

    citations = [c for c in result["A1"] if c.connection_type == "citation"]
    assert len(citations) == 1
    assert citations[0].target_author_id == "A3"
    assert citations[0].direction == "outgoing"
    assert citations[0].label == "Earlier Paper"


async def test_batch_citations_incoming_direction_set():
    mock_client = AsyncMock()
    mock_client.get_works_by_authors.return_value = [
        make_work("W1", "Original Paper", [("A1", "Alice")])
    ]
    mock_client.get_citing_works_for_works.return_value = [{
        "id": "https://openalex.org/W2",
        "authorships": [{"author": {"id": "https://openalex.org/A3", "display_name": "Carol"}}],
        "referenced_works": ["https://openalex.org/W1"],
    }]

    backend = OpenAlexBackend(mock_client, edge_types={"citation"})
    result = await backend.get_neighbors_batch(["A1"])

    citations = [c for c in result["A1"] if c.connection_type == "citation"]
    assert len(citations) == 1
    assert citations[0].direction == "incoming"


# --- work-node neighbor dispatch ---

async def test_get_neighbors_batch_dispatches_work_id():
    mock_client = AsyncMock()
    mock_client.get_works_batch.return_value = [{
        "id": "https://openalex.org/W1",
        "title": "A Paper",
        "authorships": [{"author": {"id": "https://openalex.org/A1", "display_name": "Alice"}}],
    }]
    mock_client.get_citing_works_for_works.return_value = [{
        "id": "https://openalex.org/W2",
        "authorships": [{"author": {"id": "https://openalex.org/A2", "display_name": "Bob"}}],
        "referenced_works": ["https://openalex.org/W1"],
    }]

    backend = OpenAlexBackend(mock_client)
    result = await backend.get_neighbors_batch(["W1"])

    by_type = {c.connection_type: c for c in result["W1"]}
    assert by_type["authorship"].target_author_id == "A1"
    assert by_type["citation"].target_author_id == "A2"
    assert by_type["citation"].direction == "incoming"
    # the normal author-level connection methods must not have been touched
    mock_client.get_works_by_authors.assert_not_called()


async def test_get_neighbors_batch_filters_work_edge_types():
    # The ring cache is always populated with ALL_WORK_EDGE_TYPES; only the
    # *returned* result is filtered to the active set (same pattern as authors).
    mock_client = AsyncMock()
    mock_client.get_works_batch.return_value = [{
        "id": "https://openalex.org/W1",
        "title": "A Paper",
        "authorships": [{"author": {"id": "https://openalex.org/A1", "display_name": "Alice"}}],
    }]
    mock_client.get_citing_works_for_works.return_value = [{
        "id": "https://openalex.org/W2",
        "authorships": [{"author": {"id": "https://openalex.org/A2", "display_name": "Bob"}}],
        "referenced_works": ["https://openalex.org/W1"],
    }]

    backend = OpenAlexBackend(mock_client, work_edge_types={"authorship"})
    result = await backend.get_neighbors_batch(["W1"])

    assert all(c.connection_type == "authorship" for c in result["W1"])


async def test_get_neighbors_batch_mixed_work_and_author_ids():
    mock_client = AsyncMock()
    mock_client.get_works_by_authors.return_value = []
    mock_client.get_authors_batch.return_value = []
    mock_client.get_works_batch.return_value = [{
        "id": "https://openalex.org/W1",
        "title": "A Paper",
        "authorships": [{"author": {"id": "https://openalex.org/A9", "display_name": "Dave"}}],
    }]
    mock_client.get_citing_works_for_works.return_value = []

    backend = OpenAlexBackend(mock_client)
    result = await backend.get_neighbors_batch(["A1", "W1"])

    assert result["W1"][0].target_author_id == "A9"
    assert result["A1"] == []


async def test_overlapping_batch_fetches_disjoint_ids_immediately():
    """A batch must not queue behind another batch's fetch for ids it doesn't share.

    Old behavior: batch 2 needing {A1, A2} blocked on A1's lock (held by batch 1)
    before fetching A2. New behavior: it fetches A2 right away and only awaits A1.
    """
    mock_client = AsyncMock()
    a1_started = asyncio.Event()
    release_a1 = asyncio.Event()
    a2_started = asyncio.Event()

    async def works(author_ids):
        if "A1" in author_ids:
            a1_started.set()
            await release_a1.wait()
        if "A2" in author_ids:
            a2_started.set()
        return []

    mock_client.get_works_by_authors.side_effect = works
    mock_client.get_authors_batch.return_value = []

    backend = OpenAlexBackend(mock_client)
    first = asyncio.create_task(backend.get_neighbors_batch(["A1"]))
    await a1_started.wait()
    second = asyncio.create_task(backend.get_neighbors_batch(["A1", "A2"]))

    # A2's fetch must start while A1's fetch is still blocked.
    await asyncio.wait_for(a2_started.wait(), timeout=1)

    release_a1.set()
    assert await first == {"A1": []}
    assert await second == {"A1": [], "A2": []}


async def test_owner_failure_unblocks_waiters():
    """If the owning batch's fetch raises, waiters resolve to [] instead of hanging."""
    fetch_started = asyncio.Event()
    release = asyncio.Event()

    class BoomStore(NeighborStore):
        async def fetch(self, ids):
            fetch_started.set()
            await release.wait()
            raise RuntimeError("store down")

    mock_client = AsyncMock()
    backend = OpenAlexBackend(mock_client, neighbor_cache=NeighborCache(store=BoomStore()))
    first = asyncio.create_task(backend.get_neighbors_batch(["A1"]))
    await fetch_started.wait()
    second = asyncio.create_task(backend.get_neighbors_batch(["A1"]))
    await asyncio.sleep(0)  # let `second` attach to the in-flight future
    release.set()

    with pytest.raises(RuntimeError):
        await first
    assert await asyncio.wait_for(second, timeout=1) == {"A1": []}


async def test_cached_only_batch_never_hits_client():
    mock_client = AsyncMock()
    mock_client.get_works_by_authors.return_value = [
        make_work("W1", "Paper", [("A1", "Alice"), ("A2", "Bob")])
    ]
    mock_client.get_authors_batch.return_value = []

    backend = OpenAlexBackend(mock_client, edge_types={"coauthor"})
    await backend.get_neighbors_batch(["A1"])  # populate the ring cache
    mock_client.reset_mock()

    result = await backend.get_neighbors_batch(["A1", "A9"], cached_only=True)

    assert {c.target_author_id for c in result["A1"]} == {"A2"}
    assert result["A9"] == []  # uncached id resolves empty — no fetch
    mock_client.get_works_by_authors.assert_not_called()
    mock_client.get_authors_batch.assert_not_called()
