"""Residual-based device model for devices without rich state information.

This model is intentionally humble.  It learns a time-conditioned baseline power
distribution from historical observations and *explicitly* carries an
uncertainty band.  It never claims a device is ON; it reports an estimated
contribution plus the width of the residual distribution.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from ..contracts import DeviceModelParameters, LearningSample, ModelType
from .base import (
    DeviceModel,
    context_state,
    finite,
    median,
    normalise_series,
    quantile,
    stdev,
)
from .segmentation import segment_power_series


class ResidualDeviceModel(DeviceModel):
    """Pattern-based estimate with explicit uncertainty.

    Learning strategy:

    * Greedily select the top ``k`` time-of-day bins (default 6 four-hour bins)
      that carry the largest median power; fall back to a global median.
    * Store ``residual_mean_w`` / ``residual_std_w`` and the 10/90 percentiles
      in ``notes`` so callers can report an uncertainty interval.
    * ``predict`` returns the bin median for the current hour, falling back to
      the global median.  ``uncertainty_w`` returns the robust residual spread.

    Because the model cannot know whether the device is on, its confidence is
    capped well below dedicated models (``max_confidence``), and explanations
    always include a ``residual_model_no_state`` marker.
    """

    model_type = ModelType.RESIDUAL
    minimum_samples = 5
    max_confidence = 0.45
    bins = 6

    def fit(self, samples: Sequence[LearningSample], **kwargs: Any) -> DeviceModelParameters:
        powers: list[float] = []
        by_bin: dict[int, list[float]] = {}
        for sample in samples:
            if not sample.accepted:
                continue
            power = finite(sample.event_power_w)
            if power is None:
                continue
            powers.append(power)
            by_bin.setdefault(self._bin_of(sample.captured_at.hour), []).append(power)
        global_median = median(powers) if powers else None
        bin_stats: dict[int, dict[str, float]] = {}
        if powers:
            ranked = sorted(
                by_bin.items(),
                key=lambda kv: -len(kv[1]),
            )
            keep = [b for b, values in ranked if len(values) >= 2][: self.bins]
            for b in keep:
                values = by_bin[b]
                bin_stats[b] = {
                    "median_w": float(median(values) or 0.0),
                    "std_w": float(stdev(values)),
                    "count": float(len(values)),
                }
            # A bin whose median is not meaningfully above the global median
            # carries no information and would overfit; drop it.
            if global_median is not None:
                bin_stats = {
                    b: stat
                    for b, stat in bin_stats.items()
                    if abs(stat["median_w"] - global_median) >= max(5.0, 0.10 * max(1.0, abs(global_median)))
                }
        residuals = []
        for sample in samples:
            if not sample.accepted or finite(sample.event_power_w) is None:
                continue
            pred = self._predict_from_stats(sample.captured_at.hour, bin_stats, global_median)
            if pred is not None:
                residuals.append(sample.event_power_w - pred)
        parameters = DeviceModelParameters(
            residual_mean_w=(sum(residuals) / len(residuals)) if residuals else global_median,
            residual_std_w=stdev(residuals) if residuals else (stdev(powers) if powers else None),
        )
        self.global_median_w = global_median
        self.bin_stats = bin_stats
        self.notes = {
            "bin_count": len(bin_stats),
            "global_median_w": global_median,
            "p10_w": quantile(powers, 0.10),
            "p90_w": quantile(powers, 0.90),
            "spread_w": (max(powers) - min(powers)) if powers else 0.0,
            "sample_count": len(powers),
        }
        return self._finish_fit(parameters)

    @staticmethod
    def _bin_of(hour: int) -> int:
        # Six 4-hour bins keep each bin wide enough to accumulate samples with
        # 15.7 s CT cadence and typical natural device usage.
        return max(0, min(5, int(hour) // 4))

    @staticmethod
    def _predict_from_stats(hour: int, bin_stats: dict[int, dict[str, float]], global_median: float | None) -> float | None:
        bin_id = ResidualDeviceModel._bin_of(hour)
        if bin_id in bin_stats:
            return bin_stats[bin_id]["median_w"]
        return global_median

    def predict(self, context: Any) -> float:
        state = context_state(context)
        if state is not None and state.lower() in {"off", "unavailable", "unknown"}:
            # Even a residual device with an OFF state should not be credited
            # with power.  This is a real observation, not a guess.
            return 0.0
        hour = self._hour(context)
        predicted = self._predict_from_stats(hour, self.bin_stats, self.global_median_w)
        return float(predicted or 0.0)

    def _hour(self, context: Any) -> int:
        from .protocols import context_timestamp, context_value

        hour = finite(context_value(context, "hour"))
        if hour is not None:
            return int(hour) % 24
        ts = context_timestamp(context)
        if ts is not None:
            return ts.hour
        return datetime.now().hour

    def confidence(self, context: Any | None = None) -> float:
        if self.global_median_w is None:
            return 0.0
        spread = (getattr(self, "notes", {}) or {}).get("spread_w") or 0.0
        magnitude = max(1.0, abs(self.global_median_w))
        # Wider spread → lower confidence.  Hard cap at max_confidence because
        # the model cannot observe device state.
        quality = max(0.0, 1.0 - min(1.0, float(spread) / (2.0 * magnitude)))
        return max(0.0, min(self.max_confidence, self.max_confidence * quality))

    def uncertainty_w(self, context: Any | None = None) -> float:
        if self.parameters.residual_std_w is not None:
            return max(float(self.parameters.residual_std_w), 0.0)
        p10 = (getattr(self, "notes", {}) or {}).get("p10_w")
        p90 = (getattr(self, "notes", {}) or {}).get("p90_w")
        if p10 is not None and p90 is not None:
            return max(0.0, (float(p90) - float(p10)) / 2.0)
        return 0.0

    def explanation(self, context: Any) -> list[str]:
        return super().explanation(context) + ["residual_model_no_state", "explicit_uncertainty"]

    def describe(self) -> dict[str, Any]:
        data = super().describe()
        data["notes"] = dict(getattr(self, "notes", {}))
        data["max_confidence"] = self.max_confidence
        data["bins"] = {str(k): v for k, v in self.bin_stats.items()}
        return data

    # -- series helper ---------------------------------------------------- #
    def baseline_from_series(self, series: object) -> dict[str, float]:
        """Describe a observed series as a residual baseline (diagnostic).

        Uses segmentation to separate a stable baseline from intermittent
        activity, returning median baseline, robust noise and peak.
        """
        points = normalise_series(series)
        result = segment_power_series(points)
        powers = [p for _, p in points]
        baseline = median(powers) or 0.0
        return {
            "baseline_median_w": float(baseline),
            "noise_w": result.noise_w,
            "peak_w": max(powers) if powers else 0.0,
            "segments": float(len(result.segments)),
        }
