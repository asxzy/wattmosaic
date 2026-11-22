/* ==========================================================================
   WattMosaic Engine panel — view-topology.js
   --------------------------------------------------------------------------
   Native ES module; relative imports only so the same file works at "/" and
   under /api/hassio_ingress/<token>/.  No build step, no network beyond origin.
   ========================================================================== */

import { slots } from './slots.js';
import { STRINGS, ago, badge, card, clear, describeError, el, emptyState, frag, get, label, notice, notify, percent, post, render, sourceTone, statCard, state, statusBadge, toneFor, watts } from './core.js';
import { suspectedChangesCard, versionHistoryCard } from './view-devices.js';

/* --- Topology --------------------------------------------------------- */

// The hierarchy is painted from GET /api/v1/topology/tree.  This file never
// derives the tree again — not from names, not from ct_id prefixes, not from
// entity ids.  A second derivation in the browser is exactly what used to
// produce a silently wrong tree, so the engine owns the hierarchy and the
// panel renders it verbatim.
//
// Contract 3.0 has no single "topology object" any more (the legacy
// GET /topology, /topology/nesting and POST /topology/confirm were removed at
// the cutover).  The view reads three 3.0 surfaces and joins them in
// ``topologyModel``:
//   * GET /topology/v3  — active version, typed edges (every lifecycle; the
//     PROVISIONAL ones are the old nesting suggestions) and the primary tree;
//   * GET /endpoints    — the measurement endpoints (the "CT channels");
//   * GET /topology/tree — the engine-owned hierarchy.
// ``/health`` carries ``details.topology.configured``: the only signal that
// decides "No topology configured" (no measurement endpoint at all).

const TREE_PATH = '/topology/tree';
const V3_PATH = '/topology/v3';
const ENDPOINTS_PATH = '/endpoints';

// ADR-0046: the reader currently being dragged.  Set on dragstart, cleared on
// dragend; module-level so the drop handlers on every row can read it without
// threading it through the renderer.
let draggedReader = null;
const CT_TONES = { main: 'accent', branch: 'info' };
// ADR-0029 retired the third CT role.  A payload from an older engine can
// still carry it; ``STRINGS`` above aliases it to "Branch CT" so the badge
// never title-cases the retired spelling into a different label, and the tone
// follows the alias — a branch row in every respect.  The retired spelling is
// deliberately not repeated here: the alias key is the only occurrence AC-1
// allows in the panel (see ``tools/dev/check_panel.py``).
const ctTone = (type) => {
  const key = String(type || '');
  if (CT_TONES[key]) return CT_TONES[key];
  return STRINGS[key] === 'Branch CT' ? CT_TONES.branch : 'mute';
};

const topologyHead = (topology) =>
  el('div', { class: 'page-head' }, [
    el('div', {}, [
      el('h1', {}, 'Topology'),
      el('p', { class: 'lede' },
        'The electrical model the engine solves against: panels, the circuits they feed, and the devices attributed to each circuit.'),
    ]),
    el('div', { class: 'head-actions' }, [
      // Discovery is an explicit request: it never runs on paint and never
      // writes anything on the first call.
      slots.discoverWindowSelect(),
      el('button', {
        class: 'btn',
        id: 'topology-discover-run',
        type: 'button',
        onclick: () => slots.runDiscovery(),
        disabled: state.discover.running ? true : null,
      }, state.discover.running ? 'Discovering…' : 'Discover devices & topology'),
      // ADR-0043: topology is dynamic, with no confirmation write.  Pinning an
      // edge (POST /topology/edges/{id}/pin) is the only explicit freeze.  This
      // button only moves focus to the edges card; it never writes.
      topology && (topology.edges || []).length
        ? el('button', {
          class: 'btn',
          type: 'button',
          onclick: reviewTopologyEdges,
          title: 'Pin edges one by one in the edges card; the active topology already drives results',
        }, 'Review topology edges')
        : null,
    ]),
  ]);

// Painted synchronously, before the first await.  Without it a cold route
// showed a blank, zero-height viewport for the whole request.
const topologySkeleton = () =>
  el('div', { id: 'topology-loading', class: 'topo-loading' }, [
    el('p', { class: 'stat-hint', role: 'status' }, 'Loading the electrical model…'),
    el('div', { class: 'grid cols-4', style: 'margin-bottom:16px' },
      Array.from({ length: 4 }, () => el('div', { class: 'skeleton h-stat' }))),
    el('div', { class: 'topo-grid' }, [
      el('div', { class: 'card' }, [el('div', { class: 'skeleton h-card' })]),
      el('div', { class: 'card' }, [el('div', { class: 'skeleton h-card' })]),
    ]),
  ]);

async function renderTopology(root, signal) {
  // 1. First paint, synchronously.  The page head and skeleton go up before
  //    any request so the user sees a loading state immediately.
  clear(root);
  root.append(topologyHead(null));
  root.append(topologySkeleton());

  // 2. Every request here is a cheap read of persisted state; they run
  //    together.  /status carries the learning state the banner needs and
  //    /health carries ``details.topology.configured``.
  const settle = (promise) => promise.then(
    (data) => ({ ok: true, data, err: null }),
    (err) => ({ ok: false, data: null, err })
  );
  const [v3Result, endpointsResult, treeResult, status, health] = await Promise.all([
    settle(get(V3_PATH, signal)),
    settle(get(ENDPOINTS_PATH, signal)),
    settle(get(TREE_PATH, signal)),
    get('/status', signal).catch(() => null),
    get('/health', signal).catch(() => null),
  ]);
  if (signal.aborted) return;

  const tree = treeResult.ok && treeResult.data && Array.isArray(treeResult.data.roots)
    ? treeResult.data
    : null;
  const topology = topologyModel({
    v3: v3Result.ok ? v3Result.data : null,
    endpoints: endpointsResult.ok ? endpointsResult.data : null,
    tree,
    health,
  });

  clear(root);
  root.append(topologyHead(topology.configured === false ? null : topology));

  // "No topology configured" means the engine has no measurement endpoint at
  // all — exactly ``health.details.topology.configured === false``.  An
  // empty 3.0 topology (endpoints, no edges yet) is a real topology and is
  // rendered below with an explicit "still being learned" state.
  if (topology.configured === false) {
    root.append(
      emptyState(
        'No topology configured',
        'The engine has no measurement endpoints yet, so it cannot disaggregate. Configure the WattMosaic integration (or run discovery); results stay unavailable until then.',
        { tone: 'warn' }
      )
    );
    return;
  }

  // Neither the endpoints nor the topology read answered: say so, with the
  // real error, instead of guessing "not configured".
  if (topology.configured === null && !v3Result.ok && !endpointsResult.ok) {
    root.append(
      notice(
        'Topology unavailable',
        `The engine did not return its topology (${describeError(v3Result.err || endpointsResult.err)}). Nothing was changed; try again.`,
        'bad'
      )
    );
    return;
  }

  const learningBanner = slots.topologyLearningBanner(status, topology);
  if (learningBanner) root.append(learningBanner);
  root.append(topologySummary(topology, tree));
  const empty = noConfirmedEdgesNotice(topology);
  if (empty) root.append(empty);
  if (!tree) root.append(topologyFallbackNotice(treeResult.err));

  // The hierarchy is the subject of this tab, so it gets the full width: a
  // tree squeezed into half a page forces every ct_id to wrap.
  root.append(topologyHierarchyCard(topology, tree));

  // Additive B4-UIb cards: the topology's version and any suspected
  // configuration changes the audit pass detected.  Both read only payloads
  // already fetched for this view (/topology/v3 and /status).
  root.append(versionHistoryCard(topology, null, null));
  root.append(suspectedChangesCard(status, null));

  // Placement discovery renders into its own slot; it is only populated when
  // the user asks for it (or when a previous answer is cached in state).
  root.append(slots.topologyDiscoverSlot());

  // The measurement endpoints, and the typed edges with their lifecycle.
  // PROVISIONAL edges are the engine's nesting suggestions (they replace the
  // removed /topology/nesting); Accept / Pin / Reject write only on a click.
  root.append(
    el('div', { class: 'topo-grid' }, [
      topologyChannelsCard(topology, endpointsResult.ok ? null : endpointsResult.err),
      v3Result.ok
        ? v3EdgesCard(v3TopologyModel(v3Result.data))
        : card('Typed topology edges (v3)', [
          el('p', { class: 'stat-hint' },
            `GET /api/v1/topology/v3 failed (${describeError(v3Result.err)}). The hierarchy above is still usable; no edge is promoted or written in the meantime.`),
        ], { id: 'topology-edges' }),
    ])
  );

  const notes = (tree && tree.notes) || [];
  if (notes.length) {
    root.append(
      card(
        'Engine notes',
        el('ul', { class: 'explain' },
          notes.map((note) =>
            el('li', {}, [el('span', { class: 'marker' }, '·'), el('span', {}, note)])))
      )
    );
  }
}

