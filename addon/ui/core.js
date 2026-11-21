/* ==========================================================================
   WattMosaic Engine panel — core.js
   --------------------------------------------------------------------------
   Native ES module; relative imports only so the same file works at "/" and
   under /api/hassio_ingress/<token>/.  No build step, no network beyond origin.
   ========================================================================== */

import { slots } from './slots.js';

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

//: Under Home Assistant ingress the panel lives at
//: ``/api/hassio_ingress/<token>/``.  That token's session is a 15-minute
//: sliding window held by the Supervisor, renewed on *every* request it
//: proxies.  When the tab is hidden -- or a page sits open overnight -- no
//: request is made, nothing renews the session, and the first request after
//: the window answers 401.  Only the Supervisor can mint a new session, and it
//: does so when the top-level document is reloaded; a script cannot renew it.
const INGRESS = BASE.startsWith('/api/hassio_ingress/');

function clearSessionReloadMark() {
  try {
    sessionStorage.removeItem('wm-session-reload');
  } catch {
    /* sessionStorage unavailable in private mode; nothing to clear */
  }
}

function reloadForSession() {
  // A reload should happen at most once per expiry.  If the fresh document
  // still answers 401, reloading again only flickers the page -- so record the
  // attempt and surface the error instead.  The mark is cleared by the first
  // successful call, so a *later* expiry still recovers.
  try {
    if (sessionStorage.getItem('wm-session-reload') === '1') {
      clearSessionReloadMark();
      throw new ApiError('The Home Assistant session expired.', {
        status: 401,
        code: 'session_expired',
      });
    }
    sessionStorage.setItem('wm-session-reload', '1');
  } catch (err) {
    if (err instanceof ApiError) throw err;
    // sessionStorage can be unavailable (private mode); reload anyway.
  }
  // ``replace`` avoids stacking an unusable entry in history, and keeping the
  // hash means the user lands back on the view they were reading.
  location.replace(location.href);
}

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

  // A 401 under ingress is normally the Supervisor's expired session: the
  // panel never sends an API token (the gateway trusts the ingress peer), so
  // it cannot be a bad token from this side.  Reloading is the only way to
  // obtain a fresh session -- but the *gateway's* own 401 (a misconfigured
  // WM_API_TOKEN) answers JSON, and reloading cannot fix that.  Distinguish
  // them so a bad token does not put the panel in a reload loop.
  if (response.status === 401 && INGRESS && !(payload && payload.error)) {
    reloadForSession();
    throw new ApiError('The Home Assistant session expired; reloading.', {
      status: 401,
      code: 'session_expired',
    });
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
  // The session works, so any earlier expiry mark is spent; a later expiry
  // must still be able to reload once.
  if (INGRESS) clearSessionReloadMark();
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
  shared: 'Shared CT',
  // Legacy CT role, retired by ADR-0029.  A stored value can still reach the
  // panel, and without this entry ``titleCase`` would render it as a
  // different, wrong label.  It is an alias, not a role.
  dedicated_device: 'Branch CT',
  home_total_only: 'Home total only',
  unmonitored: 'Unmonitored',
  l1: 'L1',
  l2: 'L2',
  both: 'Both legs',
  // Additive labels for the derived mapping matrix and group ranges.  A status
  // the engine invents still falls through ``titleCase`` rather than rendering
  // blank, so this table is a readability improvement, not an allowlist.
  non_identifiable: 'Non-identifiable',
  valid: 'Valid',
  invalid: 'Invalid',
  learning: 'Learning',
  provisional: 'Provisional',
  conflicted: 'Conflicted',
  direct: 'Direct',
  derived: 'Derived',
  indirect: 'Indirect',
  one_to_one: 'One-to-one',
  one_to_many: 'One-to-many',
  many_to_one: 'Many-to-one',
  many_to_many: 'Many-to-many',
  // Additive labels for version history, suspected changes and event evidence.
  // A value the engine invents still falls through ``titleCase``; these are
  // readability improvements, not an allowlist.  SUSPECTED is deliberately
  // distinct from CONFIRMED and must never read as established.
  suspected: 'Suspected',
  pending: 'Pending',
  active: 'Active',
  frozen: 'Frozen',
  rolled_back: 'Rolled back',
  isolated: 'Isolated',
  contaminated: 'Contaminated',
  insufficient_data: 'Insufficient data',
  // Additive source-ledger vocabulary for the matrix diagnostic view.  The
  // engine publishes these ids verbatim; these labels only make them readable
  // and an unknown id still falls through ``titleCase``.  Reasons stay raw.
  ha_state: 'HA state',
  ha_attributes: 'HA attributes',
  smart_plug_measurement: 'Smart plug measurement',
  ct_measurement: 'CT measurement',
  residual_events: 'Residual events',
  learned_models: 'Learned models',
  // Dynamic-topology messaging (OD-51).  The current topology is always
  // authoritative for inference and learning; candidate/unconfirmed states
  // publish a reduced confidence rather than gating.  These read as status,
  // never as a block or a required confirmation.
  topology_dynamic: 'Dynamic topology',
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
 * Discovery lookback windows
 * ---------------------------------------------------------------------- */

//: The windows the Discover card offers, in hours, with the label a person
//: reads.  The engine accepts any value up to 2160 h, but these four are the
//: ones with a known evidence/cost profile: on production recorder history
//: 24 h found 0 groups on light CTs, 48 h found 10, and 168 h found 60.
//: A longer window is strictly better evidence and strictly slower, so the
//: choice is the user's, not a silent default.
const DISCOVER_WINDOWS = Object.freeze([
  { hours: 24, label: '24 h', name: null },
  { hours: 48, label: '48 h', name: null },
  { hours: 72, label: '72 h', name: null },
  { hours: 168, label: '7 days (168 h)', name: '7 days' },
]);

//: A fast first run.  A week of history is the useful window for shared
//: light circuits, but it costs minutes, so it must be an explicit choice.
const DISCOVER_DEFAULT_HOURS = 24;

//: From this window on, a real recorder took minutes (168 h measured 155 s
//: on production).  The loading copy must say so rather than imply a pause.
const DISCOVER_LONG_HOURS = 72;

const discoverWindow = (hours) => {
  const value = Number(hours);
  return DISCOVER_WINDOWS.find((option) => option.hours === value) || null;
};

const discoverWindowName = (hours) => {
  const option = discoverWindow(hours);
  return option ? option.name : null;
};

const discoverWindowLabel = (hours) => {
  const option = discoverWindow(hours);
  return option ? option.label : `${num(hours, 0)} h`;
};

const discoverLongRun = (hours) => {
  const value = Number(hours);
  return Number.isFinite(value) && value >= DISCOVER_LONG_HOURS;
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
  // Matrix debug mode.  "debug" turns on the tracing aids (raw ids, change
  // trace, hierarchy breadcrumbs); "filter" narrows the matrix to matching
  // device/reader/circuit labels.  Both survive a re-render/poll like the
  // hierarchy filter does, so typing in the box never resets on refresh.
  matrix: {
    debug: false,
    showRawIds: false,
    filter: '',
    lastSignature: null,   // signature of the last-rendered matrix payload
    lastChangedAt: null,   // Date.now() of the last observed matrix change
    changeCount: 0,        // how many distinct matrices observed this session
    trace: [],             // newest-first [{at, summary}] of recent changes
  },
  // The hierarchy view's own state lives here, not in the DOM, so a re-render
  // (a poll, a refresh, a route change) does not reset what the user opened.
  tree: {
    open: new Set(),      // node ids the user explicitly expanded
    closed: new Set(),    // node ids the user explicitly collapsed
    filter: '',
  },
  // Placement discovery is explicit: it only runs when the user asks, so its
  // result is kept here and survives a repaint of the same tab.
  discover: {
    data: null,
    running: false,
    error: null,
    //: The window the user selected.  Selecting a window changes this only;
    //: it deliberately does *not* issue a request.  A long window costs
    //: minutes, so a stray click on the selector must never start one -- the
    //: choice takes effect when the user presses Discover.
    window: DISCOVER_DEFAULT_HOURS,
    //: The window the request that produced ``data``/``error`` actually read.
    //: It is pinned when the request starts, so changing the selector after
    //: a run cannot make Apply write a window the card never showed.
    ranWindow: null,
  },
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
 * Tier vocabulary
 * ---------------------------------------------------------------------- */

// Contract 3.0 removed GET /confidence_tiers (interfaces.md §9, "Removed at
// Z1") and has no replacement route: confidence is a vector now.  The legacy
// tier labels are the panel's static ``TIER_ORDER``/``tierInfo`` vocabulary,
// so this resolves without a request and the callers keep their shape.
async function loadTiers() {
  if (!state.tiers) state.tiers = { tiers: [], calibrated: false };
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
  const known = ['overview', 'devices', 'topology', 'quality', 'diagnostics', 'matrix'];
  if (known.includes(section)) return { view: section, deviceId: null };
  return { view: 'overview', deviceId: null };
}

function applyRoute() {
  const route = parseHash();
  // Cancel any in-flight discovery poll: its cached generation no longer
  // matches, so it will not issue another status GET after this navigation.
  slots.discoveryGeneration += 1;
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
  const [health, status, snapshot, devices, residuals] = await Promise.all([
    get('/health', signal),
    get('/status', signal).catch(() => null),
    get('/snapshot', signal).catch(() => null),
    get('/devices', signal).catch(() => []),
    // Additive derived data: an engine that predates this route still shows
    // the overview, just without the new cards.  (The legacy flat mapping
    // route was removed in 3.0; the Matrix tab reads /matrix.)
    get('/residuals', signal).catch(() => null),
  ]);
  return { health, status, snapshot, devices, residuals };
}

// The 8s live poll only feeds updateOverviewLive(), which reads health,
// status, snapshot and devices.  /residuals is expensive and its result was
// discarded on every tick, so the timer never fetches it.
async function fetchOverviewLive(signal) {
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
      await slots.renderTopology(root, signal);
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
          await slots.renderDevices(root, signal);
          break;
        case 'device':
          await slots.renderDevice(root, signal);
          break;
        case 'quality':
          await slots.renderQuality(root, signal);
          break;
        case 'diagnostics':
          await slots.renderDiagnostics(root, signal);
          break;
        case 'matrix':
          await slots.renderMatrix(root, signal);
          break;
        default:
          await slots.renderOverview(root, signal);
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

/* ---------------------------------------------------------------------- *
 * Polling
 * ---------------------------------------------------------------------- */

async function pollTick() {
  if (document.hidden) return;
  try {
    if (state.view === 'overview') {
      // Refresh only the values the live updater reads.  The poll used to
      // call fetchOverview(), which also fetched /residuals and /mapping and
      // then discarded both; the lightweight fetch skips those expensive
      // endpoints on every 8s tick.
      const { health, status, snapshot, devices } = await fetchOverviewLive();
      state.lastHealth = health;
      setConnection(health);
      setFooter(health);
      hideBanner();
      state.lastSnapshot = snapshot;
      state.lastDevices = devices;
      updateOverviewLive(health, status, snapshot, devices);
    } else {
      // Non-overview views still probe /health on the timer so the ingress
      // session stays alive and a 401 is noticed promptly.
      const health = await get('/health');
      state.lastHealth = health;
      setConnection(health);
      setFooter(health);
      hideBanner();
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

export {
  BASE,
  API_PREFIX,
  INGRESS,
  clearSessionReloadMark,
  reloadForSession,
  ApiError,
  api,
  get,
  post,
  el,
  frag,
  clear,
  num,
  watts,
  kWh,
  percent,
  duration,
  clock,
  ago,
  titleCase,
  STRINGS,
  label,
  toneFor,
  sourceTone,
  SOURCE_SWATCH,
  badge,
  statusBadge,
  TIER_ORDER,
  tierInfo,
  tierBadge,
  statCard,
  card,
  kv,
  notice,
  emptyState,
  errorState,
  loadingCards,
  describeError,
  toasts,
  notify,
  DISCOVER_WINDOWS,
  DISCOVER_DEFAULT_HOURS,
  DISCOVER_LONG_HOURS,
  discoverWindow,
  discoverWindowName,
  discoverWindowLabel,
  discoverLongRun,
  state,
  viewRoot,
  setConnection,
  setBanner,
  hideBanner,
  setFooter,
  loadTiers,
  tierDescription,
  parseHash,
  applyRoute,
  fetchOverview,
  fetchOverviewLive,
  fetchDevice,
  render,
  pollTick,
  updateOverviewLive,
  startPolling,
  stopPolling
};
