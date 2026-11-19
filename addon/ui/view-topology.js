/* ==========================================================================
   WattMosaic Engine panel — view-topology.js
   --------------------------------------------------------------------------
   Native ES module; relative imports only so the same file works at "/" and
   under /api/hassio_ingress/<token>/.  No build step, no network beyond origin.
   ========================================================================== */

import { slots } from './slots.js';
import { NESTING_PATH, NESTING_REFRESH_AFTER_MS, NESTING_TTL_MS, STRINGS, ago, api, badge, card, clear, describeError, el, emptyState, frag, get, label, notice, notify, num, percent, post, render, sourceTone, statCard, state, statusBadge, titleCase, toneFor, watts } from './core.js';
import { suspectedChangesCard, versionHistoryCard } from './view-devices.js';

/* --- Topology --------------------------------------------------------- */

// The hierarchy is painted from GET /api/v1/topology/tree.  This file never
// derives the tree again — not from names, not from ct_id prefixes, not from
// entity ids.  A second derivation in the browser is exactly what used to
// produce a silently wrong tree, so the engine owns the hierarchy and the
// panel renders it verbatim.

const TREE_PATH = '/topology/tree';
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
      }, state.discover.running ? 'Discovering…' : 'Discover devices'),
      topology
        ? el('button', {
          class: 'btn',
          type: 'button',
          onclick: confirmTopology,
          title: 'Optional — raises wiring confidence; learning and results already use the active topology',
        }, STRINGS.topology_confirm_optional)
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

  // 2. Only the fast endpoints are awaited here.  /topology/nesting compares
  //    a 12-hour window and can take tens of seconds; putting it in this
  //    Promise.all is what made the whole tab wait for it.  /status is cheap
  //    and carries the learning state the banner needs, so it joins here.
  const [topology, treeResult, status, topologyV3] = await Promise.all([
    get('/topology', signal).catch((err) => {
      if (err && err.status === 404) return null;
      throw err;
    }),
    get(TREE_PATH, signal).then(
      (data) => ({ ok: true, data, err: null }),
      (err) => ({ ok: false, data: null, err })
    ),
    get('/status', signal).catch(() => null),
    // U2: the v3 typed-edge payload (T4).  A backend without it renders an
    // explicit unavailable state; the rest of the topology view stays intact.
    get('/topology/v3', signal).catch(() => null),
  ]);
  if (signal.aborted) return;

  clear(root);
  root.append(topologyHead(topology));

  if (!topology) {
    root.append(
      emptyState(
        'No topology configured',
        'The engine cannot disaggregate without a topology. Configure it in the WattMosaic integration; results stay unavailable until then.',
        { tone: 'warn' }
      )
    );
    return;
  }

  const tree = treeResult.ok && treeResult.data && Array.isArray(treeResult.data.roots)
    ? treeResult.data
    : null;

  const learningBanner = slots.topologyLearningBanner(status, topology);
  if (learningBanner) root.append(learningBanner);
  root.append(topologySummary(topology, tree));
  if (!tree) root.append(topologyFallbackNotice(treeResult.err));

  // The hierarchy is the subject of this tab, so it gets the full width: a
  // tree squeezed into half a page forces every ct_id to wrap.
  root.append(topologyHierarchyCard(topology, tree));

  // Additive B4-UIb cards: the topology's append-only version history and any
  // suspected configuration changes the audit pass detected.  Both read only
  // payloads already fetched for this view (/topology and /status).
  root.append(versionHistoryCard(topology, null, null));
  root.append(suspectedChangesCard(status, null));

  // U2: v3 typed edges with their lifecycle, PROVISIONAL accept/reject (writes
  // only on user action) and a link to the matching matrix columns.
  root.append(topologyV3
    ? v3EdgesCard(v3TopologyModel(topologyV3))
    : emptyState(
        'Typed topology edges unavailable',
        'GET /api/v1/topology/v3 returned no payload. The hierarchy above is still usable; no edge is promoted or written in the meantime.'
      ));

  // Placement discovery renders into its own slot; it is only populated when
  // the user asks for it (or when a previous answer is cached in state).
  root.append(slots.topologyDiscoverSlot());

  // The candidates card is a real card from the start: either the cached
  // comparison or an explicit loading state.  It is replaced in place.
  root.append(
    el('div', { class: 'topo-grid' }, [
      topologyChannelsCard(topology),
      topologyCandidatesSlot(),
    ])
  );

  const notes = [...(topology.notes || []), ...((tree && tree.notes) || [])];
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

  // 3. Everything above is on screen.  Now, and only now, ask for the slow
  //    comparison; it fills one card when it lands.
  fillNestingCandidates(signal);
}

