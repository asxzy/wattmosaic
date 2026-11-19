"""ReaderStateStore: one ring per reader, keyed by ``observed_at`` (P2).

Replaces the four per-channel buffers that used to run on four different clocks
with one zero-order-hold store per reader:

* the ring is ordered by :attr:`ReaderMeasurement.observed_at` (the physical
  time of the sample), not by receive or cycle time;
* a value is *held* from its ``observed_at`` until a newer value supersedes it
  (push-on-change silence keeps the last value valid, ADR-0015 / DEC-16);
* a held reading is *stale* after ``stale_after_s`` and *missing* after
  ``missing_after_s`` -- and a missing reading is ``None`` everywhere (HB §18),
  never 0;
* innovations (successive differences), the baseline and the long-run noise
  estimate live beside the ring.  σ is estimated from up to six hours of
  non-event samples (never from the two samples inside a 30 s pre-window,
  DEC-18), with a resolution-derived floor and a 1 W minimum;
* the baseline is an EWMA (τ = 30 min) that is frozen while the detector is in
  SUSPECT or settling, so a persistent step is not absorbed (``freeze_baseline``);
* late samples are inserted in order and fire the ``on_late`` callback exactly
  once; a snapshot/restore pair persists all hidden state for restart (HB §15).

The module is **pure**: no database access and no wall clock.  Every operation
that needs "now" takes it as an explicit argument (``held(reader, at)`` etc.);
an optional injected clock is a caller convenience, never a hidden dependency.
Persistence is a caller concern via :meth:`ReaderStateStore.snapshot_state` and
:meth:`ReaderStateStore.restore_state` (and the repository methods added to
``storage/repositories/samples.py`` by this task).

Production code here never imports ``runtime.fakes``.
"""

from __future__ import annotations

import bisect
import contextlib
import json
import math
import struct
import threading
import zlib
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import islice
from typing import Any, Literal

from ..contracts import QualityFlag, ReaderMeasurement
from ..electrical.window import DEFAULT_CADENCE_S, clamp_cadence
from ..runtime.interfaces import HeldReading
from ..synchronization.aligner import (
    CADENCE_SOURCE_DEFAULT,
    CADENCE_SOURCE_OBSERVED,
    MAD_TO_SIGMA,
    MIN_CADENCE_INTERVALS,
    ResolvedCadence,
)

__all__ = [
    "AddResult",
    "ReaderState",
    "ReaderStateStore",
    "WindowSample",
]

# Defaults from design §3 / HB §6.2.
DEFAULT_STALE_AFTER_S = 120.0
DEFAULT_MISSING_AFTER_S = 600.0
DEFAULT_SIGMA_MIN_W = 1.0
DEFAULT_BASELINE_TAU_S = 30.0 * 60.0
DEFAULT_NOISE_WINDOW = timedelta(hours=6)
DEFAULT_NOISE_MIN_SAMPLES = 4
DEFAULT_RING_MIN_S = 10.0 * 60.0
DEFAULT_RING_MIN_SAMPLES = 40
DEFAULT_PERSIST_INTERVAL_S = 10.0 * 60.0
_NOISE_RECOMPUTE_EVERY = 64
# Bounded sketch for the MAD noise estimate on long histories (see
# ``_recompute_sigma``).  Histories at or below this size stay exact; longer
# ones use a deterministic strided subsample (MAD error < 0.5 % in practice).
_NOISE_MAD_MAX_SAMPLES = 256
_SNAPSHOT_MAGIC = b"RSTM"
_SNAPSHOT_VERSION = 1

_NON_EVIDENCE_FLAGS = frozenset(
    {
        QualityFlag.UNIT_INVALID.value,
        QualityFlag.IMPLAUSIBLE_RANGE.value,
        QualityFlag.OUT_OF_ORDER.value,
    }
)


def _aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _iso(dt: datetime | None) -> str | None:
    return None if dt is None else _aware(dt).isoformat()


def _parse_dt(value: str | None) -> datetime | None:
    return _aware(datetime.fromisoformat(value)) if value else None


def _encode_flags(flags: Iterable[QualityFlag]) -> tuple[str, ...]:
    return tuple(str(getattr(f, "value", f)) for f in flags)


def _decode_flags(values: Iterable[str]) -> tuple[QualityFlag, ...]:
    out: list[QualityFlag] = []
    for value in values:
        with contextlib.suppress(ValueError):
            out.append(QualityFlag(value))
    return tuple(out)


def _has_bad_flags(flags: Sequence[QualityFlag]) -> bool:
    return any(str(getattr(f, "value", f)) in _NON_EVIDENCE_FLAGS for f in flags)


