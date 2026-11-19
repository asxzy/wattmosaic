"""Schema-7 reconciliation split columns: the migration that added them.

Release ``0.2.1-dev.12`` lost ``unmonitored_w`` / ``meter_disagreement_w`` in
storage because ``reconciliation_results`` had no columns for them; migration 7
added them (plus ``meter_spread_w`` / ``meter_spread_exceeds_residual`` from
contract 1.5.0) in place.

The legacy ``Reconciliation`` contract and its ``insert_reconciliation`` /
``latest_reconciliation`` / ``row_to_reconciliation`` round-trip were removed in
the contract-3.0.0 cutover (the v3 reconciliation travels inside the
``InferenceSnapshot``), so this file now pins only the migration itself: a real
schema-6 database upgrades additively, the columns are created exactly once,
the migration body is idempotent, and the table still accepts the split.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from wattmosaic_engine.storage.database import Database
from wattmosaic_engine.storage.migrations import (
    LATEST_VERSION,
    MIGRATIONS,
    apply_migrations,
    current_version,
)

TS = datetime(2026, 11, 11, 19, 35, 32, tzinfo=UTC)

#: The live dev.12 shape: a ~672 W residual of which ~354.8 W is two meters
#: disagreeing on the same conductors and ~317.2 W is genuinely unmonitored.
LIVE_RESIDUAL_W = 672.0
LIVE_METER_DISAGREEMENT_W = 354.8
LIVE_UNMONITORED_W = 317.2


def _columns(conn: sqlite3.Connection, table: str = "reconciliation_results") -> list[str]:
    """Column names of ``table``, duplicates included (a duplicate would be a bug)."""
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def _insert_split(conn: sqlite3.Connection) -> None:
    """Write one row carrying the split columns migration 7 added."""
    conn.execute(
        """INSERT INTO reconciliation_results(timestamp, topology_version, main_power_w,
        direct_measured_w, inferred_w, model_estimated_w, unknown_residual_w,
        reconciliation_error_w, reconciliation_error_ratio, accounted_fraction,
        quality_status, unmonitored_w, meter_disagreement_w, meter_spread_w,
        meter_spread_exceeds_residual)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            TS.isoformat(), 2, 2_000.0, 1_328.0, 0.0, 0.0, LIVE_RESIDUAL_W, 0.0, 0.0,
            0.664, "good", LIVE_UNMONITORED_W, LIVE_METER_DISAGREEMENT_W,
            LIVE_METER_DISAGREEMENT_W, 0,
        ),
    )


