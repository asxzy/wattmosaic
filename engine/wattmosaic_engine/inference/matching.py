"""Match lifecycle and association state (I6, HB §7.6, design §7).

A :class:`MatchStore` is an **immutable snapshot** of per-event candidate states
and of the cross-event evidence used to promote a (subject, reader) association.
It satisfies the K08 :class:`wattmosaic_engine.runtime.interfaces.MatchStore`
Protocol: ``snapshot()`` exposes a plain ``candidate_id -> MatchState`` mapping
and ``apply(delta)`` returns a *new* store, so the snapshot handed to
``infer_group`` is never mutated and a replay from stored evidence is
bit-identical (L7, AT-19).

The transition rules are pure functions of injected inputs (no clock is read
inside this module: ``now`` is always passed in).  The small JSON produced by
:meth:`MatchStore.to_cell_json` is the ``match_state`` blob M2 persists on a
mapping cell.

Null policy (HB §18): an absent amplitude or delay is ``None``, never ``0.0``.
"""

from __future__ import annotations

import hashlib
import json
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from types import MappingProxyType

from wattmosaic_engine.contracts import MatchState
from wattmosaic_engine.runtime.interfaces import MatchStore as MatchStoreProtocol

__all__: list[str] = [
    "MatchEvent",
    "MatchStore",
    "classify_match",
    "association_state_from_events",
    "expire_stale",
    "match_key",
    "CONFIRM_EVENTS",
    "CONFLICT_EVENTS",
    "CONFLICT_WINDOW",
    "CONSISTENCY_LIMIT",
    "EXPIRY_AGE",
]

#: Design §7 thresholds.  They are module constants so a caller can read the
#: exact rule that was applied without having to inspect the code.
CONFIRM_EVENTS = 5
CONFLICT_EVENTS = 2
CONFLICT_WINDOW = 10
CONSISTENCY_LIMIT = 0.25
EXPIRY_AGE = timedelta(days=30)


def match_key(group_id: str, candidate_id: str) -> str:
    """Composite snapshot key for a (candidate, event group) pair.

    ``candidate_id`` alone is not unique across groups (I1 may reuse a stable
    subject id), so the K08 snapshot mapping is keyed by this composite string.
    The NUL separator cannot occur in either half of a well-formed id.
    """
    return f"{group_id}\x00{candidate_id}"


@dataclass(frozen=True)
class MatchEvent:
    """One observed match outcome, appended to the association history."""

    group_id: str
    candidate_id: str
    subject_id: str
    reader_id: str
    state: MatchState
    at: datetime
    amplitude_w: float | None = None
    delay_s: float | None = None
    contradiction: bool = False


