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

  const badge = (text, tone = 'mute', { dot = false } = {}) =>
    el('span', { class: 'badge', dataset: { tone } }, [
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
    topologyTab: 'channels',
    qualityFilter: 'all',
  };

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

    clear(root);
    root.append(loadingCards(4));
    root.setAttribute('aria-busy', 'true');

    try {
      await loadTiers();
      if (signal.aborted) return;
      // The skeleton is provisional: every view builds its own page head, so it
      // must be removed before the first view node is appended.  Leaving it in
      // place used to push a stale row of empty cards above the real content.
      clear(root);
      switch (state.view) {
        case 'devices':
          await renderDevices(root, signal);
          break;
        case 'device':
          await renderDevice(root, signal);
          break;
        case 'topology':
          await renderTopology(root, signal);
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

  async function renderTopology(root, signal) {
    const [topology, nesting] = await Promise.all([
      get('/topology', signal).catch((err) => {
        if (err && err.status === 404) return null;
        throw err;
      }),
      get('/topology/nesting?hours=12', signal).catch(() => null),
    ]);
    if (signal.aborted) return;

    root.append(
      el('div', { class: 'page-head' }, [
        el('div', {}, [
          el('h1', {}, 'Topology'),
          el('p', { class: 'lede' },
            'The electrical model the engine solves against: CT roles, phases, containment and the devices they feed.'),
        ]),
        el('div', { class: 'head-actions' }, [
          topology && topology.status !== 'confirmed'
            ? el('button', { class: 'btn btn-primary', type: 'button', onclick: confirmTopology }, 'Confirm topology')
            : null,
        ]),
      ])
    );

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

    root.append(topologySummary(topology, nesting));
    root.append(
      el('div', { class: 'topo-grid' }, [
        topologyChannelsCard(topology),
        el('div', { style: 'display:flex;flex-direction:column;gap:16px' }, [
          topologyTreeCard(topology),
          topologyDevicesCard(topology),
        ]),
      ])
    );
    root.append(topologyCandidatesCard(nesting));
    if ((topology.notes || []).length) {
      root.append(
        card(
          'Engine notes',
          el('ul', { class: 'explain' },
            topology.notes.map((note) =>
              el('li', {}, [el('span', { class: 'marker' }, '·'), el('span', {}, note)])))
        )
      );
    }
  }

  function topologySummary(topology, nesting) {
    const counts = { main: 0, branch: 0, dedicated_device: 0, unknown: 0 };
    for (const ch of topology.channels || []) {
      counts[ch.ct_type] = (counts[ch.ct_type] || 0) + 1;
    }
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
      el('div', { class: 'grid cols-4', style: 'margin-bottom:16px' }, [
        statCard('Version', String(topology.version ?? '—'), { hint: ago(topology.created_at) }),
        statCard('Status', label(topology.status), { tone: toneFor(topology.status) }),
        statCard('Channels', String((topology.channels || []).length), {
          hint: `${counts.main} main · ${counts.branch} branch · ${counts.dedicated_device} dedicated`,
        }),
        statCard('Devices', String((topology.devices || []).length), {
          hint: nesting && nesting.instants ? `${nesting.instants} comparison instants` : '',
        }),
      ]),
    ]);
  }

  function topologyChannelsCard(topology) {
    const channels = [...(topology.channels || [])].sort((a, b) => {
      const order = { main: 0, branch: 1, dedicated_device: 2, unknown: 3 };
      return (order[a.ct_type] ?? 9) - (order[b.ct_type] ?? 9) || String(a.ct_id).localeCompare(String(b.ct_id));
    });
    return el('section', { class: 'card' }, [
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
                el('div', { style: 'min-width:0' }, [
                  el('div', { class: 'ch-name' }, ch.name || ch.ct_id),
                  el('div', { class: 'ch-sub' }, ch.entity_id || ch.ct_id),
                  ch.parent_ct_id
                    ? el('div', { class: 'ch-sub' }, `parent: ${ch.parent_ct_id}`)
                    : null,
                ]),
                el('div', { class: 'ch-tags' }, [
                  badge(label(ch.ct_type), ch.ct_type === 'main' ? 'accent' : ch.ct_type === 'branch' ? 'info' : ch.ct_type === 'dedicated_device' ? 'ok' : 'mute'),
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

  function topologyTreeCard(topology) {
    const channels = topology.channels || [];
    const byId = new Map(channels.map((c) => [c.ct_id, c]));
    const children = new Map();
    const roots = [];
    for (const ch of channels) {
      if (ch.parent_ct_id && byId.has(ch.parent_ct_id)) {
        if (!children.has(ch.parent_ct_id)) children.set(ch.parent_ct_id, []);
        children.get(ch.parent_ct_id).push(ch);
      } else {
        roots.push(ch);
      }
    }
    const seen = new Set();
    const renderNode = (ch, depth) => {
      if (seen.has(ch.ct_id)) return null;
      seen.add(ch.ct_id);
      const kids = (children.get(ch.ct_id) || []).sort((a, b) => String(a.ct_id).localeCompare(String(b.ct_id)));
      const node = el('div', { class: `node depth-${Math.min(4, depth)}` }, [
        el('span', { class: 'arc', 'aria-hidden': 'true' }, depth === 0 ? '▸' : '└'),
        el('span', { class: 'nm' }, ch.name || ch.ct_id),
        el('span', { class: 'stat-hint' }, `(${label(ch.ct_type)})`),
      ]);
      return frag([node, ...kids.map((kid) => renderNode(kid, depth + 1))]);
    };
    const tree = el(
      'div',
      { class: 'topo-tree' },
      roots.length
        ? roots.sort((a, b) => String(a.ct_id).localeCompare(String(b.ct_id))).map((ch) => renderNode(ch, 0))
        : channels.map((ch) => renderNode(ch, 0))
    );
    return card('Containment', [tree], {
      foot: 'A CT may sit under any other CT. Nested totals are only authoritative once confirmed.',
    });
  }

  function topologyDevicesCard(topology) {
    const devices = topology.devices || [];
    return card(
      'Devices',
      [
        devices.length
          ? el('div', { class: 'table-wrap' }, [
              el('table', { class: 'data' }, [
                el('thead', {}, el('tr', {}, [
                  el('th', {}, 'Device'),
                  el('th', {}, 'Phase'),
                  el('th', {}, 'Dedicated CTs'),
                  el('th', {}, 'Branch CTs'),
                ])),
                el('tbody', {},
                  devices.map((d) =>
                    el('tr', {}, [
                      el('td', {}, [
                        el('a', { href: `#/device/${encodeURIComponent(d.device_id)}` }, d.name || d.device_id),
                        el('div', { class: 'ch-sub' }, d.device_id),
                      ]),
                      el('td', {}, label(d.phase)),
                      el('td', {}, (d.dedicated_ct_ids || []).join(', ') || '—'),
                      el('td', {}, (d.branch_ct_ids || []).join(', ') || '—'),
                    ])
                  )),
              ]),
            ])
          : emptyState('No devices', 'This topology has no devices.'),
      ],
      { flush: true }
    );
  }

  function topologyCandidatesCard(nesting) {
    if (!nesting) return frag([]);
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
          el('div', { class: 'banner-icon' }, '!'),
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
      foot: nesting.instants ? `Compared over ${nesting.instants} instants across ${nesting.channels_compared || 0} channels.` : null,
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
    startPolling();
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
