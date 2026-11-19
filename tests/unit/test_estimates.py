"""I5 — estimate status table, confidence, provenance and reconciliation.

Pure tests: no DB, no wall clock (all times injected), no ``runtime.fakes`` in
production code (the fakes here are only test inputs).  Contracts are used
directly so a regression in assembly cannot be hidden by an adapter.
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pytest
from wattmosaic_engine.contracts import (
    Bounds,
    ConfidenceVectorV3,
    DeviceEstimate,
    EstimateStatus,
    EvidenceRef,
    IdentifiabilityStatus,
    InferenceMethod,
    VersionTriple,
)
from wattmosaic_engine.inference.confidence_v3 import (
    ConfidenceInputs,
    compute_confidence,
)
from wattmosaic_engine.inference.estimates import (
    DirectReading,
    build_snapshot,
    contributing_readers,
    reconciliation_from_estimates,
    status_for,
)
from wattmosaic_engine.runtime.fakes import (
    FakeMapping,
    FakeTopology,
    fake_operator,
)
from wattmosaic_engine.runtime.interfaces import ObservationOperator

T0 = datetime(2026, 1, 1, tzinfo=UTC)
VT = VersionTriple(
    topology_version_id="topo-1", mapping_version_id="map-1", model_set_id="mset-1"
)


def _operator() -> ObservationOperator:
    topology = FakeTopology.from_tree({"Main": {"Subpanel": {"Circuit A": {}}}})
    mapping = FakeMapping.from_pairs(
        {
            ("device", "plug-device"): "Circuit A",
            ("device", "light-a"): "Circuit A",
            ("device", "light-b"): "Circuit A",
        }
    )
    return fake_operator(
        topology,
        mapping,
        ("Circuit A", "Subpanel", "Main"),
        (
            ("device", "plug-device"),
            ("device", "light-a"),
            ("device", "light-b"),
        ),
        at=T0,
    )


# --------------------------------------------------------------------------- #
# Status matrix — one test per table row (design §7 / HB §8.5).
# --------------------------------------------------------------------------- #


def test_status_row_measured_only_from_single_subject_endpoint() -> None:
    direct = DirectReading("plug-device", "Circuit A", 120.0, T0)
    status, method, point = status_for(
        identifiability=IdentifiabilityStatus.IDENTIFIABLE, direct=direct
    )
    assert status == EstimateStatus.MEASURED
    assert method == InferenceMethod.DIRECT_READING
    assert point == pytest.approx(120.0)


def test_status_row_estimated_from_joint_delta() -> None:
    status, method, point = status_for(
        identifiability=IdentifiabilityStatus.IDENTIFIABLE,
        solution_w=45.0,
        method=InferenceMethod.JOINT_DELTA,
    )
    assert status == EstimateStatus.ESTIMATED
    assert method == InferenceMethod.JOINT_DELTA
    assert point == pytest.approx(45.0)


def test_status_row_estimated_from_joint_absolute() -> None:
    status, method, point = status_for(
        identifiability=IdentifiabilityStatus.IDENTIFIABLE,
        solution_w=80.0,
        method=InferenceMethod.JOINT_ABSOLUTE,
    )
    assert status == EstimateStatus.ESTIMATED
    assert method == InferenceMethod.JOINT_ABSOLUTE
    assert point == pytest.approx(80.0)


def test_status_row_group_member_is_unknown_with_no_point() -> None:
    status, method, point = status_for(
        identifiability=IdentifiabilityStatus.GROUP_IDENTIFIABLE,
        solution_w=125.0,
    )
    assert status == EstimateStatus.UNKNOWN
    assert method == InferenceMethod.NONE
    assert point is None


def test_status_row_explicit_allocation_is_allocated() -> None:
    status, method, point = status_for(
        identifiability=IdentifiabilityStatus.NON_IDENTIFIABLE,
        solution_w=30.0,
        allocation_rule="declared_proportional",
    )
    assert status == EstimateStatus.ALLOCATED
    assert method == InferenceMethod.DECLARED
    assert point == pytest.approx(30.0)


def test_status_row_otherwise_unknown() -> None:
    status, method, point = status_for(
        identifiability=IdentifiabilityStatus.NON_IDENTIFIABLE,
        solution_w=500.0,
        model_w=250.0,
    )
    assert status == EstimateStatus.UNKNOWN
    assert method == InferenceMethod.NONE
    assert point is None


# --------------------------------------------------------------------------- #
# §17 step 8 and group-member rules.
# --------------------------------------------------------------------------- #


def test_tv_in_standby_is_measured_3w_not_zero() -> None:
    from tools.simulator.hb17 import build

    snapshot = build_snapshot(build(17).observation_view())
    tv = next(e for e in snapshot.device_estimates if e.subject_id == "tv")
    assert tv.status == EstimateStatus.MEASURED
    assert tv.point_w == pytest.approx(3.0, abs=0.01)
    assert tv.point_w > 0.0
    assert tv.method == InferenceMethod.DIRECT_READING
    assert tv.level == "standby"


def test_a_group_member_is_never_estimated() -> None:
    from tools.simulator.hb17 import build

    snapshot = build_snapshot(build(17).observation_view())
    members = [
        e for e in snapshot.device_estimates
        if e.identifiability == IdentifiabilityStatus.NON_IDENTIFIABLE
    ]
    assert members, "the co-switched lights must be unpublished members"
    for member in members:
        assert member.point_w is None
        assert member.status == EstimateStatus.UNKNOWN
        assert member.group_id is not None


def test_identified_w_counts_the_tv_once() -> None:
    """Mutation guard: the plug/circuit/parent rows never add multiple TVs."""
    from tools.simulator.hb17 import build

    snapshot = build_snapshot(build(17).observation_view())
    tv = next(e for e in snapshot.device_estimates if e.subject_id == "tv")
    assert tv.point_w == pytest.approx(3.0)
    assert len([e for e in snapshot.device_estimates if e.subject_id == "tv"]) == 1
    assert snapshot.reconciliation.identified_w == pytest.approx(3.0)


# --------------------------------------------------------------------------- #
# Provenance.
# --------------------------------------------------------------------------- #


def test_provenance_lists_only_contributing_readers() -> None:
    op = _operator()
    # plug-device enters all three rows; every reader contributes.
    assert contributing_readers(op, "plug-device") == ("Circuit A", "Subpanel", "Main")


def test_provenance_absent_for_unmapped_subject() -> None:
    assert contributing_readers(_operator(), "does-not-exist") == ()


def test_provenance_zero_weight_reader_is_absent() -> None:
    op = _operator()
    # Zero out plug-device on the Main row: it no longer contributed there.
    H = np.asarray(op.H, dtype=float).copy()
    H[2, 0] = 0.0
    op2 = ObservationOperator(
        H=H, rows=op.rows, subjects=op.subjects, entries=op.entries,
        unknown_scopes=op.unknown_scopes, soft_alternatives=op.soft_alternatives,
        topology_version_id=op.topology_version_id,
        mapping_version_id=op.mapping_version_id, at=op.at,
    )
    assert contributing_readers(op2, "plug-device") == ("Circuit A", "Subpanel")


def test_each_estimate_carries_its_own_versions_and_evidence() -> None:
    from tools.simulator.hb17 import build

    snapshot = build_snapshot(build(17).observation_view())
    for estimate in snapshot.device_estimates:
        assert estimate.versions.topology_version_id
        assert estimate.versions.mapping_version_id
        assert estimate.versions.model_set_id
        for ref in estimate.evidence_refs:
            assert isinstance(ref, EvidenceRef)
            assert ref.id
        # No batch-wide ledger: every estimate lists only its own readers.
        assert len(estimate.source_reader_ids) == len(set(estimate.source_reader_ids))


# --------------------------------------------------------------------------- #
# Reconciliation.
# --------------------------------------------------------------------------- #


def test_reconciliation_terms_are_nullable_not_zero() -> None:
    recon = reconciliation_from_estimates(devices=[])
    assert recon.identified_w is None
    assert recon.group_w is None
    assert recon.unknown_w is None
    assert recon.residual_w is None
    assert recon.balance_error_w is None


def test_reconciliation_identity_counts_each_subject_once() -> None:
    def dev(subject: str, watts: float, status: EstimateStatus) -> DeviceEstimate:
        return DeviceEstimate(
            subject_kind="device", subject_id=subject, point_w=watts,
            bounds=Bounds(lower_w=0.0, upper_w=watts + 1.0),
            status=status, identifiability=IdentifiabilityStatus.IDENTIFIABLE,
            method=InferenceMethod.JOINT_ABSOLUTE,
            confidence=ConfidenceVectorV3(), source_reader_ids=["r1", "r2", "r3"],
            evidence_refs=[], versions=VT, updated_at=T0,
        )

    recon = reconciliation_from_estimates(
        devices=[dev("tv", 120.0, EstimateStatus.ESTIMATED)],
        groups=[],
        unknowns=[],
        residual_by_reader={"Main": 1500.0},
        main_w=1620.0,
    )
    assert recon.identified_w == pytest.approx(120.0)
    assert recon.residual_w == pytest.approx(1500.0)
    assert recon.main_w == pytest.approx(1620.0)
    # Every term present -> balance is exact; a missing term keeps it None.
    assert recon.balance_error_w is None  # group_w/unknown_w absent (HB §18)
    complete = reconciliation_from_estimates(
        devices=[dev("tv", 120.0, EstimateStatus.ESTIMATED)],
        groups=[],
        unknowns=[],
        residual_by_reader={"Main": 1500.0},
        main_w=1620.0,
    )
    complete.group_w = 0.0
    complete.unknown_w = 0.0
    assert complete.balance_error_w is not None
    assert complete.balance_error_w == pytest.approx(0.0, abs=1e-9)


def test_no_proportional_prior_sharing() -> None:
    """Mutation guard: forcing a 50/50 model split must not become a point."""
    from wattmosaic_engine.inference.identifiability import member_point_w

    assert member_point_w(
        solution_w=None, model_w=125.0,
        identifiability=IdentifiabilityStatus.GROUP_IDENTIFIABLE,
    ) is None
    assert member_point_w(
        solution_w=None, model_w=125.0,
        identifiability=IdentifiabilityStatus.NON_IDENTIFIABLE,
    ) is None
    assert member_point_w(
        solution_w=None, model_w=125.0,
        identifiability=IdentifiabilityStatus.IDENTIFIABLE,
    ) == pytest.approx(125.0)


# --------------------------------------------------------------------------- #
# Confidence: seven independent components, not blended.
# --------------------------------------------------------------------------- #


def test_confidence_has_seven_components_and_no_scalar() -> None:
    conf = compute_confidence()
    assert set(ConfidenceVectorV3.model_fields) == {
        "measurement_quality", "event_association", "model_fit",
        "topology_consistency", "identifiability", "sample_coverage", "freshness",
    }
    assert not hasattr(conf, "__float__")


def test_confidence_components_use_only_their_input_family() -> None:
    conf = compute_confidence(
        ConfidenceInputs(
            reader_quality={"r1": 0.8, "r2": 0.4},
            match_states=["confirmed"],
            model_residual_w=2.0,
            model_sigma_w=2.0,
            edge_lifecycles=["confirmed", "provisional"],
            null_space_share=0.1,
            independent_event_count=5,
            age_s=0.0,
        )
    )
    assert conf.measurement_quality == pytest.approx(0.4)  # weakest reader
    assert conf.event_association == pytest.approx(1.0)  # confirmed match
    assert conf.model_fit == pytest.approx(1 / 2)  # 1/(1+(2/2)^2)
    assert conf.topology_consistency == pytest.approx(0.6)  # weakest edge
    assert conf.identifiability == pytest.approx(0.9)
    assert conf.sample_coverage == pytest.approx(1.0)
    assert conf.freshness == pytest.approx(1.0)


def test_confidence_absent_inputs_are_none_not_zero() -> None:
    conf = compute_confidence()
    for value in conf.model_dump().values():
        assert value is None


def test_confidence_freshness_decays_with_age() -> None:
    fresh = compute_confidence(ConfidenceInputs(age_s=0.0)).freshness
    stale = compute_confidence(ConfidenceInputs(age_s=300.0)).freshness
    assert fresh == pytest.approx(1.0)
    assert stale is not None and stale < 0.5


def test_confidence_docstring_states_not_a_probability() -> None:
    from wattmosaic_engine.inference import confidence_v3

    assert "not a probability" in (confidence_v3.__doc__ or "").lower()
