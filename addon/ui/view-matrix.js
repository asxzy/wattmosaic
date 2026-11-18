/* ==========================================================================
   WattMosaic Engine panel — view-matrix.js
   --------------------------------------------------------------------------
   Native ES module; relative imports only so the same file works at "/" and
   under /api/hassio_ingress/<token>/.  No build step, no network beyond origin.
   ========================================================================== */

import { el, get, label, render, statCard, state } from './core.js';
import { matrixContext, matrixObserveChange } from './matrix-model.js';
import { matrixApplyFilter, matrixCellsCard, matrixPivotCard, matrixTraceCard } from './matrix-cell.js';
import { matrixIdentifiabilityCard, matrixSourceLedgerCard } from './matrix-drill.js';

/* --- Matrix diagnostic view ------------------------------------------- *
 * A purpose-built page that fetches /mapping plus the payloads the panel
 * already reads on the overview (/devices, /snapshot, /status) and assembles
 * the three audit cards above.  It introduces no new endpoint or API field:
 * the source ledger and identifiability ranges come from the same latest
 * results the Devices view uses.  A missing payload degrades to an honest
 * empty section rather than an invented value.
 * ---------------------------------------------------------------------- */

async function renderMatrix(root, signal) {
  // /topology/tree is the read-time electrical hierarchy (panel → circuit →
  // device).  It is fetched here so the matrix can present its header as a
  // stack instead of one flat line of opaque reader ids.  An older engine that
  // 404s degrades to the device axis and name map only.
  const [mapping, devices, snapshot, status, topology] = await Promise.all([
    get('/mapping', signal).catch(() => null),
    get('/devices', signal).catch(() => []),
    get('/snapshot', signal).catch(() => null),
    get('/status', signal).catch(() => null),
    get('/topology/tree', signal).catch(() => null),
  ]);
  if (signal.aborted) return;

  // Trace any change to the matrix since the last time this view was painted.
  // The matrix is a pure read-time projection, so a changed signature means
  // the underlying topology/relations/models/HA data changed — exactly what a
  // debug view needs to surface.
  const trace = matrixObserveChange(mapping);

  const dbg = state.matrix;
  const debugBtn = el('button', {
    class: `btn btn-sm ${dbg.debug ? 'btn-primary' : 'btn-ghost'}`,
    type: 'button',
    'aria-pressed': dbg.debug ? 'true' : 'false',
    onclick: () => { state.matrix.debug = !state.matrix.debug; render(); },
  }, dbg.debug ? 'Debug: on' : 'Debug: off');

  const rawBtn = el('button', {
    class: `btn btn-sm ${dbg.showRawIds ? 'btn-primary' : 'btn-ghost'}`,
    type: 'button',
    'aria-pressed': dbg.showRawIds ? 'true' : 'false',
    disabled: !dbg.debug,
    title: dbg.debug ? 'Show raw HA ids alongside labels' : 'Enable Debug to show raw ids',
    onclick: () => { state.matrix.showRawIds = !state.matrix.showRawIds; render(); },
  }, dbg.showRawIds ? 'Raw IDs: on' : 'Raw IDs: off');

  const filterInput = el('input', {
    class: 'input matrix-filter',
    type: 'search',
    placeholder: 'Filter devices, circuits, readers…',
    value: dbg.filter,
    'aria-label': 'Filter the matrix by device, circuit or reader',
    oninput: (e) => {
      state.matrix.filter = e.target.value;
      matrixApplyFilter();
    },
  });

  root.append(
    el('div', { class: 'page-head' }, [
      el('div', {}, [
        el('h1', {}, 'Matrix & internal process'),
        el('p', { class: 'lede' },
          'How the engine mapped readers to devices and decided what it could attribute — cell by cell, source by source. Devices are labelled with their Home Assistant names.'),
      ]),
      el('div', { class: 'head-actions' }, [
        filterInput,
        rawBtn,
        debugBtn,
        el('button', { class: 'btn', type: 'button', onclick: () => render() }, 'Refresh'),
      ]),
    ])
  );

  const engineStatus = (status && status.inference) || {};
  const topologyVersion =
    (mapping && mapping.topology_version) ||
    (status && status.topology_version) ||
    '—';
  root.append(el('div', { class: 'grid cols-3', style: 'margin-bottom:16px' }, [
    statCard('Cells', mapping && mapping.coverage && mapping.coverage.counts
      ? String(mapping.coverage.counts.cell_count) : '—', {
      hint: 'Device/reader relations',
    }),
    statCard('Topology version', String(topologyVersion), {
      hint: 'Matrix was computed against',
    }),
    statCard('Inference backend', engineStatus.backend
      ? String(engineStatus.backend) : '—', {
      hint: engineStatus.degraded ? 'Degraded' : 'Last inference engine',
      tone: engineStatus.degraded ? 'warn' : null,
    }),
  ]));

  // Build the label + hierarchy context once and share it with the cards.
  const ctx = matrixContext(mapping, topology);

  if (dbg.debug) root.append(matrixTraceCard(trace, mapping));
  root.append(matrixPivotCard(mapping, ctx));
  root.append(matrixCellsCard(mapping, ctx));
  root.append(matrixSourceLedgerCard(devices, snapshot, mapping));
  root.append(matrixIdentifiabilityCard(devices, snapshot));

  // Re-apply the active filter to the freshly painted cards (the DOM is new
  // after every render).  Do this synchronously so there is no flash of the
  // full matrix before the filtered view settles.
  matrixApplyFilter();
}

export {
  renderMatrix
};
