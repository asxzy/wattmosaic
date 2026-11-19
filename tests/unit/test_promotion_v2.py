"""L5 — promotion v2 (DEC-32, DEC-34, DEF-03; design §9).

Promotion compares candidate and active on the same frozen evidence set, needs
at least five independent confirmed events (counted per event group), and a
single event never changes an ACTIVE version.  The first version for a subject
and any class change produce a "review suggested" diagnostic, never a block.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from wattmosaic_engine.contracts import (
    AdjustableSpec,
    EvidenceRef,
    FixedSpec,
    IdentifiabilityStatus,
    LearningObservation,
    LevelStat,
    ModelBasis,
    ModelClass,
    PowerLevelV3,
    PowerModelVersion,
)
from wattmosaic_engine.learning.promotion import (
    MIN_INDEPENDENT_EVENTS,
    class_change,
    evaluate_promotion_v2,
    independent_event_count,
)
from wattmosaic_engine.learning.validation import (
    evaluate_frozen,
    per_class_accepts,
    split_by_observation_id,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _stat(median_w: float, mad_w: float = 0.2, n: int = 10) -> LevelStat:
    return LevelStat(median_w=median_w, mad_w=mad_w, n=n)


def _fixed(mid_w: float) -> FixedSpec:
    return FixedSpec(levels={PowerLevelV3.ACTIVE: _stat(mid_w)})


def _version(
    *,
    number: int,
    spec: FixedSpec | AdjustableSpec,
    model_class: ModelClass = ModelClass.FIXED,
    subject_id: str = "dev-a",
) -> PowerModelVersion:
    return PowerModelVersion(
        version_id=f"mdl-{subject_id}-{number}",
        subject_kind="device",
        subject_id=subject_id,
        number=number,
        model_class=model_class,
        basis=ModelBasis.DELTA,
        spec=spec,
        trainer_version="l5-v2",
        created_at=T0,
    )


def _observation(
    *,
    index: int,
    delta_w: float,
    group: str,
    transition: str = "active",
    confirmed: bool = True,
    control_value: float | None = None,
) -> LearningObservation:
    attributes: dict[str, object] = {}
    if control_value is not None:
        attributes["control_value"] = control_value
    return LearningObservation(
        observation_id=f"obs-{index:04d}",
        subject_kind="device",
        subject_id="dev-a",
        endpoint_id="ct-row",
        event_group_ref=EvidenceRef(kind="event_group", id=group),
        transition=transition,
        attributes=attributes,
        delta_w=delta_w,
        level_w=None,
        status="confirmed" if confirmed else "provisional",
        rejection_reason=None,
        identifiability=IdentifiabilityStatus.IDENTIFIABLE,
        basis=ModelBasis.DELTA,
        created_at=T0,
    )


def _fixed_evidence(mid_w: float = 60.0, events: int = 6, per_event: int = 2) -> list[LearningObservation]:
    observations: list[LearningObservation] = []
    index = 0
    for event in range(events):
        for _ in range(per_event):
            # MAE from the 60 W model is ~0.3 W (< 15 % relative error).
            delta = mid_w + ((event % 3) - 1) * 0.3
            observations.append(
                _observation(index=index, delta_w=delta, group=f"grp-{event}")
            )
            index += 1
    return observations


# --------------------------------------------------------------------------- #
# Validation: frozen evidence, holdout keyed by observation id, per-class rules.
# --------------------------------------------------------------------------- #


def test_split_by_observation_id_is_time_ordered_and_deterministic() -> None:
    observations = [
        _observation(index=i, delta_w=60.0, group=f"grp-{i}") for i in range(12)
    ]
    train_a, hold_a = split_by_observation_id(observations)
    train_b, hold_b = split_by_observation_id(observations)

    assert train_a == train_b
    assert hold_a == hold_b
    assert not set(train_a) & set(hold_a)
    assert len(hold_a) == 3
    # The holdout is the tail by (created_at, observation_id).
    assert max(train_a) < min(hold_a)


def test_independent_events_counted_per_event_group_not_per_sample() -> None:
    observations = _fixed_evidence(events=6, per_event=3)
    assert independent_event_count(observations) == 6
    assert independent_event_count([observations[0]]) == 1


def test_per_class_criteria_fixed_allows_mae_03_w() -> None:
    observations = _fixed_evidence()
    report = evaluate_frozen(_fixed(60.0), observations, ModelClass.FIXED)
    assert report.metrics["mae"] == pytest.approx(0.2, abs=0.05)
    accepts, reason = per_class_accepts(ModelClass.FIXED, report.metrics)
    assert accepts is True, reason


def test_per_class_criteria_rejects_high_relative_error() -> None:
    observations = _fixed_evidence(mid_w=60.0)
    report = evaluate_frozen(_fixed(90.0), observations, ModelClass.FIXED)
    accepts, _reason = per_class_accepts(ModelClass.FIXED, report.metrics)
    assert accepts is False


def test_per_class_criteria_adjustable_requires_coverage() -> None:
    metrics = {"mae": 2.0, "span": 100.0, "coverage": 0.4}
    accepts, reason = per_class_accepts(ModelClass.ADJUSTABLE, metrics)
    assert accepts is False
    assert "coverage" in reason
    metrics["coverage"] = 0.8
    accepts, _ = per_class_accepts(ModelClass.ADJUSTABLE, metrics)
    assert accepts is True


def test_per_class_criteria_kernel_nrmse_threshold() -> None:
    accepts, _ = per_class_accepts(ModelClass.RESPONSE_KERNEL, {"nrmse": 0.2})
    assert accepts is True
    accepts, _ = per_class_accepts(ModelClass.RESPONSE_KERNEL, {"nrmse": 0.5})
    assert accepts is False


def test_per_class_criteria_dynamic_support_threshold() -> None:
    accepts, _ = per_class_accepts(ModelClass.DYNAMIC, {"min_state_support": 6})
    assert accepts is True
    accepts, _ = per_class_accepts(ModelClass.DYNAMIC, {"min_state_support": 3})
    assert accepts is False


# --------------------------------------------------------------------------- #
# Promotion v2.
# --------------------------------------------------------------------------- #


def test_def03_all_on_fixed_load_with_mae_03_promotes() -> None:
    observations = _fixed_evidence()
    candidate = _version(number=1, spec=_fixed(60.0))

    decision = evaluate_promotion_v2(candidate, None, observations, frozen=False, clock=lambda: T0)

    assert decision.promoted is True, decision.reason
    assert decision.independent_events >= MIN_INDEPENDENT_EVENTS
    # First version is flagged for review, not blocked (DEC-32).
    assert decision.review_suggested is True
    assert any("review_suggested" in e.reason for e in decision.events)
    # The frozen evidence-id set is recorded on the version (design §9 step 2).
    recorded = {ref.id for ref in candidate.evidence_refs if ref.kind == "observation"}
    assert recorded == {obs.observation_id for obs in observations}


def test_candidate_worse_on_frozen_set_is_not_promoted() -> None:
    # Both models pass the absolute per-class gate; the candidate is simply
    # farther from the frozen evidence than the active version.
    observations = _fixed_evidence()
    active = _version(number=1, spec=_fixed(60.0))
    candidate = _version(number=2, spec=_fixed(61.0))

    decision = evaluate_promotion_v2(
        candidate, active, observations, frozen=False, clock=lambda: T0
    )

    assert decision.promoted is False
    assert decision.reason.startswith("candidate_not_better")
    assert active.spec == _fixed(60.0)


def test_candidate_better_on_frozen_set_is_promoted() -> None:
    observations = _fixed_evidence(mid_w=62.0)
    active = _version(number=1, spec=_fixed(59.0))
    candidate = _version(number=2, spec=_fixed(62.0))

    decision = evaluate_promotion_v2(
        candidate, active, observations, frozen=False, clock=lambda: T0
    )

    assert decision.promoted is True, decision.reason
    assert decision.review_suggested is False
    # The superseded active version is recorded as a diagnostic event.
    assert any(e.event == "superseded" for e in decision.events)


def test_single_event_never_changes_an_active_version() -> None:
    # Many samples, but only one event group: independence is per group.
    observations = [
        _observation(index=i, delta_w=62.0, group="grp-only") for i in range(20)
    ]
    active = _version(number=1, spec=_fixed(59.0))
    before = active.spec.model_dump()
    candidate = _version(number=2, spec=_fixed(62.0))

    decision = evaluate_promotion_v2(
        candidate, active, observations, frozen=False, clock=lambda: T0
    )

    assert decision.promoted is False
    assert decision.reason.startswith("insufficient_independent_events")
    assert decision.independent_events == 1
    assert active.spec.model_dump() == before


def test_freeze_blocks_promotion() -> None:
    observations = _fixed_evidence()
    candidate = _version(number=1, spec=_fixed(60.0))
    decision = evaluate_promotion_v2(candidate, None, observations, frozen=True, clock=lambda: T0)

    assert decision.promoted is False
    assert decision.reason == "frozen"
    assert any("blocked_by_frozen_model" in e.reason for e in decision.events)


def test_class_change_is_a_diagnostic_proposal_not_a_block() -> None:
    # Spread evidence with control values tracking power so the adjustable
    # candidate (knots 0->0, 1->60) fits accurately and beats the fixed active
    # model, which predicts a constant 60 W.
    observations = [
        _observation(index=i, delta_w=30.0 + (i % 6) * 12.0, group=f"grp-{i // 2}", control_value=(30.0 + (i % 6) * 12.0) / 60.0)
        for i in range(12)
    ]
    active = _version(number=1, spec=_fixed(60.0))
    candidate = _version(
        number=2,
        spec=AdjustableSpec(
            control_variable="brightness",
            knots_x=[0.0, 1.0],
            knots_w=[0.0, 60.0],
            standby=_stat(0.0),
        ),
        model_class=ModelClass.ADJUSTABLE,
    )

    assert class_change(candidate, active) is True
    decision = evaluate_promotion_v2(
        candidate, active, observations, frozen=False, clock=lambda: T0
    )

    # DEC-34: propose a class change, never apply it silently, but do not block.
    assert decision.class_change_proposal == ModelClass.ADJUSTABLE
    assert decision.review_suggested is True
    assert any("class_change_proposed" in e.reason for e in decision.events)


def test_frozen_evidence_set_is_shared_by_candidate_and_active() -> None:
    observations = _fixed_evidence(mid_w=62.0)
    active = _version(number=1, spec=_fixed(60.0))
    active.evidence_refs = [
        EvidenceRef(kind="observation", id=o.observation_id) for o in observations
    ]
    candidate = _version(number=2, spec=_fixed(62.0))

    decision = evaluate_promotion_v2(
        candidate, active, observations, frozen=False, clock=lambda: T0
    )

    candidate_ids = {r.id for r in candidate.evidence_refs}
    active_ids = {r.id for r in active.evidence_refs}
    assert decision.promoted is True
    assert candidate_ids == active_ids


def test_insufficient_independent_events_for_first_version_is_blocked() -> None:
    observations = _fixed_evidence(events=4, per_event=3)
    candidate = _version(number=1, spec=_fixed(60.0))
    decision = evaluate_promotion_v2(candidate, None, observations, frozen=False, clock=lambda: T0)
    assert decision.promoted is False
    assert decision.independent_events == 4


def test_equal_frozen_score_beats_or_equals_active() -> None:
    observations = _fixed_evidence()
    active = _version(number=1, spec=_fixed(60.0))
    candidate = _version(number=2, spec=_fixed(60.0))
    decision = evaluate_promotion_v2(
        candidate, active, observations, frozen=False, clock=lambda: T0
    )
    # "beats or equals": an identical model is allowed to promote.
    assert decision.promoted is True, decision.reason
