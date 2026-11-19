"""L4 — ordered learning quality gate v2 (design §9, HB §13.1).

The gate is pure: inputs (including the relationship-confirmation callable and
the clock-derived timestamp already on the observation) are injected, no DB and
no wall clock.  T4's ``edges_confirmed_for`` is not merged yet, so tests inject a
small callable as a fake; production code depends only on ``Callable[[str], bool]``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from wattmosaic_engine.contracts import (
    EvidenceRef,
    IdentifiabilityStatus,
    LearningObservation,
    ModelBasis,
)
from wattmosaic_engine.contracts.measurement import EventGroupStatus
from wattmosaic_engine.learning.sample_filter import (
    GateDecision,
    GateInputs,
    GateRejection,
    evaluate_gate,
)
from wattmosaic_engine.learning.thresholds import LearningThresholds

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _observation(
    *,
    subject_id: str = "dev-a",
    ident: IdentifiabilityStatus = IdentifiabilityStatus.IDENTIFIABLE,
    delta_w: float | None = 50.0,
    basis: ModelBasis = ModelBasis.DELTA,
    level_w: float | None = None,
) -> LearningObservation:
    return LearningObservation(
        observation_id="obs-1",
        subject_kind="device",
        subject_id=subject_id,
        endpoint_id="ct-row",
        event_group_ref=EvidenceRef(kind="event_group", id="grp-1"),
        transition="on",
        attributes={},
        delta_w=delta_w,
        level_w=level_w,
        status="confirmed",
        rejection_reason=None,
        identifiability=ident,
        basis=basis,
        created_at=T0,
    )


def _inputs(**overrides: object) -> GateInputs:
    data: dict[str, object] = {
        "group_status": EventGroupStatus.CONFIRMED,
        "measurement_quality_ok": True,
        "unknown_share": 0.0,
        "edges_confirmed": True,
        "relationship_suspected": False,
        "independent_event": True,
        "model_consistent": True,
        "edges_confirmed_for": lambda _subject: True,
    }
    data.update(overrides)
    return GateInputs(**data)  # type: ignore[arg-type]


def test_clean_observation_is_accepted_and_confirmed() -> None:
    decision = evaluate_gate(_observation(), _inputs(), clock=lambda: T0)
    assert decision.accepted is True
    assert decision.status == "confirmed"
    assert decision.rejection_reason is None
    assert decision.group_only is False


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"measurement_quality_ok": False}, GateRejection.MEASUREMENT_QUALITY),
        ({"unknown_share": 0.35}, GateRejection.UNKNOWN_ALTERNATIVE_SHARE),
        ({"independent_event": False}, GateRejection.NOT_INDEPENDENT),
        ({"model_consistent": False}, GateRejection.MODEL_INCONSISTENT),
    ],
)
def test_each_hard_rejection_reason_is_reachable(overrides: dict, reason: GateRejection) -> None:
    decision = evaluate_gate(_observation(), _inputs(**overrides), clock=lambda: T0)
    assert decision.accepted is False
    assert decision.status == "rejected"
    assert decision.rejection_reason == reason


def test_non_identifiable_subject_is_group_only() -> None:
    """AT-07 learning side: no per-device update; the observation feeds the group."""
    decision = evaluate_gate(
        _observation(ident=IdentifiabilityStatus.NON_IDENTIFIABLE),
        _inputs(),
        clock=lambda: T0,
    )
    assert decision.accepted is False
    assert decision.group_only is True
    assert decision.rejection_reason == GateRejection.NON_IDENTIFIABLE
    assert decision.status == "rejected"


@pytest.mark.parametrize(
    "ident",
    [
        IdentifiabilityStatus.NON_IDENTIFIABLE,
        IdentifiabilityStatus.INSUFFICIENT_EVIDENCE,
        IdentifiabilityStatus.GROUP_IDENTIFIABLE,
    ],
)
def test_non_individual_identifiability_never_updates_a_device(ident: IdentifiabilityStatus) -> None:
    decision = evaluate_gate(_observation(ident=ident), _inputs(), clock=lambda: T0)
    assert decision.accepted is False
    assert decision.group_only is True


def test_relationship_in_suspected_change_is_held_provisional() -> None:
    decision = evaluate_gate(
        _observation(),
        _inputs(relationship_suspected=True),
        clock=lambda: T0,
    )
    assert decision.status == "provisional"
    assert decision.accepted is False
    assert decision.rejection_reason == GateRejection.RELATIONSHIP_SUSPECTED_CHANGE


def test_provisional_group_is_held_provisional() -> None:
    decision = evaluate_gate(
        _observation(),
        _inputs(group_status=EventGroupStatus.PROVISIONAL),
        clock=lambda: T0,
    )
    assert decision.status == "provisional"
    assert decision.accepted is False
    assert decision.rejection_reason == GateRejection.GROUP_NOT_CONFIRMED


def test_edges_confirmed_for_callable_is_used_not_an_aggregate_flag() -> None:
    """The gate asks T4's per-subject callable, never Topology.status == CONFIRMED."""
    seen: list[str] = []

    def edges_confirmed_for(subject_id: str) -> bool:
        seen.append(subject_id)
        return False

    decision = evaluate_gate(
        _observation(),
        _inputs(edges_confirmed_for=edges_confirmed_for, edges_confirmed=None),
        clock=lambda: T0,
    )
    assert seen == ["dev-a"]
    assert decision.accepted is False
    assert decision.status == "provisional"
    assert decision.rejection_reason == GateRejection.RELATIONSHIP_SUSPECTED_CHANGE


