"""Z8 — bound event-evidence growth (V2 DB-growth budget).

Regressions pinned here:

* a growing window produces ONE live evidence row per ``(event, reader)``: every
  post-sample revises that row in place (samples replaced, not duplicated), and
  the row is marked FINAL when the window closes;
* a late-data revision keeps the audit trail as a supersede chain whose older
  row carries no duplicated samples;
* the superseded predecessor's summary still resolves (provenance refs), and
  retention prunes finalized evidence/raw samples per their settings;
* migration 26 adds the ``revision`` counter with a create-copy-rename that
  preserves every row (and is version-agnostic on the latest schema).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from wattmosaic_engine.contracts import (
    EventEvidence,
    MeasurementEvent,
    MeasurementType,
    QualityFlag,
    ReaderMeasurement,
    StepClass,
)
from wattmosaic_engine.realtime.reader_state import ReaderStateStore
from wattmosaic_engine.realtime.windows import (
    OP_FINALIZE,
    OP_KEY,
    OP_REVISE,
    OP_SUPERSEDE,
    SUPERSEDES_KEY,
    EvidenceBatch,
    WindowCollector,
)
from wattmosaic_engine.storage.database import Database
from wattmosaic_engine.storage.migrations import (
    LATEST_VERSION,
    MIGRATIONS,
    current_version,
)
from wattmosaic_engine.storage.repositories.evidence import EvidenceRepository
from wattmosaic_engine.storage.tiers import apply_evidence_retention

T0 = datetime(2026, 11, 18, 12, 0, 0, tzinfo=UTC)
CADENCE_S = 15.7


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _measurement(reader: str, at: datetime, power: float, seq: int) -> ReaderMeasurement:
    return ReaderMeasurement(
        reader_id=reader,
        observed_at=at,
        received_at=at,
        active_power_w=power,
        phase="l1",
        measurement_type=MeasurementType.INSTANTANEOUS,
        measurement_window_ms=None,
        calibration_version="cal-1",
        measurement_basis="instantaneous",
        quality_flags=[],
        sequence=seq,
    )


def _event(event_id: str = "mev-1", reader: str = "Main") -> MeasurementEvent:
    return MeasurementEvent(
        event_id=event_id,
        reader_id=reader,
        delta_w=150.0,
        sigma_w=1.0,
        z_score=8.0,
        step_class=StepClass.SUSTAINED,
        onset_lo=T0,
        onset_hi=T0 + timedelta(seconds=CADENCE_S),
        pre_level_w=100.0,
        post_level_w=250.0,
        quality_score=0.9,
        detected_at=T0 + timedelta(seconds=2 * CADENCE_S),
    )


class Sink:
    """Apply P4 operations to a real S16 repository (production dispatch)."""

    def __init__(self, repo: EvidenceRepository) -> None:
        self.repo = repo
        self.metadata: dict[str, dict[str, Any]] = {}

    def put(self, batch: list[EvidenceBatch]) -> None:
        for item in batch:
            if item.op == OP_FINALIZE:
                self.repo.finalize_evidence(item.evidence)
            elif item.op == OP_SUPERSEDE:
                self.repo.put_evidence(item.evidence, supersedes_evidence_id=item.supersedes)
                clear_id = item.metadata.get("clear_samples_of")
                if clear_id is not None:
                    self.repo.clear_evidence_samples(clear_id)
            else:
                self.repo.upsert_evidence(item.evidence)
            self.metadata[item.evidence.evidence_id] = dict(item.metadata)


@pytest.fixture()
def repo(tmp_path: Path) -> EvidenceRepository:
    return EvidenceRepository(Database(tmp_path / "z8.db"))


def _open_window(store: ReaderStateStore, collector: WindowCollector) -> str:
    store.add(_measurement("Main", T0 - timedelta(seconds=CADENCE_S), 100.0, 0))
    store.add(_measurement("Main", T0, 100.0, 1))
    window = collector.open(_event(), at=T0 + timedelta(seconds=CADENCE_S))
    assert window is not None
    return window.evidence.evidence_id


# --------------------------------------------------------------------------- #
# One live row per (event, reader); finalized on close
# --------------------------------------------------------------------------- #


def test_growing_window_is_one_live_row_finalized_on_close(repo: EvidenceRepository) -> None:
    store = ReaderStateStore()
    sink = Sink(repo)
    collector = WindowCollector(store, sink=sink.put)
    eid = _open_window(store, collector)

    # 160 post-samples over the 120 s window: the old code appended 160 rows
    # each carrying the cumulative ~2 KB samples_json (~98 rows/event observed).
    for i in range(2, 162):
        collector.on_sample("Main", T0 + timedelta(seconds=i * 1.0), 250.0 + (i % 3))
        assert len(repo.evidence_for_event("mev-1")) == 1, f"extra row at sample {i}"

    live = repo.evidence(eid)
    assert live is not None and live.finalized is False
    # Samples are bounded to the window (window_start..close_at ≈ T0+135.7s).
    assert 100 < len(live.samples) < 160
    assert sink.metadata[eid][OP_KEY] == OP_REVISE

    final = collector.tick(T0 + timedelta(seconds=2000))
    assert len(final) == 1 and final[0].finalized is True
    assert final[0].evidence_id == eid
    assert sink.metadata[eid][OP_KEY] == OP_FINALIZE

    # Still exactly one row and it is finalized, with the samples intact.
    rows = repo.evidence_for_event("mev-1")
    assert len(rows) == 1
    assert rows[0].finalized is True
    assert 100 < len(rows[0].samples) < 160


def test_one_row_per_reader_for_same_event(repo: EvidenceRepository) -> None:
    store = ReaderStateStore()
    sink = Sink(repo)
    collector = WindowCollector(store, sink=sink.put)
    _open_window(store, collector)
    store.add(_measurement("Sub", T0 - timedelta(seconds=CADENCE_S), 50.0, 0))
    store.add(_measurement("Sub", T0, 50.0, 1))
    collector.open(_event("mev-2", "Sub"), at=T0 + timedelta(seconds=CADENCE_S))
    for rid in ("Main", "Sub"):
        collector.on_sample(rid, T0 + timedelta(seconds=2 * CADENCE_S), 200.0)

    rows = repo.evidence_for_event("mev-1") + repo.evidence_for_event("mev-2")
    assert len(rows) == 2  # one per (event, reader)
    assert {r.reader_id for r in rows} == {"Main", "Sub"}


# --------------------------------------------------------------------------- #
# Supersede chain keeps audit, not duplicate samples; provenance resolves
# --------------------------------------------------------------------------- #


def test_late_revision_keeps_audit_without_duplicating_samples(repo: EvidenceRepository) -> None:
    store = ReaderStateStore()
    sink = Sink(repo)
    collector = WindowCollector(store, sink=sink.put)
    eid = _open_window(store, collector)
    for i in range(2, 8):
        collector.on_sample("Main", T0 + timedelta(seconds=i * 10), 250.0)
    final = collector.close("Main")
    assert final is not None and final.evidence_id == eid
    original_sample_count = len(repo.evidence(eid).samples)

    late = collector.on_late_sample("Main", final.window_start + timedelta(seconds=5), 260.0)
    assert late is not None
    assert late.evidence_id != eid
    assert sink.metadata[late.evidence_id][SUPERSEDES_KEY] == eid
    assert sink.metadata[late.evidence_id][OP_KEY] == OP_SUPERSEDE

    # The predecessor's summary row stays and the supersede link resolves, but
    # its samples were thinned to a bounded summary (no duplicated payload).
    predecessor = repo.evidence(eid)
    assert predecessor is not None and predecessor.finalized is True
    assert repo.supersedes_evidence_id(late.evidence_id) == eid
    assert len(predecessor.samples) < original_sample_count
    assert predecessor.delta_w == final.delta_w  # audit summary preserved
    assert repo.referencing_evidence_ids(eid) == (late.evidence_id,)
    # The new row carries the full revised window, so replay is still exact.
    assert len(repo.evidence(late.evidence_id).samples) >= original_sample_count


# --------------------------------------------------------------------------- #
# Retention prunes finalized evidence and raw samples per settings
# --------------------------------------------------------------------------- #


def test_retention_prunes_expired_finalized_evidence(tmp_path: Path) -> None:
    repo = EvidenceRepository(Database(tmp_path / "prune.db"))
    old = T0 - timedelta(days=401)
    expired = EventEvidence(
        evidence_id="evd-expired",
        reader_id="Main",
        measurement_event_id="mev-old",
        window_start=old,
        window_end=old + timedelta(seconds=60),
        samples=[(old, 100.0)],
        baseline_w=100.0,
        sigma_w=1.0,
        delta_w=10.0,
        quality_flags=[QualityFlag.PRIMED],
        topology_version_id="topo-1",
        finalized=True,
    )
    repo.put_evidence(expired)
    counts = apply_evidence_retention(repo._conn, now=T0, evidence_days=400, sample_days=30)
    assert counts["rows_deleted"] == 1
    assert repo.evidence("evd-expired") is None


def test_retention_thins_raw_samples_on_30d_clock(tmp_path: Path) -> None:
    repo = EvidenceRepository(Database(tmp_path / "thin.db"))
    old = T0 - timedelta(days=60)
    samples = [(old + timedelta(seconds=5 * i), 100.0 + i) for i in range(12)]
    evidence = EventEvidence(
        evidence_id="evd-thin",
        reader_id="Main",
        measurement_event_id="mev-old",
        window_start=old,
        window_end=old + timedelta(seconds=60),
        samples=samples,
        baseline_w=100.0,
        sigma_w=1.0,
        delta_w=12.0,
        quality_flags=[],
        topology_version_id="topo-1",
        finalized=True,
    )
    repo.put_evidence(evidence)
    counts = apply_evidence_retention(repo._conn, now=T0, evidence_days=400, sample_days=30)
    assert counts["rows_thinned"] == 1
    stored = repo.evidence("evd-thin")
    assert stored is not None and 0 < len(stored.samples) < len(samples)
    assert stored.delta_w == 12.0  # summary retained


# --------------------------------------------------------------------------- #
# Migration 26 — create-copy-rename preserving rows; version-agnostic
# --------------------------------------------------------------------------- #


def test_migration_26_preserves_rows_and_is_idempotent(tmp_path: Path) -> None:
    from wattmosaic_engine.storage.migrations import v026_evidence_revision as m26

    db = Database(tmp_path / "m26.db")
    conn = db._conn
    # Seed a row through the pre-v026 shape (migrations already applied, so the
    # table has `revision`; drop it to simulate the pre-migration schema).
    repo = EvidenceRepository(db)
    repo.put_evidence(
        EventEvidence(
            evidence_id="evd-keep",
            reader_id="Main",
            measurement_event_id="mev-keep",
            window_start=T0,
            window_end=T0 + timedelta(seconds=60),
            samples=[(T0, 100.0)],
            baseline_w=100.0,
            sigma_w=1.0,
            delta_w=10.0,
            quality_flags=[],
            topology_version_id="topo-1",
            finalized=True,
        )
    )
    with db._lock:
        conn.execute("ALTER TABLE event_evidence DROP COLUMN revision")
        assert "revision" not in {r[1] for r in conn.execute("PRAGMA table_info(event_evidence)")}
        m26._apply(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(event_evidence)")}
        assert "revision" in cols
        assert conn.execute("SELECT COUNT(*) FROM event_evidence").fetchone()[0] == 1
        assert conn.execute(
            "SELECT reader_id FROM event_evidence WHERE evidence_id='evd-keep'"
        ).fetchone()[0] == "Main"
        # second application is a true no-op
        m26._apply(conn)
        assert conn.execute("SELECT COUNT(*) FROM event_evidence").fetchone()[0] == 1
    db.close()


def test_migration_26_registered_and_version_agnostic() -> None:
    assert any(m.version == 26 and m.name == "v026_evidence_revision" for m in MIGRATIONS)
    assert current_version  # imported
    # Version-agnostic: assert a floor and agreement with the discovered latest,
    # never a hard-coded "== 26".
    assert LATEST_VERSION >= 26
