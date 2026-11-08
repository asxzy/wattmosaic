"""Automatic training: decide *when* and *what* to train, produce candidates.

Training is device-specific and deliberately conservative:

* A model is only trained once at least ``min_samples_for_model`` accepted
  samples exist (default 12 from :class:`EngineConfig`), and only when at least
  ``retrain_after_new_samples`` new accepted samples arrived since the active
  model's training window.  Retraining every sample would thrash the model and
  defeat version stability.
* The model class is chosen from *device characteristics* (HA domain,
  attributes, state vocabulary, observed control variables), never hardcoded
  per entity id.
* The result is always ``ModelStatus.CANDIDATE`` with validation metrics and
  ``training_quality``.  Promotion is a separate decision (``promotion.py``).
"""

from __future__ import annotations

import contextlib
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..contracts import (
    DeviceModelVersion,
    LearningSample,
    ModelStatus,
    ModelType,
    utcnow,
)
from ..models.base import DeviceModel, build_model
from .thresholds import LearningThresholds
from .validation import split_holdout, validate_model

_CONTROL_VARIABLES_BRIGHTNESS = {"brightness", "brightness_pct", "level", "dimmer", "percent"}
_CONTROL_VARIABLES_SPEED = {
    "speed",
    "percentage",
    "fan_speed",
    "speed_pct",
    "rpm",
    "speed_level",
    "motor_speed",
    "pump_speed",
}
_TIME_VARYING_HINTS = {
    "program",
    "wash_program",
    "cycle",
    "phase",
    "remaining_time",
    "remaining",
    "job_state",
    "run_state",
    "appliance",
    "dishwasher",
    "washing_machine",
    "washer",
    "dryer",
    "oven",
    "fridge",
    "refrigerator",
    "hvac",
    "heat_pump",
}
_COMPONENT_HINTS = {"components", "sub_loads", "features", "zones", "subdevices"}


@dataclass(slots=True)
class TrainingResult:
    version: DeviceModelVersion | None
    model: DeviceModel | None = None
    trained: bool = False
    reason: str = ""
    metrics: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def infer_model_type(device: Any, samples: Sequence[LearningSample] | None = None) -> ModelType:
    """Choose a model type from device characteristics and observed samples.

    Priority is intentional: composites and appliances need their specific
    structure; control-curve models require evidence of a numeric control
    variable; enumerated states need more than two distinct labels; everything
    else falls back to binary, and finally residual when there is no state
    information at all.
    """
    attrs: Mapping[str, Any] = {}
    states: set[str] = set()
    device_class = ""
    domain = ""
    appliance = ""
    if device is not None:
        if isinstance(device, Mapping):
            raw_attrs = device.get("attributes") or {}
            attrs = raw_attrs if isinstance(raw_attrs, Mapping) else {}
            states = {str(s).lower() for s in (device.get("states") or device.get("state_options") or [])}
            device_class = str(device.get("device_class") or "").lower()
            domain = str(device.get("domain") or device.get("platform") or "").lower()
            appliance = str(device.get("appliance_type") or device.get("device_type") or "").lower()
            keys = set(attrs) | set(device)
            if keys & _COMPONENT_HINTS:
                return ModelType.COMPOSITE
        else:
            raw_attrs = getattr(device, "attributes", None)
            attrs = raw_attrs if isinstance(raw_attrs, Mapping) else {}
            states = {str(s).lower() for s in (getattr(device, "states", None) or [])}
            device_class = str(getattr(device, "device_class", "") or "").lower()
            domain = str(getattr(device, "domain", "") or "").lower()
            appliance = str(getattr(device, "appliance_type", "") or "").lower()
            keys = set(attrs)
            if keys & _COMPONENT_HINTS:
                return ModelType.COMPOSITE

    hint_text = " ".join([device_class, domain, appliance, *[str(k) for k in attrs]])
    if any(hint in hint_text for hint in _TIME_VARYING_HINTS):
        return ModelType.TIME_VARYING

    variables = {(s.control_variable or "").strip().lower() for s in (samples or []) if s.control_variable}
    variables.discard("")
    if variables & _CONTROL_VARIABLES_BRIGHTNESS:
        return ModelType.DIMMABLE
    if variables & _CONTROL_VARIABLES_SPEED:
        return ModelType.VARIABLE_SPEED
    if attrs and ({"brightness", "brightness_pct", "dim_level"} & set(attrs)):
        return ModelType.DIMMABLE
    if attrs and ({"speed", "percentage", "fan_speed", "rpm"} & set(attrs)):
        return ModelType.VARIABLE_SPEED

    # Enumerated modes: collect from explicit state vocabulary and from
    # ``state:`` notes recorded by the collector.
    if samples:
        states |= {
            note.split(":", 1)[1].strip().lower()
            for s in samples
            for note in s.notes
            if isinstance(note, str) and note.startswith(("state:", "ha_state:"))
        }
    states.discard("")
    non_operating = {"off", "on", "unavailable", "unknown", "idle", "standby"}
    meaningful = states - non_operating
    if len(meaningful) >= 2 or len(states - {"on", "off", "unavailable", "unknown"}) >= 3:
        return ModelType.MULTI_STATE

    if states and states <= {"on", "off", "unavailable", "unknown", "open", "closed"}:
        return ModelType.BINARY
    if states:
        return ModelType.BINARY
    # No state evidence at all: only a residual/pattern model is honest.
    return ModelType.RESIDUAL


