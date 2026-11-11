"""Bounded per-channel sample buffers and aligned-window construction."""

from __future__ import annotations

import math
import threading
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..contracts import Measurement, QualityStatus, utcnow

DEFAULT_MAX_AGE_S = 180.0
DEFAULT_MAX_SAMPLES_PER_CHANNEL = 600
MAD_TO_SIGMA = 1.4826

# Quality ordering used when deciding how usable a channel is.
_QUALITY_RANK = {
    QualityStatus.GOOD.value: 0,
    QualityStatus.DEGRADED.value: 1,
    QualityStatus.STALE.value: 2,
    QualityStatus.BAD.value: 3,
    QualityStatus.MISSING.value: 4,
}


def _aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _value_of(status: QualityStatus | str) -> str:
    return status.value if isinstance(status, QualityStatus) else str(status)


def _worse(a: QualityStatus | str, b: QualityStatus | str) -> str:
    va, vb = _value_of(a), _value_of(b)
    return va if _QUALITY_RANK.get(va, 3) >= _QUALITY_RANK.get(vb, 3) else vb


def _age_seconds(entry: _Entry, at: datetime) -> float:
    """Seconds between the *state* timestamp and ``at`` (never below zero).

    ADR-0003: staleness is measured from ``state_timestamp``.  ``received_at``
    only says when this process learned the value, so a state replayed by
    ``_prime`` after a reconnect would otherwise appear fresh even when Home
    Assistant recorded it hours ago.

    A clock skew that puts the state in the future clamps to zero rather than
    producing a negative age.
    """
    return max(0.0, (at - entry.state_timestamp).total_seconds())


def median(values: Iterable[float]) -> float | None:
    data = sorted(float(v) for v in values if math.isfinite(float(v)))
    if not data:
        return None
    n = len(data)
    mid = n // 2
    if n % 2:
        return data[mid]
    return (data[mid - 1] + data[mid]) / 2.0


def percentile(values: Iterable[float], q: float) -> float | None:
    data = sorted(float(v) for v in values if math.isfinite(float(v)))
    if not data:
        return None
    if q <= 0:
        return data[0]
    if q >= 100:
        return data[-1]
    rank = (len(data) - 1) * (q / 100.0)
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return data[low]
    return data[low] + (data[high] - data[low]) * (rank - low)


def p95(values: Iterable[float]) -> float | None:
    return percentile(values, 95.0)


def robust_noise_mad(values: Iterable[float]) -> float | None:
    """Robust noise estimate (scaled MAD) of the *values* themselves.

    For a slowly drifting signal this measures dispersion around the median.
    Callers wanting innovation noise should pass first differences.
    """
    data = [float(v) for v in values if math.isfinite(float(v))]
    if len(data) < 2:
        return None
    med = median(data)
    assert med is not None
    mad = median([abs(v - med) for v in data])
    if mad is None:
        return None
    return mad * MAD_TO_SIGMA


@dataclass(slots=True)
class _Entry:
    received_at: datetime
    state_timestamp: datetime
    value_w: float
    quality: str
    repeated: bool
    sequence: int
    entity_id: str
    unit: str = "W"


@dataclass(frozen=True, slots=True)
class AlignedChannel:
    """One channel inside an aligned window.

    ``unit`` is the storage unit of the window: ``W`` for power windows.  A
    channel whose ``unit`` is ``kWh``/``Wh`` must never be treated as power.
    """

    channel: str
    value_w: float | None
    age_seconds: float | None
    quality: str
    repeated: bool
    state_timestamp: datetime | None
    received_at: datetime | None
    sequence: int = 0
    entity_id: str | None = None
    unit: str = "W"

    @property
    def domain(self) -> str:
        return "energy" if self.unit in ("kWh", "Wh") else "power"

    @property
    def is_energy(self) -> bool:
        return self.domain == "energy"

    @property
    def available(self) -> bool:
        return self.value_w is not None and self.quality != QualityStatus.MISSING.value

    @property
    def is_stale(self) -> bool:
        return self.quality == QualityStatus.STALE.value

    @property
    def fresh(self) -> bool:
        """True only when this is a genuinely new, healthy measurement."""
        return (
            self.available
            and not self.repeated
            and self.quality not in (QualityStatus.STALE.value, QualityStatus.BAD.value)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "domain": self.domain,
            "unit": self.unit,
            "value_w": None if self.is_energy else self.value_w,
            "value_kwh": self.value_w if self.is_energy else None,
            "age_seconds": self.age_seconds,
            "quality": self.quality,
            "repeated": self.repeated,
            "fresh": self.fresh,
            "state_timestamp": self.state_timestamp.isoformat() if self.state_timestamp else None,
            "received_at": self.received_at.isoformat() if self.received_at else None,
            "sequence": self.sequence,
            "entity_id": self.entity_id,
        }


