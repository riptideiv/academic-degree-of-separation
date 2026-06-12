from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from backend.bigquery_backend import BigQueryBackend
from backend.models import Connection


def make_backend(edge_types=None):
    with patch("backend.bigquery_backend.bigquery.Client"):
        return BigQueryBackend("test-project", edge_types=edge_types)


async def test_coauthor_batch_returns_connections():
    backend = make_backend(edge_types={"coauthor"})
    fake_rows = [
        {"source_id": "A1", "target_id": "A2", "target_name": "Bob",
         "connection_type": "coauthor", "label": "Paper 1"},
    ]
    with patch.object(backend, "_run_query", AsyncMock(return_value=fake_rows)):
        result = await backend.get_neighbors_batch(["A1"])

    assert len(result["A1"]) == 1
    assert result["A1"][0].target_author_id == "A2"
    assert result["A1"][0].connection_type == "coauthor"
    assert result["A1"][0].label == "Paper 1"


async def test_batch_deduplicates_across_edge_types():
    backend = make_backend(edge_types={"coauthor", "institution"})
    # Same target returned by both coauthor and institution queries
    coauthor_rows = [
        {"source_id": "A1", "target_id": "A2", "target_name": "Bob",
         "connection_type": "coauthor", "label": "Paper 1"},
    ]
    institution_rows = [
        {"source_id": "A1", "target_id": "A2", "target_name": "Bob",
         "connection_type": "institution", "label": "MIT"},
    ]
    call_count = 0

    async def fake_run_query(sql, params):
        nonlocal call_count
        call_count += 1
        return coauthor_rows if call_count == 1 else institution_rows

    with patch.object(backend, "_run_query", side_effect=fake_run_query):
        result = await backend.get_neighbors_batch(["A1"])

    # A2 should appear only once (first seen wins)
    assert len(result["A1"]) == 1
    assert result["A1"][0].target_author_id == "A2"


async def test_batch_multiple_source_authors():
    backend = make_backend(edge_types={"coauthor"})
    fake_rows = [
        {"source_id": "A1", "target_id": "A3", "target_name": "Carol",
         "connection_type": "coauthor", "label": "Paper X"},
        {"source_id": "A2", "target_id": "A4", "target_name": "Dave",
         "connection_type": "coauthor", "label": "Paper Y"},
    ]
    with patch.object(backend, "_run_query", AsyncMock(return_value=fake_rows)):
        result = await backend.get_neighbors_batch(["A1", "A2"])

    assert result["A1"][0].target_author_id == "A3"
    assert result["A2"][0].target_author_id == "A4"


async def test_failed_query_returns_empty_for_affected_authors():
    backend = make_backend(edge_types={"coauthor"})
    with patch.object(backend, "_run_query", AsyncMock(side_effect=Exception("BQ error"))):
        result = await backend.get_neighbors_batch(["A1"])

    assert result["A1"] == []


async def test_excludes_frontier_authors_from_results():
    backend = make_backend(edge_types={"coauthor"})
    fake_rows = [
        # A2 is in the frontier — should be filtered out
        {"source_id": "A1", "target_id": "A2", "target_name": "Bob",
         "connection_type": "coauthor", "label": "Paper 1"},
        {"source_id": "A1", "target_id": "A3", "target_name": "Carol",
         "connection_type": "coauthor", "label": "Paper 1"},
    ]
    with patch.object(backend, "_run_query", AsyncMock(return_value=fake_rows)):
        result = await backend.get_neighbors_batch(["A1", "A2"])

    ids = [c.target_author_id for c in result["A1"]]
    assert "A2" not in ids
    assert "A3" in ids


async def test_edge_type_filtering_skips_unused_queries():
    backend = make_backend(edge_types={"institution"})
    with patch.object(backend, "_query_coauthors", AsyncMock()) as mock_co, \
         patch.object(backend, "_query_citations", AsyncMock()) as mock_cit, \
         patch.object(backend, "_query_institutions", AsyncMock(return_value=[])) as mock_inst:
        await backend.get_neighbors_batch(["A1"])

    mock_co.assert_not_called()
    mock_cit.assert_not_called()
    mock_inst.assert_called_once()
