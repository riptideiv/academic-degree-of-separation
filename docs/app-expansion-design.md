# Coauthor Distance: app expansion design

Date: 2026-10-29  
Status: implemented across three stacked branches; see [completion audit](app-expansion-audit.md)

Scope: evolve the existing author graph and campus explorer into one research discovery app

## 1. Product direction

**Find people whose work interests you, then understand how they connect.**

Coauthor Distance should help someone move from an interest, paper, or familiar
researcher to unfamiliar researchers worth investigating. Campus discovery answers
“Who at my university works on this?” Connections answers “How does this person
connect to the research I already know?” Saved gives the user somewhere to keep
what they learned.

The expansion is complete when those activities form a useful journey. The number
of features, pages, or visualizations is not a measure of completeness. A dedicated
domain does not require a larger feature set.

### Primary audience

Students exploring research opportunities and early-career researchers getting
oriented in a field. Prioritize people who know an interest and a university but
may not know which researchers to search for.

Science-curious visitors remain a secondary audience. They can go directly to
Connections and try the existing example without configuring discovery.

### Core user outcome

> I arrived knowing my university and an interest. I left with a researcher worth
> reading about, a relevant publication, and a concrete reason to investigate them.

For example, a student selects their university and a robotics topic, finds a
researcher through a relevant paper, and saves both with a note. They can then
open that researcher's network or check a connection to a professor they already
know. Returning to the shortlist completes the same journey; it does not require
starting a new graph search.

This is research discovery. The app does not establish whether a researcher is
recruiting, available to collaborate, or able to provide an introduction.

## 2. Current behavior and the gap

The existing implementation already provides:

- Researcher and paper search, an interactive graph, and bounded connection searches.
- Publication evidence for coauthor paths and explicit incomplete-search states.
- Institution suggestions using a bounded, topic-balanced candidate pool.
- Researcher details, representative publications, and affiliation evidence.
- Graph persistence, shared starting-point links, and image/report exports.

The campus explorer currently obtains its reference authors from `state.origins`
in `frontend/app.js`. It asks the user to add a researcher when that set is empty.
Adding a suggested researcher to the graph changes those origins and can change
subsequent campus suggestions. The explorer also filters out results without
complete publication evidence for a coauthor path.

Those choices suit a tool for finding connected people, but constrain broader
discovery. Someone with only a subject interest cannot begin. Someone relevant to
that interest can disappear because the bounded path search did not establish a
connection. Exploring a suggestion can silently change the discovery question.

The central design change is to separate **discovery intent**, **graph exploration**,
and **saved research** while providing explicit actions between them.

## 3. Information architecture

| Destination | Main task | Main presentation |
| --- | --- | --- |
| Discover | Find researchers at a university using interests, papers, or reference authors | Search inputs and a readable result list |
| Connections | Explore networks and inspect paths between researchers | Graph with an equivalent textual path view |
| Saved | Revisit shortlisted researchers and papers with personal notes | Simple list with researcher/paper filters |

Discover is the default destination for new visitors. Connections remains directly
accessible, including its example. About and the guide are secondary navigation.

“Explore your campus” becomes an entry into Discover with the institution selector
focused or prefilled. It is no longer a floating tool whose usefulness depends on
the current graph.

### Proposed routes

- `/discover`: university selection, reference inputs, and results.
- `/connections`: the existing graph workspace.
- `/saved`: the local shortlist.
- `/researchers/:id`: a directly addressable researcher profile.
- `/guide/`: explanation and data limitations.

The root opens Discover. Existing root-level shared graph fragments must still
open Connections and restore their original inputs. Direct navigation, refresh,
and browser Back must work on every route. FastAPI must serve the application
shell for known application routes while preserving API routes, static assets,
the guide, and genuine 404 responses.

## 4. Core journeys

### A. Start with a university and an interest

1. Select a university.
2. Enter a research interest and select a matching topic. Topic selection is
   explicit; the app must not silently turn an ambiguous phrase into a subject.
3. Review researchers with matching topics and supporting publications.
4. Open a profile, inspect the relevant work, and save the researcher with a note.
5. Optionally open their network in Connections.

