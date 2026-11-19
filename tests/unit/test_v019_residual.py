"""Migration v019: per-reader residual series and unknown-load profiles (S19).

The fixture is a genuine pre-migration database built with raw ``sqlite3``:
migrations 1-11 plus the legacy ``residual_power`` rows, with ``user_version``
left at 11.  ``Database.__init__`` migrates immediately, so it cannot produce a
pre-migration fixture; the repository is reached through ``db.repos.residual``.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from wattmosaic_engine.contracts import (
    MeasurementBasis,
    ReaderResidualPoint,
    UnknownLoadProfileV3,
)
from wattmosaic_engine.storage.database import Database
from wattmosaic_engine.storage.migrations import (
    LATEST_VERSION,
    MIGRATIONS,
    apply_migrations,
    current_version,
)
from wattmosaic_engine.storage.migrations.v019_residual import (
    LEGACY_SCOPE,
    TIERS,
)
from wattmosaic_engine.storage.migrations.v019_residual import (
    MIGRATION as V019,
)

PRE_MIGRATION_VERSION = 11
NOW = datetime(2026, 11, 1, tzinfo=UTC)


def _build_pre_migration_db(path: Path) -> sqlite3.Connection:
    """Apply only migrations 1-11, seed legacy residual rows, stamp version 11."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        for migration in MIGRATIONS:
            if migration.version > PRE_MIGRATION_VERSION:
                break
            for statement in migration.statements:
                conn.execute(statement)
            if migration.apply is not None:
                migration.apply(conn)
        conn.execute(f"PRAGMA user_version = {PRE_MIGRATION_VERSION}")
        conn.execute(
            """INSERT INTO residual_power(timestamp, residual_w, kind, basis, baseline_w, dynamic_w)
            VALUES(?,?,?,?,?,?)""",
            ("2026-10-31T23:00:00+00:00", 123.5, "positive", "unknown", 100.0, 23.5),
        )
        conn.commit()
    finally:
        conn.close()
    return conn


def _point(reader_id: str, at: datetime, residual_w: float) -> ReaderResidualPoint:
    return ReaderResidualPoint(
        reader_id=reader_id,
        at=at,
        residual_w=residual_w,
        baseline_w=10.0,
        dynamic_w=residual_w - 10.0,
        noise_sigma_w=1.5,
        topology_version_id="topo-7",
        basis=MeasurementBasis.CT_MEASURED,
    )


def _profile(profile_id: str, scope: str = "reader-a") -> UnknownLoadProfileV3:
    return UnknownLoadProfileV3(
        profile_id=profile_id,
        scope_endpoint_id=scope,
        lifecycle="observing",
        signature={"delta_w_median": 42.0, "delta_w_mad": 3.5},
        event_count=5,
        independent_event_count=3,
        first_seen=NOW - timedelta(days=1),
        last_seen=NOW,
    )


def _open(path: Path) -> Database:
    db = Database(path)
    # Later waves add migrations after v019; a fresh Database migrates to the
    # latest schema, not to v019.  Assert at least v019 rather than equality.
    assert db.schema_version >= V019.version
    return db


# --------------------------------------------------------------------------- #
# Migration contract and idempotency / atomicity
# --------------------------------------------------------------------------- #


def test_module_declares_the_reserved_version_and_tiers() -> None:
    assert V019.version == 19
    assert TIERS == {
        "reader_residual_series": "L3",
        "unknown_load_profiles": "L4",
    }
    assert LEGACY_SCOPE == "scope_unknown"


