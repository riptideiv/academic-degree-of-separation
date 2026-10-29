/* Public IDs only: private notes and browser state never enter a share link. */
(function (root) {
  function encode(query) {
    const institution = query?.institution?.id, refs = query?.references;
    if (!/^I\d{1,20}$/.test(institution) || !Array.isArray(refs) || !refs.length || refs.length > 8 || refs.some(ref => !/^[AWT]\d{1,20}$/.test(ref?.id))) throw new Error('Choose a university and up to eight valid references.');
    const params = new URLSearchParams({ institution });
    [...new Set(refs.map(x => x.id))].forEach(id => params.append('ref', id));
    return '/discover?' + params;
  }
  function decode(search) {
    if (!search) return null;
    if (search.length > 1000) throw new Error('This discovery link is too long. Start a new search.');
    const params = new URLSearchParams(search);
    if ([...params.keys()].some(key => !['institution', 'ref'].includes(key)) || params.getAll('institution').length !== 1) throw new Error('This discovery link is invalid. Start a new search.');
    const query = { institution: { id: params.get('institution') }, references: params.getAll('ref').map(id => ({ id, display_name: id })) };
    encode(query);
    query.institution.display_name = query.institution.id;
    return query;
  }
  const api = { encode, decode };
  if (typeof module !== 'undefined' && module.exports) module.exports = api; else root.DiscoveryLinks = api;
})(typeof window === 'undefined' ? this : window);
