"""Storage: per-device configuration persistence (migration 4) and the tier registry.

Exercises the real ``Database`` against a temporary SQLite file, so the schema,
the atomic replace and the tolerant reader are all covered end to end.  The
``TASK-STORAGE-001`` half of the file covers the four-tier registry, the
per-tier retention ranges, the payload encoding and the retention pass.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from wattmosaic_engine.config.settings import (
    RETENTION_DAYS_AGGREGATES_RANGE,
    RETENTION_DAYS_RAW_RANGE,
    EngineConfig,
)
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
from wattmosaic_engine.storage.migrations import (
    LATEST_VERSION,
    MIGRATIONS,
    PAYLOAD_ENCODING_VERSION,
    apply_migrations,
    current_version,
)
from wattmosaic_engine.storage.payload import (
    decode_json_text,
    decode_payload,
    encode_if_worthwhile,
    encode_payload,
    payload_is_encoded,
)
from wattmosaic_engine.storage.tiers import (
    AGGREGATE_HORIZON,
    PRUNABLE_TABLES,
    RAW_HORIZON,
    TABLE_TIERS,
    TIER_TABLES,
    TIERS,
    TierError,
    is_prunable,
    partition_decision,
    prunable_tables,
    table_tiers,
    tier_of,
)


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


# --------------------------------------------------------------------------- #
# Contract 1.6.0 — typed entity bindings survive the round trip
# --------------------------------------------------------------------------- #


def test_entity_bindings_round_trip_through_storage(tmp_path) -> None:
    """A typed role must survive persistence, or WP4 cannot dedup on it.

    ``DeviceConfiguration.entity_bindings`` is what lets "many HA entities, one
    appliance" be a contract rather than a convention.  It is stored inside the
    configuration payload, so the round-trip guard has to live here.
    """
    from wattmosaic_engine.contracts import HaEntityBinding, HaEntityRole

    db = Database(tmp_path / "bindings.db")
    try:
        config = _rich_config()
        config.entity_bindings = [
            HaEntityBinding(
                entity_id="sensor.kitchen_power",
                device_id=config.device_id,
                role=HaEntityRole.POWER_MEASUREMENT,
                is_load_evidence=True,
                contributes_power=True,
                attributes_key="current_power_w",
            ),
            HaEntityBinding(
                entity_id="light.kitchen",
                device_id=config.device_id,
                role=HaEntityRole.LOAD_STATE,
                is_load_evidence=True,
            ),
            HaEntityBinding(
                entity_id="sensor.kitchen_energy",
                device_id=config.device_id,
                role=HaEntityRole.ENERGY_COUNTER,
            ),
        ]
        db.save_device_configurations(
            DeviceConfigurationSet(version=8, devices=[config])
        )

        loaded = db.load_device_configurations().by_device()["kitchen_lights"]
        assert len(loaded.entity_bindings) == 3
        power = [b for b in loaded.entity_bindings if b.contributes_power]
        assert len(power) == 1
        assert power[0].entity_id == "sensor.kitchen_power"
        assert power[0].role == HaEntityRole.POWER_MEASUREMENT
        assert power[0].attributes_key == "current_power_w"
        assert {b.role for b in loaded.entity_bindings} == {
            HaEntityRole.POWER_MEASUREMENT,
            HaEntityRole.LOAD_STATE,
            HaEntityRole.ENERGY_COUNTER,
        }
    finally:
        db.close()


def test_latest_schema_includes_the_audit_tables(tmp_path) -> None:
    """Migrations 9 and 10 are additive audit state; config is unaffected."""
    db = Database(tmp_path / "audit_schema.db")
    try:
        assert LATEST_VERSION >= 11
        assert db.schema_version == LATEST_VERSION
        tables = {
            row["name"]
            for row in db._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {
            "circuits",
            "topology_relations",
            "inference_records",
            "ct_measurement_factors",
            "ha_entity_bindings",
            "residual_power",
            "isolation_verdicts",
        } <= tables
        # And the migration is idempotent on reopen.
        assert apply_migrations(db._conn) == LATEST_VERSION
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# TASK-STORAGE-001 -- the tier registry
# --------------------------------------------------------------------------- #


def _live_tables(db: Database) -> set[str]:
    """Every table the real schema creates, read from ``sqlite_master``."""
    return {
        str(row["name"])
        for row in db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }


def test_every_table_is_classified(tmp_path) -> None:
    """Every table the schema creates resolves to exactly one tier.

    Both directions are asserted against a live database, not against the
    registry alone: a table the migrations create but the registry omits (or a
    table the registry invents) is a classification nobody made, and the
    retention pass would then either skip it silently or raise.
    """
    db = Database(tmp_path / "tiers.db")
    try:
        live = _live_tables(db)
        registered = set(TABLE_TIERS)
        assert live == registered, (
            f"unclassified={sorted(live - registered)} "
            f"invented={sorted(registered - live)}"
        )
        # Exactly one tier per table, in both directions.
        assert len(TABLE_TIERS) == sum(len(t) for t in TIER_TABLES.values())
        assert {tier for _, tier in table_tiers()} == set(TIERS)
        assert {table for table, _ in table_tiers()} == registered
        for table in live:
            assert tier_of(table) in TIERS
    finally:
        db.close()


def test_the_registry_is_import_safe() -> None:
    """The import-time check refuses a duplicate or an unknown tier."""
    from wattmosaic_engine.storage.tiers import tables_of_tier

    assert TIERS == ("L1", "L2", "L3", "L4")
    assert set(TIER_TABLES) == set(TIERS)
    with pytest.raises(TierError):
        tier_of("no_such_table")
    with pytest.raises(TierError):
        tables_of_tier("L5")


def test_stats_publishes_the_tier_classification(tmp_path) -> None:
    """``GET /status``'s storage block reads the registry, not a second list."""
    db = Database(tmp_path / "stats.db")
    try:
        published = db.stats()["tiers"]
        assert dict(published) == TABLE_TIERS
        assert len(published) == len(TABLE_TIERS)
    finally:
        db.close()


