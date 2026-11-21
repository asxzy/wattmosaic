/* ==========================================================================
   WattMosaic Engine panel — view-discover.js
   --------------------------------------------------------------------------
   Native ES module; relative imports only so the same file works at "/" and
   under /api/hassio_ingress/<token>/.  No build step, no network beyond origin.
   ========================================================================== */

import { slots } from './slots.js';
import { DISCOVER_WINDOWS, STRINGS, api, badge, card, clock, describeError, discoverLongRun, discoverWindow, discoverWindowLabel, discoverWindowName, duration, el, emptyState, get, label, notice, notify, num, percent, post, render, state, titleCase, watts } from './core.js';

/* --- Discover devices (contract §5.4 item 6) -------------------------- *
 * Placement discovery reads the recorder and proposes devices for fixtures
 * whose transitions it can prove.  Analysing writes nothing; a write needs a
 * second, explicit click per row.  There is no modal in this panel, so the
 * two-step confirm lives on the button label itself.
 * ---------------------------------------------------------------------- */

const DISCOVER_PATH = '/discover/placements';
//: The button and card name.  One run learns both: device placements from
//: fixture transitions, and topology (which circuit feeds which) from the same
//: window of recorded power history.
const DISCOVER_TITLE = 'Discover devices & topology';

//: When the run in flight started, so a repaint mid-run (a poll, a route
//: change) re-shows the true elapsed time instead of restarting at zero.
//: Only one discovery runs at a time (``runDiscovery`` guards on ``running``).
let discoverStartedAt = 0;

//: Bumped on navigation (``applyRoute``).  A discovery poll captures the value
//: when it starts and treats a mismatch as cancellation, so it never issues a
//: status GET after the user has left the view.

//: Why the engine withheld or refused a fixture, in words a person can act on.
//: Keys are the engine's own codes; an unlisted code still falls back to
//: ``titleCase`` so a newer engine is legible rather than blank.
const WITHHELD_REASONS = {
  // Correlation withheld the transition evidence.
  not_discriminating: 'Its transitions do not move measured power',
  asymmetric_response: 'It responds to on and off differently',
  not_specific: 'Its transitions are not specific to one row',
  too_few_corroborations: 'Too few independent transitions to prove it',
  inconsistent_with_row: 'Its response contradicts the row it appears on',
  ambiguous_rows: 'Several rows could explain it equally well',
  no_row_responded: 'No CT row moved when it switched',
  no_independent_transitions: 'Its transitions came in bursts, so none could be paired',
  // The candidate population refused the entity outright.
  already_declared_state_entity: 'Already configured by the user; left unchanged',
  wattmosaic_entity: "It is WattMosaic's own output, not a load",
  not_a_load_entity_category: 'Home Assistant marks it as a setting, not a load',
  mode_entity: 'It is a mode control, so its state does not prove a load',
  not_a_load_domain: 'Its state does not prove a load is running',
};

//: Reasons that mean "a device already carries this evidence", i.e. nothing is
//: wrong with the measurement -- there is simply nothing new to write.
//: ``already configured`` is a prefix check because the placement layer emits
//: "already configured by the user; left unchanged".
const ALREADY_DECLARED_REASON = 'already_declared_state_entity';

const isAlreadyWritten = (reason) => {
  const text = String(reason || '').trim();
  return text === ALREADY_DECLARED_REASON || text.startsWith('already configured');
};

function withheldReason(reason) {
  const text = String(reason || '').trim();
  if (!text) return 'No reason given';
  if (text.startsWith('co_toggles_with_another_fixture_on_')) {
    const ct = text.slice('co_toggles_with_another_fixture_on_'.length);
    return `It always toggles with another fixture on ${ct.replace(/_/g, ' ')}`;
  }
  if (text.startsWith('identified_by_aggregate_fit_on_')) {
    const ct = text.slice('identified_by_aggregate_fit_on_'.length);
    return `Only the aggregate fit found it on ${ct.replace(/_/g, ' ')}; that is not proof of one fixture`;
  }
  if (text.startsWith('already configured')) return 'Already configured by the user; left unchanged';
  if (WITHHELD_REASONS[text]) return WITHHELD_REASONS[text];
  // Placement refusals are already sentences written for a person
  // ("CT row 'ct1' measures the whole installation; ...").  Only a bare
  // single-token code needs title-casing.
  if (/[^a-z0-9_]/.test(text)) return text;
  return titleCase(text);
}

const discoverSlotNode = () =>
  el('div', { class: 'card discover-slot', id: 'topology-discover' }, [
    el('div', { class: 'card-head' }, [
      el('h2', {}, DISCOVER_TITLE),
      el('span', { class: 'spacer' }),
      el('span', { class: 'stat-hint' },
        `${discoverWindowLabel(state.discover.window)} window · not run yet`),
    ]),
    el('div', { class: 'card-body' }, [
      el('p', { class: 'stat-hint' },
        'Find fixtures that sit on a shared circuit by comparing their transitions with the CT rows, and learn which circuit feeds which by replaying the same window of recorded power history. Device placements are only written when you apply them; learned topology joins the live evidence the engine collects anyway.'),
      el('p', { class: 'stat-hint' },
        'A longer window is stronger evidence and slower: a week of history found the shared light-circuit groups a 24 h window could not, but it reads far more recorder history.'),
    ]),
  ]);

/**
 * The window control, next to the Discover button.
 *
 * A plain ``<select>``: four options, one visible label, keyboard- and
 * screen-reader-friendly with no library and no inline handler.  Changing it
 * only records the choice in ``state.discover.window``; it never issues a
 * request.  A long window costs minutes, so a stray change must not start
 * one -- the request is the explicit Discover click, as before.
 *
 * The control is also disabled while a run is in flight, because the window
 * of a running request cannot change; the *next* run reads whatever is
 * selected when it starts.
 */
