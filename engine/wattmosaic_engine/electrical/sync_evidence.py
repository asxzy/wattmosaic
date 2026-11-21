"""E6 §6: co-sampling, synchronous verdicts and pulse evidence.

This module is pure: no clock, no I/O and no database.  The runtime feeds it
from the fast path (a bounded timestamp ring and a bounded pulse ring per
reader) and consumes its results at aligned rebuilds / ledger folds.  Nothing
here names a reader or uses an id pattern; every verdict is derived from the
declared samples alone.

Co-sampling (E6 §6)
-------------------
Two readers are co-sampled when at least ``COSAMPLE_MIN_RATIO`` of each one's
sample timestamps have a counterpart on the other within ``COSAMPLE_WINDOW_S``.
Channels of one monitor publish in one batch, so their timestamps coincide.
The runtime tracks timestamps cheaply in :class:`SampleStampRing` and refreshes
the co-sampled set only at aligned rebuilds.

Synchronous verdict (E6 §6)
---------------------------
For a co-sampled parent/child pair the T5 fold measures the parent's change
across **exactly the child's sample pair**: the parent sample at or just before
the child's pre-step sample and the parent sample at or just after the child's
post-step sample.  No anchored window and no P6 spread veto apply:

* support: ``|Δp - Δc·s_c| <= 0.10·|Δc·s_c| + 15 W`` with the same sign;
* conflict: ``|Δp| <= 3σ_p`` while ``|Δc|`` is resolvable;
* otherwise neutral.

Pulse evidence (E6 §6)
----------------------
A confirmed single-sample pulse (a step reversed at the next sample) used to be
dropped as a transient.  For co-sampled pairs the pulse ring turns one pulse
into two synchronous verdicts (the ON step and the OFF step), so automation-
alternating and pulsing loads become visible.  The P3 detector keeps its
behaviour for everything else.
"""

from __future__ import annotations

import bisect
import math
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

__all__ = [
    "COSAMPLE_WINDOW_S",
    "COSAMPLE_MIN_RATIO",
    "SYNC_MATCH_REL",
    "SYNC_MATCH_ABS_W",
    "SYNC_SIGMA_FACTOR",
    "PulseRecord",
    "SampleStampRing",
    "PulseRing",
    "co_sampled_pair",
    "co_sampled_readers",
    "sync_verdict",
    "pulse_verdicts",
]

#: E6 §6: two samples within 2 s are one monitor batch; >= 90 % of each
#: reader's stamps must match for the pair to be co-sampled.
COSAMPLE_WINDOW_S = 2.0
COSAMPLE_MIN_RATIO = 0.90
#: E6 §6 synchronous verdict tolerances.
SYNC_MATCH_REL = 0.10
SYNC_MATCH_ABS_W = 15.0
SYNC_SIGMA_FACTOR = 3.0


class SampleStampRing:
    """Bounded, O(1)-per-sample ring of ``(observed_at, power)`` stamps.

    Kept from the fast path so co-sampling can be decided from actual sample
    timestamps rather than from reader ids.  A bounded (deque-like) list is
    enough because co-sampling is refreshed from the most recent samples only.
    """

    __slots__ = ("capacity", "_stamps", "_values", "_lock")

    def __init__(self, capacity: int = 512) -> None:
        self.capacity = max(8, int(capacity))
        self._stamps: list[float] = []
        self._values: list[float] = []
        self._lock = threading.Lock()

    def record(self, at: float, value: float | None) -> None:
        if not math.isfinite(at):
            return
        with self._lock:
            self._stamps.append(float(at))
            self._values.append(float(value) if value is not None and math.isfinite(value) else math.nan)
            excess = len(self._stamps) - self.capacity
            if excess > 0:
                del self._stamps[:excess]
                del self._values[:excess]

    def snapshot(self) -> tuple[list[float], list[float]]:
        with self._lock:
            return list(self._stamps), list(self._values)

    def stamps(self) -> list[float]:
        with self._lock:
            return list(self._stamps)


@dataclass(frozen=True, slots=True)
class PulseRecord:
    """One confirmed single-sample pulse: pre, peak and post levels + times."""

    pre_t: float
    peak_t: float
    post_t: float
    pre: float
    peak: float
    post: float
    sigma: float = 1.0

    @property
    def up_delta(self) -> float:
        return self.peak - self.pre

    @property
    def down_delta(self) -> float:
        return self.post - self.peak


