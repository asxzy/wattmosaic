/* ==========================================================================
   WattMosaic Engine panel — matrix-model.js
   --------------------------------------------------------------------------
   Native ES module; relative imports only so the same file works at "/" and
   under /api/hassio_ingress/<token>/.  No build step, no network beyond origin.

   This module is the pure, node-testable contract between the v3 MatrixPayload
   (K05) and the DOM renderers.  It never fetches and never touches the DOM:
   ``headerRows``, ``rowTree``, ``cellView`` and ``applyFilters`` can be imported
   directly under node with a DOM shim (tests/unit/panel_harness.py).
   ========================================================================== */

import { badge, el, label, percent, state, watts } from './core.js';

/* --- Matrix diagnostic view (TASK-UI-MATRIX-DIAGNOSTIC) ----------------- *
 * A read-only view that explains HOW a result was produced.  It reads only
 * payloads the panel already fetches: /mapping for the cell matrix,
 * /devices and /snapshot for the source ledger and identifiability ranges, and
 * /status for version context.  Nothing is invented: an absent field reads
 * "—", and a NON_IDENTIFIABLE device shows a group range, never a per-device
 * watt.  These renderers are pure so the DOM harness can execute them.
 * ---------------------------------------------------------------------- */

// A ConfidenceVector has no blended score by design (it even refuses
// __float__).  Show only the dimensions the engine actually assessed, each as
// its own labelled percentage, plus the derivation method — never collapse
// them into one invented number.
function matrixConfidence(confidence) {
  if (!confidence || typeof confidence !== 'object') return '—';
  const dims = [
    ['measurement_quality', 'Measurement'],
    ['event_association', 'Events'],
    ['model_fit', 'Model fit'],
    ['topology_consistency', 'Topology'],
    ['identifiability', 'Identifiability'],
    ['sample_coverage', 'Coverage'],
    ['freshness', 'Freshness'],
    ['power', 'Power'],
    ['ct_association', 'CT assoc.'],
    ['circuit_classification', 'Circuit'],
    ['residual', 'Residual'],
  ].filter(([key]) => confidence[key] !== null && confidence[key] !== undefined);
  if (!dims.length) return confidence.method ? String(confidence.method) : '—';
  return el('span', { class: 'matrix-confidence' }, [
    ...dims.map(([key, name]) =>
      el('span', { class: 'chip' }, `${name} ${percent(confidence[key], 0)}`)),
    confidence.method ? el('span', { class: 'stat-hint' }, String(confidence.method)) : null,
  ]);
}

// A ledger entry is {source, reason}, both published verbatim by the engine.
function matrixLedgerList(entries, tone) {
  const list = Array.isArray(entries) ? entries : [];
  if (!list.length) {
    return el('p', { class: 'stat-hint' }, 'None recorded for this cycle.');
  }
  return el('div', { class: 'matrix-ledger' },
    list.map((entry) => el('div', { class: 'matrix-ledger-row' }, [
      badge(label(String((entry && entry.source) || 'unknown')), tone),
      el('span', { class: 'matrix-ledger-reason' },
        String((entry && entry.reason) || '—')),
    ]))
  );
}

/* --- Matrix debug helpers (legacy /mapping payload) -------------------- *
 * Pure functions over data the legacy panel fetches.  They never invent a
 * value; kept for the diagnostic cards and for the existing panel tests.
 * ---------------------------------------------------------------------- */

// Flatten /topology/tree into the lookups the legacy matrix needs.
function matrixContext(mapping, topology) {
  const data = mapping || {};
  const deviceNames = (data.device_names && typeof data.device_names === 'object')
    ? data.device_names : {};
  const deviceLabel = Object.create(null);
  const readerPath = Object.create(null);
  const panelLabels = Object.create(null);
  const ancestors = [];

  const walk = (node) => {
    if (!node || typeof node !== 'object') return;
    const kind = String(node.kind || '');
    const name = node.label ? String(node.label) : null;
    if (kind === 'panel') {
      ancestors.length = 0;
      ancestors.push({ panel: name, circuit: null, group: null });
      if (name && node.id) panelLabels[String(node.id)] = name;
    } else if (kind === 'group') {
      if (ancestors.length) ancestors[0].group = name;
    } else if (kind === 'circuit') {
      const ctx = ancestors.length ? { ...ancestors[0] } : { panel: null, group: null };
      ctx.circuit = name;
      if (node.ct_id) readerPath[String(node.ct_id)] = ctx;
      ancestors.push(ctx);
    } else if (kind === 'device') {
      const id = node.device_id || (String(node.id || '').startsWith('device:')
        ? String(node.id).slice('device:'.length) : null);
      if (id && name) deviceLabel[String(id)] = name;
    }
    (Array.isArray(node.children) ? node.children : []).forEach(walk);
    if (kind === 'circuit') ancestors.pop();
  };
  (topology && Array.isArray(topology.roots) ? topology.roots : []).forEach(walk);

  Object.keys(deviceNames).forEach((id) => {
    deviceLabel[id] = String(deviceNames[id]);
  });

  return { deviceLabel, readerPath, panelLabels, deviceNames };
}

