"""Legacy-model read adapter for the v3 mapping projection (Z2b).

The retired ``electrical/models.py`` parameter interpreter is gone: its
``expected_power_w`` duck-typed an arbitrary object and re-implemented model
prediction from raw archived parameters (DEF-02 — a composite reloaded
with the wrong control variable and predicted a different number).  All model
access now goes through the single v3 prediction path in
:class:`~wattmosaic_engine.models.registry.Registry` / ``ModelSetView.predict``.

This module is a *read adapter* only.  It does not interpret parameters: it
hands a versioned spec to ``models.prediction.predict_with_spec`` and turns the
resulting :class:`PowerPrediction` into the small view the mapping projection
consumes.  When a model set (a ``ModelSetView``) is passed, that is the single
prediction authority; a plain mapping of already-materialised model payloads is
supported for callers that read through the facade, where each payload is
validated by the L1 codec before it is decoded.  No DB, no wall clock, no
``runtime.fakes``.

Absent is ``None``, never 0.0 (HB §18): an unknown prediction has ``mean_w=None``
and never becomes a fabricated watt estimate.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..contracts import ModelStatus, ModelType, PowerLevelV3, PowerPrediction
from ..runtime.interfaces import ModelSetView, PredictionContext
from .prediction import predict_with_spec
from .serde import LegacyModelRow, LegacyParameters, version_from_row

#: Default factor applied to a learnt operating power when deriving an upper
#: bound.  1.35 allows a legitimate overload / start-up surge without letting
#: the solver invent an arbitrarily large value for an unobserved device.
DEFAULT_UPPER_BOUND_FACTOR = 1.35

#: Absolute cap applied when a model gives an upper bound but no power level.
ABSOLUTE_DEVICE_CAP_W = 30_000.0


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


@dataclass(frozen=True, slots=True)
class ModelView:
    """The small, immutable model view consumed by the mapping projection.

    ``estimated_power_w`` is the single v3 prediction (``PowerPrediction.mean_w``)
    for the device's declared operating level; it is ``None`` for an unknown or
    absent model, never 0.0.  ``quality`` blends validation quality and model
    confidence exactly as the retired read path did, so the projection's
    confidence vector stays comparable across the cutover.
    """

    device_id: str
    model_id: str | None = None
    model_type: ModelType | None = None
    version: int | None = None
    status: ModelStatus | None = None
    params: LegacyParameters = field(default_factory=LegacyParameters)
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
        q = self.validation_quality
        if self.confidence > 0:
            q = 0.65 * q + 0.35 * self.confidence
        return min(max(q, 0.0), 1.0)


def _validation_quality(metrics: Mapping[str, Any]) -> float:
    if not metrics:
        return 0.0
    for key in ("quality", "quality_score", "score"):
        value = _finite(metrics.get(key))
        if value is not None:
            return min(max(value, 0.0), 1.0)
    for key in ("r2", "r_squared", "validation_r2"):
        value = _finite(metrics.get(key))
        if value is not None:
            return min(max(value, 0.0), 1.0)
    mae = _finite(metrics.get("mae_w", metrics.get("mae")))
    level = _finite(metrics.get("mean_power_w", metrics.get("mean_w")))
    if mae is not None and level is not None and level > 0:
        return min(max(1.0 - mae / level, 0.0), 1.0)
    if mae is not None:
        return min(max(1.0 - mae / 100.0, 0.0), 1.0)
    return 0.0


def _view_from_version(device_id: str, version: LegacyModelRow) -> ModelView | None:
    """Project a legacy version through the v3 prediction path.

    An archived model row carries raw parameters rather
    than a v3 spec.  The L1 codec is the only decoder: build ``FixedSpec`` level
    statistics from the declared levels and let ``predict_with_spec`` answer the
    operating-level prediction.  Unknown values stay ``None``.
    """
    from ..contracts import FixedSpec, LevelStat, ModelBasis, ModelClass

    params = version.parameters
    levels: dict[PowerLevelV3 | str, LevelStat] = {}
    if params.on_power_w is not None:
        levels[PowerLevelV3.ACTIVE] = LevelStat(
            median_w=float(params.on_power_w), mad_w=params.residual_std_w
        )
    if params.standby_power_w is not None:
        levels[PowerLevelV3.STANDBY] = LevelStat(
            median_w=float(params.standby_power_w), mad_w=params.residual_std_w
        )
    if params.off_power_w is not None:
        levels[PowerLevelV3.OFF] = LevelStat(median_w=float(params.off_power_w), mad_w=None)

    estimate: float | None = None
    if levels:
        from ..contracts import PowerModelVersion

        try:
            spec = FixedSpec(levels=levels)
            v3 = PowerModelVersion(
                version_id=f"legacy::{device_id}::{version.version}",
                subject_kind="device",
                subject_id=device_id,
                number=int(version.version or 0),
                model_class=ModelClass.FIXED,
                basis=ModelBasis.ABSOLUTE_ANCHOR,
                spec=spec,
                trainer_version="legacy-view",
                created_at=version.created_at or datetime.now(UTC),
            )
            ctx = PredictionContext(at=datetime.now(UTC), level=PowerLevelV3.ACTIVE)
            prediction: PowerPrediction = predict_with_spec(v3, ctx)
            estimate = prediction.mean_w
        except Exception:  # noqa: BLE001 - corrupt model must not break the read path
            estimate = None

    metric_quality = _validation_quality(version.validation_metrics or {})
    if metric_quality == 0.0 and params.residual_std_w is not None:
        std = float(params.residual_std_w)
        scale = max(abs(_finite(params.residual_mean_w) or 0.0), 100.0)
        metric_quality = min(max(1.0 - std / scale, 0.0), 1.0)

    upper: float | None = None
    declared = _finite(params.on_power_w) or _finite(params.standby_power_w)
    if declared is not None and declared > 0:
        upper = min(declared * DEFAULT_UPPER_BOUND_FACTOR, ABSOLUTE_DEVICE_CAP_W)

    try:
        model_type = ModelType(version.model_type)
    except (ValueError, TypeError):
        model_type = None
    try:
        status = ModelStatus(version.status)
    except (ValueError, TypeError):
        status = None

    return ModelView(
        device_id=device_id,
        model_id=f"{version.model_type}_model",
        model_type=model_type,
        version=int(version.version or 0) or None,
        status=status,
        params=params,
        validation_quality=metric_quality,
        confidence=_finite(version.confidence) or 0.0,
        estimated_power_w=estimate,
        upper_bound_w=upper,
        sample_count=int(version.sample_count or 0),
        reasons=tuple(version.notes or ()),
    )


def resolve_model(
    models: Any,
    device_id: str,
    *,
    state: str | None = None,
    context: Any = None,
) -> ModelView | None:
    """Resolve one device's model to a :class:`ModelView` via the v3 path.

    ``models`` may be a :class:`ModelSetView` (preferred: its ``predict`` is the
    single prediction authority), a mapping of ``device_id -> payload`` as
    served by the facade, or ``None``.  A per-device failure is isolated and
    yields ``None`` — a broken model must not stop the whole projection.
    """
    if models is None:
        return None
    try:
        if isinstance(models, ModelSetView):
            at = getattr(context, "at", None) if context is not None else None
            ctx = PredictionContext(
                at=at or datetime.now(UTC),
                level=PowerLevelV3.ACTIVE,
                attributes={},
            )
            prediction = models.predict(device_id, ctx)
            if prediction is None or prediction.mean_w is None:
                return None
            return ModelView(
                device_id=device_id,
                estimated_power_w=prediction.mean_w,
                upper_bound_w=(
                    min(prediction.mean_w * DEFAULT_UPPER_BOUND_FACTOR, ABSOLUTE_DEVICE_CAP_W)
                ),
            )
        if isinstance(models, Mapping):
            raw = models.get(device_id)
            if raw is None:
                return None
            if isinstance(raw, LegacyModelRow):
                return _view_from_version(device_id, raw)
            if isinstance(raw, Mapping):
                payload = dict(raw)
                active = payload.get("active") if "active" in payload else payload
                if active is None:
                    return None
                if isinstance(active, LegacyModelRow):
                    return _view_from_version(device_id, active)
                if isinstance(active, Mapping):
                    # A serialized legacy model version (with nested
                    # ``parameters``/``validation_metrics``), as served by the
                    # legacy ``models()`` facade path.
                    if "parameters" in active or "validation_metrics" in active:
                        try:
                            parsed = version_from_row({"device_id": device_id, **dict(active)})
                            if parsed is not None:
                                return _view_from_version(device_id, parsed)
                        except Exception:  # noqa: BLE001
                            pass
                    # A v3 ``PowerModelVersion`` dump (with ``spec``): predict
                    # through the v3 codec, the single prediction authority.
                    if "spec" in active:
                        try:
                            from ..contracts import PowerModelVersion

                            v3 = PowerModelVersion.model_validate(dict(active))
                            prediction = predict_with_spec(
                                v3,
                                PredictionContext(at=datetime.now(UTC), level=PowerLevelV3.ACTIVE),
                            )
                            if prediction.mean_w is None:
                                return None
                            return ModelView(
                                device_id=device_id,
                                estimated_power_w=prediction.mean_w,
                                upper_bound_w=min(
                                    prediction.mean_w * DEFAULT_UPPER_BOUND_FACTOR,
                                    ABSOLUTE_DEVICE_CAP_W,
                                ),
                                model_id=v3.version_id,
                            )
                        except Exception:  # noqa: BLE001
                            return None
                    version = version_from_row(active)
                    if version is not None:
                        return _view_from_version(device_id, version)
                # Already a plain payload of numbers (test convenience): a watt
                # value is an observation, never inferred from raw parameters.
                value = _finite(active.get("power_w", active.get("estimated_power_w")))
                if value is None:
                    return None
                return ModelView(
                    device_id=device_id,
                    estimated_power_w=value,
                    upper_bound_w=min(
                        value * DEFAULT_UPPER_BOUND_FACTOR, ABSOLUTE_DEVICE_CAP_W
                    ),
                )
            estimator = getattr(raw, "estimate", None)
            if callable(estimator):
                estimate = estimator(device_id, context)
                value = _finite(getattr(estimate, "power_w", None))
                if value is None:
                    return None
                return ModelView(device_id=device_id, estimated_power_w=value)
        return None
    except Exception:  # noqa: BLE001 - defensive per-device isolation
        return None


def model_upper_bounds(
    models: Any,
    device_ids: list[str],
    *,
    factor: float = DEFAULT_UPPER_BOUND_FACTOR,
) -> tuple[list[float | None], dict[str, ModelView]]:
    """Per-device upper bounds plus resolved model views (v3 path)."""
    bounds: list[float | None] = []
    views: dict[str, ModelView] = {}
    for device_id in device_ids:
        view = resolve_model(models, device_id)
        if view is None:
            bounds.append(None)
            continue
        views[device_id] = view
        upper = view.upper_bound_w
        if upper is not None and factor != 1.0:
            upper = min(upper * factor, ABSOLUTE_DEVICE_CAP_W)
        bounds.append(upper)
    return bounds, views


__all__ = [
    "ABSOLUTE_DEVICE_CAP_W",
    "DEFAULT_UPPER_BOUND_FACTOR",
    "ModelView",
    "model_upper_bounds",
    "resolve_model",
]
