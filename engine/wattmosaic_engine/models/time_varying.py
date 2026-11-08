"""Time-varying appliance model (washing machine, dishwasher, dryer, fridge…).

The model combines two sources of evidence:

* **Stage profile** learned from accepted learning samples (each sample carries
  the stable power of one operating segment).  Stages are clustered 1-D on
  power, named heuristically from relative magnitude, and annotated with
  duration bounds.
* **Cycle segmentation** of an observed power series using robust change-point
  detection (:mod:`~wattmosaic_engine.models.segmentation`).  When the observed
  stages cannot be separated from measurement noise, the model reports
  ``StageStatus.UNKNOWN`` and never claims a stage name.

The model also computes runtime and energy for a cycle, which the aggregation
layer persists as ``device_runs``.
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..contracts import (
    DeviceModelParameters,
    LearningSample,
    ModelType,
    OperatingStage,
    StageStatus,
)
from .base import DeviceModel, context_power_series, context_state, finite, median, stdev
from .protocols import normalise_series
from .segmentation import SegmentationResult, segment_power_series

#: relative power bands used to give cluster names a physical meaning
_STAGE_NAMES = ("standby", "low", "medium", "high", "peak")


@dataclass(slots=True)
class CycleSummary:
    """Runtime / energy summary of one segmented appliance cycle."""

    started_at: datetime
    ended_at: datetime
    runtime_s: float
    energy_kwh: float
    peak_power_w: float
    mean_power_w: float
    stages: list[OperatingStage] = field(default_factory=list)
    stage_status: StageStatus = StageStatus.UNKNOWN
    notes: list[str] = field(default_factory=list)


class TimeVaryingApplianceModel(DeviceModel):
    """Multi-stage appliance model with cycle segmentation."""

    model_type = ModelType.TIME_VARYING
    minimum_samples = 6

    # Segmentation parameters are intentionally conservative: appliances have
    # large stage separations, so a 15 % relative change plus 5·noise is safely
    # detectable with the real 15.7 s CT cadence.
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.segment_kwargs: dict[str, Any] = {
            "relative_threshold": 0.15,
            "noise_k": 5.0,
            "merge_threshold_w": 9.0,
            "min_segment_s": 20.0,
            "ambiguity_ratio": 0.6,
        }

    # -- fitting ---------------------------------------------------------- #
    def fit(self, samples: Sequence[LearningSample], **kwargs: Any) -> DeviceModelParameters:
        accepted = [s for s in samples if s.accepted]
        powers = [finite(s.event_power_w) for s in accepted]
        powers = [p for p in powers if p is not None]
        clusters = _cluster_levels(powers)
        stages: list[OperatingStage] = []
        all_values = list(powers)
        low, high = (min(all_values), max(all_values)) if all_values else (0.0, 0.0)
        span = max(1.0, high - low)
        for centre, members in clusters:
            label = _stage_label(centre, low, high, span)
            durations = [
                s.duration_s
                for s in accepted
                if finite(s.event_power_w) is not None and abs(s.event_power_w - centre) <= max(5.0, span * 0.08)
            ]
            stages.append(
                OperatingStage(
                    name=label,
                    status=StageStatus.IDENTIFIED if len(members) >= 2 else StageStatus.PROBABLE,
                    power_mean_w=float(centre),
                    power_std_w=float(stdev(members)),
                    min_duration_s=min(durations) if durations else None,
                    max_duration_s=max(durations) if durations else None,
                    notes=[f"samples:{len(members)}"],
                )
            )
        stages.sort(key=lambda s: s.power_mean_w)
        parameters = DeviceModelParameters(
            stages=stages,
            on_power_w=median([s.power_mean_w for s in stages]) if stages else None,
            off_power_w=stages[0].power_mean_w if stages and stages[0].power_mean_w <= low + 0.1 * span else None,
            residual_mean_w=0.0 if powers else None,
            residual_std_w=(
                statistics.pstdev([p - _nearest_level(p, clusters) for p in powers]) if len(powers) > 1 else 0.0
            ),
        )
        self.notes = {
            "stage_count": len(stages),
            "levels": [round(float(c), 2) for c, _ in clusters],
            "sample_count": len(powers),
        }
        return self._finish_fit(parameters)

    def fit_series(self, series: object, **kwargs: Any) -> DeviceModelParameters:
        """Fit the stage profile directly from an observed power series.

        This is the preferred path when a dedicated CT exists: the series is
        segmented, and each segment contributes its median power + duration.
        Ambiguous segmentation still yields a profile, but every stage is marked
        ``StageStatus.UNKNOWN`` so promotion will not silently trust it.
        """
        points = normalise_series(series)
        result = segment_power_series(points, **{**self.segment_kwargs, **kwargs})
        stages: list[OperatingStage] = []
        if result.segments:
            low = min(seg.power_w for seg in result.segments)
            high = max(seg.power_w for seg in result.segments)
            span = max(1.0, high - low)
            for seg in result.segments:
                label = _stage_label(seg.power_w, low, high, span)
                status = StageStatus.UNKNOWN if result.ambiguous else StageStatus.IDENTIFIED
                stages.append(
                    OperatingStage(
                        name=label,
                        status=status,
                        power_mean_w=seg.power_w,
                        power_std_w=result.noise_w,
                        min_duration_s=seg.duration_s if seg.duration_s > 0 else None,
                        max_duration_s=seg.duration_s if seg.duration_s > 0 else None,
                        notes=[f"segment:{seg.index}", *result.notes],
                    )
                )
        parameters = DeviceModelParameters(
            stages=stages,
            on_power_w=median([s.power_mean_w for s in stages]) if stages else None,
            off_power_w=stages[0].power_mean_w if len(stages) > 1 else None,
            residual_std_w=result.noise_w,
        )
        self.notes = {
            "stage_count": len(stages),
            "segmentation_ambiguous": result.ambiguous,
            "segmentation_notes": list(result.notes),
            "sample_count": len(points),
        }
        self.segment_kwargs = {**self.segment_kwargs, **kwargs}
        return self._finish_fit(parameters)

    # -- prediction ------------------------------------------------------- #
    def _series_for(self, context: Any) -> list[tuple[datetime, float]]:
        return context_power_series(context)

    def _observed_stage(self, context: Any) -> tuple[OperatingStage | None, StageStatus, SegmentationResult | None]:
        points = self._series_for(context)
        if len(points) < 2:
            return None, StageStatus.UNKNOWN, None
        result = segment_power_series(points, **self.segment_kwargs)
        if not result.segments:
            return None, StageStatus.UNKNOWN, result
        last = result.segments[-1]
        if result.ambiguous:
            # Cannot distinguish the level → report the measured level but do
            # not claim a learned stage name.
            stage = OperatingStage(
                name="unclassified",
                status=StageStatus.UNKNOWN,
                power_mean_w=last.power_w,
                power_std_w=result.noise_w,
                notes=["ambiguous_segmentation", *result.notes],
            )
            return stage, StageStatus.UNKNOWN, result
        # Match the observed level to the nearest learned stage within a
        # tolerance based on noise and the learned stage spacing.
        learned = self.parameters.stages
        if not learned:
            return (
                OperatingStage(
                    name="unclassified",
                    status=StageStatus.UNKNOWN,
                    power_mean_w=last.power_w,
                    power_std_w=result.noise_w,
                ),
                StageStatus.UNKNOWN,
                result,
            )
        nearest = min(learned, key=lambda s: abs(s.power_mean_w - last.power_w))
        tolerance = max(10.0, self.segment_kwargs.get("merge_threshold_w", 9.0) * 2)
        if abs(nearest.power_mean_w - last.power_w) <= tolerance:
            status = StageStatus.IDENTIFIED if nearest.status == StageStatus.IDENTIFIED else StageStatus.PROBABLE
            return nearest, status, result
        return (
            OperatingStage(
                name="transient",
                status=StageStatus.UNKNOWN,
                power_mean_w=last.power_w,
                power_std_w=result.noise_w,
                notes=["observed_level_not_close_to_learned_stage"],
            ),
            StageStatus.UNKNOWN,
            result,
        )

    def predict(self, context: Any) -> float:
        stage, status, result = self._observed_stage(context)
        if stage is not None:
            return float(stage.power_mean_w)
        state = context_state(context)
        if state is not None and state.lower() in {"off", "unavailable", "unknown"}:
            return float(
                self.parameters.off_power_w
                if self.parameters.off_power_w is not None
                else (min((s.power_mean_w for s in self.parameters.stages), default=0.0))
            )
        if (
            state is not None
            and state.lower() in {"on", "running", "active", "heating", "washing"}
            and self.parameters.stages
        ):
            # Running but no series: median stage is the least-surprising guess.
            return float(median([s.power_mean_w for s in self.parameters.stages]) or 0.0)
        if self.parameters.stages:
            return float(self.parameters.stages[0].power_mean_w)
        return 0.0

    def predict_detailed(self, context: Any) -> tuple[float, str | None, StageStatus]:
        stage, status, _ = self._observed_stage(context)
        if stage is not None:
            return float(stage.power_mean_w), stage.name, status
        state = context_state(context)
        power = self.predict(context)
        if state is None:
            return power, None, StageStatus.UNKNOWN
        if self.parameters.stages:
            nearest = min(self.parameters.stages, key=lambda s: abs(s.power_mean_w - power))
            return power, nearest.name, StageStatus.UNKNOWN
        return power, None, StageStatus.UNKNOWN

    def confidence(self, context: Any | None = None) -> float:
        base = super().confidence(context)
        if not self.parameters.stages:
            return min(base, 0.15)
        stage, status, result = self._observed_stage(context) if context is not None else (None, StageStatus.UNKNOWN, None)
        if status == StageStatus.IDENTIFIED:
            factor = 1.0
        elif status == StageStatus.PROBABLE:
            factor = 0.75
        else:
            factor = 0.35
            if result is not None and result.ambiguous:
                factor = 0.25
        return max(0.0, min(1.0, base * factor))

    def explanation(self, context: Any) -> list[str]:
        stage, status, _ = self._observed_stage(context)
        parts = super().explanation(context) + ["time_varying_stage_model"]
        parts.append(f"stage:{stage.name if stage else 'none'}")
        parts.append(f"stage_status:{status.value}")
        return parts

    # -- cycle utilities --------------------------------------------------- #
    def segment_cycle(self, series: object) -> SegmentationResult:
        """Segment one complete appliance cycle."""
        return segment_power_series(normalise_series(series), **self.segment_kwargs)

    def cycle_summary(self, series: object) -> CycleSummary:
        """Compute runtime, energy, peak and mean power for a cycle.

        Energy uses the trapezoidal rule over actual elapsed time, which is the
        same convention as :class:`~wattmosaic_engine.aggregation.EnergyAggregator`
        so the two never disagree about a cycle's energy.
        """
        points = normalise_series(series)
        if not points:
            now = datetime.now()
            return CycleSummary(now, now, 0.0, 0.0, 0.0, 0.0, [], StageStatus.UNKNOWN, ["no_samples"])
        result = segment_power_series(points, **self.segment_kwargs)
        energy_kwh = trapezoidal_energy_kwh(points)
        powers = [p for _, p in points]
        stages: list[OperatingStage] = []
        for seg in result.segments:
            status = StageStatus.UNKNOWN if result.ambiguous else (
                StageStatus.IDENTIFIED if seg.duration_s >= 20.0 else StageStatus.PROBABLE
            )
            stages.append(
                OperatingStage(
                    name="unclassified" if result.ambiguous else _stage_label(
                        seg.power_w,
                        min(s.power_w for s in result.segments),
                        max(s.power_w for s in result.segments),
                        max(1.0, max(s.power_w for s in result.segments) - min(s.power_w for s in result.segments)),
                    ),
                    status=status,
                    power_mean_w=seg.power_w,
                    power_std_w=result.noise_w,
                    min_duration_s=seg.duration_s,
                    max_duration_s=seg.duration_s,
                    notes=list(result.notes) if result.ambiguous else [f"segment:{seg.index}"],
                )
            )
        return CycleSummary(
            started_at=points[0][0],
            ended_at=points[-1][0],
            runtime_s=max(0.0, (points[-1][0] - points[0][0]).total_seconds()),
            energy_kwh=energy_kwh,
            peak_power_w=max(powers),
            mean_power_w=energy_kwh * 3_600_000.0 / max(1.0, (points[-1][0] - points[0][0]).total_seconds()),
            stages=stages,
            stage_status=StageStatus.UNKNOWN if result.ambiguous else StageStatus.IDENTIFIED,
            notes=list(result.notes),
        )

    def describe(self) -> dict[str, Any]:
        data = super().describe()
        data["notes"] = dict(getattr(self, "notes", {}))
        data["segmentation_parameters"] = dict(self.segment_kwargs)
        return data


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def trapezoidal_energy_kwh(points: Sequence[tuple[datetime, float]]) -> float:
    """Trapezoidal integration over actual elapsed time (kWh)."""
    total_j = 0.0
    for (t0, p0), (t1, p1) in zip(points, points[1:], strict=False):
        dt = (t1 - t0).total_seconds()
        if dt <= 0:
            continue
        total_j += (max(0.0, p0) + max(0.0, p1)) / 2.0 * dt
    return total_j / 3_600_000.0


def _nearest_level(value: float, clusters: Sequence[tuple[float, list[float]]]) -> float:
    if not clusters:
        return value
    return min((c for c, _ in clusters), key=lambda c: abs(c - value))


def _cluster_levels(values: Sequence[float], min_gap_w: float = 10.0, max_clusters: int = 6) -> list[tuple[float, list[float]]]:
    """Cluster 1-D power observations into appliance stages.

    Sorted sequential clustering with a minimum gap: this is deterministic and
    explainable, and appliance stage levels are well separated in practice.
    A gap is opened only when it exceeds both ``min_gap_w`` and 12 % of the
    observed range, which avoids splitting a noisy single stage.
    """
    clean = sorted(float(v) for v in values if finite(v) is not None)
    if not clean:
        return []
    span = clean[-1] - clean[0]
    gap_threshold = max(min_gap_w, span * 0.12)
    clusters: list[list[float]] = [[clean[0]]]
    for value in clean[1:]:
        if value - clusters[-1][-1] >= gap_threshold:
            clusters.append([value])
        else:
            clusters[-1].append(value)
    out = [(float(median(members) or 0.0), members) for members in clusters]
    if len(out) > max_clusters:
        # Merge the closest neighbours until at most max_clusters remain.
        while len(out) > max_clusters:
            gaps = [out[i + 1][0] - out[i][0] for i in range(len(out) - 1)]
            idx = gaps.index(min(gaps))
            left, right = out[idx], out[idx + 1]
            merged_members = left[1] + right[1]
            out[idx : idx + 2] = [(float(median(merged_members) or 0.0), merged_members)]
    return out


def _stage_label(centre: float, low: float, high: float, span: float) -> str:
    if span <= 1e-9:
        return "steady"
    position = (centre - low) / span
    if position <= 0.10:
        return "standby"
    if position <= 0.35:
        return "low"
    if position <= 0.65:
        return "medium"
    if position <= 0.90:
        return "high"
    return "peak"


__all__ = ["CycleSummary", "TimeVaryingApplianceModel", "trapezoidal_energy_kwh"]