@dataclass(frozen=True, slots=True)
class EnergyChannel:
    """One cumulative-energy channel inside an energy window.

    ``value_kwh`` is always in kWh (Wh is converted at ingestion).  It is never
    expressed in watts and never enters the power matrix.
    """

    channel: str
    value_kwh: float | None
    unit: str
    age_seconds: float | None
    quality: str
    repeated: bool
    state_timestamp: datetime | None
    received_at: datetime | None
    sequence: int = 0
    entity_id: str | None = None

    @property
    def available(self) -> bool:
        return self.value_kwh is not None and self.quality != QualityStatus.MISSING.value

    @property
    def is_stale(self) -> bool:
        return self.quality == QualityStatus.STALE.value

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "domain": "energy",
            "value_kwh": self.value_kwh,
            "value_w": None,
            "unit": "kWh",
            "age_seconds": self.age_seconds,
            "quality": self.quality,
            "repeated": self.repeated,
            "state_timestamp": self.state_timestamp.isoformat() if self.state_timestamp else None,
            "received_at": self.received_at.isoformat() if self.received_at else None,
            "sequence": self.sequence,
            "entity_id": self.entity_id,
        }


@dataclass(frozen=True, slots=True)
class EnergyWindow:
    at: datetime
    max_age_s: float
    channels: Mapping[str, EnergyChannel] = field(default_factory=dict)

    def get(self, channel: str) -> EnergyChannel | None:
        return self.channels.get(channel)

    def __getitem__(self, channel: str) -> EnergyChannel:
        return self.channels[channel]

    @property
    def values_kwh(self) -> dict[str, float]:
        return {name: ch.value_kwh for name, ch in self.channels.items() if ch.value_kwh is not None}

    @property
    def missing_channels(self) -> list[str]:
        return [name for name, ch in self.channels.items() if not ch.available]

    @property
    def stale_channels(self) -> list[str]:
        return [name for name, ch in self.channels.items() if ch.is_stale]

    def to_dict(self) -> dict[str, Any]:
        return {
            "at": self.at.isoformat(),
            "max_age_s": self.max_age_s,
            "domain": "energy",
            "missing_channels": self.missing_channels,
            "stale_channels": self.stale_channels,
            "channels": {name: ch.to_dict() for name, ch in self.channels.items()},
        }


