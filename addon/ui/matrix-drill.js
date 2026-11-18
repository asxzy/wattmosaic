/* ==========================================================================
   WattMosaic Engine panel — matrix-drill.js
   --------------------------------------------------------------------------
   Native ES module; relative imports only so the same file works at "/" and
   under /api/hassio_ingress/<token>/.  No build step, no network beyond origin.
   ========================================================================== */

import { card, el, emptyState, kv, label, percent, watts } from './core.js';
import { cellView, matrixLedgerList } from './matrix-model.js';

// The joint-inference source ledger: used vs available-but-unused, each with
// the engine's concrete reason.  The engine attaches sources_used/sources_unused
// to every inference result for the cycle; /mapping cells also carry their own
// sources_used.  De-duplicate by source|reason so one shared reason is not
// printed once per device.  There is no per-cell sources_unused in any payload,
// so unused sources come from results only — an honest empty line when absent.
function matrixSourceLedgerCard(devices, snapshot, mapping) {
  const used = new Map();
  const unused = new Map();
  const add = (target, entry) => {
    if (!entry || typeof entry !== 'object') return;
    const source = String(entry.source || 'unknown');
    const reason = String(entry.reason || '—');
    target.set(`${source}|${reason}`, { source, reason });
  };
  const readResult = (result) => {
    if (!result) return;
    (Array.isArray(result.sources_used) ? result.sources_used : []).forEach((e) => add(used, e));
    (Array.isArray(result.sources_unused) ? result.sources_unused : []).forEach((e) => add(unused, e));
  };
  (Array.isArray(devices) ? devices : []).forEach((d) => readResult(d && d.latest_result));
  const snapResults = (snapshot && snapshot.results) || [];
  (Array.isArray(snapResults) ? snapResults : []).forEach(readResult);
  // Per-cell sources_used are always present on /mapping when the backend
  // publishes a matrix, so the "used" side still has real data without results.
  ((mapping && mapping.cells) || []).forEach((cell) => {
    (Array.isArray(cell && cell.sources_used) ? cell.sources_used : []).forEach((e) => add(used, e));
  });

  const sorted = (m) => [...m.values()].sort((a, b) =>
    a.source.localeCompare(b.source) || a.reason.localeCompare(b.reason));
  const hasAny = used.size > 0 || unused.size > 0;
  const body = hasAny
    ? el('div', {}, [
        el('div', { class: 'stat-label' }, 'Used this cycle'),
        matrixLedgerList(sorted(used), 'ok'),
        el('div', { class: 'stat-label', style: 'margin-top:14px' },
          'Available but unused (with reason)'),
        matrixLedgerList(sorted(unused), 'warn'),
      ])
    : emptyState(
        'No source ledger published',
        'The engine names the sources it consumed after the first inference cycle. No source list has been published yet.'
      );
  return card('Internal process — source ledger', [body], {
    foot: 'Every source and reason here is published by the engine, never inferred by the panel. A source the engine did not consume is marked unused with its own reason.',
  });
}

// The identifiability/group-range outcome.  A device with min_w/max_w or
// group_members is observable only as a GROUP SUM: show that range and label
// it NON_IDENTIFIABLE.  The engine writes power_w: 0.0 as a placeholder for
// such devices; that is NOT a measurement, so it is never rendered as a watt.
// Group rows are de-duplicated by the sorted member set: one group = one range.
function matrixIdentifiabilityCard(devices, snapshot) {
  const byGroup = new Map();
  const add = (result, fallbackId) => {
    if (!result) return;
    const hasBounds =
      (result.min_w !== null && result.min_w !== undefined) ||
      (result.max_w !== null && result.max_w !== undefined);
    const members = Array.isArray(result.group_members) ? result.group_members : [];
    if (!hasBounds && !members.length) return;
    const id = String((result.device_id || fallbackId || 'device'));
    const key = members.length ? members.slice().sort().join('|') : `id:${id}`;
    if (byGroup.has(key)) return;
    byGroup.set(key, { id, result, members });
  };
  (Array.isArray(devices) ? devices : []).forEach((d) =>
    add(d && d.latest_result, d && d.device_id));
  const snapResults = (snapshot && snapshot.results) || [];
  (Array.isArray(snapResults) ? snapResults : []).forEach((r) => add(r, r && r.device_id));

  if (!byGroup.size) {
    return card('Internal process — identifiability', [
      emptyState(
        'No group ranges',
        'The engine has not published an identifiability group range. A device with its own measurement needs no range.'
      ),
    ], {
      foot: 'A group range exists only where the reader sees a combined load it cannot split into per-device watts.',
    });
  }

  const rows = [...byGroup.values()].map(({ id, result, members }) => {
    const range = `${watts(result.min_w)} – ${watts(result.max_w)}`;
    const shown = members.length ? members.join(', ') : id;
    return el('div', { class: 'group-range-row', dataset: { status: 'non_identifiable' } }, [
      el('span', { class: 'group-range-name' }, id),
      el('span', { class: 'chip chip-non-identifiable' }, 'NON-IDENTIFIABLE'),
      el('span', { class: 'group-range-value' }, range),
      el('span', { class: 'group-range-members' }, `Group total of: ${shown}`),
    ]);
  });
  return card('Internal process — identifiability', rows, {
    foot: 'NON_IDENTIFIABLE: the reader observes only the group SUM. The range is that sum’s honest floor and cap — the engine does not fabricate a per-device split, so the 0 W placeholder is never shown as a measurement.',
  });
}

