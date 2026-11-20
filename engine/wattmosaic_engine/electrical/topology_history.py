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
* Each step is folded as one group through
  :meth:`TopologyInferenceView.observe_group`, so support, conflict, lifecycle
  and direction rules are the live ones, not a second implementation.

Pure: no I/O, no clock.  The caller provides the series and merges the learned
ledger into the live one (:meth:`TopologyInferenceView.merge_evidence`).
"""

from __future__ import annotations

import bisect
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .topology_inference import DeltaObservation, TopologyInferenceView

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
    """Fold every clean step in ``history`` (reader -> samples) into ``view``.

    ``view`` is normally an empty ledger seeded with the user's pinned and
    rejected pairs, so user authority applies during the replay too.
    """
    series = {reader: _series(rows) for reader, rows in history.items()}
    series = {reader: s for reader, s in series.items() if len(s.values) >= 4}
    sigmas = {reader: robust_sigma(s.values) for reader, s in series.items()}
    events: list[tuple[datetime, str, datetime, float]] = []
    for reader, s in series.items():
        for lo, hi, delta in clean_steps(s.times, s.values, sigmas[reader]):
            events.append((hi, reader, lo, delta))
    events.sort()
    for hi, child, lo, delta in events:
        measured: dict[str, float] = {}
        observed: dict[str, Any] = {}
        for reader, s in series.items():
            if reader == child:
                continue
            change = _anchored_change(s, lo, hi)
            if change is None:
                continue  # not measurable across this step: no evidence
            measured[reader] = change
            observed[reader] = (sigmas[reader], change)
        cause = f"hist:{child}:{hi.isoformat()}"
        view = view.observe_group(
            DeltaObservation(
                group_id=cause, deltas={child: (delta, sigmas[child])}, at=hi, cause_id=cause
            ),
            observed=observed,
            measured_by_child={child: measured},
        )
    stats = HistoryReplayStats(
        readers=len(series),
        samples=sum(len(s.values) for s in series.values()),
        steps=len(events),
    )
    return view, stats
