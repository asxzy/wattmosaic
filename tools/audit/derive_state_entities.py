"""Derive evidence-based state-entity links for a multi-channel CT topology.

Why this tool exists
--------------------
A CT channel gives us **power**, not identity.  To infer a device's state
(and therefore to learn its power signature) WattMosaic needs the HA entity that
says whether that appliance is on.  The association between the two is not
discoverable from names:

* a first pass that matched on name tokens produced nonsense — a channel whose
  name suggested one appliance was linked to a completely different one, because
  a circuit name describes where the wiring was *originally intended* to go, not
  what is actually plugged in.
* the HA entity registry often offers no help either: an installation can have
  **no entity labels, no aliases and no device labels**, so there is no
  declarative grouping to read.

The only honest evidence is *behaviour*: does this circuit's power level
separate according to that entity's state.  This tool measures exactly that from
the recorder's own history (``history/history_during_period`` — a read-only
query) and emits a reviewable candidate file.  It never calls a service, never
writes to Home Assistant, and never touches a device.

How the decision is made
------------------------
1. **State-gated level test.**

   The entity's labels are split into quiet/active by the **measured power** each
   label is associated with — never by the label text, because a complex
   controller may report its "heating" state at a *lower* power than its
   "cooling" state, the opposite of the engine's global on/off vocabulary.

   Two questions are then asked and *both* must hold:

   * sensitivity — when the entity is active, is the circuit high?
   * specificity — when the entity is quiet, is the circuit low?

   They are scored as balanced accuracy ``(sensitivity + specificity) / 2``,
   which is invariant to duty cycle.  Plain agreement is unusable here: a device
   that is on 30 % of the time agrees with an unrelated circuit ~0.97 of the
   time (measured).  An earlier version of this tool used Jaccard and, because a
   5-transition series cannot produce a high Jaccard, reported the 11 kW EV
   charger as "no entity switches with this circuit".

2. **Coverage.**  Statistical association is not enough — the entity must be the
   *load on this circuit*.  ``separation / active_level`` must exceed
   :data:`MIN_COVERAGE`.  This rejects e.g. a media player on a server-rack
   channel: it can track the rack (balanced accuracy 0.995) merely because both
   are evening-active, yet it adds tens of watts to a few-hundred-watt rack, so
   it cannot be the rack.  Without this rule the tool would fabricate that link.

3. **Relocation null.**  The strict null keeps the entity's exact ON-episode
   lengths and duty cycle and only moves them to random positions; the observed
   score must beat that distribution by :data:`MIN_NULL_MARGIN`.  This is what
   rejects an exterior-light entity on a different circuit and a filter-purge
   flag on an AC circuit, both of which reach 0.95+ by coincidence with a
   duty-cycled circuit.

4. **Circularity.**  A template sensor whose value *is* the same CT (measured
   correlation 0.998 in one installation) is not independent evidence.  Any
   entity whose state is reproducible as a threshold of the same CT at the
   winning lag is reported ``circular`` and never used as evidence.

5. **Same-appliance grouping.**  One physical appliance exposes many facets:
   a spa reports a ``water_heater``, a heating binary sensor, a filter-status
   sensor, an in-use sensor and a pump fan, all from one HA device.  Treating
   those as independent loads would "detect" that the circuit is shared and so
   reject the true appliance.  Facets are grouped by the registry's
   own ``device_id`` — a declarative fact recorded by the integration, not name
   similarity — and one representative is chosen.  When several facets qualify,
   the one needing the fewest state overrides wins, so a plain ``on``/``off``
   binary sensor beats a ``Heating``/``Cooling`` controller.

6. **Genuine confounds are still caught.**  After grouping, two entities from
   *different* HA devices that each add power while the other is already on mean
   the circuit carries two independent loads: one CT feeding two separately
   switched sockets shows a 2×2 cross-tab of distinct power levels.  That device
   is reported ``confounded`` and left unmapped.

   When two independent entities both fully explain the circuit but can never be
   observed separately (a main dimmer and a nested pot dimmer, where
   "pot on, main off" never happens), the tool reports ``ambiguous`` and names
   both.  The spec forbids resolving that by guessing, so it stays unmapped.

7. **Channel sanity.**  A CT that reads negative all week cannot be a load CT on
   an installation with no generation source (no solar or battery entity exists),
   so it is reported as a ``polarity=inverted`` candidate with the negative
   fraction as evidence.  A channel that never moves is reported dead and mapped to nothing.

Statuses
--------
``supported``   one entity passes every check.
``weak``        one entity passes a looser screen (useful, not proven).
``confounded``  the circuit carries several independently switched loads.
``ambiguous``   several entities explain it and cannot be told apart.
``unsupported`` no entity describes this circuit (or the channel is dead).

The output is a *candidate* file.  It is consumed by ``build_topology.py`` to
populate ``DeviceTopology.metadata["state_entity_ids"]``; the topology stays
``status="candidate"`` and the user still has to confirm it.

Usage::

    python tools/audit/derive_state_entities.py                 # live, 7 days
    python tools/audit/derive_state_entities.py --days 3
    python tools/audit/derive_state_entities.py --from-cache /tmp/evidence_7d.json
    python tools/audit/derive_state_entities.py --out data/state_entity_candidates.json
"""

from __future__ import annotations

import argparse
import json
import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"

#: Domains that can express "this appliance is on".
CANDIDATE_DOMAINS = frozenset(
    {
        "binary_sensor",
        "climate",
        "cover",
        "fan",
        "humidifier",
        "input_boolean",
        "light",
        "media_player",
        "siren",
        "switch",
        "valve",
        "water_heater",
    }
)

#: States that mean "we do not know"; they are never evidence of on *or* off.
UNKNOWN_STATES = frozenset({"unavailable", "unknown", "", "none"})

#: Minimum number of real state episodes for an entity to be considered.
MIN_TRANSITIONS = 4
#: Half-width of the window that defines "changed at the same instant".
COINCIDENCE_TOL_S = 90.0
#: Grid resolution (s) for the lag sweep.
GRID_S = 60.0
#: Lag sweep, because template and cloud-polled entities lag the CT by up to
#: several minutes (measured: 0 s Lutron/Gecko, 240 s template humidifier,
#: 420 s cloud EV charger).
MAX_LAG_S = 600.0
#: Minimum readings a label needs before its median is trusted.
MIN_LABEL_SAMPLES = 20
#: Minimum absolute power separation for a label split to be meaningful (W).
MIN_SEPARATION_W = 20.0
#: Separation must also exceed this many robust sigmas of the quiet level.
SEPARATION_SIGMAS = 6.0
#: The entity's step must account for at least this fraction of the circuit's
#: active power — "this entity is the load", not "this entity correlates".
MIN_COVERAGE = 0.50
#: Circuit recall: P(entity active | circuit high).  Balanced accuracy is a
#: *per-entity* score and rewards a short-duty flag for high specificity: a
#: short-duty lighting switch can reach balanced accuracy 0.98 while being on
#: for just 23 % of the circuit's high periods (measured).  Requiring the entity
#: to actually cover the circuit's high periods is what separates "the load on
#: this circuit" from "a switch that happens to precede it".  Measured for true
#: links: 0.87-0.999; for false candidates: 0.02-0.63.
MIN_CIRCUIT_RECALL = 0.85
#: A competitor only creates genuine ambiguity if it, too, covers the circuit.
MIN_AMBIGUITY_CIRCUIT_RECALL = 0.85
#: Above this, the entity covers essentially every high period of the circuit,
#: which is what a `supported` (rather than `weak`) verdict claims.  Measured in
#: one evaluation: genuine links span 0.90-0.999, while every rejected
#: competitor tops out at 0.63.
SUPPORTED_CIRCUIT_RECALL = 0.90
#: If the best entity covers the circuit this much better than the runner-up,
#: the contest is settled and is not ambiguity.
MIN_RECALL_LEAD = 0.15
#: A confound member must explain a real share of the circuit's high periods,
#: otherwise the "additive" pair is two unrelated loads that both happen to
#: precede it (measured: a pair of independent lighting switches, circuit recall
#: 0.23, produced a fake additive structure on one lighting channel).
MIN_CONFOUND_CIRCUIT_RECALL = 0.50
#: A confound member's own two labels must separate circuit power by at least
#: this much; below it the entity carries no measurable load of its own and
#: cannot be one half of a two-load circuit.
MIN_CONFOUND_SEPARATION_W = 5.0
#: Screen: lag-0 balanced accuracy needed to reach the full analysis.
SCREEN_BALANCED_ACCURACY = 0.60
#: Acceptance thresholds.
SUPPORTED_BALANCED_ACCURACY = 0.90
WEAK_BALANCED_ACCURACY = 0.85
SUPPORTED_SENSITIVITY = 0.85
SUPPORTED_SPECIFICITY = 0.85
#: The observed score must beat the relocation null by this much.  The null keeps
#: the entity's exact duty cycle and episode lengths, so this subtracts everything
#: base rate can explain.
MIN_NULL_MARGIN = 0.10
#: A huge step (an 11 kW EV charger) is unambiguous at very few transitions.
STRONG_MAGNITUDE_W = 1000.0
MIN_TRANSITIONS_STRONG = 3
#: Polarity: a load CT that is almost always negative cannot be a load CT unless
#: the house exports power.  These fractions are the evidence.
POLARITY_NEGATIVE_FRACTION = 0.90
POLARITY_POSITIVE_FRACTION = 0.02
POLARITY_NEGATIVE_W = -10.0
POLARITY_POSITIVE_W = 10.0
#: A channel is "dead" when it never moves by more than this over the whole week.
DEAD_CHANNEL_SPAN_W = 10.0
#: Confound: a second independent entity must add at least this much power while
#: the first is already on, *and* carry it alone.
MIN_MARGINAL_EFFECT_W = 15.0
#: Both cross-tab cells needed to measure a marginal effect must have this many
#: readings, otherwise "no effect" may just be absent data.
MIN_CROSSTAB_SAMPLES = 15
#: The "B on while A off" cell needs real *support* before B counts as a separate
#: load.  With only occasional co-occurrence a pair of unrelated evening-active
#: circuits looks additive: measured on one lighting channel a nested switch was
#: "on alone" for 74 of 40 000 samples and produced a fake 19 W marginal.
MIN_SOLO_SAMPLES = 200
#: Number of random relocations used for the strict null.
NULL_RESAMPLES = 200
#: A relocation null needs several independent ON episodes to mean anything.
#: With a single episode, "relocating" it is a circular shift, and the best of
#: 200 shifts lands within a few samples of the truth by construction — the null
#: would equal the observation and reject a real link (measured: the EV charger,
#: one 4-hour charging session).  Below this count the null is reported as not
#: testable and a strong-magnitude link may still pass on physical grounds.
MIN_NULL_EPISODES = 4
#: Seed for that null so a report is reproducible from the same history.
NULL_SEED = 20260101
#: Two branch CTs whose 60 s series correlate at least this strongly are flagged
#: as possible measurements of the same current.  Measured in this installation:
#: unrelated branch pairs stay below 0.72 while suspected duplicates reach 0.81
#: and 0.91 (and true double-pole legs 1.00).
MIN_DUPLICATE_CORRELATION = 0.75
#: Correlation alone cannot tell "one conductor, two clamps" from "two loads that
#: happen to run together".  The discriminator is ratio stability: if B = k * A
#: for a fixed k, the two clamps are measuring the same current.  Measured: one
#: duplicate pair gave ratio 0.539 with relative IQR 0.005 and a 0.15 W median
#: residual (one conductor), while two co-varying but distinct circuits
#: correlated at 0.838 with relative IQR 0.722.
MIN_DUPLICATE_RELATIVE_IQR = 0.05
#: And the residual around the fitted ratio must be small in absolute watts,
#: otherwise a noisy channel with a stable ratio is still not the same reading.
MIN_DUPLICATE_RESIDUAL_W = 2.0
#: Ratio stability is only meaningful while at least one channel is carrying a
#: real load; near zero both readings are noise and their ratio is meaningless.
MIN_DUPLICATE_RATIO_W = 10.0