/* --- U1 — cell / reader / row drill-downs (HB §19.4) ------------------- *
 * A drawer surfaces the evidence behind a selection: mapping history, event
 * groups, evidence refs, confidence components, reasons for uncertainty and
 * contradictions.  The v3 payload already carries these on each cell, so the
 * drawer is pure over the published data and needs no new endpoint.  M3's
 * cell/reader/row routes can enrich it later; until then the same fields are
 * read from the matrix payload.
 * ---------------------------------------------------------------------- */

function _confidenceComponents(confidence) {
  const c = confidence || {};
  const rows = Object.keys(c)
    .filter((k) => typeof c[k] === 'number')
    .sort()
    .map((k) => [label(k), percent(c[k], 0)]);
  return rows.length
    ? kv(rows)
    : el('p', { class: 'stat-hint' }, 'No confidence components published.');
}

function _evidenceRefs(refs) {
  const list = Array.isArray(refs) ? refs : [];
  if (!list.length) return el('p', { class: 'stat-hint' }, 'No evidence references published.');
  return el('div', { class: 'matrix-evidence-refs' }, list.map((r) => el('span', {
    class: 'chip', title: String((r && r.kind) || 'evidence'),
  }, `${String((r && r.kind) || 'evidence')}: ${String((r && r.id) || '—')}`)));
}

function _drillShell(title, body) {
  return card(title, body, {
    actions: [el('button', {
      class: 'btn btn-ghost btn-sm', type: 'button',
      onclick: (e) => { const p = e.target.closest ? e.target.closest('.card') : null; if (p) p.hidden = true; },
    }, 'Close')],
    foot: 'Every value here is published by the engine. Nothing is inferred by the panel.',
  });
}

// Cell drill: power, mapping, identifiability, event groups, evidence refs,
// confidence components and uncertainty/contradiction reasons.
function matrixCellDrill(cell, payload) {
  const c = cell || {};
  const view = cellView(c);
  const power = c.power || {};
  const mapping = c.mapping || {};
  const events = c.event_association || {};
  const prov = c.provenance || {};
  const bounds = power.bounds || {};
  const low = bounds.lower_w !== undefined ? bounds.lower_w : bounds.low;
  const high = bounds.upper_w !== undefined ? bounds.upper_w : bounds.high;
  const interval = (low === undefined || low === null) && (high === undefined || high === null)
    ? '—' : `${watts(low)} – ${watts(high)}`;
  const reasons = [];
  if (view.category === 'group_only' || String(c.identifiability).toLowerCase() === 'non_identifiable') {
    reasons.push('The reader observes only the group sum; no per-device split is identifiable.');
  }
  if (String(mapping.observability || '').toLowerCase() === 'not_observable') {
    reasons.push('No measurement or mapping edge observes this load at this reader.');
  }
  if (String(mapping.mapping_status || '').toLowerCase() === 'conflicted') {
    reasons.push('Competing mapping evidence conflicts; the estimate is withheld.');
  }
  if (String(mapping.mapping_status || '').toLowerCase() === 'suspected_change') {
    reasons.push('A recent configuration change makes this mapping suspect.');
  }
  return _drillShell(`Cell · ${c.row_id || '—'} × ${c.reader_id || '—'}`, [
    el('div', { class: 'matrix-drill-summary matrix-cat-' + view.category }, [
      el('span', { class: 'matrix-cell-glyph' }, view.glyph),
      el('span', {}, view.text),
      el('strong', {}, view.value),
      el('span', { class: 'stat-hint' }, interval),
    ]),
    el('div', { class: 'stat-label' }, 'Mapping & power'),
    kv([
      ['Mapping status', label(mapping.mapping_status || 'unknown')],
      ['Observability', label(mapping.observability || 'unknown')],
      ['Relation', mapping.relation_type ? label(mapping.relation_type) : '—'],
      ['Measurement status', label(power.measurement_status || 'unknown')],
      ['Estimate', view.value],
      ['Bounds', interval],
      ['Identifiability', label(c.identifiability || 'unknown')],
      ['Group', c.group_id ? String(c.group_id) : '—'],
    ]),
    el('div', { class: 'stat-label', style: 'margin-top:10px' }, 'Event groups'),
    kv([
      ['Matched events', events.matched_event_count === undefined ? '—' : String(events.matched_event_count)],
      ['Confirmed matches', events.confirmed_match_count === undefined ? '—' : String(events.confirmed_match_count)],
      ['Provisional matches', events.provisional_match_count === undefined ? '—' : String(events.provisional_match_count)],
      ['Avg response delay', events.avg_response_delay_ms === undefined || events.avg_response_delay_ms === null
        ? '—' : `${events.avg_response_delay_ms} ms`],
      ['Last matched', events.last_matched_at ? String(events.last_matched_at) : '—'],
    ]),
    el('div', { class: 'stat-label', style: 'margin-top:10px' }, 'Provenance & evidence'),
    kv([
      ['Sources used', (Array.isArray(prov.sources_used) ? prov.sources_used : []).map(label).join(', ') || '—'],
      ['Topology version', prov.topology_version_id ? String(prov.topology_version_id) : '—'],
      ['Model version', prov.model_version_id ? String(prov.model_version_id) : '—'],
      ['Mapping version', prov.mapping_version_id ? String(prov.mapping_version_id) : '—'],
    ]),
    _evidenceRefs(prov.evidence_refs),
    el('div', { class: 'stat-label', style: 'margin-top:10px' }, 'Confidence components'),
    _confidenceComponents(c.confidence),
    el('div', { class: 'stat-label', style: 'margin-top:10px' }, 'Reasons for uncertainty / contradictions'),
    reasons.length
      ? el('ul', { class: 'matrix-reasons' }, reasons.map((r) => el('li', {}, r)))
      : el('p', { class: 'stat-hint' }, 'No uncertainty or contradiction reason published.'),
  ]);
}

