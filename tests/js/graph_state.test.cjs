// Exercise the shipped graph bookkeeping offline. CYTOSCAPE_TEST_RUNTIME may
// point at the pinned browser library for an additional real-Cytoscape run.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../../frontend/app.js'), 'utf8');
function section(start, end) {
  const from = source.indexOf(start);
  assert.notEqual(from, -1, `Missing source section: ${start}`);
  const to = source.indexOf(end, from);
  assert.notEqual(to, -1, `Missing source boundary: ${end}`);
  return source.slice(from, to);
}

function graphDouble() {
  const elements = new Map();
  const collection = items => Object.assign(items, {
    remove() { items.forEach(item => item.remove()); },
  });
  return {
    add({ group = 'nodes', data }) {
      const values = { ...data };
      const element = {
        group, length: 1,
        id: () => values.id,
        data(key, value) {
          if (key === undefined) return values;
          if (typeof key === 'object') Object.assign(values, key);
          else if (arguments.length === 1) return values[key];
          else values[key] = value;
          return this;
        },
        remove() {
          elements.delete(values.id);
          for (const [id, edge] of elements) {
            if (edge.data('source') === values.id || edge.data('target') === values.id) elements.delete(id);
          }
        },
      };
      elements.set(values.id, element);
      return element;
    },
    getElementById: id => elements.get(id) || { length: 0 },
    elements: () => collection([...elements.values()]),
    nodes(selector) {
      const type = selector?.match(/type="([^"]+)"/)?.[1];
      return collection([...elements.values()].filter(e => e.group === 'nodes' && (!type || e.data('type') === type)));
    },
  };
}

function harness() {
  let cy = graphDouble();
  const sources = [];
  const context = vm.createContext({
    cy, API_BASE: '', URLSearchParams, setTimeout, clearTimeout,
    document: { querySelector: () => ({ remove() {} }) },
    seedPosition: () => ({ x: 0, y: 0 }),
    renderDegrees() {}, clearSavedState() {}, saveState() {}, runInstitutionRank() {},
    runLayout() {}, runLayoutIncremental() {}, showProgress() {}, hideProgress() {},
    rescaleExpansionNodes() {}, applyNameVisibility() {}, applyEdgeFade() {},
    getEnabledEdges: () => ['coauthor'], getEnabledWorkEdges: () => ['authorship'],
    getNeighborhood: () => ({ depth: 2, topK: 6 }),
    EventSource: class {
      constructor(url) { this.url = url; this.listeners = new Map(); sources.push(this); }
      addEventListener(name, handler) { this.listeners.set(name, handler); }
      close() { this.closed = true; }
      emit(name) { this.listeners.get(name)({ data: '{}' }); }
    },
  });
  if (process.env.CYTOSCAPE_TEST_RUNTIME) {
    // Cytoscape checks plain-object constructors, so load it in the same realm
    // as app.js rather than passing data objects across VM contexts.
    vm.runInContext(fs.readFileSync(process.env.CYTOSCAPE_TEST_RUNTIME, 'utf8'), context);
    cy = context.cy = vm.runInContext('cytoscape({ headless: true })', context);
  }
  vm.runInContext([
    section('  const state = {', '\n  // Base per-type'),
    section('  function removeResearcher(', '\n  // ── Graph helpers'),
    section('  function addOrUpdateNode(', '\n  function addEdge('),
    section('  function startExpansion(', '\n  // Re-run the whole search'),
    'function setLoading(value) { state.isLoading = value; }',
    'this.state = state; this.actions = { addOrUpdateNode, removeResearcher, startExpansion };',
  ].join('\n'), context);
  const add = (id, type, extra = {}) => {
    if (type === 'origin') context.state.origins.add(id);
    context.incoming = JSON.stringify({ id, name: id, type, ...extra });
    vm.runInContext('actions.addOrUpdateNode(JSON.parse(incoming))', context);
  };
  const edge = (id, from, to) => {
    context.incoming = JSON.stringify({ group: 'edges', data: { id, source: from, target: to } });
    vm.runInContext('cy.add(JSON.parse(incoming))', context);
  };
  const pair = (from, to) => {
    const key = [from, to].sort().join('||');
    context.state.paths.set(key, { from_id: from, to_id: to, found: true });
    return key;
  };
  return { ...context, sources, add, edge, pair, node: id => cy.getElementById(id) };
}

test('promoting an expansion to a path updates membership and preserves every pair', () => {
  const h = harness();
  h.add('X', 'expansion', { expand_owners: ['A'] });
  h.add('X', 'path', { path_pair: 'A||B' });
  h.add('X', 'path', { path_pair: 'A||C' });
  h.add('X', 'path', { path_pair: 'A||B' });
  assert.deepEqual([...h.state.pathNodes], ['X']);
  assert.deepEqual([...h.node('X').data('pathPairs')], ['A||B', 'A||C']);
  // A later neighborhood event must not demote the node or its cached record.
  h.add('X', 'expansion', { expand_owners: ['B'] });
  assert.equal(h.node('X').data('type'), 'path');
  assert.equal(h.state.authorCache.get('X').type, 'path');
  assert.deepEqual([...h.node('X').data('expandOwners')], ['A', 'B']);
});

