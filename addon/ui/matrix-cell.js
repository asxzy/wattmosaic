/* ==========================================================================
   WattMosaic Engine panel — matrix-cell.js
   --------------------------------------------------------------------------
   Native ES module; relative imports only so the same file works at "/" and
   under /api/hassio_ingress/<token>/.  No build step, no network beyond origin.
   ========================================================================== */

import { badge, card, clock, el, emptyState, get, kv, label, num, state, statusBadge, watts } from './core.js';
import { matrixConfidence, matrixContext } from './matrix-model.js';

function matrixTraceCard(trace, mapping) {
  const dbg = state.matrix;
  const rows = (trace || []).map((entry) =>
    el('div', { class: 'matrix-trace-row' }, [
      el('span', { class: 'matrix-trace-when' }, clock(new Date(entry.at).toISOString())),
      el('span', { class: 'matrix-trace-summary' }, String(entry.summary)),
    ]));
  const changed = dbg.lastChangedAt
    ? clock(new Date(dbg.lastChangedAt).toISOString()) : '—';
  return card('Matrix change trace', [
    kv([
      ['Changes observed', String(dbg.changeCount)],
      ['Last changed at', changed],
      ['Topology version', String((mapping && mapping.topology_version) || '—')],
    ]),
    rows.length
      ? el('div', { class: 'matrix-trace' }, rows)
      : el('p', { class: 'stat-hint' }, 'No changes recorded yet.'),
  ], {
    foot: 'Debug mode: each entry is a distinct matrix projection observed this session. Refresh to sample again — the matrix is recomputed read-time from the engine’s current state.',
  });
}

// Apply the Matrix filter to the currently painted cards without a full
// re-render (so typing does not re-fetch /mapping). Rows and columns whose
// visible label / id / group text matches stay visible; a row is hidden only
// when none of its cells or its own label match.
function matrixApplyFilter() {
  const query = String(state.matrix.filter || '').trim().toLowerCase();
  const table = document.querySelector('table.matrix-pivot');
  if (!table) return;
  const bodyRows = [...table.querySelectorAll('tbody tr')];
  bodyRows.forEach((row) => {
    const own = (row.querySelector('th.matrix-pivot-device')?.textContent || '').toLowerCase();
    let visible = !query || own.includes(query);
    if (!visible) {
      const cells = [...row.querySelectorAll('td.matrix-pivot-cell')];
      visible = cells.some((cell) => {
        const label = cell.getAttribute('data-filter-text') || cell.textContent || '';
        return label.toLowerCase().includes(query);
      });
    }
    row.hidden = !visible;
  });
  // Hide reader columns when no visible row has a match in that column.
  if (query) {
    const headerCells = [...table.querySelectorAll('thead th.matrix-pivot-reader')];
    headerCells.forEach((th) => {
      const idx = [...th.parentElement.children].indexOf(th);
      const anyVisible = bodyRows.some((row) => {
        if (row.hidden) return false;
        const cell = row.querySelectorAll('td.matrix-pivot-cell')[idx - 1];
        if (!cell) return false;
        const label = cell.getAttribute('data-filter-text') || cell.textContent || '';
        return label.toLowerCase().includes(query);
      });
      const colVisible = anyVisible || (th.textContent || '').toLowerCase().includes(query);
      th.hidden = !colVisible;
      bodyRows.forEach((row) => {
        const cell = row.querySelectorAll('td.matrix-pivot-cell')[idx - 1];
        if (cell) cell.hidden = !colVisible;
      });
    });
  } else {
    // Clear any column hiding from a previous filter.
    [...table.querySelectorAll('thead th.matrix-pivot-reader')].forEach((th) => { th.hidden = false; });
    bodyRows.forEach((row) =>
      [...row.querySelectorAll('td.matrix-pivot-cell')].forEach((cell) => { cell.hidden = false; }));
  }
  const counter = document.getElementById('matrix-filter-count');
  if (counter) {
    const shown = bodyRows.filter((r) => !r.hidden).length;
    counter.textContent = query ? `${shown} of ${bodyRows.length} devices` : '';
  }
}

