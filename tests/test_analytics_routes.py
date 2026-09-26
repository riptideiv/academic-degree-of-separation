"""Usage collection stays minimal; only administrators can read aggregates."""

from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI

from backend.analytics_routes import create_analytics_router
from backend.usage_analytics import UsageAnalytics


class Recorder:
    enabled = True

    def __init__(self):
        self.events = []
        self.summary = AsyncMock(return_value={"totals": {"unique_visitors": 2}})

    def record(self, visitor, event):
        self.events.append((visitor, event))
        return True


def client_for(recorder):
    app = FastAPI()
    app.include_router(create_analytics_router(recorder))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test")


def event_payload(event, visitor="a" * 32):
    return {"event": event, "visitor_id": visitor}


async def test_browser_id_is_reused_between_actions_without_cookies():
    recorder = Recorder()
    async with client_for(recorder) as client:
        visit = await client.post("/api/analytics/events", json=event_payload("page_view"))
        await client.post("/api/analytics/events", json=event_payload("author_search"))
        await client.post("/api/analytics/events", json=event_payload("graph_run"))
    async with client_for(recorder) as second:
        await second.post("/api/analytics/events", json=event_payload("page_view", "b" * 32))
    assert visit.status_code == 202
    assert visit.json() == {"accepted": True}
    assert "set-cookie" not in visit.headers
    assert visit.headers["cache-control"] == "no-store"
    ids = [visitor for visitor, _ in recorder.events]
    assert ids[0] == ids[1] == ids[2]
    assert ids[3] != ids[0]
    assert len(ids[0]) == 32


@pytest.mark.parametrize("payload", [
    {}, [], {"event": "page_view"},
    {"event": "unknown", "visitor_id": "a" * 32},
    {"event": ["page_view"], "visitor_id": "a" * 32},
    {"event": "author_search", "visitor_id": "a" * 32, "query": "private query"},
    {"event": "page_view", "visitor_id": "a" * 32, "api_key": "private-key"},
    {"event": "page_view", "visitor_id": "custom-id"},
    {"event": "page_view", "visitor_id": None},
])
async def test_only_allowlisted_event_and_random_browser_id_are_accepted(payload):
    recorder = Recorder()
    async with client_for(recorder) as client:
        response = await client.post("/api/analytics/events", json=payload)
    assert response.status_code == 422
    assert recorder.events == []
    assert "set-cookie" not in response.headers
    assert "private" not in response.text


@pytest.mark.parametrize("headers,status", [
    ({"Origin": "https://elsewhere.test"}, 403),
    ({"Origin": "null"}, 403),
    ({"Sec-Fetch-Site": "cross-site"}, 403),
    ({"Content-Type": "text/plain"}, 415),
])
async def test_collection_rejects_cross_origin_and_non_json_requests(headers, status):
    recorder = Recorder()
    async with client_for(recorder) as client:
        response = await client.post("/api/analytics/events", json=event_payload("page_view"), headers=headers)
    assert response.status_code == status
    assert recorder.events == []


async def test_collection_bounds_body_and_rejects_query_metadata():
    recorder = Recorder()
    async with client_for(recorder) as client:
        large = await client.post("/api/analytics/events", json={"event": "x" * 300})
        query = await client.post("/api/analytics/events?q=private", json=event_payload("page_view"))
        malformed = await client.post("/api/analytics/events", content="{", headers={"Content-Type": "application/json"})
    assert [result.status_code for result in (large, query, malformed)] == [413, 422, 422]
    assert recorder.events == []


@pytest.mark.parametrize("headers,enabled", [({"DNT": "1"}, True), ({"Sec-GPC": "1"}, True), ({}, False)])
async def test_optout_and_disabled_analytics_do_not_record_or_set_cookies(headers, enabled):
    recorder = Recorder()
    recorder.enabled = enabled
    async with client_for(recorder) as client:
        response = await client.post("/api/analytics/events", json=event_payload("page_view"), headers=headers)
    assert response.status_code == 204
    assert "set-cookie" not in response.headers
    assert recorder.events == []


