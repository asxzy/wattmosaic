"""Common device-model framework.

A device model is a small, explainable estimator:

``fit(samples) -> DeviceModelParameters``
    Learn parameters from accepted :class:`~wattmosaic_engine.contracts.LearningSample`
    objects.

``predict(context) -> float``
    Return estimated watts for a duck-typed context (HA state + attributes, a
    :class:`~wattmosaic_engine.models.protocols.SimpleWindow`, any mapping).

``validate(samples) -> dict[str, float]``
    Return held-out style error metrics (``mae``, ``rmse``, ``r2``, ...).

``describe() -> dict``
    Human-readable model metadata, used by diagnostics and docs.

All model classes are deliberately dependency-free (stdlib + contracts) so the
learning pipeline can be tested without the electrical solver.
"""

from __future__ import annotations

import math
import statistics
from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from typing import Any

from ..contracts import (
    DeviceModelParameters,
    LearningSample,
    ModelType,
    OperatingStage,
    StageStatus,
)
from .protocols import (
    attribute_value,
    context_power_series,
    context_state,
    context_timestamp,
    context_value,
    normalise_series,
)

# --------------------------------------------------------------------------- #
# Numeric helpers
# --------------------------------------------------------------------------- #

ON_STATES_DEFAULT = frozenset({"on", "open", "running", "active", "heating", "cooling", "true", "1"})
OFF_STATES_DEFAULT = frozenset({"off", "closed", "idle", "standby", "sleep", "inactive", "false", "0"})


def enum_text(value: Any) -> str:
    """Return the string value of an enum or plain string.

    The contracts use ``use_enum_values=True``, so fields that are typed as
    enums actually hold ``str`` at runtime.  Never call ``.value`` directly.
    """
    return str(value.value if hasattr(value, "value") else value)


def finite(value: Any) -> float | None:
    """Return ``value`` as a finite float, or ``None``."""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(out) or math.isinf(out):
        return None
    return out


def median(values: Iterable[float], default: float | None = None) -> float | None:
    clean = [v for v in (finite(x) for x in values) if v is not None]
    if not clean:
        return default
    return float(statistics.median(clean))


def quantile(values: Iterable[float], q: float, default: float | None = None) -> float | None:
    clean = sorted(v for v in (finite(x) for x in values) if v is not None)
    if not clean:
        return default
    if len(clean) == 1:
        return clean[0]
    q = min(1.0, max(0.0, q))
    pos = q * (len(clean) - 1)
    low = math.floor(pos)
    high = math.ceil(pos)
    if low == high:
        return clean[low]
    frac = pos - low
    return clean[low] * (1.0 - frac) + clean[high] * frac


def mean(values: Iterable[float], default: float | None = None) -> float | None:
    clean = [v for v in (finite(x) for x in values) if v is not None]
    if not clean:
        return default
    return float(sum(clean) / len(clean))


def stdev(values: Iterable[float], default: float = 0.0) -> float:
    clean = [v for v in (finite(x) for x in values) if v is not None]
    if len(clean) < 2:
        return default
    return float(statistics.pstdev(clean))


def robust_sigma(values: Iterable[float], default: float = 0.0) -> float:
    """1.4826 * MAD — resistant to a few bad readings."""
    clean = [v for v in (finite(x) for x in values) if v is not None]
    if not clean:
        return default
    med = statistics.median(clean)
    return float(1.4826 * statistics.median([abs(v - med) for v in clean]))


def _linear_interp(x: float, curve: Sequence[tuple[float, float]]) -> float | None:
    if not curve:
        return None
    pts = sorted((float(a), float(b)) for a, b in curve)
    if x <= pts[0][0]:
        return pts[0][1]
    if x >= pts[-1][0]:
        return pts[-1][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:], strict=False):
        if x0 <= x <= x1:
            if x1 == x0:
                return y1
            frac = (x - x0) / (x1 - x0)
            return y0 + frac * (y1 - y0)
    return pts[-1][1]


