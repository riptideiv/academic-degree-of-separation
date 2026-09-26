(() => {
  'use strict';

  const byId = id => document.getElementById(id);
  const login = byId('analytics-login');
  const input = byId('admin-token');
  const days = byId('analytics-days');
  const results = byId('analytics-results');
  const message = byId('dashboard-message');
  const signOut = byId('sign-out');
  const refresh = byId('refresh-analytics');
  const totals = ['unique_visitors', 'page_views', 'searches', 'searching_visitors', 'graph_runs', 'explorer_runs'];
  const events = ['author_search', 'work_search', 'institution_search'];
  const number = value => Number.isFinite(value) && value >= 0 ? value.toLocaleString() : '—';
  let token = '';
  let generation = 0;
  let pending = null;

  function clearResults() {
    results.hidden = true;
    totals.forEach(key => { byId(`total-${key}`).textContent = '—'; });
    events.forEach(key => { byId(`event-${key}`).textContent = '—'; });
    byId('daily-activity').replaceChildren();
    byId('period-description').textContent = '';
    byId('storage-status').textContent = '';
  }

  function setBusy(busy) {
    byId('open-dashboard').disabled = busy;
    refresh.disabled = busy;
    results.setAttribute('aria-busy', String(busy));
  }

  function forgetToken() {
    token = '';
    input.value = '';
    login.hidden = false;
    signOut.hidden = true;
    refresh.hidden = true;
    clearResults();
  }

  function render(data) {
    if (!data?.period || !data.totals || !data.events || !Array.isArray(data.daily) || !data.status) {
      throw new Error('invalid-response');
    }
    totals.forEach(key => { byId(`total-${key}`).textContent = number(data.totals[key]); });
    events.forEach(key => { byId(`event-${key}`).textContent = number(data.events[key] ?? 0); });
    byId('period-description').textContent = `${data.period.start_date} – ${data.period.end_date} · ${data.period.timezone}`;
    const rows = [...data.daily].sort((a, b) => String(b.date).localeCompare(String(a.date))).map(day => {
      const row = document.createElement('tr');
      ['date', ...totals].forEach(key => {
        const cell = document.createElement('td');
        cell.textContent = key === 'date' ? day.date : number(day[key]);
        row.append(cell);
      });
      return row;
    });
    byId('daily-activity').replaceChildren(...rows);

    const status = data.status;
    const notices = [];
    if (!status.enabled) notices.push('Collection is disabled. These are previously recorded counts.');
    if (!status.available) notices.push('Analytics storage is unavailable. Counts may be incomplete.');
    if (status.dropped_events > 0) notices.push(`${number(status.dropped_events)} events were dropped by this server process. Counts may be incomplete.`);
    if (status.pending_events > 0) notices.push(`${number(status.pending_events)} events are waiting to be saved by this server process.`);
    notices.push(`Storage: ${status.storage}. Retention: ${number(status.retention_days)} days.`);
    if (status.storage === 'sqlite') notices.push('SQLite needs a persistent disk to keep counts across server redeploys.');
    byId('storage-status').textContent = notices.join(' ');
    byId('storage-status').classList.toggle('warning', !status.enabled || !status.available || status.dropped_events > 0);
    results.hidden = false;
  }

  async function load() {
    if (!token) return;
    const requestId = ++generation;
    pending?.abort();
    const controller = new AbortController();
    pending = controller;
    const timeout = setTimeout(() => controller.abort(), 15000);
    message.textContent = 'Loading usage counts…';
    message.classList.remove('error');
    clearResults();
    setBusy(true);
    signOut.hidden = false;
    try {
      const period = ['7', '30', '90', '365'].includes(days.value) ? days.value : '30';
      const response = await fetch(`${window.RESEARCHER_API_BASE ?? ''}/api/analytics/summary?days=${period}`, {
        headers: { Authorization: `Bearer ${token}` },
        credentials: 'omit', cache: 'no-store', referrerPolicy: 'no-referrer', signal: controller.signal,
      });
      if (requestId !== generation) return;
      if (response.status === 403) {
        forgetToken();
        throw new Error('unauthorized');
      }
      if (response.status === 503) throw new Error('unavailable');
      if (!response.ok) throw new Error('request-failed');
      const data = await response.json();
      if (requestId !== generation) return;
      render(data);
      input.value = '';
      login.hidden = true;
      refresh.hidden = false;
      message.textContent = '';
    } catch (error) {
      if (requestId !== generation) return;
      clearResults();
      const messages = {
        unauthorized: 'Access denied. Check the analytics admin token configured on your server.',
        unavailable: 'Analytics storage is unavailable. Check the server configuration and try again.',
        'invalid-response': 'The server returned an unreadable analytics response. Check that the app and server are up to date.',
      };
      message.textContent = messages[error?.message] || (error?.name === 'AbortError'
        ? 'The dashboard request timed out. Try again.'
        : 'Could not load analytics. Check your connection and try again.');
      message.classList.add('error');
      if (token) refresh.hidden = false;
    } finally {
      clearTimeout(timeout);
      if (requestId === generation) {
        pending = null;
        setBusy(false);
      }
    }
  }

  login.addEventListener('submit', event => {
    event.preventDefault();
    const value = input.value.trim();
    if (!value) { input.focus(); return; }
    token = value;
    void load();
  });
  days.addEventListener('change', () => { void load(); });
  refresh.addEventListener('click', () => { void load(); });
  signOut.addEventListener('click', () => {
    generation += 1;
    pending?.abort();
    pending = null;
    forgetToken();
    setBusy(false);
    message.classList.remove('error');
    message.textContent = 'Signed out. The token and displayed counts have been cleared.';
    input.focus();
  });
})();
