/* Institution-scoped discovery. Connections are optional enrichment, never a filter. */
(function () {
  const app = window.ResearchApp;
  const $ = id => document.getElementById(id);
  const { esc, request } = app;
  let institution = null;
  try { const value = JSON.parse(localStorage.getItem('researcherHomeInstitutionV1') || 'null'); if (/^I\d{1,20}$/.test(value?.id)) institution = value; } catch { /* optional storage */ }
  let generation = 0;
  let controller = null;
  let institutionGeneration = 0;
  let institutionController = null;
  let current = null;
  let navigating = false;
  const cache = new Map();
  const connectionControllers = new Set();

  function renderInstitution() {
    $('university-form').hidden = !!institution;
    $('university-selection').hidden = !institution;
    $('university-name').textContent = institution?.display_name || '';
  }
  function setInstitution(value) {
    institution = value;
    try { if (value) localStorage.setItem('researcherHomeInstitutionV1', JSON.stringify(value)); else localStorage.removeItem('researcherHomeInstitutionV1'); }
    catch { app.notice('University selection will last for this visit only.'); }
    renderInstitution();
    changed();
  }
  function stop(message) {
    generation++;
    controller?.abort(); controller = null;
    $('discovery-submit').disabled = false;
    $('discovery-cancel').hidden = true;
    $('discovery-results').removeAttribute('aria-busy');
    if (message) $('discovery-status').textContent = message;
  }
  function changed() {
    stop(current ? 'Inputs changed. Search again to update the results below.' : '');
  }
  window.addEventListener('discovery-input', changed);
  $('university-change').onclick = () => { setInstitution(null); $('university-query').focus(); };
  $('university-query').oninput = () => { institutionGeneration++; institutionController?.abort(); $('university-results').replaceChildren(); };
  $('university-form').onsubmit = async event => {
    event.preventDefault();
    const query = $('university-query').value.trim();
    if (query.length < 2) return;
    institutionController?.abort();
    const abort = institutionController = new AbortController();
    const version = ++institutionGeneration;
    const timeout = setTimeout(() => abort.abort(), 15000);
    $('university-results').innerHTML = '<li>Searching…</li>';
    try {
      const data = await request('/api/institutions?' + new URLSearchParams({ q: query, per_page: '8' }), { signal: abort.signal });
      if (version !== institutionGeneration) return;
      $('university-results').replaceChildren();
      for (const item of data.results) {
        const li = document.createElement('li');
        li.innerHTML = `<button type="button">${esc(item.display_name)}</button><span>${esc(item.country_code || '')}</span>`;
        li.querySelector('button').onclick = () => { setInstitution({ id: item.id, display_name: item.display_name }); $('university-results').replaceChildren(); $('discovery-submit').focus(); };
        $('university-results').append(li);
      }
      if (!data.results.length) $('university-results').innerHTML = '<li>No universities found. Try another name.</li>';
    } catch (error) {
      if (version === institutionGeneration) $('university-results').innerHTML = `<li role="alert">${esc(error.name === 'AbortError' ? 'University search timed out. Try again.' : error.message)}</li>`;
    } finally { clearTimeout(timeout); }
  };
  async function search({ fromURL = false } = {}) {
    let query;
    try { query = app.intent.submit(institution); } catch (error) { app.notice(error.message); return; }
    const path = window.DiscoveryLinks.encode(query);
    if (!fromURL && window.location.pathname + window.location.search !== path) {
      navigating = true;
      app.navigate(path);
      navigating = false;
    }
    stop();
    for (const abort of connectionControllers) abort.abort();
    connectionControllers.clear();
    const version = ++generation;
    const abort = controller = new AbortController();
    const timeout = setTimeout(() => abort.abort(), 30000);
    const params = new URLSearchParams({ institution_id: query.institution.id });
    query.references.forEach(ref => params.append('reference_ids', ref.id));
    $('discovery-submit').disabled = true;
    $('discovery-cancel').hidden = false;
    $('discovery-results').setAttribute('aria-busy', 'true');
    $('discovery-status').textContent = 'Finding researchers and supporting publications…';
    app.notice('');
    window.UsageAnalytics?.track('explorer_run');
    try {
      const data = await request('/api/discover?' + params, { signal: abort.signal });
      if (version !== generation) return;
      current = data;
      cache.set(path, data);
      if (cache.size > 10) cache.delete(cache.keys().next().value);
      // Resolve shared IDs into source labels without changing the submitted intent.
      institution = data.query.institution;
      renderInstitution();
      app.replaceReferences(data.query.references);
      render(data);
      $('discovery-share').hidden = false;
    } catch (error) {
      if (version !== generation) return;
      $('discovery-status').textContent = (error.name === 'AbortError' ? 'Discovery timed out. Try fewer references or search again.' : error.message) + (current ? ' Previous results are still shown.' : '');
    } finally {
      clearTimeout(timeout);
      if (version === generation) { controller = null; $('discovery-submit').disabled = false; $('discovery-cancel').hidden = true; $('discovery-results').removeAttribute('aria-busy'); }
    }
  }
  function render(data) {
    $('discovery-status').textContent = data.message || `${data.results.length} researchers · ${data.coverage.works_checked} publications checked`;
    const results = $('discovery-results');
    results.innerHTML = `<header class="results-heading"><h2>${esc(data.query.institution.display_name)}</h2><p>${data.query.references.map(ref => esc(ref.display_name)).join(' · ')}</p></header>`;
    for (const result of data.results) {
      const author = result.author;
      const row = document.createElement('article');
      row.className = 'discovery-result';
      row.innerHTML = `<div class="result-heading"><h3><a href="/researchers/${esc(author.id)}">${esc(author.display_name)}</a></h3><span data-save-author="${esc(author.id)}"></span></div>
        <p class="evidence-label">${result.affiliation_evidence.verified_current ? 'Current affiliation verified' : 'OpenAlex last-known affiliation'} · ${esc(author.institution)}</p>
        <p class="match-topics">${result.matching_topics.map(topic => esc(topic.display_name)).join(' · ')}</p>
        <ul class="publication-list">${result.works.slice(0, 2).map(work => `<li><a href="${esc(app.safeURL(work.doi) || 'https://openalex.org/' + work.id)}" target="_blank" rel="noopener">${esc(work.title)}</a><span class="publication-year">${esc(work.publication_year || '')}</span><span data-save-paper="${esc(work.id)}"></span></li>`).join('')}</ul>
        <div class="result-actions"><button type="button" class="open-profile">Open profile</button><button type="button" class="check-connection">Check connection</button></div><div class="connection-check" hidden></div>`;
      const open = event => { event.preventDefault(); app.openProfile(author.id, result); };
      row.querySelector('h3 a').onclick = event => { if (!event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey) open(event); };
      row.querySelector('.open-profile').onclick = open;
      row.querySelector('.check-connection').onclick = () => connectionPicker(row, result, data.reference_authors || []);
      results.append(row);
    }
    if (data.coverage.note) {
      const coverage = document.createElement('p'); coverage.className = 'coverage-note'; coverage.textContent = data.coverage.note; results.append(coverage);
    }
    window.dispatchEvent(new CustomEvent('research-discover', { detail: data }));
  }
  function connectionPicker(row, result, references) {
    const panel = row.querySelector('.connection-check');
    if (!panel.hidden) { row.connectionController?.abort(); panel.hidden = true; return; }
    panel.hidden = false;
    const choices = references.filter(author => author.id !== result.author.id);
    panel.innerHTML = `<label>Connect to a researcher${choices.length ? `<select class="connection-reference"><option value="">Choose a reference author</option>${choices.map(author => `<option value="${esc(author.id)}">${esc(author.display_name)}</option>`).join('')}</select>` : ''}</label>
      <form class="connection-search input-action"><input type="search" aria-label="Find a connection reference" placeholder="Search another researcher" minlength="2" maxlength="300" required><button type="submit">Find</button></form><ul class="lookup-results"></ul><div class="connection-status" role="status"></div>`;
    const run = author => check(row, panel, result, author);
    panel.querySelector('select')?.addEventListener('change', event => {
      const author = choices.find(item => item.id === event.target.value); if (author) run(author);
    });
    let lookupVersion = 0;
    let lookupController = null;
    panel.querySelector('input').oninput = () => { lookupVersion++; lookupController?.abort(); panel.querySelector('ul').replaceChildren(); };
    panel.querySelector('form').onsubmit = async event => {
      event.preventDefault();
      lookupController?.abort();
      const abort = lookupController = new AbortController(); connectionControllers.add(abort);
      const version = ++lookupVersion;
      const timeout = setTimeout(() => abort.abort(), 15000);
      const list = panel.querySelector('ul'); list.innerHTML = '<li>Searching…</li>';
      try {
        const data = await request('/api/authors?' + new URLSearchParams({ q: panel.querySelector('input').value.trim(), per_page: '5' }), { signal: abort.signal });
        if (version !== lookupVersion || !panel.isConnected) return;
        list.replaceChildren();
        for (const author of data.results.filter(item => item.id !== result.author.id)) {
          const li = document.createElement('li'); li.innerHTML = `<div>${esc(author.display_name)}<p>${esc(author.institution || '')}</p></div><button type="button">Choose</button>`;
          li.querySelector('button').onclick = () => { list.replaceChildren(); run(author); }; list.append(li);
        }
        if (!list.children.length) list.innerHTML = '<li>No other researchers found.</li>';
      } catch (error) { if (version === lookupVersion && panel.isConnected) list.textContent = error.name === 'AbortError' ? 'Search timed out. Try again.' : error.message; }
      finally { clearTimeout(timeout); connectionControllers.delete(abort); }
    };
    (panel.querySelector('select') || panel.querySelector('input')).focus();
  }
  async function check(row, panel, result, author) {
    row.connectionController?.abort();
    const abort = row.connectionController = new AbortController(); connectionControllers.add(abort);
    const status = panel.querySelector('.connection-status');
    status.textContent = `Checking up to two coauthor steps to ${author.display_name}…`;
    const timeout = setTimeout(() => abort.abort(), 30000);
    try {
      const data = await request('/api/connections/check?' + new URLSearchParams({ from_id: author.id, to_id: result.author.id }), { signal: abort.signal });
      if (row.connectionController !== abort || !panel.isConnected) return;
      if (data.status === 'verified') {
        status.innerHTML = `<p>${data.hops} verified coauthor step${data.hops === 1 ? '' : 's'} to ${esc(author.display_name)}.</p>${app.evidenceHTML(data.steps)}<button type="button">Explore in graph</button>`;
        status.querySelector('button').onclick = async () => {
          const graph = app.graph();
          if (!graph || graph.busy()) { app.notice('Wait for the current graph search or stop it in Connections.'); return; }
          app.navigate('/connections');
          const outcome = await graph.add(author);
          if (outcome && outcome !== 'done') return;
          await graph.add(result.author);
        };
      } else {
        status.textContent = data.status === 'not_found'
          ? `No verified path of up to two steps to ${author.display_name} in this bounded search. Longer paths may exist.`
          : 'Connection search incomplete. Your research match is unchanged.';
        const retry = document.createElement('button'); retry.type = 'button'; retry.textContent = 'Retry check';
        retry.onclick = () => check(row, panel, result, author); status.append(retry);
      }
    } catch (error) {
      if (row.connectionController === abort && panel.isConnected) {
        status.textContent = error.name === 'AbortError' ? 'Connection check timed out.' : error.message;
        const retry = document.createElement('button'); retry.type = 'button'; retry.textContent = 'Retry check';
        retry.onclick = () => check(row, panel, result, author); status.append(retry);
      }
    } finally { clearTimeout(timeout); connectionControllers.delete(abort); }
  }
  function restoreRoute() {
    if (navigating || window.location.pathname !== '/discover') return;
    let query;
    try { query = window.DiscoveryLinks.decode(window.location.search); }
    catch (error) { stop(error.message); return; }
    if (!query) return;
    const path = window.DiscoveryLinks.encode(query);
    stop();
    institution = query.institution;
    app.replaceReferences(query.references);
    renderInstitution();
    if (cache.has(path)) {
      const cached = cache.get(path), sameResults = current === cached;
      current = cached;
      institution = current.query.institution;
      app.replaceReferences(current.query.references);
      renderInstitution();
      if (!sameResults) render(current);
      else $('discovery-status').textContent = current.message || `${current.results.length} researchers · ${current.coverage.works_checked} publications checked`;
      $('discovery-share').hidden = false;
    } else search({ fromURL: true });
  }
  $('discovery-share').onclick = () => { if (current) app.share(window.location.origin + window.DiscoveryLinks.encode(current.query)); };
  window.addEventListener('research-route', restoreRoute);
  $('discovery-submit').onclick = () => search();
  $('discovery-cancel').onclick = () => stop('Search stopped. Your previous results are preserved.');
  renderInstitution();
  restoreRoute();
  window.Discovery = { search, setInstitution, current: () => current, institution: () => institution };
})();