const discoverWindowSelect = () =>
  el('label', { class: 'discover-window', for: 'topology-discover-window' }, [
    el('span', { class: 'discover-window-label' }, 'Window'),
    el('select', {
      class: 'discover-window-select',
      id: 'topology-discover-window',
      'aria-label': 'Discovery lookback window',
      title: 'How much recorder history this analysis reads. A longer window finds more shared circuits and takes longer.',
      disabled: state.discover.running ? true : null,
      onchange: (event) => selectDiscoverWindow(event.target.value),
    }, DISCOVER_WINDOWS.map((option) =>
      el('option', {
        value: String(option.hours),
        selected: option.hours === state.discover.window ? true : null,
      }, option.label))),
  ]);

function selectDiscoverWindow(raw) {
  const option = discoverWindow(Number(raw));
  if (!option) return;
  // Selection is intent, not action: nothing is fetched here.  Only the
  // Discover click sends a request (see ``runDiscovery``), so a long window
  // can never start by accident.  A result already on the card keeps its own
  // window -- its footer names it -- because relabelling a finished analysis
  // with a window it never read would misstate the evidence.
  state.discover = { ...state.discover, window: option.hours };
  if (!state.discover.data && !state.discover.error && !state.discover.running) {
    swapDiscover(discoverSlotNode());
  }
}

function topologyDiscoverSlot() {
  if (state.discover.error) return discoverErrorCard(state.discover.error);
  if (state.discover.data) return discoverCard(state.discover.data);
  if (state.discover.running) return discoverLoadingCard();
  return discoverSlotNode();
}

function swapDiscover(node) {
  const current = document.getElementById('topology-discover');
  if (current) {
    current.replaceWith(node);
    return;
  }
  // No slot on screen (the user navigated away while the request ran): the
  // answer is still cached in state, so the next paint of this tab shows it.
  // Painting into a page it does not belong to would be worse than dropping
  // the render.
}

function discoverLoadingCard() {
  // The pinned window of the request in flight, not the selector's value: a
  // disabled selector cannot be changed meanwhile, but the copy must name the
  // window the running request actually reads.
  const hours = state.discover.ranWindow !== null
    ? state.discover.ranWindow
    : state.discover.window;
  const long = discoverLongRun(hours);
  const body = [
    el('div', { class: 'cand-loading', role: 'status' }, [
      el('span', { class: 'spinner', 'aria-hidden': 'true' }),
      el('span', {},
        `Comparing fixture transitions with the CT rows and replaying circuit power for topology over the last ${discoverWindowLabel(hours)}…`),
    ]),
    //: Updated by runDiscovery's status callback on each status answer.
    el('p', { class: 'stat-hint discover-job-status', id: 'topology-discover-job-status' },
      'Starting discovery job…'),
  ];
  if (long) {
    // Say what the wait is.  168 h measured 155 s on production recorder
    // history, and the engine sends nothing until the whole comparison is
    // done, so a silent card reads as a hung one.  This is a duration, not a
    // progress claim: the elapsed clock below only reports real elapsed time.
    body.push(
      el('p', { class: 'stat-hint discover-long-note' },
        (Number(hours) >= 168
          ? 'A week of recorder history measured about 2.5 minutes on production'
          : 'This window reads days of recorder history and can take a minute or more')
        + ', and the engine answers only once the whole window has been read. '
        + 'The spinner and the elapsed clock keep moving while it works; keep this tab open.')
    );
  }
  // A ticking elapsed clock is the honest sign of life for a request that
  // can run for minutes; it reports time spent, never a percentage done.
  const spent = discoverStartedAt
    ? duration((Date.now() - discoverStartedAt) / 1000)
    : '0s';
  body.push(
    el('p', { class: 'stat-hint discover-elapsed' }, [
      el('span', {}, 'Elapsed '),
      el('span', { id: 'topology-discover-elapsed' }, spent),
    ])
  );
  body.push(el('div', { class: 'skeleton h-row' }));
  body.push(el('div', { class: 'skeleton h-row' }));
  return card(DISCOVER_TITLE, body, {
    id: 'topology-discover',
    foot: `Window: ${discoverWindowLabel(hours)}${long ? ' · long runs read the full window before answering' : ''}`,
  });
}

function discoverErrorCard(err) {
  const missing = Boolean(err && err.status === 404);
  const text = missing
    ? 'This engine does not serve POST /api/v1/discover/placements (HTTP 404). Update the engine to find devices automatically; nothing was changed.'
    : `The comparison did not finish: ${describeError(err)}`;
  return card(
    DISCOVER_TITLE,
    [
      el('p', { class: 'stat-hint', style: 'margin-bottom:12px' }, text),
      el('button', {
        class: 'btn btn-sm',
        type: 'button',
        onclick: () => runDiscovery(),
      }, 'Try again'),
    ],
    {
      id: 'topology-discover',
      // The failed run still read a named window; saying which one makes the
      // failure reproducible, and the Try again button re-reads the selector.
      foot: `Window: ${discoverWindowLabel(
        state.discover.ranWindow !== null ? state.discover.ranWindow : state.discover.window
      )} · the comparison did not finish`,
    }
  );
}

/* DISCOVERY_STATUS_OUTCOME_START */
/**
 * Pure mapping from a discovery-job status payload to a render outcome.
 *
 * The start endpoint returns a handle (no ``status``), so a payload with no
 * ``status`` is the legacy synchronous result and renders directly.  A
 * ``running`` job is not done yet.  ``complete`` carries ``result`` and
 * ``failed`` carries ``error`` -- the engine's own failure detail, which is a
 * string (or an object with ``message``/``detail``).  This function is pure so
 * the source slice between these markers can be run directly under node.
 */
function discoveryStatusOutcome(payload) {
  const job = payload || {};
  const status = job.status;
  if (status === 'running' || status === 'pending') return { done: false };
  if (status === 'complete') return { done: true, result: job.result };
  if (status === 'failed') {
    const raw = job.error;
    let message;
    if (raw && typeof raw === 'object') {
      message = raw.message || raw.detail || raw.error || String(raw);
    } else {
      message = raw;
    }
    return { done: true, error: message || 'Discovery failed.' };
  }
  // No status field: a legacy synchronous full result, already final.
  return { done: true, result: payload };
}
/* DISCOVERY_STATUS_OUTCOME_END */

