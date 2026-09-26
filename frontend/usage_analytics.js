// First-party, best-effort usage counts. Never pass search content to this module.
(() => {
  'use strict';

  const endpoint = `${window.RESEARCHER_API_BASE ?? ''}/api/analytics/events`;
  const allowed = new Set([
    'page_view', 'author_search', 'work_search', 'institution_search', 'graph_run', 'explorer_run',
  ]);
  const pending = [];
  const maxPending = 32;
  const timeoutMs = 4000;
  const visitorStorageKey = 'academiaUsageVisitorV1';
  const visitorPattern = /^[0-9a-f]{32}$/;
  let visitorId = '';
  let running = false;
  let stopped = false;
  let pageQueued = false;

  function optedOut() {
    return navigator.globalPrivacyControl === true
      || ['1', 'yes'].includes(String(navigator.doNotTrack ?? window.doNotTrack).toLowerCase());
  }

  function getVisitorId() {
    if (visitorId) return visitorId;
    try {
      const saved = window.localStorage.getItem(visitorStorageKey);
      if (typeof saved === 'string' && visitorPattern.test(saved)) visitorId = saved;
    } catch { /* Storage may be unavailable in an embedded/private browser. */ }
    if (!visitorId) {
      const bytes = new Uint8Array(16);
      window.crypto.getRandomValues(bytes);
      visitorId = Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');
      try { window.localStorage.setItem(visitorStorageKey, visitorId); } catch { /* Keep one ID for this page. */ }
    }
    return visitorId;
  }

  async function send(event) {
    let timer;
    const controller = new AbortController();
    try {
      // The timeout settles even if a browser/network implementation ignores abort.
      return await Promise.race([
        fetch(endpoint, {
          method: 'POST',
          credentials: 'omit',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ event, visitor_id: getVisitorId() }),
          cache: 'no-store',
          referrerPolicy: 'no-referrer',
          signal: controller.signal,
          keepalive: true,
        }).then(response => response.status === 202),
        new Promise(resolve => {
          timer = setTimeout(() => { controller.abort(); resolve(false); }, timeoutMs);
        }),
      ]);
    } catch {
      return false;
    } finally {
      clearTimeout(timer);
    }
  }

  async function drain() {
    if (running || stopped) return;
    running = true;
    try {
      while (pending.length && !stopped) {
        // Send page views and actions in order. No retries or unbounded backlog.
        if (optedOut() || !await send(pending.shift())) stopped = true;
      }
    } catch {
      stopped = true;
    } finally {
      running = false;
      if (stopped) pending.length = 0;
    }
  }

  function track(event) {
    try {
      if (stopped || optedOut() || !allowed.has(event) || pending.length >= maxPending) return;
      if (event === 'page_view') {
        if (pageQueued) return;
        pageQueued = true;
      }
      pending.push(event);
      void drain();
    } catch {
      // Analytics must never interfere with research or require browser storage.
      stopped = true;
      pending.length = 0;
    }
  }

  window.UsageAnalytics = Object.freeze({ track });
  track('page_view');
})();
