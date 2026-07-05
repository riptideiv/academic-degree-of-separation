import asyncio
import json
import pytest
from unittest.mock import AsyncMock, patch
from httpx import AsyncClient, ASGITransport
from backend.app import app
from backend.models import AuthorResult, WorkResult


async def test_search_authors_returns_results():
    mock_results = [
        AuthorResult(id="A1", display_name="Alice", institution="MIT", works_count=10)
    ]
    with patch("backend.app._client") as mock_client:
        mock_client.search_authors = AsyncMock(return_value=(mock_results, 1))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get("/api/authors?q=Alice")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["results"]) == 1
    assert data["results"][0]["display_name"] == "Alice"
    assert data["results"][0]["id"] == "A1"
    assert data["page"] == 1
    assert data["per_page"] == 20
    assert data["total"] == 1
    assert data["total_pages"] == 1


async def test_search_authors_pagination_params():
    mock_results = [
        AuthorResult(id=f"A{i}", display_name=f"Author {i}", works_count=1)
        for i in range(20)
    ]
    with patch("backend.app._client") as mock_client:
        mock_client.search_authors = AsyncMock(return_value=(mock_results, 45))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get("/api/authors?q=Alice&page=2&per_page=20")
    assert resp.status_code == 200
    data = resp.json()
    mock_client.search_authors.assert_awaited_once_with("Alice", page=2, per_page=20)
    assert data["page"] == 2
    assert data["total"] == 45
    assert data["total_pages"] == 3


async def test_get_author_top_works_returns_results():
    mock_works = [
        {
            "id": "https://openalex.org/W1", "title": "Paper One", "cited_by_count": 50,
            "publication_year": 2020, "doi": "https://doi.org/10.1/abc",
        },
    ]
    with patch("backend.app._client") as mock_client:
        mock_client.get_author_works = AsyncMock(return_value=mock_works)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get("/api/authors/A1/works?limit=10")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["id"] == "W1"
    assert data[0]["title"] == "Paper One"
    assert data[0]["cited_by_count"] == 50
    assert data[0]["publication_year"] == 2020
    assert data[0]["doi"] == "https://doi.org/10.1/abc"
    mock_client.get_author_works.assert_awaited_once_with("A1", limit=10)


async def test_search_works_returns_results():
    mock_results = [WorkResult(id="W1", title="Paper One", author_names=["Alice"])]
    with patch("backend.app._client") as mock_client:
        mock_client.search_works = AsyncMock(return_value=(mock_results, 1))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get("/api/works?q=test")
    assert resp.status_code == 200
    data = resp.json()
    assert data["results"][0]["id"] == "W1"
    assert data["results"][0]["author_names"] == ["Alice"]
    assert data["total"] == 1
    assert data["total_pages"] == 1


async def test_graph_expand_emits_work_node_for_work_origin():
    """Adding a work (not an author) as the new origin emits a type='work' node."""
    async def mock_expand_graph(*args, **kwargs):
        return
        yield  # pragma: no cover

    with patch("backend.app._client") as mock_client, \
         patch("backend.app.OpenAlexBackend"), \
         patch("backend.graph_expand.expand_graph", mock_expand_graph), \
         patch("backend.graph_expand.stitch_edges", AsyncMock(return_value=[])):
        mock_client.get_work = AsyncMock(return_value={
            "title": "Some Paper", "cited_by_count": 42, "publication_year": 2019,
        })
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            async with ac.stream("GET", "/api/graph/expand?new_id=W1") as resp:
                assert resp.status_code == 200
                chunks = []
                async for chunk in resp.aiter_text():
                    chunks.append(chunk)

    full_text = "".join(chunks)
    lines = full_text.splitlines()
    node_events = []
    for i, line in enumerate(lines):
        if line.strip() == "event: node":
            node_events.append(json.loads(lines[i + 1][5:].strip()))

    assert len(node_events) == 1
    n = node_events[0]
    assert n["id"] == "W1"
    assert n["type"] == "work"
    assert n["name"] == "Some Paper"
    assert n["cited_by_count"] == 42
    assert n["publication_year"] == 2019
    mock_client.get_author.assert_not_called()


