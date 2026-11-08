"""Robust CT step-change detection.

Why not a fixed 2 s window
--------------------------
The live audit (``data/ct_update_profile.json``) measured a median CT
state-update interval of **15.736 s**; only one channel updated faster than 1 s
and five were slower than 30 s.  A hardcoded 2 s before/after window would see
at most one sample and would mistake batched writes for real events.  Windows
are therefore derived per call from the *observed* cadence:

``before = clamp(2 * cadence, 5 s, 60 s)``
``after  = clamp(1 * cadence, 2.5 s, 30 s)``

Detection statistic
-------------------
For a candidate change time ``t``:

* ``baseline = median(values in [t - before, t))``  (robust, outlier resistant)
* ``after    = median(values in [t, t + after])``
* ``delta    = after - baseline``
* ``noise    = 1.4826 * median(|v - baseline|)``  (MAD -> sigma for Gaussian)
* ``threshold = max(min_delta_w, k_mad * max(noise, noise_floor), resolution)``

A change is *detected* when ``|delta| >= threshold``.  ``1.4826`` is the
consistency constant that makes the MAD an unbiased estimator of the standard
deviation for normally distributed noise.  ``k_mad`` defaults to 4 (a 4-sigma
step), configurable.

The best split (largest ``|delta| / threshold``, latest on ties) is returned.
If nothing crosses the threshold the best split is still returned with
``detected=False`` so the caller can use its delta as a signed measurement
without pretending an event happened.
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from wattmosaic_engine.electrical.window import ChannelReading, ObservationWindow, shift

MAD_TO_SIGMA = 1.4826

DEFAULT_K_MAD = 4.0
DEFAULT_MIN_DELTA_W = 5.0
DEFAULT_NOISE_FLOOR_W = 1.0


@dataclass(frozen=True, slots=True)
class StepChange:
    channel_id: str
    change_time: datetime
    before_w: float
    after_w: float
    delta_w: float
    noise_w: float
    threshold_w: float
    detected: bool
    before_samples: int
    after_samples: int
    source: str = "history"

    @property
    def direction(self) -> str:
        return "increase" if self.delta_w > 0 else "decrease"

    @property
    def z_score(self) -> float:
        return self.delta_w / self.threshold_w if self.threshold_w > 0 else float("inf")

    def as_dict(self) -> dict[str, object]:
        return {
            "channel_id": self.channel_id,
            "change_time": self.change_time.isoformat(),
            "before_w": self.before_w,
            "after_w": self.after_w,
            "delta_w": self.delta_w,
            "noise_w": self.noise_w,
            "threshold_w": self.threshold_w,
            "detected": self.detected,
            "before_samples": self.before_samples,
            "after_samples": self.after_samples,
            "source": self.source,
        }


def median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    if n == 0:
        return 0.0
    mid = n // 2
    if n % 2:
        return float(ordered[mid])
    return float((ordered[mid - 1] + ordered[mid]) / 2.0)


def mad_sigma(values: Sequence[float], centre: float | None = None) -> float:
    if not values:
        return 0.0
    c = median(values) if centre is None else centre
    return MAD_TO_SIGMA * median([abs(v - c) for v in values])


def _series(reading: ChannelReading, at: datetime) -> list[tuple[datetime, float]]:
    samples = [(t, float(v)) for t, v in reading.history if t <= at]
    samples.sort(key=lambda s: s[0])
    if reading.value_w is not None:
        value = reading.signed_w()
        if value is not None:
            if samples and abs((samples[-1][0] - at).total_seconds()) < 1e-6:
                samples[-1] = (at, value)
            elif not samples or samples[-1][0] < at:
                samples.append((at, value))
    return samples


def detect_step(
    reading: ChannelReading,
    at: datetime,
    *,
    before_s: float,
    after_s: float,
    k_mad: float = DEFAULT_K_MAD,
    min_delta_w: float = DEFAULT_MIN_DELTA_W,
    noise_floor_w: float = DEFAULT_NOISE_FLOOR_W,
    min_before_samples: int = 2,
    min_after_samples: int = 1,
) -> StepChange | None:
    """Detect the dominant step in one channel's recent history."""
    samples = _series(reading, at)
    if len(samples) < 2:
        return None
    resolution = float(reading.resolution_w or 0.0)
    best: StepChange | None = None
    best_score = -1.0
    for change_time, _ in samples:
        if change_time < shift(at, -(before_s + after_s)):
            continue
        pre = [v for t, v in samples if change_time - _td(before_s) <= t < change_time]
        post = [v for t, v in samples if change_time <= t <= change_time + _td(after_s)]
        # A single pre-sample median is not a robust baseline: it cannot
        # distinguish a step from noise.  Require a minimum sample count.
        if len(pre) < min_before_samples or len(post) < min_after_samples:
            continue
        base = median(pre)
        after = median(post)
        noise = mad_sigma(pre, base)
        threshold = max(
            min_delta_w,
            k_mad * max(noise, noise_floor_w),
            resolution,
        )
        delta = after - base
        score = abs(delta) / threshold if threshold > 0 else float("inf")
        if score > best_score or (
            math.isclose(score, best_score) and best is not None and change_time > best.change_time
        ):
            best_score = score
            best = StepChange(
                channel_id=reading.channel_id,
                change_time=change_time,
                before_w=base,
                after_w=after,
                delta_w=delta,
                noise_w=noise,
                threshold_w=threshold,
                detected=abs(delta) >= threshold,
                before_samples=len(pre),
                after_samples=len(post),
                source="history",
            )
    return best