def test_edges_confirmed_callable_true_accepts() -> None:
    decision = evaluate_gate(
        _observation(),
        _inputs(edges_confirmed=None, edges_confirmed_for=lambda _subject: True),
        clock=lambda: T0,
    )
    assert decision.accepted is True


def test_gate_order_is_deterministic() -> None:
    """When several faults exist, the first in design §9 order wins."""
    decision = evaluate_gate(
        _observation(ident=IdentifiabilityStatus.NON_IDENTIFIABLE),
        _inputs(
            measurement_quality_ok=False,
            group_status=EventGroupStatus.PROVISIONAL,
            unknown_share=0.9,
            relationship_suspected=True,
            independent_event=False,
            model_consistent=False,
        ),
        clock=lambda: T0,
    )
    assert decision.rejection_reason == GateRejection.MEASUREMENT_QUALITY
    assert len(GateRejection.order()) == 7


def test_unknown_share_threshold_is_configurable() -> None:
    thresholds = LearningThresholds(max_unknown_share=0.10)
    decision = evaluate_gate(
        _observation(),
        _inputs(unknown_share=0.15),
        clock=lambda: T0,
        thresholds=thresholds,
    )
    assert decision.rejection_reason == GateRejection.UNKNOWN_ALTERNATIVE_SHARE
    decision = evaluate_gate(
        _observation(),
        _inputs(unknown_share=0.05),
        clock=lambda: T0,
        thresholds=thresholds,
    )
    assert decision.rejection_reason is None


def test_model_consistency_sigma_threshold() -> None:
    """Within 4σ is consistent; beyond it is a possible behaviour change."""
    thresholds = LearningThresholds(model_consistency_sigma=4.0)
    within = evaluate_gate(
        _observation(),
        _inputs(model_sigma_w=10.0, model_abs_error_w=35.0),
        clock=lambda: T0,
        thresholds=thresholds,
    )
    assert within.accepted is True
    beyond = evaluate_gate(
        _observation(),
        _inputs(model_sigma_w=10.0, model_abs_error_w=45.0),
        clock=lambda: T0,
        thresholds=thresholds,
    )
    assert beyond.rejection_reason == GateRejection.MODEL_INCONSISTENT


def test_decision_applies_to_an_observation() -> None:
    decision = GateDecision(
        accepted=False,
        status="provisional",
        rejection_reason=GateRejection.RELATIONSHIP_SUSPECTED_CHANGE,
        detail="relationship edge in suspected_change",
    )
    applied = decision.apply(_observation())
    assert applied.status == "provisional"
    assert applied.rejection_reason == GateRejection.RELATIONSHIP_SUSPECTED_CHANGE.value
    assert "gate" in applied.attributes["gate_note"]