class PulseRing:
    """Bounded ring of confirmed single-sample pulses (fast path: O(1) detect).

    A pulse is a level ``pre`` -> ``peak`` at one sample that reverses to
    ``post ≈ pre`` at the very next sample.  The detector keeps its own state;
    this ring only records the pattern so a T5 fold for a co-sampled pair can
    turn it into two synchronous verdicts.  It never emits a P3 event and never
    changes any other reader's behaviour.
    """

    __slots__ = ("capacity", "sigma_floor", "_pending", "_pulses", "_lock")

    def __init__(self, capacity: int = 64, *, sigma_floor: float = 1.0) -> None:
        self.capacity = max(4, int(capacity))
        self.sigma_floor = float(sigma_floor)
        self._pending: tuple[float, float, float, float] | None = None  # t_pre, pre, t_peak, peak
        self._pulses: list[PulseRecord] = []
        self._lock = threading.Lock()

    def record(
        self,
        at: float,
        value: float | None,
        *,
        sigma: float | None = None,
        tolerance_w: float = 15.0,
    ) -> PulseRecord | None:
        """Observe one sample; return a :class:`PulseRecord` when a pulse closes.

        O(1).  A sample that is different enough from the last one opens a
        *pending* excursion; if the very next sample returns within
        ``tolerance_w`` of the pre level, a pulse is recorded.  Any other third
        sample clears the pending excursion (it was a real step, not a pulse).
        """
        if not math.isfinite(at) or value is None or not math.isfinite(value):
            return None
        value = float(value)
        tol = max(tolerance_w, self.sigma_floor * 2.0)
        with self._lock:
            pending = self._pending
            out: PulseRecord | None = None
            if pending is None:
                self._pending = (at, value, at, value)
                return None
            t_pre, pre, t_peak, peak = pending
            if t_peak == t_pre:
                # The pending entry is just the first sample: a move away opens
                # the excursion, an unchanged sample refreshes the baseline.
                if abs(value - pre) > tol:
                    self._pending = (t_peak, peak, at, value)
                else:
                    self._pending = (at, value, at, value)
                return None
            if abs(value - pre) <= tol and abs(peak - pre) > tol:
                out = PulseRecord(
                    pre_t=t_pre,
                    peak_t=t_peak,
                    post_t=at,
                    pre=pre,
                    peak=peak,
                    post=value,
                    sigma=float(sigma) if sigma is not None and math.isfinite(sigma) else self.sigma_floor,
                )
                self._pulses.append(out)
                if len(self._pulses) > self.capacity:
                    del self._pulses[: len(self._pulses) - self.capacity]
            # A new level either way becomes the next pre baseline.
            self._pending = (at, value, at, value)
            return out

    def drain(self, *, since: float | None = None) -> list[PulseRecord]:
        """Return (and remove) recorded pulses, optionally only those after ``since``."""
        with self._lock:
            if since is None:
                out, self._pulses = self._pulses, []
            else:
                out = [p for p in self._pulses if p.post_t >= since]
                self._pulses = [p for p in self._pulses if p.post_t < since]
            return out

    def peek(self, *, since: float | None = None) -> list[PulseRecord]:
        with self._lock:
            if since is None:
                return list(self._pulses)
            return [p for p in self._pulses if p.post_t >= since]


def co_sampled_pair(
    stamps_a: Sequence[float],
    stamps_b: Sequence[float],
    *,
    window_s: float = COSAMPLE_WINDOW_S,
    min_ratio: float = COSAMPLE_MIN_RATIO,
    min_samples: int = 4,
) -> bool:
    """E6 §6: True when two readers' sample timestamps coincide (>= 90 %).

    Both directions must match: a sparse reader that happens to sit inside a
    dense reader's batch window is not co-sampled with it.  ``min_samples``
    prevents a couple of coincidences from declaring co-sampling.
    """
    a = sorted(float(t) for t in stamps_a if math.isfinite(float(t)))
    b = sorted(float(t) for t in stamps_b if math.isfinite(float(t)))
    if len(a) < min_samples or len(b) < min_samples:
        return False

    def matched(src: Sequence[float], other: Sequence[float]) -> float:
        hits = 0
        j = 0
        for t in src:
            while j < len(other) and other[j] < t - window_s:
                j += 1
            if j < len(other) and abs(other[j] - t) <= window_s:
                hits += 1
        return hits / len(src)

    return matched(a, b) >= min_ratio and matched(b, a) >= min_ratio


