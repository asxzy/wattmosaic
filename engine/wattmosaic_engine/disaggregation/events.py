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

Anchored queries
----------------
Every HA state transition carries the real instant it happened (``occurred_at``).
``detect_step``/``detect_changes`` accept that instant as ``anchor``.  When an
anchor is supplied:

* only candidate split times inside
  ``[anchor - anchor_before_s - anchor_grace_s,
     anchor + anchor_after_s + anchor_grace_s]`` are considered; if none
  survives the result is ``None`` -- there is deliberately *no* fallback to the
  global scan, because the global scan is what allowed a lamp 47 s away to claim
  a step;
* candidates are ranked by
  ``|delta| / threshold * exp(-|t - anchor| / cadence)`` so the nearest
  plausible step beats a distant larger one.

``anchor=None`` (the default) takes the unanchored path, which is unchanged.

Baseline sample count
---------------------
A two-point baseline cannot distinguish a quiet channel from a noisy one: two
identical readings give ``MAD = 0`` and therefore the minimum threshold, so any
resolution tick looks like an event.  ``min_before_samples`` is 4, and when the
cadence-derived window holds fewer points the baseline reaches further back into
the buffered history until it has enough (or runs out).
"""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from wattmosaic_engine.electrical.window import (
    DEFAULT_CADENCE_S,
    ChannelReading,
    ObservationWindow,
    shift,
)

MAD_TO_SIGMA = 1.4826

DEFAULT_K_MAD = 4.0
DEFAULT_MIN_DELTA_W = 5.0
DEFAULT_NOISE_FLOOR_W = 1.0
#: A baseline needs at least this many pre-samples.  With two samples a real
#: channel with zero MAD is indistinguishable from a stale duplicate, and the
#: threshold collapses to ``min_delta_w``.
DEFAULT_MIN_BEFORE_SAMPLES = 4


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


def _series(
    reading: ChannelReading, at: datetime, *, multiplier: float = 1.0
) -> list[tuple[datetime, float]]:
    samples = [(t, float(v) * multiplier) for t, v in reading.history if t <= at]
    samples.sort(key=lambda s: s[0])
    if reading.value_w is not None:
        value = reading.signed_w()
        if value is not None:
            value *= multiplier
            if samples and abs((samples[-1][0] - at).total_seconds()) < 1e-6:
                samples[-1] = (at, value)
            elif not samples or samples[-1][0] < at:
                samples.append((at, value))
    return samples


def _validate_seconds(name: str, value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number of seconds, got {value!r}")
    seconds = float(value)
    if not math.isfinite(seconds):
        raise ValueError(f"{name} must be finite, got {value!r}")
    if seconds < 0:
        raise ValueError(f"{name} must be non-negative, got {value!r}")
    return seconds


def _anchor_bounds(
    anchor: datetime | None,
    at: datetime,
    anchor_before_s: float,
    anchor_after_s: float,
    anchor_grace_s: float,
) -> tuple[datetime, datetime] | None:
    """Validate the anchor arguments and return the allowed split-time range.

    Returns ``None`` for the unanchored path.  Raises :class:`ValueError` for a
    negative/NaN window or for mixing naive and aware datetimes -- a silent
    ``TypeError`` from a datetime comparison deep inside the scan is not
    actionable for a caller wiring HA ``occurred_at`` values.  A *consistently*
    naive pair is normalised to aware UTC, matching the rest of the engine.
    """
    before = _validate_seconds("anchor_before_s", anchor_before_s)
    after = _validate_seconds("anchor_after_s", anchor_after_s)
    grace = _validate_seconds("anchor_grace_s", anchor_grace_s)
    if anchor is None:
        return None
    if not isinstance(anchor, datetime):
        raise ValueError(f"anchor must be a datetime, got {type(anchor).__name__}")
    if (anchor.tzinfo is None) != (at.tzinfo is None):
        raise ValueError(
            "anchor and at must both be timezone-aware or both naive; got "
            f"anchor.tzinfo={anchor.tzinfo!r}, at.tzinfo={at.tzinfo!r}"
        )
    anchor_utc = _to_utc(anchor)
    return shift(anchor_utc, -(before + grace)), shift(anchor_utc, after + grace)


def _to_utc(value: datetime) -> datetime:
    """Naive -> aware UTC; aware -> same instant in UTC (engine convention)."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _series_cadence_s(samples: Sequence[tuple[datetime, float]]) -> float | None:
    """Median interval between adjacent samples, or ``None`` when undefined."""
    diffs = sorted(
        (b[0] - a[0]).total_seconds()
        for a, b in zip(samples, samples[1:], strict=False)
        if (b[0] - a[0]).total_seconds() > 0
    )
    if not diffs:
        return None
    return diffs[len(diffs) // 2]


def _baseline(
    samples: Sequence[tuple[datetime, float]],
    change_time: datetime,
    before_s: float,
    cadence_s: float,
    min_samples: int,
) -> list[float]:
    """Pre-change values, reaching further back when the window is sparse.

    A ``2 x cadence`` window holds only two or three samples on a 15.7 s channel,
    but a robust MAD baseline needs four.  Rather than reject every candidate
    (which would silence detection entirely on slow channels), the baseline is
    widened to ``min_samples x cadence`` -- still bounded and still entirely
    *before* the candidate, so post-change samples can never leak in.
    """
    span = before_s
    pre = [v for t, v in samples if change_time - _td(span) <= t < change_time]
    if len(pre) < min_samples and min_samples > 2:
        # Only widen for the stricter default requirement.  A caller that
        # explicitly asks for the old 2-sample baseline gets the old window.
        span = max(span, cadence_s * min_samples)
        pre = [v for t, v in samples if change_time - _td(span) <= t < change_time]
    return pre


def detect_step(
    reading: ChannelReading,
    at: datetime,
    *,
    before_s: float,
    after_s: float,
    k_mad: float = DEFAULT_K_MAD,
    min_delta_w: float = DEFAULT_MIN_DELTA_W,
    noise_floor_w: float | None = None,
    min_before_samples: int = DEFAULT_MIN_BEFORE_SAMPLES,
    min_after_samples: int = 1,
    multiplier: float = 1.0,
    anchor: datetime | None = None,
    anchor_before_s: float = 0.0,
    anchor_after_s: float = 0.0,
    anchor_grace_s: float = 0.0,
    cadence_s: float | None = None,
) -> StepChange | None:
    """Detect the dominant step in one channel's recent history.

    ``multiplier`` is the channel's declared circuit multiplier.  It scales the
    whole series -- history, baseline and threshold alike -- so a ``2.0``
    channel reports the *load* step rather than the single-leg step.  Scaling the
    threshold too is deliberate: a doubled leg has doubled noise, so the same
    significance rule applies.

    ``noise_floor_w`` is the per-channel noise floor the quality layer measured
    (MAD sigma).  ``None`` keeps the historical 1 W floor.  It only ever *raises*
    the threshold together with ``k_mad``; ``min_delta_w`` and the channel
    resolution remain hard floors.

    ``anchor`` is the real instant of the HA state transition this call is
    explaining.  When supplied, candidate split times are confined to
    ``[anchor - anchor_before_s - anchor_grace_s,
       anchor + anchor_after_s + anchor_grace_s]`` and ranked by proximity
    (see the module docstring).  ``None`` preserves the unanchored scan.
    """
    before_s = _validate_seconds("before_s", before_s)
    after_s = _validate_seconds("after_s", after_s)
    bounds = _anchor_bounds(anchor, at, anchor_before_s, anchor_after_s, anchor_grace_s)
    anchor_utc = None if anchor is None else _to_utc(anchor)
    # Anchored calls normalise every timestamp (history, ``at`` and anchor) to
    # aware UTC, so a consistently-naive caller works and a mixed one is
    # rejected above instead of raising a bare TypeError mid-scan.
    if anchor_utc is None:
        at_used = at
        samples = _series(reading, at_used, multiplier=multiplier)
    else:
        at_used = _to_utc(at)
        normalised = dataclasses.replace(reading, history=tuple((_to_utc(t), v) for t, v in reading.history))
        samples = _series(normalised, at_used, multiplier=multiplier)
    if len(samples) < 2:
        return None
    if cadence_s is not None:
        cadence = _validate_seconds("cadence_s", cadence_s)
        if cadence <= 0:
            raise ValueError(f"cadence_s must be positive, got {cadence_s!r}")
    else:
        cadence = _series_cadence_s(samples) or DEFAULT_CADENCE_S
    resolution = float(reading.resolution_w or 0.0) * multiplier
    noise_floor = DEFAULT_NOISE_FLOOR_W if noise_floor_w is None else float(noise_floor_w)
    if not math.isfinite(noise_floor) or noise_floor < 0:
        raise ValueError(f"noise_floor_w must be finite and non-negative, got {noise_floor_w!r}")
    best: StepChange | None = None
    best_score = -1.0
    for change_time, _ in samples:
        if bounds is None:
            # Unanchored: ignore candidates so old that the post-change window
            # would not fit inside the observation horizon.
            if change_time < shift(at_used, -(before_s + after_s)):
                continue
        elif not (bounds[0] <= change_time <= bounds[1]):
            # Anchored: the anchor window is the only restriction.  A transition
            # reported a cycle or two late is still a valid candidate, and its
            # post-change window may legitimately predate ``at``.
            continue
        pre = _baseline(samples, change_time, before_s, cadence, min_before_samples)
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
            k_mad * max(noise, noise_floor),
            resolution,
        )
        delta = after - base
        magnitude = abs(delta) / threshold if threshold > 0 else float("inf")
        if anchor_utc is None:
            score = magnitude
        else:
            # Nearest plausible step wins: a 900 W lamp 47 s away must not claim
            # a 40 W step that actually happened 3 s after the transition.
            score = magnitude * math.exp(-abs((change_time - anchor_utc).total_seconds()) / cadence)
        if score > best_score:
            improved = True
        elif math.isclose(score, best_score) and best is not None:
            if anchor_utc is None:
                improved = change_time > best.change_time
            else:
                improved = abs((change_time - anchor_utc).total_seconds()) < abs(
                    (best.change_time - anchor_utc).total_seconds()
                )
        else:
            improved = False
        if improved:
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
    noise_floor_w: float | Mapping[str, float] | None = None,
    previous_values: dict[str, float] | None = None,
    min_before_samples: int = DEFAULT_MIN_BEFORE_SAMPLES,
    min_after_samples: int = 1,
    multipliers: Mapping[str, float] | None = None,
    anchor: datetime | None = None,
    anchor_before_s: float = 0.0,
    anchor_after_s: float = 0.0,
    anchor_grace_s: float = 0.0,
    cadence_s: float | None = None,
) -> dict[str, StepChange]:
    """Detect steps for every channel with timing information.

    When a channel has no history, ``previous_values`` (e.g. the snapshot from
    the previous inference cycle) provides a two-point fallback.  Such changes
    are marked ``source="previous_snapshot"`` and are only trusted when they
    exceed the threshold.

    ``multipliers`` applies each channel's declared circuit multiplier, so a
    step on a doubled leg is reported as a step in the *load*.  It must be the
    same map the rest of the pipeline uses; passing none means "no scaling",
    which is the correct behaviour for an unscaled topology.

    ``noise_floor_w`` may be a scalar floor (old behaviour) or a per-channel
    mapping from the quality layer's measured MAD.  ``None`` keeps the default
    1 W floor.

    ``anchor`` is the HA transition instant.  It is passed to every channel, so
    candidate split times are confined to the anchor window instead of the whole
    buffered history.  There is deliberately no fallback -- neither the global
    scan nor the ``previous_snapshot`` two-point delta, which is timestamped at
    ``at`` and therefore says nothing about when the step happened.  A channel
    with no candidate split inside the window is omitted from the result.
    """
    previous_values = previous_values or {}
    multipliers = multipliers or {}
    per_channel_floor = noise_floor_w if isinstance(noise_floor_w, Mapping) else None
    scalar_floor = None if per_channel_floor is not None else noise_floor_w
    anchored = _anchor_bounds(anchor, at, anchor_before_s, anchor_after_s, anchor_grace_s) is not None
    out: dict[str, StepChange] = {}
    for channel_id, reading in readings.items():
        multiplier = float(multipliers.get(channel_id, 1.0) or 1.0)
        if per_channel_floor is not None:
            floor = per_channel_floor.get(channel_id)
            floor = DEFAULT_NOISE_FLOOR_W if floor is None else float(floor)
        elif scalar_floor is None:
            floor = DEFAULT_NOISE_FLOOR_W
        else:
            floor = float(scalar_floor)
        step = detect_step(
            reading,
            at,
            before_s=before_s,
            after_s=after_s,
            k_mad=k_mad,
            min_delta_w=min_delta_w,
            noise_floor_w=floor,
            min_before_samples=min_before_samples,
            min_after_samples=min_after_samples,
            multiplier=multiplier,
            anchor=anchor,
            anchor_before_s=anchor_before_s,
            anchor_after_s=anchor_after_s,
            anchor_grace_s=anchor_grace_s,
            cadence_s=cadence_s,
        )
        if step is not None:
            out[channel_id] = step
            continue
        if anchored:
            # No split near the transition: report nothing for this channel.
            continue
        prev = previous_values.get(channel_id)
        current = reading.signed_w()
        if prev is not None and current is not None:
            current *= multiplier
            prev = float(prev) * multiplier
            threshold = max(min_delta_w, float(reading.resolution_w or 0.0) * multiplier)
            delta = current - prev
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

    The tracker records one ``(timestamp, watts)`` point per channel per *change*
    and replays it as :attr:`ChannelReading.history`, so the same MAD detector
    works against the real synchroniser.

    Duplicate suppression
    ---------------------
    The engine runs every ~5 s but CTs only publish a new state every ~15.7 s,
    so per-cycle recording produced a staircase of identical readings.  Only
    exactly-equal timestamps collapsed, leaving intervals of 5 s where the CT
    cadence was 15.7 s -- and :meth:`cadence_s` then derived 10 s/5 s
    before/after windows instead of 31 s/15 s, which is too short to hold a
    baseline.

    When the new value equals the last stored value the tracker therefore
    refreshes the last point's timestamp and does **not** append.  A point's
    timestamp is consequently "the last cycle at which this value was still
    current", and :meth:`cadence_s` measures the median interval between
    *changes* -- i.e. the CT's real update cadence -- which is exactly what the
    detector's window length is supposed to track.  ``max_samples`` bounds
    distinct values, not cycles, so the retained horizon is longer than before.

    It remains deterministic: the same call sequence produces the same history,
    and past points are only ever replaced by their own later duplicate.

    Interaction with ``min_before_samples``
    ---------------------------------------
    A perfectly flat channel now holds a single point, so the history-based
    detector cannot form a four-sample baseline for it.  That is deliberate: two
    identical samples give ``MAD = 0`` and would make every resolution tick look
    like an event.  Such a channel is still measured, by the
    ``previous_snapshot`` two-point delta (``source="previous_snapshot"``), which
    keeps ``min_delta_w``/resolution as its floor.
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
            if buf:
                last_at, last_value = buf[-1]
                if last_at > at:
                    # Out-of-order cycle: never rewrite history around a clock
                    # glitch, and never append a point that would sort wrongly.
                    continue
                if last_at == at:
                    # Same instant: replace rather than duplicate.
                    buf[-1] = (at, value)
                    self._last[channel_id] = value
                    continue
                if value == last_value:
                    # Same reading, new cycle: move the point forward in time
                    # instead of appending a duplicate staircase step.
                    buf[-1] = (at, value)
                    self._last[channel_id] = value
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
