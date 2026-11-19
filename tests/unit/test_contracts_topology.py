"""K03: topology edge, version, mapping edge, configuration event, residual and
unknown-profile contracts (interfaces.md §4).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from wattmosaic_engine import contracts
from wattmosaic_engine.contracts import topology
from wattmosaic_engine.contracts.topology import (
    ConfigurationEvent,
    EdgeEvidence,
    EdgeLifecycleV3,
    MappingEdge,
    MappingVersion,
    Observability,
    ReaderResidualPoint,
    RelationType,
    TopologyEdgeV3,
    TopologyVersionV3,
    UnknownLoadProfileV3,
)

SCHEMA_MODELS = [model for _name, model in topology.SCHEMA_EXPORTS]

T0 = datetime(2026, 1, 1, tzinfo=UTC)
T1 = T0 + timedelta(hours=1)
T2 = T0 + timedelta(hours=2)


# --------------------------------------------------------------------------- #
# Naming / export invariants (contract-task rule).
# --------------------------------------------------------------------------- #


def test_every_exported_model_is_in_all() -> None:
    for model in SCHEMA_MODELS:
        assert model.__name__ in topology.__all__


def test_every_all_name_resolves_on_the_facade() -> None:
    for name in topology.__all__:
        assert hasattr(contracts, name), f"facade missing {name!r}"


# --------------------------------------------------------------------------- #
# Enum membership (exact sets).
# --------------------------------------------------------------------------- #


def test_relation_type_has_exactly_the_hb_five() -> None:
    assert {member.value for member in RelationType} == {
        "contains",
        "shared_load",
        "paired_leg",
        "correlated",
        "same_conductor",
    }


def test_edge_lifecycle_v3_is_the_new_seven_state_enum() -> None:
    assert {member.value for member in EdgeLifecycleV3} == {
        "candidate",
        "provisional",
        "confirmed",
        "conflicted",
        "suspected_change",
        "rejected",
        "retired",
    }
    assert "under_review" not in {m.value for m in EdgeLifecycleV3}


def test_observability_has_exactly_the_seven_hb_9_7_states() -> None:
    assert {member.value for member in Observability} == {
        "direct",
        "expected",
        "inferred",
        "partial",
        "unknown",
        "not_observable",
        "conflicted",
    }


# --------------------------------------------------------------------------- #
# EdgeEvidence and its derived conflict_ratio.
# --------------------------------------------------------------------------- #


def test_edge_evidence_conflict_ratio() -> None:
    assert EdgeEvidence(support=9, conflict=1).conflict_ratio == pytest.approx(0.1)
    assert EdgeEvidence(support=3, conflict=1).conflict_ratio == pytest.approx(0.25)


def test_edge_evidence_conflict_ratio_parent_only_is_neutral() -> None:
    # parent-only is neutral: it neither supports nor conflicts.
    ev = EdgeEvidence(support=8, conflict=2, parent_only=100)
    assert ev.conflict_ratio == pytest.approx(0.2)


def test_edge_evidence_conflict_ratio_with_no_decisive_evidence() -> None:
    assert EdgeEvidence(support=0, conflict=0, parent_only=5).conflict_ratio == 0.0


# --------------------------------------------------------------------------- #
# Builders (only to keep the validator tests readable).
# --------------------------------------------------------------------------- #


def _evidence() -> EdgeEvidence:
    return EdgeEvidence(support=3, conflict=0)


def _edge(**overrides: object) -> TopologyEdgeV3:
    data: dict[str, object] = dict(
        edge_id="e-1",
        relation="contains",
        source_endpoint_id="parent",
        target_endpoint_id="child",
        lifecycle="candidate",
        declared_by="inference",
        evidence=_evidence(),
        valid_from=T0,
        pinned=False,
    )
    data.update(overrides)
    return TopologyEdgeV3.model_validate(data)


def _mapping_edge(**overrides: object) -> MappingEdge:
    data: dict[str, object] = dict(
        mapping_edge_id="m-1",
        subject_kind="device",
        subject_id="dev_1",
        endpoint_id="reader-1",
        observability="inferred",
        lifecycle="candidate",
        declared_by="auto_placement",
        evidence=_evidence(),
        valid_from=T0,
    )
    data.update(overrides)
    return MappingEdge.model_validate(data)


# --------------------------------------------------------------------------- #
# JSON round-trips for every exported model.
# --------------------------------------------------------------------------- #

_SAMPLES = {
    EdgeEvidence: {"support": 3, "conflict": 1, "parent_only": 2, "independent_causes": 1,
                   "last_support_at": T0.isoformat(), "last_conflict_at": None},
    TopologyEdgeV3: {
        "edge_id": "e-1", "relation": "contains",
        "source_endpoint_id": "parent", "target_endpoint_id": "child",
        "lifecycle": "candidate", "declared_by": "inference",
        "evidence": {"support": 3, "conflict": 0},
        "valid_from": T0.isoformat(), "valid_until": None, "pinned": False,
    },
    TopologyVersionV3: {
        "version_id": "topo-1", "number": 1, "parent_version_id": None,
        "edge_ids": ["e-1"], "status": "active", "change_event_id": None,
        "created_at": T0.isoformat(), "digest": "abc",
    },
    MappingEdge: {
        "mapping_edge_id": "m-1", "subject_kind": "device", "subject_id": "dev_1",
        "endpoint_id": "reader-1", "observability": "inferred",
        "lifecycle": "candidate", "declared_by": "auto_placement",
        "evidence": {"support": 3, "conflict": 0}, "valid_from": T0.isoformat(),
        "valid_until": None,
    },
    MappingVersion: {
        "version_id": "map-1", "number": 1, "parent_version_id": None,
        "mapping_edge_ids": ["m-1"], "status": "active", "change_event_id": None,
        "created_at": T0.isoformat(), "digest": "def",
    },
    ConfigurationEvent: {
        "event_id": "cfg-1", "kind": "calibration_change", "detection": "inferred",
        "status": "suspected", "affected_endpoint_ids": ["reader-1"],
        "affected_edge_ids": [], "affected_mapping_edge_ids": [],
        "candidate_topology_version_id": None, "candidate_mapping_version_id": None,
        "evidence_refs": [], "created_at": T0.isoformat(), "resolved_at": None,
    },
    ReaderResidualPoint: {
        "reader_id": "reader-1", "at": T0.isoformat(), "residual_w": -12.5,
        "baseline_w": None, "dynamic_w": 3.0, "noise_sigma_w": None,
        "topology_version_id": "topo-1", "basis": "ct_measured",
    },
    UnknownLoadProfileV3: {
        "profile_id": "ulp-1", "scope_endpoint_id": "reader-1",
        "lifecycle": "observing",
        "signature": {"delta_w_median": 100.0, "delta_w_mad": 5.0},
        "event_count": 5, "independent_event_count": 2,
        "first_seen": T0.isoformat(), "last_seen": T1.isoformat(),
    },
}


@pytest.mark.parametrize("model", SCHEMA_MODELS, ids=lambda m: m.__name__)
def test_model_round_trips_through_json(model: type) -> None:
    instance = model.model_validate(_SAMPLES[model])
    payload = instance.model_dump_json()
    assert model.model_validate_json(payload) == instance


# --------------------------------------------------------------------------- #
# Validators.
# --------------------------------------------------------------------------- #


def test_retired_edge_requires_valid_until() -> None:
    with pytest.raises(ValidationError, match="valid_until"):
        _edge(lifecycle="retired")
    ok = _edge(lifecycle="retired", valid_until=T1)
    assert ok.lifecycle == "retired"


def test_edge_cannot_connect_an_endpoint_to_itself() -> None:
    with pytest.raises(ValidationError, match="itself"):
        _edge(target_endpoint_id="parent")


def test_beta_basis_is_set_iff_beta_is_set() -> None:
    # beta without basis is rejected.
    with pytest.raises(ValidationError, match="iff beta"):
        _edge(beta=1.2)
    # basis without beta is rejected.
    with pytest.raises(ValidationError, match="iff beta"):
        _edge(beta_basis="estimated")
    # Neither, and both, are accepted.
    assert _edge().beta is None
    both = _edge(beta=0.9, beta_basis="recorded_factor")
    assert both.beta == 0.9 and both.beta_basis == "recorded_factor"


def test_unknown_load_profile_has_no_confirmed_state() -> None:
    for bad in ("confirmed", "CONFIRMED", "auto_confirmed"):
        with pytest.raises(ValidationError, match="never be CONFIRMED"):
            UnknownLoadProfileV3.model_validate({
                "profile_id": "ulp", "scope_endpoint_id": "reader",
                "lifecycle": bad, "first_seen": T0, "last_seen": T1,
            })
    for good in ("observing", "characterised", "candidate", "promoted", "retired"):
        obj = UnknownLoadProfileV3.model_validate({
            "profile_id": "ulp", "scope_endpoint_id": "reader",
            "lifecycle": good, "first_seen": T0, "last_seen": T1,
        })
        assert obj.lifecycle == good


def test_mapping_unknown_profile_requires_inferred_or_partial_observability() -> None:
    for obs in ("inferred", "partial"):
        obj = _mapping_edge(subject_kind="unknown_profile", subject_id="ulp-1",
                            observability=obs)
        assert obj.observability == obs
    for obs in ("direct", "expected", "unknown", "not_observable", "conflicted"):
        with pytest.raises(ValidationError, match="INFERRED or PARTIAL"):
            _mapping_edge(subject_kind="unknown_profile", subject_id="ulp-1",
                          observability=obs)


def test_mapping_device_subject_is_not_restricted_by_observability() -> None:
    for obs in ("direct", "expected", "unknown", "not_observable"):
        assert _mapping_edge(observability=obs).observability == obs


# --------------------------------------------------------------------------- #
# Configuration-change enums (moved from the legacy module).
# --------------------------------------------------------------------------- #


def test_configuration_event_uses_change_enums() -> None:
    ev = ConfigurationEvent(
        event_id="cfg-1",
        kind=topology.ConfigurationChangeKind.CALIBRATION_CHANGE,
        detection=topology.ChangeDetection.INFERRED,
        status=topology.ChangeStatus.SUSPECTED,
        created_at=T0,
    )
    assert ev.kind == topology.ConfigurationChangeKind.CALIBRATION_CHANGE.value
    assert ev.detection == topology.ChangeDetection.INFERRED.value
    assert ev.status == topology.ChangeStatus.SUSPECTED.value


def test_configuration_event_detection_has_declared_and_inferred() -> None:
    values = {m.value for m in topology.ChangeDetection}
    assert {"declared", "inferred"} <= values


# --------------------------------------------------------------------------- #
# Null policy: measurement/score fields default to None (HB §18).
# --------------------------------------------------------------------------- #


def test_null_policy_for_exported_models() -> None:
    from wattmosaic_engine.contracts import foundation

    for model in SCHEMA_MODELS:
        assert foundation.audit_model_null_policy(model) == [], model.__name__


def test_residual_point_signed_and_nullable() -> None:
    point = ReaderResidualPoint(
        reader_id="r", at=T0, residual_w=-250.0,
        topology_version_id="topo-1", basis="ct_measured",
    )
    assert point.residual_w == -250.0
    assert point.baseline_w is None
    assert point.noise_sigma_w is None
