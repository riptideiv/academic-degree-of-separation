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

function harness() {
  const nodes = new Map(), controls = new Map(), timers = new Map();
  const sources = [], messages = [], removedChips = [], addedEdges = [];
  const options = { edges: ['coauthor'], workEdges: ['authorship'] };
  let timerId = 0, layouts = 0, saves = 0;
  const control = id => {
    if (!controls.has(id)) controls.set(id, {
      disabled: false, hidden: false,
      classList: { toggle(name, value) { if (name === 'hidden') control(id).hidden = value; } },
    });
    return controls.get(id);
  };
  const node = id => ({ length: Number(nodes.has(id)), data: key => nodes.get(id)?.[key] });
  const context = vm.createContext({
    API_BASE: '', URLSearchParams,
    document: {
      getElementById: control,
      querySelectorAll: () => [],
      querySelector: selector => ({ remove: () => removedChips.push(selector) }),
    },
    cy: {
      getElementById: node,
      elements: () => ({ remove: () => nodes.clear() }),
      nodes: () => Object.assign([], { remove() {} }),
    },
    setTimeout(fn) { timers.set(++timerId, fn); return timerId; },
    clearTimeout: id => timers.delete(id),
    getEnabledEdges: () => options.edges,
    getEnabledWorkEdges: () => options.workEdges,
    getNeighborhood: () => ({ depth: 2, topK: 6 }),
    addOrUpdateNode: data => nodes.set(data.id, data),
    addEdge: data => addedEdges.push(data),
    renderDegrees() {}, updateOverlays() {},
    saveState() { saves += 1; },
    showProgress: (message, error = false) => messages.push({ message, error }),
    hideProgress: () => messages.push({ hidden: true }),
    runLayout() { layouts += 1; }, runLayoutIncremental() { layouts += 1; },
    rescaleExpansionNodes() {}, applyNameVisibility() {}, applyEdgeFade() {},
    pairKey: (a, b) => [a, b].sort().join('||'),
    EventSource: class {
      constructor(url) { this.url = url; this.listeners = new Map(); sources.push(this); }
      addEventListener(name, fn) { this.listeners.set(name, fn); }
      close() { this.closed = true; }
      emit(name, data = {}) {
        if (name === 'error') this.onerror?.();
        else this.listeners.get(name)?.({ data: typeof data === 'string' ? data : JSON.stringify(data) });
      }
    },
  });
  vm.runInContext([
    section('  const state = {', '\n  // Base per-type'),
    section('  function setLoading(', '\n  // Read the Layout sliders'),
    'this.state = state; this.actions = { startExpansion, rebuildGraph };',
  ].join('\n'), context);
  const existing = id => {
    context.state.origins.add(id);
    nodes.set(id, { id, name: id, type: 'origin' });
  };
  const start = id => {
    context.state.origins.add(id);
    return context.actions.startExpansion(id);
  };
  return { ...context, nodes, timers, sources, messages, removedChips, addedEdges,
    options, control, existing, start, layouts: () => layouts, saves: () => saves };
}

const origin = id => ({ id, name: id, type: 'origin' });
const pathResult = (from, to) => ({ from_id: from, to_id: to, found: true, hops: 1, steps: [] });

test('done closes the stream, clears layout timers and unlocks controls', async () => {
  const h = harness();
  const done = h.start('A');
  assert.equal(h.control('search-btn').disabled, true);
  assert.equal(h.control('stop-search').hidden, false);
  h.sources[0].emit('node', origin('A'));
  assert.equal(h.timers.size, 1);
  h.sources[0].emit('done');
  assert.equal(await done, 'done');
  assert.equal(h.sources[0].closed, true);
  assert.equal(h.timers.size, 0);
  assert.equal(h.state.activeSource, null);
  assert.equal(h.state.cancelExpansion, null);
  assert.equal(h.control('search-btn').disabled, false);
  assert.equal(h.control('stop-search').hidden, true);
  assert.equal(h.layouts(), 1);
});

test('an initial application failure removes the origin that never arrived', async () => {
  const h = harness();
  const done = h.start('A');
  h.sources[0].emit('app_error', { message: 'Upstream unavailable' });
  assert.equal(await done, 'error');
  assert.equal(h.state.origins.has('A'), false);
  assert.equal(h.removedChips.length, 1);
  assert.match(h.messages.at(-1).message, /Upstream unavailable/);
  assert.equal(h.messages.at(-1).error, true);
  assert.equal(h.state.isLoading, false);
});

test('network failure after a node arrives reports an incomplete connection', async () => {
  const h = harness();
  h.existing('A');
  const done = h.start('B');
  h.sources[0].emit('node', origin('B'));
  h.sources[0].emit('error');
  assert.equal(await done, 'error');
  const result = h.state.paths.get('A||B');
  assert.equal(result.search_complete, false);
  assert.equal(result.found, false);
  assert.equal(result.error, 'error');
  assert.match(result.reason, /interrupted/);
  assert.equal(h.nodes.has('B'), true);
  assert.equal(h.timers.size, 0);
});

