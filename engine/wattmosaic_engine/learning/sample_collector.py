"""Sample collection: turn HA transitions + CT windows into learning samples.

Why adaptive windows?
    The live audit measured a median CT state-update interval of **15.7 s**, not
    the informally assumed 2 s (``data/ct_update_profile.json``).  A fixed 2 s
    window would contain zero or one CT reading and produce garbage deltas.  The
    collector therefore derives its window from the *observed* cadence of the
    supporting channels and clamps it to ``[min_window_s, max_window_s]``.  If
    cadence is unknown it uses the measured house default (15.7 s).

A sample candidate records:

* robust baseline power over the pre-event window (median, resistant to spikes),
* robust post-event power over the post-settle window,
* delta, observed duration, supporting CT ids,
* the control value when the event carries one (brightness/speed/mode),
* provenance notes (state label, sample counts, noise) used later by the
  filter and by ``context_from_sample`` when re-validating a model.
"""

from __future__ import annotations

import statistics
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from ..contracts import DeviceEvent, LearningSample, utcnow
from ..models.protocols import (
    context_attributes,
    normalise_timestamp,
    window_channels,
    window_time,
)
from .thresholds import LearningThresholds

#: Attribute priority for extracting a numeric control value from an event.
_CONTROL_ATTRIBUTES: tuple[tuple[str, str], ...] = (
    ("brightness", "brightness"),
    ("brightness_pct", "brightness_pct"),
    ("speed", "speed"),
    ("speed_level", "speed"),
    ("percentage", "percentage"),
    ("fan_speed", "fan_speed"),
    ("pump_speed", "pump_speed"),
    ("rpm", "rpm"),
    ("level", "level"),
    ("dim_level", "level"),
)

#: State strings that should be recorded as a categorical control value.
_MODE_ATTRIBUTES = ("hvac_mode", "operating_mode", "mode", "program", "wash_program", "preset_mode")


def extract_control(attributes: Mapping[str, Any] | None, state: str | None) -> tuple[float | None, str | None]:
    """Extract ``(control_value, control_variable)`` from event attributes.

    A numeric attribute wins (brightness, speed, rpm, …).  Failing that, an
    enumerated mode becomes the control *variable* with ``None`` value, and the
    caller stores the state label in the notes so multi-state models can learn
    the state→power map.
    """
    attrs = dict(attributes or {})
    for attr, variable in _CONTROL_ATTRIBUTES:
        if attr in attrs and attrs[attr] is not None:
            try:
                return float(attrs[attr]), variable
            except (TypeError, ValueError):
                continue
    for attr in _MODE_ATTRIBUTES:
        if attr in attrs and attrs[attr] is not None:
            return None, attr
    if state is not None:
        return None, "state"
    return None, None


def _median(values: Sequence[float]) -> float | None:
    clean = []
    for value in values:
        try:
            v = float(value)
        except (TypeError, ValueError):
            continue
        if v == v and v not in (float("inf"), float("-inf")):
            clean.append(v)
    if not clean:
        return None
    return float(statistics.median(clean))


def _state_of(event: DeviceEvent | Mapping[str, Any] | Any) -> str | None:
    if isinstance(event, Mapping):
        state = event.get("new_state", event.get("state"))
    else:
        state = getattr(event, "new_state", None) or getattr(event, "state", None)
    if state is None:
        return None
    return str(state).strip().lower()


def _attrs_of(event: DeviceEvent | Mapping[str, Any] | Any) -> dict[str, Any]:
    if isinstance(event, Mapping):
        attrs = event.get("attributes") or {}
        return dict(attrs) if isinstance(attrs, Mapping) else {}
    return context_attributes(event)


def _time_of(event: DeviceEvent | Mapping[str, Any] | Any) -> datetime:
    raw = (
        event.get("occurred_at") or event.get("timestamp") if isinstance(event, Mapping) else getattr(event, "occurred_at", None)
    )
    return normalise_timestamp(raw) or utcnow()


def _device_of(event: DeviceEvent | Mapping[str, Any] | Any) -> str | None:
    device_id = event.get("device_id") if isinstance(event, Mapping) else getattr(event, "device_id", None)
    return str(device_id) if device_id else None


def _supporting_cts(event: DeviceEvent | Mapping[str, Any] | Any, attrs: Mapping[str, Any]) -> list[str]:
    for source in (attrs, event if isinstance(event, Mapping) else getattr(event, "__dict__", {})):
        value = source.get("supporting_ct_ids") or source.get("ct_ids")
        if value:
            if isinstance(value, str):
                return [value]
            try:
                return [str(v) for v in value]
            except TypeError:
                return []
    return []


