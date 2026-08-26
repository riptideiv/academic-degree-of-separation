import asyncio

import pytest
from unittest.mock import AsyncMock
from backend.graph_backend import IdentityScopedBackend, OpenAlexBackend, _NeighborBatch
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


class WorkBatch(list):
    """List-shaped test result carrying the optional client completeness signal."""

    def __init__(self, values, *, complete):
        super().__init__(values)
        self.complete = complete


class SummaryBatch(dict):
    """Dict-shaped grouped coauthor result with explicit completeness."""

    def __init__(self, values, *, complete):
        super().__init__(values)
        self.complete = complete


class RecordingStore(NeighborStore):
    def __init__(self):
        self.recorded = []

    async def fetch(self, ids):
        return {}

    def record(self, entries):
        self.recorded.append(entries)


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
    mock_client.get_citing_works_for_works.assert_not_awaited()
    mock_client.get_works_batch.assert_not_awaited()
    mock_client.get_authors_batch.assert_not_awaited()
    mock_client.get_institution_authors_batch.assert_not_awaited()


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


async def test_batch_failed_subquery_propagates():
    mock_client = AsyncMock()
    mock_client.get_works_by_authors.side_effect = Exception("API error")
    mock_client.get_authors_batch.return_value = []

    backend = OpenAlexBackend(mock_client, edge_types={"coauthor"})
    with pytest.raises(Exception, match="API error"):
        await backend.get_neighbors_batch(["A1"])

    assert backend._cache.get_memory(backend._cache_key("A1")) is None


async def test_failed_sibling_query_does_not_cache_partial_ring_and_retry_succeeds():
    mock_client = AsyncMock()
    mock_client.get_works_by_authors.return_value = [
        make_work("W1", "Paper AB", [("A1", "Alice"), ("A2", "Bob")])
    ]
    mock_client.get_citing_works_for_works.return_value = []
    mock_client.get_authors_batch.side_effect = [
        RuntimeError("author metadata temporarily unavailable"),
        WorkBatch([], complete=True),
    ]
    store = RecordingStore()
    cache = NeighborCache(store=store)
    backend = OpenAlexBackend(
        mock_client,
        edge_types={"coauthor", "institution"},
        neighbor_cache=cache,
    )

    with pytest.raises(RuntimeError, match="temporarily unavailable"):
        await backend.get_neighbors_batch(["A1"])

    # The successful publication subquery must not leak into either cache layer.
    assert cache.get_memory(backend._cache_key("A1")) is None
    assert store.recorded == []

    result = await backend.get_neighbors_batch(["A1"])

    assert {edge.target_author_id for edge in result["A1"]} == {"A2"}
    assert mock_client.get_works_by_authors.await_count == 2
    assert len(store.recorded) == 1


async def test_shared_work_page_refetches_only_unrepresented_authors():
    mock_client = AsyncMock()

    async def works_for(author_ids):
        if author_ids == ["A1", "A2"]:
            # The prolific author's work consumes the shared page; A2 is absent.
            return [
                make_work("W1", "Prolific paper", [("A1", "Alice"), ("A9", "Pat")])
            ]
        if author_ids == ["A2"]:
            return [
                make_work("W2", "Low-output paper", [("A2", "Bob"), ("A3", "Carol")])
            ]
        raise AssertionError(f"unexpected refetch: {author_ids}")

    mock_client.get_works_by_authors.side_effect = works_for
    mock_client.get_citing_works_for_works.return_value = []
    mock_client.get_authors_batch.return_value = []
    backend = OpenAlexBackend(mock_client, edge_types={"coauthor"})

    result = await backend.get_neighbors_batch(["A1", "A2"])

    assert {edge.target_author_id for edge in result["A1"]} == {"A9"}
    assert {edge.target_author_id for edge in result["A2"]} == {"A3"}
    assert [
        awaited.args[0] for awaited in mock_client.get_works_by_authors.await_args_list
    ] == [["A1", "A2"], ["A2"]]

    # Both complete rings are warm; neither author is fetched again.
    await backend.get_neighbors_batch(["A1", "A2"])
    assert mock_client.get_works_by_authors.await_count == 2


