"""Variable-speed device model (fans, pumps, motors).

Learns a control-value → watts curve like a dimmer, but the control vocabulary
is speed/percentage/level/RPM and the physical curve is typically quadratic
(affinity laws).  We fit the monotonic lookup directly (no forced polynomial)
because measured curves differ substantially per motor/duct, then expose the
dominant polynomial exponent in ``describe()`` for diagnostics.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

from ..contracts import DeviceModelParameters, LearningSample, ModelType, OperatingStage, StageStatus
from .base import (
    DeviceModel,
    context_control,
    context_state,
    enforce_monotonic,
    finite,
    is_on,
    median,
    stdev,
)

_SPEED_ATTRS = (
    "speed",
    "percentage",
    "fan_speed",
    "speed_pct",
    "level",
    "rpm",
    "speed_level",
    "motor_speed",
    "pump_speed",
)
_MAX_RPM = 2000.0


def _speed(context: Any) -> tuple[float | None, str | None]:
    value, name = context_control(context, *_SPEED_ATTRS)
    if value is None:
        return None, None
    if name == "rpm" and value > 100:
        # Normalise RPM to percent of a typical residential motor so a model
        # trained in % can consume RPM state attributes.
        value = min(100.0, value / _MAX_RPM * 100.0)
    return value, name


class VariableSpeedDeviceModel(DeviceModel):
    """Monotonic speed → watts lookup with discrete stage recognition."""

    model_type = ModelType.VARIABLE_SPEED
    minimum_samples = 3

    def fit(self, samples: Sequence[LearningSample], **kwargs: Any) -> DeviceModelParameters:
        points: list[tuple[float, float]] = []
        off_values: list[float] = []
        variables: list[str] = []
        for sample in samples:
            if not sample.accepted:
                continue
            variable = (sample.control_variable or "").strip().lower()
            control = finite(sample.control_value)
            power = finite(sample.event_power_w)
            if control is None or power is None:
                continue
            variables.append(variable)
            if variable == "rpm" and control > 100:
                control = min(100.0, control / _MAX_RPM * 100.0)
            if variable in _SPEED_ATTRS:
                points.append((control, power))
            elif sample.delta_power_w < 0:
                off_values.append(power)
        curve = enforce_monotonic(points)
        off_power = median(off_values) if off_values else None
        residuals = []
        for x, y in points:
            from .base import _linear_interp

            p = _linear_interp(x, curve)
            if p is not None:
                residuals.append(y - p)
        parameters = DeviceModelParameters(
            curve=curve,
            on_power_w=median([y for _, y in curve]) if curve else None,
            off_power_w=off_power,
            residual_mean_w=(sum(residuals) / len(residuals)) if residuals else None,
            residual_std_w=stdev(residuals) if residuals else None,
            stages=[self._stage_for(x, y) for x, y in curve],
        )
        self.control_variable = variables[0] if variables else "speed"
        self.power_law_exponent = self._estimate_exponent(curve)
        self.notes = {
            "curve_points": len(curve),
            "control_variable": self.control_variable,
            "power_law_exponent": self.power_law_exponent,
        }
        return self._finish_fit(parameters)

    @staticmethod
    def _stage_for(x: float, y: float) -> OperatingStage:
        # Fan/pump stages are conventionally low/medium/high at 33/66/100 %.
        if x <= 40:
            label = "low"
        elif x <= 75:
            label = "medium"
        elif x <= 100:
            label = "high"
        else:
            label = f"level_{x:g}"
        return OperatingStage(
            name=label,
            status=StageStatus.IDENTIFIED,
            power_mean_w=float(y),
            notes=[f"control_value:{x:g}"],
        )

    @staticmethod
    def _estimate_exponent(curve: Sequence[tuple[float, float]]) -> float | None:
        """Fit ``P = k * v^n`` by least squares in log space (diagnostic only)."""
        pts = [(x, y) for x, y in curve if x > 0 and y > 0]
        if len(pts) < 3:
            return None
        xs = [math.log(x) for x, _ in pts]
        ys = [math.log(y) for _, y in pts]
        mean_x = sum(xs) / len(xs)
        mean_y = sum(ys) / len(ys)
        denom = sum((x - mean_x) ** 2 for x in xs)
        if denom <= 1e-12:
            return None
        return sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)) / denom

    def _normalise_speed(self, value: float, variable: str | None) -> float:
        if variable == "rpm" and value > 100:
            return min(100.0, value / _MAX_RPM * 100.0)
        return value

    def predict(self, context: Any) -> float:
        state = context_state(context)
        on_flag = is_on(state)
        value, name = _speed(context)
        from .base import _linear_interp

        if value is not None:
            x = self._normalise_speed(value, name)
            if x <= 1.0 and on_flag is False:
                return float(self.parameters.off_power_w or 0.0)
            predicted = _linear_interp(x, self.parameters.curve)
            if predicted is not None:
                return float(predicted)
        if on_flag is False:
            return float(self.parameters.off_power_w or 0.0)
        if self.parameters.curve:
            # Unknown speed but device on: median learned stage is more honest
            # than max power; confidence is reduced.
            return float(median([y for _, y in self.parameters.curve]) or 0.0)
        return float(self.parameters.on_power_w or 0.0)

    def predict_detailed(self, context: Any) -> tuple[float, str | None, StageStatus]:
        value, _ = _speed(context)
        power = self.predict(context)
        if value is None or not self.parameters.curve:
            return power, None, StageStatus.UNKNOWN
        # Only claim a stage if the speed maps to a learned stage within 10 %.
        nearest = min(self.parameters.curve, key=lambda pair: abs(pair[0] - value))
        if abs(nearest[0] - value) <= 10.0:
            for stage in self.parameters.stages:
                notes = " ".join(stage.notes)
                if f"control_value:{nearest[0]:g}" in notes:
                    return power, stage.name, StageStatus.IDENTIFIED
            return power, f"level_{nearest[0]:g}", StageStatus.PROBABLE
        return power, None, StageStatus.UNKNOWN

    def confidence(self, context: Any | None = None) -> float:
        base = super().confidence(context)
        if not self.parameters.curve:
            return min(base, 0.2)
        value, _ = _speed(context) if context is not None else (None, None)
        if value is None:
            return max(0.0, min(1.0, base * 0.5))
        values = [x for x, _ in self.parameters.curve]
        if value < min(values) or value > max(values):
            base *= 0.7
        return max(0.0, min(1.0, base))

    def explanation(self, context: Any) -> list[str]:
        value, name = _speed(context)
        parts = super().explanation(context) + ["speed_power_curve"]
        parts.append(f"{name or 'speed'}:{value:g}" if value is not None else "speed_missing_fallback_median")
        return parts

    def describe(self) -> dict[str, Any]:
        data = super().describe()
        data["notes"] = dict(getattr(self, "notes", {}))
        return data
