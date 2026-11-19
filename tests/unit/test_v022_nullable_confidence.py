"""Migration v022: ``inference_results.confidence`` becomes nullable (ZF).

Regression: a valid :class:`DeviceEstimate` whose
:class:`ConfidenceVectorV3` components are all ``None`` used to raise
``IntegrityError`` on the ``confidence REAL NOT NULL`` column left by v017.
HB §18 says absent is ``None``, never ``0`` -- so the column is rebuilt nullable
and the repository writes ``NULL`` when identifiability was not assessed.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from wattmosaic_engine.contracts import (
    Bounds,
    ConfidenceVectorV3,
    DeviceEstimate,
    EstimateStatus,
    EventGroupStatus,
    EvidenceRef,
    IdentifiabilityStatus,
    InferenceMethod,
    JointInferenceResult,
    VersionTriple,
)
from wattmosaic_engine.storage import LATEST_VERSION
from wattmosaic_engine.storage.database import Database
from wattmosaic_engine.storage.migrations import MIGRATIONS, current_version
from wattmosaic_engine.storage.migrations.v017_results import INFERENCE_COLUMNS
from wattmosaic_engine.storage.migrations.v022_nullable_confidence import (
    MIGRATION as V022,
)
from wattmosaic_engine.storage.migrations.v022_nullable_confidence import (
    TIERS,
)

PRE_MIGRATION_VERSION = 21
PRE_INFERENCE_INSERT = """
INSERT INTO inference_results(
    device_id, timestamp, power_w, energy_kwh, source, confidence, sample_count,
    quality_status, measurement_age_seconds, supporting_ct_channels_json,
    explanation_json, provenance_json, legacy_semantics
) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
"""
NOW = datetime(2026, 11, 1, tzinfo=UTC)
VERS = VersionTriple(
    topology_version_id="topo-1", mapping_version_id="map-1", model_set_id="mset-1"
)


def _build_pre_migration_db(path: Path) -> None:
    """Real migrations up to v021, then seed one legacy row, stamp version 21."""
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
            PRE_INFERENCE_INSERT,
            (
                "old-dev",
                "2026-10-31T23:00:00+00:00",
                42.5,
                0.0,
                "inferred",
                0.7,
                1,
                "good",
                0.0,
                "[]",
                "[]",
                "{}",
                "legacy_v2",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _estimate(*, confidence: ConfidenceVectorV3, point_w: float | None = 55.0) -> DeviceEstimate:
    status = EstimateStatus.ESTIMATED if point_w is not None else EstimateStatus.UNKNOWN
    return DeviceEstimate(
        subject_kind="device",
        subject_id="dev-null-conf",
        point_w=point_w,
        bounds=Bounds(lower_w=0.0, upper_w=100.0, basis="non_negativity"),
        status=status,
        identifiability=IdentifiabilityStatus.IDENTIFIABLE,
        method=InferenceMethod.JOINT_DELTA,
        confidence=confidence,
        source_reader_ids=["reader-1"],
        evidence_refs=[EvidenceRef(kind="sample", id="sample-1")],
        versions=VERS,
        updated_at=NOW,
    )


def _result(estimate: DeviceEstimate) -> JointInferenceResult:
    return JointInferenceResult(
        event_group_id="group-1",
        event_group_revision=1,
        status=EventGroupStatus.CONFIRMED,
        device_estimates=[estimate],
        group_estimates=[],
        unknown_estimates=[],
        candidates=[],
        matches={},
        residual_w_by_reader={},
        versions=VERS,
        computed_at=NOW,
    )


# --------------------------------------------------------------------------- #
# Migration contract
# --------------------------------------------------------------------------- #


def test_module_declares_version_22_and_no_new_tables() -> None:
    assert V022.version == 22
    assert V022.name == "v022_nullable_confidence"
    assert TIERS == {}


def test_fresh_database_has_nullable_confidence(tmp_path: Path) -> None:
    db = Database(tmp_path / "fresh.db")
    try:
        assert db.schema_version == LATEST_VERSION
        assert db.schema_version >= V022.version
        not_null = {
            row[1]
            for row in db._conn.execute("PRAGMA table_info(inference_results)")
            if row[3]
        }
        assert "confidence" not in not_null
    finally:
        db.close()


def test_rebuild_preserves_rows_columns_and_indexes(tmp_path: Path) -> None:
    path = tmp_path / "shape.db"
    _build_pre_migration_db(path)
    db = Database(path)
    try:
        columns = [row[1] for row in db._conn.execute("PRAGMA table_info(inference_results)")]
        assert tuple(columns) == INFERENCE_COLUMNS
        indexes = {row[1] for row in db._conn.execute("PRAGMA index_list(inference_results)")}
        assert {
            "idx_inference_device_time",
            "idx_inference_time",
            "idx_inference_tier",
        } <= indexes
        row = db._conn.execute(
            "SELECT device_id, power_w, confidence, legacy_semantics FROM inference_results"
        ).fetchone()
        assert row["device_id"] == "old-dev"
        assert row["power_w"] == 42.5
        assert row["confidence"] == 0.7
        assert row["legacy_semantics"] == "legacy_v2"
    finally:
        db.close()


def test_apply_is_idempotent_and_failure_rolls_back(tmp_path: Path) -> None:
    path = tmp_path / "idem.db"
    _build_pre_migration_db(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        assert current_version(conn) == PRE_MIGRATION_VERSION
        conn.execute("BEGIN IMMEDIATE")
        try:
            assert V022.apply is not None
            V022.apply(conn)
            raise RuntimeError("injected failure")
        except RuntimeError:
            conn.execute("ROLLBACK")
        assert current_version(conn) == PRE_MIGRATION_VERSION
        not_null = {
            row[1]
            for row in conn.execute("PRAGMA table_info(inference_results)")
            if row[3]
        }
        assert "confidence" in not_null  # rolled back to the v017 shape
    finally:
        conn.close()

    db = Database(path)
    try:
        assert db.schema_version == LATEST_VERSION
        assert db.schema_version >= V022.version
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Repository regression: all-None ConfidenceVectorV3 persists as NULL
# --------------------------------------------------------------------------- #


def test_all_none_confidence_vector_persists_as_null(tmp_path: Path) -> None:
    """The exact ZF defect: a valid estimate with all components None writes."""
    db = Database(tmp_path / "null-conf.db")
    try:
        estimate = _estimate(confidence=ConfidenceVectorV3())
        assert all(
            getattr(estimate.confidence, name) is None
            for name in estimate.confidence.model_fields
        )
        assert db.repos.inference.put_estimates(_result(estimate)) == 1
        rows = db.repos.inference.latest_estimates()
        assert len(rows) == 1
        assert rows[0]["confidence"] is None
        assert rows[0]["power_w"] == 55.0
        # The full vector still round-trips via estimate_json (absent stays None).
        confidence = rows[0]["estimate"]["confidence"]
        assert all(value is None for value in confidence.values())
    finally:
        db.close()


def test_present_identifiability_still_writes_the_score(tmp_path: Path) -> None:
    db = Database(tmp_path / "present-conf.db")
    try:
        estimate = _estimate(confidence=ConfidenceVectorV3(identifiability=0.8))
        db.repos.inference.put_estimates(_result(estimate))
        row = db.repos.inference.latest_estimates()[0]
        assert row["confidence"] == pytest.approx(0.8)
    finally:
        db.close()