async def test_failed_individual_recovery_does_not_poison_any_frontier_ring():
    mock_client = AsyncMock()
    individual_attempts = 0

    async def works_for(author_ids):
        nonlocal individual_attempts
        if author_ids == ["A1", "A2"]:
            return [
                make_work("W1", "Prolific paper", [("A1", "Alice"), ("A9", "Pat")])
            ]
        if author_ids == ["A2"]:
            individual_attempts += 1
            if individual_attempts == 1:
                raise RuntimeError("individual fetch failed")
            return [
                make_work("W2", "Recovered paper", [("A2", "Bob"), ("A3", "Carol")])
            ]
        raise AssertionError(f"unexpected fetch: {author_ids}")

    mock_client.get_works_by_authors.side_effect = works_for
    mock_client.get_citing_works_for_works.return_value = []
    mock_client.get_authors_batch.return_value = []
    store = RecordingStore()
    cache = NeighborCache(store=store)
    backend = OpenAlexBackend(
        mock_client,
        edge_types={"coauthor"},
        neighbor_cache=cache,
    )

    with pytest.raises(RuntimeError, match="individual fetch failed"):
        await backend.get_neighbors_batch(["A1", "A2"])

    assert cache.get_memory(backend._cache_key("A1")) is None
    assert cache.get_memory(backend._cache_key("A2")) is None
    assert store.recorded == []

    result = await backend.get_neighbors_batch(["A1", "A2"])

    assert {edge.target_author_id for edge in result["A1"]} == {"A9"}
    assert {edge.target_author_id for edge in result["A2"]} == {"A3"}
    assert individual_attempts == 2
    assert len(store.recorded) == 1


async def test_complete_grouped_summary_recovers_coauthor_beyond_top_work_page():
    mock_client = AsyncMock()
    mock_client.get_works_by_authors.return_value = WorkBatch(
        [make_work("W1", "Top paper", [("A1", "Alice"), ("A2", "Bob")])],
        complete=False,
    )
    mock_client.get_coauthor_summary.return_value = SummaryBatch({
        "A2": {"name": "Bob", "works_count": 1},
        "A51": {"name": "Fifty First", "works_count": 1},
    }, complete=True)
    store = RecordingStore()
    cache = NeighborCache(store=store)
    backend = OpenAlexBackend(
        mock_client,
        edge_types={"coauthor"},
        neighbor_cache=cache,
    )

    result = await backend.get_neighbors_batch(["A1"])

    assert {edge.target_author_id for edge in result["A1"]} == {"A2", "A51"}
    assert result.complete_ids == {"A1"}
    assert cache.get_memory(backend._cache_key("A1")) is not None
    assert [set(record) for record in store.recorded] == [{backend._cache_key("A1")}]
    mock_client.get_coauthor_summary.assert_awaited_once_with("A1")


async def test_incomplete_grouped_summary_augments_current_ring_but_stays_incomplete():
    mock_client = AsyncMock()
    mock_client.get_works_by_authors.return_value = WorkBatch(
        [make_work("W1", "Partial paper", [("A1", "Alice"), ("A2", "Bob")])],
        complete=False,
    )
    mock_client.get_coauthor_summary.return_value = SummaryBatch({
        "A51": {"name": "Fifty First", "works_count": 1},
    }, complete=False)
    store = RecordingStore()
    cache = NeighborCache(store=store)
    backend = OpenAlexBackend(
        mock_client,
        edge_types={"coauthor"},
        neighbor_cache=cache,
    )

    result = await backend.get_neighbors_batch(["A1"])

    assert {edge.target_author_id for edge in result["A1"]} == {"A2", "A51"}
    assert result.complete_ids == set()
    assert cache.get_memory(backend._cache_key("A1")) is None
    assert store.recorded == []

    await backend.get_neighbors_batch(["A1"])
    assert mock_client.get_works_by_authors.await_count == 2


