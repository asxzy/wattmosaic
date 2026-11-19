"""D2: endpoint discovery and the smart-plug scope workflow.

A bare ``sensor.*_power`` published by an appliance/plug HA device is a
**candidate single-subject Measurement Endpoint**, never a CT channel and never
a device minted by discovery.  CT channels from a multi-channel monitor are
endpoints of ``kind=CT``.  Discovery stops minting ``dev_<ct_id>`` devices;
legacy rows are left untouched for the panel to offer for retirement.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from wattmosaic_engine.contracts import (
    EndpointKind,
    EndpointScope,
    HaEntityLink,
    HaEntityRole,
    MeasurementEndpoint,
    PhysicalDevice,
    ScopeStatus,
)
from wattmosaic_engine.ingestion.discovery import (
    devices_for_candidate_endpoints,
    discover_candidate_endpoints,
)


def _power(entity_id: str, value: str = "1.0") -> dict:
    return {
        "entity_id": entity_id,
        "state": value,
        "attributes": {
            "unit_of_measurement": "W",
            "device_class": "power",
            "friendly_name": entity_id,
        },
    }


def _plug_state() -> list[dict]:
    return [_power("sensor.washer_power", "60.0")]


def _plug_registry() -> dict:
    return {
        "entities": [
            {
                "entity_id": "sensor.washer_power",
                "platform": "matter",
                "name": "Washer power",
                "device_id": "ha_plug_1",
            }
        ],
        "devices": [{"id": "ha_plug_1", "name": "Washer Plug"}],
    }


def _monitor_registry() -> dict:
    return {
        "entities": [
            {
                "entity_id": eid,
                "platform": "shelly",
                "name": eid,
                "device_id": "ha_monitor_1",
            }
            for eid in (
                "sensor.panel_ct1_power",
                "sensor.panel_ct2_power",
                "sensor.panel_water_heater_ct1_power",
            )
        ],
        "devices": [{"id": "ha_monitor_1", "name": "Main Panel"}],
    }


def _monitor_state() -> list[dict]:
    return [
        _power("sensor.panel_ct1_power", "500.0"),
        _power("sensor.panel_ct2_power", "480.0"),
        _power("sensor.panel_water_heater_ct1_power", "900.0"),
    ]


def _now() -> datetime:
    return datetime(2026, 1, 2, 3, 4, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Discovery of endpoints.
# --------------------------------------------------------------------------- #


def test_plug_is_discovered_as_candidate_single_subject_endpoint():
    endpoints = discover_candidate_endpoints(_plug_state(), _plug_registry(), clock=_now)

    plug = next(e for e in endpoints if e.entity_id == "sensor.washer_power")
    assert plug.kind == EndpointKind.SMART_PLUG
    assert plug.scope == EndpointScope.SINGLE_SUBJECT
    assert plug.scope_status == ScopeStatus.CANDIDATE
    assert plug.scope_subject_id == "pd_from_ha_plug_1"
    # A candidate endpoint is not ingested until the user confirms its scope.
    assert plug.enabled is False
    # Discovery created an endpoint, not a device: devices_for_candidate_endpoints
    # is the only path that creates a Physical Device, and only on demand.
    devices = devices_for_candidate_endpoints(endpoints, _plug_registry(), clock=_now)
    assert all(d.device_id.startswith("pd_") for d in devices)
    assert not any(d.device_id.startswith("dev_") for d in devices)


def test_ct_channels_become_ct_endpoints():
    endpoints = discover_candidate_endpoints(_monitor_state(), _monitor_registry(), clock=_now)

    by_entity = {e.entity_id: e for e in endpoints}
    ct1 = by_entity["sensor.panel_ct1_power"]
    assert ct1.kind == EndpointKind.CT
    assert ct1.scope == EndpointScope.PANEL_FEED
    assert ct1.enabled is True
    # A circuit CT is never auto single-subject (DEC-05).
    circuit = by_entity["sensor.panel_water_heater_ct1_power"]
    assert circuit.scope == EndpointScope.CIRCUIT
    assert circuit.scope_status == ScopeStatus.CANDIDATE
    assert circuit.scope_subject_id is None


def test_discovery_no_longer_mints_dev_ct_devices():
    endpoints = discover_candidate_endpoints(_monitor_state(), _monitor_registry(), clock=_now)
    assert endpoints, "the monitor's CT channels are still discovered"
    devices = devices_for_candidate_endpoints(endpoints, _monitor_registry(), clock=_now)
    assert devices == [], "CT endpoints must never mint dev_<ct> devices (DEC-04)"


def test_legacy_dev_ct_device_survives_unchanged_via_device_service(tmp_path):
    from wattmosaic_engine.devices.service import DeviceService
    from wattmosaic_engine.storage import Database

    db = Database(tmp_path / "d2.db")
    db.repos.entities.upsert_device(
        PhysicalDevice(
            device_id="dev_panel_lights",
            name="Lights",
            ha_device_id="ha_unique_123",
            origin="migration",
            created_at=_now(),
        )
    )
    # A legacy CT device survives with its id and HA unique id unchanged (DEC-04).
    service = DeviceService(db)
    device = service.get("dev_panel_lights")
    assert device is not None
    assert device.device_id == "dev_panel_lights"
    assert device.ha_device_id == "ha_unique_123"
    assert device.retired_at is None
    # And it is offered for retirement, not deleted.
    retired = service.retire("dev_panel_lights", at=_now())
    assert retired is not None and retired.retired_at == _now()
    assert service.get("dev_panel_lights") is not None


# --------------------------------------------------------------------------- #
# Confirmed plug yields MEASURED (with a fake operator / model set).
# --------------------------------------------------------------------------- #


def test_confirmed_plug_yields_measured_for_its_subject():
    from wattmosaic_engine.contracts import EstimateStatus, IdentifiabilityStatus
    from wattmosaic_engine.devices.service import confirmed_endpoint_estimate_status

    subject = "pd_from_ha_plug_1"
    endpoint = MeasurementEndpoint(
        endpoint_id="washer_plug",
        kind=EndpointKind.SMART_PLUG,
        entity_id="sensor.washer_power",
        energy_entity_id=None,
        display_name="Washer",
        scope=EndpointScope.SINGLE_SUBJECT,
        scope_status=ScopeStatus.CONFIRMED,
        scope_subject_id=subject,
        panel_id=None,
        enabled=True,
        created_at=_now(),
    )
    status = confirmed_endpoint_estimate_status(endpoint, subject)
    assert status == (EstimateStatus.MEASURED, IdentifiabilityStatus.IDENTIFIABLE), (
        "a confirmed single-subject endpoint must produce MEASURED for its subject"
    )
    # A candidate endpoint never yields a measured status.
    candidate = endpoint.model_copy(update={"scope_status": ScopeStatus.CANDIDATE})
    assert confirmed_endpoint_estimate_status(candidate, subject) is None


# --------------------------------------------------------------------------- #
# A circuit CT can never be auto single-subject.
# --------------------------------------------------------------------------- #


def test_circuit_ct_cannot_be_auto_single_subject():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        MeasurementEndpoint(
            endpoint_id="ct_circuit",
            kind=EndpointKind.CT,
            entity_id="sensor.circuit_power",
            energy_entity_id=None,
            scope=EndpointScope.SINGLE_SUBJECT,
            scope_status=ScopeStatus.CANDIDATE,
            scope_subject_id="dev_some",
            enabled=True,
            created_at=_now(),
        )


# --------------------------------------------------------------------------- #
# One HA entity linked to two subjects is accepted (DEC-08).
# --------------------------------------------------------------------------- #


def test_one_ha_entity_linked_to_two_subjects_is_accepted(tmp_path):
    from wattmosaic_engine.storage import Database

    links = [
        HaEntityLink(
            entity_id="light.hallway",
            target_kind="device",
            target_id="pd_hvac",
            role=HaEntityRole.LOAD_STATE,
            valid_from=_now(),
            declared_by="user",
        ),
        HaEntityLink(
            entity_id="light.hallway",
            target_kind="component",
            target_id="lc_fan",
            role=HaEntityRole.LOAD_STATE,
            valid_from=_now(),
            declared_by="user",
        ),
    ]
    # One-to-many is legal: both links persist, neither is refused.
    from wattmosaic_engine.contracts import HaEntity, LoadComponent

    db = Database(tmp_path / "links.db")
    entities = db.repos.entities
    entities.upsert_ha_entity(
        HaEntity(entity_id="light.hallway", domain="light", first_seen=_now(), last_seen=_now())
    )
    entities.upsert_device(
        PhysicalDevice(device_id="pd_hvac", name="HVAC", origin="user", created_at=_now())
    )
    entities.upsert_component(
        LoadComponent(component_id="lc_fan", device_id="pd_hvac", name="Fan")
    )
    for link in links:
        db.repos.entities.add_entity_link(link)
    stored = db.repos.entities.links_for_entity("light.hallway")
    assert {(row["target_kind"], row["target_id"]) for row in stored} == {
        ("component", "lc_fan"),
        ("device", "pd_hvac"),
    }


# Keep the runtime fake dependency exercised: the test depends only on the
# Protocol in production, and uses the K08 fake as the deterministic double.
def test_endpoint_operator_builds_for_a_confirmed_plug():
    from wattmosaic_engine.runtime.fakes import FakeModelSet, fake_operator

    class _Topo:
        def ancestors(self, r):
            return ()

        def soft_alternatives(self):
            return ()

    class _Map:
        def most_specific_reader(self, s):
            return "washer_plug"

    op = fake_operator(
        topology=_Topo(),
        mapping=_Map(),
        reader_ids=("washer_plug",),
        subjects=(("device", "pd_from_ha_plug_1"),),
    )
    assert op.H.shape == (1, 1)
    assert FakeModelSet.fixed({}) is not None