//: A bounded poll: at most this many status GETs (one every ~2 s, so ~20 min
//: of waiting) before reporting a timeout instead of polling forever.
const DISCOVERY_MAX_POLLS = 600;
const DISCOVERY_POLL_MS = 2000;

/**
 * Start a discovery job and poll its status until it is done.
 *
 * POSTs ``body`` to get a handle.  A handle with a ``job_id`` is polled with
 * GET on ``DISCOVER_PATH/job_id`` through ``discoveryStatusOutcome``.  A
 * legacy synchronous response (no ``job_id``) is returned directly.  Resolves
 * ``{result}`` on success, ``{error}`` on a failed job, ``{timedOut:true}``
 * when the bound is hit, or ``{cancelled:true}`` when the navigation token
 * changes.  ``onProgress(payload)`` is called after each status answer.  The
 * timer is cleared on every terminal path so no GET follows a navigation.
 */
function startDiscoveryJob(body, onProgress, isCancelled) {
  return new Promise((resolve) => {
    let timer = null;
    let polls = 0;
    // One navigation token per job; mismatch means the user navigated away.
    const token = slots.discoveryGeneration;
    const cancelled = () => (isCancelled ? isCancelled() : false) || token !== slots.discoveryGeneration;
    const finish = (value) => {
      if (timer !== null) { clearTimeout(timer); timer = null; }
      resolve(value);
    };
    const poll = async (jobId) => {
      if (cancelled()) { finish({ cancelled: true }); return; }
      if (polls >= DISCOVERY_MAX_POLLS) { finish({ timedOut: true }); return; }
      polls += 1;
      try {
        const payload = await get(`${DISCOVER_PATH}/${encodeURIComponent(jobId)}`);
        if (cancelled()) { finish({ cancelled: true }); return; }
        if (onProgress) onProgress(payload);
        const outcome = discoveryStatusOutcome(payload);
        if (!outcome.done) {
          timer = setTimeout(() => poll(jobId), DISCOVERY_POLL_MS);
          return;
        }
        if (Object.prototype.hasOwnProperty.call(outcome, 'error')) {
          finish({ error: outcome.error });
        } else {
          finish({ result: outcome.result });
        }
      } catch (err) {
        // A transport error during polling is a real failure; surface its
        // message (never a generic "Discovery failed") the way a job error is.
        finish({ error: err });
      }
    };
    (async () => {
      try {
        const start = await post(DISCOVER_PATH, body);
        if (cancelled()) { finish({ cancelled: true }); return; }
        const handle = start || {};
        if (!handle.job_id) {
          // Legacy synchronous result: render it directly.
          finish({ result: start });
          return;
        }
        poll(handle.job_id);
      } catch (err) {
        finish({ error: err });
      }
    })();
  });
}

async function runDiscovery() {
  if (state.discover.running) return;
  // The window is read once, before the request, and pinned for the whole
  // run: a repaint must not mix the reviewed window with a different one.
  const hours = state.discover.window;
  state.discover = { ...state.discover, running: true, error: null, ranWindow: hours };
  // The head button says "Discovering…" while the request runs.
  const head = document.getElementById('topology-discover-run');
  if (head) {
    head.disabled = true;
    head.textContent = 'Discovering…';
  }
  const select = document.getElementById('topology-discover-window');
  if (select) select.disabled = true;
  discoverStartedAt = Date.now();
  swapDiscover(discoverLoadingCard());
  // Honest liveness for a request that can run for minutes: the clock counts
  // elapsed real time.  It is not a progress bar -- the engine reports
  // nothing until it is done, so a percentage or an ETA would be invented.
  const startedAt = discoverStartedAt;
  const ticker = setInterval(() => {
    const node = document.getElementById('topology-discover-elapsed');
    if (!node) return;
    node.textContent = duration((Date.now() - startedAt) / 1000);
  }, 1000);
  try {
    // The read-only request carries the window the user is looking at, and
    // writes nothing: ``apply`` stays false.  The apply path below sends the
    // same selection, so an apply writes the window that was reviewed.
    // Starting now goes through a job handle that we poll; the *result* shape
    // on completion is byte-for-byte the old synchronous payload.
    const outcome = await startDiscoveryJob({
      apply: false,
      include_groups: true,
      lookback_hours: hours,
    }, (payload) => {
      // Progress: each status answer confirms the job is still being worked.
      const node = document.getElementById('topology-discover-job-status');
      if (node) {
        node.textContent = payload && payload.status === 'complete'
          ? 'Finished; rendering the result…'
          : 'Discovery job is running…';
      }
    });
    if (outcome.cancelled) {
      // The user navigated away; no status GET follows the cancel (the helper
      // guards on the generation token), so there is nothing to paint here.
      state.discover = { ...state.discover, running: false };
      return;
    }
    if (outcome.timedOut) {
      const message =
        'Discovery is taking longer than expected and timed out. The engine may still be working; try a shorter window or retry.';
      state.discover = {
        data: null, running: false, error: { message }, window: hours, ranWindow: hours,
      };
      swapDiscover(discoverErrorCard({ message }));
      notify('Discovery timed out', message, 'bad');
      return;
    }
    if (Object.prototype.hasOwnProperty.call(outcome, 'error')) {
      // Surface the engine's real failure detail from the job status payload,
      // never a generic "Discovery failed" string.  ``describeError`` relays a
      // job error string verbatim and still explains a 404 start response.
      const err = outcome.error;
      state.discover = {
        data: null, running: false, error: err, window: hours, ranWindow: hours,
      };
      swapDiscover(discoverErrorCard(err));
      if (err && err.status === 404) {
        notify('Discovery unavailable', 'This engine does not serve /discover/placements.', 'bad');
      } else {
        notify('Discovery failed', describeError(err), 'bad');
      }
      return;
    }
    const data = outcome.result;
    state.discover = {
      data, running: false, error: null, window: hours, ranWindow: hours,
    };
    swapDiscover(discoverCard(data));
    // Learned topology moves circuits (and the devices on them) out of
    // "Unassigned measurements": repaint so the hierarchy shows it.
    if (data && data.topology && Number(data.topology.changed_edges) > 0) render();
  } finally {
    clearInterval(ticker);
    discoverStartedAt = 0;
    const button = document.getElementById('topology-discover-run');
    if (button) {
      button.disabled = false;
      button.textContent = DISCOVER_TITLE;
    }
    const windowSelect = document.getElementById('topology-discover-window');
    if (windowSelect) windowSelect.disabled = false;
  }
}

