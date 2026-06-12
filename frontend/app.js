(function () {
    const API_BASE = window.RESEARCHER_API_BASE ?? '';

    let selectedA = null, selectedB = null, activeSource = null;

    const inputA = document.getElementById('input-a');
    const inputB = document.getElementById('input-b');
    const dropdownA = document.getElementById('dropdown-a');
    const dropdownB = document.getElementById('dropdown-b');
    const badgeA = document.getElementById('badge-a');
    const badgeB = document.getElementById('badge-b');
    const edgeCheckboxes = ['coauthor', 'citation', 'institution']
        .map(e => document.getElementById(`edge-${e}`));
    const findBtn = document.getElementById('find-btn');
    const progressArea = document.getElementById('progress-area');
    const progressLog = document.getElementById('progress-log');
    const resultArea = document.getElementById('result-area');
    const pathChain = document.getElementById('path-chain');
    const resetBtn = document.getElementById('reset-btn');

    function debounce(fn, ms) {
        let timer;
        return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), ms); };
    }

    function escapeHtml(str) {
        const d = document.createElement('div');
        d.appendChild(document.createTextNode(str || ''));
        return d.innerHTML;
    }

    async function searchAuthors(query) {
        if (query.length < 2) return [];
        try {
            const resp = await fetch(`${API_BASE}/api/authors?q=${encodeURIComponent(query)}`);
            if (!resp.ok) return [];
            return await resp.json();
        } catch { return []; }
    }

    function renderDropdown(dropdown, authors, onSelect) {
        dropdown.innerHTML = '';
        authors.forEach(author => {
            const li = document.createElement('li');
            li.innerHTML = `
                <div class="author-name">${escapeHtml(author.display_name)}</div>
                <div class="author-meta">${escapeHtml(author.institution || 'Unknown institution')} · ${author.works_count} works</div>
            `;
            li.addEventListener('click', () => onSelect(author));
            dropdown.appendChild(li);
        });
    }

    function selectAuthor(which, author) {
        if (which === 'a') {
            selectedA = author;
            inputA.value = author.display_name;
            badgeA.textContent = `${author.display_name} · ${author.institution || 'Unknown'}`;
            badgeA.classList.remove('hidden');
            dropdownA.innerHTML = '';
        } else {
            selectedB = author;
            inputB.value = author.display_name;
            badgeB.textContent = `${author.display_name} · ${author.institution || 'Unknown'}`;
            badgeB.classList.remove('hidden');
            dropdownB.innerHTML = '';
        }
        updateFindBtn();
    }

    function updateFindBtn() {
        const anyEdge = edgeCheckboxes.some(cb => cb.checked);
        findBtn.disabled = !(selectedA && selectedB && anyEdge);
    }

    edgeCheckboxes.forEach(cb => cb.addEventListener('change', updateFindBtn));

    const debouncedA = debounce(async (q) => {
        if (selectedA && q === selectedA.display_name) return;
        selectedA = null; badgeA.classList.add('hidden'); updateFindBtn();
        renderDropdown(dropdownA, await searchAuthors(q), a => selectAuthor('a', a));
    }, 300);

    const debouncedB = debounce(async (q) => {
        if (selectedB && q === selectedB.display_name) return;
        selectedB = null; badgeB.classList.add('hidden'); updateFindBtn();
        renderDropdown(dropdownB, await searchAuthors(q), b => selectAuthor('b', b));
    }, 300);

    inputA.addEventListener('input', e => debouncedA(e.target.value));
    inputB.addEventListener('input', e => debouncedB(e.target.value));
    window.addEventListener('pagehide', () => { activeSource?.close(); });
    document.addEventListener('click', e => {
        if (!e.target.closest('#field-a')) dropdownA.innerHTML = '';
        if (!e.target.closest('#field-b')) dropdownB.innerHTML = '';
    });

    findBtn.addEventListener('click', startSearch);
    resetBtn.addEventListener('click', resetForm);

    function startSearch() {
        if (!selectedA || !selectedB) return;
        if (selectedA.id === selectedB.id) {
            showError('Select two different researchers');
            return;
        }
        const enabledEdges = ['coauthor', 'citation', 'institution']
            .filter(e => document.getElementById(`edge-${e}`).checked);
        if (enabledEdges.length === 0) {
            showError('Select at least one edge type');
            return;
        }
        if (activeSource) { activeSource.close(); activeSource = null; }

        document.getElementById('search-form').classList.add('hidden');
        progressArea.classList.remove('hidden');
        resultArea.classList.add('hidden');
        progressLog.innerHTML = '';

        const params = new URLSearchParams({ from: selectedA.id, to: selectedB.id });
        enabledEdges.forEach(e => params.append('edges', e));
        const url = `${API_BASE}/api/path?${params}`;
        const source = new EventSource(url);
        activeSource = source;

        source.addEventListener('progress', e => {
            const data = JSON.parse(e.data);
            const line = document.createElement('div');
            line.className = 'progress-line';
            line.textContent = data.message;
            progressLog.appendChild(line);
            progressLog.scrollTop = progressLog.scrollHeight;
        });

        source.addEventListener('result', e => {
            source.close(); activeSource = null;
            const data = JSON.parse(e.data);
            progressArea.classList.add('hidden');
            resultArea.classList.remove('hidden');
            data.found ? renderChain(data.path) : showError(`No path found. ${data.reason || ''}`);
        });

        source.addEventListener('app_error', e => {
            source.close(); activeSource = null;
            progressArea.classList.add('hidden');
            resultArea.classList.remove('hidden');
            let msg = 'An error occurred. Please try again.';
            try { msg = JSON.parse(e.data).message; } catch {}
            showError(msg);
        });

        source.onerror = () => {
            if (activeSource === source) {
                source.close();
                activeSource = null;
                progressArea.classList.add('hidden');
                resultArea.classList.remove('hidden');
                showError('Connection lost. Please try again.');
            }
        };
    }

    function showError(msg) {
        pathChain.innerHTML = `<div class="error-message">${escapeHtml(msg)}</div>`;
    }

    function formatConnectionType(type) {
        return { coauthor: 'co-author on', citation: 'citation connection', institution: 'colleague at' }[type] || type;
    }

    function renderChain(path) {
        const chain = document.createElement('div');
        chain.className = 'chain';

        path.forEach((step, i) => {
            const node = document.createElement('div');
            node.className = 'chain-node';

            const a = document.createElement('a');
            a.className = 'chain-author';
            a.textContent = step.author_name;
            a.href = `https://openalex.org/${step.author_id}`;
            a.target = '_blank';
            a.rel = 'noopener noreferrer';
            node.appendChild(a);

            if (step.connection_to_next && i < path.length - 1) {
                const conn = document.createElement('div');
                conn.className = 'chain-connection';
                conn.innerHTML = `<span class="chain-arrow">↓</span><span>${escapeHtml(formatConnectionType(step.connection_to_next))}</span>`;
                if (step.label) {
                    const lbl = document.createElement('span');
                    lbl.className = 'chain-label';
                    lbl.textContent = `"${step.label}"`;
                    conn.appendChild(lbl);
                }
                node.appendChild(conn);
            }

            chain.appendChild(node);
        });

        const hops = path.length - 1;
        pathChain.innerHTML = '';
        pathChain.appendChild(chain);
        const hopLabel = document.createElement('p');
        hopLabel.className = 'chain-hop-label';
        hopLabel.textContent = `Connected in ${hops} hop${hops !== 1 ? 's' : ''}`;
        pathChain.appendChild(hopLabel);
    }

    function resetForm() {
        if (activeSource) { activeSource.close(); activeSource = null; }
        dropdownA.innerHTML = dropdownB.innerHTML = '';
        document.getElementById('search-form').classList.remove('hidden');
        progressArea.classList.add('hidden');
        resultArea.classList.add('hidden');
    }
})();
