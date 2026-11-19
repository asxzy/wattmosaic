"""K02: measurement and event contracts (HB §2, §6, §12.3).

Round-trips, content-hash stability, the null-preserving reader delta, the
``with_sample`` immutability-by-construction rule and the six-state event group
lifecycle are covered here.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from wattmosaic_engine import contracts
from wattmosaic_engine.contracts import legacy, measurement
from wattmosaic_engine.contracts.foundation import EvidenceRef, VersionTriple
from wattmosaic_engine.contracts.measurement import (
    EventEvidence,
    EventGroup,
    EventGroupStatus,
    HAEvent,
    MeasurementEvent,
    MeasurementType,
    QualityFlag,
    ReaderDelta,
    ReaderMeasurement,
    StepClass,
    SubEvent,
    SubEventKind,
    ha_event_id,
    measurement_event_id,
)

SCHEMA_MODELS = [model for _name, model in measurement.SCHEMA_EXPORTS]

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
T1 = T0 + timedelta(seconds=5)

VERSIONS = {
    "topology_version_id": "topo-1",
    "mapping_version_id": "map-1",
    "model_set_id": "mset-0123456789ab",
}


def _reader_measurement() -> ReaderMeasurement:
    return ReaderMeasurement(
        reader_id="reader-1",
        observed_at=T0,
        received_at=T1,
        active_power_w=123.5,
        current_rms_a=None,
        voltage_rms_v=None,
        reactive_power_var=None,
        power_factor=None,
        energy_wh=None,
        phase="l1",
        leg=None,
        voltage_reference_v=120.0,
        measurement_type=MeasurementType.INSTANTANEOUS,
        measurement_window_ms=None,
        calibration_version="cal-1",
        measurement_basis="ct_measured",
        quality_flags=[],
        sequence=1,
    )


def _ha_event(**overrides: object) -> HAEvent:
    data: dict[str, object] = {
        "entity_id": "switch.kitchen",
        "observed_at": T0,
        "received_at": T1,
        "from_state": "off",
        "to_state": "on",
        "attributes_delta": {"brightness": 255, "friendly_name": "Kitchen"},
        "context_id": "ctx-1",
        "context_parent_id": None,
        "context_user_id": None,
        "linked": True,
    }
    data.update(overrides)
    return HAEvent.make(**data)  # type: ignore[arg-type]


def _measurement_event(**overrides: object) -> MeasurementEvent:
    data: dict[str, object] = {
        "event_id": measurement_event_id("reader-1", T0, "detector-v1"),
        "reader_id": "reader-1",
        "delta_w": 50.0,
        "sigma_w": 2.0,
        "z_score": 25.0,
        "step_class": StepClass.SUSTAINED,
        "onset_lo": T0,
        "onset_hi": T1,
        "pre_level_w": 10.0,
        "post_level_w": 60.0,
        "quality_score": 0.9,
        "detected_at": T1,
    }
    data.update(overrides)
    return MeasurementEvent(**data)  # type: ignore[arg-type]


def _evidence(**overrides: object) -> EventEvidence:
    data: dict[str, object] = {
        "evidence_id": "ev-1",
        "reader_id": "reader-1",
        "measurement_event_id": "mev-1",
        "window_start": T0,
        "window_end": T1,
        "samples": [(T0, 10.0), (T1, None)],
        "baseline_w": 10.0,
        "sigma_w": 2.0,
        "delta_w": 50.0,
        "quality_flags": [QualityFlag.REPEATED],
        "topology_version_id": "topo-1",
        "finalized": True,
    }
    data.update(overrides)
    return EventEvidence(**data)  # type: ignore[arg-type]


def _sub_event() -> SubEvent:
    return SubEvent(
        sub_event_id="sub-1",
        kind=SubEventKind.HA_STATE,
        ref=EvidenceRef(kind="ha_event", id="evt-1"),
        observed_at=T0,
        timestamp_uncertainty_s=1.0,
    )


def _reader_delta(**overrides: object) -> ReaderDelta:
    data: dict[str, object] = {
        "reader_id": "reader-1",
        "delta_w": 50.0,
        "sigma_w": 2.0,
        "completeness": "complete",
        "evidence_ref": EvidenceRef(kind="event_evidence", id="ev-1"),
    }
    data.update(overrides)
    return ReaderDelta(**data)  # type: ignore[arg-type]


def _event_group(**overrides: object) -> EventGroup:
    data: dict[str, object] = {
        "group_id": "grp-1",
        "revision": 1,
        "status": EventGroupStatus.DETECTED,
        "opened_at": T0,
        "closed_at": None,
        "sub_events": [_sub_event()],
        "reader_deltas": [_reader_delta()],
        "grouping_reasons": ["temporal_overlap"],
        "versions": VersionTriple(**VERSIONS),
    }
    data.update(overrides)
    return EventGroup(**data)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Naming / export invariants.
# --------------------------------------------------------------------------- #


def test_no_public_name_collides_with_legacy() -> None:
    legacy_names = set(dir(legacy))
    collisions = sorted(name for name in measurement.__all__ if name in legacy_names)
    assert collisions == [], f"measurement __all__ names shadow legacy: {collisions}"


def test_every_exported_name_is_in_all() -> None:
    for model in SCHEMA_MODELS:
        assert model.__name__ in measurement.__all__


# --------------------------------------------------------------------------- #
# Round-trips for every exported model.
# --------------------------------------------------------------------------- #

_SAMPLES = {
    ReaderMeasurement: _reader_measurement,
    HAEvent: _ha_event,
    MeasurementEvent: _measurement_event,
    EventEvidence: _evidence,
    SubEvent: _sub_event,
    ReaderDelta: _reader_delta,
    EventGroup: _event_group,
}


@pytest.mark.parametrize("model", SCHEMA_MODELS, ids=lambda m: m.__name__)
def test_round_trip(model: type) -> None:
    obj = _SAMPLES[model]()  # type: ignore[index]
    payload = obj.model_dump(mode="json")
    assert json.loads(json.dumps(payload)) == payload  # JSON-safe
    restored = model.model_validate(payload)
    assert restored == obj


@pytest.mark.parametrize("model", SCHEMA_MODELS, ids=lambda m: m.__name__)
def test_extra_fields_are_rejected(model: type) -> None:
    payload = _SAMPLES[model]().model_dump(mode="json")  # type: ignore[union-attr]
    payload["unexpected_field"] = 1
    with pytest.raises(ValidationError):
        model.model_validate(payload)


# --------------------------------------------------------------------------- #
# Content-hash stability.
# --------------------------------------------------------------------------- #


def test_ha_event_hash_is_stable_and_state_sensitive() -> None:
    a = _ha_event()
    b = _ha_event()
    assert a.event_id == b.event_id
    assert a.event_id == ha_event_id(
        "switch.kitchen", T0, "on", {"brightness": 255, "friendly_name": "Kitchen"}
    )
    different = _ha_event(to_state="off")
    assert different.event_id != a.event_id
    # Attribute dict order does not change the hash ("sorted attributes_delta").
    reordered = _ha_event(attributes_delta={"friendly_name": "Kitchen", "brightness": 255})
    assert reordered.event_id == a.event_id


def test_measurement_event_hash_helper() -> None:
    first = measurement_event_id("reader-1", T0, "detector-v1")
    assert first == measurement_event_id("reader-1", T0, "detector-v1")
    assert first != measurement_event_id("reader-1", T0, "detector-v2")
    assert first != measurement_event_id("reader-2", T0, "detector-v1")
    assert first != measurement_event_id("reader-1", T1, "detector-v1")
    assert _measurement_event().event_id == first


# --------------------------------------------------------------------------- #
# Validators and immutability-by-construction.
# --------------------------------------------------------------------------- #


def test_onset_order_validator() -> None:
    with pytest.raises(ValidationError):
        _measurement_event(onset_lo=T1, onset_hi=T0)


def test_data_gap_event_has_null_measurements() -> None:
    # A DATA_GAP carries no measurement: absent is None, never 0.0 (HB §18).
    gap = _measurement_event(
        step_class=StepClass.DATA_GAP,
        delta_w=None,
        sigma_w=None,
        z_score=None,
    )
    assert gap.delta_w is None
    assert gap.sigma_w is None
    assert gap.z_score is None
    # Even a round-trip must preserve the nulls.
    restored = MeasurementEvent.model_validate(gap.model_dump(mode="json"))
    assert restored.delta_w is None
    assert restored.sigma_w is None
    assert restored.z_score is None


def test_data_gap_rejects_numeric_measurements() -> None:
    # A gap may not smuggle a 0.0 sentinel (or any other number) past the
    # contract's null policy.
    for field in ("delta_w", "sigma_w", "z_score"):
        with pytest.raises(ValidationError):
            _measurement_event(step_class=StepClass.DATA_GAP, **{field: 0.0})


def test_non_gap_event_rejects_null_delta() -> None:
    # A non-DATA_GAP step must carry a measured delta (and sigma/z score); a
    # missing value may not be represented by None.
    for step_class in (StepClass.SUSTAINED, StepClass.TRANSIENT, StepClass.PERIODIC, StepClass.DRIFT):
        with pytest.raises(ValidationError):
            _measurement_event(step_class=step_class, delta_w=None)
    with pytest.raises(ValidationError):
        _measurement_event(sigma_w=None)
    with pytest.raises(ValidationError):
        _measurement_event(z_score=None)


def test_event_evidence_with_sample_returns_new_record_id() -> None:
    original = _evidence(finalized=True)
    updated = original.with_sample(T1 + timedelta(seconds=5), 12.0)
    assert updated is not original
    assert updated.evidence_id != original.evidence_id
    assert len(updated.samples) == len(original.samples) + 1
    # The original record is untouched: immutability by construction.
    assert original.samples == [(T0, 10.0), (T1, None)]
    assert original.evidence_id == "ev-1"
    assert updated.model_dump(mode="json")["samples"][-1][1] == 12.0


def test_event_group_revision_is_at_least_one() -> None:
    assert _event_group(revision=1).revision == 1
    assert _event_group(revision=2).revision == 2
    with pytest.raises(ValidationError):
        _event_group(revision=0)


def test_reader_delta_missing_iff_delta_w_is_none() -> None:
    # complete with a value is fine; missing with None is fine.
    assert _reader_delta(completeness="complete", delta_w=1.0).delta_w == 1.0
    missing = _reader_delta(completeness="missing", delta_w=None, sigma_w=None)
    assert missing.delta_w is None  # never 0
    # A missing delta may not carry a number, and a present delta may not be None.
    with pytest.raises(ValidationError):
        _reader_delta(completeness="missing", delta_w=0.0)
    for completeness in ("complete", "pre_missing", "post_pending"):
        with pytest.raises(ValidationError):
            _reader_delta(completeness=completeness, delta_w=None)


def test_event_group_status_covers_six_hb_states_in_order() -> None:
    assert [state.value for state in EventGroupStatus] == [
        "detected",
        "matching",
        "provisional",
        "confirmed",
        "revised",
        "unresolved",
    ]


def test_enum_member_sets() -> None:
    assert [m.value for m in MeasurementType] == [
        "instantaneous",
        "window_average",
        "device_reported",
        "calculated",
        "accumulated_energy",
    ]
    assert [m.value for m in QualityFlag] == [
        "unit_invalid",
        "implausible_range",
        "repeated",
        "stale_on_arrival",
        "clock_skew_future",
        "primed",
        "out_of_order",
    ]
    assert [m.value for m in StepClass] == [
        "sustained",
        "transient",
        "periodic",
        "drift",
        "data_gap",
    ]
    assert [m.value for m in SubEventKind] == [
        "ha_state",
        "ha_attribute",
        "reader_step",
        "plug_step",
    ]


def test_facade_exports_are_available() -> None:
    for name in measurement.__all__:
        assert hasattr(contracts, name)
