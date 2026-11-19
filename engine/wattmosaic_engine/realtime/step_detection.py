"""Offline CT step-confirmation helper for the P3 streaming detector.

The legacy ``disaggregation/events`` module owned the MAD-based best-split step
scan.  Only its pure confirmation helper is still used by the v3 fast loop
(:mod:`wattmosaic_engine.realtime.detection`); it is preserved verbatim here so
the cutover (Z2) can delete the legacy module without changing detector
behaviour.  The legacy ``StepTracker`` and ``detect_changes`` are gone.
"""
from __future__ import annotations

import dataclasses
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from wattmosaic_engine.electrical.window import DEFAULT_CADENCE_S, ChannelReading, shift

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


def confirm_streaming_step(
    pre: Sequence[tuple[datetime, float]],
    post: Sequence[tuple[datetime, float]],
    *,
    channel_id: str,
    sigma_w: float,
    min_delta_w: float,
    resolution_w: float | None = None,
    k_mad: float = DEFAULT_K_MAD,
) -> StepChange | None:
    """Confirm a streaming SUSPECT with the offline best-split scan.

    P3's streaming detector establishes the pre level and the onset bounds from
    the reader's rolling state.  This helper reuses :func:`detect_step` as an
    independent *confirmation* of the change point and the signed magnitude over
    the retained ``pre``/``post`` samples.  A single pre-sample is enough here
    (``min_before_samples=1``) because the baseline is already established by
    the streaming path; the scan is not the primary detector.

    Returns ``None`` when the window holds no usable samples.  ``sigma_w`` is the
    reader's long-run noise (never a fabricated zero), used as the MAD floor.
    """
    samples = sorted([*pre, *post], key=lambda item: item[0])
    if len(samples) < 2:
        return None
    at = samples[-1][0]
    reading = ChannelReading(
        channel_id=channel_id,
        value_w=samples[-1][1],
        history=tuple(samples[:-1]),
        resolution_w=resolution_w,
    )
    span = max(1.0, (samples[-1][0] - samples[0][0]).total_seconds())
    return detect_step(
        reading,
        at,
        before_s=span,
        after_s=span,
        k_mad=k_mad,
        min_delta_w=min_delta_w,
        noise_floor_w=sigma_w,
        min_before_samples=1,
        min_after_samples=1,
    )


__all__ = ["StepChange", "confirm_streaming_step", "detect_step", "median", "mad_sigma", "MAD_TO_SIGMA", "DEFAULT_K_MAD"]
