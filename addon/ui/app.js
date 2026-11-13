/* ==========================================================================
   WattMosaic Engine panel — application script
   --------------------------------------------------------------------------
   Plain ES2020, no framework, no build step, no network access beyond this
   origin.  The gateway serves this file from inside the App container, so it
   must work under Home Assistant ingress, where the panel lives under a path
   prefix such as /api/hassio_ingress/<token>/.

   Design rules that matter:

   * Every value rendered here comes from the engine API.  Nothing is invented:
     an unknown device says "unknown", not 0 W.
   * All engine-provided strings are inserted as *text*, never as HTML.  A
     device name is user data; it must not be able to inject markup.
   * Requests are relative to the document's own directory, so the same file
     works at "/" (standalone) and under the ingress prefix.
   * Polling pauses while the tab is hidden and resumes on focus.
   ========================================================================== */

'use strict';

(() => {
  /* ---------------------------------------------------------------------- *
   * Base path + API client
   * ---------------------------------------------------------------------- */

  // Directory of the current document.  Under ingress this is
  // /api/hassio_ingress/<token>/; standalone it is /.  Resolving against
  // document.baseURI keeps relative links correct when the URL has no slash.
  const BASE = (() => {
    const url = new URL('.', document.baseURI);
    return url.pathname.endsWith('/') ? url.pathname : url.pathname + '/';
  })();

  const API_PREFIX = `${BASE}api/v1`;

  class ApiError extends Error {
    constructor(message, { status = 0, code = 'network_error', retryable = false, detail = '' } = {}) {
      super(message);
      this.name = 'ApiError';
      this.status = status;
      this.code = code;
      this.retryable = retryable;
      this.detail = detail;
    }
  }

  async function api(path, { method = 'GET', body = null, signal = null } = {}) {
    const init = {
      method,
      cache: 'no-store',
      credentials: 'same-origin',
      headers: { Accept: 'application/json' },
      signal,
    };
    if (body !== null) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }

    let response;
    try {
      response = await fetch(`${API_PREFIX}${path}`, init);
    } catch (err) {
      if (err && err.name === 'AbortError') throw err;
      throw new ApiError('The engine did not answer.', { code: 'unreachable' });
    }

    const text = await response.text();
    let payload = null;
    if (text) {
      try {
        payload = JSON.parse(text);
      } catch {
        payload = null;
      }
    }

    if (!response.ok) {
      const message =
        (payload && payload.error) || `Request failed (HTTP ${response.status})`;
      throw new ApiError(message, {
        status: response.status,
        code: (payload && payload.error) || 'http_error',
        retryable: Boolean(payload && payload.retryable),
        detail: (payload && payload.detail) || '',
      });
    }
    return payload;
  }

  const get = (path, signal) => api(path, { signal });
  const post = (path, body) => api(path, { method: 'POST', body });

  /* ---------------------------------------------------------------------- *
   * Small DOM helpers
   * ---------------------------------------------------------------------- */

  const el = (tag, props = {}, children = []) => {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (key === 'class') node.className = value;
      else if (key === 'text') node.textContent = String(value);
      // There is deliberately no ``html`` key: every engine string is inserted
      // as text so a device name can never become markup.
      else if (key === 'dataset') Object.assign(node.dataset, value);
      else if (key === 'style') node.setAttribute('style', value);
      else if (key.startsWith('on') && typeof value === 'function') {
        node.addEventListener(key.slice(2).toLowerCase(), value);
      } else if (key in node && key !== 'list') {
        try { node[key] = value; } catch { node.setAttribute(key, String(value)); }
      } else {
        node.setAttribute(key, String(value));
      }
    }
    for (const child of [].concat(children)) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  };

  const frag = (children) => {
    const f = document.createDocumentFragment();
    for (const c of [].concat(children)) {
      if (c === null || c === undefined || c === false) continue;
      f.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return f;
  };

  const clear = (node) => { while (node.firstChild) node.removeChild(node.firstChild); };

  /* ---------------------------------------------------------------------- *
   * Formatting
   * ---------------------------------------------------------------------- */

  const num = (value, digits = 0) => {
    if (value === null || value === undefined || value === '') return '—';
    const n = Number(value);
    if (!Number.isFinite(n)) return '—';
    return n.toLocaleString(undefined, {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    });
  };

  const watts = (value, digits = null) => {
    if (value === null || value === undefined) return '—';
    const n = Number(value);
    if (!Number.isFinite(n)) return '—';
    const abs = Math.abs(n);
    const d = digits === null ? (abs >= 100 ? 0 : abs >= 10 ? 1 : 2) : digits;
    return `${num(n, d)} W`;
  };

  const kWh = (value) => {
    if (value === null || value === undefined) return '—';
    const n = Number(value);
    if (!Number.isFinite(n)) return '—';
    return `${num(n, n >= 100 ? 1 : 3)} kWh`;
  };

  const percent = (value, digits = 0) => {
    if (value === null || value === undefined) return '—';
    const n = Number(value);
    if (!Number.isFinite(n)) return '—';
    return `${num(n * 100, digits)}%`;
  };

  const duration = (seconds) => {
    const s = Number(seconds);
    if (!Number.isFinite(s) || s < 0) return '—';
    if (s < 60) return `${Math.round(s)}s`;
    const m = Math.floor(s / 60);
    if (m < 60) return `${m}m ${Math.round(s % 60)}s`;
    const h = Math.floor(m / 60);
    if (h < 48) return `${h}h ${m % 60}m`;
    return `${Math.floor(h / 24)}d ${h % 24}h`;
  };

  const clock = (iso) => {
    if (!iso) return '—';
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return '—';
    return d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  };

  const ago = (iso) => {
    if (!iso) return '—';
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return '—';
    const s = (Date.now() - d.getTime()) / 1000;
    if (s < -5) return clock(iso);
    if (s < 60) return `${Math.max(0, Math.round(s))}s ago`;
    if (s < 3600) return `${Math.round(s / 60)}m ago`;
    if (s < 86400) return `${Math.round(s / 3600)}h ago`;
    return d.toLocaleDateString();
  };

  const titleCase = (text) =>
    String(text ?? '')
      .replace(/[_-]+/g, ' ')
      .replace(/\b\w/g, (c) => c.toUpperCase());

  const STRINGS = {
    ok: 'OK',
    degraded: 'Degraded',
    error: 'Error',
    unknown: 'Unknown',
    good: 'Good',
    stale: 'Stale',
    bad: 'Bad',
    missing: 'Missing',
    candidate: 'Candidate',
    confirmed: 'Confirmed',
    rejected: 'Rejected',
    direct_measured: 'Direct measured',
    inferred: 'Inferred',
    model_estimated: 'Model estimated',
    configured_prior: 'Configured prior',
    identified: 'Identified',
    probable: 'Probable',
    dedicated: 'Dedicated CT',
    shared: 'Shared CT',
    home_total_only: 'Home total only',
    unmonitored: 'Unmonitored',
    l1: 'L1',
    l2: 'L2',
    both: 'Both legs',
  };
  const label = (value) => STRINGS[value] || titleCase(value || 'unknown');

  const toneFor = (status) => {
    switch (String(status || '').toLowerCase()) {
      case 'ok':
      case 'good':
      case 'confirmed':
      case 'identified':
        return 'ok';
      case 'degraded':
      case 'stale':
      case 'probable':
      case 'candidate':
        return 'warn';
      case 'error':
      case 'bad':
      case 'missing':
      case 'rejected':
        return 'bad';
      default:
        return 'mute';
    }
  };

  const sourceTone = (source) => {
    switch (source) {
      case 'direct_measured': return 'ok';
      case 'inferred': return 'info';
      case 'model_estimated': return 'accent';
      case 'configured_prior': return 'warn';
      default: return 'mute';
    }
  };

  const SOURCE_SWATCH = {
    direct_measured: 'var(--ok)',
    inferred: 'var(--info)',
    model_estimated: 'var(--accent)',
    configured_prior: 'var(--warn)',
    unknown: 'var(--mute)',
  };

  /* ---------------------------------------------------------------------- *
   * Shared components
   * ---------------------------------------------------------------------- */

  // ``title`` is forwarded verbatim: the engine puts the measurement evidence
  // behind a pill and the reader deserves to see it on hover.  It is inserted
  // through ``setAttribute`` by ``el``, so it can never become markup.
  const badge = (text, tone = 'mute', { dot = false, title = null } = {}) =>
    el('span', { class: 'badge', dataset: { tone }, title: title || null }, [
      dot ? el('span', { class: 'dot', 'aria-hidden': 'true' }) : null,
      text,
    ]);

  const statusBadge = (status) => badge(label(status), toneFor(status), { dot: true });

  const TIER_ORDER = [
    'tier_0_unknown',
    'tier_1_modelled',
    'tier_2_inferred',
    'tier_3_measured',
    'tier_4_verified',
  ];

  const tierInfo = (tier) => {
    const key = String(tier || 'tier_0_unknown');
    const index = Math.max(0, TIER_ORDER.indexOf(key));
    const words = key.replace(/^tier_\d_/, '').replace(/_/g, ' ');
    return {
      key,
      index,
      label: titleCase(words || 'unknown'),
      short: `T${index}`,
      tone: ['mute', 'warn', 'info', 'ok', 'ok'][index] || 'mute',
    };
  };

  const tierBadge = (tier) => {
    const info = tierInfo(tier);
    const bars = el(
      'span',
      { class: 'tier-bars', 'aria-hidden': 'true' },
      TIER_ORDER.map((_, i) => el('i', { class: i <= info.index ? 'on' : '' }))
    );
    return el(
      'span',
      {
        class: 'badge tier',
        dataset: { tone: info.tone },
        title: `${info.key} — an evidence grade, not a probability`,
      },
      [bars, info.label]
    );
  };

  const statCard = (labelText, value, { unit = null, hint = null, tone = null, id = null } = {}) => {
    const valueNode = el('div', { class: 'stat-value' }, [
      value,
      unit ? el('span', { class: 'unit' }, unit) : null,
    ]);
    if (id) valueNode.id = id;
    return el('div', { class: 'card stat', dataset: tone ? { tone } : {} }, [
      el('div', { class: 'stat-label' }, labelText),
      valueNode,
      hint ? el('div', { class: 'stat-hint' }, hint) : null,
    ]);
  };

  const card = (title, body, { actions = null, foot = null, flush = false, id = null } = {}) => {
    const node = el('section', { class: 'card' }, [
      el('div', { class: 'card-head' }, [
        el('h2', {}, title),
        el('span', { class: 'spacer' }),
        ...(actions || []),
      ]),
      el('div', { class: `card-body${flush ? ' flush' : ''}` }, body),
      foot ? el('div', { class: 'card-foot' }, foot) : null,
    ]);
    if (id) node.id = id;
    return node;
  };

  const kv = (rows) =>
    el(
      'dl',
      { class: 'kv' },
      rows
        .filter(([, value]) => value !== undefined)
        .map(([key, value]) => frag([el('dt', {}, key), el('dd', {}, value)]))
    );

  const notice = (title, text, tone = 'warn') =>
    el('div', { class: 'notice', dataset: { tone } }, [
      el('div', { class: 'banner-icon', 'aria-hidden': 'true' }, '!'),
      el('div', {}, [el('strong', {}, title), text ? el('p', {}, text) : null]),
    ]);

  const emptyState = (title, text, { tone = null, action = null } = {}) =>
    el('div', { class: 'state', dataset: tone ? { tone } : {} }, [
      el('div', { class: 'state-icon', 'aria-hidden': 'true' }, '∅'),
      el('h2', {}, title),
      text ? el('p', {}, text) : null,
      action || null,
    ]);

  const errorState = (title, err, retry) =>
    el('div', { class: 'state', dataset: { tone: 'bad' } }, [
      el('div', { class: 'state-icon', 'aria-hidden': 'true' }, '!'),
      el('h2', {}, title),
      el('p', {}, describeError(err)),
      retry ? el('button', { class: 'btn btn-primary', onclick: retry }, 'Try again') : null,
    ]);

  const loadingCards = (count = 4) =>
    el(
      'div',
      { class: 'grid cols-4' },
      Array.from({ length: count }, () => el('div', { class: 'skeleton h-stat' }))
    );

  const describeError = (err) => {
    if (!err) return 'Something went wrong.';
    if (err instanceof ApiError) {
      if (err.code === 'unreachable') {
        return 'The engine did not answer. It may still be starting, or the App may be stopped.';
      }
      if (err.code === 'engine_unavailable') {
        return 'The engine is running but its inference backend is not wired. Check the App log.';
      }
      if (err.status === 503) return 'The engine is busy or still starting. Retry in a moment.';
      const detail = err.detail ? ` ${err.detail}` : '';
      return `${err.message}.${detail}`.trim();
    }
    return err.message || String(err);
  };

  /* Toast notifications. */
  const toasts = () => document.getElementById('toasts');
  const notify = (title, sub = '', tone = 'ok') => {
    const node = el('div', { class: 'toast', dataset: { tone } }, [
      el('strong', {}, title),
      sub ? el('div', { class: 'sub' }, sub) : null,
    ]);
    toasts().append(node);
    setTimeout(() => {
      node.style.opacity = '0';
      node.style.transition = 'opacity .2s ease';
      setTimeout(() => node.remove(), 220);
    }, 4200);
  };

  /* ---------------------------------------------------------------------- *
   * Application state
   * ---------------------------------------------------------------------- */

  const state = {
    view: 'overview',
    deviceId: null,
    tiers: null,
    tierMap: new Map(),
    pollTimer: null,
    lastHealth: null,
    lastSnapshot: null,
    lastDevices: null,
    lastError: null,
    abort: null,
    devicesFilter: '',
    devicesSort: 'power',
    qualityFilter: 'all',
    // The hierarchy view's own state lives here, not in the DOM, so a re-render
    // (a poll, a refresh, a route change) does not reset what the user opened.
    tree: {
      open: new Set(),      // node ids the user explicitly expanded
      closed: new Set(),    // node ids the user explicitly collapsed
      filter: '',
    },
    // Nesting compares a historical window, so its answer stays true for
    // minutes.  Caching it means revisiting the tab paints instantly instead
    // of paying the whole comparison again.
    nesting: { data: null, at: 0, inflight: false },
    // Placement discovery is explicit: it only runs when the user asks, so its
    // result is kept here and survives a repaint of the same tab.
    discover: { data: null, running: false, error: null },
  };

  const NESTING_TTL_MS = 10 * 60 * 1000;
  // A refresh younger than this is not worth repeating: a just-completed
  // comparison would otherwise be fetched again the instant the tab is
  // reopened.
  const NESTING_REFRESH_AFTER_MS = 60 * 1000;
  const NESTING_PATH = '/topology/nesting?hours=12';

  const viewRoot = () => document.getElementById('view');

  /* ---------------------------------------------------------------------- *
   * Connection indicator
   * ---------------------------------------------------------------------- */

  function setConnection(health, err) {
    const node = document.getElementById('conn');
    const text = document.getElementById('conn-text');
    if (!node || !text) return;
    if (err) {
      node.dataset.state = 'offline';
      text.textContent = 'Engine unreachable';
      return;
    }
    const status = String((health && health.status) || 'unknown');
    node.dataset.state = status === 'ok' ? 'ok' : status === 'degraded' ? 'degraded' : status === 'error' ? 'error' : 'offline';
    text.textContent =
      status === 'ok' ? 'Engine OK' : status === 'degraded' ? 'Engine degraded' : label(status);
  }

  function setBanner(title, text, tone = 'bad') {
    const banner = document.getElementById('banner');
    document.getElementById('banner-title').textContent = title;
    document.getElementById('banner-text').textContent = text;
    banner.dataset.kind = tone;
    banner.hidden = false;
  }

  function hideBanner() {
    document.getElementById('banner').hidden = true;
  }

  function setFooter(health) {
    const version = document.getElementById('footer-version');
    const contract = document.getElementById('footer-contract');
    if (version) version.textContent = (health && health.engine_version) || '—';
    if (contract) contract.textContent = (health && health.contract_version) || '—';
  }

  /* ---------------------------------------------------------------------- *
   * Tier vocabulary (fetched once; the engine owns the labels)
   * ---------------------------------------------------------------------- */

  async function loadTiers() {
    if (state.tiers) return state.tiers;
    try {
      const payload = await get('/confidence_tiers');
      if (payload && Array.isArray(payload.tiers)) {
        state.tiers = payload;
        for (const tier of payload.tiers) {
          state.tierMap.set(tier.tier, tier);
        }
      }
    } catch {
      // Static fallback: the vocabulary is stable, and a missing fetch must not
      // stop the panel from rendering.  Labels stay honest either way.
      state.tiers = { tiers: [], calibrated: false };
    }
    return state.tiers;
  }

  const tierDescription = (tier) => {
    const entry = state.tierMap.get(String(tier));
    return entry && entry.description ? entry.description : '';
  };

  /* ---------------------------------------------------------------------- *
   * Router
   * ---------------------------------------------------------------------- */

  function parseHash() {
    const raw = (location.hash || '#/overview').replace(/^#\/?/, '');
    const [section, arg] = raw.split('/');
    if (section === 'device' && arg) return { view: 'device', deviceId: decodeURIComponent(arg) };
    const known = ['overview', 'devices', 'topology', 'quality', 'diagnostics'];
    if (known.includes(section)) return { view: section, deviceId: null };
    return { view: 'overview', deviceId: null };
  }

  function applyRoute() {
    const route = parseHash();
    state.view = route.view;
    state.deviceId = route.deviceId;
    for (const link of document.querySelectorAll('[data-nav]')) {
      const target = link.getAttribute('data-nav');
      const active =
        target === state.view || (target === 'devices' && state.view === 'device');
      if (active) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    }
    document.title =
      state.view === 'device'
        ? 'Device · WattMosaic'
        : `${titleCase(state.view)} · WattMosaic`;
    render();
  }

  /* ---------------------------------------------------------------------- *
   * Data fetchers
   * ---------------------------------------------------------------------- */

  async function fetchOverview(signal) {
    const [health, status, snapshot, devices] = await Promise.all([
      get('/health', signal),
      get('/status', signal).catch(() => null),
      get('/snapshot', signal).catch(() => null),
      get('/devices', signal).catch(() => []),
    ]);
    return { health, status, snapshot, devices };
  }

  async function fetchDevice(deviceId, signal) {
    const [device, models] = await Promise.all([
      get(`/devices/${encodeURIComponent(deviceId)}`, signal),
      get(`/models/${encodeURIComponent(deviceId)}`, signal).catch(() => null),
    ]);
    return { device, models };
  }

  /* ---------------------------------------------------------------------- *
   * Rendering
   * ---------------------------------------------------------------------- */

  async function render() {
    const root = viewRoot();
    if (!root) return;

    if (state.abort) state.abort.abort();
    state.abort = new AbortController();
    const signal = state.abort.signal;

    root.setAttribute('aria-busy', 'true');
    // The view container is a polite live region so route changes and value
    // ticks are announced.  A hierarchy of a hundred nodes is not a status
    // message: silence the region on the topology tab and let the explicit
    // role="status" summary announce the result instead.
    root.setAttribute('aria-live', state.view === 'topology' ? 'off' : 'polite');

    try {
      if (state.view === 'topology') {
        // The topology view paints its own page head and skeleton synchronously
        // inside renderTopology, before its first await.  It must not sit
        // behind the tier-vocabulary fetch (a cold start used to clear the view
        // and then leave it blank until the slowest request came back).
        await renderTopology(root, signal);
      } else {
        clear(root);
        root.append(loadingCards(4));
        await loadTiers();
        if (signal.aborted) return;
        // The skeleton is provisional: every view builds its own page head, so
        // it must be removed before the first view node is appended.  Leaving
        // it in place used to push a stale row of empty cards above the real
        // content.
        clear(root);
        switch (state.view) {
          case 'devices':
            await renderDevices(root, signal);
            break;
          case 'device':
            await renderDevice(root, signal);
            break;
          case 'quality':
            await renderQuality(root, signal);
            break;
          case 'diagnostics':
            await renderDiagnostics(root, signal);
            break;
          default:
            await renderOverview(root, signal);
        }
      }
      state.lastError = null;
      hideBanner();
    } catch (err) {
      if (err && err.name === 'AbortError') return;
      state.lastError = err;
      setConnection(null, err);
      clear(root);
      root.append(errorState('Could not load this view', err, () => render()));
    } finally {
      root.removeAttribute('aria-busy');
      if (state.view !== 'topology') root.setAttribute('aria-live', 'polite');
    }
  }

  /* --- Overview --------------------------------------------------------- */

  async function renderOverview(root, signal) {
    const { health, status, snapshot, devices } = await fetchOverview(signal);
    if (signal.aborted) return;

    state.lastHealth = health;
    state.lastSnapshot = snapshot;
    state.lastDevices = devices;
    setConnection(health);
    setFooter(health);
    root.append(overviewHeader());

    const recon = (snapshot && snapshot.reconciliation) || {};
    const topology = (health.details && health.details.topology) || {};

    if (topology.configured === false) {
      root.append(
        notice(
          'No topology configured',
          topology.reason ||
            'The engine is ingesting samples but has no CT topology, so it cannot emit device values. Configure the connection in the WattMosaic integration.',
          'warn'
        )
      );
    }
    if (health.status === 'error') {
      root.append(
        notice(
          'Engine reports an error',
          health.database && health.database !== 'ok'
            ? `Database: ${health.database}`
            : 'See Diagnostics for the full report.',
          'bad'
        )
      );
    }

    root.append(overviewStats(health, status, recon));
    root.append(overviewHero(recon, health, snapshot));

    const cols = el('div', { class: 'grid cols-2' }, [
      reconciliationCard(recon),
      systemCard(health, status),
    ]);
    root.append(cols);

    root.append(overviewDevices(devices, snapshot));
  }

  function overviewHeader() {
    return el('div', { class: 'page-head' }, [
      el('div', {}, [
        el('h1', {}, 'Home overview'),
        el('p', { class: 'lede' },
          'Measured total, what the engine could attribute, and the residual it refuses to hide.'),
      ]),
      el('div', { class: 'head-actions' }, [
        el('button', { class: 'btn', type: 'button', onclick: () => render() }, 'Refresh'),
      ]),
    ]);
  }

  function overviewStats(health, status, recon) {
    const accounted = recon.accounted_fraction;
    const qualityScore =
      (status && status.quality && status.quality.overall_score) ??
      (state.lastSnapshot && state.lastSnapshot.quality && state.lastSnapshot.quality.overall_score);
    return el('div', { class: 'grid cols-5', style: 'margin-bottom:16px' }, [
      statCard('Main power', num(recon.main_power_w, 0), {
        unit: 'W',
        hint: 'Sum of confirmed main CTs',
        tone: 'accent',
      }),
      statCard('Accounted', accounted === undefined ? '—' : percent(accounted, 0), {
        hint: 'Attributed to a device',
        tone: accounted >= 0.8 ? 'ok' : accounted >= 0.4 ? 'warn' : null,
      }),
      statCard('Unknown residual', num(recon.unknown_residual_w, 0), {
        unit: 'W',
        hint: 'Never redistributed',
        tone: 'warn',
      }),
      statCard('Engine', label(health.status), {
        hint: health.engine_version ? `v${health.engine_version}` : '',
        tone: toneFor(health.status),
      }),
      statCard('Data quality', qualityScore === undefined ? '—' : percent(qualityScore, 0), {
        hint: 'Aggregate channel health',
        tone: qualityScore >= 0.85 ? 'ok' : qualityScore >= 0.6 ? 'warn' : 'bad',
      }),
    ]);
  }

  function overviewHero(recon, health, snapshot) {
    const total = el('div', { class: 'hero-total' }, [
      el('span', { class: 'watts' }, num(recon.main_power_w, 0)),
      el('span', { class: 'unit' }, 'W'),
    ]);

    // Phase values are optional: a single-phase install reports neither, and
    // two "—" pills would imply a measurement that does not exist.
    const hasPhases =
      (recon.l1_power_w !== null && recon.l1_power_w !== undefined) ||
      (recon.l2_power_w !== null && recon.l2_power_w !== undefined);
    const phases = hasPhases
      ? el('div', { class: 'hero-phases' }, [
          phasePill('L1', recon.l1_power_w),
          phasePill('L2', recon.l2_power_w),
        ])
      : null;

    const meta = el('div', { class: 'hero-meta' }, [
      el('span', {}, `Topology v${recon.topology_version ?? '—'} · ${label(health.details?.topology?.status)}`),
      el('span', {}, `Updated ${ago((snapshot && snapshot.timestamp) || recon.timestamp)}`),
      el('span', {}, `Quality ${label(recon.quality_status)}`),
    ]);

    const main = el('section', { class: 'card hero-main' }, [
      el('div', { class: 'stat-label' }, 'Total incoming power'),
      total,
      phases,
      meta,
    ]);

    const breakdown = reconciliationBreakdown(recon);
    const side = card('Where the watts went', [breakdown], {
      foot: 'The residual stays unknown. It is a measurement of what the engine cannot attribute, not a device.',
    });

    return el('div', { class: 'hero' }, [main, side]);
  }

  function phasePill(name, value) {
    return el('div', { class: 'phase-pill' }, [
      el('div', { class: 'lbl' }, name),
      el('div', { class: 'v' }, value === null || value === undefined ? '—' : watts(value)),
    ]);
  }

  function reconciliationBreakdown(recon) {
    const rows = [
      ['direct_measured', 'Direct measured', recon.direct_measured_w, 'var(--ok)'],
      ['inferred', 'Inferred', recon.inferred_w, 'var(--info)'],
      ['model_estimated', 'Model estimated', recon.model_estimated_w, 'var(--accent)'],
      ['configured_prior', 'Configured prior', recon.configured_prior_w, 'var(--warn)'],
      ['unknown', 'Unknown residual', recon.unknown_residual_w, 'var(--mute)'],
    ].filter(([, , value]) => value !== undefined);

    const total = Math.max(
      Number(recon.main_power_w) || 0,
      rows.reduce((acc, [, , value]) => acc + (Number(value) || 0), 0)
    );

    const meter = el(
      'div',
      { class: 'meter', role: 'img', 'aria-label': 'Power attribution breakdown' },
      rows.map(([key, , value]) => {
        const pct = total > 0 ? (Math.max(0, Number(value) || 0) / total) * 100 : 0;
        return el('span', {
          class:
            key === 'direct_measured'
              ? 'seg-direct'
              : key === 'inferred'
                ? 'seg-inferred'
                : key === 'model_estimated'
                  ? 'seg-model'
                  : key === 'configured_prior'
                    ? 'seg-prior'
                    : 'seg-unknown',
          style: `width:${pct.toFixed(2)}%`,
          title: `${label(key)} ${watts(value)}`,
        });
      })
    );

    const legend = el(
      'div',
      { class: 'legend' },
      rows.map(([, name, value, colour]) =>
        el('span', { class: 'legend-item' }, [
          el('span', { class: 'legend-swatch', style: `background:${colour}` }),
          el('span', {}, name),
          el('span', { class: 'val' }, watts(value)),
        ])
      )
    );

    return el('div', {}, [meter, legend]);
  }

  function reconciliationCard(recon) {
    const main = Number(recon.main_power_w) || 0;
    const residual = Number(recon.unknown_residual_w) || 0;
    // The residual share is the unknown part of the measured mains.  It is not
    // ``reconciliation_error_ratio``: that is the least-squares fit error of the
    // device estimates against the CT rows, a different diagnostic.
    const residualShare = main > 0 ? residual / main : undefined;
    return card(
      'Reconciliation',
      [
        kv([
          ['Main (L1 + L2)', watts(recon.main_power_w)],
          ['Direct measured', watts(recon.direct_measured_w)],
          ['Inferred', watts(recon.inferred_w)],
          ['Model estimated', watts(recon.model_estimated_w)],
          ['Configured prior', watts(recon.configured_prior_w)],
          ['Unknown residual', watts(recon.unknown_residual_w)],
          ['Residual share', residualShare === undefined ? '—' : percent(residualShare, 1)],
          ['Measurement fit error', watts(recon.reconciliation_error_w)],
        ]),
        (recon.explanation || []).length
          ? el(
              'ul',
              { class: 'explain', style: 'margin-top:14px' },
              (recon.explanation || []).map((item) =>
                el('li', {}, [
                  el('span', { class: 'marker', 'aria-hidden': 'true' }, '·'),
                  el('span', {}, titleCase(item)),
                ])
              )
            )
          : null,
      ],
      { foot: 'Residual = measured mains minus what the model could attribute.' }
    );
  }

  function systemCard(health, status) {
    const inference = (status && status.inference) || {};
    const learning = (status && status.learning) || {};
    const details = health.details || {};
    const rows = [
      ['Home Assistant', health.ha_connected ? badge('Connected', 'ok', { dot: true }) : badge('Disconnected', 'warn', { dot: true })],
      ['Database', statusBadge(health.database)],
      ['Queue depth', String(health.queue_depth ?? '—')],
      ['Inference backend', details.inference_backend || '—'],
      ['Inference cycles', inference.cycles === undefined ? '—' : String(inference.cycles)],
      ['Last inference', ago(health.inference_last_run_at)],
      ['Learning', learning.active ? badge('Active', 'ok', { dot: true }) : badge('Idle', 'mute', { dot: true })],
      ['Uptime', duration(health.uptime_seconds)],
      ['Contract', health.contract_version || '—'],
    ];
    return card('Engine', [kv(rows)], {
      foot: 'The engine reads Home Assistant; it never switches a device.',
    });
  }

  function overviewDevices(devices, snapshot) {
    const list = Array.isArray(devices) ? devices : [];
    const withPower = list
      .map((d) => ({ d, result: d.latest_result || null }))
      .filter(({ result }) => result && Number(result.power_w) > 0)
      .sort((a, b) => Number(b.result.power_w) - Number(a.result.power_w))
      .slice(0, 8);

    const body = withPower.length
      ? withPower.map(({ d, result }) => deviceMiniRow(d, result))
      : emptyState(
          'No device values yet',
          list.length
            ? 'Devices exist but none has a power value yet. Direct CTs appear first; inferred and learned values follow as the engine observes the home.'
            : 'No topology devices. Configure the WattMosaic integration to map your CTs and devices.'
        );

    return el('section', { class: 'card', style: 'margin-top:16px' }, [
      el('div', { class: 'card-head' }, [
        el('h2', {}, 'Top consumers'),
        el('span', { class: 'spacer' }),
        el('a', { class: 'btn btn-sm btn-ghost', href: '#/devices' }, `All devices (${list.length})`),
      ]),
      el('div', {}, body),
    ]);
  }

  function deviceMiniRow(device, result) {
    const p = Number(result.power_w) || 0;
    const max = 5000;
    const width = Math.min(100, (p / max) * 100);
    return el('a', { class: 'device-mini', href: `#/device/${encodeURIComponent(device.device_id)}` }, [
      el('div', { class: 'who' }, [
        el('div', { class: 'nm' }, device.name || device.device_id),
        el('div', { class: 'sub' }, [
          badge(label(result.source), sourceTone(result.source)),
          tierBadge(result.confidence_tier || device.confidence_tier),
        ]),
      ]),
      el('div', { class: 'bar' }, [
        el('div', { class: 'meter', role: 'img', 'aria-label': `${label(result.source)} ${watts(p)}` }, [
          el('span', {
            style: `width:${width}%;background:${SOURCE_SWATCH[result.source] || 'var(--mute)'}`,
          }),
        ]),
      ]),
      el('div', { class: 'watts' }, [watts(p), el('span', { class: 'unit' }, '')]),
    ]);
  }

  /* --- Devices ---------------------------------------------------------- */

  async function renderDevices(root, signal) {
    const devices = await get('/devices', signal);
    if (signal.aborted) return;
    state.lastDevices = devices;

    root.append(
      el('div', { class: 'page-head' }, [
        el('div', {}, [
          el('h1', {}, 'Devices'),
          el('p', { class: 'lede' },
            'Every load in the active topology with its latest estimate, evidence grade and measurement source.'),
        ]),
      ])
    );

    if (!Array.isArray(devices) || devices.length === 0) {
      root.append(
        emptyState(
          'No devices in the topology',
          'The engine has no devices to disaggregate. Map CTs and devices in the WattMosaic integration, then confirm the topology.',
          { action: el('a', { class: 'btn btn-primary', href: '#/topology' }, 'View topology') }
        )
      );
      return;
    }

    const summary = deviceSummary(devices);
    root.append(summary);
    root.append(deviceToolbar(devices));

    const container = el('div', { class: 'device-grid', id: 'device-grid' });
    root.append(container);
    paintDeviceGrid(container, devices);
  }

  function deviceSummary(devices) {
    const totals = { measured: 0, inferred: 0, modelled: 0, prior: 0, unknown: 0 };
    for (const d of devices) {
      const r = d.latest_result;
      if (!r) { totals.unknown += 1; continue; }
      switch (r.source) {
        case 'direct_measured': totals.measured += 1; break;
        case 'inferred': totals.inferred += 1; break;
        case 'model_estimated': totals.modelled += 1; break;
        case 'configured_prior': totals.prior += 1; break;
        default: totals.unknown += 1;
      }
    }
    return el('div', { class: 'grid cols-5', style: 'margin-bottom:16px' }, [
      statCard('Devices', String(devices.length), { hint: 'In the active topology' }),
      statCard('Direct measured', String(totals.measured), {
        hint: 'Own CT',
        tone: totals.measured ? 'ok' : null,
      }),
      statCard('Inferred', String(totals.inferred), {
        hint: 'Solved from shared CTs',
        tone: totals.inferred ? 'accent' : null,
      }),
      statCard('Model / prior', String(totals.modelled + totals.prior), {
        hint: 'No direct evidence',
        tone: totals.modelled + totals.prior ? 'warn' : null,
      }),
      statCard('Unknown', String(totals.unknown), {
        hint: 'No honest value yet',
        tone: totals.unknown ? 'bad' : null,
      }),
    ]);
  }

  function deviceToolbar(devices) {
    const search = el('input', {
      type: 'search',
      placeholder: 'Filter by name or id…',
      value: state.devicesFilter,
      'aria-label': 'Filter devices',
      oninput: (event) => {
        state.devicesFilter = event.target.value;
        paintDeviceGrid(document.getElementById('device-grid'), devices);
      },
    });

    const sort = el(
      'div',
      { class: 'segmented', role: 'group', 'aria-label': 'Sort devices' },
      [
        ['power', 'Power'],
        ['name', 'Name'],
        ['tier', 'Evidence'],
      ].map(([key, text]) =>
        el('button', {
          type: 'button',
          'aria-pressed': String(state.devicesSort === key),
          onclick: (event) => {
            state.devicesSort = key;
            for (const btn of event.currentTarget.parentNode.children) {
              btn.setAttribute('aria-pressed', String(btn.textContent === text));
            }
            paintDeviceGrid(document.getElementById('device-grid'), devices);
          },
        }, text)
      )
    );

    return el('div', { class: 'toolbar' }, [
      el('label', { class: 'field' }, ['Search', search]),
      el('span', { class: 'spacer' }),
      el('span', { class: 'field', style: 'flex-direction:row;align-items:center;gap:10px' }, [
        el('span', {}, 'Sort'),
        sort,
      ]),
    ]);
  }

  function paintDeviceGrid(container, devices) {
    if (!container) return;
    clear(container);

    let list = [...devices];
    const query = state.devicesFilter.trim().toLowerCase();
    if (query) {
      list = list.filter((d) =>
        `${d.name || ''} ${d.device_id || ''}`.toLowerCase().includes(query)
      );
    }
    const tierRank = (d) => tierInfo(d.latest_result && d.latest_result.confidence_tier).index;
    const powerOf = (d) => Number((d.latest_result && d.latest_result.power_w) || 0);
    if (state.devicesSort === 'name') {
      list.sort((a, b) => String(a.name || a.device_id).localeCompare(String(b.name || b.device_id)));
    } else if (state.devicesSort === 'tier') {
      list.sort((a, b) => tierRank(b) - tierRank(a) || powerOf(b) - powerOf(a));
    } else {
      list.sort((a, b) => powerOf(b) - powerOf(a));
    }

    if (list.length === 0) {
      container.append(
        emptyState('No matching devices', 'Adjust the filter to see devices again.')
      );
      return;
    }
    for (const device of list) container.append(deviceCard(device));
  }

  function deviceCard(device) {
    const result = device.latest_result || null;
    // ``power_w`` is a required float in the contract, but a stored row from an
    // older release can carry null.  ``Number(null)`` is 0, which would claim a
    // measured 0 W; only a real finite number is a value.
    const hasPower = result !== null && result.power_w !== null && result.power_w !== undefined
      && Number.isFinite(Number(result.power_w));
    const source = result ? result.source : null;
    const tier = (result && result.confidence_tier) || device.confidence_tier;
    const model = device.model;

    const powerNode = hasPower
      ? el('div', { class: 'dc-power' }, [
          el('span', { class: 'n' }, num(result.power_w, Math.abs(Number(result.power_w)) >= 100 ? 0 : 1)),
          el('span', { class: 'u' }, 'W'),
        ])
      : el('div', { class: 'dc-power unknown' }, [
          el('span', { class: 'n' }, 'Unknown'),
        ]);

    const tags = el('div', { class: 'dc-tags' }, [
      result ? badge(label(source), sourceTone(source)) : badge('No value', 'mute'),
      result ? tierBadge(tier) : null,
      result && result.quality_status && result.quality_status !== 'good'
        ? statusBadge(result.quality_status)
        : null,
    ]);

    return el(
      'a',
      {
        class: 'device-card',
        href: `#/device/${encodeURIComponent(device.device_id)}`,
        'aria-label': `${device.name || device.device_id}: ${hasPower ? watts(result.power_w) : 'unknown'}`,
      },
      [
        el('div', { class: 'dc-head' }, [
          el('div', { style: 'min-width:0;flex:1 1 auto' }, [
            el('div', { class: 'dc-name' }, device.name || device.device_id),
            el('div', { class: 'dc-id' }, device.device_id),
          ]),
          device.double_pole ? badge('240 V', 'info') : null,
        ]),
        el('div', { class: 'dc-body' }, [
          powerNode,
          tags,
          result && result.operating_stage
            ? el('div', { class: 'stat-hint' }, `Stage: ${titleCase(result.operating_stage)}`)
            : null,
        ]),
        el('div', { class: 'dc-foot' }, [
          el('span', {}, model ? `Model v${model.version} · ${label(model.status)}` : 'No learned model'),
          el('span', {}, result ? ago(result.timestamp) : '—'),
        ]),
      ]
    );
  }

  /* --- Device detail ---------------------------------------------------- */

  async function renderDevice(root, signal) {
    const deviceId = state.deviceId;
    const { device, models } = await fetchDevice(deviceId, signal);
    if (signal.aborted) return;

    const result = device.latest_result || null;
    const topology = device.topology || null;

    root.append(
      el('div', { class: 'page-head' }, [
        el('div', {}, [
          el('div', { style: 'display:flex;gap:10px;align-items:center;margin-bottom:4px' }, [
            el('a', { class: 'btn btn-sm btn-ghost', href: '#/devices' }, '← Devices'),
          ]),
          el('h1', {}, device.name || device.device_id || 'Device'),
          el('p', { class: 'lede' }, device.device_id || ''),
        ]),
        el('div', { class: 'head-actions' }, [
          el('button', {
            class: 'btn btn-sm',
            type: 'button',
            onclick: () => copyExport(device.device_id),
          }, 'Copy export'),
        ]),
      ])
    );

    if (!result && !topology) {
      root.append(
        emptyState('Device not found', 'The engine does not know this device. It may have been removed from the topology.')
      );
      return;
    }

    const mainCol = el('div', { style: 'display:flex;flex-direction:column;gap:16px' });
    const sideCol = el('div', { class: 'detail-side' });

    mainCol.append(deviceHero(device, result));
    mainCol.append(deviceHistoryCard(device));
    mainCol.append(deviceEvidenceCard(result));
    if (device.configuration) mainCol.append(deviceConfigurationCard(device.configuration));

    sideCol.append(deviceSourceCard(result));
    sideCol.append(deviceTierCard(result));
    sideCol.append(deviceModelCard(device, models));
    if (topology) sideCol.append(deviceTopologyCard(topology));

    root.append(el('div', { class: 'detail-layout' }, [mainCol, sideCol]));
  }

  function deviceHero(device, result) {
    const hasPower = result !== null && result.power_w !== null && result.power_w !== undefined
      && Number.isFinite(Number(result.power_w));
    return el('section', { class: 'card' }, [
      el('div', { class: 'card-body' }, [
        el('div', { class: 'detail-hero' }, [
          el('div', {}, [
            el('div', { class: 'stat-label' }, 'Estimated power'),
            el('div', { class: 'big' }, [
              hasPower ? num(result.power_w, Math.abs(Number(result.power_w)) >= 100 ? 0 : 1) : '—',
              el('span', { class: 'unit' }, hasPower ? 'W' : ''),
            ]),
            !hasPower
              ? el('p', { class: 'stat-hint', style: 'margin-top:6px;max-width:40ch' },
                  'No honest device-level value. The load stays in the unknown residual.')
              : null,
          ]),
          el('div', { class: 'meta' }, [
            result ? badge(label(result.source), sourceTone(result.source), { dot: true }) : null,
            result ? tierBadge(result.confidence_tier) : null,
            result && result.energy_kwh !== null && result.energy_kwh !== undefined
              ? el('span', { class: 'chip' }, kWh(result.energy_kwh))
              : null,
            result && result.operating_stage
              ? el('span', { class: 'chip' }, `Stage: ${titleCase(result.operating_stage)}`)
              : null,
          ]),
        ]),
      ]),
      el('div', { class: 'card-foot' }, [
        el('span', {}, `Last update ${ago(result && result.timestamp)}`),
        result && result.measurement_age_seconds !== undefined
          ? el('span', {}, ` · Measurement age ${duration(result.measurement_age_seconds)}`)
          : null,
      ]),
    ]);
  }

  function deviceHistoryCard(device) {
    const history = (device.history || []).slice().reverse();
    if (!history.length) {
      return card('History', [
        emptyState('No stored history', 'Inference results appear here once the engine has run a few cycles for this device.'),
      ]);
    }
    return card(
      'Recent history',
      [
        sparkline(history),
        el('div', { class: 'table-wrap', style: 'margin-top:14px' }, [
          el('table', { class: 'data' }, [
            el('thead', {}, el('tr', {}, [
              el('th', {}, 'Time'),
              el('th', { class: 'num' }, 'Power'),
              el('th', {}, 'Source'),
              el('th', { class: 'num' }, 'Confidence'),
              el('th', {}, 'Quality'),
              el('th', {}, 'Stage'),
            ])),
            el('tbody', {},
              history.slice(0, 12).map((row) =>
                el('tr', {}, [
                  el('td', {}, ago(row.timestamp)),
                  el('td', { class: 'num' }, watts(row.power_w)),
                  el('td', {}, badge(label(row.source), sourceTone(row.source))),
                  el('td', { class: 'num' }, row.confidence === null || row.confidence === undefined ? '—' : percent(row.confidence, 0)),
                  el('td', {}, statusBadge(row.quality_status)),
                  el('td', {}, row.operating_stage ? titleCase(row.operating_stage) : '—'),
                ])
              )),
          ]),
        ]),
      ],
      { foot: 'Newest first. Historical rows are stored inference results, not re-simulated values.' }
    );
  }

  function sparkline(history) {
    // ``history`` arrives newest-first from the API, so sort by time before
    // mapping to x: computing the domain from the reversed array produced
    // enormous negative coordinates and painted the polygon across the page.
    const points = history
      .map((row) => ({ t: new Date(row.timestamp).getTime(), v: Number(row.power_w) }))
      .filter((p) => Number.isFinite(p.t) && Number.isFinite(p.v))
      .sort((a, b) => a.t - b.t);
    if (points.length < 2) return el('div', { class: 'stat-hint' }, 'Not enough points for a chart.');

    const w = 600;
    const h = 56;
    const pad = 4;
    const minT = points[0].t;
    const maxT = points[points.length - 1].t;
    const spanT = maxT - minT || 1;
    const maxV = Math.max(1, ...points.map((p) => p.v));
    const minV = Math.min(0, ...points.map((p) => p.v));
    const spanV = maxV - minV || 1;
    const x = (t) => pad + ((t - minT) / spanT) * (w - pad * 2);
    const y = (v) => h - pad - ((v - minV) / spanV) * (h - pad * 2);
    const line = points.map((p) => `${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`).join(' ');
    const area = `${pad},${h - pad} ${line} ${w - pad},${h - pad}`;

    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('class', 'spark');
    svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
    svg.setAttribute('preserveAspectRatio', 'none');
    svg.setAttribute('role', 'img');
    svg.setAttribute('aria-label', `Power over the last ${points.length} results, peak ${watts(maxV)}`);
    const areaNode = document.createElementNS('http://www.w3.org/2000/svg', 'polygon');
    areaNode.setAttribute('class', 'area');
    areaNode.setAttribute('points', area);
    const lineNode = document.createElementNS('http://www.w3.org/2000/svg', 'polyline');
    lineNode.setAttribute('points', line);
    svg.append(areaNode, lineNode);
    return svg;
  }

  function deviceEvidenceCard(result) {
    const explanation = (result && result.explanation) || [];
    const tierReasons = (result && result.tier_reasons) || [];
    const supporting = (result && result.supporting_ct_channels) || [];
    const items = [
      ...explanation.map((text) => ({ kind: 'explanation', text })),
      ...tierReasons.map((text) => ({ kind: 'tier', text })),
    ];

    return card(
      'Why this number',
      [
        items.length
          ? el(
              'ul',
              { class: 'explain' },
              items.map((item) =>
                el('li', {}, [
                  el('span', { class: `marker${item.kind === 'tier' ? ' marker-tier' : ''}`, 'aria-hidden': 'true' },
                    item.kind === 'tier' ? 'T' : '·'),
                  el('span', {}, titleCase(item.text)),
                ])
              )
            )
          : el('p', { class: 'stat-hint' }, 'The engine recorded no explanation for this result.'),
        supporting.length
          ? el('div', { style: 'margin-top:14px' }, [
              el('div', { class: 'stat-label', style: 'margin-bottom:8px' }, 'Supporting CT channels'),
              el('div', { style: 'display:flex;gap:6px;flex-wrap:wrap' },
                supporting.map((ct) => el('span', { class: 'chip chip-ct' }, ct))),
            ])
          : null,
      ],
      { foot: 'Explanations are generated by the engine, not by this panel.' }
    );
  }

  function deviceSourceCard(result) {
    const rows = [
      ['Source', result ? badge(label(result.source), sourceTone(result.source)) : '—'],
      ['Confidence', result ? percent(result.confidence, 0) : '—'],
      ['Calibrated', result ? (result.confidence_is_calibrated ? 'Yes' : 'No — engineering score') : '—'],
      ['Quality', result ? statusBadge(result.quality_status) : '—'],
      ['Measurement age', result ? duration(result.measurement_age_seconds) : '—'],
      ['Sample count', result ? String(result.sample_count ?? '—') : '—'],
      ['Model', result && result.model_id ? `${result.model_id} v${result.model_version ?? '—'}` : '—'],
    ];
    return card('Estimate', [kv(rows)], {
      foot: 'Confidence is an engineering score, not a probability.',
    });
  }

  function deviceTierCard(result) {
    const current = tierInfo(result && result.confidence_tier);
    const list = (state.tiers && state.tiers.tiers) || [];
    const rows = (list.length ? list : TIER_ORDER.map((tier) => ({ tier, label: tierInfo(tier).label }))).map(
      (entry) => {
        const info = tierInfo(entry.tier);
        return el('div', { class: `ts-row${entry.tier === current.key ? ' active' : ''}` }, [
          el('span', { class: 'nm' }, entry.label || info.label),
          el('span', { class: 'ds' }, entry.description ? shorten(entry.description, 96) : ''),
        ]);
      }
    );
    return card(
      'Evidence grade',
      [
        el('div', { style: 'margin-bottom:12px;display:flex;align-items:center;gap:8px' }, [
          tierBadge(current.key),
          el('span', { class: 'stat-hint' }, 'An evidence grade, not a probability.'),
        ]),
        el('div', { class: 'tier-scale' }, rows),
      ],
      { foot: 'Tiers are ordered from "nothing supports it" to "measured and corroborated".' }
    );
  }

  function deviceModelCard(device, models) {
    const active = (models && models.active) || device.model;
    if (!active) {
      return card('Model', [
        emptyState(
          'No learned model',
          'The engine learns a model only after repeated, validated cycles for this device.',
          { tone: 'warn' }
        ),
      ]);
    }
    return card('Model', [
      kv([
        ['Type', titleCase(active.model_type || '—')],
        ['Version', String(active.version ?? '—')],
        ['Status', statusBadge(active.status)],
        ['Samples', String(active.sample_count ?? '—')],
        ['Confidence', active.confidence === null || active.confidence === undefined ? '—' : percent(active.confidence, 0)],
        ['Trained to', active.trained_to ? ago(active.trained_to) : '—'],
      ]),
    ]);
  }

  function deviceTopologyCard(topology) {
    const rows = [
      ['Phase', label(topology.phase)],
      ['Double pole', topology.double_pole ? 'Yes' : 'No'],
      ['Status', statusBadge(topology.topology_status)],
      ['Dedicated CTs', (topology.dedicated_ct_ids || []).join(', ') || '—'],
      ['Branch CTs', (topology.branch_ct_ids || []).join(', ') || '—'],
      ['Parent device', topology.parent_device_id || '—'],
    ];
    return card('Placement', [kv(rows)]);
  }

  function deviceConfigurationCard(configuration) {
    const rows = [
      ['Device type', titleCase(configuration.device_type || '—')],
      ['Connectivity', titleCase(configuration.connectivity || '—')],
      ['Monitoring', label(configuration.monitoring)],
      ['Behavior', titleCase(configuration.behavior || '—')],
      ['Expected on', configuration.expected_on_w === null || configuration.expected_on_w === undefined ? '—' : watts(configuration.expected_on_w)],
      ['Expected standby', configuration.expected_standby_w === null || configuration.expected_standby_w === undefined ? '—' : watts(configuration.expected_standby_w)],
      ['Load entities', (configuration.load_entity_ids || []).join(', ') || '—'],
      ['Mode entities', (configuration.mode_entity_ids || []).join(', ') || '—'],
      ['Monitored by', (configuration.monitored_by_ct_ids || []).join(', ') || '—'],
    ];
    return card('Declared configuration', [kv(rows)], {
      foot: 'Declarations are user knowledge, never inferred from a name or an area.',
    });
  }

  const shorten = (text, max) => {
    const value = String(text || '');
    return value.length > max ? `${value.slice(0, max - 1)}…` : value;
  };

  async function copyExport(deviceId) {
    try {
      const payload = await get(`/export/${encodeURIComponent(deviceId)}`);
      await navigator.clipboard.writeText(JSON.stringify(payload, null, 2));
      notify('Export copied', `${deviceId} JSON is on the clipboard.`);
    } catch (err) {
      notify('Export failed', describeError(err), 'bad');
    }
  }

  /* --- Topology --------------------------------------------------------- */

  // The hierarchy is painted from GET /api/v1/topology/tree.  This file never
  // derives the tree again — not from names, not from ct_id prefixes, not from
  // entity ids.  A second derivation in the browser is exactly what used to
  // produce a silently wrong tree, so the engine owns the hierarchy and the
  // panel renders it verbatim.

  const TREE_PATH = '/topology/tree';
  const CT_TONES = { main: 'accent', branch: 'info', dedicated_device: 'ok' };
  const ctTone = (type) => CT_TONES[String(type || '')] || 'mute';

  const topologyHead = (topology) =>
    el('div', { class: 'page-head' }, [
      el('div', {}, [
        el('h1', {}, 'Topology'),
        el('p', { class: 'lede' },
          'The electrical model the engine solves against: panels, the circuits they feed, and the devices attributed to each circuit.'),
      ]),
      el('div', { class: 'head-actions' }, [
        // Discovery is an explicit request: it never runs on paint and never
        // writes anything on the first call.
        el('button', {
          class: 'btn',
          id: 'topology-discover-run',
          type: 'button',
          onclick: () => runDiscovery(),
          disabled: state.discover.running ? true : null,
        }, state.discover.running ? 'Discovering…' : 'Discover devices'),
        topology && topology.status !== 'confirmed'
          ? el('button', { class: 'btn btn-primary', type: 'button', onclick: confirmTopology }, 'Confirm topology')
          : null,
      ]),
    ]);

  // Painted synchronously, before the first await.  Without it a cold route
  // showed a blank, zero-height viewport for the whole request.
  const topologySkeleton = () =>
    el('div', { id: 'topology-loading', class: 'topo-loading' }, [
      el('p', { class: 'stat-hint', role: 'status' }, 'Loading the electrical model…'),
      el('div', { class: 'grid cols-4', style: 'margin-bottom:16px' },
        Array.from({ length: 4 }, () => el('div', { class: 'skeleton h-stat' }))),
      el('div', { class: 'topo-grid' }, [
        el('div', { class: 'card' }, [el('div', { class: 'skeleton h-card' })]),
        el('div', { class: 'card' }, [el('div', { class: 'skeleton h-card' })]),
      ]),
    ]);

  async function renderTopology(root, signal) {
    // 1. First paint, synchronously.  The page head and skeleton go up before
    //    any request so the user sees a loading state immediately.
    clear(root);
    root.append(topologyHead(null));
    root.append(topologySkeleton());

    // 2. Only the fast endpoints are awaited here.  /topology/nesting compares
    //    a 12-hour window and can take tens of seconds; putting it in this
    //    Promise.all is what made the whole tab wait for it.  /status is cheap
    //    and carries the learning state the banner needs, so it joins here.
    const [topology, treeResult, status] = await Promise.all([
      get('/topology', signal).catch((err) => {
        if (err && err.status === 404) return null;
        throw err;
      }),
      get(TREE_PATH, signal).then(
        (data) => ({ ok: true, data, err: null }),
        (err) => ({ ok: false, data: null, err })
      ),
      get('/status', signal).catch(() => null),
    ]);
    if (signal.aborted) return;

    clear(root);
    root.append(topologyHead(topology));

    if (!topology) {
      root.append(
        emptyState(
          'No topology configured',
          'The engine cannot disaggregate without a topology. Configure it in the WattMosaic integration; results stay unavailable until then.',
          { tone: 'warn' }
        )
      );
      return;
    }

    const tree = treeResult.ok && treeResult.data && Array.isArray(treeResult.data.roots)
      ? treeResult.data
      : null;

    const learningBanner = topologyLearningBanner(status, topology);
    if (learningBanner) root.append(learningBanner);
    root.append(topologySummary(topology, tree));
    if (!tree) root.append(topologyFallbackNotice(treeResult.err));

    // The hierarchy is the subject of this tab, so it gets the full width: a
    // tree squeezed into half a page forces every ct_id to wrap.
    root.append(topologyHierarchyCard(topology, tree));

    // Placement discovery renders into its own slot; it is only populated when
    // the user asks for it (or when a previous answer is cached in state).
    root.append(topologyDiscoverSlot());

    // The candidates card is a real card from the start: either the cached
    // comparison or an explicit loading state.  It is replaced in place.
    root.append(
      el('div', { class: 'topo-grid' }, [
        topologyChannelsCard(topology),
        topologyCandidatesSlot(),
      ])
    );

    const notes = [...(topology.notes || []), ...((tree && tree.notes) || [])];
    if (notes.length) {
      root.append(
        card(
          'Engine notes',
          el('ul', { class: 'explain' },
            notes.map((note) =>
              el('li', {}, [el('span', { class: 'marker' }, '·'), el('span', {}, note)])))
        )
      );
    }

    // 3. Everything above is on screen.  Now, and only now, ask for the slow
    //    comparison; it fills one card when it lands.
    fillNestingCandidates(signal);
  }

  function topologyFallbackNotice(err) {
    const missing = Boolean(err && err.status === 404);
    const text = missing
      ? 'This engine does not serve /api/v1/topology/tree (HTTP 404), so the engine-owned hierarchy cannot be drawn. The hierarchy card falls back to CT containment from parent_ct_id only, and devices are not placed.'
      : `The hierarchy could not be loaded (${describeError(err)}). The hierarchy card falls back to CT containment from parent_ct_id only, and devices are not placed.`;
    return notice('Hierarchy unavailable', text, 'warn');
  }

  function topologySummary(topology, tree) {
    const channels = topology.channels || [];
    const counts = { main: 0, branch: 0, dedicated_device: 0, unknown: 0 };
    for (const ch of channels) {
      counts[ch.ct_type] = (counts[ch.ct_type] || 0) + 1;
    }
    const stats = (tree && tree.stats) || null;
    const panels = stats && stats.panels !== undefined ? String(stats.panels) : '—';
    const circuits = stats && stats.circuits !== undefined
      ? String(stats.circuits)
      : String(counts.branch + counts.dedicated_device);
    const devices = stats && stats.devices !== undefined
      ? String(stats.devices)
      : String((topology.devices || []).length);
    const unplaced = stats
      ? (Number(stats.unplaced_devices) || 0) + (Number(stats.unplaced_circuits) || 0)
      : 0;

    const configured =
      topology.status === 'confirmed'
        ? null
        : notice(
            'This topology is a candidate',
            'Results carry a topology-uncertainty penalty until the topology is confirmed. Confirm it only when the CT roles match your wiring.',
            'warn'
          );

    return frag([
      configured,
      el('div', { class: 'grid cols-5', style: 'margin-bottom:16px' }, [
        statCard('Panels', panels, { hint: panels === '1' ? 'distribution board' : 'distribution boards' }),
        statCard('Circuits', circuits, {
          hint: `${counts.main} feed · ${counts.branch} branch · ${counts.dedicated_device} dedicated`,
        }),
        statCard('Devices', devices, {
          hint: unplaced ? `${unplaced} unplaced — shown, never dropped` : 'each placed once',
          tone: unplaced ? 'warn' : null,
        }),
        statCard('Status', label(topology.status), { tone: toneFor(topology.status) }),
        statCard('Version', String(topology.version ?? '—'), { hint: ago(topology.created_at) }),
      ]),
    ]);
  }

  /* --- The hierarchy ---------------------------------------------------- *
   * Real nested markup: ul.hier-list > li.hier-node > .hier-row (+ a nested
   * ul for children).  Connector lines are CSS borders on the li, so the tree
   * reads as a tree without SVG and without any inline style.
   * ---------------------------------------------------------------------- */

  const nodeKey = (node) =>
    String((node && (node.id || node.ct_id || node.device_id)) || (node && node.label) || 'node');

  const BADGE_TONES = new Set(['ok', 'warn', 'bad', 'info', 'mute', 'accent']);
  // The engine emits ``muted``; the whitelist historically said ``mute``.  The
  // two are the same tone, so accept both instead of silently demoting every
  // ``muted`` pill to an unstyled fallback (contract §5.3).
  const badgeTone = (raw) => {
    const tone = String(raw || 'mute');
    const normalised = tone === 'muted' ? 'mute' : tone;
    // An unknown tone would render an unstyled badge: fall back to the neutral
    // one rather than shipping a pill with no border or colour.
    return BADGE_TONES.has(normalised) ? normalised : 'mute';
  };

  const badgeItems = (node) =>
    (Array.isArray(node.badges) ? node.badges : [])
      .filter((item) => item && item.text !== undefined && item.text !== null);

  const badgeNode = (item) =>
    badge(String(item.text), badgeTone(item.tone), {
      title: item.title === undefined || item.title === null ? null : String(item.title),
    });

  const nodeBadges = (node) => badgeItems(node).map(badgeNode);

  /* --- Measurement honesty (contract §5.2/§5.3) ------------------------- */

  const MEASUREMENT_STATES = new Set(['measured', 'inferred', 'prior', 'none']);

  //: The reason the engine gives for *not* measuring a fixture, in plain words.
  const NOT_MEASURED_REASONS = {
    below_ct_resolution: 'The step is below what this CT can resolve',
    scene_group: 'It toggles with a scene group, so the step cannot be attributed',
    no_transition: 'It never transitioned in the measurement window',
  };

  //: When the engine names a measurement state but no reason, the state itself
  //: is still an honest explanation — better than a generic shrug.
  const MEASUREMENT_TITLES = {
    inferred: 'Inferred from co-movement; not measured directly on a CT',
    prior: 'Uses a configured prior, not a measurement',
    none: 'The engine has no measurement for this fixture',
  };

  const notMeasuredTitle = (reason, measurement, hasValue = true) => {
    if (!hasValue && String(measurement || '') === 'measured') {
      return 'Declared as measured, but the engine has not produced a value yet';
    }
    return NOT_MEASURED_REASONS[String(reason || '')]
      || MEASUREMENT_TITLES[String(measurement || '')]
      || 'The engine has no measurement for this fixture';
  };

  const hasPowerValue = (node) =>
    Boolean(node) && node.latest_power_w !== null && node.latest_power_w !== undefined
      && Number.isFinite(Number(node.latest_power_w));

  /**
   * The measurement vocabulary for a device node.
   *
   * An old engine emits no ``measurement`` key at all.  The panel must not
   * invent a measurement claim it never made, so the fallback is the contract's
   * own rule (§5.4 item 4): a node with a ``source`` counts as measured.  The
   * second half — *and it has a value to print* — is what fixes defect 4: a node
   * with no wattage is "not measured" rather than a bare ``—``.
   */
  const measurementOf = (node) => {
    if (!node) return 'none';
    if (node.measurement !== undefined && node.measurement !== null) {
      const value = String(node.measurement);
      return MEASUREMENT_STATES.has(value) ? value : 'none';
    }
    const sourced = node.source !== null && node.source !== undefined;
    return sourced && hasPowerValue(node) ? 'measured' : 'none';
  };

  //: One rule for the row and the circuit summary, so the same fixture is never
  //: "measured" in one place and "unmeasured" in the other.
  const isMeasuredNode = (node) => measurementOf(node) === 'measured';

  // The engine's device ``subtitle`` used to be dropped outright.  It is now the
  // evidence line.  Suppress only what carries no information: a repeat of the
  // row's own name, the phase already shown as a badge, or a structural
  // placeholder ("unknown", "unknown phase") — never real evidence text.
  const STRUCTURAL_SUBTITLES = new Set(['unknown', 'unknown phase', 'n/a']);
  const deviceSubtitle = (node) => {
    if (node.subtitle === null || node.subtitle === undefined) return '';
    const text = String(node.subtitle).trim();
    if (!text) return '';
    const folded = text.toLowerCase();
    if (STRUCTURAL_SUBTITLES.has(folded)) return '';
    if (text === String(node.label || '') || text === String(node.device_id || '')) return '';
    const phase = String(node.phase || '');
    if (phase && (folded === phase.toLowerCase() || folded === label(phase).toLowerCase())) return '';
    return text;
  };

  // The first evidence string is a better subtitle than an empty row: it says
  // *why* the engine believes what it shows.
  const firstEvidence = (node) =>
    (Array.isArray(node.evidence) ? node.evidence : [])
      .map((item) => (item === null || item === undefined ? '' : String(item).trim()))
      .filter(Boolean)[0] || '';

  const deviceEvidenceLine = (node) => deviceSubtitle(node) || firstEvidence(node);

  //: The engine emits ``topology_status`` as a badge whose text equals the
  //: field.  Matching by identity (not by tone or a keyword) means an unrelated
  //: "ok"-toned badge is never swallowed along with it.
  const isWiringStatusBadge = (item, node) => {
    const status = node && node.topology_status;
    if (!status) return false;
    return String(item.text).toLowerCase() === String(status).toLowerCase();
  };

  /**
   * One compact chip for a circuit's direct children (contract §5.4.4).
   *
   * ``measurement`` is the engine's vocabulary; the count never guesses beyond
   * it.  An old engine has no key, so a child with a source counts as measured —
   * that is the same rule the device row applies.
   */
  function circuitChildSummary(node) {
    const children = Array.isArray(node.children) ? node.children : [];
    const devices = children.filter((child) => child && child.kind === 'device');
    if (!devices.length) return null;
    const measured = devices.filter(isMeasuredNode).length;
    const unmeasured = devices.length - measured;
    const noun = devices.length === 1 ? 'device' : 'devices';

    // The chip uses the engine's own word when every unmeasured child really is
    // an inference — the common case.  A `prior`, `none`, or a payload with no
    // ``measurement`` key at all is not an inference, so the chip falls back to
    // the neutral "unmeasured" rather than relabelling it.
    const kinds = devices
      .filter((child) => !isMeasuredNode(child))
      .map((child) => (child.measurement !== undefined && child.measurement !== null
        ? String(child.measurement)
        : 'unmeasured'));
    const bucket = kinds.every((kind) => kind === 'inferred') ? 'inferred' : 'unmeasured';
    const text = unmeasured
      ? `${devices.length} ${noun} · ${measured} measured · ${unmeasured} ${bucket}`
      : `${devices.length} ${noun}`;

    const detail = unmeasured
      ? `${measured} of ${devices.length} fixtures on this circuit have a measured wattage; ${unmeasured} are ${bucket}.`
      : `All ${devices.length} fixtures on this circuit have a measured wattage.`;
    // The exact vocabulary the engine emitted (inferred / prior / none) belongs
    // in the tooltip, not in a chip that must stay one short phrase.
    const breakdown = unmeasured
      ? ` (${Object.entries(kinds.reduce((acc, kind) => {
          acc[kind] = (acc[kind] || 0) + 1;
          return acc;
        }, {})).map(([kind, count]) => `${count} ${kind}`).join(', ')})`
      : '';
    return el('span', { class: 'hier-count', title: detail + breakdown }, text);
  }

  // Contract §1.2 R5: a group with no children is omitted entirely.  The
  // engine already does this, but a group is pure structure, so hiding an
  // empty one costs nothing and keeps a stale payload from drawing a bare
  // "DEVICES 0" heading.
  const prunedNodes = (nodes) =>
    nodes
      .filter((node) => !(node.kind === 'group' && !(node.children || []).length))
      .map((node) => (Array.isArray(node.children) && node.children.length
        ? { ...node, children: prunedNodes(node.children) }
        : node));

  const hierarchyNodes = (topology, tree) =>
    tree && Array.isArray(tree.roots) ? prunedNodes(tree.roots) : legacyRoots(topology);

  // Fallback for an engine without /topology/tree: the old CT containment view,
  // expressed in the same node shape so one renderer serves both.  This is the
  // only place that looks at parent_ct_id, and it is explicitly labelled.
  function legacyRoots(topology) {
    const channels = [...(topology.channels || [])];
    const byId = new Map(channels.map((ch) => [ch.ct_id, ch]));
    const children = new Map();
    const roots = [];
    for (const ch of channels) {
      const parent = ch.parent_ct_id;
      if (parent && parent !== ch.ct_id && byId.has(parent)) {
        if (!children.has(parent)) children.set(parent, []);
        children.get(parent).push(ch);
      } else {
        roots.push(ch);
      }
    }

    const seen = new Set();
    const build = (ch) => {
      seen.add(ch.ct_id);
      const kids = (children.get(ch.ct_id) || [])
        .sort((a, b) => String(a.ct_id).localeCompare(String(b.ct_id)))
        .map((kid) => (seen.has(kid.ct_id) ? null : build(kid)))
        .filter(Boolean);
      return {
        id: `legacy:circuit:${ch.ct_id}`,
        kind: 'circuit',
        label: ch.name && ch.name !== ch.phase ? ch.name : ch.ct_id,
        subtitle: ch.entity_id || ch.ct_id,
        badges: [],
        ct_id: ch.ct_id,
        ct_type: ch.ct_type,
        phase: ch.phase,
        enabled: ch.enabled,
        alias_of_ct_id: ch.alias_of_ct_id,
        polarity: ch.polarity,
        topology_status: ch.topology_status,
        latest_power_w: null,
        children: kids,
      };
    };

    return roots
      .sort((a, b) => String(a.ct_id).localeCompare(String(b.ct_id)))
      .map((ch) => (seen.has(ch.ct_id) ? null : build(ch)))
      .filter(Boolean);
  }

  // Default: the first panel is open to depth 2 — the panel, its group nodes
  // and the circuits inside them are visible, while every deeper branch starts
  // collapsed.  Other panels start collapsed.
  function defaultOpenSet(nodes) {
    const ids = new Set();
    const first = nodes[0];
    if (!first) return ids;
    const walk = (node, depth) => {
      if (depth >= 2) return;
      if ((node.children || []).length) ids.add(nodeKey(node));
      for (const child of node.children || []) walk(child, depth + 1);
    };
    walk(first, 0);
    return ids;
  }

  const isExpanded = (key, defaults) => {
    if (state.tree.open.has(key)) return true;
    if (state.tree.closed.has(key)) return false;
    return defaults.has(key);
  };

  const nodeText = (node) =>
    [node.label, node.subtitle, node.id, node.ct_id, node.device_id, node.ct_type, node.phase]
      .concat(Array.isArray(node.feed_ct_ids) ? node.feed_ct_ids : [])
      .concat(Array.isArray(node.badges) ? node.badges.map((b) => (b && b.text) || '') : [])
      .filter((part) => part !== null && part !== undefined && part !== '')
      .join(' ')
      .toLowerCase();

  const nodeMatches = (node, query) => nodeText(node).includes(query);

  // Keep a node when it matches, or when any descendant does, so a filtered
  // tree still shows the path down to the match.
  const filterSubtree = (node, query) => {
    const kids = (node.children || [])
      .map((child) => filterSubtree(child, query))
      .filter(Boolean);
    if (!nodeMatches(node, query) && kids.length === 0) return null;
    return { node, kids };
  };

  const toEntry = (node) => ({
    node,
    kids: (node.children || []).map(toEntry),
  });

  function topologyHierarchyCard(topology, tree) {
    const body = el('div', {
      id: 'topology-hierarchy-body',
      class: 'hier',
      // The view container is aria-live="polite"; a tree of a hundred nodes
      // must not be read out on every repaint.
      'aria-live': 'off',
    });

    // One short announcement per user action, instead of the whole tree.
    const status = el('p', { class: 'hier-status', role: 'status' });
    const announce = (text) => { status.textContent = text; };

    // The engine's tree is cloned once — right here — and every paint reuses
    // this copy.  Re-deriving it per repaint cloned the whole tree several
    // times per keystroke and per toggle for no benefit: the payload does not
    // change while the card is on screen.
    const nodes = hierarchyNodes(topology, tree);

    const repaint = (options) => {
      const focusKey = options && options.focusKey;
      const count = paintHierarchy(body, nodes, repaint);
      if (focusKey) {
        // The button that was clicked no longer exists: the whole list was
        // rebuilt.  Put focus back on its equivalent so keyboard use does not
        // drop to the document body after every toggle.
        const next = [...body.querySelectorAll('.hier-toggle')].find((b) => b.dataset.key === focusKey);
        if (next) next.focus({ preventScroll: true });
        announce(next
          ? `${next.getAttribute('aria-expanded') === 'true' ? 'Expanded' : 'Collapsed'}. ${count} rows visible.`
          : `${count} rows visible.`);
      }
      return count;
    };

    // Substantial trees are ~100 rows; rebuilding one per keystroke is wasted
    // work, and the user cannot type faster than a paint is useful.  120 ms is
    // long enough to coalesce a burst and short enough to feel immediate.
    let filterTimer = 0;
    const applyFilter = () => {
      filterTimer = 0;
      const count = repaint();
      const query = state.tree.filter.trim();
      announce(
        query
          ? `${count} rows match “${query}”.`
          : `${count} rows visible.`
      );
    };

    const filter = el('input', {
      type: 'search',
      class: 'hier-filter',
      placeholder: 'Filter circuits and devices…',
      'aria-label': 'Filter the hierarchy',
      value: state.tree.filter,
      oninput: (event) => {
        state.tree.filter = event.target.value;
        if (filterTimer) clearTimeout(filterTimer);
        filterTimer = setTimeout(applyFilter, 120);
      },
      // Enter applies immediately: a keyboard user should not have to wait out
      // the debounce to see the result of an explicit submit.
      onkeydown: (event) => {
        if (event.key !== 'Enter') return;
        event.preventDefault();
        if (filterTimer) clearTimeout(filterTimer);
        applyFilter();
      },
    });

    const expandAll = el('button', {
      class: 'btn btn-sm',
      type: 'button',
      onclick: () => {
        setAllExpanded(nodes, true);
        announce(`Expanded all. ${repaint()} rows visible.`);
      },
    }, 'Expand all');

    const collapseAll = el('button', {
      class: 'btn btn-sm',
      type: 'button',
      onclick: () => {
        setAllExpanded(nodes, false);
        announce(`Collapsed all. ${repaint()} rows visible.`);
      },
    }, 'Collapse all');

    const node = card('Hierarchy', [status, body], {
      id: 'topology-hierarchy',
      actions: [filter, expandAll, collapseAll],
      foot: tree
        ? `Computed by the engine${tree.version !== undefined ? ` (contract v${tree.version})` : ''} at ${ago(tree.generated_at)}. Each device appears once, under the circuit that measures it.`
        : 'Fallback view: CT containment from parent_ct_id only. Devices are not placed.',
    });

    // Paint in the same task as the card so the tree is never a frame late.
    const shown = paintHierarchy(body, nodes, repaint);
    // The layout actually used, recorded on the card so a bug report can name
    // it without re-deriving it from the markup.
    node.dataset.layout = tree ? 'tree' : 'legacy';
    announce(
      `${shown} rows shown.`
      + (tree ? '' : ' Hierarchy endpoint unavailable; showing CT containment.')
    );
    return node;
  }

  function setAllExpanded(nodes, open) {
    state.tree.open.clear();
    state.tree.closed.clear();
    const walk = (node) => {
      if ((node.children || []).length) {
        const key = nodeKey(node);
        if (open) state.tree.open.add(key);
        else state.tree.closed.add(key);
      }
      for (const child of node.children || []) walk(child);
    };
    for (const node of nodes) walk(node);
  }

  // ``onToggle(options)`` rebuilds the card.  It is supplied by the caller so
  // the focus-restoring repaint lives in one place.  ``nodes`` is computed once
  // by the caller; nothing here re-clones the engine's tree.  Returns the row
  // count so the caller can announce it without another DOM query.
  function paintHierarchy(body, nodes, onToggle) {
    if (!body) return 0;
    clear(body);

    const query = state.tree.filter.trim().toLowerCase();

    let entries;
    let forcedOpen = null;
    if (query) {
      entries = nodes.map((node) => filterSubtree(node, query)).filter(Boolean);
      forcedOpen = new Set();
      const mark = (entry) => {
        if (entry.kids.length) forcedOpen.add(nodeKey(entry.node));
        entry.kids.forEach(mark);
      };
      entries.forEach(mark);
    } else {
      entries = nodes.map(toEntry);
    }

    if (!entries.length) {
      body.append(
        emptyState(
          query ? 'No node matches' : 'No nodes yet',
          query ? `Nothing in this hierarchy matches “${state.tree.filter.trim()}”.` : 'This topology has no nodes to show.'
        )
      );
      return 0;
    }

    const ctx = {
      defaults: defaultOpenSet(nodes),
      forcedOpen,
      repaint: onToggle,
      // Incremented by ``renderEntry`` for every row actually appended, so the
      // row count costs nothing extra: no second DOM query, no second walk.
      count: 0,
    };

    body.append(
      el(
        'ul',
        { class: 'hier-list hier-root' },
        entries.map((entry) => renderEntry(entry, 0, ctx))
      )
    );
    return ctx.count;
  }

  function renderEntry(entry, depth, ctx) {
    const node = entry.node;
    const key = nodeKey(node);
    const hasKids = entry.kids.length > 0;
    const expanded = hasKids
      ? (ctx.forcedOpen ? ctx.forcedOpen.has(key) : isExpanded(key, ctx.defaults))
      : null;

    const item = el('li', { class: 'hier-node', dataset: { depth: String(depth) } }, [
      hierarchyRow(node, depth, expanded, key, ctx, entry.kids.length),
    ]);
    ctx.count += 1;

    if (hasKids && expanded) {
      item.append(
        el('ul', { class: 'hier-list' }, entry.kids.map((kid) => renderEntry(kid, depth + 1, ctx)))
      );
    }

    return item;
  }

  function hierarchyRow(node, depth, expanded, key, ctx, childCount) {
    const kind = String(node.kind || 'node');
    const row = el('div', { class: 'hier-row', dataset: { kind, depth: String(depth) } });

    if (expanded === null) {
      row.append(el('span', { class: 'hier-toggle-spacer', 'aria-hidden': 'true' }));
    } else {
      row.append(
        el('button', {
          class: 'hier-toggle',
          type: 'button',
          'aria-expanded': String(expanded),
          'aria-label': `${expanded ? 'Collapse' : 'Expand'} ${node.label || nodeKey(node)}`,
          dataset: { key },
          onclick: (event) => {
            const next = event.currentTarget.getAttribute('aria-expanded') !== 'true';
            if (next) {
              state.tree.open.add(key);
              state.tree.closed.delete(key);
            } else {
              state.tree.closed.add(key);
              state.tree.open.delete(key);
            }
            ctx.repaint({ focusKey: key });
          },
        })
      );
    }

    row.append(el('span', { class: 'hier-glyph', 'aria-hidden': 'true' }, GLYPHS[kind] || '·'));

    if (kind === 'group') {
      row.append(
        el('span', { class: 'hier-group-label' }, String(node.label || node.role || 'Group').toUpperCase())
      );
      row.append(el('span', { class: 'hier-count' }, String(childCount)));
    } else if (kind === 'panel') {
      row.append(el('span', { class: 'hier-label' }, node.label || node.panel_id || 'Panel'));
      // ``monitor`` is often the same words as the board name (a panel named
      // after its monitor); printing both reads as a stutter.
      if (node.monitor && String(node.monitor) !== String(node.label || '')) {
        row.append(el('span', { class: 'hier-sub' }, node.monitor));
      }
      if (node.is_service_entry) row.append(badge('Service entry', 'accent'));
      for (const item of nodeBadges(node)) row.append(item);
      const feeds = Array.isArray(node.feed_ct_ids) ? node.feed_ct_ids : [];
      if (feeds.length) {
        row.append(
          el('span', { class: 'hier-feeds', title: 'Feed CTs' },
            feeds.map((id) => el('span', { class: 'chip chip-ct' }, String(id))))
        );
      }
      if (node.subtitle && String(node.subtitle) !== String(node.label || '')) {
        row.append(el('span', { class: 'hier-sub' }, node.subtitle));
      }
    } else if (kind === 'circuit') {
      const name = node.label || node.ct_id || 'Circuit';
      row.append(el('span', { class: 'hier-label' }, name));
      // Only show the subtitle when it adds something: the engine may set both
      // to the ct_id, and printing it twice is pure noise.
      if (node.subtitle && String(node.subtitle) !== String(name)) {
        row.append(el('span', { class: 'hier-sub' }, node.subtitle));
      }
      row.append(badge(label(node.ct_type || 'unknown'), ctTone(node.ct_type)));
      if (node.phase && node.phase !== 'unknown') row.append(badge(label(node.phase), 'mute'));
      // Direct-child summary: how many fixtures sit here, and how many of them
      // the engine actually measured.  A circuit with no device children keeps
      // today's row rather than claiming "0 devices".
      const summary = circuitChildSummary(node);
      if (summary) row.append(summary);
      if (node.latest_power_w !== null && node.latest_power_w !== undefined) {
        row.append(el('span', { class: 'hier-power' }, watts(node.latest_power_w)));
      }
      if (node.enabled === false) row.append(badge('Disabled', 'bad'));
      if (node.alias_of_ct_id) row.append(badge(`alias of ${node.alias_of_ct_id}`, 'warn'));
      if (node.polarity === 'inverted') row.append(badge('Inverted', 'warn'));
      for (const item of nodeBadges(node)) row.append(item);
    } else if (kind === 'device') {
      const name = node.label || node.device_id || 'Device';
      const measurement = measurementOf(node);
      // ``measured`` also requires a number to print: a node the engine labels
      // measured but has no value for yet would otherwise put a bare dash in
      // the measured slot — defect 4.
      const measured = measurement === 'measured' && hasPowerValue(node);
      if (node.device_id) {
        row.append(
          el('a', { class: 'hier-link', href: `#/device/${encodeURIComponent(node.device_id)}` }, name)
        );
      } else {
        row.append(el('span', { class: 'hier-label' }, name));
      }
      if (node.phase && node.phase !== 'unknown') row.append(badge(label(node.phase), 'mute'));
      if (measured) {
        // A measured fixture: the wattage is the point of the row.
        row.append(el('span', { class: 'hier-power' }, watts(node.latest_power_w)));
        if (node.source) row.append(badge(label(node.source), sourceTone(node.source)));
        if (node.confidence !== null && node.confidence !== undefined) {
          row.append(el('span', { class: 'hier-conf' }, percent(node.confidence, 0)));
        }
      } else {
        // Not measured: an empty measured slot would read as "0 W", and a bare
        // em dash in the wattage column claims a value that does not exist.
        // Say so instead, with the engine's reason on the pill's title.
        row.append(
          badge('not measured', 'mute', {
            title: notMeasuredTitle(node.not_measured_reason, measurement, hasPowerValue(node)),
            dot: false,
          })
        );
      }
      const sub = deviceEvidenceLine(node);
      if (sub) row.append(el('span', { class: 'hier-sub' }, sub));
      // ``topology_status`` is the *wiring* claim (is this device declared on
      // these CTs?), not a measurement claim.  A green "confirmed" beside "not
      // measured" is exactly the contradiction the audit found, so a device the
      // engine could not measure drops the wiring-status pill altogether.  The
      // match is by identity — the badge whose text *is* ``topology_status`` —
      // so an unrelated badge (an alias, a disabled flag) still shows.
      for (const item of badgeItems(node)) {
        if (!measured && isWiringStatusBadge(item, node)) continue;
        row.append(badgeNode(item));
      }
    } else {
      row.append(el('span', { class: 'hier-label' }, node.label || nodeKey(node)));
      if (node.subtitle) row.append(el('span', { class: 'hier-sub' }, node.subtitle));
      for (const item of nodeBadges(node)) row.append(item);
    }

    return row;
  }

  const GLYPHS = { panel: '▤', group: '▫', circuit: '◉', device: '◆' };

  function topologyChannelsCard(topology) {
    const channels = [...(topology.channels || [])].sort((a, b) => {
      const order = { main: 0, branch: 1, dedicated_device: 2, unknown: 3 };
      return (order[a.ct_type] ?? 9) - (order[b.ct_type] ?? 9) || String(a.ct_id).localeCompare(String(b.ct_id));
    });
    return el('section', { class: 'card', id: 'topology-channels' }, [
      el('div', { class: 'card-head' }, [
        el('h2', {}, 'CT channels'),
        el('span', { class: 'spacer' }),
        el('span', { class: 'stat-hint' }, `${channels.length} total`),
      ]),
      el(
        'div',
        { class: 'card-body flush' },
        channels.length
          ? channels.map((ch) =>
              el('div', { class: 'channel-row', dataset: { type: ch.ct_type } }, [
                el('span', { class: 'sw', 'aria-hidden': 'true' }),
                el('div', { class: 'ch-main' }, [
                  // A CT whose name is just its phase ("l1") tells the reader
                  // nothing; the id does.  This mirrors the pre-existing rule.
                  el('div', { class: 'ch-name' },
                    ch.name && ch.name !== ch.phase ? ch.name : ch.ct_id),
                  el('div', { class: 'ch-sub' }, ch.entity_id || ch.ct_id),
                  ch.parent_ct_id
                    ? el('div', { class: 'ch-sub' }, `parent: ${ch.parent_ct_id}`)
                    : null,
                ]),
                el('div', { class: 'ch-tags' }, [
                  badge(label(ch.ct_type), ctTone(ch.ct_type)),
                  badge(label(ch.phase), 'mute'),
                  ch.enabled === false ? badge('Disabled', 'bad') : null,
                  ch.topology_status && ch.topology_status !== 'confirmed' ? statusBadge(ch.topology_status) : null,
                  ch.polarity === 'inverted' ? badge('Inverted', 'warn') : null,
                  ch.alias_of_ct_id ? badge(`alias of ${ch.alias_of_ct_id}`, 'warn') : null,
                ]),
              ])
            )
          : emptyState('No channels', 'This topology has no CT channels.')
      ),
    ]);
  }

  /* --- Nesting candidates (slow; fills in after the tree) ---------------- */

  const nestingIsFresh = () =>
    Boolean(state.nesting.data) && Date.now() - state.nesting.at < NESTING_TTL_MS;

  function topologyCandidatesSlot() {
    if (nestingIsFresh()) {
      // Only promise a background refresh when fillNestingCandidates will
      // actually issue one: inside the refresh-after window it deliberately
      // does not, and the footer must not claim otherwise.
      const willRefresh = Date.now() - state.nesting.at >= NESTING_REFRESH_AFTER_MS;
      return topologyCandidatesCard(state.nesting.data, { cached: true, willRefresh });
    }
    return topologyCandidatesLoadingCard();
  }

  function topologyCandidatesLoadingCard() {
    return card(
      'Parent and alias candidates',
      [
        el('div', { class: 'cand-loading', role: 'status' }, [
          el('span', { class: 'spinner', 'aria-hidden': 'true' }),
          el('span', {}, 'Comparing a 12-hour window for shared conductors and nested feeds…'),
        ]),
        el('div', { class: 'skeleton h-row' }),
        el('div', { class: 'skeleton h-row' }),
        el('div', { class: 'skeleton h-row' }),
      ],
      {
        id: 'topology-candidates',
        foot: 'The hierarchy above is already usable. This comparison only adds advisory candidates; nothing is written until you confirm it.',
      }
    );
  }

  function topologyCandidatesErrorCard(err) {
    return card(
      'Parent and alias candidates',
      [
        el('p', { class: 'stat-hint', style: 'margin-bottom:12px' },
          `The comparison did not finish: ${describeError(err)}`),
        el('button', { class: 'btn btn-sm', type: 'button', onclick: () => render() }, 'Try again'),
      ],
      { id: 'topology-candidates' }
    );
  }

  function fillNestingCandidates(signal) {
    // One request at a time.  A second render (a poll, a hash change, a
    // refresh) while a comparison is in flight must not queue another 12-hour
    // window on top of it.
    if (state.nesting.inflight) return;
    const age = Date.now() - state.nesting.at;
    if (nestingIsFresh() && age < NESTING_REFRESH_AFTER_MS) return;

    state.nesting.inflight = true;
    const done = () => { state.nesting.inflight = false; };
    get(NESTING_PATH, signal)
      .then((data) => {
        done();
        if (signal.aborted || !data) return;
        state.nesting = { data, at: Date.now(), inflight: false };
        swapCandidates(topologyCandidatesCard(data));
      })
      .catch((err) => {
        done();
        if (!err || err.name === 'AbortError') return;
        if (signal.aborted) return;
        // A cached answer is still a true answer for this window: keep it.
        if (nestingIsFresh()) return;
        swapCandidates(topologyCandidatesErrorCard(err));
      });
  }

  function swapCandidates(node) {
    const current = document.getElementById('topology-candidates');
    if (current) current.replaceWith(node);
  }

  function topologyCandidatesCard(nesting, { cached = false, willRefresh = false } = {}) {
    const candidates = nesting.candidates || [];
    const ambiguous = nesting.ambiguous || [];
    const suppressed = nesting.suppressed || [];
    const notInferable = nesting.not_inferable || [];
    const aliases = nesting.candidate_aliases || {};
    const aliasEntries = Object.entries(aliases);

    const body = [];
    if (candidates.length) {
      body.push(
        el('p', { class: 'stat-hint', style: 'margin-bottom:12px' },
          'Candidates only. No parent link is written until a user confirms it.'),
        ...candidates.map((candidate) => {
          const duplicate = candidate.kind === 'duplicate';
          const evidence = candidate.evidence || {};
          const detail = evidence.reason
            ? evidence.reason
            : duplicate
              ? 'Same conductor measured twice.'
              : 'Contained at every comparable instant.';
          const facts = duplicate
            ? [
                evidence.activity_ratio !== undefined ? `${percent(evidence.activity_ratio, 0)} co-active` : null,
                evidence.magnitude_ratio !== undefined ? `${num(evidence.magnitude_ratio, 2)}× magnitude` : null,
              ]
            : [
                evidence.containment !== undefined ? `${percent(evidence.containment, 1)} containment` : null,
                evidence.instants !== undefined ? `${evidence.instants} instants` : null,
              ];
          return el('div', { class: 'cand' }, [
            el('div', { class: 'cand-title' }, [
              badge(duplicate ? 'Duplicate feed' : 'Nested feed', duplicate ? 'warn' : 'info'),
              el('span', {}, candidate.child_ct_id),
              el('span', { class: 'arrow', 'aria-hidden': 'true' }, '→'),
              el('span', {}, candidate.parent_ct_id),
            ]),
            el('div', { class: 'cand-ev' }, detail),
            facts.filter(Boolean).length
              ? el('div', { class: 'cand-ev' }, facts.filter(Boolean).join(' · '))
              : null,
          ]);
        })
      );
    } else {
      body.push(
        el('p', { class: 'stat-hint' }, nesting.reason || 'No parent or duplicate candidates in the comparison window.')
      );
    }
    if (aliasEntries.length) {
      body.push(
        el('p', { class: 'stat-hint', style: 'margin-top:12px' },
          `Aliases measured: ${aliasEntries.map(([alias, canonical]) => `${alias} = ${canonical}`).join(', ')}. These are one conductor seen twice, never summed.`)
      );
    }
    if (ambiguous.length) {
      body.push(
        el('div', { class: 'notice', dataset: { tone: 'warn' }, style: 'margin-top:14px' }, [
          el('div', { class: 'banner-icon', 'aria-hidden': 'true' }, '!'),
          el('div', {}, [
            el('strong', {}, `${ambiguous.length} channel${ambiguous.length === 1 ? '' : 's'} ambiguous`),
            el('p', {}, 'Measurement cannot establish a level. The engine reports the options instead of guessing.'),
          ]),
        ])
      );
    }
    if (notInferable.length) {
      body.push(
        el('p', { class: 'stat-hint', style: 'margin-top:12px' },
          `${notInferable.length} load${notInferable.length === 1 ? '' : 's'} cannot be placed by measurement; a declared parent is needed.`)
      );
    }
    if (suppressed.length) {
      const reasons = {};
      for (const row of suppressed) reasons[row.reason] = (reasons[row.reason] || 0) + 1;
      body.push(
        el('p', { class: 'stat-hint', style: 'margin-top:12px' },
          `${suppressed.length} relationship${suppressed.length === 1 ? '' : 's'} suppressed (`
          + `${Object.entries(reasons).map(([reason, count]) => `${titleCase(reason)}: ${count}`).join(', ')}).`)
      );
    }
    return card('Parent and alias candidates', body, {
      id: 'topology-candidates',
      foot: nesting.instants
        ? `Compared over ${nesting.instants} instants across ${nesting.channels_compared || 0} channels.`
          + (cached
            ? (willRefresh
              ? ' Showing the cached result; refreshing in the background.'
              : ' Showing the cached result from a moment ago.')
            : '')
        : null,
    });
  }

  async function confirmTopology() {
    try {
      await post('/topology/confirm', {});
      notify('Topology confirmed', 'The engine now treats this topology as authoritative.');
      render();
    } catch (err) {
      notify('Could not confirm topology', describeError(err), 'bad');
    }
  }

  /* --- Discover devices (contract §5.4 item 6) -------------------------- *
   * Placement discovery reads the recorder and proposes devices for fixtures
   * whose transitions it can prove.  Analysing writes nothing; a write needs a
   * second, explicit click per row.  There is no modal in this panel, so the
   * two-step confirm lives on the button label itself.
   * ---------------------------------------------------------------------- */

  const DISCOVER_PATH = '/discover/placements';

  //: Why the engine withheld or refused a fixture, in words a person can act on.
  //: Keys are the engine's own codes; an unlisted code still falls back to
  //: ``titleCase`` so a newer engine is legible rather than blank.
  const WITHHELD_REASONS = {
    // Correlation withheld the transition evidence.
    not_discriminating: 'Its transitions do not move measured power',
    asymmetric_response: 'It responds to on and off differently',
    not_specific: 'Its transitions are not specific to one row',
    too_few_corroborations: 'Too few independent transitions to prove it',
    inconsistent_with_row: 'Its response contradicts the row it appears on',
    ambiguous_rows: 'Several rows could explain it equally well',
    no_row_responded: 'No CT row moved when it switched',
    no_independent_transitions: 'Its transitions came in bursts, so none could be paired',
    // The candidate population refused the entity outright.
    already_declared_state_entity: 'Already configured by the user; left unchanged',
    wattmosaic_entity: "It is WattMosaic's own output, not a load",
    not_a_load_entity_category: 'Home Assistant marks it as a setting, not a load',
    mode_entity: 'It is a mode control, so its state does not prove a load',
    not_a_load_domain: 'Its state does not prove a load is running',
  };

  //: Reasons that mean "a device already carries this evidence", i.e. nothing is
  //: wrong with the measurement -- there is simply nothing new to write.
  //: ``already configured`` is a prefix check because the placement layer emits
  //: "already configured by the user; left unchanged".
  const ALREADY_DECLARED_REASON = 'already_declared_state_entity';

  const isAlreadyWritten = (reason) => {
    const text = String(reason || '').trim();
    return text === ALREADY_DECLARED_REASON || text.startsWith('already configured');
  };

  function withheldReason(reason) {
    const text = String(reason || '').trim();
    if (!text) return 'No reason given';
    if (text.startsWith('co_toggles_with_another_fixture_on_')) {
      const ct = text.slice('co_toggles_with_another_fixture_on_'.length);
      return `It always toggles with another fixture on ${ct.replace(/_/g, ' ')}`;
    }
    if (text.startsWith('identified_by_aggregate_fit_on_')) {
      const ct = text.slice('identified_by_aggregate_fit_on_'.length);
      return `Only the aggregate fit found it on ${ct.replace(/_/g, ' ')}; that is not proof of one fixture`;
    }
    if (text.startsWith('already configured')) return 'Already configured by the user; left unchanged';
    if (WITHHELD_REASONS[text]) return WITHHELD_REASONS[text];
    // Placement refusals are already sentences written for a person
    // ("CT row 'ct1' measures the whole installation; ...").  Only a bare
    // single-token code needs title-casing.
    if (/[^a-z0-9_]/.test(text)) return text;
    return titleCase(text);
  }

  const discoverSlotNode = () =>
    el('div', { class: 'card discover-slot', id: 'topology-discover' }, [
      el('div', { class: 'card-head' }, [
        el('h2', {}, 'Discover devices'),
        el('span', { class: 'spacer' }),
        el('span', { class: 'stat-hint' }, 'Not run yet'),
      ]),
      el('div', { class: 'card-body' }, [
        el('p', { class: 'stat-hint' },
          'Find fixtures that sit on a shared circuit by comparing their transitions with the CT rows. Analysing writes nothing.'),
      ]),
    ]);

  function topologyDiscoverSlot() {
    if (state.discover.error) return discoverErrorCard(state.discover.error);
    if (state.discover.data) return discoverCard(state.discover.data);
    if (state.discover.running) return discoverLoadingCard();
    return discoverSlotNode();
  }

  function swapDiscover(node) {
    const current = document.getElementById('topology-discover');
    if (current) {
      current.replaceWith(node);
      return;
    }
    // No slot on screen (the user navigated away while the request ran): the
    // answer is still cached in state, so the next paint of this tab shows it.
    // Painting into a page it does not belong to would be worse than dropping
    // the render.
  }

  function discoverLoadingCard() {
    return card(
      'Discover devices',
      [
        el('div', { class: 'cand-loading', role: 'status' }, [
          el('span', { class: 'spinner', 'aria-hidden': 'true' }),
          el('span', {}, 'Comparing fixture transitions with the CT rows over the last 24 hours…'),
        ]),
        el('div', { class: 'skeleton h-row' }),
        el('div', { class: 'skeleton h-row' }),
      ],
      { id: 'topology-discover' }
    );
  }

  function discoverErrorCard(err) {
    const missing = Boolean(err && err.status === 404);
    const text = missing
      ? 'This engine does not serve POST /api/v1/discover/placements (HTTP 404). Update the engine to find devices automatically; nothing was changed.'
      : `The comparison did not finish: ${describeError(err)}`;
    return card(
      'Discover devices',
      [
        el('p', { class: 'stat-hint', style: 'margin-bottom:12px' }, text),
        el('button', {
          class: 'btn btn-sm',
          type: 'button',
          onclick: () => runDiscovery(),
        }, 'Try again'),
      ],
      { id: 'topology-discover' }
    );
  }

  async function runDiscovery() {
    if (state.discover.running) return;
    state.discover = { ...state.discover, running: true, error: null };
    // The head button says "Discovering…" while the request runs.
    const head = document.getElementById('topology-discover-run');
    if (head) {
      head.disabled = true;
      head.textContent = 'Discovering…';
    }
    swapDiscover(discoverLoadingCard());
    try {
      const data = await post(DISCOVER_PATH, { apply: false, include_groups: true });
      state.discover = { data, running: false, error: null };
      swapDiscover(discoverCard(data));
    } catch (err) {
      state.discover = { data: null, running: false, error: err };
      swapDiscover(discoverErrorCard(err));
      if (err && err.status === 404) {
        notify('Discovery unavailable', 'This engine does not serve /discover/placements.', 'bad');
      } else {
        notify('Discovery failed', describeError(err), 'bad');
      }
    } finally {
      const button = document.getElementById('topology-discover-run');
      if (button) {
        button.disabled = false;
        button.textContent = 'Discover devices';
      }
    }
  }

  function discoverCard(data) {
    const accepted = Array.isArray(data.accepted) ? data.accepted : [];
    const groups = Array.isArray(data.groups) ? data.groups : [];
    const withheld = data.withheld && typeof data.withheld === 'object' ? Object.entries(data.withheld) : [];
    const refused = data.refused && typeof data.refused === 'object' ? Object.entries(data.refused) : [];
    //: Candidate-level refusals and placement-level refusals are both shown
    //: under Withheld, so the Placed empty state has to read them together.
    const withheldRows = withheld.concat(refused);
    const stats = data.stats || {};
    const nothing = !accepted.length && !groups.length && !withheld.length && !refused.length;

    const body = [];

    // Only a hard precondition (``ok: false``, e.g. "no topology to place
    // devices on") belongs on this card: the run produced no result at all.
    //
    // A refusal is different: the engine answers ``ok: true`` with
    // ``applied: false`` and a ``detail`` ("auto placement is disabled...", or
    // "apply is false; pass apply=true...").  That detail is produced by a
    // *read-only* run as well, so rendering it here made every ordinary
    // Discover that found anything warn "Nothing written yet" -- a write the
    // user never asked for.  The refusal explanation belongs to the Apply
    // action that can actually hit it, where the toast relays it; see
    // ``applyButton``.
    if (data.ok === false && data.detail) {
      body.push(notice('Discovery reported a problem', String(data.detail), 'bad'));
    }

    if (nothing) {
      // Empty collections mean two different things and only ``ok`` tells them
      // apart.  An ``ok: false`` response is a hard precondition -- with no
      // active topology the engine answers "no topology to place devices on"
      // before it reads a single transition, so every list comes back empty.
      // That looks exactly like a completed run that proved nothing, but the
      // comparison never happened, so only ``ok: true`` may describe one.
      if (data.ok === false) {
        body.push(emptyState(
          'Discovery could not run',
          'The engine stopped before reading any fixture transition, so nothing was analysed and no placement was computed.'
            + (data.detail ? ' The notice above names the precondition.' : '')
        ));
      } else {
        body.push(emptyState(
          'No shared circuits could be resolved',
          'The engine compared every fixture transition it has with every branch CT row and found nothing it could prove. That is a measurement result, not an error.'
        ));
      }
      return card('Discover devices', body, {
        id: 'topology-discover',
        foot: discoveryFoot(data, stats),
      });
    }

    const applyAll = accepted.length && !accepted.every((row) => row.applied)
      ? applyButton({ name: 'all placements', all: true })
      : null;

    /* Placed ------------------------------------------------------------ *
     * An empty "Placed" list does not mean the evidence was thin.  On a
     * second run every candidate a previous apply wrote is refused as
     * ``already_declared_state_entity``, so the honest message depends on
     * why nothing is placed.  Only claim the evidence bar was missed when
     * no candidate was refused for already being written; when the reasons
     * are mixed, claim only that the proved fixtures are already written and
     * let Withheld speak for itself -- the remaining rows were often never
     * measured at all, so they cannot have missed a measurement bar.
     * ------------------------------------------------------------------- */
    const alreadyWritten = withheldRows.filter(([, reason]) => isAlreadyWritten(reason)).length;
    const everythingWritten = alreadyWritten > 0 && alreadyWritten === withheldRows.length;
    const placed = accepted.length
      ? el('div', { class: 'discover-list' },
          accepted.map((row) => discoverPlacedRow(row)))
      : alreadyWritten
        ? emptyState(
            'Nothing new to place',
            everythingWritten
              ? 'The fixtures already written as devices are refused as duplicates, so nothing new was placed. They sit under Withheld because there is nothing new to add.'
              : 'Each fixture already written as a device is refused as a duplicate, so nothing new was placed; see Withheld for why each other entry was left out.'
          )
        : emptyState(
            'Nothing placed',
            'No fixture reached the evidence bar for a measured placement.'
          );

    body.push(
      el('section', { class: 'discover-section' }, [
        el('div', { class: 'discover-section-head' }, [
          el('h3', {}, 'Placed'),
          el('span', { class: 'hier-count' }, String(accepted.length)),
          el('span', { class: 'spacer' }),
          applyAll,
        ]),
        el('p', { class: 'stat-hint' },
          'Each fixture below was proven on one CT row by measured co-movement. Nothing is written until you apply it; the engine writes the accepted set.'),
        placed,
      ])
    );

    /* Groups ------------------------------------------------------------ */
    const groupSection = el('section', { class: 'discover-section' }, [
      el('div', { class: 'discover-section-head' }, [
        el('h3', {}, 'Groups'),
        el('span', { class: 'hier-count' }, String(groups.length)),
        el('span', { class: 'spacer' }),
        badge('advisory', 'info'),
      ]),
      discoverGroupsNote(groups),
      groups.length
        ? el('div', { class: 'discover-list' }, groups.map((group) => discoverGroupRow(group)))
        : el('p', { class: 'stat-hint' }, 'No co-toggling groups were found on a shared row.'),
    ]);
    body.push(groupSection);

    /* Withheld ---------------------------------------------------------- */
    body.push(
      el('section', { class: 'discover-section' }, [
        el('div', { class: 'discover-section-head' }, [
          el('h3', {}, 'Withheld'),
          el('span', { class: 'hier-count' }, String(withheldRows.length)),
          el('span', { class: 'spacer' }),
        ]),
        withheldRows.length
          ? el('div', { class: 'discover-list' },
              withheldRows.map(([entityId, reason]) =>
                el('div', { class: 'discover-row withhold' }, [
                  el('div', { class: 'discover-main' }, [
                    el('span', { class: 'hier-sub' }, String(entityId)),
                    el('span', { class: 'discover-ev' }, withheldReason(reason)),
                  ]),
                ])
              ))
          : el('p', { class: 'stat-hint' }, 'Nothing was withheld.'),
      ])
    );

    return card('Discover devices', body, {
      id: 'topology-discover',
      foot: discoveryFoot(data, stats),
    });
  }

  //: How many ``<entity>: <reason>`` notes are listed by name before the footer
  //: groups them by reason instead.  Production returns about 25, which is over
  //: 1600 characters of footer nobody reads.
  const NOTE_DETAIL_LIMIT = 4;

  /**
   * Split an engine note into ``{entity, reason}`` when it is an entity note.
   *
   * ``main.py`` appends two shapes to ``notes``: ``<entity_id>: <reason>`` from
   * the correlation report, and free prose for the truncation and refusal
   * counts.  Matching the entity id as ``domain.object_id`` is what keeps a
   * colon inside a sentence from being read as an entity note.  Returns
   * ``null`` for prose, which the caller passes through verbatim.
   */
  function splitEntityNote(note) {
    const text = String(note == null ? '' : note);
    const at = text.indexOf(': ');
    if (at < 0) return null;
    const entity = text.slice(0, at).trim();
    const reason = text.slice(at + 2).trim();
    // An engine entity id is ``domain.object_id`` with no whitespace, so prose
    // such as "12 state entities are not candidates (...)" can never be read as
    // an entity note merely because it contains a colon.
    if (!/^[a-z0-9_]+\.[a-z0-9_]+$/.test(entity)) return null;
    // ``/[^a-z0-9_]/`` is the same bare-code test ``withheldReason`` uses: a
    // reason containing any other character is already a sentence, and
    // title-casing it would mangle it.
    if (!reason || /[^a-z0-9_]/.test(reason)) return null;
    return { entity, reason };
  }

  /**
   * The footer's engine-note text: no raw enum, and short enough to read.
   *
   * A few entity notes are listed one per line ("light.hall: Too few
   * independent transitions to prove it"); many are grouped by reason with a
   * count, which still states exactly how many entries each reason covered.
   * Prose notes pass through untouched and are never title-cased.
   */
  function discoveryNoteText(notes) {
    const entityNotes = [];
    const prose = [];
    for (const note of notes) {
      const parsed = splitEntityNote(note);
      if (parsed) entityNotes.push(parsed);
      else prose.push(String(note));
    }

    const segments = [];
    if (entityNotes.length && entityNotes.length <= NOTE_DETAIL_LIMIT) {
      segments.push(
        entityNotes
          .map(({ entity, reason }) => `${entity}: ${withheldReason(reason)}`)
          .join('; ')
      );
    } else if (entityNotes.length) {
      const counts = new Map();
      for (const { reason } of entityNotes) {
        const phrase = withheldReason(reason);
        counts.set(phrase, (counts.get(phrase) || 0) + 1);
      }
      const grouped = Array.from(counts, ([phrase, count]) => [phrase, count])
        .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
        .map(([phrase, count]) => `${phrase} (${count})`);
      segments.push(
        `${entityNotes.length} entries not placed, by reason: ${grouped.join('; ')}`
      );
    }
    segments.push(...prose);
    return `Engine notes: ${segments.join('; ')}`;
  }

  function discoveryFoot(data, stats) {
    const parts = [];
    if (stats.candidate_entities !== undefined) {
      parts.push(`${stats.candidate_entities} candidate fixtures`);
    }
    if (stats.branch_rows !== undefined) parts.push(`${stats.branch_rows} branch rows`);
    if (data.lookback_hours !== undefined) parts.push(`last ${num(data.lookback_hours, 0)}h`);
    if (data.topology_version !== undefined) parts.push(`topology v${data.topology_version}`);
    const notes = (Array.isArray(data.notes) ? data.notes : [])
      // A hard precondition repeats its sentence in both ``detail`` and
      // ``notes`` (e.g. "no topology to place devices on"), and the notice
      // above already shows it.  Printing it again as an engine note is the
      // same sentence twice on one card.
      .filter((note) => !(data.detail && String(note) === String(data.detail)));
    // With every note deduped there is nothing to report; "Analysing writes
    // nothing" would be a claim about a run that never analysed.
    if (notes.length) parts.push(discoveryNoteText(notes));
    else if (!data.detail) parts.push('Analysing writes nothing.');
    return parts.join(' · ');
  }

  function discoverPlacedRow(row) {
    const facts = [];
    if (row.agreement !== undefined && row.agreement !== null) facts.push(`${percent(row.agreement, 0)} agreement`);
    if (row.corroborations !== undefined && row.corroborations !== null) facts.push(`${row.corroborations} corroborations`);
    if (row.symmetry !== undefined && row.symmetry !== null) facts.push(`${num(row.symmetry, 2)} symmetry`);
    if (row.specificity_z !== undefined && row.specificity_z !== null) facts.push(`${num(row.specificity_z, 0)}σ specificity`);
    const evidence = Array.isArray(row.evidence) ? row.evidence : [];
    const button = applyButton(row);
    return el('div', { class: 'discover-row' }, [
      el('div', { class: 'discover-main' }, [
        el('div', { class: 'discover-title' }, [
          el('span', { class: 'hier-label' }, String(row.name || row.entity_id || row.device_id || 'Fixture')),
          el('span', { class: 'arrow', 'aria-hidden': 'true' }, '→'),
          el('span', { class: 'chip chip-ct' }, String(row.ct_label || row.ct_id || 'unknown CT')),
          row.applied ? badge('applied', 'ok') : null,
        ]),
        el('div', { class: 'discover-meta' }, [
          el('span', { class: 'hier-power' }, watts(row.on_w)),
          ...facts.map((fact) => el('span', { class: 'discover-fact' }, fact)),
        ]),
        evidence.length
          ? el('ul', { class: 'discover-evidence' },
              evidence.map((item) => el('li', {}, String(item))))
          : null,
      ]),
      el('div', { class: 'discover-actions' }, [button]),
    ]);
  }

  /**
   * The two-step Apply button.
   *
   * There is no modal library in this panel and adding one is out of scope, so
   * the confirmation *is* the button: the first click asks, the second writes.
   * A blur cancels, because the user looked away.
   *
   * ``row.all`` means "apply every accepted placement".  There is no per-entity
   * filter in the frozen facade signature, so *both* buttons write the proven
   * set together — the row button is a shortcut to that same engine decision,
   * never a client-side subset.  The label and toast say so; pretending a
   * per-row write happened would be a lie about what the engine did.
   */
  function applyButton(row) {
    let armed = false;
    const isAll = Boolean(row && row.all);
    const idle = isAll ? 'Apply all' : 'Apply';
    const scope = isAll
      ? 'Write every proven placement.'
      : 'The engine writes the proven set together; this fixture is included.';
    const button = el('button', {
      class: 'btn btn-sm',
      type: 'button',
      title: scope,
      'aria-label': isAll ? 'Apply all placements' : `Apply ${row.name || row.entity_id || 'fixture'}`,
      onclick: async () => {
        if (!armed) {
          armed = true;
          button.textContent = 'Confirm apply?';
          button.setAttribute('aria-label', 'Confirm apply');
          button.dataset.armed = 'true';
          return;
        }
        if (button.disabled) return;
        button.disabled = true;
        button.textContent = 'Applying…';
        try {
          // ``apply: true`` has no per-entity filter in the engine contract:
          // the engine decides the accepted set, and re-deriving a subset in the
          // browser would be a second placement decision.  So both the per-row
          // and the Apply-all button write the proven set, and the toast says so
          // instead of claiming only one device changed.
          //
          // A 2xx does *not* mean a write: a refusal comes back ``ok: true``
          // with ``applied: false`` and a ``detail`` (auto placement disabled,
          // apply not requested).  Claiming "applied" there is a false success,
          // so the toast is chosen by the flag, not by the HTTP status.
          const result = await post(DISCOVER_PATH, { apply: true });
          if (result && result.applied === true) {
            notify(
              'Placements applied',
              isAll
                ? 'The engine wrote every proven placement and re-projected the topology.'
                : `The engine wrote every proven placement, including ${row.name || row.entity_id}, and re-projected the topology.`
            );
            // The cached analysis is now stale: the written rows carry
            // ``applied``, so re-showing the old answer would misreport them.
            state.discover = { data: null, running: false, error: null };
          } else {
            // Say only what happened: the call returned no write.  Relay the
            // engine's own explanation (it names the switch to turn on) and add
            // the one mechanical fact -- nothing changed.
            const raw = result && result.detail ? String(result.detail).trim() : '';
            const detail = raw
              ? `${raw.charAt(0).toUpperCase()}${raw.slice(1).replace(/[.\s]+$/, '')}. `
              : '';
            notify(
              'Nothing changed',
              `${detail}No placement was made; the topology is unchanged.`,
              'warn'
            );
            // Nothing was written, so the analysis on the card is still the
            // current one: keep it (and its explanation) rather than blanking
            // the panel after a refusal.  Reset the button in case this render
            // does not reach the discover slot.
            armed = false;
            button.disabled = false;
            button.dataset.armed = 'false';
            button.textContent = idle;
            state.discover = { ...state.discover, running: false };
          }
          render();
        } catch (err) {
          notify('Could not apply placement', describeError(err), 'bad');
          armed = false;
          button.disabled = false;
          button.dataset.armed = 'false';
          button.textContent = idle;
        }
      },
      onblur: () => {
        if (!armed) return;
        armed = false;
        button.dataset.armed = 'false';
        button.textContent = idle;
        button.setAttribute(
          'aria-label',
          row && row.all ? 'Apply all placements' : `Apply ${row.name || row.entity_id || 'fixture'}`
        );
      },
    }, idle);
    return button;
  }

  /* --- Groups ---------------------------------------------------------- */

  // ``individually_identifiable`` is the engine's statement that the aggregate
  // fit separated a real coefficient for this group's member.  Only a literal
  // ``true`` is evidence: an older engine omits the key, and a missing key must
  // never be read as "a measurement happened".  Everything else renders as the
  // conservative ``grouped`` claim.
  const groupIdentifiable = (group) =>
    Boolean(group && group.individually_identifiable === true);

  // The coefficient the engine fitted for one member, or ``null`` when it gave
  // none.  ``Number('')`` is ``0``, which would be a fabricated wattage, so the
  // empty string is rejected before the numeric coercion.
  const memberWatts = (member) => {
    const value = member && member.estimated_w;
    if (value === null || value === undefined || value === '') return null;
    const n = Number(value);
    return Number.isFinite(n) ? n : null;
  };

  /**
   * The Groups section used to warn, once, that *no* group was individually
   * identifiable.  That was true when the engine hard-coded the flag to false;
   * it is false now, because a single-fixture group carries its own measured
   * coefficient.  So the warning is emitted only when at least one group really
   * is unidentifiable, and in a mixed section it counts how many and defers the
   * per-row badge to say which.  An all-identified section gets a neutral note
   * instead, so "never a device" is still stated without the false warning.
   */
  function discoverGroupsNote(groups) {
    if (!groups.length) return null;
    const unidentified = groups.filter((group) => !groupIdentifiable(group));
    if (!unidentified.length) {
      return el('p', { class: 'stat-hint' },
        'Every group here is marked identified by the aggregate fit, so its fitted draw is a separated coefficient rather than an unexplained aggregate. A group is advisory evidence only and never becomes a device.');
    }
    const mixed = unidentified.length < groups.length;
    const identifiedCount = groups.length - unidentified.length;
    // The body describes what each badge means and what is on screen.  It never
    // claims a split that was measured, and it never claims the warning applies
    // to an identified row.  "Identified" is the engine's statement, not ours.
    // The co-toggle sentence is conditional because a one-member group has no
    // sibling to toggle with, and the engine may still decline to identify it.
    const coToggle = unidentified.some(
      (group) => (Array.isArray(group.members) ? group.members.length : 0) > 1
    )
      ? ' Where the members toggle together the combined draw is measured; the split between them is not.'
      : '';
    const body = mixed
      ? `The ${unidentified.length} ${unidentified.length === 1 ? 'group' : 'groups'} badged grouped ${unidentified.length === 1 ? 'has' : 'have'} no identified per-fixture wattage, so only the group total is shown. The ${identifiedCount} ${identifiedCount === 1 ? 'group' : 'groups'} badged identified ${identifiedCount === 1 ? 'carries' : 'carry'} a coefficient the aggregate fit separated. No group becomes a device.`
      : `${groups.length === 1 ? 'This group has' : 'None of these groups has'} no identified per-fixture wattage, so only the group total is shown.${coToggle} No device is created.`;
    return el('div', { class: 'notice', dataset: { tone: 'warn' }, style: 'margin-bottom:12px' }, [
      el('div', { class: 'banner-icon', 'aria-hidden': 'true' }, 'i'),
      el('div', {}, [
        el('strong', {}, mixed
          ? `Not individually identifiable for ${unidentified.length} of ${groups.length} groups`
          : 'Not individually identifiable'),
        el('p', {}, body),
      ]),
    ]);
  }

  // The tooltip must never claim a per-member coefficient that the row does
  // not show.  A singleton with a fitted ``estimated_w`` gets the strong claim;
  // every other identified shape only repeats what the engine reported.
  function identifiedTitle(soloW) {
    if (soloW !== null) {
      return 'The aggregate fit identified this one fixture on its own; the wattage is a fitted coefficient, not a split of a group total';
    }
    return 'The engine reports this group as individually identifiable: the aggregate fit separated its coefficient, so the group total is a fitted value';
  }

  // A group the engine did not mark identifiable must not claim its members
  // "toggle together" when the row holds fewer than two of them -- there is
  // nothing to toggle with.  The honest claim is the absence of a separated
  // coefficient, so only the group total is shown.
  const groupedTitle = (members) => {
    if (members.length > 1) return 'The members toggle together; the split is not measured';
    if (members.length === 1) {
      return 'The engine did not report an identified coefficient for this fixture, so only the group total is shown';
    }
    return 'The engine did not identify a per-fixture coefficient for this group, so only the group total is shown';
  };

  function discoverGroupRow(group) {
    const members = Array.isArray(group.members) ? group.members : [];
    const identified = groupIdentifiable(group);
    const margin = group.r2_margin !== undefined && group.r2_margin !== null
      ? `${num(group.r2_margin, 2)} margin over null`
      : null;
    const r2 = group.r2 !== undefined && group.r2 !== null ? `R² ${num(group.r2, 3)}` : null;
    const evidence = Array.isArray(group.evidence) ? group.evidence : [];
    // Only a *single-member* group may carry a per-member figure, and only when
    // the engine actually reported one.  The frozen contract sets
    // ``estimated_w`` to ``null`` for every member of a multi-fixture group,
    // and this panel never divides ``total_w`` by the member count -- a split
    // would be a fabrication, not a measurement.
    const soloW = identified && members.length === 1 ? memberWatts(members[0]) : null;
    const facts = [r2, margin].filter(Boolean);
    return el('div', { class: identified ? 'discover-row group identified' : 'discover-row group' }, [
      el('div', { class: 'discover-main' }, [
        el('div', { class: 'discover-title' }, [
          el('span', { class: 'chip chip-ct' }, String(group.ct_label || group.ct_id || 'unknown CT')),
          el('span', { class: 'hier-power' }, watts(group.total_w)),
          // A singleton shows a second wattage on its member chip, so the total
          // is captioned here; without it two equal numbers would look like a
          // contradiction rather than one measurement stated twice.
          soloW !== null ? el('span', { class: 'discover-fact' }, 'group total') : null,
          identified
            ? badge('identified', 'ok', { title: identifiedTitle(soloW) })
            : badge('grouped', 'warn', { title: groupedTitle(members) }),
        ]),
        el('div', { class: 'discover-meta' }, facts
          .map((fact) => el('span', { class: 'discover-fact' }, fact))),
        members.length
          ? el('div', { class: 'discover-members' },
              members.map((member) => discoverGroupMember(member, soloW !== null)))
          : null,
        evidence.length
          ? el('ul', { class: 'discover-evidence' },
              evidence.map((item) => el('li', {}, String(item))))
          : null,
      ]),
    ]);
  }

  // The member chip is captioned "identified" only when the engine actually
  // reported a coefficient for it.  A group flagged identifiable whose member
  // carries no number must not have a wattage (or the word "identified")
  // attributed to that member.
  function discoverGroupMember(member, isSolo) {
    const name = String((member && (member.name || member.entity_id)) || 'member');
    const value = isSolo ? memberWatts(member) : null;
    if (value === null) return el('span', { class: 'chip' }, name);
    return el('span', {
      class: 'chip discover-member-identified',
      title: 'This is the only member; the aggregate fit identified its draw directly',
    }, [
      el('span', { class: 'discover-member-tag' }, 'identified'),
      name,
      el('span', { class: 'discover-member-watts' }, watts(value)),
    ]);
  }

  /* --- Learning blocked banner (contract §5.4 item 7) ------------------- */

  function topologyLearningBanner(status, topology) {
    const learning = (status && status.learning) || {};
    const detail = (status && status.learning_status) || {};
    const effective = String(learning.effective_state || detail.effective_state || '');
    const blockedDetail = learning.blocked_reason_detail || detail.blocked_reason_detail || null;
    const blockedBy = learning.blocked_by || detail.blocked_by || null;
    if (effective !== 'blocked') return null;
    // The contract's explicit key is ``blocked_reason_detail``; the engine also
    // reports the same condition as ``blocked_by: "unconfirmed_topology"``, and
    // older engines as ``blocked_by: "observed_rejections"`` with a candidate
    // topology.  All three mean "the wiring is unconfirmed", which is the one
    // thing this banner offers to fix — no other blocked reason shows it.
    const quality = String(
      learning.topology_quality || detail.topology_quality || ''
    ).toLowerCase();
    const unconfirmed =
      blockedDetail === 'topology_not_confirmed'
      || blockedBy === 'unconfirmed_topology'
      || (blockedBy === 'observed_rejections' && quality === 'candidate');
    if (!unconfirmed) return null;
    if (topology && topology.status === 'confirmed') return null;

    return el('div', { class: 'notice', dataset: { tone: 'warn' } }, [
      el('div', { class: 'banner-icon', 'aria-hidden': 'true' }, '!'),
      el('div', { class: 'notice-body' }, [
        el('strong', {}, 'Learning is paused — confirm the topology to start collecting samples'),
        el('p', {}, 'Every sample is being rejected as ambiguous until the wiring is confirmed. Confirm only when the CT roles match your wiring.'),
        el('button', {
          class: 'btn btn-sm btn-primary',
          type: 'button',
          onclick: confirmTopology,
        }, 'Confirm topology'),
      ]),
    ]);
  }

  /* --- Quality ---------------------------------------------------------- */

  async function renderQuality(root, signal) {
    const report = await get('/quality', signal);
    if (signal.aborted) return;

    root.append(
      el('div', { class: 'page-head' }, [
        el('div', {}, [
          el('h1', {}, 'Data quality'),
          el('p', { class: 'lede' },
            'Per-channel health and the concrete problems the engine found. A low score is information, not a failure.'),
        ]),
        el('div', { class: 'head-actions' }, [
          el('button', { class: 'btn', type: 'button', onclick: () => render() }, 'Re-evaluate view'),
        ]),
      ])
    );

    const score = Number(report.overall_score);
    const tone = score >= 0.85 ? 'ok' : score >= 0.6 ? 'warn' : 'bad';
    const channels = Object.entries(report.channels || {});
    const issues = report.issues || [];

    root.append(
      el('section', { class: 'card', style: 'margin-bottom:16px' }, [
        el('div', { class: 'card-body' }, [
          el('div', { class: 'score-dial' }, [
            el('div', {
              class: 'dial',
              style: `--v:${Number.isFinite(score) ? Math.round(score * 100) : 0};--tone:var(--${tone})`,
              role: 'img',
              'aria-label': `Overall data quality ${percent(score, 0)}`,
            }, el('span', { class: 'n' }, percent(score, 0))),
            el('div', {}, [
              el('h2', {}, 'Overall channel health'),
              el('p', { class: 'stat-hint' },
                `${channels.length} channels evaluated · ${issues.length} issue${issues.length === 1 ? '' : 's'} · ${ago(report.timestamp)}`),
              el('div', { style: 'display:flex;gap:8px;margin-top:10px;flex-wrap:wrap' }, [
                statusBadge(score >= 0.85 ? 'good' : score >= 0.6 ? 'degraded' : 'bad'),
                issues.length ? badge(`${issues.length} open issues`, 'warn') : badge('No issues', 'ok'),
              ]),
            ]),
          ]),
        ]),
      ])
    );

    root.append(
      el('div', { class: 'grid cols-2' }, [
        qualityIssuesCard(issues),
        qualityChannelsCard(channels),
      ])
    );
  }

  function qualityIssuesCard(issues) {
    return card(
      'Issues',
      [
        issues.length
          ? el('div', {}, issues.map((text) =>
              el('div', { class: 'issue' }, [
                el('span', { class: 'sev', 'aria-hidden': 'true' }),
                el('div', { class: 'txt' }, String(text)),
              ])
            ))
          : el('p', { class: 'stat-hint' }, 'The engine reported no data-quality issues.'),
      ],
      { flush: true, foot: 'Issues are deduplicated by the engine, one per cluster.' }
    );
  }

  function qualityChannelsCard(channels) {
    const filter = state.qualityFilter;
    const visible = channels.filter(([, ch]) => {
      if (filter === 'all') return true;
      const status = String(ch.status || '').toLowerCase();
      if (filter === 'issues') return (ch.issues || []).length > 0;
      return status === filter;
    });

    const filterBar = el('div', { class: 'segmented', role: 'group', 'aria-label': 'Filter channels' },
      [['all', 'All'], ['good', 'Good'], ['degraded', 'Degraded'], ['issues', 'With issues']].map(([key, text]) =>
        el('button', {
          type: 'button',
          'aria-pressed': String(filter === key),
          onclick: () => { state.qualityFilter = key; render(); },
        }, text)
      ));

    return el('section', { class: 'card' }, [
      el('div', { class: 'card-head' }, [
        el('h2', {}, 'Channels'),
        el('span', { class: 'spacer' }),
        filterBar,
      ]),
      el('div', { class: 'card-body flush' },
        visible.length
          ? visible.map(([name, ch]) => qualityChannelRow(name, ch))
          : emptyState('No channels match', 'Change the filter to see the evaluated channels.')),
    ]);
  }

  function qualityChannelRow(name, ch) {
    const issues = ch.issues || [];
    const value =
      ch.value_kwh !== null && ch.value_kwh !== undefined
        ? kWh(ch.value_kwh)
        : ch.value_w !== null && ch.value_w !== undefined
          ? watts(ch.value_w)
          : '—';
    return el('div', { class: 'qchannel' }, [
      el('div', { class: 'qc-head' }, [
        el('span', { class: 'qc-name' }, name),
        statusBadge(ch.status),
        ch.domain ? badge(String(ch.domain), 'mute') : null,
        el('span', { class: 'spacer' }),
        el('span', { class: 'stat-hint' }, value),
        el('span', { style: 'min-width:52px;text-align:right;font-variant-numeric:tabular-nums;font-weight:600' },
          percent(ch.score, 0)),
      ]),
      issues.length
        ? el('div', { class: 'qc-detail' }, issues.join(' · '))
        : null,
      ch.repeated ? el('div', { class: 'qc-detail' }, 'Repeated value: not counted as a new measurement.') : null,
    ]);
  }

  /* --- Diagnostics ------------------------------------------------------ */

  async function renderDiagnostics(root, signal) {
    const report = await get('/diagnostics', signal);
    if (signal.aborted) return;

    root.append(
      el('div', { class: 'page-head' }, [
        el('div', {}, [
          el('h1', {}, 'Diagnostics'),
          el('p', { class: 'lede' },
            'A redacted support bundle: what the engine is doing, what it rejected, and why. Safe to attach to an issue.'),
        ]),
        el('div', { class: 'head-actions' }, [
          el('button', { class: 'btn', type: 'button', onclick: () => downloadDiagnostics(report) }, 'Download JSON'),
          el('button', { class: 'btn btn-ghost', type: 'button', onclick: () => copyDiagnostics(report) }, 'Copy'),
        ]),
      ])
    );

    const health = report.engine_health || {};
    const db = report.database || {};
    const ha = report.home_assistant || {};
    const runtime = report.runtime || {};
    const models = report.models || {};

    root.append(
      el('div', { class: 'grid cols-2', style: 'margin-bottom:16px' }, [
        card('Engine', [
          kv([
            ['Status', statusBadge(health.status)],
            ['Version', health.engine_version || '—'],
            ['Contract', health.contract_version || report.contract_version || '—'],
            ['Uptime', duration(health.uptime_seconds)],
            ['Learning', health.learning_active ? badge('Active', 'ok', { dot: true }) : badge('Paused', 'mute', { dot: true })],
            ['Last inference', ago(health.inference_last_run_at)],
            ['Last successful cycle', ago(report.last_successful_processing_at)],
            ['Generated', ago(report.generated_at)],
            ['Report OK', report.ok ? badge('Yes', 'ok') : badge('No', 'bad')],
          ]),
        ]),
        card('Home Assistant and storage', [
          kv([
            ['HA connected', ha.connected ? badge('Connected', 'ok', { dot: true }) : badge('Disconnected', 'warn', { dot: true })],
            ['HA state', label(ha.state)],
            ['Last event', ago(ha.last_event_at)],
            ['Reconnect attempt', String(ha.reconnect_attempt ?? '—')],
            ['Database', !db.available
              ? badge('Not configured', 'mute')
              : db.healthy === true
                ? badge('Healthy', 'ok')
                : db.healthy === false
                  ? badge('Unhealthy', 'bad')
                  : badge('Unknown', 'mute')],
            ['Schema current', db.schema_current ? 'Yes' : 'No'],
            ['Models learned', String(models.count ?? 0)],
          ]),
        ]),
      ])
    );

    root.append(modelsTable(models));
    root.append(channelHealthCard(report.channels || {}, report.aligner || {}));
    root.append(errorsCard(report.errors || {}));

    root.append(
      detailsPanel('Configuration (redacted)', [
        el('pre', { class: 'code' }, JSON.stringify(report.configuration || {}, null, 2)),
      ])
    );
    root.append(
      detailsPanel('Runtime counters', [
        el('pre', { class: 'code' }, JSON.stringify(runtime, null, 2)),
      ])
    );
    root.append(
      detailsPanel('Full support bundle', [
        el('p', { class: 'stat-hint', style: 'margin-bottom:10px' },
          'The complete response from /api/v1/diagnostics, already redacted by the engine.'),
        el('pre', { class: 'code' }, JSON.stringify(report, null, 2)),
      ])
    );
  }

  function modelsTable(models) {
    const devices = models.devices || {};
    const rows = Object.entries(devices).map(([id, model]) =>
      el('tr', {}, [
        el('td', {}, [el('a', { href: `#/device/${encodeURIComponent(id)}` }, id)]),
        el('td', {}, titleCase(model.model_type || '—')),
        el('td', { class: 'num' }, String(model.version ?? '—')),
        el('td', {}, statusBadge(model.status)),
        el('td', { class: 'num' }, String(model.sample_count ?? '—')),
        el('td', { class: 'num' }, model.confidence === null || model.confidence === undefined ? '—' : percent(model.confidence, 0)),
      ])
    );
    return card(
      'Learned models',
      [
        rows.length
          ? el('div', { class: 'table-wrap' }, [
              el('table', { class: 'data' }, [
                el('thead', {}, el('tr', {}, [
                  el('th', {}, 'Device'),
                  el('th', {}, 'Type'),
                  el('th', { class: 'num' }, 'Version'),
                  el('th', {}, 'Status'),
                  el('th', { class: 'num' }, 'Samples'),
                  el('th', { class: 'num' }, 'Confidence'),
                ])),
                el('tbody', {}, rows),
              ]),
            ])
          : el('p', { class: 'stat-hint' }, 'No model has been learned yet. Learning needs repeated, validated cycles.'),
      ],
      { flush: true }
    );
  }

  function channelHealthCard(channels, aligner) {
    const entries = Object.entries(channels);
    return card(
      'Channel health',
      [
        el('p', { class: 'stat-hint', style: 'margin-bottom:10px' },
          `${aligner.channels ?? entries.length} channels tracked in the aligner`
          + (aligner.samples !== undefined ? ` · ${aligner.samples} samples buffered.` : '.')),
        entries.length
          ? el('div', { class: 'table-wrap' }, [
              el('table', { class: 'data' }, [
                el('thead', {}, el('tr', {}, [
                  el('th', {}, 'Channel'),
                  el('th', { class: 'num' }, 'Samples'),
                  el('th', { class: 'num' }, 'Median interval'),
                  el('th', {}, 'Last update'),
                  el('th', {}, 'Quality'),
                ])),
                el('tbody', {},
                  entries.slice(0, 40).map(([name, info]) => {
                    const data = info && typeof info === 'object' ? info : {};
                    // The aligner reports ``quality`` / ``last_received_at`` and
                    // ``last_update_age_s``; there is no ``status`` or
                    // ``last_seen`` field, and inventing one showed a table of
                    // em dashes on every installation.
                    const quality = data.quality || (data.missing ? 'missing' : data.stale ? 'stale' : null);
                    const age = data.last_update_age_s;
                    return el('tr', {}, [
                      el('td', {}, name),
                      el('td', { class: 'num' }, String(data.sample_count ?? '—')),
                      el('td', { class: 'num' }, data.interval_median_s === null || data.interval_median_s === undefined ? '—' : `${num(data.interval_median_s, 1)} s`),
                      el('td', {}, age === null || age === undefined ? ago(data.last_received_at) : `${duration(age)} ago`),
                      el('td', {}, quality ? statusBadge(quality) : '—'),
                    ]);
                  })),
              ]),
            ])
          : el('p', { class: 'stat-hint' }, 'The aligner reported no channels yet.'),
      ],
      { flush: true }
    );
  }

  function errorsCard(errors) {
    const system = errors.recent_system_events || [];
    const quality = errors.recent_quality_events || [];
    const rows = [...system.map((row) => ({ ...row, kind: 'system' })), ...quality.map((row) => ({ ...row, kind: 'quality' }))]
      .sort((a, b) => String(b.timestamp || b.created_at || '').localeCompare(String(a.timestamp || a.created_at || '')))
      .slice(0, 25);

    return card(
      'Recent events',
      [
        rows.length
          ? el('div', { class: 'log-list' }, rows.map((row) => {
              const level = String(row.level || row.severity || 'info').toLowerCase();
              // System events carry ``category``; quality events carry ``code``
              // and ``channel_id``.
              const tag = row.category || row.code || row.kind;
              const where = row.channel_id ? ` (${row.channel_id})` : '';
              return el('div', { class: 'log-row' }, [
                el('span', { class: 'ts' }, ago(row.timestamp || row.created_at)),
                el('span', { class: 'lvl', dataset: { l: level } }, level.toUpperCase()),
                el('span', { class: 'msg' }, `${tag ? `[${tag}] ` : ''}${row.message || row.detail || JSON.stringify(row)}${where}`),
              ]);
            }))
          : el('p', { class: 'stat-hint' }, 'No recent system or quality events were recorded.'),
      ],
      { foot: 'Newest first, redacted by the engine before it leaves the process.' }
    );
  }

  function detailsPanel(title, body) {
    return el('details', { class: 'panel' }, [
      el('summary', {}, title),
      el('div', { class: 'panel-body' }, body),
    ]);
  }

  async function downloadDiagnostics(report) {
    const blob = new Blob([JSON.stringify(report, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const link = el('a', { href: url, download: 'wattmosaic-diagnostics.json' });
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
  }

  async function copyDiagnostics(report) {
    try {
      await navigator.clipboard.writeText(JSON.stringify(report, null, 2));
      notify('Diagnostics copied', 'The redacted bundle is on the clipboard.');
    } catch (err) {
      notify('Copy failed', describeError(err), 'bad');
    }
  }

  /* ---------------------------------------------------------------------- *
   * Polling
   * ---------------------------------------------------------------------- */

  async function pollTick() {
    if (document.hidden) return;
    try {
      const health = await get('/health');
      state.lastHealth = health;
      setConnection(health);
      setFooter(health);
      hideBanner();
      if (state.view === 'overview') {
        // Refresh the data cards without tearing down the DOM (keeps focus and
        // scroll position stable while values tick).
        const { status, snapshot, devices } = await fetchOverview();
        state.lastSnapshot = snapshot;
        state.lastDevices = devices;
        updateOverviewLive(health, status, snapshot, devices);
      }
    } catch (err) {
      setConnection(null, err);
      if (state.view === 'overview') {
        setBanner('Engine unreachable', describeError(err), 'bad');
      }
    }
  }

  function updateOverviewLive(health, status, snapshot, devices) {
    // A live update must never re-run the whole view: only values change.
    const root = viewRoot();
    if (!root || state.view !== 'overview') return;
    const recon = (snapshot && snapshot.reconciliation) || {};

    const total = root.querySelector('.hero-total .watts');
    if (total) total.textContent = num(recon.main_power_w, 0);

    const statValues = root.querySelectorAll('.stat .stat-value');
    const stats = root.querySelectorAll('.stat');
    if (stats.length >= 5) {
      const set = (index, value) => {
        const node = statValues[index];
        if (!node) return;
        const unit = node.querySelector('.unit');
        node.textContent = value;
        if (unit) node.append(unit);
      };
      set(0, num(recon.main_power_w, 0));
      const accounted = recon.accounted_fraction;
      set(1, accounted === undefined ? '—' : percent(accounted, 0));
      set(2, num(recon.unknown_residual_w, 0));
      set(3, label(health.status));
      const score =
        (status && status.quality && status.quality.overall_score) ??
        (snapshot && snapshot.quality && snapshot.quality.overall_score);
      set(4, score === undefined ? '—' : percent(score, 0));
    }
  }

  function startPolling() {
    stopPolling();
    state.pollTimer = setInterval(pollTick, 8000);
  }

  function stopPolling() {
    if (state.pollTimer) {
      clearInterval(state.pollTimer);
      state.pollTimer = null;
    }
  }

  /* ---------------------------------------------------------------------- *
   * Wiring
   * ---------------------------------------------------------------------- */

  function wire() {
    window.addEventListener('hashchange', () => {
      applyRoute();
      if (state.view === 'overview') startPolling();
      else stopPolling();
    });

    document.getElementById('refresh')?.addEventListener('click', () => {
      render();
      notify('Refreshed', 'Pulled the latest values from the engine.', 'ok');
    });

    document.getElementById('banner-retry')?.addEventListener('click', () => {
      hideBanner();
      render();
    });

    document.addEventListener('visibilitychange', () => {
      if (!document.hidden && state.view === 'overview') pollTick();
    });

    // Escape returns to the section list from a detail page.
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && state.view === 'device') {
        location.hash = '#/devices';
      }
    });
  }

  function boot() {
    wire();
    applyRoute();
    // The overview is the only view that consumes the poll.  Starting it
    // unconditionally meant a cold load straight onto #/topology kept an 8 s
    // /health poll running behind a slow request, competing for the same
    // connection and delaying the first paint.
    if (state.view === 'overview') startPolling();
    else stopPolling();
    pollTick();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot, { once: true });
  } else {
    boot();
  }

  // Export a couple of helpers for debugging from the browser console without
  // exposing engine data through any other surface.
  window.WattMosaicUI = Object.freeze({ reload: () => render(), route: parseHash });
})();
