# Perf + Initial Layout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make searches substantially faster (exactness-preserving pipeline fixes) and make the default map settle into a clean, untangled arrangement.

**Architecture:** Backend: raise OpenAlex concurrency + HTTP/2, make the local JSON cache store flush in the background instead of blocking the event loop, replace the batch lock convoy with per-id in-flight futures, and make the post-expansion stitch pass cache-only. Frontend: run the existing full-quality fCoSE layout once at stream end, and seed streamed nodes next to a node they actually connect to.

**Tech Stack:** FastAPI + httpx (backend), pytest + AsyncMock (tests), vanilla JS + Cytoscape/fCoSE (frontend).

**Spec:** `docs/superpowers/specs/2026-07-05-perf-and-layout-design.md`

## Global Constraints

- Preserve exactness w.r.t. the current neighbor-ring definition; the only accepted data change is the stitch pass losing leaf-to-leaf edges whose rings were never cached (spec 1d).
- Default in-memory `NeighborCache()` behavior (no store, unbounded) must stay unchanged — the whole existing suite in `tests/test_graph_backend.py` must keep passing untouched.
- Do NOT put Claude as a co-contributor in any commit. Plain commit messages, no Co-Authored-By.
- Don't touch unrelated code (CLAUDE.md rule 3).
- Work on branch `perf-and-layout` (already created).
- Run tests with `.venv/bin/pytest` from the repo root (pytest-asyncio is in auto mode; async tests need no decorator).
- Frontend has no JS test harness — frontend tasks are verified by driving the app (Task 8).

---

### Task 1: OpenAlex client — concurrency 5→10 + HTTP/2

**Files:**
- Modify: `backend/openalex_client.py:38` (semaphore), `backend/openalex_client.py:47-52` (client)
- Modify: `requirements.txt:3`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: no signature changes; behavior-only (more parallel requests, multiplexed connections).

- [ ] **Step 1: Install the h2 extra**

```bash
.venv/bin/pip install 'httpx[http2]>=0.27.0'
.venv/bin/python -c "import h2; print('h2 ok')"
```

Expected: `h2 ok`

- [ ] **Step 2: Update requirements.txt**

Change line 3 from `httpx>=0.27.0` to:

```
httpx[http2]>=0.27.0
```

- [ ] **Step 3: Raise the semaphore**

In `backend/openalex_client.py`, replace:

```python
        self._semaphore = asyncio.Semaphore(5)
```

with:

```python
        # 10 concurrent requests: the OpenAlex polite pool (mailto/api-key
        # identified, which we send) allows ~10 req/s.
        self._semaphore = asyncio.Semaphore(10)
```

- [ ] **Step 4: Enable HTTP/2 on the shared client**

In `_http_client`, replace:

```python
            self._http = httpx.AsyncClient(
                timeout=30.0, headers={"User-Agent": self._user_agent()}
            )
```

with:

```python
            self._http = httpx.AsyncClient(
                timeout=30.0, http2=True, headers={"User-Agent": self._user_agent()}
            )
```

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/pytest`
Expected: all tests PASS (no test touches the semaphore value or http2 flag).

- [ ] **Step 6: Commit**

```bash
git add backend/openalex_client.py requirements.txt
git commit -m "perf: raise OpenAlex concurrency to 10 and enable HTTP/2"
```

---

### Task 2: JsonNeighborStore — debounced background flush

**Files:**
- Modify: `backend/neighbor_store.py:62-100` (`JsonNeighborStore`)
- Create: `tests/test_neighbor_store.py`

**Interfaces:**
- Consumes: existing `NeighborStore` base API (`open/fetch/record/flush/clear/close`) — unchanged.
- Produces: same API; `record()` becomes O(1) (no file I/O), `flush()` does the write via `asyncio.to_thread`, `open()` starts a background flush loop, `close()` cancels it and does a final flush. `backend/app.py` already calls `open()`/`close()` in the lifespan — no app changes needed.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_neighbor_store.py`:

```python
import json

from backend.models import Connection
from backend.neighbor_store import JsonNeighborStore


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
```

- [ ] **Step 2: Run to verify the right ones fail**

