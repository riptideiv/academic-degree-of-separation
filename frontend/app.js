(function () {
  const API_BASE = window.RESEARCHER_API_BASE ?? '';

  // ── State ──────────────────────────────────────────────────────────────────
  const state = {
    origins: new Set(),
    pathNodes: new Set(),
    authorCache: new Map(),
    // Found connections, keyed by an order-independent pair key (see pairKey).
    // Accumulates across expansions: each add only computes paths from the new
    // researcher to existing ones, so earlier pairs must persist here.
    paths: new Map(),
    activeSource: null,
    isLoading: false,
    // When false (default), only the researchers of interest + the connecting
    // path are labeled; expansion ("neighborhood") names show on hover only.
    showNames: false,
  };

  // Base per-type edge opacity (multiplied by a distance factor in applyEdgeFade).
  const EDGE_TYPE_OPACITY = { coauthor: 0.65, citation: 0.5, institution: 0.4, authorship: 0.6 };
  const OPENALEX_KEY_STORAGE = 'researcherOpenAlexKey';
  const RANK_TIMEOUT_MS = 30000;

  // OpenAlex IDs are prefix-typed: works start with 'W', authors with 'A'.
  function isWorkId(id) {
    return id.startsWith('W');
  }

  // ── Tooltip ────────────────────────────────────────────────────────────────
  const tooltip = document.createElement('div');
  tooltip.id = 'cy-tooltip';
  document.body.appendChild(tooltip);

  let mouseX = 0, mouseY = 0;
  document.addEventListener('mousemove', e => {
    mouseX = e.clientX;
    mouseY = e.clientY;
    if (tooltip.style.display !== 'none') {
      positionTooltip();
    }
  });

  function positionTooltip() {
    const pad = 14;
    const w = tooltip.offsetWidth, h = tooltip.offsetHeight;
    const vw = window.innerWidth, vh = window.innerHeight;
    tooltip.style.left = (mouseX + pad + w > vw ? mouseX - w - pad : mouseX + pad) + 'px';
    tooltip.style.top = (mouseY + pad + h > vh ? mouseY - h - pad : mouseY + pad) + 'px';
  }

  function showTooltip(html) {
    tooltip.innerHTML = html;
    tooltip.style.display = 'block';
    positionTooltip();
  }

  function hideTooltip() {
    tooltip.style.display = 'none';
  }

  // ── Color helpers ──────────────────────────────────────────────────────────
  // Compute visual style for a single expansion node given its works/citation counts
  // and the min/max range across all expansion nodes currently in the graph.
  // tW drives shade + size (more papers = darker + larger).
  // tC drives z-index (more cited = renders on top).
  // Size range: 6px (least papers) → 102px (most papers = 3× origin node of 34px).
  function expansionStyleRelative(worksCount, citedByCount, minW, maxW, minC, maxC) {
    const tW = maxW > minW ? (worksCount - minW) / (maxW - minW) : 0.5;
    const tC = maxC > minC ? (citedByCount - minC) / (maxC - minC) : 0.5;
    const light = 210, dark = 48;
    const v = Math.round(light - tW * (light - dark));
    const hex = v.toString(16).padStart(2, '0');
    const bgColor = `#${hex}${hex}${hex}`;
    const nodeSize = Math.round(6 + tW * 96); // 6 → 102 (3× origin)
    return {
      bgColor,
      fontColor: bgColor,
      nodeSize,
      // Label scales with the node so big (prolific) nodes get big names: 7 → 20px.
      fontSize: Math.round(7 + tW * 13),
      zIdx: Math.max(1, Math.min(90, Math.round(tC * 90))),
    };
  }

  // Rescale all expansion nodes relative to each other (runs on SSE done).
  function rescaleExpansionNodes() {
    const nodes = cy.nodes('[type="expansion"]');
    if (!nodes.length) return;
    let minW = Infinity, maxW = -Infinity, minC = Infinity, maxC = -Infinity;
    nodes.forEach(n => {
      const w = n.data('works_count') || 0, c = n.data('cited_by_count') || 0;
      if (w < minW) minW = w; if (w > maxW) maxW = w;
      if (c < minC) minC = c; if (c > maxC) maxC = c;
    });
    nodes.forEach(n => {
      const s = expansionStyleRelative(
        n.data('works_count') || 0, n.data('cited_by_count') || 0,
        minW, maxW, minC, maxC,
      );
      n.data({
        bgColor: s.bgColor, fontColor: s.fontColor,
        nodeSize: s.nodeSize, fontSize: s.fontSize, zIdx: s.zIdx,
      });
    });
  }

  // ── Cytoscape init ─────────────────────────────────────────────────────────
  // Register the fCoSE layout extension (loaded via CDN) once.
  if (window.cytoscapeFcose && !cytoscape.__fcoseRegistered) {
    cytoscape.use(window.cytoscapeFcose);
    cytoscape.__fcoseRegistered = true;
  }

  const cy = cytoscape({
    container: document.getElementById('cy'),
    elements: [],
    style: [
      {
        selector: 'node',
        style: {
          label: 'data(name)',
          'text-valign': 'bottom',
          'text-halign': 'center',
          'font-size': '9px',
          'font-family': 'system-ui, sans-serif',
          color: '#777',
          'background-color': '#ddd',
          width: 10,
          height: 10,
          'text-max-width': '140px',
          'text-wrap': 'ellipsis',
          'text-overflow-wrap': 'anywhere',
          'text-margin-y': 3,
          // White halo so labels stay legible over edges/other labels.
          'text-background-color': '#fafafa',
          'text-background-opacity': 0.85,
          'text-background-shape': 'round-rectangle',
          'text-background-padding': '2px',
          'z-index': 0,
        },
      },
      // Origin: orange
      {
        selector: 'node[type="origin"]',
        style: {
          'background-color': '#f5821e',
          width: 34,
          height: 34,
          color: '#7a3d00',
          'font-weight': '600',
          'font-size': '11px',
          'z-index': 200,
        },
      },
      // Path: light blue
      {
        selector: 'node[type="path"]',
        style: {
          'background-color': '#5badd9',
          width: 22,
          height: 22,
          color: '#1a4f70',
          'font-size': '10px',
          'z-index': 100,
        },
      },
      // Work: teal diamond, same prominence class as an origin
      {
        selector: 'node[type="work"]',
        style: {
          shape: 'diamond',
          'background-color': '#2e86ab',
          width: 32,
          height: 32,
          color: '#0d3a52',
          'font-weight': '600',
          'font-size': '11px',
          'z-index': 200,
        },
      },
      // Expansion: size/shade driven by works/citations; whole-node opacity fades
      // with distance (data(op)) so the far periphery recedes. Labels are hidden
      // by default and revealed in bulk by the "Show all names" toggle (.shown).
      {
        selector: 'node[type="expansion"]',
        style: {
          'background-color': 'data(bgColor)',
          color: 'data(fontColor)',
          width: 'data(nodeSize)',
          height: 'data(nodeSize)',
          'font-size': 'data(fontSize)',
          opacity: 'data(op)',
          'text-opacity': 0,
          'z-index': 'data(zIdx)',
        },
      },
      {
        selector: 'node[type="expansion"].shown',
        style: { 'text-opacity': 1 },
      },
      // Hovered node: full opacity, label shown, rendered on top. Listed last so
      // it wins over the rules above.
      {
        selector: 'node.hl',
        style: { 'text-opacity': 1, opacity: 1, 'z-index': 300 },
      },
      {
        selector: 'node:selected',
        style: { 'border-width': 2, 'border-color': '#222' },
      },
      // One shared dark color for researcher-to-researcher edges; the dash
      // pattern (not color) says which type an edge is: solid = co-author,
      // long dash = citation, short dash = institution. Authorship (work →
      // author) edges keep their own green so they read apart from co-author
      // edges. Depth-based opacity still fades the periphery.
      {
        selector: 'edge',
        style: {
          'line-color': '#3a3a3a',
          width: 1,
          'curve-style': 'bezier',
          opacity: 'data(eop)',   // per-type base × distance factor (applyEdgeFade)
        },
      },
      {
        selector: 'edge[type="coauthor"]',
        style: { width: 1.5 },
      },
      {
        selector: 'edge[type="citation"]',
        style: { 'line-style': 'dashed', 'line-dash-pattern': [8, 5] },
      },
      // Arrowhead always points at whoever was cited; direction is computed
      // backend-side (incoming/outgoing/mutual) so no merge logic is needed here.
      {
        selector: 'edge[type="citation"][direction="outgoing"]',
        style: { 'target-arrow-shape': 'triangle', 'target-arrow-color': '#3a3a3a' },
      },
      {
        selector: 'edge[type="citation"][direction="incoming"]',
        style: { 'source-arrow-shape': 'triangle', 'source-arrow-color': '#3a3a3a' },
      },
      {
        selector: 'edge[type="citation"][direction="mutual"]',
        style: {
          'source-arrow-shape': 'triangle', 'source-arrow-color': '#3a3a3a',
          'target-arrow-shape': 'triangle', 'target-arrow-color': '#3a3a3a',
        },
      },
      {
        selector: 'edge[type="institution"]',
        style: { 'line-style': 'dashed', 'line-dash-pattern': [2, 5] },
      },
      {
        selector: 'edge[type="authorship"]',
        style: { width: 1.5, 'line-color': '#7fae8e' },
      },
    ],
    layout: { name: 'preset' },
    userZoomingEnabled: true,
    userPanningEnabled: true,
  });

  // ── Hover tooltips ─────────────────────────────────────────────────────────
  cy.on('mouseover', 'node', function (evt) {
    evt.target.addClass('hl');
    const d = evt.target.data();
    const lines = [`<strong>${escHtml(d.name)}</strong>`];
    if (d.type === 'work') {
      if (d.publication_year) lines.push(String(d.publication_year));
      lines.push(`${(d.cited_by_count || 0).toLocaleString()} citations`);
    } else {
      if (d.institution) lines.push(escHtml(d.institution));
      lines.push(`${(d.works_count || 0).toLocaleString()} works · ${(d.cited_by_count || 0).toLocaleString()} citations`);
    }
    showTooltip(lines.join('<br>'));
  });
  cy.on('mouseout', 'node', function (evt) {
    evt.target.removeClass('hl');
    hideTooltip();
  });

  cy.on('mouseover', 'edge', function (evt) {
    const d = evt.target.data();
    const typeLabel = {
      coauthor: 'Co-authorship',
      citation: 'Citation',
      institution: 'Institution',
      authorship: 'Author of work',
    }[d.type] || d.type;
    const lines = [`<em>${typeLabel}</em>`];
    if (d.type === 'citation' && d.direction) {
      const srcName = evt.target.source().data('name');
      const tgtName = evt.target.target().data('name');
      if (d.direction === 'mutual') lines.push(`${escHtml(srcName)} and ${escHtml(tgtName)} cited each other`);
      else if (d.direction === 'outgoing') lines.push(`${escHtml(srcName)} cited ${escHtml(tgtName)}`);
      else lines.push(`${escHtml(tgtName)} cited ${escHtml(srcName)}`);
    }
    if (d.label) lines.push(`"${escHtml(d.label)}"`);
    showTooltip(lines.join('<br>'));
  });
  cy.on('mouseout', 'edge', hideTooltip);

  // ── Node click → sidebar detail ───────────────────────────────────────────
  cy.on('tap', 'node', function (evt) {
    hideTooltip();
    const data = evt.target.data();
    document.getElementById('detail-name').textContent = data.name;
    document.getElementById('detail-meta').innerHTML = data.type === 'work'
      ? [
        data.publication_year ? `<div>${escHtml(String(data.publication_year))}</div>` : '',
        `<div>${(data.cited_by_count || 0).toLocaleString()} citations</div>`,
      ].join('')
      : [
        data.institution ? `<div>${escHtml(data.institution)}</div>` : '',
        `<div>${(data.works_count || 0).toLocaleString()} works · ${(data.cited_by_count || 0).toLocaleString()} citations</div>`,
      ].join('');
    document.getElementById('detail-link').href = `https://openalex.org/${data.id}`;
    document.getElementById('node-detail').classList.remove('hidden');
  });

  cy.on('tap', function (evt) {
    if (evt.target === cy) {
      document.getElementById('node-detail').classList.add('hidden');
    }
  });

  // ── Search ─────────────────────────────────────────────────────────────────
  const searchInput = document.getElementById('search-input');
  const searchBtn = document.getElementById('search-btn');
  const workSearchInput = document.getElementById('work-search-input');
  const workSearchBtn = document.getElementById('work-search-btn');
  const rankSearchForm = document.getElementById('rank-search-form');
  const rankInstitutionInput = document.getElementById('rank-institution-input');
  const rankInstitutionSearch = document.getElementById('rank-institution-search');
  const rankTargetInput = document.getElementById('rank-target-input');
  const rankTargetSearch = document.getElementById('rank-target-search');
  const rankPrimaryOnly = document.getElementById('rank-primary-only');
  const openAlexKeyInput = document.getElementById('openalex-key-input');
  const openAlexKeySave = document.getElementById('openalex-key-save');
  const openAlexKeyStatus = document.getElementById('openalex-key-status');
  const rankSelection = {
    institution: null,
    target: null,
  };

  // State for the currently open (or last opened) search modal session. Page and
  // per-author-top-papers results are cached here so revisiting a page, or
  // re-expanding an author, after closing/reopening the modal costs no extra
  // network calls. The cache is cleared only when the user commits to an item
  // via "Add" — see onAddFromModal — or when the query/entity type changes.
  const searchSession = {
    entityType: 'author',   // 'author' | 'work' | 'rank-institution' | 'rank-target'
    query: '',
    pageCache: new Map(),    // page number -> PaginatedAuthors/PaginatedWorks response
    topWorksCache: new Map(), // author id -> AuthorWork[] (author-only "top papers" panel)
    currentPage: 1,
  };

  searchBtn.addEventListener('click', () => runSearch('author', searchInput));
  searchInput.addEventListener('keydown', e => {
    if (e.key === 'Enter') runSearch('author', searchInput);
  });
  workSearchBtn.addEventListener('click', () => runSearch('work', workSearchInput));
  workSearchInput.addEventListener('keydown', e => {
    if (e.key === 'Enter') runSearch('work', workSearchInput);
  });
  rankInstitutionSearch?.addEventListener('click', () => runSearch('rank-institution', rankInstitutionInput));
  rankTargetSearch?.addEventListener('click', () => runSearch('rank-target', rankTargetInput));
  openAlexKeySave?.addEventListener('click', saveOpenAlexKey);
  openAlexKeyInput?.addEventListener('keydown', e => {
    if (e.key === 'Enter') {
      e.preventDefault();
      saveOpenAlexKey();
    }
  });
  configureStoredOpenAlexKey();
  rankSearchForm?.addEventListener('submit', e => {
    e.preventDefault();
    runInstitutionRank();
  });
  [rankInstitutionInput, rankTargetInput].forEach(input => {
    input?.addEventListener('keydown', e => {
      if (e.key !== 'Enter') return;
      e.preventDefault();
      runSearch(input === rankInstitutionInput ? 'rank-institution' : 'rank-target', input);
    });
  });
  rankInstitutionInput?.addEventListener('input', () => {
    if (rankSelection.institution?.display_name !== rankInstitutionInput.value.trim()) {
      rankSelection.institution = null;
      renderRankResults([]);
      renderRankSelectionStatus();
    }
  });
  rankTargetInput?.addEventListener('input', () => {
    if (rankSelection.target?.display_name !== rankTargetInput.value.trim()) {
      rankSelection.target = null;
      renderRankResults([]);
      renderRankSelectionStatus();
    }
  });

  async function configureStoredOpenAlexKey() {
    const saved = localStorage.getItem(OPENALEX_KEY_STORAGE);
    if (saved && openAlexKeyInput) {
      openAlexKeyInput.value = saved;
      await sendOpenAlexKey(saved, false);
      return;
    }
    try {
      const r = await fetch(`${API_BASE}/api/openalex-key`);
      if (!r.ok) return;
      const data = await r.json();
      setOpenAlexKeyStatus(data.configured ? 'API key active' : 'Add API key for live search');
    } catch {
      setOpenAlexKeyStatus('');
    }
  }

  async function saveOpenAlexKey() {
    const key = openAlexKeyInput?.value.trim() || '';
    if (!key) {
      setOpenAlexKeyStatus('Paste your OpenAlex API key');
      openAlexKeyInput?.focus();
      return;
    }
    localStorage.setItem(OPENALEX_KEY_STORAGE, key);
    await sendOpenAlexKey(key, true);
  }

  async function sendOpenAlexKey(key, showSaved) {
    setOpenAlexKeyStatus('Checking key…');
    try {
      const r = await fetch(`${API_BASE}/api/openalex-key`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ api_key: key }),
      });
      if (!r.ok) throw new Error('key failed');
      const data = await r.json();
      if (!data.configured) throw new Error('key missing');
      searchSession.pageCache.clear();
      setOpenAlexKeyStatus(showSaved ? 'API key saved' : 'API key active');
    } catch {
      setOpenAlexKeyStatus('Could not save API key');
    }
  }

  function setOpenAlexKeyStatus(message) {
    if (openAlexKeyStatus) openAlexKeyStatus.textContent = message || '';
  }

  function runSearch(entityType, inputEl) {
    const q = inputEl.value.trim();
    if (q.length < 2) return;
    if (q !== searchSession.query || entityType !== searchSession.entityType) {
      searchSession.pageCache.clear();
      searchSession.topWorksCache.clear();
      searchSession.query = q;
      searchSession.entityType = entityType;
    }
    const title = document.getElementById('search-title');
    if (title) {
      title.textContent = {
        work: 'Search results — works',
        'rank-institution': 'Search results — institutions',
        'rank-target': 'Search results — target academic',
        author: 'Search results — researchers',
      }[entityType] || 'Search results';
    }
    openSearchModal();
    loadPage(1);
  }

  // Edge-type checkboxes, work edge-type checkboxes, and neighborhood size all
  // affect the search, but are staged rather than applied immediately — the
  // user commits them via the "Apply options" button below.
  document.getElementById('apply-options')?.addEventListener('click', () => {
    if (state.origins.size) scheduleRebuild();
  });

  // "Show all names" is a pure view toggle, so no refetch is needed — save immediately.
  document.getElementById('toggle-names')?.addEventListener('change', e => {
    state.showNames = e.target.checked;
    applyNameVisibility();
    saveState();
  });

  // Layout sliders just re-run the layout (no refetch). 'change' fires on release — save immediately.
  ['layout-spacing', 'layout-link'].forEach(id => {
    document.getElementById(id)?.addEventListener('change', () => {
      if (cy.nodes().length) runLayout();
      saveState();
    });
  });

  document.getElementById('restore-settings')?.addEventListener('click', () => {
    let saved;
    try { saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}'); } catch { return; }
    if (!saved.settings) return;
    const prev = collectSettings();
    applySettings(saved.settings);
    applyNameVisibility();
    applyEdgeFade();
    // Rebuild if graph-affecting settings changed
    const graphChanged = [
      'edgeCoauthor', 'edgeCitation', 'edgeInstitution',
      'workEdgeAuthorship', 'workEdgeCitation', 'neighborhood',
    ].some(k => String(prev[k]) !== String(saved.settings[k]));
    if (graphChanged && state.origins.size) scheduleRebuild();
    else if (state.origins.size) runLayout();
  });

  document.getElementById('restore-default-layout')?.addEventListener('click', () => {
    const el = id => document.getElementById(id);
    if (el('layout-spacing')) el('layout-spacing').value = DEFAULT_SETTINGS.layoutSpacing;
    if (el('layout-link')) el('layout-link').value = DEFAULT_SETTINGS.layoutLink;
    if (state.origins.size) runLayout();
  });

  document.getElementById('clear-canvas')?.addEventListener('click', async () => {
    if (state.isLoading) return;
    // Wipe client state
    cy.elements().remove();
    state.origins.clear();
    state.pathNodes.clear();
    state.paths.clear();
    state.authorCache.clear();
    document.getElementById('origin-chips').innerHTML = '';
    document.getElementById('node-detail').classList.add('hidden');
    renderDegrees();
    clearSavedState();
    // Wipe server-side neighbor cache so the next search re-fetches from OpenAlex
    try { await fetch(`${API_BASE}/api/cache`, { method: 'DELETE' }); } catch { /* ignore */ }
  });

  // ── Search modal (shared by both author and work search) ──────────────────
  async function fetchResultsPage(q, page) {
    const endpoint = searchSession.entityType === 'work'
      ? 'works'
      : searchSession.entityType === 'rank-institution'
        ? 'institutions'
        : 'authors';
    const r = await fetch(`${API_BASE}/api/${endpoint}?q=${encodeURIComponent(q)}&page=${page}&per_page=20`);
    if (!r.ok) throw new Error('search failed');
    return r.json();
  }

  async function loadPage(page) {
    searchSession.currentPage = page;
    if (searchSession.pageCache.has(page)) {
      renderResultsList(searchSession.pageCache.get(page));
      return;
    }
    renderSearchListMessage('Searching…');
    try {
      const data = await fetchResultsPage(searchSession.query, page);
      searchSession.pageCache.set(page, data);
      if (data.message && !(data.results || []).length) {
        renderSearchListMessage(data.message);
        return;
      }
      renderResultsList(data);
    } catch {
      renderSearchListMessage('Search failed. Please try again.');
    }
  }

  function renderSearchListMessage(msg) {
    const list = document.getElementById('search-results-list');
    list.innerHTML = `<li class="empty-state">${escHtml(msg)}</li>`;
    document.getElementById('search-pagination').innerHTML = '';
  }

  function renderResultsList(data) {
    const list = document.getElementById('search-results-list');
    list.innerHTML = '';
    const isWork = searchSession.entityType === 'work';
    const isRankInstitution = searchSession.entityType === 'rank-institution';
    const isRankTarget = searchSession.entityType === 'rank-target';
    const candidates = (isRankInstitution || isRankTarget)
      ? data.results
      : data.results.filter(item => !state.origins.has(item.id));
    if (!candidates.length) {
      list.innerHTML = '<li class="empty-state">No results.</li>';
    }
    for (const item of candidates) {
      const li = document.createElement('li');
      li.className = 'result-row';
      li.dataset.id = item.id;
      const infoHtml = isRankInstitution
        ? `<strong>${escHtml(item.display_name)}</strong><br>` +
        `<small>${escHtml(item.country_code || 'Unknown country')} · ` +
        `${(item.works_count || 0).toLocaleString()} works · ${(item.cited_by_count || 0).toLocaleString()} citations</small>`
        : isWork
        ? `<strong>${escHtml(item.title)}</strong><br>` +
        `<small>${escHtml(item.author_names.join(', ') || 'Unknown authors')} · ` +
        `${item.publication_year || '—'} · ${item.cited_by_count.toLocaleString()} citations</small>`
        : `<strong>${escHtml(item.display_name)}</strong><br>` +
        `<small>${escHtml(item.institution || 'Unknown institution')} · ` +
        `${item.works_count.toLocaleString()} works · ${item.cited_by_count.toLocaleString()} citations</small>`;
      const arrowHtml = (isWork || isRankInstitution) ? '' : '<span class="result-arrow">&#9660;</span> ';
      li.innerHTML =
        `<div class="result-row-main">` +
        `<div class="result-row-info">${arrowHtml}${infoHtml}</div>` +
        `<button type="button" class="add-btn-inline">${isRankInstitution || isRankTarget ? 'Select' : 'Add'}</button>` +
        `</div>`;
      li.querySelector('.add-btn-inline').addEventListener('click', e => {
        e.stopPropagation();
        onAddFromModal(item);
      });
      // Works show everything (incl. authors) directly on the tile already, so
      // there's no expand-in-place panel for them — only authors get one (top papers).
      if (!isWork && !isRankInstitution) {
        li.querySelector('.result-row-info').addEventListener('click', () => toggleAuthorExpand(item, li));
      }
      list.appendChild(li);
    }
    renderPagination(data.page, data.total_pages);
  }

  async function toggleAuthorExpand(author, li) {
    const arrow = li.querySelector('.result-arrow');
    const existingDetail = li.nextElementSibling;
    if (existingDetail && existingDetail.classList.contains('result-detail')) {
      existingDetail.remove();
      arrow?.classList.remove('expanded');
      return;
    }
    document.querySelectorAll('#search-results-list .result-detail').forEach(d => d.remove());
    document.querySelectorAll('#search-results-list .result-arrow.expanded').forEach(a => a.classList.remove('expanded'));

    const detail = document.createElement('li');
    detail.className = 'result-detail';
    detail.innerHTML = '<em>Loading top papers…</em>';
    li.after(detail);
    arrow?.classList.add('expanded');

    const works = await loadTopWorks(author.id);
    detail.innerHTML = works.length ? renderWorksTable(works) : '<em>No works found.</em>';
  }

  // expandable degree panel cuz number of edges grows quickly
  async function toggleDegreeExpand(degree, el) {
    const arrow = el.querySelector('.degrees-arrow');
    const existingDetail = el.nextElementSibling;
    if (existingDetail && existingDetail.classList.contains('degrees-steps')) {
      existingDetail.remove();
      arrow?.classList.remove('expanded');
      return;
    }

    const detail = document.createElement('ol');
    detail.className = 'degrees-steps';
    detail.innerHTML = '';
    for (const s of degree.steps) {
      detail.innerHTML +=
        `<li><span class="step-people">${escHtml(s.from_name)} → ${escHtml(s.to_name)}</span>` +
        `<span class="step-via">${escHtml(stepPhrase(s))}</span></li>`;
    }
    el.after(detail);
    arrow?.classList.add('expanded');
  }

  async function toggleRankExpand(result, el) {
    const arrow = el.querySelector('.rank-arrow');
    const existingDetail = el.nextElementSibling;
    if (existingDetail && existingDetail.classList.contains('rank-steps')) {
      existingDetail.remove();
      arrow?.classList.remove('expanded');
      return;
    }

    const detail = document.createElement('ol');
    detail.className = 'rank-steps';
    const evidence = result.affiliation_evidence;
    if (result.author.openalex_url) {
      detail.innerHTML +=
        `<li><span class="step-people">Author profile</span>` +
        `<span class="step-via"><a href="${escAttr(result.author.openalex_url)}" target="_blank" rel="noopener">OpenAlex profile</a></span></li>`;
    }
    if (evidence) {
      const years = evidence.years?.length ? evidence.years.join(', ') : 'years not listed';
      const evidenceLabel = evidence.openalex_url
        ? `<a href="${escAttr(evidence.openalex_url)}" target="_blank" rel="noopener">${escHtml(evidence.display_name || result.matched_institution)}</a>`
        : escHtml(evidence.display_name || result.matched_institution);
      detail.innerHTML +=
        `<li><span class="step-people">Affiliation evidence</span>` +
        `<span class="step-via">${evidenceLabel} · ${escHtml(years)}</span></li>`;
    }
    if (!result.steps.length) {
      detail.innerHTML += '<li>No path details available.</li>';
    } else {
      detail.innerHTML += result.steps.map(s =>
        `<li><span class="step-people">${escHtml(s.from_name)} → ${escHtml(s.to_name)}</span>` +
        `<span class="step-via">${escHtml(stepPhrase(s))}</span></li>`
      ).join('');
    }
    el.after(detail);
    arrow?.classList.add('expanded');
  }

  async function runInstitutionRank() {
    if (state.isLoading) return;
    if (!rankSelection.institution && !rankSelection.target) {
      setRankStatus('Search and select an institution and a target academic.');
      return;
    }
    if (!rankSelection.institution) {
      setRankStatus('Search and select an institution.');
      rankInstitutionInput?.focus();
      return;
    }
    if (!rankSelection.target) {
      setRankStatus('Search and select a target academic.');
      rankTargetInput?.focus();
      return;
    }

    setRankStatus('Ranking…');
    renderRankResults([]);
    try {
      const params = new URLSearchParams({
        institution: rankSelection.institution.display_name,
        target: rankSelection.target.display_name,
        institution_id: rankSelection.institution.id,
        target_id: rankSelection.target.id,
        limit: '15',
        candidate_pool: '15',
        primary_only: rankPrimaryOnly?.checked ? 'true' : 'false',
      });
      getEnabledEdges().forEach(e => params.append('edges', e));
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), RANK_TIMEOUT_MS);
      const r = await fetch(`${API_BASE}/api/institution-rank?${params}`, {
        signal: controller.signal,
      });
      clearTimeout(timeout);
      if (!r.ok) {
        let msg = 'Ranking failed. Please try again.';
        try {
          const body = await r.json();
          if (body.message) msg = body.message;
        } catch { /* ignore */ }
        throw new Error(msg);
      }
      const data = await r.json();
      if (data.message && !(data.results || []).length) {
        setRankStatus(data.message);
        return;
      }
      const inst = data.institution?.display_name || rankSelection.institution.display_name;
      const targetName = data.target?.display_name || rankSelection.target.display_name;
      const scope = data.primary_only ? 'current primary academics' : 'academics';
      const omitted = data.unconnected_count || 0;
      const omittedNote = omitted ? ` · ${omitted} candidate${omitted === 1 ? '' : 's'} omitted with no path found` : '';
      const cacheNote = data.message ? ' · using cached local data' : '';
      setRankStatus(`${inst} ${scope} closest to ${targetName}${omittedNote}${cacheNote}`);
      renderRankResults(data.results || []);
    } catch (err) {
      const timedOut = err?.name === 'AbortError';
      setRankStatus(timedOut
        ? 'Ranking did not finish within 30 seconds. Try fewer edge types or turn off current-primary filtering.'
        : (err?.message || 'Ranking failed. Please try again.'));
    }
  }

  function setRankStatus(message) {
    const status = document.getElementById('rank-status');
    if (status) status.textContent = message || '';
  }

  function renderRankSelectionStatus() {
    const parts = [];
    if (rankSelection.institution) parts.push(`Institution: ${rankSelection.institution.display_name}`);
    if (rankSelection.target) parts.push(`Target: ${rankSelection.target.display_name}`);
    setRankStatus(parts.join(' · '));
  }

  function rankLabel(result) {
    if (!result.found) return 'no path found';
    return `${result.hops} degree${result.hops === 1 ? '' : 's'}`;
  }

  function renderRankResults(results) {
    const list = document.getElementById('rank-results');
    if (!list) return;
    list.innerHTML = '';
    if (!results.length) return;
    results.forEach((result, index) => {
      const li = document.createElement('li');
      li.className = 'rank-result';
      const author = result.author;
      const matchedInstitution = result.matched_institution || author.institution || 'Selected institution';
      const primaryInstitution = author.institution || 'Unknown primary affiliation';
      const primaryNote = primaryInstitution && primaryInstitution !== matchedInstitution
        ? ` · Primary: ${primaryInstitution}`
        : '';
      li.innerHTML =
        `<div class="rank-row">` +
        `<span class="rank-num">${index + 1}</span>` +
        `<button type="button" class="rank-main">` +
        `<strong>${escHtml(author.display_name)}</strong>` +
        `<small>${escHtml(matchedInstitution)}${escHtml(primaryNote)} · ` +
        `${author.cited_by_count.toLocaleString()} citations</small>` +
        `</button>` +
        `<span class="rank-distance"><span class="rank-arrow">&#9660;</span> ${escHtml(rankLabel(result))}</span>` +
        `<button type="button" class="rank-graph-btn">Graph</button>` +
        `</div>`;
      li.querySelector('.rank-main').addEventListener('click', () => toggleRankExpand(result, li));
      li.querySelector('.rank-distance').addEventListener('click', () => toggleRankExpand(result, li));
      li.querySelector('.rank-graph-btn').addEventListener('click', () => graphRankResult(result));
      list.appendChild(li);
    });
  }

  async function graphRankResult(result) {
    if (state.isLoading || !rankSelection.target) return;
    const target = rankSelection.target;
    const author = result.author;
    if (!state.origins.has(target.id)) {
      await addResearcher({
        id: target.id,
        display_name: target.display_name,
        institution: target.institution,
        works_count: target.works_count || 0,
        cited_by_count: target.cited_by_count || 0,
      });
    }
    if (!state.origins.has(author.id)) {
      await addResearcher({
        id: author.id,
        display_name: author.display_name,
        institution: author.institution,
        works_count: author.works_count || 0,
        cited_by_count: author.cited_by_count || 0,
      });
    }
  }

  // Renders the top-papers table: numbered + sorted by citation count (the
  // backend already sorts this way; re-sorting here is a cheap safety net),
  // each title linked to its DOI when the API provided one.
  function renderWorksTable(works) {
    const sorted = [...works].sort((a, b) => b.cited_by_count - a.cited_by_count);
    const rows = sorted.map((w, i) => {
      const titleHtml = w.doi
        ? `<a href="${escAttr(w.doi)}" target="_blank" rel="noopener">${escHtml(w.title)}</a>`
        : escHtml(w.title);
      return `<tr><td class="rank">${i + 1}</td><td class="title">${titleHtml}</td>` +
        `<td class="citations">${w.cited_by_count.toLocaleString()}</td></tr>`;
    }).join('');
    return `<table class="works-table"><tbody>${rows}</tbody></table>`;
  }

  async function loadTopWorks(authorId) {
    if (searchSession.topWorksCache.has(authorId)) return searchSession.topWorksCache.get(authorId);
    try {
      const r = await fetch(`${API_BASE}/api/authors/${authorId}/works?limit=10`);
      const works = r.ok ? await r.json() : [];
      searchSession.topWorksCache.set(authorId, works);
      return works;
    } catch { return []; }
  }

  function onAddFromModal(item) {
    if (searchSession.entityType === 'rank-institution') {
      rankSelection.institution = item;
      rankInstitutionInput.value = item.display_name;
      renderRankSelectionStatus();
    } else if (searchSession.entityType === 'rank-target') {
      rankSelection.target = item;
      rankTargetInput.value = item.display_name;
      renderRankSelectionStatus();
    } else if (searchSession.entityType === 'work') addWork(item);
    else addResearcher(item);
    searchSession.pageCache.clear();
    searchSession.topWorksCache.clear();
    closeSearchModal();
  }

  // Windowed pagination: first, last, current, and current ± 2 neighbors, with a
  // single '…' standing in for any gap of 2 or more skipped pages.
  function paginationWindow(current, total) {
    const pages = new Set([1, total, current]);
    for (let d = 1; d <= 2; d++) {
      if (current - d >= 1) pages.add(current - d);
      if (current + d <= total) pages.add(current + d);
    }
    const sorted = [...pages].filter(p => p >= 1 && p <= total).sort((a, b) => a - b);
    const out = [];
    for (let i = 0; i < sorted.length; i++) {
      if (i > 0 && sorted[i] - sorted[i - 1] > 1) out.push('…');
      out.push(sorted[i]);
    }
    return out;
  }

  function renderPagination(current, total) {
    const bar = document.getElementById('search-pagination');
    bar.innerHTML = '';
    const mkBtn = (label, page, opts = {}) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.textContent = label;
      b.disabled = !!opts.disabled;
      if (opts.current) b.classList.add('current');
      if (!opts.disabled && !opts.current) b.addEventListener('click', () => loadPage(page));
      return b;
    };
    bar.appendChild(mkBtn('<', current - 1, { disabled: current <= 1 }));
    const items = paginationWindow(current, total);
    const lastEllipsisIdx = items.lastIndexOf('…');
    items.forEach((item, i) => {
      if (item === '…') {
        if (i === lastEllipsisIdx) {
          bar.appendChild(buildPageJumpWidget(total));
        } else {
          const span = document.createElement('span');
          span.className = 'ellipsis';
          span.textContent = '…';
          bar.appendChild(span);
        }
      } else {
        bar.appendChild(mkBtn(String(item), item, { current: item === current }));
      }
    });
    bar.appendChild(mkBtn('>', current + 1, { disabled: current >= total }));
  }

  // A "…" gap replaced by a page-number input. The Go button only appears
  // while the input is focused, and is the only thing that triggers a jump —
  // no Enter-to-submit, no jump-on-blur.
  function buildPageJumpWidget(total) {
    const wrap = document.createElement('span');
    wrap.className = 'page-jump';

    const goBtn = document.createElement('button');
    goBtn.type = 'button';
    goBtn.className = 'page-jump-go hidden';
    goBtn.textContent = 'Go';

    const input = document.createElement('input');
    input.type = 'number';
    input.className = 'page-jump-input';
    input.min = '1';
    input.max = String(total);
    input.placeholder = '…';

    input.addEventListener('focus', () => goBtn.classList.remove('hidden'));
    wrap.addEventListener('focusout', e => {
      if (!wrap.contains(e.relatedTarget)) goBtn.classList.add('hidden');
    });
    goBtn.addEventListener('click', () => {
      const n = parseInt(input.value, 10);
      if (Number.isInteger(n) && n >= 1 && n <= total) loadPage(n);
    });

    wrap.append(goBtn, input);
    return wrap;
  }

  function openSearchModal() {
    const modal = document.getElementById('search-modal');
    modal.classList.remove('hidden');
    document.getElementById('search-backdrop').onclick = closeSearchModal;
    document.getElementById('search-close').onclick = closeSearchModal;
  }

  function closeSearchModal() {
    document.getElementById('search-modal').classList.add('hidden');
  }

  // ── Persistence (localStorage) ─────────────────────────────────────────────
  const STORAGE_KEY = 'researcher_graph_v1';

  const DEFAULT_SETTINGS = {
    edgeCoauthor: true,
    edgeCitation: true,
    edgeInstitution: true,
    workEdgeAuthorship: true,
    workEdgeCitation: true,
    neighborhood: '2,6',
    showNames: false,
    layoutSpacing: '5',
    layoutLink: '100',
  };

  function collectSettings() {
    return {
      edgeCoauthor: document.getElementById('edge-coauthor')?.checked ?? DEFAULT_SETTINGS.edgeCoauthor,
      edgeCitation: document.getElementById('edge-citation')?.checked ?? DEFAULT_SETTINGS.edgeCitation,
      edgeInstitution: document.getElementById('edge-institution')?.checked ?? DEFAULT_SETTINGS.edgeInstitution,
      workEdgeAuthorship: document.getElementById('work-edge-authorship')?.checked ?? DEFAULT_SETTINGS.workEdgeAuthorship,
      workEdgeCitation: document.getElementById('work-edge-citation')?.checked ?? DEFAULT_SETTINGS.workEdgeCitation,
      neighborhood: document.getElementById('neighborhood')?.value ?? DEFAULT_SETTINGS.neighborhood,
      showNames: document.getElementById('toggle-names')?.checked ?? DEFAULT_SETTINGS.showNames,
      layoutSpacing: document.getElementById('layout-spacing')?.value ?? DEFAULT_SETTINGS.layoutSpacing,
      layoutLink: document.getElementById('layout-link')?.value ?? DEFAULT_SETTINGS.layoutLink,
    };
  }

  function applySettings(s) {
    if (!s) return;
    const el = id => document.getElementById(id);
    if (el('edge-coauthor')) el('edge-coauthor').checked = s.edgeCoauthor ?? DEFAULT_SETTINGS.edgeCoauthor;
    if (el('edge-citation')) el('edge-citation').checked = s.edgeCitation ?? DEFAULT_SETTINGS.edgeCitation;
    if (el('edge-institution')) el('edge-institution').checked = s.edgeInstitution ?? DEFAULT_SETTINGS.edgeInstitution;
    if (el('work-edge-authorship')) el('work-edge-authorship').checked = s.workEdgeAuthorship ?? DEFAULT_SETTINGS.workEdgeAuthorship;
    if (el('work-edge-citation')) el('work-edge-citation').checked = s.workEdgeCitation ?? DEFAULT_SETTINGS.workEdgeCitation;
    if (el('neighborhood')) el('neighborhood').value = s.neighborhood ?? DEFAULT_SETTINGS.neighborhood;
    if (el('toggle-names')) el('toggle-names').checked = s.showNames ?? DEFAULT_SETTINGS.showNames;
    if (el('layout-spacing')) el('layout-spacing').value = s.layoutSpacing ?? DEFAULT_SETTINGS.layoutSpacing;
    if (el('layout-link')) el('layout-link').value = s.layoutLink ?? DEFAULT_SETTINGS.layoutLink;
    state.showNames = s.showNames ?? DEFAULT_SETTINGS.showNames;
  }

  function saveState() {
    const origins = [];
    state.origins.forEach(id => {
      const n = cy.getElementById(id);
      if (n.length) origins.push({ id, display_name: n.data('name') });
    });
    const elements = cy.elements().jsons();
    const paths = [...state.paths.entries()];
    const settings = collectSettings();
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify({ origins, elements, paths, settings }));
    } catch { /* quota exceeded — skip */ }
  }

  function clearSavedState() {
    localStorage.removeItem(STORAGE_KEY);
  }

  function loadSavedState() {
    let saved;
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return;
      saved = JSON.parse(raw);
    } catch { return; }

    const { origins = [], elements = [], paths = [], settings } = saved;

    // Always restore settings first (even if graph is empty)
    if (settings) applySettings(settings);

    if (!origins.length) return;

    // Restore paths map
    for (const [k, v] of paths) state.paths.set(k, v);

    // Restore origin IDs + chips (no API call — elements carry the graph)
    for (const author of origins) {
      state.origins.add(author.id);
      addChip(author.id, author.display_name);
    }

    // Restore full graph (nodes + edges) directly into Cytoscape
    try { cy.add(elements); } catch { /* ignore stale element errors */ }

    // Rebuild pathNodes set from restored graph
    cy.nodes('[type="path"]').forEach(n => state.pathNodes.add(n.id()));

    // Restore author cache from restored nodes
    cy.nodes().forEach(n => state.authorCache.set(n.id(), n.data()));

    applyNameVisibility();
    applyEdgeFade();
    renderDegrees();
    runLayout();
  }

  // ── Add researcher ─────────────────────────────────────────────────────────
  function addResearcher(author) {
    if (state.isLoading || state.origins.has(author.id)) return Promise.resolve();
    searchInput.value = '';
    state.origins.add(author.id);
    addChip(author.id, author.display_name);
    return startExpansion(author.id).then(saveState);
  }

  function addWork(work) {
    if (state.isLoading || state.origins.has(work.id)) return;
    workSearchInput.value = '';
    state.origins.add(work.id);
    addChip(work.id, work.title);
    startExpansion(work.id).then(saveState);
  }

  function addChip(id, label) {
    const work = isWorkId(id);
    const chip = document.createElement('div');
    chip.className = 'researcher-chip';
    chip.dataset.id = id;
    chip.title = label;

    const name = document.createElement('span');
    name.className = 'chip-name';
    name.textContent = work ? `📄 ${label}` : label;

    const remove = document.createElement('button');
    remove.className = 'chip-remove';
    remove.type = 'button';
    remove.textContent = '×';
    remove.title = work ? 'Remove work' : 'Remove researcher';
    remove.disabled = state.isLoading;
    remove.addEventListener('click', () => removeResearcher(id));

    chip.append(name, remove);
    document.getElementById('origin-chips').appendChild(chip);
  }

  function removeResearcher(id) {
    if (state.isLoading) return;
    state.origins.delete(id);
    document.querySelector(`.researcher-chip[data-id="${id}"]`)?.remove();
    cy.getElementById(id).remove();  // Cytoscape auto-removes connected edges

    // Collect pair keys that are now broken (both endpoints no longer present)
    const brokenPairs = new Set();
    for (const [key, d] of [...state.paths]) {
      if (d.from_id === id || d.to_id === id) {
        brokenPairs.add(key);
        state.paths.delete(key);
      }
    }
    renderDegrees();

    if (state.origins.size === 0) {
      cy.elements().remove();
      state.pathNodes.clear();
      clearSavedState();
      return;
    }

    // Remove path nodes whose every recorded pair is now broken
    cy.nodes('[type="path"]').forEach(n => {
      const pairs = n.data('pathPairs') || [];
      if (pairs.length === 0 || pairs.every(pk => brokenPairs.has(pk))) {
        state.pathNodes.delete(n.id());
        n.remove();
      }
    });

    // Remove expansion nodes whose every generating origin is now gone
    cy.nodes('[type="expansion"]').forEach(n => {
      const owners = n.data('expandOwners') || [];
      if (owners.length === 0 || owners.every(o => !state.origins.has(o) && !state.pathNodes.has(o))) {
        n.remove();
      }
    });

    saveState();
    runLayout();
  }

  // ── Graph helpers ──────────────────────────────────────────────────────────
  // Centroid of the origin nodes currently on canvas (falls back to the center).
  function originsCentroid() {
    const origins = cy.nodes('[type="origin"]');
    if (!origins.length) return { x: 0, y: 0 };
    let sx = 0, sy = 0;
    origins.forEach(o => { const p = o.position(); sx += p.x; sy += p.y; });
    return { x: sx / origins.length, y: sy / origins.length };
  }

  // Pick a starting position for a freshly-streamed node so it appears next to
  // its parent instead of piling at (0,0). Expansion nodes spawn near an
  // already-placed node they connect to (seedHints), falling back to a ring
  // around their owner origin (further out with depth); path nodes sit at their
  // pair's midpoint; everything else falls back to the origin centroid. A jitter
  // breaks up overlap and gives fCoSE a good starting point to relax from.
  function seedPosition(nodeData, seedHints) {
    const jitter = r => (Math.random() - 0.5) * 2 * r;
    const near = (p, r) => ({ x: p.x + jitter(r), y: p.y + jitter(r) });

    if (nodeData.type === 'expansion') {
      const depth = nodeData.depth || 1;
      // Prefer the already-placed node this one actually connects to (from the
      // expansion event's edge list) — the owner origin puts depth-2+ nodes at
      // a random angle unrelated to their edges, which bakes in tangles.
      const neighborId = seedHints && seedHints.get(nodeData.id);
      if (neighborId) {
        const neighbor = cy.getElementById(neighborId);
        if (neighbor.length) return near(neighbor.position(), 60 + depth * 40);
      }
      const owners = nodeData.expandOwners || [];
      for (const oid of owners) {
        const owner = cy.getElementById(oid);
        if (owner.length) {
          return near(owner.position(), 60 + depth * 40);
        }
      }
    }
    if (nodeData.type === 'path') {
      const pair = nodeData.path_pair || (nodeData.pathPairs || [])[0];
      if (pair) {
        // pair is the canonical "idA||idB" key (see pair_key in backend/app.py).
        const [idA, idB] = pair.split('||');
        const a = cy.getElementById(idA);
        const b = cy.getElementById(idB);
        if (a.length && b.length) {
          const pa = a.position(), pb = b.position();
          return near({ x: (pa.x + pb.x) / 2, y: (pa.y + pb.y) / 2 }, 40);
        }
      }
    }
    return near(originsCentroid(), 120);
  }

  function addOrUpdateNode(nodeData, seedHints) {
    if (nodeData.type === 'expansion') {
      const depth = nodeData.depth || 1;
      const op = depth <= 1 ? 1 : depth === 2 ? 0.65 : 0.4;
      nodeData = {
        ...nodeData,
        expandOwners: nodeData.expand_owners || [],
        bgColor: '#d2d2d2', fontColor: '#d2d2d2', nodeSize: 6, fontSize: 8, zIdx: 1, op,
      };
    }
    if (nodeData.type === 'path') {
      nodeData = { ...nodeData, pathPairs: nodeData.path_pair ? [nodeData.path_pair] : [] };
    }

    state.authorCache.set(nodeData.id, nodeData);
    const existing = cy.getElementById(nodeData.id);
    if (existing.length) {
      const priority = { origin: 3, work: 3, path: 2, expansion: 1 };
      if ((priority[nodeData.type] || 0) > (priority[existing.data('type')] || 0)) {
        existing.data({
          ...nodeData,
          expandOwners: existing.data('expandOwners') || nodeData.expandOwners || [],
          pathPairs: [...(existing.data('pathPairs') || []), ...(nodeData.pathPairs || [])],
        });
      } else if (nodeData.type === 'path' && nodeData.path_pair) {
        // Same type: merge the new pair key in without duplicating
        const pairs = existing.data('pathPairs') || [];
        if (!pairs.includes(nodeData.path_pair)) {
          existing.data('pathPairs', [...pairs, nodeData.path_pair]);
        }
      }
      return;
    }
    // Seed a starting position near a connected or owner node so it appears in a
    // sensible spot immediately (origins are placed/pinned by the layout, so skip them).
    const el = { group: 'nodes', data: { ...nodeData } };
    if (nodeData.type !== 'origin') el.position = seedPosition(nodeData, seedHints);
    cy.add(el);
    if (nodeData.type === 'path') state.pathNodes.add(nodeData.id);
  }

  function addEdge(edgeData) {
    // Canonical (undirected) id so A->B and B->A of the same type collapse to a
    // single straight line instead of two parallel edges that bezier curves apart.
    const [lo, hi] = [edgeData.source, edgeData.target].sort();
    const id = `${lo}||${hi}||${edgeData.type}`;
    if (cy.getElementById(id).length) return;
    if (!cy.getElementById(edgeData.source).length) return;
    if (!cy.getElementById(edgeData.target).length) return;
    const eop = EDGE_TYPE_OPACITY[edgeData.type] ?? 0.5;
    cy.add({ group: 'edges', data: { id, eop, ...edgeData } });
  }

  // ── SSE expansion ──────────────────────────────────────────────────────────
  // Resolves when the stream completes (or errors), so callers can replay it
  // sequentially. `existingOverride` lets rebuildGraph control exactly which
  // origins count as "already present" at each replay step.
  // ── Loading state ──────────────────────────────────────────────────────────
  // Disable all graph-affecting controls while a stream is in flight so the
  // user can't trigger conflicting actions or cause the scheduleRebuild loop.
  function setLoading(loading) {
    state.isLoading = loading;
    // Graph-affecting controls
    ['edge-coauthor', 'edge-citation', 'edge-institution', 'work-edge-authorship',
      'work-edge-citation', 'neighborhood', 'apply-options',
      'restore-settings', 'restore-default-layout', 'clear-canvas'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.disabled = loading;
      });
    // Chip remove buttons (created dynamically, so query each time)
    document.querySelectorAll('.chip-remove').forEach(btn => { btn.disabled = loading; });
    // Dim the search inputs/buttons so it's clear adding is blocked
    ['search-input', 'search-btn', 'work-search-input', 'work-search-btn',
      'rank-institution-input', 'rank-institution-search',
      'rank-target-input', 'rank-target-search', 'rank-primary-only', 'rank-btn'].forEach(id => {
      const el = document.getElementById(id);
      if (el) el.disabled = loading;
    });
    updateOverlays();
  }

  function startExpansion(newId, existingOverride) {
    return new Promise(resolve => {
      if (state.activeSource) { state.activeSource.close(); state.activeSource = null; }
      setLoading(true);
      showProgress('Connecting…');

      cy.nodes('[type="expansion"]').remove();

      const existingOrigins = existingOverride
        ?? [...state.origins].filter(id => id !== newId);
      const existingPathIds = [...state.pathNodes];

      // The edge-type checkboxes drive the actual search: only the enabled types
      // are sent, so co-author-only / citation-only / etc. searches are possible.
      const params = new URLSearchParams({ new_id: newId });
      if (existingOrigins.length) params.set('origin_ids', existingOrigins.join(','));
      if (existingPathIds.length) params.set('path_ids', existingPathIds.join(','));
      getEnabledEdges().forEach(e => params.append('edges', e));
      getEnabledWorkEdges().forEach(e => params.append('work_edges', e));
      const nb = getNeighborhood();
      params.set('depth', nb.depth);
      params.set('top_k', nb.topK);

      const source = new EventSource(`${API_BASE}/api/graph/expand?${params}`);
      state.activeSource = source;

      // Throttle incremental layout runs while events stream in: coalesce bursts
      // but force a run at least every `maxWait` ms so the graph visibly grows
      // *during* a long stream rather than only settling at the end.
      let growTimer = null, growForce = null;
      const growWait = 250, maxWait = 600;
      const scheduleGrow = () => {
        const now = Date.now();
        if (growForce === null) growForce = now + maxWait;
        clearTimeout(growTimer);
        const delay = Math.min(growWait, Math.max(0, growForce - now));
        growTimer = setTimeout(() => {
          growTimer = null; growForce = null;
          runLayoutIncremental();
        }, delay);
      };

      const finish = () => {
        clearTimeout(growTimer); growTimer = null; growForce = null;
        if (state.activeSource === source) state.activeSource = null;
        setLoading(false);
        resolve();
      };

      source.addEventListener('node', e => { addOrUpdateNode(JSON.parse(e.data)); scheduleGrow(); });
      source.addEventListener('edge', e => { addEdge(JSON.parse(e.data)); scheduleGrow(); });

      source.addEventListener('path', e => {
        const d = JSON.parse(e.data);
        state.paths.set(pairKey(d.from_id, d.to_id), d);
        renderDegrees();
      });

      source.addEventListener('expansion', e => {
        const data = JSON.parse(e.data);
        showProgress(`Building neighborhood (depth ${data.depth}/3)…`);
        // Nodes stream before their edges within an expansion event, so map
        // each new node to something it connects to for spawn seeding.
        const seedHints = new Map();
        data.edges.forEach(ed => {
          if (!seedHints.has(ed.target)) seedHints.set(ed.target, ed.source);
        });
        data.nodes.forEach(n => addOrUpdateNode(n, seedHints));
        data.edges.forEach(addEdge);
        scheduleGrow();
      });

      source.addEventListener('progress', e => {
        showProgress(JSON.parse(e.data).message);
      });

      source.addEventListener('done', () => {
        source.close();
        hideProgress();
        rescaleExpansionNodes();
        applyNameVisibility();
        applyEdgeFade();
        // Full-quality settle: one visible reorganization that untangles
        // whatever arrangement the streaming order produced (origins stay
        // pinned via fixedNodeConstraint inside runLayout).
        runLayout();
        finish();
      });

      source.addEventListener('app_error', e => {
        source.close();
        let msg = 'Error';
        try { msg = JSON.parse(e.data).message; } catch { /* ignore */ }
        showProgress('Error: ' + msg, true);
        setTimeout(hideProgress, 5000);
        finish();
      });

      source.onerror = () => {
        if (state.activeSource === source) {
          source.close();
          hideProgress();
          finish();
        }
      };
    });
  }

  // Re-run the whole search from the current origins with the current edge types.
  // Replays the incremental add history so every pair's path is recomputed.
  let rebuildTimer;
  function scheduleRebuild() {
    clearTimeout(rebuildTimer);
    rebuildTimer = setTimeout(rebuildGraph, 350);
  }

  async function rebuildGraph() {
    if (state.isLoading) { scheduleRebuild(); return; }
    const order = [...state.origins];
    if (!order.length) return;
    cy.elements().remove();
    state.pathNodes.clear();
    state.paths.clear();
    renderDegrees();
    for (let i = 0; i < order.length; i++) {
      await startExpansion(order[i], order.slice(0, i));
    }
    saveState();
  }

  // Read the Layout sliders into concrete force values (with sensible defaults).
  // Spacing (1-10) drives repulsion / separation / origin spread; link length is
  // the ideal edge length in px.
  function getLayoutParams() {
    const spacing = parseInt(document.getElementById('layout-spacing')?.value ?? '5', 10);
    const link = parseInt(document.getElementById('layout-link')?.value ?? '100', 10);
    return {
      repulsion: 6000 + spacing * 4000,   // spacing 5 -> 26000
      separation: 40 + spacing * 36,      // spacing 5 -> 220
      spreadFactor: spacing / 5,          // scales origin separation
      edgeLength: link,
    };
  }

  // Pin the origin researchers symmetrically around the center so they sit in
  // the middle and their neighborhoods spring outward. The separation grows
  // with graph size (and the Spacing slider) so big neighborhoods don't pile up.
  // Returns an fCoSE `fixedNodeConstraint` array; shared by the full and
  // incremental layouts so origins stay anchored in the same spots.
  function originConstraints(lp) {
    const origins = cy.nodes('[type="origin"]');
    const n = origins.length;
    const spread = Math.round((140 + Math.sqrt(cy.nodes().length) * 26) * lp.spreadFactor);
    return origins.map((node, i) => {
      let position;
      if (n <= 1) position = { x: 0, y: 0 };
      else if (n === 2) position = { x: i === 0 ? -spread : spread, y: 0 };
      else {
        const a = (i / n) * 2 * Math.PI;
        position = { x: Math.round(spread * Math.cos(a)), y: Math.round(spread * Math.sin(a)) };
      }
      return { nodeId: node.id(), position };
    });
  }

  // Incremental (non-destructive) layout used while the stream is arriving:
  // keeps every node's current position (randomize:false) and only relaxes the
  // newly-seeded nodes outward, so the graph visibly grows instead of
  // re-shuffling. Cheaper than runLayout (fewer iterations, shorter animation),
  // which does the one full-quality settle once the stream ends. `fit` re-frames
  // the viewport so the full graph always stays comfortably centered as it grows
  // (generous padding); on by default.
  function runLayoutIncremental({ fit = true } = {}) {
    if (!cy.nodes().length) return;
    if (!(window.cytoscapeFcose && cytoscape.__fcoseRegistered)) return;
    const lp = getLayoutParams();
    cy.layout({
      name: 'fcose',
      quality: 'default',
      animate: true,
      animationDuration: 250,
      randomize: false,
      fit,
      padding: 90,
      nodeSeparation: lp.separation,
      idealEdgeLength: lp.edgeLength,
      nodeRepulsion: lp.repulsion,
      gravity: 0.12,
      gravityRange: 4.0,
      fixedNodeConstraint: originConstraints(lp),
      numIter: 500,
    }).run();
  }

  function runLayout() {
    if (!cy.nodes().length) return;
    const lp = getLayoutParams();
    const fixed = originConstraints(lp);

    if (window.cytoscapeFcose && cytoscape.__fcoseRegistered) {
      // Origins are pinned, so gravity can stay low; repulsion and node separation
      // spread unconnected nodes apart, while the ideal edge length keeps connected
      // nodes close (avoids long lines across the canvas).
      cy.layout({
        name: 'fcose',
        quality: 'proof',
        animate: true,
        animationDuration: 700,
        randomize: true,
        fit: true,
        padding: 90,
        nodeSeparation: lp.separation,
        idealEdgeLength: lp.edgeLength,
        nodeRepulsion: lp.repulsion,
        gravity: 0.12,
        gravityRange: 4.0,
        fixedNodeConstraint: fixed,
        numIter: 3000,
      }).run();
      return;
    }

    // Fallback if the fCoSE CDN script failed to load (no fixed-node support).
    cy.layout({
      name: 'cose',
      animate: true,
      animationDuration: 600,
      nodeRepulsion: lp.repulsion,
      idealEdgeLength: lp.edgeLength,
      nodeOverlap: 28,
      gravity: 0.25,
      componentSpacing: 160,
      randomize: true,
      fit: true,
      padding: 90,
    }).run();
  }

  // Neighborhood size control → {depth, topK}. depth 0 = path only (no expansion).
  function getNeighborhood() {
    const raw = (document.getElementById('neighborhood')?.value || '2,6').split(',');
    return { depth: parseInt(raw[0], 10), topK: parseInt(raw[1], 10) };
  }

  // Show/hide expansion-node labels in bulk (the "Show all names" toggle).
  function applyNameVisibility() {
    cy.nodes('[type="expansion"]').toggleClass('shown', state.showNames);
  }

  // Fade edges by distance (deeper endpoint = fainter), matching the node fade.
  function applyEdgeFade() {
    cy.edges().forEach(e => {
      const d = Math.max(e.source().data('depth') || 0, e.target().data('depth') || 0);
      const factor = d <= 1 ? 1 : d === 2 ? 0.55 : 0.3;
      e.data('eop', (EDGE_TYPE_OPACITY[e.data('type')] ?? 0.5) * factor);
    });
  }

  // ── Degrees of separation ───────────────────────────────────────────────────
  // Order-independent key so A↔B and B↔A collapse to one entry.
  function pairKey(a, b) {
    return [a, b].sort().join('||');
  }

  function degreesLabel(d) {
    if (!d.found) return 'no path found';
    return `${d.hops} degree${d.hops === 1 ? '' : 's'} of separation`;
  }

  // Human-readable description of a single hop, by connection type.
  function stepPhrase(s) {
    const label = (s.label || '').trim();
    if (s.type === 'coauthor') return label ? `co-authored "${label}"` : 'co-authored a paper';
    if (s.type === 'citation') return label ? `citation via "${label}"` : 'citation link';
    if (s.type === 'institution') return label ? `both at ${label}` : 'same institution';
    return s.type;
  }

  function renderDegrees() {
    const panel = document.getElementById('degrees-panel');
    const list = document.getElementById('degrees-list');
    list.innerHTML = '';
    for (const d of state.paths.values()) {
      const li = document.createElement('li');
      let html =
        `<div class="degrees-pair">${escHtml(d.from_name)} ↔ ${escHtml(d.to_name)}</div>` +
        `<div><a class="degrees-count" style="cursor:pointer" href="#" onclick="return false;"><span class="degrees-arrow">&#9660;</span> <strong style="text-decoration:underline">${escHtml(degreesLabel(d))}</strong></a></div>`;
      li.innerHTML = html;

      // detail is inserted after count div
      li.querySelector('.degrees-count').addEventListener('click', () => toggleDegreeExpand(d, li.querySelector('.degrees-count')));

      list.appendChild(li);
    }
    panel.classList.toggle('hidden', state.paths.size === 0);
  }

  // ── UI helpers ─────────────────────────────────────────────────────────────
  function getEnabledEdges() {
    return ['coauthor', 'citation', 'institution']
      .filter(e => document.getElementById(`edge-${e}`)?.checked);
  }

  function getEnabledWorkEdges() {
    return ['authorship', 'citation']
      .filter(e => document.getElementById(`work-edge-${e}`)?.checked);
  }

  // ── Empty state + legend visibility ────────────────────────────────────────
  // Empty state shows only on a blank, idle canvas; the legend only when there
  // is a graph to explain.
  function updateOverlays() {
    const hasNodes = cy.nodes().length > 0;
    document.getElementById('empty-state')?.classList.toggle('hidden', hasNodes || state.isLoading);
    document.getElementById('graph-legend')?.classList.toggle('hidden', !hasNodes);
  }

  let overlayTimer = null;
  cy.on('add remove', () => {
    clearTimeout(overlayTimer);
    overlayTimer = setTimeout(updateOverlays, 50);
  });

  // Example CTA: two researchers with a known, interesting 2-hop connection.
  // Sequential on purpose — the second add computes the path to the first.
  document.getElementById('empty-example')?.addEventListener('click', async () => {
    if (state.isLoading || state.origins.size) return;
    await addResearcher({ id: 'A5108093963', display_name: 'Geoffrey E. Hinton' });
    await addResearcher({ id: 'A5072532913', display_name: 'Noam Chomsky' });
  });

  // ── Sidebar horizontal resize ──────────────────────────────────────────────
  // Drag the handle to resize; the width snaps to the 260px sweet spot, and
  // dragging below MIN (or clicking the handle) collapses the sidebar entirely.
  const SIDEBAR_WIDTH_KEY = 'sidebar_width_v1';
  (function initSidebarResize() {
    const sidebar = document.getElementById('sidebar');
    const resizer = document.getElementById('sidebar-resizer');
    if (!sidebar || !resizer) return;
    const SWEET = 260, SNAP = 30, MIN = 140, MAX = 520;

    // While the width transition is animating (click-to-toggle path), an
    // immediate cy.resize() would cache mid-transition container dimensions;
    // wait for transitionend (with a timeout fallback) before resizing.
    let deferredResize = null;
    function resizeAfterTransition() {
      if (deferredResize) deferredResize();
      const finish = () => {
        clearTimeout(timer);
        sidebar.removeEventListener('transitionend', onEnd);
        deferredResize = null;
        cy.resize();
      };
      const onEnd = ev => { if (ev.target === sidebar && ev.propertyName === 'width') finish(); };
      const timer = setTimeout(finish, 200);
      deferredResize = () => {
        clearTimeout(timer);
        sidebar.removeEventListener('transitionend', onEnd);
        deferredResize = null;
      };
      sidebar.addEventListener('transitionend', onEnd);
    }

    function apply(w) {
      if (w === 'collapsed') {
        sidebar.style.width = '';   // inline width would override the class's width: 0
        sidebar.classList.add('collapsed');
      } else {
        sidebar.classList.remove('collapsed');
        sidebar.style.width = w + 'px';
      }
      if (sidebar.classList.contains('resizing')) cy.resize();
      else resizeAfterTransition();
    }

    function save(w) {
      try { localStorage.setItem(SIDEBAR_WIDTH_KEY, String(w)); } catch { /* ignore */ }
    }

    const saved = localStorage.getItem(SIDEBAR_WIDTH_KEY);
    if (saved === 'collapsed') apply('collapsed');
    else if (saved && !Number.isNaN(parseInt(saved, 10))) apply(Math.min(MAX, Math.max(MIN, parseInt(saved, 10))));

    resizer.addEventListener('pointerdown', e => {
      e.preventDefault();
      resizer.setPointerCapture(e.pointerId);
      resizer.classList.add('active');
      sidebar.classList.add('resizing');
      const startX = e.clientX;
      const startW = sidebar.classList.contains('collapsed') ? 0 : sidebar.getBoundingClientRect().width;
      let moved = false;
      let current = startW > 0 ? startW : 'collapsed';

      const onMove = ev => {
        const dx = ev.clientX - startX;
        if (Math.abs(dx) > 4) moved = true;
        if (!moved) return;
        let w = startW + dx;
        if (Math.abs(w - SWEET) <= SNAP) w = SWEET;       // sweet-spot snap
        if (w < MIN) { current = 'collapsed'; apply('collapsed'); return; }
        current = Math.min(MAX, Math.round(w));
        apply(current);
      };

      const onUp = () => {
        resizer.removeEventListener('pointermove', onMove);
        resizer.removeEventListener('pointerup', onUp);
        resizer.removeEventListener('pointercancel', onUp);
        resizer.classList.remove('active');
        sidebar.classList.remove('resizing');
        if (!moved) {
          // Plain click: toggle collapsed <-> sweet spot
          current = sidebar.classList.contains('collapsed') ? SWEET : 'collapsed';
          apply(current);
        } else {
          cy.resize();
        }
        save(current);
      };

      resizer.addEventListener('pointermove', onMove);
      resizer.addEventListener('pointerup', onUp);
      resizer.addEventListener('pointercancel', onUp);
    });

    // Obvious fold/unfold control (visible on desktop; the mobile switcher takes
    // over below the breakpoint). Toggles between collapsed and the sweet spot.
    const menuBtn = document.getElementById('menu-toggle');
    if (menuBtn) menuBtn.addEventListener('click', () => {
      const w = sidebar.classList.contains('collapsed') ? SWEET : 'collapsed';
      apply(w);
      save(w);
    });
  })();

  // ── Mobile view switcher (Graph / Menu) ─────────────────────────────────────
  // Below the breakpoint the sidebar is a full-screen overlay driven by a
  // body.mobile-menu-open class rather than the desktop collapse/width logic.
  (function initMobileView() {
    const mq = window.matchMedia('(max-width: 780px)');
    const graphBtn = document.getElementById('view-graph');
    const menuBtn = document.getElementById('view-menu');
    if (!graphBtn || !menuBtn) return;

    function setMenuOpen(open) {
      document.body.classList.toggle('mobile-menu-open', open);
      graphBtn.classList.toggle('active', !open);
      menuBtn.classList.toggle('active', open);
      // Let the overlay's transform transition finish before remeasuring the graph.
      setTimeout(() => {
        cy.resize();
        if (!open && cy.nodes().length) cy.fit(undefined, 40);
      }, 240);
    }

    graphBtn.addEventListener('click', () => setMenuOpen(false));
    menuBtn.addEventListener('click', () => setMenuOpen(true));

    function syncMode() {
      if (mq.matches) {
        setMenuOpen(false);   // entering mobile: show the graph first
      } else {
        document.body.classList.remove('mobile-menu-open');
        cy.resize();
      }
    }
    mq.addEventListener('change', syncMode);
    syncMode();
  })();

  // ── Collapsible sidebar sections ───────────────────────────────────────────
  const SECTIONS_KEY = 'sidebar_sections_v1';
  (function initSectionCollapse() {
    let saved = {};
    try { saved = JSON.parse(localStorage.getItem(SECTIONS_KEY) || '{}'); } catch { /* ignore */ }
    document.querySelectorAll('#sidebar details.side-card').forEach(card => {
      if (card.id in saved) card.open = !!saved[card.id];
      card.addEventListener('toggle', () => {
        const states = {};
        document.querySelectorAll('#sidebar details.side-card').forEach(c => { states[c.id] = c.open; });
        try { localStorage.setItem(SECTIONS_KEY, JSON.stringify(states)); } catch { /* ignore */ }
      });
    });
  })();

  function showProgress(msg, isError = false) {
    const overlay = document.getElementById('progress-overlay');
    const text = document.getElementById('progress-text');
    overlay.classList.remove('hidden');
    overlay.style.borderColor = isError ? '#e5b3ae' : '#ddd';
    text.style.color = isError ? '#c0392b' : '#555';
    text.textContent = msg;
  }

  function hideProgress() {
    document.getElementById('progress-overlay').classList.add('hidden');
  }

  function escHtml(str) {
    const d = document.createElement('div');
    d.appendChild(document.createTextNode(str || ''));
    return d.innerHTML;
  }

  function escAttr(str) {
    return escHtml(str).replace(/"/g, '&quot;');
  }

  // Restore any previously saved session on page load.
  loadSavedState();
  updateOverlays();
})();
