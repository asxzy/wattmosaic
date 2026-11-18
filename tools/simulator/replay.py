"""Replay driver skeleton: feed HA-shaped messages to a consumer on an injected clock.

This is the deterministic way a later task drives the realtime pipeline.  It
takes a :class:`~tools.simulator.hb17.HB17House` (or any iterable of records
with ``ha_messages`` / ``reader_samples`` / ``truth`` keys), advances an
injected clock, and calls ``consumer.on_state_changed(msg)`` for every HA
message in delivery order.  P6 binds it to ``EngineCore``; here it is tested
with a recording stub, so this module has no dependency on the engine.

Two guarantees matter:

* **No wall clock.**  Every timestamp comes from the house's ``start_time``
  plus a simulated offset supplied by ``clock``.  A replay is byte-identical
  across runs and machines.
* **Observation seam.**  Only observation-shaped messages are handed to the
  consumer.  The ``truth`` frame never leaves the driver: a static assertion
  (``assert_no_ground_truth_keys``) guards the stream before delivery.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol

from .frames import assert_no_ground_truth_keys

#: An injected clock: returns a simulated epoch-seconds value.
Clock = Callable[[], float]


class StateChangeConsumer(Protocol):
    """The minimal surface the replay driver needs from its consumer."""

    def on_state_changed(self, msg: dict[str, Any]) -> None:  # pragma: no cover - protocol
        ...


@dataclass
class ReplayResult:
    """What a replay run reports to its caller."""

    n_ticks: int = 0
    n_messages: int = 0
    n_samples: int = 0
    delivered_entities: list[str] = field(default_factory=list)
    final_t_s: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "n_ticks": self.n_ticks,
            "n_messages": self.n_messages,
            "n_samples": self.n_samples,
            "delivered_entities": sorted(set(self.delivered_entities)),
            "final_t_s": self.final_t_s,
        }


class RecordingConsumer:
    """A test double: records every message and sample it is given."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.samples: list[dict[str, Any]] = []

    def on_state_changed(self, msg: dict[str, Any]) -> None:
        self.messages.append(dict(msg))

    def on_sample(self, msg: dict[str, Any]) -> None:
        self.samples.append(dict(msg))


class MutableClock:
    """An injected clock the driver (or test) advances explicitly."""

    def __init__(self, start: float = 0.0) -> None:
        self.now_s = float(start)

    def __call__(self) -> float:
        return self.now_s

    def advance_to(self, t: float) -> None:
        self.now_s = float(t)

    def advance_by(self, dt: float) -> None:
        self.now_s += float(dt)


def _iter_records(source: Any) -> Iterable[dict[str, Any]]:
    """Accept an ``HB17House`` (``.records``) or a bare iterable of records."""
    if hasattr(source, "records"):
        return source.records
    return source


def replay(
    source: Any,
    consumer: StateChangeConsumer,
    *,
    clock: Clock | None = None,
    on_sample: Callable[[dict[str, Any]], None] | None = None,
    check_seam: bool = True,
) -> ReplayResult:
    """Drive ``consumer`` through an observation-only record stream.

    Parameters
    ----------
    source:
        An :class:`~tools.simulator.hb17.HB17House` or an iterable of records.
    consumer:
        Any object with ``on_state_changed(msg)``.  P6 passes ``EngineCore``.
    clock:
        Injected clock.  If omitted, an internal :class:`MutableClock` is used
        and advanced to each record's ``t_s``.  The clock's returned value is
        passed nowhere that matters for correctness; its purpose is to make
        time an explicit, replayable input rather than a wall-clock read.
    on_sample:
        Optional callback for reader samples.  ``EngineCore.on_sample`` can be
        bound here.  Absent by default so the driver can be used with only a
        state-change consumer.
    check_seam:
        When true (default), the observation frame is scanned for ground-truth
        keys before a single message is delivered.  This is the leak gate.
    """
    records = list(_iter_records(source))
    if check_seam:
        observation = {
            "records": [
                {
                    "ha_messages": r.get("ha_messages") or [],
                    "reader_samples": r.get("reader_samples") or {},
                }
                for r in records
            ]
        }
        assert_no_ground_truth_keys(observation, where="replay observation stream")

    own_clock = MutableClock()

    result = ReplayResult()
    for record in records:
        t_s = float(record.get("t_s", 0.0))
        if clock is None:
            own_clock.advance_to(t_s)
        result.n_ticks += 1
        result.final_t_s = t_s

        # HA messages first (the command), then reader samples (the physics
        # confirming it) — the order a real integration sees them.
        for msg in record.get("ha_messages") or ():
            consumer.on_state_changed(dict(msg))
            result.n_messages += 1
            result.delivered_entities.append(str(msg.get("entity_id", "")))

        samples = record.get("reader_samples") or {}
        for reader_id, sample in samples.items():
            payload = {"reader_id": reader_id, **dict(sample)}
            if on_sample is not None:
                on_sample(payload)
            result.n_samples += 1

    return result


def drive_house(
    house: Any,
    consumer: StateChangeConsumer,
    *,
    clock: Clock | None = None,
    on_sample: Callable[[dict[str, Any]], None] | None = None,
) -> ReplayResult:
    """Convenience wrapper: replay an :class:`HB17House` with its own clock."""
    return replay(house, consumer, clock=clock, on_sample=on_sample)


def iter_messages(source: Any) -> Iterator[dict[str, Any]]:
    """Yield observation-shaped HA messages in delivery order (no consumer)."""
    for record in _iter_records(source):
        for msg in record.get("ha_messages") or ():
            yield dict(msg)


__all__ = [
    "Clock",
    "MutableClock",
    "ReplayResult",
    "RecordingConsumer",
    "StateChangeConsumer",
    "drive_house",
    "iter_messages",
    "replay",
]