function topologyFallbackNotice(err) {
  const missing = Boolean(err && err.status === 404);
  const text = missing
    ? 'This engine does not serve /api/v1/topology/tree (HTTP 404), so the engine-owned hierarchy cannot be drawn. The hierarchy card falls back to CT containment from parent_ct_id only, and devices are not placed.'
    : `The hierarchy could not be loaded (${describeError(err)}). The hierarchy card falls back to CT containment from parent_ct_id only, and devices are not placed.`;
  return notice('Hierarchy unavailable', text, 'warn');
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
    : String((topology.devices || []).length);
  const unplaced = stats
    ? (Number(stats.unplaced_devices) || 0) + (Number(stats.unplaced_circuits) || 0)
    : 0;

  // OD-51: a candidate topology is dynamic and authoritative for inference
  // and learning — never a gate.  Show its published confidence as status,
  // not as a "confirm to proceed" warning.
  const configured =
    topology.status === 'confirmed'
      ? null
      : notice(
          STRINGS.topology_dynamic + ' — authoritative for inference and learning',
          'The current topology drives results and learning now. Candidate wiring carries a reduced confidence score; samples are collected with topology confidence tracked. Confirming the CT roles is optional and raises that confidence.',
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
      statCard('Version', String(topology.version ?? '—'), { hint: ago(topology.created_at) }),
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

  const repaint = (options) => {
    const focusKey = options && options.focusKey;
    const count = paintHierarchy(body, nodes, repaint);
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

  const node = card('Hierarchy', [status, body], {
    id: 'topology-hierarchy',
    actions: [filter, expandAll, collapseAll],
    foot: tree
      ? `Computed by the engine${tree.version !== undefined ? ` (contract v${tree.version})` : ''} at ${ago(tree.generated_at)}. Each device appears once, under the circuit that measures it.`
      : 'Fallback view: CT containment from parent_ct_id only. Devices are not placed.',
  });

  // Paint in the same task as the card so the tree is never a frame late.
  const shown = paintHierarchy(body, nodes, repaint);
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
function paintHierarchy(body, nodes, onToggle) {
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
  const row = el('div', { class: 'hier-row', dataset: { kind, depth: String(depth) } });

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

const GLYPHS = { panel: '▤', group: '▫', circuit: '◉', device: '◆' };

function topologyChannelsCard(topology) {
  const channels = [...(topology.channels || [])].sort((a, b) => {
    const order = { main: 0, branch: 1, unknown: 2 };
    return (order[a.ct_type] ?? 9) - (order[b.ct_type] ?? 9) || String(a.ct_id).localeCompare(String(b.ct_id));
  });
  return el('section', { class: 'card', id: 'topology-channels' }, [
    el('div', { class: 'card-head' }, [
      el('h2', {}, 'CT channels'),
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
                badge(label(ch.phase), 'mute'),
                ch.enabled === false ? badge('Disabled', 'bad') : null,
                ch.topology_status && ch.topology_status !== 'confirmed' ? statusBadge(ch.topology_status) : null,
                ch.polarity === 'inverted' ? badge('Inverted', 'warn') : null,
                ch.alias_of_ct_id ? badge(`alias of ${ch.alias_of_ct_id}`, 'warn') : null,
              ]),
            ])
          )
        : emptyState('No channels', 'This topology has no CT channels.')
    ),
  ]);
}

/* --- Nesting candidates (slow; fills in after the tree) ---------------- */

const nestingIsFresh = () =>
  Boolean(state.nesting.data) && Date.now() - state.nesting.at < NESTING_TTL_MS;