An institution alone shows a prompt to add an interest, paper, or reference author.
Version one does not substitute a list of the university's most-cited researchers.

### B. Start with a paper or researcher

1. Select a paper by title or a researcher by name, confirming the correct record.
2. Choose a university to search within.
3. Review matching researchers and the evidence for their relevance.
4. Request a connection to a selected reference author when useful.

A paper can contribute topic evidence without making all of its authors graph
origins. When a path requires a person as an endpoint, offer the paper's authors
for explicit selection.

### C. Start with the graph

1. Open Connections and choose researchers or use the example.
2. Inspect a person and their publication evidence.
3. Select “Find related researchers” to add that person as a Discover reference.
4. Choose a university, inspect results, and save useful discoveries.

### D. Return to a shortlist

1. Open Saved and select a researcher or paper.
2. Revisit the profile, source publication, or recorded discovery context.
3. Edit a note, explore a connection, or remove the item.

Version one has one shortlist. Multiple projects, folders, and collaborative lists
are deferred until actual use demonstrates a need.

## 5. Discover

### Inputs and state

A discovery query contains an institution and one or more selected topics, papers,
or reference authors. Start with a small bounded selection; choose exact limits
after measuring API cost and result quality.

Selected references remain visible and editable above the results. University and
reference changes are applied through a clear Search action. Opening profiles,
saving people, and adding graph nodes do not modify the query or rerank results.

The first release requires an institution. Searching across all institutions is a
later extension, not another initial mode to design and validate.

### Results

Each result should provide enough evidence to decide whether to open it:

- Name and reported affiliation, with current-affiliation status when supported.
- Matching research topics.
- One or two relevant publications, with year and source links.
- A short factual reason for the match, such as a selected topic appearing in a
  listed publication.
- Open profile and Save actions.

Evidence replaces generic descriptions. Avoid prominent citation counts, ordinal
“best researcher” rankings, and opaque percentage-match badges. Missing topical
evidence must remain missing rather than being inferred from prestige or proximity.

### Relevance and connection are separate

Research relevance determines eligibility for discovery results. Coauthor distance
is optional additional evidence, calculated for selected people rather than used
as a requirement for the whole list.

Distinguish these connection states in the data and UI:

- Not checked.
- Checking.
- Verified path found, with supporting publications.
- No path found within the search limits.
- Search incomplete or failed.

A failed or incomplete connection search must not remove an otherwise relevant
researcher. For a topic-only query, the app cannot show a distance until the user
chooses a reference person. Connection settings on the graph do not change
discovery relevance.

### Retrieval and ranking approach

Reuse the existing institution resolution, topic metadata, candidate balancing,
and affiliation evidence where their semantics fit. Retrieve a bounded candidate
set using the selected institution and reference topics or publications. Deduplicate
by OpenAlex author ID, then rank using explainable topical and publication evidence.

Keep candidate retrieval, relevance ranking, and optional path enrichment as
separate steps. Broad topic matches should not outweigh more specific publication
evidence solely because an author has more citations. Exact ranking weights remain
an implementation experiment and must be evaluated against example queries.