Run: `.venv/bin/pytest tests/test_neighbor_store.py -v`
Expected: `test_record_does_not_write_synchronously` FAILS (current `record` writes the file inline). `test_close_flushes_pending` may pass by accident (record already wrote). Others PASS. The key red test is the first one.

- [ ] **Step 3: Implement the background-flush store**

Replace the whole `JsonNeighborStore` class in `backend/neighbor_store.py` with:

```python
class JsonNeighborStore(NeighborStore):
    """Serialise the whole cache to a JSON file alongside the app.

    Holds its own full in-memory copy (`_data`) so it stays the source of truth
    independent of the bounded LRU. `record` only updates `_data` and marks it
    dirty (O(1), request path); a background task started in `open` flushes to
    disk every FLUSH_INTERVAL_S, and `close` cancels it and does a final flush
    — the same pattern as SupabaseNeighborStore. The dump + write run in a
    worker thread so the event loop never blocks on file I/O.
    """

    def __init__(self, path: Path):
        self._path = path
        self._data: dict[str, list[Connection]] = {}
        self._dirty = False
        self._flush_task: asyncio.Task | None = None

    async def open(self) -> None:
        if self._path.exists():
            try:
                raw = json.loads(self._path.read_text())
                self._data = {aid: [Connection(**c) for c in conns] for aid, conns in raw.items()}
            except Exception as exc:
                log.warning("Could not load neighbor cache from disk: %s", exc)
        self._flush_task = asyncio.create_task(self._flush_loop())

    async def fetch(self, ids: list[str]) -> dict[str, list[Connection]]:
        return {i: self._data[i] for i in ids if i in self._data}

    def record(self, entries: dict[str, list[Connection]]) -> None:
        self._data.update(entries)
        self._dirty = True

    async def _flush_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(FLUSH_INTERVAL_S)
                await self.flush()
        except asyncio.CancelledError:
            raise
        except Exception:  # keep the loop alive across transient I/O errors
            log.exception("Neighbor-cache flush loop error; continuing")

    async def flush(self) -> None:
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
        self._data.clear()
        self._dirty = False
        if self._path.exists():
            self._path.unlink()

    async def close(self) -> None:
        if self._flush_task is not None:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
            self._flush_task = None
        await self.flush()
```

(`asyncio`, `json`, `log`, `FLUSH_INTERVAL_S`, `Path`, `Connection` are all already imported/defined in this module.)

- [ ] **Step 4: Run the new tests**

Run: `.venv/bin/pytest tests/test_neighbor_store.py -v`
Expected: all 5 PASS.

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/pytest`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/neighbor_store.py tests/test_neighbor_store.py
git commit -m "perf: flush JSON neighbor store in background instead of on the request path"
```

---

### Task 3: Batch reads — per-id in-flight futures instead of lock convoy

**Files:**
- Modify: `backend/graph_backend.py:55` (`_cache_locks` → `_inflight`), `backend/graph_backend.py:164-217` (`get_neighbors_batch`)
- Test: `tests/test_graph_backend.py` (add 2 tests; `test_concurrent_overlapping_batches_share_cache_miss` at line 195 must keep passing unmodified)

