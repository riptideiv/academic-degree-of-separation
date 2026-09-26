const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../../frontend/analytics_dashboard.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '../../frontend/analytics.html'), 'utf8');
const flush = () => new Promise(resolve => setImmediate(resolve));
const summary = () => ({
  period: { days: 30, start_date: '2026-08-28', end_date: '2026-09-26', timezone: 'UTC' },
  totals: { unique_visitors: 12, page_views: 25, searches: 18, searching_visitors: 7, graph_runs: 20, explorer_runs: 8 },
  events: { author_search: 14, work_search: 3, institution_search: 1 },
  daily: [
    { date: '2026-09-25', unique_visitors: 8, page_views: 12, searches: 10, searching_visitors: 4, graph_runs: 12, explorer_runs: 5 },
    { date: '2026-09-26', unique_visitors: 9, page_views: 13, searches: 8, searching_visitors: 5, graph_runs: 8, explorer_runs: 3 },
  ],
  status: { enabled: true, storage: 'sqlite', available: true, pending_events: 0, dropped_events: 0, retention_days: 365 },
});

function harness() {
  const requests = [], elements = new Map(), timers = new Map();
  let timerId = 0;
  function element(tag = 'div') {
    const classes = new Set();
    return {
      tag, value: '', textContent: '', hidden: false, disabled: false, children: [], listeners: {},
      classList: { add: name => classes.add(name), remove: name => classes.delete(name), toggle(name, enabled) { enabled ? classes.add(name) : classes.delete(name); }, contains: name => classes.has(name) },
      addEventListener(name, listener) { this.listeners[name] = listener; },
      replaceChildren(...children) { this.children = children; },
      append(child) { this.children.push(child); },
      setAttribute() {}, focus() { this.focused = true; },
    };
  }
  for (const match of html.matchAll(/<[^>]*\bid="([^"]+)"[^>]*>/g)) {
    const node = element();
    node.hidden = /\bhidden\b/.test(match[0]);
    elements.set(match[1], node);
  }
  elements.get('analytics-days').value = '30';
  const context = vm.createContext({
    AbortController,
    setTimeout(fn) { timers.set(++timerId, fn); return timerId; },
    clearTimeout: id => timers.delete(id),
    document: { getElementById: id => elements.get(id), createElement: element },
    fetch(url, init) {
      return new Promise((resolve, reject) => {
        requests.push({
          url, init, reject,
          respond: (data, status = 200) => resolve({ status, ok: status >= 200 && status < 300, json: async () => data }),
          respondPendingJson: promise => resolve({ status: 200, ok: true, json: () => promise }),
        });
      });
    },
  });
  context.window = context;
  for (const property of ['localStorage', 'sessionStorage']) {
    Object.defineProperty(context, property, { get() { throw new Error('Tokens must never use persistent browser storage'); } });
  }
  vm.runInContext(source, context);
  const get = id => elements.get(id);
  const submit = (token = 'private-admin-token') => {
    get('admin-token').value = token;
    get('analytics-login').listeners.submit({ preventDefault() {} });
  };
  return { get, submit, requests, timers };
}

test('dashboard is idle until authenticated and sends the secret only in the authorization header', async () => {
  const h = harness();
  assert.equal(h.requests.length, 0);
  h.get('analytics-days').listeners.change();
  assert.equal(h.requests.length, 0);
  h.submit();
  assert.equal(h.requests.length, 1);
  assert.equal(h.requests[0].url, '/api/analytics/summary?days=30');
  assert.equal(h.requests[0].init.headers.Authorization, 'Bearer private-admin-token');
  assert.equal(h.requests[0].init.credentials, 'omit');
  assert.equal(h.requests[0].init.cache, 'no-store');
  assert.equal(h.requests[0].init.referrerPolicy, 'no-referrer');
  h.requests[0].respond(summary());
  await flush();
  assert.equal(h.get('admin-token').value, '');
  assert.equal(h.get('analytics-login').hidden, true);
  assert.equal(h.get('analytics-results').hidden, false);
  assert.equal(h.get('total-unique_visitors').textContent, '12');
  assert.equal(h.get('total-searches').textContent, '18');
  assert.equal(h.get('event-work_search').textContent, '3');
  assert.equal(h.get('daily-activity').children[0].children[0].textContent, '2026-09-26');
  assert.equal(h.get('daily-activity').children[0].children[1].textContent, '9');
  assert.equal(h.timers.size, 0);
  h.get('analytics-days').value = '7';
  h.get('analytics-days').listeners.change();
  assert.equal(h.requests[1].url, '/api/analytics/summary?days=7');
  assert.equal(h.requests[1].init.headers.Authorization, 'Bearer private-admin-token');
  h.requests[1].respond(summary());
  await flush();
});

