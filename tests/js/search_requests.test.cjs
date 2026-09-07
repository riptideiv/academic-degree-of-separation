// Exercise the shipped search functions with controllable network responses.
// No DOM/browser dependencies are needed to reproduce request ordering races.
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
  const requests = [];
  const rendered = [];
  const messages = [];
  const elements = new Map();
  const context = vm.createContext({
    API_BASE: '',
    AbortController,
    openAlexKeyStatus: { textContent: '' },
    document: {
      getElementById(id) {
        if (!elements.has(id)) elements.set(id, {
          textContent: '', classList: { add() {}, remove() {} },
        });
        return elements.get(id);
      },
    },
    fetch(url, { signal } = {}) {
      return new Promise((resolve, reject) => {
        // Deliberately allow a response after abort, as when response decoding
        // wins the race. The generation check must protect the UI on its own.
        requests.push({
          url, signal, reject,
          respond(data) { resolve({ ok: true, json: async () => data }); },
        });
      });
    },
    renderResultsList: data => rendered.push(data),
    renderSearchListMessage: message => messages.push(message),
  });
  vm.runInContext([
    section('  const searchSession = {', '\n  searchBtn.addEventListener'),
    section('  async function sendOpenAlexKey(', '\n  function runSearch('),
    section('  function runSearch(', '\n  // Edge-type checkboxes'),
    section('  async function fetchResultsPage(', '\n  function renderSearchListMessage('),
    section('  function openSearchModal(', '\n  // ── Persistence'),
    'this.actions = { runSearch, loadPage, closeSearchModal, sendOpenAlexKey }; this.session = searchSession;',
  ].join('\n'), context);
  return { ...context, requests, rendered, messages };
}

const result = (id, page = 1) => ({ results: [{ id }], page, total_pages: 3 });
const flush = () => new Promise(resolve => setImmediate(resolve));

test('late response cannot overwrite a different query or entity cache', async () => {
  const h = harness();
  h.actions.runSearch('author', { value: 'Alice' });
  h.actions.closeSearchModal();
  h.actions.runSearch('work', { value: 'Networks' });
  assert.match(h.requests[0].url, /\/authors\?q=Alice&/);
  assert.match(h.requests[1].url, /\/works\?q=Networks&/);
  const current = result('W1');
  h.requests[1].respond(current);
  await flush();
  h.requests[0].respond(result('A1'));
  await flush();
  assert.deepEqual(h.rendered, [current]);
  assert.equal(h.session.pageCache.get(1), current);
  assert.equal(h.requests[0].signal.aborted, true);
});

test('closing the modal invalidates late responses and aborted failures', async () => {
  for (const reject of [false, true]) {
    const h = harness();
    h.actions.runSearch('author', { value: 'Alice' });
    h.actions.closeSearchModal();
    assert.equal(h.requests[0].signal.aborted, true);
    if (reject) {
      h.requests[0].reject(Object.assign(new Error('aborted'), { name: 'AbortError' }));
    } else {
      h.requests[0].respond(result('A1'));
    }
    await flush();
    assert.deepEqual(h.rendered, []);
    assert.deepEqual(h.messages, ['Searching…']);
    assert.equal(h.session.pageCache.size, 0);
    assert.equal(h.session.pendingController, null);
  }
});

test('the newest page wins when responses arrive out of order', async () => {
  const h = harness();
  h.session.query = 'Alice';
  const second = h.actions.loadPage(2);
  const third = h.actions.loadPage(3);
  const current = result('A3', 3);
  h.requests[1].respond(current);
  await third;
  h.requests[0].respond(result('A2', 2));
  await second;
  assert.deepEqual(h.rendered, [current]);
  assert.equal(h.requests[0].signal.aborted, true);
  assert.equal(h.session.currentPage, 3);
  assert.equal(h.session.pageCache.has(2), false);
  assert.equal(h.session.pendingController, null);
});

test('selecting a cached page cancels a pending page without another fetch', async () => {
  const h = harness();
  const cached = result('A1');
  h.session.pageCache.set(1, cached);
  const pending = h.actions.loadPage(2);
  await h.actions.loadPage(1);
  assert.equal(h.requests.length, 1);
  assert.equal(h.requests[0].signal.aborted, true);
  h.requests[0].respond(result('A2', 2));
  await pending;
  assert.deepEqual(h.rendered, [cached]);
  assert.equal(h.session.currentPage, 1);
});

test('reopening the same query reuses cached results without a network request', async () => {
  const h = harness();
  h.actions.runSearch('author', { value: 'Alice' });
  const cached = result('A1');
  h.requests[0].respond(cached);
  await flush();
  h.actions.closeSearchModal();
  h.actions.runSearch('author', { value: '  Alice  ' });
  assert.equal(h.requests.length, 1);
  assert.deepEqual(h.rendered, [cached, cached]);
});

test('same query and page after closing still ignores the earlier request', async () => {
  const h = harness();
  h.actions.runSearch('author', { value: 'Alice' });
  h.actions.closeSearchModal();
  h.actions.runSearch('author', { value: 'Alice' });
  h.requests[0].respond(result('A-old'));
  await flush();
  assert.deepEqual(h.rendered, []);
  assert.equal(h.session.pendingController.signal, h.requests[1].signal);
  const current = result('A-new');
  h.requests[1].respond(current);
  await flush();
  assert.deepEqual(h.rendered, [current]);
  assert.equal(h.session.pageCache.get(1), current);
});

test('changing entity type invalidates cached pages even with identical query text', async () => {
  const h = harness();
  h.actions.runSearch('author', { value: 'Cambridge' });
  h.requests[0].respond(result('A1'));
  await flush();
  h.actions.runSearch('rank-institution', { value: 'Cambridge' });
  assert.equal(h.requests.length, 2);
  assert.equal(h.session.pageCache.size, 0);
  assert.match(h.requests[1].url, /\/institutions\?q=Cambridge&/);
  h.requests[1].respond(result('I1'));
  await flush();
  assert.equal(h.session.pageCache.get(1).results[0].id, 'I1');
});

test('an active network failure remains visible and can be retried', async () => {
  const h = harness();
  const pending = h.actions.loadPage(1);
  h.requests[0].reject(new Error('offline'));
  await pending;
  assert.deepEqual(h.messages, ['Searching…', 'Search failed. Please try again.']);
  assert.equal(h.session.pendingController, null);
  const retry = h.actions.loadPage(1);
  const current = result('A1');
  h.requests[1].respond(current);
  await retry;
  assert.deepEqual(h.rendered, [current]);
});

test('saving an API key restarts an active search and discards pre-key results', async () => {
  const h = harness();
  h.actions.runSearch('author', { value: 'Alice' });
  const saving = h.actions.sendOpenAlexKey('test-key', true);
  h.requests[1].respond({ configured: true });
  await saving;
  assert.equal(h.requests[0].signal.aborted, true);
  assert.equal(h.requests.length, 3);
  assert.equal(h.requests[2].url, h.requests[0].url);
  const current = result('A-live');
  h.requests[2].respond(current);
  await flush();
  h.requests[0].respond(result('A-fallback'));
  await flush();
  assert.deepEqual(h.rendered, [current]);
  assert.equal(h.session.pageCache.get(1), current);
  assert.equal(h.openAlexKeyStatus.textContent, 'API key saved');
});