**Interfaces:**
- Consumes: `NeighborCache.get_memory / fetch_from_store / put` (existing).
- Produces: `OpenAlexBackend.get_neighbors_batch(ids: list[str]) -> dict[str, list[Connection]]` — same signature and semantics; only the concurrency behavior changes. Task 4 modifies this same method again (adds `cached_only`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_graph_backend.py`, and add `from backend.neighbor_store import NeighborCache, NeighborStore` below the existing `from backend.graph_backend import OpenAlexBackend` import.

```python
async def test_overlapping_batch_fetches_disjoint_ids_immediately():
    """A batch must not queue behind another batch's fetch for ids it doesn't share.

    Old behavior: batch 2 needing {A1, A2} blocked on A1's lock (held by batch 1)
    before fetching A2. New behavior: it fetches A2 right away and only awaits A1.
    """
    mock_client = AsyncMock()
    a1_started = asyncio.Event()
    release_a1 = asyncio.Event()
    a2_started = asyncio.Event()

    async def works(author_ids):
        if "A1" in author_ids:
            a1_started.set()
            await release_a1.wait()
        if "A2" in author_ids:
            a2_started.set()
        return []

    mock_client.get_works_by_authors.side_effect = works
    mock_client.get_authors_batch.return_value = []

    backend = OpenAlexBackend(mock_client)
    first = asyncio.create_task(backend.get_neighbors_batch(["A1"]))
    await a1_started.wait()
    second = asyncio.create_task(backend.get_neighbors_batch(["A1", "A2"]))

    # A2's fetch must start while A1's fetch is still blocked.
    await asyncio.wait_for(a2_started.wait(), timeout=1)

    release_a1.set()
    assert await first == {"A1": []}
    assert await second == {"A1": [], "A2": []}


async def test_owner_failure_unblocks_waiters():
    """If the owning batch's fetch raises, waiters resolve to [] instead of hanging."""
    fetch_started = asyncio.Event()
    release = asyncio.Event()

    class BoomStore(NeighborStore):
        async def fetch(self, ids):
            fetch_started.set()
            await release.wait()
            raise RuntimeError("store down")

    mock_client = AsyncMock()
    backend = OpenAlexBackend(mock_client, neighbor_cache=NeighborCache(store=BoomStore()))
    first = asyncio.create_task(backend.get_neighbors_batch(["A1"]))
    await fetch_started.wait()
    second = asyncio.create_task(backend.get_neighbors_batch(["A1"]))
    await asyncio.sleep(0)  # let `second` attach to the in-flight future
    release.set()

    with pytest.raises(RuntimeError):
        await first
    assert await asyncio.wait_for(second, timeout=1) == {"A1": []}
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_graph_backend.py::test_overlapping_batch_fetches_disjoint_ids_immediately tests/test_graph_backend.py::test_owner_failure_unblocks_waiters -v`
Expected: both FAIL — the first with `TimeoutError` from `wait_for` (batch 2 is stuck on A1's lock), the second with `TimeoutError` (waiter hangs; the old `finally` releases locks so it may instead fail on the store raising for `second` too — either failure mode is fine, it must not PASS).

- [ ] **Step 3: Implement in-flight futures**

In `backend/graph_backend.py`, in `OpenAlexBackend.__init__`, replace:

```python
        # Deduplicate overlapping cache misses from parallel path searches.
        self._cache_locks: dict[str, asyncio.Lock] = {}
```

with:

```python
        # Deduplicate overlapping cache misses from parallel path searches: the
        # first batch to need an id owns its fetch (a Future in _inflight);
        # concurrent batches await that future for shared ids while fetching
        # their own un-shared ids immediately.
        self._inflight: dict[str, asyncio.Future] = {}
```

Replace the whole `get_neighbors_batch` method with:

```python
    async def get_neighbors_batch(self, ids: list[str]) -> dict[str, list[Connection]]:
        """
        Return neighbors for all ids (author or work). Reads walk the cache layers
        in order — in-memory LRU, then the durable store, then OpenAlex — with each
        id fetched under ALL_EDGE_TYPES/ALL_WORK_EDGE_TYPES so its ring is complete;
        results are filtered to the type-appropriate active set before returning
        (self._work_edge_types for work ids, self._edge_types for author ids —
        dispatched by OpenAlex ID prefix).
        """
        unique = sorted(set(ids))
        resolved: dict[str, list[Connection]] = {}

        # 1) In-memory hits (sync, LRU-touch).
        for i in unique:
            hit = self._cache.get_memory(i)
            if hit is not None:
                resolved[i] = hit

        # 2) Claim ids nobody is fetching; collect futures for ids already in flight.
        loop = asyncio.get_running_loop()
        owned: list[str] = []
        waiting: dict[str, asyncio.Future] = {}
        for i in unique:
            if i in resolved:
                continue
            fut = self._inflight.get(i)
            if fut is None:
                self._inflight[i] = loop.create_future()
                owned.append(i)
            else:
                waiting[i] = fut

        # 3) Fetch owned ids: durable store first, then OpenAlex.
        try:
            if owned:
                from_store = await self._cache.fetch_from_store(owned)
                still_missing = [i for i in owned if i not in from_store]
                fresh: dict[str, list[Connection]] = {}
                if still_missing:
                    fresh = await self._fetch_neighbors_batch(still_missing)
                    self._cache.put(fresh)
                for i in owned:
                    conns = from_store[i] if i in from_store else fresh.get(i, [])
                    resolved[i] = conns
                    fut = self._inflight.pop(i)
                    if not fut.done():
                        fut.set_result(conns)
        except BaseException:
            # Resolve our futures so waiters don't hang, and drop the in-flight
            # entries so a later call retries the fetch.
            for i in owned:
                fut = self._inflight.pop(i, None)
                if fut is not None and not fut.done():
                    fut.set_result([])
            raise

        # 4) Await fetches owned by concurrent batches.
        for i, fut in waiting.items():
            resolved[i] = await fut

        result: dict[str, list[Connection]] = {}
        for i in ids:
            active = self._work_edge_types if _is_work_id(i) else self._edge_types
            result[i] = [c for c in resolved.get(i, []) if c.connection_type in active]
        return result
```

- [ ] **Step 4: Run the batch tests**

Run: `.venv/bin/pytest tests/test_graph_backend.py -v`
Expected: all PASS, including the untouched `test_concurrent_overlapping_batches_share_cache_miss`.

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/pytest`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/graph_backend.py tests/test_graph_backend.py
git commit -m "perf: share overlapping batch fetches via in-flight futures"
```

---

### Task 4: Cache-only stitch pass

**Files:**
- Modify: `backend/graph_backend.py` (base `GraphBackend.get_neighbors_batch` at lines 17-26 and `OpenAlexBackend.get_neighbors_batch` from Task 3)
- Modify: `backend/bigquery_backend.py:25` (accept the new kwarg)
- Modify: `backend/graph_expand.py:166` (stitch call)
- Test: `tests/test_graph_backend.py` (1 test), Create: `tests/test_graph_expand.py`

**Interfaces:**
- Consumes: Task 3's `get_neighbors_batch` internals (`resolved`/`owned`/`waiting` structure).
- Produces: `get_neighbors_batch(ids, cached_only: bool = False)` on `GraphBackend`, `OpenAlexBackend`, and `BigQueryBackend`. With `cached_only=True`, `OpenAlexBackend` reads memory + durable store only; ids with no cached ring resolve to `[]` and OpenAlex is never called. Base class and BigQuery accept the flag but ignore it (their reads have no remote-API cost concern / are the exact-data path).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_graph_backend.py`:

```python
async def test_cached_only_batch_never_hits_client():
    mock_client = AsyncMock()
    mock_client.get_works_by_authors.return_value = [
        make_work("W1", "Paper", [("A1", "Alice"), ("A2", "Bob")])
    ]
    mock_client.get_authors_batch.return_value = []

    backend = OpenAlexBackend(mock_client, edge_types={"coauthor"})
    await backend.get_neighbors_batch(["A1"])  # populate the ring cache
    mock_client.reset_mock()

    result = await backend.get_neighbors_batch(["A1", "A9"], cached_only=True)

    assert {c.target_author_id for c in result["A1"]} == {"A2"}
    assert result["A9"] == []  # uncached id resolves empty — no fetch
    mock_client.get_works_by_authors.assert_not_called()
    mock_client.get_authors_batch.assert_not_called()
```

Create `tests/test_graph_expand.py`:

```python
from unittest.mock import AsyncMock

from backend.graph_backend import GraphBackend
from backend.graph_expand import expand_graph
from backend.models import Connection


class RecordingBackend(GraphBackend):
    """Serves canned rings and records every batch call's cached_only flag."""

    def __init__(self, rings):
        self.rings = rings
        self.calls = []  # (sorted_ids, cached_only)

    async def get_neighbors(self, author_id):
        return self.rings.get(author_id, [])

    async def get_neighbors_batch(self, ids, cached_only=False):
        self.calls.append((sorted(ids), cached_only))
        return {i: self.rings.get(i, []) for i in ids}


def coauthor(target, name):
    return Connection(
        target_author_id=target, target_name=name,
        connection_type="coauthor", label="P1",
    )


async def test_stitch_pass_is_cache_only():
    backend = RecordingBackend({
        "A1": [coauthor("A2", "Bob")],
        "A2": [coauthor("A1", "Alice")],
    })
    client = AsyncMock()
    client.get_authors_batch.return_value = []

    events = [e async for e in expand_graph(backend, client, ["A1"], max_depth=1, top_k=5)]

    assert any(e["type"] == "expansion" for e in events)
    # Expansion levels fetch normally; the final stitch read must be cache-only.
    assert backend.calls[-1] == (["A1", "A2"], True)
    assert all(cached is False for _, cached in backend.calls[:-1])
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/pytest tests/test_graph_backend.py::test_cached_only_batch_never_hits_client tests/test_graph_expand.py -v`
Expected: `test_cached_only_batch_never_hits_client` FAILS with `TypeError: ... unexpected keyword argument 'cached_only'`; `test_stitch_pass_is_cache_only` FAILS on the `cached_only=True` assertion (stitch currently calls with the default).

- [ ] **Step 3: Implement**

In `backend/graph_backend.py`, base class — replace the `get_neighbors_batch` signature and docstring:

```python
    async def get_neighbors_batch(
        self, author_ids: list[str], cached_only: bool = False
    ) -> dict[str, list[Connection]]:
        """Expand all author_ids concurrently. Override for bulk-query backends.

        `cached_only` asks the backend to serve only already-cached rings and
        skip remote-API fetches (ids without a cached ring resolve to []).
        Backends whose bulk reads carry no remote-API cost may ignore it.
        """
```

(body unchanged)

In `OpenAlexBackend.get_neighbors_batch` (the Task 3 version): change the signature to

```python
    async def get_neighbors_batch(
        self, ids: list[str], cached_only: bool = False
    ) -> dict[str, list[Connection]]:
```

and insert this block between step 1 (memory hits) and step 2 (claim ids):

```python
        # Cache-only mode (stitch pass): memory + durable store, never OpenAlex.
        # Ids with no cached ring resolve to [] via the final filter loop.
        if cached_only:
            misses = [i for i in unique if i not in resolved]
            if misses:
                resolved.update(await self._cache.fetch_from_store(misses))
            result: dict[str, list[Connection]] = {}
            for i in ids:
                active = self._work_edge_types if _is_work_id(i) else self._edge_types
                result[i] = [c for c in resolved.get(i, []) if c.connection_type in active]
            return result
```

In `backend/bigquery_backend.py:25`, change the override signature to accept (and ignore) the flag, so `expand_graph` can pass it regardless of backend:

```python
    async def get_neighbors_batch(
        self, author_ids: list[str], cached_only: bool = False
    ) -> dict[str, list[Connection]]:
```

(body unchanged — BigQuery reads are the exact-data path with no per-ring API cost.)

In `backend/graph_expand.py`, the stitch pass — replace:

```python
        neighbor_map = await backend.get_neighbors_batch(list(graph_nodes))
```

with:

```python
        # Cache-only: stitch edges are cosmetic periphery, not worth a fresh
        # round of OpenAlex calls for never-expanded leaf rings (leaf-to-leaf
        # edges whose rings were never cached are simply not drawn).
        neighbor_map = await backend.get_neighbors_batch(list(graph_nodes), cached_only=True)
```

- [ ] **Step 4: Run the new tests**

Run: `.venv/bin/pytest tests/test_graph_backend.py::test_cached_only_batch_never_hits_client tests/test_graph_expand.py -v`
Expected: both PASS.

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/pytest`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/graph_backend.py backend/bigquery_backend.py backend/graph_expand.py tests/test_graph_backend.py tests/test_graph_expand.py
git commit -m "perf: make the stitch pass cache-only"
```

---

### Task 5: Parallelize work-metadata backfill in `_collect_path`

**Files:**
- Modify: `backend/app.py:183-189`

**Interfaces:**
- Consumes: `OpenAlexClient.get_work(work_id) -> dict` (existing).
- Produces: no signature changes; the sequential per-work `await` loop becomes one `asyncio.gather`.

- [ ] **Step 1: Replace the sequential loop**

In `backend/app.py`, `_collect_path`, replace:

```python
    for n in nodes:
        if n["type"] != "work":
            continue
        w = to_obj if n["id"] == to_id else await _client.get_work(n["id"])
        n["cited_by_count"] = w.get("cited_by_count", 0)
        n["publication_year"] = w.get("publication_year")
        n["name"] = w.get("title", n["name"])
```

with:

```python
    async def _work_details(n: dict) -> dict:
        return to_obj if n["id"] == to_id else await _client.get_work(n["id"])

    work_nodes = [n for n in nodes if n["type"] == "work"]
    if work_nodes:
        details = await asyncio.gather(*[_work_details(n) for n in work_nodes])
        for n, w in zip(work_nodes, details):
            n["cited_by_count"] = w.get("cited_by_count", 0)
            n["publication_year"] = w.get("publication_year")
            n["name"] = w.get("title", n["name"])
```

(`asyncio` is already imported in `app.py`.)

- [ ] **Step 2: Run the full suite + import check**

```bash
.venv/bin/pytest
.venv/bin/python -c "import backend.app; print('import ok')"
```

Expected: tests PASS, `import ok`.

- [ ] **Step 3: Commit**

```bash
git add backend/app.py
git commit -m "perf: gather work-metadata backfill instead of sequential awaits"
```

---

### Task 6: Frontend — full-quality layout on stream end

**Files:**
- Modify: `frontend/app.js:1052-1053` (the `done` handler inside `startExpansion`)

**Interfaces:**
- Consumes: existing `runLayout()` (`frontend/app.js:1161` — fCoSE quality `proof`, `randomize:true`, `numIter:3000`, origins pinned via `fixedNodeConstraint`, with a `cose` fallback).
- Produces: behavior only — on SSE `done`, the graph reorganizes once into the full-quality arrangement (approved tradeoff) instead of freezing in its streamed-in state.

- [ ] **Step 1: Swap the final settle**

In the `done` listener, replace:

```js
        // Final settle: relax + frame the already-grown graph without a re-shuffle.
        runLayoutIncremental({ fit: true });
```

with:

```js
        // Full-quality settle: one visible reorganization that untangles
        // whatever arrangement the streaming order produced (origins stay
        // pinned via fixedNodeConstraint inside runLayout).
        runLayout();
```

- [ ] **Step 2: Syntax check**

Run: `node --check frontend/app.js`
Expected: no output (exit 0).

- [ ] **Step 3: Commit**

```bash
git add frontend/app.js
git commit -m "fix: run full-quality layout at stream end instead of incremental settle"
```

(Visual verification happens in Task 8.)

---

### Task 7: Frontend — edge-aware spawn seeding + path midpoint fix

**Files:**
- Modify: `frontend/app.js:869-895` (`seedPosition`), `frontend/app.js:897-936` (`addOrUpdateNode` signature + call), `frontend/app.js:1034-1040` (`expansion` handler)

**Interfaces:**
- Consumes: node/edge payload shapes from `backend/graph_expand.py` (`{nodes: [...], edges: [{source, target, ...}]}`); `path_pair` is the canonical string `"idA||idB"` built by `pair_key` in `backend/app.py:358`.
- Produces: `seedPosition(nodeData, seedHints?)` and `addOrUpdateNode(nodeData, seedHints?)` where `seedHints` is a `Map<nodeId, neighborNodeId>`. All existing single-argument call sites stay valid (hints are optional).

- [ ] **Step 1: Make seedPosition edge-aware and fix the midpoint branch**

Replace the whole `seedPosition` function with:

```js
  function seedPosition(nodeData, seedHints) {
    const jitter = r => (Math.random() - 0.5) * 2 * r;
    const near = (p, r) => ({ x: p.x + jitter(r), y: p.y + jitter(r) });

    if (nodeData.type === 'expansion') {
      const depth = nodeData.depth || 1;
      // Prefer the already-placed node this one actually connects to (from the
      // expansion event's edge list) — the owner origin puts depth-2+ nodes at
      // a random angle unrelated to their edges, which bakes in tangles.
      const neighborId = seedHints && seedHints.get(nodeData.id);
      if (neighborId) {
        const neighbor = cy.getElementById(neighborId);
        if (neighbor.length) return near(neighbor.position(), 60 + depth * 40);
      }
      const owners = nodeData.expandOwners || [];
      for (const oid of owners) {
        const owner = cy.getElementById(oid);
        if (owner.length) {
          return near(owner.position(), 60 + depth * 40);
        }
      }
    }
    if (nodeData.type === 'path') {
      const pair = nodeData.path_pair || (nodeData.pathPairs || [])[0];
      if (pair) {
        // pair is the canonical "idA||idB" key (see pair_key in backend/app.py).
        const [idA, idB] = pair.split('||');
        const a = cy.getElementById(idA);
        const b = cy.getElementById(idB);
        if (a.length && b.length) {
          const pa = a.position(), pb = b.position();
          return near({ x: (pa.x + pb.x) / 2, y: (pa.y + pb.y) / 2 }, 40);
        }
      }
    }
    return near(originsCentroid(), 120);
  }
```

- [ ] **Step 2: Thread hints through addOrUpdateNode**

Change the `addOrUpdateNode` signature (line 897) from `function addOrUpdateNode(nodeData) {` to:

```js
  function addOrUpdateNode(nodeData, seedHints) {
```

and its seeding line from `if (nodeData.type !== 'origin') el.position = seedPosition(nodeData);` to:

```js
    if (nodeData.type !== 'origin') el.position = seedPosition(nodeData, seedHints);
```

- [ ] **Step 3: Build hints in the expansion handler**

Replace the `expansion` listener body:

```js
      source.addEventListener('expansion', e => {
        const data = JSON.parse(e.data);
        showProgress(`Building neighborhood (depth ${data.depth}/3)…`);
        // Nodes stream before their edges within an expansion event, so map
        // each new node to something it connects to for spawn seeding.
        const seedHints = new Map();
        data.edges.forEach(ed => {
          if (!seedHints.has(ed.target)) seedHints.set(ed.target, ed.source);
        });
        data.nodes.forEach(n => addOrUpdateNode(n, seedHints));
        data.edges.forEach(addEdge);
        scheduleGrow();
      });
```

- [ ] **Step 4: Syntax check**

Run: `node --check frontend/app.js`
Expected: no output (exit 0).

- [ ] **Step 5: Commit**

```bash
git add frontend/app.js
git commit -m "fix: seed streamed nodes near a real graph neighbor; fix path midpoint seeding"
```

---

### Task 8: End-to-end verification + AGENTS.md update

**Files:**
- Modify: `AGENTS.md` (Progress section)

**Interfaces:**
- Consumes: everything above.
- Produces: verified behavior + updated project log.

- [ ] **Step 1: Full test suite**

Run: `.venv/bin/pytest -v`
Expected: all PASS.

- [ ] **Step 2: Drive the app**

```bash
.venv/bin/uvicorn backend.app:app --port 8000
```

Then (Chrome DevTools MCP or manually with Eric watching):
1. Open `http://127.0.0.1:8000`, use the empty-state example CTA (Hinton A5108093963 ↔ Chomsky A5072532913).
2. Confirm the run completes and note rough wall-clock vs. before (the pair is likely cached, so also try one fresh researcher pair for a cold-path timing).
3. Confirm that when the progress overlay disappears, the graph performs one animated reorganization and settles untangled (no long edges crossing the whole canvas, no random-looking placements).
4. Confirm streamed nodes appear near their connected node while the stream runs (not at random angles around the origin).
5. Check the browser console for errors.

Expected: faster run, one clean final settle, no console errors.

- [ ] **Step 3: Update AGENTS.md**

Add to the Progress section (adjust facts to what was actually observed):

```markdown
- **2026-07-05 — Perf + initial-layout fixes (branch `perf-and-layout`).**
  Spec: `docs/superpowers/specs/2026-07-05-perf-and-layout-design.md`.
  Backend: OpenAlex semaphore 5→10 + HTTP/2 (`httpx[http2]`); JsonNeighborStore
  flushes on a background task (record no longer rewrites the file on the
  request path); `get_neighbors_batch` dedups overlapping fetches via per-id
  in-flight futures (replaces the lock convoy); stitch pass is cache-only
  (`cached_only=True` — leaf↔leaf edges without cached rings aren't drawn).
  Frontend: SSE `done` now runs the full-quality `runLayout()` (one visible
  reorganization, approved); expansion nodes seed near a real connected
  neighbor via per-event seed hints; fixed path-node midpoint seeding (was
  reading `.from_id` off the `"idA||idB"` string). Heuristic fast-mode toggle
  deliberately deferred.
```

- [ ] **Step 4: Commit**

```bash
git add AGENTS.md
git commit -m "docs: log perf + layout changes in AGENTS.md"
```
