/* ==========================================================================
   WattMosaic Engine panel — main.js
   --------------------------------------------------------------------------
   Native ES module; relative imports only so the same file works at "/" and
   under /api/hassio_ingress/<token>/.  No build step, no network beyond origin.
   ========================================================================== */

import { slots } from './slots.js';
import { applyRoute, el, get, hideBanner, notify, parseHash, pollTick, render, startPolling, state, stopPolling } from './core.js';
import { renderOverview } from './view-overview.js';
import { eventEvidenceCard, groupRangesCard, mappingMatrixCard, renderDevice, renderDevices, residualDecompositionCard, suspectedChangesCard, versionHistoryCard } from './view-devices.js';
import { confirmTopology, renderTopology } from './view-topology.js';
import { discoverWindowSelect, runDiscovery, topologyDiscoverSlot, topologyLearningBanner } from './view-discover.js';
import { renderQuality } from './view-quality.js';
import { renderDiagnostics } from './view-diagnostics.js';
import { renderMatrix } from './view-matrix.js';
import { matrixContext } from './matrix-model.js';

/* ---------------------------------------------------------------------- *
 * Wiring
 * ---------------------------------------------------------------------- */

// The static shell's <nav> is not part of this task's scope, so the Matrix
// entry is added here at boot.  It reuses the same [data-nav] contract the
// router already understands, so active-state handling needs no change.  If
// the shell later gains the link itself, this is a no-op.
function addMatrixNav() {
  const nav = document.querySelector('nav.nav');
  if (!nav || nav.querySelector('[data-nav="matrix"]')) return;
  const link = el('a', { href: '#/matrix', 'data-nav': 'matrix' }, 'Matrix');
  const diagnostics = nav.querySelector('[data-nav="diagnostics"]');
  if (diagnostics) diagnostics.insertAdjacentElement('afterend', link);
  else nav.append(link);
}

function wire() {
  addMatrixNav();
  window.addEventListener('hashchange', () => {
    applyRoute();
    if (state.view === 'overview') startPolling();
    else stopPolling();
  });

  document.getElementById('refresh')?.addEventListener('click', () => {
    render();
    notify('Refreshed', 'Pulled the latest values from the engine.', 'ok');
  });

  document.getElementById('banner-retry')?.addEventListener('click', () => {
    hideBanner();
    render();
  });

  document.addEventListener('visibilitychange', () => {
    if (document.hidden) return;
    // Touch the API on *every* resume, not just the overview.  The ingress
    // session is renewed per request, so a tab hidden past the Supervisor's
    // 15-minute window comes back expired; probing here makes the 401 (and the
    // reload that fixes it) happen immediately instead of on the next click.
    if (state.view === 'overview') pollTick();
    else get('/health').catch(() => {});
  });

  // Escape returns to the section list from a detail page.
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && state.view === 'device') {
      location.hash = '#/devices';
    }
  });
}


// Register the cross-module slots.  This is the only place that writes them;
// doing it before boot() keeps core's view dispatch and the views' cross-calls
// working with an acyclic import graph.
Object.assign(slots, {
  renderOverview,
  renderDevices,
  renderDevice,
  renderTopology,
  renderQuality,
  renderDiagnostics,
  renderMatrix,
  residualDecompositionCard,
  mappingMatrixCard,
  groupRangesCard,
  versionHistoryCard,
  suspectedChangesCard,
  eventEvidenceCard,
  discoverWindowSelect,
  topologyDiscoverSlot,
  runDiscovery,
  topologyLearningBanner,
  confirmTopology,
});

function boot() {
  wire();
  applyRoute();
  // The overview is the only view that consumes the poll.  Starting it
  // unconditionally meant a cold load straight onto #/topology kept an 8 s
  // /health poll running behind a slow request, competing for the same
  // connection and delaying the first paint.
  if (state.view === 'overview') startPolling();
  else stopPolling();
  pollTick();
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', boot, { once: true });
} else {
  boot();
}

// Export a couple of helpers for debugging from the browser console without
// exposing engine data through any other surface.
window.WattMosaicUI = Object.freeze({
  reload: () => render(),
  route: parseHash,
  // Matrix debug: inspect/toggle the tracing aids from the console.
  matrixDebug: {
    state: () => ({ ...state.matrix, trace: state.matrix.trace.slice() }),
    enable: (on = true) => { state.matrix.debug = Boolean(on); render(); },
    rawIds: (on = true) => { state.matrix.showRawIds = Boolean(on); render(); },
    filter: (text = '') => { state.matrix.filter = String(text); render(); },
    context: (mapping) => matrixContext(mapping, null),
  },
});

export {
  addMatrixNav,
  wire,
  boot
};
