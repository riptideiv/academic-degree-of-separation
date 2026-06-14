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
  const EDGE_TYPE_OPACITY = { coauthor: 0.65, citation: 0.5, institution: 0.4 };

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
      {
        selector: 'edge',
        style: {
          'line-color': '#e0e0e0',
          width: 1,
          'curve-style': 'bezier',
          opacity: 'data(eop)',   // per-type base × distance factor (applyEdgeFade)
        },
      },
      {
        selector: 'edge[type="coauthor"]',
        style: { 'line-color': '#999', width: 1.5 },
      },
      {
        selector: 'edge[type="citation"]',
        style: { 'line-color': '#bbb', 'line-style': 'dashed' },
      },
      {
        selector: 'edge[type="institution"]',
        style: { 'line-color': '#ccc', 'line-style': 'dotted' },
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
    if (d.institution) lines.push(escHtml(d.institution));
    lines.push(`${(d.works_count || 0).toLocaleString()} works · ${(d.cited_by_count || 0).toLocaleString()} citations`);
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

  // Edge-type checkboxes re-run the search with the new set of connection types.
  ['coauthor', 'citation', 'institution'].forEach(t => {
    document.getElementById(`edge-${t}`)?.addEventListener('change', () => {
      if (state.origins.size) scheduleRebuild();
    });
  });

  // Neighborhood size changes the search (depth / breadth), so re-run it.
  document.getElementById('neighborhood')?.addEventListener('change', () => {
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
    const graphChanged = ['edgeCoauthor', 'edgeCitation', 'edgeInstitution', 'neighborhood']
      .some(k => String(prev[k]) !== String(saved.settings[k]));
    if (graphChanged && state.origins.size) scheduleRebuild();
    else if (state.origins.size) runLayout();
  });

  document.getElementById('restore-default-layout')?.addEventListener('click', () => {
    const el = id => document.getElementById(id);
    if (el('layout-spacing')) el('layout-spacing').value = DEFAULT_SETTINGS.layoutSpacing;
    if (el('layout-link'))    el('layout-link').value    = DEFAULT_SETTINGS.layoutLink;
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

  // ── Persistence (localStorage) ─────────────────────────────────────────────
  const STORAGE_KEY = 'researcher_graph_v1';

  const DEFAULT_SETTINGS = {
    edgeCoauthor: true,
    edgeCitation: true,
    edgeInstitution: true,
    neighborhood: '2,6',
    showNames: false,
    layoutSpacing: '5',
    layoutLink: '100',
  };

  function collectSettings() {
    return {
      edgeCoauthor:    document.getElementById('edge-coauthor')?.checked    ?? DEFAULT_SETTINGS.edgeCoauthor,
      edgeCitation:    document.getElementById('edge-citation')?.checked    ?? DEFAULT_SETTINGS.edgeCitation,
      edgeInstitution: document.getElementById('edge-institution')?.checked ?? DEFAULT_SETTINGS.edgeInstitution,
      neighborhood:    document.getElementById('neighborhood')?.value       ?? DEFAULT_SETTINGS.neighborhood,
      showNames:       document.getElementById('toggle-names')?.checked     ?? DEFAULT_SETTINGS.showNames,
      layoutSpacing:   document.getElementById('layout-spacing')?.value     ?? DEFAULT_SETTINGS.layoutSpacing,
      layoutLink:      document.getElementById('layout-link')?.value        ?? DEFAULT_SETTINGS.layoutLink,
    };
  }

  function applySettings(s) {
    if (!s) return;
    const el = id => document.getElementById(id);
    if (el('edge-coauthor'))    el('edge-coauthor').checked    = s.edgeCoauthor    ?? DEFAULT_SETTINGS.edgeCoauthor;
    if (el('edge-citation'))    el('edge-citation').checked    = s.edgeCitation    ?? DEFAULT_SETTINGS.edgeCitation;
    if (el('edge-institution')) el('edge-institution').checked = s.edgeInstitution ?? DEFAULT_SETTINGS.edgeInstitution;
    if (el('neighborhood'))     el('neighborhood').value       = s.neighborhood    ?? DEFAULT_SETTINGS.neighborhood;
    if (el('toggle-names'))     el('toggle-names').checked     = s.showNames       ?? DEFAULT_SETTINGS.showNames;
    if (el('layout-spacing'))   el('layout-spacing').value     = s.layoutSpacing   ?? DEFAULT_SETTINGS.layoutSpacing;
    if (el('layout-link'))      el('layout-link').value        = s.layoutLink      ?? DEFAULT_SETTINGS.layoutLink;
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
      addChip(author);
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
    if (state.isLoading || state.origins.has(author.id)) return;
    searchInput.value = '';
    searchDropdown.innerHTML = '';
    state.origins.add(author.id);
    addChip(author);
    startExpansion(author.id).then(saveState);
  }

  function addChip(author) {
    const chip = document.createElement('div');
    chip.className = 'researcher-chip';
    chip.dataset.id = author.id;
    chip.title = author.display_name;

    const name = document.createElement('span');
    name.className = 'chip-name';
    name.textContent = author.display_name;

    const remove = document.createElement('button');
    remove.className = 'chip-remove';
    remove.type = 'button';
    remove.textContent = '×';
    remove.title = 'Remove researcher';
    remove.disabled = state.isLoading;
    remove.addEventListener('click', () => removeResearcher(author.id));

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
      if (owners.length === 0 || owners.every(o => !state.origins.has(o))) {
        n.remove();
      }
    });

    saveState();
    runLayout();
  }

  // ── Graph helpers ──────────────────────────────────────────────────────────
  function addOrUpdateNode(nodeData) {
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
      const priority = { origin: 3, path: 2, expansion: 1 };
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
    cy.add({ group: 'nodes', data: { ...nodeData } });
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
    ['edge-coauthor', 'edge-citation', 'edge-institution', 'neighborhood',
     'restore-settings', 'restore-default-layout', 'clear-canvas'].forEach(id => {
      const el = document.getElementById(id);
      if (el) el.disabled = loading;
    });
    // Chip remove buttons (created dynamically, so query each time)
    document.querySelectorAll('.chip-remove').forEach(btn => { btn.disabled = loading; });
    // Dim the search input so it's clear adding is blocked
    const si = document.getElementById('search-input');
    if (si) si.disabled = loading;
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
      const nb = getNeighborhood();
      params.set('depth', nb.depth);
      params.set('top_k', nb.topK);

      const source = new EventSource(`${API_BASE}/api/graph/expand?${params}`);
      state.activeSource = source;

      const finish = () => {
        if (state.activeSource === source) state.activeSource = null;
        setLoading(false);
        resolve();
      };

      source.addEventListener('node', e => addOrUpdateNode(JSON.parse(e.data)));
      source.addEventListener('edge', e => addEdge(JSON.parse(e.data)));

      source.addEventListener('path', e => {
        const d = JSON.parse(e.data);
        state.paths.set(pairKey(d.from_id, d.to_id), d);
        renderDegrees();
      });

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
        source.close();
        hideProgress();
        rescaleExpansionNodes();
        applyNameVisibility();
        applyEdgeFade();
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

  function runLayout() {
    if (!cy.nodes().length) return;
    const lp = getLayoutParams();

    // Pin the origin researchers symmetrically around the center so they sit in
    // the middle and their neighborhoods spring outward. The separation grows
    // with graph size (and the Spacing slider) so big neighborhoods don't pile up.
    const origins = cy.nodes('[type="origin"]');
    const n = origins.length;
    const spread = Math.round((140 + Math.sqrt(cy.nodes().length) * 26) * lp.spreadFactor);
    const fixed = origins.map((node, i) => {
      let position;
      if (n <= 1) position = { x: 0, y: 0 };
      else if (n === 2) position = { x: i === 0 ? -spread : spread, y: 0 };
      else {
        const a = (i / n) * 2 * Math.PI;
        position = { x: Math.round(spread * Math.cos(a)), y: Math.round(spread * Math.sin(a)) };
      }
      return { nodeId: node.id(), position };
    });

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
        padding: 60,
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
      padding: 60,
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
        `<div class="degrees-count">${escHtml(degreesLabel(d))}</div>`;
      if (d.found && Array.isArray(d.steps) && d.steps.length) {
        html += '<ol class="degrees-steps">';
        for (const s of d.steps) {
          html +=
            `<li><span class="step-people">${escHtml(s.from_name)} → ${escHtml(s.to_name)}</span>` +
            `<span class="step-via">${escHtml(stepPhrase(s))}</span></li>`;
        }
        html += '</ol>';
      }
      li.innerHTML = html;
      list.appendChild(li);
    }
    panel.classList.toggle('hidden', state.paths.size === 0);
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

  // Restore any previously saved session on page load.
  loadSavedState();
})();
