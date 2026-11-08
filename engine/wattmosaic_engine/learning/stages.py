"""Operating-stage recognition for multi-stage appliances.

The substantive algorithm is the robust change-point segmenter in
:mod:`wattmosaic_engine.models.segmentation`; this module adapts it to the
contract objects the learning pipeline persists:

* :func:`recognize_stages` turns a power series into ``list[OperatingStage]``
  with ``StageStatus`` resolved from the segmentation quality.
* :func:`current_stage` reports the stage for the most recent portion of a
  series (used for live ``operating_stage`` fields).
* :func:`segment_cycle` returns the raw :class:`SegmentationResult` for callers
  that need cycle boundaries (aggregation persists runs).

Rule from spec §12: never claim a stage has been identified when the available
observations cannot distinguish it.  Ambiguous segmentation therefore produces
either ``StageStatus.PROBABLE`` (weak but non-overlapping separation) or
``StageStatus.UNKNOWN`` (overlapping levels / noise as large as the change).
"""

from __future__ import annotations

from typing import Any

from ..contracts import OperatingStage, StageStatus
from ..models.protocols import normalise_series
from ..models.segmentation import Segment, SegmentationResult, robust_noise, segment_power_series

#: Minimum duration before a stage can be called "identified" rather than
#: "probable".  With 15.7 s cadence, ~3 readings (≈47 s) is the practical floor;
#: we use 45 s and require the segmenter's own min_segment_s to have passed.
_MIN_IDENTIFIED_DURATION_S = 45.0


def _stage_status(segment: Segment, result: SegmentationResult, index: int, total: int) -> StageStatus:
    if result.ambiguous:
        return StageStatus.UNKNOWN
    if segment.duration_s < _MIN_IDENTIFIED_DURATION_S and total > 1:
        return StageStatus.PROBABLE
    if result.noise_w > 0 and segment.power_w > 0 and result.noise_w / segment.power_w > 0.25:
        return StageStatus.PROBABLE
    return StageStatus.IDENTIFIED


def _label(segment: Segment, result: SegmentationResult) -> str:
    if result.ambiguous:
        return "unclassified"
    levels = [s.power_w for s in result.segments] or [segment.power_w]
    low, high = min(levels), max(levels)
    span = max(1e-9, high - low)
    position = (segment.power_w - low) / span
    if position <= 0.10:
        return "standby"
    if position <= 0.35:
        return "low"
    if position <= 0.65:
        return "medium"
    if position <= 0.90:
        return "high"
    return "peak"


def recognize_stages(
    series: Any,
    *,
    min_change_w: float = 15.0,
    relative_threshold: float = 0.15,
    noise_k: float = 4.0,
    merge_threshold_w: float = 8.0,
    min_segment_s: float = 20.0,
) -> list[OperatingStage]:
    """Return contract ``OperatingStage`` objects for a power series."""
    result = segment_power_series(
        series,
        min_change_w=min_change_w,
        relative_threshold=relative_threshold,
        noise_k=noise_k,
        merge_threshold_w=merge_threshold_w,
        min_segment_s=min_segment_s,
    )
    total = len(result.segments)
    stages: list[OperatingStage] = []
    for index, segment in enumerate(result.segments):
        status = _stage_status(segment, result, index, total)
        stages.append(
            OperatingStage(
                name=_label(segment, result),
                status=status,
                power_mean_w=float(segment.power_w),
                power_std_w=float(result.noise_w),
                min_duration_s=float(segment.duration_s) if segment.duration_s > 0 else None,
                max_duration_s=float(segment.duration_s) if segment.duration_s > 0 else None,
                notes=[
                    f"segment:{index}",
                    f"start:{segment.start.isoformat()}",
                    f"end:{segment.end.isoformat()}",
                    *result.notes,
                ],
            )
        )
    return stages


def current_stage(series: Any, *, recent_fraction: float = 0.30) -> tuple[str | None, StageStatus]:
    """Identify the stage visible in the most recent part of a series.

    ``recent_fraction`` limits detection to the tail so a long historical cycle
    cannot make the *current* stage ambiguous just because an earlier stage was
    noisy.  If the tail contains no resolvable change point the stage is
    ``UNKNOWN``.
    """
    points = normalise_series(series)
    if not points:
        return None, StageStatus.UNKNOWN
    if len(points) >= 4:
        cut = max(2, int(len(points) * max(0.05, min(1.0, recent_fraction))))
        points = points[-cut:]
    result = segment_power_series(points, min_segment_s=0.0)
    if not result.segments:
        return None, StageStatus.UNKNOWN
    if result.ambiguous:
        return "unclassified", StageStatus.UNKNOWN
    last = result.segments[-1]
    if len(result.segments) == 1:
        # Single level: it is a *level*, not a named stage transition.
        return _label(last, result), StageStatus.PROBABLE
    return _label(last, result), StageStatus.IDENTIFIED


def segment_cycle(series: Any, **kwargs: Any) -> SegmentationResult:
    return segment_power_series(series, **kwargs)


def stage_distinguishable(series: Any, min_change_w: float = 15.0, noise_k: float = 4.0) -> bool:
    """True when the series contains at least one resolvable change point."""
    result = segment_power_series(series, min_change_w=min_change_w, noise_k=noise_k)
    return len(result.segments) >= 1 and not result.ambiguous


__all__ = [
    "current_stage",
    "recognize_stages",
    "robust_noise",
    "segment_cycle",
    "segment_power_series",
    "stage_distinguishable",
]
