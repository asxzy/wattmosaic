"""S16 — migration v016: reader samples v2, evidence, groups and records.

Builds genuine pre-migration databases with raw ``sqlite3`` (applying only the
released migrations 1-11), then drives the real migration module and the real
repositories.  The acceptance checks from the task file:

* a :class:`ReaderMeasurement` round-trips through ``ct_samples`` with **every**
  field intact;
* re-inserting evidence raises instead of updating;
* group revisions are immutable and a new revision is a new row;
* the record-chain validator holds on read;

plus the Wave-2 storage rules: calling ``apply`` twice is a no-op, and an
exception partway through ``apply`` leaves ``PRAGMA user_version`` unchanged.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from wattmosaic_engine.contracts import (
    EventEvidence,
    EventGroup,
    EventGroupStatus,
    EventGroupStatusV3,
    EventInferenceRecord,
    EvidenceRef,
    JointInferenceResult,
    MeasurementBasis,
    MeasurementType,
    QualityFlag,
    ReaderDelta,
    ReaderMeasurement,
    SubEvent,
    SubEventKind,
    VersionTriple,
)
from wattmosaic_engine.storage.database import Database
from wattmosaic_engine.storage.migrations import MIGRATIONS, add_column_if_missing
from wattmosaic_engine.storage.migrations import v016_events as module
from wattmosaic_engine.storage.repositories import enum_value

PRE_MIGRATION_VERSION = 15
NOW = datetime(2026, 11, 1, 0, 0, 0, tzinfo=UTC)
VERSIONS = VersionTriple(
    topology_version_id="topo-1", mapping_version_id="map-1", model_set_id="mset-abc"
)


# --------------------------------------------------------------------------- #
# Fixtures: a real pre-migration database built with raw sqlite3
# --------------------------------------------------------------------------- #


def _build_pre_migration_db(path: Path, upto: int = PRE_MIGRATION_VERSION) -> sqlite3.Connection:
    """Apply only migrations <= ``upto`` (all released ones) on a raw connection."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    for migration in MIGRATIONS:
        if migration.version > upto:
            break
        for stmt in migration.statements:
            conn.execute(stmt)
        if migration.apply is not None:
            migration.apply(conn)
    conn.execute(f"PRAGMA user_version = {upto}")
    conn.commit()
    return conn


def _insert_legacy_sample(conn: sqlite3.Connection) -> None:
    """One pre-existing ct_samples row that must survive the migration."""
    conn.execute(
        """INSERT INTO ct_samples(ct_id, entity_id, value_w, raw_value, unit,
        state_timestamp, received_at, quality, repeated, sequence)
        VALUES('legacy_ct', 'sensor.legacy', 42.0, 42.0, 'W', ?, ?, 'good', 0, 1)""",
        (NOW.isoformat(), NOW.isoformat()),
    )
    conn.commit()


# --------------------------------------------------------------------------- #
# Module contract + migration mechanics
# --------------------------------------------------------------------------- #


def test_module_declares_the_reserved_version_and_tiers() -> None:
    assert module.MIGRATION.version == 16
    assert module.MIGRATION.name == "v016_events"
    assert module.TIERS == {
        "event_evidence": "L2",
        "event_groups": "L2",
        "event_group_members": "L2",
        "event_inference_records": "L2",
        "ha_events_v3": "L2",
    }


def test_apply_creates_every_table_and_adds_every_sample_column(tmp_path: Path) -> None:
    conn = _build_pre_migration_db(tmp_path / "pre.db")
    try:
        for stmt in module.MIGRATION.statements:
            conn.execute(stmt)
        module.MIGRATION.apply(conn)
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {
            "event_evidence",
            "event_groups",
            "event_group_members",
            "event_inference_records",
            "ha_events_v3",
        } <= tables
        columns = {row[1] for row in conn.execute("PRAGMA table_info(ct_samples)")}
        assert {
            "active_power_v2_w", "current_rms_a", "voltage_rms_v", "reactive_power_var",
            "power_factor", "energy_wh", "phase", "leg", "measurement_type",
            "measurement_window_ms", "calibration_version", "quality_flags_json",
            "measurement_basis", "observed_at",
        } <= columns
    finally:
        conn.close()