# --------------------------------------------------------------------------- #
# Series extraction
# --------------------------------------------------------------------------- #
def numeric_series(rows: Sequence[Mapping[str, Any]] | None) -> tuple[np.ndarray, np.ndarray]:
    """``(times, values)`` for a numeric recorder series, sorted, finite only.

    ``minimal_response=True`` history rows carry only ``s`` and ``lu``, so
    ``lu`` (last_updated) is the only timestamp available.  For a *power* sensor
    an attribute update does not change the value, so duplicate timestamps are
    harmless here; it is the state series where ``lu`` is unsafe and the caller
    must use :func:`state_episodes`, which de-duplicates consecutive values.
    """
    if not rows:
        return np.empty(0, dtype=float), np.empty(0, dtype=float)
    times = np.empty(len(rows), dtype=float)
    values = np.empty(len(rows), dtype=float)
    n = 0
    for row in rows:
        try:
            raw_ts = row.get("lu") if row.get("lu") is not None else row.get("lc")
            ts = float(raw_ts)
            value = float(row.get("s"))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(ts) or not math.isfinite(value) or ts <= 0:
            continue
        times[n] = ts
        values[n] = value
        n += 1
    times = times[:n]
    values = values[:n]
    order = np.argsort(times, kind="stable")
    return times[order], values[order]


def state_episodes(rows: Sequence[Mapping[str, Any]] | None) -> list[tuple[float, str]]:
    """``[(t, label), …]`` collapsed to real state changes.

    Recorder history with ``minimal_response=True`` omits ``last_changed``, so
    consecutive rows with the same value are *not* separate transitions.  This
    matters a lot: one climate entity emitted 201 rows over 7 days but only 76
    real state changes, because ``lu`` also moves on attribute updates.
    Counting rows would make it look like it switches every 50 min.
    """
    raw: list[tuple[float, str]] = []
    for row in rows or []:
        state = row.get("s")
        if state is None:
            continue
        try:
            raw_ts = row.get("lu") if row.get("lu") is not None else row.get("lc")
            ts = float(raw_ts)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(ts) or ts <= 0:
            continue
        raw.append((ts, str(state).strip()))
    raw.sort(key=lambda item: item[0])
    episodes: list[tuple[float, str]] = []
    for ts, label in raw:
        if episodes and episodes[-1][1] == label:
            continue
        episodes.append((ts, label))
    return episodes


def aggregate_power(
    series: Sequence[tuple[np.ndarray, np.ndarray]],
) -> tuple[np.ndarray, np.ndarray] | None:
    """Sum several CT legs on the first leg's timeline (the device total).

    A double-pole device is two CTs; its power is the sum of both legs, never
    either leg alone.  Legs sampled at slightly different instants are joined by
    linear interpolation, which is adequate at a 15.7 s cadence.
    """
    parts = [item for item in series if item[0].size]
    if not parts:
        return None
    times = parts[0][0]
    total = parts[0][1].astype(float).copy()
    for other_t, other_v in parts[1:]:
        total += np.interp(times, other_t, other_v)
    return times, total


def _robust_sigma(values: np.ndarray) -> float:
    if values.size < 2:
        return 0.0
    centre = float(np.median(values))
    return float(1.4826 * np.median(np.abs(values - centre)))


# --------------------------------------------------------------------------- #
# Labelling
# --------------------------------------------------------------------------- #
def _sample_labels(
    times: np.ndarray, episodes: Sequence[tuple[float, str]]
) -> np.ndarray:
    """Label in force at each time (times before the first episode get it)."""
    if not episodes:
        return np.empty(0, dtype=object)
    episode_times = np.array([item[0] for item in episodes], dtype=float)
    labels = np.array([item[1] for item in episodes], dtype=object)
    index = np.clip(
        np.searchsorted(episode_times, times, side="right") - 1, 0, len(episodes) - 1
    )
    return labels[index]


def label_table(
    times: np.ndarray, values: np.ndarray, episodes: Sequence[tuple[float, str]]
) -> dict[str, tuple[float, int]]:
    """``label -> (median power while in that label, sample count)``.

    Grouped with one argsort rather than one boolean mask per episode: this runs
    for ~1 900 entities per device, and the mask version took ~5 s/device.
    """
    if not episodes or times.size == 0:
        return {}
    episode_times = np.array([item[0] for item in episodes], dtype=float)
    labels = np.array([item[1] for item in episodes], dtype=object)
    index = np.searchsorted(episode_times, times, side="right") - 1
    valid = index >= 0
    if not valid.any():
        return {}
    index = index[valid]
    vals = values[valid]
    order = np.argsort(index, kind="stable")
    sorted_index = index[order]
    sorted_values = vals[order]
    starts = np.flatnonzero(np.r_[True, sorted_index[1:] != sorted_index[:-1]])
    ends = np.r_[starts[1:], sorted_index.size]

    chunks: dict[str, list[np.ndarray]] = {}
    for start, end in zip(starts, ends, strict=False):
        position = int(sorted_index[start])
        if position >= len(labels):
            continue
        chunks.setdefault(str(labels[position]), []).append(sorted_values[start:end])

    # The minimum applies to the *label total*, never to a single episode.  A
    # short episode is still real evidence of the level that label implies: the
    # HRV's upper socket turns off in 466 brief episodes, and dropping them as
    # "too short to trust" raised the measured quiet median from 8 W to 141 W
    # and fabricated a 0.07 coverage ratio for a genuine 144 W load.
    table: dict[str, tuple[float, int]] = {}
    for label, parts in chunks.items():
        chunk = parts[0] if len(parts) == 1 else np.concatenate(parts)
        if chunk.size >= MIN_LABEL_SAMPLES:
            table[label] = (float(np.median(chunk)), int(chunk.size))
    return table


def split_labels(
    table: Mapping[str, tuple[float, int]],
) -> tuple[list[str], list[str], float, float] | None:
    """Split labels into (quiet, active) by the largest gap in median power.

    The split is chosen from *measured power*, not from the label text, so a
    complex controller splits correctly even when its "cooling" label is the
    low-power state — and the engine then needs a per-device override saying
    ``cooling`` is *off*, because the global ``ON_STATES`` contains it.
    """
    labels = [label for label in table if label.strip().lower() not in UNKNOWN_STATES]
    if len(labels) < 2:
        return None
    labels.sort(key=lambda label: table[label][0])
    medians = [table[label][0] for label in labels]
    gaps = [(medians[i + 1] - medians[i], i) for i in range(len(medians) - 1)]
    _gap, position = max(gaps, key=lambda item: item[0])
    quiet, active = labels[: position + 1], labels[position + 1 :]
    if not quiet or not active:
        return None
    return quiet, active, medians[position], medians[position + 1]


def _episodes_span(
    times: np.ndarray, episodes: Sequence[tuple[float, str]]
) -> tuple[float, float]:
    return (
        max(float(times[0]), float(episodes[0][0])),
        min(float(times[-1]), float(episodes[-1][0])),
    )


# --------------------------------------------------------------------------- #
# Statistics
# --------------------------------------------------------------------------- #
def balanced_accuracy(circuit_on: np.ndarray, state_on: np.ndarray) -> float:
    """``(sensitivity + specificity) / 2``; invariant to the entity's duty cycle.

    ``state_on`` is the *prediction*: it says "the circuit should be high now".
    Sensitivity is the fraction of predicted-high samples where the circuit is
    actually high; specificity is the fraction of predicted-low samples where it
    is actually low.  Requiring both prevents an always-on entity from scoring
    well by matching a constantly-loaded circuit.
    """
    if circuit_on.size == 0 or state_on.size != circuit_on.size:
        return 0.0
    predicted_high = int(state_on.sum())
    predicted_low = int(state_on.size - predicted_high)
    if predicted_high == 0 or predicted_low == 0:
        return 0.0
    true_positive = float(np.sum(circuit_on & state_on))
    true_negative = float(np.sum(~circuit_on & ~state_on))
    return 0.5 * (true_positive / predicted_high + true_negative / predicted_low)


