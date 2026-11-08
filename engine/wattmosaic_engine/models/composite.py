"""Composite device model: independent sub-loads whose powers add."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..contracts import DeviceModelParameters, LearningSample, ModelType, StageStatus
from .base import DeviceModel, build_model, context_from_sample, enum_text, finite, stdev

#: Notes tag used to associate a sample with a sub-load, e.g. ``component:heater``.
COMPONENT_PREFIX = "component:"


def sample_component(sample: LearningSample, default: str = "main") -> str:
    for note in sample.notes:
        text = str(note)
        if text.startswith(COMPONENT_PREFIX):
            return text.split(":", 1)[1].strip() or default
    return default


def _component_from_context(context: Any) -> str | None:
    if isinstance(context, Mapping):
        comp = context.get("component")
        if comp:
            return str(comp)
        components = context.get("components")
        if isinstance(components, Mapping):
            return None
    comp = getattr(context, "component", None)
    return str(comp) if comp else None


class CompositeDeviceModel(DeviceModel):
    """Sum of independently learned sub-loads.

    A composite device is one logical HA device whose power is the sum of
    independent loads (e.g. an air handler = fan + electric resistance heat, or
    a smart bed = two adjustable bases).  Each sub-load is trained only from
    samples tagged ``component:<name>``; untagged samples belong to the
    ``main`` component.

    ``predict(context)`` sums *all* learned components by default.  Passing
    ``component=<name>`` predicts that sub-load alone; passing
    ``components={name: context}`` (or ``subcontexts``) predicts each with its
    own context then sums, which is how composite state is normally modelled.
    """

    model_type = ModelType.COMPOSITE
    minimum_samples = 3

    def __init__(self, *args: Any, model_types: Mapping[str, ModelType] | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.model_types: dict[str, ModelType] = dict(model_types or {})
        self.submodels: dict[str, DeviceModel] = {}
        self._fit_components: dict[str, int] = {}

    # -- fitting ---------------------------------------------------------- #
    def fit(self, samples: Sequence[LearningSample], **kwargs: Any) -> DeviceModelParameters:
        buckets: dict[str, list[LearningSample]] = {}
        for sample in samples:
            if not sample.accepted:
                continue
            buckets.setdefault(sample_component(sample), []).append(sample)

        self.submodels = {}
        self._fit_components = {}
        max_power = 0.0
        for component, component_samples in buckets.items():
            model_type = self.model_types.get(component)
            if model_type is None:
                # Default: a sub-load is binary unless it carries a control
                # variable, in which case the simple curve model is right.
                has_control = any(s.control_value is not None for s in component_samples)
                model_type = ModelType.DIMMABLE if has_control else ModelType.BINARY
            model = build_model(model_type)
            model.fit(component_samples)
            # Validate on its own subset (that is the only honest comparison).
            metrics = model.validate(component_samples)
            model.validation_metrics = metrics
            self.submodels[component] = model
            self._fit_components[component] = len(component_samples)
            powers = [finite(s.event_power_w) for s in component_samples]
            powers = [p for p in powers if p is not None]
            if powers:
                max_power = max(max_power, max(powers))

        total_residuals: list[float] = []
        for sample in samples:
            if not sample.accepted:
                continue
            predicted = self._predict_component(sample_component(sample), context_from_sample(sample))
            if predicted is not None:
                total_residuals.append(sample.event_power_w - predicted)

        parameters = DeviceModelParameters(
            on_power_w=max_power if self.submodels else None,
            residual_mean_w=(sum(total_residuals) / len(total_residuals)) if total_residuals else None,
            residual_std_w=stdev(total_residuals) if total_residuals else None,
        )
        self.notes = {"components": sorted(self.submodels), "sample_counts": dict(self._fit_components)}
        return self._finish_fit(parameters)

    # -- prediction ------------------------------------------------------- #
    def _predict_component(self, component: str, context: Any) -> float | None:
        model = self.submodels.get(component)
        if model is None:
            return None
        try:
            return float(model.predict(context))
        except Exception:
            return None

    def predict(self, context: Any) -> float:
        component = _component_from_context(context)
        if component is not None and component in self.submodels:
            predicted = self._predict_component(component, context)
            return float(predicted or 0.0)

        components = None
        if isinstance(context, Mapping):
            components = context.get("components") or context.get("subcontexts")
        else:
            components = getattr(context, "components", None) or getattr(context, "subcontexts", None)
        if isinstance(components, Mapping):
            total = 0.0
            for name, sub_context in components.items():
                name = str(name)
                if name not in self.submodels:
                    continue
                # Merge parent context so shared fields (state, attributes day)
                # are visible to the sub-model if the sub-context is partial.
                merged = sub_context
                if isinstance(sub_context, Mapping) and isinstance(context, Mapping):
                    merged = {**context, **sub_context}
                predicted = self._predict_component(name, merged)
                if predicted is not None:
                    total += max(0.0, predicted)
            return total

        total = 0.0
        for name in self.submodels:
            predicted = self._predict_component(name, context)
            if predicted is not None:
                total += max(0.0, predicted)
        return total

    def predict_detailed(self, context: Any) -> tuple[float, str | None, StageStatus]:
        power = self.predict(context)
        active = [
            name
            for name, model in self.submodels.items()
            if (self._predict_component(name, context) or 0.0) > 0.5
        ]
        if not active:
            return power, None, StageStatus.UNKNOWN
        status = StageStatus.IDENTIFIED if len(active) == len(self.submodels) else StageStatus.PROBABLE
        return power, "+".join(active), status

    def confidence(self, context: Any | None = None) -> float:
        if not self.submodels:
            return 0.0
        confidences = [model.confidence(context) for model in self.submodels.values()]
        # The weakest sub-load dominates: a confident fan cannot rescue an
        # unmodelled heater.
        return max(0.0, min(1.0, min(confidences) if confidences else 0.0))

    def explanation(self, context: Any) -> list[str]:
        parts = super().explanation(context) + [f"composite_components:{','.join(sorted(self.submodels))}"]
        component = _component_from_context(context)
        if component:
            parts.append(f"component:{component}")
        return parts

    def describe(self) -> dict[str, Any]:
        data = super().describe()
        data["notes"] = dict(getattr(self, "notes", {}))
        data["components"] = {
            name: {"model_type": enum_text(model.model_type), "confidence": model.confidence()}
            for name, model in self.submodels.items()
        }
        return data
