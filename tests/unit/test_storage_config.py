"""Storage: per-device configuration persistence (migration 4).

Exercises the real ``Database`` against a temporary SQLite file, so the schema,
the atomic replace and the tolerant reader are all covered end to end.
"""

from __future__ import annotations

from datetime import UTC, datetime

from wattmosaic_engine.contracts import (
    ControllerConfiguration,
    ControllerStateMapping,
    DeviceConfiguration,
    DeviceConfigurationSet,
    DeviceType,
    ExpectedPower,
    HaConnectivity,
    LightConfiguration,
    LightType,
    MonitoringLevel,
    PowerBehavior,
)
from wattmosaic_engine.storage.database import Database
from wattmosaic_engine.storage.migrations import LATEST_VERSION, apply_migrations


def _rich_config(device_id: str = "kitchen_lights") -> DeviceConfiguration:
    return DeviceConfiguration(
        device_id=device_id,
        device_type=DeviceType.LIGHT_GROUP,
        connectivity=HaConnectivity.CONNECTED,
        state_entity_ids=["light.kitchen", "light.kitchen_under_cabinet"],
        mode_entity_ids=["sensor.kitchen_scene"],
        controllable_entity_ids=["light.kitchen"],
        monitoring=MonitoringLevel.SHARED,
        monitored_by_ct_ids=["ct_sub_l1"],
        behavior=PowerBehavior.VARIABLE,
        expected=ExpectedPower(
            on_w=45.0,
            idle_w=0.5,
            standby_w=0.3,
            startup_w=48.0,
            tolerance_w=3.0,
            tolerance_fraction=0.15,
        ),
        light=LightConfiguration(
            light_type=LightType.LED,
            bulb_count=5,
            watts_per_bulb_on=9.0,
            standby_w=0.2,
            min_output_fraction=0.05,
            dimming_curve=[[0.0, 0.0], [0.5, 0.6], [1.0, 1.0]],
        ),
        controller=ControllerConfiguration(
            mode_entity_id="climate.kitchen",
            load_entity_ids=["binary_sensor.kitchen_load"],
            states=[
                ControllerStateMapping(label="heat", role="load", power_w=1200.0, confirmed=True),
                ControllerStateMapping(label="heat_cool", role="mode", confirmed=False),
            ],
            on_delay_s=30.0,
            off_delay_s=15.0,
            obeys_control=True,
        ),
        configured_by="user",
        notes=["audited 2026-11-01", "five-bulb fixture"],
    )


def _controller_config(device_id: str = "hvac_fan") -> DeviceConfiguration:
    return DeviceConfiguration(
        device_id=device_id,
        device_type=DeviceType.HVAC_COMBINED,
        connectivity=HaConnectivity.CONNECTED,
        mode_entity_ids=["climate.thermostat"],
        monitoring=MonitoringLevel.HOME_TOTAL_ONLY,
        behavior=PowerBehavior.COMPLEX,
        controller=ControllerConfiguration(
            mode_entity_id="climate.thermostat",
            load_entity_ids=["binary_sensor.fan_running"],
            states=[ControllerStateMapping(label="fan", role="load", power_w=85.0, confirmed=True)],
        ),
        notes=["mode entity is context only"],
    )


# --------------------------------------------------------------------------- #
# Migrations
# --------------------------------------------------------------------------- #


