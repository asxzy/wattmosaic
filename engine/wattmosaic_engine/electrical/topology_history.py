"""Learn topology from recorded history (the "Discover" button).

The live T5 ledger only sees events after the engine started, so a fresh install
had to wait hours for its first edge.  Recorded power history (HA's recorder)
holds the same evidence: this module replays it through the *same* ledger.

* A **clean step** of a reader is a jump between consecutive samples whose
  levels just before and just after are stable, larger than the reader's own
  robust noise.
* Every other reader is measured across that step exactly like the live runtime
  does (``core.runtime`` step-anchored measurement): the median of its samples
  in the 45 s before the step's last pre-sample versus the 45 s after its first
  post-sample, with the held value as the "before" level of a quiet reporter.
* Each step is classified with the ledger's own rule
  (``topology_inference._classify_step``) and counted per pair in the order it
  happened, with ``observe_group``'s bookkeeping: a pair exists from its first
  support, and only an existing pair collects conflict or parent-only steps.
  The learned edges are then settled by
  :meth:`TopologyInferenceView.merge_evidence`, so lifecycle and direction rules
  are the live ones.

Counting in plain dicts instead of folding every step through
``observe_group`` matters: the fold copies the whole ledger per step, and a
week of history (~27k steps) held the GIL for minutes, starving the engine's
event loop until the gateway timed requests out (live, 2026-11-20).

Pure: no I/O, no clock.  The caller provides the series and merges the learned
ledger into the live one (:meth:`TopologyInferenceView.merge_evidence`).
"""

from __future__ import annotations

import bisect
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

from wattmosaic_engine.contracts import EdgeLifecycleV3, RelationType, TopologyEdgeV3

from .topology_inference import (
    _DORMANT,
    _SIGMA_FACTOR,
    TopologyEvidence,
    TopologyInferenceView,
    _classify_step,
)

__all__ = ["HistoryReplayStats", "clean_steps", "learn_topology_from_history", "robust_sigma"]

#: Median windows either side of a step (matches the runtime's ``_T5_ANCHOR_S``).
ANCHOR_S = 45.0
#: A quiet reporter's held value counts as its "before" level within this span.
HOLD_LOOKBACK_S = 600.0
#: A step must exceed this many robust sigmas of its own reader, and this floor.
STEP_SIGMA_FACTOR = 5.0
MIN_STEP_W = 20.0
#: The samples either side of a step may vary by at most this share of the step.
STABLE_FRACTION = 0.25
SIGMA_FLOOR_W = 2.0


@dataclass(frozen=True)
class HistoryReplayStats:
    readers: int
    samples: int
    steps: int

    def as_dict(self) -> dict[str, Any]:
        return {"readers": self.readers, "samples": self.samples, "steps": self.steps}


@dataclass(frozen=True)
class _Series:
    times: list[datetime]
    values: list[float]

    def window(self, start: datetime, end: datetime) -> list[float]:
        lo = bisect.bisect_left(self.times, start)
        hi = bisect.bisect_right(self.times, end)
        return self.values[lo:hi]

    def held_before(self, at: datetime, lookback_s: float) -> float | None:
        index = bisect.bisect_right(self.times, at) - 1
        if index < 0 or (at - self.times[index]).total_seconds() > lookback_s:
            return None
        return self.values[index]


def _series(rows: Sequence[tuple[datetime, float | None]]) -> _Series:
    clean = sorted((t, float(v)) for t, v in rows if v is not None)
    return _Series([t for t, _ in clean], [v for _, v in clean])


def robust_sigma(values: Sequence[float]) -> float:
    """Noise sigma from consecutive differences (MAD), floored at 2 W.

    The median absolute difference ignores the reader's real steps, so a busy
    feed is not judged by its load changes.
    """
    diffs = [abs(b - a) for a, b in zip(values, values[1:], strict=False)]
    if not diffs:
        return SIGMA_FLOOR_W
    return max(1.4826 * statistics.median(diffs) / 2**0.5, SIGMA_FLOOR_W)


def clean_steps(
    times: Sequence[datetime], values: Sequence[float], sigma: float
) -> list[tuple[datetime, datetime, float]]:
    """``(last pre-step sample, first post-step sample, delta)`` per clean step."""
    threshold = max(MIN_STEP_W, STEP_SIGMA_FACTOR * sigma)
    out: list[tuple[datetime, datetime, float]] = []
    for k in range(2, len(values) - 1):
        jump = values[k] - values[k - 1]
        if abs(jump) < threshold:
            continue
        pre, post = values[k - 2 : k], values[k : k + 2]
        allowed = STABLE_FRACTION * abs(jump)
        if max(pre) - min(pre) > allowed or max(post) - min(post) > allowed:
            continue
        delta = statistics.median(post) - statistics.median(pre)
        out.append((times[k - 1], times[k], float(delta)))
    return out


