"""P1 — bounded in-memory ring of HA events (DEC-17).

Events of unlinked entities are held for grouping/candidate generation, never
persisted.  The ring is bounded by both count (10 000) and age (30 min),
whichever is smaller, and offers ``window(since, until)`` and ``by_context(id)``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from wattmosaic_engine.contracts import HAEvent
from wattmosaic_engine.realtime.candidate_ring import CandidateRing

T0 = datetime(2026, 11, 18, 12, 0, 0, tzinfo=UTC)


def event(
    index: int,
    at: datetime,
    *,
    context_id: str | None = None,
    entity_id: str = "switch.lamp",
) -> HAEvent:
    return HAEvent.make(
        entity_id=entity_id,
        observed_at=at,
        received_at=at,
        to_state="on",
        context_id=context_id,
        linked=False,
    )


def test_add_and_window_returns_events_in_range() -> None:
    ring = CandidateRing()
    e1 = event(0, T0 + timedelta(seconds=1))
    e2 = event(1, T0 + timedelta(seconds=10))
    e3 = event(2, T0 + timedelta(seconds=20))
    for e in (e1, e2, e3):
        ring.add(e)

    found = ring.window(T0, T0 + timedelta(seconds=15))
    assert [e.event_id for e in found] == [e1.event_id, e2.event_id]

    # Closed interval at both ends.
    assert [e.event_id for e in ring.window(e2.observed_at, e2.observed_at)] == [e2.event_id]


def test_window_uses_observed_at_not_received_at() -> None:
    ring = CandidateRing()
    e = HAEvent.make(
        entity_id="switch.lamp",
        observed_at=T0 - timedelta(minutes=10),
        received_at=T0,
        to_state="on",
        linked=False,
    )
    ring.add(e, now=T0)
    assert ring.window(T0 - timedelta(minutes=11), T0) == [e]


def test_by_context_finds_matching_events() -> None:
    ring = CandidateRing()
    a = event(0, T0, context_id="ctx-1")
    b = event(1, T0 + timedelta(seconds=5), context_id="ctx-1")
    c = event(2, T0 + timedelta(seconds=6), context_id="ctx-2")
    for e in (a, b, c):
        ring.add(e)

    assert [e.event_id for e in ring.by_context("ctx-1")] == [a.event_id, b.event_id]
    assert ring.by_context("missing") == []


def test_capacity_evicts_oldest() -> None:
    ring = CandidateRing(capacity=3)
    events = [event(i, T0 + timedelta(seconds=i)) for i in range(5)]
    for e in events:
        ring.add(e)

    remaining = ring.window(T0 - timedelta(seconds=1), T0 + timedelta(seconds=100))
    assert {e.event_id for e in remaining} == {e.event_id for e in events[-3:]}
    assert ring.dropped_count == 2


def test_age_bound_evicts_older_than_30_minutes() -> None:
    ring = CandidateRing(max_age_s=30 * 60)
    old = event(0, T0 - timedelta(minutes=31))
    recent = event(1, T0 - timedelta(minutes=1))
    ring.add(old, now=T0)
    ring.add(recent, now=T0)

    remaining = ring.window(T0 - timedelta(hours=1), T0 + timedelta(seconds=1))
    assert {e.event_id for e in remaining} == {recent.event_id}


def test_explicit_now_drives_eviction() -> None:
    """No wall clock: the caller supplies ``now``, making the ring deterministic."""
    ring = CandidateRing(max_age_s=60)
    e = event(0, T0)
    ring.add(e, now=T0)
    assert ring.window(T0, T0) == [e]
    ring.prune(now=T0 + timedelta(seconds=61))
    assert ring.window(T0, T0) == []


def test_ring_is_in_memory_only() -> None:
    """Nothing is persisted from the ring (DEC-17): it holds objects only."""
    ring = CandidateRing(capacity=2)
    assert not hasattr(ring, "db")
    assert not hasattr(ring, "connection")
    assert not hasattr(ring, "repository")
    ring.add(event(0, T0))
    assert len(ring) == 1
