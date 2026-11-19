"""End-to-end: storage schema, persistence round-trips and restart recovery.

Since the contract-3.0.0 cutover the legacy topology tables are read-only
archives and the legacy write helpers (``save_topology``, ``insert_ct_samples``,
``insert_inference_results``, ``insert_reconciliation``) are gone.  Samples are
written as ``ReaderMeasurement`` through ``db.repos.samples.insert_readings`` and
inference history through the v3 ``db.repos.inference.put_estimates`` writer.
"""

from __future__ import annotations

import datetime as dt
import sqlite3

import pytest

from tests.end_to_end.conftest import measurement_readings
from tools.simulator import HouseholdScenario, SimulatorConfig
from tools.simulator.sensors import QualityLevel


def _scenario(minutes: float = 20, interval: float = 15, seed: int = 7):
    return HouseholdScenario(
        SimulatorConfig(
            seed=seed, minutes=minutes, interval=interval, quality=QualityLevel.GOOD
        )
    )


# --------------------------------------------------------------------------- #
# Schema / migrations
# --------------------------------------------------------------------------- #


def test_database_creates_latest_schema(tmp_db_path) -> None:
    from wattmosaic_engine.storage.database import Database
    from wattmosaic_engine.storage.migrations import LATEST_VERSION

    db = Database(tmp_db_path)
    try:
        assert db.schema_version == LATEST_VERSION
        report = db.integrity_check()
        assert report["integrity"] == "ok"
        assert report["quick_check"] == "ok"
        assert report["schema_version"] == report["latest_schema_version"]
    finally:
        db.close()


def test_migrations_are_idempotent(tmp_db_path) -> None:
    """Re-opening an existing database must not re-apply or corrupt migrations."""
    from wattmosaic_engine.storage.database import Database

    db = Database(tmp_db_path)
    version = db.schema_version
    db.set_state("e2e", {"marker": 1})
    db.close()

    again = Database(tmp_db_path)
    try:
        assert again.schema_version == version
        assert again.get_state("e2e") == {"marker": 1}
        assert again.integrity_check()["integrity"] == "ok"
    finally:
        again.close()


def test_migration_from_v1_upgrades_in_place(tmp_db_path) -> None:
    """Simulate a pre-existing v1 database and confirm the upgrade is additive."""
    from wattmosaic_engine.storage.database import Database
    from wattmosaic_engine.storage.migrations import LATEST_VERSION, MIGRATIONS

    first = MIGRATIONS[0]
    conn = sqlite3.connect(str(tmp_db_path), isolation_level=None)
    try:
        for stmt in first.statements:
            conn.execute(stmt)
        conn.execute(f"PRAGMA user_version = {first.version}")
        conn.execute(
            "INSERT INTO engine_state(key, value, updated_at) VALUES('legacy', '{\"a\": 1}', ?)",
            (dt.datetime.now(dt.UTC).isoformat(),),
        )
    finally:
        conn.close()

    db = Database(tmp_db_path)
    try:
        assert db.schema_version == LATEST_VERSION
        assert db.get_state("legacy") == {"a": 1}
        # A table added by a later migration now exists and is usable.
        db.repos.models.set_model_frozen("any_device", True)
        assert db.repos.models.is_model_frozen("any_device") is True
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Sample persistence
# --------------------------------------------------------------------------- #


def test_reader_samples_persist_both_timestamps(engine_db) -> None:
    db = engine_db
    records = list(_scenario(minutes=10).records())
    readings = [reading for rec in records for reading in measurement_readings(rec)]
    inserted = db.repos.samples.insert_readings(readings)
    assert inserted == len(readings) > 0

    last = records[-1]
    ct_id, m = next(iter(last["measurements"].items()))
    latest = db.repos.samples.latest_ct_sample(ct_id)
    assert latest is not None
    assert latest["ct_id"] == ct_id
    assert latest["state_timestamp"]
    assert latest["received_at"]
    # physical_timestamp is a legacy column the reader contract does not carry.
    assert "physical_timestamp" in latest

    # The typed read path restores the observation and arrival instants exactly.
    reading = db.repos.samples.latest_reading(ct_id)
    assert reading is not None
    assert reading.observed_at == dt.datetime.fromisoformat(m["state_timestamp"])
    assert reading.received_at == dt.datetime.fromisoformat(m["received_at"])
    assert reading.active_power_w == pytest.approx(float(m["value_w"]))
    assert reading.sequence == int(m["sequence"])


