"""P4: user-declared PAIRED_LEG / SAME_CONDUCTOR edges constrain the T5 fold.

Coincident equal steps on the two legs of a declared pair must not create a
CONTAINS edge between them; a SAME_CONDUCTOR alias with a present canonical
reader gets no CONTAINS edges (its evidence folds under the canonical).  Names
here are arbitrary (no scenario knowledge).
"""

from __future__ import annotations

from datetime import UTC, datetime

from wattmosaic_engine.contracts import (
    EdgeEvidence,
    RelationType,
    TopologyEdgeState,
    TopologyEdgeV3,
)
from wattmosaic_engine.electrical.topology_inference import DeltaObservation, TopologyInferenceView

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _declared(relation: RelationType, source: str, target: str) -> TopologyEdgeV3:
    return TopologyEdgeV3(
        edge_id=f"decl::{relation.value}::{source}::{target}",
        relation=relation,
        source_endpoint_id=source,
        target_endpoint_id=target,
        lifecycle=TopologyEdgeState.STRUCTURAL,
        declared_by="user",
        beta=1.0,
        beta_basis="unity",
        evidence=EdgeEvidence(),
        valid_from=T0,
        pinned=False,
    )


def _conclusions(view: TopologyInferenceView) -> set[tuple[str, str]]:
    return {
        (edge.source_endpoint_id, edge.target_endpoint_id)
        for edge in view.edges
        if str(getattr(edge.relation, "value", edge.relation)) == "contains"
        and edge.lifecycle not in (TopologyEdgeState.REJECTED, TopologyEdgeState.RETIRED)
    }


def test_declared_pair_legs_never_get_contains_between_them() -> None:
    legs = frozenset({"leg_x", "leg_y"})
    edges = (
        _declared(RelationType.PAIRED_LEG, "leg_x", "leg_y"),
        _declared(RelationType.PAIRED_LEG, "leg_y", "leg_x"),
    )
    view = TopologyInferenceView(edges=edges, at=T0)
    for i in range(8):
        view = view.observe_group(
            DeltaObservation(
                group_id=f"g{i}",
                deltas={"leg_x": (150.0, 2.0), "leg_y": (150.0, 2.0)},
                at=T0,
                cause_id=f"cause-{i % 2}",
            ),
            declared_pairs=frozenset({legs}),
        )
    conclusions = _conclusions(view)
    assert ("leg_x", "leg_y") not in conclusions
    assert ("leg_y", "leg_x") not in conclusions


def test_existing_inferred_contains_between_declared_legs_is_rejected() -> None:
    inferred = TopologyEdgeV3(
        edge_id="te-inf::leg_x::leg_y",
        relation=RelationType.CONTAINS,
        source_endpoint_id="leg_x",
        target_endpoint_id="leg_y",
        lifecycle=TopologyEdgeState.STRUCTURAL,
        declared_by="inference",
        beta=1.0,
        beta_basis="unity",
        evidence=EdgeEvidence(support=20, independent_causes=3),
        valid_from=T0,
        pinned=False,
    )
    view = TopologyInferenceView(edges=(inferred,), at=T0)
    view = view.observe_group(
        DeltaObservation(
            group_id="g0",
            deltas={"elsewhere": (50.0, 2.0)},
            at=T0,
            cause_id="c0",
        ),
        declared_pairs=frozenset({frozenset({"leg_x", "leg_y"})}),
    )
    edge = view.edge("leg_x", "leg_y")
    assert edge is not None
    assert edge.lifecycle == TopologyEdgeState.REJECTED


def test_declared_alias_gets_no_contains_while_canonical_is_present() -> None:
    aliases = {"alias_r": "canonical_r"}
    view = TopologyInferenceView(at=T0)
    for i in range(8):
        view = view.observe_group(
            DeltaObservation(
                group_id=f"g{i}",
                deltas={"canonical_r": (150.0, 2.0), "alias_r": (150.0, 2.0)},
                at=T0,
                cause_id=f"cause-{i % 2}",
            ),
            declared_pairs=frozenset({frozenset({"alias_r", "canonical_r"})}),
            alias_canonical=aliases,
        )
    conclusions = _conclusions(view)
    # The alias is suppressed while the canonical reader has a reading.
    assert not any(reader == "alias_r" for pair in conclusions for reader in pair)


def test_runtime_reads_declared_alias_direction_and_ignores_inferred_edges() -> None:
    """The canonical of a declared SAME_CONDUCTOR edge is its target.

    Regression: the first P4 cut picked the canonical as the smaller endpoint
    id, which would suppress a real panel CT as a parent whenever the alias id
    happens to sort first.  Inferred SAME_CONDUCTOR/PAIRED_LEG edges carry no
    installer direction and must not constrain the fold.
    """
    from types import SimpleNamespace

    from wattmosaic_engine.contracts import RelationType, TopologyEdgeState
    from wattmosaic_engine.core.runtime import RuntimeMixin

    def edge(relation, source, target, declared_by):
        return SimpleNamespace(
            relation=relation,
            lifecycle=TopologyEdgeState.STRUCTURAL,
            source_endpoint_id=source,
            target_endpoint_id=target,
            declared_by=declared_by,
        )

    edges = [
        edge(RelationType.SAME_CONDUCTOR, "a_alias", "z_canonical", "user"),
        edge(RelationType.SAME_CONDUCTOR, "b_inferred", "c_inferred", "inference"),
        edge(RelationType.PAIRED_LEG, "leg_1", "leg_2", "user"),
    ]
    service = SimpleNamespace(
        current=lambda: SimpleNamespace(topology_version_id="tv-1"),
        topology_view=lambda _vid: SimpleNamespace(edges=lambda: edges),
    )
    core = SimpleNamespace(version_service=service)
    pairs, aliases = RuntimeMixin._declared_topology_constraints(core)  # type: ignore[arg-type]
    assert aliases == {"a_alias": "z_canonical"}
    assert frozenset({"leg_1", "leg_2"}) in pairs
    assert frozenset({"a_alias", "z_canonical"}) in pairs
    assert frozenset({"b_inferred", "c_inferred"}) not in pairs