def enforce_monotonic(curve: Sequence[tuple[float, float]]) -> list[tuple[float, float]]:
    """Sort by control value and make watts non-decreasing (PAVA).

    Physical power must not *decrease* when a dimmer/speed control increases.
    Pool-adjacent-violators keeps the fitted values as close as possible to the
    observations while guaranteeing monotonicity.
    """
    pts = sorted((float(a), float(b)) for a, b in curve if finite(a) is not None and finite(b) is not None)
    if not pts:
        return []
    # collapse duplicate x (median y)
    collapsed: list[list[float]] = []
    for x, y in pts:
        if collapsed and abs(collapsed[-1][0] - x) < 1e-9:
            collapsed[-1][1] = (collapsed[-1][1] + y) / 2.0
        else:
            collapsed.append([x, y])
    # PAVA over y
    blocks: list[list[Any]] = [[y, 1, [x]] for x, y in collapsed]
    i = 0
    while i < len(blocks) - 1:
        if blocks[i][0] > blocks[i + 1][0] + 1e-12:
            total_weight = blocks[i][1] + blocks[i + 1][1]
            weighted = blocks[i][0] * blocks[i][1] + blocks[i + 1][0] * blocks[i + 1][1]
            blocks[i] = [weighted / total_weight, total_weight, blocks[i][2] + blocks[i + 1][2]]
            blocks.pop(i + 1)
            if i > 0:
                i -= 1
        else:
            i += 1
    out: list[tuple[float, float]] = []
    for value, _weight, xs in blocks:
        for x in xs:
            out.append((float(x), float(value)))
    out.sort(key=lambda p: p[0])
    return out


# --------------------------------------------------------------------------- #
# Metric helpers
# --------------------------------------------------------------------------- #


def error_metrics(actual: Sequence[float], predicted: Sequence[float]) -> dict[str, float]:
    """MAE / RMSE / R² / MAPE for two equal-length sequences."""
    pairs = [
        (float(a), float(p))
        for a, p in zip(actual, predicted, strict=True)
        if finite(a) is not None and finite(p) is not None
    ]
    n = len(pairs)
    if n == 0:
        return {"n": 0.0, "mae": 0.0, "rmse": 0.0, "r2": 0.0, "mape": 0.0, "max_error": 0.0}
    errors = [p - a for a, p in pairs]
    abs_errors = [abs(e) for e in errors]
    mae = sum(abs_errors) / n
    rmse = math.sqrt(sum(e * e for e in errors) / n)
    mean_actual = sum(a for a, _ in pairs) / n
    ss_tot = sum((a - mean_actual) ** 2 for a, _ in pairs)
    ss_res = sum(e * e for e in errors)
    r2 = (1.0 if ss_res <= 1e-12 else 0.0) if ss_tot <= 1e-12 else 1.0 - ss_res / ss_tot
    nonzero = [abs(a) for a, _ in pairs if abs(a) > 1e-9]
    if nonzero:
        mape = sum(abs(e) / a for e, (a, _) in zip(errors, pairs, strict=True) if abs(a) > 1e-9) / len(nonzero) * 100.0
    else:
        mape = 0.0
    return {
        "n": float(n),
        "mae": float(mae),
        "rmse": float(rmse),
        "r2": float(r2),
        "mape": float(mape),
        "max_error": float(max(abs_errors)),
    }


def sample_actual_w(sample: LearningSample) -> float:
    """Ground truth for validation of one sample.

    ``event_power_w`` is the robust post-event power.  For a state transition to
    ON that is the ON power; for a transition to OFF it is the OFF/standby
    power; for a brightness/speed sample it is the power at that setting.
    """
    return float(sample.event_power_w)


