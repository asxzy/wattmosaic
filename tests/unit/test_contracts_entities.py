"""K01: entity contracts — devices, components, HA entities/links and endpoints.

Covers HB §1.1's four object kinds and §1.2's many-to-many cardinalities, the
scope validators (including the DEC-05 circuit-CT guard) and legacy id
preservation (DEC-04).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from wattmosaic_engine import contracts
from wattmosaic_engine.contracts import entities, legacy
from wattmosaic_engine.contracts.entities import (
    EndpointConfigRevision,
    EndpointKind,
    EndpointScope,
    HaEntity,
    HaEntityLink,
    LoadComponent,
    MeasurementEndpoint,
    PhysicalDevice,
    ScopeStatus,
)

SCHEMA_MODELS = [model for _name, model in entities.SCHEMA_EXPORTS]
T0 = datetime(2026, 1, 1, tzinfo=UTC)
T1 = T0 + timedelta(hours=1)


def _device(device_id: str = "dev_ct_07") -> PhysicalDevice:
    return PhysicalDevice(
        device_id=device_id,
        name="Living-room lights",
        device_type="light",
        load_shape=legacy.LoadShape.UNKNOWN,
        ha_device_id=None,
        area_id="living_room",
        origin="migration",
        created_at=T0,
        retired_at=None,
    )


def _endpoint(
    *,
    endpoint_id: str = "ct_07",
    kind: EndpointKind = EndpointKind.CT,
    scope: EndpointScope = EndpointScope.CIRCUIT,
    scope_status: ScopeStatus = ScopeStatus.CANDIDATE,
    scope_subject_id: str | None = None,
    entity_id: str | None = "sensor.ct_07_power",
    revisions: list[EndpointConfigRevision] | None = None,
) -> MeasurementEndpoint:
    return MeasurementEndpoint(
        endpoint_id=endpoint_id,
        kind=kind,
        entity_id=entity_id,
        energy_entity_id=None,
        display_name=None,
        scope=scope,
        scope_status=scope_status,
        scope_subject_id=scope_subject_id,
        panel_id="panel_1",
        enabled=True,
        created_at=T0,
        revisions=revisions or [],
    )


def _link(entity_id: str, target_id: str, target_kind: str = "device") -> HaEntityLink:
    return HaEntityLink(
        entity_id=entity_id,
        target_kind=target_kind,
        target_id=target_id,
        role=legacy.HaEntityRole.LOAD_STATE,
        valid_from=T0,
        valid_until=None,
        declared_by="user",
    )


def _revision(
    *,
    revision: int = 1,
    declared_by: str = "migration",
    valid_from: datetime = T0,
    valid_until: datetime | None = None,
) -> EndpointConfigRevision:
    return EndpointConfigRevision(
        endpoint_id="ct_07",
        revision=revision,
        valid_from=valid_from,
        valid_until=valid_until,
        phase="l1",
        leg=None,
        voltage_reference_v=None,
        polarity="normal",
        multiplier=1.0,
        calibration_version="v1",
        measurement_type="instantaneous",
        measurement_window_ms=None,
        expected_interval_s=None,
        resolution_w=None,
        declared_by=declared_by,
    )


# --------------------------------------------------------------------------- #
# Naming / export hygiene (contract-task rules).
# --------------------------------------------------------------------------- #


def test_no_public_name_collides_with_legacy() -> None:
    legacy_names = set(dir(legacy))
    collisions = sorted(name for name in entities.__all__ if name in legacy_names)
    assert collisions == [], f"entities __all__ names shadow legacy: {collisions}"


def test_every_exported_model_is_in_all_and_schema_exports() -> None:
    for model in SCHEMA_MODELS:
        assert model.__name__ in entities.__all__
    expected = {
        "MeasurementEndpoint",
        "EndpointConfigRevision",
        "PhysicalDevice",
        "LoadComponent",
        "HaEntity",
        "HaEntityLink",
    }
    assert {model.__name__ for model in SCHEMA_MODELS} == expected


def test_no_retired_dedicated_vocabulary_in_public_names() -> None:
    assert not [name for name in entities.__all__ if "dedicated" in name.lower()]


# --------------------------------------------------------------------------- #
# JSON round-trips for every exported model.
# --------------------------------------------------------------------------- #

_SAMPLES = {
    MeasurementEndpoint: {
        "endpoint_id": "ct_07",
        "kind": "ct",
        "entity_id": None,
        "energy_entity_id": None,
        "display_name": None,
        "scope": "circuit",
        "scope_status": "candidate",
        "scope_subject_id": None,
        "panel_id": "panel_1",
        "enabled": True,
        "created_at": T0.isoformat(),
        "revisions": [],
    },
    EndpointConfigRevision: {
        "endpoint_id": "ct_07",
        "revision": 1,
        "valid_from": T0.isoformat(),
        "valid_until": None,
        "phase": "both",
        "leg": None,
        "voltage_reference_v": None,
        "polarity": "normal",
        "multiplier": 1.0,
        "calibration_version": "v1",
        "measurement_type": "instantaneous",
        "measurement_window_ms": None,
        "expected_interval_s": None,
        "resolution_w": None,
        "declared_by": "migration",
    },
    PhysicalDevice: {
        "device_id": "dev_ct_07",
        "name": "Lights",
        "device_type": "light",
        "load_shape": "unknown",
        "sign_class": "consumer",
        "ha_device_id": None,
        "area_id": None,
        "origin": "migration",
        "created_at": T0.isoformat(),
        "retired_at": None,
    },
    LoadComponent: {
        "component_id": "lc_01",
        "device_id": "dev_ct_07",
        "name": "Compressor",
        "load_shape": "unknown",
        "sign_class": "consumer",
    },
    HaEntity: {
        "entity_id": "light.living_room",
        "domain": "light",
        "ha_device_id": "ha_1",
        "first_seen": T0.isoformat(),
        "last_seen": T1.isoformat(),
    },
    HaEntityLink: {
        "entity_id": "light.living_room",
        "target_kind": "device",
        "target_id": "dev_ct_07",
        "role": "load_state",
        "valid_from": T0.isoformat(),
        "valid_until": None,
        "declared_by": "user",
    },
}


@pytest.mark.parametrize("model", SCHEMA_MODELS, ids=lambda m: m.__name__)
def test_model_round_trips_through_json(model: type) -> None:
    instance = model.model_validate(_SAMPLES[model])
    payload = instance.model_dump_json()
    assert model.model_validate_json(payload) == instance


# --------------------------------------------------------------------------- #
# HB §1.2 cardinality bullets, built from the entity/link models.
# --------------------------------------------------------------------------- #


def test_one_device_to_several_components() -> None:
    device = _device()
    components = [
        LoadComponent(
            component_id=f"lc_{i}",
            device_id=device.device_id,
            name=f"Component {i}",
            load_shape=legacy.LoadShape.UNKNOWN,
        )
        for i in range(3)
    ]
    assert len({c.component_id for c in components}) == 3
    assert {c.device_id for c in components} == {device.device_id}


def test_one_device_to_several_entities() -> None:
    device = _device()
    links = [_link(f"light.room_{i}", device.device_id) for i in range(3)]
    assert len({link.entity_id for link in links}) == 3
    assert {link.target_id for link in links} == {device.device_id}


def test_one_device_to_several_endpoints() -> None:
    device = _device()
    endpoints = [
        _endpoint(
            endpoint_id=f"plug_{i}",
            kind=EndpointKind.SMART_PLUG,
            scope=EndpointScope.SINGLE_SUBJECT,
            scope_status=ScopeStatus.CONFIRMED,
            scope_subject_id=device.device_id,
        )
        for i in range(3)
    ]
    assert len({e.endpoint_id for e in endpoints}) == 3
    assert {e.scope_subject_id for e in endpoints} == {device.device_id}


def test_one_reader_to_many_devices() -> None:
    """A circuit reader observes many devices — no single FK is forced."""
    reader = _endpoint(endpoint_id="ct_main", kind=EndpointKind.CT, scope=EndpointScope.CIRCUIT)
    devices = [_device(f"dev_{i}") for i in range(3)]
    links = [_link(reader.entity_id, device.device_id) for device in devices]
    assert len({link.target_id for link in links}) == 3
    assert all(link.entity_id == reader.entity_id for link in links)
    assert "device_id" not in MeasurementEndpoint.model_fields


def test_many_readers_to_one_device() -> None:
    device = _device()
    readers = [
        _endpoint(
            endpoint_id=f"reader_{i}",
            kind=kind,
            scope=EndpointScope.SINGLE_SUBJECT,
            scope_status=ScopeStatus.CONFIRMED,
            scope_subject_id=device.device_id,
        )
        for i, kind in enumerate((EndpointKind.SMART_PLUG, EndpointKind.APPLIANCE_MONITOR))
    ]
    assert len({r.endpoint_id for r in readers}) == 2
    assert {r.scope_subject_id for r in readers} == {device.device_id}


def test_one_entity_to_several_subjects() -> None:
    entity_id = "light.hallway"
    links = [
        _link(entity_id, "dev_hvac", "device"),
        _link(entity_id, "lc_compressor", "component"),
        _link(entity_id, "lc_fan", "component"),
    ]
    assert {link.entity_id for link in links} == {entity_id}
    assert {(link.target_kind, link.target_id) for link in links} == {
        ("device", "dev_hvac"),
        ("component", "lc_compressor"),
        ("component", "lc_fan"),
    }


def test_endpoint_with_no_ha_device() -> None:
    endpoint = _endpoint(entity_id=None, kind=EndpointKind.CT, scope=EndpointScope.CIRCUIT)
    device = _device("dev_unobserved")
    assert endpoint.entity_id is None
    assert device.ha_device_id is None
    assert contracts.entities.HaEntityLink is HaEntityLink


# --------------------------------------------------------------------------- #
# Scope and interval validators.
# --------------------------------------------------------------------------- #


def test_scope_subject_id_requires_single_subject() -> None:
    with pytest.raises(ValidationError):
        _endpoint(scope=EndpointScope.CIRCUIT, scope_subject_id="dev_1")


def test_single_subject_requires_a_subject_id() -> None:
    with pytest.raises(ValidationError):
        _endpoint(
            kind=EndpointKind.SMART_PLUG,
            scope=EndpointScope.SINGLE_SUBJECT,
            scope_status=ScopeStatus.CONFIRMED,
            scope_subject_id=None,
        )


def test_confirmed_single_subject_endpoint_is_accepted() -> None:
    endpoint = _endpoint(
        kind=EndpointKind.SMART_PLUG,
        scope=EndpointScope.SINGLE_SUBJECT,
        scope_status=ScopeStatus.CONFIRMED,
        scope_subject_id="dev_1",
    )
    assert endpoint.scope_subject_id == "dev_1"


def test_ct_single_subject_requires_confirmed_scope() -> None:
    with pytest.raises(ValidationError):
        _endpoint(
            kind=EndpointKind.CT,
            scope=EndpointScope.SINGLE_SUBJECT,
            scope_status=ScopeStatus.CANDIDATE,
            scope_subject_id="dev_1",
            revisions=[_revision(declared_by="user")],
        )


def test_ct_single_subject_requires_user_declared_latest_revision() -> None:
    with pytest.raises(ValidationError):
        _endpoint(
            kind=EndpointKind.CT,
            scope=EndpointScope.SINGLE_SUBJECT,
            scope_status=ScopeStatus.CONFIRMED,
            scope_subject_id="dev_1",
            revisions=[_revision(declared_by="migration")],
        )


def test_ct_single_subject_checks_the_latest_revision_only() -> None:
    """An older discovery revision cannot taint a user-declared latest one."""
    endpoint = _endpoint(
        kind=EndpointKind.CT,
        scope=EndpointScope.SINGLE_SUBJECT,
        scope_status=ScopeStatus.CONFIRMED,
        scope_subject_id="dev_1",
        revisions=[
            _revision(revision=1, declared_by="discovery"),
            _revision(revision=2, declared_by="user", valid_from=T1),
        ],
    )
    assert endpoint.latest_revision.revision == 2


def test_a_circuit_ct_never_auto_receives_single_subject() -> None:
    """A CT with a candidate scope and no user revision is rejected."""
    with pytest.raises(ValidationError):
        _endpoint(
            kind=EndpointKind.CT,
            scope=EndpointScope.SINGLE_SUBJECT,
            scope_status=ScopeStatus.CANDIDATE,
            scope_subject_id="dev_1",
        )


@pytest.mark.parametrize(
    "factory",
    [
        lambda: _revision(valid_until=T0),
        lambda: HaEntityLink(
            entity_id="light.x",
            target_kind="device",
            target_id="dev_x",
            role=legacy.HaEntityRole.UNKNOWN,
            valid_from=T0,
            valid_until=T0,
            declared_by="user",
        ),
    ],
)
def test_valid_until_must_follow_valid_from(factory) -> None:
    with pytest.raises(ValidationError):
        factory()


def test_valid_until_after_valid_from_is_accepted() -> None:
    assert _revision(valid_until=T1).valid_until == T1
    assert _link("light.x", "dev_x").valid_until is None


# --------------------------------------------------------------------------- #
# Legacy id preservation (DEC-04) and reused enums.
# --------------------------------------------------------------------------- #


def test_legacy_device_id_is_accepted_verbatim() -> None:
    device = _device("dev_ct_07")
    assert device.device_id == "dev_ct_07"
    component = LoadComponent(
        component_id="lc_01",
        device_id="dev_ct_07",
        name="Compressor",
        load_shape=None,
    )
    assert component.device_id == "dev_ct_07"


def test_reused_legacy_enums_are_the_real_objects() -> None:
    assert entities.LoadShape is legacy.LoadShape
    assert entities.HaEntityRole is legacy.HaEntityRole
    assert entities.MeasurementBasis is legacy.MeasurementBasis
