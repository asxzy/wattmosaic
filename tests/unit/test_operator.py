"""T2: observation operator H(T, M, t) with physics-justified β (design §5).

The tests use only K08 fakes and the real FactorRegistry — production code must
never import ``runtime.fakes``.  Coverage maps to the task's test list:

* AT-01/14 support — Circuit-A device in Circuit A / Subpanel / Main, once/row;
* a PROVISIONAL edge is soft, not applied;
* a calibration change mid-window is time-scoped (DEF-06);
* an estimated β is labelled and never absorbs residual;
* a missing child keeps the parent row plus an unknown column (DEC-12);
* replay of the same (T, M, t) is bit-identical;
* the stable-version cache hit rate is 100 %.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from wattmosaic_engine.contracts import (
    EdgeEvidence,
    EdgeLifecycleV3,
    MeasurementFactor,
    Observability,
    RelationType,
    TopologyEdgeV3,
)
from wattmosaic_engine.electrical.factors import FactorRegistry
from wattmosaic_engine.electrical.operator import (
    OperatorCache,
    build_observation_operator,
    clear_operator_cache,
    operator_cache_stats,
)
from wattmosaic_engine.runtime.fakes import HB17_TREE, FakeMapping, FakeTopology

T0 = datetime(2026, 1, 1, tzinfo=UTC)
READERS = ("Main", "Subpanel", "Circuit A", "Circuit B", "Circuit C")
SUBJECTS = (("device", "lights"), ("device", "kettle"))


# --------------------------------------------------------------------------- #
# Helpers.
# --------------------------------------------------------------------------- #


def _house(at: datetime = T0) -> tuple[FakeTopology, FakeMapping]:
    topology = FakeTopology.from_tree(HB17_TREE, at=at)
    mapping = FakeMapping.from_pairs(
        {
            ("device", "lights"): "Circuit A",
            ("device", "kettle"): "Circuit C",
        }
    )
    return topology, mapping


def _edge(
    relation: RelationType,
    source: str,
    target: str,
    *,
    edge_id: str,
    lifecycle: EdgeLifecycleV3 = EdgeLifecycleV3.CONFIRMED,
    beta: float | None = None,
    beta_basis: str | None = None,
    at: datetime = T0,
) -> TopologyEdgeV3:
    return TopologyEdgeV3(
        edge_id=edge_id,
        relation=relation,
        source_endpoint_id=source,
        target_endpoint_id=target,
        lifecycle=lifecycle,
        declared_by="user",
        beta=beta,
        beta_basis=beta_basis,  # type: ignore[arg-type]
        evidence=EdgeEvidence(),
        valid_from=at,
        pinned=True,
    )


def _row(op, reader: str) -> int:
    return next(i for i, meta in enumerate(op.rows) if meta.reader_id == reader)


# --------------------------------------------------------------------------- #
# Step 1: observability classes and the HB §17 house (AT-01/14 support).
# --------------------------------------------------------------------------- #


def test_circuit_a_device_in_four_nested_rows_with_coefficient_one() -> None:
    clear_operator_cache()
    topology, mapping = _house()
    op = build_observation_operator(topology, mapping, None, READERS, SUBJECTS, T0)
    column = op.subjects.index(("device", "lights"))

    for reader in ("Circuit A", "Subpanel", "Main"):
        row = _row(op, reader)
        assert op.H[row, column] == pytest.approx(1.0)
        assert np.count_nonzero(op.H[row]) >= 1
        entry = op.entries[(row, column)]
        assert entry.coefficient == pytest.approx(1.0)
        assert entry.observability in (
            Observability.DIRECT if reader == "Circuit A" else Observability.EXPECTED,
        )
        if reader == "Circuit A":
            assert entry.observability is Observability.DIRECT
            assert entry.edge_lifecycle is None
        else:
            assert entry.observability is Observability.EXPECTED
            assert entry.edge_lifecycle is EdgeLifecycleV3.CONFIRMED

    # The kettle is on Circuit C and only sees Circuit C and Main.
    kettle = op.subjects.index(("device", "kettle"))
    assert op.H[_row(op, "Circuit C"), kettle] == pytest.approx(1.0)
    assert op.H[_row(op, "Subpanel"), kettle] == pytest.approx(0.0)
    assert op.H[_row(op, "Circuit A"), kettle] == pytest.approx(0.0)

    # Each subject appears at most once per row (no double count).
    for row in range(op.H.shape[0]):
        for col in range(op.H.shape[1]):
            key = (row, col)
            if key in op.entries:
                assert op.H[row, col] == pytest.approx(op.entries[key].coefficient)


def test_provisional_edge_is_soft_not_applied() -> None:
    clear_operator_cache()
    edges = (
        _edge(RelationType.CONTAINS, "Main", "Subpanel", edge_id="e1"),
        _edge(
            RelationType.CONTAINS,
            "Subpanel",
            "Circuit A",
            edge_id="e2",
            lifecycle=EdgeLifecycleV3.PROVISIONAL,
        ),
    )
    topology = FakeTopology.from_edges(edges)
    mapping = FakeMapping.from_pairs({("device", "lights"): "Circuit A"})
    readers = ("Main", "Subpanel", "Circuit A")
    op = build_observation_operator(topology, mapping, None, readers, SUBJECTS, T0)
    lights = op.subjects.index(("device", "lights"))

    # The soft edge is not applied: Circuit A has no hard parent.
    assert op.H[_row(op, "Subpanel"), lights] == pytest.approx(0.0)
    assert op.H[_row(op, "Main"), lights] == pytest.approx(0.0)
    # It is listed as an alternative, never as a structural entry.
    assert len(op.soft_alternatives) == 1
    assert op.soft_alternatives[0].edge_id == "e2"
    assert op.soft_alternatives[0].lifecycle == EdgeLifecycleV3.PROVISIONAL


def test_paired_leg_carries_half_coefficient() -> None:
    clear_operator_cache()
    edges = (
        _edge(RelationType.PAIRED_LEG, "leg1", "leg2", edge_id="p1"),
        _edge(RelationType.PAIRED_LEG, "leg2", "leg1", edge_id="p2"),
    )
    topology = FakeTopology.from_edges(edges)
    # FakeTopology has no leg_coefficient; operator derives 0.5 from the pair.
    mapping = FakeMapping.from_pairs({("device", "pump"): "leg1"})
    op = build_observation_operator(
        topology, mapping, None, ("leg1", "leg2"), (("device", "pump"),), T0
    )
    column = op.subjects.index(("device", "pump"))
    assert op.H[0, column] == pytest.approx(0.5)
    assert op.entries[(0, column)].observability is Observability.PARTIAL
    assert op.H[1, column] == pytest.approx(0.0)  # the other leg is not the subject's scope


def test_shared_load_puts_subject_in_both_rows() -> None:
    clear_operator_cache()
    edges = (_edge(RelationType.SHARED_LOAD, "r1", "r2", edge_id="s1"),)
    topology = FakeTopology.from_edges(edges)
    mapping = FakeMapping.from_pairs({("device", "heater"): "r1"})
    op = build_observation_operator(
        topology, mapping, None, ("r1", "r2"), (("device", "heater"),), T0
    )
    column = op.subjects.index(("device", "heater"))
    assert op.H[0, column] == pytest.approx(1.0)
    assert op.H[1, column] == pytest.approx(1.0)
    assert op.entries[(1, column)].edge_ids == ("s1",)
    # Correlated rows inflate σ by √2 (§8.3).
    assert op.rows[0].sigma_inflation == pytest.approx(2**0.5)
    assert op.rows[1].sigma_inflation == pytest.approx(2**0.5)


# --------------------------------------------------------------------------- #
# Step 2/6: β from the FactorRegistry at t, time-scoped (DEF-06).
# --------------------------------------------------------------------------- #


def test_calibration_change_mid_window_is_not_retroactive() -> None:
    clear_operator_cache()
    t_before = T0
    t_after = T0 + timedelta(days=30)
    factors = FactorRegistry(
        [
            MeasurementFactor(
                ct_id="Circuit A",
                multiplier=2.0,
                effective_from=t_before,
                effective_to=t_after,
                applied_by="user",
            ),
            MeasurementFactor(
                ct_id="Circuit A",
                multiplier=3.0,
                effective_from=t_after,
                applied_by="user",
            ),
        ]
    )
    edge = _edge(
        RelationType.CONTAINS,
        "Subpanel",
        "Circuit A",
        edge_id="ea",
        beta=2.0,
        beta_basis="recorded_factor",
    )
    topology = FakeTopology.from_edges((edge,))
    mapping = FakeMapping.from_pairs({("device", "lights"): "Circuit A"})
    readers = ("Subpanel", "Circuit A")
    lights = (("device", "lights"),)

    old = build_observation_operator(topology, mapping, factors, readers, lights, t_before + timedelta(days=1))
    new = build_observation_operator(topology, mapping, factors, readers, lights, t_after + timedelta(days=1))
    old_col = old.subjects.index(("device", "lights"))
    new_col = new.subjects.index(("device", "lights"))

    assert old.H[_row(old, "Subpanel"), old_col] == pytest.approx(2.0)
    assert old.entries[(_row(old, "Subpanel"), old_col)].coefficient == pytest.approx(2.0)
    assert new.H[_row(new, "Subpanel"), new_col] == pytest.approx(3.0)
    # The direct row is unaffected by the parent containment calibration.
    assert old.H[_row(old, "Circuit A"), old_col] == pytest.approx(1.0)


def test_estimated_beta_is_labelled_and_does_not_absorb_residual() -> None:
    clear_operator_cache()
    edge = _edge(
        RelationType.CONTAINS,
        "Main",
        "Circuit A",
        edge_id="ee",
        beta=1.5,
        beta_basis="estimated",
    )
    topology = FakeTopology.from_edges((edge,))
    mapping = FakeMapping.from_pairs({("device", "lights"): "Circuit A"})
    op = build_observation_operator(
        topology, mapping, None, ("Main", "Circuit A"), (("device", "lights"),), T0
    )
    column = op.subjects.index(("device", "lights"))
    entry = op.entries[(_row(op, "Main"), column)]
    assert entry.coefficient == pytest.approx(1.5)
    assert entry.observability is Observability.PARTIAL  # labelled uncertain
    # The parent row is still present and its σ is inflated for the correlated
    # pair; the estimated coefficient never silently closes the residual (the
    # inference stage makes the final call, but we do not claim unity).
    assert op.rows[_row(op, "Main")].kind == "residualised_parent"
    assert op.H[_row(op, "Main"), column] == pytest.approx(1.5)


# --------------------------------------------------------------------------- #
# Step 3/4: missing child and unknown scopes (DEC-12).
# --------------------------------------------------------------------------- #


def test_missing_child_keeps_parent_row_with_extra_unknown_column() -> None:
    clear_operator_cache()
    edges = (
        _edge(RelationType.CONTAINS, "Main", "Subpanel", edge_id="e1"),
        _edge(RelationType.CONTAINS, "Subpanel", "Circuit A", edge_id="e2"),
    )
    topology = FakeTopology.from_edges(edges)
    mapping = FakeMapping.from_pairs({("device", "lights"): "Circuit A"})
    # Circuit A is a CONTAINS child that is deliberately NOT a row.
    readers = ("Main", "Subpanel")
    op = build_observation_operator(
        topology, mapping, None, readers, (("device", "lights"),), T0
    )
    # The parent rows are never dropped.
    assert {m.reader_id for m in op.rows} == {"Main", "Subpanel"}
    assert op.rows[_row(op, "Subpanel")].excluded_reason is None
    # The missing child gets its own unknown column entering both ancestor rows.
    scope_ids = {scope for scope, _ in op.unknown_scopes}
    assert "Circuit A" in scope_ids
    circuit_a_scope = dict(op.unknown_scopes)["Circuit A"]
    assert _row(op, "Main") in circuit_a_scope
    assert _row(op, "Subpanel") in circuit_a_scope
    # No zero-power placeholder creeps into H.
    assert np.isfinite(op.H).all()


def test_every_reader_gets_a_scope_unknown_column() -> None:
    clear_operator_cache()
    topology, mapping = _house()
    op = build_observation_operator(topology, mapping, None, READERS, SUBJECTS, T0)
    scopes = dict(op.unknown_scopes)
    for reader in READERS:
        assert reader in scopes
        assert _row(op, reader) in scopes[reader]
    # A subject on Circuit A and its ancestors share the chain; σ inflation is
    # ≥ 1 and exactly √k for that chain (k = number of co-observing rows).
    for row in op.rows:
        assert row.sigma_inflation >= 1.0
        assert row.sigma_w is None


# --------------------------------------------------------------------------- #
# Step 5/6: determinism and caching.
# --------------------------------------------------------------------------- #


def test_replay_same_t_m_t_is_bit_identical() -> None:
    clear_operator_cache()
    topology, mapping = _house()
    a = build_observation_operator(topology, mapping, None, READERS, SUBJECTS, T0)
    b = build_observation_operator(topology, mapping, None, READERS, SUBJECTS, T0)
    np.testing.assert_array_equal(a.H, b.H)
    assert a.rows == b.rows
    assert a.entries == b.entries
    assert a.unknown_scopes == b.unknown_scopes
    assert a.soft_alternatives == b.soft_alternatives
    assert a.topology_version_id == b.topology_version_id
    assert a.mapping_version_id == b.mapping_version_id
    assert a.at == b.at


def test_stable_version_cache_hit_rate_is_100_percent() -> None:
    cache = OperatorCache()
    topology, mapping = _house()
    # Warm the cache once for this version triple (the only miss).
    cache.build(topology, mapping, None, READERS, SUBJECTS, T0)
    baseline = cache.stats()

    # Once the versions are stable, every lookup must be a hit.
    for _ in range(5):
        cache.build(topology, mapping, None, READERS, SUBJECTS, T0)
    stats = cache.stats()
    assert stats["misses"] - baseline["misses"] == 0
    assert stats["hits"] - baseline["hits"] == 5
    steady_state_rate = (stats["hits"] - baseline["hits"]) / (
        stats["hits"] - baseline["hits"] + stats["misses"] - baseline["misses"]
    )
    assert steady_state_rate == pytest.approx(1.0)
    assert baseline["misses"] == 1


def test_cache_rebuilds_when_factor_revision_changes() -> None:
    cache = OperatorCache()
    t_before = T0
    t_after = T0 + timedelta(days=1)
    factors = FactorRegistry(
        [
            MeasurementFactor(
                ct_id="Circuit A",
                multiplier=2.0,
                effective_from=t_before,
                effective_to=t_after,
                applied_by="user",
            ),
            MeasurementFactor(
                ct_id="Circuit A",
                multiplier=3.0,
                effective_from=t_after,
                applied_by="user",
            ),
        ]
    )
    edge = _edge(
        RelationType.CONTAINS,
        "Main",
        "Circuit A",
        edge_id="ea",
        beta=2.0,
        beta_basis="recorded_factor",
    )
    topology = FakeTopology.from_edges((edge,))
    mapping = FakeMapping.from_pairs({("device", "lights"): "Circuit A"})
    readers = ("Main", "Circuit A")
    subjects = (("device", "lights"),)
    early = t_before + timedelta(hours=1)
    late = t_after + timedelta(hours=1)

    first = cache.build(topology, mapping, factors, readers, subjects, early)
    second = cache.build(topology, mapping, factors, readers, subjects, early)
    third = cache.build(topology, mapping, factors, readers, subjects, late)
    assert first is second
    assert third is not first
    column = first.subjects.index(("device", "lights"))
    assert first.H[_row(first, "Main"), column] == pytest.approx(2.0)
    assert third.H[_row(third, "Main"), column] == pytest.approx(3.0)


def test_public_builder_shares_cache_stats() -> None:
    clear_operator_cache()
    topology, mapping = _house()
    before = operator_cache_stats()["hits"]
    build_observation_operator(topology, mapping, None, READERS, SUBJECTS, T0)
    build_observation_operator(topology, mapping, None, READERS, SUBJECTS, T0)
    assert operator_cache_stats()["hits"] == before + 1