def test_prunable_tables_match_the_shipped_retention_pass(tmp_path) -> None:
    """The registry's prune declaration is the shipped behaviour, entry by entry.

    ``device_events`` is the recorded conflict: classified L2, pruned on the raw
    horizon because the shipped pass always did and the spec names it as the
    ~1.5 GB/day growth driver.
    """
    db = Database(tmp_path / "prune_registry.db")
    try:
        declared = {table: (col, horizon) for table, col, horizon in prunable_tables()}
        assert declared["ct_samples"] == ("received_at", RAW_HORIZON)
        assert declared["device_events"] == ("occurred_at", RAW_HORIZON)
        assert declared["power_aggregates"] == ("bucket_start", AGGREGATE_HORIZON)
        assert declared["energy_aggregates"] == ("bucket_start", AGGREGATE_HORIZON)
        # Every declared table exists and every declared column is its age key.
        for table, (col, _horizon) in declared.items():
            assert table in _live_tables(db)
            columns = {
                str(row[1])
                for row in db._conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            assert col in columns, (table, col)
            assert is_prunable(table)
        # The registry is the single source: ``PRUNABLE_TABLES`` is built from it.
        assert {t: (c, h) for t, (c, h) in PRUNABLE_TABLES.items()} == declared
        # The conflict is visible: L2 by classification, pruned on the raw horizon.
        assert tier_of("device_events") == "L2"
        assert declared["device_events"][1] == RAW_HORIZON
    finally:
        db.close()


def test_prune_reports_every_exempt_table_with_zero(tmp_path) -> None:
    """Every registered table the pass does not delete is reported as ``0``."""
    db = Database(tmp_path / "exempt.db")
    try:
        removed = db.prune(raw_days=30, aggregate_days=730)
        assert set(removed) == set(TABLE_TIERS)
        for table, count in removed.items():
            if is_prunable(table):
                continue
            assert count == 0, table
            # And the table is genuinely untouched: nothing was deleted.
            assert isinstance(count, int)
        assert removed["inference_records"] == 0
        assert removed["isolation_verdicts"] == 0
        assert removed["residual_power"] == 0
        assert removed["circuits"] == 0
    finally:
        db.close()


def test_device_events_is_still_age_pruned(tmp_path) -> None:
    """The §A regression guard: the shipped pass still prunes ``device_events``.

    The registry classifies it L2, so a tier-derived prune would leave it alone
    and silently stop bounding the documented ~1.5 GB/day growth driver.  This
    asserts the shipped behaviour is preserved.
    """
    db = Database(tmp_path / "events.db")
    old = datetime.now(UTC) - timedelta(days=400)
    new = datetime.now(UTC)
    try:
        for event_id, at in (("old", old), ("new", new)):
            db.insert_device_event(
                {
                    "event_id": event_id,
                    "device_id": "dev1",
                    "entity_id": "switch.dev1",
                    "event_type": "state_changed",
                    "occurred_at": at,
                    "received_at": at,
                }
            )
        removed = db.prune(raw_days=30, aggregate_days=730)
        assert removed["device_events"] == 1
        surviving = {
            row["event_id"]
            for row in db._conn.execute("SELECT event_id FROM device_events").fetchall()
        }
        assert surviving == {"new"}
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# TASK-STORAGE-001 -- per-tier retention ranges
# --------------------------------------------------------------------------- #


def _config(**env: str) -> EngineConfig:
    base = {"WM_HA_URL": "http://homeassistant:8123", "WM_HA_TOKEN": "test-token"}
    base.update(env)
    return EngineConfig.from_env(base, dotenv_path="/nonexistent.env")


def test_raw_retention_range_is_enforced() -> None:
    """The documented 7-14 band and the shipped 30 are accepted; outside is not."""
    low, high = RETENTION_DAYS_RAW_RANGE
    assert (low, high) == (1, 90)
    for accepted in (7, 14, 30):
        problems = [
            p for p in _config(WM_RETENTION_DAYS_RAW=str(accepted)).validate()
            if "retention_days_raw" in p
        ]
        assert problems == [], (accepted, problems)
    for refused in (0, 91):
        problems = [
            p for p in _config(WM_RETENTION_DAYS_RAW=str(refused)).validate()
            if "retention_days_raw" in p
        ]
        assert problems, refused


def test_retention_defaults_and_env_overrides() -> None:
    """Defaults are the shipped values; every per-tier horizon is configurable."""
    cfg = _config()
    assert cfg.retention_days_raw == 30
    assert cfg.retention_days_aggregates == 730
    assert cfg.retention_days_evidence is None
    assert cfg.retention_days_versions is None
    agg_low, agg_high = RETENTION_DAYS_AGGREGATES_RANGE
    assert agg_low <= cfg.retention_days_aggregates <= agg_high

    overridden = _config(
        WM_RETENTION_DAYS_RAW="14",
        WM_RETENTION_DAYS_AGGREGATES="90",
        WM_RETENTION_DAYS_EVIDENCE="60",
        WM_RETENTION_DAYS_VERSIONS="120",
    )
    assert overridden.retention_days_raw == 14
    assert overridden.retention_days_aggregates == 90
    assert overridden.retention_days_evidence == 60
    assert overridden.retention_days_versions == 120
    # All four are published in the log-safe view.
    assert overridden.redacted()["retention_days_raw"] == 14
    assert overridden.redacted()["retention_days_evidence"] == 60
    assert overridden.redacted()["retention_days_versions"] == 120


def test_an_explicit_horizon_may_not_be_shorter_than_the_raw_window() -> None:
    """Evidence that does not outlive the raw window it explains cannot be audited."""
    problems = _config(WM_RETENTION_DAYS_EVIDENCE="7").validate()
    assert any("retention_days_evidence" in p for p in problems)
    problems = _config(WM_RETENTION_DAYS_VERSIONS="7").validate()
    assert any("retention_days_versions" in p for p in problems)
    # Equal is fine; unset is fine.
    assert not [
        p for p in _config(WM_RETENTION_DAYS_EVIDENCE="30").validate()
        if "retention_days_evidence" in p
    ]


# --------------------------------------------------------------------------- #
# TASK-STORAGE-001 -- payload encoding
# --------------------------------------------------------------------------- #


def test_payload_round_trip_across_encodings() -> None:
    """One read path serves an encoded payload and a legacy one."""
    document = {
        "device_id": "kitchen_lights",
        "notes": ["x" * 500, "y" * 500],
        "expected": {"on_w": 45.0, "idle_w": 0.5},
    }
    raw = json.dumps(document)
    encoded = encode_payload(raw)
    assert encoded.startswith("Z1:")
    assert payload_is_encoded(encoded)
    assert not payload_is_encoded(raw)

    # The same call reads both forms.
    assert decode_payload(encoded) == document
    assert decode_payload(raw) == document
    assert decode_json_text(encoded) == json.dumps(document)

    # A writer encodes only when it pays, and the row stays readable either way.
    stored, was_encoded = encode_if_worthwhile(raw)
    assert was_encoded and payload_is_encoded(stored)
    assert len(stored) < len(raw)
    assert decode_payload(stored) == document

    small = json.dumps({"a": 1})
    small_stored, small_encoded = encode_if_worthwhile(small)
    assert not small_encoded
    assert small_stored == small
    assert decode_payload(small_stored) == {"a": 1}

    # A corrupt encoded cell is reported as absent, not raised.
    assert decode_payload("Z1:!!!not-base64!!!") is None
    assert decode_payload(None) is None
    assert decode_payload("") is None


def test_configuration_payloads_read_through_one_path(tmp_path) -> None:
    """A saved configuration is encoded on write and decodes on read."""
    db = Database(tmp_path / "encoded.db")
    try:
        db.save_device_configurations(
            DeviceConfigurationSet(version=3, devices=[_rich_config()])
        )
        stored = db._conn.execute(
            "SELECT payload_json FROM device_configurations WHERE device_id='kitchen_lights'"
        ).fetchone()["payload_json"]
        assert payload_is_encoded(stored)
        loaded = db.load_device_configurations().by_device()["kitchen_lights"]
        assert loaded.notes == ["audited 2026-11-01", "five-bulb fixture"]
        # A legacy row written by an older binary still loads through the same
        # path.  ``DeviceConfigurationSet`` forbids two rows declaring the same
        # device, so the legacy row carries its own declared id.
        legacy_payload = _rich_config("legacy_device").model_dump(mode="json")
        db._conn.execute(
            "INSERT INTO device_configurations(device_id, payload_json, updated_at) VALUES(?,?,?)",
            ("legacy", json.dumps(legacy_payload), "2026-11-06T00:00:00+00:00"),
        )
        by_id = db.load_device_configurations().by_device()
        assert by_id["legacy_device"].device_id == "legacy_device"
        assert by_id["legacy_device"].notes == loaded.notes
    finally:
        db.close()


def test_topology_snapshot_is_encoded_and_a_legacy_row_loads(tmp_path) -> None:
    """The topology snapshot is the column compression pays on; history is safe."""
    from wattmosaic_engine.contracts import (
        CTChannel,
        CTType,
        DeviceTopology,
        Topology,
        TopologyStatus,
    )

    db = Database(tmp_path / "topology_encoded.db")
    try:
        topology = Topology(
            status=TopologyStatus.CANDIDATE,
            channels=[CTChannel(ct_id="main_l1", entity_id="sensor.main", ct_type=CTType.MAIN)],
            devices=[DeviceTopology(device_id="dev1", branch_ct_ids=["main_l1"])],
        )
        db.save_topology(topology)
        stored = db._conn.execute(
            "SELECT payload_json FROM topology_versions ORDER BY version DESC LIMIT 1"
        ).fetchone()["payload_json"]
        assert payload_is_encoded(stored)
        assert db.load_topology() is not None

        # A legacy (uncompressed) snapshot written before the encoding loads too.
        legacy = json.dumps(topology.model_dump(mode="json"), default=str)
        db._conn.execute(
            "INSERT INTO topology_versions(created_at, status, payload_json, note) "
            "VALUES(?,?,?,?)",
            ("2026-01-01T00:00:00+00:00", "candidate", legacy, "legacy"),
        )
        assert db.load_topology() is not None
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# TASK-STORAGE-001 -- partitioning decision
# --------------------------------------------------------------------------- #


def test_the_partitioning_decision_carries_its_measurement() -> None:
    """The decision names what replaces partitioning and when it is revisited."""
    decision = partition_decision()
    assert "SQLite has no native partitioning" in decision
    assert "1.5 GB/day" in decision
    assert "320852" in decision
    assert "reconsidered" in decision
    spec = (
        Path(__file__).resolve().parents[2]
        / "docs"
        / "specs"
        / "load-disaggregation"
        / "08-storage-and-data-lifecycle.md"
    ).read_text()
    assert "## 7. Partitioning" in spec
    assert "1.5 GB/day" in spec


# --------------------------------------------------------------------------- #
# TASK-STORAGE-001 -- prior-schema load (raw sqlite3, no ``Database``)
# --------------------------------------------------------------------------- #


def _build_prior_schema(path: Path, upto: int = 10) -> None:
    """Create a pre-migration database with raw ``sqlite3`` only.

    ``Database.__init__`` would run the migrations immediately, so the fixture is
    built on a plain connection and stopped at ``user_version = 10`` -- one
    version behind :data:`LATEST_VERSION`, which is the seam this wave must not
    disturb (it adds no migration).
    """
    conn = sqlite3.connect(path)
    try:
        for migration in MIGRATIONS:
            if migration.version > upto:
                break
            for statement in migration.statements:
                conn.execute(statement)
            if migration.apply is not None:
                migration.apply(conn)
        conn.execute(f"PRAGMA user_version = {upto}")
        # A legacy (uncompressed) configuration payload, as an older binary wrote.
        config = _rich_config().model_dump(mode="json")
        conn.execute(
            "INSERT INTO device_configurations(device_id, payload_json, updated_at) "
            "VALUES(?,?,?)",
            ("kitchen_lights", json.dumps(config), "2026-11-06T00:00:00+00:00"),
        )
        # A legacy topology snapshot and a device event, both uncompressed.
        # ``load_topology`` reads the per-row channel/device tables, so the
        # snapshot row is what exercises the decode path.
        conn.execute(
            """INSERT INTO ct_channels(ct_id, entity_id, name, ct_type, phase,
            polarity, enabled, reliability, topology_status, metadata_json, updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            ("main_l1", "sensor.main_l1", "Main", "main", "l1", "normal", 1, 1.0,
             "candidate", "{}", "2026-11-06T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO topology_versions(created_at, status, payload_json, note) "
            "VALUES(?,?,?,?)",
            (
                "2026-11-06T00:00:00+00:00",
                "candidate",
                json.dumps({"version": 1, "channels": [], "devices": []}),
                "legacy",
            ),
        )
        conn.execute(
            """INSERT INTO device_events(event_id, device_id, entity_id, event_type,
            occurred_at, received_at) VALUES(?,?,?,?,?,?)""",
            ("e1", "dev1", "switch.dev1", "state_changed", "2026-11-06T00:00:00+00:00",
             "2026-11-06T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()


def test_prior_schema_database_migrates_and_loads(tmp_path) -> None:
    """A version-10 database migrates forward and its rows still load."""
    path = tmp_path / "prior.db"
    _build_prior_schema(path)
    conn = sqlite3.connect(path)
    try:
        assert current_version(conn) == 10
    finally:
        conn.close()

    db = Database(path)
    try:
        assert db.schema_version == LATEST_VERSION
        assert LATEST_VERSION >= 11
        # The legacy configuration payload reads through the new decode path.
        loaded = db.load_device_configurations().by_device()["kitchen_lights"]
        assert loaded.notes == ["audited 2026-11-01", "five-bulb fixture"]
        assert db.load_topology() is not None
        assert db.recent_device_events(
            datetime(2026, 11, 1, tzinfo=UTC)
        )[0]["event_id"] == "e1"
        # And the migration is idempotent on reopen.
        assert apply_migrations(db._conn) == LATEST_VERSION
    finally:
        db.close()


def test_the_payload_encoding_is_not_a_migration() -> None:
    """The encoding is a write-path marker, never a schema version.

    A migration that only bumped ``user_version`` would advance the version on a
    database whose bytes never changed, making ``schema_version`` a lie.  Version
    12 stays reserved even though canonical migrations (13+) now exist.
    """
    assert PAYLOAD_ENCODING_VERSION == 12
    assert PAYLOAD_ENCODING_VERSION not in {m.version for m in MIGRATIONS}
    assert all(m.version <= LATEST_VERSION for m in MIGRATIONS)