// Human label for a device id: prefer the HA name from the projection, then a
// topology device node name, then the raw id (never an invented placeholder).
function matrixDeviceLabel(id, ctx, showRaw) {
  const name = ctx.deviceLabel[String(id)];
  if (name && !showRaw) return name;
  if (name && showRaw) return `${name} · ${id}`;
  return String(id);
}

// Device-major pivot of the mapping matrix, built from the backend's additive
// axes: rows are the real devices (``devices``), columns the CT readers
// (``readers``).  CT ids therefore only ever appear as column headers, never
// as row labels.  Each cell shows a compact signal (status + observability +
// contribution) and carries its full detail in a hover/focus panel, reusing
// the cell-detail helpers below.  A non-identifiable cell shows "—" for its
// contribution, never an invented per-device watt.  Older backends without
// both axes fall back to the flat cell list.
function matrixPivotCard(mapping, ctx) {
  const data = mapping || {};
  const context = ctx || matrixContext(mapping, null);
  const showRaw = Boolean(state.matrix.debug && state.matrix.showRawIds);
  const devices = Array.isArray(data.devices) ? data.devices : [];
  const readers = Array.isArray(data.readers) ? data.readers : [];
  if (!devices.length || !readers.length) return matrixCellsCard(mapping, ctx);

  const cells = Array.isArray(data.cells) ? data.cells : [];
  const byPair = new Map();
  cells.forEach((cell) => {
    const c = cell || {};
    byPair.set(`${c.device_id}|${c.reader_id}`, c);
  });

  const contributionOf = (c) => {
    const status = String(c.status || 'unknown');
    if (status.toLowerCase() === 'non_identifiable') return '—';
    const estimate = c.estimated_contribution_w;
    return estimate === null || estimate === undefined ? '—' : watts(estimate);
  };

  const detail = (c) => {
    const status = String(c.status || 'unknown');
    const low = c.min_contribution_w;
    const high = c.max_contribution_w;
    const bounds = (low === null || low === undefined) && (high === null || high === undefined)
      ? '—' : `${watts(low)} – ${watts(high)}`;
    const sources = (Array.isArray(c.sources_used) ? c.sources_used : [])
      .map((s) => String((s && s.source) || 'unknown'));
    return el('div', { class: 'matrix-pivot-detail' }, [
      el('div', { class: 'stat-label' }, 'Cell detail'),
      kv([
        ['Status', label(status)],
        ['Observability', label(c.observability || 'unknown')],
        ['Relation', c.relation_type ? label(c.relation_type) : '—'],
        ['Contribution', contributionOf(c)],
        ['Bounds', bounds],
        ['Evidence count', c.evidence_count === undefined || c.evidence_count === null
          ? '—' : String(c.evidence_count)],
        ['Model version', c.model_version === undefined || c.model_version === null
          ? '—' : `v${c.model_version}`],
        ['Topology version', c.topology_version ? String(c.topology_version) : '—'],
        ['Reader config', data.reader_configuration_version
          ? String(data.reader_configuration_version) : '—'],
        ['Sources used', sources.length ? sources.join(', ') : '—'],
        ['Unknown load', c.is_unknown_load ? badge('unknown load', 'warn') : '—'],
      ]),
      el('div', { class: 'stat-label', style: 'margin-top:8px' }, 'Confidence'),
      matrixConfidence(c.confidence),
    ]);
  };

  // Reader label: prefer the electrical circuit name from /topology/tree;
  // fall back to the raw reader id when the engine publishes no hierarchy.
  const readerLabel = (reader) => {
    const path = context.readerPath[String(reader)];
    return path && path.circuit ? String(path.circuit) : String(reader);
  };
  // The header is a STACK: one row for the panel/group level, one for the
  // circuit/reader level, instead of a single flat line of opaque ids.  Each
  // column keeps its reader id in dataset so cells still line up by index.
  const panelRow = el('tr', { class: 'matrix-hdr-panel' }, [
    el('th', { class: 'matrix-pivot-corner', rowspan: '2' }, 'Device / circuit'),
    ...readers.map((reader) => {
      const path = context.readerPath[String(reader)];
      const group = path && (path.panel || path.group) ? String(path.panel || path.group) : '';
      return el('th', {
        class: 'matrix-hdr-group', dataset: { reader: String(reader) },
        title: group || 'Reader grouped under no published panel',
      }, group || '—');
    }),
  ]);
  const circuitRow = el('tr', { class: 'matrix-hdr-circuit' },
    readers.map((reader) => el('th', {
      class: 'matrix-pivot-reader', dataset: { reader: String(reader) },
      title: String(reader),
    }, readerLabel(reader)))
  );
  const header = [panelRow, circuitRow];

  const bodyRows = devices.map((device) => el('tr', {}, [
    el('th', {
      class: 'matrix-pivot-device', scope: 'row', dataset: { device: String(device) },
      title: String(device),
    }, matrixDeviceLabel(device, context, showRaw)),
    ...readers.map((reader) => {
      const c = byPair.get(`${device}|${reader}`);
      const filterText = `${matrixDeviceLabel(device, context, false)} ${readerLabel(reader)} ${reader}`;
      if (!c) {
        return el('td', {
          class: 'matrix-pivot-cell matrix-pivot-empty',
          dataset: { device: String(device), reader: String(reader), filterText },
          title: `No mapping for ${matrixDeviceLabel(device, context, showRaw)} / ${readerLabel(reader)}`,
        }, '—');
      }
      return el('td', {
        class: 'matrix-pivot-cell', tabindex: '0',
        dataset: { device: String(device), reader: String(reader), filterText },
        title: `${label(c.status || 'unknown')} · ${label(c.observability || 'unknown')}`,
      }, [
        el('span', { class: 'matrix-pivot-signal' }, [
          statusBadge(String(c.status || 'unknown')),
          el('span', { class: 'matrix-pivot-obs' }, label(c.observability || 'unknown')),
          el('span', { class: 'matrix-pivot-watt' }, contributionOf(c)),
        ]),
        detail(c),
      ]);
    }),
  ]));

  return card('Mapping matrix — device × reader', [
    el('div', { class: 'matrix-filter-count', id: 'matrix-filter-count' }),
    el('div', { class: 'table-wrap matrix-pivot-wrap' }, [
      el('table', { class: 'data matrix-pivot' }, [
        el('thead', {}, header),
        el('tbody', {}, bodyRows),
      ]),
    ]),
  ], {
    foot: 'Rows are real devices, labelled with their Home Assistant names; columns are CT readers grouped by electrical panel/circuit. The header is a stack: panel above circuit. Hover or focus a cell for its full detail — bounds, confidence and provenance are the engine’s published values, never an invented per-device watt.',
  });
}

