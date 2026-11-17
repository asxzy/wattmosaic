"""Migration matrix: every supported prior schema version reaches LATEST safely.

``REQ-STORAGE-012``/``REQ-TEST-009`` require that each schema version the engine
still supports can be migrated to the latest one, and that doing so is
idempotent and loses no data.  A single "from v10" test (``test_migrations.py``)
cannot see a regression that breaks only the v5->v6 or v8->v9 hop, so this
matrix drives *every* prior version through the real migration entry points.

The fixture is built on a plain ``sqlite3`` connection, exactly like
``test_migrations.py``: ``Database.__init__`` runs migrations immediately and so
cannot produce a database still sitting at an old ``user_version``.  Each hop
seeds a sentinel row in ``engine_state`` (present since schema v1) and verifies
it survives; before/after table+column inventories verify no schema object is
lost; a second ``apply_migrations`` verifies idempotence at the entry-point level.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from wattmosaic_engine.storage.migrations import (
    LATEST_VERSION,
    MIGRATIONS,
    apply_migrations,
    current_version,
)

#: The versions this matrix migrates *from*: every released prior version below
#: LATEST.  Derived from the migration table so a new schema version is covered
#: automatically instead of being silently skipped.
PRIOR_VERSIONS: tuple[int, ...] = tuple(
    m.version for m in MIGRATIONS if m.version < LATEST_VERSION
)

SENTINEL_KEY = "migration_matrix_sentinel"
SENTINEL_VALUE_TEMPLATE = "data-from-v{version}"
NOW = "2026-01-01T00:00:00+00:00"


def _build_at(path: Path, upto: int) -> sqlite3.Connection:
    """Apply the real migrations up to ``upto``, then stamp ``user_version``.

    Reuses the shipped statements and ``apply`` hooks so the fixture cannot drift
    from the production schema; it stops at ``upto`` because ``Database`` would
    otherwise migrate straight to LATEST.
    """
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


def _seed(conn: sqlite3.Connection, version: int) -> None:
    """Write the sentinel row that must survive every migration."""
    conn.execute(
        "INSERT INTO engine_state(key, value, updated_at) VALUES(?, ?, ?)",
        (SENTINEL_KEY, SENTINEL_VALUE_TEMPLATE.format(version=version), NOW),
    )
    conn.commit()


def _snapshot(conn: sqlite3.Connection) -> dict[str, set[str]]:
    """All tables and their columns, as a comparable inventory."""
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }
    return {table: {row[1] for row in conn.execute(f"PRAGMA table_info({table})")} for table in tables}


def _sentinel(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT value FROM engine_state WHERE key = ?", (SENTINEL_KEY,)
    ).fetchone()
    assert row is not None, "sentinel row disappeared during migration"
    return str(row[0])


def test_matrix_covers_every_prior_version() -> None:
    """Guard the matrix: no supported prior version is silently omitted."""
    assert PRIOR_VERSIONS
    assert tuple(range(1, LATEST_VERSION)) == PRIOR_VERSIONS


@pytest.mark.parametrize("version", PRIOR_VERSIONS)
def test_migration_to_latest_is_lossless_and_idempotent(
    version: int, tmp_path: Path
) -> None:
    """For one prior version: migrate to LATEST, prove no loss, prove no-op repeat."""
    db_path = tmp_path / f"schema_v{version}.db"
    conn = _build_at(db_path, version)
    try:
        assert current_version(conn) == version, "fixture is not at the intended version"
        _seed(conn, version)
        schema_before = _snapshot(conn)
        expected_sentinel = SENTINEL_VALUE_TEMPLATE.format(version=version)

        # First application reaches LATEST and preserves the row + every object.
        assert apply_migrations(conn) == LATEST_VERSION
        assert current_version(conn) == LATEST_VERSION
        assert _sentinel(conn) == expected_sentinel, "sentinel data was lost or rewritten"
        schema_after = _snapshot(conn)
        for table, columns in schema_before.items():
            assert table in schema_after, f"migration dropped table {table}"
            assert columns <= schema_after[table], (
                f"migration dropped columns of {table}: {columns - schema_after[table]}"
            )

        # Second application at LATEST is a true no-op: version, data and schema
        # are all unchanged.
        version_after_first = current_version(conn)
        assert apply_migrations(conn) == LATEST_VERSION
        assert current_version(conn) == version_after_first
        assert _sentinel(conn) == expected_sentinel
        assert _snapshot(conn) == schema_after, "a second migration changed the schema"
    finally:
        conn.close()