class SampleCollector:
    """Accumulates CT snapshots and builds :class:`LearningSample` candidates."""

    def __init__(
        self,
        thresholds: LearningThresholds | None = None,
        *,
        cadence_s: float | None = None,
        history: int = 4096,
    ) -> None:
        self.thresholds = thresholds or LearningThresholds()
        self._cadence_s = float(cadence_s) if cadence_s else self.thresholds.assumed_cadence_s
        self._snapshots: deque[tuple[datetime, dict[str, float]]] = deque(maxlen=max(16, history))
        self._timestamps: deque[datetime] = deque(maxlen=512)

    # -- cadence ---------------------------------------------------------- #
    def set_cadence(self, cadence_s: float | None) -> None:
        if cadence_s and cadence_s > 0 and cadence_s == cadence_s:
            self._cadence_s = float(cadence_s)
            self.thresholds.assumed_cadence_s = float(cadence_s)

    def observe_cadence(self, timestamps: Iterable[datetime | float | str]) -> float:
        """Estimate cadence from a sequence of CT timestamps (median interval)."""
        parsed = [normalise_timestamp(t) for t in timestamps]
        clean = sorted({t for t in parsed if t is not None})
        if len(clean) < 2:
            return self._cadence_s
        intervals = [(b - a).total_seconds() for a, b in zip(clean, clean[1:], strict=False) if (b - a).total_seconds() > 0]
        if not intervals:
            return self._cadence_s
        self.set_cadence(statistics.median(intervals))
        return self._cadence_s

    def cadence_from_health(self, channel_health: Mapping[str, Any]) -> float:
        """Derive cadence from ``SampleAligner.channel_health()``-style data."""
        intervals: list[float] = []
        for value in channel_health.values():
            if isinstance(value, Mapping):
                candidate = value.get("median_interval_s") or value.get("median_s") or value.get("expected_interval_s")
            else:
                candidate = getattr(value, "median_interval_s", None) or getattr(value, "expected_interval_s", None)
            if candidate:
                try:
                    intervals.append(float(candidate))
                except (TypeError, ValueError):
                    continue
        if intervals:
            self.set_cadence(statistics.median(intervals))
        return self._cadence_s

    @property
    def cadence_s(self) -> float:
        return self._cadence_s

    @property
    def window_s(self) -> float:
        return self.thresholds.window_s(self._cadence_s)

    # -- observation ------------------------------------------------------ #
    def observe(self, window: Any) -> None:
        """Record one aligned window snapshot (idempotent per timestamp)."""
        at = window_time(window)
        values: dict[str, float] = {}
        for ct_id, reading in window_channels(window).items():
            value = reading.get("value")
            if value is None:
                continue
            values[str(ct_id)] = float(value)
        if not values:
            return
        self._timestamps.append(at)
        if self._snapshots and self._snapshots[-1][0] == at:
            self._snapshots[-1] = (at, {**self._snapshots[-1][1], **values})
        else:
            self._snapshots.append((at, values))

    def clear(self) -> None:
        self._snapshots.clear()
        self._timestamps.clear()

    def _values_between(
        self, start: datetime, end: datetime, ct_id: str, *, include_start: bool = True
    ) -> list[float]:
        out: list[float] = []
        for at, values in self._snapshots:
            if at < start:
                continue
            if at > end:
                break
            if at == start and not include_start:
                continue
            if ct_id in values:
                out.append(values[ct_id])
        return out

    def _channels_in_window(self, start: datetime, end: datetime) -> list[str]:
        seen: list[str] = []
        for at, values in self._snapshots:
            if start <= at <= end:
                for ct_id in values:
                    if ct_id not in seen:
                        seen.append(ct_id)
        return seen

    # -- collection ------------------------------------------------------- #
    def consider(self, events: Sequence[Any], window: Any) -> list[LearningSample]:
        """Build candidates for ``events`` using the accumulated CT history.

        One malformed event never aborts the batch (spec §21).
        """
        self.observe(window)
        samples: list[LearningSample] = []
        for event in events:
            try:
                sample = self.build(event)
            except Exception:
                continue
            if sample is not None:
                samples.append(sample)
        return samples

    def build(
        self,
        event: Any,
        *,
        supporting_ct_ids: Sequence[str] | None = None,
        baseline_values: Mapping[str, Sequence[float]] | None = None,
        event_values: Mapping[str, Sequence[float]] | None = None,
        at: datetime | None = None,
    ) -> LearningSample | None:
        """Build one sample from an event and (optionally) explicit CT values.

        Explicit ``baseline_values``/``event_values`` make the collector usable
        by an event processor that has already extracted windows; otherwise the
        internal history is used.
        """
        at = at or _time_of(event)
        attrs = _attrs_of(event)
        state = _state_of(event)
        cts = list(supporting_ct_ids or _supporting_cts(event, attrs))
        half_window = timedelta(seconds=self.window_s)
        settle = timedelta(seconds=self.thresholds.settle_s)
        baseline_start = at - half_window
        baseline_end = at - timedelta(seconds=min(self.thresholds.settle_s, self.window_s / 4.0))
        event_start = at + settle
        event_end = at + half_window

        baseline_median: float | None = None
        event_median: float | None = None
        used_cts: list[str] = []
        baseline_counts: dict[str, int] = {}
        event_counts: dict[str, int] = {}

        if baseline_values is not None and event_values is not None:
            for ct_id in cts or list(baseline_values.keys()):
                before = list(baseline_values.get(ct_id, []))
                after = list(event_values.get(ct_id, []))
                if not before or not after:
                    continue
                baseline_median = _median(before)
                event_median = _median(after)
                used_cts.append(str(ct_id))
                baseline_counts[str(ct_id)] = len(before)
                event_counts[str(ct_id)] = len(after)
                break
        else:
            if not cts:
                # No declared association: this event cannot be measured.
                # Falling back to "whatever CT happens to be in the window"
                # would attribute this device's power to an arbitrary circuit,
                # which is exactly the fabricated attribution the spec forbids.
                # Emit the rejected-shaped sample instead (see below) so the
                # reason is recorded rather than guessed.
                cts = []
            for ct_id in cts:
                before = self._values_between(baseline_start, baseline_end, ct_id)
                after = self._values_between(event_start, event_end, ct_id)
                if len(before) < self.thresholds.min_baseline_samples or len(after) < self.thresholds.min_event_samples:
                    continue
                baseline_median = _median(before)
                event_median = _median(after)
                used_cts.append(str(ct_id))
                baseline_counts[str(ct_id)] = len(before)
                event_counts[str(ct_id)] = len(after)
                break

        if not cts:
            # Missing association: still emit a rejected-shaped candidate so the
            # reason is persisted (the filter decides, the collector does not).
            cts = []

        baseline = baseline_median if baseline_median is not None else 0.0
        measured = event_median if event_median is not None else baseline
        delta = measured - baseline
        control_value, control_variable = extract_control(attrs, state)

        notes: list[str] = [
            f"window_s:{self.window_s:.1f}",
            f"cadence_s:{self._cadence_s:.1f}",
        ]
        if state is not None:
            notes.append(f"state:{state}")
        if control_variable and control_value is None:
            notes.append(f"mode_variable:{control_variable}")
        if baseline_counts:
            notes.append(f"baseline_samples:{min(baseline_counts.values())}")
        if event_counts:
            notes.append(f"event_samples:{min(event_counts.values())}")
        noise = attrs.get("noise_w") or attrs.get("sigma_w")
        if noise is not None:
            notes.append(f"noise_w:{float(noise):.2f}")
        association = attrs.get("association_confidence")
        if association is not None:
            notes.append(f"association_confidence:{float(association):.3f}")
        timestamp_quality = attrs.get("timestamp_quality")
        if timestamp_quality is not None:
            notes.append(f"timestamp_quality:{timestamp_quality}")
        topology_quality = attrs.get("topology_quality")
        if topology_quality is not None:
            notes.append(f"topology_quality:{topology_quality}")
        if event_median is None:
            notes.append("no_measurement")

        duration = max(0.0, (event_end - event_start).total_seconds()) if event_median is not None else 0.0
        return LearningSample(
            sample_id=uuid4().hex,
            device_id=_device_of(event) or "unknown",
            captured_at=at,
            duration_s=duration,
            baseline_power_w=float(baseline),
            event_power_w=float(measured),
            delta_power_w=float(delta),
            control_value=control_value,
            control_variable=control_variable,
            supporting_ct_ids=used_cts,
            quality_score=0.0,
            accepted=True,
            notes=notes,
        )


__all__ = ["SampleCollector", "extract_control"]