test('cancelling preserves completed paths and ignores every late stream event', async () => {
  const h = harness();
  h.existing('A');
  h.existing('C');
  const done = h.start('B');
  const stream = h.sources[0];
  stream.emit('node', origin('B'));
  stream.emit('path', pathResult('B', 'A'));
  h.state.cancelExpansion();
  assert.equal(await done, 'cancelled');
  assert.equal(h.state.paths.get('A||B').found, true);
  assert.equal(h.state.paths.get('B||C').error, 'cancelled');
  const snapshot = { nodes: h.nodes.size, paths: h.state.paths.size, messages: h.messages.length, layouts: h.layouts() };
  stream.emit('node', origin('late'));
  stream.emit('edge', { source: 'A', target: 'B' });
  stream.emit('path', pathResult('B', 'C'));
  stream.emit('expansion', { depth: 1, nodes: [origin('late')], edges: [] });
  stream.emit('progress', { message: 'late' });
  stream.emit('app_error', { message: 'late' });
  stream.emit('done');
  stream.emit('error');
  assert.deepEqual({ nodes: h.nodes.size, paths: h.state.paths.size, messages: h.messages.length, layouts: h.layouts() }, snapshot);
  assert.equal(h.addedEdges.length, 0);
  assert.equal(h.timers.size, 0);
  assert.equal(h.state.paths.get('B||C').error, 'cancelled');
});

test('replacing a stream resolves the old promise without disturbing its successor', async () => {
  const h = harness();
  const first = h.start('A');
  h.sources[0].emit('node', origin('A'));
  const second = h.start('B');
  assert.equal(await first, 'cancelled');
  h.sources[0].emit('done');
  h.sources[0].emit('error');
  assert.equal(h.state.activeSource, h.sources[1]);
  assert.equal(h.state.isLoading, true);
  h.sources[1].emit('node', origin('B'));
  h.sources[1].emit('done');
  assert.equal(await second, 'done');
});

test('malformed streamed JSON releases controls and reports an actionable error', async () => {
  const h = harness();
  const done = h.start('A');
  h.sources[0].emit('node', '{');
  assert.equal(await done, 'error');
  assert.match(h.messages.at(-1).message, /Could not read/);
  assert.equal(h.state.isLoading, false);
  assert.equal(h.sources[0].closed, true);
});

test('cancelled rebuilds stop replay and remove unattempted origin roles', async () => {
  for (const discoveredAsNeighbor of [false, true]) {
    const h = harness();
    ['A', 'B', 'C'].forEach(h.existing);
    const rebuilt = h.actions.rebuildGraph();
    h.sources[0].emit('node', origin('A'));
    if (discoveredAsNeighbor) h.sources[0].emit('expansion', {
      depth: 1, nodes: [{ id: 'B', name: 'B', type: 'expansion', expand_owners: ['A'] }], edges: [],
    });
    h.state.cancelExpansion();
    await rebuilt;
    assert.equal(h.sources.length, 1);
    assert.equal(h.saves(), 1);
    assert.equal(h.state.isLoading, false);
    assert.deepEqual([...h.state.origins], ['A']);
    assert.deepEqual([...h.nodes.keys()], discoveredAsNeighbor ? ['A', 'B'] : ['A']);
    if (discoveredAsNeighbor) assert.equal(h.nodes.get('B').type, 'expansion');
    assert.deepEqual(h.removedChips.sort(), [
      '.researcher-chip[data-id="B"]',
      '.researcher-chip[data-id="C"]',
    ]);
  }
});

test('unchecked edge groups send explicit none instead of restoring server defaults', async () => {
  const h = harness();
  h.options.edges = [];
  h.options.workEdges = [];
  const done = h.start('A');
  const params = new URL(h.sources[0].url, 'http://test').searchParams;
  assert.deepEqual(params.getAll('edges'), ['none']);
  assert.deepEqual(params.getAll('work_edges'), ['none']);
  h.sources[0].emit('node', origin('A'));
  h.sources[0].emit('done');
  await done;
});

test('path results retain the edge choices used for their search', async () => {
  const h = harness();
  h.existing('A');
  const done = h.start('B');
  h.options.edges = ['citation'];
  h.options.workEdges = [];
  h.sources[0].emit('node', origin('B'));
  h.sources[0].emit('path', pathResult('A', 'B'));
  const result = h.state.paths.get('A||B');
  assert.deepEqual([...result.edge_types], ['coauthor']);
  assert.deepEqual([...result.work_edge_types], ['authorship']);
  h.sources[0].emit('done');
  await done;
});
