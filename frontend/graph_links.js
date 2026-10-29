/* Share only public graph starting points and search options, never browser settings. */
(function (root) {
  const AUTHOR_EDGES = ['coauthor', 'citation', 'institution'];
  const WORK_EDGES = ['authorship', 'citation'];
  const NEIGHBORHOODS = ['0,1', '1,4', '2,6', '2,12', '3,16'];

  function validate(value) {
    if (!value || value.v !== 1 || !Array.isArray(value.origins)
      || value.origins.length < 1 || value.origins.length > 10) throw new Error('Invalid shared map');
    const seen = new Set();
    const origins = value.origins.map(origin => {
      if (!origin || !/^[AW]\d{1,20}$/.test(origin.id) || seen.has(origin.id)
        || typeof origin.name !== 'string' || !origin.name.trim() || origin.name.length > 200) {
        throw new Error('Invalid shared starting point');
      }
      seen.add(origin.id);
      return { id: origin.id, name: origin.name.trim() };
    });
    function edges(input, allowed) {
      if (!Array.isArray(input) || input.some(edge => !allowed.includes(edge))) throw new Error('Invalid connection types');
      return [...new Set(input)];
    }
    if (!NEIGHBORHOODS.includes(value.neighborhood)) throw new Error('Invalid neighborhood');
    return { v: 1, origins, edges: edges(value.edges, AUTHOR_EDGES),
      workEdges: edges(value.workEdges, WORK_EDGES), neighborhood: value.neighborhood };
  }

  function encode(baseUrl, value) {
    const url = new URL(baseUrl);
    url.search = '';
    url.username = '';
    url.password = '';
    url.hash = new URLSearchParams({ map: JSON.stringify(validate(value)) }).toString();
    return url.href;
  }

  function decode(hash) {
    if (!hash || !new URLSearchParams(hash.replace(/^#/, '')).has('map')) return null;
    if (hash.length > 16000) throw new Error('Shared map is too large');
    return validate(JSON.parse(new URLSearchParams(hash.replace(/^#/, '')).get('map')));
  }

  const api = { encode, decode };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.GraphLinks = api;
})(typeof window === 'undefined' ? this : window);