@dataclass(frozen=True)
class WindowSample:
    """One retained sample in a :meth:`ReaderStateStore.window` result."""

    observed_at: datetime
    received_at: datetime
    active_power_w: float | None
    quality_flags: tuple[QualityFlag, ...]
    sequence: int


@dataclass(frozen=True)
class AddResult:
    """Outcome of :meth:`ReaderStateStore.add` (P2 API)."""

    late: bool
    reader_id: str
    observed_at: datetime
    #: ``True`` when a genuinely new (non-repeated) sample advanced the state.
    accepted: bool = False


@dataclass(frozen=True)
class ReaderState:
    """A frozen, inspectable view of one reader's state."""

    reader_id: str
    samples: tuple[WindowSample, ...]
    observed_at: datetime | None
    received_at: datetime | None
    held_since: datetime | None
    last_confirmed_at: datetime | None
    baseline_w: float | None
    sigma_w: float | None
    resolution_w: float | None
    frozen_until: datetime | None
    frozen: bool
    last_sequence: int
    has_baseline_at: bool = False
    last_baseline_at: datetime | None = None


class _ReaderRing:
    """Per-reader ring plus rolling baseline, noise and hold bookkeeping."""

    __slots__ = (
        "reader_id",
        "samples",
        "_times",
        "_maxlen",
        "_maxage",
        "observed_at",
        "received_at",
        "held_since",
        "last_confirmed_at",
        "baseline_w",
        "sigma_w",
        "resolution_w",
        "frozen_until",
        "frozen",
        "last_sequence",
        "_innovations",
        "_sorted_innovations",
        "_last_power",
        "_last_baseline_at",
        "_ewma_tau",
        "_sigma_min_w",
        "_intervals",
        "_interval_median",
        "_cadence_default",
    )

    def __init__(
        self,
        reader_id: str,
        *,
        max_samples: int,
        max_age_s: float,
    ) -> None:
        self.reader_id = reader_id
        self.samples: deque[WindowSample] = deque()
        self._times: list[datetime] = []
        self._maxlen = int(max_samples)
        self._maxage = float(max_age_s)
        self.observed_at: datetime | None = None
        self.received_at: datetime | None = None
        self.held_since: datetime | None = None
        self.last_confirmed_at: datetime | None = None
        self.baseline_w: float | None = None
        self.sigma_w: float | None = None
        self.resolution_w: float | None = None
        self.frozen_until: datetime | None = None
        self.frozen: bool = False
        self.last_sequence: int = -1
        self._last_baseline_at: datetime | None = None
        self._last_power: float | None = None
        self._ewma_tau: float = DEFAULT_BASELINE_TAU_S
        self._sigma_min_w: float = DEFAULT_SIGMA_MIN_W
        self._innovations: deque[tuple[datetime, float]] = deque()
        # Sorted mirror of the innovation values, kept in sync incrementally.
        # MAD needs the median of the values and of their absolute deviations;
        # sorting on every (64-sample) recompute was the dominant p99 fast-path
        # cost.  Keeping the values sorted makes the first median O(1) and the
        # second ~4x cheaper, while an insert/remove is a ~1 us C-level shift.
        self._sorted_innovations: list[float] = []
        # Incremental cadence: ``resolve_channel_cadence`` is the median of the
        # ring's positive received-time gaps.  Maintaining those gaps in a
        # sorted sketch gives the *exact* same median in O(log n) per sample
        # (O(n) only on a rare structural disruption), instead of re-sorting the
        # whole ring on every ``cadence()`` call.  P3 asks every sample, so this
        # is the difference between O(1) and O(ring) on the fast path.
        self._intervals: list[float] = []  # sorted positive received-time gaps
        self._interval_median: float | None = None
        self._cadence_default: float = clamp_cadence(DEFAULT_CADENCE_S)

    # -- insertion --------------------------------------------------------- #
    def add(self, reading: ReaderMeasurement, *, now: datetime | None) -> AddResult:
        at = _aware(reading.observed_at)
        flags = set(reading.quality_flags)
        accepted = not any(f in _NON_EVIDENCE_FLAGS for f in flags)
        sample = WindowSample(
            at,
            _aware(reading.received_at),
            reading.active_power_w,
            tuple(reading.quality_flags),
            reading.sequence,
        )

        if self._times:
            if at < self._times[-1]:
                # Late: insert in observed_at order, fire the callback once.
                index = bisect.bisect_left(self._times, at)
                while index < len(self._times) and self._times[index] == at:
                    if reading.sequence > self.samples[index].sequence:
                        self.samples[index] = sample
                        self._reindex_cadence()
                        self._reindex_noise()
                        self.last_sequence = max(self.last_sequence, reading.sequence)
                        return AddResult(True, self.reader_id, at, accepted)
                    return AddResult(True, self.reader_id, at, False)
                self.samples.insert(index, sample)
                self._times.insert(index, at)
                self._trim()
                self._reindex_cadence()
                self._reindex_noise()
                self.last_sequence = max(self.last_sequence, reading.sequence)
                return AddResult(True, self.reader_id, at, accepted)
            if at == self._times[-1]:
                # Same timestamp: a higher sequence is a re-read/replay that
                # supersedes; anything else is a duplicate.
                if reading.sequence <= self.samples[-1].sequence:
                    return AddResult(False, self.reader_id, at, False)
                self.samples[-1] = sample
                self._reindex_cadence()
                self.last_sequence = max(self.last_sequence, reading.sequence)
                self._advance(at, reading, now)
                return AddResult(False, self.reader_id, at, accepted)

        # Hot path: in-order append.  Update the sorted interval sketch with the
        # one new gap and any gap dropped by trimming the front, rather than
        # re-deriving the whole median (O(log n) instead of O(n) per sample).
        prev = self.samples[-1] if self.samples else None
        self.samples.append(sample)
        self._times.append(at)
        if prev is not None:
            self._add_gap(prev, sample)
        while len(self.samples) > self._maxlen:
            self._remove_gap(self.samples[0], self.samples[1])
            self.samples.popleft()
            self._times.pop(0)
        self._refresh_interval_median()
        self.last_sequence = max(self.last_sequence, reading.sequence)
        self._advance(at, reading, now)
        return AddResult(False, self.reader_id, at, accepted)

    def _trim(self) -> None:
        while len(self.samples) > self._maxlen:
            self.samples.popleft()
            self._times.pop(0)

    # -- incremental cadence (exact median-of-intervals sketch) -------------- #
    @staticmethod
    def _gap(a: WindowSample, b: WindowSample) -> float | None:
        """The positive received-time gap ``resolve_channel_cadence`` would count."""
        if b.received_at < a.received_at:
            return None
        seconds = (b.received_at - a.received_at).total_seconds()
        return seconds if seconds > 0 else None

    def _add_gap(self, a: WindowSample, b: WindowSample) -> None:
        gap = self._gap(a, b)
        if gap is not None:
            bisect.insort(self._intervals, gap)

    def _remove_gap(self, a: WindowSample, b: WindowSample) -> None:
        gap = self._gap(a, b)
        if gap is None:
            return
        index = bisect.bisect_left(self._intervals, gap)
        if index < len(self._intervals) and self._intervals[index] == gap:
            self._intervals.pop(index)

    def _refresh_interval_median(self) -> None:
        data = self._intervals
        if not data:
            self._interval_median = None
            return
        mid = len(data) // 2
        self._interval_median = (
            data[mid] if len(data) % 2 else (data[mid - 1] + data[mid]) / 2.0
        )

    def _reindex_cadence(self) -> None:
        """Rebuild the interval sketch from the ring (rare structural change)."""
        self._intervals = []
        for a, b in zip(self.samples, islice(self.samples, 1, None), strict=False):
            gap = self._gap(a, b)
            if gap is not None:
                self._intervals.append(gap)
        self._intervals.sort()
        self._refresh_interval_median()

    def _advance(self, at: datetime, reading: ReaderMeasurement, now: datetime | None) -> None:
        """Update hold, baseline and noise for a new (in-order) sample."""
        flags = set(reading.quality_flags)
        repeated = QualityFlag.REPEATED in flags
        contradicted = bool(flags & _NON_EVIDENCE_FLAGS)
        power = reading.active_power_w

        # A repeated value (or a contradicted one) is not new evidence: it
        # refreshes last_confirmed_at only, and never starts a hold on its own.
        if repeated or contradicted:
            instant = _aware(now) if now is not None else at
            if self.last_confirmed_at is None or instant > self.last_confirmed_at:
                self.last_confirmed_at = instant
            return

        if self.held_since is None:
            self.held_since = at
        self.observed_at = at
        self.last_confirmed_at = at
        self.received_at = _aware(reading.received_at)

        # σ always has an absolute floor once the reader has any usable sample,
        # even if this particular reading has no active power (e.g. energy-only
        # or repeated).  An estimate of "at least 1 W" is not a fabricated 0.
        if self.sigma_w is None:
            self.sigma_w = self._sigma_min_w

        if power is not None and math.isfinite(power):
            self._update_resolution_and_noise(at, power)
            self._last_power = power
            if QualityFlag.PRIMED in flags or self.baseline_w is None:
                # `_prime` replays seed the baseline, never a step.
                self.baseline_w = power
            elif not self.frozen:
                last = self._last_baseline_at or at
                age = max(0.0, (at - last).total_seconds())
                alpha = -math.expm1(-age / self._ewma_tau) if age > 0 else 0.0
                self.baseline_w += alpha * (power - self.baseline_w)
            self._last_baseline_at = at

    def _update_resolution_and_noise(self, at: datetime, power: float) -> None:
        # ``_last_power`` is the previous *value*, not the previous innovation:
        # differencing two innovations would cancel around zero and report a
        # σ near zero.  Keep the last value explicitly (O(1)).
        previous = self._last_power
        if previous is not None:
            diff = power - previous
            # No fabricated zero for the first sample: an innovation is only
            # defined once there are two readings to differ (HB §18).
            self._innovations.append((at, diff))
            bisect.insort(self._sorted_innovations, diff)
            resolution = abs(diff)
            if resolution > 0.0 and (self.resolution_w is None or resolution < self.resolution_w):
                self.resolution_w = resolution
        cutoff = at - DEFAULT_NOISE_WINDOW
        while self._innovations and self._innovations[0][0] < cutoff:
            _, expired = self._innovations.popleft()
            index = bisect.bisect_left(self._sorted_innovations, expired)
            if index < len(self._sorted_innovations):
                self._sorted_innovations.pop(index)
        if self.sigma_w is None or len(self._innovations) % _NOISE_RECOMPUTE_EVERY == 0:
            self._recompute_sigma()

    def _reindex_noise(self) -> None:
        """Rebuild innovations after a late insert perturbs the sequence."""
        self._innovations.clear()
        self._sorted_innovations.clear()
        previous: float | None = None
        for sample in self.samples:
            value = sample.active_power_w
            if value is None or not math.isfinite(value) or _has_bad_flags(sample.quality_flags):
                continue
            if previous is not None:
                diff = value - previous
                self._innovations.append((sample.observed_at, diff))
                bisect.insort(self._sorted_innovations, diff)
            previous = value
        self._last_power = previous
        self._recompute_sigma()

    def _recompute_sigma(self) -> None:
        # MAD of signed successive differences: for independent symmetric
        # noise with level σ, Δ = x[i] - x[i-1] has stdev √2·σ, so the scaled
        # MAD of the innovations is divided by √2 to recover the level noise.
        # Using the values themselves would measure dispersion around the
        # level, not the noise.  ``_sorted_innovations`` is a sorted mirror, so
        # the level median is O(1); this is exactly ``robust_noise_mad`` over
        # the (finite) innovation values, just without re-sorting them.
        data = self._sorted_innovations
        raw: float | None = None
        if len(data) >= DEFAULT_NOISE_MIN_SAMPLES:
            if len(data) <= _NOISE_MAD_MAX_SAMPLES:
                sample = data
            else:
                # Bounded sketch for long histories: an evenly-spaced,
                # deterministic subsample of the (already sorted) innovations.
                # MAD is a robust order statistic, so 256 quantiles estimate it
                # to within a fraction of a percent; re-sorting thousands of
                # values on the fast path was the dominant p99 cost.  Short
                # histories (every detection test, and any reader with <=256
                # innovations) keep the exact value -- no subsampling at all.
                # A C-level strided slice is ~4x faster than a Python-level
                # sampled comprehension (the sorted values are already ordered,
                # so an evenly-strided subset spans the whole distribution).
                sample = data[:: max(1, len(data) // _NOISE_MAD_MAX_SAMPLES)][
                    :_NOISE_MAD_MAX_SAMPLES
                ]
            mid = len(sample) // 2
            med = sample[mid] if len(sample) % 2 else (sample[mid - 1] + sample[mid]) / 2.0
            deviations = sorted(abs(value - med) for value in sample)
            dm = len(deviations) // 2
            mad = deviations[dm] if len(deviations) % 2 else (deviations[dm - 1] + deviations[dm]) / 2.0
            raw = mad * MAD_TO_SIGMA
        mad = raw / math.sqrt(2.0) if raw is not None else None
        floor = self.resolution_w / math.sqrt(12.0) if self.resolution_w is not None else None
        self.sigma_w = max(
            v for v in (self._sigma_min_w, mad, floor) if v is not None
        )

    # -- reads ------------------------------------------------------------- #
    def held(
        self,
        at: datetime,
        *,
        stale_after_s: float,
        missing_after_s: float,
    ) -> HeldReading:
        """The zero-order-held value at ``at`` (K08 handoff)."""
        instant = _aware(at)
        if self.observed_at is None or self.held_since is None:
            return HeldReading(None, instant, instant, "missing")
        last_confirmed = self.last_confirmed_at or self.held_since
        anchor = max(self.held_since, last_confirmed)
        age = max(0.0, (instant - anchor).total_seconds())
        if age > missing_after_s:
            return HeldReading(None, self.held_since, last_confirmed, "missing")
        state: Literal["held", "stale", "missing"] = "held" if age <= stale_after_s else "stale"
        # The value is the one the zero-order hold reports at ``instant``;
        # ``held_since`` is the timestamp of that value's observation, so a
        # point-in-time query never reports a future hold start.
        value, value_at = self._held_value_and_time(instant)
        held_since = value_at if value_at is not None else self.held_since
        return HeldReading(value, held_since, last_confirmed, state)

    def _held_value_and_time(self, at: datetime) -> tuple[float | None, datetime | None]:
        for sample in reversed(self.samples):
            if sample.observed_at > at:
                continue
            value = sample.active_power_w
            if value is None or not math.isfinite(value) or _has_bad_flags(sample.quality_flags):
                continue
            if QualityFlag.REPEATED in sample.quality_flags:
                # A repeated value confirms the current hold; it does not start
                # a new one, so held_since stays at the original observation.
                continue
            return value, sample.observed_at
        return None, None

    def window(self, since: datetime, until: datetime) -> tuple[WindowSample, ...]:
        start = bisect.bisect_left(self._times, _aware(since))
        stop = bisect.bisect_right(self._times, _aware(until))
        return tuple(islice(self.samples, start, stop))

    def cadence(self) -> ResolvedCadence:
        """Resolve this reader's observed cadence from the retained samples.

        This is the exact result of :func:`resolve_channel_cadence` over the
        ring's received times, but computed from an incrementally maintained
        sorted sketch of the inter-arrival gaps: O(1) amortized per sample (the
        sketch is updated as samples are added), instead of re-sorting the whole
        ring on every call.  P3 asks on every sample, so this keeps the fast path
        off the O(ring) path.
        """
        count = len(self._intervals)
        if count < MIN_CADENCE_INTERVALS:
            return ResolvedCadence(
                seconds=self._cadence_default,
                source=CADENCE_SOURCE_DEFAULT,
                intervals=0,
            )
        assert self._interval_median is not None
        return ResolvedCadence(
            seconds=clamp_cadence(self._interval_median or self._cadence_default),
            source=CADENCE_SOURCE_OBSERVED,
            intervals=count,
        )

    def state(self) -> ReaderState:
        return ReaderState(
            reader_id=self.reader_id,
            samples=tuple(self.samples),
            observed_at=self.observed_at,
            received_at=self.received_at,
            held_since=self.held_since,
            last_confirmed_at=self.last_confirmed_at,
            baseline_w=self.baseline_w,
            sigma_w=self.sigma_w,
            resolution_w=self.resolution_w,
            frozen_until=self.frozen_until,
            frozen=self.frozen,
            last_sequence=self.last_sequence,
            last_baseline_at=self._last_baseline_at,
        )

    def prune(self, now: datetime) -> int:
        cutoff = _aware(now) - timedelta(seconds=self._maxage)
        removed = 0
        while self.samples and self.samples[0].observed_at < cutoff:
            self.samples.popleft()
            self._times.pop(0)
            removed += 1
        if removed:
            self._reindex_cadence()
        noise_cutoff = _aware(now) - DEFAULT_NOISE_WINDOW
        while self._innovations and self._innovations[0][0] < noise_cutoff:
            _, expired = self._innovations.popleft()
            index = bisect.bisect_left(self._sorted_innovations, expired)
            if index < len(self._sorted_innovations):
                self._sorted_innovations.pop(index)
        return removed


class ReaderStateStore:
    """One zero-order-hold ring per reader, keyed by ``observed_at``.

    Parameters
    ----------
    stale_after_s, missing_after_s:
        Hold policy (defaults 120 s / 600 s from design §3).
    sigma_min_w:
        Absolute noise floor (1 W); the resolution-derived floor is applied on
        top of it per reader.
    baseline_tau_s:
        EWMA time constant for the baseline (30 min).
    ring_min_s, ring_min_samples:
        The ring covers ``max(ring_min_s, ring_min_samples × cadence)``.
    clock:
        Optional injected ``Callable[[], datetime]`` -- a caller convenience
        for ``prune()`` / persistence, never a hidden dependency.
    """

    def __init__(
        self,
        *,
        stale_after_s: float = DEFAULT_STALE_AFTER_S,
        missing_after_s: float = DEFAULT_MISSING_AFTER_S,
        sigma_min_w: float = DEFAULT_SIGMA_MIN_W,
        baseline_tau_s: float = DEFAULT_BASELINE_TAU_S,
        ring_min_s: float = DEFAULT_RING_MIN_S,
        ring_min_samples: int = DEFAULT_RING_MIN_SAMPLES,
        persist_interval_s: float = DEFAULT_PERSIST_INTERVAL_S,
        clock: Callable[[], datetime] | None = None,
        resolution_w_by_reader: Mapping[str, float] | None = None,
    ) -> None:
        if missing_after_s < stale_after_s:
            raise ValueError("missing_after_s must be >= stale_after_s")
        self.stale_after_s = float(stale_after_s)
        self.missing_after_s = float(missing_after_s)
        self.sigma_min_w = float(sigma_min_w)
        self.baseline_tau_s = float(baseline_tau_s)
        self.ring_min_s = float(ring_min_s)
        self.ring_min_samples = int(ring_min_samples)
        self.persist_interval_s = float(persist_interval_s)
        self._clock = clock
        # Declared quantisation resolution per reader (from the endpoint
        # revision).  ``ReaderMeasurement`` does not carry it, so P6/H1 pass it
        # here; a reader without one falls back to the smallest observed
        # non-zero innovation.
        self._resolution_w_by_reader: Mapping[str, float] = dict(resolution_w_by_reader or {})
        self._rings: dict[str, _ReaderRing] = {}
        self._callbacks: list[Callable[[str, datetime], None]] = []
        self._lock = threading.RLock()
        self._last_persist_at: datetime | None = None

    # -- ingestion --------------------------------------------------------- #
    def _make_ring(self, reader_id: str) -> _ReaderRing:
        seconds = max(1.0, clamp_cadence(DEFAULT_CADENCE_S))
        max_age = max(self.ring_min_s, self.ring_min_samples * seconds)
        ring = _ReaderRing(
            reader_id,
            max_samples=max(self.ring_min_samples, int(max_age / seconds) + 1),
            max_age_s=max_age,
        )
        ring._ewma_tau = self.baseline_tau_s
        ring._sigma_min_w = self.sigma_min_w
        declared_resolution = self._resolution_w_by_reader.get(reader_id)
        if declared_resolution is not None and math.isfinite(declared_resolution):
            ring.resolution_w = float(declared_resolution)
        return ring

    def _sync_ring_config(self, ring: _ReaderRing) -> None:
        ring._ewma_tau = self.baseline_tau_s
        ring._sigma_min_w = self.sigma_min_w

    def add(self, reading: ReaderMeasurement) -> AddResult:
        """Add a normalized reading to its reader's ring.

        Out-of-order samples are inserted in ``observed_at`` order; the
        ``on_late`` callbacks fire exactly once for a genuinely late insertion.
        """
        with self._lock:
            ring = self._rings.get(reading.reader_id)
            if ring is None:
                ring = self._make_ring(reading.reader_id)
                self._rings[reading.reader_id] = ring
            self._sync_ring_config(ring)
            result = ring.add(reading, now=self._clock() if self._clock else None)
        if result.late and result.accepted:
            self._fire_late(reading.reader_id, _aware(reading.observed_at))
        return result

    def on_late(self, callback: Callable[[str, datetime], None]) -> None:
        """Register a late-arrival callback ``(reader_id, observed_at)``."""
        with self._lock:
            self._callbacks.append(callback)

    def _fire_late(self, reader_id: str, at: datetime) -> None:
        with self._lock:
            callbacks = tuple(self._callbacks)
        for callback in callbacks:
            callback(reader_id, at)

    # -- reads ------------------------------------------------------------- #
    def held(self, reader: str, at: datetime) -> HeldReading:
        """The zero-order-held reading for ``reader`` at ``at`` (K08)."""
        with self._lock:
            ring = self._rings.get(reader)
            if ring is None:
                instant = _aware(at)
                return HeldReading(None, instant, instant, "missing")
            return ring.held(
                at,
                stale_after_s=self.stale_after_s,
                missing_after_s=self.missing_after_s,
            )

    def held_all(self, at: datetime) -> dict[str, HeldReading]:
        """Held readings for every reader known to this store."""
        with self._lock:
            readers = tuple(self._rings)
        return {reader: self.held(reader, at) for reader in readers}

    def window(self, reader: str, since: datetime, until: datetime) -> tuple[WindowSample, ...]:
        """Samples in the closed observed-time window ``[since, until]``."""
        with self._lock:
            ring = self._rings.get(reader)
            return () if ring is None else ring.window(since, until)

    def sigma(self, reader: str) -> float | None:
        """Long-run noise σ_r (W), or ``None`` when no estimate exists yet."""
        with self._lock:
            ring = self._rings.get(reader)
            return None if ring is None else ring.sigma_w

    def baseline(self, reader: str) -> float | None:
        """The current (possibly frozen) baseline level (W), or ``None``."""
        with self._lock:
            ring = self._rings.get(reader)
            return None if ring is None else ring.baseline_w

    def reader_state(self, reader: str) -> ReaderState | None:
        """An immutable view of one reader's full state, for diagnostics."""
        with self._lock:
            ring = self._rings.get(reader)
            return None if ring is None else ring.state()

    def sample_at(self, reader: str, at: datetime) -> WindowSample | None:
        """The sample observed exactly at ``at``, or ``None`` (no ring copy).

        This is the O(log ring) equivalent of scanning ``reader_state().samples``
        for a matching ``observed_at``.  P3 calls it several times per sample; a
        full :class:`ReaderState` copy per call made the fast path O(ring).
        """
        instant = _aware(at)
        with self._lock:
            ring = self._rings.get(reader)
            if ring is None or not ring._times:
                return None
            index = bisect.bisect_right(ring._times, instant)
            if index > 0 and ring._times[index - 1] == instant:
                # Last matching sample wins, matching the old reversed scan.
                return ring.samples[index - 1]
            return None

    def resolution(self, reader: str) -> float | None:
        """Declared/observed resolution for ``reader``, without a ring copy."""
        with self._lock:
            ring = self._rings.get(reader)
            return None if ring is None else ring.resolution_w

    def cadence(self, reader: str) -> ResolvedCadence | None:
        """Observed cadence for ``reader``, with its source tag."""
        with self._lock:
            ring = self._rings.get(reader)
            return None if ring is None else ring.cadence()

    # -- baseline control (P3) --------------------------------------------- #
    def freeze_baseline(self, reader: str, until: datetime) -> None:
        """Freeze ``reader``'s baseline until ``until`` (SUSPECT / settling).

        While frozen the EWMA does not move, so a persistent step is not
        absorbed into the baseline.  A time in the past unfreezes immediately.
        """
        with self._lock:
            ring = self._rings.get(reader)
            if ring is None:
                return
            ring.frozen_until = _aware(until)
            ring.frozen = True

    def unfreeze_baseline(self, reader: str) -> None:
        """Release a hold placed by :meth:`freeze_baseline`."""
        with self._lock:
            ring = self._rings.get(reader)
            if ring is not None:
                ring.frozen = False
                ring.frozen_until = None

    def release_expired_freezes(self, at: datetime) -> None:
        """Unfreeze readers whose ``frozen_until`` has passed."""
        instant = _aware(at)
        with self._lock:
            for ring in self._rings.values():
                if ring.frozen and ring.frozen_until is not None and instant >= ring.frozen_until:
                    ring.frozen = False
                    ring.frozen_until = None

    # -- persistence ------------------------------------------------------- #
    def snapshot_state(self) -> bytes:
        """Serialize all hidden state into a deterministic blob (HB §15)."""
        with self._lock:
            payload: dict[str, Any] = {
                "version": _SNAPSHOT_VERSION,
                "config": {
                    "stale_after_s": self.stale_after_s,
                    "missing_after_s": self.missing_after_s,
                    "sigma_min_w": self.sigma_min_w,
                    "baseline_tau_s": self.baseline_tau_s,
                    "ring_min_s": self.ring_min_s,
                    "ring_min_samples": self.ring_min_samples,
                    "persist_interval_s": self.persist_interval_s,
                },
                "last_persist_at": _iso(self._last_persist_at),
                "readers": [_encode_state(ring.state()) for ring in self._rings.values()],
            }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        header = struct.pack(">4sBB", _SNAPSHOT_MAGIC, _SNAPSHOT_VERSION, 0)
        return header + struct.pack(">I", zlib.crc32(raw) & 0xFFFFFFFF) + raw

    def restore_state(self, blob: bytes) -> None:
        """Restore hidden state from a :meth:`snapshot_state` blob.

        Unknown/corrupt blobs are rejected with ``ValueError`` rather than
        silently producing a store full of zeros.
        """
        if len(blob) < 10:
            raise ValueError("reader-state blob is truncated")
        magic, version, flags = struct.unpack(">4sBB", blob[:6])
        if magic != _SNAPSHOT_MAGIC or version != _SNAPSHOT_VERSION or flags != 0:
            raise ValueError("unsupported reader-state blob")
        (crc,) = struct.unpack(">I", blob[6:10])
        raw = blob[10:]
        if zlib.crc32(raw) & 0xFFFFFFFF != crc:
            raise ValueError("reader-state blob failed its checksum")
        payload = json.loads(raw.decode("utf-8"))
        config = payload.get("config", {})
        restored = ReaderStateStore(
            stale_after_s=config.get("stale_after_s", self.stale_after_s),
            missing_after_s=config.get("missing_after_s", self.missing_after_s),
            sigma_min_w=config.get("sigma_min_w", self.sigma_min_w),
            baseline_tau_s=config.get("baseline_tau_s", self.baseline_tau_s),
            ring_min_s=config.get("ring_min_s", self.ring_min_s),
            ring_min_samples=config.get("ring_min_samples", self.ring_min_samples),
            persist_interval_s=config.get("persist_interval_s", self.persist_interval_s),
            resolution_w_by_reader=self._resolution_w_by_reader,
        )
        for encoded in payload.get("readers", ()):
            ring = _decode_state(encoded)
            restored._rings[ring.reader_id] = ring
        restored._last_persist_at = _parse_dt(payload.get("last_persist_at"))
        # Swap all state atomically; callbacks are wiring, not hidden state, so
        # they intentionally survive a restore.
        with self._lock:
            self.stale_after_s = restored.stale_after_s
            self.missing_after_s = restored.missing_after_s
            self.sigma_min_w = restored.sigma_min_w
            self.baseline_tau_s = restored.baseline_tau_s
            self.ring_min_s = restored.ring_min_s
            self.ring_min_samples = restored.ring_min_samples
            self.persist_interval_s = restored.persist_interval_s
            self._rings = restored._rings
            self._last_persist_at = restored._last_persist_at

    def should_persist(self, at: datetime) -> bool:
        """Whether the 10-minute persistence deadline has elapsed at ``at``."""
        with self._lock:
            if self._last_persist_at is None:
                return True
            return (_aware(at) - self._last_persist_at).total_seconds() >= self.persist_interval_s

    def mark_persisted(self, at: datetime) -> None:
        """Record that a snapshot was persisted at ``at``."""
        with self._lock:
            self._last_persist_at = _aware(at)

    # -- maintenance ------------------------------------------------------- #
    def readers(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._rings)

    def prune(self, now: datetime | None = None) -> int:
        """Drop samples older than the ring's age window; return the count."""
        if now is not None:
            instant = _aware(now)
        elif self._clock is not None:
            instant = _aware(self._clock())
        else:
            raise ValueError("prune() requires now or an injected clock")
        with self._lock:
            return sum(ring.prune(instant) for ring in self._rings.values())


def _encode_state(state: ReaderState) -> dict[str, Any]:
    return {
        "reader_id": state.reader_id,
        "samples": [
            {
                "observed_at": _iso(s.observed_at),
                "received_at": _iso(s.received_at),
                "active_power_w": s.active_power_w,
                "flags": _encode_flags(s.quality_flags),
                "sequence": s.sequence,
            }
            for s in state.samples
        ],
        "observed_at": _iso(state.observed_at),
        "received_at": _iso(state.received_at),
        "held_since": _iso(state.held_since),
        "last_confirmed_at": _iso(state.last_confirmed_at),
        "baseline_w": state.baseline_w,
        "sigma_w": state.sigma_w,
        "resolution_w": state.resolution_w,
        "frozen_until": _iso(state.frozen_until),
        "frozen": state.frozen,
        "last_sequence": state.last_sequence,
        "last_baseline_at": _iso(state.last_baseline_at),
    }


def _decode_state(data: dict[str, Any]) -> _ReaderRing:
    ring = _ReaderRing(
        data["reader_id"],
        max_samples=DEFAULT_RING_MIN_SAMPLES,
        max_age_s=DEFAULT_RING_MIN_S,
    )
    for item in data.get("samples", ()):
        ring.samples.append(
            WindowSample(
                observed_at=_parse_dt(item["observed_at"]),  # type: ignore[arg-type]
                received_at=_parse_dt(item["received_at"]),  # type: ignore[arg-type]
                active_power_w=item.get("active_power_w"),
                quality_flags=_decode_flags(item.get("flags", ())),
                sequence=int(item.get("sequence", 0)),
            )
        )
    ring._times = [s.observed_at for s in ring.samples]
    ring._reindex_cadence()
    ring.observed_at = _parse_dt(data.get("observed_at"))
    ring.received_at = _parse_dt(data.get("received_at"))
    ring.held_since = _parse_dt(data.get("held_since"))
    ring.last_confirmed_at = _parse_dt(data.get("last_confirmed_at"))
    ring.baseline_w = data.get("baseline_w")
    ring.sigma_w = data.get("sigma_w")
    ring.resolution_w = data.get("resolution_w")
    ring.frozen_until = _parse_dt(data.get("frozen_until"))
    ring.frozen = bool(data.get("frozen", False))
    ring.last_sequence = int(data.get("last_sequence", -1))
    ring._last_baseline_at = _parse_dt(data.get("last_baseline_at"))
    ring._reindex_noise()
    return ring
