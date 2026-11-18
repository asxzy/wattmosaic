"""Migration v013: canonical entity tables and lossless backfill (S13).

The fixture is a genuine schema-11 database built with a plain ``sqlite3``
connection, using the shipped migration statements so it cannot drift from the
real schema.  ``Database.__init__`` would migrate immediately and so cannot
produce a pre-migration database.

Idempotency is tested by calling the migration's ``apply`` callable twice
*directly*: ``apply_migrations`` skips an already-applied version, so running the
entry point twice cannot detect a double-add bug in the callback.  A failure is
injected mid-apply by monkeypatching a helper the callback calls; the surrounding
``apply_migrations`` wraps it in ``BEGIN IMMEDIATE``/``COMMIT``, so the failure
must leave ``user_version`` unchanged and no new tables.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from wattmosaic_engine.storage import migrations as migrations_module
from wattmosaic_engine.storage.migrations import (
    LATEST_VERSION,
    MIGRATIONS,
    Migration,
    apply_migrations,
    current_version,
)
from wattmosaic_engine.storage.migrations import v013_entities as v013

PRE_MIGRATION_VERSION = 11
NOW = "2026-11-01T00:00:00+00:00"

# The migration under test, resolved from the module so a rename fails loudly.
_V013 = v013.MIGRATION


# --------------------------------------------------------------------------- #
# A schema-11 fixture, built without ``Database``
# --------------------------------------------------------------------------- #


def _build_schema11(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    for migration in MIGRATIONS:
        if migration.version > PRE_MIGRATION_VERSION:
            break
        for statement in migration.statements:
            conn.execute(statement)
        if migration.apply is not None:
            migration.apply(conn)
    conn.execute(f"PRAGMA user_version = {PRE_MIGRATION_VERSION}")
    conn.commit()
    return conn


def _channel(conn: sqlite3.Connection, ct_id: str, *, ct_type: str = "branch",
             phase: str = "l1", polarity: str = "normal", parent: str | None = None,
             multiplier: float = 1.0, panel_id: str = "main_panel",
             enabled: int = 1, metadata: dict[str, Any] | None = None) -> None:
    conn.execute(
        """INSERT INTO ct_channels(ct_id, entity_id, name, ct_type, phase, parent_ct_id,
           polarity, device_id, paired_ct_id, enabled, resolution_w, expected_interval_s,
           reliability, topology_status, metadata_json, panel_id, panel_name, updated_at,
           alias_of_ct_id, measurement_multiplier)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            ct_id, f"sensor.{ct_id}", ct_id, ct_type, phase, parent, polarity,
            None, None, enabled, 0.5, 15.0, 1.0, "candidate",
            json.dumps(metadata or {}), panel_id, "Main", NOW, None, multiplier,
        ),
    )


