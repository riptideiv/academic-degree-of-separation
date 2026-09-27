import asyncio
import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

import backend.app as api
from backend.openalex_client import OpenAlexClient, request_api_key


async def test_personal_keys_are_isolated_between_concurrent_requests(monkeypatch):
    monkeypatch.setenv("OPENALEX_KEY", "deployment-key")
    client = OpenAlexClient()
    seen_keys = []
    all_started = asyncio.Event()

    async def upstream(request):
        seen_keys.append(request.url.params.get("api_key"))
        if len(seen_keys) == 3:
            all_started.set()
        await asyncio.wait_for(all_started.wait(), timeout=1)
        return httpx.Response(200, json={"results": [], "meta": {"count": 0}})

    client._http = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    with patch.object(api, "_client", client), patch.object(client, "clear_author_cache") as clear:
        async with (
            httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="https://test") as first,
            httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="https://test") as second,
            httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="https://test") as anonymous,
        ):
            response = await first.post("/api/openalex-key", json={"api_key": "first-key"})
            assert response.json() == {"configured": True, "source": "personal"}
            cookie = response.headers["set-cookie"]
            assert "HttpOnly" in cookie and "SameSite=strict" in cookie
            assert "Secure" in cookie and "Path=/api" in cookie
            assert "Max-Age" not in cookie  # browser-session lifetime
            await second.post("/api/openalex-key", json={"api_key": "second-key"})
            await asyncio.gather(*[
                browser.get(f"/api/authors/A{index}/works")
                for index, browser in enumerate((first, second, anonymous), start=1)
            ])
            assert sorted(seen_keys) == ["deployment-key", "first-key", "second-key"]
            assert (await first.get("/api/openalex-key")).json()["source"] == "personal"
            assert (await anonymous.get("/api/openalex-key")).json()["source"] == "server"
            cleared = await first.post("/api/openalex-key", json={"api_key": ""})
            assert cleared.json() == {"configured": True, "source": "server"}
            assert (await first.get("/api/openalex-key")).json()["source"] == "server"
            assert (await second.get("/api/openalex-key")).json()["source"] == "personal"
        clear.assert_not_called()
    assert request_api_key.get() is None
    assert client._api_key == "deployment-key"
    await client.aclose()


@pytest.mark.parametrize("payload", [{}, [], {"api_key": None}, {"api_key": "x" * 513}, {"api_key": "a\nb"}])
async def test_key_payloads_are_validated_without_echoing_secrets(payload):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="https://test") as client:
        response = await client.post("/api/openalex-key", json=payload)
    assert response.status_code == 422
    assert "set-cookie" not in response.headers


async def test_key_writes_reject_cross_origin_query_secrets_and_oversized_body():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="https://test") as client:
        cross_origin = await client.post(
            "/api/openalex-key", json={"api_key": "private-key"}, headers={"Origin": "https://attacker.test"}
        )
        query = await client.post("/api/openalex-key?api_key=private-key", json={"api_key": ""})
        large = await client.post("/api/openalex-key", json={"api_key": "x" * 2000})
    assert cross_origin.status_code == 403
    assert query.status_code == 422
    assert large.status_code == 413
    assert all("private-key" not in response.text for response in (cross_origin, query))


@pytest.mark.parametrize("remote_host,hostname", [
    ("203.0.113.10", "localhost"), ("127.0.0.1", "public.test"), ("203.0.113.10", "public.test"),
    ("127.0.0.1", "localhost"),
])
async def test_remote_cache_deletion_is_disabled_without_admin_token(monkeypatch, remote_host, hostname):
    monkeypatch.delenv("CACHE_ADMIN_TOKEN", raising=False)
    transport = httpx.ASGITransport(app=api.app, client=(remote_host, 1234))
    with patch.object(api._cache, "clear", AsyncMock()) as clear:
        async with httpx.AsyncClient(transport=transport, base_url=f"http://{hostname}") as client:
            response = await client.delete("/api/cache", headers={
                "X-Forwarded-For": "127.0.0.1",
                "Forwarded": "for=198.51.100.42;host=public.example;proto=https",
            })
    assert response.status_code == 403
    clear.assert_not_awaited()


async def test_remote_cache_deletion_requires_correct_admin_token(monkeypatch):
    monkeypatch.setenv("CACHE_ADMIN_TOKEN", "admin-secret")
    transport = httpx.ASGITransport(app=api.app, client=("203.0.113.10", 1234))
    with patch.object(api._cache, "clear", AsyncMock()) as clear, patch.object(api._client, "clear_author_cache"):
        async with httpx.AsyncClient(transport=transport, base_url="https://public.test") as client:
            denied = await client.delete("/api/cache", headers={"Authorization": "Bearer wrong"})
            allowed = await client.delete("/api/cache", headers={"Authorization": "Bearer admin-secret"})
            cross_origin = await client.delete("/api/cache", headers={
                "Authorization": "Bearer admin-secret", "Origin": "https://attacker.test",
            })
    assert denied.status_code == cross_origin.status_code == 403
    assert allowed.json() == {"cleared": True}
    clear.assert_awaited_once()