def _anchored_change(series: _Series, lo: datetime, hi: datetime) -> float | None:
    span = timedelta(seconds=ANCHOR_S)
    post = series.window(hi, hi + span)
    if not post:
        return None
    pre = series.window(lo - span, lo)
    if not pre:
        held = series.held_before(lo, HOLD_LOOKBACK_S)
        if held is None:
            return None
        pre = [held]
    return float(statistics.median(post) - statistics.median(pre))


def learn_topology_from_history(
    view: TopologyInferenceView,
    history: Mapping[str, Sequence[tuple[datetime, float | None]]],
) -> tuple[TopologyInferenceView, HistoryReplayStats]:
    """Count every clean step in ``history`` (reader -> samples) per pair.

    ``view`` is normally an empty ledger seeded with the user's pinned,
    declared and rejected pairs: those pairs are never counted (user authority).
    Returns the seed plus CANDIDATE edges carrying the counted evidence;
    :meth:`TopologyInferenceView.merge_evidence` settles their lifecycles.
    """
    series = {reader: _series(rows) for reader, rows in history.items()}
    series = {reader: s for reader, s in series.items() if len(s.values) >= 4}
    sigmas = {reader: robust_sigma(s.values) for reader, s in series.items()}
    events: list[tuple[datetime, str, datetime, float]] = []
    for reader, s in series.items():
        for lo, hi, delta in clean_steps(s.times, s.values, sigmas[reader]):
            events.append((hi, reader, lo, delta))
    events.sort()

    authority = {
        (e.source_endpoint_id, e.target_endpoint_id)
        for e in view.edges
        if e.pinned or e.declared_by == "user" or e.lifecycle in _DORMANT
    }
    support: dict[tuple[str, str], int] = {}
    conflict: dict[tuple[str, str], int] = {}
    parent_only: dict[tuple[str, str], int] = {}
    causes: dict[tuple[str, str], set[str]] = {}
    first_at: dict[tuple[str, str], datetime] = {}
    last_support: dict[tuple[str, str], datetime] = {}
    last_conflict: dict[tuple[str, str], datetime] = {}

    for hi, child, lo, delta in events:
        cause = f"hist:{child}:{hi.isoformat()}"
        sigma_c = sigmas[child]
        for reader, s in series.items():
            if reader == child:
                continue
            change = _anchored_change(s, lo, hi)
            if change is None:
                continue  # not measurable across this step: no evidence
            # ``reader`` as a candidate parent of the stepping ``child``.
            key = (reader, child)
            if key not in authority:
                verdict = _classify_step(change, delta, sigma_p=sigmas[reader], sigma_c=sigma_c)
                if verdict == "support":
                    support[key] = support.get(key, 0) + 1
                    causes.setdefault(key, set()).add(cause)
                    first_at.setdefault(key, hi)
                    last_support[key] = hi
                elif verdict == "conflict" and key in support:
                    # As in observe_group: a pair exists from its first support,
                    # and only an existing pair collects conflict.
                    conflict[key] = conflict.get(key, 0) + 1
                    causes[key].add(cause)
                    last_conflict[key] = hi
            # The stepping reader as parent of a ``reader`` that stayed flat: a
            # parent-only step (neutral residual evidence, AT-02).
            down = (child, reader)
            if down in support and abs(change) <= _SIGMA_FACTOR * sigmas[reader]:
                parent_only[down] = parent_only.get(down, 0) + 1

    seeded = {(e.source_endpoint_id, e.target_endpoint_id) for e in view.edges}
    learned = tuple(
        TopologyEdgeV3(
            edge_id=f"te-inf::{parent}::{child}",
            relation=RelationType.CONTAINS,
            source_endpoint_id=parent,
            target_endpoint_id=child,
            lifecycle=EdgeLifecycleV3.CANDIDATE,
            declared_by="inference",
            beta=1.0,
            beta_basis="unity",
            evidence=TopologyEvidence(
                support=count,
                conflict=conflict.get((parent, child), 0),
                parent_only=parent_only.get((parent, child), 0),
                independent_causes=len(causes[(parent, child)]),
                last_support_at=last_support.get((parent, child)),
                last_conflict_at=last_conflict.get((parent, child)),
            ),
            valid_from=first_at[(parent, child)],
            pinned=False,
        )
        for (parent, child), count in sorted(support.items())
        if (parent, child) not in seeded
    )
    result = replace(
        view,
        edges=(*view.edges, *learned),
        causes_by_edge={
            **view.causes_by_edge,
            **{(p, c, "contains"): frozenset(values) for (p, c), values in causes.items()},
        },
    )
    stats = HistoryReplayStats(
        readers=len(series),
        samples=sum(len(s.values) for s in series.values()),
        steps=len(events),
    )
    return result, stats