/**
 * What the history replay taught the topology.  ``topology`` is absent from an
 * apply run (it does not replay again) and from an older engine: no section.
 */
function discoverTopologySection(topology) {
  if (!topology || typeof topology !== 'object') return null;
  const head = el('div', { class: 'discover-section-head' }, [
    el('h3', {}, 'Topology'),
    el('span', { class: 'spacer' }),
  ]);
  if (topology.ok === false) {
    return el('section', { class: 'discover-section' }, [
      head,
      el('p', { class: 'stat-hint' },
        `Topology could not be learned from history: ${String(topology.detail || 'unknown reason')}. Live discovery continues.`),
    ]);
  }
  const replay = topology.replay || {};
  const counts = topology.lifecycles || {};
  const confirmed = Array.isArray(topology.confirmed) ? topology.confirmed : [];
  const summary = `Replayed ${num(replay.steps || 0)} power steps from ${num(replay.readers || 0)} circuits: `
    + `${num(counts.confirmed || 0)} confirmed, ${num(counts.provisional || 0)} provisional, `
    + `${num(counts.candidate || 0)} candidate, ${num(counts.conflicted || 0)} contradicted pairs.`;
  const rows = confirmed.map((edge) =>
    el('div', { class: 'discover-row' }, [
      el('div', { class: 'discover-main' }, [
        el('span', { class: 'hier-label' }, `${String(edge.parent)} → ${String(edge.child)}`),
      ]),
    ]));
  return el('section', { class: 'discover-section' }, [
    head,
    el('p', { class: 'stat-hint' }, summary),
    rows.length
      ? el('div', { class: 'discover-list' }, rows)
      : el('p', { class: 'stat-hint' },
        'No feed is proven yet; a longer window gives more evidence. The hierarchy fills in as live events confirm edges.'),
  ]);
}

function discoverCard(data) {
  const accepted = Array.isArray(data.accepted) ? data.accepted : [];
  const groups = Array.isArray(data.groups) ? data.groups : [];
  const withheld = data.withheld && typeof data.withheld === 'object' ? Object.entries(data.withheld) : [];
  const refused = data.refused && typeof data.refused === 'object' ? Object.entries(data.refused) : [];
  //: Candidate-level refusals and placement-level refusals are both shown
  //: under Withheld, so the Placed empty state has to read them together.
  const withheldRows = withheld.concat(refused);
  const stats = data.stats || {};
  const nothing = !accepted.length && !groups.length && !withheld.length && !refused.length;

  const body = [];

  // Only a hard precondition (``ok: false``, e.g. "no topology to place
  // devices on") belongs on this card: the run produced no result at all.
  //
  // A refusal is different: the engine answers ``ok: true`` with
  // ``applied: false`` and a ``detail`` ("auto placement is disabled...", or
  // "apply is false; pass apply=true...").  That detail is produced by a
  // *read-only* run as well, so rendering it here made every ordinary
  // Discover that found anything warn "Nothing written yet" -- a write the
  // user never asked for.  The refusal explanation belongs to the Apply
  // action that can actually hit it, where the toast relays it; see
  // ``applyButton``.
  if (data.ok === false && data.detail) {
    body.push(notice('Discovery reported a problem', String(data.detail), 'bad'));
  }
  const topologySection = discoverTopologySection(data.topology);
  if (topologySection) body.push(topologySection);

  if (nothing) {
    // Empty collections mean two different things and only ``ok`` tells them
    // apart.  An ``ok: false`` response is a hard precondition -- with no
    // active topology the engine answers "no topology to place devices on"
    // before it reads a single transition, so every list comes back empty.
    // That looks exactly like a completed run that proved nothing, but the
    // comparison never happened, so only ``ok: true`` may describe one.
    if (data.ok === false) {
      body.push(emptyState(
        'Discovery could not run',
        'The engine stopped before reading any fixture transition, so nothing was analysed and no placement was computed.'
          + (data.detail ? ' The notice above names the precondition.' : '')
      ));
    } else {
      body.push(emptyState(
        'No shared circuits could be resolved',
        'The engine compared every fixture transition it has with every branch CT row and found nothing it could prove. That is a measurement result, not an error.'
      ));
    }
    return card(DISCOVER_TITLE, body, {
      id: 'topology-discover',
      foot: discoveryFoot(data, stats),
    });
  }

  const applyAll = accepted.length && !accepted.every((row) => row.applied)
    ? applyButton({ name: 'all placements', all: true })
    : null;

  /* Placed ------------------------------------------------------------ *
   * An empty "Placed" list does not mean the evidence was thin.  On a
   * second run every candidate a previous apply wrote is refused as
   * ``already_declared_state_entity``, so the honest message depends on
   * why nothing is placed.  Only claim the evidence bar was missed when
   * no candidate was refused for already being written; when the reasons
   * are mixed, claim only that the proved fixtures are already written and
   * let Withheld speak for itself -- the remaining rows were often never
   * measured at all, so they cannot have missed a measurement bar.
   * ------------------------------------------------------------------- */
  const alreadyWritten = withheldRows.filter(([, reason]) => isAlreadyWritten(reason)).length;
  const everythingWritten = alreadyWritten > 0 && alreadyWritten === withheldRows.length;
  const placed = accepted.length
    ? el('div', { class: 'discover-list' },
        accepted.map((row) => discoverPlacedRow(row)))
    : alreadyWritten
      ? emptyState(
          'Nothing new to place',
          everythingWritten
            ? 'The fixtures already written as devices are refused as duplicates, so nothing new was placed. They sit under Withheld because there is nothing new to add.'
            : 'Each fixture already written as a device is refused as a duplicate, so nothing new was placed; see Withheld for why each other entry was left out.'
        )
      : emptyState(
          'Nothing placed',
          'No fixture reached the evidence bar for a measured placement.'
        );

  body.push(
    el('section', { class: 'discover-section' }, [
      el('div', { class: 'discover-section-head' }, [
        el('h3', {}, 'Placed'),
        el('span', { class: 'hier-count' }, String(accepted.length)),
        el('span', { class: 'spacer' }),
        applyAll,
      ]),
      el('p', { class: 'stat-hint' },
        'Each fixture below was proven on one CT row by measured co-movement. Nothing is written until you apply it; the engine writes the accepted set.'),
      placed,
    ])
  );

  /* Groups ------------------------------------------------------------ */
  const groupSection = el('section', { class: 'discover-section' }, [
    el('div', { class: 'discover-section-head' }, [
      el('h3', {}, 'Groups'),
      el('span', { class: 'hier-count' }, String(groups.length)),
      el('span', { class: 'spacer' }),
      badge('advisory', 'info'),
    ]),
    discoverGroupsNote(groups),
    groups.length
      ? el('div', { class: 'discover-list' }, groups.map((group) => discoverGroupRow(group)))
      : el('p', { class: 'stat-hint' }, 'No co-toggling groups were found on a shared row.'),
  ]);
  body.push(groupSection);

  /* Withheld ---------------------------------------------------------- */
  body.push(
    el('section', { class: 'discover-section' }, [
      el('div', { class: 'discover-section-head' }, [
        el('h3', {}, 'Withheld'),
        el('span', { class: 'hier-count' }, String(withheldRows.length)),
        el('span', { class: 'spacer' }),
      ]),
      withheldRows.length
        ? el('div', { class: 'discover-list' },
            withheldRows.map(([entityId, reason]) =>
              el('div', { class: 'discover-row withhold' }, [
                el('div', { class: 'discover-main' }, [
                  el('span', { class: 'hier-sub' }, String(entityId)),
                  el('span', { class: 'discover-ev' }, withheldReason(reason)),
                ]),
              ])
            ))
        : el('p', { class: 'stat-hint' }, 'Nothing was withheld.'),
    ])
  );

  return card(DISCOVER_TITLE, body, {
    id: 'topology-discover',
    foot: discoveryFoot(data, stats),
  });
}

