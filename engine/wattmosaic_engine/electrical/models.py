"""Device-model adapters and influence bounds.

Agent F owns ``ModelRegistry`` / ``ModelEstimate`` (see
``docs/interface_contracts.md`` §3.5).  This module never imports that package
(so the electrical/inference layer stays testable in isolation and cannot
create an import cycle).  Instead every model access goes through
:func:`resolve_model`, which understands:

* the contract objects ``DeviceModelVersion`` / ``DeviceModelParameters``;
* any ``ModelRegistry`` exposing ``active(device_id)`` and/or
  ``estimate(device_id, context)``;
* a plain ``Mapping[str, DeviceModelVersion]``;
* a plain mapping of ``device_id -> watts`` (test convenience).

The result is a small immutable :class:`ModelView` that the inference engine
uses for prior power, expected on/off behaviour, upper bounds and validation
quality.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from wattmosaic_engine.contracts import (
    DeviceModelParameters,
    DeviceModelVersion,
    ModelStatus,
    ModelType,
)

#: Default factor applied to a learnt operating power when deriving an upper
#: bound.  1.35 allows a legitimate overload / start-up surge without letting
#: the solver invent an arbitrarily large value for an unobserved device.
DEFAULT_UPPER_BOUND_FACTOR = 1.35

#: Absolute cap applied when a model gives an upper bound but no power level.
ABSOLUTE_DEVICE_CAP_W = 30_000.0


@dataclass(frozen=True, slots=True)
class ModelView:
    device_id: str
    model_id: str | None = None
    model_type: ModelType | None = None
    version: int | None = None
    status: ModelStatus | None = None
    params: DeviceModelParameters = field(default_factory=DeviceModelParameters)
    validation_quality: float = 0.0
    confidence: float = 0.0
    estimated_power_w: float | None = None
    expected_on: bool | None = None
    stage: str | None = None
    stage_status: str | None = None
    upper_bound_w: float | None = None
    sample_count: int = 0
    reasons: tuple[str, ...] = ()

    @property
    def active(self) -> bool:
        return self.status in (ModelStatus.ACTIVE, ModelStatus.FROZEN)

    @property
    def quality(self) -> float:
        """Blend of validation metrics and model self-confidence in [0,1]."""
        q = self.validation_quality
        if self.confidence > 0:
            q = 0.65 * q + 0.35 * self.confidence
        return min(max(q, 0.0), 1.0)

    def power_for_state(self, state: str | None) -> float | None:
        """Expected watts for a HA state, using the model's parameter set."""
        return expected_power_w(self.params, state)


def _finite(value: Any) -> float | None:
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    return v


def _validation_quality(metrics: Mapping[str, Any]) -> float:
    """Map validation metrics to an engineering quality score in [0,1].

    Priority: explicit ``quality``/``r2`` metric, else ``mae_w`` relative to
    the device's own power level (``1 - mae/level``), else 0.
    """
    if not metrics:
        return 0.0
    for key in ("quality", "quality_score", "score"):
        v = _finite(metrics.get(key))
        if v is not None:
            return min(max(v, 0.0), 1.0)
    for key in ("r2", "r_squared", "validation_r2"):
        v = _finite(metrics.get(key))
        if v is not None:
            return min(max(v, 0.0), 1.0)
    mae = _finite(metrics.get("mae_w", metrics.get("mae")))
    level = _finite(metrics.get("mean_power_w", metrics.get("mean_w")))
    if mae is not None and level and level > 0:
        return min(max(1.0 - mae / level, 0.0), 1.0)
    if mae is not None:
        return min(max(1.0 - mae / 100.0, 0.0), 1.0)
    return 0.0


def _quality_from_params(params: DeviceModelParameters, metrics: Mapping[str, Any]) -> float:
    q = _validation_quality(metrics)
    if q == 0.0:
        # Fall back to the residual spread when available.
        mean = params.residual_mean_w
        std = params.residual_std_w
        if std is not None and std >= 0:
            scale = max(abs(mean or 0.0), 100.0)
            q = min(max(1.0 - std / scale, 0.0), 1.0)
    return q


