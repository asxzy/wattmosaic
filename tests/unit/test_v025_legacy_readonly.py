"""Z1 migration v025: legacy tables become read-only ``*_legacy`` archives.

DEC-02 keeps legacy tables for one release as a rollback record rather than
dropping them.  The S13-S19 migrations copied their data into the canonical v3
tables; this module archives the originals with an atomic rename and makes them
immutable with ``RAISE(ABORT)`` triggers.  No data is lost and no table is
dropped.

The upgrade fixtures are genuine old-schema databases built on a plain
``sqlite3`` connection from the shipped migration statements (``Database``
would migrate straight to LATEST), with one row per legacy table seeded by raw
SQL -- the legacy contract models and write helpers no longer exist.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from wattmosaic_engine.storage import migrations as migrations_module
from wattmosaic_engine.storage.database import Database
from wattmosaic_engine.storage.migrations import (
    LATEST_VERSION,
    MIGRATIONS,
    apply_migrations,
    current_version,
)
from wattmosaic_engine.storage.migrations import v025_legacy_readonly as v025

NOW = "2026-01-01T00:00:00+00:00"

#: One raw-SQL row per legacy table, valid against the released schemas 9-11.
LEGACY_SEED: dict[str, tuple[str, tuple[object, ...]]] = {
    "ct_channels": (
        "INSERT INTO ct_channels(ct_id, entity_id, ct_type, updated_at) "
        "VALUES('ct_main', 'sensor.ct_main', 'main', ?)",
        (NOW,),
    ),
    "devices": (
        "INSERT INTO devices(device_id, name, updated_at) VALUES('dev_a', 'Dev A', ?)",
        (NOW,),
    ),
    "topology_versions": (
        "INSERT INTO topology_versions(created_at, status, payload_json) "
        "VALUES(?, 'candidate', '{}')",
        (NOW,),
    ),
    "topology_relations": (
        "INSERT INTO topology_relations(relation_id, kind, subject_id, object_id, created_at) "
        "VALUES('rel_1', 'feeds', 'ct_main', 'dev_a', ?)",
        (NOW,),
    ),
    "device_model_versions": (
        "INSERT INTO device_model_versions(device_id, model_type, version, status, created_at) "
        "VALUES('dev_a', 'constant', 1, 'active', ?)",
        (NOW,),
    ),
    "device_models": (
        "INSERT INTO device_models(model_id, device_id, model_type, active_version, "
        "created_at, updated_at) VALUES('m1', 'dev_a', 'constant', 1, ?, ?)",
        (NOW, NOW),
    ),
    "learning_samples": (
        "INSERT INTO learning_samples(sample_id, device_id, captured_at, created_at) "
        "VALUES('s1', 'dev_a', ?, ?)",
        (NOW, NOW),
    ),
    "residual_power": (
        "INSERT INTO residual_power(timestamp, residual_w) VALUES(?, 12.5)",
        (NOW,),
    ),
    "device_events": (
        "INSERT INTO device_events(event_id, device_id, entity_id, event_type, "
        "occurred_at, received_at) VALUES('e1', 'dev_a', 'switch.dev_a', 'state_changed', ?, ?)",
        (NOW, NOW),
    ),
    "ha_entity_bindings": (
        "INSERT INTO ha_entity_bindings(entity_id, device_id) VALUES('switch.dev_a', 'dev_a')",
        (),
    ),
}


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


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def _triggers(conn: sqlite3.Connection) -> dict[str, tuple[str, str]]:
    """``trigger name -> (table, sql)`` for every trigger in the schema."""
    return {
        row[0]: (row[1], row[2])
        for row in conn.execute("SELECT name, tbl_name, sql FROM sqlite_master WHERE type='trigger'")
    }


def _rows(conn: sqlite3.Connection, table: str) -> list[tuple[object, ...]]:
    return [tuple(row) for row in conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid')]


def _assert_archived_read_only(conn: sqlite3.Connection) -> None:
    """Every legacy table is ``*_legacy`` only, guarded by three abort triggers."""
    tables = _tables(conn)
    triggers = _triggers(conn)
    for table in v025.LEGACY_TABLES:
        target = v025.archived_name(table)
        assert table not in tables, f"{table} was not archived"
        assert target in tables, f"{target} is missing"
        for operation in ("insert", "update", "delete"):
            name = f"wm_legacy_readonly_{target}_{operation}"
            assert name in triggers, f"missing {operation} trigger on {target}"
            on_table, sql = triggers[name]
            assert on_table == target
            assert f"BEFORE {operation.upper()}" in sql
            assert "RAISE(ABORT" in sql


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
        "learning_samples",
        "residual_power",
        "device_events",
        "ha_entity_bindings",
    }
    assert set(v025.LEGACY_TABLES) == expected
    assert set(LEGACY_SEED) == expected
    assert v025.archived_name("ct_channels") == "ct_channels_legacy"
    # The archived tables keep the tier their data always had.
    assert {
        f"{table}_legacy": tier for table, tier in v025.LEGACY_TABLE_TIERS.items()
    } == v025.TIERS
    assert set(v025.LEGACY_TABLE_TIERS) == expected
    assert v025.TIERS["device_events_legacy"] == "L2"


def test_fresh_database_opens_with_archived_read_only_legacy_tables(tmp_path: Path) -> None:
    """Opening a brand-new ``Database`` reaches LATEST with only ``*_legacy`` names."""
    db = Database(tmp_path / "fresh.db")
    try:
        assert db.schema_version == LATEST_VERSION
        _assert_archived_read_only(db._conn)
        for table in v025.LEGACY_TABLES:
            count = db._conn.execute(f'SELECT COUNT(*) FROM "{table}_legacy"').fetchone()[0]
            assert count == 0
    finally:
        db.close()


@pytest.mark.parametrize("version", (9, 10, 11))
def test_upgrade_to_latest_archives_every_legacy_table_losslessly(
    version: int, tmp_path: Path
) -> None:
    """A released-schema fixture upgrades to LATEST; v025 renames, never drops."""
    conn = _build_at(tmp_path / f"v{version}.db", version)
    try:
        conn.execute(
            "INSERT INTO engine_state(key, value, updated_at) VALUES(?, ?, ?)",
            ("sentinel", "kept", NOW),
        )
        for sql, params in LEGACY_SEED.values():
            conn.execute(sql, params)
        conn.commit()
        before_tables = _tables(conn)
        before_rows = {table: _rows(conn, table) for table in v025.LEGACY_TABLES}
        assert all(len(rows) == 1 for rows in before_rows.values())

        assert apply_migrations(conn) == LATEST_VERSION
        assert current_version(conn) == LATEST_VERSION
        _assert_archived_read_only(conn)

        after_tables = _tables(conn)
        renamed = {
            table: v025.archived_name(table) for table in v025.LEGACY_TABLES
        }
        # No table dropped: every pre-upgrade table survives, the legacy ones
        # under their archived name.
        for table in before_tables:
            assert renamed.get(table, table) in after_tables, f"{table} was dropped"
        # Every archived row is still there (later migrations may add columns,
        # e.g. v019's ``legacy_scope``, so compare the original column prefix).
        for table, rows in before_rows.items():
            archived = _rows(conn, renamed[table])
            assert [row[: len(rows[0])] for row in archived] == rows
        assert conn.execute(
            "SELECT value FROM engine_state WHERE key = 'sentinel'"
        ).fetchone()[0] == "kept"

        # Every write to every archive aborts and leaves the row in place.
        for table in v025.LEGACY_TABLES:
            target = renamed[table]
            insert_sql, insert_params = LEGACY_SEED[table]
            writes: tuple[tuple[str, tuple[object, ...]], ...] = (
                (insert_sql.replace(f"INSERT INTO {table}(", f"INSERT INTO {target}(", 1),
                 insert_params),
                (f'UPDATE "{target}" SET rowid = rowid', ()),
                (f'DELETE FROM "{target}"', ()),
            )
            for statement, params in writes:
                with pytest.raises(sqlite3.IntegrityError, match="read-only"):
                    conn.execute(statement, params)
            assert conn.execute(f'SELECT COUNT(*) FROM "{target}"').fetchone()[0] == 1
    finally:
        conn.close()


def test_second_apply_at_latest_is_a_no_op(tmp_path: Path) -> None:
    """Re-running v025 (directly or via the entry point) changes nothing."""
    conn = _build_at(tmp_path / "again.db", 11)
    try:
        for sql, params in LEGACY_SEED.values():
            conn.execute(sql, params)
        conn.commit()
        assert apply_migrations(conn) == LATEST_VERSION
        tables = _tables(conn)
        triggers = _triggers(conn)
        rows = {table: _rows(conn, table) for table in tables}

        assert v025.archive_legacy_tables(conn) == ()
        assert v025.MIGRATION.apply is not None
        v025.MIGRATION.apply(conn)
        conn.commit()
        assert apply_migrations(conn) == LATEST_VERSION

        assert current_version(conn) == LATEST_VERSION
        assert _tables(conn) == tables
        assert _triggers(conn) == triggers
        assert {table: _rows(conn, table) for table in tables} == rows
    finally:
        conn.close()


def test_reopening_a_latest_database_keeps_the_archive(tmp_path: Path) -> None:
    path = tmp_path / "reopen.db"
    Database(path).close()
    db = Database(path)
    try:
        assert db.schema_version == LATEST_VERSION
        _assert_archived_read_only(db._conn)
    finally:
        db.close()


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
