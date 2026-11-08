"""Binary (ON / OFF / standby / startup / shutdown) device model."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ..contracts import DeviceModelParameters, LearningSample, ModelType
from .base import (
    DeviceModel,
    context_state,
    finite,
    is_on,
    median,
    quantile,
    stdev,
)

# Target states used in ``LearningSample.notes`` by the sample collector.
_ON_LABELS = {"on", "running", "active", "open", "true", "1"}
_OFF_LABELS = {"off", "closed", "false", "0"}
_STANDBY_LABELS = {"standby", "sleep", "idle", "deep_sleep"}
_STARTUP_LABELS = {"startup", "starting", "start"}
_SHUTDOWN_LABELS = {"shutdown", "stopping", "stop", "cooldown"}


def _target_state(sample: LearningSample) -> str:
    """Recover the post-event state label recorded by the collector.

    The collector records ``state:<new_state>`` in ``notes``.  If absent we
    infer from the sign of the delta: a positive delta means the device turned
    on, a negative delta means it turned off.
    """
    for note in sample.notes:
        text = str(note)
        if text.startswith(("state:", "ha_state:")):
            return text.split(":", 1)[1].strip().lower()
    if sample.delta_power_w > 0:
        return "on"
    if sample.delta_power_w < 0:
        return "off"
    return "unknown"


def _label_group(label: str) -> str | None:
    if label in _ON_LABELS:
        return "on"
    if label in _OFF_LABELS:
        return "off"
    if label in _STANDBY_LABELS:
        return "standby"
    if label in _STARTUP_LABELS:
        return "startup"
    if label in _SHUTDOWN_LABELS:
        return "shutdown"
    return None


class BinaryDeviceModel(DeviceModel):
    """Learns a distribution of power for each discrete operating state.

    Fitting uses the robust median so a handful of inrush samples do not drag
    the steady-state ON power; the 10th/90th percentiles are kept as notes for
    diagnostics.  OFF power is learned from post-OFF samples (or the pre-event
    baseline when a device is switched off but never sampled while off).
    """

    model_type = ModelType.BINARY
    minimum_samples = 4

    def fit(self, samples: Sequence[LearningSample], **kwargs: Any) -> DeviceModelParameters:
        groups: dict[str, list[float]] = {k: [] for k in ("on", "off", "standby", "startup", "shutdown")}
        baselines: list[float] = []
        for sample in samples:
            if not sample.accepted:
                continue
            label = _target_state(sample)
            group = _label_group(label)
            power = finite(sample.event_power_w)
            if group is not None and power is not None:
                groups[group].append(power)
            # Turning a device OFF reveals its OFF power in the baseline of the
            # *next* state; the collector stores that as event_power.  A turning
            # OFF sample whose event power looks like the old ON power is not
            # useful for OFF.  Negative-delta baselines instead inform standby.
            if sample.delta_power_w < 0:
                base = finite(sample.baseline_power_w)
                if base is not None and base > 0:
                    baselines.append(base)

        def pick(name: str) -> float | None:
            values = groups[name]
            if not values:
                return None
            return median(values)

        on_power = pick("on")
        off_power = pick("off")
        standby_power = pick("standby")
        startup_power = pick("startup")
        shutdown_power = pick("shutdown")

        if off_power is None and standby_power is None:
            # Without a measured OFF sample, use the lowest stable baseline seen
            # before an ON transition only if it is plausibly a standby load.
            # We deliberately do *not* invent 0 W; leave it unset instead.
            pass

        residual_values = []
        for sample in samples:
            if not sample.accepted:
                continue
            predicted = self._predict_from_values(
                on_power, off_power, standby_power, startup_power, shutdown_power,
                _target_state(sample),
            )
            if predicted is not None:
                residual_values.append(sample.event_power_w - predicted)

        parameters = DeviceModelParameters(
            on_power_w=on_power,
            off_power_w=off_power,
            standby_power_w=standby_power,
            startup_power_w=startup_power,
            shutdown_power_w=shutdown_power,
            residual_mean_w=(sum(residual_values) / len(residual_values)) if residual_values else None,
            residual_std_w=stdev(residual_values) if residual_values else None,
        )
        self.notes = {
            "on_samples": len(groups["on"]),
            "off_samples": len(groups["off"]),
            "standby_samples": len(groups["standby"]),
            "on_p10": quantile(groups["on"], 0.10),
            "on_p90": quantile(groups["on"], 0.90),
            "on_spread_w": stdev(groups["on"]) if groups["on"] else 0.0,
            "off_baseline_observations": len(baselines),
        }
        return self._finish_fit(parameters)

    @staticmethod
    def _predict_from_values(
        on: float | None,
        off: float | None,
        standby: float | None,
        startup: float | None,
        shutdown: float | None,
        label: str,
    ) -> float | None:
        group = _label_group(label)
        if group == "on":
            return on
        if group == "off":
            return off if off is not None else standby
        if group == "standby":
            return standby if standby is not None else off
        if group == "startup":
            return startup if startup is not None else on
        if group == "shutdown":
            return shutdown if shutdown is not None else off
        return None

    def predict(self, context: Any) -> float:
        state = context_state(context)
        on_flag = is_on(state)
        if state is None:
            # No HA state: the only defensible estimate is the ON power, and it
            # is flagged as low-confidence by ``confidence()``.
            return float(self.parameters.on_power_w or 0.0)
        label = state
        if state in _ON_LABELS:
            label = "on"
        elif state in _OFF_LABELS:
            label = "off"
        elif "standby" in state:
            label = "standby"
        elif state in _STARTUP_LABELS:
            label = "startup"
        elif state in _SHUTDOWN_LABELS:
            label = "shutdown"
        predicted = self._predict_from_values(
            self.parameters.on_power_w,
            self.parameters.off_power_w,
            self.parameters.standby_power_w,
            self.parameters.startup_power_w,
            self.parameters.shutdown_power_w,
            label,
        )
        if predicted is not None:
            return float(predicted)
        if on_flag is True:
            return float(self.parameters.on_power_w or 0.0)
        if on_flag is False:
            return float(self.parameters.off_power_w or self.parameters.standby_power_w or 0.0)
        return float(self.parameters.on_power_w or 0.0)

    def confidence(self, context: Any | None = None) -> float:
        state = context_state(context) if context is not None else None
        base = super().confidence(context)
        if state is None:
            # Unknown state: cannot condition the estimate.
            base *= 0.35
        elif state in _ON_LABELS and self.parameters.on_power_w is None:
            base *= 0.4
        elif state in _OFF_LABELS and self.parameters.off_power_w is None:
            base *= 0.5
        spread = (self.notes or {}).get("on_spread_w") if hasattr(self, "notes") else None
        if state in _ON_LABELS and spread is not None:
            on = self.parameters.on_power_w or 0.0
            if on > 0:
                base *= max(0.3, 1.0 - min(1.0, float(spread) / on))
        return max(0.0, min(1.0, base))

    def explanation(self, context: Any) -> list[str]:
        state = context_state(context) or "unknown"
        return super().explanation(context) + [f"ha_state:{state}"]

    def describe(self) -> dict[str, Any]:
        data = super().describe()
        data["notes"] = dict(getattr(self, "notes", {}))
        return data
