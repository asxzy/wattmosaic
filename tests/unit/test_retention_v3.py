"""D5 — retention, tiers and storage health for the new (wave-2/3) tables.

The four acceptance bullets from the task file:

* pruning never deletes L4 versions or the matrix history;
* thinning an old evidence window keeps Δ, baseline, σ and quality, and a
  replay (the L7 seam, ``replay_delta``) still reproduces the signed Δ;
* an evidence row that a newer revision still references is never deleted;
* storage health reports every table registered in ``TIERS``.

Plus the EXTRA carried by the orchestrator: the ``supersedes`` link left by P4
only in the in-memory ``EvidenceBatch`` is persisted through migration 21 and
the evidence repository, and the prune pass honours it.

The retention helpers are driven by an *injected* ``now``: no wall clock and no
database are owned by the pure decision layer.  The integration checks use a
real :class:`~wattmosaic_engine.storage.database.Database` over a temporary
file.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from wattmosaic_engine.contracts import (
    HealthResponse,
    MeasurementEvent,
    QualityFlag,
    StepClass,
)
from wattmosaic_engine.core.diagnostics_api import DiagnosticsApiMixin
from wattmosaic_engine.diagnostics.report import collect_diagnostics, storage_health_section
from wattmosaic_engine.realtime.windows import replay_delta
from wattmosaic_engine.storage.database import Database
from wattmosaic_engine.storage.migrations import (
    LATEST_VERSION,
    MIGRATIONS,
    SPARE_VERSIONS,
    apply_migrations,
    current_version,
)
from wattmosaic_engine.storage.repositories.evidence import EvidenceRepository
from wattmosaic_engine.storage.tiers import (
    DEFAULT_EVIDENCE_RETENTION_DAYS,
    DEFAULT_SAMPLES_RETENTION_DAYS,
    LEGACY_EVENT_TABLES,
    PRUNABLE_TABLES,
    TIERS,
    apply_evidence_retention,
    canonical_evidence_table,
    never_pruned_tables,
    prunable_tables,
    referenced_evidence_ids,
    table_tiers,
    time_column_of,
)

NOW = datetime(2026, 11, 18, 12, 0, 0, tzinfo=UTC)


def _measurement_event() -> MeasurementEvent:
    return MeasurementEvent(
        event_id="mev-1",
        reader_id="Main",
        delta_w=150.0,
        sigma_w=2.5,
        z_score=8.0,
        step_class=StepClass.SUSTAINED,
        onset_lo=NOW - timedelta(seconds=30),
        onset_hi=NOW - timedelta(seconds=15),
        pre_level_w=100.0,
        post_level_w=250.0,
        quality_score=0.9,
        detected_at=NOW,
    )


def _evidence(evidence_id: str, *, at: datetime, n_samples: int = 12) -> object:
    from wattmosaic_engine.contracts import EventEvidence

    samples = [
        (at + timedelta(seconds=i * 5), 100.0 + 12.5 * i) for i in range(n_samples)
    ]
    return EventEvidence(
        evidence_id=evidence_id,
        reader_id="Main",
        measurement_event_id="mev-1",
        window_start=at,
        window_end=at + timedelta(seconds=n_samples * 5),
        samples=samples,
        baseline_w=100.0,
        sigma_w=2.5,
        delta_w=150.0,
        quality_flags=[QualityFlag.PRIMED],
        topology_version_id="topo-1",
        finalized=True,
    )


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "retention_v3.db")
    yield database
    database.close()


# --------------------------------------------------------------------------- #
# Prune declarations: versions and matrix history are immortal
# --------------------------------------------------------------------------- #


def test_prune_never_deletes_l4_versions_or_matrix_history() -> None:
    """Every L4 versioned table (including matrix history) is exempt."""
    immortal = set(never_pruned_tables())
    assert {
        "topology_versions_v3",
        "mapping_versions",
        "matrix_cell_versions",
        "power_model_versions_v3",
        "event_inference_records",
    } <= immortal
    # The exemption is total: none of them is in the delete declaration.
    assert not immortal & set(PRUNABLE_TABLES)
    for table, _col, _horizon in prunable_tables():
        assert table not in immortal


def test_prune_pass_leaves_version_rows_untouched(db: Database) -> None:
    """A real prune removes old raw rows but keeps every version row."""
    old = (NOW - timedelta(days=900)).isoformat()
    with db._lock:
        db._conn.execute(
            "INSERT INTO topology_versions_v3(version_id, number, status, created_at, digest) "
            "VALUES('topo-old', 99, 'superseded', ?, 'd')",
            (old,),
        )
        db._conn.execute(
            "INSERT INTO mapping_versions(version_id, number, status, created_at, digest) "
            "VALUES('map-old', 99, 'superseded', ?, 'd')",
            (old,),
        )
        db._conn.execute(
            """INSERT INTO matrix_cell_versions(
                row_kind, row_id, reader_id, mapping_json, power_json,
                event_association_json, confidence_json, provenance_json, timestamps_json,
                identifiability, visual, mapping_status, observability, updated_at,
                derived, mapping_version_id, valid_from)
                VALUES('DEVICE', 'd1', 'Main', '{}', '{}', '[]', '{}', '{}', '{}',
                'identifiable', 'directly_measured', 'confirmed', 'direct', ?, 0, 'map-1', ?)""",
            (old, old),
        )
        db._conn.execute(
            "INSERT INTO power_model_versions_v3(version_id, subject_kind, subject_id, number, "
            "model_class, basis, spec_json, schema_version, trainer_version, created_at) "
            "VALUES('mdl-old', 'device', 'd1', 1, 'FIXED', 'DELTA', '{}', 1, 't', ?)",
            (old,),
        )
    with db._lock:
        before = {
            table: db._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "topology_versions_v3",
                "mapping_versions",
                "matrix_cell_versions",
                "power_model_versions_v3",
            )
        }
    removed = db.prune(raw_days=1, aggregate_days=1)
    with db._lock:
        for table, count in before.items():
            assert db._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == count, table
    assert removed["topology_versions_v3"] == 0
    assert removed["mapping_versions"] == 0
    assert removed["matrix_cell_versions"] == 0


# --------------------------------------------------------------------------- #
# DEC-30: raw window samples thin after 30 days; evidence lives 400 days
# --------------------------------------------------------------------------- #


def test_thinning_keeps_summary_fields_and_replay(db: Database) -> None:
    repo = EvidenceRepository(db)
    old = NOW - timedelta(days=60)
    evidence = _evidence("evd-old", at=old)
    repo.put_evidence(evidence)
    original_bytes = evidence.model_dump_json()

    counts = apply_evidence_retention(db._conn, now=NOW)
    assert counts["rows_thinned"] == 1
    assert counts["samples_dropped"] > 0

    stored = repo.evidence("evd-old")
    assert stored is not None
    # Only the points that summarise the window remain; the shape is intact.
    assert 0 < len(stored.samples) <= 2
    assert stored.delta_w == evidence.delta_w
    assert stored.baseline_w == evidence.baseline_w
    assert stored.sigma_w == evidence.sigma_w
    assert stored.quality_flags == evidence.quality_flags
    assert stored.window_start == evidence.window_start
    assert stored.window_end == evidence.window_end
    # Replay (L7) reproduces exactly the live signed delta from stored evidence.
    assert replay_delta(stored) == evidence.delta_w
    # The in-memory record is never mutated by retention.
    assert evidence.model_dump_json() == original_bytes


def test_evidence_row_is_deleted_after_400_days_when_unreferenced(db: Database) -> None:
    repo = EvidenceRepository(db)
    very_old = NOW - timedelta(days=401)
    repo.put_evidence(_evidence("evd-expired", at=very_old))
    counts = apply_evidence_retention(db._conn, now=NOW)
    assert counts["rows_deleted"] == 1
    assert repo.evidence("evd-expired") is None
    assert DEFAULT_EVIDENCE_RETENTION_DAYS == 400
    assert DEFAULT_SAMPLES_RETENTION_DAYS == 30


def test_referenced_evidence_row_is_never_deleted(db: Database) -> None:
    """P4's ``supersedes`` link survives and protects the older revision."""
    repo = EvidenceRepository(db)
    very_old = NOW - timedelta(days=500)
    old = _evidence("evd-superseded", at=very_old)
    newer = _evidence("evd-newer", at=NOW - timedelta(days=1))
    repo.put_evidence(old)
    repo.put_evidence(newer, supersedes_evidence_id="evd-superseded")

    assert repo.supersedes_evidence_id("evd-newer") == "evd-superseded"
    assert repo.supersedes_evidence_id("evd-superseded") is None
    assert repo.referencing_evidence_ids("evd-superseded") == ("evd-newer",)
    assert referenced_evidence_ids(db._conn) == frozenset({"evd-superseded"})

    counts = apply_evidence_retention(db._conn, now=NOW)
    assert counts["rows_deleted"] == 0
    assert repo.evidence("evd-superseded") is not None
    assert repo.evidence("evd-newer") is not None


