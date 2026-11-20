"""The ``capture`` retention tier and ``prune(capture_days=...)``.

ADR-0042 gives ``ct_samples`` its own capture horizon so the on-loop prune can
hand it to the background purge.  Without ``capture_days`` the pass keeps the
legacy behaviour (raw cutoff) so direct storage callers and existing tests are
unchanged; with any ``capture_days`` value the capture-tier tables are skipped
and reported as ``0`` (T02).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from wattmosaic_engine.contracts import (
    MeasurementBasis,
    MeasurementType,
    ReaderMeasurement,
)
from wattmosaic_engine.storage.database import Database
from wattmosaic_engine.storage.repositories import iso
from wattmosaic_engine.storage.tiers import (
    CAPTURE_HORIZON,
    PRUNABLE_TABLES,
    RAW_CAPTURE_ENV,
    prunable_tables,
)


def _reading(power_w: float, at: datetime, sequence: int) -> ReaderMeasurement:
    return ReaderMeasurement(
        reader_id="ct_test",
        observed_at=at,
        received_at=at,
        active_power_w=power_w,
        phase="l1",
        measurement_type=MeasurementType.INSTANTANEOUS,
        calibration_version="cal-1",
        measurement_basis=MeasurementBasis.CT_MEASURED,
        sequence=sequence,
    )


def _insert_old_system_event(db: Database, at: datetime) -> None:
    with db._lock:
        db._conn.execute(
            "INSERT INTO system_events(timestamp, level, category, message, details_json) "
            "VALUES(?,?,?,?,?)",
            (iso(at), "info", "test", "old", "{}"),
        )


def test_prune_without_capture_days_keeps_legacy_behaviour(tmp_path) -> None:
    """No ``capture_days``: ``ct_samples`` is pruned on the raw horizon as before."""
    db = Database(tmp_path / "legacy.db")
    old = datetime.now(UTC) - timedelta(days=400)
    new = datetime.now(UTC)
    try:
        db.repos.samples.insert_readings([_reading(1.0, old, 1), _reading(2.0, new, 2)])
        removed = db.prune(raw_days=30, aggregate_days=730)
        assert removed["ct_samples"] == 1
        assert len(db.repos.samples.ct_samples_between(
            new - timedelta(days=1), new + timedelta(days=1)
        )) == 1
    finally:
        db.close()


def test_prune_with_capture_days_skips_ct_samples(tmp_path) -> None:
    """Any ``capture_days`` (incl. 0) leaves ``ct_samples`` to the background purge."""
    for days in (0, 3, 90):
        db = Database(tmp_path / f"capture_{days}.db")
        old = datetime.now(UTC) - timedelta(days=400)
        new = datetime.now(UTC)
        try:
            db.repos.samples.insert_readings([_reading(1.0, old, 1), _reading(2.0, new, 2)])
            removed = db.prune(raw_days=30, aggregate_days=730, capture_days=days)
            assert removed["ct_samples"] == 0
            assert db.repos.samples.ct_samples_between(
                old - timedelta(days=1), old + timedelta(days=1)
            ), f"old row must survive with capture_days={days}"
            assert db.repos.samples.ct_samples_between(
                new - timedelta(days=1), new + timedelta(days=1)
            ), f"new row must survive with capture_days={days}"
        finally:
            db.close()


def test_prune_with_capture_days_still_prunes_other_raw_tables(tmp_path) -> None:
    """Skipping the capture tier does not exempt other raw-tier tables."""
    db = Database(tmp_path / "other_raw.db")
    old = datetime.now(UTC) - timedelta(days=400)
    try:
        _insert_old_system_event(db, old)
        removed = db.prune(raw_days=30, aggregate_days=730, capture_days=2)
        assert removed["system_events"] == 1
    finally:
        db.close()


def test_ct_samples_is_in_capture_tier() -> None:
    """The registry declares ``ct_samples`` on the capture horizon, not raw."""
    declared = {table: (col, horizon) for table, col, horizon in prunable_tables()}
    assert declared["ct_samples"] == ("received_at", CAPTURE_HORIZON)
    assert PRUNABLE_TABLES["ct_samples"] == ("received_at", CAPTURE_HORIZON)


def test_raw_capture_env_name() -> None:
    """The capture env var name is the one fixed in the interface."""
    assert RAW_CAPTURE_ENV == "WM_RAW_CAPTURE_DAYS"
