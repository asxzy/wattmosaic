/* ==========================================================================
   WattMosaic Engine panel — view-overview.js
   --------------------------------------------------------------------------
   Native ES module; relative imports only so the same file works at "/" and
   under /api/hassio_ingress/<token>/.  No build step, no network beyond origin.
   ========================================================================== */

import { slots } from './slots.js';
import { SOURCE_SWATCH, ago, badge, card, duration, el, emptyState, fetchOverview, get, kv, label, notice, num, percent, render, setConnection, setFooter, sourceTone, statCard, state, statusBadge, tierBadge, titleCase, toneFor, watts } from './core.js';
import { cellView } from './matrix-model.js';

/* ====================================================================== *
 *  U2 — v3 overview model (pure, node-testable).
 * ---------------------------------------------------------------------- *
 *  Every number the overview shows carries its status, identifiability and
 *  a path to its provenance.  The model is derived only from the v3
 *  MatrixPayload (K05) the panel already fetches from GET /api/v1/matrix —
 *  no number is invented and no UNKNOWN / group member is printed as a
 *  measured watt (HB §18).  Rendering cards consume this same model, so
 *  what the tests assert is what the panel actually paints.
 * ====================================================================== */

const _absent = (value) => value === null || value === undefined;
const _text = (value) => (_absent(value) || value === '' ? null : String(value));

// The eight HB §19.3 categories have distinct status text.  A cell's status
// text is the engine-published category, never a guess from the wattage.
const _statusText = (cell) => {
  const view = cellView(cell);
  return view.text || null;
};

// A provenance drill is a relative API path the user can open to see where a
// number came from.  It is built only from ids the payload actually carries.
function _provenanceDrill(cell, payload) {
  const c = cell || {};
  const rowId = _text(c.row_id);
  const readerId = _text(c.reader_id);
  const prov = c.provenance || {};
  if (rowId && readerId) return `/matrix/cells/${encodeURIComponent(rowId)}/${encodeURIComponent(readerId)}`;
  if (String(c.row_kind || '') === 'reader_residual' && readerId) {
    return `/matrix/readers/${encodeURIComponent(readerId)}`;
  }
  if (rowId) return `/matrix/rows/${encodeURIComponent(rowId)}`;
  const evidence = Array.isArray(prov.evidence_refs) ? prov.evidence_refs[0] : null;
  if (evidence && _text(evidence.kind) && _text(evidence.id)) {
    return `/provenance/${encodeURIComponent(String(evidence.kind))}/${encodeURIComponent(String(evidence.id))}`;
  }
  return _text(payload && payload.mapping_version_id)
    ? `/matrix?at=${encodeURIComponent(String(payload.mapping_version_id))}`
    : '/matrix';
}

// One rendered number in a view model.  ``value`` is display text: an UNKNOWN
// or non-identifiable cell renders "—", never "0 W".  ``watts`` is the raw
// published estimate (None when absent) so cards never read a placeholder.
function _renderedNumber(cell, payload, id) {
  const c = cell || {};
  const view = cellView(c);
  const power = c.power || {};
  const bounds = power.bounds || {};
  const low = bounds.lower_w !== undefined ? bounds.lower_w : bounds.low;
  const high = bounds.upper_w !== undefined ? bounds.upper_w : bounds.high;
  const identifiability = _text(c.identifiability);
  const versions = (payload && payload.versions) || {};
  const prov = c.provenance || {};
  const versionsPresent =
    _text(versions.topology_version_id) || _text(prov.topology_version_id)
    || _text(versions.mapping_version_id) || _text(prov.mapping_version_id)
    || _text(versions.model_set_id) || _text(prov.model_version_id);
  return {
    id: id || `${_text(c.row_id) || 'row'}@${_text(c.reader_id) || 'reader'}`,
    value: view.value,
    watts: _absent(power.estimated_power_w) ? null : power.estimated_power_w,
    status_text: _statusText(c) || 'Unknown',
    identifiability: identifiability || 'unknown',
    // Bounds are always present as an object (HB §18: absence is None, never
    // 0.0).  A cell that published no interval still explains that honestly:
    // lower_w and upper_w are null rather than invented zeros.
    bounds: { lower_w: _absent(low) ? null : low, upper_w: _absent(high) ? null : high },
    provenance_drill: _provenanceDrill(c, payload),
    versions_present: Boolean(versionsPresent),
  };
}

