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
AUTHOR_EDGE_TYPES = {"coauthor", "citation", "institution"}
WORK_EDGE_TYPES = {"authorship", "citation"}
Ring = dict[str, list[Connection]]


def merge_rings(existing: Ring | None, incoming: Ring) -> Ring:
    """Return a monotonic per-edge-type merge without mutating either input."""
    merged = dict(existing or {})
    merged.update(incoming)
    return merged


def normalize_ring(value: Ring | list[Connection], *, is_work: bool = False) -> Ring:
    return value if isinstance(value, dict) else decode_ring(value, is_work=is_work)


def decode_ring(raw: object, *, is_work: bool = False) -> Ring:
    """Decode current bucket entries and legacy flat-list entries."""
    if isinstance(raw, str):
        raw = json.loads(raw)
    if isinstance(raw, list):
        edge_types = WORK_EDGE_TYPES if is_work else AUTHOR_EDGE_TYPES
        ring: Ring = {edge_type: [] for edge_type in edge_types}
        for item in raw:
            conn = item if isinstance(item, Connection) else Connection(**item)
            ring.setdefault(conn.connection_type, []).append(conn)
        return ring
    if not isinstance(raw, dict):
        raise TypeError("neighbor ring must be a list or object")
    return {
        edge_type: [item if isinstance(item, Connection) else Connection(**item) for item in items]
        for edge_type, items in raw.items()
        if isinstance(items, list)
    }


def encode_ring(ring: Ring) -> dict[str, list[dict]]:
    return {
        edge_type: [conn.model_dump() for conn in connections]
        for edge_type, connections in ring.items()
    }


def select_ring(ring: Ring, required: set[str]) -> list[Connection] | None:
    if not required.issubset(ring):
        return None
    return [conn for edge_type in required for conn in ring[edge_type]]


