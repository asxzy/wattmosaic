"""T5 — pure topology inference engine tests (asymmetric delta evidence ledger).

These build ``EventEvidence``/``EventGroup`` contracts and ``DeltaObservation``
inputs directly; P4 is not merged yet, so the HB §17 stream adapter is tested
against simulator records rather than P4 code.  No test imports ``runtime.fakes``
into production code and no test uses the wall clock.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from wattmosaic_engine.contracts import (
    EdgeLifecycleV3,
    RelationType,
    TopologyEdgeV3,
)
from wattmosaic_engine.electrical.topology_inference import (
    CONTAINMENT_MIN,
    DeltaObservation,
    TopologyEvidence,
    TopologyInferenceView,
    detect_same_conductor,
    infer_topology,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _delta(
    group_id: str,
    deltas: dict[str, float],
    *,
    at: datetime | None = None,
    cause: str | None = None,
    sigma: float = 2.0,
) -> DeltaObservation:
    return DeltaObservation(
        group_id=group_id,
        deltas={reader: (value, sigma) for reader, value in deltas.items()},
        at=at or T0,
        cause_id=cause,
    )


def _view(**kwargs) -> TopologyInferenceView:
    return TopologyInferenceView(
        clock=lambda: T0,
        at=T0,
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# AT-02: parent-only steps are neutral.
# --------------------------------------------------------------------------- #


def test_20_parent_only_steps_leave_support_conflict_and_lifecycle_unchanged():
    view = _view(
        edges=(
            TopologyEdgeV3(
                edge_id="e-main-sub",
                relation=RelationType.CONTAINS,
                source_endpoint_id="main",
                target_endpoint_id="sub",
                lifecycle=EdgeLifecycleV3.CONFIRMED,
                declared_by="user",
                beta=1.0,
                beta_basis="unity",
                evidence=TopologyEvidence(support=3, conflict=0),
                valid_from=T0,
                pinned=False,
            ),
        ),
        absolute={
            "main": [1000.0] * 30,
            "sub": [500.0] * 30,
        },
    )
    before = view.edge("main", "sub")
    assert before is not None
    for _ in range(20):
        view = view.observe_parent_only("main", delta_w=25.0)
    edge = view.edge("main", "sub")
    assert edge is not None
    assert edge.evidence.support == before.evidence.support
    assert edge.evidence.supporting == before.evidence.supporting
    assert edge.evidence.conflict == 0
    assert edge.evidence.conflicting == 0
    assert edge.evidence.parent_only == 20
    assert edge.lifecycle == EdgeLifecycleV3.CONFIRMED


def test_a_parent_only_step_in_a_group_is_neutral():
    view = _view(
        edges=(
            TopologyEdgeV3(
                edge_id="e-main-sub",
                relation=RelationType.CONTAINS,
                source_endpoint_id="main",
                target_endpoint_id="sub",
                lifecycle=EdgeLifecycleV3.CONFIRMED,
                declared_by="user",
                evidence=TopologyEvidence(support=3, conflict=0),
                valid_from=T0,
                pinned=False,
            ),
        ),
        absolute={"main": [1000.0] * 30, "sub": [500.0] * 30},
    )
    view = view.observe_group(_delta("g1", {"main": 40.0}, cause="unknown"))
    edge = view.edge("main", "sub")
    assert edge is not None
    assert edge.evidence.parent_only == 1
    assert edge.evidence.conflict == 0
    assert edge.evidence.support == 3


# --------------------------------------------------------------------------- #
# Lifecycle (DEC-10 / design §5).
# --------------------------------------------------------------------------- #


def _chain_view() -> TopologyInferenceView:
    return _view(
        absolute={"main": [1000.0] * 30, "sub": [500.0] * 30},
    )


def test_one_coincident_event_leaves_the_pair_candidate():
    view = _chain_view().observe_group(_delta("g1", {"main": 30.0, "sub": 30.0}, cause="c1"))
    edge = view.edge("main", "sub")
    assert edge is not None
    assert edge.lifecycle == EdgeLifecycleV3.CANDIDATE
    assert edge.evidence.support == 1
    assert edge.evidence.independent_causes == 1


def test_eight_mirrored_child_steps_from_two_causes_confirm():
    view = _chain_view()
    for i in range(8):
        # Two distinct causes (unknown profiles) alternate.
        view = view.observe_group(
            _delta(f"g{i}", {"main": 25.0, "sub": 25.0}, cause=f"cause-{i % 2}")
        )
    edge = view.edge("main", "sub")
    assert edge is not None
    assert edge.evidence.support == 8
    assert edge.evidence.independent_causes == 2
    assert edge.evidence.conflict_ratio == 0.0
    assert edge.lifecycle == EdgeLifecycleV3.CONFIRMED


def test_three_support_from_two_causes_becomes_provisional():
    view = _chain_view()
    for i, cause in enumerate(("a", "a", "b")):
        view = view.observe_group(_delta(f"g{i}", {"main": 10.0, "sub": 10.0}, cause=cause))
    edge = view.edge("main", "sub")
    assert edge is not None
    assert edge.lifecycle == EdgeLifecycleV3.PROVISIONAL


def test_eight_support_from_one_cause_never_confirms():
    from wattmosaic_engine.electrical.topology_inference import MIN_INDEPENDENT_CAUSES

    assert MIN_INDEPENDENT_CAUSES == 2
    view = _chain_view()
    for i in range(8):
        view = view.observe_group(_delta(f"g{i}", {"main": 25.0, "sub": 25.0}, cause="only-cause"))
    edge = view.edge("main", "sub")
    assert edge is not None
    assert edge.evidence.independent_causes == 1
    assert edge.lifecycle != EdgeLifecycleV3.CONFIRMED


def test_a_child_moving_without_the_parent_counts_against_the_edge():
    view = _chain_view()
    for i in range(8):
        view = view.observe_group(
            _delta(f"g{i}", {"main": 0.0, "sub": 25.0}, cause=f"cause-{i % 2}")
        )
    edge = view.edge("main", "sub")
    assert edge is not None
    assert edge.evidence.conflict == 8
    assert edge.evidence.support == 0
    assert edge.lifecycle == EdgeLifecycleV3.CONFLICTED


def test_versions_proposed_and_promoted_on_confirmation():
    class RecordingService:
        def __init__(self) -> None:
            self.proposed: list[tuple[list[TopologyEdgeV3], str | None]] = []
            self.promoted: list[tuple[str, str]] = []

        def propose_topology(self, edges, change_event_id):
            self.proposed.append((list(edges), change_event_id))
            return "topo-2"

        def promote_topology(self, version_id, reason):
            self.promoted.append((version_id, reason))

    service = RecordingService()
    view = _chain_view()
    view = TopologyInferenceView(
        edges=view.edges,
        clock=view.clock,
        versions=service,
        at=view.at,
        absolute=view.absolute,
    )
    for i in range(8):
        view = view.observe_group(
            _delta(f"g{i}", {"main": 25.0, "sub": 25.0}, cause=f"cause-{i % 2}")
        )
    assert len(service.proposed) == 1
    assert service.promoted[0][0] == "topo-2"
    assert view.version_id == "topo-2"


def test_pinned_and_rejected_pairs_are_never_edited():
    pinned = TopologyEdgeV3(
        edge_id="pinned",
        relation=RelationType.CONTAINS,
        source_endpoint_id="main",
        target_endpoint_id="sub",
        lifecycle=EdgeLifecycleV3.CONFIRMED,
        declared_by="user",
        evidence=TopologyEvidence(),
        valid_from=T0,
        pinned=True,
    )
    rejected = TopologyEdgeV3(
        edge_id="rejected",
        relation=RelationType.CONTAINS,
        source_endpoint_id="main",
        target_endpoint_id="other",
        lifecycle=EdgeLifecycleV3.REJECTED,
        declared_by="user",
        evidence=TopologyEvidence(),
        valid_from=T0,
        pinned=False,
    )
    view = _view(edges=(pinned, rejected), absolute={"main": [1000.0] * 30, "sub": [500.0] * 30})
    view = view.observe_group(_delta("g", {"main": 25.0, "sub": 25.0}, cause="a"))
    view = view.observe_parent_only("main", delta_w=25.0)
    assert view.edge("main", "sub") == pinned
    assert view.edge("main", "other") == rejected


# --------------------------------------------------------------------------- #
# Guards.
# --------------------------------------------------------------------------- #


def test_correlated_pair_is_never_structural():
    # Two independent readers that repeatedly co-respond in the same direction
    # but whose absolute series do not contain each other produce a CORRELATED
    # edge -- which is never returned by contains_edges().  The series cross, so
    # neither is contained by the other.
    view = _view(
        absolute={
            "a": [900.0, 1200.0, 950.0, 1250.0, 1000.0] * 2,
            "b": [1300.0, 1000.0, 1400.0, 900.0, 1350.0] * 2,
        }
    )
    for i in range(6):
        view = view.observe_group(_delta(f"g{i}", {"a": 20.0, "b": 20.0}, cause=f"c{i % 2}"))
    correlated = [e for e in view.edges if e.relation == RelationType.CORRELATED]
    assert len(correlated) == 1
    # A CORRELATED edge never becomes a structural CONTAINS edge.
    assert all(e.edge_id != correlated[0].edge_id for e in view.contains_edges())


def _relation(item) -> RelationType | str:
    return getattr(item, "value", item)


def test_branch_ct_parents_can_be_inferred_from_delta_evidence():
    """Branch CTs are no longer silently 'not inferable' (nesting.py:726-736)."""
    view = _view(absolute={"main": [1000.0] * 30, "branch": [300.0] * 30})
    for i in range(8):
        view = view.observe_group(
            _delta(f"g{i}", {"main": 30.0, "branch": 30.0}, cause=f"c{i % 2}")
        )
    edge = view.edge("main", "branch")
    assert edge is not None
    assert edge.lifecycle == EdgeLifecycleV3.CONFIRMED


def test_containment_gate_is_never_lowered():
    from wattmosaic_engine.electrical.topology_inference import _containment_gate

    assert CONTAINMENT_MIN == 0.995
    assert _containment_gate([1000.0] * 10, [1000.0] * 10) is True
    assert _containment_gate([1000.0] * 10, [1100.0] * 10) is False


def test_scaled_copy_refuses_to_promote():
    view = _view(
        absolute={
            "main": [100.0, 200.0, 300.0, 400.0, 500.0] * 2,
            "sub": [50.0, 100.0, 150.0, 200.0, 250.0] * 2,
        }
    )
    for i in range(8):
        view = view.observe_group(
            _delta(f"g{i}", {"main": 25.0, "sub": 12.5}, cause=f"c{i % 2}")
        )
    edge = view.edge("main", "sub")
    assert edge is not None
    assert edge.lifecycle != EdgeLifecycleV3.CONFIRMED


def test_unknown_basis_refuses_to_promote():
    view = _view(absolute={"main": [1000.0] * 30, "sub": [500.0] * 30})
    for i in range(8):
        view = view.observe_group(
            _delta(f"g{i}", {"main": 25.0, "sub": 25.0}, cause=f"c{i % 2}"),
            basis_by_reader={"sub": "unknown"},
        )
    edge = view.edge("main", "sub")
    assert edge is not None
    assert edge.lifecycle != EdgeLifecycleV3.CONFIRMED


def test_declared_phase_label_is_not_trusted():
    from wattmosaic_engine.electrical.topology_inference import _phase_gate

    # A "phase mismatch" with measured same-sign delta agreement passes.
    assert _phase_gate(25.0, 25.0, 2.0, 2.0, {"sub": {"phase": "l1"}, "main": {"phase": "l2"}}, "sub", "main") is True
    # A genuinely opposite delta is gated unless it agrees within noise.
    assert _phase_gate(25.0, -25.0, 1.0, 1.0, {}, "sub", "main") is False


# --------------------------------------------------------------------------- #
# Duplicate / CORRELATED edges.
# --------------------------------------------------------------------------- #


def test_same_conductor_edge_is_emitted_and_not_structural():
    series = {
        "ct_a": [200.0, 300.0, 400.0, 500.0] * 4,
        "ct_b": [201.0, 299.0, 402.0, 499.0] * 4,
    }
    edges = detect_same_conductor(series, at=T0)
    assert len(edges) >= 1
    edge = edges[0]
    assert edge.relation == RelationType.SAME_CONDUCTOR
    assert edge.lifecycle == EdgeLifecycleV3.CANDIDATE
    view = _view(edges=tuple(edges))
    assert view.contains_edges() == ()


def test_correlated_pair_never_becomes_a_contains_edge():
    view = _view(
        absolute={
            "a": [900.0, 1200.0, 950.0, 1250.0, 1000.0] * 2,
            "b": [1300.0, 1000.0, 1400.0, 900.0, 1350.0] * 2,
        }
    )
    for i in range(6):
        view = view.observe_group(_delta(f"g{i}", {"a": 30.0, "b": 30.0}, cause=f"c{i % 2}"))
    correlated_ids = {e.edge_id for e in view.edges if e.relation == RelationType.CORRELATED}
    assert correlated_ids
    assert all(e.edge_id not in correlated_ids for e in view.contains_edges())


# --------------------------------------------------------------------------- #
# HB §17 adapter (P4 is not merged; drive from EventEvidence-shaped records).
# --------------------------------------------------------------------------- #


def test_infer_topology_accepts_hb17_observation_records():
    from tools.simulator.hb17 import build

    house = build(17, minutes=6)
    view = infer_topology(house.observation_view(), clock=lambda: T0)
    # The declared wiring is recovered as pinned CONFIRMED edges.
    assert view.edge("ct_main", "ct_sub") is not None
    assert view.edge("ct_sub", "ct_a") is not None
    assert view.edge("ct_sub", "ct_b") is not None
    assert view.edge("ct_main", "ct_c") is not None
    for edge in view.contains_edges():
        assert edge.lifecycle in (EdgeLifecycleV3.CONFIRMED, EdgeLifecycleV3.CANDIDATE)


def test_infer_topology_accepts_event_group_contracts():
    from wattmosaic_engine.contracts import EventGroup, EventGroupStatus, ReaderDelta, SubEvent, SubEventKind
    from wattmosaic_engine.contracts.foundation import EvidenceRef, VersionTriple

    triple = VersionTriple(
        topology_version_id="topo-1", mapping_version_id="map-1", model_set_id="mset-x"
    )
    groups = []
    for i in range(8):
        groups.append(
            EventGroup(
                group_id=f"g{i}",
                revision=1,
                status=EventGroupStatus.CONFIRMED,
                opened_at=T0 + timedelta(minutes=i),
                closed_at=T0 + timedelta(minutes=i, seconds=30),
                sub_events=[
                    SubEvent(
                        sub_event_id=f"s{i}",
                        kind=SubEventKind.READER_STEP,
                        ref=EvidenceRef(kind="event_evidence", id=f"cause-{i % 2}"),
                        observed_at=T0 + timedelta(minutes=i),
                        timestamp_uncertainty_s=1.0,
                    )
                ],
                reader_deltas=[
                    ReaderDelta(
                        reader_id="main",
                        delta_w=25.0,
                        sigma_w=2.0,
                        completeness="complete",
                    ),
                    ReaderDelta(
                        reader_id="sub",
                        delta_w=25.0,
                        sigma_w=2.0,
                        completeness="complete",
                    ),
                ],
                versions=triple,
            )
        )
    from wattmosaic_engine.electrical.topology_inference import _containment_gate

    view = infer_topology(
        groups,
        clock=lambda: T0,
        # Feed the absolute gate via the ledger after construction.
    )
    # Without absolute series the containment gate is not applied; 8 mirrored
    # groups from 2 causes still confirm.
    edge = view.edge("main", "sub")
    assert edge is not None
    assert edge.lifecycle == EdgeLifecycleV3.CONFIRMED
    assert _containment_gate(None, None) is True


def test_missing_reader_delta_is_absent_not_zero():
    group = _delta("g1", {"main": 25.0})
    assert "sub" not in group.deltas


# -- Live mains carry concurrent changes (dev-server finding, 2026-11-20) ----- #
# On a real house every main/sub-feed step mixes the child's change with other
# loads, and a small branch step on a busy feed is often not flagged by P3.  The
# old ledger counted both as conflict, so true containments went CONFLICTED
# (36-74 % conflict) and nothing was ever suggested.


def test_a_co_step_of_a_different_size_is_neutral_not_conflict():
    view = _chain_view()
    for i in range(4):
        view = view.observe_group(
            _delta(f"g{i}", {"main": 60.0, "sub": 25.0}, cause=f"cause-{i}")
        )
    edge = view.edge("main", "sub")
    assert edge is None or (edge.evidence.conflict == 0 and edge.evidence.support == 0)


def test_a_measured_parent_level_change_counts_as_support():
    view = _chain_view()
    for i in range(3):
        view = view.observe_group(
            _delta(f"g{i}", {"sub": 25.0}, cause=f"cause-{i}"),
            observed={"main": (2.0, 24.0), "sub": (2.0, 25.0)},
        )
    edge = view.edge("main", "sub")
    assert edge is not None
    assert edge.evidence.support == 3
    assert edge.evidence.conflict == 0


def test_a_measured_flat_parent_counts_as_conflict():
    # A co-step creates the pair; a later child step under a parent measured
    # flat counts against it.  A flat parent alone never creates an edge.
    view = _chain_view().observe_group(_delta("g0", {"main": 25.0, "sub": 25.0}, cause="c0"))
    view = view.observe_group(
        _delta("g1", {"sub": 25.0}, cause="cause-1"),
        observed={"main": (2.0, 0.5), "sub": (2.0, 25.0)},
    )
    edge = view.edge("main", "sub")
    assert edge is not None
    assert edge.evidence.support == 1
    assert edge.evidence.conflict == 1
    fresh = _chain_view().observe_group(
        _delta("g2", {"sub": 25.0}, cause="cause-2"),
        observed={"main": (2.0, 0.5), "sub": (2.0, 25.0)},
    )
    assert fresh.edge("main", "sub") is None


def test_a_parent_that_moved_by_another_amount_is_neutral():
    view = _chain_view().observe_group(
        _delta("g0", {"sub": 25.0}, cause="cause-0"),
        observed={"main": (2.0, 90.0), "sub": (2.0, 25.0)},
    )
    edge = view.edge("main", "sub")
    assert edge is None or (edge.evidence.conflict == 0 and edge.evidence.support == 0)
