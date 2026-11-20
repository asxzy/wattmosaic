"""Topology linking v2 runtime fixes (design §5 T5, ADR-0033).

* a fold reads the ledger view under the ledger lock (a concurrent history
  merge is never overwritten by a stale view);
* the structural decisions survive a restart (``engine_state``);
* the edge-health book is written when it changed, at most every ~30 s;
* a sample stamped in the future never freezes a reader's aligned history.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from wattmosaic_engine.config.settings import EngineConfig
from wattmosaic_engine.contracts import (
    EdgeEvidence,
    EdgeLifecycleV3,
    EventGroup,
    EventGroupStatus,
    EvidenceRef,
    ReaderDelta,
    ReaderMeasurement,
    RelationType,
    TopologyEdgeV3,
    VersionTriple,
)
from wattmosaic_engine.electrical.topology_health import HealthBook
from wattmosaic_engine.main import EngineCore
from wattmosaic_engine.storage.database import Database

T0 = datetime(2026, 11, 20, 7, 0, 0, tzinfo=UTC)


def _core(tmp_path: Path, clock) -> EngineCore:
    db = Database(tmp_path / "fixes.db")
    core = EngineCore(EngineConfig(database_path=str(tmp_path / "fixes.db"), ha_token="x"),
                      db, clock=clock)
    core._wire_v3_services()  # noqa: SLF001 - production start path
    return core


def _delta(reader: str, watts: float) -> ReaderDelta:
    return ReaderDelta(
        reader_id=reader, delta_w=watts, sigma_w=1.0, completeness="complete",
        evidence_ref=EvidenceRef(kind="event_evidence", id=f"evd-{reader}"),
    )


def _group(group_id: str, reader: str = "branch", *, deltas: list[ReaderDelta] | None = None) -> EventGroup:
    return EventGroup(
        group_id=group_id,
        revision=1,
        status=EventGroupStatus.CONFIRMED,
        opened_at=T0,
        closed_at=T0 + timedelta(seconds=30),
        reader_deltas=deltas or [_delta(reader, 200.0)],
        versions=VersionTriple(
            topology_version_id="topo-0", mapping_version_id="map-0", model_set_id="mset-0"
        ),
    )


class _SignallingLock:
    """The ledger lock, announcing each attempt to enter it."""

    def __init__(self, inner: threading.Lock, entering: threading.Event) -> None:
        self._inner = inner
        self._entering = entering

    def __enter__(self):
        self._entering.set()
        self._inner.acquire()
        return self

    def __exit__(self, *exc):
        self._inner.release()
        return False


def test_a_fold_never_overwrites_a_concurrent_merge(tmp_path: Path) -> None:
    core = _core(tmp_path, lambda: T0)
    inner = threading.Lock()
    entering = threading.Event()
    core._ledger_lock = _SignallingLock(inner, entering)  # noqa: SLF001
    marker = TopologyEdgeV3(
        edge_id="te-user::merged::marker", relation=RelationType.CONTAINS,
        source_endpoint_id="merged", target_endpoint_id="marker",
        lifecycle=EdgeLifecycleV3.CONFIRMED, declared_by="user",
        evidence=EdgeEvidence(), valid_from=T0, pinned=False,
    )
    inner.acquire()  # a history merge holds the lock ...
    worker = threading.Thread(target=core._fold_group_ledger, args=(_group("g-race"),))  # noqa: SLF001
    worker.start()
    assert entering.wait(5.0)
    # ... and publishes its merged view before releasing it.
    current = core.topology_inference
    core.topology_inference = replace(current, edges=(*current.edges, marker))
    inner.release()
    worker.join(5.0)
    assert not worker.is_alive()
    assert core.topology_inference.edge("merged", "marker") is not None
    assert "g-race" in core.topology_inference.seen_groups


def test_structure_decisions_survive_a_restart(tmp_path: Path) -> None:
    core = _core(tmp_path, lambda: T0)
    core._persist_structure_decisions({"sub_1": "main_2"})  # noqa: SLF001
    restarted = _core(tmp_path, lambda: T0)
    assert dict(restarted.topology_inference.structure_decisions) == {"sub_1": "main_2"}


def test_a_fold_persists_changed_structure_decisions(tmp_path: Path) -> None:
    core = _core(tmp_path, lambda: T0)
    core.topology_inference = replace(core.topology_inference, structure_decisions={"a": "b"})
    # The settle forgets a decision whose edge is gone; the fold persists that.
    core._fold_group_ledger(_group("g1"))  # noqa: SLF001
    assert dict(core.topology_inference.structure_decisions) == {}
    assert core.db.get_state("t5_structure_decisions") == "{}"


def test_edge_health_writes_are_throttled(tmp_path: Path) -> None:
    now = [T0]
    core = _core(tmp_path, lambda: now[0])
    writes: list[str] = []
    core._persist_edge_health = lambda book: writes.append(book.to_json())  # noqa: SLF001
    book = HealthBook().record("feed", "load", "support", T0)
    core._persist_edge_health_throttled(book, changed=True)  # noqa: SLF001
    assert len(writes) == 1
    now[0] = T0 + timedelta(seconds=10)
    core._persist_edge_health_throttled(book, changed=True)  # noqa: SLF001
    core._persist_edge_health_throttled(book, changed=False)  # noqa: SLF001
    assert len(writes) == 1
    now[0] = T0 + timedelta(seconds=31)
    core._persist_edge_health_throttled(book, changed=False)  # noqa: SLF001 - pending change
    assert len(writes) == 2
    now[0] = T0 + timedelta(seconds=90)
    core._persist_edge_health_throttled(book, changed=False)  # noqa: SLF001 - nothing new
    assert len(writes) == 2


def _reading(at: datetime, received: datetime, watts: float, seq: int) -> ReaderMeasurement:
    return ReaderMeasurement(
        reader_id="skewed", observed_at=at, received_at=received, active_power_w=watts,
        sequence=seq, phase="unknown", measurement_type="instantaneous",
        calibration_version="discovery", measurement_basis="ct_measured",
    )


async def test_a_future_sample_never_freezes_the_aligned_history(tmp_path: Path) -> None:
    now = [T0]
    core = _core(tmp_path, lambda: now[0])
    await core.on_reading(_reading(T0 + timedelta(hours=1), T0, 100.0, 0))  # skewed clock
    for i in range(1, 30):
        now[0] = T0 + timedelta(seconds=10 * i)
        await core.on_reading(_reading(now[0], now[0], 100.0 + i, i))
    rows = core._t5_history["skewed"]  # noqa: SLF001
    assert rows[-1][2] == 129.0
    assert len(rows) >= 29


def test_confirmed_edges_show_health_in_the_topology_api(tmp_path: Path) -> None:
    """Live: no CONFIRMED edge in GET /topology/v3 ever carried ``health``."""
    core = _core(tmp_path, lambda: T0)
    edge = TopologyEdgeV3(
        edge_id="te-inf::feed::load", relation=RelationType.CONTAINS,
        source_endpoint_id="feed", target_endpoint_id="load",
        lifecycle=EdgeLifecycleV3.CONFIRMED, declared_by="inference", beta=1.0, beta_basis="unity",
        evidence=EdgeEvidence(support=20, independent_causes=3), valid_from=T0, pinned=False,
    )
    core.db.repos.topology_v3.upsert_edge(edge)
    view = core.topology_inference
    core.topology_inference = replace(
        view, edges=(*view.edges, edge),
        causes_by_edge={**view.causes_by_edge, ("feed", "load", "contains"): frozenset({"a", "b", "c"})},
    )
    # Before any decisive verdict the confirmed edge still shows its health.
    rows = {row["edge_id"]: row for row in core.topology_v3()["edges"]}
    assert rows["te-inf::feed::load"]["health"]["status"] == "unknown"
    assert rows["te-inf::feed::load"]["health"]["recent"] == []
    for i in range(2):
        core._fold_group_ledger(  # noqa: SLF001
            _group(f"g{i}", deltas=[_delta("feed", 300.0), _delta("load", 300.0)])
        )
    assert core.topology_inference.health.get("feed", "load").recent == ("s", "s")
    rows = {row["edge_id"]: row for row in core.topology_v3()["edges"]}
    assert rows["te-inf::feed::load"]["health"]["recent"] == ["s", "s"]