def test_active_run_reference_protects_evidence(db: Database) -> None:
    """An active derived run (L7) can hold evidence past its horizon."""
    repo = EvidenceRepository(db)
    repo.put_evidence(_evidence("evd-replay", at=NOW - timedelta(days=401)))
    counts = apply_evidence_retention(
        db._conn, now=NOW, active_reference_ids=frozenset({"evd-replay"})
    )
    assert counts["rows_deleted"] == 0
    assert repo.evidence("evd-replay") is not None


def test_absent_values_stay_none(db: Database) -> None:
    repo = EvidenceRepository(db)
    very_old = NOW - timedelta(days=401)
    repo.put_evidence(_evidence("evd-nulls", at=very_old))
    with db._lock:
        db._conn.execute(
            "UPDATE event_evidence SET baseline_w=NULL, sigma_w=NULL WHERE evidence_id='evd-nulls'"
        )
    apply_evidence_retention(db._conn, now=NOW)
    assert repo.evidence("evd-nulls") is None  # deleted; no sentinel was substituted


def test_supersedes_column_round_trips_through_contract_reader(db: Database) -> None:
    repo = EvidenceRepository(db)
    repo.put_evidence(_evidence("evd-a", at=NOW), supersedes_evidence_id="evd-previous")
    # The contract model has no metadata field: reading it must not choke on the
    # new column, while the link is available from the repository separately.
    stored = repo.evidence("evd-a")
    assert stored is not None
    assert "supersedes_evidence_id" not in stored.model_dump()
    assert repo.supersedes_evidence_id("evd-a") == "evd-previous"


