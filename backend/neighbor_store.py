"""Neighbor-cache persistence + a bounded in-memory front (`NeighborCache`).

Layering, fastest → slowest:

1. `NeighborCache` — a bounded LRU dict in process memory. Every read hits this
   first; it's the only layer on the hot path.
2. `NeighborStore` — durable persistence (`JsonNeighborStore` locally,
   `SupabaseNeighborStore` in prod). Consulted on an LRU miss via `fetch`, and
   written through on a fresh OpenAlex fetch via `record`.
3. OpenAlex — the source of truth, hit only when both layers miss (in the
   backend, not here).

Previously the whole table was resident in memory; the LRU bounds that so the
process stays flat under load, at the cost of a per-id `fetch` on a cold read.
The stores own their own full copy of the data (JSON file / Postgres table), so
they are NOT coupled to what the LRU currently holds — an entry evicted from the
LRU is still durable and re-fetchable.
"""

import asyncio
import json
import logging
import os
from collections import OrderedDict
from pathlib import Path

from backend.models import Connection

log = logging.getLogger(__name__)

# How often the background flush tasks (JSON / Supabase) persist pending entries.
FLUSH_INTERVAL_S = 5.0


class NeighborStore:
    """Durable persistence for the neighbor cache.

    `record` is called from the request path and MUST NOT block on I/O; actual
    persistence happens in `flush` (either inline or on a background task).
    """

    async def open(self) -> None:
        """Prepare the store (open connections, load files, start background tasks)."""

    async def fetch(self, ids: list[str]) -> dict[str, list[Connection]]:
        """Return the durable entries for `ids` (absent ids simply omitted)."""
        return {}

    def record(self, entries: dict[str, list[Connection]]) -> None:
        """Persist newly-fetched entries (non-blocking)."""

    async def flush(self) -> None:
        pass

    async def clear(self) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        pass


class JsonNeighborStore(NeighborStore):
    """Serialise the whole cache to a JSON file alongside the app.

    Holds its own full in-memory copy (`_data`) so it stays the source of truth
    independent of the bounded LRU. `record` only updates `_data` and marks it
    dirty (O(1), request path); a background task started in `open` flushes to
    disk every FLUSH_INTERVAL_S, and `close` stops it and does a final flush
    — the same pattern as SupabaseNeighborStore. The dump + write run in a
    worker thread so the event loop never blocks on file I/O.
    """

    def __init__(self, path: Path):
        self._path = path
        self._data: dict[str, list[Connection]] = {}
        self._dirty = False
        self._flush_task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        # Serialises flush bodies so two writers can never race on the file.
        self._flush_lock = asyncio.Lock()

    async def open(self) -> None:
        if self._path.exists():
            try:
                raw = json.loads(self._path.read_text())
                self._data = {aid: [Connection(**c) for c in conns] for aid, conns in raw.items()}
            except Exception as exc:
                log.warning("Could not load neighbor cache from disk: %s", exc)
        if self._flush_task is None:
            self._stop = asyncio.Event()
            self._flush_task = asyncio.create_task(self._flush_loop())

    async def fetch(self, ids: list[str]) -> dict[str, list[Connection]]:
        return {i: self._data[i] for i in ids if i in self._data}

    def record(self, entries: dict[str, list[Connection]]) -> None:
        self._data.update(entries)
        self._dirty = True

    async def _flush_loop(self) -> None:
        # Runs until close() sets _stop. The final iteration doubles as the
        # close-time flush, so close() never cancels a write mid-flight (a
        # cancelled `to_thread` would keep writing in its worker thread).
        while True:
            stopping = self._stop.is_set()
            if not stopping:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=FLUSH_INTERVAL_S)
                    stopping = True
                except asyncio.TimeoutError:
                    pass
            try:
                await self.flush()
            except Exception:  # keep the loop alive across transient I/O errors
                log.exception("Neighbor-cache flush loop error; continuing")
            if stopping:
                return

    async def flush(self) -> None:
        async with self._flush_lock:
            if not self._dirty:
                return
            self._dirty = False
            # Shallow snapshot on the loop thread: record() replaces whole entries
            # (never mutates lists in place), so the worker thread can serialise
            # the snapshot without racing concurrent record() calls.
            snapshot = dict(self._data)

            def _write() -> None:
                serialisable = {
                    aid: [c.model_dump() for c in conns] for aid, conns in snapshot.items()
                }
                self._path.write_text(json.dumps(serialisable))

            try:
                await asyncio.to_thread(_write)
            except Exception as exc:
                self._dirty = True  # retry on the next tick
                log.warning("Could not save neighbor cache to disk: %s", exc)

    async def clear(self) -> None:
        async with self._flush_lock:
            self._data.clear()
            self._dirty = False
            if self._path.exists():
                self._path.unlink()

    async def close(self) -> None:
        if self._flush_task is not None:
            self._stop.set()
            await self._flush_task  # waits out any in-flight write + final flush
            self._flush_task = None
        await self.flush()  # covers stores that were never open()ed


