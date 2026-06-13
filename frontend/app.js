(function () {
  const API_BASE = window.RESEARCHER_API_BASE ?? '';

  // ── State ──────────────────────────────────────────────────────────────────
  const state = {
    origins: new Set(),
    pathNodes: new Set(),
    authorCache: new Map(),
    activeSource: null,
    isLoading: false,
  };

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
    tooltip.style.top  = (mouseY + pad + h > vh ? mouseY - h - pad : mouseY + pad) + 'px';
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
    return {
      bgColor,
      fontColor: bgColor,
      nodeSize: Math.round(6 + tW * 96), // 6 → 102 (3× origin)
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
      n.data({ bgColor: s.bgColor, fontColor: s.fontColor, nodeSize: s.nodeSize, zIdx: s.zIdx });
    });
  }

  // ── Cytoscape init ─────────────────────────────────────────────────────────
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
          color: '#aaa',
          'background-color': '#ddd',
          width: 10,
          height: 10,
          'text-max-width': '80px',
          'text-wrap': 'ellipsis',
          'text-overflow-wrap': 'anywhere',
          'text-margin-y': 3,
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
      // Expansion: all visual properties driven by works_count / cited_by_count
      {
        selector: 'node[type="expansion"]',
        style: {
          'background-color': 'data(bgColor)',
          color: 'data(fontColor)',
          width: 'data(nodeSize)',
          height: 'data(nodeSize)',
          'font-size': '9px',
          'z-index': 'data(zIdx)',
        },
      },
      {
        selector: 'node:selected',
        style: { 'border-width': 2, 'border-color': '#222' },
      },
      {
        selector: 'edge',
        style: {
          'line-color': '#e0e0e0',
          width: 1,
          'curve-style': 'bezier',
          opacity: 0.5,
        },
      },
      {
        selector: 'edge[type="coauthor"]',
        style: { 'line-color': '#999', width: 1.5, opacity: 0.65 },
      },
      {
        selector: 'edge[type="citation"]',
        style: { 'line-color': '#bbb', 'line-style': 'dashed', opacity: 0.5 },
      },
      {
        selector: 'edge[type="institution"]',
        style: { 'line-color': '#ccc', 'line-style': 'dotted', opacity: 0.4 },
      },
    ],
    layout: { name: 'preset' },
    userZoomingEnabled: true,
    userPanningEnabled: true,
  });

  // ── Hover tooltips ─────────────────────────────────────────────────────────
  cy.on('mouseover', 'node', function (evt) {
    const d = evt.target.data();
    const lines = [`<strong>${escHtml(d.name)}</strong>`];
    if (d.institution) lines.push(escHtml(d.institution));
    lines.push(`${(d.works_count || 0).toLocaleString()} works · ${(d.cited_by_count || 0).toLocaleString()} citations`);
    showTooltip(lines.join('<br>'));
  });
  cy.on('mouseout', 'node', hideTooltip);

  cy.on('mouseover', 'edge', function (evt) {
    const d = evt.target.data();
    const typeLabel = {
      coauthor: 'Co-authorship',
      citation: 'Citation',
      institution: 'Institution',
    }[d.type] || d.type;
    const lines = [`<em>${typeLabel}</em>`];
    if (d.label) lines.push(`"${escHtml(d.label)}"`);
    showTooltip(lines.join('<br>'));
  });
  cy.on('mouseout', 'edge', hideTooltip);

  // ── Node click → sidebar detail ───────────────────────────────────────────
  cy.on('tap', 'node', function (evt) {
    hideTooltip();
    const data = evt.target.data();
    document.getElementById('detail-name').textContent = data.name;
    document.getElementById('detail-meta').innerHTML = [
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
  const searchDropdown = document.getElementById('search-dropdown');
  let searchTimer;

  searchInput.addEventListener('input', () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(async () => {
      const q = searchInput.value.trim();
      if (q.length < 2) { searchDropdown.innerHTML = ''; return; }
      const authors = await fetchAuthors(q);
      renderDropdown(authors);
    }, 300);
  });

  document.addEventListener('click', e => {
    if (!e.target.closest('#search-wrapper')) searchDropdown.innerHTML = '';
  });

  async function fetchAuthors(q) {
    try {
      const r = await fetch(`${API_BASE}/api/authors?q=${encodeURIComponent(q)}`);
      return r.ok ? r.json() : [];
    } catch { return []; }
  }

  function renderDropdown(authors) {
    searchDropdown.innerHTML = '';
    for (const a of authors) {
      if (state.origins.has(a.id)) continue;
      const li = document.createElement('li');
      li.innerHTML = `<strong>${escHtml(a.display_name)}</strong><br><small>${escHtml(a.institution || 'Unknown institution')} · ${a.works_count} works</small>`;
      li.addEventListener('mousedown', e => { e.preventDefault(); addResearcher(a); });
      searchDropdown.appendChild(li);
    }
  }

  // ── Add researcher ─────────────────────────────────────────────────────────
  function addResearcher(author) {
    if (state.isLoading || state.origins.has(author.id)) return;
    searchInput.value = '';
    searchDropdown.innerHTML = '';
    state.origins.add(author.id);
    addChip(author);
    startExpansion(author.id);
  }

  function addChip(author) {
    const chip = document.createElement('div');
    chip.className = 'researcher-chip';
    chip.title = author.display_name;
    chip.textContent = author.display_name;
    document.getElementById('origin-chips').appendChild(chip);
  }

  // ── Graph helpers ──────────────────────────────────────────────────────────
  function addOrUpdateNode(nodeData) {
    if (nodeData.type === 'expansion') {
      // Placeholder style — rescaleExpansionNodes() applies relative scale on done
      nodeData = { ...nodeData, bgColor: '#d2d2d2', fontColor: '#d2d2d2', nodeSize: 6, zIdx: 1 };
    }
    state.authorCache.set(nodeData.id, nodeData);
    const existing = cy.getElementById(nodeData.id);
    if (existing.length) {
      const priority = { origin: 3, path: 2, expansion: 1 };
      if ((priority[nodeData.type] || 0) > (priority[existing.data('type')] || 0)) {
        existing.data(nodeData);
      }
      return;
    }
    cy.add({ group: 'nodes', data: { ...nodeData } });
    if (nodeData.type === 'path') state.pathNodes.add(nodeData.id);
  }

  function addEdge(edgeData) {
    const id = `${edgeData.source}||${edgeData.target}||${edgeData.type}`;
    if (cy.getElementById(id).length) return;
    if (!cy.getElementById(edgeData.source).length) return;
    if (!cy.getElementById(edgeData.target).length) return;
    cy.add({ group: 'edges', data: { id, ...edgeData } });
  }

  // ── SSE expansion ──────────────────────────────────────────────────────────
  function startExpansion(newId) {
    if (state.activeSource) { state.activeSource.close(); state.activeSource = null; }
    state.isLoading = true;
    showProgress('Connecting…');

    cy.nodes('[type="expansion"]').remove();

    const existingOrigins = [...state.origins].filter(id => id !== newId);
    const existingPathIds = [...state.pathNodes];
    const enabledEdges = getEnabledEdges();

    const params = new URLSearchParams({ new_id: newId });
    if (existingOrigins.length) params.set('origin_ids', existingOrigins.join(','));
    if (existingPathIds.length) params.set('path_ids', existingPathIds.join(','));
    enabledEdges.forEach(e => params.append('edges', e));

    const source = new EventSource(`${API_BASE}/api/graph/expand?${params}`);
    state.activeSource = source;

    source.addEventListener('node', e => addOrUpdateNode(JSON.parse(e.data)));
    source.addEventListener('edge', e => addEdge(JSON.parse(e.data)));

    source.addEventListener('expansion', e => {
      const data = JSON.parse(e.data);
      showProgress(`Building neighborhood (depth ${data.depth}/3)…`);
      data.nodes.forEach(addOrUpdateNode);
      data.edges.forEach(addEdge);
    });

    source.addEventListener('progress', e => {
      showProgress(JSON.parse(e.data).message);
    });

    source.addEventListener('done', () => {
      source.close(); state.activeSource = null;
      state.isLoading = false;
      hideProgress();
      rescaleExpansionNodes();
      runLayout();
    });

    source.addEventListener('app_error', e => {
      source.close(); state.activeSource = null;
      state.isLoading = false;
      let msg = 'Error';
      try { msg = JSON.parse(e.data).message; } catch { /* ignore */ }
      showProgress('Error: ' + msg, true);
      setTimeout(hideProgress, 5000);
    });

    source.onerror = () => {
      if (state.activeSource === source) {
        source.close(); state.activeSource = null;
        state.isLoading = false;
        hideProgress();
      }
    };
  }

  function runLayout() {
    cy.layout({
      name: 'cose',
      animate: true,
      animationDuration: 600,
      nodeRepulsion: 8000,
      idealEdgeLength: 80,
      nodeOverlap: 10,
      randomize: false,
      fit: cy.nodes().length <= 3,
    }).run();
  }

  // ── UI helpers ─────────────────────────────────────────────────────────────
  function getEnabledEdges() {
    return ['coauthor', 'citation', 'institution']
      .filter(e => document.getElementById(`edge-${e}`)?.checked);
  }

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
})();
