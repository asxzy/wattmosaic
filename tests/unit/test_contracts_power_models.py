"""K06: power-model contracts — spec union, versions, events, prediction, observation."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest
from pydantic import TypeAdapter, ValidationError
from wattmosaic_engine import contracts
from wattmosaic_engine.contracts import power_models
from wattmosaic_engine.contracts.power_models import (
    SPEC_SCHEMA_VERSION,
    AdjustableSpec,
    ComponentSumSpec,
    DeltaPrior,
    DynamicSpec,
    FixedSpec,
    KernelPhase,
    LearningObservation,
    LevelStat,
    ModelBasis,
    ModelClass,
    PowerLevelV3,
    PowerModelEvent,
    PowerModelSpec,
    PowerModelVersion,
    PowerPrediction,
    PredictionStatus,
    ResponseKernelSpec,
    observation_id,
)

SCHEMA_MODELS = [model for _name, model in power_models.SCHEMA_EXPORTS]
SPEC_ADAPTER = TypeAdapter(PowerModelSpec)
AT = datetime(2026, 1, 1, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Naming: no public name may collide with the legacy surface.
# --------------------------------------------------------------------------- #


def test_power_level_v3_keeps_off_idle_standby_distinct() -> None:
    # The legacy PowerLevel collapses OFF into STANDBY; the v3 enum restores it.
    assert {m.value for m in PowerLevelV3} >= {
        "off",
        "standby",
        "idle",
        "active",
        "transient",
        "unknown",
    }
    assert len({m.value for m in PowerLevelV3}) == len(list(PowerLevelV3))


def test_every_exported_model_is_in_all() -> None:
    for model in SCHEMA_MODELS:
        assert model.__name__ in power_models.__all__


def test_spec_schema_version_starts_at_one() -> None:
    assert SPEC_SCHEMA_VERSION == 1


# --------------------------------------------------------------------------- #
# Spec union: round-trip of every kind via the discriminator.
# --------------------------------------------------------------------------- #

_SPEC_SAMPLES = {
    "fixed": {
        "kind": "fixed",
        "levels": {"standby": {"median_w": 7.0, "mad_w": 0.3, "n": 10}},
        "transition_delta": {"on": {"median_w": 50.0, "mad_w": 1.0, "n": 5}},
    },
    "adjustable": {
        "kind": "adjustable",
        "control_variable": "brightness",
        "knots_x": [0.0, 0.5, 1.0],
        "knots_w": [7.0, 30.0, 60.0],
        "monotone": True,
        "x_coverage": [0.0, 1.0],
        "standby": {"median_w": 7.0, "mad_w": 0.3, "n": 10},
    },
    "dynamic": {
        "kind": "dynamic",
        "states": {
            "fan_only": {"median_w": 20.0, "mad_w": 1.0, "n": 8},
            "compressor": {"median_w": 120.0, "mad_w": 5.0, "n": 4},
        },
        "ha_state_to_states": {"cool": ["fan_only", "compressor"]},
        "transition_counts": {"fan_only": {"compressor": 3}},
        "duration_s": {"fan_only": {"median_w": 300.0, "mad_w": 20.0, "n": 8}},
    },
    "response_kernel": {
        "kind": "response_kernel",
        "by_transition": {
            "on": [
                {
                    "phase": "startup",
                    "delay_s": {"median_w": 0.0, "mad_w": 0.0, "n": 5},
                    "amplitude_w": {"median_w": 200.0, "mad_w": 10.0, "n": 5},
                    "duration_s": {"median_w": 2.0, "mad_w": 0.5, "n": 5},
                }
            ]
        },
    },
    "component_sum": {"kind": "component_sum", "component_ids": ["comp-a", "comp-b"]},
}


@pytest.mark.parametrize("kind", list(_SPEC_SAMPLES))
def test_each_spec_kind_round_trips_via_discriminator(kind: str) -> None:
    spec = SPEC_ADAPTER.validate_python(_SPEC_SAMPLES[kind])
    payload = spec.model_dump_json()
    assert SPEC_ADAPTER.validate_json(payload) == spec
    assert spec.kind == kind
    # The concrete class is selected by the discriminator.
    assert isinstance(
        spec,
        {
            "fixed": FixedSpec,
            "adjustable": AdjustableSpec,
            "dynamic": DynamicSpec,
            "response_kernel": ResponseKernelSpec,
            "component_sum": ComponentSumSpec,
        }[kind],
    )


def test_kernel_phase_is_a_model() -> None:
    phase = KernelPhase.model_validate(_SPEC_SAMPLES["response_kernel"]["by_transition"]["on"][0])
    assert SPEC_ADAPTER.validate_json(
        ResponseKernelSpec(
            by_transition={"on": [phase]}
        ).model_dump_json()
    ).kind == "response_kernel"


def test_spec_rejects_unknown_discriminator() -> None:
    with pytest.raises(ValidationError):
        SPEC_ADAPTER.validate_python({"kind": "mystery"})


# --------------------------------------------------------------------------- #
# extra="forbid": a spec with extra keys is rejected.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("kind", list(_SPEC_SAMPLES))
def test_spec_rejects_extra_keys(kind: str) -> None:
    payload = dict(_SPEC_SAMPLES[kind])
    payload["surprise"] = 1
    with pytest.raises(ValidationError):
        SPEC_ADAPTER.validate_python(payload)


def test_extra_keys_rejected_on_sibling_models() -> None:
    with pytest.raises(ValidationError):
        LevelStat(median_w=1.0, oops=True)
    with pytest.raises(ValidationError):
        PowerPrediction(
            level="active",
            mean_w=1.0,
            sigma_w=None,
            status="KNOWN",
            model_version_id=None,
            oops=True,
        )


# --------------------------------------------------------------------------- #
# Adjustable knot validation.
# --------------------------------------------------------------------------- #


def test_adjustable_knots_sorted_and_equal_length() -> None:
    spec = AdjustableSpec.model_validate(_SPEC_SAMPLES["adjustable"])
    assert spec.knots_x == sorted(spec.knots_x)
    assert len(spec.knots_x) == len(spec.knots_w)


def test_adjustable_rejects_unsorted_knots() -> None:
    payload = dict(_SPEC_SAMPLES["adjustable"])
    payload["knots_x"] = [0.0, 1.0, 0.5]
    with pytest.raises(ValidationError):
        AdjustableSpec.model_validate(payload)


def test_adjustable_rejects_mismatched_knot_lengths() -> None:
    payload = dict(_SPEC_SAMPLES["adjustable"])
    payload["knots_w"] = [7.0, 30.0]
    with pytest.raises(ValidationError):
        AdjustableSpec.model_validate(payload)


# --------------------------------------------------------------------------- #
# Prediction: an unknown prediction keeps None; status/mean equivalence.
# --------------------------------------------------------------------------- #


def test_unknown_prediction_keeps_none() -> None:
    p = PowerPrediction(
        level="unknown", mean_w=None, sigma_w=None, status="UNKNOWN", model_version_id=None
    )
    assert p.mean_w is None
    assert PowerPrediction.model_validate_json(p.model_dump_json()) == p
    assert p.status == "UNKNOWN"


def test_known_prediction_requires_mean() -> None:
    with pytest.raises(ValidationError):
        PowerPrediction(
            level="active", mean_w=None, sigma_w=None, status="KNOWN", model_version_id=None
        )


def test_unknown_status_requires_none_mean() -> None:
    with pytest.raises(ValidationError):
        PowerPrediction(
            level="active", mean_w=10.0, sigma_w=None, status="UNKNOWN", model_version_id=None
        )


def test_prediction_status_enum_members() -> None:
    assert {m.value for m in PredictionStatus} == {"KNOWN", "UNKNOWN"}


# --------------------------------------------------------------------------- #
# Delta prior.
# --------------------------------------------------------------------------- #


def test_delta_prior_round_trip() -> None:
    prior = DeltaPrior(mean_w=50.0, sigma_w=2.0, sign="pos", model_version_id="mdl-x-1")
    assert DeltaPrior.model_validate_json(prior.model_dump_json()) == prior


def test_delta_prior_rejects_bad_sign() -> None:
    with pytest.raises(ValidationError):
        DeltaPrior(mean_w=50.0, sigma_w=2.0, sign="sideways", model_version_id="mdl-x-1")


# --------------------------------------------------------------------------- #
# Versions and events.
# --------------------------------------------------------------------------- #


def _version() -> PowerModelVersion:
    return PowerModelVersion(
        version_id="mdl-dev_light-1",
        subject_kind="device",
        subject_id="dev_light",
        number=1,
        model_class="FIXED",
        basis="DELTA",
        spec=SPEC_ADAPTER.validate_python(_SPEC_SAMPLES["fixed"]),
        schema_version=SPEC_SCHEMA_VERSION,
        evidence_refs=[{"kind": "event_group", "id": "grp-1"}],
        topology_version_at_training="topo-3",
        trainer_version="trainer-1",
        parent_version_id=None,
        created_at=AT,
    )


def test_power_model_version_round_trip() -> None:
    version = _version()
    assert PowerModelVersion.model_validate_json(version.model_dump_json()) == version
    assert version.schema_version == 1


def test_power_model_event_round_trip_and_closed_vocabulary() -> None:
    for event in (
        "candidate",
        "activated",
        "superseded",
        "rejected",
        "rolled_back",
        "frozen",
        "unfrozen",
    ):
        record = PowerModelEvent(
            version_id="mdl-dev_light-1", event=event, reason="r", at=AT
        )
        assert PowerModelEvent.model_validate_json(record.model_dump_json()) == record
    with pytest.raises(ValidationError):
        PowerModelEvent(version_id="v", event="bogus", reason="r", at=AT)


# --------------------------------------------------------------------------- #
# Models carry a basis for learning observations (ABSOLUTE_ANCHOR -> level_w).
# --------------------------------------------------------------------------- #


def test_model_basis_members() -> None:
    assert {m.value for m in ModelBasis} == {
        "DELTA",
        "ABSOLUTE_ANCHOR",
        "LEGACY_ABSOLUTE",
    }


def test_model_class_members() -> None:
    assert {m.value for m in ModelClass} == {
        "FIXED",
        "ADJUSTABLE",
        "DYNAMIC",
        "RESPONSE_KERNEL",
        "COMPONENT_SUM",
    }


def _observation(**overrides: object) -> LearningObservation:
    data = {
        "observation_id": observation_id("grp-1", 2, "dev_light", "ct-1", "topo-3"),
        "subject_kind": "device",
        "subject_id": "dev_light",
        "endpoint_id": "ct-1",
        "event_group_ref": {"kind": "event_group", "id": "grp-1"},
        "transition": "on",
        "attributes": {"brightness": 0.5},
        "delta_w": 50.0,
        "level_w": None,
        "status": "confirmed",
        "rejection_reason": None,
        "identifiability": "identifiable",
        "basis": "DELTA",
        "created_at": AT,
    }
    data.update(overrides)
    return LearningObservation.model_validate(data)


def test_observation_id_is_deterministic() -> None:
    args = ("grp-1", 2, "dev_light", "ct-1", "topo-3")
    assert observation_id(*args) == observation_id(*args)
    # Independent of argument mutability and re-derivable from the same inputs.
    assert observation_id(*list(args)) == observation_id(*args)


def test_observation_id_matches_sha256_of_canonical_inputs() -> None:
    args = ("grp-1", 2, "dev_light", "ct-1", "topo-3")
    canonical = "\x1f".join([str(a) for a in args])
    expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    assert observation_id(*args) == expected


def test_observation_id_changes_with_any_input() -> None:
    base = observation_id("grp-1", 2, "dev", "ct", "topo")
    for i in range(5):
        args = ["grp-1", 2, "dev", "ct", "topo"]
        args[i] = str(args[i]) + "-x"
        assert observation_id(*args) != base


def test_observation_round_trip() -> None:
    obs = _observation()
    assert LearningObservation.model_validate_json(obs.model_dump_json()) == obs


def test_level_w_allowed_only_for_absolute_anchor() -> None:
    # ABSOLUTE_ANCHOR may set level_w.
    anchored = _observation(basis="ABSOLUTE_ANCHOR", level_w=7.0)
    assert anchored.level_w == 7.0
    # DELTA requires level_w to be None.
    with pytest.raises(ValidationError):
        _observation(basis="DELTA", level_w=7.0)
    # ABSOLUTE_ANCHOR may legitimately leave it as None as well.
    assert _observation(basis="ABSOLUTE_ANCHOR", level_w=None).level_w is None


# --------------------------------------------------------------------------- #
# Facade reachability.
# --------------------------------------------------------------------------- #


def test_power_model_names_reachable_from_facade() -> None:
    assert contracts.PowerModelSpec is PowerModelSpec
    assert contracts.PowerPrediction is PowerPrediction
    assert contracts.LearningObservation is LearningObservation