test('signing out clears token and counts and cannot be undone by a late response', async () => {
  const h = harness();
  h.submit();
  h.requests[0].respond(summary());
  await flush();
  h.get('refresh-analytics').listeners.click();
  let finishJson;
  h.requests[1].respondPendingJson(new Promise(resolve => { finishJson = resolve; }));
  await flush();
  h.get('sign-out').listeners.click();
  assert.equal(h.requests[1].init.signal.aborted, true);
  assert.equal(h.get('total-unique_visitors').textContent, '—');
  assert.equal(h.get('daily-activity').children.length, 0);
  assert.equal(h.get('analytics-results').hidden, true);
  assert.equal(h.get('admin-token').value, '');
  finishJson(summary());
  await flush();
  assert.equal(h.get('analytics-results').hidden, true);
  assert.equal(h.get('analytics-login').hidden, false);
  assert.equal(h.get('sign-out').hidden, true);
  assert.match(h.get('dashboard-message').textContent, /Signed out/);
  h.get('analytics-days').listeners.change();
  h.get('refresh-analytics').listeners.click();
  assert.equal(h.requests.length, 2);
});

test('invalid authentication forgets the secret and provides a useful error', async () => {
  const h = harness();
  h.submit();
  h.requests[0].respond({}, 403);
  await flush();
  assert.equal(h.get('admin-token').value, '');
  assert.equal(h.get('analytics-results').hidden, true);
  assert.match(h.get('dashboard-message').textContent, /Access denied/);
  h.get('refresh-analytics').listeners.click();
  assert.equal(h.requests.length, 1);
});

test('storage outages, network errors and malformed data never render misleading counts', async () => {
  for (const failure of ['unavailable', 'network', 'malformed']) {
    const h = harness();
    h.submit();
    if (failure === 'unavailable') h.requests[0].respond({}, 503);
    if (failure === 'network') h.requests[0].reject(new Error('offline'));
    if (failure === 'malformed') h.requests[0].respond({});
    await flush();
    assert.equal(h.get('analytics-results').hidden, true);
    assert.equal(h.get('total-page_views').textContent, '—');
    assert.equal(h.get('dashboard-message').classList.contains('error'), true);
    assert.equal(h.get('refresh-analytics').disabled, false);
    assert.equal(h.get('sign-out').hidden, false);
  }
});

test('dashboard highlights incomplete collection and renders server values as text', async () => {
  const h = harness();
  const data = summary();
  data.status.enabled = false;
  data.status.available = false;
  data.status.dropped_events = 3;
  data.status.pending_events = 2;
  data.daily[0].date = '<img src=x onerror=alert(1)>';
  h.submit();
  h.requests[0].respond(data);
  await flush();
  const status = h.get('storage-status');
  assert.equal(status.classList.contains('warning'), true);
  assert.match(status.textContent, /disabled/);
  assert.match(status.textContent, /unavailable/);
  assert.match(status.textContent, /3 events were dropped/);
  assert.match(status.textContent, /2 events are waiting/);
  assert.ok(h.get('daily-activity').children.some(row => row.children[0].textContent === data.daily[0].date));
});

test('newer period wins even if an aborted request responds late', async () => {
  const h = harness();
  h.submit();
  h.get('analytics-days').value = '90';
  h.get('analytics-days').listeners.change();
  assert.equal(h.requests[0].init.signal.aborted, true);
  const newest = summary();
  newest.totals.searches = 200;
  h.requests[1].respond(newest);
  await flush();
  h.requests[0].respond(summary());
  await flush();
  assert.equal(h.get('total-searches').textContent, '200');
});
