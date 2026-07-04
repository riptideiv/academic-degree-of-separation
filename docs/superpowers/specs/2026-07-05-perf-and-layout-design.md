# Performance + Initial Layout Design

Date: 2026-07-05
Status: approved by Eric

## Problems

1. Processing speed is too slow across the whole run (path BFS + neighborhood
   expansion), on both cold and warm cache.
2. The initial map with default configs is messy: node positions look random,
   edges tangle across the canvas, and clumps overlap.

Constraints from brainstorming:

- Prefer speedups that preserve exactness (exact w.r.t. the current neighbor
  ring definition). A heuristic "fast mode" is acceptable only as a later,
  optional toggle - out of scope for this change.
- A one-time visible re-layout jump at the end of streaming is acceptable.

## Part 1 - Speed (exactness-preserving pipeline fixes)

### 1a. Raise OpenAlex concurrency + HTTP/2

`backend/openalex_client.py`

- Semaphore 5 -> 10 (polite pool allows ~10 req/s; mailto/api-key already sent).
- Enable HTTP/2 on the shared `httpx.AsyncClient` (`http2=True`, requires the
  `h2` extra: `httpx[http2]`).

### 1b. Non-blocking JSON neighbor store

`backend/neighbor_store.py` - `JsonNeighborStore.record` currently rewrites the
entire JSON file (1.3 MB and growing) synchronously on the request path, on
every fresh fetch.

- Mirror the `SupabaseNeighborStore` pattern: `record()` only updates `_data`
  and marks dirty; a background task started in `open()` flushes to disk every
  `FLUSH_INTERVAL_S`; `close()` cancels the task and does a final flush.
- Serialization + file write happen in the background task. Use
  `asyncio.to_thread` for the dump/write so even the background flush does not
  block the loop.

### 1c. Per-id in-flight deduplication instead of lock convoy

`backend/graph_backend.py` - `get_neighbors_batch` currently acquires per-id
locks for ALL its misses up front and holds them across the whole fetch, so
parallel path searches serialize almost completely.

- Replace `_cache_locks` with `_inflight: dict[str, asyncio.Future]`.
- A batch claims only the ids nobody else is fetching (creates futures for
  them), awaits existing futures for ids already being fetched by another
  batch, and resolves its futures when its fetch completes (failures resolve
  to `[]` and clear the entry so a later call can retry).
- Result: two concurrent searches over overlapping frontiers share work
  instead of queueing behind each other.

### 1d. Stitch pass goes cache-only

`backend/graph_expand.py` - the post-expansion stitch pass fetches full rings
for never-expanded leaf nodes from OpenAlex just to draw periphery edges.

- Add a cache-only read path (memory LRU + durable store, never OpenAlex) and
  use it for the stitch pass.
- Accepted tradeoff: a few leaf-to-leaf edges are not drawn when the leaf ring
  was never previously cached. These edges are cosmetic periphery.
- Implementation: a `cached_only: bool = False` parameter on
  `get_neighbors_batch` (base class provides the trivial fallback).

### 1e. Parallelize work-metadata backfill

`backend/app.py` `_collect_path` - the per-work `get_work` calls in the
backfill loop run sequentially; gather them.

## Part 2 - Initial layout

### 2a. Full-quality final layout on stream end

`frontend/app.js` - on the SSE `done` event, replace the incremental settle
(`runLayoutIncremental({fit:true})`, randomize:false, 500 iter) with the
existing `runLayout()` (fCoSE quality `proof`, `randomize:true`, 3000 iter,
origins pinned via `fixedNodeConstraint`). Streaming keeps using the cheap
incremental layout; the end-of-stream pass untangles whatever the streaming
order produced. One visible reorganization jump - approved.

### 2b. Edge-aware spawn seeding

`frontend/app.js` `seedPosition` - expansion nodes currently seed near their
owner ORIGIN at a random angle, unrelated to the node they connect to.

- Seed near an already-placed graph neighbor when one exists; fall back to the
  owner origin, then centroid, as today. Within an `expansion` SSE event nodes
  are added before their edges, so the neighbor is resolved from the same
  event's payload: the handler builds a target -> source hint map from
  `data.edges` and passes it through to `addOrUpdateNode` / `seedPosition`.
- Keep the existing jitter radii.

### 2c. Fix path-node midpoint seeding bug

`seedPosition` reads `pair.from_id` off `path_pair`, but `path_pair` is the
string `"idA||idB"` (see `pair_key` in `backend/app.py`), so the midpoint
branch never runs and path nodes seed at the centroid. Split the key on `||`
and look up both endpoint nodes.

## Out of scope

- Heuristic fast-mode toggle (beam-limited BFS frontier) - possible follow-up.
- BigQuery backend.
- Constraint-based radial placement (fCoSE placement constraints).

## Testing

- Existing `tests/test_graph_backend.py` must keep passing (default in-memory
  `NeighborCache()` behavior unchanged).
- New tests: in-flight dedup (two concurrent `get_neighbors_batch` calls with
  overlapping misses trigger one fetch per id); JSON store flush-on-close;
  cache-only batch read returns without hitting the client.
- Frontend verified by driving the app (Chrome DevTools MCP): run the
  Hinton <-> Chomsky example, confirm the final layout pass runs on `done` and
  the graph settles untangled; compare rough wall-clock before/after.
