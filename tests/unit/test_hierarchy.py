"""WP8 — CT hierarchy discovery, the conservative parent/child relation (Group I).

What this file proves
---------------------
Parenthood is the single most dangerous claim in the system: a wrong edge
silently authorises a wrong total, and a wrong total is indistinguishable from a
correct one once it is stored.  So the four relation kinds are asserted to be
**separate, non-implying outputs** of the same evidence:

* ``DEVICE_CT_ASSOCIATION`` -- WP5, consumed: "this CT observes this device";
* ``CT_MEASUREMENT_OVERLAP`` -- "these two CTs respond to the same device";
* ``CIRCUIT_MEMBERSHIP`` -- "this device/CT is on this circuit" (WP6, consumed);
* ``ELECTRICAL_PARENT_CHILD`` -- "this CT's conductors are fed through that CT".

The rows exercised here:

* ``I5``/``I8`` insufficient evidence and undeterminable topology stay empty;
* the mandatory **Hot Tub** fixture (CT1 +250 W, CT2 +250 W, x2) yields an
  overlap and **zero** parent/child edges;
* a single device seen by two CTs is an overlap, never parenthood;
* a larger reading is not automatically the parent;
* correlation alone (including a consistent multiple of the same shape) is not
  parenthood;
* synthetic main -> sub-panel -> branch is recovered, with no extra edge;
* cycles (2- and 3-node), self edges, alias self edges and declared conflicts
  are rejected **with a reason**, never silently dropped;
* a partial chain leaves an unknown CT unresolved without inventing an edge;
* an ``UNKNOWN`` measurement basis blocks inference;
* relabelling the CTs leaves the inferred structure unchanged.

Fixtures come from WP2 (``tools.simulator.hot_tub`` and
``tools.simulator.labhouse.build_scenario``) and the real WP5 finder.  Ground
truth is used **only** in assertions.
"""

from __future__ import annotations

import math
import random
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from wattmosaic_engine.contracts import (
    Circuit,
    CircuitClass,
    CTChannel,
    CTType,
    IsolationStatus,
    MeasurementFactor,
    Phase,
    RelationKind,
    TopologyRelation,
    TopologyStatus,
)
from wattmosaic_engine.disaggregation.association import (
    AssociationConfig,
    AssociationFinder,
    Observation,
)
from wattmosaic_engine.disaggregation.isolation import EventIsolation
from wattmosaic_engine.electrical.factors import FactorRegistry
from wattmosaic_engine.electrical.hierarchy import (
    HierarchyConfig,
    HierarchyFinder,
    ValidationResult,
    explain,
    persist,
)

from tools.simulator.hot_tub import CT_L1, CT_L2, hot_tub
from tools.simulator.hot_tub import DEVICE_ID as TUB_ID
from tools.simulator.labhouse import build_scenario

