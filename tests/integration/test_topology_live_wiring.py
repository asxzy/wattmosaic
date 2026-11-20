"""Topology linking v2 live wiring (design §5 T5): health persistence, aligned series, API.

* the edge-health book survives a restart (``engine_state`` key ``t5_edge_health``);
* the runtime keeps a bounded 30 s structural memory and hands the ledger one
  cached aligned series object between rebuilds;
* ``GET /topology/v3`` CONTAINS rows carry ``health``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from wattmosaic_engine.config.settings import EngineConfig
from wattmosaic_engine.contracts import EdgeEvidence, RelationType, TopologyEdgeState, TopologyEdgeV3
from wattmosaic_engine.core.topology_api import with_edge_health
from wattmosaic_engine.electrical.topology_health import HealthBook
from wattmosaic_engine.main import EngineCore
from wattmosaic_engine.storage.database import Database

T0 = datetime(2026, 11, 20, 7, 0, 0, tzinfo=UTC)


def _core(tmp_path: Path, clock) -> EngineCore:
    db = Database(tmp_path / "wiring.db")
    return EngineCore(EngineConfig(database_path=str(tmp_path / "wiring.db"), ha_token="x"),
                      db, clock=clock)


def test_edge_health_is_persisted_and_restored(tmp_path: Path) -> None:
    core = _core(tmp_path, lambda: T0)
    book = HealthBook().record("feed", "load", "conflict", T0).record("feed", "load", "support", T0)
    core._persist_edge_health(book)  # noqa: SLF001
    core._wire_v3_services()  # noqa: SLF001 - production start path
    restored = core.topology_inference.health
    assert restored.get("feed", "load").recent == ("c", "s")


def test_aligned_series_are_built_from_a_bounded_history_and_cached(tmp_path: Path) -> None:
    now = [T0]
    core = _core(tmp_path, lambda: now[0])
    for i in range(120):
        at = T0 + timedelta(seconds=5 * i)
        core._t5_record("a", at, 100.0 + (i // 10) * 50.0)  # noqa: SLF001
        if i % 3 == 0:
            core._t5_record("b", at, 40.0)  # noqa: SLF001
    memory = core._t5_memory  # noqa: SLF001
    step = memory.step_s
    snapshot = memory.series(T0.timestamp() + 595)
    # Six samples share each 30 s bucket: only the last one is kept.
    assert len(snapshot["a"]) == 20
    assert snapshot["a"][0] == 100.0 + (5 // 10) * 50.0
    assert snapshot["a"][3] == 100.0 + (23 // 10) * 50.0
    # Late samples never rewrite the past.
    core._t5_record("a", T0, 9999.0)  # noqa: SLF001
    assert memory.series(T0.timestamp() + 595)["a"][0] != 9999.0
    assert step == 30.0

    now[0] = T0 + timedelta(seconds=600)
    first = core._t5_aligned_series()  # noqa: SLF001
    assert first is not None and set(first) == {"a", "b"}
    assert len(first["a"]) == len(first["b"]) == 21
    assert first["b"][-1] == 40.0  # a quiet reader is held (recorder semantics)
    now[0] = T0 + timedelta(seconds=700)
    assert core._t5_aligned_series() is first  # noqa: SLF001 - cached object
    now[0] = T0 + timedelta(seconds=1000)
    assert core._t5_aligned_series() is not first  # noqa: SLF001 - rebuilt


def test_topology_v3_rows_gain_health() -> None:
    edge = TopologyEdgeV3(
        edge_id="te-inf::feed::load", relation=RelationType.CONTAINS,
        source_endpoint_id="feed", target_endpoint_id="load",
        lifecycle=TopologyEdgeState.STRUCTURAL, declared_by="inference",
        evidence=EdgeEvidence(support=9, independent_causes=2), valid_from=T0, pinned=False,
    )
    result = {"edges": [
        {"relation": "contains", "source_endpoint_id": "feed", "target_endpoint_id": "load",
         "edge_id": edge.edge_id},
        {"relation": "contains", "source_endpoint_id": "x", "target_endpoint_id": "y"},
    ]}
    book = HealthBook().record("feed", "load", "support", T0)
    rows = with_edge_health(result, book)["edges"]
    assert rows[0]["health"]["recent"] == ["s"]
    assert rows[0]["health"]["status"] == "unknown"
    assert "parent" not in rows[0]["health"] and "child" not in rows[0]["health"]
    assert "health" not in rows[1]