class NeighborStore:
    """Durable persistence for the neighbor cache.

    `record` is called from the request path and MUST NOT block on I/O; actual
    persistence happens in `flush` (either inline or on a background task).
    """

    async def open(self) -> None:
        """Prepare the store (open connections, load files, start background tasks)."""

    async def fetch(self, ids: list[str]) -> dict[str, Ring]:
        """Return the durable entries for `ids` (absent ids simply omitted)."""
        return {}

    def record(self, entries: dict[str, Ring]) -> None:
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
        self._data: dict[str, Ring] = {}
        self._dirty = False
        self._flush_task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        # Serialises flush bodies so two writers can never race on the file.
        self._flush_lock = asyncio.Lock()

    async def open(self) -> None:
        if self._path.exists():
            try:
                raw = json.loads(self._path.read_text())
                loaded: dict[str, Ring] = {}
                for aid, value in raw.items():
                    try:
                        loaded[aid] = decode_ring(value, is_work=aid.startswith("W"))
                    except Exception as exc:
                        log.warning("Skipping corrupt neighbor-cache entry %s: %s", aid, exc)
                self._data = loaded
            except Exception as exc:
                log.warning("Could not load neighbor cache from disk: %s", exc)
        if self._flush_task is None:
            self._stop = asyncio.Event()
            self._flush_task = asyncio.create_task(self._flush_loop())

    async def fetch(self, ids: list[str]) -> dict[str, Ring]:
        return {i: self._data[i] for i in ids if i in self._data}

    def record(self, entries: dict[str, Ring]) -> None:
        for aid, value in entries.items():
            ring = normalize_ring(value, is_work=aid.startswith("W"))
            self._data[aid] = merge_rings(self._data.get(aid), ring)
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
                serialisable = {aid: encode_ring(ring) for aid, ring in snapshot.items()}
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
        DO UPDATE SET connections = CASE
            WHEN jsonb_typeof(neighbor_cache.connections) = 'object'
            THEN neighbor_cache.connections || EXCLUDED.connections
            ELSE EXCLUDED.connections
        END, updated_at = now()
    """

    def __init__(self, dsn: str):
        self._dsn = dsn
        self._pool = None
        self._pending: dict[str, Ring] = {}
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

    async def fetch(self, ids: list[str]) -> dict[str, Ring]:
        if self._pool is None or not ids:
            return {}
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT author_id, connections FROM neighbor_cache WHERE author_id = ANY($1::text[])",
                ids,
            )
        result: dict[str, Ring] = {}
        for row in rows:
            conns = row["connections"]
            if isinstance(conns, str):  # jsonb may come back as text
                conns = json.loads(conns)
            try:
                result[row["author_id"]] = decode_ring(
                    conns, is_work=row["author_id"].startswith("W")
                )
            except Exception as exc:
                log.warning("Skipping corrupt Supabase neighbor-cache entry %s: %s", row["author_id"], exc)
        return result

    def record(self, entries: dict[str, Ring]) -> None:
        for aid, value in entries.items():
            ring = normalize_ring(value, is_work=aid.startswith("W"))
            self._pending[aid] = merge_rings(self._pending.get(aid), ring)

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
            (aid, json.dumps(encode_ring(ring))) for aid, ring in batch.items()
        ]
        try:
            async with self._pool.acquire() as conn:
                await conn.executemany(self._UPSERT, records)
        except Exception:
            for aid, ring in batch.items():  # requeue for the next tick
                self._pending[aid] = merge_rings(ring, self._pending.get(aid, {}))
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
        self._mem: "OrderedDict[str, Ring]" = OrderedDict()
        self._inflight: dict[str, tuple[asyncio.Future, frozenset[str]]] = {}
        self._hits = 0
        self._misses = 0

    def get_memory(self, id_: str, required: set[str]) -> list[Connection] | None:
        """Return the cached entry for `id_` (LRU-touch), or None if not resident."""
        ring = self._mem.get(id_)
        if ring is not None:
            self._mem.move_to_end(id_)
            selected = select_ring(ring, required)
            if selected is not None:
                self._hits += 1
                return selected
        self._misses += 1
        return None

    def stats(self) -> tuple[int, int]:
        return self._hits, self._misses

    def _put_memory(self, id_: str, ring: Ring) -> None:
        self._mem[id_] = merge_rings(self._mem.get(id_), ring)
        self._mem.move_to_end(id_)
        if self._max is not None:
            while len(self._mem) > self._max:
                self._mem.popitem(last=False)

    async def fetch_from_store(
        self, ids: list[str], required_by_id: dict[str, set[str]]
    ) -> dict[str, list[Connection]]:
        """Consult the durable store for `ids`, populating memory with any hits."""
        if self._store is None or not ids:
            return {}
        found = await self._store.fetch(ids)
        result: dict[str, list[Connection]] = {}
        for i, value in found.items():
            ring = normalize_ring(value, is_work=i.startswith("W"))
            self._put_memory(i, ring)
            selected = select_ring(self._mem[i], required_by_id[i])
            if selected is not None:
                result[i] = selected
        return result

    def put(self, entries: dict[str, Ring]) -> None:
        """Write freshly-fetched entries through to memory + the durable store."""
        normalized: dict[str, Ring] = {}
        for i, value in entries.items():
            ring = normalize_ring(value, is_work=i.startswith("W"))
            self._put_memory(i, ring)
            normalized[i] = ring
        if self._store is not None:
            self._store.record(normalized)

    def claim(self, id_: str, required: set[str]) -> tuple[bool, asyncio.Future]:
        """Claim a remote fetch, or return a covering shared in-flight future."""
        current = self._inflight.get(id_)
        if current is not None and required.issubset(current[1]):
            return False, current[0]
        future = asyncio.get_running_loop().create_future()
        # Incomparable fetches may overlap; monotonic bucket merges make that safe.
        if current is None:
            self._inflight[id_] = (future, frozenset(required))
        return True, future

    def finish(self, id_: str, future: asyncio.Future, *, success: bool = True) -> None:
        current = self._inflight.get(id_)
        if current is not None and current[0] is future:
            self._inflight.pop(id_, None)
        if not future.done():
            future.set_result(success)

    async def clear(self) -> None:
        self._mem.clear()
        if self._store is not None:
            await self._store.clear()
