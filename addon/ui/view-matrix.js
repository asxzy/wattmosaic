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

import { card, el, emptyState, get, render, statCard, state } from './core.js';
import { applyFilters, cellView, flattenRows, leafReaders, matrixObserveVersion, matrixContext, matrixObserveChange, rowTree } from './matrix-model.js';
import { matrixApplyFilter, matrixCellsCard, matrixCompactCell, matrixHeaderTable, matrixLegend, matrixPivotCard, matrixTraceCard } from './matrix-cell.js';
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
    title: `${view.text}${view.interval ? ` · ${view.interval}` : ''}`,
    onclick: () => onDrill && onDrill(cell),
  }, [
    matrixCompactCell(cell),
  ]);
  return el('td', { class: 'matrix-grid-cell', dataset: { category: view.category } }, btn);
}

// Render the v3 matrix grid: nested headers, a frozen identity column and a
// row tree with distinct special rows.  The grid is sparse (only published
// cells) and GROUP members are collapsed by default (perf: 500×100 fixture).
function matrixGridCard(payload, onDrill) {
  const data = payload || {};
  const columns = Array.isArray(data.columns) ? data.columns : [];
  const rows = rowTree(data.rows);
  const flat = flattenRows(rows);
  const cells = Array.isArray(data.cells) ? data.cells : [];
  const leaves = leafReaders(columns);
  const readers = leaves.map((l) => l.readerId);

  const byPair = new Map();
  cells.forEach((cell) => {
    if (!cell || !cell.row_id || !cell.reader_id) return;
    byPair.set(`${cell.row_id}|${cell.reader_id}`, cell);
  });

  // Root rows are rooms / devices / special rows.  Group children (members)
  // are hidden until the group is expanded.  Derived rows are marked.
  const cellRow = (node) => {
    const isSpecial = ['group', 'unknown_profile', 'reader_residual', 'summary'].includes(node.kind);
    const th = el('th', {
      class: `matrix-grid-identity matrix-row-${node.kind}${node.derived ? ' matrix-row-derived' : ''}`,
      scope: 'row',
      dataset: { rowKind: node.kind, rowId: node.rowId || '', depth: String(node.depth), nodeId: node.nodeId },
      title: `${node.label}${node.derived ? ' (derived)' : ''}`,
    }, `${node.kind === 'room' ? '▾ ' : ''}${isSpecial ? `${_rowGlyph(node.kind)} ` : ''}${node.label}`);
    const tds = readers.map((reader) => {
      if (!reader || !node.rowId) return el('td', { class: 'matrix-grid-cell matrix-grid-empty' }, '—');
      const cell = byPair.get(`${node.rowId}|${reader}`);
      if (!cell) return el('td', { class: 'matrix-grid-cell matrix-grid-empty' }, '—');
      return _v3Cell(cell, onDrill);
    });
    return { node, tr: el('tr', { dataset: { rowKind: node.kind, rowId: node.rowId || '' } }, [th, ...tds]) };
  };

  // Collapse group members by default: only root rows render until a group is
  // expanded by the user.  This keeps node count bounded for large fixtures.
  const expanded = new Set();
  const body = [];
  const renderNode = (node) => {
    const built = cellRow(node);
    body.push(built.tr);
    if (node.kind === 'group' && Array.isArray(node.children)) {
      const memberTrs = [];
      node.children.forEach((child) => {
        const cb = cellRow(child);
        memberTrs.push(cb.tr);
        // Member rows show a group-only cell: "—" plus the interval and a
        // non-identifiable label, never a per-member number.
        cb.tr.className = 'matrix-row-member';
      });
      const toggle = el('button', {
        class: 'matrix-group-toggle', type: 'button',
        'aria-expanded': expanded.has(node.nodeId) ? 'true' : 'false',
        onclick: () => {
          const open = !expanded.has(node.nodeId);
          if (open) expanded.add(node.nodeId); else expanded.delete(node.nodeId);
          memberTrs.forEach((tr) => { tr.hidden = !open; });
          toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
        },
      }, expanded.has(node.nodeId) ? `▾ ${node.children.length} members` : `▸ ${node.children.length} members`);
      built.tr.children[0].append(el('span', { class: 'matrix-group-toggle-wrap' }, toggle));
      body.push(...memberTrs);
      memberTrs.forEach((tr) => { tr.hidden = !expanded.has(node.nodeId); });
    }
  };
  // Render only top-level rows; nested non-group children are appended when
  // their group is expanded (group members).  Rooms show their device rows.
  flat.filter((n) => n.depth === 0).forEach(renderNode);

  // Add a residual row label for any reader residual kind present.
  const total = data.physical_total || {};
  return card('Mapping matrix', [
    el('div', { class: 'matrix-filter-count', id: 'matrix-filter-count' }),
    matrixLegend(),
    el('div', { class: 'table-wrap matrix-grid-wrap' }, [
      el('table', { class: 'data matrix-grid' }, [
        matrixHeaderTable(columns),
        el('tbody', {}, body),
      ]),
    ]),
    el('div', { class: 'matrix-totals' }, [
      statCard('Measured total', total.measured_w !== undefined && total.measured_w !== null
        ? String(total.measured_w) : '—', { hint: 'Physical total (each load counted once)' }),
      statCard('Identified', total.identified_w !== undefined && total.identified_w !== null
        ? String(total.identified_w) : '—'),
      statCard('Group', total.group_w !== undefined && total.group_w !== null
        ? String(total.group_w) : '—'),
      statCard('Unknown', total.unknown_w !== undefined && total.unknown_w !== null
        ? String(total.unknown_w) : '—'),
      statCard('Residual', total.residual_w !== undefined && total.residual_w !== null
        ? String(total.residual_w) : '—'),
    ]),
  ], {
    foot: 'Columns come from the v3 column tree; Σ colSpan per level = reader count. A group row shows its group value; members show "—" plus a non-identifiable label. Select any cell for its evidence drill-down.',
  });
}

function _rowGlyph(kind) {
  switch (kind) {
    case 'group': return '⊞';
    case 'unknown_profile': return '?';
    case 'reader_residual': return '∅';
    case 'summary': return 'Σ';
    default: return '';
  }
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

  root.append(matrixGridCard(v3, onDrill));

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