async def test_collector_ignores_cookies_including_openalex_credentials():
    recorder = Recorder()
    async with client_for(recorder) as client:
        client.cookies.set("openalex_personal_key", "private-key", path="/api")
        response = await client.post("/api/analytics/events", json=event_payload("page_view"))
    assert response.status_code == 202
    assert "set-cookie" not in response.headers
    assert recorder.events == [("a" * 32, "page_view")]


async def test_reports_require_separate_admin_token_and_never_accept_it_in_url(monkeypatch):
    recorder = Recorder()
    monkeypatch.delenv("ANALYTICS_ADMIN_TOKEN", raising=False)
    monkeypatch.setenv("CACHE_ADMIN_TOKEN", "cache-secret")
    async with client_for(recorder) as client:
        unconfigured = await client.get("/api/analytics/summary")
        monkeypatch.setenv("ANALYTICS_ADMIN_TOKEN", "analytics-secret")
        for token in ("", "wrong", "cache-secret"):
            denied = await client.get("/api/analytics/summary", headers={"Authorization": f"Bearer {token}"})
            assert denied.status_code == 403
            assert denied.headers["cache-control"] == "no-store"
        query = await client.get("/api/analytics/summary?token=analytics-secret")
        cross_origin = await client.get("/api/analytics/summary", headers={
            "Authorization": "Bearer analytics-secret", "Origin": "https://elsewhere.test",
        })
        allowed = await client.get("/api/analytics/summary?days=7", headers={"Authorization": "Bearer analytics-secret"})
    assert unconfigured.status_code == cross_origin.status_code == 403
    assert query.status_code == 422
    assert "analytics-secret" not in query.text
    assert allowed.status_code == 200
    assert allowed.json() == {"totals": {"unique_visitors": 2}}
    assert allowed.headers["cache-control"] == "no-store"
    recorder.summary.assert_awaited_once_with(7)


async def test_report_errors_are_safe_and_invalid_ranges_do_not_read_store(monkeypatch):
    recorder = Recorder()
    recorder.summary.side_effect = RuntimeError("postgres://private-password@db")
    monkeypatch.setenv("ANALYTICS_ADMIN_TOKEN", "secret")
    async with client_for(recorder) as client:
        for days in (0, 366, "bad"):
            invalid = await client.get(f"/api/analytics/summary?days={days}", headers={"Authorization": "Bearer secret"})
            assert invalid.status_code == 422
        recorder.summary.assert_not_awaited()
        failed = await client.get("/api/analytics/summary", headers={"Authorization": "Bearer secret"})
    assert failed.status_code == 503
    assert "private-password" not in failed.text
    assert failed.headers["cache-control"] == "no-store"


async def test_browser_events_flow_into_persisted_summary(tmp_path, monkeypatch):
    analytics = UsageAnalytics(tmp_path / "usage.sqlite3")
    monkeypatch.setenv("ANALYTICS_ADMIN_TOKEN", "secret")
    await analytics.open()
    try:
        async with client_for(analytics) as first:
            for event in ("page_view", "author_search", "author_search", "graph_run"):
                await first.post("/api/analytics/events", json=event_payload(event))
        async with client_for(analytics) as second:
            await second.post("/api/analytics/events", json=event_payload("page_view", "b" * 32))
            result = await second.get("/api/analytics/summary?days=7", headers={"Authorization": "Bearer secret"})
        assert result.status_code == 200
        assert result.json()["totals"] == {
            "page_views": 2, "unique_visitors": 2, "searches": 2,
            "searching_visitors": 1, "graph_runs": 1, "explorer_runs": 0,
        }
        assert len(result.json()["daily"]) == 7
        assert "visitor_id" not in result.text
    finally:
        await analytics.close()
