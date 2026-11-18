"""Migration v017: nullable inference results, snapshots and group estimates (S17).

The fixture is a genuine pre-migration database built with raw ``sqlite3``:
migrations 1-16 plus legacy ``inference_results`` / ``reconciliation_results``
rows, with ``user_version`` left at 16.  ``Database.__init__`` migrates
immediately, so it cannot produce a pre-migration fixture; the repository is
reached through ``db.repos.inference``.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from wattmosaic_engine.contracts import (
    Bounds,
    ConfidenceVectorV3,
    DataQualityReport,
    DeviceEstimate,
    DeviceGroupEstimate,
    EstimateStatus,
    EventGroupStatusV3,
    EvidenceRef,
    IdentifiabilityStatus,
    InferenceMethod,
    InferenceSnapshot,
    JointInferenceResult,
    ReconciliationV3,
    VersionTriple,
)
from wattmosaic_engine.storage.database import Database
from wattmosaic_engine.storage.migrations import (
    LATEST_VERSION,
    MIGRATIONS,
    current_version,
)
from wattmosaic_engine.storage.migrations.v017_results import (
    INFERENCE_COLUMNS,
    RECONCILIATION_COLUMNS,
    TIERS,
)
from wattmosaic_engine.storage.migrations.v017_results import (
    MIGRATION as V017,
)

PRE_MIGRATION_VERSION = 16
NOW = datetime(2026, 11, 1, tzinfo=UTC)

_LEGACY_INFERENCE_INSERT = """INSERT INTO inference_results(
    device_id, timestamp, power_w, energy_kwh, source, confidence, sample_count,
    quality_status, measurement_age_seconds, supporting_ct_channels_json,
    explanation_json, provenance_json
) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)"""


def _build_pre_migration_db(path: Path) -> sqlite3.Connection:
    """Apply migrations 1-16, seed legacy rows, stamp version 16.

    The wave-2 migrations 13-16 are applied when present in this tree, but the
    fixture works from the schema-11 base too: it only needs the two legacy
    tables, which are created by migration 3 and never dropped before v17.
    """
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
            _LEGACY_INFERENCE_INSERT,
            (
                "placeholder",
                "2026-10-31T23:00:00+00:00",
                0.0,
                0.0,
                "inferred",
                0.5,
                1,
                "good",
                0.0,
                "[]",
                "[]",
                json.dumps({"power_w_is_placeholder": True}),
            ),
        )
        conn.execute(
            _LEGACY_INFERENCE_INSERT,
            (
                "measured",
                "2026-10-31T23:00:00+00:00",
                42.5,
                0.0,
                "direct_measured",
                0.9,
                1,
                "good",
                0.0,
                "[]",
                "[]",
                "{}",
            ),
        )
        conn.execute(
            _LEGACY_INFERENCE_INSERT,
            (
                "derived",
                "2026-10-31T23:00:00+00:00",
                12.5,
                0.0,
                "inferred",
                0.7,
                1,
                "good",
                0.0,
                "[]",
                "[]",
                json.dumps({"anything": "else"}),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return conn


def _versions() -> VersionTriple:
    return VersionTriple(
        topology_version_id="topo-1",
        mapping_version_id="map-1",
        model_set_id="mset-abc",
    )


def _estimate(
    subject_id: str,
    point_w: float | None,
    identifiability: IdentifiabilityStatus,
    status: EstimateStatus,
    group_id: str | None = None,
) -> DeviceEstimate:
    return DeviceEstimate(
        subject_kind="device",
        subject_id=subject_id,
        point_w=point_w,
        bounds=Bounds(lower_w=0.0, upper_w=100.0, basis="non_negativity"),
        status=status,
        identifiability=identifiability,
        group_id=group_id,
        method=InferenceMethod.JOINT_DELTA,
        confidence=ConfidenceVectorV3(identifiability=0.8),
        source_reader_ids=["reader-1"],
        evidence_refs=[EvidenceRef(kind="sample", id="sample-1")],
        versions=_versions(),
        updated_at=NOW,
        provisional=False,
    )


def _result(*estimates: DeviceEstimate) -> JointInferenceResult:
    return JointInferenceResult(
        event_group_id="group-1",
        event_group_revision=1,
        status=EventGroupStatusV3.CONFIRMED,
        device_estimates=list(estimates),
        group_estimates=[],
        unknown_estimates=[],
        candidates=[],
        matches={},
        residual_w_by_reader={},
        versions=_versions(),
        computed_at=NOW,
    )


def _snapshot(*estimates: DeviceEstimate, snapshot_id: str = "snap-1") -> InferenceSnapshot:
    return InferenceSnapshot(
        snapshot_id=snapshot_id,
        observed_at=NOW,
        computed_at=NOW,
        versions=_versions(),
        reader_measurements=[],
        device_estimates=list(estimates),
        group_estimates=[],
        residual_estimates=[],
        reconciliation=ReconciliationV3(main_w=100.0),
        system_quality=DataQualityReport(),
        stale_sources=[],
        unresolved_events=[],
    )


def _open(path: Path) -> Database:
    db = Database(path)
    assert db.schema_version == LATEST_VERSION
    return db


# --------------------------------------------------------------------------- #
# Migration contract, legacy data rules and idempotency / atomicity
# --------------------------------------------------------------------------- #


def test_module_declares_the_reserved_version_and_tiers() -> None:
    assert V017.version == 17
    assert TIERS == {
        "inference_snapshots": "L2",
        "device_group_estimates": "L1",
    }


def test_placeholder_rows_become_null_and_flagged(tmp_path: Path) -> None:
    path = tmp_path / "placeholder.db"
    _build_pre_migration_db(path)

    db = _open(path)
    try:
        row = db._conn.execute(
            "SELECT power_w, legacy_semantics FROM inference_results "
            "WHERE device_id='placeholder'"
        ).fetchone()
        assert row["power_w"] is None
        assert row["legacy_semantics"] == "placeholder_zero"
    finally:
        db.close()


def test_a_non_placeholder_legacy_value_is_unchanged_but_flagged(tmp_path: Path) -> None:
    path = tmp_path / "non_placeholder.db"
    _build_pre_migration_db(path)

    db = _open(path)
    try:
        measured = db._conn.execute(
            "SELECT power_w, legacy_semantics FROM inference_results "
            "WHERE device_id='measured'"
        ).fetchone()
        assert measured["power_w"] == 42.5
        assert measured["legacy_semantics"] == "legacy_direct"

        derived = db._conn.execute(
            "SELECT power_w, legacy_semantics FROM inference_results "
            "WHERE device_id='derived'"
        ).fetchone()
        assert derived["power_w"] == 12.5
        assert derived["legacy_semantics"] == "legacy_v2"
    finally:
        db.close()


def test_column_order_and_indexes_survive_the_rebuild(tmp_path: Path) -> None:
    path = tmp_path / "shape.db"
    _build_pre_migration_db(path)

    db = _open(path)
    try:
        inf_columns = [row[1] for row in db._conn.execute("PRAGMA table_info(inference_results)")]
        rec_columns = [
            row[1] for row in db._conn.execute("PRAGMA table_info(reconciliation_results)")
        ]
        assert tuple(inf_columns) == INFERENCE_COLUMNS
        assert tuple(rec_columns) == RECONCILIATION_COLUMNS
        inf_indexes = {row[1] for row in db._conn.execute("PRAGMA index_list(inference_results)")}
        assert {
            "idx_inference_device_time",
            "idx_inference_time",
            "idx_inference_tier",
        } <= inf_indexes
        rec_indexes = {
            row[1] for row in db._conn.execute("PRAGMA index_list(reconciliation_results)")
        }
        assert "idx_reconciliation_time" in rec_indexes

        # power_w is now nullable; every new column is present.
        not_null = {
            row[1]
            for row in db._conn.execute("PRAGMA table_info(inference_results)")
            if row[3]
        }
        assert "power_w" not in not_null
        assert "group_w" in rec_columns
    finally:
        db.close()


def test_apply_callable_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "idempotent.db"
    _build_pre_migration_db(path)

    # Invoke the migration's own callable twice on the same connection; the
    # second call must be a true no-op (no table rebuild, no row rewrite).
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        assert current_version(conn) == PRE_MIGRATION_VERSION
        assert V017.apply is not None
        V017.apply(conn)
        first = conn.execute(
            "SELECT id, device_id, power_w, legacy_semantics FROM inference_results "
            "ORDER BY id"
        ).fetchall()
        schema_first = {
            table: [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
            for table in ("inference_results", "reconciliation_results")
        }
        V017.apply(conn)
        second = conn.execute(
            "SELECT id, device_id, power_w, legacy_semantics FROM inference_results "
            "ORDER BY id"
        ).fetchall()
        schema_second = {
            table: [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
            for table in ("inference_results", "reconciliation_results")
        }
        assert [dict(r) for r in first] == [dict(r) for r in second]
        assert schema_first == schema_second
    finally:
        conn.close()


def test_failure_mid_apply_leaves_user_version_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "failure.db"
    _build_pre_migration_db(path)

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        assert current_version(conn) == PRE_MIGRATION_VERSION
        conn.execute("BEGIN IMMEDIATE")
        try:
            assert V017.apply is not None
            V017.apply(conn)
            # Inject a failure after the schema changes but before COMMIT;
            # apply_migrations' ROLLBACK must restore user_version to 16.
            raise RuntimeError("injected failure")
        except RuntimeError:
            conn.execute("ROLLBACK")
        assert current_version(conn) == PRE_MIGRATION_VERSION
        # The rebuild was rolled back too: no marker column, original NOT NULL.
        columns = [row[1] for row in conn.execute("PRAGMA table_info(inference_results)")]
        assert "legacy_semantics" not in columns
    finally:
        conn.close()

    # A clean apply still works after a rolled-back attempt.
    db = _open(path)
    try:
        assert db.schema_version == LATEST_VERSION
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Repository: v3 estimates, snapshots and group history
# --------------------------------------------------------------------------- #


def test_a_null_round_trips_as_none(tmp_path: Path) -> None:
    path = tmp_path / "roundtrip.db"
    _build_pre_migration_db(path)
    db = _open(path)
    try:
        unknown = _estimate(
            "member",
            None,
            IdentifiabilityStatus.NON_IDENTIFIABLE,
            EstimateStatus.UNKNOWN,
            group_id="group-1",
        )
        assert db.repos.inference.put_estimates(_result(unknown)) == 1
        rows = db.repos.inference.latest_estimates()
        assert len(rows) == 1
        assert rows[0]["power_w"] is None
        assert rows[0]["status"] == "unknown"
        assert rows[0]["identifiability"] == "non_identifiable"
        assert rows[0]["group_id"] == "group-1"
        assert rows[0]["lower_w"] == 0.0
        assert rows[0]["upper_w"] == 100.0
        assert rows[0]["estimate"]["point_w"] is None
    finally:
        db.close()


def test_estimates_persist_every_estimate_field(tmp_path: Path) -> None:
    path = tmp_path / "fields.db"
    _build_pre_migration_db(path)
    db = _open(path)
    try:
        estimate = _estimate(
            "dev-1",
            55.0,
            IdentifiabilityStatus.IDENTIFIABLE,
            EstimateStatus.ESTIMATED,
        )
        db.repos.inference.put_estimates(_result(estimate))
        row = db.repos.inference.latest_estimates()[0]
        assert row["power_w"] == 55.0
        assert row["method"] == "joint_delta"
        assert row["level"] is None
        assert row["topology_version_id"] == "topo-1"
        assert row["mapping_version_id"] == "map-1"
        assert row["model_set_id"] == "mset-abc"
        assert row["provisional"] == 0
        assert row["estimate"]["versions"]["topology_version_id"] == "topo-1"
        assert row["estimate"]["evidence_refs"] == [{"kind": "sample", "id": "sample-1"}]
    finally:
        db.close()


def test_a_duplicate_snapshot_is_not_written_twice(tmp_path: Path) -> None:
    path = tmp_path / "snapshot.db"
    _build_pre_migration_db(path)
    db = _open(path)
    try:
        snapshot = _snapshot(_estimate("dev-1", 10.0, IdentifiabilityStatus.IDENTIFIABLE, EstimateStatus.ESTIMATED))
        assert db.repos.inference.put_snapshot(snapshot) is True
        assert db.repos.inference.put_snapshot(snapshot) is False
        count = db._conn.execute("SELECT COUNT(*) FROM inference_snapshots").fetchone()[0]
        assert count == 1

        latest = db.repos.inference.latest_snapshot()
        assert latest is not None
        assert latest["snapshot_id"] == "snap-1"
        assert latest["payload"]["reconciliation"]["main_w"] == 100.0
    finally:
        db.close()


def test_snapshot_writes_again_when_content_changes(tmp_path: Path) -> None:
    path = tmp_path / "snapshot_change.db"
    _build_pre_migration_db(path)
    db = _open(path)
    try:
        assert db.repos.inference.put_snapshot(_snapshot()) is True
        changed = _snapshot(
            _estimate("dev-1", 10.0, IdentifiabilityStatus.IDENTIFIABLE, EstimateStatus.ESTIMATED),
            snapshot_id="snap-2",
        )
        assert db.repos.inference.put_snapshot(changed) is True
        count = db._conn.execute("SELECT COUNT(*) FROM inference_snapshots").fetchone()[0]
        assert count == 2
        assert db.repos.inference.latest_snapshot()["snapshot_id"] == "snap-2"
    finally:
        db.close()


def test_group_history_is_ordered_and_filtered(tmp_path: Path) -> None:
    path = tmp_path / "groups.db"
    _build_pre_migration_db(path)
    db = _open(path)
    try:
        group = DeviceGroupEstimate(
            group_id="group-1",
            member_ids=["a", "b"],
            point_w=60.0,
            bounds=Bounds(lower_w=50.0, upper_w=70.0, basis="group_sum"),
            identifiability=IdentifiabilityStatus.GROUP_IDENTIFIABLE,
            source_reader_ids=["reader-1"],
            evidence_refs=[EvidenceRef(kind="event_group", id="eg-1")],
            versions=_versions(),
            updated_at=NOW,
        )
        db.repos.inference.put_group_estimate(group)

        history = db.repos.inference.group_history("group-1", NOW - timedelta(days=1))
        assert len(history) == 1
        assert history[0]["point_w"] == 60.0
        assert history[0]["estimate"]["member_ids"] == ["a", "b"]
        assert db.repos.inference.group_history("other", NOW - timedelta(days=1)) == []
    finally:
        db.close()


def test_reconciliation_round_trip_writes_every_column(tmp_path: Path) -> None:
    """The pre-v17 INSERT bound 20 values into 21 placeholders and failed at
    runtime; the rebuilt path writes and reads back every column, including the
    new nullable ``group_w``.
    """
    from wattmosaic_engine.contracts import Reconciliation

    path = tmp_path / "reconciliation.db"
    _build_pre_migration_db(path)
    db = _open(path)
    try:
        rec = Reconciliation(
            timestamp=NOW,
            topology_version=1,
            main_power_w=100.0,
            l1_power_w=20.0,
            l2_power_w=30.0,
            direct_measured_w=10.0,
            inferred_w=10.0,
            model_estimated_w=10.0,
            configured_prior_w=5.0,
            unknown_residual_w=5.0,
            unmonitored_w=2.0,
            meter_disagreement_w=1.0,
            meter_spread_w=0.5,
            meter_spread_exceeds_residual=True,
            reconciliation_error_w=0.25,
            reconciliation_error_ratio=0.01,
            accounted_fraction=0.99,
            measurement_uncertainty_w=1.5,
            quality_status="good",
            explanation=["balanced"],
            topology_confirmed=True,
        )
        db.insert_reconciliation(rec)
        stored = db.latest_reconciliation()
        assert stored is not None
        assert stored["main_power_w"] == 100.0
        assert stored["configured_prior_w"] == 5.0
        assert stored["unmonitored_w"] == 2.0
        assert stored["meter_spread_exceeds_residual"] == 1
        assert stored["topology_confirmed"] == 1
        assert stored["group_w"] is None
        assert json.loads(stored["explanation_json"]) == ["balanced"]
    finally:
        db.close()


def test_migration_is_part_of_the_discovered_registry() -> None:
    # Discovery loads the module under a private name, so the registry holds a
    # distinct Migration instance -- identity is by version, not object equality.
    assert any(m.version == V017.version and m.name == V017.name for m in MIGRATIONS)
