/* Versioned, bounded local shortlist. Imports validate completely before any write. */
(function (root) {
  'use strict';
  const KEY = 'coauthor_saved_v1', MAX_ITEMS = 300, MAX_NOTE = 6000, MAX_BYTES = 2000000;
  const copy = value => JSON.parse(JSON.stringify(value));
  function context(value) {
    if (!value) return undefined;
    const institution = value.institution?.id;
    const refs = value.references;
    if (!/^I\d{1,20}$/.test(institution) || !Array.isArray(refs) || !refs.length || refs.length > 8 || refs.some(x => !/^[AWT]\d{1,20}$/.test(x?.id))) throw new Error('Invalid saved discovery context.');
    return { institution: { id: institution }, references: [...new Set(refs.map(x => x.id))].map(id => ({ id })) };
  }
  function item(value) {
    if (!value || !/^[AW]\d{1,20}$/.test(value.id) || value.type !== (value.id[0] === 'A' ? 'researcher' : 'paper')) throw new Error('Invalid saved researcher or paper.');
    if (typeof value.display_name !== 'string' || !value.display_name.trim() || value.display_name.length > 300) throw new Error('Invalid saved title.');
    if (typeof value.note !== 'string' || value.note.length > MAX_NOTE) throw new Error(`Notes must be at most ${MAX_NOTE} characters.`);
    if (typeof value.saved_at !== 'string' || !/^\d{4}-\d\d-\d\dT/.test(value.saved_at) || !Number.isFinite(Date.parse(value.saved_at))) throw new Error('Invalid saved date.');
    const result = { id: value.id, type: value.type, display_name: value.display_name.trim(), saved_at: new Date(value.saved_at).toISOString(), note: value.note };
    if (value.context) result.context = context(value.context);
    return result;
  }
  function parse(raw) {
    if (typeof raw !== 'string' || new TextEncoder().encode(raw).length > MAX_BYTES) throw new Error('Import must be a JSON file smaller than 2 MB.');
    let data; try { data = JSON.parse(raw); } catch { throw new Error('This file is not valid JSON.'); }
    if (data?.version !== 1 || !Array.isArray(data.items) || data.items.length > MAX_ITEMS) throw new Error('Unsupported saved-list format or more than 300 items.');
    const items = data.items.map(item);
    if (new Set(items.map(x => x.id)).size !== items.length) throw new Error('The import contains duplicate IDs.');
    return items;
  }
  function merge(current, incoming, policy = 'both') {
    if (!['both', 'current', 'imported'].includes(policy)) throw new Error('Choose a note conflict policy.');
    const result = new Map(current.map(x => [x.id, copy(x)]));
    for (const incomingItem of incoming) {
      const next = item(incomingItem), old = result.get(next.id);
      if (old) {
        if (old.note && next.note && old.note !== next.note) {
          if (policy === 'current') next.note = old.note;
          if (policy === 'both') next.note = old.note + '\n\n— Imported note —\n' + next.note;
        } else next.note = old.note || next.note;
        next.saved_at = old.saved_at;
        next.context = old.context || next.context;
      }
      result.set(next.id, item(next));
    }
    if (result.size > MAX_ITEMS) throw new Error('The merged shortlist exceeds 300 items. Remove some items first.');
    return [...result.values()];
  }
  function create(storage) {
    let items = [], warning = '', writable = true, recovery = null;
    try {
      const raw = storage.getItem(KEY);
      if (raw) { try { items = parse(raw); } catch { recovery = raw; writable = false; warning = 'Stored research could not be read. The original data is preserved; new changes last for this visit only.'; } }
    } catch { writable = false; warning = 'Browser storage is unavailable. Changes last for this visit only; export to keep them.'; }
    function commit(next) {
      const raw = JSON.stringify({ version: 1, items: next });
      parse(raw);
      items = copy(next);
      if (writable) try { storage.setItem(KEY, raw); } catch { writable = false; warning = 'Browser storage is full or unavailable. Changes last for this visit only; export to keep them.'; }
      return copy(items);
    }
    return {
      list: () => copy(items), warning: () => warning, recovery: () => recovery,
      has: id => items.some(x => x.id === id),
      add(value) {
        if (items.some(x => x.id === value.id)) return;
        return commit(merge(items, [item({ ...value, note: value.note || '', saved_at: value.saved_at || new Date().toISOString() })]));
      },
      note(id, note) { if (!items.some(x => x.id === id)) throw new Error('This item is no longer saved.'); return commit(items.map(x => x.id === id ? item({ ...x, note }) : x)); },
      remove(id) { const removed = items.find(x => x.id === id); commit(items.filter(x => x.id !== id)); return removed ? copy(removed) : null; },
      import(raw, policy) { return commit(merge(items, parse(raw), policy)); },
      preview(raw) { const incoming = parse(raw); return { count: incoming.length, added: incoming.filter(x => !items.some(y => y.id === x.id)).length, conflicts: incoming.filter(x => items.some(y => y.id === x.id && y.note && x.note && y.note !== x.note)).map(x => x.display_name) }; },
      export: () => JSON.stringify({ version: 1, items }, null, 2),
    };
  }
  const api = { create, parse, merge, context, KEY, MAX_ITEMS, MAX_NOTE, MAX_BYTES };
  if (typeof module !== 'undefined' && module.exports) module.exports = api; else root.SavedStore = api;
})(typeof window === 'undefined' ? this : window);