def test_samples_between_filters_by_window(engine_db) -> None:
    db = engine_db
    records = list(_scenario(minutes=10).records())
    for rec in records:
        db.repos.samples.insert_readings(measurement_readings(rec))

    start = dt.datetime.fromisoformat(records[0]["timestamp"])
    end = dt.datetime.fromisoformat(records[len(records) // 2]["timestamp"])
    in_window = db.repos.samples.ct_samples_between(start, end)
    assert in_window
    for row in in_window:
        ts = dt.datetime.fromisoformat(row["received_at"])
        assert start <= ts <= end

    only_main = db.repos.samples.ct_samples_between(start, end, ct_ids=["ct_main_l1"])
    assert only_main
    assert {row["ct_id"] for row in only_main} == {"ct_main_l1"}


# --------------------------------------------------------------------------- #
# Inference persistence (v3 writer)
# --------------------------------------------------------------------------- #

_BASE = dt.datetime(2026, 1, 1, 0, 0, tzinfo=dt.UTC)


def _put_history(db, device_id: str, points: list[tuple[dt.datetime, float]]) -> int:
    """Persist ``(at, watts)`` points through the v3 ``inference_results`` writer."""
    from wattmosaic_engine.contracts import (
        Bounds,
        ConfidenceVectorV3,
        DeviceEstimate,
        EstimateStatus,
        EventGroupStatus,
        IdentifiabilityStatus,
        InferenceMethod,
        JointInferenceResult,
        VersionTriple,
    )

    versions = VersionTriple(
        topology_version_id="topo-1", mapping_version_id="map-1", model_set_id="mset-1"
    )
    estimates = [
        DeviceEstimate(
            subject_kind="device",
            subject_id=device_id,
            point_w=power,
            bounds=Bounds(),
            status=EstimateStatus.ESTIMATED,
            identifiability=IdentifiabilityStatus.IDENTIFIABLE,
            method=InferenceMethod.JOINT_DELTA,
            confidence=ConfidenceVectorV3(identifiability=0.9),
            source_reader_ids=["ct_main_l1"],
            evidence_refs=[],
            versions=versions,
            updated_at=at,
        )
        for at, power in points
    ]
    return db.repos.inference.put_estimates(
        JointInferenceResult(
            status=EventGroupStatus.CONFIRMED,
            device_estimates=estimates,
            group_estimates=[],
            unknown_estimates=[],
            candidates=[],
            matches={},
            residual_w_by_reader={},
            versions=versions,
            computed_at=points[-1][0],
        )
    )


def test_inference_results_persist_and_read_back(engine_db) -> None:
    db = engine_db
    ts = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC)
    for i in range(3):
        assert _put_history(db, f"dev_{i}", [(ts, 100.0 + i)]) == 1
    latest = db.repos.inference.latest_inference_results()
    assert {r["device_id"] for r in latest} == {"dev_0", "dev_1", "dev_2"}

    # A newer write replaces the "latest" view but history is preserved.
    _put_history(db, "dev_0", [(ts + dt.timedelta(seconds=15), 250.0)])
    latest = {r["device_id"]: r for r in db.repos.inference.latest_inference_results()}
    assert latest["dev_0"]["power_w"] == 250.0
    history = db.inference_history("dev_0", since=ts - dt.timedelta(minutes=1))
    assert len(history) == 2


def test_wal_database_survives_reopen(tmp_db_path) -> None:
    from wattmosaic_engine.storage.database import Database

    writer = Database(tmp_db_path)
    writer.set_state("marker", {"value": 42})
    writer.checkpoint()
    writer.close()

    reader = Database(tmp_db_path)
    try:
        assert reader.get_state("marker") == {"value": 42}
        assert reader.integrity_check()["integrity"] == "ok"
    finally:
        reader.close()


# --------------------------------------------------------------------------- #
# Energy aggregation + restart recovery
# --------------------------------------------------------------------------- #


def _inference_results(duration_s: int = 1200, step_s: int = 15):
    """A square-wave load (1 kW / 200 W, 5-minute period) as aggregator results.

    ``EnergyAggregator.add_result`` is duck-typed; the legacy ``InferenceResult``
    contract no longer exists, so a namespace with the fields it reads is used.
    """
    from types import SimpleNamespace

    out = []
    for k in range(duration_s // step_s + 1):
        at = _BASE + dt.timedelta(seconds=k * step_s)
        power = 1000.0 if (k * step_s) % 300 < 150 else 200.0
        out.append(
            SimpleNamespace(
                device_id="test_device",
                timestamp=at,
                power_w=power,
                source="inferred",
                quality_status=None,
                provenance={},
            )
        )
    return out


def _store(db, results) -> None:
    _put_history(db, "test_device", [(r.timestamp, r.power_w) for r in results])


def test_energy_aggregation_persists_buckets(engine_db) -> None:
    from wattmosaic_engine.aggregation.aggregator import EnergyAggregator

    db = engine_db
    results = _inference_results()
    _store(db, results)

    agg = EnergyAggregator(db)
    for r in results:
        agg.add_result(r)
    final = results[-1].timestamp
    buckets = agg.flush(now=final + dt.timedelta(seconds=5))
    assert buckets
    total = agg.energy_total("test_device")
    assert total > 0
    # 1 kW half the time, 200 W the other half over 20 minutes.
    expected = (1000 * 0.5 + 200 * 0.5) * (1200 / 3600) / 1000.0
    assert total == pytest.approx(expected, rel=0.05)


def test_restart_recovery_reproduces_energy_total(engine_db) -> None:
    """Crash/restart must not lose or double-count energy."""
    from wattmosaic_engine.aggregation.aggregator import EnergyAggregator

    db = engine_db
    results = _inference_results()
    _store(db, results)

    agg = EnergyAggregator(db)
    for r in results:
        agg.add_result(r)
    final = results[-1].timestamp
    # Flush without closing the final bucket: simulates a process killed while
    # the last bucket is still open.
    agg.flush(now=final + dt.timedelta(seconds=5))
    before = agg.energy_total("test_device")
    assert before > 0
    db.close()

    from wattmosaic_engine.storage.database import Database

    reopened = Database(db.path)
    try:
        recovered = EnergyAggregator(reopened)
        rebuilt = recovered.recompute_incomplete_buckets(
            now=final + dt.timedelta(seconds=5)
        )
        assert rebuilt, "restart recovery must rebuild at least one bucket"
        after = recovered.energy_total("test_device")
        assert after == pytest.approx(before, rel=1e-6)
        # No duplicate rows: replay upserts in place.  ``Database.energy_total``
        # now selects the smallest bucket size (mirroring the repository), so it
        # agrees with the aggregator instead of summing the same energy once per
        # resolution.
        assert after == pytest.approx(reopened.energy_total("test_device"), rel=1e-6)
    finally:
        reopened.close()


def test_restart_recovery_is_idempotent(engine_db) -> None:
    from wattmosaic_engine.aggregation.aggregator import EnergyAggregator

    db = engine_db
    results = _inference_results(duration_s=900)
    _store(db, results)
    agg = EnergyAggregator(db)
    for r in results:
        agg.add_result(r)
    final = results[-1].timestamp
    agg.flush(now=final + dt.timedelta(seconds=5))
    baseline = agg.energy_total("test_device")

    for _ in range(3):
        again = EnergyAggregator(db)
        again.recompute_incomplete_buckets(now=final + dt.timedelta(seconds=5))
        assert again.energy_total("test_device") == pytest.approx(baseline, rel=1e-9)


# --------------------------------------------------------------------------- #
# Retention
# --------------------------------------------------------------------------- #


def test_prune_removes_old_raw_rows_only(engine_db) -> None:
    from wattmosaic_engine.contracts import MeasurementType, ReaderMeasurement

    db = engine_db
    old = dt.datetime(2020, 1, 1, tzinfo=dt.UTC)
    new = dt.datetime.now(dt.UTC)

    def reading(ct_id: str, at: dt.datetime) -> ReaderMeasurement:
        return ReaderMeasurement(
            reader_id=ct_id,
            observed_at=at,
            received_at=at,
            active_power_w=1.0,
            phase="l1",
            measurement_type=MeasurementType.INSTANTANEOUS,
            calibration_version="cal-1",
            measurement_basis="ct_measured",
            sequence=1,
        )

    db.repos.samples.insert_readings([reading("ct_main_l1", old), reading("ct_main_l2", new)])
    db.upsert_energy_aggregate("test_device", old, 300, 1.0)
    db.upsert_energy_aggregate("test_device", new, 300, 2.0)

    removed = db.prune(raw_days=30, aggregate_days=730)
    assert removed["ct_samples"] == 1
    assert db.repos.samples.latest_ct_sample("ct_main_l1") is None
    assert db.repos.samples.latest_ct_sample("ct_main_l2") is not None
    # The 2020 aggregate is past the 730-day aggregate retention window and is
    # removed; only the recent one survives.
    assert db.energy_total("test_device") == pytest.approx(2.0)
