/* ==========================================================================
   WattMosaic Engine panel — matrix-model.js
   --------------------------------------------------------------------------
   Native ES module; relative imports only so the same file works at "/" and
   under /api/hassio_ingress/<token>/.  No build step, no network beyond origin.
   ========================================================================== */

import { badge, el, label, percent, state } from './core.js';

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
    ['power', 'Power'],
    ['ct_association', 'CT assoc.'],
    ['topology', 'Topology'],
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
// Reasons are code strings (e.g. "rows_in_fit:ct_kitchen"); show them as text,
// never title-cased into a sentence the engine did not say.
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

/* --- Matrix debug helpers --------------------------------------------- *
 * Pure functions over data the panel already fetches.  They never invent a
 * value: a missing name falls back to the id, a missing topology falls back
 * to a flat header.  Kept independent of the DOM (except where noted) so the
 * harness can exercise them.
 * ---------------------------------------------------------------------- */

// Flatten /topology/tree into the lookups the matrix needs:
//   deviceLabel:  device id (dev_...) or HA hex id -> human name
//   readerPath:   reader/CT id -> { panel, circuit, label }
//   groupLabel:   panel -> label (used to group the header stack)
// The tree nodes carry {id, kind, label, children, device_id, ct_id}.  A device
// node's device_id links the /devices axis; a circuit's ct_id links a reader.
// HA hex-id devices from the mapping axis are labelled via mapping.device_names.
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
      // Keep the panel context for descending device nodes.
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

  // The /devices payload is not passed here, but topology device nodes carry
  // names too; HA hex-axis names come straight from the mapping projection.
  Object.keys(deviceNames).forEach((id) => {
    deviceLabel[id] = String(deviceNames[id]);
  });

  return { deviceLabel, readerPath, panelLabels, deviceNames };
}

// A signature of exactly the fields that define the matrix projection.  Two
// matrices with the same signature are byte-identical by contract; a change
// means the engine's underlying topology/relations/models/HA data changed.
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

// Compare this render's matrix to the last one and record a trace entry when
// it changed.  Returns the fresh trace (newest first, capped) for the card.
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

export {
  matrixConfidence,
  matrixLedgerList,
  matrixContext,
  matrixSignature,
  matrixObserveChange
};