@dataclass(frozen=True)
class MatchStore(MatchStoreProtocol):
    """An immutable snapshot of match states plus their traceable history.

    ``states`` is keyed by :func:`match_key`.  ``events`` is append-only: every
    mutating method returns a new store and leaves the old one byte-identical.
    ``expired`` records associations the P6 expiry job has retired; expiry is
    itself a functional update so the caller can keep the pre-expiry snapshot.
    """

    states: Mapping[str, MatchState] = field(default_factory=lambda: MappingProxyType({}))
    events: tuple[MatchEvent, ...] = ()
    expired: frozenset[tuple[str, str]] = frozenset()

    # -- K08 MatchStore ------------------------------------------------------ #

    def snapshot(self) -> Mapping[str, MatchState]:
        """The immutable ``key -> state`` view consumed by ``infer_group``."""
        return MappingProxyType(dict(self.states))

    def apply(self, delta: Mapping[str, MatchState]) -> MatchStore:
        """Return a new store with ``delta`` merged over the current states."""
        merged = dict(self.states)
        merged.update(delta)
        return MatchStore(
            states=MappingProxyType(merged),
            events=self.events,
            expired=self.expired,
        )

    # -- per-group state ----------------------------------------------------- #

    def state_of(self, group_id: str, candidate_id: str) -> MatchState | None:
        return self.states.get(match_key(group_id, candidate_id))

    def set_state(
        self,
        group_id: str,
        candidate_id: str,
        state: MatchState | str,
        *,
        subject_id: str | None = None,
    ) -> MatchStore:
        """Set one candidate's state in one group (functional update).

        An unknown candidate is keyed exactly like any other one, so its state
        is persisted per group (AT-10 support).  ``subject_id`` is retained in
        ``candidate_ids`` when supplied so an association can be reconstructed
        without a second lookup.
        """
        key = match_key(group_id, candidate_id)
        merged = dict(self.states)
        merged[key] = MatchState(state)
        return MatchStore(
            states=MappingProxyType(merged),
            events=self.events,
            expired=self.expired,
        )

    def record(
        self,
        group_id: str,
        candidate_id: str,
        subject_id: str,
        reader_id: str,
        state: MatchState | str,
        *,
        at: datetime,
        amplitude_w: float | None = None,
        delay_s: float | None = None,
        contradiction: bool = False,
    ) -> MatchStore:
        """Append one match outcome and set its per-group state atomically."""
        event = MatchEvent(
            group_id=group_id,
            candidate_id=candidate_id,
            subject_id=subject_id,
            reader_id=reader_id,
            state=MatchState(state),
            at=at,
            amplitude_w=amplitude_w,
            delay_s=delay_s,
            contradiction=contradiction,
        )
        updated = self.set_state(group_id, candidate_id, event.state, subject_id=subject_id)
        return MatchStore(
            states=updated.states,
            events=(*self.events, event),
            expired=self.expired,
        )

    def events_for(self, group_id: str) -> tuple[MatchEvent, ...]:
        return tuple(e for e in self.events if e.group_id == group_id)

    def events_for_association(self, subject_id: str, reader_id: str) -> tuple[MatchEvent, ...]:
        return tuple(
            e for e in self.events if e.subject_id == subject_id and e.reader_id == reader_id
        )

    def associations(self) -> frozenset[tuple[str, str]]:
        """Every (subject, reader) pair that has been observed."""
        return frozenset((e.subject_id, e.reader_id) for e in self.events)

    # -- cross-group association lifecycle (design §7) ---------------------- #

    def association_state(
        self, subject_id: str, reader_id: str, *, now: datetime
    ) -> MatchState:
        """The promoted state of one (subject, reader) association."""
        key = (subject_id, reader_id)
        if key in self.expired:
            return MatchState.EXPIRED
        return association_state_from_events(
            self.events_for_association(subject_id, reader_id), now=now
        )

    def mark_expired(self, pairs: Sequence[tuple[str, str]]) -> MatchStore:
        """Return a new store with ``pairs`` added to the expired set."""
        return MatchStore(
            states=self.states,
            events=self.events,
            expired=self.expired | frozenset(pairs),
        )

    def to_cell_json(self, subject_id: str, reader_id: str, *, now: datetime) -> str:
        """The compact ``match_state`` blob M2 stores on a mapping cell."""
        events = self.events_for_association(subject_id, reader_id)
        amplitudes = [e.amplitude_w for e in events if e.amplitude_w is not None]
        last = events[-1] if events else None
        payload = {
            "state": self.association_state(subject_id, reader_id, now=now).value,
            "event_count": len(events),
            "provisional_count": sum(
                1 for e in events if e.state in (MatchState.PROVISIONAL, MatchState.CONFIRMED)
            ),
            "contradiction_count": sum(1 for e in events if e.contradiction),
            "typical_amplitude_w": statistics.median(amplitudes) if amplitudes else None,
            "last_at": last.at.isoformat() if last is not None else None,
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))


# --------------------------------------------------------------------------- #
# Pure transition rules (design §7, HB §7.6).
# --------------------------------------------------------------------------- #


