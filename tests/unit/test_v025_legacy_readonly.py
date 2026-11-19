"""Z1 migration v025: legacy tables become read-only ``*_legacy`` archives.

DEC-02 keeps legacy tables for one release as a rollback record rather than
dropping them.  The S13-S19 migrations copied their data into the canonical v3
tables; this module archives the originals with an atomic rename and makes them
immutable with ``RAISE(ABORT)`` triggers.  No data is lost and no table is
dropped.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from wattmosaic_engine.storage import migrations as migrations_module
from wattmosaic_engine.storage.migrations import (
    LATEST_VERSION,
    MIGRATIONS,
    apply_migrations,
    current_version,
)
from wattmosaic_engine.storage.migrations import v025_legacy_readonly as v025


def _build_at(path: Path, upto: int) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    for migration in MIGRATIONS:
        if migration.version > upto:
            break
        for statement in migration.statements:
            conn.execute(statement)
        if migration.apply is not None:
            migration.apply(conn)
    conn.execute(f"PRAGMA user_version = {upto}")
    conn.commit()
    return conn


def test_v025_is_discovered_and_is_the_latest() -> None:
    versions = [m.version for m in MIGRATIONS]
    assert 25 in versions
    assert LATEST_VERSION == migrations_module.LATEST_VERSION >= 25
    assert max(versions) == LATEST_VERSION


def test_legacy_table_names_are_declared() -> None:
    expected = {
        "ct_channels",
        "devices",
        "topology_versions",
        "topology_relations",
        "device_model_versions",
        "device_models",
        "residual_power",
        "device_events",
        "ha_entity_bindings",
    }
    assert set(v025.LEGACY_TABLES) == expected
    assert v025.archived_name("ct_channels") == "ct_channels_legacy"


def test_upgrade_from_schema_11_through_v025_is_lossless(tmp_path: Path) -> None:
    """A schema-11 fixture upgrades to LATEST; v025 does not drop anything."""
    conn = _build_at(tmp_path / "old.db", 11)
    try:
        conn.execute(
            "INSERT INTO engine_state(key, value, updated_at) VALUES(?, ?, ?)",
            ("sentinel", "kept", "2026-01-01T00:00:00+00:00"),
        )
        conn.commit()
        before = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert apply_migrations(conn) == LATEST_VERSION
        assert current_version(conn) == LATEST_VERSION
        after = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert before <= after  # no table dropped by the cutover
        assert conn.execute(
            "SELECT value FROM engine_state WHERE key = 'sentinel'"
        ).fetchone()[0] == "kept"
    finally:
        conn.close()


def test_archive_renames_and_makes_tables_read_only(tmp_path: Path) -> None:
    """The explicit archive step renames the tables and every write aborts."""
    conn = sqlite3.connect(tmp_path / "archive.db")
    try:
        conn.execute("CREATE TABLE ct_channels (ct_id TEXT PRIMARY KEY, entity_id TEXT)")
        conn.execute("INSERT INTO ct_channels VALUES('ct_main', 'sensor.main')")
        conn.commit()

        archived = v025.archive_legacy_tables(conn)
        conn.commit()
        assert "ct_channels" in archived

        # The original name is gone; the archived name holds the row.
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "ct_channels_legacy" in names
        assert "ct_channels" not in names
        assert conn.execute("SELECT ct_id FROM ct_channels_legacy").fetchone()[0] == "ct_main"

        for statement in (
            "INSERT INTO ct_channels_legacy VALUES('ct_new', 'sensor.new')",
            "UPDATE ct_channels_legacy SET entity_id = 'sensor.changed' WHERE ct_id = 'ct_main'",
            "DELETE FROM ct_channels_legacy WHERE ct_id = 'ct_main'",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="read-only"):
                conn.execute(statement)
        # The archived row survived all three rejected writes.
        assert conn.execute("SELECT COUNT(*) FROM ct_channels_legacy").fetchone()[0] == 1
    finally:
        conn.close()


def test_archive_is_idempotent(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "idem.db")
    try:
        conn.execute("CREATE TABLE residual_power (scope_id TEXT, residual_w REAL)")
        conn.commit()
        assert v025.archive_legacy_tables(conn) == ("residual_power",)
        conn.commit()
        assert v025.archive_legacy_tables(conn) == ()
        conn.commit()
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "residual_power_legacy" in names
    finally:
        conn.close()