# --------------------------------------------------------------------------- #
# Migration 21
# --------------------------------------------------------------------------- #


def test_migration_21_adds_the_column_idempotently(tmp_path: Path) -> None:
    db_path = tmp_path / "m21.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        assert apply_migrations(conn) == LATEST_VERSION
        assert current_version(conn) == LATEST_VERSION
        # v025 (Z1) makes 21 no longer the newest schema; assert a floor instead
        # of pinning the latest version so this test survives later migrations.
        assert LATEST_VERSION >= 21
        assert 21 not in SPARE_VERSIONS
        assert any(m.version == 21 for m in MIGRATIONS)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(event_evidence)")}
        assert "supersedes_evidence_id" in columns
        # A second application is a true no-op.
        assert apply_migrations(conn) == LATEST_VERSION
        assert apply_migrations(conn) == LATEST_VERSION
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Re-tiering declaration and storage health
# --------------------------------------------------------------------------- #


def test_device_events_is_declared_as_the_legacy_event_table() -> None:
    assert LEGACY_EVENT_TABLES == ("device_events_legacy",)
    assert canonical_evidence_table() == "event_evidence"
    # Z1 archived the legacy table read-only, so nothing prunes it any more
    # (a DELETE would abort on its read-only trigger).
    assert "device_events" not in PRUNABLE_TABLES
    assert "device_events_legacy" not in PRUNABLE_TABLES


def test_storage_health_lists_every_registered_table(db: Database) -> None:
    report = storage_health_section(db, now=NOW)
    assert set(report) == {name for name, _tier in table_tiers()}
    assert set(TIERS) == {"L1", "L2", "L3", "L4"}
    for entry in report.values():
        assert entry["tier"] in TIERS
        assert isinstance(entry["row_count"], int)
        # Absent values are None, never 0.0 (HB §18).
        assert entry["growth_per_day"] is None or isinstance(entry["growth_per_day"], int)
        assert entry["oldest_row"] is None or isinstance(entry["oldest_row"], str)
    # The new tables are all present and report real (zero) counts on a fresh db.
    assert report["event_evidence"]["row_count"] == 0
    assert report["ha_events_v3"]["row_count"] == 0


def test_storage_health_appears_in_the_diagnostics_report(db: Database) -> None:
    report = collect_diagnostics(config=None, database=db, core=None, now=NOW)
    assert "storage_health" in report
    assert set(report["storage_health"]) == {name for name, _tier in table_tiers()}


def test_time_columns_are_declared_for_every_table() -> None:
    for name, _tier in table_tiers():
        column = time_column_of(name)
        assert column is None or isinstance(column, str)


def test_status_publishes_mapping_version_id() -> None:
    """Step 4 (requested by M3): ``/status`` carries ``mapping_version_id``."""

    class Stub(DiagnosticsApiMixin):
        started_wall = NOW
        inference_backend = None
        inference_error = None
        inference_last_run_at = None
        cycles = 0
        learning_active = False
        latest_quality = None
        samples_ingested = 0
        events_ingested = 0
        samples_persisted = 0
        events_persisted = 0
        events_evicted_before_maturity = 0
        unlinked_events_ignored = 0
        power_events_not_state = 0
        persistence_errors = 0

        def __init__(self) -> None:
            self.db = SimpleNamespace(stats=lambda: {"counts": {}})
            self.persist_queue = SimpleNamespace(stats=lambda: {})

        def health(self) -> HealthResponse:
            return HealthResponse(
                status="ok",
                contract_version="2.4.0",
                engine_version="test",
                database="ok",
                ha_connected=True,
                ha_last_event_at=None,
                uptime_seconds=1.0,
                queue_depth=0,
                inference_last_run_at=None,
                learning_active=False,
                details={},
            )

        def _learning_status_payload(self) -> dict:
            return {}

        def _matrix_version_id(self) -> str:
            return "map-42"

        def inference_backend_status(self) -> None:
            return None

    status = Stub().status()
    assert status["mapping_version_id"] == "map-42"
