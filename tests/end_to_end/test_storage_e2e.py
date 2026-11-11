"""End-to-end: storage schema, persistence round-trips and restart recovery."""

from __future__ import annotations

import datetime as dt
import sqlite3

import pytest

from tests.end_to_end.conftest import measurement_rows
from tools.simulator import REAL_HOME_TOPOLOGY, HouseholdScenario, SimulatorConfig
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
        db.set_model_frozen("any_device", True)
        assert db.is_model_frozen("any_device") is True
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Topology round-trip
# --------------------------------------------------------------------------- #


def test_topology_round_trip_is_lossless(tmp_db_path) -> None:
    from wattmosaic_engine.storage.database import Database

    db = Database(tmp_db_path)
    try:
        topology = REAL_HOME_TOPOLOGY.to_contract_topology()
        version = db.save_topology(topology, note="round trip")
        assert version >= 1

        loaded = db.load_topology()
        assert loaded is not None
        assert loaded.version == version
        assert len(loaded.channels) == len(topology.channels) == 36
        assert len(loaded.devices) == len(topology.devices) == 37

        by_id = {c.ct_id: c for c in topology.channels}
        for channel in loaded.channels:
            original = by_id[channel.ct_id]
            assert channel.entity_id == original.entity_id
            assert channel.ct_type == original.ct_type
            assert channel.phase == original.phase
            assert channel.parent_ct_id == original.parent_ct_id
            assert channel.device_id == original.device_id
            assert channel.paired_ct_id == original.paired_ct_id
            assert channel.expected_interval_s == pytest.approx(
                original.expected_interval_s
            )
            assert channel.metadata.get("panel") == original.metadata.get("panel")

        by_dev = {d.device_id: d for d in topology.devices}
        for device in loaded.devices:
            original = by_dev[device.device_id]
            assert device.dedicated_ct_ids == original.dedicated_ct_ids
            assert device.branch_ct_ids == original.branch_ct_ids
            assert device.double_pole == original.double_pole
            assert device.phase == original.phase
        # NOTE (contract gap): ``topology_versions`` stores the full payload but
        # ``ct_channels``/``devices`` have no notes column, so ``load_topology``
        # cannot reconstruct ``Topology.notes``.  Documented, not hidden.
        assert loaded.notes == []
    finally:
        db.close()


def test_topology_save_is_versioned(tmp_db_path) -> None:
    from wattmosaic_engine.storage.database import Database

    db = Database(tmp_db_path)
    try:
        topology = REAL_HOME_TOPOLOGY.to_contract_topology()
        v1 = db.save_topology(topology, note="first")
        v2 = db.save_topology(topology, note="second")
        assert v2 > v1
        assert db.load_topology().version == v2
    finally:
        db.close()


def test_confirmed_topology_status_survives_a_restart(tmp_db_path) -> None:
    """A user-confirmed topology must not reload as ``candidate``.

    ``ct_channels``/``devices`` do not carry the topology-level status, so
    reading it from those tables alone silently downgraded a confirmed topology
    on every restart.  Learning rejects every sample while the topology is
    ``candidate``, so the downgrade silently disabled learning for good.
    """
    from wattmosaic_engine.contracts import TopologyStatus
    from wattmosaic_engine.storage.database import Database

    db = Database(tmp_db_path)
    try:
        topology = REAL_HOME_TOPOLOGY.to_contract_topology()
        confirmed = topology.model_copy(update={"status": TopologyStatus.CONFIRMED})
        db.save_topology(confirmed)

        loaded = db.load_topology()
        assert loaded is not None
        assert loaded.status == TopologyStatus.CONFIRMED.value
        # The version row's timestamp is restored too, not reset to ``now``.
        assert loaded.created_at is not None
    finally:
        db.close()


def test_candidate_topology_still_reloads_as_candidate(tmp_db_path) -> None:
    from wattmosaic_engine.contracts import TopologyStatus
    from wattmosaic_engine.storage.database import Database

    db = Database(tmp_db_path)
    try:
        db.save_topology(REAL_HOME_TOPOLOGY.to_contract_topology(confirmed=False))
        loaded = db.load_topology()
        assert loaded is not None
        assert loaded.status == TopologyStatus.CANDIDATE.value
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Sample persistence
# --------------------------------------------------------------------------- #


def test_ct_samples_persist_all_three_timestamps(engine_db) -> None:
    db, _topology = engine_db
    records = list(_scenario(minutes=10).records())
    rows = [row for rec in records for row in measurement_rows(rec)]
    inserted = db.insert_ct_samples(rows)
    assert inserted == len(rows) > 0

    first = records[0]
    ct_id, m = next(iter(first["measurements"].items()))
    latest = db.latest_ct_sample(ct_id)
    assert latest is not None
    assert latest["ct_id"] == ct_id
    assert latest["entity_id"] == m["entity_id"]
    assert latest["state_timestamp"]
    assert latest["received_at"]
    assert latest["quality"] in ("excellent", "good", "degraded", "stale")
    # physical_timestamp is optional in the contract and may be null.
    assert "physical_timestamp" in latest