async def test_truncated_authorship_evidence_is_not_cached():
    mock_client = AsyncMock()
    mock_client.get_works_by_authors.return_value = [{
        **make_work(
            "W1",
            "Truncated paper",
            [("A1", "Alice"), ("A2", "Bob")],
        ),
        "is_authors_truncated": True,
    }]
    store = RecordingStore()
    cache = NeighborCache(store=store)
    backend = OpenAlexBackend(
        mock_client,
        edge_types={"coauthor"},
        neighbor_cache=cache,
    )

    with pytest.raises(RuntimeError, match="Truncated authorship evidence"):
        await backend.get_neighbors_batch(["A1"])

    assert cache.get_memory(backend._cache_key("A1")) is None
    assert store.recorded == []


async def test_incomplete_shared_page_recovers_and_caches_only_complete_authors():
    mock_client = AsyncMock()

    async def works_for(author_ids):
        if author_ids == ["A1", "A2"]:
            return WorkBatch([
                make_work("W1", "Prolific paper", [("A1", "Alice"), ("A9", "Pat")])
            ], complete=False)
        if author_ids == ["A1"]:
            return WorkBatch([
                make_work("W1", "Prolific paper", [("A1", "Alice"), ("A9", "Pat")])
            ], complete=False)
        if author_ids == ["A2"]:
            return WorkBatch([
                make_work("W2", "Low-output paper", [("A2", "Bob"), ("A3", "Carol")])
            ], complete=True)
        raise AssertionError(f"unexpected fetch: {author_ids}")

    mock_client.get_works_by_authors.side_effect = works_for
    mock_client.get_coauthor_summary.return_value = SummaryBatch({
        "A10": {"name": "Tenth", "works_count": 1},
    }, complete=False)
    store = RecordingStore()
    cache = NeighborCache(store=store)
    backend = OpenAlexBackend(
        mock_client,
        edge_types={"coauthor"},
        neighbor_cache=cache,
    )

    result = await backend.get_neighbors_batch(["A1", "A2"])

    assert {edge.target_author_id for edge in result["A1"]} == {"A9", "A10"}
    assert {edge.target_author_id for edge in result["A2"]} == {"A3"}
    assert result.complete_ids == {"A2"}
    assert cache.get_memory(backend._cache_key("A1")) is None
    assert cache.get_memory(backend._cache_key("A2")) is not None
    assert [set(record) for record in store.recorded] == [{backend._cache_key("A2")}]


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

    first_result = await first
    second_result = await second
    assert first_result == {"A1": []}
    assert second_result == {"A1": []}
    assert first_result.complete_ids == set()
    assert second_result.complete_ids == set()
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
    # A scoped work ring fetches and caches only its configured edge types.
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
    mock_client.get_citing_works_for_works.assert_not_awaited()


async def test_cache_is_namespaced_by_active_edge_set():
    mock_client = AsyncMock()
    mock_client.get_works_by_authors.return_value = [
        make_work("W1", "Paper", [("A1", "Alice"), ("A2", "Bob")])
    ]
    mock_client.get_authors_batch.return_value = [{
        "id": "https://openalex.org/A1",
        "display_name": "Alice",
        "last_known_institutions": [{
            "id": "https://openalex.org/I1",
            "display_name": "MIT",
        }],
    }]
    mock_client.get_institution_authors_batch.return_value = [{
        "id": "https://openalex.org/A3",
        "display_name": "Carol",
        "last_known_institutions": [{
            "id": "https://openalex.org/I1",
            "display_name": "MIT",
        }],
    }]
    cache = NeighborCache()
    coauthor_backend = OpenAlexBackend(
        mock_client,
        edge_types={"coauthor"},
        neighbor_cache=cache,
    )
    institution_backend = OpenAlexBackend(
        mock_client,
        edge_types={"institution"},
        neighbor_cache=cache,
    )

    coauthors = await coauthor_backend.get_neighbors_batch(["A1"])
    institutions = await institution_backend.get_neighbors_batch(["A1"])

    assert {edge.target_author_id for edge in coauthors["A1"]} == {"A2"}
    assert {edge.target_author_id for edge in institutions["A1"]} == {"A3"}
    mock_client.get_works_by_authors.assert_awaited_once_with(["A1"])
    mock_client.get_authors_batch.assert_awaited_once_with(["A1"])


