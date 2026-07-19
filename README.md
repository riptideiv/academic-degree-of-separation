# Six Degrees of Academia

"Six degrees of separation" for academics. Pick two (or more) researchers and the
app finds how they're connected (through **co-authorship**, **citations**, or
**shared institutions**) using the free [OpenAlex](https://openalex.org) scholarly
graph, then draws the network interactively.

- **Backend:** FastAPI. Bidirectional BFS over OpenAlex, streamed to the browser via
  Server-Sent Events (SSE).
- **Frontend:** plain HTML/CSS/JS with [Cytoscape.js](https://js.cytoscape.org/)
  (+ the fCoSE layout) for the graph. No build step.

## Quick start

```bash
# 1. Create a virtualenv and install dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Run the dev server (auto-reloads on save)
uvicorn backend.app:app --reload --port 8000

# 3. Open the app
open http://127.0.0.1:8000        # macOS  (or just visit the URL)
```

Type a researcher's name in the sidebar, choose a result, then add a second one (or
use the example on the empty canvas). The sidebar shows the **degrees of separation**
and the full shortest path, while the graph renders their surrounding research network.
Optionally choose a home institution in **Institution Explorer** to discover local
researchers whose coauthor networks are closest to the researchers in the main graph.

OpenAlex now requires an API key for normal API use. Keys are free; create an account
and copy yours from [openalex.org/settings/api](https://openalex.org/settings/api).

## Configuration

For local development, put configuration in `.env.local` at the repository root.
`backend.app` loads this file without overriding variables already supplied by the
shell or deployment platform. Do not commit `.env.local`.

| Variable | Purpose |
|---|---|
| `OPENALEX_KEY` | OpenAlex API key used by all scholarly-data requests. Get a free key at [openalex.org/settings/api](https://openalex.org/settings/api). |
| `OPENALEX_MAILTO` | Courtesy contact identifier sent to OpenAlex in the User-Agent and `mailto` parameter. |
| `SUPABASE_POOLER_CONNECTION_STRING` | Supabase Supavisor **transaction pooler** Postgres connection string (normally port 6543). Enables the durable neighbor cache; without it the app uses `neighbor_cache.json`. |
| `SUPABASE_URL` | Supabase project URL. Kept with the project configuration for other Supabase integrations; the current asyncpg cache connects through `SUPABASE_POOLER_CONNECTION_STRING`. |
| `SUPABASE_POOL_MAX` | Maximum asyncpg pool size for Supabase (default `5`). Keep this small for the transaction pooler. |
| `NEIGHBOR_CACHE_MAX` | Maximum neighbor rings retained in the in-memory LRU (default `10000`). Durable-store contents are unaffected. |
| `BACKEND` | Graph backend: `openalex` (default) or `bigquery`. |
| `GOOGLE_CLOUD_PROJECT` | Required only when `BACKEND=bigquery`. |
| `OPENALEX_CONCURRENCY` | Optional maximum number of concurrent OpenAlex requests; defaults to `15` with a key and `8` without one. |

Example `.env.local`:

```dotenv
OPENALEX_KEY=your_openalex_key
OPENALEX_MAILTO=you@example.edu
SUPABASE_POOLER_CONNECTION_STRING=postgresql://...
SUPABASE_URL=https://your-project.supabase.co
GOOGLE_CLOUD_PROJECT=your-gcp-project
```

## Running the tests

```bash
pytest -q
```

The suite mocks all network access (`respx` for HTTP, `AsyncMock`/`ASGITransport` for
the app), so it's fast and offline.

## Project layout

```
backend/
  app.py              FastAPI app: search, graph SSE, institution suggestions, cache
  bfs.py              Bidirectional BFS path-finder
  graph_backend.py    OpenAlex graph backend (co-author / citation / institution edges)
  graph_expand.py     Neighborhood expansion (ranked BFS) for the visualization
  openalex_client.py  Thin async OpenAlex HTTP client (shared, pooled, HTTP/2,
                      author-metadata LRU)
  neighbor_store.py   Neighbor-ring cache: bounded LRU + durable store (JSON/Supabase)
  bigquery_backend.py Optional BigQuery backend (same interface)
  models.py           Pydantic models
frontend/
  index.html, app.js, style.css   Cytoscape UI (served as static files)
scripts/
  bench_search.py     Benchmark harness for /api/graph/expand (cold vs. warm cache)
  bench_ab.py         Interleaved A/B cold-search benchmark: working tree vs a baseline git ref
tests/                pytest suite
```

## How it works

1. **Search** (`/api/authors`) resolves names to OpenAlex author IDs.
2. **Expand** (`/api/graph/expand`) streams the graph as you add researchers: the
   shortest-path search (BFS) between the new researcher and each existing one runs
   in the background while a ranked neighborhood around everyone is built and
   streamed, so the graph grows during the search. Each connected pair emits a
   `path` event carrying the hop count and the ordered steps as soon as the search
   finishes.
3. **Institution Explorer** (`/api/institution-suggestions`) treats author origins as
   a research-interest profile. It ranks current-primary researchers at the selected
   home institution using coauthorship paths only; citations and shared institutions
   do not affect these suggestions. Profiles emphasize topics, representative works,
   OpenAlex/ORCID links, with adding a suggestion to the graph as a secondary action.
4. The **frontend** consumes the SSE stream, draws nodes/edges with Cytoscape, and
   lists the degrees of separation + shortest paths in the sidebar. Edge types share
   one color and are distinguished by dash pattern (solid = co-authorship, dashed =
   citation with the arrow pointing at the cited, short dash = shared institution;
   work → author edges are green); a collapsible **legend** in the corner explains
   the encoding. Sidebar controls (grouped into collapsible cards): **Edge types**
   choose which connection types the search uses (co-author / citation
   / institution); **Neighborhood** sets how much surrounding network to draw (from
   "just the connection" up to a large neighborhood); **Show all names** reveals every
   node's label (otherwise only the researchers + connecting path are labeled, and
   the rest show their name on hover); **Layout** sliders (Spacing, Link length) tune
   the force-directed layout live without re-running the search. The sidebar itself
   can be dragged to resize (snapping to its default width), collapsed entirely by
   clicking the handle, or folded to the left with the **☰ menu button**; widths and
   card states persist across reloads. On narrow screens (phones) the sidebar becomes a
   full-screen overlay and a segmented **Graph / Menu** switcher at the top toggles
   between the graph and the controls.

After expanding each researcher's neighborhood, the backend also adds the real edges
among the nodes that are already on screen, so the connecting/middle nodes link into
the network instead of forming isolated chains between the two hubs. This stitch pass
reads only the neighbor cache (no extra OpenAlex calls), so edges between nodes whose
rings were never fetched are simply not drawn.

## Deployment

`render.yaml` describes a one-service deploy on [Render](https://render.com): it
installs `requirements.txt`, runs `uvicorn backend.app:app`, and declares the required
secret environment variables. Render does not read the local `.env.local` file.