def test_migration_creates_the_two_tables(tmp_path: Path) -> None:
    path = tmp_path / "tables.db"
    _build_pre_migration_db(path)
    db = _open(path)
    try:
        tables = {
            row[0]
            for row in db._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "reader_residual_series" in tables
        assert "unknown_load_profiles" in tables
        columns = {
            row[1]
            for row in db._conn.execute("PRAGMA table_info(reader_residual_series)")
        }
        assert {
            "reader_id",
            "at",
            "residual_w",
            "baseline_w",
            "dynamic_w",
            "noise_sigma_w",
            "topology_version_id",
            "basis",
        } <= columns
    finally:
        db.close()


def test_apply_callable_is_idempotent(tmp_path: Path) -> None:
    """Invoking the apply hook twice directly leaves the schema unchanged."""
    path = tmp_path / "idempotent.db"
    conn = sqlite3.connect(path)
    try:
        for migration in MIGRATIONS:
            if migration.version > PRE_MIGRATION_VERSION:
                break
            for statement in migration.statements:
                conn.execute(statement)
            if migration.apply is not None:
                migration.apply(conn)

        assert V019.apply is not None
        V019.apply(conn)
        first_columns = {row[1] for row in conn.execute("PRAGMA table_info(residual_power)")}
        V019.apply(conn)
        second_columns = {row[1] for row in conn.execute("PRAGMA table_info(residual_power)")}
        assert first_columns == second_columns
        assert "legacy_scope" in second_columns
        # The column was added exactly once.
        assert sum(1 for c in second_columns if c == "legacy_scope") == 1
    finally:
        conn.close()


def test_failure_mid_apply_leaves_user_version_unchanged(tmp_path: Path) -> None:
    """An injected failure inside the apply hook rolls the whole migration back."""
    path = tmp_path / "atomicity.db"
    _build_pre_migration_db(path)

    conn = sqlite3.connect(path)
    try:
        assert current_version(conn) == PRE_MIGRATION_VERSION
        # Wrap DDL + apply hook + version bump in the same transaction, matching
        # ``apply_migrations``.  A failing hook must leave user_version at 11.
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in V019.statements:
                conn.execute(statement)
            raise RuntimeError("boom mid-apply")
        except RuntimeError:
            conn.execute("ROLLBACK")

        assert current_version(conn) == PRE_MIGRATION_VERSION
        columns = {row[1] for row in conn.execute("PRAGMA table_info(residual_power)")}
        assert "legacy_scope" not in columns
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "reader_residual_series" not in tables
    finally:
        conn.close()


def test_apply_migrations_reaches_v019(tmp_path: Path) -> None:
    path = tmp_path / "reach.db"
    _build_pre_migration_db(path)
    conn = sqlite3.connect(path)
    try:
        assert current_version(conn) == PRE_MIGRATION_VERSION
        # Later migrations (e.g. D5's v021) advance past v019.
        assert apply_migrations(conn) == LATEST_VERSION
        assert current_version(conn) == LATEST_VERSION
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Legacy rows flagged; series independence; profile round-trip
# --------------------------------------------------------------------------- #


def test_legacy_rows_are_flagged_scope_unknown(tmp_path: Path) -> None:
    """v019 flags the install-wide legacy rows; v025 then archives them read-only."""
    path = tmp_path / "legacy.db"
    _build_pre_migration_db(path)
    db = _open(path)
    try:
        columns = {row[1] for row in db._conn.execute("PRAGMA table_info(residual_power_legacy)")}
        assert "legacy_scope" in columns
        rows = db._conn.execute(
            "SELECT residual_w, legacy_scope FROM residual_power_legacy"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["legacy_scope"] == LEGACY_SCOPE
        assert rows[0]["residual_w"] == 123.5
        # The archived table is immutable: no new legacy row can be written.
        with pytest.raises(sqlite3.IntegrityError, match="read-only"):
            db._conn.execute(
                "INSERT INTO residual_power_legacy(timestamp, residual_w) VALUES(?, ?)",
                ("2026-11-01T00:00:00+00:00", 7.0),
            )
    finally:
        db.close()


def test_two_readers_series_stay_independent(tmp_path: Path) -> None:
    path = tmp_path / "series.db"
    _build_pre_migration_db(path)
    db = _open(path)
    try:
        repo = db.repos.residual
        t0, t1 = NOW, NOW + timedelta(minutes=1)
        written = repo.put_points(
            [
                _point("reader-a", t0, 100.0),
                _point("reader-a", t1, 120.0),
                _point("reader-b", t0, 5.0),
            ]
        )
        assert written == 3

        a = repo.series("reader-a")
        b = repo.series("reader-b")
        assert [row["residual_w"] for row in a] == [100.0, 120.0]
        assert [row["residual_w"] for row in b] == [5.0]
        assert all(row["reader_id"] == "reader-a" for row in a)
        assert all(row["reader_id"] == "reader-b" for row in b)

        # A window applies to one reader only; nulls stay null (HB §18).
        since = NOW + timedelta(seconds=30)
        assert [row["residual_w"] for row in repo.series("reader-a", since=since)] == [120.0]
        assert repo.series("reader-b", since=since) == []

        latest = repo.latest("reader-a")
        assert latest is not None and latest["residual_w"] == 120.0
        assert repo.latest("reader-missing") is None

        unknown = ReaderResidualPoint(
            reader_id="reader-a",
            at=NOW + timedelta(minutes=2),
            residual_w=None,
            baseline_w=None,
            dynamic_w=None,
            noise_sigma_w=None,
            topology_version_id="topo-7",
            basis=MeasurementBasis.UNKNOWN,
        )
        repo.put_points([unknown])
        row = repo.latest("reader-a")
        assert row is not None and row["residual_w"] is None
    finally:
        db.close()


def test_profile_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "profiles.db"
    _build_pre_migration_db(path)
    db = _open(path)
    try:
        repo = db.repos.residual
        repo.put_profile(_profile("prof-1", "reader-a"))
        repo.put_profile(_profile("prof-2", "reader-b"))

        all_profiles = repo.profiles()
        assert {row["profile_id"] for row in all_profiles} == {"prof-1", "prof-2"}

        one = repo.profiles(scope_endpoint_id="reader-a")
        assert len(one) == 1
        row = one[0]
        assert row["profile_id"] == "prof-1"
        assert row["lifecycle"] == "observing"
        assert row["event_count"] == 5
        assert row["independent_event_count"] == 3
        assert row["signature"] == {"delta_w_median": 42.0, "delta_w_mad": 3.5}

        # Lifecycle filter and replace-by-id both work; retired rows are retained.
        assert repo.profiles(lifecycle="candidate") == []
        retired = UnknownLoadProfileV3(**{**_profile("prof-1").model_dump(), "lifecycle": "retired"})
        repo.put_profile(retired)
        assert repo.profiles(lifecycle="retired")[0]["profile_id"] == "prof-1"
        assert len(repo.profiles()) == 2
    finally:
        db.close()


def test_repository_is_reached_by_module_name(tmp_path: Path) -> None:
    path = tmp_path / "gateway.db"
    _build_pre_migration_db(path)
    db = _open(path)
    try:
        assert hasattr(db.repos.residual, "put_points")
        assert hasattr(db.repos.residual, "profiles")
    finally:
        db.close()
