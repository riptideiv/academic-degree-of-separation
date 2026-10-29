const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const links = require('../../frontend/discovery_links.js');
const state = require('../../frontend/discovery_state.js');
const source = fs.readFileSync(require.resolve('../../frontend/discovery.js'), 'utf8');
function harness(initialSearch = '') {
  const nodes = new Map();
  function node(id) {
    if (!nodes.has(id)) nodes.set(id, { textContent: '', innerHTML: '', hidden: false, disabled: false,
      setAttribute() {}, removeAttribute() {}, replaceChildren() {}, append() {}, focus() {} });
    return nodes.get(id);
  }
  const requests = [], events = {}, tracked = [];
  const location = { pathname: '/discover', search: initialSearch };
  const intent = state.create([{ id: 'T1', display_name: 'Robotics' }]);
  const context = vm.createContext({
    document: { getElementById: node },
    window: { location, DiscoveryLinks: links, ResearchApp: { intent, esc: String, notice() {}, navigate(path) { const url = new URL(path, 'http://test'); location.pathname = url.pathname; location.search = url.search; events['research-route']?.(); }, replaceReferences(values) { intent.replace(values); },
      request(url, options) { return new Promise((resolve, reject) => requests.push({ url, options, resolve, reject })); } },
      addEventListener(name, cb) { events[name] = cb; }, dispatchEvent() {},
      UsageAnalytics: { track: name => tracked.push(name) } },
    localStorage: { getItem: () => JSON.stringify({id:'I1',display_name:'University'}), setItem() {} },
    URLSearchParams, AbortController, setTimeout: () => 1, clearTimeout() {}, CustomEvent: class {},
  });
  vm.runInContext(source, context);
  const response = name => ({query:{institution:{id:'I1',display_name:name},references:[{id:'T1',display_name:'Robotics'}]},results:[],coverage:{works_checked:2}});
  return { node, requests, events, intent, tracked, location, api:context.window.Discovery, response };
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


test('a directly shared query overrides local draft references and malformed links issue no requests', async () => {
  const h = harness('?institution=I2&ref=W3');
  assert.equal(h.requests.length,1);
  assert.match(h.requests[0].url,/institution_id=I2&reference_ids=W3/);
  assert.equal(h.intent.list()[0].id,'W3');
  const bad = harness('?institution=I2&ref=bad');
  assert.equal(bad.requests.length,0);
  assert.match(bad.node('discovery-status').textContent,/valid references/);
  h.requests[0].resolve(h.response('Shared')); await new Promise(resolve=>setImmediate(resolve));
});

test('history restoration recovers cached results and cancels a newer in-flight query', async () => {
  const h = harness();
  const initial = h.api.search(); h.requests[0].resolve(h.response('Original')); await initial;
  const originalSearch = h.location.search;
  h.intent.replace([{id:'T2',display_name:'Second topic'}]);
  const next = h.api.search();
  h.location.search = originalSearch; h.events['research-route']();
  assert.equal(h.requests[1].options.signal.aborted,true);
  assert.equal(h.api.current().query.institution.display_name,'Original');
  assert.equal(h.intent.list()[0].id,'T1');
  h.requests[1].resolve(h.response('Late')); await next;
  assert.equal(h.api.current().query.institution.display_name,'Original');
  assert.equal(h.requests.length,2);
});