@dataclass(frozen=True, slots=True)
class AlignedWindow:
    at: datetime
    max_age_s: float
    channels: Mapping[str, AlignedChannel] = field(default_factory=dict)
    #: State *before* the transitions captured in ``state_transitions``, keyed
    #: by ``DeviceTopology.device_id``.
    previous_states: Mapping[str, str] = field(default_factory=dict)
    #: Device events observed since the previous window.  The disaggregation
    #: engine derives transitions from these, so a window that cannot carry
    #: them silently disables state-transition inference: ``_collect_transitions``
    #: falls back to ``previous_states``, and with both absent every transition
    #: is invisible.  That is why these live on the window rather than being
    #: read out of band.
    state_transitions: tuple[object, ...] = ()
    #: Free-form per-cycle context from the caller.  The disaggregation engine
    #: reads ``device_levels`` (a dimmer's measured output fraction) and
    #: ``channel_noise_floor_w`` (the quality layer's per-channel MAD) back out
    #: of here.  It lives on the window rather than being passed beside it so a
    #: window stays a complete description of one observation instant, and so
    #: ``dataclasses.replace`` -- the only way to attach it, since the window is
    #: frozen -- can carry it without a second parameter on ``infer``.
    metadata: Mapping[str, object] = field(default_factory=dict)

    # -- convenience accessors ------------------------------------------- #
    def __getitem__(self, channel: str) -> AlignedChannel:
        return self.channels[channel]

    def get(self, channel: str) -> AlignedChannel | None:
        return self.channels.get(channel)

    @property
    def values_w(self) -> dict[str, float]:
        """Current values for every channel with a value (repeats included)."""
        return {
            name: ch.value_w
            for name, ch in self.channels.items()
            if ch.value_w is not None
        }

    @property
    def fresh_values_w(self) -> dict[str, float]:
        """Values usable as fresh learning/inference observations."""
        return {name: ch.value_w for name, ch in self.channels.items() if ch.fresh and ch.value_w is not None}

    @property
    def fresh_channels(self) -> list[str]:
        return [name for name, ch in self.channels.items() if ch.fresh]

    @property
    def repeated_channels(self) -> list[str]:
        return [name for name, ch in self.channels.items() if ch.repeated]

    @property
    def missing_channels(self) -> list[str]:
        return [name for name, ch in self.channels.items() if not ch.available]

    @property
    def stale_channels(self) -> list[str]:
        return [name for name, ch in self.channels.items() if ch.is_stale]

    @property
    def complete(self) -> bool:
        return not self.missing_channels

    @property
    def age_max_s(self) -> float | None:
        ages = [ch.age_seconds for ch in self.channels.values() if ch.age_seconds is not None]
        return max(ages) if ages else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "at": self.at.isoformat(),
            "max_age_s": self.max_age_s,
            "complete": self.complete,
            "fresh_channels": self.fresh_channels,
            "repeated_channels": self.repeated_channels,
            "missing_channels": self.missing_channels,
            "stale_channels": self.stale_channels,
            "channels": {name: ch.to_dict() for name, ch in self.channels.items()},
        }


@dataclass(frozen=True, slots=True)
class ChannelHealth:
    """Observed update characteristics for one channel."""

    channel: str
    entity_id: str | None = None
    sample_count: int = 0
    repeat_count: int = 0
    repeat_ratio: float = 0.0
    interval_median_s: float | None = None
    interval_p95_s: float | None = None
    interval_max_s: float | None = None
    interval_min_s: float | None = None
    effective_rate_hz: float | None = None
    last_update_age_s: float | None = None
    last_value_w: float | None = None
    last_received_at: datetime | None = None
    resolution_w: float | None = None
    noise_mad_w: float | None = None
    missing: bool = True
    stale: bool = True
    quality: str = QualityStatus.MISSING.value

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "entity_id": self.entity_id,
            "sample_count": self.sample_count,
            "repeat_count": self.repeat_count,
            "repeat_ratio": self.repeat_ratio,
            "interval_median_s": self.interval_median_s,
            "interval_p95_s": self.interval_p95_s,
            "interval_max_s": self.interval_max_s,
            "interval_min_s": self.interval_min_s,
            "effective_rate_hz": self.effective_rate_hz,
            "last_update_age_s": self.last_update_age_s,
            "last_value_w": self.last_value_w,
            "last_received_at": self.last_received_at.isoformat() if self.last_received_at else None,
            "resolution_w": self.resolution_w,
            "noise_mad_w": self.noise_mad_w,
            "missing": self.missing,
            "stale": self.stale,
            "quality": self.quality,
        }


