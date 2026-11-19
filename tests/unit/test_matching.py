"""I6: match lifecycle (HB §7.6, design §7) and immutable MatchStore snapshots.

Covers the task's test bullets:

* 1 event gives PROVISIONAL; 5 consistent events give CONFIRMED;
* a contradiction gives CONFLICTED; 30 days idle gives EXPIRED;
* the unknown candidate's state is persisted per group (AT-10 support);
* K08 ``MatchStore`` immutability: ``apply`` returns a fresh store;
* replay determinism: the same event history gives the same state;
* the ``match_state`` cell JSON uses ``None`` (never 0) for an absent amplitude.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from wattmosaic_engine.contracts import MatchState
from wattmosaic_engine.inference import matching
from wattmosaic_engine.runtime.interfaces import MatchStore as MatchStoreProtocol

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _event(
    *,
    group: str = "g1",
    candidate: str = "cand-1",
    subject: str = "device-1",
    reader: str = "ct_main",
    state: MatchState = MatchState.PROVISIONAL,
    at: datetime = T0,
    amplitude: float | None = 100.0,
    delay: float | None = None,
    contradiction: bool = False,
) -> matching.MatchEvent:
    return matching.MatchEvent(
        group_id=group,
        candidate_id=candidate,
        subject_id=subject,
        reader_id=reader,
        state=state,
        at=at,
        amplitude_w=amplitude,
        delay_s=delay,
        contradiction=contradiction,
    )


def test_store_satisfies_k08_protocol() -> None:
    store = matching.MatchStore()
    assert isinstance(store, MatchStoreProtocol)
    assert dict(store.snapshot()) == {}


def test_apply_returns_a_new_store_and_never_mutates() -> None:
    store = matching.MatchStore()
    delta = {matching.match_key("g1", "cand-1"): MatchState.PROPOSED}
    updated = store.apply(delta)
    assert updated is not store
    assert store.snapshot() == {}
    assert updated.snapshot()[matching.match_key("g1", "cand-1")] == MatchState.PROPOSED
    assert dict(updated.snapshot()) is not store.snapshot()


def test_one_event_gives_provisional() -> None:
    store = matching.MatchStore().record(
        "g1",
        "cand-1",
        "device-1",
        "ct_main",
        MatchState.PROVISIONAL,
        at=T0,
        amplitude_w=100.0,
    )
    assert store.state_of("g1", "cand-1") == MatchState.PROVISIONAL
    assert store.association_state("device-1", "ct_main", now=T0) == MatchState.PROVISIONAL


def test_five_consistent_events_give_confirmed() -> None:
    store = matching.MatchStore()
    for i in range(5):
        store = store.record(
            f"g{i}",
            f"cand-{i}",
            "device-1",
            "ct_main",
            MatchState.PROVISIONAL,
            at=T0 + timedelta(minutes=i),
            amplitude_w=100.0 + (i - 2) * 0.5,  # MAD/median well below 0.25
            delay_s=1.0,
        )
    assert store.association_state("device-1", "ct_main", now=T0 + timedelta(hours=1)) == (
        MatchState.CONFIRMED
    )


def test_four_events_stay_provisional() -> None:
    store = matching.MatchStore()
    for i in range(4):
        store = store.record(
            f"g{i}",
            f"cand-{i}",
            "device-1",
            "ct_main",
            MatchState.PROVISIONAL,
            at=T0 + timedelta(minutes=i),
            amplitude_w=100.0,
        )
    assert store.association_state("device-1", "ct_main", now=T0 + timedelta(hours=1)) == (
        MatchState.PROVISIONAL
    )


def test_inconsistent_amplitude_does_not_confirm() -> None:
    store = matching.MatchStore()
    for i, amplitude in enumerate((100.0, 200.0, 50.0, 400.0, 25.0)):
        store = store.record(
            f"g{i}",
            f"cand-{i}",
            "device-1",
            "ct_main",
            MatchState.PROVISIONAL,
            at=T0 + timedelta(minutes=i),
            amplitude_w=amplitude,
        )
    assert store.association_state("device-1", "ct_main", now=T0 + timedelta(hours=1)) == (
        MatchState.PROVISIONAL
    )


def test_two_contradictions_in_last_ten_give_conflicted() -> None:
    store = matching.MatchStore()
    for i in range(8):
        store = store.record(
            f"g{i}",
            f"cand-{i}",
            "device-1",
            "ct_main",
            MatchState.PROVISIONAL,
            at=T0 + timedelta(minutes=i),
            amplitude_w=100.0,
        )
    store = store.record(
        "g8",
        "cand-8",
        "device-1",
        "ct_main",
        MatchState.CONFLICTED,
        at=T0 + timedelta(minutes=8),
        contradiction=True,
    )
    store = store.record(
        "g9",
        "cand-9",
        "device-1",
        "ct_main",
        MatchState.CONFLICTED,
        at=T0 + timedelta(minutes=9),
        contradiction=True,
    )
    assert store.association_state("device-1", "ct_main", now=T0 + timedelta(hours=1)) == (
        MatchState.CONFLICTED
    )


def test_expiry_after_thirty_days() -> None:
    store = matching.MatchStore().record(
        "g1",
        "cand-1",
        "device-1",
        "ct_main",
        MatchState.PROVISIONAL,
        at=T0,
        amplitude_w=100.0,
    )
    later = T0 + timedelta(days=31)
    assert store.association_state("device-1", "ct_main", now=later) == MatchState.EXPIRED

    expired = matching.expire_stale(store, now=later)
    assert expired is not store
    assert ("device-1", "ct_main") in expired.expired
    assert expired.association_state("device-1", "ct_main", now=later) == MatchState.EXPIRED
    # The pre-expiry snapshot is untouched (replay determinism).
    assert store.expired == frozenset()


def test_unknown_candidate_state_persisted_per_group() -> None:
    """AT-10 support: the unknown scope has a state keyed by group."""
    store = matching.MatchStore()
    store = store.set_state("g1", "unknown-scope::ct_a", MatchState.PROPOSED)
    store = store.set_state("g2", "unknown-scope::ct_a", MatchState.PROVISIONAL)
    assert store.state_of("g1", "unknown-scope::ct_a") == MatchState.PROPOSED
    assert store.state_of("g2", "unknown-scope::ct_a") == MatchState.PROVISIONAL

    store = store.record(
        "g2",
        "unknown-scope::ct_a",
        "unknown-scope",
        "ct_a",
        MatchState.PROVISIONAL,
        at=T0,
        amplitude_w=45.0,
    )
    assert len(store.events_for("g2")) == 1
    assert len(store.events_for("g1")) == 0


def test_classify_match_rules() -> None:
    assert (
        matching.classify_match(share=0.6, identifiable=True) == MatchState.PROVISIONAL
    )
    assert (
        matching.classify_match(share=0.05, identifiable=True, unknown_share=0.8)
        == MatchState.REJECTED
    )
    assert (
        matching.classify_match(share=0.3, identifiable=True) == MatchState.PROPOSED
    )
    assert (
        matching.classify_match(share=0.9, identifiable=False) == MatchState.AMBIGUOUS
    )
    # Absent share is PROPOSED, never a fabricated zero that could reject.
    assert matching.classify_match(share=None, identifiable=True) == MatchState.PROPOSED


def test_cell_json_uses_none_for_absent_amplitude() -> None:
    store = matching.MatchStore().record(
        "g1",
        "cand-1",
        "device-1",
        "ct_main",
        MatchState.PROVISIONAL,
        at=T0,
        amplitude_w=None,
    )
    cell = store.to_cell_json("device-1", "ct_main", now=T0)
    assert '"typical_amplitude_w":null' in cell
    assert '0.0' not in cell


def test_history_is_traceable_and_replay_is_deterministic() -> None:
    events = [
        _event(group="g1", candidate="c1", state=MatchState.PROVISIONAL, at=T0),
        _event(
            group="g2",
            candidate="c2",
            state=MatchState.REJECTED,
            at=T0 + timedelta(seconds=1),
            contradiction=True,
        ),
    ]
    a = matching.MatchStore()
    b = matching.MatchStore()
    for event in events:
        kwargs = {
            "at": event.at,
            "amplitude_w": event.amplitude_w,
            "delay_s": event.delay_s,
            "contradiction": event.contradiction,
        }
        a = a.record(
            event.group_id,
            event.candidate_id,
            event.subject_id,
            event.reader_id,
            event.state,
            **kwargs,
        )
        b = b.record(
            event.group_id,
            event.candidate_id,
            event.subject_id,
            event.reader_id,
            event.state,
            **kwargs,
        )
    assert a == b
    assert a.snapshot() == b.snapshot()
    assert [e.group_id for e in a.events] == ["g1", "g2"]


def test_empty_store_is_unmatched() -> None:
    assert (
        matching.association_state_from_events([], now=T0) == MatchState.UNMATCHED
    )


def test_is_promoted() -> None:
    assert matching.is_promoted(MatchState.CONFIRMED)
    assert matching.is_promoted("provisional")
    assert not matching.is_promoted(MatchState.UNMATCHED)
    assert not matching.is_promoted(MatchState.PROPOSED)