async def test_plain_bounded_citation_ring_is_not_cached_as_complete():
    mock_client = AsyncMock()
    mock_client.get_works_by_authors.return_value = [
        make_work("W1", "Original", [("A1", "Alice")])
    ]
    mock_client.get_citing_works_for_works.return_value = [{
        "id": "https://openalex.org/W2",
        "authorships": [{
            "author": {"id": "https://openalex.org/A3", "display_name": "Carol"}
        }],
        "referenced_works": ["https://openalex.org/W1"],
    }]
    store = RecordingStore()
    cache = NeighborCache(store=store)
    backend = OpenAlexBackend(
        mock_client,
        edge_types={"citation"},
        neighbor_cache=cache,
    )

    result = await backend.get_neighbors_batch(["A1"])

    assert [edge.target_author_id for edge in result["A1"]] == ["A3"]
    assert result.complete_ids == set()
    assert cache.get_memory(backend._cache_key("A1")) is None
    assert store.recorded == []


async def test_plain_bounded_institution_ring_is_not_cached_as_complete():
    mock_client = AsyncMock()
    mock_client.get_authors_batch.return_value = [{
        "id": "https://openalex.org/A1",
        "display_name": "Alice",
        "last_known_institutions": [{
            "id": "https://openalex.org/I1",
            "display_name": "MIT",
        }],
    }]
    mock_client.get_institution_authors_batch.return_value = [{
        "id": "https://openalex.org/A3",
        "display_name": "Carol",
        "last_known_institutions": [{
            "id": "https://openalex.org/I1",
            "display_name": "MIT",
        }],
    }]
    store = RecordingStore()
    cache = NeighborCache(store=store)
    backend = OpenAlexBackend(
        mock_client,
        edge_types={"institution"},
        neighbor_cache=cache,
    )

    result = await backend.get_neighbors_batch(["A1"])

    assert [edge.target_author_id for edge in result["A1"]] == ["A3"]
    assert result.complete_ids == set()
    assert cache.get_memory(backend._cache_key("A1")) is None
    assert store.recorded == []


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
    """If the owning batch's fetch raises, waiters retry the fetch themselves."""
    fetch_started = asyncio.Event()
    release = asyncio.Event()

    class FlakyStore(NeighborStore):
        def __init__(self):
            self.calls = 0

        async def fetch(self, ids):
            self.calls += 1
            if self.calls == 1:
                fetch_started.set()
                await release.wait()
                raise RuntimeError("store down")
            return {
                i: [Connection(
                    target_author_id="A2",
                    target_name="Bob",
                    connection_type="coauthor",
                    label="Paper",
                )]
                for i in ids
            }

    store = FlakyStore()
    mock_client = AsyncMock()
    backend = OpenAlexBackend(mock_client, neighbor_cache=NeighborCache(store=store))
    first = asyncio.create_task(backend.get_neighbors_batch(["A1"]))
    await fetch_started.wait()
    second = asyncio.create_task(backend.get_neighbors_batch(["A1"]))
    await asyncio.sleep(0)  # let `second` attach to the in-flight future
    release.set()

    with pytest.raises(RuntimeError):
        await first
    result = await asyncio.wait_for(second, timeout=1)
    assert [c.target_author_id for c in result["A1"]] == ["A2"]
    assert store.calls == 2  # the waiter re-fetched after the owner failed
    mock_client.get_works_by_authors.assert_not_called()


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
    assert result.complete_ids == {"A1"}
    mock_client.get_works_by_authors.assert_not_called()
    mock_client.get_authors_batch.assert_not_called()


