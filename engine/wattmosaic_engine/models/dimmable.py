"""Dimmable-device model: control value → power curve with interpolation."""

from __future__ import annotations

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

#: brightness controls are often 0-255 while some integrations expose 0-100 %.
_BRIGHTNESS_ATTRS = ("brightness", "brightness_pct", "level", "dimmer", "percent")


def _brightness(context: Any) -> tuple[float | None, str | None]:
    value, name = context_control(context, *_BRIGHTNESS_ATTRS)
    if value is None:
        return None, None
    return value, name


class DimmableDeviceModel(DeviceModel):
    """Brightness/level → watts lookup with linear interpolation.

    * The fitted curve is sorted by control value and enforced monotonic (PAVA),
      because a dimmer cannot consume less power at a higher setting in steady
      state.
    * ``predict`` linearly interpolates between learned points and clamps beyond
      the learned range (never extrapolates wildly).
    * A device reported ``off`` yields its learned OFF/standby power; a device
      reported ``on`` with no brightness attribute yields the mean of the curve
      (flagged lower confidence).
    """

    model_type = ModelType.DIMMABLE
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
            if control is not None and variable in _BRIGHTNESS_ATTRS:
                if power is not None:
                    points.append((control, power))
                variables.append(variable)
            elif (
                variable not in _BRIGHTNESS_ATTRS
                and power is not None
                and sample.delta_power_w < 0
            ):
                # No perceived control: treat as an ON sample for the fallback.
                off_values.append(power)
        curve = enforce_monotonic(points)
        on_power = median([y for _, y in curve]) if curve else None
        off_power = median(off_values) if off_values else None
        residuals: list[float] = []
        for x, y in points:
            from .base import _linear_interp  # local import keeps helper private-ish

            predicted = _linear_interp(x, curve)
            if predicted is not None:
                residuals.append(y - predicted)
        parameters = DeviceModelParameters(
            curve=curve,
            on_power_w=on_power,
            off_power_w=off_power,
            residual_mean_w=(sum(residuals) / len(residuals)) if residuals else None,
            residual_std_w=stdev(residuals) if residuals else None,
            stages=self._stages_from_curve(curve),
        )
        self.control_variable = variables[0] if variables else "brightness"
        self.notes = {
            "curve_points": len(curve),
            "control_variable": self.control_variable,
            "monotonic": all(
                curve[i][1] <= curve[i + 1][1] + 1e-9 for i in range(len(curve) - 1)
            ),
        }
        return self._finish_fit(parameters)

    # -- control normalisation ------------------------------------------- #
    def _normalise_control(self, value: float, variable: str | None) -> float:
        """Convert a context control value into the units used at fit time.

        Most HA lights expose ``brightness`` in 0-255 *and* integrations may
        expose ``brightness_pct`` in 0-100.  If the model was trained on one and
        queried with the other we convert rather than mis-scale by 2.55×.
        """
        trained = getattr(self, "control_variable", None) or "brightness"
        if variable is None or variable == trained:
            return value
        if "pct" in variable and "pct" not in trained:
            return value * 2.55
        if "pct" not in variable and "pct" in trained:
            return value / 2.55
        return value

    def predict(self, context: Any) -> float:
        state = context_state(context)
        on_flag = is_on(state)
        value, name = _brightness(context)
        from .base import _linear_interp

        if value is not None:
            x = self._normalise_control(value, name)
            if x <= 1.0 and on_flag is False:
                return float(self.parameters.off_power_w or 0.0)
            predicted = _linear_interp(x, self.parameters.curve)
            if predicted is not None:
                return float(predicted)
        if on_flag is False:
            return float(self.parameters.off_power_w or 0.0)
        if self.parameters.curve:
            return float(median([y for _, y in self.parameters.curve]) or 0.0)
        return float(self.parameters.on_power_w or 0.0)

    def predict_detailed(self, context: Any) -> tuple[float, str | None, StageStatus]:
        value, _ = _brightness(context)
        power = self.predict(context)
        if value is None:
            return power, None, StageStatus.UNKNOWN
        curve = self.parameters.curve
        if len(curve) < 2:
            return power, None, StageStatus.UNKNOWN
        # Stage = nearest learned control point.
        nearest = min(curve, key=lambda pair: abs(pair[0] - value))
        return power, f"level_{nearest[0]:g}", StageStatus.IDENTIFIED

    def confidence(self, context: Any | None = None) -> float:
        base = super().confidence(context)
        value, _ = _brightness(context) if context is not None else (None, None)
        if not self.parameters.curve:
            return min(base, 0.2)
        if value is None:
            base *= 0.5  # brightness unknown: fallback to mean curve power
            return max(0.0, min(1.0, base))
        points = [x for x, _ in self.parameters.curve]
        if value < min(points) or value > max(points):
            base *= 0.75  # clamped / extrapolated
        elif len(points) >= 2:
            gap = min(abs(value - x) for x in points)
            span = max(points) - min(points) or 1.0
            base *= 1.0 - 0.35 * min(1.0, gap / span)
        return max(0.0, min(1.0, base))

    def explanation(self, context: Any) -> list[str]:
        value, name = _brightness(context)
        parts = super().explanation(context) + ["brightness_curve_interpolation"]
        if value is not None:
            parts.append(f"{name or 'brightness'}:{value:g}")
        else:
            parts.append("brightness_missing_fallback_mean")
        return parts

    def describe(self) -> dict[str, Any]:
        data = super().describe()
        data["notes"] = dict(getattr(self, "notes", {}))
        return data

    def _stages_from_curve(self, curve: Sequence[tuple[float, float]]) -> list[OperatingStage]:
        return [
            OperatingStage(
                name=f"level_{x:g}",
                status=StageStatus.IDENTIFIED if len(curve) >= 2 else StageStatus.PROBABLE,
                power_mean_w=float(y),
                notes=[f"control_value:{x:g}"],
            )
            for x, y in curve
        ]
