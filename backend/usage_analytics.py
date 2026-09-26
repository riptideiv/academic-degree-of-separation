"""Small, best-effort usage counters without search text or network identifiers."""

import asyncio
import hashlib
import logging
import sqlite3
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

EVENTS = (
    "page_view", "author_search", "work_search", "institution_search",
    "graph_run", "explorer_run",
)
SEARCH_EVENTS = ("author_search", "work_search", "institution_search")
RETENTION_DAYS = 365
MAX_PENDING_EVENTS = 5000
BATCH_SIZE = 250
IO_TIMEOUT = 2.0
FLUSH_INTERVAL = 1.0
RETRY_INTERVAL = 5.0

_TABLE = "usage_analytics_events"
_SCHEMA = f"""CREATE TABLE IF NOT EXISTS {_TABLE} (
    event_id TEXT PRIMARY KEY,
    visitor_hash TEXT NOT NULL,
    event_date TEXT NOT NULL,
    event_type TEXT NOT NULL
)"""
_INDEX = f"CREATE INDEX IF NOT EXISTS usage_analytics_date_idx ON {_TABLE} (event_date)"
_SEARCH_SQL = ", ".join(f"'{event}'" for event in SEARCH_EVENTS)
_METRICS = {
    "page_views": "COUNT(CASE WHEN event_type = 'page_view' THEN 1 END)",
    "unique_visitors": "COUNT(DISTINCT visitor_hash)",
    "searches": f"COUNT(CASE WHEN event_type IN ({_SEARCH_SQL}) THEN 1 END)",
    "searching_visitors": f"COUNT(DISTINCT CASE WHEN event_type IN ({_SEARCH_SQL}) THEN visitor_hash END)",
    "graph_runs": "COUNT(CASE WHEN event_type = 'graph_run' THEN 1 END)",
    "explorer_runs": "COUNT(CASE WHEN event_type = 'explorer_run' THEN 1 END)",
}
_COLUMNS = ", ".join(
    [f"{expression} AS {name}" for name, expression in _METRICS.items()]
    + [f"COUNT(CASE WHEN event_type = '{event}' THEN 1 END) AS event_{event}" for event in EVENTS]
)


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


def _cutoff() -> str:
    return (_utc_today() - timedelta(days=RETENTION_DAYS - 1)).isoformat()


@dataclass(frozen=True)
class _Event:
    event_id: str
    visitor_hash: str
    event_date: str
    event_type: str

    def values(self) -> tuple[str, str, str, str]:
        return self.event_id, self.visitor_hash, self.event_date, self.event_type


