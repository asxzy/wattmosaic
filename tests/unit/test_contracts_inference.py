"""K04: inference contracts — candidates, estimates, groups, records, snapshot.

Run on the unmodified tree this file fails (the K04 seam is empty); the
implementation in ``contracts/inference.py`` makes it pass.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from wattmosaic_engine import contracts
from wattmosaic_engine.contracts import inference, legacy
from wattmosaic_engine.contracts.foundation import (
    Bounds,
    ConfidenceVectorV3,
    EstimateStatus,
    EvidenceRef,
    IdentifiabilityStatus,
    InferenceMethod,
    VersionTriple,
)
from wattmosaic_engine.contracts.inference import (
    CandidateHypothesis,
    CandidateSource,
    DeviceEstimate,
    DeviceGroupEstimate,
    EventGroupStatusV3,
    EventInferenceRecord,
    InferenceSnapshot,
    JointInferenceResult,
    MatchState,
    ReconciliationV3,
    UnknownEstimate,
)

SCHEMA_MODELS = [model for _stem, model in inference.SCHEMA_EXPORTS]


# --------------------------------------------------------------------------- #
# Helpers.
# --------------------------------------------------------------------------- #


def _versions() -> VersionTriple:
    return VersionTriple(
        topology_version_id="topo-1",
        mapping_version_id="map-1",
        model_set_id="mset-0123456789ab",
    )


def _now() -> datetime:
    return datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _bounds(lower: float, upper: float) -> Bounds:
    return Bounds(lower_w=lower, upper_w=upper, basis="posterior")


def _confidence() -> ConfidenceVectorV3:
    return ConfidenceVectorV3(measurement_quality=0.9)


def _estimate(**overrides: object) -> DeviceEstimate:
    data: dict[str, object] = {
        "subject_kind": "device",
        "subject_id": "dev-1",
        "point_w": 10.0,
        "bounds": _bounds(0.0, 20.0),
        "status": EstimateStatus.ESTIMATED,
        "identifiability": IdentifiabilityStatus.IDENTIFIABLE,
        "group_id": None,
        "level": None,
        "method": InferenceMethod.JOINT_DELTA,
        "allocation_rule": None,
        "confidence": _confidence(),
        "source_reader_ids": ["reader-1"],
        "evidence_refs": [EvidenceRef(kind="sample", id="s-1")],
        "versions": _versions(),
        "model_version_id": None,
        "updated_at": _now(),
        "provisional": False,
    }
    data.update(overrides)
    return DeviceEstimate(**data)  # type: ignore[arg-type]


def _group(**overrides: object) -> DeviceGroupEstimate:
    data: dict[str, object] = {
        "group_id": "grp-1",
        "member_ids": ["dev-1", "dev-2"],
        "point_w": 250.0,
        "bounds": _bounds(200.0, 300.0),
        "identifiability": IdentifiabilityStatus.GROUP_IDENTIFIABLE,
        "source_reader_ids": ["reader-1", "reader-2"],
        "evidence_refs": [EvidenceRef(kind="event_group", id="eg-1")],
        "versions": _versions(),
        "updated_at": _now(),
    }
    data.update(overrides)
    return DeviceGroupEstimate(**data)  # type: ignore[arg-type]


def _candidate() -> CandidateHypothesis:
    return CandidateHypothesis(
        candidate_id="cand-1",
        subject_kind="device",
        subject_id="dev-1",
        sources=[CandidateSource.HA_STATE],
        transition="off->on",
        evidence_terms={"A": 1.5, "B": -0.5},
        filtered=False,
        filter_reason=None,
    )


def _result(**overrides: object) -> JointInferenceResult:
    data: dict[str, object] = {
        "event_group_id": "eg-1",
        "event_group_revision": 1,
        "status": EventGroupStatusV3.CONFIRMED,
        "device_estimates": [_estimate()],
        "group_estimates": [_group()],
        "unknown_estimates": [
            UnknownEstimate(
                scope_reader_id="reader-3",
                delta_w=None,
                point_w=None,
                sigma_w=None,
                profile_id=None,
            )
        ],
        "candidates": [_candidate()],
        "matches": {"cand-1": MatchState.CONFIRMED},
        "residual_w_by_reader": {"reader-1": None},
        "versions": _versions(),
        "computed_at": _now(),
    }
    data.update(overrides)
    return JointInferenceResult(**data)  # type: ignore[arg-type]


def _reconciliation(**overrides: object) -> ReconciliationV3:
    data: dict[str, object] = {
        "main_w": 1000.0,
        "identified_w": 400.0,
        "group_w": 250.0,
        "unknown_w": 100.0,
        "residual_w": 250.0,
        "export_w": 0.0,
        "meter_spread_w": 5.0,
    }
    data.update(overrides)
    return ReconciliationV3(**data)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Package/naming invariants (contract-task rules).
# --------------------------------------------------------------------------- #


def test_no_public_name_collides_with_legacy() -> None:
    legacy_names = set(dir(legacy))
    collisions = sorted(name for name in inference.__all__ if name in legacy_names)
    assert collisions == [], f"inference __all__ names shadow legacy: {collisions}"


def test_every_exported_name_is_in_all() -> None:
    for model in SCHEMA_MODELS:
        assert model.__name__ in inference.__all__
    assert len(SCHEMA_MODELS) == len({stem for stem, _ in inference.SCHEMA_EXPORTS})


def test_names_reachable_from_facade() -> None:
    assert contracts.DeviceEstimate is DeviceEstimate
    assert contracts.InferenceSnapshot is InferenceSnapshot
    assert contracts.ReconciliationV3 is ReconciliationV3


@pytest.mark.parametrize("model", SCHEMA_MODELS, ids=lambda m: m.__name__)
def test_exported_model_round_trips_through_json(model: type) -> None:
    samples = {
        CandidateHypothesis: _candidate(),
        DeviceEstimate: _estimate(),
        DeviceGroupEstimate: _group(),
        UnknownEstimate: UnknownEstimate(
            scope_reader_id="reader-3", delta_w=None, point_w=None, sigma_w=None, profile_id=None
        ),
        JointInferenceResult: _result(),
        EventInferenceRecord: EventInferenceRecord(
            record_id="rec-1",
            event_group_id="eg-1",
            interpretation_revision=1,
            previous_record_id=None,
            result=_result(),
            reason="initial",
            created_at=_now(),
        ),
        ReconciliationV3: _reconciliation(),
        InferenceSnapshot: InferenceSnapshot(
            snapshot_id="snap-01",
            observed_at=_now(),
            computed_at=_now(),
            versions=_versions(),
            reader_measurements=[],
            device_estimates=[_estimate()],
            group_estimates=[_group()],
            residual_estimates=[],
            reconciliation=_reconciliation(),
            system_quality=legacy.DataQualityReport(),
            stale_sources=[],
            unresolved_events=[],
        ),
    }
    instance = samples[model]
    assert model.model_validate_json(instance.model_dump_json()) == instance


# --------------------------------------------------------------------------- #
# Enums.
# --------------------------------------------------------------------------- #


def test_match_states_cover_hb_7_6() -> None:
    assert {member.value for member in MatchState} == {
        "unmatched",
        "proposed",
        "provisional",
        "confirmed",
        "ambiguous",
        "conflicted",
        "rejected",
        "expired",
    }


def test_candidate_sources_cover_all_ten() -> None:
    assert {member.value for member in CandidateSource} == {
        "ha_state",
        "ha_attribute",
        "learned_profile",
        "prior_mapping",
        "plug",
        "unknown_profile",
        "topology_visible",
        "delayed_response",
        "device_group",
        "unknown_scope",
    }


def test_event_group_status_v3_covers_six_states() -> None:
    assert [member.value for member in EventGroupStatusV3] == [
        "detected",
        "matching",
        "provisional",
        "confirmed",
        "revised",
        "unresolved",
    ]


# --------------------------------------------------------------------------- #
# DeviceEstimate validators (HB §8.4 / §8.5).
# --------------------------------------------------------------------------- #


def test_allocated_requires_allocation_rule() -> None:
    good = _estimate(status=EstimateStatus.ALLOCATED, allocation_rule="declared_proportional")
    assert good.allocation_rule == "declared_proportional"
    with pytest.raises(ValidationError):
        _estimate(status=EstimateStatus.ALLOCATED, allocation_rule=None)


def test_non_allocated_forbids_allocation_rule() -> None:
    assert _estimate(status=EstimateStatus.ESTIMATED, allocation_rule=None).allocation_rule is None
    with pytest.raises(ValidationError):
        _estimate(status=EstimateStatus.ESTIMATED, allocation_rule="declared_proportional")


def test_measured_requires_direct_reading_and_one_reader() -> None:
    good = _estimate(
        status=EstimateStatus.MEASURED,
        method=InferenceMethod.DIRECT_READING,
        source_reader_ids=["reader-1"],
    )
    assert good.status == EstimateStatus.MEASURED
    with pytest.raises(ValidationError):
        _estimate(
            status=EstimateStatus.MEASURED,
            method=InferenceMethod.JOINT_DELTA,
            source_reader_ids=["reader-1"],
        )
    with pytest.raises(ValidationError):
        _estimate(
            status=EstimateStatus.MEASURED,
            method=InferenceMethod.DIRECT_READING,
            source_reader_ids=["reader-1", "reader-2"],
        )


def test_non_identifiable_member_never_gets_a_number() -> None:
    good = _estimate(
        status=EstimateStatus.UNKNOWN,
        identifiability=IdentifiabilityStatus.NON_IDENTIFIABLE,
        point_w=None,
        group_id="grp-1",
    )
    assert good.point_w is None
    # Mutation case from the task: point_w=125.0 + NON_IDENTIFIABLE must raise.
    with pytest.raises(ValidationError):
        _estimate(
            status=EstimateStatus.UNKNOWN,
            identifiability=IdentifiabilityStatus.NON_IDENTIFIABLE,
            point_w=125.0,
            group_id="grp-1",
        )


def test_insufficient_evidence_forbids_a_point() -> None:
    with pytest.raises(ValidationError):
        _estimate(
            status=EstimateStatus.UNKNOWN,
            identifiability=IdentifiabilityStatus.INSUFFICIENT_EVIDENCE,
            point_w=5.0,
        )


def test_null_point_forces_unknown_status() -> None:
    good = _estimate(status=EstimateStatus.UNKNOWN, point_w=None)
    assert good.point_w is None
    # A group member with no point is UNKNOWN too.
    assert _estimate(status=EstimateStatus.UNKNOWN, point_w=None, group_id="grp-1").point_w is None
    with pytest.raises(ValidationError):
        _estimate(status=EstimateStatus.ESTIMATED, point_w=None)


def test_group_member_non_identifiable_has_no_point() -> None:
    with pytest.raises(ValidationError):
        _estimate(
            group_id="grp-1",
            identifiability=IdentifiabilityStatus.NON_IDENTIFIABLE,
            point_w=42.0,
        )


# --------------------------------------------------------------------------- #
# DeviceGroupEstimate validators.
# --------------------------------------------------------------------------- #


def test_group_point_requires_group_identifiable_and_containing_bounds() -> None:
    good = _group(point_w=250.0, bounds=_bounds(200.0, 300.0))
    assert good.point_w == 250.0
    with pytest.raises(ValidationError):
        _group(point_w=250.0, identifiability=IdentifiabilityStatus.IDENTIFIABLE)
    with pytest.raises(ValidationError):
        _group(point_w=350.0, bounds=_bounds(200.0, 300.0))


def test_group_without_point_is_allowed() -> None:
    group = _group(point_w=None, identifiability=IdentifiabilityStatus.NON_IDENTIFIABLE)
    assert group.point_w is None


# --------------------------------------------------------------------------- #
# EventInferenceRecord version chain.
# --------------------------------------------------------------------------- #


def _record(**overrides: object) -> EventInferenceRecord:
    data: dict[str, object] = {
        "record_id": "rec-1",
        "event_group_id": "eg-1",
        "interpretation_revision": 1,
        "previous_record_id": None,
        "result": _result(),
        "reason": "initial",
        "created_at": _now(),
    }
    data.update(overrides)
    return EventInferenceRecord(**data)  # type: ignore[arg-type]


def test_first_record_revision_one_has_no_predecessor() -> None:
    record = _record()
    assert record.interpretation_revision == 1
    assert record.previous_record_id is None


def test_revision_one_with_predecessor_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _record(interpretation_revision=1, previous_record_id="rec-0")


def test_later_revision_requires_predecessor() -> None:
    revised = _record(interpretation_revision=2, previous_record_id="rec-0", reason="late_data")
    assert revised.previous_record_id == "rec-0"
    with pytest.raises(ValidationError):
        _record(interpretation_revision=2, previous_record_id=None)
    with pytest.raises(ValidationError):
        _record(interpretation_revision=0)


# --------------------------------------------------------------------------- #
# JointInferenceResult.result_id determinism.
# --------------------------------------------------------------------------- #


def test_result_id_is_deterministic_and_order_independent() -> None:
    first = _result()
    same = _result()
    assert first.result_id == same.result_id
    assert first.result_id.startswith("res-")

    reordered = _result(device_estimates=[_estimate(subject_id="dev-2"), _estimate(subject_id="dev-1")])
    assert reordered.result_id != first.result_id  # a different estimate set changes the hash

    same_estimates_reordered = _result(
        device_estimates=[
            _estimate(subject_id="dev-1", point_w=1.0),
            _estimate(subject_id="dev-2", point_w=2.0),
        ]
    )
    swapped = _result(
        device_estimates=[
            _estimate(subject_id="dev-2", point_w=2.0),
            _estimate(subject_id="dev-1", point_w=1.0),
        ]
    )
    assert same_estimates_reordered.result_id == swapped.result_id

    different_versions = _result(
        versions=VersionTriple(
            topology_version_id="topo-2",
            mapping_version_id="map-1",
            model_set_id="mset-0123456789ab",
        )
    )
    assert different_versions.result_id != first.result_id


def test_result_id_survives_json_round_trip() -> None:
    result = _result()
    assert JointInferenceResult.model_validate_json(result.model_dump_json()).result_id == result.result_id


# --------------------------------------------------------------------------- #
# ReconciliationV3 and snapshot null preservation.
# --------------------------------------------------------------------------- #


def test_reconciliation_defaults_are_all_none() -> None:
    empty = ReconciliationV3()
    assert all(value is None for value in empty.model_dump().values())
    assert empty.balance_error_w is None


def test_balance_error_is_none_if_any_term_is_none() -> None:
    assert _reconciliation(main_w=None).balance_error_w is None
    assert _reconciliation(identified_w=None).balance_error_w is None
    assert _reconciliation(group_w=None).balance_error_w is None
    assert _reconciliation(unknown_w=None).balance_error_w is None
    assert _reconciliation(residual_w=None).balance_error_w is None


def test_balance_error_computes_the_accounting_identity() -> None:
    # main == identified + group + unknown + residual -> zero error.
    assert _reconciliation().balance_error_w == pytest.approx(0.0)
    assert _reconciliation(main_w=1010.0).balance_error_w == pytest.approx(10.0)


def test_snapshot_round_trip_preserves_nulls() -> None:
    unknown_member = _estimate(
        subject_id="dev-2",
        point_w=None,
        identifiability=IdentifiabilityStatus.NON_IDENTIFIABLE,
        status=EstimateStatus.UNKNOWN,
        group_id="grp-1",
    )
    snapshot = InferenceSnapshot(
        snapshot_id="snap-01",
        observed_at=_now(),
        computed_at=_now(),
        versions=_versions(),
        reader_measurements=[],
        device_estimates=[_estimate(), unknown_member],
        group_estimates=[_group(point_w=None, identifiability=IdentifiabilityStatus.NON_IDENTIFIABLE)],
        residual_estimates=[],
        reconciliation=_reconciliation(main_w=None, identified_w=None),
        system_quality=legacy.DataQualityReport(),
        stale_sources=["reader-9"],
        unresolved_events=["eg-2"],
    )
    restored = InferenceSnapshot.model_validate_json(snapshot.model_dump_json())
    assert restored == snapshot
    assert restored.device_estimates[1].point_w is None
    assert restored.device_estimates[1].identifiability == IdentifiabilityStatus.NON_IDENTIFIABLE
    assert restored.reconciliation.main_w is None
    assert restored.reconciliation.identified_w is None
    assert restored.reconciliation.balance_error_w is None
