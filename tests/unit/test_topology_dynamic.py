"""Dynamic topology edge state (ADR-0043, contract 4.0).

An edge's state is a pure function of *recent* evidence, the gates and its
current state.  It follows the evidence in both directions: a plug move
demotes an edge and a recovered wiring promotes it again.  Decay is by verdict
count, never wall clock, so every test here drives the public
:class:`TopologyInferenceView` API with synthetic verdicts and an injected
clock -- no database, no wall clock, no scenario knowledge.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from wattmosaic_engine.contracts import (
    RelationType,
    TopologyEdgeState,
    TopologyEdgeV3,
)
from wattmosaic_engine.electrical.topology_inference import (
    EVIDENCE_DECAY,
    EVIDENCE_WINDOW,
    DeltaObservation,
    TopologyEvidence,
    TopologyInferenceView,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _delta(
    group_id: str,
    deltas: dict[str, float],
    *,
    cause: str | None = None,
    at: datetime | None = None,
) -> DeltaObservation:
    return DeltaObservation(
        group_id=group_id,
        deltas={reader: (value, 2.0) for reader, value in deltas.items()},
        at=at or T0,
        cause_id=cause,
    )


def _view(**kwargs) -> TopologyInferenceView:
    return TopologyInferenceView(clock=lambda: T0, at=T0, **kwargs)


def _chain() -> TopologyInferenceView:
    """A known basis with two constant, nested readers (containment gate passes)."""
    return _view(absolute={"main": [1000.0] * 30, "sub": [500.0] * 30})


def _life(view: TopologyInferenceView, parent: str, child: str) -> TopologyEdgeState | str:
    edge = view.edge(parent, child)
    return edge.lifecycle if edge is not None else "absent"


def _supports(view: TopologyInferenceView, n: int, *, start: int = 0, step: int = 25) -> TopologyInferenceView:
    for i in range(start, start + n):
        view = view.observe_group(
            _delta(f"s{i}", {"main": float(step), "sub": float(step)}, cause=f"cause-{i % 2}")
        )
    return view


def _conflicts(view: TopologyInferenceView, n: int, *, start: int = 0, step: int = 25) -> TopologyInferenceView:
    for i in range(start, start + n):
        # The child steps, the parent holds: a decisive conflict for main→sub.
        view = view.observe_group(
            _delta(f"c{i}", {"main": 0.0, "sub": float(step)}, cause=f"cause-{i % 2}")
        )
    return view


def test_one_support_stays_candidate_twelve_clean_supports_make_it_structural() -> None:
    assert EVIDENCE_WINDOW == 20 and EVIDENCE_DECAY == 0.95
    view = _chain().observe_group(_delta("g1", {"main": 30.0, "sub": 30.0}, cause="c1"))
    edge = view.edge("main", "sub")
    assert edge is not None
    assert edge.lifecycle == TopologyEdgeState.CANDIDATE
    assert abs(edge.evidence.support_w - 1.0) < 1e-9  # one event never passes CANDIDATE
    view = _supports(view, 11, start=1)
    edge = view.edge("main", "sub")
    assert edge is not None
    assert edge.evidence.support_w >= 8.0
    assert edge.evidence.conflict_ratio == 0.0
    assert edge.lifecycle == TopologyEdgeState.STRUCTURAL


def test_moving_a_plug_demotes_then_conflicts_the_edge() -> None:
    view = _supports(_chain(), 20)
    assert _life(view, "main", "sub") == TopologyEdgeState.STRUCTURAL
    view = _conflicts(view, 8)
    # ADR-0043: the old structural decision no longer freezes the edge.  Eight
    # conflicts already push the weighted ratio past the CONFLICTED bound, so
    # the edge is out of STRUCTURAL; further conflicts keep it CONFLICTED.
    assert _life(view, "main", "sub") != TopologyEdgeState.STRUCTURAL
    view = _conflicts(view, 15, start=8)
    assert _life(view, "main", "sub") == TopologyEdgeState.CONFLICTED
    assert view.edge("main", "sub").evidence.conflict_ratio > 0.25  # type: ignore[union-attr]


def test_a_conflicted_inferred_edge_recovers_to_structural() -> None:
    view = _conflicts(_chain(), 12)
    assert _life(view, "main", "sub") == TopologyEdgeState.CONFLICTED
    ratio_before = view.edge("main", "sub").evidence.conflict_ratio  # type: ignore[union-attr]
    # Fifteen clean supports already improve the ratio; because *both* weights
    # decay, full recovery to STRUCTURAL takes a few more verdicts than the
    # conflict run that broke it (ADR-0043 decays by verdict count).
    view = _supports(view, 30, start=100)
    assert _life(view, "main", "sub") != TopologyEdgeState.CONFLICTED
    assert view.edge("main", "sub").evidence.conflict_ratio < ratio_before  # type: ignore[union-attr]
    view = _supports(view, 40, start=130)
    assert _life(view, "main", "sub") == TopologyEdgeState.STRUCTURAL


def test_parent_only_verdicts_change_neither_state_nor_weights() -> None:
    view = _supports(_chain(), 5)
    edge = view.edge("main", "sub")
    assert edge is not None
    before = (edge.lifecycle, edge.evidence.support_w, edge.evidence.conflict_w,
              edge.evidence.support, edge.evidence.conflict)
    for _ in range(50):
        view = view.observe_parent_only("main", delta_w=25.0)
    edge = view.edge("main", "sub")
    assert edge is not None
    after = (edge.lifecycle, edge.evidence.support_w, edge.evidence.conflict_w,
             edge.evidence.support, edge.evidence.conflict)
    assert after == before
    assert edge.evidence.parent_only == 50


def test_a_decided_edge_whose_evidence_falls_to_provisional_stays_provisional() -> None:
    """ADR-0043 removes the sticky structural lock (no series needed)."""
    edge = TopologyEdgeV3(
        edge_id="te-inf::main::sub",
        relation=RelationType.CONTAINS,
        source_endpoint_id="main",
        target_endpoint_id="sub",
        lifecycle=TopologyEdgeState.STRUCTURAL,
        declared_by="inference",
        beta=1.0,
        beta_basis="unity",
        # E6 §2 / ADR-0045: hysteresis now needs support_w >= 2 and a Wilson
        # conflict lower bound < 0.15.  With support_w 4 / conflict_w 2 the
        # lower bound is 0.18, so the decided edge falls to PROVISIONAL (the
        # point ratio 0.33 is still below the 0.34 PROVISIONAL bound).
        evidence=TopologyEvidence(support=4, conflict=2, support_w=4.0, conflict_w=2.0, independent_causes=3),
        valid_from=T0,
        pinned=False,
    )
    causes = {("main", "sub", "contains"): frozenset({"a", "b", "c"})}
    view = TopologyInferenceView(
        edges=(edge,),
        causes_by_edge=causes,
        clock=lambda: T0,
        at=T0,
        structure_decisions={"sub": "main"},
    )
    settled = view.observe_group(
        DeltaObservation(group_id="g", deltas={"elsewhere": (25.0, 2.0)}, at=T0 + timedelta(seconds=1))
    )
    assert _life(settled, "main", "sub") == TopologyEdgeState.PROVISIONAL


def test_a_pinned_edge_stays_frozen_and_reports_its_conflict_ratio() -> None:
    edge = TopologyEdgeV3(
        edge_id="pinned",
        relation=RelationType.CONTAINS,
        source_endpoint_id="main",
        target_endpoint_id="sub",
        lifecycle=TopologyEdgeState.STRUCTURAL,
        declared_by="inference",
        beta=1.0,
        beta_basis="unity",
        evidence=TopologyEvidence(support=8, conflict=3, support_w=8.0, conflict_w=3.0),
        valid_from=T0,
        pinned=True,
    )
    view = _view(edges=(edge,), absolute={"main": [1000.0] * 30, "sub": [500.0] * 30})
    view = _conflicts(view, 10)
    result = view.edge("main", "sub")
    assert result is not None
    assert result.lifecycle == TopologyEdgeState.STRUCTURAL  # pin is the only freeze
    assert abs(result.evidence.conflict_ratio - 3.0 / 11.0) < 1e-9


def test_a_user_declared_edge_conflicts_under_sustained_conflict_then_recovers() -> None:
    edge = TopologyEdgeV3(
        edge_id="declared",
        relation=RelationType.CONTAINS,
        source_endpoint_id="main",
        target_endpoint_id="sub",
        lifecycle=TopologyEdgeState.STRUCTURAL,
        declared_by="user",
        beta=1.0,
        beta_basis="unity",
        evidence=TopologyEvidence(),
        valid_from=T0,
        pinned=False,
    )
    view = _view(edges=(edge,), absolute={"main": [1000.0] * 30, "sub": [500.0] * 30})
    view = _conflicts(view, 8)
    assert _life(view, "main", "sub") == TopologyEdgeState.CONFLICTED
    # Once the recent ratio falls below the CONFLICTED bound the declared prior
    # restores STRUCTURAL immediately (ADR-0043).
    view = _supports(view, 16, start=200)
    assert _life(view, "main", "sub") == TopologyEdgeState.STRUCTURAL


class _RecordingVersions:
    """Counts proposals; ``current`` is unknown so debounce is the only gate."""

    def __init__(self) -> None:
        self.proposed: list[str] = []

    def current(self):
        raise RuntimeError("unknown")

    def propose_topology(self, edges, change_event_id):
        self.proposed.append("")
        return f"topo-{len(self.proposed)}"

    def promote_topology(self, version_id, reason):
        self.proposed[-1] = reason


def test_version_debounce_coalesces_two_promotions_within_900s() -> None:
    now = [T0]
    service = _RecordingVersions()
    view = TopologyInferenceView(
        clock=lambda: now[0],
        at=T0,
        versions=service,
        absolute={"main": [1000.0] * 30, "sub": [500.0] * 30, "other": [300.0] * 30, "third": [200.0] * 30},
    )
    view = _supports(view, 12)
    assert len(service.proposed) == 1
    now[0] = T0 + timedelta(seconds=100)
    view = view.observe_group(_delta("o", {"main": 25.0, "other": 25.0}, cause="x", at=now[0]))
    for i in range(11):
        now[0] = now[0] + timedelta(seconds=1)
        view = view.observe_group(
            _delta(f"o{i}", {"main": 25.0, "other": 25.0}, cause=f"x{i % 2}", at=now[0])
        )
    # The second promotion happened inside the 900 s window: coalesced.
    assert len(service.proposed) == 1
    assert _life(view, "main", "other") == TopologyEdgeState.STRUCTURAL
    now[0] = now[0] + timedelta(seconds=900)
    view = view.observe_group(_delta("late", {"main": 25.0, "third": 25.0}, cause="x", at=now[0]))
    for i in range(11):
        now[0] = now[0] + timedelta(seconds=1)
        view = view.observe_group(
            _delta(f"late{i}", {"main": 25.0, "third": 25.0}, cause=f"x{i % 2}", at=now[0])
        )
    # The window has opened: one more version is published.
    assert len(service.proposed) >= 2
