"""P4 — Event window collector and immutable event evidence.

Acceptance bullets, against the *production* module:

* the pre-window holds >= 2 samples at a 15.7 s cadence, otherwise
  ``insufficient_pre`` is flagged (and the sample count is exact at the
  boundary);
* evidence is immutable: a second write of the same id through S16
  ``put_evidence`` raises, and a revision creates a brand-new record;
* a late sample inside a FINAL window creates a superseding record whose
  ``supersedes`` metadata names the old id, and the old record is byte-identical
  before and after (disk and in memory);
* replaying Δ from stored evidence after the raw ring is pruned equals the live
  Δ (σ/baseline come from P2, never from the 2-sample pre-window).

No wall clock and no database are used by the collector itself; the persistence
sink is a caller-supplied callback (P6 will supply the persist queue).  The
immutability/replay checks use a real S16 ``EvidenceRepository`` over an
in-memory SQLite database, so ``put_evidence``'s duplicate-id rejection is the
real one, not a reimplementation.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from wattmosaic_engine.contracts import (
    EventEvidence,
    MeasurementBasis,
    MeasurementEvent,
    MeasurementType,
    ReaderMeasurement,
    StepClass,
)
from wattmosaic_engine.realtime.detection import ChangeDetector, DetectorConfig
from wattmosaic_engine.realtime.reader_state import ReaderStateStore
from wattmosaic_engine.realtime.windows import (
    BATCH_SIZE,
    DEFAULT_POST_S,
    DEFAULT_PRE_S,
    DEFAULT_REQUIRED_PRE_SAMPLES,
    INSUFFICIENT_PRE,
    SUPERSEDES_KEY,
    EvidenceBatch,
    ResolvedWindowConfig,
    WindowCollector,
    WindowConfig,
    batch_evidence,
    build_evidence,
    replay_delta,
)
from wattmosaic_engine.storage.database import Database
from wattmosaic_engine.storage.repositories.evidence import EvidenceRepository

T0 = datetime(2026, 11, 18, 12, 0, 0, tzinfo=UTC)
CADENCE_S = 15.7
TOPO_V = "topo-1"


# --------------------------------------------------------------------------- #
# Helpers (pure, explicit times — no wall clock)
# --------------------------------------------------------------------------- #


def measurement(
    reader: str,
    at: datetime,
    power: float | None,
    *,
    sequence: int = 0,
) -> ReaderMeasurement:
    return ReaderMeasurement(
        reader_id=reader,
        observed_at=at,
        received_at=at + timedelta(seconds=2),
        active_power_w=power,
        phase="l1",
        measurement_type=MeasurementType.INSTANTANEOUS,
        measurement_window_ms=None,
        calibration_version="cal-1",
        measurement_basis=MeasurementBasis.CT_MEASURED.value,
        quality_flags=[],
        sequence=sequence,
    )


def event(
    *,
    event_id: str = "mev-1",
    reader: str = "Main",
    delta_w: float = 150.0,
    sigma_w: float | None = 1.0,
    step_class: StepClass = StepClass.SUSTAINED,
) -> MeasurementEvent:
    gap = step_class == StepClass.DATA_GAP  # P3b: a gap carries None, never 0
    return MeasurementEvent(
        event_id=event_id,
        reader_id=reader,
        delta_w=None if gap else delta_w,
        sigma_w=None if gap else (sigma_w if sigma_w is not None else 1.0),
        z_score=None if gap else 8.0,
        step_class=step_class,
        onset_lo=T0,
        onset_hi=T0 + timedelta(seconds=CADENCE_S),
        pre_level_w=100.0,
        post_level_w=None if gap else 100.0 + delta_w,
        quality_score=0.9,
        detected_at=T0 + timedelta(seconds=2 * CADENCE_S),
    )


class Sink:
    """Batches evidence into a real S16 repository, recording metadata too."""

    def __init__(self, repo: EvidenceRepository) -> None:
        self.repo = repo
        self.batches: list[list[EvidenceBatch]] = []
        self.metadata: dict[str, dict[str, Any]] = {}

    def put(self, batch: Sequence[EvidenceBatch]) -> None:
        for item in batch:
            self.repo.put_evidence(item.evidence)  # real duplicate-id rule
            self.metadata[item.evidence.evidence_id] = dict(item.metadata)
        self.batches.append(list(batch))


@pytest.fixture()
def repo(tmp_path: Any) -> EvidenceRepository:
    db = Database(tmp_path / "evidence.db")
    return EvidenceRepository(db)


# --------------------------------------------------------------------------- #
# Configuration resolution (global -> class -> device)
# --------------------------------------------------------------------------- #


def test_batch_evidence_chunks_for_persist_queue() -> None:
    base = event()
    items = [
        EvidenceBatch(
            build_evidence(
                event=base,
                samples=[],
                baseline_w=None,
                sigma_w=None,
                window_start=T0,
                window_end=T0 + timedelta(seconds=120),
                quality_flags=[],
                topology_version_id=TOPO_V,
                evidence_id=f"evd-{i}",
            )
        )
        for i in range(BATCH_SIZE + 3)
    ]
    batches = batch_evidence(items)
    assert [len(b) for b in batches] == [BATCH_SIZE, 3]


def test_window_config_defaults() -> None:
    cfg = WindowConfig()
    assert cfg.pre_s == DEFAULT_PRE_S == 30.0
    assert cfg.post_s == DEFAULT_POST_S == 120.0
    assert cfg.min_pre_samples == DEFAULT_REQUIRED_PRE_SAMPLES == 2


def test_window_config_resolves_global_class_device() -> None:
    cfg = WindowConfig(
        pre_s=30.0,
        post_s=120.0,
        min_pre_samples=2,
        by_class={"light": ResolvedWindowConfig(pre_s=15.0, post_s=60.0)},
        by_reader={"Main": ResolvedWindowConfig(post_s=45.0)},
    )
    assert cfg.resolve("Other", ()) == ("global", cfg.global_config)
    # The class override fills only the fields it names; the rest inherit the
    # global defaults, just like IsolationConfig.
    assert cfg.resolve("Lamp", ("light",)) == (
        "class",
        ResolvedWindowConfig(15.0, 60.0, 2),
    )
    assert cfg.resolve("Main", ("light",)) == (
        "device",
        ResolvedWindowConfig(30.0, 45.0, 2),
    )


# --------------------------------------------------------------------------- #
# Pre-window capture and insufficient_pre
# --------------------------------------------------------------------------- #


def test_pre_window_holds_two_samples_at_15_7s_cadence() -> None:
    store = ReaderStateStore()
    cfg = WindowConfig(min_pre_samples=2)
    collector = WindowCollector(store, cfg, topology_version_id=TOPO_V)
    when = T0 + timedelta(seconds=2 * CADENCE_S)
    store.add(measurement("Main", T0 - timedelta(seconds=CADENCE_S), 100.0, sequence=0))
    store.add(measurement("Main", T0, 100.0, sequence=1))
    window = collector.open(event(), at=when)

    assert window is not None
    pre = [t for t, _ in window.pre_samples]
    assert pre == [T0 - timedelta(seconds=CADENCE_S), T0]
    assert INSUFFICIENT_PRE not in window.flags
    assert window.evidence.baseline_w == 100.0
    assert window.evidence.sigma_w == 1.0
    assert window.evidence.window_start == T0 - timedelta(seconds=CADENCE_S)
    assert window.evidence.window_end == when + timedelta(seconds=DEFAULT_POST_S)
    assert window.evidence.topology_version_id == TOPO_V


def test_insufficient_pre_when_fewer_than_two_samples() -> None:
    store = ReaderStateStore()
    collector = WindowCollector(store)
    when = T0 + timedelta(seconds=2 * CADENCE_S)
    store.add(measurement("Main", T0, 100.0, sequence=0))
    window = collector.open(event(), at=when)

    assert window is not None
    assert len(window.pre_samples) == 1
    assert INSUFFICIENT_PRE in window.flags
    # The flag is carried in window/batch metadata (K02 QualityFlag has no such
    # member), so a consumer can see why the pre-window was thin.
    assert window.metadata[INSUFFICIENT_PRE] is True
    assert window.evidence.samples == []  # no post-samples captured yet


def test_data_gap_event_does_not_open_a_window() -> None:
    store = ReaderStateStore()
    collector = WindowCollector(store)
    gap = event(step_class=StepClass.DATA_GAP)
    assert collector.open(gap, at=T0) is None
    assert collector.windows == {}


# --------------------------------------------------------------------------- #
# PROVISIONAL / FINAL lifecycle, callbacks, batching
# --------------------------------------------------------------------------- #


def test_provisional_then_final_batched_and_callbacks(repo: EvidenceRepository) -> None:
    store = ReaderStateStore()
    sink = Sink(repo)
    provisional: list[EventEvidence] = []
    finals: list[EventEvidence] = []
    collector = WindowCollector(
        store,
        sink=sink.put,
        on_provisional=provisional.append,
        on_final=finals.append,
    )
    store.add(measurement("Main", T0 - timedelta(seconds=CADENCE_S), 100.0, sequence=0))
    store.add(measurement("Main", T0, 100.0, sequence=1))
    window = collector.open(event(), at=T0 + timedelta(seconds=CADENCE_S))
    assert window is not None and window.evidence.finalized is False

    # First post-sample flips the window to PROVISIONAL: a new immutable record
    # with a distinct id, persisted as one row.  Further post-samples snapshot
    # new records but do not re-announce PROVISIONAL.
    first = collector.on_sample("Main", T0 + timedelta(seconds=2 * CADENCE_S), 250.0)
    assert provisional and provisional[-1].finalized is False
    assert provisional[-1].evidence_id == first.evidence_id
    assert provisional[-1].evidence_id != window.evidence.evidence_id
    assert sink.batches == [[sink.batches[0][0]]]
    assert len(sink.batches[0]) == 1
    second = collector.on_sample("Main", T0 + timedelta(seconds=3 * CADENCE_S), 251.0)
    assert len(provisional) == 1  # announced only at the first post-sample
    assert second is not None and second.evidence_id != first.evidence_id
    assert len(sink.batches) == 2

    # Post-window expiry FINALIZES a new immutable record; the provisional one
    # is untouched on disk.
    final = collector.tick(T0 + timedelta(seconds=131 * CADENCE_S))
    assert len(final) == 1 and final[0].finalized is True
    assert finals[-1].evidence_id == final[0].evidence_id
    assert len(sink.batches) == 3
    assert repo.evidence(provisional[-1].evidence_id) is not None


def test_close_finalizes_without_post_samples(repo: EvidenceRepository) -> None:
    store = ReaderStateStore()
    sink = Sink(repo)
    finals: list[EventEvidence] = []
    collector = WindowCollector(store, sink=sink.put, on_final=finals.append)
    store.add(measurement("Main", T0 - timedelta(seconds=CADENCE_S), 100.0, sequence=0))
    store.add(measurement("Main", T0, 100.0, sequence=1))
    collector.open(event(), at=T0 + timedelta(seconds=CADENCE_S))

    collector.tick(T0 + timedelta(seconds=200 * CADENCE_S))
    assert len(finals) == 1 and finals[0].finalized is True
    assert finals[0].samples == []
    assert len(sink.batches) == 1


# --------------------------------------------------------------------------- #
# Immutability and revision
# --------------------------------------------------------------------------- #


def test_reput_same_id_raises(repo: EvidenceRepository) -> None:
    store = ReaderStateStore()
    sink = Sink(repo)
    collector = WindowCollector(store, sink=sink.put)
    store.add(measurement("Main", T0 - timedelta(seconds=CADENCE_S), 100.0, sequence=0))
    store.add(measurement("Main", T0, 100.0, sequence=1))
    collector.open(event(), at=T0 + timedelta(seconds=CADENCE_S))
    collector.on_sample("Main", T0 + timedelta(seconds=2 * CADENCE_S), 250.0)
    final = collector.close("Main")
    assert final is not None

    with pytest.raises(sqlite3.IntegrityError):
        repo.put_evidence(final)

    # A revision gets a new id, so persisting it is a legal second insert.  The
    # collector already wrote it through its sink; assert the stored row.
    revised = collector.revise(final, reason="late_sample")
    assert revised.evidence_id != final.evidence_id
    assert repo.evidence(revised.evidence_id) == revised


def test_late_sample_supersedes_final_and_keeps_old_byte_identical(repo: EvidenceRepository) -> None:
    store = ReaderStateStore()
    sink = Sink(repo)
    finals: list[EventEvidence] = []
    collector = WindowCollector(store, sink=sink.put, on_final=finals.append)
    store.add(measurement("Main", T0 - timedelta(seconds=CADENCE_S), 100.0, sequence=0))
    store.add(measurement("Main", T0, 100.0, sequence=1))
    collector.open(event(), at=T0 + timedelta(seconds=CADENCE_S))
    collector.on_sample("Main", T0 + timedelta(seconds=2 * CADENCE_S), 250.0)
    final = collector.close("Main")
    assert final is not None
    old_bytes = final.model_dump_json()

    # A late sample whose observed time falls inside the already-FINAL window.
    late_at = final.window_start + timedelta(seconds=5 * CADENCE_S)
    late = collector.on_late_sample("Main", late_at, 260.0)
    assert late is not None
    assert late.evidence_id != final.evidence_id
    assert sink.metadata[late.evidence_id][SUPERSEDES_KEY] == final.evidence_id
    assert sink.metadata[late.evidence_id]["reason"] == "late_sample"
    assert (late_at, 260.0) in late.samples
    assert finals[-1].evidence_id == late.evidence_id
    # The old record was never touched (in memory or on disk).
    assert final.model_dump_json() == old_bytes
    assert repo.evidence(final.evidence_id) == final


# --------------------------------------------------------------------------- #
# Replay after raw samples are pruned / data gaps
# --------------------------------------------------------------------------- #


def test_replay_delta_from_evidence_equals_live_delta_after_prune(repo: EvidenceRepository) -> None:
    store = ReaderStateStore()
    sink = Sink(repo)
    collector = WindowCollector(store, sink=sink.put)
    store.add(measurement("Main", T0 - timedelta(seconds=CADENCE_S), 100.0, sequence=0))
    store.add(measurement("Main", T0, 100.0, sequence=1))
    collector.open(event(), at=T0 + timedelta(seconds=CADENCE_S))
    collector.on_sample("Main", T0 + timedelta(seconds=2 * CADENCE_S), 250.0)
    final = collector.close("Main")
    assert final is not None
    live_delta = final.delta_w

    # Drop the raw ring entirely (as retention would after 30 days).
    store._rings.clear()
    assert store.reader_state("Main") is None

    stored = repo.evidence(final.evidence_id)
    assert stored == final
    assert replay_delta(stored) == live_delta


def test_data_gap_sample_keeps_none_delta_and_is_never_zero(repo: EvidenceRepository) -> None:
    store = ReaderStateStore()
    sink = Sink(repo)
    collector = WindowCollector(store, sink=sink.put)
    store.add(measurement("Main", T0 - timedelta(seconds=CADENCE_S), 100.0, sequence=0))
    store.add(measurement("Main", T0, 100.0, sequence=1))
    collector.open(event(), at=T0 + timedelta(seconds=CADENCE_S))

    collector.on_sample("Main", T0 + timedelta(seconds=2 * CADENCE_S), None)
    final = collector.close("Main")
    assert final is not None
    assert final.delta_w == 150.0  # Δ comes from P3, not from a single missing sample
    assert replay_delta(final) == 150.0
    assert (T0 + timedelta(seconds=2 * CADENCE_S), None) in final.samples
    # An absent sample is recorded as None, not 0.0.
    assert all(value is None or isinstance(value, float) for _, value in final.samples)


def test_build_evidence_keeps_absent_sigma_as_none() -> None:
    # There is no long-run σ estimate yet: it stays None, never a fabricated
    # 0.0 (HB §18).  The Δ itself is a real measured value from P3.
    evidence = build_evidence(
        event=event(),
        samples=[(T0, None)],
        baseline_w=100.0,
        sigma_w=None,
        window_start=T0,
        window_end=T0 + timedelta(seconds=120),
        quality_flags=[],
        topology_version_id=TOPO_V,
        evidence_id="evd-nosigma",
    )
    assert evidence.sigma_w is None
    assert evidence.samples == [(T0, None)]
    assert replay_delta(evidence) == 150.0


def test_handoff_from_live_detector_uses_p2_sigma_not_prewindow() -> None:
    store = ReaderStateStore()
    emitted: list[MeasurementEvent] = []
    collector = WindowCollector(store)
    detector = ChangeDetector(store, emitted.append, DetectorConfig())

    # P2 needs ~64 samples of warmup before σ leaves its 1 W floor; P3 then
    # emits exactly one SUSTAINED event for the sustained step.
    values: list[float | None] = [100.0] * 64 + [250.0] * 8
    for i, value in enumerate(values):
        when = T0 + timedelta(seconds=i * CADENCE_S)
        store.add(measurement("Main", when, value, sequence=i))
        detector.process("Main", when)
        if emitted and len(collector.windows) == 0:
            collector.on_detected(emitted[-1], store, at=when)

    assert len(emitted) == 1
    assert len(collector.windows) == 1
    window = next(iter(collector.windows.values()))
    assert window.evidence.delta_w == pytest.approx(150.0)
    # σ comes from P2's long-run per-reader estimate, not the ~2 samples in the
    # pre-window (DEC-18) and not P3's event-level σ.
    assert window.evidence.sigma_w == pytest.approx(store.sigma("Main"))
    assert window.evidence.sigma_w is not None and window.evidence.sigma_w >= 1.0