function matrixReaderDrill(payload) {
  const data = payload || {};
  const totals = data.reader_totals || {};
  const body = el('div', {}, [
    el('p', { class: 'stat-hint' },
      'A reader column shows its own measured total, known contributions and unexplained residual. Columns are never summed.'),
    el('div', { class: 'table-wrap' }, [
      el('table', { class: 'data' }, [
        el('thead', {}, el('tr', {}, [
          el('th', {}, 'Reader'), el('th', { class: 'num' }, 'Measured'),
          el('th', { class: 'num' }, 'Known'), el('th', { class: 'num' }, 'Residual'),
          el('th', { class: 'num' }, 'Unknown'),
        ])),
        el('tbody', {}, Object.keys(totals).sort().map((reader) => {
          const t = totals[reader] || {};
          return el('tr', {}, [
            el('th', {}, reader),
            el('td', { class: 'num' }, watts(t.measured_w)),
            el('td', { class: 'num' }, watts(t.known_contrib_w)),
            el('td', { class: 'num' }, watts(t.residual_w)),
            el('td', { class: 'num' }, watts(t.unknown_w)),
          ]);
        })),
      ]),
    ]),
  ]);
  return _drillShell('Reader drill-down', body);
}

function matrixRowDrill(payload) {
  const data = payload || {};
  const cells = Array.isArray(data.cells) ? data.cells : [];
  const byRow = new Map();
  cells.forEach((c) => {
    if (!c || !c.row_id) return;
    if (!byRow.has(c.row_id)) byRow.set(c.row_id, []);
    byRow.get(c.row_id).push(c);
  });
  const body = el('div', {}, [
    el('p', { class: 'stat-hint' },
      'A row shows its observed readers and mapping history. A group row represents its members; it does not identify them individually.'),
    ...[...byRow.entries()].sort((a, b) => a[0].localeCompare(b[0])).map(([rowId, rcells]) => {
      const readers = rcells.map((c) => `${c.reader_id}: ${cellView(c).text}`).join(', ');
      return el('div', { class: 'matrix-row-drill-item' }, [
        el('div', { class: 'stat-label' }, rowId),
        el('div', { class: 'stat-hint' }, readers),
      ]);
    }),
  ]);
  return _drillShell('Row drill-down', body);
}

// Append a cell drill drawer to the DOM root.  Kept as a function (not a card
// in the grid) so clicking a cell opens it on demand without a full re-render.
function matrixDrillDrawer(kind, context) {
  if (kind === 'cell') return matrixCellDrill(context.cell, context.payload);
  if (kind === 'reader') return matrixReaderDrill(context.payload);
  if (kind === 'row') return matrixRowDrill(context.payload);
  return _drillShell('Detail', el('p', { class: 'stat-hint' }, 'No detail available.'));
}

export {
  matrixSourceLedgerCard,
  matrixIdentifiabilityCard,
  matrixCellDrill,
  matrixReaderDrill,
  matrixRowDrill,
  matrixDrillDrawer,
};
