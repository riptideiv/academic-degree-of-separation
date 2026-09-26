const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');

const source = fs.readFileSync(path.join(__dirname, '../../frontend/usage_analytics.js'), 'utf8');
const flush = () => new Promise(resolve => setImmediate(resolve));

function harness(navigator = {}, options = {}) {
  const requests = [], timers = new Map();
  const saved = options.storage ?? new Map();
  const storageAccess = [];
  let timerId = 0;
  const context = vm.createContext({
    navigator, AbortController, crypto: webcrypto,
    setTimeout(fn) { timers.set(++timerId, fn); return timerId; },
    clearTimeout: id => timers.delete(id),
    fetch(url, init) {
      return new Promise((resolve, reject) => requests.push({
        url, init, reject, respond: status => resolve({ status }),
      }));
    },
    ...options,
  });
  context.window = context;
  Object.defineProperty(context, 'localStorage', { get() {
    storageAccess.push('access');
    if (options.blockedStorage) throw new Error('Browser storage is blocked');
    return {
      getItem: key => saved.get(key) ?? null,
      setItem: (key, value) => saved.set(key, value),
    };
  } });
  Object.defineProperty(context, 'sessionStorage', { get() { throw new Error('Storage must not be read'); } });
  vm.runInContext(source, context);
  return { requests, timers, context, saved, storageAccess, track: context.UsageAnalytics.track };
}

test('page view precedes actions and payloads contain only event names and one random identifier', async () => {
  const h = harness({}, { RESEARCHER_API_BASE: 'https://api.example.test' });
  h.track('author_search');
  h.track('work_search');
  assert.equal(h.requests.length, 1);
  const visitorId = JSON.parse(h.requests[0].init.body).visitor_id;
  assert.match(visitorId, /^[0-9a-f]{32}$/);
  assert.deepEqual(JSON.parse(h.requests[0].init.body), { event: 'page_view', visitor_id: visitorId });
  h.requests[0].respond(202);
  await flush();
  assert.equal(h.requests.length, 2);
  h.requests[1].respond(202);
  await flush();
  assert.equal(h.requests.length, 3);
  assert.deepEqual(h.requests.map(request => JSON.parse(request.init.body).event), ['page_view', 'author_search', 'work_search']);
  for (const request of h.requests) {
    assert.equal(request.url, 'https://api.example.test/api/analytics/events');
    assert.deepEqual(Object.keys(request.init.headers), ['Content-Type']);
    assert.equal(request.init.credentials, 'omit');
    assert.equal(request.init.referrerPolicy, 'no-referrer');
    assert.equal(request.init.cache, 'no-store');
    assert.equal(request.init.keepalive, true);
    assert.deepEqual(Object.keys(JSON.parse(request.init.body)), ['event', 'visitor_id']);
    assert.equal(JSON.parse(request.init.body).visitor_id, visitorId);
  }
  h.requests[2].respond(202);
  await flush();
  assert.equal(h.timers.size, 0);
});

test('privacy preferences prevent even the initial request', () => {
  for (const navigator of [{ doNotTrack: '1' }, { doNotTrack: 'yes' }, { globalPrivacyControl: true }]) {
    const h = harness(navigator);
    h.track('author_search');
    assert.equal(h.requests.length, 0);
    assert.equal(h.storageAccess.length, 0);
  }
  assert.equal(harness({}, { doNotTrack: '1' }).requests.length, 0);
});

test('a valid saved browser identifier is reused across page loads', async () => {
  const storage = new Map();
  const first = harness({}, { storage });
  const visitorId = JSON.parse(first.requests[0].init.body).visitor_id;
  assert.equal(storage.get('academiaUsageVisitorV1'), visitorId);
  const second = harness({}, { storage });
  assert.equal(JSON.parse(second.requests[0].init.body).visitor_id, visitorId);
  first.requests[0].respond(202);
  second.requests[0].respond(202);
  await flush();
});

test('invalid stored identifiers are replaced rather than sent to analytics', async () => {
  for (const invalid of ['person@example.com', 'A'.repeat(32), '', '0'.repeat(33)]) {
    const storage = new Map([['academiaUsageVisitorV1', invalid]]);
    const h = harness({}, { storage });
    const visitorId = JSON.parse(h.requests[0].init.body).visitor_id;
    assert.match(visitorId, /^[0-9a-f]{32}$/);
    assert.notEqual(visitorId, invalid);
    assert.equal(storage.get('academiaUsageVisitorV1'), visitorId);
    h.requests[0].respond(202);
    await flush();
  }
});

