"""Versioned inference records: version monotonicity and retention survival.

Two defects motivate this file, both named in `docs/assessment_gap_analysis.md`:

* **G15** — ``inference_results`` rows carry no algorithm version, no previous
  value and no delta, so a stored result cannot be explained after an upgrade.
  ``InferenceRecord`` is the append-only, versioned successor.
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

import pytest
from wattmosaic_engine.contracts import (
    ConfidenceVector,
    InferenceRecord,
    MeasurementBasis,
    Source,
)
from wattmosaic_engine.storage.database import Database
from wattmosaic_engine.storage.migrations import LATEST_VERSION

BASE = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)


def _record(
    record_id: str,
    *,
    device_id: str = "dev1",
    timestamp: datetime = BASE,
    revision: int = 1,
    previous_record_id: str | None = None,
    previous_power_w: float | None = None,
    power_w: float = 100.0,
    algorithm_version: str = "disagg@2.1",
    source: Source = Source.INFERRED,
    basis: MeasurementBasis = MeasurementBasis.CT_MEASURED,
    confidence: ConfidenceVector | None = None,
    reason: str = "branch_increase",
) -> InferenceRecord:
    return InferenceRecord(
        record_id=record_id,
        device_id=device_id,
        timestamp=timestamp,
        algorithm_version=algorithm_version,
        revision=revision,
        previous_record_id=previous_record_id,
        previous_power_w=previous_power_w,
        power_w=power_w,
        source=source,
        measurement_basis=basis,
        confidence=confidence or ConfidenceVector(power=0.7, method="blend_v1"),
        reason=reason,
        explanation=["first_cycle"],
        evidence_refs=[f"sample:{record_id}"],
        evidence={"window_s": 30},
    )


# --------------------------------------------------------------------------- #
# Migration 9
# --------------------------------------------------------------------------- #


def test_a_fresh_database_reaches_the_latest_schema(tmp_path) -> None:
    db = Database(tmp_path / "fresh.db")
    try:
        assert db.schema_version == LATEST_VERSION == 10
        tables = {
            row["name"]
            for row in db._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {
            "circuits",
            "topology_relations",
            "inference_records",
            "ct_measurement_factors",
            "ha_entity_bindings",
            "residual_power",
            "isolation_verdicts",
        } <= tables
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
        db.insert_inference_record(_record("r1"))
        assert len(db.inference_records_for_device("dev1")) == 1
    finally:
        db.close()


def test_re_running_migration_9_is_a_no_op(tmp_path) -> None:
    from wattmosaic_engine.storage.migrations import apply_migrations

    path = tmp_path / "twice.db"
    first = Database(path)
    try:
        first.insert_inference_record(_record("r1"))
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
# Round-trip
# --------------------------------------------------------------------------- #


def test_inference_record_round_trips_every_field(tmp_path) -> None:
    db = Database(tmp_path / "rt.db")
    try:
        record = _record(
            "r1",
            revision=3,
            previous_record_id="r0",
            previous_power_w=90.0,
            power_w=110.0,
            basis=MeasurementBasis.FACTOR_ADJUSTED,
            confidence=ConfidenceVector(
                power=0.8, topology=0.6, residual=0.4, method="blend_v1"
            ),
        )
        db.insert_inference_record(record)
        rows = db.inference_records_for_device("dev1")
        assert len(rows) == 1
        row = rows[0]
        assert row["record_id"] == "r1"
        assert row["device_id"] == "dev1"
        assert row["algorithm_version"] == "disagg@2.1"
        assert row["contract_version"]
        assert row["revision"] == 3
        assert row["previous_record_id"] == "r0"
        assert row["previous_power_w"] == pytest.approx(90.0)
        assert row["power_w"] == pytest.approx(110.0)
        assert row["source"] == "inferred"
        assert row["measurement_basis"] == "factor_adjusted"
        confidence = ConfidenceVector.model_validate(row["confidence"])
        assert confidence.power == pytest.approx(0.8)
        assert confidence.topology == pytest.approx(0.6)
        assert confidence.residual == pytest.approx(0.4)
        assert confidence.method == "blend_v1"
        assert row["reason"] == "branch_increase"
        assert row["explanation"] == ["first_cycle"]
        assert row["evidence_refs"] == ["sample:r1"]
        assert row["evidence"] == {"window_s": 30}
    finally:
        db.close()


def test_latest_inference_record_is_the_highest_revision(tmp_path) -> None:
    db = Database(tmp_path / "latest.db")
    try:
        for revision in (1, 2, 3):
            db.insert_inference_record(
                _record(
                    f"r{revision}",
                    revision=revision,
                    timestamp=BASE + timedelta(minutes=revision),
                )
            )
        latest = db.latest_inference_record("dev1")
        assert latest is not None
        assert latest["record_id"] == "r3"
        assert latest["revision"] == 3
        assert db.latest_inference_record("nope") is None
    finally:
        db.close()


def test_inference_records_for_device_honours_since_and_limit(tmp_path) -> None:
    db = Database(tmp_path / "window.db")
    try:
        for revision in (1, 2, 3):
            db.insert_inference_record(
                _record(
                    f"r{revision}",
                    revision=revision,
                    timestamp=BASE + timedelta(hours=revision),
                )
            )
        recent = db.inference_records_for_device(
            "dev1", since=BASE + timedelta(hours=2)
        )
        assert [r["record_id"] for r in recent] == ["r2", "r3"]
        limited = db.inference_records_for_device("dev1", limit=1)
        assert len(limited) == 1
    finally:
        db.close()


def test_insert_inference_record_is_append_only(tmp_path) -> None:
    """A revision never overwrites its predecessor."""
    db = Database(tmp_path / "append.db")
    try:
        db.insert_inference_record(_record("r1", revision=1))
        db.insert_inference_record(
            _record(
                "r2",
                revision=2,
                timestamp=BASE + timedelta(minutes=1),
                previous_record_id="r1",
                previous_power_w=100.0,
                power_w=120.0,
            )
        )
        rows = db.inference_records_for_device("dev1")
        assert [r["record_id"] for r in rows] == ["r1", "r2"]
        assert rows[1]["previous_power_w"] == pytest.approx(100.0)
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Retention exemption (G16)
# --------------------------------------------------------------------------- #


def test_prune_exempts_audit_tables_but_still_prunes_raw_samples(tmp_path) -> None:
    """A 400-day-old audit record survives; a 400-day-old raw sample does not.

    ``inference_records`` is audit state, not a raw sample.  The existing raw
    horizon exists to bound ``ct_samples`` growth; applying it to a versioned
    record would delete the very history the record exists to keep.
    """
    from wattmosaic_engine.contracts import MeasurementFactor

    db = Database(tmp_path / "retention.db")
    old = datetime.now(UTC) - timedelta(days=400)
    new = datetime.now(UTC)
    try:
        db.insert_inference_record(
            _record("old", timestamp=old, revision=1)
        )
        db.insert_measurement_factor(
            MeasurementFactor(
                ct_id="panel_l1",
                multiplier=2.0,
                effective_from=old,
                effective_to=None,
                applied_by="user",
                declared_by="user",
            )
        )
        db.insert_ct_samples(
            [
                {
                    "ct_id": "c",
                    "entity_id": "sensor.c",
                    "value_w": 1.0,
                    "raw_value": 1.0,
                    "unit": "W",
                    "state_timestamp": old,
                    "received_at": old,
                },
                {
                    "ct_id": "c",
                    "entity_id": "sensor.c",
                    "value_w": 2.0,
                    "raw_value": 2.0,
                    "unit": "W",
                    "state_timestamp": new,
                    "received_at": new,
                },
            ]
        )

        removed = db.prune(raw_days=30, aggregate_days=730)

        # Raw samples obey the horizon as before.
        assert removed["ct_samples"] == 1
        assert db.ct_samples_between(new - timedelta(days=1), new + timedelta(days=1))
        # Audit state does not.
        assert db.inference_records_for_device("dev1")
        assert db.factors_for_ct("panel_l1")
        assert removed["inference_records"] == 0
        assert removed["ct_measurement_factors"] == 0
    finally:
        db.close()


def test_prune_does_not_touch_the_other_audit_tables(tmp_path) -> None:
    """Circuits, relations and entity bindings are audit state too."""
    from wattmosaic_engine.contracts import (
        Circuit,
        DeviceConfiguration,
        DeviceConfigurationSet,
        HaEntityBinding,
        HaEntityRole,
        RelationKind,
        TopologyRelation,
    )

    db = Database(tmp_path / "audit.db")
    old = datetime.now(UTC) - timedelta(days=400)
    try:
        db.upsert_circuit(Circuit(circuit_id="c1", name="Kitchen"))
        db.insert_topology_relation(
            TopologyRelation(
                relation_id="r1",
                kind=RelationKind.DEVICE_CT_ASSOCIATION,
                subject_id="dev1",
                object_id="ct1",
                created_at=old,
            )
        )
        # ``ha_entity_bindings`` is written as a projection of the declaration.
        db.save_device_configurations(
            DeviceConfigurationSet(
                devices=[
                    DeviceConfiguration(
                        device_id="dev1",
                        entity_bindings=[
                            HaEntityBinding(
                                entity_id="sensor.power",
                                device_id="dev1",
                                role=HaEntityRole.POWER_MEASUREMENT,
                                contributes_power=True,
                            )
                        ],
                    )
                ]
            )
        )
        removed = db.prune(raw_days=30, aggregate_days=730)
        assert removed.get("circuits", 0) == 0
        assert removed.get("topology_relations", 0) == 0
        assert removed.get("ha_entity_bindings", 0) == 0
        assert db.circuits_all()
        assert db.relations_of_kind(RelationKind.DEVICE_CT_ASSOCIATION)
        # The binding survived the prune and is still readable.
        loaded = db.load_device_configurations()
        assert loaded.devices[0].entity_bindings[0].entity_id == "sensor.power"
    finally:
        db.close()


def test_residual_power_history_round_trips_and_survives_prune(tmp_path) -> None:
    from wattmosaic_engine.contracts import ResidualKind, ResidualPower

    db = Database(tmp_path / "residual.db")
    old = datetime.now(UTC) - timedelta(days=400)
    try:
        db.insert_residual_power(
            ResidualPower(
                residual_w=-180.0,
                resolved=False,
                unresolved_since=old,
                explained_by_device_w={"dev1": 120.0},
                basis=MeasurementBasis.FACTOR_ADJUSTED,
            ),
            timestamp=old,
        )
        history = db.residual_history()
        assert len(history) == 1
        assert history[0]["residual_w"] == pytest.approx(-180.0)
        assert history[0]["kind"] == ResidualKind.NEGATIVE.value
        assert history[0]["resolved"] is False
        assert history[0]["explained_by_device_w"] == {"dev1": 120.0}

        removed = db.prune(raw_days=30, aggregate_days=730)
        assert removed.get("residual_power", 0) == 0
        assert db.residual_history(since=old - timedelta(days=1))
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Migration 10: isolation verdicts are durable, and every ref prefix resolves
# --------------------------------------------------------------------------- #


def _verdict(event_id: str = "e1", *, device_id: str = "dev1", at: datetime = BASE):
    from wattmosaic_engine.contracts import EventIsolation, IsolationStatus

    return EventIsolation(
        status=IsolationStatus.ISOLATED,
        event_id=event_id,
        device_id=device_id,
        entity_id=f"switch.{device_id}",
        occurred_at=at,
        received_at=at,
        analysis_offset_s=0.0,
        isolation_window_s=30.0,
        window_start=at,
        window_end=at + timedelta(seconds=30),
        reason="isolated",
        evidence={"concurrent_events": 0},
    )


def test_an_isolation_verdict_round_trips_and_replaces_on_reanalysis(tmp_path) -> None:
    db = Database(tmp_path / "verdicts.db")
    try:
        db.insert_isolation_verdict(_verdict(), algorithm_version="audit@1.0")
        row = db.isolation_verdict("e1")
        assert row is not None
        assert row["status"] == "isolated"
        assert row["device_id"] == "dev1"
        assert row["algorithm_version"] == "audit@1.0"
        assert row["evidence"] == {"concurrent_events": 0}

        # The event id is the identity: reanalysis replaces, never appends.
        db.insert_isolation_verdict(
            _verdict(event_id="e1"), algorithm_version="audit@1.0"
        )
        assert len(db.isolation_verdicts()) == 1
        assert db.isolation_verdict("missing") is None
    finally:
        db.close()


def test_isolation_verdicts_are_audit_state_and_survive_prune(tmp_path) -> None:
    db = Database(tmp_path / "verdict_prune.db")
    old = datetime.now(UTC) - timedelta(days=400)
    try:
        db.insert_isolation_verdict(_verdict(at=old))
        removed = db.prune(raw_days=30, aggregate_days=730)
        assert removed.get("isolation_verdicts", 0) == 0
        assert db.isolation_verdict("e1") is not None
    finally:
        db.close()


def test_every_evidence_ref_prefix_names_a_row_that_exists(tmp_path) -> None:
    """One resolver answers every prefix this engine emits -- and nothing else."""
    from wattmosaic_engine.contracts import (
        RelationKind,
        TopologyRelation,
        TopologyStatus,
    )

    db = Database(tmp_path / "refs.db")
    try:
        # unknown prefixes / malformed refs are not rows
        assert not db.resolve_evidence_ref("")
        assert not db.resolve_evidence_ref("nocolon")
        assert not db.resolve_evidence_ref("event:")
        assert not db.resolve_evidence_ref("result:dev1@2026-01-01")
        assert not db.resolve_evidence_ref("event:never-analysed")
        assert not db.resolve_evidence_ref("relation:never-stored")
        assert not db.resolve_evidence_ref("circuit:never-stored")
        assert not db.resolve_evidence_ref("record:never-stored")

        db.insert_isolation_verdict(_verdict())
        assert db.resolve_evidence_ref("event:e1")

        db.insert_topology_relation(
            TopologyRelation(
                relation_id="rel_1",
                kind=RelationKind.DEVICE_CT_ASSOCIATION,
                subject_id="dev1",
                object_id="ct_a",
                status=TopologyStatus.CANDIDATE,
                declared_by="inference",
            )
        )
        assert db.resolve_evidence_ref("relation:rel_1")

        db.insert_inference_record(_record("rec_1"))
        assert db.resolve_evidence_ref("record:rec_1")

        # Each prefix only answers for its own table.
        assert not db.resolve_evidence_ref("event:rel_1")
        assert not db.resolve_evidence_ref("record:e1")
    finally:
        db.close()