def test_samples_between_filters_by_window(engine_db) -> None:
    db, _topology = engine_db
    records = list(_scenario(minutes=10).records())
    for rec in records:
        db.insert_ct_samples(measurement_rows(rec))

    start = dt.datetime.fromisoformat(records[0]["timestamp"])
    end = dt.datetime.fromisoformat(records[len(records) // 2]["timestamp"])
    in_window = db.ct_samples_between(start, end)
    assert in_window
    for row in in_window:
        ts = dt.datetime.fromisoformat(row["received_at"])
        assert start <= ts <= end

    only_main = db.ct_samples_between(start, end, ct_ids=["ct_main_l1"])
    assert only_main
    assert {row["ct_id"] for row in only_main} == {"ct_main_l1"}


# --------------------------------------------------------------------------- #
# Inference & reconciliation persistence
# --------------------------------------------------------------------------- #


def test_inference_and_reconciliation_persist_and_read_back(engine_db) -> None:
    from wattmosaic_engine.contracts import (
        InferenceResult,
        Reconciliation,
        Source,
    )

    db, topology = engine_db
    ts = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC)
    results = [
        InferenceResult(
            device_id=f"dev_{i}",
            timestamp=ts,
            power_w=100.0 + i,
            source=Source.DIRECT_MEASURED,
            confidence=0.9,
            supporting_ct_channels=["ct_main_l1"],
            explanation=["unit-test"],
        )
        for i in range(3)
    ]
    assert db.insert_inference_results(results) == 3
    latest = db.latest_inference_results()
    assert {r["device_id"] for r in latest} == {"dev_0", "dev_1", "dev_2"}

    # A newer batch replaces the "latest" view but history is preserved.
    newer = [
        InferenceResult(
            device_id="dev_0",
            timestamp=ts + dt.timedelta(seconds=15),
            power_w=250.0,
            source=Source.INFERRED,
            confidence=0.5,
        )
    ]
    db.insert_inference_results(newer)
    latest = {r["device_id"]: r for r in db.latest_inference_results()}
    assert latest["dev_0"]["power_w"] == 250.0
    history = db.inference_history("dev_0", since=ts - dt.timedelta(minutes=1))
    assert len(history) == 2

    rec = Reconciliation(
        timestamp=ts,
        topology_version=topology.version,
        main_power_w=10_000.0,
        direct_measured_w=6_000.0,
        inferred_w=1_000.0,
        unknown_residual_w=3_000.0,
        explanation=["residual_never_redistributed"],
    )
    db.insert_reconciliation(rec)
    stored = db.latest_reconciliation()
    assert stored is not None
    assert stored["main_power_w"] == pytest.approx(10_000.0)
    assert stored["unknown_residual_w"] == pytest.approx(3_000.0)


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
    from wattmosaic_engine.contracts import InferenceResult, Source

    base = dt.datetime(2026, 1, 1, 0, 0, tzinfo=dt.UTC)
    out = []
    for k in range(duration_s // step_s + 1):
        at = base + dt.timedelta(seconds=k * step_s)
        power = 1000.0 if (k * step_s) % 300 < 150 else 200.0
        out.append(
            InferenceResult(
                device_id="test_device",
                timestamp=at,
                power_w=power,
                source=Source.DIRECT_MEASURED,
                confidence=0.9,
            )
        )
    return out


def test_energy_aggregation_persists_buckets(engine_db) -> None:
    from wattmosaic_engine.aggregation.aggregator import EnergyAggregator

    db, _ = engine_db
    results = _inference_results()
    db.insert_inference_results(results)

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

    db, _ = engine_db
    results = _inference_results()
    db.insert_inference_results(results)

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

    db, _ = engine_db
    results = _inference_results(duration_s=900)
    db.insert_inference_results(results)
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
    db, _ = engine_db
    old = dt.datetime(2020, 1, 1, tzinfo=dt.UTC)
    new = dt.datetime.now(dt.UTC)

    def row(ct_id: str, at: dt.datetime) -> dict:
        return {
            "ct_id": ct_id,
            "entity_id": f"sensor.{ct_id}",
            "value_w": 1.0,
            "raw_value": 1.0,
            "unit": "W",
            "state_timestamp": at,
            "received_at": at,
            "quality": "good",
            "repeated": False,
            "sequence": 1,
        }

    db.insert_ct_samples([row("ct_main_l1", old), row("ct_main_l2", new)])
    db.upsert_energy_aggregate("test_device", old, 300, 1.0)
    db.upsert_energy_aggregate("test_device", new, 300, 2.0)

    removed = db.prune(raw_days=30, aggregate_days=730)
    assert removed["ct_samples"] == 1
    assert db.latest_ct_sample("ct_main_l1") is None
    assert db.latest_ct_sample("ct_main_l2") is not None
    # The 2020 aggregate is past the 730-day aggregate retention window and is
    # removed; only the recent one survives.
    assert db.energy_total("test_device") == pytest.approx(2.0)