def _components(circuit_on: np.ndarray, state_on: np.ndarray) -> dict[str, float]:
    predicted_high = int(state_on.sum())
    predicted_low = int(state_on.size - predicted_high)
    true_positive = float(np.sum(circuit_on & state_on))
    true_negative = float(np.sum(~circuit_on & ~state_on))
    return {
        "balanced_accuracy": 0.5
        * (
            (true_positive / predicted_high if predicted_high else 0.0)
            + (true_negative / predicted_low if predicted_low else 0.0)
        ),
        "sensitivity": true_positive / predicted_high if predicted_high else 0.0,
        "specificity": true_negative / predicted_low if predicted_low else 0.0,
    }


def score_at_lag(
    circuit_on: np.ndarray,
    state_on: np.ndarray,
    *,
    grid_s: float,
    max_lag_s: float,
) -> dict[str, float]:
    """Best balanced accuracy over a lag sweep, plus its components."""
    best: dict[str, float] | None = None
    max_shift = int(round(max_lag_s / grid_s))
    for shift in range(-max_shift, max_shift + 1):
        if shift == 0:
            predicted, observed = state_on, circuit_on
        elif shift > 0:
            # The entity reported its state `shift` samples late: compare the
            # state now with the circuit `shift` samples ago.
            predicted, observed = state_on[shift:], circuit_on[:-shift]
        else:
            predicted, observed = state_on[:shift], circuit_on[-shift:]
        score = balanced_accuracy(observed, predicted)
        if best is None or score > best["balanced_accuracy"]:
            best = {**_components(observed, predicted), "lag_s": float(shift * grid_s)}
    return best or {
        "balanced_accuracy": 0.0,
        "sensitivity": 0.0,
        "specificity": 0.0,
        "lag_s": 0.0,
    }


def derivable_mismatch(
    values: np.ndarray, active_mask: np.ndarray
) -> tuple[int, float]:
    """Can the state be reproduced as a threshold of this CT?

    Returns ``(minimum mismatching samples, threshold)``.  ``0`` means the entity
    is a deterministic function of the channel — circular evidence that must not
    be presented as independent confirmation.  Evaluated at the winning lag,
    because a template derived from this CT aligns perfectly at its own lag.
    """
    usable = int(values.size)
    if usable == 0 or active_mask.size != usable:
        return usable, float("nan")
    target = active_mask.astype(np.int8)
    if target.min() == target.max():
        return usable, float("nan")
    percentiles = np.unique(np.percentile(values, np.linspace(1, 99, 197)))
    best_mismatch = usable
    best_threshold = float("nan")
    for threshold in percentiles:
        mismatch = int(np.sum((values > threshold).astype(np.int8) != target))
        if mismatch < best_mismatch:
            best_mismatch = mismatch
            best_threshold = float(threshold)
            if best_mismatch == 0:
                break
    return best_mismatch, best_threshold


def relocation_null(
    circuit_on: np.ndarray,
    state_on: np.ndarray,
    *,
    seed: int = NULL_SEED,
    resamples: int = NULL_RESAMPLES,
) -> float:
    """Best balanced accuracy when ON episodes are moved to random positions.

    This is the strict null.  It preserves the entity's exact duty cycle, its
    ON-episode lengths and their count — everything that drives base-rate
    agreement — and changes only *when* they happen.  A schedule-driven entity
    that happens to overlap an unrelated circuit scores high here too, so
    requiring the observed score to beat this distribution is a real test.

    Returns ``0.0`` when there are too few episodes to relocate meaningfully; the
    caller must not treat that as "passed the null" — see :func:`null_is_testable`.
    """
    n = int(circuit_on.size)
    if n == 0 or state_on.size != n:
        return 0.0
    padded = np.concatenate([[False], state_on.astype(bool), [False]])
    transitions = np.diff(padded.astype(np.int8))
    starts = np.flatnonzero(transitions == 1)
    ends = np.flatnonzero(transitions == -1)
    lengths = ends - starts
    if lengths.size == 0 or int(lengths.sum()) >= n:
        return 0.0
    rng = np.random.default_rng(seed)
    best = 0.0
    placed_ok = 0
    for _ in range(resamples):
        placed = np.zeros(n, dtype=bool)
        valid = True
        for length in lengths:
            span = int(length)
            if span >= n:
                valid = False
                break
            start = int(rng.integers(0, n - span + 1))
            if placed[start : start + span].any():
                valid = False
                break
            placed[start : start + span] = True
        if not valid:
            continue
        placed_ok += 1
        best = max(best, balanced_accuracy(circuit_on, placed))
    return best if placed_ok else 0.0


def _on_episode_lengths(state_on: np.ndarray) -> np.ndarray:
    padded = np.concatenate([[False], state_on.astype(bool), [False]])
    transitions = np.diff(padded.astype(np.int8))
    starts = np.flatnonzero(transitions == 1)
    ends = np.flatnonzero(transitions == -1)
    return ends - starts


def null_is_testable(state_on: np.ndarray) -> bool:
    """Is there enough ON/OFF structure for the relocation null to mean anything?

    With one long episode (a single 4-hour EV charging session) the "relocated"
    surrogates are circular shifts, and the best of 200 of them lands within a
    few samples of the truth: the null equals the observation and would reject a
    real, physically obvious link.
    """
    return int(_on_episode_lengths(state_on).size) >= MIN_NULL_EPISODES


# --------------------------------------------------------------------------- #
# Per-candidate analysis
# --------------------------------------------------------------------------- #
def analyse_candidate(
    times: np.ndarray,
    values: np.ndarray,
    episodes: Sequence[tuple[float, str]],
    entity_id: str,
    *,
    grid_s: float = GRID_S,
    max_lag_s: float = MAX_LAG_S,
    min_transitions: int = MIN_TRANSITIONS,
) -> dict[str, Any] | None:
    """Full evidence for one (circuit, entity) pair, or ``None`` if unusable."""
    if values.size < 50 or len(episodes) < min_transitions:
        return None
    table = label_table(times, values, episodes)
    split = split_labels(table)
    if split is None:
        return None
    quiet, active, low, high = split

    sample_labels = _sample_labels(times, episodes)
    quiet_mask = np.isin(sample_labels, quiet)
    if int(quiet_mask.sum()) < 5 or int((~quiet_mask).sum()) < 5:
        return None
    sigma = _robust_sigma(values[quiet_mask])
    separation = high - low
    required = max(MIN_SEPARATION_W, SEPARATION_SIGMAS * sigma)
    if separation < required:
        return {
            "entity_id": entity_id,
            "rejected": "insufficient_separation",
            "separation_w": round(separation, 1),
            "required_w": round(required, 1),
            "sigma_w": round(sigma, 3),
            "label_medians_w": {label: round(value[0], 1) for label, value in table.items()},
        }

    if inverts_on_off(active, quiet):
        return {
            "entity_id": entity_id,
            "rejected": "inverted_on_off_labels",
            "label_medians_w": {label: round(value[0], 1) for label, value in table.items()},
            "note": (
                "the circuit is high while the entity reports off, so the entity "
                "cannot be this circuit's load"
            ),
        }

    # Coverage is measured, not enforced here: failing it is often the *first
    # sign of a second load on the same CT* (the HRV socket pair splits one
    # circuit 7 W / 147 W / 147 W / 290 W, so each entity covers only half the
    # level).  Rejecting early would hide that and report "no entity separates
    # this circuit" instead of the true, useful answer: confounded.
    coverage = separation / max(abs(high), MIN_SEPARATION_W)

    start, end = _episodes_span(times, episodes)
    if end - start < 1800:
        return None
    grid = np.arange(start, end, grid_s)
    if grid.size < 60:
        return None

    threshold = (low + high) / 2.0
    circuit_on = np.interp(grid, times, values) > threshold
    state_on = np.isin(_sample_labels(grid, episodes), list(active))

    best = score_at_lag(circuit_on, state_on, grid_s=grid_s, max_lag_s=max_lag_s)
    testable = null_is_testable(state_on)
    null_best = relocation_null(circuit_on, state_on) if testable else 0.0

    # Recall/coverage must be measured at the *winning* lag, not at lag 0.  A
    # template or cloud entity reports its state minutes after the load switched,
    # so at lag 0 the entity looks absent during part of every circuit-on period
    # and its circuit recall is understated (measured: the humidifier's lag is
    # -240 s and confident recall jumps from 0.87 at lag 0 to 0.96 at its lag).
    if best["lag_s"]:
        shift = int(round(best["lag_s"] / grid_s))
        if shift > 0:
            observed, predicted = circuit_on[:-shift], state_on[shift:]
        else:
            observed, predicted = circuit_on[-shift:], state_on[:shift]
    else:
        observed, predicted = circuit_on, state_on
    aligned = _components(observed, predicted)
    high_count = int(observed.sum())
    circuit_recall = (
        float(np.sum(observed & predicted)) / high_count if high_count else 0.0
    )
    circuit_precision = (
        float(np.sum(observed & predicted)) / int(predicted.sum())
        if int(predicted.sum())
        else 0.0
    )

    lagged_labels = _sample_labels(times - best["lag_s"], episodes)
    mismatch, derivable_threshold = derivable_mismatch(
        values, np.isin(lagged_labels, list(active))
    )

    changes = sum(
        1
        for index, (_, label) in enumerate(episodes)
        if index and label.lower() not in UNKNOWN_STATES
    )

    return {
        "entity_id": entity_id,
        "balanced_accuracy": round(best["balanced_accuracy"], 4),
        "sensitivity": round(best["sensitivity"], 4),
        "specificity": round(best["specificity"], 4),
        "lag_s": int(best["lag_s"]),
        "null_balanced_accuracy": round(null_best, 4) if testable else None,
        "null_testable": testable,
        "margin_over_null": round(best["balanced_accuracy"] - null_best, 4)
        if testable
        else None,
        "coverage": round(float(coverage), 4),
        "threshold_w": round(threshold, 1),
        "separation_w": round(separation, 1),
        "required_separation_w": round(required, 1),
        "sigma_w": round(sigma, 3),
        "quiet_labels": quiet,
        "active_labels": active,
        "quiet_median_w": round(low, 1),
        "active_median_w": round(high, 1),
        "transitions": changes,
        "label_medians_w": {label: round(value[0], 1) for label, value in table.items()},
        "derivable_mismatch_samples": int(mismatch),
        "derivable_threshold_w": None
        if math.isnan(derivable_threshold)
        else round(derivable_threshold, 2),
        "circular": bool(mismatch == 0),
        "circuit_high_fraction": round(float(np.mean(circuit_on)), 4),
        "active_fraction": round(float(np.mean(state_on)), 4),
        # Circuit recall: P(entity active | circuit high).  Balanced accuracy is
        # a per-entity score and is happy with a short-duty flag that is very
        # specific; this says whether the entity actually covers the circuit.
        "circuit_recall": round(circuit_recall, 4),
        "circuit_precision": round(circuit_precision, 4),
        "aligned_sensitivity": round(aligned["sensitivity"], 4),
        "aligned_specificity": round(aligned["specificity"], 4),
    }