def _td(seconds: float):
    from datetime import timedelta

    return timedelta(seconds=seconds)


def detect_changes(
    readings: dict[str, ChannelReading],
    at: datetime,
    *,
    before_s: float,
    after_s: float,
    k_mad: float = DEFAULT_K_MAD,
    min_delta_w: float = DEFAULT_MIN_DELTA_W,
    noise_floor_w: float = DEFAULT_NOISE_FLOOR_W,
    previous_values: dict[str, float] | None = None,
    min_before_samples: int = 2,
    min_after_samples: int = 1,
) -> dict[str, StepChange]:
    """Detect steps for every channel with timing information.

    When a channel has no history, ``previous_values`` (e.g. the snapshot from
    the previous inference cycle) provides a two-point fallback.  Such changes
    are marked ``source="previous_snapshot"`` and are only trusted when they
    exceed the threshold.
    """
    previous_values = previous_values or {}
    out: dict[str, StepChange] = {}
    for channel_id, reading in readings.items():
        step = detect_step(
            reading,
            at,
            before_s=before_s,
            after_s=after_s,
            k_mad=k_mad,
            min_delta_w=min_delta_w,
            noise_floor_w=noise_floor_w,
            min_before_samples=min_before_samples,
            min_after_samples=min_after_samples,
        )
        if step is not None:
            out[channel_id] = step
            continue
        prev = previous_values.get(channel_id)
        current = reading.signed_w()
        if prev is not None and current is not None:
            threshold = max(min_delta_w, float(reading.resolution_w or 0.0))
            delta = current - float(prev)
            out[channel_id] = StepChange(
                channel_id=channel_id,
                change_time=at,
                before_w=float(prev),
                after_w=current,
                delta_w=delta,
                noise_w=0.0,
                threshold_w=threshold,
                detected=abs(delta) >= threshold,
                before_samples=1,
                after_samples=1,
                source="previous_snapshot",
            )
    return out


@dataclass(slots=True)
class StepTracker:
    """Bounded per-channel observation buffer for event detection.

    The production ``synchronization.AlignedWindow`` deliberately exposes only a
    point-in-time snapshot: no per-channel sample history, no cadence and no
    previous snapshot.  Without a tracker, step detection would therefore never
    run outside tests.

    The tracker records one ``(timestamp, watts)`` point per channel per
    inference window and replays it as :attr:`ChannelReading.history`, so the
    same MAD detector works against the real synchroniser.

    It is *append-only and bounded*, and it never rewrites a recorded
    observation, so a given call sequence deterministically produces the same
    history and therefore the same inference.  Repeated (unchanged) samples are
    recorded once per window, not once per state update, because the engine is
    called per inference cycle.
    """

    max_samples: int = 64
    _history: dict[str, list[tuple[datetime, float]]] = field(default_factory=dict, init=False)
    _last: dict[str, float] = field(default_factory=dict, init=False)

    def observe(self, readings: Mapping[str, ChannelReading], at: datetime) -> None:
        for channel_id, reading in readings.items():
            value = reading.signed_w()
            if value is None:
                continue
            buf = self._history.setdefault(channel_id, [])
            if buf and buf[-1][0] >= at:
                # Same instant: replace rather than duplicate.
                if buf[-1][0] == at:
                    buf[-1] = (at, value)
                continue
            buf.append((at, value))
            if len(buf) > self.max_samples:
                del buf[: len(buf) - self.max_samples]
            self._last[channel_id] = value

    def history(self, channel_id: str) -> tuple[tuple[datetime, float], ...]:
        return tuple(self._history.get(channel_id, ()))

    def previous_value(self, channel_id: str) -> float | None:
        return self._last.get(channel_id)

    def previous_values(self) -> dict[str, float]:
        return dict(self._last)

    def cadence_s(self) -> float | None:
        medians: list[float] = []
        for buf in self._history.values():
            if len(buf) < 2:
                continue
            diffs = sorted(
                (b[0] - a[0]).total_seconds()
                for a, b in zip(buf, buf[1:], strict=False)
                if (b[0] - a[0]).total_seconds() > 0
            )
            if diffs:
                medians.append(diffs[len(diffs) // 2])
        if not medians:
            return None
        medians.sort()
        return medians[len(medians) // 2]

    def enrich(self, window: object) -> object:
        """Return ``window`` with tracker history/cadence filled in where absent.

        Accepts either an :class:`ObservationWindow` (returned enriched) or a
        foreign window (returned unchanged; the engine coerces it and applies
        the tracker separately).
        """
        if not isinstance(window, ObservationWindow):
            return window
        readings = window.readings
        enriched: dict[str, ChannelReading] = {}
        changed = False
        for channel_id, reading in readings.items():
            if not isinstance(reading, ChannelReading):
                enriched[channel_id] = reading
                continue
            if reading.history:
                enriched[channel_id] = reading
                continue
            hist = self.history(channel_id)
            if hist:
                enriched[channel_id] = dataclasses.replace(reading, history=hist)
                changed = True
            else:
                enriched[channel_id] = reading
        if not changed and window.cadence_s is not None:
            return window
        return dataclasses.replace(
            window,
            readings=enriched,
            cadence_s=window.cadence_s or self.cadence_s(),
        )
