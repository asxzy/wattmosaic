/* ==========================================================================
   WattMosaic Engine panel — view-matrix.js
   --------------------------------------------------------------------------
   Native ES module; relative imports only so the same file works at "/" and
   under /api/hassio_ingress/<token>/.  No build step, no network beyond origin.

   U1 — the matrix is now a primary, user-facing view (HB §19).  It renders the
   K05 v3 MatrixPayload from ``GET /matrix`` (relative URL, like every other
   panel call: core.js resolves it under /api/v1).  Until M3 lands the panel
   builds against the K05 fixture; when /matrix is unavailable it renders an
   explicit "matrix API unavailable" state and NEVER falls back to the legacy
   /mapping payload.  A cheap poll of /status.mapping_version_id triggers a
   refetch only when the version changes.
   ========================================================================== */

import { card, el, emptyState, get, label, render, statCard, state, watts } from './core.js';
import { applyFilters, cellView, leafReaders, matrixObserveVersion, matrixContext, matrixObserveChange, rowTree } from './matrix-model.js';
import { matrixApplyFilter, matrixCellsCard, matrixCompactCell, matrixHeaderRowEls, matrixHeaderTable, matrixLegend, matrixPivotCard, matrixTraceCard } from './matrix-cell.js';
import { matrixDrillDrawer, matrixIdentifiabilityCard, matrixReaderDrill, matrixRowDrill, matrixSourceLedgerCard } from './matrix-drill.js';

// The compact default hides the debug trace, raw ids, source ledger and
// identifiability cards.  They move behind the "Diagnostic" toggle (DEC-29).
function _matrixIsUnavailable() {
  return emptyState(
    'Matrix API unavailable',
    'The engine has not published a v3 matrix yet. This view needs GET /api/v1/matrix; it will not show the legacy /mapping payload.'
  );
}

// Fetch the v3 matrix payload.  A 404 (or any error) returns null so the caller
// renders the explicit unavailable state — never the legacy /mapping shape.
async function fetchMatrix(signal) {
  try {
    return await get('/matrix', signal);
  } catch {
    return null;
  }
}

// Render one matrix body cell from a v3 cell, wired to the cell drill drawer.
function _v3Cell(cell, onDrill) {
  const view = cellView(cell);
  const btn = el('button', {
    class: `matrix-cell-btn matrix-cat-${view.category}`,
    type: 'button',
    dataset: { rowId: cell.row_id || '', readerId: cell.reader_id || '', category: view.category },
    onclick: () => onDrill && onDrill(cell),
  }, [
    matrixCompactCell(cell),
  ]);
  return el('td', { class: 'matrix-grid-cell', dataset: { category: view.category } }, btn);
}

// A (row, reader) pair with no cell.  Cells are sparse by design (design §8):
// no mapping edge, no EXPECTED ancestor and no match history means this reader
// does not observe the row, so the pair renders as not observable — a quiet
// dot rather than a dash that reads like missing data.
function _absentCell(rowLabel, readerLabel) {
  return el('td', {
    class: 'matrix-grid-cell matrix-grid-empty',
    title: `${rowLabel} · ${readerLabel}: not observable (no mapping, expected path or match history)`,
  }, '·');
}

// Per-reader total rows, from the engine's ``reader_totals`` (HB §19.4).  The
// residual is reported per reader, never distributed; totals are never summed
// across readers.  These replace the one-residual-row-per-reader block.
const TOTAL_ROWS = [
  { key: 'measured_w', label: 'Measured', place: 'top',
    hint: 'What each reader measures now' },
  { key: 'known_contrib_w', label: 'Known contributions', place: 'bottom',
    hint: 'Sum of the published device contributions on each reader' },
  { key: 'unknown_w', label: 'Unknown loads', place: 'bottom', optional: true,
    hint: 'Unknown-load profiles active on each reader' },
  { key: 'residual_w', label: 'Unexplained residual', place: 'bottom',
    hint: 'Measured minus known and unknown, per reader — reported, never distributed' },
];

function _staleReaders(data) {
  const out = new Map();
  (Array.isArray(data.stale_sources) ? data.stale_sources : []).forEach((entry) => {
    const text = String(entry || '');
    const idx = text.indexOf(':');
    const reader = (idx >= 0 ? text.slice(0, idx) : text).trim();
    if (reader) out.set(reader, idx >= 0 ? text.slice(idx + 1).trim() : 'stale');
  });
  return out;
}

