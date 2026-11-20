"""M3: confirmed-edge health monitor (rolling verdict window)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from wattmosaic_engine.contracts import (
    EdgeEvidence,
    RelationType,
    TopologyEdgeState,
    TopologyEdgeV3,
)
from wattmosaic_engine.electrical.topology_health import (
    HEALTH_WINDOW,
    EdgeHealth,
    HealthBook,
    HealthStatus,
    demotions,
)

T0 = datetime(2026, 11, 20, 0, 0, tzinfo=UTC)


def _record(n_support: int, n_conflict: int = 0) -> EdgeHealth:
    health = EdgeHealth(parent="p", child="c")
    for i in range(n_support):
        health = health.record("support", T0 + timedelta(seconds=i))
    for i in range(n_conflict):
        health = health.record("conflict", T0 + timedelta(seconds=100 + i))
    return health


def test_status_transitions() -> None:
    assert _record(4).status is HealthStatus.UNKNOWN
    assert _record(5).status is HealthStatus.HEALTHY
    # 0.20 is not > DEGRADED_CONFLICT_RATIO (0.20): still HEALTHY.
    assert _record(16, 4).status is HealthStatus.HEALTHY
    assert _record(15, 5).status is HealthStatus.DEGRADED
    # Window rolled: 20 s then 11 c leaves 9 s + 11 c = 0.55 -> BROKEN.
    rolled = _record(HEALTH_WINDOW, 11)
    assert len(rolled.recent) == HEALTH_WINDOW
    assert rolled.recent.count("s") == 9 and rolled.recent.count("c") == 11
    assert rolled.status is HealthStatus.BROKEN


def test_none_verdict_changes_nothing_and_bad_verdict_raises() -> None:
    health = _record(3, 1)
    assert health.record(None, T0) is health
    with pytest.raises(ValueError):
        health.record("maybe", T0)


def test_immutability_of_the_book() -> None:
    book = HealthBook()
    updated = book.record("p", "c", "support", T0)
    assert book.get("p", "c").checked == 0
    assert updated.get("p", "c").checked == 1
    assert dict(book.entries) == {}
    assert ("p", "c") in updated.entries


def test_json_round_trip_and_invalid_input() -> None:
    book = HealthBook()
    book = book.record("p", "c", "support", T0)
    book = book.record("p", "c", "conflict", T0 + timedelta(seconds=5, microseconds=123456))
    book = book.record("other", "child", "support", T0 + timedelta(hours=1))
    text = book.to_json()
    assert json.loads(text)  # deterministic JSON
    assert book.to_json() == text
    loaded = HealthBook.from_json(text)
    assert loaded == book

    assert HealthBook.from_json(None).entries == {}
    assert HealthBook.from_json("").entries == {}
    assert HealthBook.from_json("garbage").entries == {}


def _broken_book(*keys: tuple[str, str]) -> HealthBook:
    book = HealthBook()
    for parent, child in keys:
        for i in range(11):
            book = book.record(parent, child, "conflict", T0 + timedelta(seconds=i))
    return book


def _edge(
    edge_id: str,
    *,
    relation: object = RelationType.CONTAINS,
    lifecycle: object = TopologyEdgeState.STRUCTURAL,
    declared_by: str = "inference",
    pinned: bool = False,
    parent: str = "p",
    child: str = "c",
) -> TopologyEdgeV3:
    return TopologyEdgeV3(
        edge_id=edge_id,
        relation=relation,
        source_endpoint_id=parent,
        target_endpoint_id=child,
        lifecycle=lifecycle,
        declared_by=declared_by,
        evidence=EdgeEvidence(),
        valid_from=T0,
        pinned=pinned,
    )


def test_demotions_only_lists_confirmed_inferred_unpinned_broken_edges() -> None:
    book = _broken_book(("p", "c"), ("p", "user"), ("p", "pinned"), ("p", "healthy"))
    edges = [
        _edge("good"),                                              # demote
        _edge("user", declared_by="user", child="user"),             # user-declared
        _edge("pinned", pinned=True, child="pinned"),              # pinned
        _edge("candidate", lifecycle=TopologyEdgeState.CANDIDATE),    # not confirmed
        _edge("other-rel", relation=RelationType.SHARED_LOAD),      # not contains
        SimpleNamespace(
            edge_id="ns-good",
            relation="contains",
            lifecycle="structural",
            declared_by="inference",
            pinned=False,
            source_endpoint_id="p",
            target_endpoint_id="c",
        ),
    ]
    # The healthy edge is in the book but never reached BROKEN.
    assert demotions(book, edges) == [
        ("good", "health: 11/11 recent steps unmatched"),
        ("ns-good", "health: 11/11 recent steps unmatched"),
    ]


def test_prune_keeps_only_given_keys() -> None:
    book = HealthBook()
    book = book.record("p", "c", "support", T0)
    book = book.record("other", "child", "support", T0)
    pruned = book.prune([("p", "c")])
    assert set(pruned.entries) == {("p", "c")}
    assert set(book.entries) == {("p", "c"), ("other", "child")}
