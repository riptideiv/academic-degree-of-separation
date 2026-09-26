import asyncio
import hashlib
import sqlite3
import sys
from contextlib import asynccontextmanager
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend import usage_analytics as analytics
from backend.usage_analytics import EVENTS, SEARCH_EVENTS, UsageAnalytics


@pytest.fixture
def today(monkeypatch):
    value = date(2026, 9, 26)
    monkeypatch.setattr(analytics, "_utc_today", lambda: value)
    return value


@pytest.fixture
async def store(tmp_path, today):
    instance = UsageAnalytics(tmp_path / "analytics.sqlite3")
    await instance.open()
    yield instance
    await instance.close()


async def test_counts_persist_and_store_only_hashed_identifiers(tmp_path, today):
    path = tmp_path / "nested" / "analytics.sqlite3"
    first = UsageAnalytics(path)
    await first.open()
    browser_id = "37c079e0b3e24251b2a23da8c99c0c62"
    assert first.record(browser_id, "page_view")
    assert first.record(browser_id, "author_search")
    await first.close()

    with sqlite3.connect(path) as connection:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(usage_analytics_events)")]
        rows = connection.execute("SELECT * FROM usage_analytics_events").fetchall()
    assert columns == ["event_id", "visitor_hash", "event_date", "event_type"]
    assert len(rows) == 2
    assert len({row[0] for row in rows}) == 2
    assert {row[1] for row in rows} == {hashlib.sha256(browser_id.encode()).hexdigest()}
    assert {row[2] for row in rows} == {"2026-09-26"}
    assert browser_id.encode() not in path.read_bytes()

    reopened = UsageAnalytics(path)
    await reopened.open()
    result = await reopened.summary(1)
    await reopened.close()
    assert result["totals"] == {
        "page_views": 1, "unique_visitors": 1, "searches": 1,
        "searching_visitors": 1, "graph_runs": 0, "explorer_runs": 0,
    }
    assert result["status"]["pending_events"] == 0
    assert result["status"]["available"] is True


async def test_date_windows_deduplicate_visitors_and_fill_missing_days(store, today, monkeypatch):
    monkeypatch.setattr(analytics, "_utc_today", lambda: today - timedelta(days=2))
    store.record("same-browser", "page_view")
    store.record("same-browser", "author_search")
    store.record("old-browser", "work_search")
    monkeypatch.setattr(analytics, "_utc_today", lambda: today)
    store.record("same-browser", "page_view")
    store.record("same-browser", "institution_search")
    store.record("new-browser", "graph_run")
    store.record("new-browser", "explorer_run")

    result = await store.summary(3)
    assert result["period"] == {
        "days": 3, "start_date": "2026-09-24", "end_date": "2026-09-26", "timezone": "UTC",
    }
    assert result["totals"] == {
        "page_views": 2, "unique_visitors": 3, "searches": 3,
        "searching_visitors": 2, "graph_runs": 1, "explorer_runs": 1,
    }
    assert result["events"] == {event: (2 if event == "page_view" else 1) for event in EVENTS}
    assert [row["date"] for row in result["daily"]] == ["2026-09-24", "2026-09-25", "2026-09-26"]
    assert result["daily"][1] == {
        "date": "2026-09-25", "page_views": 0, "unique_visitors": 0, "searches": 0,
        "searching_visitors": 0, "graph_runs": 0, "explorer_runs": 0,
    }
    assert result["daily"][0]["unique_visitors"] == result["daily"][2]["unique_visitors"] == 2
    recent = await store.summary(1)
    assert recent["totals"]["unique_visitors"] == 2
    assert recent["totals"]["searching_visitors"] == 1
    assert recent["totals"]["searches"] == 1
    assert SEARCH_EVENTS == ("author_search", "work_search", "institution_search")


