"""Per-reader companion quantities (V, I, PF) declared on a monitor (ADR-0044).

A monitor publishes voltage/current/power-factor entities beside its power
entity.  The integration resolves them to same-device entity ids and declares
them on the endpoint config revision; the engine never reads HA's registries
(ADR-0028).  This module is the pure in-memory store for the *latest* companion
values of each reader and a bounded ring of (timestamp, V, I, PF) samples for
the aligned analyses (voltage-sag leg classification, branch-current tag).

Design rules:

* Companion entities are **never readers**: they never produce a power reading
  and never enter the fast detection path.  The caller dispatches their state
  changes through :meth:`CompanionStore.observe` using the owning reader id.
* Absent is ``None`` (HB §18): a value is used to fill a power sample's
  companion fields only when it is at most :data:`COMPANION_MAX_AGE_S` older
  than the sample; otherwise the field stays ``None``.
* The fast path is O(1): a dict lookup for the latest record and a bounded
  deque append for the ring.  All analysis runs at aligned rebuilds only.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

__all__ = [
    "COMPANION_MAX_AGE_S",
    "COMPANION_RING_WINDOW_S",
    "CompanionRecord",
    "CompanionStore",
    "entity_to_reader",
]

#: A companion value older than this relative to a power sample is absent.
COMPANION_MAX_AGE_S = 30.0
#: The ring holds about a week of 30 s buckets, like ``StructureMemory``.
COMPANION_RING_WINDOW_S = 7 * 24 * 3600.0
#: Grid of the analysis ring.
COMPANION_BUCKET_S = 30.0


@dataclass(frozen=True, slots=True)
class CompanionRecord:
    """The latest declared companion values of one reader."""

    voltage_v: float | None = None
    current_a: float | None = None
    power_factor: float | None = None
    observed_at: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "voltage_v": self.voltage_v,
            "current_a": self.current_a,
            "power_factor": self.power_factor,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
        }


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number not in (float("inf"), float("-inf")) else None


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def entity_to_reader(declarations: Mapping[str, Mapping[str, str | None]]) -> dict[str, str]:
    """Invert ``{reader_id: {voltage_entity_id: ..., ...}}`` to ``{entity: reader}``.

    The integration declares the three companion entity ids with the endpoint.
    This is the only mapping the source needs to route a companion state change
    to its owning reader; absent ids are skipped.  A companion entity belongs to
    exactly one reader (a monitor's own power entity is not in this mapping).
    """
    out: dict[str, str] = {}
    for reader, entities in declarations.items():
        for entity_id in entities.values():
            if isinstance(entity_id, str) and entity_id:
                out[entity_id] = str(reader)
    return out


class CompanionStore:
    """Latest companion record and bounded per-reader sample ring."""

    def __init__(
        self,
        *,
        bucket_s: float = COMPANION_BUCKET_S,
        window_s: float = COMPANION_RING_WINDOW_S,
    ) -> None:
        self.bucket_s = float(bucket_s)
        self.window_s = float(window_s)
        self._latest: dict[str, CompanionRecord] = {}
        # One deque per reader, capped at the number of buckets in the window.
        self._rings: dict[str, deque[tuple[float, float | None, float | None, float | None]]] = {}
        # Power samples for the same readers, used by the hourly factor job
        # (production keeps no raw ct_samples by default, ADR-0042).  Kept as
        # (timestamp, watts) pairs on the same bounded ring.
        self._power: dict[str, deque[tuple[float, float]]] = {}
        self._maxlen = max(1, int(window_s // bucket_s) + 1)
        self._lock = threading.Lock()

    # -- writes (fast path, O(1)) ------------------------------------------- #
    def observe(
        self,
        reader: str,
        *,
        voltage_v: Any = None,
        current_a: Any = None,
        power_factor: Any = None,
        observed_at: datetime,
    ) -> CompanionRecord:
        """Record one companion state change for ``reader`` (never a reading).

        The quantity that changed is merged into the reader's latest record; the
        other quantities keep their previous value and timestamp only for the
        whole record's ``observed_at``, which tracks the most recent change.
        """
        v, i, pf = _finite(voltage_v), _finite(current_a), _finite(power_factor)
        moment = _aware(observed_at)
        with self._lock:
            previous = self._latest.get(reader)
            record = CompanionRecord(
                voltage_v=v if v is not None else (previous.voltage_v if previous else None),
                current_a=i if i is not None else (previous.current_a if previous else None),
                power_factor=pf if pf is not None else (previous.power_factor if previous else None),
                observed_at=moment,
            )
            self._latest[reader] = record
            ring = self._rings.setdefault(reader, deque(maxlen=self._maxlen))
            ring.append((moment.timestamp(), record.voltage_v, record.current_a, record.power_factor))
            return record

    def record_power(self, reader: str, watts: float, observed_at: datetime) -> None:
        """Keep a bounded power sample for the hourly factor-inference job.

        This is companion-side bookkeeping only: the reading has already been
        delivered to the normal fast path; nothing here re-emits it.
        """
        if reader not in self._latest and reader not in self._power:
            return
        value = _finite(watts)
        if value is None:
            return
        with self._lock:
            ring = self._power.setdefault(reader, deque(maxlen=self._maxlen))
            ring.append((_aware(observed_at).timestamp(), value))

    # -- reads -------------------------------------------------------------- #
    def power_ring(self, reader: str) -> tuple[tuple[float, float], ...]:
        """A snapshot copy of the reader's bounded power ring."""
        with self._lock:
            return tuple(self._power.get(reader, ()))

    def latest(self, reader: str) -> CompanionRecord | None:
        with self._lock:
            return self._latest.get(reader)

    def values_at(
        self, reader: str, sample_at: datetime, *, max_age_s: float = COMPANION_MAX_AGE_S
    ) -> tuple[float | None, float | None, float | None]:
        """Companion fields for a power sample at ``sample_at``.

        Each field is included only when the record's observation is no more
        than ``max_age_s`` older than the sample; a value that is too old (or a
        future value from a skewed device clock) stays absent.  This is the
        single place that decides whether a power sample gets V/I/PF.
        """
        record = self.latest(reader)
        if record is None or record.observed_at is None:
            return (None, None, None)
        age = (sample_at - record.observed_at).total_seconds()
        if age < 0 or age > max_age_s:
            return (None, None, None)
        return (record.voltage_v, record.current_a, record.power_factor)

    def ring(self, reader: str) -> tuple[tuple[float, float | None, float | None, float | None], ...]:
        """A snapshot copy of the reader's bounded companion ring."""
        with self._lock:
            return tuple(self._rings.get(reader, ()))

    def readers(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._latest))