//: How many ``<entity>: <reason>`` notes are listed by name before the footer
//: groups them by reason instead.  Production returns about 25, which is over
//: 1600 characters of footer nobody reads.
const NOTE_DETAIL_LIMIT = 4;

/**
 * Split an engine note into ``{entity, reason}`` when it is an entity note.
 *
 * ``main.py`` appends two shapes to ``notes``: ``<entity_id>: <reason>`` from
 * the correlation report, and free prose for the truncation and refusal
 * counts.  Matching the entity id as ``domain.object_id`` is what keeps a
 * colon inside a sentence from being read as an entity note.  Returns
 * ``null`` for prose, which the caller passes through verbatim.
 */
function splitEntityNote(note) {
  const text = String(note == null ? '' : note);
  const at = text.indexOf(': ');
  if (at < 0) return null;
  const entity = text.slice(0, at).trim();
  const reason = text.slice(at + 2).trim();
  // An engine entity id is ``domain.object_id`` with no whitespace, so prose
  // such as "12 state entities are not candidates (...)" can never be read as
  // an entity note merely because it contains a colon.
  if (!/^[a-z0-9_]+\.[a-z0-9_]+$/.test(entity)) return null;
  // ``/[^a-z0-9_]/`` is the same bare-code test ``withheldReason`` uses: a
  // reason containing any other character is already a sentence, and
  // title-casing it would mangle it.
  if (!reason || /[^a-z0-9_]/.test(reason)) return null;
  return { entity, reason };
}

/**
 * The footer's engine-note text: no raw enum, and short enough to read.
 *
 * A few entity notes are listed one per line ("light.hall: Too few
 * independent transitions to prove it"); many are grouped by reason with a
 * count, which still states exactly how many entries each reason covered.
 * Prose notes pass through untouched and are never title-cased.
 */
function discoveryNoteText(notes) {
  const entityNotes = [];
  const prose = [];
  for (const note of notes) {
    const parsed = splitEntityNote(note);
    if (parsed) entityNotes.push(parsed);
    else prose.push(String(note));
  }

  const segments = [];
  if (entityNotes.length && entityNotes.length <= NOTE_DETAIL_LIMIT) {
    segments.push(
      entityNotes
        .map(({ entity, reason }) => `${entity}: ${withheldReason(reason)}`)
        .join('; ')
    );
  } else if (entityNotes.length) {
    const counts = new Map();
    for (const { reason } of entityNotes) {
      const phrase = withheldReason(reason);
      counts.set(phrase, (counts.get(phrase) || 0) + 1);
    }
    const grouped = Array.from(counts, ([phrase, count]) => [phrase, count])
      .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
      .map(([phrase, count]) => `${phrase} (${count})`);
    segments.push(
      `${entityNotes.length} entries not placed, by reason: ${grouped.join('; ')}`
    );
  }
  segments.push(...prose);
  return `Engine notes: ${segments.join('; ')}`;
}

function discoveryFoot(data, stats) {
  const parts = [];
  if (stats.candidate_entities !== undefined) {
    parts.push(`${stats.candidate_entities} candidate fixtures`);
  }
  if (stats.branch_rows !== undefined) parts.push(`${stats.branch_rows} branch rows`);
  // The engine echoes the window it read.  An older engine omits it, so fall
  // back to the window the card's own request was pinned to: the footer must
  // name the window the numbers came from, not a later selector change.
  const windowHours = data.lookback_hours !== undefined
    ? data.lookback_hours
    : (state.discover.ranWindow !== null ? state.discover.ranWindow : state.discover.window);
  if (windowHours !== undefined) {
    const named = discoverWindowName(windowHours);
    parts.push(`last ${num(windowHours, 0)}h${named ? ` (${named})` : ''}`);
  }
  if (data.topology_version !== undefined) parts.push(`topology v${data.topology_version}`);
  const notes = (Array.isArray(data.notes) ? data.notes : [])
    // A hard precondition repeats its sentence in both ``detail`` and
    // ``notes`` (e.g. "no topology to place devices on"), and the notice
    // above already shows it.  Printing it again as an engine note is the
    // same sentence twice on one card.
    .filter((note) => !(data.detail && String(note) === String(data.detail)));
  // With every note deduped there is nothing to report; "Analysing writes
  // nothing" would be a claim about a run that never analysed.
  if (notes.length) parts.push(discoveryNoteText(notes));
  else if (!data.detail) parts.push('Analysing writes nothing.');
  return parts.join(' · ');
}