function topologyFallbackNotice(err) {
  const missing = Boolean(err && err.status === 404);
  const text = missing
    ? 'This engine does not serve /api/v1/topology/tree (HTTP 404), so the engine-owned hierarchy cannot be drawn. The hierarchy card falls back to containment from structural CONTAINS edges only, and devices are not placed.'
    : `The hierarchy could not be loaded (${describeError(err)}). The hierarchy card falls back to containment from structural CONTAINS edges only, and devices are not placed.`;
  return notice('Hierarchy unavailable', text, 'warn');
}

/* --- The 3.0 topology model (pure, node-testable) ---------------------- */

const _absent = (value) => value === null || value === undefined;
const _edgeText = (value) => (_absent(value) || value === '' ? null : String(value));
const _lower = (value) => String(value || '').toLowerCase();

// An edge still in force: not rejected or retired, and not closed.
const edgeInForce = (edge) =>
  Boolean(edge)
  && !['rejected', 'retired'].includes(_lower(edge.lifecycle))
  && _absent(edge.valid_until);

// ADR-0043: there is no topology confirmation.  An edge is structural when
// its published state is STRUCTURAL (legacy "confirmed" is accepted for old
// payloads) or a user pinned it.  Pin is the only way to freeze an edge.
const edgeStructural = (edge) =>
  edgeInForce(edge)
  && (['structural', 'confirmed'].includes(_lower(edge.lifecycle)) || edge.pinned === true);

// "structural" when any active edge is STRUCTURAL or pinned, else "candidate".
const topologyStatusFromEdges = (edges) =>
  ((Array.isArray(edges) ? edges : []).some(edgeStructural) ? 'structural' : 'candidate');

// GET /endpoints answers ``{endpoints: [...]}``; tolerate a bare list.
const endpointList = (payload) => {
  if (Array.isArray(payload)) return payload;
  if (payload && Array.isArray(payload.endpoints)) return payload.endpoints;
  return null;
};

const latestRevision = (endpoint) => {
  const revisions = Array.isArray(endpoint && endpoint.revisions) ? endpoint.revisions : [];
  return revisions.reduce(
    (best, rev) => (!best || Number(rev.revision) > Number(best.revision) ? rev : best),
    null
  );
};

// child endpoint -> parent endpoint, from structural CONTAINS edges only.  A
// PROVISIONAL edge is a soft suggestion and never places a node (HB §16).
function containsParents(edges) {
  const parents = new Map();
  for (const edge of Array.isArray(edges) ? edges : []) {
    if (_lower(edge && edge.relation) !== 'contains' || !edgeStructural(edge)) continue;
    const parent = _edgeText(edge.source_endpoint_id);
    const child = _edgeText(edge.target_endpoint_id);
    if (parent && child && parent !== child && !parents.has(child)) parents.set(child, parent);
  }
  return parents;
}

// One measurement endpoint as the channel row the cards render.  The CT role
// is read from the declared scope and the structural CONTAINS edges; it is
// never guessed from a name or an id.
function endpointChannel(endpoint, parents, hasChildren) {
  const id = String(endpoint.endpoint_id);
  const rev = latestRevision(endpoint) || {};
  const parent = parents.get(id) || null;
  const scope = _lower(endpoint.scope);
  const ctType = scope === 'panel_feed' || (!parent && hasChildren.has(id))
    ? 'main'
    : parent || scope === 'circuit'
      ? 'branch'
      : 'unknown';
  return {
    ct_id: id,
    name: _edgeText(endpoint.display_name),
    entity_id: _edgeText(endpoint.entity_id),
    kind: _edgeText(endpoint.kind),
    scope: _edgeText(endpoint.scope),
    scope_status: _edgeText(endpoint.scope_status),
    ct_type: ctType,
    phase: _edgeText(rev.phase) || 'unknown',
    polarity: _edgeText(rev.polarity) || 'normal',
    enabled: endpoint.enabled !== false,
    parent_ct_id: parent,
  };
}

// health.details.topology.configured is authoritative; without it, "has at
// least one endpoint" is the same rule.  ``null`` means "cannot tell".
function topologyConfigured(health, endpoints) {
  const details = health && health.details && health.details.topology;
  if (details && typeof details.configured === 'boolean') return details.configured;
  return Array.isArray(endpoints) ? endpoints.length > 0 : null;
}

/**
 * Join the 3.0 reads into the one object the topology cards render.
 *
 * ``v3`` is GET /topology/v3, ``endpoints`` GET /endpoints, ``tree`` GET
 * /topology/tree and ``health`` GET /health; any of them may be null.
 */
function topologyModel({ v3 = null, endpoints = null, tree = null, health = null } = {}) {
  const edges = v3 && Array.isArray(v3.edges) ? v3.edges : [];
  const list = endpointList(endpoints);
  const parents = containsParents(edges);
  const hasChildren = new Set(parents.values());
  const channels = (list || [])
    .filter((ep) => ep && !_absent(ep.endpoint_id))
    .map((ep) => endpointChannel(ep, parents, hasChildren));
  const healthTopology = (health && health.details && health.details.topology) || {};
  const stats = (tree && tree.stats) || {};
  return {
    configured: topologyConfigured(health, list),
    status: topologyStatusFromEdges(edges),
    version: v3 && !_absent(v3.version) ? v3.version : null,
    version_id: (v3 && _edgeText(v3.version_id)) || _edgeText(healthTopology.version),
    created_at: null,
    channels,
    endpoint_count: list ? list.length : (Number(healthTopology.endpoints) || 0),
    edges,
    edge_count: edges.length,
    active_edge_count: edges.filter(edgeInForce).length,
    structural_edge_count: edges.filter(edgeStructural).length,
    device_count: !_absent(stats.devices) ? Number(stats.devices)
      : (!_absent(healthTopology.devices) ? Number(healthTopology.devices) : null),
    primary_display_tree: v3 && Array.isArray(v3.primary_display_tree) ? v3.primary_display_tree : [],
  };
}

// An empty 3.0 topology is normal on a fresh install: the endpoints exist but
// no containment edge has become structural yet.  ADR-0043 replaces the old
// "No confirmed edges yet" notice with the honest learning message.
function noConfirmedEdgesNotice(topology) {
  if (!topology || topology.configured === false) return null;
  if ((topology.structural_edge_count || 0) > 0) return null;
  const endpoints = topology.endpoint_count || (topology.channels || []).length;
  const suggested = topology.active_edge_count || 0;
  return notice(
    'Topology is still being learned from events',
    `${endpoints} measurement endpoint${endpoints === 1 ? '' : 's'} known, but no containment edge is structural yet, so the hierarchy lists them unplaced. `
      + (suggested
        ? `${suggested} suggested edge${suggested === 1 ? '' : 's'} can be pinned or rejected in the edges card below.`
        : 'Run discovery, or let the engine propose edges from events; suggestions appear in the edges card below.'),
    'info'
  );
}

