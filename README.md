# Coauthor Distance

**Find people whose work interests you, then understand how they connect.**
Start with your university and a research topic, paper, or author. Inspect supporting
publications, explore coauthor connections, and keep a private shortlist with notes.
Powered by [OpenAlex](https://openalex.org); no sign-up is required.

[Open the tool](https://academic-degree-of-separation.onrender.com/) ·
[How it works](https://academic-degree-of-separation.onrender.com/guide/) ·
[Expansion design](docs/app-expansion-design.md)

- **Discover:** find researchers at an institution from up to eight explicit topic,
  paper, or author references. Results carry publication evidence; an optional
  connection check does not determine whether a match is eligible.
- **Connections:** explore the existing graph, verified paths, and publication
  evidence. Coauthorship is the default; citations and institutions are optional.
  Share starting points, save a graph image, or export a connection report.
- **Saved:** save researchers and papers with local notes. Export/import JSON to
  move them between browsers; conflicting notes are previewed before import.

Results are discovery leads, not recruitment claims or an exhaustive ranking.

- **Backend:** FastAPI. Bidirectional BFS over OpenAlex, streamed to the browser via
  Server-Sent Events (SSE).
- **Frontend:** plain HTML/CSS/JS with [Cytoscape.js](https://js.cytoscape.org/)
  (+ the fCoSE layout) for the graph. No build step.

## Quick start

```bash
# Clone the repository
git clone https://github.com/riptideiv/coauthor-distance.git
cd coauthor-distance

# 1. Create a virtualenv and install dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Run the dev server (auto-reloads on save)
uvicorn backend.app:app --reload --port 8000

# 3. Open the app
open http://127.0.0.1:8000        # macOS  (or just visit the URL)
```

Open **Discover**, select a reference and university, then choose **Find researchers**.
Open a result's profile to read recent or most-cited work, save it, or explore its
network. **Connections** also works independently and includes an example pair.

OpenAlex now requires an API key for normal API use. Keys are free; create an account
and copy yours from [openalex.org/settings/api](https://openalex.org/settings/api).

## Saved research and sharing

The versioned `coauthor_saved_v1` browser key contains up to 300 researchers/papers,
6,000 characters per note, and optional public discovery context. Export format:
`{"version":1,"items":[...]}`. Each item has `id`, `type`, `display_name`, `saved_at`,
`note`, and optionally `context` (institution and reference IDs). Import validates
all records before changing state, previews conflicts, and merges by ID. Notes are
included in exports; unrelated browser state and credentials are excluded. JSON
imports/exports are limited to 2 MB. Corrupt original storage is preserved for recovery;
storage failure uses an explicitly labeled in-memory fallback.

Profile links use `/researchers/A…`; discovery links use
`/discover?institution=I…&ref=T…` (repeat `ref` for other references). Links contain
public IDs only and rebuild current results. Graph `#map=` links remain compatible.
Notes and saved lists stay local. Moving hosts/domains requires exporting from the
old origin and importing on the new one.

## Configuration

For local development, put configuration in `.env.local` at the repository root.
`backend.app` loads this file without overriding variables already supplied by the
shell or deployment platform. Do not commit `.env.local`.

| Variable | Purpose |
|---|---|
| `OPENALEX_KEY` | OpenAlex API key used by all scholarly-data requests. Get a free key at [openalex.org/settings/api](https://openalex.org/settings/api). |
| `OPENALEX_MAILTO` | Courtesy contact identifier sent to OpenAlex in the User-Agent and `mailto` parameter. |
| `SUPABASE_POOLER_CONNECTION_STRING` | Supabase Supavisor **transaction pooler** Postgres connection string (normally port 6543). Enables the durable neighbor cache; without it the app uses `neighbor_cache_v2.json`. |
| `SUPABASE_URL` | Supabase project URL. Kept with the project configuration for other Supabase integrations; the current asyncpg cache connects through `SUPABASE_POOLER_CONNECTION_STRING`. |
| `SUPABASE_POOL_MAX` | Maximum asyncpg pool size for Supabase (default `5`). Keep this small for the transaction pooler. |
| `NEIGHBOR_CACHE_MAX` | Maximum neighbor rings retained in the in-memory LRU (default `10000`). Durable-store contents are unaffected. |
| `BACKEND` | Graph backend: `openalex` (default) or `bigquery`. |
| `GOOGLE_CLOUD_PROJECT` | Required only when `BACKEND=bigquery`. |
| `OPENALEX_CONCURRENCY` | Optional maximum number of concurrent OpenAlex requests; defaults to `15` with a key and `8` without one. |
| `CACHE_ADMIN_TOKEN` | Optional secret for server cache administration. `DELETE /api/cache` requires this token as a Bearer credential; without it, cache deletion is disabled. Normal searches need no admin token. |
| `ANALYTICS_ENABLED` | First-party usage collection, enabled by default. Set `false` to stop collecting events. |
| `ANALYTICS_ADMIN_TOKEN` | Separate secret for the private usage dashboard at `/analytics.html`. Without it, report access is disabled; background collection can still run. |

Example `.env.local`:

```dotenv
OPENALEX_KEY=your_openalex_key
OPENALEX_MAILTO=you@example.edu
SUPABASE_POOLER_CONNECTION_STRING=postgresql://...
SUPABASE_URL=https://your-project.supabase.co
GOOGLE_CLOUD_PROJECT=your-gcp-project
```

## Search visibility and outreach

The app and the static `/guide/` page have distinct titles, descriptions, canonical
URLs, and social previews. The guide is readable without JavaScript and linked
from the app. `/robots.txt` advertises `/sitemap.xml`, which lists only those two
public pages. The private analytics page remains `noindex` and crawlable so search
engines can read that directive. API and operational routes are excluded from
crawling; robots rules are not access controls.

Canonical and social URLs target the current Render deployment. When deploying
under a new public hostname, update the URLs together in `frontend/index.html`,
`frontend/guide/index.html`, `frontend/robots.txt`, and `frontend/sitemap.xml`.
`frontend/social-preview.svg` is the editable source for the 1200 × 630 PNG used
by social previews; regenerate the PNG when changing the artwork.

The legacy link at `www.xingruic.net/tools/researchers` redirects to the Render
deployment. That redirect is configured outside this repository. When connecting
a new public domain, update that redirect and the GitHub repository homepage too.

Publishing a sitemap does not itself submit it to Search
Console or guarantee indexing. The guide is untracked; existing analytics count
activity in the interactive app, not guide visits or guide-to-app conversions.

## Running the tests

```bash
pytest -q
node --test tests/js/*.test.cjs
```

The suite mocks all network access (`respx` for HTTP, `AsyncMock`/`ASGITransport` for
the app), so it's fast and offline.
Pytest wraps some legacy JavaScript regression tests when Node.js is available.
Run the Node command above for the complete browser-state, discovery, saved-list,
request-ordering, and export suite.
GitHub Actions runs the full suite on Python 3.12 and 3.13, plus the JavaScript
behavior and syntax checks with Node.js 24.

For a live search comparison against a previous commit:

```bash
python scripts/bench_ab.py --ref HEAD~1 --rounds 3 --edges coauthor \
  --pair "Geoffrey Hinton::Yoshua Bengio"
```

The benchmark alternates isolated servers, authenticates and verifies cache resets,
and checks that streams finish with the expected path results. Failed, interrupted,
and no-path runs are reported separately and cause an unsuccessful benchmark exit;
they cannot count as a speed improvement. Live runs consume the configured OpenAlex
allowance. For `bench_search.py` against an existing server, set `CACHE_ADMIN_TOKEN`
to the same value configured on that server.

## Project layout

```
backend/
  app.py              FastAPI app: search, graph SSE, institution suggestions, cache
  discovery.py        Institution-scoped topic/publication discovery
  bfs.py              Bidirectional BFS path-finder
  graph_backend.py    OpenAlex graph backend (co-author / citation / institution edges)
  graph_expand.py     Neighborhood expansion (ranked BFS) for the visualization
  affiliation_overrides.py  Reviewed exact-ID affiliation / work-scope corrections
  data/affiliation_overrides.json  Official-source correction records
  institution_ranking.py  Stable topic/citation candidate balancing and result order
  path_evidence.py    Work-level author identity and path-continuity checks
  openalex_client.py  Thin async OpenAlex HTTP client (shared, pooled, HTTP/2,
                      author-metadata LRU)
  neighbor_store.py   Neighbor-ring cache: bounded LRU + durable store (JSON/Supabase)
  usage_analytics.py  Background usage storage and aggregate reports (SQLite/Postgres)
  analytics_routes.py  Minimal event collection and protected report API
  bigquery_backend.py Optional BigQuery backend (same interface)
  models.py           Pydantic models
frontend/
  index.html, app.js, style.css   Existing graph UI and application shell
  app_shell.js, shell.css        Routes, profiles, and shared interface
  discovery.js, discovery_state.js, discovery_links.js   Discovery inputs/results/sharing
  saved.js, saved_store.js       Shortlist, notes, validated import/export
  analytics.html      Private usage dashboard; enter the analytics admin token
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
3. **Discover** (`/api/discover`) uses explicit topics and topics from selected papers
   or authors. It samples up to 15 recent works for each of up to eight topics,
   extracts candidates with authorship evidence at the selected institution, checks
   last-known/reviewed affiliation and identity scopes, and returns up to 20 people
   from at most 80 candidates. Ranking uses topic coverage, capped supporting-work
   counts, and recency, rather than citations or graph distance. Coverage labels
   disclose bounded or partial results. See [live evaluation](docs/discovery-evaluation.md).
   Optional `/api/connections/check` verifies a path of up to two coauthor steps
   between explicitly chosen people. The legacy `/api/institution-suggestions`
   endpoint retains its verified-path contract for existing clients.
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

**Stop search** closes the active stream and keeps the graph and paths already
received. Incomplete or interrupted pair searches are labeled explicitly; use
**Apply options** to retry. Turning off every connection type now means no edges
of that kind, including when all work connections are disabled.

**Fit graph** brings the network back into view without rerunning a layout or
search. **Save image** downloads a PNG of the whole network, and **Download
connection report** saves a Markdown report containing ordered paths, OpenAlex
profile links, citation directions, search options, and coverage limitations.
Exports run locally and do not include API keys. **Clear canvas** clears only this
browser's graph; it preserves the shared cache for fast future searches. The search
dialog supports Escape, keyboard focus containment, and focus restoration.
Researcher results can be expanded with Enter or Space to view their top papers.
If a paper request fails, its panel offers a retry instead of reporting an empty
publication list. Searching and exploring still work when browser storage is
unavailable; saved settings and graphs then last only for the current page.

After expanding each researcher's neighborhood, the backend also adds the real edges
among the nodes that are already on screen, so the connecting/middle nodes link into
the network instead of forming isolated chains between the two hubs. This stitch pass
reads only the neighbor cache (no extra OpenAlex calls), so edges between nodes whose
rings were never fetched are simply not drawn.

Author and work rings use the `v4` cache namespace. Older author rings could omit
links to authors fetched in the same batch; older work rings could cache missing
OpenAlex records as empty neighborhoods. Older rings are rebuilt on first use,
so the first search after upgrading can have cold cache misses. Existing `v4`
author entries are retained. No durable table or JSON file is deleted during this
migration. JSON cache saves replace the file atomically, preserving the previous
complete cache if a write fails. Repeated coauthors are deduplicated before
constructing connections, retaining the first publication's evidence without extra
API requests.

### Browser keys and API boundaries

Personal OpenAlex keys are set through a same-origin JSON request to
`POST /api/openalex-key` (`{"api_key":"..."}`). They live in an HttpOnly,
SameSite=Strict session cookie scoped to `/api`, marked Secure over HTTPS. A
personal key applies only to that browser's requests and never replaces the
deployment key. Sending an empty key removes the personal override and falls back
to the deployment key. Old localStorage keys are migrated once and removed;
the input is cleared after saving. Key endpoints use `Cache-Control: no-store`.

Graph endpoints accept canonical author/work IDs or their OpenAlex URLs and reject
malformed values before making upstream requests. An expansion accepts up to 25
existing origins and 500 path IDs; Institution Explorer accepts up to 10 origins.
Excess input returns HTTP 422 instead of silently dropping researchers. Omitted
edge options retain the API defaults; `edges=none` or `work_edges=none` explicitly
disables that group. Mixing `none` with another type is invalid.

Serve the UI and API from the same HTTPS origin in production. Public API errors
contain safe messages and codes; upstream URLs containing credentials are never
returned in error responses. Configure `CACHE_ADMIN_TOKEN` only when remote cache
administration is needed, and keep it out of browser code.

### Reviewed affiliation corrections

When OpenAlex has a missing current affiliation, add a reviewed entry to
`backend/data/affiliation_overrides.json` using exact OpenAlex author/institution IDs
and an official university evidence URL. `action` may be `include` or `exclude`;
exclusions win. Do not match corrections by name. For a conflated author profile, add
`verified_work_ids` so coauthor paths and representative works use only the reviewed
identity's publications. Omit that field for a clean author profile whose affiliation
alone needs correction.

## Deployment

`render.yaml` describes a one-service deploy on [Render](https://render.com): it
installs `requirements.txt`, runs `uvicorn backend.app:app`, and declares the required
secret environment variables. Render does not read the local `.env.local` file.

## Usage analytics

Background collection starts when the updated app is deployed. Open
`https://your-app-host/analytics.html` and enter `ANALYTICS_ADMIN_TOKEN` to view
7-, 30-, 90-, or 365-day totals and daily activity. The dashboard keeps its token
only in memory and sends it in an Authorization header. Signing out clears it;
never put it in a URL. The Render Blueprint generates this secret on sync if it
does not already exist; retrieve it from the service's Environment settings. For
services not managed by the Blueprint, set a long random secret there manually.
See [Render's secret generation documentation](https://render.com/docs/blueprint-spec#generating-random-secrets).

| Metric | What it counts |
|---|---|
| Unique visitors | Distinct random browser IDs over the selected period, including returning browsers only once. This estimates browsers, not individual people. |
| Visits | Page loads that run the tracker; reloading adds a visit. Health checks and assets are excluded. |
| Search submissions | Valid researcher, work, or institution search submissions, including searches served from the browser cache. Pagination and automatic request retries are excluded. |
| Searching visitors | Distinct browser IDs that submitted a search during the period. |
| Graph runs | Graph expansion requests; applying options can run several expansions. |
| Explorer runs | Institution recommendation requests, including automatic refreshes. |

The existing `SUPABASE_POOLER_CONNECTION_STRING` enables durable PostgreSQL
storage across Render restarts. The app creates `usage_analytics_events` with row
level security and no public policies; use the trusted table-owner database
connection, not a browser Supabase key. Without a database connection, analytics
uses the ignored local `usage_analytics.sqlite3` file. **Render's default ephemeral
filesystem does not preserve that local file across deploys.** The dashboard
identifies its storage mode.

Only the event type, UTC day, random event ID, and a hash of the random browser ID
are stored. A random browser ID is kept in local browser storage, with an in-memory
fallback when storage is blocked. Analytics does not use cookies, so it also works
when the tool is embedded in an iframe with third-party cookies blocked. No
search text, researcher names, IP
addresses, referrers, or API keys are stored in these analytics records. The
tracker respects Do Not Track and Global Privacy Control. Up to 365 UTC days are
retained; older records are removed during database activity. Counts are
approximate: blocked or cleared browser storage, partitioned iframe storage,
multiple devices, opt-outs, automation, and network failures can affect them.

Writes run in the background with a bounded queue and idempotent database
retries. Collection failures do not stop searches. Reports show queued and
dropped events when applicable; a process crash can lose events not yet flushed.
This feature does not collect retroactive usage or change hosting access logs.
