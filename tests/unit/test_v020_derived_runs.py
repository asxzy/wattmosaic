"""Migration v020: the versioned derived namespace (L7).

A genuine pre-migration database is built with raw ``sqlite3`` through
migrations 1-19 and stamped ``user_version = 19``.  ``Database.__init__``
migrates immediately, so the pre-migration shape cannot be obtained from a
``Database`` instance.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from wattmosaic_engine.contracts import VersionTriple
from wattmosaic_engine.storage.database import Database
from wattmosaic_engine.storage.migrations import MIGRATIONS, apply_migrations
from wattmosaic_engine.storage.migrations.v020_derived_runs import (
    MIGRATION as V020,
)
from wattmosaic_engine.storage.migrations.v020_derived_runs import (
    TIERS,
)

PRE_MIGRATION_VERSION = 19
VT = VersionTriple(
    topology_version_id="topo-1", mapping_version_id="map-1", model_set_id="mset-1"
)


def _build_pre_migration_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        for migration in MIGRATIONS:
            if migration.version > PRE_MIGRATION_VERSION:
                break
            for statement in migration.statements:
                conn.execute(statement)
            if migration.apply is not None:
                migration.apply(conn)
        conn.execute(f"PRAGMA user_version = {PRE_MIGRATION_VERSION}")
        conn.commit()
    finally:
        conn.close()


def test_module_declares_reserved_version_and_tiers() -> None:
    assert V020.version == 20
    assert V020.name == "derived_runs"
    assert TIERS == {
        "derived_runs": "L2",
        "derived_estimates": "L2",
    }


def test_migration_creates_the_two_tables(tmp_path: Path) -> None:
    path = tmp_path / "pre.db"
    _build_pre_migration_db(path)
    conn = sqlite3.connect(path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "derived_runs" not in tables
    assert "derived_estimates" not in tables
    conn.close()

    conn = sqlite3.connect(path)
    assert apply_migrations(conn) == 20
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"derived_runs", "derived_estimates"} <= tables
    cols = {r[1] for r in conn.execute("PRAGMA table_info(derived_runs)")}
    assert {
        "run_id",
        "triple_json",
        "reason",
        "started_at",
        "completed_at",
        "superseded_by",
        "chunks",
        "fingerprint",
    } <= cols
    ecols = {r[1] for r in conn.execute("PRAGMA table_info(derived_estimates)")}
    assert {
        "run_id",
        "subject_kind",
        "subject_id",
        "point_w",
        "lower_w",
        "upper_w",
        "status",
        "model_version_id",
    } <= ecols
    conn.close()


def test_migration_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "pre.db"
    _build_pre_migration_db(path)
    conn = sqlite3.connect(path)
    assert apply_migrations(conn) == 20
    for statement in V020.statements:
        conn.execute(statement)
    if V020.apply is not None:
        V020.apply(conn)
    assert apply_migrations(conn) == 20
    conn.close()


def test_database_opens_at_v020(tmp_path: Path) -> None:
    path = tmp_path / "pre.db"
    _build_pre_migration_db(path)
    db = Database(path)
    assert db.schema_version == 20
    db.close()