function topologyCandidatesSlot() {
  if (nestingIsFresh()) {
    // Only promise a background refresh when fillNestingCandidates will
    // actually issue one: inside the refresh-after window it deliberately
    // does not, and the footer must not claim otherwise.
    const willRefresh = Date.now() - state.nesting.at >= NESTING_REFRESH_AFTER_MS;
    return topologyCandidatesCard(state.nesting.data, { cached: true, willRefresh });
  }
  return topologyCandidatesLoadingCard();
}

function topologyCandidatesLoadingCard() {
  return card(
    'Parent and alias candidates',
    [
      el('div', { class: 'cand-loading', role: 'status' }, [
        el('span', { class: 'spinner', 'aria-hidden': 'true' }),
        el('span', {}, 'Comparing a 12-hour window for shared conductors and nested feeds…'),
      ]),
      el('div', { class: 'skeleton h-row' }),
      el('div', { class: 'skeleton h-row' }),
      el('div', { class: 'skeleton h-row' }),
    ],
    {
      id: 'topology-candidates',
      foot: 'The hierarchy above is already usable. This comparison only adds advisory candidates; nothing is written until you confirm it.',
    }
  );
}

function topologyCandidatesErrorCard(err) {
  return card(
    'Parent and alias candidates',
    [
      el('p', { class: 'stat-hint', style: 'margin-bottom:12px' },
        `The comparison did not finish: ${describeError(err)}`),
      el('button', { class: 'btn btn-sm', type: 'button', onclick: () => render() }, 'Try again'),
    ],
    { id: 'topology-candidates' }
  );
}

function fillNestingCandidates(signal) {
  // One request at a time.  A second render (a poll, a hash change, a
  // refresh) while a comparison is in flight must not queue another 12-hour
  // window on top of it.
  if (state.nesting.inflight) return;
  const age = Date.now() - state.nesting.at;
  if (nestingIsFresh() && age < NESTING_REFRESH_AFTER_MS) return;

  state.nesting.inflight = true;
  const done = () => { state.nesting.inflight = false; };
  get(NESTING_PATH, signal)
    .then((data) => {
      done();
      if (signal.aborted || !data) return;
      state.nesting = { data, at: Date.now(), inflight: false };
      swapCandidates(topologyCandidatesCard(data));
    })
    .catch((err) => {
      done();
      if (!err || err.name === 'AbortError') return;
      if (signal.aborted) return;
      // A cached answer is still a true answer for this window: keep it.
      if (nestingIsFresh()) return;
      swapCandidates(topologyCandidatesErrorCard(err));
    });
}

function swapCandidates(node) {
  const current = document.getElementById('topology-candidates');
  if (current) current.replaceWith(node);
}