function matrixSignature(mapping) {
  const d = mapping || {};
  const cells = Array.isArray(d.cells) ? d.cells : [];
  const cellSig = cells.map((c) => [
    c && c.device_id, c && c.reader_id, c && c.status, c && c.observability,
    c && c.relation_type, c && c.estimated_contribution_w, c && c.min_contribution_w,
    c && c.max_contribution_w, c && c.model_version,
  ].join('~')).join('|');
  return [
    d.reader_configuration_version, d.topology_version,
    (d.devices || []).join(','), (d.readers || []).join(','), cellSig,
  ].join('::');
}

function matrixObserveChange(mapping) {
  const dbg = state.matrix;
  const sig = matrixSignature(mapping);
  if (sig !== dbg.lastSignature) {
    const now = Date.now();
    const count = Array.isArray(mapping && mapping.cells) ? mapping.cells.length : 0;
    dbg.changeCount += 1;
    dbg.lastSignature = sig;
    dbg.lastChangedAt = now;
    dbg.trace.unshift({
      at: now,
      summary: dbg.changeCount === 1 ? 'first observation' : `${count} cell(s)`,
    });
    if (dbg.trace.length > 20) dbg.trace.length = 20;
  }
  return dbg.trace;
}

/* ====================================================================== *
 *  U1 — v3 MatrixPayload model (pure, node-testable).
 * ====================================================================== */

const LEVELS = ['L0', 'L1', 'L2', 'L3', 'L4'];

// The eight HB §19.3 visual categories.  Each has DISTINCT text and glyph so
// no category relies on colour alone (HB §19.3 / DEC-29).
const CATEGORIES = [
  { category: 'directly_measured', text: 'Directly measured', glyph: '●', tone: 'ok' },
  { category: 'confirmed_inferred', text: 'Confirmed inferred', glyph: '◆', tone: 'ok' },
  { category: 'provisional', text: 'Provisional', glyph: '◇', tone: 'warn' },
  { category: 'group_only', text: 'Group-only (non-identifiable)', glyph: '⊞', tone: 'warn' },
  { category: 'unknown', text: 'Unknown', glyph: '?', tone: 'mute' },
  { category: 'not_observable', text: 'Not observable', glyph: '∅', tone: 'mute' },
  { category: 'conflicted', text: 'Conflicted', glyph: '!', tone: 'bad' },
  { category: 'retired', text: 'Retired', glyph: '×', tone: 'mute' },
];
const CATEGORY_BY_ID = Object.fromEntries(CATEGORIES.map((c) => [c.category, c]));

function levelIndex(level) {
  const i = LEVELS.indexOf(String(level || '').toUpperCase());
  return i < 0 ? 0 : i;
}

// Number of reader leaves below a column node.  A reader leaf is a node with a
// reader_id or kind "reader", or any node without children.
function countLeaves(node) {
  if (!node || typeof node !== 'object') return 0;
  const children = Array.isArray(node.children) ? node.children : [];
  if (node.reader_id || node.kind === 'reader' || children.length === 0) return 1;
  return children.reduce((sum, child) => sum + countLeaves(child), 0);
}

// Flatten the reader leaves of a column tree in display order.
function leafReaders(columns) {
  const out = [];
  const walk = (node, path) => {
    if (!node || typeof node !== 'object') return;
    const nextPath = [...path, node];
    const children = Array.isArray(node.children) ? node.children : [];
    if (node.reader_id || node.kind === 'reader' || children.length === 0) {
      out.push({
        nodeId: String(node.node_id || ''),
        readerId: node.reader_id ? String(node.reader_id) : null,
        reader_id: node.reader_id ? String(node.reader_id) : null,
        label: String(node.label || node.reader_id || node.node_id || ''),
        level: String(node.level || 'L4'),
        path: nextPath,
        annotations: Array.isArray(node.annotations) ? node.annotations : [],
      });
      return;
    }
    children.forEach((child) => walk(child, nextPath));
  };
  (Array.isArray(columns) ? columns : []).forEach((root) => walk(root, []));
  return out;
}

