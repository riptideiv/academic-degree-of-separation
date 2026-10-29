# App expansion completion audit

Date: 2026-10-29. Scope: the three-phase implementation in
[the design](app-expansion-design.md) and [the plan](app-expansion-plan.md).
This audit separates implementation evidence from deployment and audience validation.

## Requirements and evidence

| Requirement | Implementation and verification |
| --- | --- |
| One coherent student discovery journey | Discover → shared profile → Saved → Connections; live MIT/paper journey saved James A. Michaelov and a publication, persisted a note, and reopened the original query. |
| Three stacked branches | `codex/expansion-phase-1` → `codex/expansion-phase-2` → `codex/expansion-phase-3`, based on `origin/master`. [PR #15](https://github.com/riptideiv/coauthor-distance/pull/15), [#16](https://github.com/riptideiv/coauthor-distance/pull/16), and [#17](https://github.com/riptideiv/coauthor-distance/pull/17) expose each phase separately. Branch ancestry was checked with `git merge-base --is-ancestor`; all three remain unmerged for review. |
| Direct routes, refresh, genuine 404s | FastAPI serves only known app routes. `tests/test_app_expansion.py` checks every route and its absolute assets, invalid researcher routes, unknown pages, and API 404s. Browser refresh tested on discovery, profile, and Saved. |
| Independent discovery intent | `discovery_state.js` owns up to eight explicit references; graph origins never rerank discovery. `discovery_state.test.cjs` checks independent copies and graph-triggered refresh no-ops. Graph additions left the selected paper unchanged in the browser. |
| Start from a topic, paper, or researcher | `/api/topics`, `/api/works`, `/api/authors` select explicit records; `/api/discover` resolves them. Live topic, paper, and author cases across three institutions are recorded in `discovery-evaluation.md`. |
| Institution-scoped publication evidence | `backend/discovery.py` checks authors' own publication affiliations, reviewed identity scopes, and last-known/reviewed institution evidence. `tests/test_discovery.py` covers foreign coauthors, former affiliations, unavailable references, and scoped identities. |
| Explainable relevance, bounded cost | Rank by matched topics, capped supporting-work count, and recency. Up to eight topic lanes, 15 works per lane, 80 candidates, and 20 results. Coverage messages distinguish sampling/failure from exhaustive results. Citation counts do not determine discovery ranking. |
| Optional connections never filter relevant results | Separate `/api/connections/check` with explicit reference-person selection. Tests distinguish verified, incomplete, failed, and bounded-negative outcomes. Live Chuchu Fan → Sanjit Seshia check found a publication-verified two-hop path while retaining discovery results. |
| Shared researcher profiles | `/api/researchers/A…` and `app_shell.js` serve the same profile from Discover, graph selections/search, and Saved. Browser checks confirmed consistent Saved state and public source links. |
| Affiliation uncertainty and identity integrity | Profiles show all reported last-known institutions, or the reviewed current institution. API tests cover multiple affiliations without claiming current employment, source evidence, and reviewed publication scopes. No contact or recruitment data is inferred. |
| Relevant and recent publications | Discovery context shows matching papers; profiles offer Recent / Most cited. API tests check sort validation and reviewed work scopes; the live selector changed Michaelov's first paper from a 2026 work to “Do Large Language Models Know What Humans Know?” (2023). |
| Existing graph preserved | Original graph/SSE modules remain in place. Regression suites cover search lifecycle, cancellation, identity evidence, graph ownership, settings, exports, and incomplete status. Live Hinton–Bengio graph completed with nine nodes and a one-hop “Deep learning” textual path; coverage remained explicitly limited. |
| Explicit transitions preserve work | Explore connections adds a researcher through the existing graph operation. Find related researchers explicitly adds a discovery reference. Existing graph origins survived navigation and the live second-author addition. |
| One saved list for people and papers | `saved_store.js` and `saved.js` deduplicate by canonical ID; shared controls are available on profiles, discovery results/publications, graph search results, and graph paper details. Browser checks saved a researcher, profile paper, and graph-search paper. |
| Notes and safe removal | Notes save while typing. Reload retained the test note. Removing and undoing a researcher restored notes and discovery context. Store tests also check copy isolation and repeated-save preservation. |
| Storage failures preserve data | Tests cover blocked storage, quota failure, and corrupt original data. An in-memory fallback remains exportable; unreadable original storage is preserved and can be downloaded. Warnings distinguish temporary changes from durable storage. |
| Validated portable import/export | Version 1 JSON, 300 items, 6,000 characters per note, 2 MB limit. Store tests cover malformed JSON, IDs, schema, duplicates, oversized notes, atomic failures, and merge policies. A real browser export was read from disk; import preview showed two duplicates and a note conflict, and Keep both preserved both notes. |
| Sharing excludes private state | `discovery_links.js` permits institution/reference IDs only. Tests reject malformed/oversized links and verify credential/unrelated-field exclusion. Actual copied discovery and profile URLs contained only public IDs; refreshing the discovery link resolved labels and returned results. |
| Back navigation restores context | Query URLs participate in history; up to ten result sets are cached. Request tests cover stale responses and cached history restoration. Browser Back returned to Yu Yao's result, scroll position 2184, with the Open profile button focused. |
| Legacy graph links | Existing `graph_links.js` validation and restore tests remain green. A real root `/#map=…` link opened Connections, restored Hinton and five nodes, and consumed the fragment so later reloads preserve edits. |
| Retry, cancellation, and race safety | Request generation guards and AbortControllers isolate reference, institution, discovery, profile/publication, and optional connection requests. Tests confirm stale/cancelled discovery cannot overwrite current results, failed refresh retains prior results, and API timeouts cancel child work. |
| Responsive, keyboard-accessible interface | Desktop 1440×900 and mobile 375×812 inspected; mobile main content had equal client/scroll width (360 px). Saved filters, visible keyboard focus, modal dismissal on navigation, and history focus restoration tested. Existing graph modal/keyboard regression tests remain green; reduced-motion CSS remains enabled. |
| Updated public product description | README, guide, application metadata/structured feature list, and Reddit draft describe Discover, Connections, Saved, data limits, and local-only persistence. The actual registered hostname is unknown, so canonical and hosting configuration remain unchanged. |

## Validation commands

- `/tmp/researcher-launch-venv/bin/python -m pytest -q`: **359 passed, 3 skipped**.
  Skips are three existing tests for the superseded institution-ranking endpoint.
- `node --test tests/js/*.test.cjs`: **83 passed**.
- `node --check` on every `frontend/*.js`; `git diff --check`.
- Local preview used analytics disabled and remote persistent-store connections disabled.
  Browser console showed no runtime errors during the completed journeys.
- GitHub CI runs the Python suite on 3.12 and 3.13 plus the JavaScript suite and syntax
  checks. The PR check pages are the authoritative status for their published heads.

## Review images

[Saved, desktop](screenshots/expansion-saved-desktop.jpg) ·
[Discover, mobile](screenshots/expansion-discover-mobile.jpg)

## Limits and follow-up work

The live relevance sample is small and depends on OpenAlex's current records.
Affiliation and publication metadata can be wrong or stale, and discovery is bounded.
No student usability study has been conducted; the design's proposed student sessions
remain a product-validation follow-up, not evidence supplied by automated tests.

Saved data has no account or cloud sync. Browser storage is origin-specific; transfer
requires explicit export/import. The registered hostname must be supplied before a
domain migration. Deployment and posting to Reddit are separate actions; this audit
does not claim that the public service has been updated or that a post was published.
