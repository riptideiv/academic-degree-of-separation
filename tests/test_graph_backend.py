import pytest
from unittest.mock import AsyncMock
from backend.graph_backend import OpenAlexBackend
from backend.models import Connection


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