def screen_candidates(
    times: np.ndarray,
    values: np.ndarray,
    state_series: Mapping[str, list[tuple[float, str]]],
    *,
    min_transitions: int = MIN_TRANSITIONS,
    top_k: int = 12,
) -> list[dict[str, Any]]:
    """Cheap first pass at lag 0, ranking entities by balanced accuracy.

    Scoring at lag 0 on the raw samples avoids the interpolation grid, which
    makes this ~1 s for ~1 900 entities per device.  The surviving shortlist is
    re-analysed with the full lag sweep, coverage rule and relocation null, so a
    wrong lag-0 alignment can only cost a candidate its place in the shortlist —
    it cannot produce a mapping on its own.
    """
    out: list[dict[str, Any]] = []
    for entity_id, episodes in state_series.items():
        changes = sum(
            1
            for index, (_, label) in enumerate(episodes)
            if index and label.lower() not in UNKNOWN_STATES
        )
        if changes < min_transitions:
            continue
        table = label_table(times, values, episodes)
        split = split_labels(table)
        if split is None:
            continue
        quiet, active, low, high = split
        if high - low < MIN_SEPARATION_W:
            continue
        labels = _sample_labels(times, episodes)
        state_on = np.isin(labels, list(active))
        circuit_on = values > (low + high) / 2.0
        score = balanced_accuracy(circuit_on, state_on)
        if score < SCREEN_BALANCED_ACCURACY:
            continue
        out.append(
            {
                "entity_id": entity_id,
                "screen_balanced_accuracy": round(score, 4),
                "separation_w": round(high - low, 1),
                "active_labels": active,
                "quiet_labels": quiet,
            }
        )
    out.sort(key=lambda row: (-row["screen_balanced_accuracy"], row["entity_id"]))
    return out[: max(1, top_k)]