function discoverPlacedRow(row) {
  const facts = [];
  if (row.agreement !== undefined && row.agreement !== null) facts.push(`${percent(row.agreement, 0)} agreement`);
  if (row.corroborations !== undefined && row.corroborations !== null) facts.push(`${row.corroborations} corroborations`);
  if (row.symmetry !== undefined && row.symmetry !== null) facts.push(`${num(row.symmetry, 2)} symmetry`);
  if (row.specificity_z !== undefined && row.specificity_z !== null) facts.push(`${num(row.specificity_z, 0)}σ specificity`);
  const evidence = Array.isArray(row.evidence) ? row.evidence : [];
  const button = applyButton(row);
  return el('div', { class: 'discover-row' }, [
    el('div', { class: 'discover-main' }, [
      el('div', { class: 'discover-title' }, [
        el('span', { class: 'hier-label' }, String(row.name || row.entity_id || row.device_id || 'Fixture')),
        el('span', { class: 'arrow', 'aria-hidden': 'true' }, '→'),
        el('span', { class: 'chip chip-ct' }, String(row.ct_label || row.ct_id || 'unknown CT')),
        row.applied ? badge('applied', 'ok') : null,
      ]),
      el('div', { class: 'discover-meta' }, [
        el('span', { class: 'hier-power' }, watts(row.on_w)),
        ...facts.map((fact) => el('span', { class: 'discover-fact' }, fact)),
      ]),
      evidence.length
        ? el('ul', { class: 'discover-evidence' },
            evidence.map((item) => el('li', {}, String(item))))
        : null,
    ]),
    el('div', { class: 'discover-actions' }, [button]),
  ]);
}

/**
 * The two-step Apply button.
 *
 * There is no modal library in this panel and adding one is out of scope, so
 * the confirmation *is* the button: the first click asks, the second writes.
 * A blur cancels, because the user looked away.
 *
 * ``row.all`` means "apply every accepted placement".  There is no per-entity
 * filter in the frozen facade signature, so *both* buttons write the proven
 * set together — the row button is a shortcut to that same engine decision,
 * never a client-side subset.  The label and toast say so; pretending a
 * per-row write happened would be a lie about what the engine did.
 */
function applyButton(row) {
  let armed = false;
  const isAll = Boolean(row && row.all);
  const idle = isAll ? 'Apply all' : 'Apply';
  const scope = isAll
    ? 'Write every proven placement.'
    : 'The engine writes the proven set together; this fixture is included.';
  const button = el('button', {
    class: 'btn btn-sm',
    type: 'button',
    title: scope,
    'aria-label': isAll ? 'Apply all placements' : `Apply ${row.name || row.entity_id || 'fixture'}`,
    onclick: async () => {
      if (!armed) {
        armed = true;
        button.textContent = 'Confirm apply?';
        button.setAttribute('aria-label', 'Confirm apply');
        button.dataset.armed = 'true';
        return;
      }
      if (button.disabled) return;
      button.disabled = true;
      button.textContent = 'Applying…';
      try {
        // ``apply: true`` has no per-entity filter in the engine contract:
        // the engine decides the accepted set, and re-deriving a subset in the
        // browser would be a second placement decision.  So both the per-row
        // and the Apply-all button write the proven set, and the toast says so
        // instead of claiming only one device changed.
        //
        // A 2xx does *not* mean a write: a refusal comes back ``ok: true``
        // with ``applied: false`` and a ``detail`` (auto placement disabled,
        // apply not requested).  Claiming "applied" there is a false success,
        // so the toast is chosen by the flag, not by the HTTP status.
        //
        // The window is the one the analysis on this card was run with, not
        // the selector's current value: an apply must write the evidence the
        // user just reviewed.  ``runDiscovery`` pins that value in
        // ``state.discover.ranWindow`` when the read-only request is sent.
        // Apply is asynchronous too: start the job and poll until it finishes,
        // then read the write result from ``outcome.result``.  ``startDiscoveryJob``
        // cleans up its timer on every terminal path.
        const outcome = await startDiscoveryJob({
          apply: true,
          // ``ranWindow`` is null only before any run; there is no card to
          // apply from then, so the selected window is the safe fallback.
          lookback_hours: state.discover.ranWindow !== null
            ? state.discover.ranWindow
            : state.discover.window,
        });
        if (outcome.error || outcome.timedOut) {
          throw outcome.error || new Error('Applying the placements timed out.');
        }
        if (outcome.cancelled) return;
        const result = outcome.result;
        if (result && result.applied === true) {
          notify(
            'Placements applied',
            isAll
              ? 'The engine wrote every proven placement and re-projected the topology.'
              : `The engine wrote every proven placement, including ${row.name || row.entity_id}, and re-projected the topology.`
          );
          // The cached analysis is now stale: the written rows carry
          // ``applied``, so re-showing the old answer would misreport them.
          // The chosen window survives, so the next Discover re-reads it.
          state.discover = {
            data: null,
            running: false,
            error: null,
            window: state.discover.window,
            ranWindow: null,
          };
        } else {
          // Say only what happened: the call returned no write.  Relay the
          // engine's own explanation (it names the switch to turn on) and add
          // the one mechanical fact -- nothing changed.
          const raw = result && result.detail ? String(result.detail).trim() : '';
          const detail = raw
            ? `${raw.charAt(0).toUpperCase()}${raw.slice(1).replace(/[.\s]+$/, '')}. `
            : '';
          notify(
            'Nothing changed',
            `${detail}No placement was made; the topology is unchanged.`,
            'warn'
          );
          // Nothing was written, so the analysis on the card is still the
          // current one: keep it (and its explanation) rather than blanking
          // the panel after a refusal.  Reset the button in case this render
          // does not reach the discover slot.
          armed = false;
          button.disabled = false;
          button.dataset.armed = 'false';
          button.textContent = idle;
          state.discover = { ...state.discover, running: false };
        }
        render();
      } catch (err) {
        notify('Could not apply placement', describeError(err), 'bad');
        armed = false;
        button.disabled = false;
        button.dataset.armed = 'false';
        button.textContent = idle;
      }
    },
    onblur: () => {
      if (!armed) return;
      armed = false;
      button.dataset.armed = 'false';
      button.textContent = idle;
      button.setAttribute(
        'aria-label',
        row && row.all ? 'Apply all placements' : `Apply ${row.name || row.entity_id || 'fixture'}`
      );
    },
  }, idle);
  return button;
}

