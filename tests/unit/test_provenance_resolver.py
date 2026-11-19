"""D4 — the typed provenance resolver and the nine HB §18 questions.

Pure tests: all times/contracts are injected, no database or wall clock is
read, and ``runtime.fakes`` is used only as test input (never in production
code).  The central guarantee is that every typed evidence ref on a published
estimate resolves, and that a group member never acquires an individual value.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from wattmosaic_engine.contracts import (
    Bounds,
    ConfidenceVectorV3,
    DataQualityReport,
    DeviceEstimate,
    DeviceGroupEstimate,
    EstimateStatus,
    EvidenceRef,
    IdentifiabilityStatus,
    InferenceMethod,
    InferenceSnapshot,
    MeasurementType,
    ReaderMeasurement,
    ReconciliationV3,
    VersionTriple,
)
from wattmosaic_engine.provenance import (
    QUESTION_KEYS,
    EvidenceLookup,
    ProvenanceAnswer,
    ProvenanceResolver,
    SnapshotEvidenceLookup,
    answer_for,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)
VT = VersionTriple(
    topology_version_id="topo-1", mapping_version_id="map-1", model_set_id="mset-1"
)


def _measurement(reader_id: str, value: float) -> ReaderMeasurement:
    return ReaderMeasurement(
        reader_id=reader_id,
        observed_at=T0,
        received_at=T0,
        active_power_w=value,
        phase="unknown",
        measurement_type=MeasurementType.INSTANTANEOUS,
        calibration_version="test",
        measurement_basis="monitor_reported",
        quality_flags=[],
        sequence=1,
    )


def _estimate(
    subject_id: str,
    *,
    refs: list[EvidenceRef] | None = None,
    point_w: float | None = 60.0,
    status: EstimateStatus = EstimateStatus.ESTIMATED,
    ident: IdentifiabilityStatus = IdentifiabilityStatus.IDENTIFIABLE,
    method: InferenceMethod = InferenceMethod.JOINT_DELTA,
    group_id: str | None = None,
    model_version_id: str | None = "mdl-dev-1",
    source_readers: list[str] | None = None,
) -> DeviceEstimate:
    return DeviceEstimate(
        subject_kind="device",
        subject_id=subject_id,
        point_w=point_w,
        bounds=Bounds(lower_w=0.0, upper_w=100.0, basis="non_negativity"),
        status=status,
        identifiability=ident,
        group_id=group_id,
        method=method,
        model_version_id=model_version_id,
        confidence=ConfidenceVectorV3(measurement_quality=0.8),
        source_reader_ids=source_readers or ["Circuit A", "Main"],
        evidence_refs=refs or [],
        versions=VT,
        updated_at=T0,
    )


def _group(
    group_id: str = "group-1",
    *,
    members: tuple[str, ...] = ("lamp-a", "lamp-b"),
    refs: list[EvidenceRef] | None = None,
) -> DeviceGroupEstimate:
    return DeviceGroupEstimate(
        group_id=group_id,
        member_ids=list(members),
        point_w=45.0,
        bounds=Bounds(lower_w=0.0, upper_w=100.0, basis="group_sum"),
        identifiability=IdentifiabilityStatus.GROUP_IDENTIFIABLE,
        source_reader_ids=["Main"],
        evidence_refs=refs or [],
        versions=VT,
        updated_at=T0,
    )


def _snapshot(
    estimates: list[DeviceEstimate],
    groups: list[DeviceGroupEstimate] | None = None,
) -> InferenceSnapshot:
    return InferenceSnapshot(
        snapshot_id="snap-1",
        observed_at=T0,
        computed_at=T0,
        versions=VT,
        reader_measurements=[_measurement("Circuit A", 60.0), _measurement("Main", 100.0), _measurement("Subpanel", 40.0)],
        device_estimates=estimates,
        group_estimates=groups or [],
        residual_estimates=[],
        reconciliation=ReconciliationV3(main_w=100.0, identified_w=60.0),
        system_quality=DataQualityReport(timestamp=T0),
        stale_sources=[],
        unresolved_events=[],
    )


# --------------------------------------------------------------------------- #
# Evidence resolution.
# --------------------------------------------------------------------------- #


def test_snapshot_evidence_lookup_resolves_measurement_and_version_refs() -> None:
    snapshot = _snapshot([_estimate("dev-1")])
    lookup = SnapshotEvidenceLookup(snapshot)
    assert lookup.resolve(EvidenceRef(kind="sample", id="Circuit A")) == snapshot.reader_measurements[0]
    assert lookup.resolve(EvidenceRef(kind="observation", id="Main")) == snapshot.reader_measurements[1]
    assert lookup.resolve(EvidenceRef(kind="edge", id="topo-1")) == "topo-1"
    assert lookup.resolve(EvidenceRef(kind="mapping_edge", id="map-1")) == "map-1"
    assert lookup.resolve(EvidenceRef(kind="model_version", id="mset-1")) == "mset-1"
    # A reference to an unknown id is unresolved, never a fabricated value.
    assert lookup.resolve(EvidenceRef(kind="sample", id="nope")) is None
    assert lookup.resolve(EvidenceRef(kind="event_group", id="eg-1")) is None


def test_every_estimate_evidence_ref_resolves() -> None:
    snapshot = _snapshot(
        [
            _estimate(
                "dev-1",
                refs=[
                    EvidenceRef(kind="sample", id="Circuit A"),
                    EvidenceRef(kind="observation", id="Main"),
                    EvidenceRef(kind="event_group", id="eg-1"),
                ],
            )
        ]
    )
    # An event_group ref needs an external lookup; supply one so the test pins
    # that *every* ref on an estimate is resolvable (the D4 acceptance rule).
    class WithEvent(EvidenceLookup):
        def __init__(self, base: SnapshotEvidenceLookup) -> None:
            self.base = base

        def resolve(self, ref: EvidenceRef) -> Any | None:
            if ref.kind == "event_group":
                return {"group_id": ref.id, "status": "confirmed"}
            return self.base.resolve(ref)

    resolver = ProvenanceResolver(
        snapshot, lookup=WithEvent(SnapshotEvidenceLookup(snapshot))
    )
    answer = resolver.for_device(snapshot.device_estimates[0])
    assert answer.unresolved_refs == ()
    assert len(answer.reader_measurements) == 2
    assert len(answer.event_associations) == 1


def test_unresolved_refs_are_reported_not_fabricated() -> None:
    snapshot = _snapshot(
        [_estimate("dev-1", refs=[EvidenceRef(kind="sample", id="nope")])]
    )
    answer = ProvenanceResolver(snapshot).for_device(snapshot.device_estimates[0])
    assert "sample:nope" in answer.unresolved_refs
    assert answer.reader_measurements == ()


# --------------------------------------------------------------------------- #
# The nine questions for ESTIMATED, MEASURED, and a group member.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("kind", ["estimated", "measured", "group_member"])
def test_all_nine_questions_are_answered(kind: str) -> None:
    if kind == "measured":
        estimate = _estimate(
            "tv",
            refs=[EvidenceRef(kind="sample", id="Circuit A")],
            point_w=3.0,
            status=EstimateStatus.MEASURED,
            ident=IdentifiabilityStatus.IDENTIFIABLE,
            method=InferenceMethod.DIRECT_READING,
            source_readers=["Circuit A"],
        )
    elif kind == "group_member":
        estimate = _estimate(
            "lamp-a",
            refs=[EvidenceRef(kind="sample", id="Main")],
            point_w=None,
            status=EstimateStatus.UNKNOWN,
            ident=IdentifiabilityStatus.NON_IDENTIFIABLE,
            method=InferenceMethod.NONE,
            group_id="group-1",
        )
    else:
        estimate = _estimate(
            "dev-1", refs=[EvidenceRef(kind="sample", id="Circuit A")]
        )
    group = _group()
    snapshot = _snapshot([estimate], [group])
    answer = answer_for(estimate, snapshot)

    assert isinstance(answer, ProvenanceAnswer)
    assert set(QUESTION_KEYS) <= set(answer.questions)
    assert len(QUESTION_KEYS) == 9
    # Each question has a non-missing answer of the expected basic shape.
    assert isinstance(answer.answer("reader_measurements"), list)
    assert isinstance(answer.answer("event_associations"), list)
    assert answer.answer("topology_version_id") == "topo-1"
    assert answer.answer("mapping_version_id") == "map-1"
    assert answer.answer("status") is not None or kind == "group_member"
    assert answer.answer("identifiability") is not None
    bounds = answer.answer("bounds")
    assert set(bounds) == {"lower_w", "upper_w", "basis"}
    assert answer.answer("updated_at") == T0
    assert answer.model_version_id == "mdl-dev-1" or kind != "estimated"


def test_measured_question_answers_are_exact() -> None:
    estimate = _estimate(
        "tv",
        refs=[EvidenceRef(kind="sample", id="Circuit A")],
        point_w=3.0,
        status=EstimateStatus.MEASURED,
        ident=IdentifiabilityStatus.IDENTIFIABLE,
        method=InferenceMethod.DIRECT_READING,
        source_readers=["Circuit A"],
    )
    snapshot = _snapshot([estimate])
    answer = answer_for(estimate, snapshot)
    assert answer.status == "measured"
    assert answer.identifiability == "identifiable"
    assert answer.model_version_id == "mdl-dev-1"
    assert answer.lower_w == 0.0
    assert answer.upper_w == 100.0
    assert answer.updated_at == T0
    assert answer.reader_measurements[0].reader_id == "Circuit A"


def test_estimated_question_answers_are_exact() -> None:
    estimate = _estimate("dev-1", refs=[EvidenceRef(kind="sample", id="Main")])
    answer = answer_for(estimate, _snapshot([estimate]))
    assert answer.status == "estimated"
    assert answer.identifiability == "identifiable"
    assert answer.point_w == 60.0


def test_group_member_has_no_individual_value_anywhere() -> None:
    member = _estimate(
        "lamp-a",
        refs=[EvidenceRef(kind="sample", id="Main")],
        point_w=None,
        status=EstimateStatus.UNKNOWN,
        ident=IdentifiabilityStatus.NON_IDENTIFIABLE,
        method=InferenceMethod.NONE,
        group_id="group-1",
    )
    group = _group()
    snapshot = _snapshot([member], [group])
    answer = answer_for(member, snapshot)

    assert answer.point_w is None
    assert answer.status == "unknown"
    assert answer.identifiability == "non_identifiable"
    assert answer.group_id == "group-1"
    # The only bounds published for the member are the group's justified sum.
    assert answer.lower_w == group.bounds.lower_w
    assert answer.upper_w == group.bounds.upper_w
    # No point value on the member itself in the whole snapshot.
    assert all(e.point_w is None for e in snapshot.device_estimates if e.group_id == "group-1")


def test_group_estimate_answer_has_no_individual_device_number() -> None:
    group = _group(refs=[EvidenceRef(kind="event_group", id="eg-1"), EvidenceRef(kind="sample", id="Main")])
    snapshot = _snapshot([], [group])

    class WithEvent:
        def resolve(self, ref: EvidenceRef) -> Any | None:
            if ref.kind == "event_group":
                return {"group_id": ref.id}
            return SnapshotEvidenceLookup(snapshot).resolve(ref)

    answer = ProvenanceResolver(snapshot, lookup=WithEvent()).for_group(group)
    assert answer.estimate_kind == "group"
    assert answer.point_w == 45.0
    assert answer.status is None  # groups have no MEASURED/ESTIMATED status
    assert len(answer.event_associations) == 1
    assert len(answer.reader_measurements) == 1


def test_answer_rejects_unknown_question() -> None:
    estimate = _estimate("dev-1")
    answer = answer_for(estimate, _snapshot([estimate]))
    with pytest.raises(KeyError):
        answer.answer("not_a_question")