function _totalsRow(spec, leaves, totals, stale) {
  const tds = leaves.map((leaf) => {
    const t = totals[leaf.readerId] || {};
    const value = t[spec.key];
    if ((value === null || value === undefined) && stale.has(leaf.readerId)) {
      return el('td', {
        class: 'matrix-grid-total matrix-grid-stale',
        title: `${leaf.label}: ${stale.get(leaf.readerId)}`,
      }, 'stale');
    }
    return el('td', {
      class: 'matrix-grid-total',
      title: `${spec.label} · ${leaf.label}`,
    }, value === null || value === undefined ? '—' : watts(value));
  });
  return el('tr', { class: `matrix-row-total matrix-row-total-${spec.place}`, dataset: { rowKind: 'total', total: spec.key } }, [
    el('th', { class: 'matrix-grid-identity matrix-row-total-label', scope: 'row', title: spec.hint }, spec.label),
    el('td', { class: 'matrix-grid-now' }, ''),
    ...tds,
  ]);
}

// The row's current value, from the published snapshot (``/devices`` for
// devices and components, ``group_estimates`` for groups).  A non-identifiable
// subject shows its interval, never an invented number (DEC-19).
function _nowCell(node, ctx) {
  const est = node.kind === 'group' ? ctx.groupById.get(node.rowId) : ctx.nowById.get(node.rowId);
  if (!est) {
    return el('td', { class: 'matrix-grid-now matrix-grid-now-none', title: 'No published estimate' }, '—');
  }
  const point = est.point_w;
  const bounds = est.bounds || {};
  const status = est.status ? label(String(est.status)) : (est.identifiability ? label(String(est.identifiability)) : '');
  let text = '—';
  if (point !== null && point !== undefined) text = watts(point);
  else if (bounds.upper_w !== null && bounds.upper_w !== undefined) {
    text = `${watts(bounds.lower_w === null || bounds.lower_w === undefined ? 0 : bounds.lower_w)} – ${watts(bounds.upper_w)}`;
  }
  return el('td', { class: 'matrix-grid-now', title: status ? `Now: ${status}` : 'Now' }, [
    el('span', { class: 'matrix-now-value' }, text),
    status ? el('span', { class: 'matrix-now-status' }, status) : null,
  ]);
}

const _KIND_TAG = {
  component: 'component',
  group: 'group',
  unknown_profile: 'unknown load',
};