// Moves focus to the edges card.  It never writes: in 3.0 an edge is
// confirmed one at a time with POST /topology/edges/{id}/pin, on a click.
function reviewTopologyEdges() {
  const target = document.getElementById('topology-edges');
  if (target && typeof target.scrollIntoView === 'function') {
    target.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
  if (target && typeof target.focus === 'function') {
    // A section is not focusable by default; -1 lets focus land without
    // adding the card to the tab order.
    target.setAttribute('tabindex', '-1');
    target.focus({ preventScroll: true });
  }
}

function topologySummary(topology, tree) {
  const channels = topology.channels || [];
  const counts = { main: 0, branch: 0, unknown: 0 };
  for (const ch of channels) {
    counts[ch.ct_type] = (counts[ch.ct_type] || 0) + 1;
  }
  const stats = (tree && tree.stats) || null;
  const panels = stats && stats.panels !== undefined ? String(stats.panels) : '—';
  const circuits = stats && stats.circuits !== undefined
    ? String(stats.circuits)
    : String(counts.branch + counts.unknown);
  const devices = stats && stats.devices !== undefined
    ? String(stats.devices)
    : topology.device_count !== undefined && topology.device_count !== null
      ? String(topology.device_count)
      : String((topology.devices || []).length);
  const unplaced = stats
    ? (Number(stats.unplaced_devices) || 0) + (Number(stats.unplaced_circuits) || 0)
    : 0;

  // OD-51: a candidate topology is dynamic and authoritative for inference
  // and learning — never a gate.  Show its published confidence as status,
  // not as a "confirm to proceed" warning.
  const configured =
    topology.status === 'structural'
      ? null
      : notice(
          STRINGS.topology_dynamic + ' — authoritative for inference and learning',
          'The current topology is learned from events and follows the evidence in both directions. Each edge shows its state and strength; pin an edge in the card below to freeze it.',
          'info'
        );

  return frag([
    configured,
    el('div', { class: 'grid cols-5', style: 'margin-bottom:16px' }, [
      statCard('Panels', panels, { hint: panels === '1' ? 'distribution board' : 'distribution boards' }),
      statCard('Circuits', circuits, {
        hint: `${counts.main} feed · ${counts.branch} branch · ${counts.unknown} unknown`,
      }),
      statCard('Devices', devices, {
        hint: unplaced ? `${unplaced} unplaced — shown, never dropped` : 'each placed once',
        tone: unplaced ? 'warn' : null,
      }),
      statCard('Status', label(topology.status), { tone: toneFor(topology.status) }),
      statCard('Version', String(topology.version ?? '—'), {
        hint: topology.created_at ? ago(topology.created_at) : (topology.version_id || ''),
      }),
    ]),
  ]);
}

/* --- The hierarchy ---------------------------------------------------- *
 * Real nested markup: ul.hier-list > li.hier-node > .hier-row (+ a nested
 * ul for children).  Connector lines are CSS borders on the li, so the tree
 * reads as a tree without SVG and without any inline style.
 * ---------------------------------------------------------------------- */

const nodeKey = (node) =>
  String((node && (node.id || node.ct_id || node.device_id)) || (node && node.label) || 'node');

const BADGE_TONES = new Set(['ok', 'warn', 'bad', 'info', 'mute', 'accent']);
// The engine emits ``muted``; the whitelist historically said ``mute``.  The
// two are the same tone, so accept both instead of silently demoting every
// ``muted`` pill to an unstyled fallback (contract §5.3).
const badgeTone = (raw) => {
  const tone = String(raw || 'mute');
  const normalised = tone === 'muted' ? 'mute' : tone;
  // An unknown tone would render an unstyled badge: fall back to the neutral
  // one rather than shipping a pill with no border or colour.
  return BADGE_TONES.has(normalised) ? normalised : 'mute';
};

const badgeItems = (node) =>
  (Array.isArray(node.badges) ? node.badges : [])
    .filter((item) => item && item.text !== undefined && item.text !== null);

const badgeNode = (item) =>
  badge(String(item.text), badgeTone(item.tone), {
    title: item.title === undefined || item.title === null ? null : String(item.title),
  });

const nodeBadges = (node) => badgeItems(node).map(badgeNode);

/* --- Measurement honesty (contract §5.2/§5.3) ------------------------- */

const MEASUREMENT_STATES = new Set(['measured', 'inferred', 'prior', 'none']);

//: The reason the engine gives for *not* measuring a fixture, in plain words.
const NOT_MEASURED_REASONS = {
  below_ct_resolution: 'The step is below what this CT can resolve',
  scene_group: 'It toggles with a scene group, so the step cannot be attributed',
  no_transition: 'It never transitioned in the measurement window',
};

//: When the engine names a measurement state but no reason, the state itself
//: is still an honest explanation — better than a generic shrug.
const MEASUREMENT_TITLES = {
  inferred: 'Inferred from co-movement; not measured directly on a CT',
  prior: 'Uses a configured prior, not a measurement',
  none: 'The engine has no measurement for this fixture',
};

const notMeasuredTitle = (reason, measurement, hasValue = true) => {
  if (!hasValue && String(measurement || '') === 'measured') {
    return 'Declared as measured, but the engine has not produced a value yet';
  }
  return NOT_MEASURED_REASONS[String(reason || '')]
    || MEASUREMENT_TITLES[String(measurement || '')]
    || 'The engine has no measurement for this fixture';
};

const hasPowerValue = (node) =>
  Boolean(node) && node.latest_power_w !== null && node.latest_power_w !== undefined
    && Number.isFinite(Number(node.latest_power_w));

/**
 * The measurement vocabulary for a device node.
 *
 * An old engine emits no ``measurement`` key at all.  The panel must not
 * invent a measurement claim it never made, so the fallback is the contract's
 * own rule (§5.4 item 4): a node with a ``source`` counts as measured.  The
 * second half — *and it has a value to print* — is what fixes defect 4: a node
 * with no wattage is "not measured" rather than a bare ``—``.
 */
const measurementOf = (node) => {
  if (!node) return 'none';
  if (node.measurement !== undefined && node.measurement !== null) {
    const value = String(node.measurement);
    return MEASUREMENT_STATES.has(value) ? value : 'none';
  }
  const sourced = node.source !== null && node.source !== undefined;
  return sourced && hasPowerValue(node) ? 'measured' : 'none';
};

//: One rule for the row and the circuit summary, so the same fixture is never
//: "measured" in one place and "unmeasured" in the other.
const isMeasuredNode = (node) => measurementOf(node) === 'measured';

// The engine's device ``subtitle`` used to be dropped outright.  It is now the
// evidence line.  Suppress only what carries no information: a repeat of the
// row's own name, the phase already shown as a badge, or a structural
// placeholder ("unknown", "unknown phase") — never real evidence text.
const STRUCTURAL_SUBTITLES = new Set(['unknown', 'unknown phase', 'n/a']);
const deviceSubtitle = (node) => {
  if (node.subtitle === null || node.subtitle === undefined) return '';
  const text = String(node.subtitle).trim();
  if (!text) return '';
  const folded = text.toLowerCase();
  if (STRUCTURAL_SUBTITLES.has(folded)) return '';
  if (text === String(node.label || '') || text === String(node.device_id || '')) return '';
  const phase = String(node.phase || '');
  if (phase && (folded === phase.toLowerCase() || folded === label(phase).toLowerCase())) return '';
  return text;
};

// The first evidence string is a better subtitle than an empty row: it says
// *why* the engine believes what it shows.
const firstEvidence = (node) =>
  (Array.isArray(node.evidence) ? node.evidence : [])
    .map((item) => (item === null || item === undefined ? '' : String(item).trim()))
    .filter(Boolean)[0] || '';

const deviceEvidenceLine = (node) => deviceSubtitle(node) || firstEvidence(node);

//: The engine emits ``topology_status`` as a badge whose text equals the
//: field.  Matching by identity (not by tone or a keyword) means an unrelated
//: "ok"-toned badge is never swallowed along with it.
const isWiringStatusBadge = (item, node) => {
  const status = node && node.topology_status;
  if (!status) return false;
  return String(item.text).toLowerCase() === String(status).toLowerCase();
};

/**
 * One compact chip for a circuit's direct children (contract §5.4.4).
 *
 * ``measurement`` is the engine's vocabulary; the count never guesses beyond
 * it.  An old engine has no key, so a child with a source counts as measured —
 * that is the same rule the device row applies.
 */
function circuitChildSummary(node) {
  const children = Array.isArray(node.children) ? node.children : [];
  const devices = children.filter((child) => child && child.kind === 'device');
  if (!devices.length) return null;
  const measured = devices.filter(isMeasuredNode).length;
  const unmeasured = devices.length - measured;
  const noun = devices.length === 1 ? 'device' : 'devices';

  // The chip uses the engine's own word when every unmeasured child really is
  // an inference — the common case.  A `prior`, `none`, or a payload with no
  // ``measurement`` key at all is not an inference, so the chip falls back to
  // the neutral "unmeasured" rather than relabelling it.
  const kinds = devices
    .filter((child) => !isMeasuredNode(child))
    .map((child) => (child.measurement !== undefined && child.measurement !== null
      ? String(child.measurement)
      : 'unmeasured'));
  const bucket = kinds.every((kind) => kind === 'inferred') ? 'inferred' : 'unmeasured';
  const text = unmeasured
    ? `${devices.length} ${noun} · ${measured} measured · ${unmeasured} ${bucket}`
    : `${devices.length} ${noun}`;

  const detail = unmeasured
    ? `${measured} of ${devices.length} fixtures on this circuit have a measured wattage; ${unmeasured} are ${bucket}.`
    : `All ${devices.length} fixtures on this circuit have a measured wattage.`;
  // The exact vocabulary the engine emitted (inferred / prior / none) belongs
  // in the tooltip, not in a chip that must stay one short phrase.
  const breakdown = unmeasured
    ? ` (${Object.entries(kinds.reduce((acc, kind) => {
        acc[kind] = (acc[kind] || 0) + 1;
        return acc;
      }, {})).map(([kind, count]) => `${count} ${kind}`).join(', ')})`
    : '';
  return el('span', { class: 'hier-count', title: detail + breakdown }, text);
}

// Contract §1.2 R5: a group with no children is omitted entirely.  The
// engine already does this, but a group is pure structure, so hiding an
// empty one costs nothing and keeps a stale payload from drawing a bare
// "DEVICES 0" heading.
const prunedNodes = (nodes) =>
  nodes
    .filter((node) => !(node.kind === 'group' && !(node.children || []).length))
    .map((node) => (Array.isArray(node.children) && node.children.length
      ? { ...node, children: prunedNodes(node.children) }
      : node));

const hierarchyNodes = (topology, tree) =>
  tree && Array.isArray(tree.roots) ? prunedNodes(tree.roots) : legacyRoots(topology);

// Fallback for an engine without /topology/tree: the old CT containment view,
// expressed in the same node shape so one renderer serves both.  This is the
// only place that looks at parent_ct_id, and it is explicitly labelled.
function legacyRoots(topology) {
  const channels = [...(topology.channels || [])];
  const byId = new Map(channels.map((ch) => [ch.ct_id, ch]));
  const children = new Map();
  const roots = [];
  for (const ch of channels) {
    const parent = ch.parent_ct_id;
    if (parent && parent !== ch.ct_id && byId.has(parent)) {
      if (!children.has(parent)) children.set(parent, []);
      children.get(parent).push(ch);
    } else {
      roots.push(ch);
    }
  }

  const seen = new Set();
  const build = (ch) => {
    seen.add(ch.ct_id);
    const kids = (children.get(ch.ct_id) || [])
      .sort((a, b) => String(a.ct_id).localeCompare(String(b.ct_id)))
      .map((kid) => (seen.has(kid.ct_id) ? null : build(kid)))
      .filter(Boolean);
    return {
      id: `legacy:circuit:${ch.ct_id}`,
      kind: 'circuit',
      label: ch.name && ch.name !== ch.phase ? ch.name : ch.ct_id,
      subtitle: ch.entity_id || ch.ct_id,
      badges: [],
      ct_id: ch.ct_id,
      ct_type: ch.ct_type,
      phase: ch.phase,
      enabled: ch.enabled,
      alias_of_ct_id: ch.alias_of_ct_id,
      polarity: ch.polarity,
      topology_status: ch.topology_status,
      latest_power_w: null,
      children: kids,
    };
  };

  return roots
    .sort((a, b) => String(a.ct_id).localeCompare(String(b.ct_id)))
    .map((ch) => (seen.has(ch.ct_id) ? null : build(ch)))
    .filter(Boolean);
}

// Default: the first panel is open to depth 2 — the panel, its group nodes
// and the circuits inside them are visible, while every deeper branch starts
// collapsed.  Other panels start collapsed.
function defaultOpenSet(nodes) {
  const ids = new Set();
  const first = nodes[0];
  if (!first) return ids;
  const walk = (node, depth) => {
    if (depth >= 2) return;
    if ((node.children || []).length) ids.add(nodeKey(node));
    for (const child of node.children || []) walk(child, depth + 1);
  };
  walk(first, 0);
  return ids;
}

const isExpanded = (key, defaults) => {
  if (state.tree.open.has(key)) return true;
  if (state.tree.closed.has(key)) return false;
  return defaults.has(key);
};

const nodeText = (node) =>
  [node.label, node.subtitle, node.id, node.ct_id, node.device_id, node.ct_type, node.phase]
    .concat(Array.isArray(node.feed_ct_ids) ? node.feed_ct_ids : [])
    .concat(Array.isArray(node.badges) ? node.badges.map((b) => (b && b.text) || '') : [])
    .filter((part) => part !== null && part !== undefined && part !== '')
    .join(' ')
    .toLowerCase();

const nodeMatches = (node, query) => nodeText(node).includes(query);

// Keep a node when it matches, or when any descendant does, so a filtered
// tree still shows the path down to the match.
const filterSubtree = (node, query) => {
  const kids = (node.children || [])
    .map((child) => filterSubtree(child, query))
    .filter(Boolean);
  if (!nodeMatches(node, query) && kids.length === 0) return null;
  return { node, kids };
};

const toEntry = (node) => ({
  node,
  kids: (node.children || []).map(toEntry),
});

/* --- Circuit moves: pure helpers (ADR-0046, node-testable) -------------- *
 * The engine owns topology.  These helpers only answer "may the user move
 * this circuit there?" from the tree the engine sent; they never derive a
 * parent or mutate the tree.  Devices are leaves and never move or accept a
 * drop.  ``both_legs`` readers appear more than once, so reader ids are
 * deduplicated everywhere.
 * ---------------------------------------------------------------------- */

// Every reader_id below ``readerId``, across all occurrences.  Device nodes
// are ignored: they are not circuits and cannot be move targets.
function descendantReaders(nodes, readerId) {
  const out = new Set();
  const walk = (list, ancestorMatched) => {
    for (const node of list || []) {
      if (!node || typeof node !== 'object') continue;
      const matches = node.kind === 'circuit'
        && String(node.reader_id || '') === String(readerId);
      const under = ancestorMatched || matches;
      if (under && !matches && node.kind === 'circuit' && node.reader_id) {
        out.add(String(node.reader_id));
      }
      walk(node.children, under);
    }
  };
  walk(nodes, false);
  return out;
}

// The reader's current parent, if the tree places it under a circuit.  A
// circuit may occur more than once (both_legs), so collect every parent.
function parentReaders(nodes, readerId) {
  const parents = new Set();
  const walk = (list, parent) => {
    for (const node of list || []) {
      if (!node || typeof node !== 'object') continue;
      const isCircuit = node.kind === 'circuit' && node.reader_id;
      const next = isCircuit ? String(node.reader_id) : parent;
      if (isCircuit && String(node.reader_id) === String(readerId) && parent) {
        parents.add(parent);
      }
      walk(node.children, next);
    }
  };
  walk(nodes, null);
  return parents;
}

// Every circuit reader in the tree, deduped, excluding itself, its descendants
// and its current parent(s).  Returns [{reader_id, label}] sorted by label.
function validMoveTargets(nodes, readerId) {
  const descendants = descendantReaders(nodes, readerId);
  const parents = parentReaders(nodes, readerId);
  const seen = new Map();
  const walk = (list) => {
    for (const node of list || []) {
      if (!node || typeof node !== 'object') continue;
      if (node.kind === 'circuit' && node.reader_id) {
        const id = String(node.reader_id);
        if (id !== String(readerId) && !descendants.has(id) && !parents.has(id) && !seen.has(id)) {
          seen.set(id, { reader_id: id, label: String(node.label || id) });
        }
      }
      walk(node.children);
    }
  };
  walk(nodes);
  return [...seen.values()].sort((a, b) => a.label.localeCompare(b.label));
}

// True when ``targetReaderId`` is null (top level) or an allowed parent.
function canDropOn(nodes, readerId, targetReaderId) {
  if (targetReaderId === null || targetReaderId === undefined || targetReaderId === '') return true;
  return validMoveTargets(nodes, readerId).some((t) => t.reader_id === String(targetReaderId));
}

// The only write in the move flow: POST /topology/circuits/{id}/parent.  A
// null parent sends {parent: null} and means "make this a top-level circuit".
async function moveCircuit(readerId, parentId, http = post) {
  return http(`/topology/circuits/${encodeURIComponent(String(readerId))}/parent`, {
    parent: parentId === undefined ? null : parentId,
  });
}

function topologyHierarchyCard(topology, tree) {
  const body = el('div', {
    id: 'topology-hierarchy-body',
    class: 'hier',
    // The view container is aria-live="polite"; a tree of a hundred nodes
    // must not be read out on every repaint.
    'aria-live': 'off',
  });

  // One short announcement per user action, instead of the whole tree.
  const status = el('p', { class: 'hier-status', role: 'status' });
  const announce = (text) => { status.textContent = text; };

  // The engine's tree is cloned once — right here — and every paint reuses
  // this copy.  Re-deriving it per repaint cloned the whole tree several
  // times per keystroke and per toggle for no benefit: the payload does not
  // change while the card is on screen.
  const nodes = hierarchyNodes(topology, tree);

  // ADR-0046: drag-and-drop is only available when the
  // hierarchy came from the engine.  The legacy fallback stays read-only.
  const canMove = Boolean(tree);
  const labelOf = (readerId) => {
    let found = String(readerId);
    const walk = (list) => {
      for (const node of list || []) {
        if (!node || typeof node !== 'object') continue;
        if (node.kind === 'circuit' && String(node.reader_id) === String(readerId)) {
          found = String(node.label || node.reader_id);
          return;
        }
        walk(node.children);
      }
    };
    walk(nodes);
    return found;
  };

  const clearHighlights = () => {
    body.classList?.remove?.('is-dragging');
    for (const row of body.querySelectorAll?.('.hier-row') || []) {
      row.classList?.remove?.('drop-ok');
      row.classList?.remove?.('is-dragging');
    }
    dropzone.classList?.remove?.('drop-ok');
  };

  const endDrag = () => {
    draggedReader = null;
    clearHighlights();
  };

  const confirmAndMove = (readerId, parentId) => {
    const childLabel = labelOf(readerId);
    const parentLabel = parentId === null || parentId === undefined || parentId === ''
      ? ''
      : labelOf(parentId);
    const question = `Move ${childLabel} under ${parentLabel || 'the top level'}? `
      + 'This overrides what the engine inferred.';
    if (!window.confirm(question)) return;
    moveCircuit(readerId, parentId || null).then(
      () => {
        endDrag();
        notify('Circuit moved', 'The topology was updated; inference will not undo this.');
        render();
      },
      (err) => notify('Could not move circuit', describeError(err), 'bad')
    );
  };

  const moveCtx = canMove ? {
    nodes,
    confirmAndMove,
    endDrag,
    clearHighlights,
    setDragged: (readerId) => {
      draggedReader = readerId;
      body.classList?.add?.('is-dragging');
    },
    dragged: () => draggedReader,
  } : null;

  const dropzone = canMove
    ? el('div', {
      class: 'hier-dropzone',
      dataset: { target: 'root' },
      ondragover: (event) => {
        if (!draggedReader) return;
        if (canDropOn(nodes, draggedReader, null)) {
          event.preventDefault?.();
          dropzone.classList?.add?.('drop-ok');
        }
      },
      ondragleave: () => dropzone.classList?.remove?.('drop-ok'),
      ondrop: (event) => {
        event.preventDefault?.();
        const child = draggedReader;
        endDrag();
        if (child) confirmAndMove(child, null);
      },
    }, 'Drop here to make it a top-level circuit')
    : null;

  const repaint = (options) => {
    const focusKey = options && options.focusKey;
    const count = paintHierarchy(body, nodes, repaint, moveCtx);
    if (focusKey) {
      // The button that was clicked no longer exists: the whole list was
      // rebuilt.  Put focus back on its equivalent so keyboard use does not
      // drop to the document body after every toggle.
      const next = [...body.querySelectorAll('.hier-toggle')].find((b) => b.dataset.key === focusKey);
      if (next) next.focus({ preventScroll: true });
      announce(next
        ? `${next.getAttribute('aria-expanded') === 'true' ? 'Expanded' : 'Collapsed'}. ${count} rows visible.`
        : `${count} rows visible.`);
    }
    return count;
  };

  // Substantial trees are ~100 rows; rebuilding one per keystroke is wasted
  // work, and the user cannot type faster than a paint is useful.  120 ms is
  // long enough to coalesce a burst and short enough to feel immediate.
  let filterTimer = 0;
  const applyFilter = () => {
    filterTimer = 0;
    const count = repaint();
    const query = state.tree.filter.trim();
    announce(
      query
        ? `${count} rows match “${query}”.`
        : `${count} rows visible.`
    );
  };

  const filter = el('input', {
    type: 'search',
    class: 'hier-filter',
    placeholder: 'Filter circuits and devices…',
    'aria-label': 'Filter the hierarchy',
    value: state.tree.filter,
    oninput: (event) => {
      state.tree.filter = event.target.value;
      if (filterTimer) clearTimeout(filterTimer);
      filterTimer = setTimeout(applyFilter, 120);
    },
    // Enter applies immediately: a keyboard user should not have to wait out
    // the debounce to see the result of an explicit submit.
    onkeydown: (event) => {
      if (event.key !== 'Enter') return;
      event.preventDefault();
      if (filterTimer) clearTimeout(filterTimer);
      applyFilter();
    },
  });

  const expandAll = el('button', {
    class: 'btn btn-sm',
    type: 'button',
    onclick: () => {
      setAllExpanded(nodes, true);
      announce(`Expanded all. ${repaint()} rows visible.`);
    },
  }, 'Expand all');

  const collapseAll = el('button', {
    class: 'btn btn-sm',
    type: 'button',
    onclick: () => {
      setAllExpanded(nodes, false);
      announce(`Collapsed all. ${repaint()} rows visible.`);
    },
  }, 'Collapse all');

  const node = card('Hierarchy', [status, dropzone, body].filter(Boolean), {
    id: 'topology-hierarchy',
    actions: [filter, expandAll, collapseAll],
    foot: tree
      ? `Computed by the engine${tree.version !== undefined ? ` (contract v${tree.version})` : ''} at ${ago(tree.generated_at)}. Each device appears under the circuit it is mapped to. Drag a circuit onto another circuit (or the top-level zone) to correct its parent; your choice overrides inference.`
      : 'Fallback view: containment from structural CONTAINS edges only. Devices are not placed.',
  });

  // Paint in the same task as the card so the tree is never a frame late.
  const shown = paintHierarchy(body, nodes, repaint, moveCtx);
  // The layout actually used, recorded on the card so a bug report can name
  // it without re-deriving it from the markup.
  node.dataset.layout = tree ? 'tree' : 'legacy';
  announce(
    `${shown} rows shown.`
    + (tree ? '' : ' Hierarchy endpoint unavailable; showing CT containment.')
  );
  return node;
}

function setAllExpanded(nodes, open) {
  state.tree.open.clear();
  state.tree.closed.clear();
  const walk = (node) => {
    if ((node.children || []).length) {
      const key = nodeKey(node);
      if (open) state.tree.open.add(key);
      else state.tree.closed.add(key);
    }
    for (const child of node.children || []) walk(child);
  };
  for (const node of nodes) walk(node);
}

// ``onToggle(options)`` rebuilds the card.  It is supplied by the caller so
// the focus-restoring repaint lives in one place.  ``nodes`` is computed once
// by the caller; nothing here re-clones the engine's tree.  Returns the row
// count so the caller can announce it without another DOM query.
function paintHierarchy(body, nodes, onToggle, moveCtx = null) {
  if (!body) return 0;
  clear(body);

  const query = state.tree.filter.trim().toLowerCase();

  let entries;
  let forcedOpen = null;
  if (query) {
    entries = nodes.map((node) => filterSubtree(node, query)).filter(Boolean);
    forcedOpen = new Set();
    const mark = (entry) => {
      if (entry.kids.length) forcedOpen.add(nodeKey(entry.node));
      entry.kids.forEach(mark);
    };
    entries.forEach(mark);
  } else {
    entries = nodes.map(toEntry);
  }

  if (!entries.length) {
    body.append(
      emptyState(
        query ? 'No node matches' : 'No nodes yet',
        query ? `Nothing in this hierarchy matches “${state.tree.filter.trim()}”.` : 'This topology has no nodes to show.'
      )
    );
    return 0;
  }

  const ctx = {
    defaults: defaultOpenSet(nodes),
    forcedOpen,
    repaint: onToggle,
    moveCtx,
    // Incremented by ``renderEntry`` for every row actually appended, so the
    // row count costs nothing extra: no second DOM query, no second walk.
    count: 0,
  };

  body.append(
    el(
      'ul',
      { class: 'hier-list hier-root' },
      entries.map((entry) => renderEntry(entry, 0, ctx))
    )
  );
  return ctx.count;
}

function renderEntry(entry, depth, ctx) {
  const node = entry.node;
  const key = nodeKey(node);
  const hasKids = entry.kids.length > 0;
  const expanded = hasKids
    ? (ctx.forcedOpen ? ctx.forcedOpen.has(key) : isExpanded(key, ctx.defaults))
    : null;

  const item = el('li', { class: 'hier-node', dataset: { depth: String(depth) } }, [
    hierarchyRow(node, depth, expanded, key, ctx, entry.kids.length),
  ]);
  ctx.count += 1;

  if (hasKids && expanded) {
    item.append(
      el('ul', { class: 'hier-list' }, entry.kids.map((kid) => renderEntry(kid, depth + 1, ctx)))
    );
  }

  return item;
}

function hierarchyRow(node, depth, expanded, key, ctx, childCount) {
  const kind = String(node.kind || 'node');
  const band = placementBand(node);
  const moveCtx = ctx.moveCtx;
  const readerId = node.reader_id === undefined || node.reader_id === null
    ? null
    : String(node.reader_id);
  const movable = Boolean(moveCtx && kind === 'circuit' && readerId);
  const row = el('div', {
    class: 'hier-row',
    ...(movable ? { draggable: 'true', 'data-reader-id': readerId } : {}),
    dataset: {
      kind,
      depth: String(depth),
      ...(band ? { band } : {}),
      ...(node.structural === true ? { structural: 'true' } : {}),
    },
    ...(movable ? {
      ondragstart: (event) => {
        moveCtx.setDragged(readerId);
        event.dataTransfer?.setData?.('text/plain', readerId);
        if (event.dataTransfer) event.dataTransfer.effectAllowed = 'move';
        row.classList?.add?.('is-dragging');
      },
      ondragend: () => moveCtx.endDrag(),
      ondragover: (event) => {
        const dragged = moveCtx.dragged();
        if (!dragged || !canDropOn(moveCtx.nodes, dragged, readerId)) return;
        event.preventDefault?.();
        if (event.dataTransfer) event.dataTransfer.dropEffect = 'move';
        row.classList?.add?.('drop-ok');
      },
      ondragleave: () => {
        row.classList?.remove?.('drop-ok');
      },
      ondrop: (event) => {
        event.preventDefault?.();
        const dragged = moveCtx.dragged();
        moveCtx.endDrag();
        if (dragged) moveCtx.confirmAndMove(dragged, readerId);
      },
    } : {}),
  });

  if (expanded === null) {
    row.append(el('span', { class: 'hier-toggle-spacer', 'aria-hidden': 'true' }));
  } else {
    row.append(
      el('button', {
        class: 'hier-toggle',
        type: 'button',
        'aria-expanded': String(expanded),
        'aria-label': `${expanded ? 'Collapse' : 'Expand'} ${node.label || nodeKey(node)}`,
        dataset: { key },
        onclick: (event) => {
          const next = event.currentTarget.getAttribute('aria-expanded') !== 'true';
          if (next) {
            state.tree.open.add(key);
            state.tree.closed.delete(key);
          } else {
            state.tree.closed.add(key);
            state.tree.open.delete(key);
          }
          ctx.repaint({ focusKey: key });
        },
      })
    );
  }

  row.append(el('span', { class: 'hier-glyph', 'aria-hidden': 'true' }, GLYPHS[kind] || '·'));

  if (kind === 'group') {
    row.append(
      el('span', { class: 'hier-group-label' }, String(node.label || node.role || 'Group').toUpperCase())
    );
    row.append(el('span', { class: 'hier-count' }, String(childCount)));
  } else if (kind === 'panel') {
    row.append(el('span', { class: 'hier-label' }, node.label || node.panel_id || 'Panel'));
    // ``monitor`` is often the same words as the board name (a panel named
    // after its monitor); printing both reads as a stutter.
    if (node.monitor && String(node.monitor) !== String(node.label || '')) {
      row.append(el('span', { class: 'hier-sub' }, node.monitor));
    }
    if (node.is_service_entry) row.append(badge('Service entry', 'accent'));
    for (const item of nodeBadges(node)) row.append(item);
    const feeds = Array.isArray(node.feed_ct_ids) ? node.feed_ct_ids : [];
    if (feeds.length) {
      row.append(
        el('span', { class: 'hier-feeds', title: 'Feed CTs' },
          feeds.map((id) => el('span', { class: 'chip chip-ct' }, String(id))))
      );
    }
    if (node.subtitle && String(node.subtitle) !== String(node.label || '')) {
      row.append(el('span', { class: 'hier-sub' }, node.subtitle));
    }
  } else if (kind === 'circuit') {
    const name = node.label || node.ct_id || 'Circuit';
    row.append(el('span', { class: 'hier-label' }, name));
    // Only show the subtitle when it adds something: the engine may set both
    // to the ct_id, and printing it twice is pure noise.
    if (node.subtitle && String(node.subtitle) !== String(name)) {
      row.append(el('span', { class: 'hier-sub' }, node.subtitle));
    }
    row.append(badge(label(node.ct_type || 'unknown'), ctTone(node.ct_type)));
    if (node.phase && node.phase !== 'unknown') row.append(badge(label(node.phase), 'mute'));
    // Direct-child summary: how many fixtures sit here, and how many of them
    // the engine actually measured.  A circuit with no device children keeps
    // today's row rather than claiming "0 devices".
    const summary = circuitChildSummary(node);
    if (summary) row.append(summary);
    if (node.latest_power_w !== null && node.latest_power_w !== undefined) {
      row.append(el('span', { class: 'hier-power' }, watts(node.latest_power_w)));
    }
    if (node.enabled === false) row.append(badge('Disabled', 'bad'));
    if (node.alias_of_ct_id) row.append(badge(`alias of ${node.alias_of_ct_id}`, 'warn'));
    if (node.polarity === 'inverted') row.append(badge('Inverted', 'warn'));
    for (const chip of nodeChips(node)) row.append(chip);
    for (const item of nodeBadges(node)) row.append(item);
  } else if (kind === 'device') {
    const name = node.label || node.device_id || 'Device';
    const measurement = measurementOf(node);
    // ``measured`` also requires a number to print: a node the engine labels
    // measured but has no value for yet would otherwise put a bare dash in
    // the measured slot — defect 4.
    const measured = measurement === 'measured' && hasPowerValue(node);
    if (node.device_id) {
      row.append(
        el('a', { class: 'hier-link', href: `#/device/${encodeURIComponent(node.device_id)}` }, name)
      );
    } else {
      row.append(el('span', { class: 'hier-label' }, name));
    }
    if (node.phase && node.phase !== 'unknown') row.append(badge(label(node.phase), 'mute'));
    if (measured) {
      // A measured fixture: the wattage is the point of the row.
      row.append(el('span', { class: 'hier-power' }, watts(node.latest_power_w)));
      if (node.source) row.append(badge(label(node.source), sourceTone(node.source)));
      if (node.confidence !== null && node.confidence !== undefined) {
        row.append(el('span', { class: 'hier-conf' }, percent(node.confidence, 0)));
      }
    } else {
      // Not measured: an empty measured slot would read as "0 W", and a bare
      // em dash in the wattage column claims a value that does not exist.
      // Say so instead, with the engine's reason on the pill's title.
      row.append(
        badge('not measured', 'mute', {
          title: notMeasuredTitle(node.not_measured_reason, measurement, hasPowerValue(node)),
          dot: false,
        })
      );
    }
    const sub = deviceEvidenceLine(node);
    if (sub) row.append(el('span', { class: 'hier-sub' }, sub));
    // ``topology_status`` is the *wiring* claim (is this device declared on
    // these CTs?), not a measurement claim.  A green "confirmed" beside "not
    // measured" is exactly the contradiction the audit found, so a device the
    // engine could not measure drops the wiring-status pill altogether.  The
    // match is by identity — the badge whose text *is* ``topology_status`` —
    // so an unrelated badge (an alias, a disabled flag) still shows.
    for (const item of badgeItems(node)) {
      if (!measured && isWiringStatusBadge(item, node)) continue;
      row.append(badgeNode(item));
    }
  } else {
    row.append(el('span', { class: 'hier-label' }, node.label || nodeKey(node)));
    if (node.subtitle) row.append(el('span', { class: 'hier-sub' }, node.subtitle));
    for (const item of nodeBadges(node)) row.append(item);
  }

  return row;
}

// E6 §1: placement confidence is a display style, never a change to H.  The
// engine puts ``band`` and ``structural`` on each circuit node; the row records
// them as dataset attributes so the stylesheet can draw high links solid,
// medium dashed and low dotted.  Chips are text only (no inline style).
const PLACEMENT_BANDS = new Set(['high', 'medium', 'low', 'unplaced']);
const placementBand = (node) => {
  const band = String((node && node.band) || '');
  return PLACEMENT_BANDS.has(band) ? band : null;
};

const nodeChips = (node) =>
  (Array.isArray(node.chips) ? node.chips : [])
    .filter((chip) => chip && chip.text !== undefined && chip.text !== null)
    .map((chip) => badge(String(chip.text), badgeTone(chip.tone)));

const GLYPHS = { panel: '▤', group: '▫', circuit: '◉', device: '◆' };

function topologyChannelsCard(topology, err = null) {
  const channels = [...(topology.channels || [])].sort((a, b) => {
    const order = { main: 0, branch: 1, unknown: 2 };
    return (order[a.ct_type] ?? 9) - (order[b.ct_type] ?? 9) || String(a.ct_id).localeCompare(String(b.ct_id));
  });
  const empty = err
    ? emptyState('Endpoints unavailable', `GET /api/v1/endpoints failed (${describeError(err)}).`)
    : emptyState('No measurement endpoints', 'The engine has no measurement endpoints yet.');
  return el('section', { class: 'card', id: 'topology-channels' }, [
    el('div', { class: 'card-head' }, [
      el('h2', {}, 'Measurement endpoints'),
      el('span', { class: 'spacer' }),
      el('span', { class: 'stat-hint' }, `${channels.length} total`),
    ]),
    el(
      'div',
      { class: 'card-body flush' },
      channels.length
        ? channels.map((ch) =>
            el('div', { class: 'channel-row', dataset: { type: ch.ct_type } }, [
              el('span', { class: 'sw', 'aria-hidden': 'true' }),
              el('div', { class: 'ch-main' }, [
                // A CT whose name is just its phase ("l1") tells the reader
                // nothing; the id does.  This mirrors the pre-existing rule.
                el('div', { class: 'ch-name' },
                  ch.name && ch.name !== ch.phase ? ch.name : ch.ct_id),
                el('div', { class: 'ch-sub' }, ch.entity_id || ch.ct_id),
                ch.parent_ct_id
                  ? el('div', { class: 'ch-sub' }, `parent: ${ch.parent_ct_id}`)
                  : null,
              ]),
              el('div', { class: 'ch-tags' }, [
                badge(label(ch.ct_type), ctTone(ch.ct_type)),
                ch.kind && ch.kind !== 'ct' ? badge(label(ch.kind), 'mute') : null,
                badge(label(ch.phase), 'mute'),
                ch.enabled === false ? badge('Disabled', 'bad') : null,
                ch.scope === 'single_subject' && ch.scope_status && ch.scope_status !== 'confirmed'
                  ? statusBadge(ch.scope_status)
                  : null,
                ch.polarity === 'inverted' ? badge('Inverted', 'warn') : null,
              ]),
            ])
          )
        : empty
    ),
  ]);
}

/* ====================================================================== *
 *  U2 — v3 typed-edge topology (pure view helpers).
 * ---------------------------------------------------------------------- *
 *  GET /topology/v3 (T4) publishes typed edges with a lifecycle state.  Only
 *  a user action writes: accepting a PROVISIONAL suggestion or pinning any
 *  other edge POSTs /pin, rejecting POSTs /reject.  Nothing here mutates on
 *  render.  Each edge can link to the matching matrix columns for its
 *  reader(s).
 * ====================================================================== */

// Normalise a T4 topology edge into the fields the panel renders.  Lifecycle
// is published verbatim; an absent lifecycle is 'candidate', never promoted.
// The 3.0 field names (relation, source/target_endpoint_id) are read first;
// the older aliases are kept so a stale payload still renders.
function v3EdgeView(edge) {
  const e = edge || {};
  const lifecycle = _edgeText(e.lifecycle)
    || _edgeText(e.status)
    || (e.confirmed === true ? 'confirmed' : 'candidate');
  const edgeType = _edgeText(e.relation) || _edgeText(e.edge_type) || _edgeText(e.type) || 'unknown';
  const child = _edgeText(e.target_endpoint_id) || _edgeText(e.child_id) || _edgeText(e.child_device_id);
  const readers = Array.isArray(e.reader_ids)
    ? e.reader_ids.filter(Boolean).map(String)
    : [e.reader_id !== undefined ? e.reader_id : _edgeText(e.target_endpoint_id)]
      .filter((v) => !_absent(v)).map(String);
  const life = String(lifecycle).toLowerCase();
  const closed = !_absent(e.valid_until) || life === 'rejected' || life === 'retired';
  return {
    edge_id: _edgeText(e.edge_id) || _edgeText(e.id),
    edge_type: edgeType,
    lifecycle: life,
    parent_id: _edgeText(e.source_endpoint_id) || _edgeText(e.parent_id) || _edgeText(e.parent_device_id),
    child_id: child,
    reader_ids: readers,
    pinned: e.pinned === true,
    provisional: life === 'provisional',
    // ADR-0043: show the edge's dynamic state and recent support weight.
    state: life,
    strength: Number(e.strength !== undefined && e.strength !== null ? e.strength
      : (e.evidence && e.evidence.support_w) || 0),
    conflict_ratio: Number(e.conflict_ratio !== undefined && e.conflict_ratio !== null
      ? e.conflict_ratio : ((e.evidence && e.evidence.conflict_ratio) || 0)),
    // A stored inferred edge outside the active version.  Shown as a soft
    // suggestion; Accept/Pin/Reject still write only on a user click.
    in_version: e.in_version !== false,
    // Any edge still in force and not yet under user authority can be pinned.
    pinnable: !closed && e.pinned !== true && life !== 'provisional',
    matrix_drill: readers.length
      ? `/matrix/readers/${encodeURIComponent(readers[0])}`
      : '/matrix',
  };
}

// Build the typed-edge model from a /topology/v3 payload.  Edges with a
// published lifecycle are all kept; nothing is promoted past CANDIDATE here.
function v3TopologyModel(payload) {
  const data = payload || {};
  const edges = Array.isArray(data.edges)
    ? data.edges
    : Array.isArray(data.topology_edges) ? data.topology_edges : [];
  const views = edges.map(v3EdgeView);
  return {
    edges: views,
    provisional: views.filter((e) => e.provisional),
    byType: views.reduce((acc, e) => {
      (acc[e.edge_type] ||= []).push(e);
      return acc;
    }, {}),
    discovery: discoverySummary(data),
  };
}

// The discovery summary from GET /topology/v3.  It counts stored inferred
// edges by lifecycle and names the top candidate/provisional pairs, so an
// operator can see discovery working *before* any PROVISIONAL suggestion
// exists.  A missing summary simply renders nothing (older engines).
function discoverySummary(payload) {
  const d = payload && payload.discovery;
  if (!d) return null;
  const counts = d.counts || {};
  const candidates = Number(counts.candidate) || 0;
  const provisionals =
    (Number(counts.provisional) || 0)
    + (Number(counts.structural) || 0)
    + (Number(counts.confirmed) || 0)
    + (Number(counts.conflicted) || 0);
  // Only show the summary while there is nothing to act on in the edges card.
  if (provisionals > 0) return null;
  const pairs = Array.isArray(d.top_pairs) ? d.top_pairs : [];
  const top = pairs[0];
  const thresholds = d.thresholds || {};
  const provisionalSupport = Number(thresholds.MIN_SUPPORT_PROVISIONAL) || 3;
  const independentNeeded = Number(thresholds.MIN_INDEPENDENT_CAUSES) || 2;
  const lines = [`Discovery: ${candidates} candidate pair${candidates === 1 ? '' : 's'} observed`];
  if (top) {
    lines.push(
      `; top pair ${top.source_endpoint_id || '—'}→${top.target_endpoint_id || '—'}`
      + ` support ${Number(top.support) || 0}/${provisionalSupport}`
      + ` from ${Number(top.independent_causes) || 0}/${independentNeeded} independent causes`
    );
  }
  lines.push('. Candidate pairs are single-event observations; they are not promoted until they become provisional.');
  return notice('Topology discovery', lines.join(''), 'info');
}

// The ONLY writes in the topology view: a user clicking Accept, Pin or
// Reject.  Accept and Pin POST /pin; Reject POSTs /reject.  No render path
// calls these (HB §16: one event never promotes an edge past CANDIDATE).
async function acceptProvisionalEdge(edgeId, http = post) {
  if (!_edgeText(edgeId)) return null;
  return http(`/topology/edges/${encodeURIComponent(String(edgeId))}/pin`, {});
}

async function rejectProvisionalEdge(edgeId, http = post) {
  if (!_edgeText(edgeId)) return null;
  return http(`/topology/edges/${encodeURIComponent(String(edgeId))}/reject`, {});
}

const edgeAction = (text, cls, action, ok, fail) =>
  el('button', {
    class: cls,
    type: 'button',
    onclick: () => action().then(
      () => { notify(ok[0], ok[1]); render(); },
      (err) => notify(fail, describeError(err), 'bad')
    ),
  }, text);

// Render the typed-edge card.  The buttons are wired to the user actions
// above; PROVISIONAL edges are soft and always show a lifecycle badge.
function v3EdgesCard(model) {
  const m = model || v3TopologyModel(null);
  const rows = m.edges.map((edge) =>
    el('div', {
      class: 'topo-v3-edge',
      dataset: {
        lifecycle: edge.lifecycle,
        type: edge.edge_type,
        version: edge.in_version === false ? 'suggestion' : 'active',
      },
    }, [
      badge(label(edge.edge_type), 'info'),
      badge(label(edge.state || edge.lifecycle), edge.provisional ? 'warn' : 'mute'),
      edge.in_version === false ? badge('suggestion', 'info') : null,
      edge.pinned ? badge('Pinned', 'ok') : null,
      el('span', {}, `${edge.parent_id || '—'} → ${edge.child_id || '—'}`),
      el('span', { class: 'stat-hint' }, `state ${edge.state || edge.lifecycle} · strength ${edge.strength.toFixed(1)} · conflict ${Math.round((edge.conflict_ratio || 0) * 100)}%`),
      el('a', { class: 'v3-provenance', href: `#${edge.matrix_drill}` }, 'Matrix columns'),
      edge.provisional || edge.pinnable
        ? el('span', { class: 'topo-v3-actions' }, [
            edgeAction(
              'Pin',
              'btn btn-sm',
              () => acceptProvisionalEdge(edge.edge_id),
              ['Edge pinned', 'The edge is now frozen under your authority.'],
              'Could not pin edge'
            ),
            edgeAction(
              'Reject',
              'btn btn-sm btn-ghost',
              () => rejectProvisionalEdge(edge.edge_id),
              ['Edge rejected', 'The edge was rejected.'],
              'Could not reject edge'
            ),
          ])
        : null,
    ])
  );
  const body = rows.length ? rows.slice() : [
    el('p', { class: 'stat-hint' }, 'No typed edges published by the topology service yet.'),
  ];
  if (m.discovery) body.unshift(m.discovery);
  return card('Typed topology edges (v3)', body, {
    id: 'topology-edges',
    foot: 'State and strength come from the topology service and follow recent evidence. Only an operator Pin or Reject writes; a single event never promotes an edge past candidate.',
  });
}

export {
  v3EdgeView,
  v3TopologyModel,
  discoverySummary,
  acceptProvisionalEdge,
  rejectProvisionalEdge,
  moveCircuit,
  descendantReaders,
  validMoveTargets,
  canDropOn,
  v3EdgesCard,
  TREE_PATH,
  V3_PATH,
  ENDPOINTS_PATH,
  edgeInForce,
  edgeStructural,
  topologyStatusFromEdges,
  endpointList,
  containsParents,
  endpointChannel,
  topologyConfigured,
  topologyModel,
  noConfirmedEdgesNotice,
  reviewTopologyEdges,
  CT_TONES,
  ctTone,
  topologyHead,
  topologySkeleton,
  renderTopology,
  topologyFallbackNotice,
  topologySummary,
  nodeKey,
  BADGE_TONES,
  badgeTone,
  badgeItems,
  badgeNode,
  nodeBadges,
  MEASUREMENT_STATES,
  NOT_MEASURED_REASONS,
  MEASUREMENT_TITLES,
  notMeasuredTitle,
  hasPowerValue,
  measurementOf,
  isMeasuredNode,
  STRUCTURAL_SUBTITLES,
  deviceSubtitle,
  firstEvidence,
  deviceEvidenceLine,
  isWiringStatusBadge,
  circuitChildSummary,
  prunedNodes,
  PLACEMENT_BANDS,
  placementBand,
  nodeChips,
  hierarchyNodes,
  legacyRoots,
  defaultOpenSet,
  isExpanded,
  nodeText,
  nodeMatches,
  filterSubtree,
  toEntry,
  topologyHierarchyCard,
  setAllExpanded,
  paintHierarchy,
  renderEntry,
  hierarchyRow,
  GLYPHS,
  topologyChannelsCard
};