// HA entity attributes for every device estimate.  Per HB §18 / H1, a device
// with no MEASURED (or ESTIMATED+IDENTIFIABLE) value exposes None for its power
// and its lower/upper bounds, plus the three version ids and the status.
function _haAttribute(cell, payload) {
  const c = cell || {};
  const view = cellView(c);
  const power = c.power || {};
  const bounds = power.bounds || {};
  const low = bounds.lower_w !== undefined ? bounds.lower_w : bounds.low;
  const high = bounds.upper_w !== undefined ? bounds.upper_w : bounds.high;
  const prov = c.provenance || {};
  const versions = (payload && payload.versions) || {};
  const noNumber = view.category === 'unknown' || view.category === 'not_observable'
    || view.category === 'group_only' || _absent(power.estimated_power_w);
  return {
    status: _text(power.measurement_status) || view.category,
    identifiability: _text(c.identifiability) || 'unknown',
    lower_w: _absent(low) ? null : low,
    upper_w: _absent(high) ? null : high,
    group_id: _text(c.group_id),
    point_w: noNumber ? null : power.estimated_power_w,
    provisional: String(view.category) === 'provisional'
      || String((c.mapping || {}).mapping_status || '').toLowerCase() === 'provisional',
    topology_version_id: _text(prov.topology_version_id) || _text(versions.topology_version_id),
    mapping_version_id: _text(prov.mapping_version_id) || _text(versions.mapping_version_id),
    model_version_id: _text(prov.model_version_id) || _text(versions.model_set_id),
  };
}

// The whole view model for the v3 overview.  Cells are de-duplicated by
// (row_id, reader_id) so nested readers do not render a value twice, and a
// group member never masquerades as a measured per-device number.
function v3ViewModel(payload) {
  const data = payload || {};
  const cells = Array.isArray(data.cells) ? data.cells : [];
  const seen = new Set();
  const renderedNumbers = [];
  const haAttributes = {};
  const groups = [];
  const readers = [];
  const unknown = [];
  const summary = {
    mapping_version_id: _text(data.mapping_version_id),
    measured_w: null, identified_w: null, group_w: null, unknown_w: null, residual_w: null,
  };

  for (const cell of cells) {
    const c = cell || {};
    const key = `${_text(c.row_id) || ''}|${_text(c.reader_id) || ''}`;
    if (!seen.has(key)) {
      seen.add(key);
      renderedNumbers.push(_renderedNumber(c, data));
    }
    const ident = String(c.identifiability || '').toLowerCase();
    const kind = String(c.row_kind || '');
    if (kind === 'group') {
      groups.push(renderedNumbers.find((n) => n.id === `${_text(c.row_id)}@${_text(c.reader_id)}`));
    } else if (kind === 'unknown_profile') {
      unknown.push(renderedNumbers.find((n) => n.id === `${_text(c.row_id)}@${_text(c.reader_id)}`));
    } else if (kind === 'reader_residual') {
      readers.push(renderedNumbers.find((n) => n.id === `${_text(c.row_id)}@${_text(c.reader_id)}`));
    }
    if (kind === 'device' && _text(c.row_id)) haAttributes[String(c.row_id)] = _haAttribute(c, data);
  }

  const total = data.physical_total || {};
  summary.measured_w = _absent(total.measured_w) ? null : total.measured_w;
  summary.identified_w = _absent(total.identified_w) ? null : total.identified_w;
  summary.group_w = _absent(total.group_w) ? null : total.group_w;
  summary.unknown_w = _absent(total.unknown_w) ? null : total.unknown_w;
  summary.residual_w = _absent(total.residual_w) ? null : total.residual_w;

  const readerTotals = data.reader_totals || {};
  const readerRows = Object.keys(readerTotals).sort().map((reader) => {
    const t = readerTotals[reader] || {};
    return {
      reader_id: reader,
      measured_w: _absent(t.measured_w) ? null : t.measured_w,
      residual_w: _absent(t.residual_w) ? null : t.residual_w,
      unknown_w: _absent(t.unknown_w) ? null : t.unknown_w,
      drill: `/matrix/readers/${encodeURIComponent(reader)}`,
    };
  });

  return {
    rendered_numbers: renderedNumbers.filter(Boolean),
    ha_attributes: haAttributes,
    groups: groups.filter(Boolean),
    readers: readerRows,
    unknown: unknown.filter(Boolean),
    residual: readers.filter(Boolean),
    summary,
  };
}