@dataclass(frozen=True, slots=True)
class EnergyHealth:
    """Monotonicity / counter-reset diagnostics for a cumulative energy meter."""

    channel: str
    entity_id: str | None = None
    sample_count: int = 0
    last_value_kwh: float | None = None
    last_update_age_s: float | None = None
    last_received_at: datetime | None = None
    monotonic: bool = True
    reset_count: int = 0
    max_drop_kwh: float = 0.0
    total_increase_kwh: float | None = None
    unit: str = "kWh"
    missing: bool = True
    stale: bool = True
    quality: str = QualityStatus.MISSING.value

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "entity_id": self.entity_id,
            "domain": "energy",
            "unit": "kWh",
            "sample_count": self.sample_count,
            "last_value_kwh": self.last_value_kwh,
            "last_update_age_s": self.last_update_age_s,
            "last_received_at": self.last_received_at.isoformat() if self.last_received_at else None,
            "monotonic": self.monotonic,
            "reset_count": self.reset_count,
            "max_drop_kwh": self.max_drop_kwh,
            "total_increase_kwh": self.total_increase_kwh,
            "missing": self.missing,
            "stale": self.stale,
            "quality": self.quality,
        }


class SampleAligner:
    """Bounded, thread-safe-enough per-channel ring buffers."""

    def __init__(
        self,
        *,
        max_age_s: float = DEFAULT_MAX_AGE_S,
        max_samples_per_channel: int = DEFAULT_MAX_SAMPLES_PER_CHANNEL,
        stale_after_s: float | None = None,
    ) -> None:
        if max_samples_per_channel < 2:
            raise ValueError("max_samples_per_channel must be >= 2")
        self.max_age_s = float(max_age_s)
        self.max_samples_per_channel = int(max_samples_per_channel)
        self.stale_after_s = float(stale_after_s if stale_after_s is not None else max_age_s)
        self._buffers: dict[str, deque[_Entry]] = {}
        self._energy_buffers: dict[str, deque[_Entry]] = {}
        self._last_seq: dict[str, int] = {}
        self._lock = threading.Lock()
        self.added_count = 0
        self.dropped_count = 0
        self.rejected_count = 0
        self.ignored_non_power = 0
        self.energy_added_count = 0

    # -- ingestion -------------------------------------------------------- #
    def add(self, measurement: Measurement) -> None:
        """Route a measurement into the power or energy domain.

        Power (``W``/``kW``) goes to the power buffers used by the solver.
        Energy (``kWh``/``Wh``) goes to separate cumulative-counter buffers; it is
        never converted to watts and never enters a power window.
        """
        channel = measurement.ct_id
        try:
            received = _aware(measurement.received_at)
            state_ts = _aware(measurement.state_timestamp)
            unit = str(measurement.unit)
            if unit in ("kWh", "Wh"):
                value = float(measurement.value) / (1000.0 if unit == "Wh" else 1.0)
                self._add_energy(channel, measurement, value, received, state_ts)
                return
            if unit not in ("W", "kW"):
                self.ignored_non_power += 1
                return
            value = float(measurement.effective_power_w())
        except Exception:  # noqa: BLE001 - malformed sample never breaks the loop
            self.rejected_count += 1
            return
        if not math.isfinite(value):
            self.rejected_count += 1
            return
        entry = _Entry(
            received_at=received,
            state_timestamp=state_ts,
            value_w=value,
            quality=_value_of(measurement.quality),
            repeated=bool(measurement.repeated),
            sequence=int(measurement.sequence or 0),
            entity_id=measurement.entity_id,
            unit="W",
        )
        with self._lock:
            self._append_locked(self._buffers, channel, entry)
            self._prune_locked(
                self._buffers, channel, self._buffers[channel], self._buffers[channel][-1].received_at
            )
            self.added_count += 1

    def _add_energy(
        self,
        channel: str,
        measurement: Measurement,
        value_kwh: float,
        received: datetime,
        state_ts: datetime,
    ) -> None:
        if not math.isfinite(value_kwh):
            self.rejected_count += 1
            return
        entry = _Entry(
            received_at=received,
            state_timestamp=state_ts,
            value_w=value_kwh,
            quality=_value_of(measurement.quality),
            repeated=bool(measurement.repeated),
            sequence=int(measurement.sequence or 0),
            entity_id=measurement.entity_id,
            unit="kWh",
        )
        with self._lock:
            self._append_locked(self._energy_buffers, channel, entry)
            self._prune_locked(
                self._energy_buffers,
                channel,
                self._energy_buffers[channel],
                self._energy_buffers[channel][-1].received_at,
            )
            self.energy_added_count += 1

    def _append_locked(self, store: dict[str, deque[_Entry]], channel: str, entry: _Entry) -> None:
        buf = store.get(channel)
        if buf is None:
            buf = deque(maxlen=self.max_samples_per_channel)
            store[channel] = buf
        seq = self._last_seq.get(channel, -1)
        if entry.sequence and entry.sequence < seq:
            # Out-of-order arrival: insert in order instead of at the tail.
            self._insert_ordered(buf, entry)
        else:
            buf.append(entry)
            if len(buf) == buf.maxlen:
                self.dropped_count += 1
        self._last_seq[channel] = max(seq, entry.sequence)

    @staticmethod
    def _insert_ordered(buf: deque[_Entry], entry: _Entry) -> None:
        items = list(buf)
        idx = len(items)
        for i in range(len(items) - 1, -1, -1):
            if items[i].received_at <= entry.received_at:
                idx = i + 1
                break
            idx = i
        items.insert(idx, entry)
        buf.clear()
        buf.extend(items[-buf.maxlen :] if buf.maxlen else items)

    def _prune_locked(
        self, store: dict[str, deque[_Entry]], channel: str, buf: deque[_Entry], reference: datetime
    ) -> None:
        cutoff = reference.timestamp() - self.max_age_s
        while buf and buf[0].received_at.timestamp() < cutoff:
            buf.popleft()
            self.dropped_count += 1
        if not buf:
            store.pop(channel, None)

    # -- window ----------------------------------------------------------- #
    def window(
        self,
        at: datetime | None = None,
        channels: Iterable[str] | None = None,
        max_age_s: float | None = None,
    ) -> AlignedWindow:
        """Build a point-in-time aligned window.

        For each requested channel the newest sample at or before ``at`` is
        used.  A sample older than ``max_age_s`` is reported with
        ``quality="stale"``; a channel with no sample is ``missing``.
        """
        at_aw = _aware(at or utcnow())
        max_age = float(self.max_age_s if max_age_s is None else max_age_s)
        names = list(channels) if channels is not None else []
        with self._lock:
            if channels is None:
                names = sorted(self._buffers)
            out: dict[str, AlignedChannel] = {}
            for name in names:
                buf = self._buffers.get(name)
                entry = self._latest_at(buf, at_aw) if buf else None
                out[name] = self._make_channel(name, entry, at_aw, max_age)
        return AlignedWindow(at=at_aw, max_age_s=max_age, channels=out)

    def energy_window(
        self,
        at: datetime | None = None,
        channels: Iterable[str] | None = None,
        max_age_s: float | None = None,
    ) -> EnergyWindow:
        """Same semantics as :meth:`window`, but for cumulative energy counters."""
        at_aw = _aware(at or utcnow())
        max_age = float(self.max_age_s if max_age_s is None else max_age_s)
        with self._lock:
            names = list(channels) if channels is not None else sorted(self._energy_buffers)
            out: dict[str, EnergyChannel] = {}
            for name in names:
                buf = self._energy_buffers.get(name)
                entry = self._latest_at(buf, at_aw) if buf else None
                out[name] = self._make_energy_channel(name, entry, at_aw, max_age)
        return EnergyWindow(at=at_aw, max_age_s=max_age, channels=out)

    @staticmethod
    def _make_energy_channel(
        name: str, entry: _Entry | None, at: datetime, max_age: float
    ) -> EnergyChannel:
        if entry is None:
            return EnergyChannel(
                channel=name,
                value_kwh=None,
                unit="kWh",
                age_seconds=None,
                quality=QualityStatus.MISSING.value,
                repeated=False,
                state_timestamp=None,
                received_at=None,
            )
        age = max(0.0, (at - entry.received_at).total_seconds())
        quality = entry.quality
        if age > max_age:
            quality = _worse(quality, QualityStatus.STALE)
        return EnergyChannel(
            channel=name,
            value_kwh=entry.value_w,
            unit="kWh",
            age_seconds=age,
            quality=quality,
            repeated=entry.repeated,
            state_timestamp=entry.state_timestamp,
            received_at=entry.received_at,
            sequence=entry.sequence,
            entity_id=entry.entity_id,
        )

    @staticmethod
    def _latest_at(buf: deque[_Entry], at: datetime) -> _Entry | None:
        # Buffers are time-ordered; scan from the back for the first <= at.
        for entry in reversed(buf):
            if entry.received_at <= at:
                return entry
        return None

    @staticmethod
    def _make_channel(name: str, entry: _Entry | None, at: datetime, max_age: float) -> AlignedChannel:
        if entry is None:
            return AlignedChannel(
                channel=name,
                value_w=None,
                age_seconds=None,
                quality=QualityStatus.MISSING.value,
                repeated=False,
                state_timestamp=None,
                received_at=None,
            )
        age = _age_seconds(entry, at)
        quality = entry.quality
        if age > max_age:
            quality = _worse(quality, QualityStatus.STALE)
        return AlignedChannel(
            channel=name,
            value_w=entry.value_w,
            age_seconds=age,
            quality=quality,
            repeated=entry.repeated,
            state_timestamp=entry.state_timestamp,
            received_at=entry.received_at,
            sequence=entry.sequence,
            entity_id=entry.entity_id,
            unit=entry.unit,
        )

    # -- health ----------------------------------------------------------- #
    def channel_health(
        self,
        now: datetime | None = None,
        stale_after_s: float | None = None,
    ) -> dict[str, ChannelHealth]:
        at = _aware(now or utcnow())
        threshold = float(stale_after_s if stale_after_s is not None else self.stale_after_s)
        health: dict[str, ChannelHealth] = {}
        with self._lock:
            snapshot = {name: list(buf) for name, buf in self._buffers.items()}
        for name, entries in snapshot.items():
            health[name] = self._health_for(name, entries, at, threshold)
        return health

    def channel_health_one(
        self, channel: str, now: datetime | None = None, stale_after_s: float | None = None
    ) -> ChannelHealth:
        at = _aware(now or utcnow())
        threshold = float(stale_after_s if stale_after_s is not None else self.stale_after_s)
        with self._lock:
            entries = list(self._buffers.get(channel, ()))
        return self._health_for(channel, entries, at, threshold)

    @staticmethod
    def _health_for(
        name: str, entries: list[_Entry], at: datetime, threshold: float
    ) -> ChannelHealth:
        if not entries:
            return ChannelHealth(channel=name)
        intervals = [
            (b.received_at - a.received_at).total_seconds()
            for a, b in zip(entries, entries[1:], strict=False)
            if b.received_at >= a.received_at
        ]
        intervals = [i for i in intervals if i >= 0]
        values = [e.value_w for e in entries]
        diffs = [abs(b - a) for a, b in zip(values, values[1:], strict=False) if b != a]
        # Innovation noise: difference between consecutive distinct readings.
        innovations = [b - a for a, b in zip(values, values[1:], strict=False)]
        last = entries[-1]
        # Same rule as ``_make_channel``: staleness comes from the state
        # timestamp, not from when the frame arrived.
        age = _age_seconds(last, at)
        repeat_count = sum(1 for e in entries if e.repeated)
        med_interval = median(intervals)
        missing = age > threshold
        quality = last.quality
        if missing:
            quality = _worse(quality, QualityStatus.STALE)
        return ChannelHealth(
            channel=name,
            entity_id=last.entity_id,
            sample_count=len(entries),
            repeat_count=repeat_count,
            repeat_ratio=repeat_count / len(entries),
            interval_median_s=med_interval,
            interval_p95_s=p95(intervals),
            interval_max_s=max(intervals) if intervals else None,
            interval_min_s=min(intervals) if intervals else None,
            effective_rate_hz=(1.0 / med_interval) if med_interval and med_interval > 0 else None,
            last_update_age_s=age,
            last_value_w=last.value_w,
            last_received_at=last.received_at,
            resolution_w=min(diffs) if diffs else None,
            noise_mad_w=robust_noise_mad(innovations),
            missing=missing,
            stale=missing,
            quality=quality,
        )

    def energy_health(
        self, now: datetime | None = None, stale_after_s: float | None = None
    ) -> dict[str, EnergyHealth]:
        """Monotonicity/reset analysis for cumulative energy channels."""
        at = _aware(now or utcnow())
        threshold = float(stale_after_s if stale_after_s is not None else self.stale_after_s)
        with self._lock:
            snapshot = {name: list(buf) for name, buf in self._energy_buffers.items()}
        out: dict[str, EnergyHealth] = {}
        for name, entries in snapshot.items():
            out[name] = self._energy_health_for(name, entries, at, threshold)
        return out

    @staticmethod
    def _energy_health_for(
        name: str, entries: list[_Entry], at: datetime, threshold: float
    ) -> EnergyHealth:
        if not entries:
            return EnergyHealth(channel=name)
        values = [e.value_w for e in entries]
        resets = 0
        max_drop = 0.0
        total_increase = 0.0
        for prev, cur in zip(values, values[1:], strict=False):
            delta = cur - prev
            if delta < 0:
                resets += 1
                max_drop = max(max_drop, abs(delta))
            else:
                total_increase += delta
        last = entries[-1]
        age = max(0.0, (at - last.received_at).total_seconds())
        missing = age > threshold
        quality = last.quality
        if missing:
            quality = _worse(quality, QualityStatus.STALE)
        if resets:
            quality = _worse(quality, QualityStatus.DEGRADED)
        return EnergyHealth(
            channel=name,
            entity_id=last.entity_id,
            sample_count=len(entries),
            last_value_kwh=last.value_w,
            last_update_age_s=age,
            last_received_at=last.received_at,
            monotonic=resets == 0,
            reset_count=resets,
            max_drop_kwh=max_drop,
            total_increase_kwh=total_increase,
            missing=missing,
            stale=missing,
            quality=quality,
        )

    # -- maintenance ------------------------------------------------------ #
    def channels(self) -> list[str]:
        with self._lock:
            return sorted(self._buffers)

    def energy_channels(self) -> list[str]:
        with self._lock:
            return sorted(self._energy_buffers)

    def latest(self, channel: str) -> AlignedChannel | None:
        with self._lock:
            buf = self._buffers.get(channel)
            entry = buf[-1] if buf else None
        if entry is None:
            return None
        return self._make_channel(channel, entry, utcnow(), self.max_age_s)

    def prune(self, now: datetime | None = None) -> int:
        """Drop samples older than ``max_age_s``.  Returns channels emptied."""
        at = _aware(now or utcnow())
        emptied = 0
        with self._lock:
            for store in (self._buffers, self._energy_buffers):
                for name in list(store):
                    buf = store[name]
                    self._prune_locked(store, name, buf, at)
                    if name not in store:
                        emptied += 1
        return emptied

    def clear(self) -> None:
        with self._lock:
            self._buffers.clear()
            self._energy_buffers.clear()
            self._last_seq.clear()

    def stats(self) -> dict[str, Any]:
        with self._lock:
            sizes = {name: len(buf) for name, buf in self._buffers.items()}
            energy_sizes = {name: len(buf) for name, buf in self._energy_buffers.items()}
        return {
            "channels": len(sizes),
            "samples": sum(sizes.values()),
            "energy_channels": len(energy_sizes),
            "energy_samples": sum(energy_sizes.values()),
            "max_samples_per_channel": self.max_samples_per_channel,
            "max_age_s": self.max_age_s,
            "added": self.added_count,
            "energy_added": self.energy_added_count,
            "dropped": self.dropped_count,
            "rejected": self.rejected_count,
            "ignored_non_power": self.ignored_non_power,
            "buffer_sizes": sizes,
            "energy_buffer_sizes": energy_sizes,
        }


__all__ = [
    "AlignedChannel",
    "AlignedWindow",
    "ChannelHealth",
    "DEFAULT_MAX_AGE_S",
    "DEFAULT_MAX_SAMPLES_PER_CHANNEL",
    "EnergyChannel",
    "EnergyHealth",
    "EnergyWindow",
    "SampleAligner",
    "median",
    "p95",
    "percentile",
    "robust_noise_mad",
]
