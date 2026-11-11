"""Contract and storage tests (orchestrator-owned).

These guard the interfaces every other module depends on.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from wattmosaic_engine import contracts
from wattmosaic_engine.contracts import (
    CTChannel,
    CTType,
    DeviceTopology,
    InferenceResult,
    Measurement,
    Phase,
    Polarity,
    Reconciliation,
    ServiceCommand,
    Source,
    Topology,
    TopologyStatus,
)
from wattmosaic_engine.storage import LATEST_VERSION, Database
from wattmosaic_engine.storage.migrations import MIGRATIONS, apply_migrations, current_version

ROOT = Path(__file__).resolve().parents[2]


def test_contract_version_present() -> None:
    assert contracts.CONTRACT_VERSION == "1.3.0"


def test_schema_files_match_contract_version() -> None:
    index = json.loads((ROOT / "data" / "schemas" / "index.json").read_text())
    assert index["contract_version"] == contracts.CONTRACT_VERSION
    assert "inference_result" in index["schemas"]


def test_measurement_kw_conversion_and_polarity() -> None:
    m = Measurement(
        entity_id="sensor.x",
        ct_id="c",
        value=1.5,
        unit="kW",
        state_timestamp=datetime.now(UTC),
    )
    assert m.effective_power_w() == pytest.approx(1500.0)


def test_measurement_negative_power_preserved() -> None:
    m = Measurement(
        entity_id="sensor.x",
        ct_id="c",
        value=-420.0,
        state_timestamp=datetime.now(UTC),
    )
    assert m.effective_power_w() == -420.0


def test_inverted_polarity_flips_sign() -> None:
    m = Measurement(
        entity_id="sensor.x",
        ct_id="c",
        value=420.0,
        polarization=Polarity.INVERTED,
        state_timestamp=datetime.now(UTC),
    )
    assert m.effective_power_w() == -420.0


def test_measurement_rejects_non_finite() -> None:
    with pytest.raises(ValueError):
        Measurement(
            entity_id="sensor.x",
            ct_id="c",
            value=float("inf"),
            state_timestamp=datetime.now(UTC),
        )


def test_confidence_bounds() -> None:
    with pytest.raises(ValueError):
        InferenceResult(
            device_id="d",
            timestamp=datetime.now(UTC),
            power_w=1.0,
            source=Source.INFERRED,
            confidence=1.5,
        )


def test_confidence_defaults_uncalibrated() -> None:
    r = InferenceResult(
        device_id="d",
        timestamp=datetime.now(UTC),
        power_w=10.0,
        source=Source.INFERRED,
        confidence=0.5,
    )
    assert r.confidence_is_calibrated is False


def test_contracts_forbid_unknown_fields() -> None:
    with pytest.raises(ValueError):
        ServiceCommand(command="start_learning", bogus_field=1)  # type: ignore[call-arg]


def test_service_command_enum_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        ServiceCommand(command="delete_everything")  # type: ignore[arg-type]


def test_topology_helpers() -> None:
    t = Topology(
        channels=[CTChannel(ct_id="a", entity_id="sensor.a", ct_type=CTType.MAIN, phase=Phase.L1)],
        devices=[DeviceTopology(device_id="d1", dedicated_ct_ids=["a"])],
    )
    assert t.channel_map()["a"].entity_id == "sensor.a"
    assert t.device_map()["d1"].device_id == "d1"


def test_reconciliation_keeps_residual_separate() -> None:
    r = Reconciliation(
        main_power_w=1000.0,
        direct_measured_w=300.0,
        inferred_w=200.0,
        unknown_residual_w=500.0,
    )
    assert r.unknown_residual_w == 500.0
    assert r.direct_measured_w + r.inferred_w + r.unknown_residual_w == r.main_power_w


# --------------------------- storage ------------------------------------- #


def test_migrations_idempotent(tmp_path: Path) -> None:
    db = Database(tmp_path / "a.db")
    try:
        assert db.schema_version == LATEST_VERSION
        assert apply_migrations(db._conn) == LATEST_VERSION
        assert current_version(db._conn) == LATEST_VERSION
    finally:
        db.close()


def test_migration_versions_are_sequential() -> None:
    versions = [m.version for m in MIGRATIONS]
    assert versions == sorted(versions)
    assert versions == list(range(1, len(versions) + 1))


def test_wal_enabled(tmp_path: Path) -> None:
    db = Database(tmp_path / "w.db")
    try:
        mode = db._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
    finally:
        db.close()


def _topology() -> Topology:
    return Topology(
        status=TopologyStatus.CANDIDATE,
        channels=[
            CTChannel(ct_id="main_l1", entity_id="sensor.main_l1", ct_type=CTType.MAIN, phase=Phase.L1),
            CTChannel(ct_id="br1", entity_id="sensor.br1", ct_type=CTType.BRANCH, phase=Phase.L1,
                      parent_ct_id="main_l1"),
            CTChannel(
                ct_id="panel_l1", entity_id="sensor.panel_l1", ct_type=CTType.MAIN, phase=Phase.L1,
                alias_of_ct_id="main_l1", measurement_multiplier=2.5,
            ),
        ],
        devices=[DeviceTopology(device_id="dev1", dedicated_ct_ids=["br1"])],
    )


def test_topology_roundtrip_preserves_alias_and_multiplier(tmp_path: Path) -> None:
    """Storage must not silently drop declaration fields it has no column for.

    ``alias_of_ct_id`` and ``measurement_multiplier`` existed on the contract
    but had no ``ct_channels`` column, so ``save_topology`` wrote an explicit
    column list without them and ``load_topology`` rebuilt every channel with
    the defaults.  A user's alias declaration therefore never reached the
    compute path: the whole-home total kept summing a wire measured twice.
    """
    db = Database(tmp_path / "t.db")
    try:
        db.save_topology(_topology())
        loaded = db.load_topology()
        assert loaded is not None
        by_id = loaded.channel_map()
        assert by_id["panel_l1"].alias_of_ct_id == "main_l1"
        assert by_id["panel_l1"].measurement_multiplier == 2.5
        # Undeclared channels keep the neutral default, not a stray value.
        assert by_id["main_l1"].alias_of_ct_id is None
        assert by_id["main_l1"].measurement_multiplier == 1.0
    finally:
        db.close()


def test_saved_alias_removes_the_duplicate_feed(tmp_path: Path) -> None:
    """The persisted declaration must change the computed whole-home total."""
    from wattmosaic_engine.electrical.topology import build_graph

    db = Database(tmp_path / "t.db")
    try:
        db.save_topology(_topology())
        loaded = db.load_topology()
        assert loaded is not None
        graph = build_graph(loaded)
        # ``panel_l1`` is the alias of ``main_l1``: only the canonical wire feeds
        # the total.  Without the fix both mains would appear here.
        assert graph.effective_feeds() == ["main_l1"]
    finally:
        db.close()


def test_save_topology_accepts_child_before_parent(tmp_path: Path) -> None:
    """A nested channel may be listed before its parent.

    ``ct_channels.parent_ct_id`` is a self-referencing foreign key and
    ``PRAGMA foreign_keys=ON`` is set.  Inserting in payload order therefore
    aborted the whole write with ``FOREIGN KEY constraint failed`` whenever a
    sub-panel feed came before the service feed -- which is exactly the order
    the wizard sends channels in.  Nesting could never be saved at all.
    """
    db = Database(tmp_path / "t.db")
    try:
        child_first = Topology(
            status=TopologyStatus.CANDIDATE,
            channels=[
                CTChannel(
                    ct_id="sub_l1", entity_id="sensor.sub_l1", ct_type=CTType.MAIN,
                    phase=Phase.L1, parent_ct_id="service_l1",
                ),
                CTChannel(
                    ct_id="service_l1", entity_id="sensor.service_l1",
                    ct_type=CTType.MAIN, phase=Phase.L1,
                ),
            ],
        )
        db.save_topology(child_first)
        loaded = db.load_topology()
        assert loaded is not None
        assert loaded.channel_map()["sub_l1"].parent_ct_id == "service_l1"
    finally:
        db.close()


def test_save_topology_rejects_a_parent_cycle_without_corrupting_state(tmp_path: Path) -> None:
    """An impossible cycle must fail atomically, leaving the old topology intact.

    ``ct_channels.parent_ct_id`` is a self-referencing foreign key, so a cycle
    cannot be stored.  ``TopologyGraph`` treats it as a problem rather than a
    relationship, and the config wizard already refuses to submit one; what
    matters at the storage boundary is that the rejected write rolls back
    instead of leaving the installation with no topology at all.
    """
    db = Database(tmp_path / "t.db")
    try:
        db.save_topology(_topology())
        before = db.load_topology()
        assert before is not None

        cyclic = Topology(
            status=TopologyStatus.CANDIDATE,
            channels=[
                CTChannel(
                    ct_id="a", entity_id="sensor.a", ct_type=CTType.MAIN,
                    phase=Phase.L1, parent_ct_id="b",
                ),
                CTChannel(
                    ct_id="b", entity_id="sensor.b", ct_type=CTType.MAIN,
                    phase=Phase.L1, parent_ct_id="a",
                ),
            ],
        )
        with pytest.raises(sqlite3.IntegrityError):
            db.save_topology(cyclic)

        after = db.load_topology()
        assert after is not None
        assert {c.ct_id for c in after.channels} == {
            c.ct_id for c in before.channels
        }
    finally:
        db.close()


def test_topology_roundtrip(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.db")
    try:
        version = db.save_topology(_topology())
        assert version >= 1
        loaded = db.load_topology()
        assert loaded is not None
        assert {c.ct_id for c in loaded.channels} == {"main_l1", "br1", "panel_l1"}
        assert loaded.device_map()["dev1"].dedicated_ct_ids == ["br1"]
    finally:
        db.close()


def test_ct_sample_insert_and_latest(tmp_path: Path) -> None:
    db = Database(tmp_path / "s.db")
    now = datetime.now(UTC)
    try:
        n = db.insert_ct_samples(
            [
                {
                    "ct_id": "main_l1",
                    "entity_id": "sensor.main_l1",
                    "value_w": 100.0,
                    "raw_value": 100.0,
                    "unit": "W",
                    "state_timestamp": now,
                    "received_at": now,
                    "quality": "good",
                    "repeated": False,
                    "sequence": 1,
                },
                {
                    "ct_id": "main_l1",
                    "entity_id": "sensor.main_l1",
                    "value_w": 120.0,
                    "raw_value": 120.0,
                    "unit": "W",
                    "state_timestamp": now + timedelta(seconds=15),
                    "received_at": now + timedelta(seconds=15),
                    "quality": "good",
                    "repeated": False,
                    "sequence": 2,
                },
            ]
        )
        assert n == 2
        latest = db.latest_ct_sample("main_l1")
        assert latest is not None
        assert latest["value_w"] == pytest.approx(120.0)
    finally:
        db.close()


def test_inference_results_roundtrip_and_provenance(tmp_path: Path) -> None:
    db = Database(tmp_path / "i.db")
    now = datetime.now(UTC)
    try:
        db.insert_inference_results(
            [
                InferenceResult(
                    device_id="dev1",
                    timestamp=now,
                    power_w=42.0,
                    source=Source.INFERRED,
                    confidence=0.7,
                    supporting_ct_channels=["br1"],
                    explanation=["branch_power_increase"],
                    provenance={"window_s": 30},
                )
            ]
        )
        rows = db.latest_inference_results()
        assert len(rows) == 1
        assert json.loads(rows[0]["explanation_json"]) == ["branch_power_increase"]
        assert json.loads(rows[0]["provenance_json"])["window_s"] == 30
    finally:
        db.close()


def test_learning_sample_rejection_reason_persisted(tmp_path: Path) -> None:
    db = Database(tmp_path / "l.db")
    try:
        from wattmosaic_engine.contracts import LearningSample, SampleRejection

        sample = LearningSample(
            sample_id="s1",
            device_id="dev1",
            captured_at=datetime.now(UTC),
            delta_power_w=10.0,
            accepted=False,
            rejection_reason=SampleRejection.STALE_MEASUREMENT,
        )
        db.insert_learning_sample(sample)
        all_rows = db.learning_samples("dev1", accepted_only=False)
        assert all_rows[0]["rejection_reason"] == "stale_measurement"
        assert db.count_learning_samples("dev1", accepted_only=True) == 0
    finally:
        db.close()


def test_model_promotion_and_rollback(tmp_path: Path) -> None:
    db = Database(tmp_path / "m.db")
    try:
        from wattmosaic_engine.contracts import DeviceModelVersion, ModelStatus, ModelType

        for version, r2 in ((1, 0.8), (2, 0.9)):
            db.save_model_version(
                DeviceModelVersion(
                    device_id="dev1",
                    model_type=ModelType.BINARY,
                    version=version,
                    status=ModelStatus.CANDIDATE,
                    validation_metrics={"r2": r2},
                ),
                activate=True,
            )
        active = db.active_model("dev1", "binary")
        assert active is not None and active["version"] == 2
        previous = db.rollback_model("dev1", "binary")
        assert previous == 1
        active = db.active_model("dev1", "binary")
        assert active is not None and active["version"] == 1
    finally:
        db.close()


def test_freezing_model(tmp_path: Path) -> None:
    db = Database(tmp_path / "f.db")
    try:
        db.set_model_frozen("dev1", True)
        assert db.is_model_frozen("dev1") is True
        db.set_model_frozen("dev1", False)
        assert db.is_model_frozen("dev1") is False
    finally:
        db.close()


def test_prune_removes_old_rows_only(tmp_path: Path) -> None:
    db = Database(tmp_path / "p.db")
    old = datetime.now(UTC) - timedelta(days=400)
    new = datetime.now(UTC)
    try:
        db.insert_ct_samples(
            [
                {
                    "ct_id": "c", "entity_id": "sensor.c", "value_w": 1.0, "raw_value": 1.0,
                    "unit": "W", "state_timestamp": old, "received_at": old,
                },
                {
                    "ct_id": "c", "entity_id": "sensor.c", "value_w": 2.0, "raw_value": 2.0,
                    "unit": "W", "state_timestamp": new, "received_at": new,
                },
            ]
        )
        removed = db.prune(raw_days=30, aggregate_days=730)
        assert removed["ct_samples"] == 1
        assert len(db.ct_samples_between(new - timedelta(days=1), new + timedelta(days=1))) == 1
    finally:
        db.close()


def test_corrupt_database_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "corrupt.db"
    path.write_bytes(b"not a sqlite database at all" * 10)
    # sqlite3 raises DatabaseError ("file is not a database"); the storage layer
    # must not swallow it, because silently starting on a corrupt file would
    # destroy historical data.
    with pytest.raises(sqlite3.DatabaseError):
        Database(path)


def test_export_device_data_shape(tmp_path: Path) -> None:
    db = Database(tmp_path / "e.db")
    try:
        out = db.export_device_data("nope")
        assert out["device_id"] == "nope"
        assert out["inference_results"] == []
        assert "exported_at" in out
    finally:
        db.close()


def test_integrity_check_reports_ok(tmp_path: Path) -> None:
    db = Database(tmp_path / "ic.db")
    try:
        report = db.integrity_check()
        assert report["integrity"] == "ok"
        assert report["schema_version"] == LATEST_VERSION
    finally:
        db.close()


def test_no_secret_like_columns_in_schema(tmp_path: Path) -> None:
    db = Database(tmp_path / "sec.db")
    try:
        rows = db._conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        names = {r[0] for r in rows}
        assert not any("token" in n.lower() or "secret" in n.lower() for n in names)
        for n in names:
            cols = [c[1].lower() for c in db._conn.execute(f"PRAGMA table_info({n})").fetchall()]
            assert not any("token" in c or "secret" in c for c in cols), (n, cols)
    finally:
        db.close()