def _latest_split(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        "SELECT * FROM reconciliation_results ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    return dict(row)


def _build_schema_6(path: Path) -> None:
    """Create a database exactly as release 0.2.1-dev.12 left it (schema 6)."""
    conn = sqlite3.connect(str(path), isolation_level=None)
    try:
        for migration in MIGRATIONS:
            if migration.version > 6:
                break
            for stmt in migration.statements:
                conn.execute(stmt)
            # ``apply`` is the schema-7 hook; older migration objects do not
            # have it, so the helper must tolerate its absence.
            hook = getattr(migration, "apply", None)
            if hook is not None:
                hook(conn)
        conn.execute("PRAGMA user_version = 6")
    finally:
        conn.close()


def test_migration_7_adds_the_split_columns_in_place(tmp_path: Path) -> None:
    """A real schema-6 DB upgrades additively; existing rows are untouched."""
    path = tmp_path / "prod_like.db"
    _build_schema_6(path)

    # A reconciliation row written by the old code: no split columns existed.
    conn = sqlite3.connect(str(path), isolation_level=None)
    try:
        assert current_version(conn) == 6
        assert "unmonitored_w" not in _columns(conn)
        conn.execute(
            """INSERT INTO reconciliation_results(timestamp, topology_version, main_power_w,
            l1_power_w, l2_power_w, direct_measured_w, inferred_w, model_estimated_w,
            unknown_residual_w, reconciliation_error_w, reconciliation_error_ratio,
            accounted_fraction, quality_status, explanation_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                TS.isoformat(), 1, 2_000.0, None, None, 1_400.0, 0.0, 0.0,
                600.0, 0.0, 0.0, 0.7, "good", "[]",
            ),
        )
        old_id = conn.execute("SELECT id FROM reconciliation_results").fetchone()[0]
    finally:
        conn.close()

    # Opening with the current code applies migration 7.
    db = Database(path)
    try:
        assert db.schema_version == LATEST_VERSION
        assert LATEST_VERSION >= 7  # this test only needs 7 to have run
        cols = _columns(db._conn)
        assert {
            "unmonitored_w",
            "meter_disagreement_w",
            "meter_spread_w",
            "meter_spread_exceeds_residual",
            "configured_prior_w",
            "measurement_uncertainty_w",
            "topology_confirmed",
        } <= set(cols)

        # The old row survived and reads its split as NULL (absent, not zero).
        old = db._conn.execute(
            "SELECT * FROM reconciliation_results WHERE id=?", (old_id,)
        ).fetchone()
        assert dict(old)["unknown_residual_w"] == pytest.approx(600.0)
        assert dict(old)["unmonitored_w"] is None
        assert dict(old)["topology_confirmed"] is None

        # And a new row stores a real split and its spread.
        _insert_split(db._conn)
        rec = _latest_split(db._conn)
        assert rec["unmonitored_w"] == pytest.approx(LIVE_UNMONITORED_W)
        assert rec["meter_disagreement_w"] == pytest.approx(LIVE_METER_DISAGREEMENT_W)
        assert rec["meter_spread_w"] == pytest.approx(LIVE_METER_DISAGREEMENT_W)
    finally:
        db.close()


def test_migrations_are_idempotent_across_reopens(tmp_path: Path) -> None:
    """Running the migration set twice must not fail on the columns it added."""
    path = tmp_path / "twice.db"
    first = Database(path)
    try:
        assert first.schema_version == LATEST_VERSION
    finally:
        first.close()

    second = Database(path)
    try:
        # apply_migrations is a no-op at the current version ...
        assert apply_migrations(second._conn) == LATEST_VERSION
        # ... and re-running the version-7 body explicitly is still safe.
        for migration in MIGRATIONS:
            if migration.version == 7:
                hook = getattr(migration, "apply", None)
                assert hook is not None
                hook(second._conn)
        cols = _columns(second._conn)
        assert "unmonitored_w" in cols
        assert "meter_disagreement_w" in cols
        # The table still works after the redundant run.
        _insert_split(second._conn)
        assert _latest_split(second._conn)["meter_disagreement_w"] == pytest.approx(
            LIVE_METER_DISAGREEMENT_W
        )
    finally:
        second.close()


def test_migration_7_noops_when_create_table_already_has_the_columns(tmp_path: Path) -> None:
    """A fresh DB whose ``CREATE TABLE`` already names the columns must not fail.

    This is the interrupted-migration / hand-built-DB case: the column exists
    but ``user_version`` is still 6.  ``ALTER TABLE ADD COLUMN`` would raise
    ``duplicate column name`` without the ``PRAGMA table_info`` guard.
    """
    path = tmp_path / "prepopulated.db"
    _build_schema_6(path)

    conn = sqlite3.connect(str(path), isolation_level=None)
    try:
        conn.execute("ALTER TABLE reconciliation_results ADD COLUMN unmonitored_w REAL")
        conn.execute(
            "ALTER TABLE reconciliation_results ADD COLUMN meter_disagreement_w REAL"
        )
        conn.execute("ALTER TABLE reconciliation_results ADD COLUMN meter_spread_w REAL")
        conn.execute(
            "ALTER TABLE reconciliation_results ADD COLUMN meter_spread_exceeds_residual INTEGER"
        )
        assert current_version(conn) == 6
    finally:
        conn.close()

    db = Database(path)
    try:
        assert db.schema_version == LATEST_VERSION
        cols = _columns(db._conn)
        assert cols.count("unmonitored_w") == 1
        assert cols.count("meter_disagreement_w") == 1
        assert cols.count("meter_spread_w") == 1
        assert cols.count("meter_spread_exceeds_residual") == 1
        _insert_split(db._conn)
        assert _latest_split(db._conn)["unmonitored_w"] == pytest.approx(LIVE_UNMONITORED_W)
    finally:
        db.close()


def test_latest_schema_version_covers_every_migration() -> None:
    """Migration numbers are unique and ordered; 1-11 are contiguous.

    This is the guard for the defect that nearly shipped here: a branch added
    migration 7 while the main line had already shipped its own 7.  The two
    collided, ``apply_migrations`` skipped both, and the panel columns were
    never added -- so every topology write failed on a healthy-looking install.
    A duplicate number fails here instead.  Above 11, parallel wave-2 tasks may
    land with gaps (21-24 are spare), so a strict 1..N rule no longer applies.
    """
    versions = [m.version for m in MIGRATIONS]
    assert len(versions) == len(set(versions)), f"duplicate migration version: {versions}"
    assert versions == sorted(versions)
    assert versions[:11] == list(range(1, 12))
    assert max(versions) == LATEST_VERSION
    assert all(13 <= version <= 25 for version in versions if version > 11)
    assert 12 not in versions
    assert LATEST_VERSION >= 8  # the panel identity columns
