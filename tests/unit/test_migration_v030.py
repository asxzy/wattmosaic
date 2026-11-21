"""Focused tests for migration 30 (companion entity columns, ADR-0044).

The migration adds three nullable columns to ``endpoint_config_revisions``
idempotently.  The repository must round-trip them on a fresh revision and read
them back through ``endpoint_revision_at`` and the contract builder.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from wattmosaic_engine.devices.service import _revision_from_row
from wattmosaic_engine.storage.migrations import LATEST_VERSION, apply_migrations
from wattmosaic_engine.storage.migrations.v030_companion_entities import (
    COMPANION_COLUMNS,
    MIGRATION,
    TIERS,
)

_NOW = datetime(2026, 1, 1, tzinfo=UTC)


def _db(path: Path) -> sqlite3.Connection:
    """Build a plain connection at schema v29 (before v030 adds columns)."""
    from wattmosaic_engine.storage.migrations import MIGRATIONS

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    for migration in MIGRATIONS:
        if migration.version > 29:
            break
        for statement in migration.statements:
            conn.execute(statement)
        if migration.apply is not None:
            migration.apply(conn)
    conn.execute("PRAGMA user_version = 29")
    conn.commit()
    return conn


def test_v030_declares_version_and_columns() -> None:
    assert MIGRATION.version == 30
    assert MIGRATION.name == "v030_companion_entities"
    assert TIERS == {}
    assert COMPANION_COLUMNS == (
        "voltage_entity_id",
        "current_entity_id",
        "power_factor_entity_id",
    )
    assert LATEST_VERSION >= 30


def test_migration_adds_columns_idempotently(tmp_path: Path) -> None:
    conn = _db(tmp_path / "v030.db")
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(endpoint_config_revisions)")}
        for name in COMPANION_COLUMNS:
            assert name not in cols

        assert apply_migrations(conn) == LATEST_VERSION
        cols = {r[1] for r in conn.execute("PRAGMA table_info(endpoint_config_revisions)")}
        assert set(COMPANION_COLUMNS) <= cols

        # A second application is a no-op (ALTER TABLE is idempotent here).
        assert apply_migrations(conn) == LATEST_VERSION
        cols2 = {r[1] for r in conn.execute("PRAGMA table_info(endpoint_config_revisions)")}
        assert cols2 == cols
    finally:
        conn.close()


def test_repository_round_trip_of_companion_ids(tmp_path: Path) -> None:
    from wattmosaic_engine.contracts import EndpointConfigRevision
    from wattmosaic_engine.storage.database import Database

    db = Database(tmp_path / "roundtrip.db")
    db.repos.entities.upsert_endpoint(
        {
            "endpoint_id": "r1",
            "kind": "ct",
            "scope": "circuit",
            "scope_status": "candidate",
            "entity_id": "sensor.r1_power",
            "enabled": True,
            "created_at": _NOW,
        }
    )
    db.repos.entities.append_endpoint_revision(
        EndpointConfigRevision(
            endpoint_id="r1",
            revision=1,
            valid_from=_NOW,
            phase="unknown",
            polarity="normal",
            multiplier=1.0,
            calibration_version="test@1",
            measurement_type="instantaneous",
            voltage_entity_id="sensor.r1_v",
            current_entity_id="sensor.r1_i",
            power_factor_entity_id="sensor.r1_pf",
            declared_by="user",
        )
    )
    row = db.repos.entities.endpoint_revision_at("r1", _NOW)
    assert row is not None
    assert row["voltage_entity_id"] == "sensor.r1_v"
    assert row["current_entity_id"] == "sensor.r1_i"
    assert row["power_factor_entity_id"] == "sensor.r1_pf"

    revision = _revision_from_row(row)
    assert revision.voltage_entity_id == "sensor.r1_v"
    assert revision.current_entity_id == "sensor.r1_i"
    assert revision.power_factor_entity_id == "sensor.r1_pf"