// Render the v3 matrix grid: one <thead> holding the L0–L4 levels plus the
// frozen identity and "Now" columns, then the row tree (rooms → devices →
// components; groups collapsed), the per-reader total rows and the legend.
function matrixGridCard(payload, onDrill, context) {
  const data = payload || {};
  const ctx = {
    nowById: (context && context.nowById) || new Map(),
    groupById: (context && context.groupById) || new Map(),
    nameById: (context && context.nameById) || new Map(),
  };
  const columns = Array.isArray(data.columns) ? data.columns : [];
  const tree = rowTree(data.rows);
  const cells = Array.isArray(data.cells) ? data.cells : [];
  const leaves = leafReaders(columns);
  const totals = (data.reader_totals && typeof data.reader_totals === 'object') ? data.reader_totals : {};
  const stale = _staleReaders(data);

  const byPair = new Map();
  cells.forEach((cell) => {
    if (!cell || !cell.row_id || !cell.reader_id) return;
    byPair.set(`${cell.row_id}|${cell.reader_id}`, cell);
  });

  // Reader residual and summary rows are shown as the total rows instead.
  const totalKinds = new Set(['reader_residual', 'summary']);
  const treeIds = new Set();
  (function collect(nodes) {
    nodes.forEach((n) => { if (n.rowId) treeIds.add(n.rowId); collect(n.children || []); });
  })(tree);

  // Never drop data silently: a cell whose row is not in the row tree, or a
  // device with a published estimate but no registry row, still gets a row.
  const extra = new Map();
  const addExtra = (id, kind) => {
    if (!id || treeIds.has(id) || extra.has(id)) return;
    extra.set(id, {
      nodeId: `row::unregistered::${id}`, kind: kind || 'device',
      label: ctx.nameById.get(id) || id, rowId: id, children: [], unregistered: true,
    });
  };
  cells.forEach((cell) => {
    if (cell && !totalKinds.has(String(cell.row_kind || ''))) addExtra(String(cell.row_id || ''), String(cell.row_kind || 'device'));
  });
  ctx.nowById.forEach((_, id) => addExtra(id, 'device'));

  const rowEl = (node, depth) => {
    const tag = node.unregistered ? 'not in device registry' : _KIND_TAG[node.kind];
    const th = el('th', {
      class: `matrix-grid-identity matrix-row-${node.kind}`,
      scope: 'row',
      dataset: { rowKind: node.kind, rowId: node.rowId || '', depth: String(depth), nodeId: node.nodeId },
      title: node.rowId && node.rowId !== node.label ? `${node.label} · ${node.rowId}` : node.label,
    }, [
      el('span', { class: 'matrix-row-label' }, `${node.kind === 'room' ? '▾ ' : ''}${node.label}`),
      tag ? el('span', { class: 'matrix-row-tag' }, tag) : null,
    ]);
    const tds = leaves.map((leaf) => {
      if (!leaf.readerId || !node.rowId || node.kind === 'room') {
        return el('td', { class: 'matrix-grid-cell matrix-grid-empty' }, '');
      }
      const cell = byPair.get(`${node.rowId}|${leaf.readerId}`);
      return cell ? _v3Cell(cell, onDrill) : _absentCell(node.label, leaf.label);
    });
    const now = node.kind === 'room' ? el('td', { class: 'matrix-grid-now' }, '') : _nowCell(node, ctx);
    return el('tr', { dataset: { rowKind: node.kind, rowId: node.rowId || '', depth: String(depth) } }, [th, now, ...tds]);
  };

  const body = [];
  let loadRows = 0;
  const renderNode = (node, depth) => {
    if (totalKinds.has(node.kind)) return;
    const tr = rowEl(node, depth);
    body.push(tr);
    if (node.kind !== 'room') loadRows += 1;
    const children = Array.isArray(node.children) ? node.children : [];
    if (node.kind === 'group' && children.length) {
      // Group members are collapsed by default (perf: 500×100 fixture) and
      // show their own cells only: never a share of the group value.
      const memberTrs = children.map((child) => {
        const m = rowEl(child, depth + 1);
        m.className = 'matrix-row-member';
        m.hidden = true;
        return m;
      });
      const toggle = el('button', {
        class: 'matrix-group-toggle', type: 'button', 'aria-expanded': 'false',
        onclick: () => {
          const open = toggle.getAttribute('aria-expanded') !== 'true';
          memberTrs.forEach((m) => { m.hidden = !open; });
          toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
          toggle.textContent = `${open ? '▾' : '▸'} ${children.length} members`;
        },
      }, `▸ ${children.length} members`);
      tr.children[0].append(el('span', { class: 'matrix-group-toggle-wrap' }, toggle));
      body.push(...memberTrs);
      return;
    }
    children.forEach((child) => renderNode(child, depth + 1));
  };
  tree.forEach((node) => renderNode(node, 0));
  extra.forEach((node) => renderNode(node, 0));

  const top = TOTAL_ROWS.filter((s) => s.place === 'top');
  const bottom = TOTAL_ROWS.filter((s) => s.place === 'bottom' && (!s.optional
    || leaves.some((leaf) => {
      const v = (totals[leaf.readerId] || {})[s.key];
      return v !== null && v !== undefined;
    })));

  const lead = [
    el('th', { class: 'matrix-grid-identity matrix-grid-corner', rowspan: '5', scope: 'col' }, 'Load'),
    el('th', { class: 'matrix-grid-now-head', rowspan: '5', scope: 'col', title: 'Current published estimate' }, 'Now'),
  ];

  const total = data.physical_total || {};
  const fmt = (v) => (v !== undefined && v !== null ? watts(v) : '—');
  const notice = loadRows === 0
    ? el('p', { class: 'matrix-notice' },
        'No devices are registered with the engine yet, so the matrix has no load rows. Accept placements in Discover (or add devices) and they appear here as rows.')
    : null;
  return card('Mapping matrix', [
    el('div', { class: 'matrix-filter-count', id: 'matrix-filter-count' },
      `${loadRows} load row${loadRows === 1 ? '' : 's'} · ${leaves.length} reader${leaves.length === 1 ? '' : 's'} · ${cells.length} cell${cells.length === 1 ? '' : 's'}`),
    notice,
    matrixLegend(),
    el('div', { class: 'table-wrap matrix-grid-wrap' }, [
      el('table', { class: 'data matrix-grid' }, [
        el('thead', {}, matrixHeaderRowEls(columns, lead)),
        el('tbody', {}, [
          ...top.map((s) => _totalsRow(s, leaves, totals, stale)),
          ...body,
          ...bottom.map((s) => _totalsRow(s, leaves, totals, stale)),
        ]),
      ]),
    ]),
    el('div', { class: 'matrix-totals' }, [
      statCard('Total', fmt(total.total_w), { hint: 'Physical total (each load counted once)' }),
      statCard('Measured', fmt(total.measured_w), { hint: 'Directly measured devices' }),
      statCard('Identified', fmt(total.identified_w)),
      statCard('Group', fmt(total.group_w)),
      statCard('Unknown', fmt(total.unknown_w)),
      statCard('Residual', fmt(total.residual_w), { hint: 'Residual at the main reader(s)' }),
    ]),
  ], {
    foot: 'Columns come from the topology (domain → panel → subpanel → circuit → reader). "Now" is each load’s current published estimate. A cell shows its contribution (or interval), its mapping status and observability, matched events and evidence age; "·" means this reader does not observe the load. Group members never get a share of the group value. Select any cell for its evidence.',
  });
}

