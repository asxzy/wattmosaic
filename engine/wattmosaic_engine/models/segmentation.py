"""Change-point segmentation for appliance power series.

Used by :class:`~wattmosaic_engine.models.time_varying.TimeVaryingApplianceModel`
and re-exported through ``learning.stages`` for operating-stage recognition.

Algorithm (deliberately simple, explainable and deterministic):

1. Sort the series by time; drop non-finite watts.
2. Compute a robust noise scale (1.4826 · MAD of first differences / √2).
3. Slide a centred window and mark a *change point* at ``i`` when the robust
   level difference between the ``before`` and ``after`` windows exceeds both
   ``relative_threshold × max(level)`` and ``k × noise``.  Requiring both the
   relative and the noise criterion suppresses noise-driven splits while still
   detecting modest transitions in quiet loads.
4. Merge adjacent segments whose medians differ by less than
   ``merge_threshold_w`` (and whose gap is not significant), then drop segments
   shorter than ``min_segment_s``.

The result is a list of :class:`Segment` objects with start/end time, median
power and duration.  Nothing here assumes a sampling rate; timestamps govern.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from .protocols import normalise_series


@dataclass(slots=True)
class Segment:
    start: datetime
    end: datetime
    power_w: float
    duration_s: float
    sample_count: int = 0
    index: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def energy_kwh(self) -> float:
        """Trapezoidal-free rectangular estimate (segment is ~constant power)."""
        return max(0.0, self.power_w) * max(0.0, self.duration_s) / 3_600_000.0


@dataclass(slots=True)
class SegmentationResult:
    segments: list[Segment]
    ambiguous: bool = False
    change_points: list[datetime] = field(default_factory=list)
    noise_w: float = 0.0
    notes: list[str] = field(default_factory=list)

    def distinct_levels(self, tolerance_w: float = 5.0) -> int:
        levels: list[float] = []
        for segment in self.segments:
            if not levels or all(abs(segment.power_w - level) > tolerance_w for level in levels):
                levels.append(segment.power_w)
        return len(levels)


def robust_noise(values: Sequence[float]) -> float:
    if len(values) < 3:
        return 0.0
    diffs = [abs(b - a) for a, b in zip(values, values[1:], strict=False)]
    med = statistics.median(diffs)
    mad = statistics.median([abs(d - med) for d in diffs])
    return float(1.4826 * mad / math.sqrt(2.0))


def _median(values: Sequence[float]) -> float:
    return float(statistics.median(values)) if values else 0.0


def segment_power_series(
    series: object,
    *,
    window: int = 3,
    relative_threshold: float = 0.15,
    noise_k: float = 4.0,
    min_change_w: float = 15.0,
    merge_threshold_w: float = 8.0,
    min_segment_s: float = 20.0,
    ambiguity_ratio: float = 0.6,
) -> SegmentationResult:
    """Segment a ``[(datetime, watts)]`` series into near-constant stages.

    Detection is *local*: a change point is opened when the robust level
    difference between the preceding and following windows exceeds

    ``max(min_change_w, noise_k · robust_noise, relative_threshold · max(|level_before|, |level_after|))``

    The relative term compares against the *larger of the two local levels*,
    not against the series maximum.  That matters for appliances: a washing
    machine whose heating stage is 2.2 kW still has a real 150 W tumble stage,
    which a max-relative threshold would silently merge into idle.  The
    absolute ``min_change_w`` floor prevents chasing CT resolution noise, and
    ``noise_k · noise`` handles very noisy channels.

    ``ambiguity_ratio`` marks the segmentation ambiguous when the smallest
    detected level separation is below ``ambiguity_ratio`` of the local
    detection threshold — i.e. when consecutive stages are barely
    distinguishable from measurement noise.
    """
    points = normalise_series(series)
    if len(points) < 2:
        if len(points) == 1:
            dt, power = points[0]
            return SegmentationResult(
                segments=[Segment(dt, dt, power, 0.0, 1, 0)],
                ambiguous=True,
                notes=["series_too_short"],
            )
        return SegmentationResult(segments=[], ambiguous=True, notes=["no_samples"])

    times = [p[0] for p in points]
    values = [p[1] for p in points]
    n = len(values)
    noise = robust_noise(values)
    spread = max(values) - min(values)
    if spread <= 1e-9:
        return SegmentationResult(
            segments=[Segment(times[0], times[-1], _median(values), _duration(times[0], times[-1]), n, 0)],
            ambiguous=False,
            noise_w=noise,
            notes=["constant_series"],
        )

    half = max(1, window // 2)
    change_indices: list[int] = []
    change_strength: list[float] = []
    last_change = -1
    i = half
    while i < n - half:
        before = values[max(0, i - window) : i]
        after = values[i : min(n, i + window)]
        if not before or not after:
            i += 1
            continue
        level_before = _median(before)
        level_after = _median(after)
        local_scale = max(abs(level_before), abs(level_after))
        threshold = max(min_change_w, noise_k * noise, relative_threshold * local_scale)
        if abs(level_after - level_before) >= threshold:
            # Local maximum of |delta| within ±window avoids duplicate splits.
            candidates = range(max(half, i - window), min(n - half, i + window + 1))
            local = max(
                candidates,
                key=lambda j: abs(
                    _median(values[max(0, j - window) : j]) - _median(values[j : min(n, j + window)])
                ),
            )
            if local > last_change + window:
                change_indices.append(local)
                change_strength.append(abs(level_after - level_before))
                last_change = local
                i = local + window
                continue
        i += 1

    boundaries = [0, *change_indices, n]
    raw_segments: list[Segment] = []
    for idx, (a, b) in enumerate(zip(boundaries, boundaries[1:], strict=False)):
        if b <= a:
            continue
        chunk = values[a:b]
        start, end = times[a], times[b - 1]
        raw_segments.append(
            Segment(
                start=start,
                end=end,
                power_w=_median(chunk),
                duration_s=_duration(start, end),
                sample_count=len(chunk),
                index=idx,
            )
        )

    # merge adjacent segments that are within merge threshold
    merged: list[Segment] = []
    for segment in raw_segments:
        if merged and abs(segment.power_w - merged[-1].power_w) <= max(merge_threshold_w, noise_k * noise):
            prev = merged.pop()
            total_count = prev.sample_count + segment.sample_count
            weighted = (prev.power_w * prev.sample_count + segment.power_w * segment.sample_count) / max(
                1, total_count
            )
            merged.append(
                Segment(
                    start=prev.start,
                    end=segment.end,
                    power_w=float(weighted),
                    duration_s=_duration(prev.start, segment.end),
                    sample_count=total_count,
                    index=prev.index,
                )
            )
        else:
            merged.append(segment)

    # drop too-short interior segments (merge into the previous one)
    cleaned: list[Segment] = []
    for segment in merged:
        if cleaned and 0 < segment.duration_s < min_segment_s and segment is not merged[-1]:
            prev = cleaned.pop()
            total = prev.sample_count + segment.sample_count
            cleaned.append(
                Segment(
                    start=prev.start,
                    end=segment.end,
                    power_w=(prev.power_w * prev.sample_count + segment.power_w * segment.sample_count)
                    / max(1, total),
                    duration_s=_duration(prev.start, segment.end),
                    sample_count=total,
                    index=prev.index,
                )
            )
        else:
            cleaned.append(segment)
    for idx, segment in enumerate(cleaned):
        segment.index = idx

    ambiguous = False
    notes: list[str] = []
    # The smallest level change we trust: an absolute floor tied to CT
    # resolution, scaled by measured noise.  Deliberately *not* relative to the
    # largest stage — a 150 W tumble stage is real even in a 2.2 kW appliance.
    detectable = max(min_change_w, noise_k * noise)
    indistinguishable = max(noise_k * noise, ambiguity_ratio * detectable)
    if len(cleaned) < 2:
        notes.append("single_stage")
        # A single segment is still ambiguous if its internal spread is wide
        # enough to hide unresolved levels.  Pure constant series (spread 0)
        # stay unambiguous; a noisy envelope whose spread exceeds what we could
        # have split is marked unknown rather than silently claimed as one stage.
        chunk = list(values)
        if len(chunk) >= 4:
            p05 = _quantile(chunk, 0.05)
            p95 = _quantile(chunk, 0.95)
            hidden_threshold = max(2.0 * min_change_w, 2.0 * noise)
            if (p95 - p05) > hidden_threshold:
                ambiguous = True
                notes.append(
                    f"possible_unresolved_levels:spread={p95 - p05:.1f}W>threshold={hidden_threshold:.1f}W"
                )
    else:
        separations = [
            abs(b.power_w - a.power_w) for a, b in zip(cleaned, cleaned[1:], strict=False) if abs(b.power_w - a.power_w) > 0
        ]
        if separations:
            min_sep = min(separations)
            if min_sep < indistinguishable:
                ambiguous = True
                notes.append(
                    f"indistinguishable_levels:min_separation={min_sep:.1f}W<threshold={indistinguishable:.1f}W"
                )
        else:
            ambiguous = True
            notes.append("no_level_separation")
    return SegmentationResult(
        segments=cleaned,
        ambiguous=ambiguous,
        change_points=[times[i] for i in change_indices],
        noise_w=noise,
        notes=notes,
    )


def _quantile(values: Sequence[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    pos = q * (len(ordered) - 1)
    low = math.floor(pos)
    high = math.ceil(pos)
    if low == high:
        return float(ordered[low])
    frac = pos - low
    return float(ordered[low] * (1.0 - frac) + ordered[high] * frac)


def _duration(start: datetime, end: datetime) -> float:
    return max(0.0, (end - start).total_seconds())


__all__ = ["Segment", "SegmentationResult", "robust_noise", "segment_power_series"]