def expected_power_w(params: DeviceModelParameters, state: str | None) -> float | None:
    """Parametric power estimate for a HA state string.

    Order of precedence:
    multi-state map -> dimmable/variable curve (identity control value) ->
    running/on states -> off / standby.
    """
    normalized = (state or "").strip().lower()
    if normalized in params.state_map:
        return _finite(params.state_map[normalized])
    for key, value in params.state_map.items():
        if key.strip().lower() == normalized:
            return _finite(value)
    if params.curve and normalized:
        # Curve is (control_value, watts); accept a numeric state.
        try:
            control = float(normalized)
        except ValueError:
            control = None
        if control is not None:
            pts = sorted((float(a), float(b)) for a, b in params.curve)
            if pts:
                if control <= pts[0][0]:
                    return pts[0][1]
                if control >= pts[-1][0]:
                    return pts[-1][1]
                for (x0, y0), (x1, y1) in zip(pts, pts[1:], strict=False):
                    if x0 <= control <= x1:
                        if x1 == x0:
                            return y1
                        frac = (control - x0) / (x1 - x0)
                        return y0 + frac * (y1 - y0)
    on_states = {"on", "running", "heating", "cooling", "active", "open", "playing", "washing"}
    off_states = {"off", "idle", "standby", "closed", "paused", "unavailable", "unknown"}
    if normalized in on_states:
        return _finite(params.on_power_w if params.on_power_w is not None else params.standby_power_w)
    if normalized in off_states:
        if normalized == "standby":
            return _finite(params.standby_power_w)
        return _finite(params.off_power_w if params.off_power_w is not None else params.standby_power_w)
    # Unknown state string: use an "on" model only if one exists, but never
    # the standby value as if it were an active load.
    return _finite(params.on_power_w)


def _view_from_version(device_id: str, model: DeviceModelVersion) -> ModelView:
    params = model.parameters
    upper = None
    level = params.on_power_w or params.standby_power_w
    if level is not None and level > 0:
        upper = float(level) * DEFAULT_UPPER_BOUND_FACTOR
    if params.stages:
        stage_max = max((s.power_mean_w + 3 * s.power_std_w) for s in params.stages)
        upper = max(upper or 0.0, stage_max * DEFAULT_UPPER_BOUND_FACTOR) or None
    if params.curve:
        upper = max(upper or 0.0, max(float(v) for _, v in params.curve) * DEFAULT_UPPER_BOUND_FACTOR) or None
    if upper is not None:
        upper = min(upper, ABSOLUTE_DEVICE_CAP_W)
    return ModelView(
        device_id=device_id,
        model_id=f"{model.model_type}_model",
        model_type=ModelType(model.model_type),
        version=model.version,
        status=ModelStatus(model.status),
        params=params,
        validation_quality=_quality_from_params(params, model.validation_metrics),
        confidence=float(model.confidence),
        estimated_power_w=None if level is None else float(level),
        upper_bound_w=upper,
        sample_count=int(model.sample_count or 0),
        reasons=tuple(model.notes),
    )