async def test_collect_path_to_work_endpoint():
    """An existing work origin as the path target dispatches through get_work, not get_author."""
    mock_path = [
        {"author_id": "A1", "author_name": "Alice", "connection_to_next": "authorship", "label": "Some Paper"},
        {"author_id": "W1", "author_name": "Some Paper", "connection_to_next": None, "label": None},
    ]

    async def mock_find_path(*args, **kwargs):
        yield {"type": "result", "found": True, "path": mock_path, "hops": 1}

    async def mock_expand_graph(*args, **kwargs):
        return
        yield  # pragma: no cover

    with patch("backend.app._client") as mock_client, \
         patch("backend.app.find_path", mock_find_path), \
         patch("backend.app.OpenAlexBackend"), \
         patch("backend.graph_expand.expand_graph", mock_expand_graph), \
         patch("backend.graph_expand.stitch_edges", AsyncMock(return_value=[])):
        mock_client.get_author = AsyncMock(return_value={
            "display_name": "Alice", "works_count": 5, "cited_by_count": 10,
            "last_known_institutions": [],
        })
        mock_client.get_work = AsyncMock(return_value={
            "title": "Some Paper", "cited_by_count": 99, "publication_year": 2018,
        })
        mock_client.get_authors_batch = AsyncMock(return_value=[])
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            async with ac.stream("GET", "/api/graph/expand?new_id=A1&origin_ids=W1") as resp:
                assert resp.status_code == 200
                chunks = []
                async for chunk in resp.aiter_text():
                    chunks.append(chunk)

    full_text = "".join(chunks)
    lines = full_text.splitlines()
    node_events = []
    for i, line in enumerate(lines):
        if line.strip() == "event: node":
            node_events.append(json.loads(lines[i + 1][5:].strip()))

    work_nodes = [n for n in node_events if n["id"] == "W1"]
    assert len(work_nodes) == 1
    assert work_nodes[0]["type"] == "work"
    assert work_nodes[0]["cited_by_count"] == 99
    assert work_nodes[0]["publication_year"] == 2018


async def test_search_authors_requires_q():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get("/api/authors")
    assert resp.status_code == 422


async def test_path_sse_streams_events():
    mock_path = [
        {"author_id": "A1", "author_name": "Alice", "connection_to_next": "coauthor", "label": "Paper"},
        {"author_id": "A2", "author_name": "Bob", "connection_to_next": None, "label": None},
    ]

    async def mock_find_path(*args, **kwargs):
        yield {"type": "progress", "message": "Searching..."}
        yield {"type": "result", "found": True, "path": mock_path, "hops": 1}

    with patch("backend.app._client") as mock_client, \
         patch("backend.app.find_path", mock_find_path), \
         patch("backend.app.OpenAlexBackend"):
        mock_client.get_author = AsyncMock(side_effect=lambda aid: {
            "display_name": {"A1": "Alice", "A2": "Bob"}.get(aid, aid)
        })
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            async with ac.stream("GET", "/api/path?from=A1&to=A2") as resp:
                assert resp.status_code == 200
                assert "text/event-stream" in resp.headers["content-type"]
                chunks = []
                async for chunk in resp.aiter_text():
                    chunks.append(chunk)

    full_text = "".join(chunks)
    events = []
    for line in full_text.splitlines():
        if line.startswith("data:"):
            events.append(json.loads(line[5:].strip()))

    assert any(e.get("type") == "progress" for e in events)
    result_events = [e for e in events if e.get("type") == "result"]
    assert len(result_events) == 1
    assert result_events[0]["found"] is True

    # Also check event: lines to verify SSE event type names
    event_type_lines = [l for l in full_text.splitlines() if l.startswith("event:")]
    event_type_names = [l.split(":", 1)[1].strip() for l in event_type_lines]
    assert "progress" in event_type_names
    assert "result" in event_type_names


