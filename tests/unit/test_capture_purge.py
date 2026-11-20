"""Unit tests for the batched ``ct_samples`` purge (T03, ADR-0042).

A real :class:`~wattmosaic_engine.storage.database.Database` provides the schema
and indexes; only the purge transaction shape is under test here.  The runtime
wiring that calls it belongs to T08.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import threading
from typing import Any

import pytest
from wattmosaic_engine.contracts import MeasurementType, ReaderMeasurement
from wattmosaic_engine.storage.capture import _batch_select_sql, purge_ct_samples

UTC = dt.UTC


def _reading(received_at: dt.datetime, sequence: int = 1) -> ReaderMeasurement:
    return ReaderMeasurement(
        reader_id="ct_test",
        observed_at=received_at,
        received_at=received_at,
        active_power_w=1.0,
        phase="l1",
        measurement_type=MeasurementType.INSTANTANEOUS,
        calibration_version="cal-1",
        measurement_basis="ct_measured",
        sequence=sequence,
    )


def _count(db: Any) -> int:
    return int(db._conn.execute("SELECT COUNT(*) FROM ct_samples").fetchone()[0])


class AdvancingClock:
    """A monotonic stub that advances a fixed step on every call."""

    def __init__(self, step: float) -> None:
        self.value = 0.0
        self.step = step

    def __call__(self) -> float:
        self.value += self.step
        return self.value


class TrackingLock:
    """Records acquire/release order into a shared event list."""

    def __init__(self, events: list[str] | None = None) -> None:
        self._lock = threading.Lock()
        self.events = events if events is not None else []

    def __enter__(self) -> TrackingLock:
        self._lock.acquire()
        self.events.append("acquire")
        return self

    def __exit__(self, *exc: object) -> bool:
        self._lock.release()
        self.events.append("release")
        return False


class RecordingConn:
    """Connection proxy logging BEGIN/COMMIT/ROLLBACK into a shared event list."""

    def __init__(self, conn: sqlite3.Connection, events: list[str]) -> None:
        self._conn = conn
        self._events = events

    def execute(self, sql: str, *args: Any, **kwargs: Any) -> Any:
        command = sql.strip().upper()
        if command in {"BEGIN IMMEDIATE", "COMMIT", "ROLLBACK"}:
            self._events.append(command)
        return self._conn.execute(sql, *args, **kwargs)

    @property
    def in_transaction(self) -> bool:
        return self._conn.in_transaction

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


class FailingCommit:
    """Connection proxy that raises on ``COMMIT`` but delegates everything else."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def execute(self, sql: str, *args: Any, **kwargs: Any) -> Any:
        if sql.strip().upper() == "COMMIT":
            raise sqlite3.OperationalError("simulated COMMIT failure")
        return self._conn.execute(sql, *args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


def test_purge_all_when_cutoff_none(tmp_db_path: Any) -> None:
    from wattmosaic_engine.storage.database import Database

    db = Database(tmp_db_path)
    try:
        db.repos.samples.insert_readings(
            [
                _reading(dt.datetime(2020, 1, 1, tzinfo=UTC) + dt.timedelta(seconds=i))
                for i in range(12)
            ]
        )
        assert _count(db) == 12

        result = purge_ct_samples(
            db._conn, db._lock, cutoff=None, batch_rows=5, max_batches=None, time_budget_s=None
        )
        assert (result.deleted, result.batches, result.done) == (12, 3, True)
        assert _count(db) == 0
    finally:
        db.close()


def test_purge_respects_cutoff(tmp_db_path: Any) -> None:
    from wattmosaic_engine.storage.database import Database

    db = Database(tmp_db_path)
    try:
        old = dt.datetime(2020, 1, 1, tzinfo=UTC)
        new = dt.datetime(2024, 1, 1, tzinfo=UTC)
        db.repos.samples.insert_readings([_reading(old), _reading(new)])
        cutoff = dt.datetime(2022, 1, 1, tzinfo=UTC)

        result = purge_ct_samples(db._conn, db._lock, cutoff=cutoff, batch_rows=5000)
        assert (result.deleted, result.batches, result.done) == (1, 1, True)
        remaining = db._conn.execute("SELECT received_at FROM ct_samples").fetchall()
        assert len(remaining) == 1
        assert remaining[0][0] == new.isoformat()
    finally:
        db.close()


def test_purge_stops_at_max_batches(tmp_db_path: Any) -> None:
    from wattmosaic_engine.storage.database import Database

    db = Database(tmp_db_path)
    try:
        db.repos.samples.insert_readings(
            [
                _reading(dt.datetime(2020, 1, 1, tzinfo=UTC) + dt.timedelta(seconds=i))
                for i in range(12)
            ]
        )

        first = purge_ct_samples(
            db._conn,
            db._lock,
            cutoff=None,
            batch_rows=5,
            max_batches=1,
            time_budget_s=None,
        )
        assert (first.deleted, first.batches, first.done) == (5, 1, False)
        assert _count(db) == 7

        second = purge_ct_samples(
            db._conn,
            db._lock,
            cutoff=None,
            batch_rows=5,
            max_batches=1,
            time_budget_s=None,
        )
        assert (second.deleted, second.batches, second.done) == (5, 1, False)
        assert _count(db) == 2
    finally:
        db.close()


def test_purge_stops_at_time_budget(tmp_db_path: Any) -> None:
    from wattmosaic_engine.storage.database import Database

    db = Database(tmp_db_path)
    try:
        db.repos.samples.insert_readings(
            [
                _reading(dt.datetime(2020, 1, 1, tzinfo=UTC) + dt.timedelta(seconds=i))
                for i in range(12)
            ]
        )
        clock = AdvancingClock(step=10.0)

        result = purge_ct_samples(
            db._conn,
            db._lock,
            cutoff=None,
            batch_rows=5,
            max_batches=None,
            time_budget_s=1.0,
            monotonic=clock,
        )
        assert (result.deleted, result.batches, result.done) == (5, 1, False)
        assert _count(db) == 7
    finally:
        db.close()


def test_each_batch_commits(tmp_db_path: Any) -> None:
    from wattmosaic_engine.storage.database import Database

    db = Database(tmp_db_path)
    try:
        db.repos.samples.insert_readings(
            [
                _reading(dt.datetime(2020, 1, 1, tzinfo=UTC) + dt.timedelta(seconds=i))
                for i in range(12)
            ]
        )
        events: list[str] = []
        lock = TrackingLock(events)
        purge_ct_samples(
            RecordingConn(db._conn, events),  # type: ignore[arg-type]
            lock,  # type: ignore[arg-type]
            cutoff=None,
            batch_rows=5,
            max_batches=1,
            time_budget_s=None,
        )
        assert events == ["acquire", "BEGIN IMMEDIATE", "COMMIT", "release"]

        # A brand-new connection sees only committed rows.  An uncommitted
        # transaction on db._conn would leave the count at 12 here.
        fresh = sqlite3.connect(str(tmp_db_path))
        try:
            assert fresh.execute("SELECT COUNT(*) FROM ct_samples").fetchone()[0] == 7
        finally:
            fresh.close()
    finally:
        db.close()


def test_lock_released_between_batches(tmp_db_path: Any) -> None:
    from wattmosaic_engine.storage.database import Database

    db = Database(tmp_db_path)
    try:
        db.repos.samples.insert_readings(
            [
                _reading(dt.datetime(2020, 1, 1, tzinfo=UTC) + dt.timedelta(seconds=i))
                for i in range(12)
            ]
        )
        events: list[str] = []
        lock = TrackingLock(events)
        result = purge_ct_samples(
            RecordingConn(db._conn, events),  # type: ignore[arg-type]
            lock,  # type: ignore[arg-type]
            cutoff=None,
            batch_rows=5,
            max_batches=None,
            time_budget_s=None,
        )
        assert (result.deleted, result.batches, result.done) == (12, 3, True)
        assert events == [
            "acquire",
            "BEGIN IMMEDIATE",
            "COMMIT",
            "release",
        ] * 3
    finally:
        db.close()


def test_cutoff_query_uses_time_index(tmp_db_path: Any) -> None:
    """The age-filtered batch must seek idx_ct_samples_time, never a full scan."""
    from wattmosaic_engine.storage.database import Database

    db = Database(tmp_db_path)
    try:
        db.repos.samples.insert_readings(
            [
                _reading(dt.datetime(2020, 1, 1, tzinfo=UTC) + dt.timedelta(seconds=i))
                for i in range(12)
            ]
        )
        select_sql, select_params = _batch_select_sql("2022-01-01T00:00:00+00:00")
        assert "received_at < ?" in select_sql
        plan = db._conn.execute(
            "EXPLAIN QUERY PLAN " + select_sql, (*select_params, 5)
        ).fetchall()
        details = [str(row[3]) for row in plan]
        assert any("ct_samples" in d and ("USING INDEX" in d or "USING COVERING INDEX" in d) for d in details)
        assert not any("SCAN ct_samples" in d for d in details)
    finally:
        db.close()


def test_row_exactly_at_cutoff_is_kept(tmp_db_path: Any) -> None:
    from wattmosaic_engine.storage.database import Database

    db = Database(tmp_db_path)
    try:
        at = dt.datetime(2022, 1, 1, tzinfo=UTC)
        before = at - dt.timedelta(seconds=1)
        db.repos.samples.insert_readings([_reading(before), _reading(at)])

        result = purge_ct_samples(db._conn, db._lock, cutoff=at, batch_rows=5)
        assert (result.deleted, result.batches, result.done) == (1, 1, True)
        remaining = db._conn.execute("SELECT received_at FROM ct_samples").fetchall()
        assert [row[0] for row in remaining] == [at.isoformat()]
    finally:
        db.close()


def test_non_utc_cutoff_is_normalised(tmp_db_path: Any) -> None:
    from wattmosaic_engine.storage.database import Database

    db = Database(tmp_db_path)
    try:
        # 03:00+02:00 == 01:00 UTC.  Rows at 00:00 UTC (older) and 02:00 UTC
        # (newer) straddle it; a naive string compare on the +02:00 spelling
        # would classify them wrongly.
        old = dt.datetime(2020, 1, 1, 0, 0, tzinfo=UTC)
        new = dt.datetime(2020, 1, 1, 2, 0, tzinfo=UTC)
        db.repos.samples.insert_readings([_reading(old), _reading(new)])
        cutoff = dt.datetime(2020, 1, 1, 3, 0, tzinfo=dt.timezone(dt.timedelta(hours=2)))

        result = purge_ct_samples(db._conn, db._lock, cutoff=cutoff, batch_rows=5)
        assert (result.deleted, result.batches, result.done) == (1, 1, True)
        remaining = db._conn.execute("SELECT received_at FROM ct_samples").fetchall()
        assert [row[0] for row in remaining] == [new.isoformat()]
    finally:
        db.close()


def test_missing_table_is_noop() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        result = purge_ct_samples(conn, threading.Lock(), cutoff=None)
        assert (result.deleted, result.batches, result.done) == (0, 0, True)
    finally:
        conn.close()


def test_table_disappears_after_progress(tmp_db_path: Any) -> None:
    """A table dropped between batches keeps the progress already committed."""
    from wattmosaic_engine.storage.database import Database

    db = Database(tmp_db_path)
    try:
        db.repos.samples.insert_readings(
            [
                _reading(dt.datetime(2020, 1, 1, tzinfo=UTC) + dt.timedelta(seconds=i))
                for i in range(10)
            ]
        )

        class DropAfterFirst(RecordingConn):
            def execute(self, sql: str, *args: Any, **kwargs: Any) -> Any:
                result = super().execute(sql, *args, **kwargs)
                if sql.strip().upper() == "COMMIT":
                    self._conn.execute("DROP TABLE ct_samples")
                return result

        events: list[str] = []
        result = purge_ct_samples(
            DropAfterFirst(db._conn, events),  # type: ignore[arg-type]
            TrackingLock(events),  # type: ignore[arg-type]
            cutoff=None,
            batch_rows=5,
            max_batches=None,
            time_budget_s=None,
        )
        assert (result.deleted, result.batches, result.done) == (5, 1, True)
    finally:
        db.close()


def test_rollback_on_error(tmp_db_path: Any) -> None:
    from wattmosaic_engine.storage.database import Database

    db = Database(tmp_db_path)
    try:
        db.repos.samples.insert_readings(
            [
                _reading(dt.datetime(2020, 1, 1, tzinfo=UTC) + dt.timedelta(seconds=i))
                for i in range(3)
            ]
        )
        assert _count(db) == 3
        lock = threading.Lock()

        with pytest.raises(sqlite3.OperationalError, match="simulated COMMIT failure"):
            purge_ct_samples(
                FailingCommit(db._conn),  # type: ignore[arg-type]
                lock,
                cutoff=None,
                batch_rows=5,
                time_budget_s=None,
            )

        # The failed batch was rolled back, and the lock was released.
        assert _count(db) == 3
        assert lock.acquire(blocking=False) is True
        lock.release()
    finally:
        db.close()


def test_rejects_bad_arguments(tmp_db_path: Any) -> None:
    from wattmosaic_engine.storage.database import Database

    db = Database(tmp_db_path)
    try:
        with pytest.raises(ValueError):
            purge_ct_samples(db._conn, db._lock, cutoff=None, batch_rows=0)
        with pytest.raises(ValueError):
            purge_ct_samples(db._conn, db._lock, cutoff=None, max_batches=0)
    finally:
        db.close()