class UsageAnalytics:
    """Bounded, nonblocking recording backed by SQLite or an independent PG pool.

    IDs identify browsers, not people. Only a SHA-256 digest of the supplied
    random browser ID is stored. Failed batches retain their original event IDs
    so retrying a write with an uncertain outcome cannot double-count it.
    """

    def __init__(self, path: Path, dsn: str | None = None, enabled: bool = True):
        self.enabled = enabled
        self._path = Path(path)
        self._dsn = dsn
        self._pool = None
        self._ready = False
        self._available = False
        self._closed = False
        self._failure_logged = False
        self._pending: deque[_Event] = deque()
        self._dropped = 0
        self._lock = asyncio.Lock()
        self._worker: asyncio.Task | None = None

    async def open(self) -> None:
        if not self.enabled or self._closed or self._worker is not None:
            return
        async with self._lock:
            await self._ensure_storage()
        self._worker = asyncio.create_task(self._run(), name="usage-analytics")

    def record(self, visitor_id: str, event: str) -> bool:
        """Queue an event without disk/network I/O; False means it was not queued."""
        if not self.enabled or self._closed:
            return False
        if event not in EVENTS or not isinstance(visitor_id, str) or not 1 <= len(visitor_id) <= 128:
            return False
        if len(self._pending) >= MAX_PENDING_EVENTS:
            self._dropped += 1
            return False
        self._pending.append(_Event(
            uuid.uuid4().hex,
            hashlib.sha256(visitor_id.encode("utf-8")).hexdigest(),
            _utc_today().isoformat(),
            event,
        ))
        return True

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(FLUSH_INTERVAL)
            if not await self._flush_pending(BATCH_SIZE):
                await asyncio.sleep(RETRY_INTERVAL)

    def _failed(self) -> None:
        self._available = False
        if not self._failure_logged:
            # Exceptions can contain a DSN, host name, or filesystem path.
            logger.warning("Usage analytics storage is unavailable; queued events will be retried.")
            self._failure_logged = True

    def _succeeded(self) -> None:
        self._available = True
        self._failure_logged = False

    async def _ensure_storage(self) -> bool:
        if self._ready:
            return True
        try:
            await asyncio.wait_for(self._initialize(), timeout=IO_TIMEOUT)
        except Exception:
            self._failed()
            return False
        self._ready = True
        self._succeeded()
        return True

    async def _initialize(self) -> None:
        if not self._dsn:
            await asyncio.to_thread(self._sqlite_initialize)
            return
        import asyncpg

        pool = await asyncpg.create_pool(
            dsn=self._dsn, min_size=1, max_size=2,
            statement_cache_size=0, command_timeout=IO_TIMEOUT, timeout=IO_TIMEOUT,
        )
        try:
            async with pool.acquire() as connection:
                async with connection.transaction():
                    await connection.execute(_SCHEMA)
                    # Supabase may expose public-schema tables through its API.
                    # No public policies: only the owning app role can access data.
                    await connection.execute(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY")
                    await connection.execute(_INDEX)
                    await connection.execute(f"DELETE FROM {_TABLE} WHERE event_date < $1", _cutoff())
        except BaseException:
            pool.terminate()
            raise
        self._pool = pool

    def _sqlite_connect(self) -> sqlite3.Connection:
        # Each thread owns its connection; SQLite's short busy timeout also bounds
        # work that continues after cancellation of an asyncio.to_thread call.
        connection = sqlite3.connect(self._path, timeout=0.25)
        connection.row_factory = sqlite3.Row
        return connection

    def _sqlite_initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        connection = self._sqlite_connect()
        try:
            with connection:
                connection.execute(_SCHEMA)
                connection.execute(_INDEX)
                connection.execute(f"DELETE FROM {_TABLE} WHERE event_date < ?", (_cutoff(),))
        finally:
            connection.close()

    async def _flush_pending(self, limit: int | None = None) -> bool:
        async with self._lock:
            if not await self._ensure_storage():
                return False
            # Leave this prefix in the queue until the database confirms success.
            batch = list(self._pending)[:limit]
            if not batch:
                return True
            try:
                await asyncio.wait_for(self._write(batch), timeout=IO_TIMEOUT)
            except Exception:
                self._failed()
                return False
            for _ in batch:
                self._pending.popleft()
            self._succeeded()
            return True

    async def _write(self, batch: list[_Event]) -> None:
        if not self._dsn:
            await asyncio.to_thread(self._sqlite_write, batch)
            return
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                await connection.executemany(
                    f"INSERT INTO {_TABLE} (event_id, visitor_hash, event_date, event_type) "
                    "VALUES ($1, $2, $3, $4) ON CONFLICT (event_id) DO NOTHING",
                    [event.values() for event in batch],
                )
                await connection.execute(f"DELETE FROM {_TABLE} WHERE event_date < $1", _cutoff())

    def _sqlite_write(self, batch: list[_Event]) -> None:
        connection = self._sqlite_connect()
        try:
            with connection:
                connection.executemany(
                    f"INSERT OR IGNORE INTO {_TABLE} (event_id, visitor_hash, event_date, event_type) "
                    "VALUES (?, ?, ?, ?)", [event.values() for event in batch],
                )
                connection.execute(f"DELETE FROM {_TABLE} WHERE event_date < ?", (_cutoff(),))
        finally:
            connection.close()

    async def summary(self, days: int) -> dict:
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= RETENTION_DAYS:
            raise ValueError(f"days must be between 1 and {RETENTION_DAYS}")
        end = _utc_today()
        start = end - timedelta(days=days - 1)
        totals = {name: 0 for name in _METRICS}
        event_totals = {event: 0 for event in EVENTS}
        daily = {}
        try:
            if self.enabled:
                # Flush the bounded queue before reading. A failed write may still
                # leave older data readable; status exposes pending/dropped events.
                await asyncio.wait_for(self._flush_pending(), timeout=IO_TIMEOUT * 2)
            async with self._lock:
                if not await self._ensure_storage():
                    raise RuntimeError("storage unavailable")
                row, rows = await asyncio.wait_for(
                    self._read(start.isoformat(), end.isoformat()), timeout=IO_TIMEOUT,
                )
            totals = {name: int(row[name]) for name in _METRICS}
            event_totals = {event: int(row[f"event_{event}"]) for event in EVENTS}
            daily = {row["date"]: {name: int(row[name]) for name in _METRICS} for row in rows}
            self._succeeded()
        except Exception:
            self._failed()
            raise RuntimeError("Usage analytics are temporarily unavailable.") from None
        return {
            "period": {"days": days, "start_date": start.isoformat(), "end_date": end.isoformat(), "timezone": "UTC"},
            "totals": totals,
            "events": event_totals,
            "daily": [
                {"date": (day := (start + timedelta(days=offset)).isoformat()),
                 **daily.get(day, {name: 0 for name in _METRICS})}
                for offset in range(days)
            ],
            "status": {
                "enabled": self.enabled,
                "storage": "postgresql" if self._dsn else "sqlite",
                "available": self._available,
                "pending_events": len(self._pending),
                "dropped_events": self._dropped,
                "retention_days": RETENTION_DAYS,
            },
        }

    async def _read(self, start: str, end: str):
        if not self._dsn:
            return await asyncio.to_thread(self._sqlite_read, start, end)
        async with self._pool.acquire() as connection:
            async with connection.transaction(isolation="repeatable_read"):
                await connection.execute(f"DELETE FROM {_TABLE} WHERE event_date < $1", _cutoff())
                row = await connection.fetchrow(
                    f"SELECT {_COLUMNS} FROM {_TABLE} WHERE event_date BETWEEN $1 AND $2", start, end,
                )
                rows = await connection.fetch(
                    f"SELECT event_date AS date, {_COLUMNS} FROM {_TABLE} "
                    "WHERE event_date BETWEEN $1 AND $2 GROUP BY event_date ORDER BY event_date", start, end,
                )
                return row, rows

    def _sqlite_read(self, start: str, end: str):
        connection = self._sqlite_connect()
        try:
            with connection:
                connection.execute(f"DELETE FROM {_TABLE} WHERE event_date < ?", (_cutoff(),))
                row = connection.execute(
                    f"SELECT {_COLUMNS} FROM {_TABLE} WHERE event_date BETWEEN ? AND ?", (start, end),
                ).fetchone()
                rows = connection.execute(
                    f"SELECT event_date AS date, {_COLUMNS} FROM {_TABLE} "
                    "WHERE event_date BETWEEN ? AND ? GROUP BY event_date ORDER BY event_date", (start, end),
                ).fetchall()
                return row, rows
        finally:
            connection.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._worker is not None:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
            self._worker = None
        if self.enabled and self._pending:
            try:
                await asyncio.wait_for(self._flush_pending(), timeout=IO_TIMEOUT * 2)
            except Exception:
                self._failed()
        if self._pending:
            self._dropped += len(self._pending)
            self._pending.clear()
        if self._pool is not None:
            try:
                await asyncio.wait_for(self._pool.close(), timeout=IO_TIMEOUT)
            except Exception:
                self._pool.terminate()
            self._pool = None