def test_apply_is_idempotent(tmp_path: Path) -> None:
    """Invoking the apply callable twice leaves the same schema and rows."""
    conn = _build_pre_migration_db(tmp_path / "idempotent.db")
    try:
        _insert_legacy_sample(conn)
        for stmt in module.MIGRATION.statements:
            conn.execute(stmt)
        module.MIGRATION.apply(conn)
        before = {
            row[0]: {r[1] for r in conn.execute(f"PRAGMA table_info({row[0]})")}
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        rows_before = conn.execute("SELECT COUNT(*) FROM ct_samples").fetchone()[0]

        module.MIGRATION.apply(conn)  # second call must be a no-op

        after = {
            row[0]: {r[1] for r in conn.execute(f"PRAGMA table_info({row[0]})")}
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert before == after
        assert conn.execute("SELECT COUNT(*) FROM ct_samples").fetchone()[0] == rows_before
    finally:
        conn.close()


def test_failure_mid_apply_leaves_user_version_unchanged(tmp_path: Path) -> None:
    """A raised error inside apply rolls the migration back atomically."""
    conn = _build_pre_migration_db(tmp_path / "failure.db")
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == PRE_MIGRATION_VERSION
        conn.execute("BEGIN IMMEDIATE")
        with pytest.raises(RuntimeError, match="inject"):
            for stmt in module.MIGRATION.statements:
                conn.execute(stmt)
            module.MIGRATION.apply(conn)
            raise RuntimeError("inject failure after DDL, before user_version bump")
        conn.execute("ROLLBACK")
        assert conn.execute("PRAGMA user_version").fetchone()[0] == PRE_MIGRATION_VERSION
        # The partial DDL is gone too: the migration table was rolled back.
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert "event_evidence" not in names
    finally:
        conn.close()


def test_add_column_if_missing_does_not_duplicate(tmp_path: Path) -> None:
    conn = _build_pre_migration_db(tmp_path / "cols.db")
    try:
        add_column_if_missing(conn, "ct_samples", "phase", "TEXT")
        add_column_if_missing(conn, "ct_samples", "phase", "TEXT")
        names = [row[1] for row in conn.execute("PRAGMA table_info(ct_samples)")]
        assert names.count("phase") == 1
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# ReaderMeasurement round-trip through ct_samples
# --------------------------------------------------------------------------- #


def test_reader_measurement_round_trips_every_field(tmp_path: Path) -> None:
    db = Database(tmp_path / "samples.db")
    try:
        reading = ReaderMeasurement(
            reader_id="br1",
            observed_at=NOW,
            received_at=NOW + timedelta(seconds=2),
            active_power_w=123.5,
            current_rms_a=0.54,
            voltage_rms_v=230.2,
            reactive_power_var=12.25,
            power_factor=0.97,
            energy_wh=4567.0,
            phase="l1",
            leg="l2",
            voltage_reference_v=230.0,
            measurement_type=MeasurementType.WINDOW_AVERAGE,
            measurement_window_ms=1000,
            calibration_version="cal-2026-11",
            measurement_basis=MeasurementBasis.CT_MEASURED.value,
            quality_flags=[QualityFlag.REPEATED, QualityFlag.STALE_ON_ARRIVAL],
            sequence=7,
        )
        assert db.repos.samples.insert_readings([reading]) == 1
        stored = db.repos.samples.latest_reading("br1")
        assert stored is not None
        assert stored == reading
        row = db.repos.samples.latest_ct_sample("br1")
        assert row is not None
        assert enum_value(row["measurement_type"]) == MeasurementType.WINDOW_AVERAGE.value
        assert row["measurement_basis"] == MeasurementBasis.CT_MEASURED.value
        assert json.loads(row["quality_flags_json"]) == ["repeated", "stale_on_arrival"]
        assert row["observed_at"] is not None
        readings = db.repos.samples.readings_between("br1", NOW - timedelta(seconds=1), NOW + timedelta(seconds=1))
        assert readings == [reading]
    finally:
        db.close()


def test_energy_only_reading_keeps_active_power_none(tmp_path: Path) -> None:
    """HB §18: an absent active power stays None, never 0 (W2/S16)."""
    db = Database(tmp_path / "energy.db")
    try:
        reading = ReaderMeasurement(
            reader_id="meter",
            observed_at=NOW,
            received_at=NOW,
            active_power_w=None,
            energy_wh=12345.0,
            phase="unknown",
            measurement_type=MeasurementType.ACCUMULATED_ENERGY,
            calibration_version="cal-1",
            measurement_basis=MeasurementBasis.MONITOR_REPORTED.value,
            quality_flags=[],
            sequence=1,
        )
        assert db.repos.samples.insert_readings([reading]) == 1
        stored = db.repos.samples.latest_reading("meter")
        assert stored is not None
        assert stored.active_power_w is None
        assert stored.energy_wh == 12345.0
    finally:
        db.close()


def test_legacy_ct_sample_still_reads_after_migration(tmp_path: Path) -> None:
    conn = _build_pre_migration_db(tmp_path / "legacy.db")
    try:
        _insert_legacy_sample(conn)
        for stmt in module.MIGRATION.statements:
            conn.execute(stmt)
        module.MIGRATION.apply(conn)
        row = conn.execute("SELECT ct_id, value_w FROM ct_samples WHERE ct_id='legacy_ct'").fetchone()
        assert row["ct_id"] == "legacy_ct"
        assert row["value_w"] == 42.0
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Evidence immutability
# --------------------------------------------------------------------------- #


def _evidence(evidence_id: str = "evd-1") -> EventEvidence:
    return EventEvidence(
        evidence_id=evidence_id,
        reader_id="br1",
        measurement_event_id="mev-1",
        window_start=NOW,
        window_end=NOW + timedelta(seconds=120),
        samples=[(NOW, 10.0), (NOW + timedelta(seconds=30), None)],
        baseline_w=10.0,
        sigma_w=1.5,
        delta_w=10.0,
        quality_flags=[QualityFlag.REPEATED],
        topology_version_id="topo-1",
        finalized=True,
    )


def test_put_evidence_persists_and_refuses_an_update(tmp_path: Path) -> None:
    db = Database(tmp_path / "evidence.db")
    try:
        evidence = _evidence()
        db.repos.evidence.put_evidence(evidence)
        assert db.repos.evidence.evidence("evd-1") == evidence
        with pytest.raises(sqlite3.IntegrityError):
            db.repos.evidence.put_evidence(evidence)
        assert len(db.repos.evidence.evidence_for_event("mev-1")) == 1
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Event group revisions
# --------------------------------------------------------------------------- #


def _group(revision: int, status: EventGroupStatus = EventGroupStatus.CONFIRMED) -> EventGroup:
    sub = SubEvent(
        sub_event_id="sub-1",
        kind=SubEventKind.READER_STEP,
        ref=EvidenceRef(kind="event_evidence", id="evd-1"),
        observed_at=NOW,
        timestamp_uncertainty_s=0.5,
    )
    delta = ReaderDelta(
        reader_id="br1",
        delta_w=10.0,
        sigma_w=1.5,
        completeness="complete",
        evidence_ref=EvidenceRef(kind="event_evidence", id="evd-1"),
    )
    return EventGroup(
        group_id="grp-1",
        revision=revision,
        status=status,
        opened_at=NOW,
        closed_at=NOW + timedelta(seconds=120),
        sub_events=[sub],
        reader_deltas=[delta],
        grouping_reasons=["temporal_overlap"],
        versions=VERSIONS,
    )


def test_group_revisions_are_immutable_and_new_revision_is_a_new_row(tmp_path: Path) -> None:
    db = Database(tmp_path / "groups.db")
    try:
        first = _group(1)
        db.repos.evidence.put_group_revision(first)
        with pytest.raises(sqlite3.IntegrityError):
            db.repos.evidence.put_group_revision(first)
        assert db.repos.evidence.latest_group("grp-1") == first

        revised = _group(2, EventGroupStatus.REVISED)
        db.repos.evidence.put_group_revision(revised)
        assert db.repos.evidence.latest_group("grp-1") == revised
        assert db.repos.evidence.group_revision("grp-1", 1) == first
        assert [g.revision for g in db.repos.evidence.groups()] == [2, 1]
        assert [g.group_id for g in db.repos.evidence.groups(since=NOW, status=EventGroupStatus.REVISED)] == ["grp-1"]
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Event inference records + the chain validator
# --------------------------------------------------------------------------- #


def _result() -> JointInferenceResult:
    return JointInferenceResult(
        event_group_id="grp-1",
        event_group_revision=1,
        status=EventGroupStatusV3.CONFIRMED,
        device_estimates=[],
        group_estimates=[],
        unknown_estimates=[],
        candidates=[],
        matches={},
        residual_w_by_reader={"br1": 0.0},
        versions=VERSIONS,
        computed_at=NOW,
    )


def _record(record_id: str, revision: int, previous: str | None) -> EventInferenceRecord:
    return EventInferenceRecord(
        record_id=record_id,
        event_group_id="grp-1",
        interpretation_revision=revision,
        previous_record_id=previous,
        result=_result(),
        reason="initial" if revision == 1 else "late_data",
        created_at=NOW + timedelta(minutes=revision),
    )


def test_record_chain_validator_holds_on_read(tmp_path: Path) -> None:
    db = Database(tmp_path / "records.db")
    try:
        first = _record("rec-1", 1, None)
        second = _record("rec-2", 2, "rec-1")
        db.repos.evidence.put_record(first)
        db.repos.evidence.put_record(second)
        rows = db.repos.evidence.records_for_group("grp-1")
        assert [r.record_id for r in rows] == ["rec-1", "rec-2"]
        assert rows[0].interpretation_revision == 1 and rows[0].previous_record_id is None
        assert rows[1].interpretation_revision == 2 and rows[1].previous_record_id == "rec-1"
        assert rows[1].result.result_id == second.result.result_id

        # A corrupted row (revision 1 with a previous id) is rejected on read by
        # the contract's own validator, not silently returned.
        with pytest.raises(ValueError, match="previous_record_id"):
            db.repos.evidence.record_from_row(
                {
                    "record_id": "bad",
                    "event_group_id": "grp-1",
                    "interpretation_revision": 1,
                    "previous_record_id": "rec-1",
                    "result_json": second.result.model_dump_json(),
                    "reason": "initial",
                    "created_at": NOW.isoformat(),
                }
            )
    finally:
        db.close()


def test_records_for_subject_finds_group_members(tmp_path: Path) -> None:
    db = Database(tmp_path / "subject.db")
    try:
        db.repos.evidence.put_evidence(_evidence())
        db.repos.evidence.put_group_revision(_group(1))
        db.repos.evidence.put_record(_record("rec-1", 1, None))
        records = db.repos.evidence.records_for_subject("evd-1")
        assert [r.record_id for r in records] == ["rec-1"]
        assert db.repos.evidence.records_for_subject("nonexistent") == []
    finally:
        db.close()


def test_ha_events_v3_table_is_device_free(tmp_path: Path) -> None:
    """The new HA event table has no device_id column (binding is a match output)."""
    conn = _build_pre_migration_db(tmp_path / "ha.db")
    try:
        for stmt in module.MIGRATION.statements:
            conn.execute(stmt)
        module.MIGRATION.apply(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(ha_events_v3)")}
        assert "device_id" not in cols
        assert {
            "event_id", "entity_id", "observed_at", "received_at", "context_id",
            "context_parent_id", "context_user_id", "linked",
        } <= cols
    finally:
        conn.close()


def test_history_rows_for_identifiability(tmp_path: Path) -> None:
    db = Database(tmp_path / "history.db")
    try:
        db.repos.evidence.put_evidence(_evidence())
        db.repos.evidence.put_group_revision(_group(1))
        rows = db.repos.evidence.history_rows_for_identifiability(
            ["evd-1", "absent-subject"], NOW - timedelta(days=1)
        )
        assert len(rows) == 1
        assert rows[0]["group_id"] == "grp-1"
        # Independent event pattern: a present subject has its delta, an absent
        # one stays None -- never 0 (HB §18).
        assert rows[0]["pattern"] == {"evd-1": 10.0, "absent-subject": None}
    finally:
        db.close()