// GET /api/v1/mapping -> {cells:[...], coverage:{counts, ...}, ...}.  Richer
// than the overview's compact card: it exposes every field the cell publishes
// so a person can audit the mapping cell by cell.  A non-identifiable cell has
// bounds but no point estimate, rendered "—" and never a fabricated watt.
function matrixCellsCard(mapping, ctx) {
  const data = mapping || {};
  const context = ctx || matrixContext(mapping, null);
  const showRaw = Boolean(state.matrix.debug && state.matrix.showRawIds);
  const cells = Array.isArray(data.cells) ? data.cells : [];
  const rows = cells.map((cell) => {
    const c = cell || {};
    const status = String(c.status || 'unknown');
    const nonIdentifiable = status.toLowerCase() === 'non_identifiable';
    const estimate = c.estimated_contribution_w;
    const contribution = nonIdentifiable || estimate === null || estimate === undefined
      ? '—'
      : watts(estimate);
    const low = c.min_contribution_w;
    const high = c.max_contribution_w;
    const bounds = (low === null || low === undefined) && (high === null || high === undefined)
      ? '—'
      : `${watts(low)} – ${watts(high)}`;
    const sources = (Array.isArray(c.sources_used) ? c.sources_used : [])
      .map((s) => String((s && s.source) || 'unknown'));
    return el('tr', {}, [
      el('td', {}, c.device_id
        ? matrixDeviceLabel(c.device_id, context, showRaw) : '—'),
      el('td', {}, String(c.reader_id || '—')),
      el('td', {}, statusBadge(status)),
      el('td', {}, label(c.observability || 'unknown')),
      el('td', {}, c.relation_type ? label(c.relation_type) : '—'),
      el('td', { class: 'num' }, contribution),
      el('td', { class: 'num' }, bounds),
      el('td', {}, matrixConfidence(c.confidence)),
      el('td', { class: 'num' },
        c.evidence_count === undefined || c.evidence_count === null
          ? '—' : String(c.evidence_count)),
      el('td', {}, sources.length ? sources.join(', ') : '—'),
      el('td', {}, c.model_version === undefined || c.model_version === null
        ? '—' : `v${c.model_version}`),
      el('td', {}, c.topology_version ? String(c.topology_version) : '—'),
      el('td', {}, c.is_unknown_load ? badge('unknown load', 'warn') : '—'),
    ]);
  });

  const coverage = (data.coverage && data.coverage.counts) || {};
  const byStatus = coverage.by_status || {};
  const byCardinality = coverage.by_cardinality || {};
  const summaryRows = [
    ['Reader config version', String(data.reader_configuration_version || '—')],
    ['Matrix topology version', String(data.topology_version || '—')],
    ['Cells', String(coverage.cell_count === undefined ? cells.length : coverage.cell_count)],
    ['Unknown loads', String(coverage.unknown_load_count === undefined ? 0 : coverage.unknown_load_count)],
    ...Object.keys(byStatus).sort().map((key) => [`Status: ${label(key)}`, String(byStatus[key])]),
    ...Object.keys(byCardinality).sort().map((key) => [`Relation: ${label(key)}`, String(byCardinality[key])]),
  ];

  const body = rows.length
    ? el('div', {}, [
        el('div', { class: 'table-wrap' }, [
          el('table', { class: 'data matrix-cells' }, [
            el('thead', {}, el('tr', {}, [
              el('th', {}, 'Device'),
              el('th', {}, 'Reader'),
              el('th', {}, 'Status'),
              el('th', {}, 'Observability'),
              el('th', {}, 'Relation'),
              el('th', { class: 'num' }, 'Estimate'),
              el('th', { class: 'num' }, 'Bounds'),
              el('th', {}, 'Confidence'),
              el('th', { class: 'num' }, 'Evidence'),
              el('th', {}, 'Sources used'),
              el('th', {}, 'Model'),
              el('th', {}, 'Topology'),
              el('th', {}, 'Marker'),
            ])),
            el('tbody', {}, rows),
          ]),
        ]),
        el('div', { class: 'mapping-coverage', style: 'margin-top:14px' }, [
          el('div', { class: 'stat-label' }, 'Coverage'),
          kv(summaryRows),
        ]),
      ])
    : emptyState(
        'No mapping cells',
        'The engine has not derived a device/reader mapping matrix yet.'
      );
  return card('Mapping matrix — cell detail', [body], {
    foot: 'Each row is one device↔reader relation as published by the engine. A non-identifiable cell carries a range, not an invented per-device watt; confidence dimensions stay separate, never blended.',
  });
}

export {
  matrixTraceCard,
  matrixApplyFilter,
  matrixDeviceLabel,
  matrixPivotCard,
  matrixCellsCard
};