test('sequential removals prune old pair keys and orphaned bridge neighborhoods', () => {
  const h = harness();
  ['A', 'B', 'C'].forEach(id => h.add(id, 'origin'));
  h.add('X', 'path', { path_pair: h.pair('A', 'B') });
  h.add('X', 'path', { path_pair: h.pair('A', 'C') });
  h.add('N', 'expansion', { expand_owners: ['X'] });
  h.actions.removeResearcher('B');
  assert.deepEqual([...h.node('X').data('pathPairs')], ['A||C']);
  assert.equal(h.node('N').length, 1);
  h.actions.removeResearcher('C');
  assert.equal(h.node('X').length, 0);
  assert.equal(h.node('N').length, 0);
  assert.equal(h.state.pathNodes.size, 0);
  assert.deepEqual([...h.state.authorCache.keys()], ['A']);
});

test('removing an origin preserves it as an intermediate on a surviving connection', () => {
  const h = harness();
  ['A', 'B'].forEach(id => h.add(id, 'origin'));
  const survivingPair = h.pair('A', 'B');
  h.add('X', 'path', { path_pair: survivingPair });
  h.add('X', 'origin');
  assert.equal(h.state.pathNodes.has('X'), false);
  h.pair('A', 'X');
  h.add('N', 'expansion', { expand_owners: ['X'] });
  h.edge('AX', 'A', 'X');
  h.edge('XB', 'X', 'B');
  h.actions.removeResearcher('X');
  assert.equal(h.node('X').data('type'), 'path');
  assert.equal(h.state.origins.has('X'), false);
  assert.equal(h.state.pathNodes.has('X'), true);
  assert.deepEqual([...h.state.paths.keys()], [survivingPair]);
  assert.equal(h.node('AX').length, 1);
  assert.equal(h.node('XB').length, 1);
  assert.equal(h.node('N').length, 1);
});

test('a path passing through an existing origin keeps its origin role', () => {
  const h = harness();
  ['A', 'B', 'X'].forEach(id => h.add(id, 'origin'));
  h.add('X', 'path', { path_pair: h.pair('A', 'B') });
  assert.equal(h.node('X').data('type'), 'origin');
  assert.equal(h.state.pathNodes.has('X'), false);
  assert.equal(h.state.authorCache.get('X').type, 'origin');
  h.actions.removeResearcher('X');
  assert.equal(h.node('X').data('type'), 'path');
  assert.equal(h.state.pathNodes.has('X'), true);
});

test('shared expansion ownership survives repeated stream phases and removals', () => {
  const h = harness();
  ['A', 'B', 'C'].forEach(id => h.add(id, 'origin'));
  h.add('N', 'expansion', { expand_owners: ['A'] });
  h.add('N', 'expansion', { expand_owners: ['B', 'A'] });
  assert.deepEqual([...h.node('N').data('expandOwners')], ['A', 'B']);
  h.actions.removeResearcher('A');
  assert.equal(h.node('N').length, 1);
  assert.deepEqual([...h.node('N').data('expandOwners')], ['B']);
  h.actions.removeResearcher('B');
  assert.equal(h.node('N').length, 0);
  assert.equal(h.state.authorCache.has('N'), false);
});

test('removing the last origin releases all graph state and connected edges', () => {
  const h = harness();
  h.add('A', 'origin');
  h.add('N', 'expansion', { expand_owners: ['A'] });
  h.edge('AN', 'A', 'N');
  h.actions.removeResearcher('A');
  assert.equal(h.cy.elements().length, 0);
  assert.equal(h.state.origins.size, 0);
  assert.equal(h.state.pathNodes.size, 0);
  assert.equal(h.state.paths.size, 0);
  assert.equal(h.state.authorCache.size, 0);
});

test('the next stream retains promoted bridges and releases old neighborhood cache entries', async () => {
  const h = harness();
  ['A', 'B', 'C'].forEach(id => h.add(id, 'origin'));
  h.add('X', 'expansion', { expand_owners: ['A'] });
  h.add('X', 'path', { path_pair: h.pair('A', 'B') });
  h.add('N', 'expansion', { expand_owners: ['A'] });
  const done = h.actions.startExpansion('C');
  const params = new URL(h.sources[0].url, 'http://test').searchParams;
  assert.equal(params.get('path_ids'), 'X');
  assert.equal(params.get('origin_ids'), 'A,B');
  assert.equal(h.node('X').length, 1);
  assert.equal(h.node('N').length, 0);
  assert.equal(h.state.authorCache.has('N'), false);
  h.sources[0].emit('done');
  await done;
  assert.equal(h.state.isLoading, false);
  assert.equal(h.state.activeSource, null);
});