// Build the L0–L4 header rows from a column tree.  Each row has one cell per
// node at that level (plus spanning placeholders where a branch skips a
// level), so Σ colSpan for every row equals the number of reader leaves.
// Cells carry parentId to express nesting and annotations from secondary
// edges — which never become extra leaves.
function headerRows(columns) {
  const rows = LEVELS.map((level) => ({ level, cells: [] }));
  const roots = Array.isArray(columns) ? columns : [];

  const makeCell = (node, parentId, targetIdx, synthetic) => {
    const idx = levelIndex(node.level);
    return {
      nodeId: synthetic ? `${node.node_id || 'col'}@${LEVELS[targetIdx]}` : String(node.node_id || ''),
      level: LEVELS[targetIdx],
      label: String(node.label || node.reader_id || node.node_id || ''),
      kind: synthetic ? 'span' : String(node.kind || ''),
      readerId: node.reader_id ? String(node.reader_id) : null,
      colSpan: countLeaves(node),
      parentId: parentId === undefined ? null : parentId,
      annotations: synthetic || !Array.isArray(node.annotations) ? [] : node.annotations,
      synthetic: Boolean(synthetic),
    };
  };

  const collect = (node, targetIdx, out, parentId) => {
    if (!node || typeof node !== 'object') return;
    const idx = levelIndex(node.level);
    if (idx === targetIdx) {
      out.push(makeCell(node, parentId, targetIdx, false));
      return;
    }
    if (idx > targetIdx) {
      // A branch skipped this level: emit a spanning placeholder so the row
      // still sums to the leaf count, pointing at the nearest upper ancestor.
      out.push(makeCell(node, parentId, targetIdx, true));
      return;
    }
    const children = Array.isArray(node.children) ? node.children : [];
    children.forEach((child) => collect(child, targetIdx, out, String(node.node_id || '')));
  };

  rows.forEach((row, idx) => {
    roots.forEach((root) => collect(root, idx, row.cells, null));
  });
  return rows;
}

// Normalise the v3 row tree.  GROUP rows expose their member row ids from
// their children; every node records its declared derived flag (SUMMARY rows).
function rowTree(rows) {
  const build = (node) => {
    if (!node || typeof node !== 'object') return null;
    const children = (Array.isArray(node.children) ? node.children : [])
      .map(build).filter(Boolean);
    const out = {
      nodeId: String(node.node_id || ''),
      kind: String(node.kind || 'device'),
      label: String(node.label || node.row_id || node.node_id || ''),
      rowId: node.row_id ? String(node.row_id) : null,
      // SUMMARY rows are derived by definition (K05 validator): a hand-built
      // fixture that omits the flag still renders as a derived row.
      derived: Boolean(node.derived) || String(node.kind || '').toLowerCase() === 'summary',
      children,
    };
    if (out.kind === 'group') {
      out.memberRowIds = children
        .filter((c) => c.rowId && c.kind !== 'group')
        .map((c) => c.rowId);
    }
    return out;
  };
  return (Array.isArray(rows) ? rows : []).map(build).filter(Boolean);
}

// Depth-first flatten of a row tree with depth and group membership.
function flattenRows(tree) {
  const out = [];
  const walk = (node, depth) => {
    if (!node) return;
    out.push({
      nodeId: node.nodeId,
      kind: node.kind,
      label: node.label,
      rowId: node.rowId,
      derived: Boolean(node.derived),
      depth,
      memberRowIds: Array.isArray(node.memberRowIds) ? node.memberRowIds.slice() : [],
    });
    (Array.isArray(node.children) ? node.children : []).forEach((c) => walk(c, depth + 1));
  };
  (Array.isArray(tree) ? tree : []).forEach((root) => walk(root, 0));
  return out;
}

function _isAbsent(value) {
  return value === null || value === undefined;
}

// A simple relative-age label for a timestamp.  No value is fabricated: an
// absent or invalid timestamp reads "—".
function _age(iso) {
  if (!iso) return '—';
  const then = new Date(iso);
  if (Number.isNaN(then.getTime())) return '—';
  const seconds = Math.max(0, (Date.now() - then.getTime()) / 1000);
  if (seconds < 60) return `${Math.round(seconds)}s ago`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}

function _confidenceGlyph(confidence) {
  if (!confidence || typeof confidence !== 'object') return '—';
  const n = Object.keys(confidence).filter(
    (k) => !_isAbsent(confidence[k]) && typeof confidence[k] === 'number'
  ).length;
  if (n >= 5) return '◈';
  if (n >= 1) return '◇';
  return '—';
}