# --------------------------------------------------------------------------- #
# Same-appliance grouping and confound detection
# --------------------------------------------------------------------------- #
def group_by_appliance(
    survivors: Sequence[Mapping[str, Any]],
    registry_devices: Mapping[str, str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Collapse facets of one HA device, then pick one representative per group.

    ``registry_devices`` maps ``entity_id -> HA device_id``.  That identifier is
    a *declarative* fact written by the integration: it says these entities
    belong to one physical appliance.  It is not name similarity and it does not
    guess what the appliance is.  Grouping on it is what stops a complex
    appliance's several facets (heater, heating flag, filter status, pump,
    in-use flag) from being mistaken for five loads on one circuit.

    Entities with no registry device (template helpers) are grouped alone, so
    they still compete normally.
    """
    usable = [dict(row) for row in survivors if row.get("active_labels")]
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in usable:
        entity_id = str(row["entity_id"])
        key = registry_devices.get(entity_id) or f"entity:{entity_id}"
        groups.setdefault(key, []).append(row)

    representatives: list[dict[str, Any]] = []
    facets: list[dict[str, Any]] = []
    for key, members in groups.items():
        members.sort(
            key=lambda row: (
                -float(row.get("balanced_accuracy") or 0.0),
                len(row.get("state_overrides") or {}),
                str(row["entity_id"]),
            )
        )
        representative = members[0]
        representative["appliance_key"] = key
        if len(members) > 1:
            representative["same_appliance_entities"] = [
                str(item["entity_id"]) for item in members[1:]
            ]
            for item in members[1:]:
                facets.append(
                    {
                        "entity_id": item["entity_id"],
                        "same_appliance_as": representative["entity_id"],
                        "balanced_accuracy": item.get("balanced_accuracy"),
                        "separation_w": item.get("separation_w"),
                    }
                )
        representatives.append(representative)
    representatives.sort(
        key=lambda row: -float(row.get("balanced_accuracy") or 0.0)
    )
    return representatives, facets


def _cross_tab(
    times: np.ndarray,
    values: np.ndarray,
    episodes_a: Sequence[tuple[float, str]],
    active_a: Sequence[str],
    episodes_b: Sequence[tuple[float, str]],
    active_b: Sequence[str],
) -> dict[tuple[bool, bool], tuple[int, float | None]]:
    """``(a_on, b_on) -> (sample count, median power)`` at each CT reading."""
    labels_a = np.isin(_sample_labels(times, episodes_a), list(active_a))
    labels_b = np.isin(_sample_labels(times, episodes_b), list(active_b))
    cells: dict[tuple[bool, bool], tuple[int, float | None]] = {}
    for a_on in (False, True):
        for b_on in (False, True):
            mask = (labels_a == a_on) & (labels_b == b_on)
            count = int(mask.sum())
            cells[(a_on, b_on)] = (
                count,
                float(np.median(values[mask])) if count else None,
            )
    return cells


def marginal_effects(
    cells: Mapping[tuple[bool, bool], tuple[int, float | None]],
    *,
    min_samples: int = MIN_CROSSTAB_SAMPLES,
) -> dict[str, float | None]:
    """How much power each entity adds while the other is already ON.

    Independence is established by *marginal* effect, not correlation: two loads
    are genuinely separate if switching B on while A is already on raises the
    circuit **and** switching A on while B is already on raises it too.  A
    control signal for the same load (a flame flag, a thermostat call) has no
    marginal effect at all, which is exactly why correlation cannot tell a
    control signal from a second load.
    """
    def med(a_on: bool, b_on: bool) -> float | None:
        count, value = cells[(a_on, b_on)]
        return value if count >= min_samples else None

    quiet = med(False, False)
    a_only = med(True, False)
    b_only = med(False, True)
    both = med(True, True)
    support = {
        "quiet": int(cells[(False, False)][0]),
        "a_only": int(cells[(True, False)][0]),
        "b_only": int(cells[(False, True)][0]),
        "both": int(cells[(True, True)][0]),
    }
    return {
        "support": support,
        "quiet_median_w": quiet,
        "a_only_median_w": a_only,
        "b_only_median_w": b_only,
        "both_median_w": both,
        "b_marginal_w": None if a_only is None or both is None else both - a_only,
        "a_marginal_w": None if b_only is None or both is None else both - b_only,
        "a_alone_w": None if quiet is None or a_only is None else a_only - quiet,
        "b_alone_w": None if quiet is None or b_only is None else b_only - quiet,
    }


def _is_independent_load(effects: Mapping[str, Any]) -> bool:
    """Both marginals and both solo effects must be real power with real support."""
    support = effects.get("support") or {}
    if (
        int(support.get("a_only") or 0) < MIN_SOLO_SAMPLES
        or int(support.get("b_only") or 0) < MIN_SOLO_SAMPLES
        or int(support.get("both") or 0) < MIN_CROSSTAB_SAMPLES
    ):
        # Without observations of each load on its own, "additive" cannot be
        # distinguished from two nested signals; that case is ambiguity.
        return False
    values = (
        effects["a_marginal_w"],
        effects["b_marginal_w"],
        effects["a_alone_w"],
        effects["b_alone_w"],
    )
    if any(value is None for value in values):
        return False
    return all(float(value) >= MIN_MARGINAL_EFFECT_W for value in values)  # type: ignore[arg-type]


def detect_confound(
    representatives: Sequence[Mapping[str, Any]],
    state_series: Mapping[str, list[tuple[float, str]]],
    times: np.ndarray,
    values: np.ndarray,
) -> dict[str, Any] | None:
    """Report a two-load circuit, or ``None`` when one entity explains it."""
    usable = [
        row
        for row in representatives
        if row.get("active_labels")
        # A confound member must itself explain a real share of the circuit's high
        # periods.  Without this, two unrelated loads that both precede the same
        # evening window look "additive": measured, one lighting switch with
        # circuit recall 0.23 paired with an unrelated switch and produced a
        # fake two-load structure that hid the real answer.
        and float(row.get("circuit_recall") or 0.0) >= MIN_CONFOUND_CIRCUIT_RECALL
        and float(row.get("separation_w") or 0.0) >= MIN_CONFOUND_SEPARATION_W
    ]
    if len(usable) < 2:
        return None
    for i in range(len(usable)):
        for j in range(i + 1, len(usable)):
            first, second = usable[i], usable[j]
            cells = _cross_tab(
                times,
                values,
                state_series.get(str(first["entity_id"])) or [],
                first["active_labels"],
                state_series.get(str(second["entity_id"])) or [],
                second["active_labels"],
            )
            effects = marginal_effects(cells)
            if not _is_independent_load(effects):
                continue
            return {
                "kind": "additive_multi_device",
                "members": [first["entity_id"], second["entity_id"]],
                "levels_w": {
                    "00": _round_or_none(effects["quiet_median_w"]),
                    "10": _round_or_none(effects["a_only_median_w"]),
                    "01": _round_or_none(effects["b_only_median_w"]),
                    "11": _round_or_none(effects["both_median_w"]),
                },
                "step_w": {
                    str(first["entity_id"]): _round_or_none(effects["a_marginal_w"]),
                    str(second["entity_id"]): _round_or_none(effects["b_marginal_w"]),
                },
                "note": (
                    "switching either entity on adds power while the other is already on, "
                    "so this circuit carries two independent loads and no single entity "
                    "describes it"
                ),
            }
    return None


def _round_or_none(value: float | None) -> float | None:
    return None if value is None else round(float(value), 1)


def _is_plausible_load(row: Mapping[str, Any]) -> bool:
    """Would this entity, on its own, justify a ``supported`` mapping?"""
    return (
        float(row.get("balanced_accuracy") or 0.0) >= SUPPORTED_BALANCED_ACCURACY
        and float(row.get("circuit_recall") or 0.0) >= MIN_CIRCUIT_RECALL
        and float(row.get("coverage") or 0.0) >= MIN_COVERAGE
        and not row.get("circular")
        and (
            not row.get("null_testable")
            or float(row.get("margin_over_null") or 0.0) >= MIN_NULL_MARGIN
        )
    )


def _carries_power(alone_w: float | None, marginal_w: float | None) -> bool:
    """Does this entity demonstrably add power of its own?

    Either measurement suffices: its level while alone versus quiet, or the step
    it adds while the rival is already on.
    """
    return any(
        value is not None and float(value) >= MIN_MARGINAL_EFFECT_W
        for value in (alone_w, marginal_w)
    )


def _carries_no_power(alone_w: float | None, marginal_w: float | None) -> bool:
    """Is it *measured* that this entity adds no power of its own?

    Requires at least one measurement below the threshold and none above it; an
    entirely unmeasured entity cannot be declared powerless.
    """
    measured = False
    for value in (alone_w, marginal_w):
        if value is None:
            continue
        measured = True
        if float(value) >= MIN_MARGINAL_EFFECT_W:
            return False
    return measured


def detect_sole_carrier(
    representatives: Sequence[Mapping[str, Any]],
    state_series: Mapping[str, list[tuple[float, str]]],
    times: np.ndarray,
    values: np.ndarray,
) -> dict[str, Any] | None:
    """Pick the one entity that actually *is* the load, when a rival is a flag.

    A high balanced accuracy only says an entity's state tracks the circuit; it
    does not say the entity carries power.  Measured on the outdoor-plug channel:
    ``switch.landscape_socket_1`` switches 169 W on, while
    ``light.exterior_exterior_front_fixture`` flips at the same moments and adds
    nothing of its own (marginal -1 W while the socket is already on).  The
    fixture is a control/companion signal, not the load, so the socket is the
    mapping and the pair is *not* ambiguous.

    The test is the 2x2 cross-tab marginal: exactly one member must add real
    power while the other is already on, and that same member must carry real
    power alone.
    """
    usable = [
        row
        for row in representatives
        if row.get("active_labels")
        and float(row.get("circuit_recall") or 0.0) >= MIN_AMBIGUITY_CIRCUIT_RECALL
        # At least one member must independently look like a full load.  Two
        # weak candidates must not "settle" each other: two sub-threshold
        # candidates would otherwise hand the mapping to whichever happened to
        # add more watts.
        and _is_plausible_load(row)
    ]
    if not any(_is_plausible_load(row) for row in usable):
        return None
    if len(usable) < 2:
        return None
    best: dict[str, Any] | None = None
    for i in range(len(usable)):
        for j in range(i + 1, len(usable)):
            first, second = usable[i], usable[j]
            cells = _cross_tab(
                times,
                values,
                state_series.get(str(first["entity_id"])) or [],
                first["active_labels"],
                state_series.get(str(second["entity_id"])) or [],
                second["active_labels"],
            )
            effects = marginal_effects(cells)
            a_marg = effects["a_marginal_w"]
            b_marg = effects["b_marginal_w"]
            a_alone = effects["a_alone_w"]
            b_alone = effects["b_alone_w"]
            # "Carries power" and "carries none" must each be *demonstrated*.
            # A member that is never seen alone has an unmeasurable marginal, so
            # the evidence has to come from the level it is associated with
            # instead: one member is never seen without the other, yet the
            # companion adds only 2.6 W over the quiet level while the load adds
            # 260 W, so the load is unambiguously identified.
            a_load = _carries_power(a_alone, a_marg)
            b_load = _carries_power(b_alone, b_marg)
            if a_load == b_load:
                # Both carry power (a real confound, reported elsewhere) or
                # neither does — not resolvable, so leave it ambiguous.
                continue
            loser_alone, loser_marg = (b_alone, b_marg) if a_load else (a_alone, a_marg)
            if not _carries_no_power(loser_alone, loser_marg):
                # The "loser" might carry power that was simply not observed.
                continue
            winner = first if a_load else second
            loser = second if a_load else first
            best = {
                "winner": str(winner["entity_id"]),
                "rival": str(loser["entity_id"]),
                "winner_marginal_w": _round_or_none(a_marg if a_load else b_marg),
                "rival_marginal_w": _round_or_none(b_marg if a_load else a_marg),
                "levels_w": {
                    "00": _round_or_none(effects["quiet_median_w"]),
                    "10": _round_or_none(effects["a_only_median_w"]),
                    "01": _round_or_none(effects["b_only_median_w"]),
                    "11": _round_or_none(effects["both_median_w"]),
                },
                "note": (
                    "the rival switches with this circuit but adds no power of its own "
                    "while the winner is already on, so it is a companion/control "
                    "signal rather than a second load"
                ),
            }
    return best


def _ratio_stability(
    a: np.ndarray, b: np.ndarray
) -> tuple[float | None, float | None, float | None]:
    """``(ratio, relative_iqr, median_abs_residual_w)`` for ``b = ratio * a``.

    Only readings where at least one channel carries a real load are used: near
    zero both signals are noise, and a ratio of noise is meaningless.
    """
    carrying = (np.abs(a) >= MIN_DUPLICATE_RATIO_W) | (np.abs(b) >= MIN_DUPLICATE_RATIO_W)
    if int(carrying.sum()) < 500:
        return None, None, None
    x = a[carrying]
    y = b[carrying]
    if float(np.median(np.abs(x))) < MIN_DUPLICATE_RATIO_W and np.median(np.abs(y)) < MIN_DUPLICATE_RATIO_W:
        return None, None, None
    # Robust slope: the median of the pointwise ratio, not a least-squares fit,
    # so a handful of unrelated switching events cannot drag the estimate.
    with np.errstate(divide="ignore", invalid="ignore"):
        pointwise = np.where(x != 0, y / x, np.nan)
    pointwise = pointwise[np.isfinite(pointwise)]
    if pointwise.size < 500:
        return None, None, None
    ratio = float(np.median(pointwise))
    if not ratio or not np.isfinite(ratio):
        return None, None, None
    low, high = np.percentile(pointwise, [25, 75])
    relative_iqr = float((high - low) / abs(ratio))
    residual = float(np.median(np.abs(y - ratio * x)))
    return ratio, relative_iqr, residual


def detect_duplicate_channels(
    series_by_channel: Mapping[str, tuple[np.ndarray, np.ndarray]],
    *,
    paired: Mapping[str, str] | None = None,
    min_correlation: float = MIN_DUPLICATE_CORRELATION,
) -> list[dict[str, Any]]:
    """Report pairs of CTs that may be measuring the same physical current.

    Two channels carrying the same watts are not two loads; summing or solving
    them double-counts, and mapping a state entity to both would attribute one
    appliance twice.  Correlation alone is not enough to claim that: two
    genuinely different loads on the same schedule correlate strongly too.
    The discriminator is **ratio stability**.  If ``B = k * A`` for a fixed
    ``k`` the two clamps are seeing the same current (possibly through a
    different CT ratio); if the ratio wanders, they are different currents.

    Measured in one installation::

        pair A (duplicate)   r 0.845  ratio 0.539  rel IQR 0.005  0.15 W residual
        pair B (distinct)    r 0.838  ratio 0.231  rel IQR 0.722  1.35 W residual

    The first is one conductor measured twice; the second is two separate loads
    that happen to be busy at the same times.

    ``paired`` names channels that are already known to be the two legs of one
    double-pole circuit.  Those are reported as ``double_pole_legs`` rather than
    as a defect: correlation there is expected and correct.

    The finding is reported as evidence; this function never merges, relabels or
    disables a channel, because only the installer knows what is clamped where.
    """
    paired = paired or {}
    keys = sorted(series_by_channel)
    out: list[dict[str, Any]] = []
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            first, second = keys[i], keys[j]
            kind = "double_pole_legs" if paired.get(first) == second else None
            a_t, a_v = series_by_channel[first]
            b_t, b_v = series_by_channel[second]
            if a_v.size < 50 or b_v.size < 50:
                continue
            start = max(float(a_t[0]), float(b_t[0]))
            end = min(float(a_t[-1]), float(b_t[-1]))
            if end - start < 3600:
                continue
            grid = np.arange(start, end, GRID_S)
            a = np.interp(grid, a_t, a_v)
            b = np.interp(grid, b_t, b_v)
            if a.std() == 0 or b.std() == 0:
                continue
            correlation = float(np.corrcoef(a, b)[0, 1])
            if correlation < min_correlation and kind is None:
                continue
            ratio, relative_iqr, residual = _ratio_stability(a, b)
            same_current = (
                ratio is not None
                and relative_iqr is not None
                and residual is not None
                and relative_iqr < MIN_DUPLICATE_RELATIVE_IQR
                and residual < MIN_DUPLICATE_RESIDUAL_W
            )
            if kind is not None:
                # A declared double-pole pair should track closely: the two legs
                # of one 240 V circuit carry the same current.  If it does not,
                # say so rather than presenting the declaration as corroborated
                # (measured: two declared pairs correlated at 0.28 and -0.00,
                # against 1.00 for a genuine one).
                if correlation < min_correlation:
                    kind = "double_pole_legs_uncorrelated"
            elif same_current:
                kind = "same_current"
            elif correlation >= min_correlation:
                kind = "correlated_activity"
            else:
                continue
            out.append(
                {
                    "kind": kind,
                    "first": first,
                    "second": second,
                    "correlation": round(correlation, 4),
                    "median_first_w": round(float(np.median(a)), 1),
                    "median_second_w": round(float(np.median(b)), 1),
                    "ratio_first_to_second": None
                    if not ratio
                    else round(1.0 / ratio, 4),
                    "ratio_relative_iqr": None
                    if relative_iqr is None
                    else round(relative_iqr, 4),
                    "median_abs_residual_w": None
                    if residual is None
                    else round(residual, 3),
                    "note": _duplicate_note(kind),
                }
            )
    order = {
        "same_current": 0,
        "double_pole_legs": 1,
        "double_pole_legs_uncorrelated": 2,
        "correlated_activity": 3,
    }
    out.sort(key=lambda item: (order[item["kind"]], -item["correlation"]))
    return out


def _duplicate_note(kind: str) -> str:
    if kind == "same_current":
        return (
            "the two readings hold a fixed ratio to within a fraction of a watt, so these "
            "CTs are almost certainly clamped on the same conductor: do not sum them and do "
            "not attribute the same appliance to both"
        )
    if kind == "double_pole_legs":
        return (
            "the two legs of one double-pole circuit; correlation is expected, and the pair "
            "describes a single device rather than two"
        )
    if kind == "double_pole_legs_uncorrelated":
        return (
            "the topology declares these two legs as one double-pole circuit, but they do not "
            "track each other, so the declaration is unconfirmed: one leg may be unclamped, "
            "reversed, or wired to a different circuit"
        )
    return (
        "the two channels are active at the same times but their ratio wanders, so they are "
        "different currents that happen to share a schedule; not evidence of duplication"
    )


def detect_ambiguity(
    representatives: Sequence[Mapping[str, Any]],
    state_series: Mapping[str, list[tuple[float, str]]],
    times: np.ndarray,
    values: np.ndarray,
) -> dict[str, Any] | None:
    """Several independent entities each fully explain the circuit.

    Happens when loads are *nested*: a nested dimmer and its parent always come
    on together, so "nested on, parent off" is never observed and the marginal
    test cannot separate them.  Two entities both covering the circuit is real
    ambiguity, and the spec forbids resolving it by guessing.
    """
    full = [
        row
        for row in representatives
        if float(row.get("balanced_accuracy") or 0.0) >= SUPPORTED_BALANCED_ACCURACY
        and float(row.get("coverage") or 0.0) >= MIN_COVERAGE
        # A competitor only makes the answer ambiguous if it, too, explains the
        # circuit.  Balanced accuracy alone is a per-entity score and can be
        # high for a short-duty flag that is merely very specific: one lighting
        # switch reached 0.98 while being on during only 23 % of its circuit's
        # high periods (measured).  Those are not rival explanations, so they
        # must not block the mapping.
        and float(row.get("circuit_recall") or 0.0) >= MIN_AMBIGUITY_CIRCUIT_RECALL
        and not row.get("circular")
        and (
            not row.get("null_testable")
            or float(row.get("margin_over_null") or 0.0) >= MIN_NULL_MARGIN
        )
    ]
    full.sort(key=lambda row: -float(row.get("circuit_recall") or 0.0))
    if len(full) < 2:
        return None
    # A clear winner is not ambiguity.  Two nested dimmers observed together
    # cover the circuit equally; a genuine load and a coincidental flag are far
    # apart.
    lead = float(full[0].get("circuit_recall") or 0.0) - float(
        full[1].get("circuit_recall") or 0.0
    )
    if lead > MIN_RECALL_LEAD:
        return None
    # Even an equally-covering pair is settled if the cross-tab shows exactly one
    # of them carrying the power; that is a companion signal, not a rival load.
    sole = detect_sole_carrier(representatives, state_series, times, values)
    if sole is not None:
        return None
    return {
        "kind": "ambiguous_multiple_entities",
        "members": [str(row["entity_id"]) for row in full],
        "scores": {
            str(row["entity_id"]): row.get("balanced_accuracy") for row in full
        },
        "note": (
            "several independent entities each explain this circuit and cannot be "
            "observed separately; the mapping must not be guessed"
        ),
    }


def select_best(
    representatives: Sequence[Mapping[str, Any]],
    sole_carrier: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """The single entity to map, or ``None``.

    Ranking is by *how much of the circuit the entity accounts for*
    (:func:`circuit_recall`), then by balanced accuracy.  Balanced accuracy
    alone is a per-entity score and can rank a companion flag above the real
    load; recall cannot, because a flag is only on during a fraction of the
    circuit's high periods.

    When the cross-tab shows exactly one member carrying the power, that member
    wins outright even if its recall is not the highest — measured on the
    outdoor-plug channel, where ``switch.landscape_socket_1`` switches the 169 W
    load while ``light.exterior_exterior_front_fixture`` flips at the same
    instants and adds no power of its own.
    """
    if not representatives:
        return None
    if sole_carrier is not None:
        winner = sole_carrier.get("winner")
        for row in representatives:
            if str(row.get("entity_id")) == winner and _is_plausible_load(row):
                return row
    scored = [row for row in representatives if "balanced_accuracy" in row]
    if not scored:
        return None
    return max(
        scored,
        key=lambda row: (
            float(row.get("circuit_recall") or 0.0),
            row["balanced_accuracy"],
            -abs(row["lag_s"]),
        ),
    )


def classify(
    representatives: Sequence[Mapping[str, Any]],
    confound: Mapping[str, Any] | None,
    ambiguity: Mapping[str, Any] | None,
    *,
    channel_finding: str | None = None,
    sole_carrier: Mapping[str, Any] | None = None,
) -> tuple[str, str]:
    """``(status, reason)`` from the surviving candidates and the structural tests."""
    if channel_finding == "polarity_inverted_candidate":
        return "unsupported", "channel_polarity_suspect"
    if channel_finding == "flat_no_events":
        return "unsupported", "channel_has_no_events"
    if confound is not None:
        return "confounded", str(confound.get("kind") or "confounded")
    if ambiguity is not None:
        return "ambiguous", str(ambiguity.get("kind") or "ambiguous")
    scored = [row for row in representatives if "balanced_accuracy" in row]
    if not scored:
        return "unsupported", "no_entity_separates_this_circuit"
    best = select_best(scored, sole_carrier)
    if best is None:
        return "unsupported", "no_entity_separates_this_circuit"
    strong = best["separation_w"] >= STRONG_MAGNITUDE_W
    minimum = MIN_TRANSITIONS_STRONG if strong else MIN_TRANSITIONS
    if best["transitions"] < minimum:
        return "unsupported", f"too_few_transitions:{best['transitions']}"
    if best["circular"]:
        return "unsupported", "circular_evidence_only"
    score = best["balanced_accuracy"]
    if score < WEAK_BALANCED_ACCURACY:
        return "unsupported", f"balanced_accuracy_below_weak:{score:.3f}"
    # The relocation null only applies where it is statistically testable.  Too
    # few ON episodes means the surrogates are shifts of the same event, which
    # would reject a real link (the EV charger has exactly one charging session).
    if best.get("null_testable"):
        margin = best.get("margin_over_null") or 0.0
        if margin < MIN_NULL_MARGIN:
            return (
                "unsupported",
                f"not_above_null:score={score:.3f}"
                f" null={best.get('null_balanced_accuracy', 0.0):.3f}",
            )
    coverage = float(best.get("coverage") or 0.0)
    if coverage < MIN_COVERAGE:
        # Tracks the circuit but cannot be the load: the entity adds only part of
        # the level.  Usually means a second, unmeasured load shares the circuit.
        return "unsupported", f"insufficient_coverage:{coverage:.2f}"
    circuit_recall = float(best.get("circuit_recall") or 0.0)
    if circuit_recall < MIN_CIRCUIT_RECALL:
        # The entity is specific to the circuit but is active during only part of
        # the circuit's high periods, so something else also drives it.  Measured
        # on one lighting channel: the real dimmer covered 0.999 of the high
        # periods, while two competing switches covered 0.23 and 0.34 — every one
        # of them scored above 0.92 balanced accuracy.
        return "unsupported", f"insufficient_circuit_recall:{circuit_recall:.2f}"
    if circuit_recall >= SUPPORTED_CIRCUIT_RECALL:
        support_level = "full"
    elif circuit_recall >= MIN_AMBIGUITY_CIRCUIT_RECALL:
        support_level = "partial"
    else:
        support_level = "weak"
    if (
        support_level == "full"
        and score >= SUPPORTED_BALANCED_ACCURACY
        and best["sensitivity"] >= SUPPORTED_SENSITIVITY
        and best["specificity"] >= SUPPORTED_SPECIFICITY
    ):
        return "supported", "state_gated_level_evidence"
    if (
        support_level in {"full", "partial"}
        and score >= WEAK_BALANCED_ACCURACY
        and best["sensitivity"] >= 0.75
        and best["specificity"] >= 0.75
    ):
        return "weak", "partial_evidence"
    return (
        "unsupported",
        f"sensitivity_specificity_below_weak:"
        f"{best['sensitivity']:.2f}/{best['specificity']:.2f}",
    )


#: Labels that unambiguously mean "this appliance is running" / "not running".
CANONICAL_ON = frozenset({"on", "running", "heating", "active", "open", "playing"})
CANONICAL_OFF = frozenset({"off", "idle", "standby", "closed", "paused", "stopped"})


def inverts_on_off(active: Sequence[str], quiet: Sequence[str]) -> bool:
    """The entity would have to be read inside-out to explain this circuit.

    A remote-control flag can report ``on`` while its circuit is *low* and
    ``off`` while it is high, purely because the flag is set when the appliance
    is idle.  Installing that mapping would teach the engine that "off" means
    176 W — worse than no mapping at all.

    A genuine per-device vocabulary difference is different: a complex controller
    reports ``cooling`` while drawing only 22 W, and ``cooling`` is not a
    canonical OFF label, so this test does not fire for it.
    """
    active_lower = {label.strip().lower() for label in active}
    quiet_lower = {label.strip().lower() for label in quiet}
    inverted = bool(active_lower & CANONICAL_OFF) and bool(quiet_lower & CANONICAL_ON)
    return inverted


def state_overrides(active: Sequence[str], quiet: Sequence[str]) -> dict[str, str]:
    """Labels whose engine meaning must be corrected for this device.

    The engine's global ``ON_STATES`` contains ``cooling``, but a complex
    controller may report ``cooling`` while drawing 22 W and ``heating`` while
    drawing 4.5 kW.  Mapping that entity without correcting ``cooling`` would
    make the engine believe the heater is on at 22 W and let the solver attribute
    the rest of the branch to it.  Overrides are declarative and per-device, so
    no global constant changes meaning for other devices.
    """
    from wattmosaic_engine.disaggregation.engine import ON_STATES

    out: dict[str, str] = {}
    for label, evidence_on in [(item, True) for item in active] + [
        (item, False) for item in quiet
    ]:
        key = label.strip().lower()
        if not key:
            continue
        if evidence_on and key not in ON_STATES:
            out[label] = "on"
        elif not evidence_on and key in ON_STATES:
            out[label] = "off"
    return out


# --------------------------------------------------------------------------- #
# Channel findings
# --------------------------------------------------------------------------- #
def channel_finding(times: np.ndarray, values: np.ndarray) -> dict[str, Any]:
    """Polarity / dead-channel evidence for one CT."""
    if values.size == 0:
        return {"finding": "no_data", "samples": 0}
    negative = float(np.mean(values <= POLARITY_NEGATIVE_W))
    positive = float(np.mean(values >= POLARITY_POSITIVE_W))
    span = float(values.max() - values.min())
    finding = "normal"
    if negative >= POLARITY_NEGATIVE_FRACTION and positive <= POLARITY_POSITIVE_FRACTION:
        finding = "polarity_inverted_candidate"
    elif span < DEAD_CHANNEL_SPAN_W:
        finding = "flat_no_events"
    return {
        "finding": finding,
        "samples": int(values.size),
        "median_w": round(float(np.median(values)), 2),
        "min_w": round(float(values.min()), 2),
        "max_w": round(float(values.max()), 2),
        "span_w": round(span, 2),
        "negative_fraction": round(negative, 4),
        "positive_fraction": round(positive, 4),
        "median_interval_s": round(
            float(np.median(np.diff(times))) if times.size > 1 else 0.0, 2
        ),
    }


def polarity_evidence(report: Mapping[str, Any]) -> dict[str, Any] | None:
    """Justify a ``polarity=inverted`` candidate, or ``None``.

    A load CT on a house with no generation cannot read negative: there is no
    PV/battery entity in this installation at all.  A channel that is negative
    for the whole week is therefore a reversed clamp, not export.
    """
    if report.get("finding") != "polarity_inverted_candidate":
        return None
    return {
        "candidate_polarity": "inverted",
        "negative_fraction": report["negative_fraction"],
        "median_w": report["median_w"],
        "min_w": report["min_w"],
        "reason": (
            "load CT reads negative for the whole observation window; no generation "
            "entity exists in this installation, so the sign is a reversed clamp"
        ),
    }


# --------------------------------------------------------------------------- #
# Report assembly
# --------------------------------------------------------------------------- #
def paired_channels(topology: Mapping[str, Any] | None) -> dict[str, str]:
    """``ct_id -> partner ct_id`` for declared double-pole channel pairs."""
    out: dict[str, str] = {}
    for channel in (topology or {}).get("channels") or []:
        partner = channel.get("paired_ct_id")
        if partner:
            out[str(channel["ct_id"])] = str(partner)
    return out


def build_report(
    *,
    power: Mapping[str, Sequence[Mapping[str, Any]]],
    states: Mapping[str, Sequence[Mapping[str, Any]]],
    devices: Sequence[Mapping[str, Any]],
    platforms: Mapping[str, str] | None = None,
    registry_devices: Mapping[str, str] | None = None,
    source: Mapping[str, Any] | None = None,
    topology: Mapping[str, Any] | None = None,
    top_k: int = 12,
) -> dict[str, Any]:
    """Build the candidate report from already-fetched history.

    Pure with respect to the network: everything here can be reproduced from a
    saved payload, which is what the tests do.
    """
    platforms = platforms or {}
    registry_devices = registry_devices or {}
    channel_reports: dict[str, Any] = {}
    for entity_id, rows in power.items():
        times, values = numeric_series(rows)
        report = channel_finding(times, values)
        report["entity_id"] = entity_id
        evidence = polarity_evidence(report)
        if evidence:
            report["polarity_evidence"] = evidence
        channel_reports[entity_id] = report

    state_series: dict[str, list[tuple[float, str]]] = {}
    for entity_id, rows in states.items():
        episodes = state_episodes(rows)
        if len(episodes) >= 2:
            state_series[entity_id] = episodes

    device_reports: dict[str, Any] = {}
    counts = {
        "supported": 0,
        "weak": 0,
        "confounded": 0,
        "ambiguous": 0,
        "unsupported": 0,
    }
    polarity_devices: list[str] = []

    for device in devices:
        device_id = str(device["device_id"])
        ct_ids = [str(item) for item in device.get("ct_ids") or []]
        ct_entities = [str(item) for item in device.get("ct_entities") or []]
        series = [numeric_series(power.get(entity_id)) for entity_id in ct_entities]
        aggregate = aggregate_power(series)

        findings = [
            channel_reports.get(entity_id, {}).get("finding") for entity_id in ct_entities
        ]
        finding = None
        if any(item == "polarity_inverted_candidate" for item in findings):
            finding = "polarity_inverted_candidate"
            polarity_devices.append(device_id)
        elif findings and all(item == "flat_no_events" for item in findings if item):
            finding = "flat_no_events"

        entry: dict[str, Any] = {
            "device_id": device_id,
            "name": device.get("name"),
            "ct_ids": ct_ids,
            "ct_entities": ct_entities,
            "channel_finding": finding,
            "candidates": [],
            "facets": [],
            "state_entity_ids": [],
            "state_overrides": {},
            "confound": None,
            "ambiguity": None,
        }

        if aggregate is None:
            entry.update(status="unsupported", reason="no_channel_data")
            device_reports[device_id] = entry
            counts["unsupported"] += 1
            continue

        times, values = aggregate
        entry["channel_profile"] = channel_finding(times, values)

        if finding is not None:
            status, reason = classify([], None, None, channel_finding=finding)
            entry.update(status=status, reason=reason)
            device_reports[device_id] = entry
            counts[status] += 1
            continue

        screened = screen_candidates(times, values, state_series, top_k=top_k)
        entry["screened"] = [
            {
                "entity_id": row["entity_id"],
                "screen_balanced_accuracy": row["screen_balanced_accuracy"],
                "separation_w": row["separation_w"],
                "platform": platforms.get(row["entity_id"]),
            }
            for row in screened
        ]

        survivors: list[dict[str, Any]] = []
        for row in screened:
            analysis = analyse_candidate(
                times, values, state_series[row["entity_id"]], row["entity_id"]
            )
            if analysis is None:
                continue
            analysis["platform"] = platforms.get(row["entity_id"])
            analysis["state_overrides"] = state_overrides(
                analysis.get("active_labels") or [], analysis.get("quiet_labels") or []
            )
            survivors.append(analysis)
        entry["candidates"] = survivors

        representatives, facets = group_by_appliance(survivors, registry_devices)
        entry["facets"] = facets

        confound = detect_confound(representatives, state_series, times, values)
        ambiguity = detect_ambiguity(representatives, state_series, times, values)
        sole = detect_sole_carrier(representatives, state_series, times, values)
        entry["sole_carrier"] = sole
        entry["confound"] = confound
        entry["ambiguity"] = ambiguity
        status, reason = classify(
            representatives,
            confound,
            ambiguity,
            channel_finding=finding,
            sole_carrier=sole,
        )
        entry.update(status=status, reason=reason)
        counts[status] += 1

        if status in {"supported", "weak"}:
            best = select_best(representatives, sole)
            if best is None:
                entry.update(status="unsupported", reason="no_entity_separates_this_circuit")
                counts["weak" if status == "weak" else "supported"] -= 1
                counts["unsupported"] += 1
                device_reports[device_id] = entry
                continue
            entry["state_entity_ids"] = [best["entity_id"]]
            entry["state_overrides"] = best.get("state_overrides") or {}
            entry["evidence"] = {
                "method": "state_gated_level_test_with_lag_sweep",
                "balanced_accuracy": best["balanced_accuracy"],
                "sensitivity": best["sensitivity"],
                "specificity": best["specificity"],
                "lag_s": best["lag_s"],
                "null_balanced_accuracy": best["null_balanced_accuracy"],
                "margin_over_null": best["margin_over_null"],
                "coverage": best["coverage"],
                "separation_w": best["separation_w"],
                "required_separation_w": best["required_separation_w"],
                "sigma_w": best["sigma_w"],
                "transitions": best["transitions"],
                "quiet_labels": best["quiet_labels"],
                "active_labels": best["active_labels"],
                "quiet_median_w": best["quiet_median_w"],
                "active_median_w": best["active_median_w"],
                "derivable_mismatch_samples": best["derivable_mismatch_samples"],
                "circular": best["circular"],
                "circuit_high_fraction": best["circuit_high_fraction"],
                "active_fraction": best["active_fraction"],
                "circuit_recall": best["circuit_recall"],
                "circuit_precision": best["circuit_precision"],
                "same_appliance_entities": list(
                    best.get("same_appliance_entities") or []
                ),
            }
        elif confound is not None:
            entry["evidence"] = {
                "method": "confound_test",
                "confound": dict(confound),
                "members": list(confound.get("members") or []),
            }
        elif ambiguity is not None:
            entry["evidence"] = {
                "method": "ambiguity_test",
                "ambiguity": dict(ambiguity),
                "members": list(ambiguity.get("members") or []),
            }

        device_reports[device_id] = entry

    polarity = {
        entity_id: report["polarity_evidence"]
        for entity_id, report in channel_reports.items()
        if report.get("polarity_evidence")
    }

    # Cross-channel check: two branch CTs that track each other almost exactly
    # may be measuring the same current.  Done per *CT channel*, not per device:
    # the duplicate pair found here has one channel on each of two different
    # devices, which device-level aggregation would compare as two unrelated
    # loads.
    duplicate_series: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    channel_devices: dict[str, str] = {}
    for device in devices:
        device_id = str(device["device_id"])
        for ct_id, entity_id in zip(
            device.get("ct_ids") or [], device.get("ct_entities") or [], strict=False
        ):
            series = numeric_series(power.get(entity_id))
            if series[1].size >= 50:
                duplicate_series[str(ct_id)] = series
                channel_devices[str(ct_id)] = device_id
    duplicates = detect_duplicate_channels(
        duplicate_series, paired=paired_channels(topology)
    )

    # A device whose only channel duplicates another device's channel cannot be
    # mapped independently: any entity that fits the conductor fits both, so a
    # mapping would attribute one appliance twice.  Withdraw such mappings and
    # say why.  The alternative — silently keeping the higher-scoring of the two
    # — invents certainty about which CT the installer actually clamped.
    duplicate_of: dict[str, dict[str, Any]] = {}
    for finding in duplicates:
        if finding["kind"] != "same_current":
            continue
        forward = finding["ratio_first_to_second"]
        for mine, other in (
            (finding["first"], finding["second"]),
            (finding["second"], finding["first"]),
        ):
            device_id = channel_devices.get(mine)
            other_device = channel_devices.get(other)
            if device_id is None or other_device is None or device_id == other_device:
                continue
            # Record the ratio in this device's own direction: watts of this
            # channel per watt of the channel it duplicates.
            ratio = forward if mine == finding["first"] else (1.0 / forward if forward else None)
            duplicate_of[device_id] = {
                "duplicate_of": other_device,
                "duplicate_ct_id": other,
                "ratio": None if ratio is None else round(float(ratio), 4),
                "correlation": finding["correlation"],
                "ratio_relative_iqr": finding["ratio_relative_iqr"],
                "median_abs_residual_w": finding["median_abs_residual_w"],
            }

    for device_id in sorted(duplicate_of):
        detail = duplicate_of[device_id]
        entry = device_reports.get(device_id)
        if entry is None:
            continue
        entry["duplicate_of"] = detail["duplicate_of"]
        entry["duplicate_ratio"] = detail["ratio"]
        entry["duplicate_evidence"] = dict(detail)
        if entry["status"] in {"confounded", "ambiguous"}:
            # The channel already refuses to name a single appliance, and that
            # finding stands: two independently switchable loads do add power on
            # this conductor whether or not it is also clamped twice.  Duplication is recorded alongside, not instead.
            entry["duplicate_evidence"]["note"] = (
                "this channel is also a duplicate reading of another CT; the existing "
                "multi-load finding is unaffected and no single entity may be mapped"
            )
            continue
        if entry["status"] in {"supported", "weak"}:
            previous = entry["status"]
            counts[previous] -= 1
            counts["unsupported"] += 1
            entry.update(
                status="unsupported",
                reason=f"duplicate_of:{detail['duplicate_of']}",
                state_entity_ids=[],
                state_overrides={},
            )
            entry["evidence"] = {
                "method": "duplicate_channel_test",
                "previous_status": previous,
                "previous_entity_ids": list(
                    (entry.get("evidence") or {}).get("state_entity_ids") or []
                ),
                **detail,
                "note": (
                    "this channel holds a fixed ratio to another channel to within a "
                    "fraction of a watt, so the two CTs are measuring the same conductor; "
                    "mapping an entity here would attribute one appliance twice"
                ),
            }

    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "source": dict(source or {}),
        "method": {
            "name": "evidence-based state entity derivation",
            "grid_s": GRID_S,
            "max_lag_s": MAX_LAG_S,
            "coincidence_tolerance_s": COINCIDENCE_TOL_S,
            "min_transitions": MIN_TRANSITIONS,
            "min_separation_w": MIN_SEPARATION_W,
            "min_coverage": MIN_COVERAGE,
            "supported_balanced_accuracy": SUPPORTED_BALANCED_ACCURACY,
            "weak_balanced_accuracy": WEAK_BALANCED_ACCURACY,
            "min_null_margin": MIN_NULL_MARGIN,
            "min_marginal_effect_w": MIN_MARGINAL_EFFECT_W,
            "null_resamples": NULL_RESAMPLES,
            "safeguards": [
                "state-gated level test scored by balanced accuracy (never name similarity)",
                "coverage test: the entity must be the load, not merely correlate",
                "lag sweep for template/cloud entities (0-600 s measured)",
                "relocation null preserving duty cycle and episode lengths",
                "circularity test against the same CT at the winning lag",
                "same-appliance facets grouped by the HA registry device_id",
                "marginal-effect confound test on the 2x2 cross-tab",
                "sole-carrier test: a companion/control flag that adds no power is not ambiguity",
                "nested-circuit ambiguity reported, never resolved",
                "channel polarity and dead-channel detection",
                "cross-channel duplicate-CT correlation report",
            ],
        },
        "summary": {
            **counts,
            "devices": len(device_reports),
            "channels": len(channel_reports),
            "polarity_candidates": len(polarity),
            "duplicate_channel_candidates": len(duplicates),
            "mapped_devices": counts["supported"] + counts["weak"],
        },
        "polarity_candidates": polarity,
        "duplicate_channel_candidates": duplicates,
        "channels": channel_reports,
        "devices": device_reports,
    }


def device_inputs(topology: Mapping[str, Any]) -> list[dict[str, Any]]:
    """``device_id``/``ct_ids``/``ct_entities`` from a topology payload."""
    channels = {str(item["ct_id"]): item for item in topology.get("channels") or []}
    out: list[dict[str, Any]] = []
    for device in topology.get("devices") or []:
        ct_ids = [
            *(device.get("dedicated_ct_ids") or []),
            *(device.get("branch_ct_ids") or []),
        ]
        out.append(
            {
                "device_id": device["device_id"],
                "name": device.get("name"),
                "ct_ids": [str(item) for item in ct_ids],
                "ct_entities": [
                    channels[str(item)]["entity_id"]
                    for item in ct_ids
                    if str(item) in channels
                ],
            }
        )
    return out


def registry_maps(
    registry: Mapping[str, Any],
) -> tuple[dict[str, str], dict[str, str]]:
    """``(entity -> platform, entity -> device_id)`` from a registry payload."""
    platforms: dict[str, str] = {}
    devices: dict[str, str] = {}
    for item in registry.get("entities") or []:
        entity_id = item.get("entity_id")
        if not entity_id:
            continue
        platforms[str(entity_id)] = str(item.get("platform") or "")
        device_id = item.get("device_id")
        if device_id:
            devices[str(entity_id)] = str(device_id)
    return platforms, devices


async def fetch_live(days: float) -> dict[str, Any]:
    """Read registries, states and history from HA (read-only)."""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from ha_client import HAClient, fetch_history

    async with HAClient.from_env() as client:
        registries = await client.registries()
        entities = registries.get("entities") or []
        if not isinstance(entities, list):
            raise RuntimeError(f"entity registry unavailable: {entities}")
        registry = {"entities": entities}
        platforms, registry_devices = registry_maps(registry)
        state_ids = [
            str(item["entity_id"])
            for item in entities
            if item.get("entity_id")
            and str(item["entity_id"]).split(".", 1)[0] in CANDIDATE_DOMAINS
            and not item.get("disabled_by")
        ]
        topology = json.loads((DATA / "topology_candidates.json").read_text())
        power_ids = sorted(
            {
                str(channel["entity_id"])
                for channel in topology.get("channels") or []
                if channel.get("entity_id")
            }
        )
        states = await fetch_history(
            client, state_ids, days=days, minimal=True, significant=True
        )
        power = await fetch_history(client, power_ids, days=days, minimal=True)
    return {
        "states": states,
        "power": power,
        "platforms": platforms,
        "registry_devices": registry_devices,
        "topology": topology,
        "source": {
            "kind": "live",
            "days": days,
            "entities_requested": len(state_ids),
            "power_entities_requested": len(power_ids),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--from-cache", type=Path, default=None)
    parser.add_argument("--days", type=float, default=7.0)
    parser.add_argument("--out", type=Path, default=DATA / "state_entity_candidates.json")
    parser.add_argument("--topology", type=Path, default=DATA / "topology_candidates.json")
    parser.add_argument("--registry", type=Path, default=DATA / "entity_registry_slim.json")
    parser.add_argument("--top-k", type=int, default=12)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    import asyncio

    if args.from_cache is not None:
        payload = json.loads(args.from_cache.read_text())
        topology = json.loads(args.topology.read_text())
        registry: dict[str, Any] = {}
        if args.registry.exists():
            # A history cache does not carry the registry; read the platform and
            # device map from the last recorded registry dump so a replay is
            # faithful to the live run.
            registry = json.loads(args.registry.read_text())
        platforms, registry_devices = registry_maps(registry)
        platforms.update(payload.get("platforms") or {})
        registry_devices.update(payload.get("registry_devices") or {})
        source = {
            "kind": "cache",
            "path": str(args.from_cache),
            **{key: payload[key] for key in ("start", "end") if key in payload},
        }
        states = payload.get("states") or {}
        power = payload.get("power") or {}
    else:
        payload = asyncio.run(fetch_live(args.days))
        topology = payload["topology"]
        platforms = payload["platforms"]
        registry_devices = payload["registry_devices"]
        source = payload["source"]
        states = payload["states"]
        power = payload["power"]

    report = build_report(
        power=power,
        states=states,
        devices=device_inputs(topology),
        platforms=platforms,
        registry_devices=registry_devices,
        source=source,
        topology=topology,
        top_k=args.top_k,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, sort_keys=False) + "\n")

    summary = report["summary"]
    if not args.quiet:
        print(json.dumps(summary, indent=2))
        for device_id, entry in report["devices"].items():
            print(
                f'{entry["status"]:12s} {device_id:52s} {entry["reason"]:38s}'
                f' {",".join(entry["state_entity_ids"])}'
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