OpenAlex exposes author topics and institutional affiliations, which provides a
foundation for this design. Field availability does not establish recommendation
quality. Last-known affiliation is also not independently verified employment.
See the [OpenAlex author documentation](https://help.openalex.org/data/authors/).

## 6. Shared researcher profiles

Use one researcher profile component and data model across search results,
Discover, Connections, and Saved. A profile opened from a list may appear in a
panel on desktop and a full page on mobile. Its URL remains shareable; Back returns
to the prior query, scroll position, or graph context.

The profile contains:

- Identity: name, OpenAlex ID, ORCID when available, and source links.
- Affiliation: reported institution, evidence status, and source where available.
- Research: topics and relevant publications, with recent work available separately.
- Discovery context: why this person matches the current query, when applicable.
- Connection evidence: optional paths and the publications supporting each hop.
- Actions: Save, Explore connections, and Find related researchers.

A standalone profile must make sense without a discovery query. Contextual match
evidence belongs to the query, not to the person's permanent profile. Changing the
query must not overwrite the underlying author identity or unrelated saved notes.

Do not infer job titles, recruitment status, contact details, or verified university
pages from an author name. Link to known sources and represent unavailable fields
honestly.

## 7. Connections

Preserve the existing graph's useful behavior: researcher/paper inputs, coauthor
defaults, optional edge types, cancellation, bounded-search feedback, publication
evidence, and image/report exports.

Use explicit actions to cross destinations:

- “Explore connections” opens a researcher's graph or adds them to the existing
  workspace without clearing unrelated work.
- “Show connection” carries the selected reference and result into a focused path
  view; replacing an existing workspace must be an explicit choice.
- “Find related researchers” adds an author to Discover's visible reference inputs.
- Save works on people and papers from graph details as well as discovery results.

The graph and its textual path view share the same evidence and search-status
semantics. Graph layouts remain exploratory; visual proximity is not another
research-relevance score.

## 8. Saved and persistence

Save researchers and papers by their canonical OpenAlex IDs. An item stores its
type, display label, saved timestamp, optional note, and optional discovery context.
Deduplicate repeated saves and preserve an existing note.

Use versioned browser storage with an in-memory fallback when storage is blocked
or full. Clearly state when changes cannot persist. The initial list is private to
this browser, has no account requirement, and does not synchronize between devices.

Support explicit JSON export/import for portability, including a clear indication
that exported notes are included. Validate imported types, IDs, lengths, and schema
versions; merge duplicate IDs without silently overwriting conflicting notes.

Keep separate state for:

| State | Contains | Changes when |
| --- | --- | --- |
| Discovery query | Institution and selected references | User explicitly edits and submits inputs |
| Connection workspace | Graph origins, elements, paths, and settings | User explores or edits the graph |
| Saved items | People, papers, notes, and optional context | User saves, edits, imports, or removes items |
| Profile cache | Source records and retrieval timestamps | Data is fetched or refreshed |

Existing graph storage and the home institution preference need compatible loading.
Do not silently turn every existing graph node into a discovery interest. Offer an
explicit action to use current graph origins as references.

Browser storage is scoped to a website origin. Moving from Render to the registered
domain will not automatically carry saved state across; retain the old-host export
path and document import on the new domain. Never copy API credentials into exports.

## 9. Sharing, privacy, and failure behavior

Profile links carry a public author ID. Discovery links carry validated institution
and reference IDs, and rebuild results against current data. Existing graph links
continue to rebuild their starting points and options. Shared results are not
promised to be frozen snapshots.

Personal notes, saved lists, API credentials, and visitor identifiers are excluded
from ordinary share links. Sharing a shortlist or notes would require a separate,
explicit export or future sharing action.

Discovery must distinguish no matching candidates, unavailable metadata, upstream
errors, and an exhausted search budget. Preserve current inputs and useful results
when a retry is possible. Cancel or ignore stale responses after query changes.
The profile remains useful if connection enrichment fails.

If extending existing analytics, record only coarse action counts under the
existing privacy controls. Do not send search text, reference IDs, universities,
notes, or saved-list contents as analytics properties.

## 10. Visual and interaction direction

Continue the restrained interface established in the cleanup:

- Simple, persistent navigation between Discover, Connections, and Saved.
- Visible input labels, a clear primary action, and readable result lists.
- Actual topics, publications, and evidence in place of repetitive helper prose.
- A consistent researcher profile instead of different cards for every surface.
- Details disclosed beside the relevant control or in the guide.
- Responsive layouts with no essential hover-only actions.

Avoid a dashboard of decorative metrics or feature cards. Discover should feel
like a useful search page; Connections should devote its space to exploration.
Saved should remain a list rather than a project-management product.

Navigation must preserve context, support keyboard use and visible focus, and
announce loading and errors without overwhelming live regions. Panel focus must
return to the opener on close. The graph's essential evidence remains readable
without manipulating the canvas.

## 11. Technical approach

Retain FastAPI, plain HTML/CSS/JavaScript, and Cytoscape for the initial expansion.
Route support and shared state do not themselves require a frontend framework.
Extract responsibilities from `frontend/app.js` as their new boundaries become
clear rather than combining the expansion with a wholesale rewrite.

Suggested frontend boundaries are routing, discovery inputs/results, researcher
profiles, saved-item storage, and the existing connection workspace. Share source
records through normalized entity IDs; keep query-specific evidence separate.

On the backend, extract reusable discovery operations from the large
`/api/institution-suggestions` handler. Preserve that endpoint's current verified-path
contract while existing clients depend on it. A separate discovery endpoint should
return relevant researchers without requiring a path. A profile endpoint can
provide a consistent researcher record and publication data.

Proposed discovery responses should include the normalized query, candidate results,
match evidence, affiliation evidence, and bounded-search coverage. Optional
connection results use their own status and evidence fields. Define and validate
the exact request/response schemas during implementation before wiring the UI.

Use bounded candidate counts, concurrency limits, caching, and request cancellation.
Fetch lightweight discovery results before expensive per-person path searches.
Measure upstream request count and latency before selecting budgets; do not promise
an unmeasured response-time target or run a graph search for every result by default.

## 12. Delivery phases

### Phase 1: separate the workflows

- Add destination navigation and routes with browser-history support.
- Move the existing campus experience into Discover.
- Introduce discovery references independent of graph state.
- Extract the shared researcher profile and explicit cross-destination actions.
- Preserve current graph behavior, stored state, and legacy share links.

This is an intermediate foundation. Discovery still requiring reference authors
does not yet satisfy the expansion's primary user outcome.

### Phase 2: enable independent discovery

- Add explicit topic selection and paper-based reference inputs.
- Implement institution-scoped relevance retrieval with publication evidence.
- Separate optional path lookup from result eligibility.
- Handle uncertainty, partial results, retries, and stale responses consistently.
- Evaluate relevance across several institutions and research fields.

### Phase 3: complete the return journey

- Add the local shortlist, notes, and validated export/import.
- Complete profile/discovery sharing and cross-destination context restoration.
- Verify desktop/mobile workflows, keyboard use, and legacy-link compatibility.
- Update the guide and public copy to describe the finished experience.

The first complete release includes all three phases. Domain setup is a separate
deployment task; the registered hostname and hosting configuration must be confirmed
before changing canonical URLs or redirects.

## 13. Acceptance and evaluation

### Functional acceptance

- A university-plus-topic query can return evidence-backed discoveries without
  creating a graph or selecting an author first.
- A relevant researcher stays visible when no coauthor path is found.
- Adding or removing graph nodes does not alter discovery inputs or results.
- The same author opens the same profile and saved state from every destination.
- Back navigation restores the originating query/list or graph context.
- Direct profile links, new discovery links, and existing graph links work on refresh.
- Notes survive reload where storage is available, and are absent from share links.
- Storage failures and invalid imports do not erase existing saved data.
- Existing graph search, cancellation, evidence, and exports continue to work.

### Quality evaluation

Build a small, explicitly reviewed set of institution/topic/paper queries spanning
different fields. Inspect whether suggested people have supporting publications,
whether identity and affiliation labels are accurate, and whether results are
dominated by famous researchers. Document limitations instead of treating this
small evaluation as a universal recommendation-quality benchmark.

Ask a small group of students to complete the core journey without coaching. Record
whether they find someone worth investigating, can explain why the person appeared,
and can distinguish a topic match from a verified coauthor connection. Saving an
item is a useful behavior signal but does not alone prove that results are useful.

Test new state boundaries, routing/history, response ordering, evidence semantics,
storage failure, and import validation. Reuse the existing graph and backend tests
for regression coverage; avoid tests that merely repeat interface copy.

## 14. Out of scope and open decisions

Out of scope for the first expansion: accounts, cloud synchronization, messaging,
automated outreach, recruitment claims, AI chat, generated researcher biographies,
full literature management, global university rankings, collaboration workspaces,
and automatic searches across every institution.

Resolve these during implementation or evaluation:

1. How topic search handles ambiguous phrases and fields with weak coverage.
2. Which publication evidence best explains a match without over-weighting volume.
3. The candidate and request budgets that provide useful coverage at acceptable cost.
4. How much recent-work filtering improves results compared with broader coverage.
5. The exact registered hostname and migration timing for existing browser state.

These questions refine the design. They do not change its core boundary: Discover
finds relevant people, Connections explains networks, and Saved preserves useful
discoveries.