def context_from_sample(sample: LearningSample, *, state: str | None = None) -> dict[str, Any]:
    """Reconstruct a prediction context from a stored learning sample.

    ``notes`` may carry ``state:<name>`` (recorded by the collector) or a
    ``component:<name>`` tag used by composite models.
    """
    attributes: dict[str, Any] = {}
    notes = list(sample.notes)
    resolved_state = state
    component: str | None = None
    hour_hint: int | None = None
    for note in notes:
        text = str(note)
        if text.startswith(("state:", "ha_state:")):
            resolved_state = text.split(":", 1)[1]
        elif text.startswith("component:"):
            component = text.split(":", 1)[1]
        elif text.startswith("hour:"):
            try:
                hour_hint = int(text.split(":", 1)[1])
            except ValueError:
                hour_hint = None
    if resolved_state is None:
        if sample.delta_power_w > 0:
            resolved_state = "on"
        elif sample.delta_power_w < 0:
            resolved_state = "off"
    variable = (sample.control_variable or "").strip().lower()
    if sample.control_value is not None:
        if variable in ("brightness", "brightness_pct"):
            key = "brightness" if variable == "brightness" else "brightness_pct"
            attributes[key] = sample.control_value
        elif variable in ("speed", "percentage", "fan_speed", "speed_pct", "level", "rpm"):
            attributes[variable] = sample.control_value
        elif variable in ("state", "mode", "operating_mode"):
            if resolved_state is None:
                resolved_state = str(sample.control_value)
        else:
            attributes[variable or "control_value"] = sample.control_value
    ctx: dict[str, Any] = {
        "device_id": sample.device_id,
        "state": resolved_state,
        "attributes": attributes,
        "captured_at": sample.captured_at,
        "timestamp": sample.captured_at,
        "control_value": sample.control_value,
        "control_variable": sample.control_variable,
        "hour": sample.captured_at.hour if hour_hint is None else hour_hint,
    }
    if component:
        ctx["component"] = component
    return ctx


def context_control(context: Any, *names: str) -> tuple[float | None, str | None]:
    """Read a numeric control value from attributes or top level."""
    for name in names:
        value = finite(attribute_value(context, name))
        if value is not None:
            return value, name
    value = finite(context_value(context, "control_value"))
    variable = context_value(context, "control_variable")
    if value is not None:
        return value, str(variable) if variable else "control_value"
    return None, None


def context_numeric(context: Any, *names: str) -> float | None:
    for name in names:
        value = finite(attribute_value(context, name))
        if value is not None:
            return value
    return None


def is_on(state: str | None, on_states: frozenset[str] = ON_STATES_DEFAULT) -> bool | None:
    if state is None:
        return None
    text = state.strip().lower()
    if text in on_states:
        return True
    if text in OFF_STATES_DEFAULT:
        return False
    return None


# --------------------------------------------------------------------------- #
# Base model
# --------------------------------------------------------------------------- #


