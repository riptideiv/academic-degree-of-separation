/* Pure report generation: no requests, credentials, or browser state. */
(function (root) {
  function text(value) {
    return String(value ?? '').replace(/[\r\n]+/g, ' ').replace(/[\\`*_{}\[\]<>#|]/g, '\\$&');
  }

  function person(id, name) {
    const label = text(name || id);
    return /^[AW]\d+$/.test(id) ? `[${label}](https://openalex.org/${id})` : label;
  }

  function status(path) {
    if (path.error) return 'Search interrupted';
    if (!path.found) return path.search_complete === false ? 'Search incomplete — no path found yet' : 'No path found within the search limit';
    return `${path.hops} degree${path.hops === 1 ? '' : 's'} of separation`;
  }

  function report({ origins = [], paths = [] }, date = new Date()) {
    const lines = [
      '# Six Degrees of Academia — connection report', '',
      `Exported ${date.toISOString()}`, '',
      'Source: [OpenAlex](https://openalex.org/). Paths come from a bounded search of the available graph; a missing path does not prove there is no connection.',
      'Affiliations reflect the source records and may not be current appointments.', '',
      '## Researchers and works', '',
      ...origins.map(o => `- ${person(o.id, o.name || o.display_name)}`), '',
      '## Connections', '',
    ];
    if (!paths.length) lines.push('No pair searches have completed.', '');
    for (const path of paths) {
      lines.push(`### ${text(path.from_name || path.from_id)} ↔ ${text(path.to_name || path.to_id)}`, '', status(path), '');
      if (Array.isArray(path.edge_types)) lines.push(`Researcher connections searched: ${path.edge_types.map(text).join(', ') || 'none'}.`, '');
      if (Array.isArray(path.work_edge_types)) lines.push(`Work connections searched: ${path.work_edge_types.map(text).join(', ') || 'none'}.`, '');
      if (path.reason) lines.push(text(path.reason), '');
      if (path.found && path.search_complete === false) lines.push(
        path.hops > 1 ? 'Coverage is incomplete; a shorter path may exist outside the searched records.'
          : 'Connection found; surrounding graph coverage is incomplete.', '',
      );
      for (const [i, step] of (path.steps || []).entries()) {
        const from = person(step.from_id, step.from_name);
        const to = person(step.to_id, step.to_name);
        let relation = { coauthor: 'co-authored', institution: 'shared institution', authorship: 'authorship' }[step.type] || 'citation';
        if (step.type === 'citation') {
          relation = { incoming: 'is cited by', outgoing: 'cites', mutual: 'citations in both directions' }[step.direction] || relation;
        }
        lines.push(`${i + 1}. ${from} → ${to} — ${relation}${step.label ? `: ${text(step.label)}` : ''}`);
      }
      lines.push('');
    }
    return lines.join('\n');
  }

  const api = { report, status };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.ResearchExport = api;
})(typeof window === 'undefined' ? this : window);
