# Researcher Degree of Separation

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

Type a researcher's name in the sidebar, click a result, then add a second one. The
sidebar shows the **degrees of separation** and the full shortest path (names plus the
paper or institution behind each hop), and the graph renders their neighborhoods.

No API key is required, but configuring your email is recommended (see below).

## Configuration

All optional. Set via environment variables, or an `api-keys.json` file in the repo
root (git-ignored):

| Env var | `api-keys.json` key | Purpose |
|---|---|---|
| `OPENALEX_MAILTO` | `mailto` | Your email. Puts requests in OpenAlex's faster ["polite pool"](https://docs.openalex.org/how-to-use-the-api/rate-limits-and-authentication#the-polite-pool). **Recommended.** |
| `OPENALEX_KEY` | `openalex-key` | OpenAlex premium API key, if you have one. |
| `BACKEND` | (n/a) | `openalex` (default) or `bigquery`. |
| `GOOGLE_CLOUD_PROJECT` | `gcp-project` | GCP project for the optional BigQuery backend. |

Example `api-keys.json`:

```json
{ "mailto": "you@example.com" }
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
  app.py              FastAPI app: /api/authors, /api/path, /api/graph/expand (SSE)
  bfs.py              Bidirectional BFS path-finder
  graph_backend.py    OpenAlex graph backend (co-author / citation / institution edges)
  graph_expand.py     Neighborhood expansion (ranked BFS) for the visualization
  openalex_client.py  Thin async OpenAlex HTTP client (shared, pooled)
  bigquery_backend.py Optional BigQuery backend (same interface)
  models.py           Pydantic models
frontend/
  index.html, app.js, style.css   Cytoscape UI (served as static files)
tests/                pytest suite
```

## How it works

1. **Search** (`/api/authors`) resolves names to OpenAlex author IDs.
2. **Expand** (`/api/graph/expand`) streams the graph as you add researchers: it finds
   the shortest path between the new researcher and each existing one (BFS), then
   builds a ranked neighborhood around everyone. Each connected pair emits a `path`
   event carrying the hop count and the ordered steps.
3. The **frontend** consumes the SSE stream, draws nodes/edges with Cytoscape, and
   lists the degrees of separation + shortest paths in the sidebar. Sidebar controls:
   **Edge types** choose which connection types the search uses (co-author / citation
   / institution); **Neighborhood** sets how much surrounding network to draw (from
   "just the connection" up to a large neighborhood); **Show all names** reveals every
   node's label (otherwise only the researchers + connecting path are labeled, and
   the rest show their name on hover); **Layout** sliders (Spacing, Link length) tune
   the force-directed layout live without re-running the search.

After expanding each researcher's neighborhood, the backend also adds the real edges
among the nodes that are already on screen, so the connecting/middle nodes link into
the network instead of forming isolated chains between the two hubs.

## Deployment

`render.yaml` describes a one-service deploy on [Render](https://render.com): it
installs `requirements.txt` and runs `uvicorn backend.app:app`.