// Render the user-facing matrix.  The /matrix payload is authoritative; 404
// renders the explicit unavailable state.  Polling /status is cheap and only
// refetches the matrix when mapping_version_id changes.
async function renderMatrix(root, signal) {
  const [payload, devices, snapshot, status] = await Promise.all([
    fetchMatrix(signal),
    get('/devices', signal).catch(() => []),
    get('/snapshot', signal).catch(() => null),
    get('/status', signal).catch(() => null),
  ]);
  if (signal.aborted) return;

  const dbg = state.matrix;
  const versionChanged = matrixObserveVersion(status);

  // Page head: compact default with a "Diagnostic" toggle.
  const debugBtn = el('button', {
    class: `btn btn-sm ${dbg.debug ? 'btn-primary' : 'btn-ghost'}`,
    type: 'button',
    'aria-pressed': dbg.debug ? 'true' : 'false',
    onclick: () => { state.matrix.debug = !state.matrix.debug; render(); },
  }, dbg.debug ? 'Diagnostic: on' : 'Diagnostic: off');

  const rawBtn = el('button', {
    class: `btn btn-sm ${dbg.showRawIds ? 'btn-primary' : 'btn-ghost'}`,
    type: 'button',
    'aria-pressed': dbg.showRawIds ? 'true' : 'false',
    disabled: !dbg.debug,
    title: dbg.debug ? 'Show raw ids alongside labels' : 'Enable Diagnostic to show raw ids',
    onclick: () => { state.matrix.showRawIds = !state.matrix.showRawIds; render(); },
  }, dbg.showRawIds ? 'Raw IDs: on' : 'Raw IDs: off');

  const filterInput = el('input', {
    class: 'input matrix-filter',
    type: 'search',
    placeholder: 'Filter panel, circuit, reader, device, status…',
    value: dbg.filter,
    'aria-label': 'Filter the matrix',
    oninput: (e) => {
      state.matrix.filter = e.target.value;
      matrixApplyFilter();
    },
  });

  root.append(
    el('div', { class: 'page-head' }, [
      el('div', {}, [
        el('h1', {}, 'Matrix'),
        el('p', { class: 'lede' },
          'Where each reader sees each load, with its evidence. Select a cell, column or row for the full detail.'),
      ]),
      el('div', { class: 'head-actions' }, [
        filterInput,
        rawBtn,
        debugBtn,
        el('button', { class: 'btn', type: 'button', onclick: () => render() }, 'Refresh'),
      ]),
    ])
  );

  if (!payload) {
    root.append(_matrixIsUnavailable());
    return;
  }
  if (versionChanged) {
    // The version changed since the last poll: the fetch above is fresh.  A
    // future tick with the same version will not refetch.
    state.matrix.lastChangedAt = Date.now();
  }

  const v3 = applyFilters(payload, _parseFilters(dbg.filter));
  const onDrill = (cell) => matrixDrillDrawer('cell', {
    cell,
    payload,
    devices,
    snapshot,
  });

  root.append(matrixGridCard(v3, onDrill, _gridContext(devices, snapshot)));

  // Column and row drill-downs (HB §19.4).
  root.append(el('div', { class: 'matrix-drill-actions' }, [
    el('button', { class: 'btn btn-ghost btn-sm', type: 'button',
      onclick: () => matrixReaderDrill(payload) }, 'Reader detail'),
    el('button', { class: 'btn btn-ghost btn-sm', type: 'button',
      onclick: () => matrixRowDrill(payload) }, 'Row detail'),
  ]));

  if (dbg.debug) {
    const trace = matrixObserveChange(payload);
    root.append(matrixTraceCard(trace, payload));
    root.append(matrixSourceLedgerCard(devices, snapshot, payload));
    root.append(matrixIdentifiabilityCard(devices, snapshot));
  }

  // The Legacy cards are kept behind the Diagnostic toggle only; the matrix
  // grid itself always comes from the v3 payload, never /mapping.
  if (dbg.debug && payload.legacyMapping) {
    const ctx = matrixContext(payload.legacyMapping, null);
    root.append(matrixPivotCard(payload.legacyMapping, ctx));
    root.append(matrixCellsCard(payload.legacyMapping, ctx));
  }

  matrixApplyFilter();

  // Cheap poll: only /status.mapping_version_id.  A refetch happens solely when
  // the version changes; an unchanged version never re-requests /matrix.
  _scheduleVersionPoll(signal);
}