/* --- Groups ---------------------------------------------------------- */

// ``individually_identifiable`` is the engine's statement that the aggregate
// fit separated a real coefficient for this group's member.  Only a literal
// ``true`` is evidence: an older engine omits the key, and a missing key must
// never be read as "a measurement happened".  Everything else renders as the
// conservative ``grouped`` claim.
const groupIdentifiable = (group) =>
  Boolean(group && group.individually_identifiable === true);

// The coefficient the engine fitted for one member, or ``null`` when it gave
// none.  ``Number('')`` is ``0``, which would be a fabricated wattage, so the
// empty string is rejected before the numeric coercion.
const memberWatts = (member) => {
  const value = member && member.estimated_w;
  if (value === null || value === undefined || value === '') return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
};

/**
 * The Groups section used to warn, once, that *no* group was individually
 * identifiable.  That was true when the engine hard-coded the flag to false;
 * it is false now, because a single-fixture group carries its own measured
 * coefficient.  So the warning is emitted only when at least one group really
 * is unidentifiable, and in a mixed section it counts how many and defers the
 * per-row badge to say which.  An all-identified section gets a neutral note
 * instead, so "never a device" is still stated without the false warning.
 */
function discoverGroupsNote(groups) {
  if (!groups.length) return null;
  const unidentified = groups.filter((group) => !groupIdentifiable(group));
  if (!unidentified.length) {
    return el('p', { class: 'stat-hint' },
      'Every group here is marked identified by the aggregate fit, so its fitted draw is a separated coefficient rather than an unexplained aggregate. A group is advisory evidence only and never becomes a device.');
  }
  const mixed = unidentified.length < groups.length;
  const identifiedCount = groups.length - unidentified.length;
  // The body describes what each badge means and what is on screen.  It never
  // claims a split that was measured, and it never claims the warning applies
  // to an identified row.  "Identified" is the engine's statement, not ours.
  // The co-toggle sentence is conditional because a one-member group has no
  // sibling to toggle with, and the engine may still decline to identify it.
  const coToggle = unidentified.some(
    (group) => (Array.isArray(group.members) ? group.members.length : 0) > 1
  )
    ? ' Where the members toggle together the combined draw is measured; the split between them is not.'
    : '';
  const body = mixed
    ? `The ${unidentified.length} ${unidentified.length === 1 ? 'group' : 'groups'} badged grouped ${unidentified.length === 1 ? 'has' : 'have'} no identified per-fixture wattage, so only the group total is shown. The ${identifiedCount} ${identifiedCount === 1 ? 'group' : 'groups'} badged identified ${identifiedCount === 1 ? 'carries' : 'carry'} a coefficient the aggregate fit separated. No group becomes a device.`
    : `${groups.length === 1 ? 'This group has' : 'None of these groups has'} no identified per-fixture wattage, so only the group total is shown.${coToggle} No device is created.`;
  return el('div', { class: 'notice', dataset: { tone: 'warn' }, style: 'margin-bottom:12px' }, [
    el('div', { class: 'banner-icon', 'aria-hidden': 'true' }, 'i'),
    el('div', {}, [
      el('strong', {}, mixed
        ? `Not individually identifiable for ${unidentified.length} of ${groups.length} groups`
        : 'Not individually identifiable'),
      el('p', {}, body),
    ]),
  ]);
}

// The tooltip must never claim a per-member coefficient that the row does
// not show.  A singleton with a fitted ``estimated_w`` gets the strong claim;
// every other identified shape only repeats what the engine reported.
function identifiedTitle(soloW) {
  if (soloW !== null) {
    return 'The aggregate fit identified this one fixture on its own; the wattage is a fitted coefficient, not a split of a group total';
  }
  return 'The engine reports this group as individually identifiable: the aggregate fit separated its coefficient, so the group total is a fitted value';
}

// A group the engine did not mark identifiable must not claim its members
// "toggle together" when the row holds fewer than two of them -- there is
// nothing to toggle with.  The honest claim is the absence of a separated
// coefficient, so only the group total is shown.
const groupedTitle = (members) => {
  if (members.length > 1) return 'The members toggle together; the split is not measured';
  if (members.length === 1) {
    return 'The engine did not report an identified coefficient for this fixture, so only the group total is shown';
  }
  return 'The engine did not identify a per-fixture coefficient for this group, so only the group total is shown';
};