// cellView(cell) -> {category, text, glyph, value, interval, confidence, age}.
// The M1 ``visual`` field is the deterministic category; a fallback derives it
// from mapping status/observability for hand-built or legacy cells.  The text
// and glyph are taken from CATEGORIES, so all eight categories are distinct
// without relying on colour (HB §19.3).
function cellView(cell) {
  const c = cell || {};
  const power = c.power || {};
  const mapping = c.mapping || {};
  const timestamps = c.timestamps || {};
  const bounds = power.bounds || {};
  const status = String(mapping.mapping_status || '').toLowerCase();
  const obs = String(mapping.observability || '').toLowerCase();
  let category = String(c.visual || '').toLowerCase();
  if (!CATEGORY_BY_ID[category]) {
    if (obs === 'not_observable') category = 'not_observable';
    else if (status === 'retired') category = 'retired';
    else if (status === 'conflicted') category = 'conflicted';
    else if (status === 'provisional') category = 'provisional';
    else if (status === 'unknown' || obs === 'unknown') category = 'unknown';
    else if (String(c.identifiability || '').toLowerCase() === 'non_identifiable'
             || c.group_id) category = 'group_only';
    else if (String(power.measurement_status || '').toLowerCase() === 'measured') category = 'directly_measured';
    else if (status === 'confirmed') category = 'confirmed_inferred';
    else category = 'unknown';
  }
  const meta = CATEGORY_BY_ID[category] || CATEGORY_BY_ID.unknown;
  const nonIdentifiable =
    category === 'group_only' ||
    String(c.identifiability || '').toLowerCase() === 'non_identifiable' ||
    (obs === 'unknown' && _isAbsent(power.estimated_power_w));

  const estimate = power.estimated_power_w;
  // An UNKNOWN / NOT_OBSERVABLE cell never publishes a number, even if a
  // legacy 0 W placeholder leaks through (HB §18: absent is None, never 0).
  const noNumber = category === 'unknown' || category === 'not_observable'
    || nonIdentifiable || _isAbsent(estimate);
  const value = noNumber ? '—' : watts(estimate);
  const low = bounds.lower_w !== undefined ? bounds.lower_w : bounds.low;
  const high = bounds.upper_w !== undefined ? bounds.upper_w : bounds.high;
  const interval = _isAbsent(low) && _isAbsent(high)
    ? null
    : `${_isAbsent(low) ? '—' : watts(low)} – ${_isAbsent(high) ? '—' : watts(high)}`;

  let text = meta.text;
  if (nonIdentifiable && category !== 'group_only') {
    text = `${meta.text} — non-identifiable`;
  }
  return {
    category,
    text,
    glyph: meta.glyph,
    tone: meta.tone,
    value,
    interval,
    confidence: _confidenceGlyph(c.confidence),
    age: _age(timestamps.updated_at || timestamps.last_observed_at),
  };
}

function _leavesForLabel(columns, kind, needle) {
  const wanted = String(needle || '').toLowerCase();
  return leafReaders(columns)
    .filter((leaf) => leaf.path.some((node) =>
      String(node.kind || '').toLowerCase() === kind &&
      String(node.label || '').toLowerCase().includes(wanted)))
    .map((leaf) => leaf.readerId)
    .filter(Boolean);
}

function _rowIdsForRoom(rows, needle) {
  const wanted = String(needle || '').toLowerCase();
  const ids = new Set();
  const walk = (node) => {
    if (!node || typeof node !== 'object') return;
    if (String(node.kind || '').toLowerCase() === 'room' &&
        String(node.label || '').toLowerCase().includes(wanted)) {
      (function collect(n) {
        if (!n || typeof n !== 'object') return;
        if (n.row_id) ids.add(String(n.row_id));
        (Array.isArray(n.children) ? n.children : []).forEach(collect);
      })(node);
      return;
    }
    (Array.isArray(node.children) ? node.children : []).forEach(walk);
  };
  (Array.isArray(rows) ? rows : []).forEach(walk);
  return ids;
}

// Prune a column tree to the given reader ids, preserving ancestors.
function _pruneColumns(columns, readerIds) {
  const allowed = new Set(readerIds);
  const prune = (node) => {
    if (!node || typeof node !== 'object') return null;
    if (node.reader_id || node.kind === 'reader') {
      return allowed.has(String(node.reader_id)) ? { ...node, children: [] } : null;
    }
    const children = (Array.isArray(node.children) ? node.children : [])
      .map(prune).filter(Boolean);
    return children.length ? { ...node, children } : null;
  };
  return (Array.isArray(columns) ? columns : []).map(prune).filter(Boolean);
}