def training_quality(
    samples: Sequence[LearningSample],
    metrics: dict[str, float],
    *,
    all_samples: Sequence[LearningSample] | None = None,
) -> tuple[float, list[str]]:
    """Compute a 0–1 training-quality score and explanatory notes.

    Components (equal weight):
    * sample sufficiency (saturates at 4× the minimum sample count),
    * acceptance ratio (if rejected samples are known),
    * fit quality (validation R²),
    * relative error (MAE / mean absolute power),
    * control coverage (fraction of samples carrying a control value) — only
      counted when any sample has one, so it cannot penalise binary devices.
    """
    accepted = [s for s in samples if s.accepted]
    notes: list[str] = []
    if not accepted:
        return 0.0, ["no_accepted_samples"]
    minimum = 12
    sufficiency = min(1.0, len(accepted) / (minimum * 4.0))
    if all_samples:
        ratio = len(accepted) / max(1, len(all_samples))
        acceptance = ratio
    else:
        acceptance = 1.0
    r2 = float(metrics.get("r2", 0.0))
    fit = max(0.0, min(1.0, r2))
    mean_power = statistics.mean(abs(s.event_power_w) for s in accepted) or 1.0
    mae = float(metrics.get("mae", mean_power))
    relative_error = max(0.0, 1.0 - min(1.0, mae / max(mean_power, 1.0)))
    with_control = sum(1 for s in accepted if s.control_value is not None)
    if with_control:
        control_coverage = with_control / len(accepted)
        if control_coverage < 0.5:
            notes.append(f"low_control_coverage:{control_coverage:.2f}")
    else:
        control_coverage = 1.0  # not applicable
    components = [sufficiency, acceptance, fit, relative_error, control_coverage]
    score = sum(components) / len(components)
    notes.append(f"sufficiency={sufficiency:.2f}")
    notes.append(f"acceptance={acceptance:.2f}")
    notes.append(f"fit_r2={fit:.2f}")
    notes.append(f"relative_error_quality={relative_error:.2f}")
    return max(0.0, min(1.0, score)), notes


