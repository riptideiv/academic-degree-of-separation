import json
from unittest.mock import patch

import httpx
import pytest

from scripts import bench_ab, bench_search


def frame(event, data):
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@pytest.mark.parametrize("path,done,status", [
    ({"found": True, "hops": 2, "search_complete": False}, True, "found"),
    ({"found": False, "hops": None, "search_complete": True}, True, "no_path"),
    ({"found": False, "hops": None, "search_complete": False}, True, "incomplete"),
    ({"found": True, "hops": None}, True, "incomplete"),
    ({"found": True, "hops": 2}, False, "interrupted"),
    (None, True, "incomplete"),
    ({"found": False, "error": {"code": "rate_limited"}}, True, "error"),
])
async def test_benchmarks_separate_successful_paths_from_failed_searches(path, done, status):
    stream = frame("node", {"id": "A1"})
    if path is not None:
        stream += frame("path", path)
    if done:
        stream += frame("done", {})
    async with httpx.AsyncClient(
        base_url="http://localhost", transport=httpx.MockTransport(lambda request: httpx.Response(200, text=stream)),
    ) as client:
        result = await bench_search.consume_expand(client, {"new_id": "A1", "origin_ids": "A2"})
    assert result["status"] == status
    assert result["successful"] is (status == "found")


async def test_benchmarks_do_not_ignore_application_errors():
    stream = frame("app_error", {"message": "OpenAlex unavailable"}) + frame("done", {})
    async with httpx.AsyncClient(
        base_url="http://localhost", transport=httpx.MockTransport(lambda request: httpx.Response(200, text=stream)),
    ) as client:
        result = await bench_search.consume_expand(client, {"new_id": "A1"})
    assert result["status"] == "error"
    assert result["successful"] is False


async def test_benchmarks_require_a_confirmed_cache_reset():
    async with httpx.AsyncClient(
        base_url="http://localhost", transport=httpx.MockTransport(lambda request: httpx.Response(403)),
    ) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await bench_search.reset_cache(client)


async def test_benchmark_resolution_handles_empty_rate_limit_fallback():
    async with httpx.AsyncClient(
        base_url="http://localhost", transport=httpx.MockTransport(lambda request: httpx.Response(
            200, json={"results": [], "message": "OpenAlex rate limited"},
        )),
    ) as client:
        with pytest.raises(RuntimeError, match="Could not resolve Alice: OpenAlex rate limited"):
            await bench_search.resolve(client, "Alice")


def test_ab_servers_cannot_reload_shared_database_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPABASE_POOLER_CONNECTION_STRING", "secret-shared-dsn")
    monkeypatch.setenv("SUPABASE_DB_URL", "old-shared-dsn")
    with patch("scripts.bench_ab.subprocess.Popen") as start:
        bench_ab._start_server(tmp_path, 9000)
    env = start.call_args.kwargs["env"]
    assert env["SUPABASE_POOLER_CONNECTION_STRING"] == ""
    assert env["SUPABASE_DB_URL"] == ""
    assert env["CACHE_ADMIN_TOKEN"] == bench_ab.CACHE_ADMIN_TOKEN
