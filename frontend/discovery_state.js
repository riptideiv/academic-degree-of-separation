/* Public discovery intent never derives implicitly from graph state. */
(function (root) {
  function reference(value) {
    if (!value || !/^[AWT]\d{1,20}$/.test(value.id) || typeof value.display_name !== 'string'
      || !value.display_name.trim() || value.display_name.length > 300) throw new Error('Invalid reference');
    return { id: value.id, display_name: value.display_name.trim() };
  }
  function create(initial = []) {
    let references = [];
    for (const item of initial.slice(0, 8)) {
      try { const ref = reference(item); if (!references.some(x => x.id === ref.id)) references.push(ref); } catch { /* skip damaged saved values */ }
    }
    let submitted = null;
    return {
      list: () => references.map(item => ({ ...item })),
      add(value) {
        const ref = reference(value);
        if (references.some(item => item.id === ref.id)) return;
        if (references.length >= 8) throw new Error('Choose up to eight references.');
        references.push(ref);
      },
      replace(values) {
        if (!Array.isArray(values) || values.length > 8) throw new Error('Choose up to eight references.');
        const next = values.map(reference);
        references = next.filter((item, index) => next.findIndex(x => x.id === item.id) === index);
      },
      remove(id) { references = references.filter(item => item.id !== id); },
      submit(institution) {
        if (!institution || !/^I\d{1,20}$/.test(institution.id)) throw new Error('Choose a university.');
        if (!references.length) throw new Error('Choose an interest, paper, or researcher.');
        submitted = { institution: { id: institution.id, display_name: institution.display_name }, references: references.map(item => ({ ...item })) };
        return JSON.parse(JSON.stringify(submitted));
      },
      submitted: () => submitted ? JSON.parse(JSON.stringify(submitted)) : null,
    };
  }
  const api = { create, reference };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.DiscoveryState = api;
})(typeof window === 'undefined' ? this : window);
