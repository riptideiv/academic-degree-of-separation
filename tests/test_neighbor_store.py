import asyncio
import json
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.models import Connection
from backend.neighbor_store import JsonNeighborStore, SupabaseNeighborStore


def conn(target="A2"):
    return Connection(
        target_author_id=target, target_name="Bob",
        connection_type="coauthor", label="Paper",
    )


async def test_record_does_not_write_synchronously(tmp_path):
    store = JsonNeighborStore(tmp_path / "cache.json")
    store.record({"A1": [conn()]})
    assert not (tmp_path / "cache.json").exists()


async def test_flush_persists_recorded_entries(tmp_path):
    path = tmp_path / "cache.json"
    store = JsonNeighborStore(path)
    store.record({"A1": [conn()]})
    await store.flush()
    raw = json.loads(path.read_text())
    assert raw["A1"][0]["target_author_id"] == "A2"


async def test_close_flushes_pending(tmp_path):
    path = tmp_path / "cache.json"
    store = JsonNeighborStore(path)
    await store.open()
    store.record({"A1": [conn()]})
    await store.close()
    assert json.loads(path.read_text())["A1"][0]["target_author_id"] == "A2"


async def test_fetch_reads_recorded_before_flush(tmp_path):
    store = JsonNeighborStore(tmp_path / "cache.json")
    store.record({"A1": [conn()]})
    found = await store.fetch(["A1"])
    assert found["A1"][0].target_author_id == "A2"


async def test_open_loads_previous_flush(tmp_path):
    path = tmp_path / "cache.json"
    first = JsonNeighborStore(path)
    first.record({"A1": [conn()]})
    await first.flush()

    second = JsonNeighborStore(path)
    await second.open()
    found = await second.fetch(["A1"])
    await second.close()
    assert found["A1"][0].target_author_id == "A2"


class FakePool:
    """A database write that tests can pause without timing-dependent sleeps."""

    def __init__(self):
        self.rows = {}
        self.write_started = asyncio.Event()
        self.release_write = asyncio.Event()
        self.operations = []
        self.error = None
        self.closed = False

    @asynccontextmanager
    async def acquire(self):
        assert not self.closed
        yield self

    async def execute(self, query):
        if query == "DELETE FROM neighbor_cache_v2":
            self.rows.clear()
            self.operations.append("delete")

    async def executemany(self, query, records):
        self.write_started.set()
        await self.release_write.wait()
        if self.error is not None:
            raise self.error
        self.rows.update({aid: json.loads(conns) for aid, conns in records})
        self.operations.append("write")

    async def close(self):
        self.closed = True


async def test_supabase_close_waits_for_inflight_write_and_flushes_new_entries(monkeypatch):
    pool = FakePool()
    monkeypatch.setitem(
        sys.modules, "asyncpg",
        SimpleNamespace(create_pool=AsyncMock(return_value=pool)),
    )
    monkeypatch.setattr("backend.neighbor_store.FLUSH_INTERVAL_S", 0.001)
    store = SupabaseNeighborStore("test-dsn")
    await store.open()
    store.record({"A1": [conn()]})
    await asyncio.wait_for(pool.write_started.wait(), timeout=1)

    # This entry arrives after the background writer has taken its batch.
    store.record({"A3": [conn("A4")]})
    closing = asyncio.create_task(store.close())
    try:
        await asyncio.wait_for(store._stop.wait(), timeout=1)
        assert not closing.done()
        assert not pool.closed
    finally:
        pool.release_write.set()
        await asyncio.wait_for(closing, timeout=1)

    assert pool.rows["A1"][0]["target_author_id"] == "A2"
    assert pool.rows["A3"][0]["target_author_id"] == "A4"
    assert pool.operations == ["write", "write"]
    assert not store._pending
    assert store._flush_task is None
    assert pool.closed


async def test_supabase_clear_waits_for_inflight_write():
    pool = FakePool()
    store = SupabaseNeighborStore("test-dsn")
    store._pool = pool
    store.record({"A1": [conn()]})
    writing = asyncio.create_task(store.flush())
    await asyncio.wait_for(pool.write_started.wait(), timeout=1)
    store.record({"A3": [conn("A4")]})

    clearing = asyncio.create_task(store.clear())
    try:
        await asyncio.sleep(0)  # let clear attempt to acquire the write lock
        assert not clearing.done()
        assert not pool.operations
    finally:
        pool.release_write.set()
        await asyncio.wait_for(asyncio.gather(writing, clearing), timeout=1)

    assert pool.operations == ["write", "delete"]
    assert not pool.rows
    assert not store._pending


@pytest.mark.parametrize("failure", ["error", "cancellation"])
async def test_supabase_failed_flush_requeues_batch_without_replacing_newer_entries(failure):
    pool = FakePool()
    store = SupabaseNeighborStore("test-dsn")
    store._pool = pool
    store.record({"A1": [conn("old")], "A2": [conn("retry")]})
    writing = asyncio.create_task(store.flush())
    await asyncio.wait_for(pool.write_started.wait(), timeout=1)
    store.record({"A1": [conn("new")], "A3": [conn("fresh")]})

    if failure == "cancellation":
        writing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await writing
    else:
        pool.error = RuntimeError("temporary database failure")
        pool.release_write.set()
        await asyncio.wait_for(writing, timeout=1)

    assert {aid: conns[0].target_author_id for aid, conns in store._pending.items()} == {
        "A1": "new", "A2": "retry", "A3": "fresh",
    }
    pool.error = None
    pool.release_write.set()
    await store.flush()
    assert {aid: conns[0]["target_author_id"] for aid, conns in pool.rows.items()} == {
        "A1": "new", "A2": "retry", "A3": "fresh",
    }
    assert not store._pending
