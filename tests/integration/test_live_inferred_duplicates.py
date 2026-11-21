"""E6 §4/§6: the inferred SAME_CONDUCTOR detector is wired into the live runtime.

E6a only called :func:`detect_inferred_duplicates` from the legacy
``infer_topology`` path, which no live caller uses.  The runtime now runs it at
aligned rebuilds, adds the edge to the T5 ledger with declared_by=inference,
and treats an inferred STRUCTURAL alias like a declared one for suppression.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
from wattmosaic_engine.config.settings import EngineConfig
from wattmosaic_engine.contracts import RelationType, TopologyEdgeState
from wattmosaic_engine.main import EngineCore
from wattmosaic_engine.storage.database import Database

T0 = datetime(2026, 11, 21, tzinfo=UTC)


def _core(now: list[datetime] | None = None) -> EngineCore:
    db = Database(":memory:")
    clock_now = now if now is not None else [T0]
    core = EngineCore(
        EngineConfig(database_path=":memory:", ha_token="x", api_token="x", learning_enabled=False),
        db,
        clock=lambda: clock_now[0],
    )
    core._wire_v3_services()  # noqa: SLF001
    return core


def test_inferred_duplicate_is_added_to_the_live_ledger() -> None:
    now = [T0 + timedelta(days=7)]
    core = _core(now)
    try:
        rng = np.random.default_rng(11)
        n = 2000
        base = 300.0 + 100.0 * np.sin(np.arange(n) / 30.0) + rng.normal(0, 2, n)
        copy = 0.95 * base + 100.0
        from wattmosaic_engine.electrical.sync_evidence import SampleStampRing

        stamps = [T0 + timedelta(seconds=120 * i) for i in range(n)]
        for index, (reader, values) in enumerate((("main", base), ("copy", copy))):
            ring = SampleStampRing(capacity=n + 1)
            core._sample_stamps[reader] = ring  # noqa: SLF001
            offset = timedelta(seconds=index * 37)  # different monitors, not co-sampled
            for stamp, value in zip(stamps, values, strict=True):
                at = stamp + offset
                core._t5_record(reader, at, float(value))  # noqa: SLF001
                ring.record(at.timestamp(), float(value))
        # Force an aligned rebuild and cache the detector output.
        aligned = core._t5_aligned_series()  # noqa: SLF001
        assert aligned is not None
        core._maybe_cache_inferred_duplicates(aligned)  # noqa: SLF001
        cached = core._t5_inferred_duplicates  # noqa: SLF001
        assert len(cached) == 1
        edge = cached[0]
        assert edge.relation == RelationType.SAME_CONDUCTOR
        assert edge.declared_by == "inference"
        assert edge.lifecycle == TopologyEdgeState.CANDIDATE

        view = core._merge_inferred_duplicate_edges(core.topology_inference)  # noqa: SLF001
        live = next(e for e in view.edges if e.relation == RelationType.SAME_CONDUCTOR)
        assert live.edge_id == edge.edge_id
    finally:
        if core.background_executor is not None:
            core.background_executor.shutdown(wait=False)
        core.db.close()


def test_inferred_structural_alias_is_suppressed_like_a_declared_one() -> None:
    from wattmosaic_engine.contracts import EdgeEvidence, TopologyEdgeV3

    core = _core()
    try:
        inferred = TopologyEdgeV3(
            edge_id="te-inf-same::canonical_r::alias_r",
            relation=RelationType.SAME_CONDUCTOR,
            source_endpoint_id="canonical_r",
            target_endpoint_id="alias_r",
            lifecycle=TopologyEdgeState.STRUCTURAL,
            declared_by="inference",
            evidence=EdgeEvidence(),
            valid_from=T0,
            pinned=False,
        )
        from dataclasses import replace as _replace

        view = _replace(core.topology_inference, edges=(inferred,))
        pairs, aliases = core._inferred_alias_constraints(view)  # noqa: SLF001
        assert frozenset(("canonical_r", "alias_r")) in pairs
        assert aliases == {"alias_r": "canonical_r"}
    finally:
        if core.background_executor is not None:
            core.background_executor.shutdown(wait=False)
        core.db.close()
