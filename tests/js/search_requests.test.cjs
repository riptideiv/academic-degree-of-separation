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
  const tracked = [];
  const elements = new Map();
  const context = vm.createContext({
    API_BASE: '',
    UsageAnalytics: { track: event => tracked.push(event) },
    AbortController,
    openAlexKeyInput: { value: '' },
    openAlexKeyStatus: { textContent: '' },
    document: {
      createElement(tagName) {
        return {
          tagName, attributes: {}, listeners: {}, children: [], innerHTML: '',
          setAttribute(name, value) { this.attributes[name] = value; },
          addEventListener(name, callback) { this.listeners[name] = callback; },
          replaceChildren(...children) { this.innerHTML = ''; this.children = children; },
        };
      },
      getElementById(id) {
        if (!elements.has(id)) elements.set(id, {
          textContent: '', classList: { add() {}, remove() {} },
          addEventListener() {},
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
          respond(data, ok = true) { resolve({ ok, json: async () => data }); },
        });
      });
    },
    renderResultsList: data => rendered.push(data),
    renderSearchListMessage: message => messages.push(message),
    renderWorksTable: works => `Works: ${works.map(work => work.id).join(', ')}`,
  });
  vm.runInContext([
    section('  const searchSession = {', '\n  searchBtn.addEventListener'),
    section('  async function sendOpenAlexKey(', '\n  function runSearch('),
    section('  function runSearch(', '\n  // Edge-type checkboxes'),
    section('  async function fetchResultsPage(', '\n  function renderSearchListMessage('),
    section('  function openSearchModal(', '\n  // ── Persistence'),
    section('  function invalidateTopWorksCache(', '\n  function onAddFromModal('),
    'this.actions = { runSearch, loadPage, closeSearchModal, sendOpenAlexKey, loadTopWorks, renderTopWorks }; this.session = searchSession;',
  ].join('\n'), context);
  return { ...context, requests, rendered, messages, tracked };
}

const result = (id, page = 1) => ({ results: [{ id }], page, total_pages: 3 });
const flush = () => new Promise(resolve => setImmediate(resolve));

test('analytics counts valid manual submissions, including cached searches, but not pagination or retries', async () => {
  const h = harness();
  h.actions.runSearch('author', { value: ' ' });
  h.actions.runSearch('author', { value: 'A' });
  assert.deepEqual(h.tracked, []);
  h.actions.runSearch('author', { value: 'Private name' });
  h.requests[0].respond(result('A1'));
  await flush();
  h.actions.runSearch('author', { value: 'Private name' });
  const page = h.actions.loadPage(2);
  h.requests[1].reject(new Error('offline'));
  await page;
  const retry = h.actions.loadPage(2);
  h.requests[2].respond(result('A2', 2));
  await retry;
  assert.deepEqual(h.tracked, ['author_search', 'author_search']);
  for (const entity of ['rank-target', 'work', 'rank-institution']) {
    h.actions.runSearch(entity, { value: 'Private search content' });
    h.requests.at(-1).respond(result('X'));
    await flush();
  }
  assert.deepEqual(h.tracked, ['author_search', 'author_search', 'author_search', 'work_search', 'institution_search']);
});

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

test('safe server error messages tell the user how to recover', async () => {
  const h = harness();
  const pending = h.actions.loadPage(1);
  const message = 'OpenAlex rejected the API key. Update your key in Advanced settings.';
  h.requests[0].respond({ code: 'upstream_auth', message }, false);
  await pending;
  assert.deepEqual(h.messages, ['Searching…', message]);
  assert.equal(h.session.pageCache.size, 0);
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

test('failed top-paper requests remain retryable and do not become cached empty results', async () => {
  for (const failure of ['http', 'network', 'malformed']) {
    const h = harness();
    const first = h.actions.loadTopWorks('A1');
    if (failure === 'http') h.requests[0].respond({ message: 'OpenAlex is temporarily unavailable.' }, false);
    else if (failure === 'network') h.requests[0].reject(new Error('offline'));
    else h.requests[0].respond({ unexpected: 'object' });
    await assert.rejects(first);
    assert.equal(h.session.topWorksCache.size, 0);
    const retry = h.actions.loadTopWorks('A1');
    const works = [{ id: 'W1' }];
    h.requests[1].respond(works);
    assert.equal(await retry, works);
    assert.equal(await h.actions.loadTopWorks('A1'), works);
    assert.equal(h.requests.length, 2);
  }
});

test('a successful empty top-paper response is cached', async () => {
  const h = harness();
  const pending = h.actions.loadTopWorks('A1');
  const works = [];
  h.requests[0].respond(works);
  assert.equal(await pending, works);
  assert.equal(await h.actions.loadTopWorks('A1'), works);
  assert.equal(h.requests.length, 1);
});

test('changing or removing a personal key clears top papers and rejects late pre-key responses', async () => {
  for (const key of ['new-key', '']) {
    const h = harness();
    h.session.topWorksCache.set('A2', [{ id: 'W-cached' }]);
    const old = h.actions.loadTopWorks('A1');
    const saving = h.actions.sendOpenAlexKey(key, true);
    h.requests[1].respond({ configured: Boolean(key) });
    await saving;
    assert.equal(h.session.topWorksCache.size, 0);
    const current = h.actions.loadTopWorks('A1');
    const works = [{ id: 'W-current' }];
    h.requests[2].respond(works);
    await current;
    h.requests[0].respond([{ id: 'W-old' }]);
    await assert.rejects(old, /stale works response/);
    assert.equal(h.session.topWorksCache.get('A1'), works);
  }
});

test('the works panel shows a useful failure and its retry button fetches again', async () => {
  const h = harness();
  const panel = h.document.createElement('div');
  const first = h.actions.renderTopWorks(panel, 'A1', 5, () => true);
  assert.equal(panel.attributes['aria-busy'], 'true');
  const message = 'OpenAlex rejected the API key. Update your key in Advanced settings.';
  h.requests[0].respond({ message }, false);
  await first;
  assert.equal(panel.attributes['aria-busy'], 'false');
  assert.equal(panel.children[0].textContent, message);
  assert.equal(panel.children[1].tagName, 'button');
  const retry = panel.children[1].listeners.click();
  h.requests[1].respond([{ id: 'W1' }, { id: 'W2' }]);
  await retry;
  assert.equal(panel.innerHTML, 'Works: W1, W2');
  assert.equal(panel.attributes['aria-busy'], 'false');
});

test('a closed works panel ignores late failures', async () => {
  const h = harness();
  const panel = h.document.createElement('div');
  let current = true;
  const pending = h.actions.renderTopWorks(panel, 'A1', 5, () => current);
  current = false;
  h.requests[0].respond({}, false);
  await pending;
  assert.equal(panel.children.length, 0);
});
