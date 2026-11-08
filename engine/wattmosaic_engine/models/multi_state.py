"""Multi-state device model: HA state string → power map."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ..contracts import DeviceModelParameters, LearningSample, ModelType, OperatingStage, StageStatus
from .base import (
    DeviceModel,
    context_state,
    finite,
    median,
    stdev,
)

#: states that mean "not operating" even though they are distinct labels
_NON_OPERATING = {"off", "unavailable", "unknown", "none", "idle", "standby", "closed", "false", "0"}


def _sample_state(sample: LearningSample) -> str | None:
    for note in sample.notes:
        text = str(note)
        if text.startswith(("state:", "ha_state:")):
            return text.split(":", 1)[1].strip().lower()
    if sample.control_variable in ("state", "mode", "operating_mode") and sample.control_value is not None:
        return str(sample.control_value).strip().lower()
    return None


class MultiStateDeviceModel(DeviceModel):
    """State-conditioned power for devices with enumerated operating modes.

    Examples: HVAC ``heat``/``cool``/``fan_only``, a media receiver's
    ``movie``/``music`` mode, or a robot vacuum's ``cleaning``/``returning``.

    Unknown states are *not* guessed: ``predict`` returns the learned OFF power
    (if any) and ``confidence`` drops sharply, plus ``predict_detailed`` reports
    ``StageStatus.UNKNOWN``.
    """

    model_type = ModelType.MULTI_STATE
    minimum_samples = 4

    def fit(self, samples: Sequence[LearningSample], **kwargs: Any) -> DeviceModelParameters:
        buckets: dict[str, list[float]] = {}
        transitions: dict[str, int] = {}
        previous: str | None = None
        ordered = sorted(
            (s for s in samples if s.accepted),
            key=lambda s: (s.captured_at, s.sample_id),
        )
        for sample in ordered:
            state = _sample_state(sample)
            power = finite(sample.event_power_w)
            if state is None or power is None:
                continue
            buckets.setdefault(state, []).append(power)
            if previous is not None and previous != state:
                transitions[f"{previous}->{state}"] = transitions.get(f"{previous}->{state}", 0) + 1
            previous = state
        state_map = {state: float(median(values)) for state, values in buckets.items() if values}
        stages: list[OperatingStage] = []
        for state, values in buckets.items():
            mean_power = sum(values) / len(values)
            std = stdev(values)
            stages.append(
                OperatingStage(
                    name=state,
                    status=StageStatus.IDENTIFIED if len(values) >= 2 else StageStatus.PROBABLE,
                    power_mean_w=float(mean_power),
                    power_std_w=float(std),
                    notes=[f"samples:{len(values)}"],
                )
            )
        off_candidates = [state_map[s] for s in state_map if s in _NON_OPERATING]
        on_candidates = [state_map[s] for s in state_map if s not in _NON_OPERATING]
        residuals = []
        for sample in ordered:
            state = _sample_state(sample)
            if state is None or state not in state_map:
                continue
            residual = sample.event_power_w - state_map[state]
            if finite(residual) is not None:
                residuals.append(residual)
        parameters = DeviceModelParameters(
            state_map=state_map,
            on_power_w=median(on_candidates) if on_candidates else None,
            off_power_w=median(off_candidates) if off_candidates else None,
            standby_power_w=state_map.get("standby"),
            stages=stages,
            residual_mean_w=(sum(residuals) / len(residuals)) if residuals else None,
            residual_std_w=stdev(residuals) if residuals else None,
        )
        self.notes = {
            "states": sorted(state_map),
            "sample_counts": {state: len(values) for state, values in buckets.items()},
            "transitions": transitions,
        }
        return self._finish_fit(parameters)

    def predict(self, context: Any) -> float:
        state = context_state(context)
        if state is not None and state in self.parameters.state_map:
            return float(self.parameters.state_map[state])
        if state is not None and state in _NON_OPERATING:
            return float(
                self.parameters.off_power_w
                if self.parameters.off_power_w is not None
                else self.parameters.state_map.get(state, 0.0)
            )
        # Unknown state: never fabricate an ON power.
        if self.parameters.off_power_w is not None:
            return float(self.parameters.off_power_w)
        return 0.0

    def predict_detailed(self, context: Any) -> tuple[float, str | None, StageStatus]:
        state = context_state(context)
        power = self.predict(context)
        if state is None:
            return power, None, StageStatus.UNKNOWN
        if state in self.parameters.state_map:
            counts = (getattr(self, "notes", {}) or {}).get("sample_counts", {})
            count = int(counts.get(state, 0))
            status = StageStatus.IDENTIFIED if count >= 2 else StageStatus.PROBABLE
            return power, state, status
        if state in _NON_OPERATING:
            return power, state, StageStatus.PROBABLE
        return power, None, StageStatus.UNKNOWN

    def confidence(self, context: Any | None = None) -> float:
        base = super().confidence(context)
        state = context_state(context) if context is not None else None
        if not self.parameters.state_map or state is None:
            return max(0.0, min(1.0, base * 0.3))
        if state in self.parameters.state_map:
            counts = (getattr(self, "notes", {}) or {}).get("sample_counts", {})
            count = int(counts.get(state, 0))
            if count < 2:
                base *= 0.75
            return max(0.0, min(1.0, base))
        if state in _NON_OPERATING and self.parameters.off_power_w is not None:
            return max(0.0, min(1.0, base * 0.7))
        return max(0.0, min(1.0, base * 0.25))

    def explanation(self, context: Any) -> list[str]:
        state = context_state(context)
        parts = super().explanation(context) + [f"ha_state:{state or 'unknown'}"]
        if state is None or state not in self.parameters.state_map:
            parts.append("state_not_in_learned_map")
        return parts

    def describe(self) -> dict[str, Any]:
        data = super().describe()
        data["notes"] = dict(getattr(self, "notes", {}))
        return data
