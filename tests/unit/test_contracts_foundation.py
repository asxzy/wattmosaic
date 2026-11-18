"""K00: foundation contracts — version refs, bounds, confidence and helpers."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from wattmosaic_engine import contracts
from wattmosaic_engine.contracts import foundation, legacy
from wattmosaic_engine.contracts.foundation import (
    Bounds,
    ConfidenceVectorV3,
    EstimateStatus,
    EvidenceKind,
    EvidenceRef,
    EvidenceRefParseResult,
    IdentifiabilityStatus,
    InferenceMethod,
    VersionTriple,
    model_set_id,
    parse_evidence_ref,
    parse_version_number,
    topology_version_id,
)

SCHEMA_MODELS = [model for _name, model in foundation.SCHEMA_EXPORTS]


# --------------------------------------------------------------------------- #
# Naming: no public name may collide with the legacy surface.
# --------------------------------------------------------------------------- #


def test_no_public_name_collides_with_legacy() -> None:
    legacy_names = set(dir(legacy))
    collisions = sorted(name for name in foundation.__all__ if name in legacy_names)
    assert collisions == [], f"foundation __all__ names shadow legacy: {collisions}"


def test_every_exported_name_is_in_all() -> None:
    for name in SCHEMA_MODELS:
        assert name.__name__ in foundation.__all__


# --------------------------------------------------------------------------- #
# JSON round-trips for every exported model.
# --------------------------------------------------------------------------- #

_SAMPLES = {
    VersionTriple: {
        "topology_version_id": "topo-3",
        "mapping_version_id": "map-7",
        "model_set_id": "mset-0123456789ab",
    },
    EvidenceRef: {"kind": "sample", "id": "sample-1"},
    Bounds: {"lower_w": 10.0, "upper_w": 20.0, "basis": "reader_ceiling"},
    ConfidenceVectorV3: {
        "measurement_quality": 0.9,
        "event_association": 0.5,
        "freshness": None,
    },
}


@pytest.mark.parametrize("model", SCHEMA_MODELS, ids=lambda m: m.__name__)
def test_model_round_trips_through_json(model: type) -> None:
    instance = model.model_validate(_SAMPLES[model])
    payload = instance.model_dump_json()
    assert model.model_validate_json(payload) == instance


def test_confidence_vector_v3_round_trip_all_components() -> None:
    vector = ConfidenceVectorV3(
        measurement_quality=0.1,
        event_association=0.2,
        model_fit=0.3,
        topology_consistency=0.4,
        identifiability=0.5,
        sample_coverage=0.6,
        freshness=0.7,
    )
    assert ConfidenceVectorV3.model_validate_json(vector.model_dump_json()) == vector


# --------------------------------------------------------------------------- #
# Version-id helpers.
# --------------------------------------------------------------------------- #


def test_topology_version_id_and_parse_round_trip() -> None:
    for n in (0, 1, 42, 1000):
        version_id = topology_version_id(n)
        assert version_id == f"topo-{n}"
        assert parse_version_number(version_id) == n


def test_parse_version_number_supports_every_numbered_family() -> None:
    assert parse_version_number("topo-5") == 5
    assert parse_version_number("map-9") == 9
    assert parse_version_number("mdl-dev_light-12") == 12


def test_parse_version_number_rejects_non_numbered_ids() -> None:
    for bad in ("snap-01ABC", "topo-x", "topo-1junk", "", "other-3"):
        with pytest.raises(ValueError):
            parse_version_number(bad)


def test_model_set_id_is_order_independent_and_deduplicated() -> None:
    ids = ["mdl-b-2", "mdl-a-1", "mdl-c-3"]
    expected = model_set_id(ids)
    assert expected == model_set_id(list(reversed(ids)))
    assert expected == model_set_id(ids + ["mdl-a-1"])
    assert expected.startswith("mset-")
    assert len(expected) == len("mset-") + 12
    assert model_set_id(ids) != model_set_id(["mdl-a-1", "mdl-b-2"])


# --------------------------------------------------------------------------- #
# Bounds validators.
# --------------------------------------------------------------------------- #


def test_bounds_accepts_a_valid_interval() -> None:
    bounds = Bounds(lower_w=0.0, upper_w=100.0)
    assert (bounds.lower_w, bounds.upper_w) == (0.0, 100.0)


def test_bounds_rejects_inverted_interval() -> None:
    with pytest.raises(ValidationError):
        Bounds(lower_w=10.0, upper_w=5.0)


def test_bounds_rejects_unauthorised_zero_width() -> None:
    with pytest.raises(ValidationError):
        Bounds(lower_w=7.0, upper_w=7.0)


def test_bounds_point_constructor_allows_zero_width() -> None:
    point = Bounds._point(7.0, basis="posterior")
    assert point.lower_w == point.upper_w == 7.0
    assert point.basis == "posterior"


def test_allow_point_is_not_a_public_field_or_in_schema() -> None:
    assert "allow_point" not in Bounds.model_fields
    assert "allow_point" not in Bounds.model_json_schema()["properties"]
    # A zero-width interval encoded in JSON must still be rejected: the private
    # gate cannot be reopened by a payload.
    with pytest.raises(ValidationError):
        Bounds.model_validate({"lower_w": 7.0, "upper_w": 7.0, "allow_point": True})


# --------------------------------------------------------------------------- #
# Confidence vector semantics.
# --------------------------------------------------------------------------- #


def test_confidence_vector_has_exactly_the_seven_components() -> None:
    assert set(ConfidenceVectorV3.model_fields) == {
        "measurement_quality",
        "event_association",
        "model_fit",
        "topology_consistency",
        "identifiability",
        "sample_coverage",
        "freshness",
    }


def test_confidence_vector_defaults_are_none_and_range_is_enforced() -> None:
    empty = ConfidenceVectorV3()
    assert all(value is None for value in empty.model_dump().values())
    with pytest.raises(ValidationError):
        ConfidenceVectorV3(model_fit=1.5)
    with pytest.raises(ValidationError):
        ConfidenceVectorV3(model_fit=-0.1)


def test_confidence_vector_docstring_says_not_a_probability() -> None:
    assert "not a probability" in (ConfidenceVectorV3.__doc__ or "").lower()


# --------------------------------------------------------------------------- #
# Evidence refs and status enums.
# --------------------------------------------------------------------------- #


def test_parse_evidence_ref() -> None:
    result = parse_evidence_ref("edge:edge-42")
    assert isinstance(result, EvidenceRefParseResult)
    assert EvidenceKind(result.kind) is EvidenceKind.EDGE
    assert result.id == "edge-42"
    for bad in ("nonsense:1", "sample:", "no-colon"):
        with pytest.raises(ValueError):
            parse_evidence_ref(bad)


def test_status_enums_members() -> None:
    assert {m.value for m in EstimateStatus} == {
        "measured",
        "estimated",
        "allocated",
        "unknown",
    }
    assert {m.value for m in IdentifiabilityStatus} == {
        "identifiable",
        "partially_identifiable",
        "group_identifiable",
        "non_identifiable",
        "insufficient_evidence",
    }
    assert {m.value for m in InferenceMethod} == {
        "direct_reading",
        "joint_delta",
        "joint_absolute",
        "model_prior",
        "declared",
        "none",
    }


def test_foundation_names_reachable_from_facade() -> None:
    assert contracts.Bounds is Bounds
    assert contracts.ConfidenceVectorV3 is ConfidenceVectorV3