class Trainer:
    """Creates candidate :class:`DeviceModelVersion` objects from samples."""

    def __init__(self, thresholds: LearningThresholds | None = None) -> None:
        self.thresholds = thresholds or LearningThresholds()

    # -- gating ----------------------------------------------------------- #
    def should_train(
        self,
        device_id: str,
        samples: Sequence[LearningSample],
        versions: Sequence[DeviceModelVersion] | None = None,
        *,
        new_sample_threshold: int | None = None,
    ) -> tuple[bool, str]:
        accepted = [s for s in samples if s.accepted]
        if len(accepted) < self.thresholds.min_samples_for_model:
            return False, f"insufficient_samples:{len(accepted)}<{self.thresholds.min_samples_for_model}"
        if versions:
            active = next((v for v in versions if v.status == ModelStatus.ACTIVE.value), None)
            if active is not None and active.trained_to is not None:
                new = [s for s in accepted if s.captured_at > active.trained_to]
                needed = new_sample_threshold if new_sample_threshold is not None else max(
                    1, self.thresholds.min_samples_for_model // 3
                )
                if len(new) < needed:
                    return False, f"insufficient_new_samples:{len(new)}<{needed}"
        return True, "ready"

    # -- training --------------------------------------------------------- #
    def train(
        self,
        device_id: str,
        samples: Sequence[LearningSample],
        device: Any = None,
        *,
        model_type: ModelType | str | None = None,
        versions: Sequence[DeviceModelVersion] | None = None,
        power_series: Any = None,
    ) -> TrainingResult:
        accepted = [s for s in samples if s.accepted]
        if not accepted:
            return TrainingResult(None, trained=False, reason="no_accepted_samples")
        if len(accepted) < self.thresholds.min_samples_for_model:
            return TrainingResult(
                None,
                trained=False,
                reason=f"insufficient_samples:{len(accepted)}<{self.thresholds.min_samples_for_model}",
            )
        if model_type is None:
            model_type = infer_model_type(device, accepted)
        try:
            model_type = ModelType(model_type)
        except ValueError:
            return TrainingResult(None, trained=False, reason=f"unknown_model_type:{model_type}")

        train_samples, validation_samples = split_holdout(
            accepted,
            self.thresholds.holdout_fraction,
            self.thresholds.min_validation_samples,
        )
        model = build_model(model_type)
        try:
            model.fit(train_samples)
        except Exception as exc:
            return TrainingResult(None, trained=False, reason=f"fit_failed:{type(exc).__name__}:{exc}")

        # Time-varying appliances can additionally learn from an observed
        # series when one is provided (dedicated CT or reconstructed).
        if model_type == ModelType.TIME_VARYING and power_series is not None:
            with contextlib.suppress(Exception):
                model.fit_series(power_series)

        report = validate_model(model, validation_samples, self.thresholds, training_samples=train_samples)
        model.validation_metrics = dict(report.metrics)
        quality, quality_notes = training_quality(train_samples, report.metrics, all_samples=samples)

        existing = list(versions) if versions is not None else []
        next_number = max((v.version for v in existing), default=0) + 1
        notes = [
            f"trained_on:{len(train_samples)}",
            f"validated_on:{len(validation_samples)}",
            *quality_notes,
            *report.notes,
        ]
        if model_type == ModelType.TIME_VARYING and power_series is not None:
            notes.append("learned_from_power_series")

        captured = [s.captured_at for s in accepted]
        version = DeviceModelVersion(
            device_id=device_id,
            model_type=model_type,
            version=next_number,
            status=ModelStatus.CANDIDATE,
            created_at=utcnow(),
            trained_from=min(captured),
            trained_to=max(captured),
            sample_count=len(train_samples),
            training_quality=quality,
            validation_metrics=dict(report.metrics),
            parameters=model.parameters.model_copy(deep=True),
            confidence=float(model.confidence()) if hasattr(model, "confidence") else 0.0,
            notes=notes,
        )
        return TrainingResult(
            version,
            model=model,
            trained=True,
            reason="trained",
            metrics=dict(report.metrics),
            notes=notes,
        )


__all__ = ["Trainer", "TrainingResult", "infer_model_type", "training_quality"]