class SupabaseNeighborStore(NeighborStore):
    """Persist the cache to a Supabase/Postgres table via asyncpg.

    `record` only stashes entries into `_pending` (non-blocking); a background
    task started in `open` flushes them on the `FLUSH_INTERVAL_S` cadence,
    upserting only what changed. `_pending` holds the actual Connection values
    (not just ids), so flushing never depends on what the LRU currently holds.
    """

    _TABLE_DDL = """
        CREATE TABLE IF NOT EXISTS neighbor_cache (
            author_id  text PRIMARY KEY,
            connections jsonb NOT NULL,
            updated_at timestamptz DEFAULT now()
        )
    """
    _UPSERT = """
        INSERT INTO neighbor_cache (author_id, connections, updated_at)
        VALUES ($1, $2, now())
        ON CONFLICT (author_id)
        DO UPDATE SET connections = EXCLUDED.connections, updated_at = now()
    """

    def __init__(self, dsn: str):
        self._dsn = dsn
        self._pool = None
        self._pending: dict[str, list[Connection]] = {}
        self._flush_task: asyncio.Task | None = None

    async def open(self) -> None:
        import asyncpg

        # Keep the pool small: Supabase's Supavisor pooler has a limited client
        # budget (session mode especially). statement_cache_size=0 is required for
        # transaction-mode pooling (port 6543), where prepared statements can't be
        # reused across pooled transactions; it's harmless in session mode too.
        self._pool = await asyncpg.create_pool(
            self._dsn,
            min_size=1,
            max_size=int(os.environ.get("SUPABASE_POOL_MAX", "5")),
            statement_cache_size=0,
        )
        async with self._pool.acquire() as conn:
            await conn.execute(self._TABLE_DDL)
        self._flush_task = asyncio.create_task(self._flush_loop())
        log.info("Supabase neighbor store ready (lazy per-id fetch)")

    async def fetch(self, ids: list[str]) -> dict[str, list[Connection]]:
        if self._pool is None or not ids:
            return {}
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT author_id, connections FROM neighbor_cache WHERE author_id = ANY($1::text[])",
                ids,
            )
        result: dict[str, list[Connection]] = {}
        for row in rows:
            conns = row["connections"]
            if isinstance(conns, str):  # jsonb may come back as text
                conns = json.loads(conns)
            result[row["author_id"]] = [Connection(**c) for c in conns]
        return result

    def record(self, entries: dict[str, list[Connection]]) -> None:
        self._pending.update(entries)

    async def _flush_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(FLUSH_INTERVAL_S)
                await self.flush()
        except asyncio.CancelledError:
            raise
        except Exception:  # keep the loop alive across transient DB errors
            log.exception("Neighbor-cache flush loop error; continuing")

    async def flush(self) -> None:
        if not self._pending or self._pool is None:
            return
        batch = self._pending
        self._pending = {}
        records = [
            (aid, json.dumps([c.model_dump() for c in conns])) for aid, conns in batch.items()
        ]
        try:
            async with self._pool.acquire() as conn:
                await conn.executemany(self._UPSERT, records)
        except Exception:
            for aid, conns in batch.items():  # requeue for the next tick
                self._pending.setdefault(aid, conns)
            log.exception("Failed to flush %d neighbor-cache entries", len(records))

    async def clear(self) -> None:
        self._pending.clear()
        if self._pool is not None:
            async with self._pool.acquire() as conn:
                await conn.execute("DELETE FROM neighbor_cache")

    async def close(self) -> None:
        if self._flush_task is not None:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
        await self.flush()
        if self._pool is not None:
            await self._pool.close()


class NeighborCache:
    """Bounded LRU in front of a `NeighborStore`.

    `max_size=None` disables eviction (unbounded) — used when there's no store
    (tests/default), preserving the original always-resident behavior.
    """

    def __init__(self, store: NeighborStore | None = None, max_size: int | None = None):
        self._store = store
        self._max = max_size
        self._mem: "OrderedDict[str, list[Connection]]" = OrderedDict()

    def get_memory(self, id_: str) -> list[Connection] | None:
        """Return the cached entry for `id_` (LRU-touch), or None if not resident."""
        conns = self._mem.get(id_)
        if conns is not None:
            self._mem.move_to_end(id_)
        return conns

    def _put_memory(self, id_: str, conns: list[Connection]) -> None:
        self._mem[id_] = conns
        self._mem.move_to_end(id_)
        if self._max is not None:
            while len(self._mem) > self._max:
                self._mem.popitem(last=False)

    async def fetch_from_store(self, ids: list[str]) -> dict[str, list[Connection]]:
        """Consult the durable store for `ids`, populating memory with any hits."""
        if self._store is None or not ids:
            return {}
        found = await self._store.fetch(ids)
        for i, conns in found.items():
            self._put_memory(i, conns)
        return found

    def put(self, entries: dict[str, list[Connection]]) -> None:
        """Write freshly-fetched entries through to memory + the durable store."""
        for i, conns in entries.items():
            self._put_memory(i, conns)
        if self._store is not None:
            self._store.record(entries)

    async def clear(self) -> None:
        self._mem.clear()
        if self._store is not None:
            await self._store.clear()