def _device(conn: sqlite3.Connection, device_id: str, *, metadata: dict[str, Any] | None = None,
            name: str | None = None) -> None:
    conn.execute(
        """INSERT INTO devices(device_id, name, dedicated_ct_ids_json, branch_ct_ids_json,
           phase, double_pole, parent_device_id, topology_status, enabled, metadata_json,
           updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            device_id, name or device_id, "[]", "[]", "l1", 0, None, "candidate", 1,
            json.dumps(metadata or {}), NOW,
        ),
    )


def _config(conn: sqlite3.Connection, device_id: str, payload: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO device_configurations(device_id, payload_json, updated_at) VALUES(?,?,?)",
        (device_id, json.dumps(payload), NOW),
    )


def _factor(conn: sqlite3.Connection, ct_id: str, multiplier: float,
            effective_from: str, effective_to: str | None = None) -> None:
    conn.execute(
        """INSERT INTO ct_measurement_factors(ct_id, multiplier, effective_from, effective_to,
           applied_by, declared_by, note) VALUES(?,?,?,?,?,?,?)""",
        (ct_id, multiplier, effective_from, effective_to, "user", "user", "calibration"),
    )


def _binding(conn: sqlite3.Connection, entity_id: str, device_id: str, role: str) -> None:
    conn.execute(
        """INSERT INTO ha_entity_bindings(entity_id, device_id, role, is_load_evidence,
           contributes_power, attributes_key) VALUES(?,?,?,?,?,?)""",
        (entity_id, device_id, role, 1, 0, None),
    )


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }


def _seed(conn: sqlite3.Connection) -> None:
    """Seed nested CTs with factors, a dev_<ct> auto device, a user device with
    three entity links in three different stores, and a retired-role config."""
    # Nested CTs, with a time-scoped multiplier history on the branch CT.
    _channel(conn, "ct_main", ct_type="main", phase="both", panel_id="main_panel")
    _channel(conn, "ct_branch", parent="ct_main", multiplier=2.0)
    _factor(conn, "ct_branch", 1.5, "2026-06-01T00:00:00+00:00", "2026-07-01T00:00:00+00:00")
    _factor(conn, "ct_branch", 3.0, "2026-07-01T00:00:00+00:00")

    # A discovery-generated CT circuit: id matches dev_<ct> and carries a marker.
    _device(conn, "dev_ct_branch", metadata={"shared_circuit": True})
    # A user device with entity links in three different stores.
    _device(
        conn,
        "dev_user_lamp",
        metadata={
            "state_entity_ids": ["switch.lamp_via_metadata"],
            "link_state_entity_ids": ["switch.lamp_via_link"],
        },
        name="Lamp",
    )
    _config(
        conn,
        "dev_user_lamp",
        {
            "device_type": "light",
            "load_shape": "fixed",
            "ha_device_id": "ha-lamp-device",
            "state_entity_ids": ["switch.lamp_config"],
            "mode_entity_ids": ["select.lamp_mode"],
            "controllable_entity_ids": ["switch.lamp_config"],
            "monitoring": "shared",
        },
    )
    _binding(conn, "switch.lamp_binding", "dev_user_lamp", "load_state")

    # A device whose config carries the retired dedicated role.
    _device(conn, "dev_retired", name="Retired")
    _config(conn, "dev_retired", {"monitoring": "dedicated", "device_type": "unknown"})

    conn.commit()


@pytest.fixture()
def schema11_db(tmp_path: Path) -> Path:
    path = tmp_path / "schema11.db"
    conn = _build_schema11(path)
    try:
        assert current_version(conn) == PRE_MIGRATION_VERSION
        _seed(conn)
    finally:
        conn.close()
    return path


def _migrated(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    assert apply_migrations(conn) == LATEST_VERSION
    return conn


# --------------------------------------------------------------------------- #
# DDL and discovery
# --------------------------------------------------------------------------- #


def test_tiers_are_all_l4() -> None:
    assert v013.TIERS == {
        "measurement_endpoints": "L4",
        "endpoint_config_revisions": "L4",
        "physical_devices": "L4",
        "load_components": "L4",
        "ha_entities": "L4",
        "ha_entity_links": "L4",
    }


def test_all_tables_created(schema11_db: Path) -> None:
    conn = _migrated(schema11_db)
    try:
        tables = _tables(conn)
        assert {
            "measurement_endpoints", "endpoint_config_revisions", "physical_devices",
            "load_components", "ha_entities", "ha_entity_links",
        } <= tables
    finally:
        conn.close()


def test_legacy_tables_are_not_dropped_or_altered(schema11_db: Path) -> None:
    conn = _migrated(schema11_db)
    try:
        tables = _tables(conn)
        assert {"ct_channels", "devices", "device_configurations", "ct_measurement_factors",
                "ha_entity_bindings"} <= tables
        columns = {row[1] for row in conn.execute("PRAGMA table_info(ct_channels)")}
        assert {"ct_id", "measurement_multiplier", "panel_id"} <= columns
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Endpoints and revisions
# --------------------------------------------------------------------------- #


def test_endpoints_ids_preserved_and_scope(schema11_db: Path) -> None:
    conn = _migrated(schema11_db)
    try:
        rows = {r["endpoint_id"]: dict(r) for r in conn.execute("SELECT * FROM measurement_endpoints")}
        assert set(rows) == {"ct_main", "ct_branch"}
        assert rows["ct_main"]["kind"] == "ct"
        assert rows["ct_main"]["scope"] == "panel_feed"
        assert rows["ct_branch"]["scope"] == "circuit"
        assert rows["ct_branch"]["panel_id"] == "main_panel"
        assert rows["ct_branch"]["enabled"] == 1
    finally:
        conn.close()


def test_revision_count_and_ids(schema11_db: Path) -> None:
    conn = _migrated(schema11_db)
    try:
        rows = conn.execute(
            "SELECT endpoint_id, revision, multiplier, valid_from, valid_until "
            "FROM endpoint_config_revisions ORDER BY endpoint_id, revision"
        ).fetchall()
        # main: one baseline revision. branch: baseline + two factor intervals = 4.
        assert len(rows) == 4
        branch = [dict(r) for r in rows if r["endpoint_id"] == "ct_branch"]
        assert [r["revision"] for r in branch] == [1, 2, 3]
        assert [r["multiplier"] for r in branch] == [2.0, 1.5, 3.0]
    finally:
        conn.close()


def test_endpoint_revision_at_returns_right_multiplier_per_interval(schema11_db: Path) -> None:
    from datetime import datetime

    from wattmosaic_engine.storage.database import Database

    _migrated(schema11_db).close()  # ensure the fixture is at schema 13
    db = Database(schema11_db)
    try:
        entities = db.repos.entities

        def mult(at: str) -> float | None:
            rev = entities.endpoint_revision_at(
                "ct_branch", datetime.fromisoformat(at.replace("Z", "+00:00"))
            )
            return rev["multiplier"] if rev else None

        # Before the first factor, the baseline multiplier applies.
        assert mult("2026-05-01T00:00:00+00:00") == 2.0
        # Inside the first factor interval, 1.5; after it, 3.0 (open-ended).
        assert mult("2026-06-15T00:00:00+00:00") == 1.5
        assert mult("2026-08-01T00:00:00+00:00") == 3.0
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Physical devices
# --------------------------------------------------------------------------- #


def test_devices_ids_verbatim_and_counts(schema11_db: Path) -> None:
    conn = _migrated(schema11_db)
    try:
        rows = {r["device_id"]: dict(r) for r in conn.execute("SELECT * FROM physical_devices")}
        # All three legacy devices survive with the same ids.
        assert set(rows) == {"dev_ct_branch", "dev_user_lamp", "dev_retired"}
        assert all(r["origin"] == "migration" for r in rows.values())
        assert rows["dev_user_lamp"]["name"] == "Lamp"
        assert rows["dev_user_lamp"]["device_type"] == "light"
        assert rows["dev_user_lamp"]["load_shape"] == "fixed"
        assert rows["dev_user_lamp"]["ha_device_id"] == "ha-lamp-device"
    finally:
        conn.close()


def test_legacy_ct_device_flag(schema11_db: Path) -> None:
    conn = _migrated(schema11_db)
    try:
        flagged = conn.execute(
            "SELECT metadata_json FROM physical_devices WHERE device_id='dev_ct_branch'"
        ).fetchone()
        assert json.loads(flagged["metadata_json"])["legacy_ct_device"] == 1
        # A user device is not flagged even though its id starts with dev_.
        user = conn.execute(
            "SELECT metadata_json FROM physical_devices WHERE device_id='dev_user_lamp'"
        ).fetchone()
        assert "legacy_ct_device" not in json.loads(user["metadata_json"])
    finally:
        conn.close()


def test_retired_role_flagged_not_rejected(schema11_db: Path) -> None:
    conn = _migrated(schema11_db)
    try:
        row = conn.execute(
            "SELECT metadata_json FROM physical_devices WHERE device_id='dev_retired'"
        ).fetchone()
        assert json.loads(row["metadata_json"])["legacy_retired_role"] == 1
        # Nothing is deleted for carrying a retired role.
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM physical_devices"
        ).fetchone()["n"] == 3
    finally:
        conn.close()


def test_load_components_empty(schema11_db: Path) -> None:
    conn = _migrated(schema11_db)
    try:
        assert conn.execute("SELECT COUNT(*) AS n FROM load_components").fetchone()["n"] == 0
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# HA entities and links
# --------------------------------------------------------------------------- #


def test_links_deduplicated_across_three_stores(schema11_db: Path) -> None:
    conn = _migrated(schema11_db)
    try:
        # switch.lamp_config is in both config state_entity_ids and
        # controllable_entity_ids, so the first store wins its role (load_state).
        rows = conn.execute(
            "SELECT entity_id, role, declared_by FROM ha_entity_links "
            "WHERE entity_id='switch.lamp_config' ORDER BY role"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["declared_by"] == "migration"
        assert rows[0]["role"] == "load_state"

        # The three distinct state stores each produce one link.
        state_entities = {
            r["entity_id"]
            for r in conn.execute(
                "SELECT entity_id FROM ha_entity_links WHERE role='load_state'"
            ).fetchall()
        }
        assert {
            "switch.lamp_via_metadata", "switch.lamp_via_link",
            "switch.lamp_binding", "switch.lamp_config",
        } <= state_entities

        total = conn.execute("SELECT COUNT(*) AS n FROM ha_entity_links").fetchone()["n"]
        # 4 distinct state entities (binding + 2 metadata + 1 config) + 1 mode
        # entity.  The config entity appears in two stores but collapses to one.
        assert total == 5
    finally:
        conn.close()


def test_entities_have_valid_domain_and_seen_window(schema11_db: Path) -> None:
    conn = _migrated(schema11_db)
    try:
        rows = conn.execute("SELECT * FROM ha_entities").fetchall()
        assert rows
        for row in rows:
            assert row["domain"] == row["entity_id"].split(".", 1)[0]
            assert row["first_seen"] <= row["last_seen"]
    finally:
        conn.close()


def test_repository_queries(schema11_db: Path) -> None:
    from wattmosaic_engine.storage.database import Database

    _migrated(schema11_db).close()
    db = Database(schema11_db)
    try:
        entities = db.repos.entities

        links = entities.links_for_entity("switch.lamp_binding")
        assert len(links) == 1
        assert links[0]["target_id"] == "dev_user_lamp"

        subjects = entities.subjects_for_ha_device("ha-lamp-device")
        # All five lamp entities resolve to the lamp subject through the HA
        # device id recorded in its config payload.
        assert {s["entity_id"] for s in subjects} == {
            "switch.lamp_binding", "switch.lamp_via_metadata", "switch.lamp_via_link",
            "switch.lamp_config", "select.lamp_mode",
        }
        assert all(s["target_id"] == "dev_user_lamp" for s in subjects)

        # CRUD round trip through the facade.
        entities.upsert_device(
            {
                "device_id": "pd_new", "name": "New", "origin": "user",
                "created_at": "2026-11-01T00:00:00+00:00", "metadata": {},
            }
        )
        assert entities.get_device("pd_new")["name"] == "New"
        entities.retire_device("pd_new", "2026-11-02T00:00:00+00:00")
        assert entities.get_device("pd_new")["retired_at"] is not None
        entities.delete_device("pd_new")
        assert entities.get_device("pd_new") is None
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Idempotency, failure and backup
# --------------------------------------------------------------------------- #


def test_apply_callback_is_idempotent(schema11_db: Path) -> None:
    conn = sqlite3.connect(schema11_db)
    conn.row_factory = sqlite3.Row
    try:
        # Apply the DDL, then run the backfill callback twice directly.
        for statement in _V013.statements:
            conn.execute(statement)
        assert _V013.apply is not None
        _V013.apply(conn)
        conn.commit()
        first = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in v013.TIERS
        }
        _V013.apply(conn)
        conn.commit()
        second = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in v013.TIERS
        }
        assert second == first
    finally:
        conn.close()


def test_failure_mid_apply_leaves_user_version_unchanged(schema11_db: Path,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    conn = sqlite3.connect(schema11_db)
    conn.row_factory = sqlite3.Row
    try:
        before_version = current_version(conn)
        before_tables = _tables(conn)
        assert before_version == PRE_MIGRATION_VERSION

        def boom(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("injected failure mid-apply")

        # Discovery loads the module under a private name, so the registered
        # migration's ``apply`` closure refers to a different module object than
        # ``v013``.  Patch the globals of the registered callback itself.
        registered = next(m for m in MIGRATIONS if m.version == 13)
        assert registered.apply is not None
        monkeypatch.setitem(registered.apply.__globals__, "_backfill_devices", boom)
        with pytest.raises(RuntimeError, match="injected failure"):
            apply_migrations(conn)

        assert current_version(conn) == before_version
        assert _tables(conn) == before_tables
        assert "measurement_endpoints" not in _tables(conn)
    finally:
        conn.close()


def test_backup_file_exists(schema11_db: Path) -> None:
    from wattmosaic_engine.storage.database import Database

    db = Database(schema11_db)
    try:
        assert db.backup_path is not None
        assert db.backup_path.exists()
        assert db.backup_path.name.startswith(f"{schema11_db.name}.pre-v013.")
        backup = sqlite3.connect(db.backup_path)
        try:
            assert int(backup.execute("PRAGMA user_version").fetchone()[0]) == PRE_MIGRATION_VERSION
        finally:
            backup.close()
    finally:
        db.close()


def test_migration_is_discovered_at_13() -> None:
    versions = [m.version for m in migrations_module.MIGRATIONS]
    assert versions.count(13) == 1
    assert _V013.version == 13
    assert isinstance(_V013, Migration)