async def test_retention_includes_today_and_previous_364_days(store, today, monkeypatch):
    monkeypatch.setattr(analytics, "_utc_today", lambda: today - timedelta(days=365))
    store.record("expired", "page_view")
    await store._flush_pending()
    monkeypatch.setattr(analytics, "_utc_today", lambda: today - timedelta(days=364))
    store.record("boundary", "page_view")
    monkeypatch.setattr(analytics, "_utc_today", lambda: today)
    result = await store.summary(365)
    assert result["totals"]["page_views"] == 1
    assert result["daily"][0]["page_views"] == 1
    with sqlite3.connect(store._path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM usage_analytics_events").fetchone()[0] == 1


async def test_retry_after_committed_write_is_idempotent(store, monkeypatch):
    store.record("browser", "author_search")
    actual_write = store._write

    async def committed_but_timed_out(batch):
        await actual_write(batch)
        raise TimeoutError("uncertain commit")

    monkeypatch.setattr(store, "_write", committed_but_timed_out)
    assert await store._flush_pending() is False
    assert len(store._pending) == 1
    monkeypatch.setattr(store, "_write", actual_write)
    result = await store.summary(1)
    assert result["totals"]["searches"] == 1
    assert result["status"]["pending_events"] == 0


async def test_bounded_queue_recovers_from_write_failure(store, monkeypatch, caplog):
    monkeypatch.setattr(analytics, "MAX_PENDING_EVENTS", 2)
    actual_write = store._write
    monkeypatch.setattr(store, "_write", AsyncMock(side_effect=RuntimeError("password=secret")))
    assert store.record("one", "author_search") is True
    assert store.record("two", "work_search") is True
    assert store.record("three", "page_view") is False
    assert await store._flush_pending() is False
    assert len(store._pending) == 2
    assert "secret" not in caplog.text
    monkeypatch.setattr(store, "_write", actual_write)
    result = await store.summary(1)
    assert result["totals"]["searches"] == 2
    assert result["status"]["dropped_events"] == 1
    assert result["status"]["pending_events"] == 0


async def test_open_failure_is_nonfatal_and_retries(tmp_path, today, monkeypatch):
    store = UsageAnalytics(tmp_path / "analytics.sqlite3")
    actual_initialize = store._initialize
    monkeypatch.setattr(store, "_initialize", AsyncMock(side_effect=OSError("unavailable")))
    await store.open()
    try:
        assert store.record("browser", "page_view") is True
        with pytest.raises(RuntimeError, match="temporarily unavailable"):
            await store.summary(1)
        assert len(store._pending) == 1
        monkeypatch.setattr(store, "_initialize", actual_initialize)
        result = await store.summary(1)
        assert result["totals"]["page_views"] == 1
        assert result["status"]["available"] is True
    finally:
        await store.close()


async def test_background_flush_runs_without_dashboard_reads(tmp_path, today, monkeypatch):
    monkeypatch.setattr(analytics, "FLUSH_INTERVAL", 0.005)
    store = UsageAnalytics(tmp_path / "analytics.sqlite3")
    await store.open()
    try:
        store.record("browser", "page_view")
        async with asyncio.timeout(1):
            while store._pending:
                await asyncio.sleep(0.005)
        with sqlite3.connect(store._path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM usage_analytics_events").fetchone()[0] == 1
    finally:
        await store.close()


async def test_disabled_store_preserves_historical_reporting_without_collection(tmp_path, today):
    path = tmp_path / "disabled.sqlite3"
    previous = UsageAnalytics(path)
    await previous.open()
    previous.record("previous-browser", "page_view")
    previous.record("previous-browser", "author_search")
    await previous.close()

    store = UsageAnalytics(path, enabled=False)
    await store.open()
    assert store._worker is None
    assert store._ready is False
    assert store.record("browser", "page_view") is False
    result = await store.summary(2)
    await store.close()
    assert result["totals"]["unique_visitors"] == 1
    assert result["totals"]["page_views"] == 1
    assert result["totals"]["searches"] == 1
    assert len(result["daily"]) == 2
    assert result["status"]["enabled"] is False
    assert result["status"]["available"] is True
    assert store._worker is None
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM usage_analytics_events").fetchone()[0] == 2


@pytest.mark.parametrize("days", [0, -1, 366, True, 1.5, "7"])
async def test_invalid_summary_ranges_are_rejected(store, days):
    with pytest.raises(ValueError):
        await store.summary(days)


async def test_invalid_events_and_identifiers_are_not_recorded(store):
    for visitor, event in [("browser", "search text"), ("", "page_view"), (None, "page_view"), ("x" * 129, "page_view")]:
        assert store.record(visitor, event) is False
    assert (await store.summary(1))["totals"]["unique_visitors"] == 0


async def test_io_timeout_and_shutdown_are_bounded(store, monkeypatch):
    monkeypatch.setattr(analytics, "IO_TIMEOUT", 0.01)

    async def stalled_write(batch):
        await asyncio.sleep(10)

    monkeypatch.setattr(store, "_write", stalled_write)
    store.record("browser", "page_view")
    async with asyncio.timeout(0.5):
        assert await store._flush_pending() is False
        await store.close()
    assert store._worker is None
    assert not store._pending
    assert store._dropped == 1
    assert store.record("browser", "page_view") is False


async def test_postgres_uses_independent_bounded_pool_and_idempotent_parameterized_queries(tmp_path, today, monkeypatch):
    queries = []

    class Connection:
        @asynccontextmanager
        async def transaction(self, **kwargs):
            yield

        async def execute(self, query, *args):
            queries.append((query, args))

        async def executemany(self, query, values):
            queries.append((query, values))

        async def fetchrow(self, query, *args):
            queries.append((query, args))
            return {**{name: 0 for name in analytics._METRICS}, **{f"event_{event}": 0 for event in EVENTS}}

        async def fetch(self, query, *args):
            queries.append((query, args))
            return []

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            yield Connection()

        close = AsyncMock()
        terminate = lambda self: None

    pool = Pool()
    create_pool = AsyncMock(return_value=pool)
    monkeypatch.setitem(sys.modules, "asyncpg", SimpleNamespace(create_pool=create_pool))
    store = UsageAnalytics(tmp_path / "unused.sqlite3", dsn="postgresql://private-dsn")
    await store.open()
    try:
        store.record("browser", "author_search")
        result = await store.summary(7)
    finally:
        await store.close()
    assert create_pool.await_args.kwargs["min_size"] == 1
    assert create_pool.await_args.kwargs["max_size"] == 2
    assert create_pool.await_args.kwargs["statement_cache_size"] == 0
    assert queries[0][0].startswith("CREATE TABLE")
    assert queries[1][0] == "ALTER TABLE usage_analytics_events ENABLE ROW LEVEL SECURITY"
    insert_query, batch = next(item for item in queries if item[0].startswith("INSERT"))
    assert "ON CONFLICT (event_id) DO NOTHING" in insert_query
    assert "VALUES ($1, $2, $3, $4)" in insert_query
    assert batch[0][1] == hashlib.sha256(b"browser").hexdigest()
    assert batch[0][2:] == ("2026-09-26", "author_search")
    assert all("private-dsn" not in query for query, _ in queries)
    assert any(args == ("2026-09-20", "2026-09-26") for _, args in queries)
    assert result["status"]["storage"] == "postgresql"
    assert not store._path.exists()
    pool.close.assert_awaited_once()