// A numeric cell rendered as a card row: status + identifiability + provenance
// link, with bounds but never a fabricated watt for UNKNOWN / group members.
function _numberRow(item) {
  const attrs = {
    class: 'v3-number-row',
    dataset: { status: String(item.status_text).toLowerCase().replace(/\s+/g, '_') },
  };
  return el('div', attrs, [
    el('span', { class: 'v3-number-id' }, item.id),
    el('strong', { class: 'v3-number-value' }, item.value === '—' ? '—' : item.value),
    badge(item.status_text, item.status_text === 'Unknown' ? 'mute' : 'info'),
    badge(label(item.identifiability), 'warn'),
    item.bounds
      ? el('span', { class: 'stat-hint' },
          `${item.bounds.lower_w === null ? '—' : watts(item.bounds.lower_w)} – ${item.bounds.upper_w === null ? '—' : watts(item.bounds.upper_w)}`)
      : el('span', { class: 'stat-hint' }, 'no bounds published'),
    el('a', { class: 'v3-provenance', href: `#${item.provenance_drill}` }, 'Why?'),
  ]);
}

function matrixSummaryCard(model) {
  const s = model.summary || {};
  return card('Matrix summary', [
    kv([
      ['Measured', watts(s.measured_w)],
      ['Identified', watts(s.identified_w)],
      ['Group', watts(s.group_w)],
      ['Unknown', watts(s.unknown_w)],
      ['Residual', watts(s.residual_w)],
      ['Mapping version', s.mapping_version_id || '—'],
    ]),
    el('div', { class: 'stat-hint', style: 'margin-top:10px' },
      'Each number carries its own status and provenance in the cell drill-down.'),
  ], { foot: 'From GET /api/v1/matrix. Columns are never summed across readers.' });
}

function _numberCard(title, items, foot) {
  const list = Array.isArray(items) ? items : [];
  return card(title, list.length ? list.map(_numberRow) : [
    el('p', { class: 'stat-hint' }, 'None published for this cycle.'),
  ], { foot });
}

// U2: build a MatrixPayload-shaped view from the snapshot/devices/residuals the
// overview already fetches.  This avoids a second /matrix request on first
// render (the Matrix tab owns that request) and never calls the legacy
// /mapping.  v3 fields are read verbatim; a legacy result is rendered with its
// published status/identifiability and honest bounds, never an invented number.
function overviewMatrixPayload(snapshot, devices, residuals) {
  const snap = snapshot || {};
  const list = Array.isArray(devices) ? devices : [];
  const cells = [];
  const add = (rowId, readerId, rowKind, estimate) => {
    if (!rowId || !readerId) return;
    const e = estimate || {};
    cells.push({
      row_id: String(rowId),
      reader_id: String(readerId),
      row_kind: rowKind,
      identifiability: e.identifiability || 'unknown',
      group_id: e.group_id || null,
      power: {
        measurement_status: e.status || e.measurement_status || null,
        estimated_power_w: _absent(e.point_w)
          ? (_absent(e.power_w) ? null : e.power_w) : e.point_w,
        bounds: e.bounds || null,
      },
      mapping: { mapping_status: e.mapping_status || null, observability: e.observability || null },
      provenance: e.provenance || {},
    });
  };
  (Array.isArray(snap.device_estimates) ? snap.device_estimates : []).forEach((e) => {
    const readers = Array.isArray(e && e.source_reader_ids) ? e.source_reader_ids : [null];
    readers.forEach((r) => add(e && e.subject_id, r, 'device', e));
  });
  (Array.isArray(snap.group_estimates) ? snap.group_estimates : []).forEach((e) => {
    const readers = Array.isArray(e && e.source_reader_ids) ? e.source_reader_ids : [null];
    readers.forEach((r) => add(e && e.group_id, r, 'group', { ...e, identifiability: 'group_identifiable' }));
  });
  (Array.isArray(snap.unknown_estimates) ? snap.unknown_estimates : []).forEach((e) => {
    add(e && e.scope_reader_id, e && e.scope_reader_id, 'unknown_profile',
      { ...e, identifiability: 'insufficient_evidence', status: 'estimated' });
  });
  // The legacy /devices shape: a group placeholder (0 W) is never a number.
  list.forEach((d) => {
    const r = d && d.latest_result;
    if (!r) return;
    const ident = Array.isArray(r.group_members) && r.group_members.length
      ? 'non_identifiable' : (r.identifiable === false ? 'non_identifiable' : 'identifiable');
    add(d.device_id, (r.supporting_ct_channels || [])[0] || 'reader', 'device', {
      ...r,
      identifiability: ident,
      bounds: (!_absent(r.min_w) || !_absent(r.max_w))
        ? { lower_w: r.min_w ?? null, upper_w: r.max_w ?? null } : null,
      power_w: r.power_w_is_placeholder ? null : r.power_w,
    });
  });
  const recon = snap.reconciliation || {};
  return {
    mapping_version_id: (snap.versions && snap.versions.mapping_version_id) || null,
    versions: snap.versions || {},
    cells,
    reader_totals: {},
    physical_total: {
      measured_w: recon.main_w ?? recon.main_power_w ?? null,
      identified_w: recon.identified_w ?? null,
      group_w: recon.group_w ?? null,
      unknown_w: recon.unknown_w ?? null,
      residual_w: recon.residual_w ?? recon.unknown_residual_w ?? null,
    },
  };
}

