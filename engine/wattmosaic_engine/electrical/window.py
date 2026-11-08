"""Aligned observation window primitives used by the electrical model.

The engine's synchronisation layer (owned by Agent D) exposes an
``AlignedWindow`` with per-channel value/age/quality/repeat information.  This
module defines the *duck-typed adapter* used by the electrical and
disaggregation code so that neither package depends on the concrete
synchroniser implementation (avoids an import cycle and keeps the packages
testable in isolation).

Two representations are accepted everywhere:

``ObservationWindow``
    The canonical local representation.

Anything else (mapping, object with ``readings`` / ``samples`` / ``channels``)
    Coerced through :func:`coerce_window`.  A plain mapping of
    ``channel_id -> float`` is treated as a fresh, good-quality reading with
    age ``0``.

Cadence is *always* derived from observation, never hardcoded to 2 s.  Field
measurements on multi-channel CT installs commonly show a median CT
state-update interval of ``15.7 s`` (see ``docs/testing.md``); that value is
only the documented last-resort fallback when a window contains no timing
information.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from wattmosaic_engine.contracts import Measurement, Polarity, QualityStatus

#: Documented fallback derived from the measured HA environment
#: (``median_of_channel_medians_s = 15.736``).  Used *only* when a window
#: carries no cadence and no usable history timestamps.
DEFAULT_CADENCE_S = 15.7

#: Cadence bounds.  Real channels update between ~1 s (matter plugs, batched
#: writes) and ~45 s (slow channels).  Clamping prevents pathological windows.
MIN_CADENCE_S = 0.25
MAX_CADENCE_S = 120.0


@dataclass(frozen=True, slots=True)
class ChannelReading:
    """One aligned channel observation.

    ``history`` is an optional time-ordered sequence of ``(timestamp, watts)``
    samples ending at (or before) ``at`` and is what event detection uses.
    ``value_w=None`` means the channel has no usable current value (missing or
    expired), which is different from a measured ``0.0``.
    """

    channel_id: str
    value_w: float | None
    age_s: float = 0.0
    quality: QualityStatus = QualityStatus.GOOD
    repeated: bool = False
    polarization: Polarity = Polarity.NORMAL
    history: tuple[tuple[datetime, float], ...] = ()
    resolution_w: float | None = None
    expected_interval_s: float | None = None

    def is_usable(self, max_age_s: float | None = None) -> bool:
        if self.value_w is None:
            return False
        if self.quality in (QualityStatus.MISSING, QualityStatus.BAD):
            return False
        return not (max_age_s is not None and self.age_s > max_age_s)

    def signed_w(self) -> float | None:
        """Polarity-corrected value; sign is preserved (negative = export)."""
        if self.value_w is None:
            return None
        return -self.value_w if self.polarization == Polarity.INVERTED else self.value_w

    def median_history_interval_s(self) -> float | None:
        ts = [t for t, _ in self.history]
        if len(ts) < 2:
            return None
        diffs = [
            (b - a).total_seconds()
            for a, b in zip(ts, ts[1:], strict=False)
            if (b - a).total_seconds() > 0
        ]
        if not diffs:
            return None
        diffs.sort()
        return diffs[len(diffs) // 2]


@dataclass(slots=True)
class ObservationWindow:
    """Aligned multi-channel window at a single inference instant."""

    at: datetime
    readings: dict[str, ChannelReading] = field(default_factory=dict)
    cadence_s: float | None = None
    #: State *before* the transitions captured in ``state_transitions``.
    previous_states: dict[str, str] = field(default_factory=dict)
    #: Transitions observed in this window (dicts or ``DeviceEvent`` objects).
    state_transitions: list[object] = field(default_factory=list)
    metadata: dict[str, object] = field(default_factory=dict)

    # -- construction helpers -------------------------------------------------
    @classmethod
    def from_values(
        cls,
        at: datetime,
        values: Mapping[str, float | None],
        *,
        ages: Mapping[str, float] | None = None,
        qualities: Mapping[str, QualityStatus] | None = None,
        repeated: Mapping[str, bool] | None = None,
        histories: Mapping[str, Sequence[tuple[datetime, float]]] | None = None,
        reversed_polarity: Iterable[str] = (),
        cadence_s: float | None = None,
        previous_states: Mapping[str, str] | None = None,
        state_transitions: Sequence[object] | None = None,
    ) -> ObservationWindow:
        ages = ages or {}
        qualities = qualities or {}
        repeated = repeated or {}
        histories = histories or {}
        inverted = set(reversed_polarity)
        readings = {
            cid: ChannelReading(
                channel_id=cid,
                value_w=value,
                age_s=float(ages.get(cid, 0.0)),
                quality=qualities.get(cid, QualityStatus.GOOD),
                repeated=bool(repeated.get(cid, False)),
                polarization=Polarity.INVERTED if cid in inverted else Polarity.NORMAL,
                history=tuple(histories.get(cid, ())),
            )
            for cid, value in values.items()
        }
        return cls(
            at=at,
            readings=readings,
            cadence_s=cadence_s,
            previous_states=dict(previous_states or {}),
            state_transitions=list(state_transitions or []),
            metadata={},
        )

    # -- access ---------------------------------------------------------------
    def get(self, channel_id: str) -> ChannelReading | None:
        return self.readings.get(channel_id)

    def value(self, channel_id: str) -> float | None:
        r = self.readings.get(channel_id)
        return r.signed_w() if r is not None else None

    def channels(self) -> tuple[str, ...]:
        return tuple(self.readings)

    def cadence(self) -> float:
        """Observed cadence in seconds (median of channel medians)."""
        return observed_cadence_s(self)

    def before_after_seconds(
        self,
        *,
        before_multiplier: float = 2.0,
        after_multiplier: float = 1.0,
        min_before_s: float = 5.0,
        max_before_s: float = 60.0,
        min_after_s: float = 2.5,
        max_after_s: float = 30.0,
    ) -> tuple[float, float]:
        c = self.cadence()
        before = min(max(before_multiplier * c, min_before_s), max_before_s)
        after = min(max(after_multiplier * c, min_after_s), max_after_s)
        return before, after


@dataclass(frozen=True, slots=True)
class CoercedWindow:
    """Normalised view over any supported window representation."""

    at: datetime
    readings: dict[str, ChannelReading]
    cadence_s: float | None
    previous_states: dict[str, str]
    state_transitions: list[object]
    metadata: dict[str, object]

    def cadence(self) -> float:
        if self.cadence_s is not None:
            return clamp_cadence(self.cadence_s)
        return observed_cadence_s(self)

    def before_after_seconds(self, **kwargs: float) -> tuple[float, float]:
        return ObservationWindow.before_after_seconds(self, **kwargs)  # type: ignore[arg-type]


def clamp_cadence(value: float) -> float:
    if not math.isfinite(value) or value <= 0:
        return DEFAULT_CADENCE_S
    return min(max(value, MIN_CADENCE_S), MAX_CADENCE_S)


def observed_cadence_s(window: object, *, default: float = DEFAULT_CADENCE_S) -> float:
    """Derive cadence from a window.

    Priority:
    1. explicit ``window.cadence_s`` (synchroniser estimate);
    2. median of per-channel median history intervals;
    3. per-channel ``expected_interval_s``;
    4. documented ``default`` (15.7 s from the real audit).
    """
    explicit = getattr(window, "cadence_s", None)
    if isinstance(explicit, (int, float)) and math.isfinite(float(explicit)) and explicit > 0:
        return clamp_cadence(float(explicit))

    readings = getattr(window, "readings", None)
    if isinstance(readings, Mapping):
        medians = [
            r.median_history_interval_s()
            for r in readings.values()
            if isinstance(r, ChannelReading)
        ]
        medians = [m for m in medians if m is not None]
        if medians:
            medians.sort()
            return clamp_cadence(medians[len(medians) // 2])
        expected = [
            r.expected_interval_s
            for r in readings.values()
            if isinstance(r, ChannelReading) and r.expected_interval_s
        ]
        expected = [e for e in expected if e is not None]
        if expected:
            expected.sort()
            return clamp_cadence(float(expected[len(expected) // 2]))
    return clamp_cadence(default)


def _to_reading(channel_id: str, raw: object, at: datetime) -> ChannelReading:
    if isinstance(raw, ChannelReading):
        return raw
    if isinstance(raw, Measurement):
        return ChannelReading(
            channel_id=channel_id,
            value_w=raw.effective_power_w(),
            age_s=max(0.0, (at - raw.state_timestamp).total_seconds()),
            quality=raw.quality,
            repeated=raw.repeated,
            polarization=raw.polarization,
        )
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        value = float(raw)
        return ChannelReading(
            channel_id=channel_id,
            value_w=value if math.isfinite(value) else None,
        )
    if isinstance(raw, Mapping):
        value = raw.get("value_w", raw.get("value"))
        history = raw.get("history") or ()
        return ChannelReading(
            channel_id=channel_id,
            value_w=None if value is None else float(value),
            age_s=float(raw.get("age_s", raw.get("age_seconds", 0.0)) or 0.0),
            quality=QualityStatus(str(raw.get("quality", "good"))),
            repeated=bool(raw.get("repeated", False)),
            polarization=Polarity(str(raw.get("polarization", raw.get("polarity", "normal")))),
            history=tuple((t, float(v)) for t, v in history),
            resolution_w=raw.get("resolution_w"),
            expected_interval_s=raw.get("expected_interval_s"),
        )
    # Generic object (e.g. the synchroniser's ChannelSample).
    value = getattr(raw, "value_w", getattr(raw, "value", None))
    history = getattr(raw, "history", ()) or ()
    quality = getattr(raw, "quality", QualityStatus.GOOD)
    if not isinstance(quality, QualityStatus):
        try:
            quality = QualityStatus(str(quality))
        except ValueError:
            quality = QualityStatus.BAD
    pol = getattr(raw, "polarization", getattr(raw, "polarity", Polarity.NORMAL))
    if not isinstance(pol, Polarity):
        try:
            pol = Polarity(str(pol))
        except ValueError:
            pol = Polarity.NORMAL
    return ChannelReading(
        channel_id=channel_id,
        value_w=None if value is None else float(value),
        age_s=float(getattr(raw, "age_s", getattr(raw, "age_seconds", 0.0)) or 0.0),
        quality=quality,
        repeated=bool(getattr(raw, "repeated", getattr(raw, "is_repeat", False))),
        polarization=pol,
        history=tuple((t, float(v)) for t, v in history),
        resolution_w=getattr(raw, "resolution_w", None),
        expected_interval_s=getattr(raw, "expected_interval_s", None),
    )


def coerce_window(window: object, *, at: datetime | None = None) -> CoercedWindow:
    """Normalise any supported window object to :class:`CoercedWindow`."""
    if isinstance(window, ObservationWindow):
        return CoercedWindow(
            at=window.at,
            readings=dict(window.readings),
            cadence_s=window.cadence_s,
            previous_states=dict(window.previous_states),
            state_transitions=list(window.state_transitions),
            metadata=dict(window.metadata),
        )

    when = at or getattr(window, "at", None) or getattr(window, "timestamp", None)
    if when is None:
        when = datetime.now().astimezone()
    if not isinstance(when, datetime):  # pragma: no cover - defensive
        when = datetime.now().astimezone()

    raw_readings: object | None = None
    for attr in ("readings", "samples", "channels", "values"):
        candidate = getattr(window, attr, None)
        if isinstance(candidate, Mapping):
            raw_readings = candidate
            break
    if raw_readings is None and isinstance(window, Mapping):
        raw_readings = window
    if raw_readings is None:
        raise TypeError(
            "unsupported window: expected ObservationWindow, a mapping, or an "
            "object exposing a readings/samples/channels mapping"
        )

    readings = {str(cid): _to_reading(str(cid), raw, when) for cid, raw in raw_readings.items()}
    cadence = getattr(window, "cadence_s", getattr(window, "cadence", None))
    if callable(cadence):  # pragma: no cover - defensive
        cadence = cadence()
    prev = getattr(window, "previous_states", None) or {}
    transitions = getattr(window, "state_transitions", None) or []
    meta = getattr(window, "metadata", None) or {}
    return CoercedWindow(
        at=when,
        readings=readings,
        cadence_s=float(cadence) if isinstance(cadence, (int, float)) else None,
        previous_states={str(k): str(v) for k, v in dict(prev).items()},
        state_transitions=list(transitions),
        metadata=dict(meta),
    )


def shift(at: datetime, seconds: float) -> datetime:
    return at + timedelta(seconds=seconds)
