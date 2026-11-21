"""A declared SAME_CONDUCTOR alias is never a CONTAINS endpoint while its canonical is fresh.

Regression (real-history replay, 2026-11-20): a slow alias meter (one sample per
~100 s) almost never steps in the same event group as its fast canonical CT,
so the per-group rule ("suppress the alias while the canonical steps in this
group") let the alias collect CONTAINS evidence as the parent of the
canonical's children, and one such edge became STRUCTURAL.  Freshness is now
time based (the runtime passes ``canonical_fresh``).  Names are arbitrary.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from wattmosaic_engine.contracts import (
    EdgeEvidence,
    RelationType,
    TopologyEdgeState,
    TopologyEdgeV3,
)
from wattmosaic_engine.electrical.topology_inference import DeltaObservation, TopologyInferenceView

T0 = datetime(2026, 1, 1, tzinfo=UTC)
ALIASES = {"alias_r": "canonical_r"}
PAIRS = frozenset({frozenset({"alias_r", "canonical_r"})})


def _contains_readers(view: TopologyInferenceView) -> set[str]:
    return {
        reader
        for edge in view.edges
        if str(getattr(edge.relation, "value", edge.relation)) == "contains"
        and edge.lifecycle not in (TopologyEdgeState.REJECTED, TopologyEdgeState.RETIRED)
        for reader in (edge.source_endpoint_id, edge.target_endpoint_id)
    }


def _fold(view: TopologyInferenceView, i: int, **kwargs: object) -> TopologyInferenceView:
    # The alias and a child step together; the fresh canonical is observed and
    # moved with them but did not step in this group (its P3 step fell in
    # another group).
    return view.observe_group(
        DeltaObservation(
            group_id=f"g{i}",
            deltas={"alias_r": (150.0, 2.0), "child_r": (150.0, 2.0)},
            at=T0 + timedelta(minutes=i),
            cause_id=f"cause-{i}",
        ),
        observed={"canonical_r": (2.0, 150.0)},
        declared_pairs=PAIRS,
        alias_canonical=ALIASES,
        **kwargs,
    )


def test_alias_of_a_fresh_canonical_never_gets_contains_edges() -> None:
    view = TopologyInferenceView(at=T0)
    for i in range(10):
        view = _fold(view, i, canonical_fresh=frozenset({"canonical_r"}))
    assert "alias_r" not in _contains_readers(view)


def test_alias_of_a_stale_canonical_folds_under_the_canonical() -> None:
    view = TopologyInferenceView(at=T0)
    for i in range(10):
        view = view.observe_group(
            DeltaObservation(
                group_id=f"g{i}",
                deltas={"alias_r": (150.0, 2.0), "child_r": (150.0, 2.0)},
                at=T0 + timedelta(minutes=i),
                cause_id=f"cause-{i}",
            ),
            declared_pairs=PAIRS,
            alias_canonical=ALIASES,
            canonical_fresh=frozenset(),
        )
    readers = _contains_readers(view)
    assert "alias_r" not in readers
    edge = view.edge("canonical_r", "child_r")
    assert edge is not None and edge.evidence.support >= 1


def test_existing_inferred_alias_edge_is_retired() -> None:
    inferred = TopologyEdgeV3(
        edge_id="te-inf::alias_r::child_r",
        relation=RelationType.CONTAINS,
        source_endpoint_id="alias_r",
        target_endpoint_id="child_r",
        lifecycle=TopologyEdgeState.STRUCTURAL,
        declared_by="inference",
        beta=1.0,
        beta_basis="unity",
        evidence=EdgeEvidence(support=20, independent_causes=3),
        valid_from=T0,
        pinned=False,
    )
    view = TopologyInferenceView(edges=(inferred,), at=T0).observe_group(
        DeltaObservation(group_id="g0", deltas={"elsewhere": (50.0, 2.0)}, at=T0, cause_id="c0"),
        declared_pairs=PAIRS,
        alias_canonical=ALIASES,
        canonical_fresh=frozenset({"canonical_r"}),
    )
    assert view.edge("alias_r", "child_r").lifecycle == TopologyEdgeState.RETIRED


def test_runtime_freshness_uses_the_slower_cadence_window() -> None:
    from wattmosaic_engine.core.runtime import RuntimeMixin

    now = T0 + timedelta(hours=1)
    last = {"canonical_r": now - timedelta(seconds=200), "other_c": now - timedelta(seconds=900)}

    def window(reader: str, since: datetime, until: datetime) -> tuple[object, ...]:
        at = last.get(reader)
        if at is None or not since <= at <= until:
            return ()
        return (SimpleNamespace(observed_at=at, active_power_w=100.0),)

    cadences = {"canonical_r": 15.7, "alias_r": 100.0, "other_c": 15.7, "other_a": 15.7}
    state = SimpleNamespace(
        window=window, cadence=lambda reader: SimpleNamespace(seconds=cadences.get(reader))
    )
    core = SimpleNamespace(
        reader_state=state,
        _clock=lambda: now,
        _ALIAS_STALE_MIN_S=RuntimeMixin._ALIAS_STALE_MIN_S,
        _ALIAS_STALE_FACTOR=RuntimeMixin._ALIAS_STALE_FACTOR,
    )
    core._parent_sample_interval_uncapped = lambda reader: RuntimeMixin._parent_sample_interval_uncapped(  # noqa: E731
        core, reader
    )
    fresh = RuntimeMixin._fresh_canonicals(
        core, {"alias_r": "canonical_r", "other_a": "other_c"}  # type: ignore[arg-type]
    )
    # 200 s ago is inside 3 x 100 s (the alias's slower cadence); 900 s is stale.
    assert fresh == frozenset({"canonical_r"})