function topologyCandidatesCard(nesting, { cached = false, willRefresh = false } = {}) {
  const candidates = nesting.candidates || [];
  const ambiguous = nesting.ambiguous || [];
  const suppressed = nesting.suppressed || [];
  const notInferable = nesting.not_inferable || [];
  const aliases = nesting.candidate_aliases || {};
  const aliasEntries = Object.entries(aliases);

  const body = [];
  if (candidates.length) {
    body.push(
      el('p', { class: 'stat-hint', style: 'margin-bottom:12px' },
        'Candidates only. No parent link is written until a user confirms it.'),
      ...candidates.map((candidate) => {
        const duplicate = candidate.kind === 'duplicate';
        const evidence = candidate.evidence || {};
        const detail = evidence.reason
          ? evidence.reason
          : duplicate
            ? 'Same conductor measured twice.'
            : 'Contained at every comparable instant.';
        const facts = duplicate
          ? [
              evidence.activity_ratio !== undefined ? `${percent(evidence.activity_ratio, 0)} co-active` : null,
              evidence.magnitude_ratio !== undefined ? `${num(evidence.magnitude_ratio, 2)}× magnitude` : null,
            ]
          : [
              evidence.containment !== undefined ? `${percent(evidence.containment, 1)} containment` : null,
              evidence.instants !== undefined ? `${evidence.instants} instants` : null,
            ];
        return el('div', { class: 'cand' }, [
          el('div', { class: 'cand-title' }, [
            badge(duplicate ? 'Duplicate feed' : 'Nested feed', duplicate ? 'warn' : 'info'),
            el('span', {}, candidate.child_ct_id),
            el('span', { class: 'arrow', 'aria-hidden': 'true' }, '→'),
            el('span', {}, candidate.parent_ct_id),
          ]),
          el('div', { class: 'cand-ev' }, detail),
          facts.filter(Boolean).length
            ? el('div', { class: 'cand-ev' }, facts.filter(Boolean).join(' · '))
            : null,
        ]);
      })
    );
  } else {
    body.push(
      el('p', { class: 'stat-hint' }, nesting.reason || 'No parent or duplicate candidates in the comparison window.')
    );
  }
  if (aliasEntries.length) {
    body.push(
      el('p', { class: 'stat-hint', style: 'margin-top:12px' },
        `Aliases measured: ${aliasEntries.map(([alias, canonical]) => `${alias} = ${canonical}`).join(', ')}. These are one conductor seen twice, never summed.`)
    );
  }
  if (ambiguous.length) {
    body.push(
      el('div', { class: 'notice', dataset: { tone: 'warn' }, style: 'margin-top:14px' }, [
        el('div', { class: 'banner-icon', 'aria-hidden': 'true' }, '!'),
        el('div', {}, [
          el('strong', {}, `${ambiguous.length} channel${ambiguous.length === 1 ? '' : 's'} ambiguous`),
          el('p', {}, 'Measurement cannot establish a level. The engine reports the options instead of guessing.'),
        ]),
      ])
    );
  }
  if (notInferable.length) {
    body.push(
      el('p', { class: 'stat-hint', style: 'margin-top:12px' },
        `${notInferable.length} load${notInferable.length === 1 ? '' : 's'} cannot be placed by measurement; a declared parent is needed.`)
    );
  }
  if (suppressed.length) {
    const reasons = {};
    for (const row of suppressed) reasons[row.reason] = (reasons[row.reason] || 0) + 1;
    body.push(
      el('p', { class: 'stat-hint', style: 'margin-top:12px' },
        `${suppressed.length} relationship${suppressed.length === 1 ? '' : 's'} suppressed (`
        + `${Object.entries(reasons).map(([reason, count]) => `${titleCase(reason)}: ${count}`).join(', ')}).`)
    );
  }
  return card('Parent and alias candidates', body, {
    id: 'topology-candidates',
    foot: nesting.instants
      ? `Compared over ${nesting.instants} instants across ${nesting.channels_compared || 0} channels.`
        + (cached
          ? (willRefresh
            ? ' Showing the cached result; refreshing in the background.'
            : ' Showing the cached result from a moment ago.')
          : '')
      : null,
  });
}

async function confirmTopology() {
  try {
    await post('/topology/confirm', {});
    notify('Topology confirmed', 'Confidence for this topology is now at its maximum. Learning and results were already using it.');
    render();
  } catch (err) {
    notify('Could not confirm topology', describeError(err), 'bad');
  }
}

/* ====================================================================== *
 *  U2 — v3 typed-edge topology (pure view helpers).
 * ---------------------------------------------------------------------- *
 *  GET /topology (T4) publishes typed edges with a lifecycle state.  Only a
 *  user action writes: accepting a PROVISIONAL suggestion POSTs /pin, rejecting
 *  POSTs /reject.  Nothing here mutates on render.  Each edge can link to the
 *  matching matrix columns for its reader(s).
 * ====================================================================== */

const _absent = (value) => value === null || value === undefined;
const _edgeText = (value) => (_absent(value) || value === '' ? null : String(value));