// Structured filters over a v3 MatrixPayload.  Supported keys: reader, status,
// category, identifiability, unknownOnly, panel, subpanel, circuit, room, type
// (row_kind), confidence (minimum) and activeStandby (operating_state).
// Filters combine with AND.  An empty filter object returns the full payload.
function applyFilters(payload, filters) {
  const data = payload || {};
  const f = filters || {};
  const columns = Array.isArray(data.columns) ? data.columns : [];
  const rows = Array.isArray(data.rows) ? data.rows : [];
  const cells = Array.isArray(data.cells) ? data.cells : [];

  const readerSet = new Set();
  const restrictReaders = (ids) => ids.forEach((id) => readerSet.add(id));
  if (f.reader) restrictReaders([String(f.reader)]);
  if (f.panel) {
    restrictReaders(_leavesForLabel(columns, 'panel', f.panel));
    // "Unassigned measurements" is an L1 container of kind "unassigned".
    restrictReaders(_leavesForLabel(columns, 'unassigned', f.panel));
  }
  if (f.subpanel) restrictReaders(_leavesForLabel(columns, 'subpanel', f.subpanel));
  if (f.circuit) restrictReaders(_leavesForLabel(columns, 'circuit', f.circuit));
  // When any column-level filter was given, only its readers survive.
  const hasColumnFilter = Boolean(f.reader || f.panel || f.subpanel || f.circuit);

  const roomIds = f.room ? _rowIdsForRoom(rows, f.room) : null;

  const filteredCells = cells.filter((cell) => {
    const c = cell || {};
    if (hasColumnFilter && !readerSet.has(String(c.reader_id))) return false;
    if (f.status && String((c.mapping || {}).mapping_status || '') !== String(f.status)) return false;
    if (f.category && String(c.visual || '') !== String(f.category)) return false;
    if (f.identifiability && String(c.identifiability || '') !== String(f.identifiability)) return false;
    if (f.unknownOnly && String(c.row_kind || '') !== 'unknown_profile') return false;
    if (f.type && String(c.row_kind || '') !== String(f.type)) return false;
    if (f.room && !(c.row_id && roomIds.has(String(c.row_id)))) return false;
    if (f.confidence !== undefined && f.confidence !== null && f.confidence !== '') {
      const conf = (c.confidence || {});
      const values = Object.keys(conf)
        .filter((k) => typeof conf[k] === 'number')
        .map((k) => conf[k]);
      const min = values.length ? Math.min(...values) : null;
      if (min === null || min < Number(f.confidence)) return false;
    }
    if (f.activeStandby !== undefined && f.activeStandby !== null && f.activeStandby !== '') {
      const os = String((c.power || {}).operating_state || '').toLowerCase();
      if (f.activeStandby === 'active' && os !== 'active') return false;
      if (f.activeStandby === 'standby' && os !== 'standby') return false;
    }
    return true;
  });

  const keptColumns = hasColumnFilter ? _pruneColumns(columns, readerSet) : columns;
  return {
    ...data,
    cells: filteredCells,
    columns: keptColumns,
    leafReaders: leafReaders(keptColumns),
    rows,
  };
}

// The last /status.mapping_version_id observed by the matrix view.  Kept here
// (not in core.js) so this task stays inside its owned files.
let _lastMappingVersion = null;

// Observe /status.mapping_version_id: return true only on a *change* from the
// last observed version.  An absent version never counts as a change.
function matrixObserveVersion(status) {
  const s = status || {};
  const version = s.mapping_version_id !== undefined && s.mapping_version_id !== null
    ? s.mapping_version_id
    : (s.versions && s.versions.mapping_version_id !== undefined
        ? s.versions.mapping_version_id : null);
  if (version === null || version === undefined) return false;
  if (_lastMappingVersion === version) return false;
  _lastMappingVersion = version;
  return true;
}

export {
  // Legacy diagnostic helpers (kept for the diagnostic cards and old tests).
  matrixConfidence,
  matrixLedgerList,
  matrixContext,
  matrixSignature,
  matrixObserveChange,
  // U1 v3 model.
  CATEGORIES,
  LEVELS,
  levelIndex,
  countLeaves,
  leafReaders,
  headerRows,
  rowTree,
  flattenRows,
  cellView,
  applyFilters,
  matrixObserveVersion,
};
