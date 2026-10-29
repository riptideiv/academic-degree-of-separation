/* Application navigation, discovery intent, and shared researcher profiles. */
(function () {
  'use strict';
  const $ = id => document.getElementById(id);
  const API = window.RESEARCHER_API_BASE ?? '';
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const safeURL = value => { try { const url = new URL(value); return ['https:', 'http:'].includes(url.protocol) ? url.href : ''; } catch { return ''; } };
  let initial = [];
  try { const value = JSON.parse(localStorage.getItem('coauthor_discovery_refs_v1') || '[]'); if (Array.isArray(value)) initial = value; } catch { /* optional storage */ }
  const intent = window.DiscoveryState.create(initial);
  let graph = null;
  let route = '';
  let profileRequest = 0;
  let profileController = null;
  let papersController = null;
  let papersGeneration = 0;
  let searchController = null;
  let searchGeneration = 0;
  let profileContext = null;
  const scrolls = new Map();
  const focusTargets = new Map();
  let currentProfile = null;
  let currentPapers = [];
  let shareReturnFocus = null;

  async function request(path, options = {}) {
    const response = await fetch(API + path, options);
    let data;
    try { data = await response.json(); } catch { throw new Error('The server returned an unreadable response. Try again.'); }
    if (!response.ok) throw new Error(typeof data.message === 'string' ? data.message : typeof data.detail === 'string' ? data.detail : 'Could not load this research. Try again.');
    return data;
  }
  function notice(text) { $('app-notice').textContent = text; }
  function rememberReferences() {
    try { localStorage.setItem('coauthor_discovery_refs_v1', JSON.stringify(intent.list())); }
    catch { notice('These references will last for this visit only; browser storage is unavailable.'); }
  }
  function renderReferences() {
    $('discovery-references').replaceChildren();
    for (const item of intent.list()) {
      const chip = document.createElement('li');
      chip.innerHTML = `<span>${esc(item.display_name)}</span><button type="button" aria-label="Remove ${esc(item.display_name)}">×</button>`;
      chip.querySelector('button').onclick = () => { intent.remove(item.id); rememberReferences(); renderReferences(); notice('References changed. Search to update results.'); window.dispatchEvent(new Event('discovery-input')); };
      $('discovery-references').append(chip);
    }
  }
  function addReference(item) {
    try { intent.add({ id: item.id, display_name: String(item.display_name || item.id).slice(0, 300) }); rememberReferences(); renderReferences(); searchGeneration++; searchController?.abort(); $('reference-results').replaceChildren(); $('reference-query').value = ''; notice('Reference added. Search to update results.'); window.dispatchEvent(new Event('discovery-input')); }
    catch (error) { notice(error.message); }
  }
  function pathView(path) {
    if (/^\/researchers\/A\d{1,20}$/.test(path)) return 'profile';
    return ({ '/': 'discover', '/discover': 'discover', '/connections': 'connections', '/saved': 'saved' })[path] || 'discover';
  }
  function navigate(path, { replace = false, context = null } = {}) {
    if (!/^\/(discover|connections|saved|researchers\/A\d{1,20})([?#].*)?$/.test(path)) return;
    scrolls.set(route, $('app-main').scrollTop);
    focusTargets.set(route, document.activeElement);
    const from = window.location.pathname + window.location.search;
    history[replace ? 'replaceState' : 'pushState']({ from, appNavigation: true }, '', path);
    profileContext = context;
    renderRoute(true);
  }
  function renderRoute(focus = false) {
    const previous = route;
    route = window.location.pathname + window.location.search;
    const view = pathView(window.location.pathname);
    if (view !== 'connections') graph?.dismissSearch?.();
    document.body.dataset.view = view;
    $('use-graph-references').hidden = !(graph?.origins() || []).some(item => item.id.startsWith('A'));
    if (previous && previous !== route) { notice(''); $('app-share-fallback').hidden = true; }
    for (const name of ['discover', 'connections', 'saved', 'profile']) {
      const el = $(name + '-view');
      el.hidden = name !== view;
      el.inert = name !== view;
    }
    document.querySelectorAll('[data-nav]').forEach(link => {
      const active = link.dataset.nav === view;
      if (active) link.setAttribute('aria-current', 'page'); else link.removeAttribute('aria-current');
    });
    document.title = `${({ discover: 'Discover researchers', connections: 'Connections', saved: 'Saved', profile: 'Researcher' })[view]} · Coauthor Distance`;
    $('app-main').scrollTop = scrolls.get(route) || 0;
    if (view === 'connections') requestAnimationFrame(() => graph?.resize());
    if (view === 'profile') { if (previous !== route || !currentProfile) loadProfile(window.location.pathname.split('/').pop()); }
    else { profileRequest += 1; profileController?.abort(); papersController?.abort(); }
    if (focus) $(view + '-heading')?.focus({ preventScroll: true });
    else if (focusTargets.get(route)?.isConnected) focusTargets.get(route).focus({ preventScroll: true });
    window.dispatchEvent(new CustomEvent('research-route', { detail: { view, path: route } }));
  }
  function openProfile(id, context = null) {
    if (!/^A\d{1,20}$/.test(id)) return;
    navigate('/researchers/' + id, { context });
  }
  async function loadProfile(id) {
    const version = ++profileRequest;
    profileController?.abort();
    profileController = new AbortController();
    currentProfile = null;
    currentPapers = [];
    papersController?.abort();
    $('profile-heading').textContent = 'Loading researcher…';
    $('profile-body').replaceChildren();
    const controller = profileController;
    const timeout = setTimeout(() => controller.abort(), 20000);
    try {
      const profile = await request('/api/researchers/' + id, { signal: profileController.signal });
      if (version !== profileRequest) return;
      currentProfile = profile;
      $('profile-heading').textContent = profile.display_name;
      document.title = `${profile.display_name} · Coauthor Distance`;
      const affiliations = profile.affiliations?.length ? profile.affiliations : [profile.affiliation_evidence].filter(Boolean);
      const affiliationHTML = affiliations.length ? `<p class="evidence-label">${affiliations.every(e => e.verified_current) ? 'Current affiliation verified' : 'OpenAlex last-known affiliations'}</p>` + affiliations.map(evidence => {
        const source = safeURL(evidence.source_url || evidence.openalex_url), name = esc(evidence.display_name || profile.institution);
        return `<p class="profile-affiliation">${source ? `<a href="${esc(source)}" target="_blank" rel="noopener">${name}</a>` : name}</p>`;
      }).join('') : '<p class="profile-affiliation">Affiliation not listed</p>';
      const orcid = safeURL(profile.orcid);
      $('profile-body').innerHTML = `<div class="profile-affiliations">${affiliationHTML}</div>
        <div class="topic-list">${(profile.topics || []).map(t => `<span>${esc(t.display_name)}</span>`).join('')}</div>
        <div class="profile-actions"><button id="profile-explore" type="button">Explore connections</button><button id="profile-related" type="button">Find related researchers</button><span id="profile-save-slot"></span></div>
        <div class="source-links"><a href="https://openalex.org/${esc(id)}" target="_blank" rel="noopener">OpenAlex ↗</a>${orcid ? `<a href="${esc(orcid)}" target="_blank" rel="noopener">ORCID ↗</a>` : ''}<button type="button" id="profile-share">Share profile</button></div>
        <section id="profile-context"></section><section><h2>Publications</h2><label for="profile-paper-sort">Order <select id="profile-paper-sort"><option value="recent">Recent</option><option value="cited">Most cited</option></select></label><div id="profile-papers" aria-live="polite">Loading publications…</div></section>`;
      $('profile-explore').onclick = () => explore(profile);
      $('profile-related').onclick = () => { addReference(profile); navigate('/discover'); };
      $('profile-share').onclick = () => share(location.origin + '/researchers/' + id);
      if (profileContext?.matching_topics?.length) {
        $('profile-context').innerHTML = '<h2>Why this researcher appeared</h2><p>' + profileContext.matching_topics.map(t => esc(t.display_name)).join(' · ') + '</p><ul class="publication-list">' + (profileContext.works || []).map(p => `<li><a href="https://openalex.org/${esc(p.id)}" target="_blank" rel="noopener">${esc(p.title)}</a> ${esc(p.publication_year || '')}</li>`).join('') + '</ul>';
      }
      if (profileContext?.steps?.length) {
        $('profile-context').innerHTML = '<h2>Connection evidence</h2>' + evidenceHTML(profileContext.steps);
      }
      window.dispatchEvent(new CustomEvent('research-profile', { detail: profile }));
      $('profile-paper-sort').onchange = () => loadPapers(id, version);
      await loadPapers(id, version);
    } catch (error) {
      if (version !== profileRequest) return;
      $('profile-heading').textContent = 'Researcher unavailable';
      $('profile-body').innerHTML = `<p role="alert">${esc(error.name === 'AbortError' ? 'The request timed out. Try again.' : error.message)}</p><button type="button" id="profile-retry">Retry</button>`;
      $('profile-retry').onclick = () => loadProfile(id);
    } finally { clearTimeout(timeout); }
  }
  function evidenceHTML(steps) {
    return `<ol class="publication-list">${steps.map(step => {
      const url = safeURL(step.work_url) || (/^W\d+$/.test(step.work_id) ? 'https://openalex.org/' + step.work_id : '');
      return `<li><p>${esc(step.from_name)} → ${esc(step.to_name)}</p>${url ? `<a href="${esc(url)}" target="_blank" rel="noopener">${esc(step.title || step.label)}</a>` : esc(step.title || step.label)}</li>`;
    }).join('')}</ol>`;
  }
  async function loadPapers(id, version) {
    papersController?.abort();
    const abort = papersController = new AbortController(), generation = ++papersGeneration;
    const timeout = setTimeout(() => abort.abort(), 15000);
    $('profile-papers').textContent = 'Loading publications…';
    try {
      const papers = await request('/api/authors/' + id + '/works?' + new URLSearchParams({ limit: '10', sort: $('profile-paper-sort').value }), { signal: abort.signal });
      if (version !== profileRequest || generation !== papersGeneration) return;
      currentPapers = papers;
      $('profile-papers').innerHTML = papers.length ? `<ul class="publication-list">${papers.map(p => `<li><a href="${esc(safeURL(p.doi) || 'https://openalex.org/' + p.id)}" target="_blank" rel="noopener">${esc(p.title)}</a><span class="publication-year">${esc(p.publication_year || '')}</span><span data-save-paper="${esc(p.id)}"></span></li>`).join('')}</ul>` : '<p>No publications listed.</p>';
      window.dispatchEvent(new CustomEvent('research-papers', { detail: papers }));
    } catch (error) {
      if (version !== profileRequest || generation !== papersGeneration) return;
      $('profile-papers').innerHTML = `<p>${esc(error.name === 'AbortError' ? 'Publication request timed out.' : error.message)}</p><button type="button" id="papers-retry">Retry publications</button>`;
      $('papers-retry').onclick = () => loadPapers(id, version);
    } finally { clearTimeout(timeout); }
  }
  async function explore(profile) {
    if (!graph) { notice('The graph is still loading. Try again shortly.'); return; }
    if (graph.busy()) { notice('Wait for the current graph search, or stop it in Connections.'); return; }
    navigate('/connections');
    await graph.add(profile);
  }
  async function share(url) {
    shareReturnFocus = document.activeElement;
    try { await navigator.clipboard.writeText(url); notice('Link copied.'); }
    catch { $('app-share-link').value = url; $('app-share-fallback').hidden = false; $('app-share-link').focus(); $('app-share-link').select(); }
  }
  async function searchReferences(event) {
    event.preventDefault();
    const query = $('reference-query').value.trim();
    if (query.length < 2) { $('reference-query').focus(); return; }
    searchController?.abort();
    searchController = new AbortController();
    const generation = ++searchGeneration;
    $('reference-results').innerHTML = '<li>Searching…</li>';
    const controller = searchController;
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const kind = $('reference-kind').value;
      const data = await request('/api/' + kind + '?' + new URLSearchParams({ q: query, per_page: '8' }), { signal: searchController.signal });
      if (generation !== searchGeneration) return;
      $('reference-results').replaceChildren();
      for (const item of data.results) {
        const row = document.createElement('li');
        const name = item.display_name || item.title;
        const label = kind === 'topics' ? 'topic' : kind === 'works' ? 'paper' : 'researcher';
        const title = kind === 'authors' ? `<a href="/researchers/${esc(item.id)}" data-researcher="${esc(item.id)}">${esc(name)}</a>` : `<strong>${esc(name)}</strong>`;
        const detail = kind === 'topics' ? [item.subfield?.display_name, item.field?.display_name].filter(Boolean).join(' · ') : kind === 'works' ? [item.publication_year, ...(item.author_names || []).slice(0, 3)].filter(Boolean).join(' · ') : item.institution;
        row.innerHTML = `<div>${title}<p>${esc(detail || '')}</p></div><button type="button">Use ${label}</button>`;
        row.querySelector('button').onclick = () => { addReference({ id: item.id, display_name: name }); $('reference-query').focus(); };
        $('reference-results').append(row);
      }
      if (!data.results.length) $('reference-results').innerHTML = `<li>${esc(data.message || 'No matches. Try another name.')}</li>`;
    } catch (error) {
      if (generation === searchGeneration) $('reference-results').innerHTML = `<li role="alert">${esc(error.name === 'AbortError' ? 'Search timed out. Try again.' : error.message)}</li>`;
    } finally { clearTimeout(timeout); }
  }
  function closeShare() { $('app-share-fallback').hidden = true; shareReturnFocus?.focus(); }
  $('app-share-close').onclick = closeShare;
  $('app-share-fallback').addEventListener('keydown', event => { if (event.key === 'Escape') closeShare(); });
  $('reference-form').addEventListener('submit', searchReferences);
  $('reference-kind').addEventListener('change', () => {
    searchGeneration++; searchController?.abort(); $('reference-results').replaceChildren();
    $('reference-query').placeholder = ({ topics: 'Search a topic, e.g. robotics', works: 'Search a paper title', authors: 'Search a researcher' })[$('reference-kind').value];
    $('reference-query').focus();
  });
  $('reference-query').addEventListener('input', () => { searchGeneration++; searchController?.abort(); $('reference-results').replaceChildren(); });
  $('use-graph-references').onclick = () => { for (const item of graph?.origins() || []) if (item.id.startsWith('A')) addReference(item); };
  $('profile-back').onclick = () => { if (history.state?.appNavigation) history.back(); else navigate('/discover', { replace: true }); };
  document.addEventListener('click', event => {
    const link = event.target.closest('a[data-nav], a[data-researcher]');
    if (!link || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return;
    event.preventDefault();
    if (link.dataset.researcher) openProfile(link.dataset.researcher); else navigate(link.getAttribute('href'));
  });
  window.addEventListener('popstate', () => { scrolls.set(route, $('app-main').scrollTop); profileContext = null; renderRoute(); });
  window.ResearchApp = { references: intent.list, intent, addReference,
    replaceReferences(values) { intent.replace(values.map(value => ({ id: value.id, display_name: String(value.display_name || value.id).slice(0, 300) }))); rememberReferences(); renderReferences(); }, openProfile, navigate, explore, request, share, notice, esc, safeURL, evidenceHTML,
    profile: () => currentProfile, papers: () => currentPapers.slice(), graph: () => graph,
    graphReady(value) { graph = value; requestAnimationFrame(() => { $('use-graph-references').hidden = !graph.origins().some(item => item.id.startsWith('A')); if (pathView(window.location.pathname) === 'connections') graph.resize(); }); },
  };
  if (location.hash.startsWith('#map=')) history.replaceState(null, '', '/connections' + location.hash);
  renderReferences();
  renderRoute();
})();