async def test_path_sse_passes_edge_types_to_backend():
    captured = {}

    async def mock_find_path(*args, **kwargs):
        yield {"type": "result", "found": False, "reason": "No path found"}

    class CapturingBackend:
        def __init__(self, client, edge_types=None, **kwargs):
            captured["edge_types"] = edge_types

        async def get_neighbors(self, author_id):
            return []

    with patch("backend.app._client") as mock_client, \
         patch("backend.app.find_path", mock_find_path), \
         patch("backend.app.OpenAlexBackend", CapturingBackend):
        mock_client.get_author = AsyncMock(return_value={"display_name": "Alice"})
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            async with ac.stream("GET", "/api/path?from=A1&to=A2&edges=coauthor&edges=institution") as resp:
                assert resp.status_code == 200
                async for _ in resp.aiter_text():
                    pass

    assert captured["edge_types"] == {"coauthor", "institution"}


async def test_graph_expand_emits_path_event():
    """Adding a researcher with an existing origin streams a `path` event carrying hops."""
    mock_path = [
        {"author_id": "A1", "author_name": "Alice", "connection_to_next": "coauthor", "label": "Paper"},
        {"author_id": "A2", "author_name": "Bob", "connection_to_next": None, "label": None},
    ]

    async def mock_find_path(*args, **kwargs):
        yield {"type": "progress", "message": "Searching..."}
        yield {"type": "result", "found": True, "path": mock_path, "hops": 1}

    async def mock_expand_graph(*args, **kwargs):
        # Empty async generator, so the test skips real neighborhood expansion.
        return
        yield  # pragma: no cover

    with patch("backend.app._client") as mock_client, \
         patch("backend.app.find_path", mock_find_path), \
         patch("backend.app.OpenAlexBackend"), \
         patch("backend.graph_expand.expand_graph", mock_expand_graph), \
         patch("backend.graph_expand.stitch_edges", AsyncMock(return_value=[])):
        mock_client.get_author = AsyncMock(side_effect=lambda aid: {
            "display_name": {"A1": "Alice", "A2": "Bob"}.get(aid, aid)
        })
        # _collect_path backfills path-node metadata via a batched author lookup.
        mock_client.get_authors_batch = AsyncMock(return_value=[])
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            async with ac.stream("GET", "/api/graph/expand?new_id=A1&origin_ids=A2") as resp:
                assert resp.status_code == 200
                chunks = []
                async for chunk in resp.aiter_text():
                    chunks.append(chunk)

    full_text = "".join(chunks)
    assert "event: path" in full_text

    path_events = []
    lines = full_text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "event: path":
            data_line = lines[i + 1]
            assert data_line.startswith("data:")
            path_events.append(json.loads(data_line[5:].strip()))

    assert len(path_events) == 1
    pe = path_events[0]
    assert pe["found"] is True
    assert pe["hops"] == 1
    assert pe["from_id"] == "A1"
    assert pe["to_id"] == "A2"
    # The ordered shortest-path steps (names + paper) are included for the sidebar.
    assert pe["steps"] == [
        {"from_name": "Alice", "to_name": "Bob", "type": "coauthor", "label": "Paper", "direction": None}
    ]


async def test_path_sse_yields_app_error_on_exception():
    with patch("backend.app._client") as mock_client:
        mock_client.get_author = AsyncMock(side_effect=RuntimeError("API down"))
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            async with ac.stream("GET", "/api/path?from=A1&to=A2") as resp:
                assert resp.status_code == 200
                chunks = []
                async for chunk in resp.aiter_text():
                    chunks.append(chunk)

    full_text = "".join(chunks)
    assert "event: app_error" in full_text
    for line in full_text.splitlines():
        if line.startswith("data:"):
            data = json.loads(line[5:].strip())
            if "message" in data:
                assert "API down" in data["message"]
                break


