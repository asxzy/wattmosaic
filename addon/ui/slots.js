/* ==========================================================================
   WattMosaic Engine panel — slots.js
   --------------------------------------------------------------------------
   A tiny mutable hub for references that used to share one IIFE scope.  The
   router dispatches to view renderers through these slots (so core does not
   statically import every view), and views cross-call through them too, which
   keeps the module dependency graph acyclic.  `main.js` is the only writer.
   ========================================================================== */

export const slots = {
  discoveryGeneration: 0,
  renderOverview: null,
  renderDevices: null,
  renderDevice: null,
  renderTopology: null,
  renderQuality: null,
  renderDiagnostics: null,
  renderMatrix: null,
  residualDecompositionCard: null,
  mappingMatrixCard: null,
  groupRangesCard: null,
  versionHistoryCard: null,
  suspectedChangesCard: null,
  eventEvidenceCard: null,
  discoverWindowSelect: null,
  topologyDiscoverSlot: null,
  runDiscovery: null,
  topologyLearningBanner: null,
  confirmTopology: null,
};