@pytest.mark.parametrize("url", [
    "/api/path?from=A1,authorships.author.id:A2&to=A3",
    "/api/path?from=W1&to=A3",
    "/api/graph/expand?new_id=../../authors",
    "/api/graph/expand?new_id=A1&origin_ids=A2,broken",
    "/api/graph/expand?new_id=A1&path_ids=I1",
    "/api/graph/expand?new_id=A1&edges=none&edges=coauthor",
    "/api/graph/expand?new_id=A1&edges=unknown",
    "/api/graph/expand?new_id=A1&origin_ids=" + ",".join(f"A{index}" for index in range(26)),
    "/api/institution-suggestions?institution_id=A1&origin_ids=A2",
    "/api/authors/broken/works",
])
async def test_invalid_ids_and_edge_filters_fail_before_upstream_calls(url):
    with patch.object(api, "_client") as upstream:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://test") as client:
            response = await client.get(url)
    assert response.status_code == 422
    assert upstream.mock_calls == []


async def test_canonical_ids_and_explicit_empty_edge_sets_are_preserved():
    with patch.object(api, "_client") as upstream, patch.object(api, "_make_backend") as make_backend:
        upstream.get_author = AsyncMock(return_value={"display_name": "Alice"})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://test") as client:
            response = await client.get("/api/graph/expand", params={
                "new_id": "https://openalex.org/a1", "edges": "none", "work_edges": "none", "depth": 0,
            })
    assert response.status_code == 200
    assert "event: done" in response.text
    upstream.get_author.assert_awaited_once_with("A1")
    make_backend.assert_called_once_with(set(), set())


async def test_upstream_errors_do_not_expose_api_keys_in_json_or_sse():
    request = httpx.Request("GET", "https://api.openalex.org/authors/A1?api_key=private-secret")
    response = httpx.Response(401, request=request)
    with pytest.raises(httpx.HTTPStatusError) as caught:
        response.raise_for_status()
    with patch.object(api, "_client") as upstream:
        upstream.get_author = AsyncMock(side_effect=caught.value)
        upstream.get_author_works = AsyncMock(side_effect=caught.value)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://test") as client:
            responses = await asyncio.gather(
                client.get("/api/path?from=A1&to=A2"),
                client.get("/api/graph/expand?new_id=A1"),
                client.get("/api/authors/A1/works"),
            )
    for result in responses:
        assert "private-secret" not in result.text
        assert "api_key=" not in result.text
        assert "upstream_auth" in result.text


async def test_lifespan_closes_http_client_even_when_store_close_fails():
    with patch.object(api, "_store") as store, patch.object(api, "_client") as client:
        store.open = AsyncMock()
        store.close = AsyncMock(side_effect=RuntimeError("disk failure"))
        client.aclose = AsyncMock()
        with pytest.raises(RuntimeError, match="disk failure"):
            async with api.lifespan(api.app):
                pass
        client.aclose.assert_awaited_once()


async def test_graph_path_failure_emits_incomplete_pair_instead_of_silent_success():
    with patch.object(api, "_client") as upstream, patch.object(api, "_make_backend"), patch.object(
        api, "_collect_path", AsyncMock(side_effect=RuntimeError("private failure details")),
    ):
        upstream.get_author = AsyncMock(return_value={"display_name": "Alice"})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://test") as client:
            response = await client.get("/api/graph/expand?new_id=A1&origin_ids=A2&depth=0")
    frames = response.text.split("\n\n")
    result = next(json.loads(frame.split("data: ")[1]) for frame in frames if frame.startswith("event: path"))
    assert result["found"] is False
    assert result["search_complete"] is False
    assert result["reason"] == "error"
    assert result["error"]["code"] == "search_failed"
    assert "private failure details" not in response.text


async def test_graph_stream_close_cancels_and_waits_for_path_tasks():
    from starlette.requests import Request

    started = asyncio.Event()
    finished = asyncio.Event()

    async def pending_path(*args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            finished.set()

    async def expansion(*args, **kwargs):
        await started.wait()
        yield {"type": "expansion", "nodes": [], "edges": []}

    request = Request({
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": "/api/graph/expand",
        "raw_path": b"/api/graph/expand",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 12345),
        "server": ("test", 443),
    })

    with patch.object(api, "_client") as upstream, patch.object(api, "_make_backend"), patch.object(
        api, "_collect_path", pending_path,
    ), patch("backend.graph_expand.expand_graph", expansion):
        upstream.get_author = AsyncMock(return_value={"display_name": "Alice"})
        response = await api.graph_expand(
            request,
            new_id="A1", origin_ids="A2", path_ids="", edges=["coauthor"],
            work_edges=["authorship"], depth=1, top_k=1,
        )
        stream = response.body_iterator
        while "event: expansion" not in await asyncio.wait_for(anext(stream), timeout=1):
            pass
        await asyncio.wait_for(stream.aclose(), timeout=1)
    assert finished.is_set()
