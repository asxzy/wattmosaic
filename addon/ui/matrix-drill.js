/* ==========================================================================
   WattMosaic Engine panel — matrix-drill.js
   --------------------------------------------------------------------------
   Native ES module; relative imports only so the same file works at "/" and
   under /api/hassio_ingress/<token>/.  No build step, no network beyond origin.
   ========================================================================== */

import { card, el, emptyState, label, watts } from './core.js';
import { matrixLedgerList } from './matrix-model.js';

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

export {
  matrixSourceLedgerCard,
  matrixIdentifiabilityCard
};