async def test_graph_expand_failure_still_emits_paths_and_app_error():
    """A phase-1 expansion failure flushes gathered path results before app_error."""
    mock_path = [
        {"author_id": "A1", "author_name": "Alice", "connection_to_next": "coauthor", "label": "Paper"},
        {"author_id": "A2", "author_name": "Bob", "connection_to_next": None, "label": None},
    ]

    async def mock_find_path(*args, **kwargs):
        yield {"type": "result", "found": True, "path": mock_path, "hops": 1}

    async def mock_expand_graph(*args, **kwargs):
        raise RuntimeError("store down")
        yield  # pragma: no cover

    with patch("backend.app._client") as mock_client, \
         patch("backend.app.find_path", mock_find_path), \
         patch("backend.app.OpenAlexBackend"), \
         patch("backend.graph_expand.expand_graph", mock_expand_graph):
        mock_client.get_author = AsyncMock(side_effect=lambda aid: {
            "display_name": {"A1": "Alice", "A2": "Bob"}.get(aid, aid)
        })
        mock_client.get_authors_batch = AsyncMock(return_value=[])
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            async with ac.stream("GET", "/api/graph/expand?new_id=A1&origin_ids=A2") as resp:
                assert resp.status_code == 200
                chunks = [c async for c in resp.aiter_text()]

    full_text = "".join(chunks)
    assert "event: path" in full_text
    assert "event: app_error" in full_text
    assert "store down" in full_text
    assert "event: done" not in full_text
    assert full_text.index("event: path") < full_text.index("event: app_error")


async def test_graph_expand_stitch_suppresses_already_streamed_edges():
    """The final stitch receives the canonical keys of every edge already streamed."""
    async def mock_expand_graph(*args, **kwargs):
        yield {
            "type": "expansion", "depth": 1,
            "nodes": [{"id": "A9", "name": "Nine", "type": "expansion", "depth": 1}],
            "edges": [{"source": "A1", "target": "A9", "type": "coauthor", "label": "", "direction": None}],
        }

    stitch_mock = AsyncMock(return_value=[])
    with patch("backend.app._client") as mock_client, \
         patch("backend.app.OpenAlexBackend"), \
         patch("backend.graph_expand.expand_graph", mock_expand_graph), \
         patch("backend.graph_expand.stitch_edges", stitch_mock):
        mock_client.get_author = AsyncMock(return_value={"display_name": "Alice"})
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            async with ac.stream("GET", "/api/graph/expand?new_id=A1") as resp:
                assert resp.status_code == 200
                async for _ in resp.aiter_text():
                    pass

    stitch_mock.assert_awaited_once()
    emitted = stitch_mock.await_args.args[2]
    assert ("A1", "A9", "coauthor") in emitted


async def test_graph_expand_emits_path_as_soon_as_search_finishes():
    """Path results flush between phase-1 expansion events once the gather is done."""
    mock_path = [
        {"author_id": "A1", "author_name": "Alice", "connection_to_next": "coauthor", "label": "Paper"},
        {"author_id": "A2", "author_name": "Bob", "connection_to_next": None, "label": None},
    ]

    async def mock_find_path(*args, **kwargs):
        yield {"type": "result", "found": True, "path": mock_path, "hops": 1}

    async def mock_expand_graph(*args, **kwargs):
        for depth in (1, 2, 3):
            yield {"type": "expansion", "depth": depth, "nodes": [], "edges": []}
            await asyncio.sleep(0.05)

    with patch("backend.app._client") as mock_client, \
         patch("backend.app.find_path", mock_find_path), \
         patch("backend.app.OpenAlexBackend"), \
         patch("backend.graph_expand.expand_graph", mock_expand_graph), \
         patch("backend.graph_expand.stitch_edges", AsyncMock(return_value=[])):
        mock_client.get_author = AsyncMock(side_effect=lambda aid: {
            "display_name": {"A1": "Alice", "A2": "Bob"}.get(aid, aid)
        })
        mock_client.get_authors_batch = AsyncMock(return_value=[])
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            async with ac.stream("GET", "/api/graph/expand?new_id=A1&origin_ids=A2") as resp:
                assert resp.status_code == 200
                chunks = [c async for c in resp.aiter_text()]

    lines = "".join(chunks).splitlines()
    path_indices = [i for i, l in enumerate(lines) if l.strip() == "event: path"]
    expansion_indices = [i for i, l in enumerate(lines) if l.strip() == "event: expansion"]
    assert len(path_indices) == 1
    assert path_indices[0] < expansion_indices[-1]


async def test_clear_cache_wipes_author_lru_too():
    with patch("backend.app._client") as mock_client, \
         patch("backend.app._cache") as mock_cache:
        mock_cache.clear = AsyncMock()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.delete("/api/cache")
    assert resp.status_code == 200
    mock_cache.clear.assert_awaited_once()
    mock_client.clear_author_cache.assert_called_once()
