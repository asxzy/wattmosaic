"""Bounded in-memory ring of HA events for unlinked entities (DEC-17).

Nothing here is persisted: events that join an Event Group that becomes
evidence are persisted by P5, not by this ring.  The ring is bounded by both
``capacity`` (10 000) and ``max_age_s`` (30 min), whichever is smaller — an
event is retained only while it satisfies both bounds.

Pure: all time comes from the explicit ``now`` argument to :meth:`add` /
:meth:`prune` (defaulting to the newest event's time when the ring is non-empty),
so replay is deterministic and there is no wall-clock dependency in tests.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from datetime import datetime

from ..contracts import HAEvent

DEFAULT_CAPACITY = 10_000
DEFAULT_MAX_AGE_S = 30 * 60


class CandidateRing:
    """An insertion-ordered, age- and count-bounded ring of :class:`HAEvent`."""

    def __init__(
        self,
        *,
        capacity: int = DEFAULT_CAPACITY,
        max_age_s: float = DEFAULT_MAX_AGE_S,
    ) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        if max_age_s <= 0:
            raise ValueError("max_age_s must be > 0")
        self.capacity = capacity
        self.max_age_s = float(max_age_s)
        self._events: deque[HAEvent] = deque()
        self._dropped = 0

    def __len__(self) -> int:
        return len(self._events)

    @property
    def dropped_count(self) -> int:
        """How many events have been evicted (count or age bound)."""
        return self._dropped

    def add(self, event: HAEvent, *, now: datetime | None = None) -> None:
        """Append one event and evict until both bounds hold."""
        stamp = now if now is not None else (
            max((e.observed_at for e in self._events), default=event.observed_at)
        )
        self._events.append(event)
        self.prune(now=stamp)

    def prune(self, *, now: datetime) -> None:
        """Drop events older than ``max_age_s`` and then those beyond capacity."""
        cutoff = now.timestamp() - self.max_age_s
        while self._events and self._events[0].observed_at.timestamp() < cutoff:
            self._events.popleft()
            self._dropped += 1
        while len(self._events) > self.capacity:
            self._events.popleft()
            self._dropped += 1

    def window(self, since: datetime, until: datetime) -> list[HAEvent]:
        """Events whose ``observed_at`` lies in the closed ``[since, until]``."""
        return [e for e in self._events if since <= e.observed_at <= until]

    def by_context(self, context_id: str) -> list[HAEvent]:
        """All retained events sharing an HA ``context.id``, in insertion order."""
        return [e for e in self._events if e.context_id == context_id]

    def __iter__(self) -> Iterable[HAEvent]:
        return iter(tuple(self._events))


__all__ = ["CandidateRing", "DEFAULT_CAPACITY", "DEFAULT_MAX_AGE_S"]
