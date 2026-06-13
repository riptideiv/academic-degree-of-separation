import json
import pytest
from unittest.mock import AsyncMock, patch
from httpx import AsyncClient, ASGITransport
from backend.app import app
from backend.models import AuthorResult


async def test_search_authors_returns_results():
    mock_results = [
        AuthorResult(id="A1", display_name="Alice", institution="MIT", works_count=10)
    ]
    with patch("backend.app._client") as mock_client:
        mock_client.search_authors = AsyncMock(return_value=mock_results)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            resp = await ac.get("/api/authors?q=Alice")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["display_name"] == "Alice"
    assert data[0]["id"] == "A1"


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
