# App expansion implementation plan

Date: 2026-10-29
Design: [app-expansion-design.md](app-expansion-design.md)
Status: in progress

## Branch stack

1. `codex/expansion-phase-1` from `origin/master`: navigation, independent discovery references, shared researcher profiles, graph integration, and legacy-link compatibility.
2. `codex/expansion-phase-2` from phase 1: topic and paper inputs, institution-scoped publication-backed discovery, optional connection checks, relevance evaluation.
3. `codex/expansion-phase-3` from phase 2: saved researchers/papers, notes, safe import/export, sharing, documentation, and complete journey validation.

Each branch receives its own commits and stacked pull request. Do not squash or merge the lower branches while the stack is under construction. Preserve the existing graph backend and its verified-path endpoint.

## Implementation and verification gates

### Phase 1

- [x] Application routes and navigation preserve browser history and graph state.
- [x] Discovery references are explicit and independent of graph origins.
- [x] Researcher profiles have a common endpoint and reusable UI.
- [x] Profile/graph/discovery actions preserve the user's existing work.
- [x] Legacy graph fragments and directly opened routes work.
- [x] Route, profile, and state-boundary tests pass; desktop/mobile rendered UI checked.

### Phase 2

- [x] Explicit topic selection and selected papers work without an existing graph.
- [x] Discovery produces institution-associated people with publication evidence.
- [x] Connection checking is optional and cannot remove relevant results.
- [x] Input validation, stale-response handling, cancellation, and bounded work are tested.
- [x] Live topic, paper, and author queries evaluated across different fields/institutions.

### Phase 3

- [ ] One local shortlist supports researchers and papers, deduplication, and notes.
- [ ] Storage failure, malformed imports, duplicate note conflicts, and exports handled safely.
- [ ] Sharing excludes personal state and restores public discovery inputs.
- [ ] Guide and public metadata reflect the finished product.
- [ ] Full backend/JS suites and desktop/mobile journey checks pass.
- [ ] Final requirement-by-requirement audit recorded with concrete evidence.

## Constraints

Keep the existing restrained design, FastAPI, vanilla JavaScript, and Cytoscape.
Use named modules for new responsibilities rather than expanding the graph IIFE.
Keep user-owned `.agents/` and `.archon/` untracked. Do not change hosting or
canonical domains until the actual registered hostname is known. Local preview
must disable analytics and remote persistent-store connections.

## Phase 1 evidence

339 pytest tests passed (3 optional skips); 68 JavaScript tests passed.
Live local checks covered researcher search, a directly opened Hinton profile,
publication loading, profile-to-graph navigation, graph completion, and returning
to the unchanged reference selection. Narrow-mobile and desktop layouts inspected.
The Saved destination is an intermediate shell until phase 3.

## Phase 2 evidence

356 pytest tests passed (3 optional skips); 71 JavaScript tests passed.
See [discovery-evaluation.md](discovery-evaluation.md) for live examples, observed
limitations, and fixes. Browser checks covered topic selection, university selection,
publication-backed results, optional author selection, and a verified two-hop path.