T0 = datetime(2026, 1, 1, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# WP5 fixtures: real association relations from isolated device events
# --------------------------------------------------------------------------- #


def sample(ct_id: str, at: datetime, value: float) -> dict[str, Any]:
    return {"ct_id": ct_id, "state_timestamp": at.isoformat(), "value_w": float(value)}


def isolation(device_id: str, at: datetime) -> EventIsolation:
    return EventIsolation(
        status=IsolationStatus.ISOLATED,
        event_id=f"{device_id}@{at.isoformat()}",
        device_id=device_id,
        entity_id=f"switch.{device_id}",
        occurred_at=at,
        received_at=at + timedelta(seconds=1),
        analysis_offset_s=0.0,
        isolation_window_s=30.0,
        window_start=at,
        window_end=at + timedelta(seconds=30),
        competing_event_ids=[],
        competing_device_ids=[],
        reason="isolated",
        evidence={},
    )


def event(
    finder: AssociationFinder,
    device_id: str,
    at: datetime,
    step_w: float,
    shares: dict[str, float],
    *,
    baseline_w: float = 1000.0,
    before_n: int = 4,
    after_n: int = 6,
) -> list[Observation]:
    """One isolated device event; each CT sees ``share * step_w`` of it."""
    before: list[dict[str, Any]] = []
    after: list[dict[str, Any]] = []
    for ct_id, share in shares.items():
        for i in range(before_n):
            before.append(sample(ct_id, at - timedelta(seconds=15.0 * (before_n - i)), baseline_w))
        for i in range(after_n):
            after.append(
                sample(ct_id, at + timedelta(seconds=15.0 * i), baseline_w + step_w * share)
            )
    return finder.observe(
        isolation(device_id, at), before, after, [], device_delta_w=step_w
    )


def association_relations(
    shares: dict[str, dict[str, float]],
    *,
    steps: tuple[float, ...] = (900.0, 1200.0),
    registry: FactorRegistry | None = None,
) -> list[TopologyRelation]:
    """One isolated event per device per cycle, through the real WP5 finder."""
    finder = AssociationFinder(AssociationConfig(min_events=1))
    observations: list[Observation] = []
    for cycle_index, step in enumerate(steps):
        at = T0 + timedelta(hours=cycle_index)
        for index, (device_id, cts) in enumerate(shares.items()):
            observations.extend(
                event(finder, device_id, at + timedelta(minutes=5 * index), step, cts)
            )
    return finder.analyze(observations, registry=registry or FactorRegistry())


# --------------------------------------------------------------------------- #
# Measurement-scope series fixtures (aligned watts, index-aligned)
# --------------------------------------------------------------------------- #


def nested_series(
    n: int = 240,
) -> tuple[list[float], list[float], list[float]]:
    """``main > sub > branch`` in two distinct load regimes.

    The parent already contains the child at every instant, and the *other*
    loads behind the parent are what make the child a subset rather than a
    copy.  Two regimes matter: one regime cannot establish consistency.
    """
    parent: list[float] = []
    middle: list[float] = []
    child: list[float] = []
    for i in range(n):
        high = i >= n // 2
        parent.append((9000.0 if high else 4000.0) + (2600.0 if high else 1100.0) + 200.0)
        middle.append((2600.0 if high else 1100.0) + (900.0 if high else 350.0) + 60.0)
        child.append(900.0 if high else 350.0)
    return parent, middle, child


def overlapping_series(n: int = 240, *, seed: int = 11) -> tuple[list[float], list[float]]:
    """A sub-panel feed genuinely inside the main feed, two regimes, with noise."""
    rng = random.Random(seed)
    parent: list[float] = []
    child: list[float] = []
    for i in range(n):
        high = i >= n // 2
        child.append((2200.0 if high else 700.0) + rng.uniform(0.0, 40.0))
        parent.append((9000.0 if high else 3600.0) + child[-1] + rng.uniform(0.0, 60.0))
    return parent, child


def correlated_series(n: int = 240, *, seed: int = 5) -> tuple[list[float], list[float]]:
    """Two unrelated CTs whose shapes track each other but cross each other.

    They agree in *shape* (correlation ~1) and disagree in *level* in no
    consistent direction, which is exactly the evidence a nesting test needs and
    a correlation test does not: neither one contains the other.
    """
    rng = random.Random(seed)
    common = [1200.0 + 700.0 * math.sin(i / 7.0) for i in range(n)]
    first = [value + rng.uniform(-90.0, 90.0) for value in common]
    second = [value + rng.uniform(-90.0, 90.0) for value in common]
    return first, second


# --------------------------------------------------------------------------- #
# structural helpers
# --------------------------------------------------------------------------- #


def edge(child: str, parent: str, **kwargs: Any) -> TopologyRelation:
    payload: dict[str, Any] = {
        "relation_id": f"test:{child}->{parent}",
        "kind": RelationKind.ELECTRICAL_PARENT_CHILD,
        "subject_id": child,
        "object_id": parent,
        "status": kwargs.pop("status", TopologyStatus.CANDIDATE),
        "declared_by": kwargs.pop("declared_by", "inference"),
    }
    payload.update(kwargs)
    return TopologyRelation(**payload)


def channels(**aliases: str) -> dict[str, dict[str, Any]]:
    """Every named CT plus optional ``alias -> canonical`` declarations."""
    out: dict[str, dict[str, Any]] = {}
    for child, parent in aliases.items():
        out.setdefault(child, {"ct_type": "branch"})
        out.setdefault(parent, {"ct_type": "branch"})
        out[child]["alias_of_ct_id"] = parent
    for ct_id in ("ct_main", "ct_sub", "ct_branch", "ct_other", "ct_a", "ct_b", "ct_c"):
        out.setdefault(ct_id, {"ct_type": "branch"})
    return out


def pairs(relations: list[TopologyRelation]) -> set[tuple[str, str]]:
    return {
        (r.subject_id, r.object_id)
        for r in relations
        if RelationKind(r.kind) is RelationKind.ELECTRICAL_PARENT_CHILD
        and TopologyStatus(r.status) is not TopologyStatus.REJECTED
    }


def of_kind(relations: list[TopologyRelation], kind: RelationKind) -> list[TopologyRelation]:
    return [r for r in relations if RelationKind(r.kind) is kind]


# --------------------------------------------------------------------------- #
# Synthetic hierarchy recovery
# --------------------------------------------------------------------------- #


def test_synthetic_hierarchy_is_recovered_without_extra_edges() -> None:
    """main -> sub-panel -> branch is found once, at the right level.

    The grandparent edge (branch -> main) is deliberately *not* emitted: the
    shared response of two CTs says they are on the same path, not that they are
    directly connected.  An immediate parent is the tightest container.
    """
    main_series, sub_series, branch_series = nested_series()
    relations = association_relations(
        {
            "device_a": {"ct_branch": 0.5, "ct_sub": 0.4, "ct_main": 0.1},
            "device_b": {"ct_branch": 0.3, "ct_sub": 0.3, "ct_main": 0.4},
            "device_c": {"ct_main": 0.5},
        }
    )
    finder = HierarchyFinder()
    found = finder.infer(
        relations,
        [],
        series={
            "ct_main": main_series,
            "ct_sub": sub_series,
            "ct_branch": branch_series,
        },
    )
    assert pairs(found) == {("ct_branch", "ct_sub"), ("ct_sub", "ct_main")}
    assert finder.report["undeterminable"] == []


def test_a_partial_chain_leaves_the_unknown_ct_unresolved() -> None:
    """A -> B with C never observed: one edge, and nothing invented about C."""
    main_series, sub_series, _ = nested_series()
    relations = association_relations(
        {
            "device_a": {"ct_sub": 0.5, "ct_main": 0.5},
            "device_b": {"ct_sub": 0.4, "ct_main": 0.6},
        }
    )
    found = HierarchyFinder().infer(
        relations, [], series={"ct_main": main_series, "ct_sub": sub_series}
    )
    assert pairs(found) == {("ct_sub", "ct_main")}
    assert not any("ct_unknown" in (r.subject_id, r.object_id) for r in found)


# --------------------------------------------------------------------------- #
# Overlap vs parenthood, and the mandatory Hot Tub fixture
# --------------------------------------------------------------------------- #


def test_overlapping_measurements_are_overlap_and_nest_only_on_scope_evidence() -> None:
    """Both CTs see both loads: an overlap *and* (here) a real nesting.

    The two outputs are different relations with different kinds; the overlap
    does not assert parenthood and the parenthood does not assert a sum.
    """
    main_series, sub_series = overlapping_series()
    relations = association_relations(
        {
            "device_a": {"ct_sub": 0.5, "ct_main": 0.5},
            "device_b": {"ct_sub": 0.4, "ct_main": 0.6},
        }
    )
    finder = HierarchyFinder()
    overlaps = finder.overlaps(relations)
    found = finder.infer(
        relations, [], series={"ct_main": main_series, "ct_sub": sub_series}
    )

    assert len(overlaps) == 1
    overlap = overlaps[0]
    assert RelationKind(overlap.kind) is RelationKind.CT_MEASUREMENT_OVERLAP
    assert overlap.evidence["implies_parenthood"] is False
    assert set(overlap.evidence["devices"]) == {"device_a", "device_b"}

    assert pairs(found) == {("ct_sub", "ct_main")}
    nesting = found[0]
    assert RelationKind(nesting.kind) is RelationKind.ELECTRICAL_PARENT_CHILD
    assert nesting.evidence["accounting"]["adds_totals"] is False
    assert "double" in nesting.evidence["accounting"]["note"].lower()
    assert nesting.evidence["devices"] == ["device_a", "device_b"]


def test_hot_tub_equal_deltas_yield_overlap_only() -> None:
    """MANDATORY REGRESSION: CT1 +250, CT2 +250, x2 -- overlap, no parenthood.

    Both CTs respond to one appliance by the same amount.  That is the strongest
    possible *response* agreement, and it still says nothing about which CT feeds
    which: they are the two legs of one load.  Even when the caller supplies
    perfectly nested-looking series, a single independent device cannot meet the
    independence bar.
    """
    fixture = hot_tub("factor_two")
    published = fixture.published_table()
    ha = fixture.observations()["ha_power"]
    finder = AssociationFinder(AssociationConfig(min_events=1))
    observations: list[Observation] = []
    for index in range(2):
        at = T0 + timedelta(hours=index)
        before: list[dict[str, Any]] = []
        after: list[dict[str, Any]] = []
        for ct_id, row in published.items():
            for i in range(4):
                before.append(sample(ct_id, at - timedelta(seconds=15.0 * (4 - i)), row["before_w"]))
            for i in range(6):
                after.append(sample(ct_id, at + timedelta(seconds=15.0 * i), row["after_w"]))
        observations.extend(
            finder.observe(
                isolation(TUB_ID, at),
                before,
                after,
                [],
                device_delta_w=ha["after_w"] - ha["before_w"],
            )
        )
    rows: list[MeasurementFactor] = []
    for items in fixture.as_measurement_factors().values():
        rows.extend(items)
    registry = FactorRegistry(rows)
    relations = finder.analyze(observations, registry=registry)

    hierarchy = HierarchyFinder()
    overlaps = hierarchy.overlaps(relations, registry=registry)
    # A nested-looking series is supplied on purpose: the bar must still refuse.
    inflated = [1000.0 + i for i in range(240)]
    found = hierarchy.infer(
        relations,
        [],
        registry=registry,
        series={CT_L1: inflated, CT_L2: [value * 0.98 for value in inflated]},
    )

    assert len(overlaps) == 1
    assert {overlaps[0].subject_id, overlaps[0].object_id} == {CT_L1, CT_L2}
    assert pairs(found) == set()
    assert found == []
    assert hierarchy.report["refused"][f"{CT_L1}|{CT_L2}"] == "insufficient_independent_devices"


def test_two_cts_responding_to_one_device_are_overlap_not_parenthood() -> None:
    relations = association_relations({"only_device": {"ct_a": 0.5, "ct_b": 0.5}})
    finder = HierarchyFinder()
    series = {"ct_a": [900.0 + i for i in range(240)], "ct_b": [400.0 + i for i in range(240)]}
    overlaps = finder.overlaps(relations)
    found = finder.infer(relations, [], series=series)
    assert len(overlaps) == 1
    assert pairs(found) == set()


def test_larger_reading_is_not_automatically_the_parent() -> None:
    """The bigger CT is bigger on average and still not the container.

    It dips far below the smaller CT for a fifth of the record, so the
    containment precondition fails; magnitude alone decides nothing.
    """
    n = 240
    big = [5000.0 if i < int(n * 0.8) else 100.0 for i in range(n)]
    small = [800.0] * n
    relations = association_relations(
        {
            "device_a": {"ct_big": 0.5, "ct_small": 0.5},
            "device_b": {"ct_big": 0.5, "ct_small": 0.5},
        }
    )
    finder = HierarchyFinder()
    found = finder.infer(relations, [], series={"ct_big": big, "ct_small": small})
    assert pairs(found) == set()
    assert finder.report["refused"]


def test_correlation_alone_yields_no_edge() -> None:
    first, second = correlated_series()
    relations = association_relations(
        {
            "device_a": {"ct_first": 0.5, "ct_second": 0.5},
            "device_b": {"ct_first": 0.5, "ct_second": 0.5},
        }
    )
    found = HierarchyFinder().infer(
        relations, [], series={"ct_first": first, "ct_second": second}
    )
    assert pairs(found) == set()


def test_a_consistent_multiple_of_one_conductor_is_not_parenthood() -> None:
    """A x2-scaled copy is one wire with a factor, not two nested CTs.

    Containment holds perfectly in one direction, so the direction test alone
    would happily return an edge.  The duplicate/multiple guard is what stops a
    single conductor from acquiring a fictional parent.
    """
    rng = random.Random(3)
    full = [1200.0 + rng.uniform(-40.0, 40.0) for _ in range(240)]
    half = [value / 2.0 for value in full]
    relations = association_relations(
        {
            "device_a": {"ct_full": 0.5, "ct_half": 0.5},
            "device_b": {"ct_full": 0.5, "ct_half": 0.5},
        }
    )
    finder = HierarchyFinder()
    found = finder.infer(relations, [], series={"ct_full": full, "ct_half": half})
    assert pairs(found) == set()
    assert finder.report["refused"]["ct_full|ct_half"] == "scaled_copy_of_one_conductor"


# --------------------------------------------------------------------------- #
# Validation: cycles, self edges, aliases, declared facts
# --------------------------------------------------------------------------- #


def test_two_node_cycle_is_rejected_with_a_reason() -> None:
    proposed = [edge("ct_a", "ct_b"), edge("ct_b", "ct_a")]
    result = HierarchyFinder().validate(proposed, channels())
    assert isinstance(result, ValidationResult)
    assert pairs(list(result.valid)) == {("ct_a", "ct_b")}
    assert len(result.rejected) == 1
    rejected = result.rejected[0]
    assert TopologyStatus(rejected.status) is TopologyStatus.REJECTED
    assert "cycle" in rejected.evidence["reason"]
    assert rejected.evidence["cycle"] == ["ct_b", "ct_a", "ct_b"]


def test_three_node_cycle_is_rejected_with_a_reason() -> None:
    proposed = [edge("ct_a", "ct_b"), edge("ct_b", "ct_c"), edge("ct_c", "ct_a")]
    result = HierarchyFinder().validate(proposed, channels())
    assert pairs(list(result.valid)) == {("ct_a", "ct_b"), ("ct_b", "ct_c")}
    assert len(result.rejected) == 1
    assert result.rejected[0].evidence["reason"].startswith("cycle")
    assert set(result.rejected[0].evidence["cycle"]) == {"ct_a", "ct_b", "ct_c"}


def test_self_edge_is_rejected_not_dropped() -> None:
    """A self edge cannot even be constructed through the contract's validator.

    ``validate`` therefore accepts the raw description too and still reports the
    rejection instead of silently losing it.
    """
    with pytest.raises(ValueError):
        TopologyRelation(
            relation_id="bad",
            kind=RelationKind.ELECTRICAL_PARENT_CHILD,
            subject_id="ct_a",
            object_id="ct_a",
        )
    result = HierarchyFinder().validate(
        [
            {
                "relation_id": "bad",
                "kind": "electrical_parent_child",
                "subject_id": "ct_a",
                "object_id": "ct_a",
            }
        ],
        channels(),
    )
    assert result.valid == ()
    assert len(result.rejected) == 1
    assert result.rejected[0].evidence["reason"] == "self_edge"
    assert TopologyStatus(result.rejected[0].status) is TopologyStatus.REJECTED


def test_alias_self_edge_is_rejected() -> None:
    proposed = [edge("ct_alias", "ct_sub")]
    result = HierarchyFinder().validate(proposed, channels(ct_alias="ct_sub"))
    assert result.valid == ()
    assert len(result.rejected) == 1
    assert result.rejected[0].evidence["reason"] == "alias_self_edge"
    assert result.rejected[0].evidence["canonical_subject_id"] == "ct_sub"


def test_alias_channels_never_produce_an_edge() -> None:
    main_series, sub_series = overlapping_series()
    relations = association_relations(
        {
            "device_a": {"ct_sub": 0.5, "ct_alias": 0.5},
            "device_b": {"ct_sub": 0.5, "ct_alias": 0.5},
        }
    )
    finder = HierarchyFinder()
    found = finder.infer(
        relations,
        [],
        series={"ct_sub": sub_series, "ct_alias": main_series},
        channels=channels(ct_alias="ct_sub"),
    )
    assert pairs(found) == set()
    assert finder.report["refused"]["ct_alias|ct_sub"] == "alias_of_same_conductor"


def test_declared_user_edge_beats_a_conflicting_inference() -> None:
    declared = [
        edge(
            "ct_sub",
            "ct_main",
            relation_id="declared:sub->main",
            status=TopologyStatus.CONFIRMED,
            declared_by="user",
        )
    ]
    conflicting = edge("ct_main", "ct_sub")
    merged = HierarchyFinder().merge(declared, [conflicting])
    assert pairs([r for r in merged if TopologyStatus(r.status) is not TopologyStatus.REJECTED]) == {
        ("ct_sub", "ct_main")
    }
    rejected = [r for r in merged if TopologyStatus(r.status) is TopologyStatus.REJECTED]
    assert len(rejected) == 1
    assert rejected[0].evidence["reason"] == "contradicts_declared"

    # ``infer`` must not emit the contradiction either.
    finder = HierarchyFinder(declared=declared)
    main_series, sub_series = overlapping_series()
    relations = association_relations(
        {
            "device_a": {"ct_sub": 0.5, "ct_main": 0.5},
            "device_b": {"ct_sub": 0.4, "ct_main": 0.6},
        }
    )
    found = finder.infer(
        relations,
        [],
        declared=declared,
        series={"ct_sub": sub_series, "ct_main": main_series},
    )
    assert pairs(found) == set()
    assert finder.report["declared_wins"]


def test_merge_does_not_duplicate_a_declared_edge() -> None:
    declared = [
        edge(
            "ct_sub",
            "ct_main",
            relation_id="declared:sub->main",
            status=TopologyStatus.CONFIRMED,
            declared_by="user",
        )
    ]
    merged = HierarchyFinder().merge(declared, [edge("ct_sub", "ct_main")])
    assert len(merged) == 1
    assert TopologyStatus(merged[0].status) is TopologyStatus.CONFIRMED
    assert merged[0].declared_by == "user"


def test_merge_rejects_a_two_node_cycle() -> None:
    """D4: ``merge`` must validate the *union*, not just the new edge."""
    merged = HierarchyFinder().merge([], [edge("ct_a", "ct_b"), edge("ct_b", "ct_a")])
    assert pairs(merged) == {("ct_a", "ct_b")}
    rejected = [r for r in merged if TopologyStatus(r.status) is TopologyStatus.REJECTED]
    assert len(rejected) == 1
    assert rejected[0].subject_id == "ct_b"
    assert rejected[0].evidence["reason"] == "cycle: ct_b -> ct_a -> ct_b"
    assert rejected[0].evidence["cycle"] == ["ct_b", "ct_a", "ct_b"]
    # A rejected edge is returned, never silently dropped.
    assert len(merged) == 2


def test_merge_names_the_declared_contradiction_when_an_inversion_is_proposed() -> None:
    """A reversed declared edge is a contradiction first, a cycle second."""
    merged = HierarchyFinder().merge([edge("ct_a", "ct_b")], [edge("ct_b", "ct_a")])
    assert pairs(merged) == {("ct_a", "ct_b")}
    rejected = [r for r in merged if TopologyStatus(r.status) is TopologyStatus.REJECTED]
    assert len(rejected) == 1
    assert rejected[0].evidence["reason"] == "contradicts_declared"


def test_merge_rejects_a_three_node_cycle_created_by_two_earlier_edges() -> None:
    """Each edge is individually reasonable; the union is not."""
    merged = HierarchyFinder().merge(
        [], [edge("ct_a", "ct_b"), edge("ct_b", "ct_c"), edge("ct_c", "ct_a")]
    )
    assert pairs(merged) == {("ct_a", "ct_b"), ("ct_b", "ct_c")}
    rejected = [r for r in merged if TopologyStatus(r.status) is TopologyStatus.REJECTED]
    assert len(rejected) == 1
    assert rejected[0].subject_id == "ct_c"
    assert rejected[0].evidence["reason"] == "cycle: ct_c -> ct_a -> ct_b -> ct_c"
    assert rejected[0].evidence["cycle"] == ["ct_c", "ct_a", "ct_b", "ct_c"]


def test_merge_surfaces_a_self_contradicting_declared_set() -> None:
    """A user declaration that contradicts itself is shown, never honoured."""
    declared = [
        edge("ct_a", "ct_b", declared_by="user", status=TopologyStatus.CONFIRMED),
        edge("ct_b", "ct_a", declared_by="user", status=TopologyStatus.CONFIRMED),
    ]
    merged = HierarchyFinder().merge(declared, [])
    assert pairs(merged) == {("ct_a", "ct_b")}
    rejected = [r for r in merged if TopologyStatus(r.status) is TopologyStatus.REJECTED]
    assert len(rejected) == 1
    assert rejected[0].subject_id == "ct_b"
    assert rejected[0].evidence["reason"].startswith("declared_cycle: ")
    assert rejected[0].evidence["cycle"] == ["ct_b", "ct_a", "ct_b"]
    # The declared edge is the one that closes the loop, so it is the one shown.
    assert rejected[0].declared_by == "user"


def test_merge_accepts_a_diamond_that_is_not_a_cycle() -> None:
    """Two parents of one child, and one grandparent: legal, so it must pass."""
    declared = [edge("ct_a", "ct_top", declared_by="user")]
    inferred = [
        edge("ct_b", "ct_top"),
        edge("ct_child", "ct_a"),
        edge("ct_child", "ct_b"),
    ]
    merged = HierarchyFinder().merge(declared, inferred)
    assert pairs(merged) == {
        ("ct_a", "ct_top"),
        ("ct_b", "ct_top"),
        ("ct_child", "ct_a"),
        ("ct_child", "ct_b"),
    }
    assert not [r for r in merged if TopologyStatus(r.status) is TopologyStatus.REJECTED]


def test_parent_cycle_is_the_shared_definition_used_by_merge_and_persist() -> None:
    from wattmosaic_engine.electrical.hierarchy import parent_cycle

    assert parent_cycle([], "ct_a", "ct_a") == ["ct_a", "ct_a"]
    assert parent_cycle([("ct_a", "ct_b")], "ct_b", "ct_a") == ["ct_b", "ct_a", "ct_b"]
    assert parent_cycle([("ct_a", "ct_b"), ("ct_b", "ct_c")], "ct_c", "ct_a") == [
        "ct_c",
        "ct_a",
        "ct_b",
        "ct_c",
    ]
    assert parent_cycle([("ct_a", "ct_b")], "ct_a", "ct_c") is None


def test_persist_refuses_an_edge_that_closes_a_cycle_against_stored_rows(
    tmp_db_path: Any,
) -> None:
    """The storage guard is the last line: a hand-built edge cannot loop the graph."""
    from wattmosaic_engine.storage.database import Database

    db = Database(tmp_db_path)
    try:
        assert persist([edge("ct_a", "ct_b")], db) == 1
        with pytest.raises(ValueError, match="refusing to persist a parent/child cycle"):
            persist([edge("ct_b", "ct_a")], db)
        # Nothing from the refused batch was written.
        rows = db.relations_of_kind(RelationKind.ELECTRICAL_PARENT_CHILD)
        assert [(r["subject_id"], r["object_id"]) for r in rows] == [("ct_a", "ct_b")]

        # A two-edge batch whose *second* edge closes the loop is refused as a
        # whole, before either row is written.
        with pytest.raises(ValueError, match="cycle"):
            persist([edge("ct_c", "ct_a"), edge("ct_a", "ct_c")], db)
        rows = db.relations_of_kind(RelationKind.ELECTRICAL_PARENT_CHILD)
        assert len(rows) == 1

        # A rejected row is not part of the persisted graph, so it may not guard.
        persist([edge("ct_b", "ct_a", status=TopologyStatus.REJECTED)], db)
        with pytest.raises(ValueError):
            persist([edge("ct_b", "ct_c"), edge("ct_c", "ct_b")], db)
    finally:
        db.close()



def test_validate_rejects_an_edge_that_contradicts_a_declared_channel_fact() -> None:
    """A declared ``parent_ct_id`` is a fact; inference may not invert it."""
    declared_channels = {
        "ct_sub": {"ct_type": "branch", "parent_ct_id": "ct_main", "topology_status": "confirmed"},
        "ct_main": {"ct_type": "main"},
    }
    result = HierarchyFinder().validate([edge("ct_main", "ct_sub")], declared_channels)
    assert result.valid == ()
    assert result.rejected[0].evidence["reason"] == "contradicts_declared"


def test_validate_rejects_an_edge_to_an_unknown_channel() -> None:
    result = HierarchyFinder().validate([edge("ct_a", "ct_nope")], channels())
    assert result.valid == ()
    assert result.rejected[0].evidence["reason"] == "unknown_ct"


# --------------------------------------------------------------------------- #
# Undeterminable / blocked
# --------------------------------------------------------------------------- #


def test_undeterminable_topology_is_empty_with_a_reason() -> None:
    """No measurement-scope series means no parenthood -- never a guess."""
    relations = association_relations(
        {
            "device_a": {"ct_sub": 0.5, "ct_main": 0.5},
            "device_b": {"ct_sub": 0.4, "ct_main": 0.6},
        }
    )
    finder = HierarchyFinder()
    found = finder.infer(relations, [])
    assert found == []
    assert finder.report["reason"] == "no_measurement_scope_evidence"
    assert finder.report["explanation"]


def test_a_single_regime_is_insufficient_even_with_good_containment() -> None:
    """Consistency across *conditions* is a separate bar from containment."""
    flat_parent = [5200.0] * 240
    flat_child = [1200.0] * 240
    relations = association_relations(
        {
            "device_a": {"ct_sub": 0.5, "ct_main": 0.5},
            "device_b": {"ct_sub": 0.4, "ct_main": 0.6},
        }
    )
    finder = HierarchyFinder()
    found = finder.infer(
        relations, [], series={"ct_main": flat_parent, "ct_sub": flat_child}
    )
    assert pairs(found) == set()
    assert finder.report["refused"]["ct_main|ct_sub"] == "single_operating_condition"


def test_unknown_measurement_basis_blocks_hierarchy_inference() -> None:
    main_series, sub_series = overlapping_series()
    relations = association_relations(
        {
            "device_a": {"ct_sub": 0.5, "ct_main": 0.5},
            "device_b": {"ct_sub": 0.4, "ct_main": 0.6},
        }
    )
    registry = FactorRegistry(
        [
            MeasurementFactor(
                ct_id="ct_main",
                multiplier=2.0,
                effective_from=T0 - timedelta(days=1),
                applied_by="unknown",
                declared_by="user",
            )
        ]
    )
    finder = HierarchyFinder()
    found = finder.infer(
        relations,
        [],
        registry=registry,
        now=T0 + timedelta(hours=1),
        series={"ct_main": main_series, "ct_sub": sub_series},
    )
    assert pairs(found) == set()
    assert finder.report["refused"]["ct_main|ct_sub"] == "basis_unestablished"


# --------------------------------------------------------------------------- #
# Separation of the four relation kinds
# --------------------------------------------------------------------------- #


def test_the_four_relation_kinds_are_distinct_non_implying_outputs() -> None:
    """The same evidence produces four typed edges that assert different things."""
    main_series, sub_series = overlapping_series()
    relations = association_relations(
        {
            "device_a": {"ct_sub": 0.5, "ct_main": 0.5},
            "device_b": {"ct_sub": 0.4, "ct_main": 0.6},
        }
    )
    finder = HierarchyFinder()
    overlaps = finder.overlaps(relations)
    nesting = finder.infer(
        relations, [], series={"ct_main": main_series, "ct_sub": sub_series}
    )
    circuit = Circuit(
        circuit_id="cir_test",
        classification=CircuitClass.SHARED,
        classification_status=TopologyStatus.CANDIDATE,
        ct_ids=["ct_main", "ct_sub"],
        device_ids=["device_a", "device_b"],
    )
    membership = [
        TopologyRelation(
            relation_id=f"membership:{device}",
            kind=RelationKind.CIRCUIT_MEMBERSHIP,
            subject_id=device,
            object_id=circuit.circuit_id,
            evidence={"circuit_id": circuit.circuit_id},
        )
        for device in circuit.device_ids
    ]

    kinds = {
        RelationKind(r.kind)
        for r in [*relations, *overlaps, *nesting, *membership]
    }
    assert kinds == {
        RelationKind.DEVICE_CT_ASSOCIATION,
        RelationKind.CT_MEASUREMENT_OVERLAP,
        RelationKind.CIRCUIT_MEMBERSHIP,
        RelationKind.ELECTRICAL_PARENT_CHILD,
    }
    # Association never claims a parent, overlap never claims a parent, and the
    # parent/child edge never claims membership.
    assert pairs(list(relations)) == set()
    assert pairs(list(overlaps)) == set()
    assert all(r.evidence.get("implies_parenthood") is False for r in overlaps)
    assert all(r.evidence["circuit_id"] == "cir_test" for r in membership)
    assert nesting and nesting[0].evidence["accounting"]["adds_totals"] is False


# --------------------------------------------------------------------------- #
# Determinism / invariance / explainability / persistence
# --------------------------------------------------------------------------- #


def test_relabelling_the_cts_leaves_the_structure_unchanged() -> None:
    main_series, sub_series, branch_series = nested_series()
    shares = {
        "device_a": {"ct_branch": 0.5, "ct_sub": 0.4, "ct_main": 0.1},
        "device_b": {"ct_branch": 0.3, "ct_sub": 0.3, "ct_main": 0.4},
    }
    renamed = {
        "device_a": {"ct_x": 0.5, "ct_y": 0.4, "ct_z": 0.1},
        "device_b": {"ct_x": 0.3, "ct_y": 0.3, "ct_z": 0.4},
    }
    forward = {"ct_main": "ct_z", "ct_sub": "ct_y", "ct_branch": "ct_x"}

    first = HierarchyFinder().infer(
        association_relations(shares),
        [],
        series={
            "ct_main": main_series,
            "ct_sub": sub_series,
            "ct_branch": branch_series,
        },
    )
    second = HierarchyFinder().infer(
        association_relations(renamed),
        [],
        series={
            "ct_z": main_series,
            "ct_y": sub_series,
            "ct_x": branch_series,
        },
    )
    assert {(forward[a], forward[b]) for a, b in pairs(first)} == pairs(second)


def test_explain_cites_devices_conditions_and_margins() -> None:
    main_series, sub_series = overlapping_series()
    relations = association_relations(
        {
            "device_a": {"ct_sub": 0.5, "ct_main": 0.5},
            "device_b": {"ct_sub": 0.4, "ct_main": 0.6},
        }
    )
    finder = HierarchyFinder()
    found = finder.infer(
        relations, [], series={"ct_main": main_series, "ct_sub": sub_series}
    )
    assert len(found) == 1
    text = finder.explain(found[0])
    assert "device_a" in text and "device_b" in text
    assert "ct_sub" in text and "ct_main" in text
    assert "condition" in text
    assert "%" in text or "fraction" in text

    overlap = finder.overlaps(relations)[0]
    overlap_text = explain(overlap)
    assert "overlap" in overlap_text.lower()
    assert "not" in overlap_text.lower()


def test_persist_is_idempotent_on_relation_id(tmp_db_path: Any) -> None:
    from wattmosaic_engine.storage.database import Database

    main_series, sub_series = overlapping_series()
    relations = association_relations(
        {
            "device_a": {"ct_sub": 0.5, "ct_main": 0.5},
            "device_b": {"ct_sub": 0.4, "ct_main": 0.6},
        }
    )
    finder = HierarchyFinder()
    found = finder.infer(
        relations, [], series={"ct_main": main_series, "ct_sub": sub_series}
    )
    assert len(found) == 1

    db = Database(tmp_db_path)
    try:
        assert persist(found, db) == 1
        assert persist(found, db) == 1
        rows = db.relations_of_kind(RelationKind.ELECTRICAL_PARENT_CHILD)
        assert len(rows) == 1
        assert rows[0]["status"] == TopologyStatus.CANDIDATE.value
        assert rows[0]["subject_id"] == "ct_sub"
        assert rows[0]["object_id"] == "ct_main"
    finally:
        db.close()


def test_config_rejects_impossible_thresholds() -> None:
    with pytest.raises(ValueError):
        HierarchyConfig(min_independent_devices=1)
    with pytest.raises(ValueError):
        HierarchyConfig(max_power_violation_frac=1.5)
    with pytest.raises(ValueError):
        HierarchyConfig(min_response_consistency=0.0)


# --------------------------------------------------------------------------- #
# The WP2 fixture, end to end: overlap on a real stratum, parenthood refused
# --------------------------------------------------------------------------- #


def test_main_subpanel_stratum_never_turns_a_shared_device_into_a_parent() -> None:
    """Real WP2 observations: the sub-panel feed is not invented from responses."""
    house = build_scenario("main_subpanel_overlap", 42, minutes=30.0, interval=15.0)
    view = house.observations()
    measurements: dict[str, dict[int, float]] = {}
    for index, record in enumerate(view):
        for ct_id, reading in record["latest_measurements"].items():
            measurements.setdefault(ct_id, {})[index] = float(reading["value_w"])

    assert "ct_main_l1" in measurements and "ct_sub_l1" in measurements
    ticks = sorted(set().union(*[set(rows) for rows in measurements.values()]))
    parent = [measurements["ct_main_l1"].get(tick, math.nan) for tick in ticks]
    child = [measurements["ct_sub_l1"].get(tick, math.nan) for tick in ticks]

    # No WP5 relations exist for these two channels on this short stratum, so the
    # independence bar alone must refuse: hierarchy never runs on series alone.
    finder = HierarchyFinder()
    found = finder.infer([], [], series={"ct_main_l1": parent, "ct_sub_l1": child})
    assert found == []
    assert finder.report["reason"] == "no_device_evidence"

    # And the fixture's declared structure is what a caller may confirm, not
    # something the finder emits on its own.
    declared = [
        edge(
            "ct_sub_l1",
            "ct_main_l1",
            relation_id="declared:sub_l1->main_l1",
            status=TopologyStatus.CONFIRMED,
            declared_by="user",
        )
    ]
    merged = finder.merge(declared, found)
    assert pairs(merged) == {("ct_sub_l1", "ct_main_l1")}


def test_ct_type_and_name_are_never_used_as_evidence() -> None:
    """A channel typed ``main`` with a main-sounding name still needs evidence."""
    relations = association_relations({"device_a": {"ct_sub": 0.5, "ct_main": 0.5}})
    finder = HierarchyFinder()
    typed = {
        "ct_main": CTChannel(
            ct_id="ct_main", entity_id="sensor.ct_main_power", ct_type=CTType.MAIN, phase=Phase.L1
        ),
        "ct_sub": CTChannel(
            ct_id="ct_sub", entity_id="sensor.ct_sub_power", ct_type=CTType.BRANCH, phase=Phase.L1
        ),
    }
    found = finder.infer(
        relations,
        [],
        channels=typed,
        series={"ct_main": [5000.0] * 240, "ct_sub": [1000.0] * 240},
    )
    assert pairs(found) == set()