// Lookups for the grid's "Now" column, from payloads the view already fetches:
// /devices (v3 DeviceEstimate[], or the legacy {device_id, name} shape) and the
// snapshot's group estimates.  Nothing is computed here, only indexed.
function _gridContext(devices, snapshot) {
  const nowById = new Map();
  const nameById = new Map();
  (Array.isArray(devices) ? devices : []).forEach((d) => {
    if (!d || typeof d !== 'object') return;
    const id = d.subject_id || d.device_id;
    if (!id) return;
    if (d.name) nameById.set(String(id), String(d.name));
    if (d.subject_id) nowById.set(String(id), d);
  });
  const groupById = new Map();
  const groups = snapshot && Array.isArray(snapshot.group_estimates) ? snapshot.group_estimates : [];
  groups.forEach((g) => { if (g && g.group_id) groupById.set(String(g.group_id), g); });
  return { nowById, nameById, groupById };
}

// A single in-flight matrix version poll, restarted on each render.  The token
// stops a stale timer from a previous (aborted) render from refetching.
// Tests may shorten the interval via window.__WM_MATRIX_POLL_MS.
let _pollToken = 0;
const MATRIX_POLL_MS = (typeof window !== 'undefined' && window.__WM_MATRIX_POLL_MS)
  ? window.__WM_MATRIX_POLL_MS : 5000;

function _scheduleVersionPoll(signal) {
  const token = ++_pollToken;
  if (typeof window === 'undefined' || !window.setTimeout) return;
  const tick = async () => {
    if (token !== _pollToken || signal.aborted || state.view !== 'matrix') return;
    let status;
    try {
      status = await get('/status', signal);
    } catch {
      status = null;
    }
    if (token !== _pollToken || signal.aborted) return;
    if (status && matrixObserveVersion(status)) {
      // A new mapping version: refetch and repaint the whole view.
      render();
      return;
    }
    window.setTimeout(tick, MATRIX_POLL_MS);
  };
  window.setTimeout(tick, MATRIX_POLL_MS);
}

// Parse a filter expression into structured filter keys.  A free-text token
// like "status:provisional" maps to the structured filter; plain words narrow
// by label via applyFilters's panel/circuit/reader matching.
function _parseFilters(text) {
  const out = {};
  const raw = String(text || '').trim();
  if (!raw) return out;
  for (const token of raw.split(/\s+/)) {
    const [key, value] = token.split(':', 2);
    if (!value) continue;
    const k = key.toLowerCase();
    if (['reader', 'status', 'category', 'identifiability', 'panel', 'subpanel', 'circuit', 'room', 'type'].includes(k)) {
      out[k] = value;
    } else if (k === 'unknown') {
      out.unknownOnly = String(value).toLowerCase() !== 'false';
    } else if (k === 'confidence') {
      out.confidence = Number(value) || 0;
    }
  }
  return out;
}

export {
  renderMatrix,
  matrixGridCard,
  matrixHeaderTable,
  matrixLegend,
};
