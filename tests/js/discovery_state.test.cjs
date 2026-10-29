const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { create } = require('../../frontend/discovery_state.js');

test('draft edits and external graph mutations do not change submitted discovery intent', () => {
  const graphAuthor = { id: 'A1', display_name: 'One' };
  const state = create();
  state.add(graphAuthor);
  const submitted = state.submit({ id: 'I1', display_name: 'University' });
  graphAuthor.id = 'A2';
  state.add({ id: 'A3', display_name: 'Three' });
  state.remove('A1');
  submitted.references[0].id = 'A4';
  assert.equal(state.submitted().references[0].id, 'A1');
  assert.deepEqual(state.list(), [{ id: 'A3', display_name: 'Three' }]);
});

test('references are bounded, deduplicated, and exclude unrelated data', () => {
  const state = create([{ id: 'bad', display_name: 'Invalid' }]);
  assert.throws(() => state.submit({ id: 'I1' }));
  state.add({ id: 'A1', display_name: ' One ', api_key: 'secret' });
  state.add({ id: 'A1', display_name: 'One' });
  assert.deepEqual(state.list(), [{ id: 'A1', display_name: 'One' }]);
  for (let i = 2; i <= 8; i++) state.add({ id: 'A' + i, display_name: 'Person' });
  assert.throws(() => state.add({ id: 'A9', display_name: 'Person' }));
  assert.throws(() => state.submit({ id: 'not-an-institution' }));
});

test('graph-triggered refreshes cannot rerun discovery and explicit searches use only selected references', async () => {
  const source = fs.readFileSync(require.resolve('../../frontend/app.js'), 'utf8');
  const start = source.indexOf('  async function runInstitutionRank(');
  const end = source.indexOf('\n  function setExplorerLoading(', start);
  const requests = [];
  const context = vm.createContext({
    ResearchApp: { references: () => [{ id: 'A2', display_name: 'Reference' }] },
    state: { isLoading: false, origins: new Set(['A1']) },
    rankSelection: { institution: { id: 'I1', display_name: 'University' } },
    explorerUpdating: false, explorerRefreshPending: false, suggestionRequestId: 0,
    setRankStatus() {}, renderRankResults() {}, setExplorerLoading() {},
    URLSearchParams, AbortController, API_BASE: '', RANK_TIMEOUT_MS: 30000,
    setTimeout() {}, clearTimeout() {}, explorerCoverageNote: () => '', hasCompletePathEvidence: () => true,
    fetch: async url => { requests.push(url); return { ok: true, json: async () => ({ results: [] }) }; },
  });
  vm.runInContext(source.slice(start, end) + '\nthis.run = runInstitutionRank;', context);
  await context.run();
  assert.equal(requests.length, 0);
  await context.run(true);
  assert.deepEqual(new URL(requests[0], 'http://test').searchParams.getAll('origin_ids'), ['A2']);
  context.state.origins.add('A3');
  await context.run();
  assert.equal(requests.length, 1);
});
