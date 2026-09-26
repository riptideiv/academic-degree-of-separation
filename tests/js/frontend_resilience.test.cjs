// Exercise optional persistence and native disclosure controls without a browser dependency.
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

// A small DOM double supports the actual row renderer and disclosure handlers.
function element(tagName = 'div') {
  const el = {
    tagName, className: '', attributes: {}, listeners: {}, children: [], dataset: {}, style: {},
    setAttribute(name, value) { this.attributes[name] = value; },
    getAttribute(name) { return this.attributes[name]; },
    addEventListener(name, callback) { this.listeners[name] = callback; },
    removeEventListener(name) { delete this.listeners[name]; },
    appendChild(child) { child.parent = this; this.children.push(child); },
    after(child) {
      child.parent = this.parent;
      this.parent.children.splice(this.parent.children.indexOf(this) + 1, 0, child);
    },
    remove() {
      if (this.parent) this.parent.children.splice(this.parent.children.indexOf(this), 1);
      this.parent = null;
    },
    get nextElementSibling() { return this.parent?.children[this.parent.children.indexOf(this) + 1]; },
    get isConnected() { return Boolean(this.parent); },
    querySelector(selector) { return this.controls?.[selector]; },
    set innerHTML(html) {
      this.html = html;
      this.children.forEach(child => { child.parent = null; });
      this.children = [];
      const info = html.match(/<(button|div)[^>]*class="result-row-info"([^>]*)>/);
      if (info) {
        const control = element(info[1]);
        for (const match of info[2].matchAll(/(aria-[\w-]+)="([^"]*)"/g)) control.setAttribute(match[1], match[2]);
        this.controls = {
          '.result-row-info': control,
          '.add-btn-inline': element('button'),
          '.result-arrow': element('span'),
        };
      }
    },
    get innerHTML() { return this.html; },
  };
  el.classList = {
    contains: name => el.className.split(' ').includes(name),
    add(name) { if (!this.contains(name)) el.className = `${el.className} ${name}`.trim(); },
    remove(name) { el.className = el.className.split(' ').filter(value => value !== name).join(' '); },
  };
  return el;
}

test('blocked storage does not interrupt sidebar startup, institution selection, or graph clearing', () => {
  for (const accessFailure of [false, true]) {
    const controls = new Map(['sidebar', 'sidebar-resizer', 'menu-toggle'].map(id => [id, element()]));
    const events = [];
    const context = vm.createContext({
      document: { getElementById: id => controls.get(id) },
      cy: { resize() {} }, setTimeout: () => 1, clearTimeout() {},
      HOME_INSTITUTION_STORAGE: 'institution', STORAGE_KEY: 'graph',
      searchSession: { entityType: 'rank-institution', pageCache: new Map() },
      rankSelection: { institution: null }, rankInstitutionInput: {}, suggestionRequestId: 0,
      setExplorerLoading() {}, renderHomeInstitution: () => events.push('rendered'),
      renderRankSelectionStatus() {}, runInstitutionRank() {}, invalidateTopWorksCache() {},
      closeSearchModal: () => events.push('closed'),
    });
    const blocked = () => { throw new Error('Storage denied'); };
    if (accessFailure) Object.defineProperty(context, 'localStorage', { get: blocked });
    else context.localStorage = { getItem: blocked, setItem: blocked, removeItem: blocked };
    vm.runInContext([
      section('  const storage = {', '\n  // ── State'),
      section('  const SIDEBAR_WIDTH_KEY =', '\n  // ── Mobile view switcher'),
      section('  function onAddFromModal(', '\n  // Windowed pagination'),
      section('  function clearSavedState(', '\n  // ── Add researcher'),
      'this.actions = { onAddFromModal, clearSavedState, loadSavedState };',
    ].join('\n'), context);
    assert.equal(typeof controls.get('sidebar-resizer').listeners.pointerdown, 'function');
    assert.equal(typeof controls.get('menu-toggle').listeners.click, 'function');
    controls.get('menu-toggle').listeners.click();
    assert.equal(controls.get('sidebar').classList.contains('collapsed'), true);
    context.actions.onAddFromModal({ id: 'I1', display_name: 'Example University' });
    assert.equal(context.rankSelection.institution.id, 'I1');
    assert.deepEqual(events, ['rendered', 'closed']);
    assert.doesNotThrow(context.actions.clearSavedState);
    assert.doesNotThrow(context.actions.loadSavedState);
  }
});

test('author disclosures use native buttons and keep aria state aligned with visible details', async () => {
  const list = element('ul');
  const document = {
    createElement: element,
    getElementById: () => list,
    querySelectorAll(selector) {
      if (selector.endsWith('.result-detail')) return list.children.filter(child => child.className === 'result-detail');
      const rows = list.children.filter(child => child.className === 'result-row');
      if (selector.endsWith('.result-arrow.expanded')) {
        return rows.map(row => row.controls['.result-arrow']).filter(arrow => arrow.classList.contains('expanded'));
      }
      return rows.map(row => row.controls['.result-row-info']).filter(control => control.getAttribute('aria-expanded') === 'true');
    },
  };
  const context = vm.createContext({
    document, state: { origins: new Set() }, searchSession: { entityType: 'author' },
    escHtml: value => String(value), escAttr: value => String(value),
    renderPagination() {}, onAddFromModal() {},
    renderTopWorks: async container => { container.innerHTML = 'Works'; },
  });
  vm.runInContext([
    section('  function renderResultsList(', '\n  // expandable degree panel'),
    'this.renderResultsList = renderResultsList;',
  ].join('\n'), context);
  context.renderResultsList({
    results: ['A1', 'A2'].map(id => ({ id, display_name: id, works_count: 2, cited_by_count: 3 })),
    page: 1, total_pages: 1,
  });
  const rows = [...list.children];
  const [first, second] = rows.map(row => row.querySelector('.result-row-info'));
  assert.equal(first.tagName, 'button'); // Enter/Space activation comes from native button semantics.
  assert.equal(first.getAttribute('aria-expanded'), 'false');
  assert.equal(second.getAttribute('aria-expanded'), 'false');
  await first.listeners.click();
  assert.equal(first.getAttribute('aria-expanded'), 'true');
  assert.equal(rows[0].nextElementSibling.id, first.getAttribute('aria-controls'));
  await second.listeners.click();
  assert.equal(first.getAttribute('aria-expanded'), 'false');
  assert.equal(second.getAttribute('aria-expanded'), 'true');
  assert.equal(rows[0].nextElementSibling, rows[1]);
  await second.listeners.click();
  assert.equal(second.getAttribute('aria-expanded'), 'false');
  assert.equal(rows[1].nextElementSibling, undefined);
});