async def test_identity_scope_bypasses_broad_source_ring():
    inner = AsyncMock()
    client = AsyncMock()
    client.get_coauthor_summary.return_value = SummaryBatch({
        "A5051014644": {
            "name": "Marc Lange",
            "works_count": 1,
            "label": "Running it up the flagpole",
        },
    }, complete=True)
    backend = IdentityScopedBackend(inner, client, edge_types={"coauthor", "citation"})

    result = await backend.get_neighbors_batch(["A5072773992"])

    inner.get_neighbors_batch.assert_not_awaited()
    assert [(edge.target_author_id, edge.connection_type, edge.label) for edge in result["A5072773992"]] == [
        ("A5051014644", "coauthor", "Running it up the flagpole"),
    ]
    assert result.complete_ids == {"A5072773992"}
    assert set(client.get_coauthor_summary.await_args.kwargs["verified_work_ids"]) == {
        "W2891784578", "W2800467463", "W2345731203",
        "W3204177509", "W3139199066", "W3136126418",
    }


async def test_identity_scope_filters_broad_inbound_edges_both_directions():
    inner = AsyncMock()
    inner.get_neighbors_batch.return_value = {
        "A1": [
            Connection(
                target_author_id="A5072773992",
                target_name="Katrina Elliott",
                connection_type="coauthor",
                label="Unreviewed broad-profile paper",
            ),
            Connection(
                target_author_id="A2",
                target_name="Bob",
                connection_type="coauthor",
                label="Safe paper",
            ),
        ],
        "A9": [Connection(
            target_author_id="A5072773992",
            target_name="Katrina Elliott",
            connection_type="coauthor",
            label="False conflated edge",
        )],
    }
    client = AsyncMock()
    client.get_coauthor_summary.return_value = SummaryBatch({
        "A1": {
            "name": "Alice",
            "works_count": 1,
            "label": "Reviewed Katrina paper",
        },
    }, complete=True)
    backend = IdentityScopedBackend(inner, client, edge_types={"coauthor"})

    result = await backend.get_neighbors_batch(["A1", "A9"])

    assert [(edge.target_author_id, edge.label) for edge in result["A1"]] == [
        ("A5072773992", "Reviewed Katrina paper"),
        ("A2", "Safe paper"),
    ]
    assert result["A9"] == []


async def test_identity_scope_preserves_incomplete_inner_ring_signal():
    inner = AsyncMock()
    inner.get_neighbors_batch.return_value = _NeighborBatch({
        "A1": [Connection(
            target_author_id="A2",
            target_name="Bob",
            connection_type="coauthor",
            label="Partial paper",
        )],
    }, complete_ids=set())
    client = AsyncMock()
    backend = IdentityScopedBackend(inner, client, edge_types={"coauthor"})

    result = await backend.get_neighbors_batch(["A1"])

    assert [edge.target_author_id for edge in result["A1"]] == ["A2"]
    assert result.complete_ids == set()


async def test_identity_scope_incomplete_reviewed_summary_stays_incomplete():
    inner = AsyncMock()
    client = AsyncMock()
    client.get_coauthor_summary.return_value = SummaryBatch({
        "A5051014644": {
            "name": "Marc Lange",
            "works_count": 1,
            "label": "Running it up the flagpole",
        },
    }, complete=False)
    backend = IdentityScopedBackend(inner, client, edge_types={"coauthor"})

    result = await backend.get_neighbors_batch(["A5072773992"])

    assert [edge.target_author_id for edge in result["A5072773992"]] == [
        "A5051014644"
    ]
    assert result.complete_ids == set()


async def test_identity_scope_cached_only_never_fetches_scoped_ring():
    inner = AsyncMock()
    inner.get_neighbors_batch.return_value = {}
    client = AsyncMock()
    backend = IdentityScopedBackend(inner, client, edge_types={"coauthor"})

    result = await backend.get_neighbors_batch(["A5072773992"], cached_only=True)

    assert result == {"A5072773992": []}
    assert result.complete_ids == set()
    inner.get_neighbors_batch.assert_not_awaited()
    client.get_coauthor_summary.assert_not_awaited()
