"""Versioned inference records: version monotonicity and retention survival.

Two defects motivate this file, both named in `docs/assessment_gap_analysis.md`:

* **G15** — ``inference_results`` rows carry no algorithm version, no previous
  value and no delta, so a stored result cannot be explained after an upgrade.
  ``inference_records`` is the append-only, versioned successor (its legacy
  writer was removed in contract 3.0.0; the table and read path remain).
* **G16** — ``prune()`` deletes ``inference_results`` past ``retention_days_raw``
  (30 days by default).  An audit trail that evaporates in 30 days cannot audit
  anything, so the audit tables are exempt from raw retention while raw *samples*
  keep their existing horizon.

The tests below exercise the real ``Database`` against a temporary SQLite file,
because the contract can be perfect while storage silently drops the fields —
which is exactly what happened to the residual split in 1.4.0.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from wattmosaic_engine.contracts import (
    MeasurementBasis,
    MeasurementFactor,
    MeasurementType,
    ReaderMeasurement,
)
from wattmosaic_engine.storage.database import Database
from wattmosaic_engine.storage.migrations import LATEST_VERSION

BASE = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)


def _insert_record(
    db: Database,
    record_id: str,
    *,
    device_id: str = "dev1",
    timestamp: datetime = BASE,
    revision: int = 1,
    topology_version: int = 1,
    power_w: float = 100.0,
) -> None:
    """Write one ``inference_records`` row directly.

    The legacy ``InferenceRecord`` writer was removed in contract 3.0.0; the
    table and its read path (``db.repos.records``) remain, so the tests seed
    rows with SQL and exercise the surviving migration/read/retention paths.
    """
    with db._lock:
        db._conn.execute(
            "INSERT INTO inference_records(record_id, device_id, timestamp, "
            "algorithm_version, contract_version, topology_version, revision, power_w) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                record_id,
                device_id,
                timestamp.isoformat(),
                "disagg@2.1",
                "1.6.0",
                topology_version,
                revision,
                power_w,
            ),
        )


# --------------------------------------------------------------------------- #
# Migration 9
# --------------------------------------------------------------------------- #


def test_a_fresh_database_reaches_the_latest_schema(tmp_path) -> None:
    db = Database(tmp_path / "fresh.db")
    try:
        assert db.schema_version == LATEST_VERSION
        tables = {
            row["name"]
            for row in db._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {
            "circuits",
            "topology_relations_legacy",
            "inference_records",
            "ct_measurement_factors",
            "ha_entity_bindings_legacy",
            "residual_power_legacy",
            "isolation_verdicts",
        } <= tables
        # Migration 25 archives the legacy tables; the originals are gone.
        assert not {"topology_relations", "ha_entity_bindings", "residual_power"} & tables
    finally:
        db.close()


def test_migration_9_creates_both_inference_record_indexes(tmp_path) -> None:
    db = Database(tmp_path / "idx.db")
    try:
        indexes = {
            row["name"]
            for row in db._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='inference_records'"
            )
        }
        assert any("device" in name and "revision" in name for name in indexes), indexes
        # The (device_id, timestamp) index matters for the history read path.
        cols = {
            row["name"]: [c[2] for c in db._conn.execute(f"PRAGMA index_info({row['name']})")]
            for row in db._conn.execute("PRAGMA index_list(inference_records)")
        }
        assert ["device_id", "timestamp"] in cols.values(), cols
        assert ["device_id", "revision"] in cols.values(), cols
    finally:
        db.close()


def test_migrating_a_schema_8_database_is_additive(tmp_path) -> None:
    """A v8 install keeps every row and gains the audit tables.

    Migration 9 must be additive and forward-only like the eight before it.
    Rebuilding ``inference_results`` or rewriting ``ct_samples`` would be a data
    migration; this only adds tables.
    """
    import sqlite3

    from wattmosaic_engine.storage.migrations import MIGRATIONS

    path = tmp_path / "v8.db"
    conn = sqlite3.connect(str(path), isolation_level=None)
    try:
        for migration in MIGRATIONS:
            if migration.version > 8:
                break
            for statement in migration.statements:
                conn.execute(statement)
            hook = getattr(migration, "apply", None)
            if hook is not None:
                hook(conn)
        conn.execute("PRAGMA user_version = 8")
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 8
        # A row the v8 install already had.
        conn.execute(
            """INSERT INTO inference_results(device_id, timestamp, power_w, source, confidence,
            quality_status) VALUES(?,?,?,?,?,?)""",
            ("dev1", BASE.isoformat(), 42.0, "inferred", 0.4, "good"),
        )
        conn.execute(
            """INSERT INTO reconciliation_results(timestamp, topology_version, main_power_w,
            direct_measured_w, inferred_w, model_estimated_w, unknown_residual_w,
            reconciliation_error_w, reconciliation_error_ratio, accounted_fraction,
            quality_status) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (BASE.isoformat(), 1, 1000.0, 400.0, 0.0, 0.0, 600.0, 0.0, 0.0, 0.4, "good"),
        )
        assert "inference_records" not in {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()

    db = Database(path)
    try:
        assert db.schema_version == LATEST_VERSION
        # Existing rows are untouched.
        n_results = db._conn.execute("SELECT COUNT(*) AS n FROM inference_results").fetchone()["n"]
        assert n_results == 1
        n_recon = db._conn.execute(
            "SELECT COUNT(*) AS n FROM reconciliation_results"
        ).fetchone()["n"]
        assert n_recon == 1
        # And the new audit surface is usable.
        _insert_record(db, "r1")
        assert len(db.inference_records_for_device("dev1")) == 1
    finally:
        db.close()


def test_re_running_migration_9_is_a_no_op(tmp_path) -> None:
    from wattmosaic_engine.storage.migrations import apply_migrations

    path = tmp_path / "twice.db"
    first = Database(path)
    try:
        _insert_record(first, "r1")
    finally:
        first.close()

    second = Database(path)
    try:
        assert apply_migrations(second._conn) == LATEST_VERSION
        assert second._conn.execute(
            "SELECT COUNT(*) AS n FROM inference_records"
        ).fetchone()["n"] == 1
    finally:
        second.close()


def test_an_interrupted_migration_9_still_applies(tmp_path) -> None:
    """The tables exist but ``user_version`` is still 8: the case a restart sees.

    ``CREATE TABLE IF NOT EXISTS`` makes this a no-op rather than a
    ``table already exists`` abort, which is why migration 9 needs no ``apply``
    hook (unlike the ``ALTER TABLE`` migrations 7 and 8).
    """
    import sqlite3

    from wattmosaic_engine.storage.migrations import MIGRATIONS

    path = tmp_path / "interrupted.db"
    conn = sqlite3.connect(str(path), isolation_level=None)
    try:
        for migration in MIGRATIONS:
            if migration.version > 9:
                break
            for statement in migration.statements:
                conn.execute(statement)
            hook = getattr(migration, "apply", None)
            if hook is not None:
                hook(conn)
        conn.execute("PRAGMA user_version = 8")
        conn.execute(
            "INSERT INTO inference_records(record_id, device_id, timestamp, "
            "algorithm_version, contract_version, power_w) VALUES(?,?,?,?,?,?)",
            ("r1", "dev1", BASE.isoformat(), "disagg@2", "1.6.0", 12.0),
        )
    finally:
        conn.close()

    db = Database(path)
    try:
        assert db.schema_version == LATEST_VERSION
        # The row written before the version advanced is still there.
        assert len(db.inference_records_for_device("dev1")) == 1
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Read path
# --------------------------------------------------------------------------- #


def test_latest_inference_record_is_the_highest_revision(tmp_path) -> None:
    db = Database(tmp_path / "latest.db")
    try:
        for revision in (1, 2, 3):
            _insert_record(
                db,
                f"r{revision}",
                revision=revision,
                timestamp=BASE + timedelta(minutes=revision),
            )
        latest = db.repos.records.latest_inference_record("dev1")
        assert latest is not None
        assert latest["record_id"] == "r3"
        assert latest["revision"] == 3
        assert db.repos.records.latest_inference_record("nope") is None
    finally:
        db.close()


def test_inference_records_for_device_honours_since_and_limit(tmp_path) -> None:
    db = Database(tmp_path / "window.db")
    try:
        for revision in (1, 2, 3):
            _insert_record(
                db,
                f"r{revision}",
                revision=revision,
                timestamp=BASE + timedelta(hours=revision),
            )
        recent = db.inference_records_for_device(
            "dev1", since=BASE + timedelta(hours=2)
        )
        assert [r["record_id"] for r in recent] == ["r2", "r3"]
        limited = db.inference_records_for_device("dev1", limit=1)
        assert len(limited) == 1
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Retention exemption (G16)
# --------------------------------------------------------------------------- #


def _reading(power_w: float, at: datetime, sequence: int) -> ReaderMeasurement:
    return ReaderMeasurement(
        reader_id="c",
        observed_at=at,
        received_at=at,
        active_power_w=power_w,
        phase="l1",
        measurement_type=MeasurementType.INSTANTANEOUS,
        calibration_version="cal-1",
        measurement_basis=MeasurementBasis.CT_MEASURED,
        sequence=sequence,
    )


def test_prune_exempts_audit_tables_but_still_prunes_raw_samples(tmp_path) -> None:
    """A 400-day-old audit record survives; a 400-day-old raw sample does not.

    ``inference_records`` is audit state, not a raw sample.  The existing raw
    horizon exists to bound ``ct_samples`` growth; applying it to a versioned
    record would delete the very history the record exists to keep.
    """
    db = Database(tmp_path / "retention.db")
    old = datetime.now(UTC) - timedelta(days=400)
    new = datetime.now(UTC)
    try:
        _insert_record(db, "old", timestamp=old, revision=1)
        db.repos.factors.insert_measurement_factor(
            MeasurementFactor(
                ct_id="panel_l1",
                multiplier=2.0,
                effective_from=old,
                effective_to=None,
                applied_by="user",
                declared_by="user",
            )
        )
        db.repos.samples.insert_readings([_reading(1.0, old, 1), _reading(2.0, new, 2)])

        removed = db.prune(raw_days=30, aggregate_days=730)

        # Raw samples obey the horizon as before.
        assert removed["ct_samples"] == 1
        assert db.repos.samples.ct_samples_between(
            new - timedelta(days=1), new + timedelta(days=1)
        )
        # Audit state does not.
        assert db.inference_records_for_device("dev1")
        assert db.factors_for_ct("panel_l1")
        assert removed["inference_records"] == 0
        assert removed["ct_measurement_factors"] == 0
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Reconciliation read path & version retention (REQ-TOPO-009, REQ-CHANGE-004/007)
# --------------------------------------------------------------------------- #


def test_records_keep_their_topology_version(tmp_path) -> None:
    """A record stores the version it was produced under, unchanged, on read."""
    db = Database(tmp_path / "keep_version.db")
    try:
        for version, record_id in ((1, "r1"), (2, "r2"), (3, "r3")):
            _insert_record(db, record_id, topology_version=version)
        versions = {
            r["record_id"]: r["topology_version"]
            for r in db.inference_records_for_device("dev1")
        }
        assert versions == {"r1": 1, "r2": 2, "r3": 3}
    finally:
        db.close()


def test_reconciliation_read_path_is_additive_no_status_column(tmp_path) -> None:
    """Staleness is computed from the existing version field, not a new column."""
    db = Database(tmp_path / "no_flag.db")
    try:
        _insert_record(db, "r1")
        cols = {row["name"] for row in db._conn.execute("PRAGMA table_info(inference_records)")}
        # No status/stale flag column was added by reconciliation.
        assert not {"status", "stale", "is_stale", "superseded"} & cols
        assert "topology_version" in cols
        assert db.inference_records_for_device("dev1")[0]["topology_version"] == 1
    finally:
        db.close()
