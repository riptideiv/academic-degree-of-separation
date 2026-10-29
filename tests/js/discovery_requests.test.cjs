const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const state = require('../../frontend/discovery_state.js');
const source = fs.readFileSync(require.resolve('../../frontend/discovery.js'), 'utf8');
function harness() {
  const nodes = new Map();
  function node(id) {
    if (!nodes.has(id)) nodes.set(id, { textContent: '', innerHTML: '', hidden: false, disabled: false,
      setAttribute() {}, removeAttribute() {}, replaceChildren() {}, append() {}, focus() {} });
    return nodes.get(id);
  }
  const requests = [], events = {}, tracked = [];
  const intent = state.create([{ id: 'T1', display_name: 'Robotics' }]);
  const context = vm.createContext({
    document: { getElementById: node },
    window: { ResearchApp: { intent, esc: String, notice() {},
      request(url, options) { return new Promise((resolve, reject) => requests.push({ url, options, resolve, reject })); } },
      addEventListener(name, cb) { events[name] = cb; }, dispatchEvent() {},
      UsageAnalytics: { track: name => tracked.push(name) } },
    localStorage: { getItem: () => JSON.stringify({id:'I1',display_name:'University'}), setItem() {} },
    URLSearchParams, AbortController, setTimeout: () => 1, clearTimeout() {}, CustomEvent: class {},
  });
  vm.runInContext(source, context);
  const response = name => ({query:{institution:{display_name:name},references:[]},results:[],coverage:{works_checked:2}});
  return { node, requests, events, intent, tracked, api:context.window.Discovery, response };
}

test('a cancelled discovery cannot overwrite results even when the upstream ignores abort', async () => {
  const h = harness();
  const pending = h.api.search();
  h.node('discovery-cancel').onclick();
  assert.equal(h.requests[0].options.signal.aborted, true);
  h.requests[0].resolve(h.response('Old'));
  await pending;
  assert.equal(h.api.current(), null);
  assert.match(h.node('discovery-status').textContent, /stopped/);
});

test('newer submitted intent wins against late completion of an older request', async () => {
  const h = harness();
  const first = h.api.search();
  h.intent.remove('T1'); h.intent.add({id:'W2',display_name:'Paper'});
  h.events['discovery-input']();
  const second = h.api.search();
  assert.match(h.requests[1].url, /reference_ids=W2/);
  h.requests[1].resolve(h.response('New')); await second;
  h.requests[0].resolve(h.response('Old')); await first;
  assert.equal(h.api.current().query.institution.display_name, 'New');
  assert.equal(h.node('discovery-submit').disabled, false);
});

test('failed refresh retains successful results and has an actionable retry message', async () => {
  const h = harness();
  const first = h.api.search(); h.requests[0].resolve(h.response('University')); await first;
  const retry = h.api.search(); h.requests[1].reject(new Error('Upstream unavailable')); await retry;
  assert.equal(h.api.current().query.institution.display_name, 'University');
  assert.match(h.node('discovery-status').textContent, /Previous results are still shown/);
  assert.deepEqual(h.tracked, ['explorer_run','explorer_run']);
});