class DeviceModel(ABC):
    """Abstract base for all device models."""

    model_type: ModelType = ModelType.RESIDUAL
    #: minimum accepted samples before ``fit`` can produce a meaningful model
    minimum_samples: int = 3

    def __init__(
        self,
        parameters: DeviceModelParameters | None = None,
        *,
        validation_metrics: dict[str, float] | None = None,
        confidence: float | None = None,
        name: str | None = None,
    ) -> None:
        self.parameters = parameters if parameters is not None else DeviceModelParameters()
        self.validation_metrics: dict[str, float] = dict(validation_metrics or {})
        self.model_confidence = confidence
        self.name = name or self.__class__.__name__

    # -- required API ----------------------------------------------------- #
    @abstractmethod
    def fit(self, samples: Sequence[LearningSample], **kwargs: Any) -> DeviceModelParameters:
        """Learn parameters from accepted samples and store them on ``self``."""

    @abstractmethod
    def predict(self, context: Any) -> float:
        """Estimate device power in watts for ``context``."""

    def predict_detailed(self, context: Any) -> tuple[float, str | None, StageStatus]:
        """Return ``(power_w, stage_name, stage_status)``."""
        return self.predict(context), None, StageStatus.UNKNOWN

    def validate(self, samples: Sequence[LearningSample]) -> dict[str, float]:
        if not samples:
            return error_metrics([], [])
        actual: list[float] = []
        predicted: list[float] = []
        for sample in samples:
            try:
                predicted.append(float(self.predict(context_from_sample(sample))))
            except Exception:
                continue
            actual.append(sample_actual_w(sample))
        metrics = error_metrics(actual, predicted)
        metrics["sample_count"] = float(len(samples))
        return metrics

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "model_type": enum_text(self.model_type),
            "minimum_samples": self.minimum_samples,
            "validation_metrics": dict(self.validation_metrics),
            "confidence": self.confidence(),
            "parameters": self.parameters.model_dump(mode="json"),
        }

    # -- optional hooks --------------------------------------------------- #
    def confidence(self, context: Any | None = None) -> float:
        """Engineering confidence in ``[0, 1]`` (not a calibrated probability)."""
        if self.model_confidence is not None:
            base = float(self.model_confidence)
        else:
            r2 = self.validation_metrics.get("r2")
            base = 0.5 if r2 is None else 0.25 + 0.75 * max(0.0, min(1.0, r2))
        return max(0.0, min(1.0, base))

    def explanation(self, context: Any) -> list[str]:
        return [f"model:{enum_text(self.model_type)}", f"model_name:{self.name}"]

    def uncertainty_w(self, context: Any | None = None) -> float:
        """One-sigma prediction uncertainty in watts (engineering estimate)."""
        rmse = self.validation_metrics.get("rmse")
        if rmse is not None:
            return float(rmse)
        residual_std = self.parameters.residual_std_w
        return float(residual_std) if residual_std is not None else 0.0

    # -- helpers ---------------------------------------------------------- #
    def _finish_fit(self, parameters: DeviceModelParameters) -> DeviceModelParameters:
        self.parameters = parameters
        return parameters

    def _stages_from_curve(self, curve: Sequence[tuple[float, float]]) -> list[OperatingStage]:
        stages: list[OperatingStage] = []
        for x, y in curve:
            stages.append(
                OperatingStage(
                    name=f"level_{x:g}",
                    status=StageStatus.IDENTIFIED,
                    power_mean_w=float(y),
                    notes=[f"control_value:{x:g}"],
                )
            )
        return stages


def model_class_for(model_type: ModelType | str) -> type[DeviceModel]:
    """Resolve a :class:`ModelType` to its concrete class (lazy import)."""
    from .binary import BinaryDeviceModel
    from .composite import CompositeDeviceModel
    from .dimmable import DimmableDeviceModel
    from .multi_state import MultiStateDeviceModel
    from .residual import ResidualDeviceModel
    from .time_varying import TimeVaryingApplianceModel
    from .variable_speed import VariableSpeedDeviceModel

    key = enum_text(model_type) if isinstance(model_type, ModelType) else str(model_type)
    mapping: dict[str, type[DeviceModel]] = {
        ModelType.BINARY.value: BinaryDeviceModel,
        ModelType.DIMMABLE.value: DimmableDeviceModel,
        ModelType.VARIABLE_SPEED.value: VariableSpeedDeviceModel,
        ModelType.MULTI_STATE.value: MultiStateDeviceModel,
        ModelType.COMPOSITE.value: CompositeDeviceModel,
        ModelType.TIME_VARYING.value: TimeVaryingApplianceModel,
        ModelType.RESIDUAL.value: ResidualDeviceModel,
    }
    try:
        return mapping[key]
    except KeyError as exc:
        raise ValueError(f"unknown model type: {model_type!r}") from exc


def build_model(
    model_type: ModelType | str,
    parameters: DeviceModelParameters | None = None,
    *,
    validation_metrics: dict[str, float] | None = None,
    confidence: float | None = None,
) -> DeviceModel:
    cls = model_class_for(model_type)
    return cls(parameters, validation_metrics=validation_metrics, confidence=confidence)


__all__ = [
    "ON_STATES_DEFAULT",
    "OFF_STATES_DEFAULT",
    "DeviceModel",
    "build_model",
    "context_control",
    "context_from_sample",
    "context_numeric",
    "context_power_series",
    "context_state",
    "context_timestamp",
    "enforce_monotonic",
    "error_metrics",
    "finite",
    "is_on",
    "mean",
    "median",
    "model_class_for",
    "normalise_series",
    "quantile",
    "robust_sigma",
    "sample_actual_w",
    "stdev",
]