// Normalise a T4 topology edge into the fields the panel renders.  Lifecycle
// is published verbatim; an absent lifecycle is 'candidate', never promoted.
function v3EdgeView(edge) {
  const e = edge || {};
  const lifecycle = _edgeText(e.lifecycle)
    || _edgeText(e.status)
    || (e.confirmed === true ? 'confirmed' : 'candidate');
  const edgeType = _edgeText(e.edge_type) || _edgeText(e.type) || 'unknown';
  const readers = Array.isArray(e.reader_ids)
    ? e.reader_ids.filter(Boolean).map(String)
    : [e.reader_id].filter((v) => !_absent(v)).map(String);
  return {
    edge_id: _edgeText(e.edge_id) || _edgeText(e.id),
    edge_type: edgeType,
    lifecycle: String(lifecycle).toLowerCase(),
    parent_id: _edgeText(e.parent_id) || _edgeText(e.parent_device_id),
    child_id: _edgeText(e.child_id) || _edgeText(e.child_device_id),
    reader_ids: readers,
    provisional: String(lifecycle).toLowerCase() === 'provisional',
    matrix_drill: readers.length
      ? `/matrix/readers/${encodeURIComponent(readers[0])}`
      : '/matrix',
  };
}

// Build the typed-edge model from a /topology payload.  Edges with a published
// lifecycle are all kept; nothing is promoted past CANDIDATE by this panel.
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
  };
}

// The ONLY writes in the topology view: a user clicking Accept or Reject.  A
// PROVISIONAL edge accept POSTs /pin; a reject POSTs /reject.  No render path
// calls these (HB §16: one event never promotes an edge past CANDIDATE).
async function acceptProvisionalEdge(edgeId, http = post) {
  if (!_edgeText(edgeId)) return null;
  return http(`/topology/edges/${encodeURIComponent(String(edgeId))}/pin`, {});
}

async function rejectProvisionalEdge(edgeId, http = post) {
  if (!_edgeText(edgeId)) return null;
  return http(`/topology/edges/${encodeURIComponent(String(edgeId))}/reject`, {});
}

// Render the typed-edge card.  Accept/Reject buttons are wired to the user
// actions above; PROVISIONAL edges are soft and always show a lifecycle badge.
function v3EdgesCard(model) {
  const m = model || v3TopologyModel(null);
  const rows = m.edges.map((edge) =>
    el('div', { class: 'topo-v3-edge', dataset: { lifecycle: edge.lifecycle, type: edge.edge_type } }, [
      badge(label(edge.edge_type), 'info'),
      badge(label(edge.lifecycle), edge.provisional ? 'warn' : 'mute'),
      el('span', {}, `${edge.parent_id || '—'} → ${edge.child_id || '—'}`),
      el('a', { class: 'v3-provenance', href: `#${edge.matrix_drill}` }, 'Matrix columns'),
      edge.provisional
        ? el('span', { class: 'topo-v3-actions' }, [
            el('button', {
              class: 'btn btn-sm', type: 'button',
              onclick: () => acceptProvisionalEdge(edge.edge_id).then(
                () => notify('Edge accepted', 'The provisional edge was pinned.'),
                (err) => notify('Could not accept edge', describeError(err), 'bad')
              ),
            }, 'Accept'),
            el('button', {
              class: 'btn btn-sm btn-ghost', type: 'button',
              onclick: () => rejectProvisionalEdge(edge.edge_id).then(
                () => notify('Edge rejected', 'The provisional edge was rejected.'),
                (err) => notify('Could not reject edge', describeError(err), 'bad')
              ),
            }, 'Reject'),
          ])
        : null,
    ])
  );
  return card('Typed topology edges (v3)', rows.length ? rows : [
    el('p', { class: 'stat-hint' }, 'No typed edges published by the topology service.'),
  ], {
    foot: 'Lifecycle comes from the topology service. Only an operator Accept or Reject writes; a single event never promotes an edge past candidate.',
  });
}

export {
  v3EdgeView,
  v3TopologyModel,
  acceptProvisionalEdge,
  rejectProvisionalEdge,
  v3EdgesCard,
  TREE_PATH,
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
  topologyChannelsCard,
  nestingIsFresh,
  topologyCandidatesSlot,
  topologyCandidatesLoadingCard,
  topologyCandidatesErrorCard,
  fillNestingCandidates,
  swapCandidates,
  topologyCandidatesCard,
  confirmTopology
};