def co_sampled_readers(
    stamps: Mapping[str, Sequence[float]],
    *,
    window_s: float = COSAMPLE_WINDOW_S,
    min_ratio: float = COSAMPLE_MIN_RATIO,
) -> frozenset[frozenset[str]]:
    """All unordered co-sampled reader pairs from their sample stamps."""
    readers = sorted(r for r, ts in stamps.items() if len(ts) >= 2)
    pairs: set[frozenset[str]] = set()
    for i, a in enumerate(readers):
        for b in readers[i + 1 :]:
            if co_sampled_pair(stamps[a], stamps[b], window_s=window_s, min_ratio=min_ratio):
                pairs.add(frozenset((a, b)))
    return frozenset(pairs)


def _parent_change_at(
    parent_stamps: Sequence[float],
    parent_values: Sequence[float],
    pre_t: float,
    post_t: float,
) -> float | None:
    """The parent's change between the samples bounding the child's sample pair.

    ``pre_t``/``post_t`` are the child's pre-step and post-step sample times.
    Use the parent sample at or just before ``pre_t`` and the one at or just
    after ``post_t``.  No window and no spread veto: this is exactly the child's
    sample pair.
    """
    if pre_t > post_t or not parent_stamps:
        return None
    before = bisect.bisect_right(parent_stamps, pre_t) - 1
    after = bisect.bisect_left(parent_stamps, post_t)
    if before < 0 or after >= len(parent_stamps) or before > after:
        return None
    pre, post = parent_values[before], parent_values[after]
    if not (math.isfinite(pre) and math.isfinite(post)):
        return None
    return float(post - pre)


def sync_verdict(
    parent_delta: float | None,
    child_delta: float,
    *,
    sigma_p: float,
    beta: float = 1.0,
    sync_scale: float = 1.0,
) -> str | None:
    """E6 §6 synchronous verdict with no window and no P6 spread veto.

    ``parent_delta`` is measured across exactly the child's sample pair (see
    :func:`_parent_change_at`); ``None`` means the parent had no bounding sample
    and the pair is neutral.  ``sync_scale`` is the child's monitor factor
    inverse (applied to the child delta before classification, like the legacy
    fold).  Support requires the same sign and a within-tolerance match;
    conflict requires the parent flat within 3σ while the child is resolvable.
    """
    if parent_delta is None or not math.isfinite(parent_delta):
        return None
    scaled = beta * float(child_delta) * float(sync_scale)
    tolerance = SYNC_MATCH_REL * abs(scaled) + SYNC_MATCH_ABS_W
    flat_band = SYNC_SIGMA_FACTOR * float(sigma_p)
    if abs(parent_delta - scaled) <= tolerance and (parent_delta > 0) == (scaled > 0):
        return "support"
    if abs(parent_delta) <= flat_band and abs(scaled) > flat_band:
        return "conflict"
    return None


def pulse_verdicts(
    pulse: PulseRecord,
    parent_stamps: Sequence[float],
    parent_values: Sequence[float],
    *,
    sigma_p: float,
    beta: float = 1.0,
) -> tuple[tuple[str, float | None, float], ...]:
    """Two synchronous verdicts for one confirmed pulse (E6 §6).

    Returns ``((verdict, parent_change, child_delta), ...)`` for the ON step
    (pre -> peak) and the OFF step (peak -> post).  The child deltas come from
    the pulse itself; the parent changes are measured across each child sample
    pair with :func:`_parent_change_at`.
    """
    up = _parent_change_at(parent_stamps, parent_values, pulse.pre_t, pulse.peak_t)
    down = _parent_change_at(parent_stamps, parent_values, pulse.peak_t, pulse.post_t)
    return (
        (sync_verdict(up, pulse.up_delta, sigma_p=sigma_p, beta=beta) or "neutral", up, pulse.up_delta),
        (sync_verdict(down, pulse.down_delta, sigma_p=sigma_p, beta=beta) or "neutral", down, pulse.down_delta),
    )