def test_migrations_reach_version_4_and_are_idempotent(tmp_path) -> None:
    db_path = tmp_path / "config.db"
    first = Database(db_path)
    try:
        assert first.schema_version == LATEST_VERSION
        assert first.schema_version >= 4
        tables = {
            row["name"]
            for row in first._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "device_configurations" in tables
    finally:
        first.close()

    second = Database(db_path)
    try:
        assert second.schema_version == LATEST_VERSION
        assert apply_migrations(second._conn) == LATEST_VERSION
    finally:
        second.close()


# --------------------------------------------------------------------------- #
# Round-trip
# --------------------------------------------------------------------------- #


def test_save_load_round_trip_preserves_rich_configuration(tmp_path) -> None:
    db = Database(tmp_path / "config.db")
    try:
        rich = _rich_config()
        controller = _controller_config()
        payload = DeviceConfigurationSet(
            version=7,
            updated_at=datetime(2026, 11, 6, 12, 0, tzinfo=UTC),
            devices=[rich, controller],
        )
        db.save_device_configurations(payload)

        loaded = db.load_device_configurations()
        assert loaded.version == 7
        assert [d.device_id for d in loaded.devices] == ["hvac_fan", "kitchen_lights"]

        by_id = loaded.by_device()
        light = by_id["kitchen_lights"]
        assert light.device_type == DeviceType.LIGHT_GROUP
        assert light.monitoring == MonitoringLevel.SHARED
        assert light.behavior == PowerBehavior.VARIABLE
        assert light.monitored_by_ct_ids == ["ct_sub_l1"]
        assert light.state_entity_ids == ["light.kitchen", "light.kitchen_under_cabinet"]
        assert light.mode_entity_ids == ["sensor.kitchen_scene"]
        assert light.light is not None
        assert light.light.bulb_count == 5
        assert light.light.light_type == LightType.LED
        assert light.light.watts_per_bulb_on == 9.0
        assert light.light.min_output_fraction == 0.05
        assert light.light.dimming_curve == [(0.0, 0.0), (0.5, 0.6), (1.0, 1.0)]
        assert light.expected.on_w == 45.0
        assert light.expected.startup_w == 48.0
        assert light.expected.tolerance_fraction == 0.15
        assert light.controller is not None
        assert light.controller.load_states() == {"heat": 1200.0}
        assert light.controller.on_delay_s == 30.0
        assert light.notes == ["audited 2026-11-01", "five-bulb fixture"]

        fan = by_id["hvac_fan"]
        assert fan.controller is not None
        assert fan.controller.load_states() == {"fan": 85.0}
        assert fan.controller.roles()["fan"] == "load"
    finally:
        db.close()


def test_get_device_configuration_returns_none_for_unknown_device(tmp_path) -> None:
    db = Database(tmp_path / "config.db")
    try:
        db.save_device_configurations(
            DeviceConfigurationSet(version=1, devices=[_rich_config()])
        )
        assert db.get_device_configuration("does_not_exist") is None
        found = db.get_device_configuration("kitchen_lights")
        assert found is not None
        assert found.device_id == "kitchen_lights"
        assert found.light is not None
        assert found.light.bulb_count == 5
    finally:
        db.close()


def test_upsert_replaces_row_instead_of_duplicating(tmp_path) -> None:
    db = Database(tmp_path / "config.db")
    try:
        first = _rich_config()
        db.save_device_configurations(DeviceConfigurationSet(version=1, devices=[first]))
        assert db.load_device_configurations().version == 1

        second = _controller_config("kitchen_lights")
        db.save_device_configurations(
            DeviceConfigurationSet(
                version=2,
                updated_at=datetime(2026, 11, 6, 13, 0, tzinfo=UTC),
                devices=[second],
            )
        )

        rows = db._conn.execute("SELECT COUNT(*) AS n FROM device_configurations").fetchone()
        assert rows["n"] == 1
        loaded = db.load_device_configurations()
        assert loaded.version == 2
        assert len(loaded.devices) == 1
        assert loaded.devices[0].device_type == DeviceType.HVAC_COMBINED
    finally:
        db.close()


def test_corrupt_payload_row_is_skipped_not_raised(tmp_path) -> None:
    db = Database(tmp_path / "config.db")
    try:
        db.save_device_configurations(
            DeviceConfigurationSet(version=4, devices=[_rich_config()])
        )
        db._conn.execute(
            "INSERT INTO device_configurations(device_id, payload_json, updated_at) VALUES(?,?,?)",
            ("broken", "{not json", "2026-11-06T00:00:00+00:00"),
        )
        loaded = db.load_device_configurations()
        assert [d.device_id for d in loaded.devices] == ["kitchen_lights"]
        assert db.get_device_configuration("broken") is None
    finally:
        db.close()


def test_empty_table_returns_empty_set(tmp_path) -> None:
    db = Database(tmp_path / "config.db")
    try:
        loaded = db.load_device_configurations()
        assert isinstance(loaded, DeviceConfigurationSet)
        assert loaded.devices == []
    finally:
        db.close()
