/* Shared save controls and a local, portable research shortlist. */
(function () {
  'use strict';
  const app = window.ResearchApp, { esc } = app;
  let storage; try { storage = localStorage; } catch { storage = { getItem() { throw new Error('Unavailable'); } }; }
  const store = window.SavedStore.create(storage);
  const entities = new Map();
  const $ = id => document.getElementById(id);
  let renderedState = '';
  let filter = 'all', removed = null, pendingImport = null, importGeneration = 0;
  $('saved-content').innerHTML = `<p class="saved-privacy">Saved in this browser. Export includes your notes.</p>
    <div class="saved-tools"><label>Show <select id="saved-filter"><option value="all">All research</option><option value="researcher">Researchers</option><option value="paper">Papers</option></select></label>
    <button type="button" id="saved-export">Export JSON</button><button type="button" id="saved-import">Import JSON</button><input type="file" id="saved-file" accept="application/json,.json" hidden></div>
    <p id="saved-warning" role="status"></p><button type="button" id="saved-recovery" hidden>Download original data</button>
    <div id="saved-import-preview" hidden></div><div id="saved-status" role="status"></div><div id="saved-list"></div>`;
  function warnings() { $('saved-warning').textContent = store.warning(); $('saved-recovery').hidden = store.recovery() === null; }
  function download(text, name) {
    const url = URL.createObjectURL(new Blob([text], { type: 'application/json' }));
    const link = document.createElement('a'); link.href = url; link.download = name; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  function register(value, context) {
    if (!value?.id || !/^[AW]\d{1,20}$/.test(value.id)) return;
    entities.set(value.id, { id: value.id, display_name: String(value.display_name || value.title || value.name || value.id).slice(0, 300), type: value.id[0] === 'A' ? 'researcher' : 'paper', ...((context || entities.get(value.id)?.context) ? { context: context || entities.get(value.id).context } : {}) });
  }
  function updateControls() {
    document.querySelectorAll('[data-save-author], [data-save-paper]').forEach(slot => {
      const id = slot.dataset.saveAuthor || slot.dataset.savePaper;
      if (!entities.has(id)) return;
      let button = slot.querySelector('button');
      if (!button) { button = document.createElement('button'); button.type = 'button'; button.className = 'save-research'; slot.append(button); }
      button.textContent = store.has(id) ? 'Saved — view' : 'Save';
      button.setAttribute('aria-label', (store.has(id) ? 'View saved ' : 'Save ') + entities.get(id).display_name);
      button.onclick = () => {
        if (store.has(id)) { app.navigate('/saved'); return; }
        try { store.add(entities.get(id)); updateControls(); warnings(); app.notice(store.warning() || 'Saved to your shortlist.'); }
        catch (error) { app.notice(error.message); }
      };
    });
  }
  function render() {
    const state = filter + store.export();
    warnings();
    if (state === renderedState) return;
    renderedState = state;
    const list = $('saved-list'); list.replaceChildren();
    const items = store.list().filter(x => filter === 'all' || x.type === filter).reverse();
    for (const item of items) {
      const row = document.createElement('article'); row.className = 'saved-item';
      const title = item.type === 'researcher'
        ? `<a href="/researchers/${esc(item.id)}" data-researcher="${esc(item.id)}">${esc(item.display_name)}</a>`
        : `<a href="https://openalex.org/${esc(item.id)}" target="_blank" rel="noopener">${esc(item.display_name)} ↗</a>`;
      row.innerHTML = `<div class="result-heading"><h3>${title}</h3><button type="button" class="remove-saved" aria-label="Remove ${esc(item.display_name)}">Remove</button></div>
        <label for="note-${esc(item.id)}">Note</label><textarea id="note-${esc(item.id)}" rows="2" maxlength="6000">${esc(item.note)}</textarea><span class="note-status" role="status"></span>
        ${item.context ? '<button type="button" class="reopen-search text-button">Reopen discovery</button>' : ''}`;
      row.querySelector('textarea').oninput = event => {
        try { store.note(item.id, event.target.value); renderedState = filter + store.export(); row.querySelector('.note-status').textContent = store.warning() ? 'Kept for this visit.' : 'Saved'; warnings(); }
        catch (error) { row.querySelector('.note-status').textContent = error.message; }
      };
      row.querySelector('.remove-saved').onclick = () => {
        removed = store.remove(item.id); render(); updateControls();
        $('saved-status').textContent = `Removed ${item.display_name}. `;
        const undo = document.createElement('button'); undo.type = 'button'; undo.textContent = 'Undo';
        undo.onclick = () => { try { store.add(removed); removed = null; $('saved-status').replaceChildren(); render(); updateControls(); } catch (error) { $('saved-status').textContent = error.message; } };
        $('saved-status').append(undo); undo.focus();
      };
      if (item.context) row.querySelector('.reopen-search').onclick = () => app.navigate(window.DiscoveryLinks.encode(item.context));
      list.append(row);
    }
    if (!items.length) list.innerHTML = `<p class="saved-empty">${store.list().length ? 'No saved ' + (filter === 'paper' ? 'papers' : 'researchers') + '.' : 'Your shortlist is empty.'}</p><a href="/discover" data-nav="discover">Find researchers</a>`;
  }
  $('saved-filter').onchange = event => { filter = event.target.value; render(); };
  $('saved-export').onclick = () => download(store.export(), 'coauthor-distance-saved.json');
  $('saved-recovery').onclick = () => download(store.recovery(), 'coauthor-distance-original-data.json');
  $('saved-import').onclick = () => $('saved-file').click();
  $('saved-file').onchange = async event => {
    const version = ++importGeneration;
    const file = event.target.files[0]; event.target.value = '';
    pendingImport = null; $('saved-import-preview').hidden = true;
    if (!file) return;
    try {
      if (file.size > window.SavedStore.MAX_BYTES) throw new Error('Choose a JSON file smaller than 2 MB.');
      const raw = await file.text();
      if (version !== importGeneration) return;
      const preview = store.preview(raw); pendingImport = raw;
      $('saved-import-preview').innerHTML = `<h3>Import ${preview.count} items</h3><p>${preview.added} new · ${preview.count - preview.added} already saved</p>
        ${preview.conflicts.length ? `<p>Different notes for: ${preview.conflicts.map(esc).join(', ')}</p><label>Conflicting notes<select id="saved-conflict"><option value="both">Keep both notes</option><option value="current">Keep existing notes</option><option value="imported">Use imported notes</option></select></label>` : ''}
        <button type="button" id="saved-import-confirm">Import items</button><button type="button" id="saved-import-cancel">Cancel</button><p id="saved-import-error" role="alert"></p>`;
      $('saved-import-preview').hidden = false;
      $('saved-import-confirm').onclick = () => {
        try { store.import(pendingImport, $('saved-conflict')?.value || 'both'); pendingImport = null; $('saved-import-preview').hidden = true; $('saved-status').textContent = 'Import complete.'; render(); updateControls(); $('saved-import').focus(); }
        catch (error) { $('saved-import-error').textContent = error.message; }
      };
      $('saved-import-cancel').onclick = () => { pendingImport = null; $('saved-import-preview').hidden = true; $('saved-import').focus(); };
      $('saved-import-confirm').focus();
    } catch (error) { if (version === importGeneration) $('saved-status').textContent = error.message; }
  };
  window.addEventListener('research-route', event => { if (event.detail.view === 'saved') render(); });
  window.addEventListener('research-profile', event => {
    register(event.detail);
    $('profile-save-slot').dataset.saveAuthor = event.detail.id; updateControls();
  });
  window.addEventListener('research-papers', event => { event.detail.forEach(p => register(p)); updateControls(); });
  window.addEventListener('research-selections', event => { event.detail.forEach(p => register(p)); updateControls(); });
  window.addEventListener('research-selection', event => { register(event.detail); updateControls(); });
  window.addEventListener('research-discover', event => {
    for (const result of event.detail.results) { register(result.author, event.detail.query); result.works.forEach(p => register(p, event.detail.query)); }
    updateControls();
  });
  // A cached response can finish between deferred scripts; also hydrate current data.
  const initialProfile = app.profile();
  if (initialProfile && $('profile-save-slot')) {
    register(initialProfile); $('profile-save-slot').dataset.saveAuthor = initialProfile.id;
  }
  (app.papers?.() || []).forEach(p => register(p));
  const initialDiscovery = window.Discovery?.current();
  for (const result of initialDiscovery?.results || []) {
    register(result.author, initialDiscovery.query);
    result.works.forEach(p => register(p, initialDiscovery.query));
  }
  updateControls();
  render();
})();