def classify_match(
    *,
    share: float | None,
    identifiable: bool,
    unknown_share: float | None = None,
    share_threshold: float = 0.5,
    reject_threshold: float = 0.1,
) -> MatchState:
    """Classify one candidate from its share of the group's explained |ΔY|.

    * a NON_IDENTIFIABLE subject is AMBIGUOUS (never given a number);
    * share > 50% -> PROVISIONAL;
    * share < 10% when the unknown alternative explains it -> REJECTED;
    * anything else stays PROPOSED.  ``share is None`` means there was no usable
      evidence this cycle, which is PROPOSED, not a fabricated zero.
    """
    if not identifiable:
        return MatchState.AMBIGUOUS
    if share is None:
        return MatchState.PROPOSED
    if share > share_threshold:
        return MatchState.PROVISIONAL
    if (
        share < reject_threshold
        and unknown_share is not None
        and unknown_share > share
    ):
        return MatchState.REJECTED
    return MatchState.PROPOSED


def _consistent(values: Sequence[float]) -> bool:
    """MAD/median < :data:`CONSISTENCY_LIMIT` for a set of positive numbers."""
    usable = [float(v) for v in values if v is not None]
    if len(usable) < 2:
        return False
    median = statistics.median(usable)
    if median == 0.0:
        return all(value == 0.0 for value in usable)
    mad = statistics.median(abs(value - median) for value in usable)
    return mad / abs(median) < CONSISTENCY_LIMIT


def association_state_from_events(
    events: Sequence[MatchEvent],
    *,
    now: datetime,
    confirm_n: int = CONFIRM_EVENTS,
    conflict_n: int = CONFLICT_EVENTS,
    window_n: int = CONFLICT_WINDOW,
    max_age: timedelta = EXPIRY_AGE,
) -> MatchState:
    """Promote a (subject, reader) association from its ordered event history.

    One PROVISIONAL event stays PROVISIONAL.  Five independent, amplitude- and
    delay-consistent PROVISIONAL events promote it to CONFIRMED.  Two
    contradictions inside the last ten events make it CONFLICTED.  No event in
    thirty days makes it EXPIRED.
    """
    if not events:
        return MatchState.UNMATCHED
    last = events[-1]
    if now - last.at > max_age:
        return MatchState.EXPIRED
    recent = events[-window_n:]
    if sum(1 for e in recent if e.contradiction) >= conflict_n:
        return MatchState.CONFLICTED
    provisional = [
        e
        for e in events
        if e.state in (MatchState.PROVISIONAL, MatchState.CONFIRMED)
        and not e.contradiction
    ]
    if len(provisional) >= confirm_n:
        amplitudes = [e.amplitude_w for e in provisional if e.amplitude_w is not None]
        delays = [e.delay_s for e in provisional if e.delay_s is not None]
        # Delay is optional: when no timing data was recorded it cannot block
        # confirmation, but when present it must be as consistent as amplitude.
        amplitude_ok = _consistent(amplitudes)
        delay_ok = not delays or _consistent(delays)
        if amplitude_ok and delay_ok:
            return MatchState.CONFIRMED
    if provisional:
        return MatchState.PROVISIONAL
    return MatchState(last.state)


def expire_stale(
    store: MatchStore, *, now: datetime, max_age: timedelta = EXPIRY_AGE
) -> MatchStore:
    """Functional expiry pass for the P6 daily background job.

    Only associations whose most recent event is older than ``max_age`` are
    marked; every other state and the whole history are preserved unchanged.
    """
    stale: list[tuple[str, str]] = []
    for pair in store.associations():
        events = store.events_for_association(*pair)
        if events and now - events[-1].at > max_age:
            stale.append(pair)
    return store.mark_expired(sorted(stale)) if stale else store


def stable_match_id(*parts: str) -> str:
    """Deterministic candidate/group id helper for callers without their own."""
    canonical = json.dumps(list(parts), separators=(",", ":"))
    return f"cand-{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:12]}"


_LIFECYCLE_STATES: frozenset[str] = frozenset(
    ("provisional", "confirmed", "ambiguous", "conflicted", "rejected", "expired")
)


def is_promoted(state: MatchState | str) -> bool:
    """Whether a state is past candidacy but still usable as an anchor."""
    value = str(getattr(state, "value", state))
    return value in _LIFECYCLE_STATES