def _view_from_model_object(device_id: str, model: Any) -> ModelView:
    """Handle any object that looks like a model / estimate."""
    if isinstance(model, DeviceModelVersion):
        return _view_from_version(device_id, model)
    if isinstance(model, (int, float)) and not isinstance(model, bool):
        value = _finite(model)
        return ModelView(
            device_id=device_id,
            status=ModelStatus.ACTIVE,
            estimated_power_w=value,
            upper_bound_w=None if value is None else min(value * DEFAULT_UPPER_BOUND_FACTOR, ABSOLUTE_DEVICE_CAP_W),
        )
    params = getattr(model, "parameters", getattr(model, "params", None))
    if isinstance(params, Mapping):
        params = DeviceModelParameters(**dict(params))
    if not isinstance(params, DeviceModelParameters):
        params = DeviceModelParameters()
    metrics = getattr(model, "validation_metrics", {}) or {}
    if not isinstance(metrics, Mapping):
        metrics = {}
    level = getattr(model, "power_w", getattr(model, "estimated_power_w", None))
    level = _finite(level)
    if level is None:
        level = _finite(params.on_power_w)
    upper = getattr(model, "upper_bound_w", None)
    upper = _finite(upper)
    if upper is None and level is not None:
        upper = min(level * DEFAULT_UPPER_BOUND_FACTOR, ABSOLUTE_DEVICE_CAP_W)
    status = getattr(model, "status", getattr(model, "model_status", None))
    try:
        status_enum = ModelStatus(status) if status is not None else None
    except ValueError:
        status_enum = None
    model_type = getattr(model, "model_type", None)
    try:
        type_enum = ModelType(model_type) if model_type is not None else None
    except ValueError:
        type_enum = None
    return ModelView(
        device_id=device_id,
        model_id=getattr(model, "model_id", None),
        model_type=type_enum,
        version=getattr(model, "version", None),
        status=status_enum,
        params=params,
        validation_quality=_quality_from_params(params, metrics),
        confidence=float(getattr(model, "confidence", 0.0) or 0.0),
        estimated_power_w=level,
        expected_on=getattr(model, "expected_on", None),
        stage=getattr(model, "stage", getattr(model, "operating_stage", None)),
        stage_status=getattr(model, "stage_status", None),
        upper_bound_w=upper,
        sample_count=int(getattr(model, "sample_count", 0) or 0),
    )


class _NullRegistry:
    def active(self, device_id: str) -> None:  # pragma: no cover - trivial
        return None

    def estimate(self, device_id: str, context: Any = None) -> None:  # pragma: no cover
        return None


_NULL = _NullRegistry()


def resolve_model(
    models: Any,
    device_id: str,
    *,
    state: str | None = None,
    context: Any = None,
) -> ModelView | None:
    """Best-effort normalisation of ``models`` for one device.

    Returns ``None`` when no usable model is available.  Never raises because
    of a foreign model object: per-device error isolation is a hard requirement.
    """
    if models is None:
        return None
    try:
        if isinstance(models, Mapping):
            raw = models.get(device_id)
            return None if raw is None else _view_from_model_object(device_id, raw)

        estimator = getattr(models, "estimate", None)
        if callable(estimator):
            try:
                est = estimator(device_id, context) if context is not None else estimator(device_id)
            except TypeError:
                est = estimator(device_id)
            if est is not None:
                view = _view_from_model_object(device_id, est)
                if view.estimated_power_w is None and state is not None:
                    view = _with_state_power(view, state)
                return view

        active = getattr(models, "active", None)
        if callable(active):
            raw = active(device_id)
            if raw is not None:
                return _with_state_power(_view_from_model_object(device_id, raw), state)

        getter = getattr(models, "get", None)
        if callable(getter):
            raw = getter(device_id)
            if raw is not None:
                return _with_state_power(_view_from_model_object(device_id, raw), state)
    except Exception:  # pragma: no cover - defensive isolation
        return None
    return None


def _with_state_power(view: ModelView, state: str | None) -> ModelView:
    if state is None:
        return view
    expected = view.power_for_state(state)
    if expected is None:
        return view
    import dataclasses

    return dataclasses.replace(view, estimated_power_w=expected)


def model_upper_bounds(
    models: Any,
    device_ids: list[str],
    *,
    states: Mapping[str, str] | None = None,
    context: Any = None,
    factor: float = DEFAULT_UPPER_BOUND_FACTOR,
) -> tuple[list[float | None], dict[str, ModelView]]:
    """Per-device upper bounds for the solver, plus resolved model views."""
    bounds: list[float | None] = []
    views: dict[str, ModelView] = {}
    states = states or {}
    for did in device_ids:
        view = resolve_model(models, did, state=states.get(did), context=context)
        if view is None:
            bounds.append(None)
            continue
        views[did] = view
        upper = view.upper_bound_w
        if upper is not None and factor != 1.0:
            upper = min(upper * factor, ABSOLUTE_DEVICE_CAP_W)
        bounds.append(upper)
    return bounds, views