function discoverGroupRow(group) {
  const members = Array.isArray(group.members) ? group.members : [];
  const identified = groupIdentifiable(group);
  const margin = group.r2_margin !== undefined && group.r2_margin !== null
    ? `${num(group.r2_margin, 2)} margin over null`
    : null;
  const r2 = group.r2 !== undefined && group.r2 !== null ? `R² ${num(group.r2, 3)}` : null;
  const evidence = Array.isArray(group.evidence) ? group.evidence : [];
  // Only a *single-member* group may carry a per-member figure, and only when
  // the engine actually reported one.  The frozen contract sets
  // ``estimated_w`` to ``null`` for every member of a multi-fixture group,
  // and this panel never divides ``total_w`` by the member count -- a split
  // would be a fabrication, not a measurement.
  const soloW = identified && members.length === 1 ? memberWatts(members[0]) : null;
  const facts = [r2, margin].filter(Boolean);
  return el('div', { class: identified ? 'discover-row group identified' : 'discover-row group' }, [
    el('div', { class: 'discover-main' }, [
      el('div', { class: 'discover-title' }, [
        el('span', { class: 'chip chip-ct' }, String(group.ct_label || group.ct_id || 'unknown CT')),
        el('span', { class: 'hier-power' }, watts(group.total_w)),
        // A singleton shows a second wattage on its member chip, so the total
        // is captioned here; without it two equal numbers would look like a
        // contradiction rather than one measurement stated twice.
        soloW !== null ? el('span', { class: 'discover-fact' }, 'group total') : null,
        identified
          ? badge('identified', 'ok', { title: identifiedTitle(soloW) })
          : badge('grouped', 'warn', { title: groupedTitle(members) }),
      ]),
      el('div', { class: 'discover-meta' }, facts
        .map((fact) => el('span', { class: 'discover-fact' }, fact))),
      members.length
        ? el('div', { class: 'discover-members' },
            members.map((member) => discoverGroupMember(member, soloW !== null)))
        : null,
      evidence.length
        ? el('ul', { class: 'discover-evidence' },
            evidence.map((item) => el('li', {}, String(item))))
        : null,
    ]),
  ]);
}

// The member chip is captioned "identified" only when the engine actually
// reported a coefficient for it.  A group flagged identifiable whose member
// carries no number must not have a wattage (or the word "identified")
// attributed to that member.
function discoverGroupMember(member, isSolo) {
  const name = String((member && (member.name || member.entity_id)) || 'member');
  const value = isSolo ? memberWatts(member) : null;
  if (value === null) return el('span', { class: 'chip' }, name);
  return el('span', {
    class: 'chip discover-member-identified',
    title: 'This is the only member; the aggregate fit identified its draw directly',
  }, [
    el('span', { class: 'discover-member-tag' }, 'identified'),
    name,
    el('span', { class: 'discover-member-watts' }, watts(value)),
  ]);
}

/* --- Dynamic-topology learning banner (contract §5.4 item 7, OD-51) ---- *
 * Since OD-51 the current topology is always authoritative for inference
 * and learning.  The backend now publishes an informational ``topology_note``
 * (with ``remedy: None``) for a dynamic topology, and ``blocked`` reflects
 * only genuinely contradictory sample evidence — never an unconfirmed
 * topology.  This banner surfaces that note as status, not as a gate.  It
 * also tolerates older payloads that still report ``topology_not_confirmed``
 * as a block: those render as the same informational note, never as a
 * halt-and-confirm gate.
 * ---------------------------------------------------------------------- */

function topologyLearningBanner(status, topology) {
  const learning = (status && status.learning) || {};
  // ``/status`` nests the full accounting under ``details`` in some payloads;
  // older engines used a top-level ``learning_status``.  Accept either so a
  // stale backend cannot reintroduce the old "paused" message.
  const detail = (status && status.learning_status)
    || ((status && status.details && status.details.learning_status) || {});
  const effective = String(learning.effective_state || detail.effective_state || '');
  const blockedDetail = learning.blocked_reason_detail || detail.blocked_reason_detail || null;
  const blockedBy = learning.blocked_by || detail.blocked_by || null;
  const topologyNote = learning.topology_note || detail.topology_note || null;
  const quality = String(
    learning.topology_quality || detail.topology_quality || ''
  ).toLowerCase();

  // A structural topology has no reduced-confidence note to show (ADR-0043).
  if (topology && topology.status === 'structural') return null;

  // The backend's informational note is the primary signal now.  Fall back
  // to a candidate quality when a payload predates the note field.
  const dynamic =
    Boolean(topologyNote)
    || quality === 'candidate'
    || quality === 'unconfirmed'
    || quality === 'unknown';

  // Gracefully treat a legacy topology-based block as informational.  It is
  // NOT a reason to tell the user learning is halted or confirmation is due.
  const legacyTopologyBlock =
    effective === 'blocked'
    && (blockedDetail === 'topology_not_confirmed'
      || blockedBy === 'unconfirmed_topology'
      || (blockedBy === 'observed_rejections' && quality === 'candidate'));
  if (!dynamic && !legacyTopologyBlock) return null;

  const note = topologyNote
    || 'The current topology is authoritative for inference and learning. '
      + 'Candidate wiring carries a reduced confidence score; samples are '
      + 'collected with topology confidence tracked.';
  return el('div', { class: 'notice', dataset: { tone: 'info' } }, [
    el('div', { class: 'banner-icon', 'aria-hidden': 'true' }, 'i'),
    el('div', { class: 'notice-body' }, [
      el('strong', {}, STRINGS.topology_dynamic + ' — learning is active'),
      el('p', {}, note),
      el('button', {
        class: 'btn btn-sm',
        type: 'button',
        onclick: () => slots.reviewTopologyEdges(),
        title: 'Pin edges one by one in the edges card; learning and results already use the active topology',
      }, 'Review edges'),
    ]),
  ]);
}

export {
  DISCOVER_PATH,
  discoverStartedAt,
  WITHHELD_REASONS,
  ALREADY_DECLARED_REASON,
  isAlreadyWritten,
  withheldReason,
  discoverSlotNode,
  discoverWindowSelect,
  selectDiscoverWindow,
  topologyDiscoverSlot,
  swapDiscover,
  discoverLoadingCard,
  discoverErrorCard,
  discoveryStatusOutcome,
  DISCOVERY_MAX_POLLS,
  DISCOVERY_POLL_MS,
  startDiscoveryJob,
  runDiscovery,
  discoverCard,
  NOTE_DETAIL_LIMIT,
  splitEntityNote,
  discoveryNoteText,
  discoveryFoot,
  discoverPlacedRow,
  applyButton,
  groupIdentifiable,
  memberWatts,
  discoverGroupsNote,
  identifiedTitle,
  groupedTitle,
  discoverGroupRow,
  discoverGroupMember,
  topologyLearningBanner
};