test('blocked local storage keeps one in-memory visitor identifier throughout a page', async () => {
  const h = harness({}, { blockedStorage: true });
  h.track('author_search');
  h.requests[0].respond(202);
  await flush();
  assert.equal(h.requests.length, 2);
  assert.equal(JSON.parse(h.requests[0].init.body).visitor_id, JSON.parse(h.requests[1].init.body).visitor_id);
  h.requests[1].respond(202);
  await flush();
  assert.equal(h.timers.size, 0);
});

test('only allowed primitive names are accepted and a page view cannot be duplicated', async () => {
  const h = harness();
  [undefined, null, {}, { event: 'author_search', query: 'Private name' }, 'Private name', 'page_view'].forEach(h.track);
  h.requests[0].respond(202);
  await flush();
  assert.equal(h.requests.length, 1);
});

test('a disabled, failed or unavailable tracker stops without retries or rejected promises', async () => {
  for (const response of [204, 403, 429, 500, 'network']) {
    const h = harness();
    h.track('graph_run');
    if (response === 'network') h.requests[0].reject(new Error('offline'));
    else h.requests[0].respond(response);
    await flush();
    assert.doesNotThrow(() => h.track('author_search'));
    assert.equal(h.requests.length, 1);
    assert.equal(h.timers.size, 0);
  }
});

test('an ignored network abort cannot leave a growing or hung queue', async () => {
  const h = harness();
  h.track('author_search');
  [...h.timers.values()][0]();
  await flush();
  assert.equal(h.requests[0].init.signal.aborted, true);
  h.track('work_search');
  h.requests[0].respond(202);
  await flush();
  assert.equal(h.requests.length, 1);
  assert.equal(h.timers.size, 0);
});

test('the pending event queue has a fixed bound', async () => {
  const h = harness();
  for (let i = 0; i < 1000; i++) h.track('graph_run');
  for (let i = 0; i < 33; i++) {
    h.requests[i].respond(202);
    await flush();
  }
  assert.equal(h.requests.length, 33); // Initial page view plus at most 32 queued actions.
  assert.equal(h.timers.size, 0);
});

test('a changed privacy preference discards pending actions', async () => {
  const navigator = {};
  const h = harness(navigator);
  h.track('author_search');
  navigator.globalPrivacyControl = true;
  h.requests[0].respond(202);
  await flush();
  assert.equal(h.requests.length, 1);
});

test('explorer attempts are counted only once prerequisites are satisfied', async () => {
  const app = fs.readFileSync(path.join(__dirname, '../../frontend/app.js'), 'utf8');
  const from = app.indexOf('  async function runInstitutionRank(');
  const to = app.indexOf('\n  function setExplorerLoading(', from);
  assert.ok(from >= 0 && to > from);
  const tracked = [], requests = [];
  const context = vm.createContext({
    UsageAnalytics: { track: event => tracked.push(event) },
    state: { isLoading: false, origins: new Set() },
    rankSelection: { institution: null },
    explorerUpdating: false, explorerRefreshPending: false, suggestionRequestId: 0,
    rankInstitutionInput: { focus() {} },
    setRankStatus() {}, renderRankResults() {}, setExplorerLoading() {},
    isWorkId: id => id.startsWith('W'),
    URLSearchParams, AbortController, API_BASE: '', RANK_TIMEOUT_MS: 30000,
    setTimeout() {}, clearTimeout() {}, explorerCoverageNote: () => '', hasCompletePathEvidence: () => true,
    fetch: async (url) => { requests.push(url); return { ok: true, json: async () => ({ results: [] }) }; },
  });
  vm.runInContext(app.slice(from, to) + '\nthis.run = runInstitutionRank;', context);
  await context.run();
  context.rankSelection.institution = { display_name: 'Private institution', id: 'I1' };
  await context.run();
  context.state.origins.add('W1');
  await context.run();
  assert.deepEqual(tracked, []);
  context.state.origins.add('A1');
  context.state.isLoading = true;
  await context.run();
  assert.deepEqual(tracked, []);
  context.state.isLoading = false;
  await context.run();
  assert.deepEqual(tracked, ['explorer_run']);
  assert.equal(requests.length, 1);
  context.explorerUpdating = true;
  await context.run();
  assert.deepEqual(tracked, ['explorer_run']);
});