// U2: fetch only what the overview renders.  The old fetchOverview() also
// requested the flat /mapping payload on first render; that card is gone, so
// this local fetch deliberately omits /mapping (core.js is not owned by U2).
async function fetchOverviewV3(signal) {
  const [health, status, snapshot, devices, residuals] = await Promise.all([
    get('/health', signal),
    get('/status', signal).catch(() => null),
    get('/snapshot', signal).catch(() => null),
    get('/devices', signal).catch(() => []),
    get('/residuals', signal).catch(() => null),
  ]);
  return { health, status, snapshot, devices, residuals };
}

async function renderOverview(root, signal) {
  const { health, status, snapshot, devices, residuals } = await fetchOverviewV3(signal);
  if (signal.aborted) return;
  // U2: the flat /mapping card is gone, and the first render never calls
  // /mapping (nor a second /matrix).  The v3 cards read the snapshot/devices
  // data the overview already fetched; a v3 matrix payload, when present there,
  // supplies the per-number status, identifiability and provenance.

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

  const groupRanges = slots.groupRangesCard(devices);
  if (groupRanges) root.append(groupRanges);
  if (residuals) root.append(slots.residualDecompositionCard(residuals));

  // U2 v3 cards, built from the data this view already fetched.
  const model = v3ViewModel(overviewMatrixPayload(snapshot, devices, residuals));
  if (!model.rendered_numbers.length && !residuals) {
    root.append(emptyState(
      'Matrix v3 unavailable',
      'No v3 estimate or matrix payload was published yet. The summary, group, residual and unknown cards need one; no value is guessed in the meantime.'
    ));
  } else {
    root.append(matrixSummaryCard(model));
    root.append(_numberCard(
      'Group estimates',
      model.groups,
      'A group is observable only as its SUM. Members show "—", never a per-device watt.'
    ));
    root.append(_numberCard(
      'Residual by reader',
      model.readers.map((r) => ({
        id: r.reader_id,
        value: _absent(r.residual_w) ? '—' : watts(r.residual_w),
        status_text: 'Reader residual',
        identifiability: 'identifiable',
        bounds: null,
        provenance_drill: r.drill,
      })),
      'Residual is a signed measurement of what a reader could not attribute — never a device.'
    ));
    root.append(_numberCard(
      'Unknown loads',
      model.unknown,
      'An unknown load carries a provisional status and bounds; it is never shown as a confirmed measured device.'
    ));
  }
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

export {
  v3ViewModel,
  overviewMatrixPayload,
  matrixSummaryCard,
  fetchOverviewV3,
  renderOverview,
  overviewHeader,
  overviewStats,
  overviewHero,
  phasePill,
  reconciliationBreakdown,
  reconciliationCard,
  systemCard,
  overviewDevices,
  deviceMiniRow
};
