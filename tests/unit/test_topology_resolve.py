"""Topology linking v2, wave 2a: structural resolution and multi-child step counting.

Synthetic reproductions of the live facts (design §5 T5, W2-integration.md):

* hot tub legs that match both mains end up split across L1/L2, never both on one;
* ``P +100 = Y +30 + Z +70`` supports both children;
* explain-away with a confirmed child (and its health);
* crossed sub-panel feeds confirmed structurally from regression + capacity;
* a confirmed edge whose parent stops following is demoted via edge health;
* a duplicate meter is never made a child.
"""

from __future__ import annotations

import random
import time
from datetime import UTC, datetime

from wattmosaic_engine.contracts import EdgeEvidence, EdgeLifecycleV3, RelationType, TopologyEdgeV3
from wattmosaic_engine.electrical import topology_resolve as tr
from wattmosaic_engine.electrical.topology_capacity import capacity_violation, fits, paired_legs
from wattmosaic_engine.electrical.topology_health import HealthStatus
from wattmosaic_engine.electrical.topology_inference import DeltaObservation, TopologyInferenceView
from wattmosaic_engine.electrical.topology_regression import diff_regression, duplicate_meters

T0 = datetime(2026, 11, 20, tzinfo=UTC)
CONFIRMED = EdgeLifecycleV3.CONFIRMED
PROVISIONAL = EdgeLifecycleV3.PROVISIONAL


def _edge(parent: str, child: str, lifecycle: EdgeLifecycleV3, *, support: int = 20,
          conflict: int = 0) -> TopologyEdgeV3:
    return TopologyEdgeV3(
        edge_id=f"te-inf::{parent}::{child}",
        relation=RelationType.CONTAINS,
        source_endpoint_id=parent,
        target_endpoint_id=child,
        lifecycle=lifecycle,
        declared_by="inference",
        beta=1.0,
        beta_basis="unity",
        evidence=EdgeEvidence(support=support, conflict=conflict, independent_causes=3),
        valid_from=T0,
        pinned=False,
    )


def _causes(edges: list[TopologyEdgeV3]) -> dict[tuple[str, str, str], frozenset[str]]:
    return {
        (e.source_endpoint_id, e.target_endpoint_id, "contains"): frozenset({"c1", "c2", "c3"})
        for e in edges
    }


def _view(edges: list[TopologyEdgeV3], **kw) -> TopologyInferenceView:
    return TopologyInferenceView(edges=tuple(edges), causes_by_edge=_causes(edges), at=T0, **kw)


def _life(view_or_edges, parent: str, child: str) -> str:
    edges = view_or_edges.edges if hasattr(view_or_edges, "edges") else view_or_edges
    for edge in edges:
        if edge.source_endpoint_id == parent and edge.target_endpoint_id == child:
            return str(getattr(edge.lifecycle, "value", edge.lifecycle))
    return "absent"


def _steps(rng: random.Random, n: int, levels: list[float], p_change: float) -> list[float]:
    value = rng.choice(levels)
    out = []
    for _ in range(n):
        if rng.random() < p_change:
            value = rng.choice(levels)
        out.append(value)
    return out


def _noise(rng: random.Random, series: list[float], sigma: float = 5.0) -> list[float]:
    return [v + rng.gauss(0.0, sigma) for v in series]


# --------------------------------------------------------------------------- #
# (a) hot tub legs
# --------------------------------------------------------------------------- #


def _hot_tub(n: int = 1200, seed: int = 7) -> dict[str, list[float | None]]:
    rng = random.Random(seed)
    tub = _steps(rng, n, [0.0, 0.0, 0.0, 0.0, 0.0, 1500.0], 0.04)
    base1 = _steps(rng, n, [150.0, 300.0, 500.0, 900.0], 0.05)
    base2 = _steps(rng, n, [700.0, 1100.0, 1600.0, 2400.0], 0.05)
    return {
        "leg_a": _noise(rng, tub, 3.0),
        "leg_b": _noise(rng, tub, 3.0),
        "main_1": _noise(rng, [b + t for b, t in zip(base1, tub, strict=True)]),
        "main_2": _noise(rng, [b + t for b, t in zip(base2, tub, strict=True)]),
    }


def test_hot_tub_legs_are_split_across_both_mains_never_both_on_one() -> None:
    series = _hot_tub()
    analysis = tr.analyse(series)
    assert [(a, b) for a, b, _r in analysis.pairs] == [("leg_a", "leg_b")]
    # The two mains are not one wire (the legs dominate them only while on).
    assert {frozenset((a, b)) for a, b, _r, _o in analysis.duplicates} <= {
        frozenset(("leg_a", "leg_b"))
    }
    edges = [
        _edge("main_1", "leg_a", CONFIRMED),
        _edge("main_1", "leg_b", CONFIRMED),
        _edge("main_2", "leg_a", PROVISIONAL, support=6),
        _edge("main_2", "leg_b", PROVISIONAL, support=6),
    ]
    view = _view(edges, aligned=series)
    # A tub step matches both mains on both legs; a pair is ONE candidate.
    view = view.observe_group(
        DeltaObservation(
            group_id="tub-on",
            deltas={"main_1": (1500.0, 10.0), "main_2": (1500.0, 10.0),
                    "leg_a": (1500.0, 3.0), "leg_b": (1500.0, 3.0)},
            at=T0, cause_id="tub",
        ),
        levels={"main_1": 600.0, "main_2": 1400.0, "leg_a": 200.0, "leg_b": 200.0},
    )
    for main in ("main_1", "main_2"):
        for leg in ("leg_a", "leg_b"):
            edge = view.edge(main, leg)
            assert edge is not None and edge.evidence.support >= 7, (main, leg)
    # Never both legs CONFIRMED under one main.
    for main in ("main_1", "main_2"):
        confirmed = [leg for leg in ("leg_a", "leg_b") if _life(view, main, leg) == "confirmed"]
        assert len(confirmed) <= 1, main
    # main_1 is too small to carry both legs: one leg stays there, the other is
    # resolved to main_2 (soft: which leg is which is not identifiable).
    assert sorted(
        _life(view, "main_1", leg) for leg in ("leg_a", "leg_b")
    ) == ["confirmed", "provisional"]
    assert view.edge("leg_a", "leg_b") is None or _life(view, "leg_a", "leg_b") != "confirmed"


def test_a_stepping_leg_pair_is_never_summed_into_one_parent() -> None:
    """-402 = leg -209 + leg -211 on one main was spurious live."""
    series = _hot_tub()
    view = _view([], aligned=series)
    view = view.observe_group(
        DeltaObservation(
            group_id="g",
            deltas={"main_2": (-420.0, 10.0), "leg_a": (-209.0, 3.0), "leg_b": (-211.0, 3.0)},
            at=T0, cause_id="c",
        )
    )
    for leg in ("leg_a", "leg_b"):
        edge = view.edge("main_2", leg)
        assert edge is None or edge.evidence.support == 0


# --------------------------------------------------------------------------- #
# (b) and (c): multi-child matching and explain-away
# --------------------------------------------------------------------------- #


def test_parent_step_explained_by_two_children_supports_both() -> None:
    view = TopologyInferenceView(at=T0)
    view = view.observe_group(
        DeltaObservation(
            group_id="g1",
            deltas={"p": (100.0, 2.0), "y": (30.0, 2.0), "z": (70.0, 2.0)},
            at=T0, cause_id="c1",
        )
    )
    assert view.edge("p", "y").evidence.support == 1
    assert view.edge("p", "z").evidence.support == 1
    # The children never explain each other or the parent.
    assert view.edge("y", "z").evidence.support == 0
    assert view.edge("y", "p").evidence.support == 0


def test_a_confirmed_child_is_explained_away_and_its_health_recorded() -> None:
    edges = [_edge("p", "a", CONFIRMED)]
    view = _view(edges)
    view = view.observe_group(
        DeltaObservation(
            group_id="g1",
            deltas={"p": (150.0, 2.0), "a": (100.0, 2.0), "b": (50.0, 2.0)},
            at=T0, cause_id="c1",
        )
    )
    assert view.edge("p", "b").evidence.support == 1
    assert view.edge("p", "a").evidence.support == 21
    assert view.health.get("p", "a").recent == ("s",)
    assert _life(view, "p", "a") == "confirmed"


def test_single_child_groups_keep_the_one_to_one_rule() -> None:
    view = TopologyInferenceView(at=T0)
    view = view.observe_group(
        DeltaObservation(group_id="g", deltas={"p": (100.0, 2.0), "c": (100.0, 2.0)},
                         at=T0, cause_id="x")
    )
    assert view.edge("p", "c").evidence.support == 1
    assert view.edge("c", "p").evidence.support == 1


# --------------------------------------------------------------------------- #
# (d) crossed sub-panel feeds
# --------------------------------------------------------------------------- #


def _crossed(n: int = 1500, seed: int = 3) -> dict[str, list[float | None]]:
    rng = random.Random(seed)
    sub_1 = _steps(rng, n, [100.0, 600.0, 1200.0, 2000.0], 0.06)
    sub_2 = _steps(rng, n, [80.0, 400.0, 900.0, 1500.0], 0.06)
    base_1 = _steps(rng, n, [400.0, 700.0, 1100.0, 1700.0], 0.08)
    base_2 = _steps(rng, n, [500.0, 900.0, 1300.0, 2000.0], 0.08)
    return {
        # sub_1 is fed from main_2 and sub_2 from main_1 (crossed).
        "main_1": _noise(rng, [b + s for b, s in zip(base_1, sub_2, strict=True)], 20.0),
        "main_2": _noise(rng, [b + s for b, s in zip(base_2, sub_1, strict=True)], 20.0),
        "sub_1": _noise(rng, sub_1),
        "sub_2": _noise(rng, sub_2),
    }


def test_crossed_sub_panels_are_confirmed_structurally() -> None:
    series = _crossed()
    edges = [
        _edge("main_1", "sub_1", PROVISIONAL, support=5),
        _edge("main_2", "sub_1", PROVISIONAL, support=5),
        _edge("main_1", "sub_2", PROVISIONAL, support=5),
        _edge("main_2", "sub_2", PROVISIONAL, support=5),
    ]
    resolved, transitions = tr.resolve_structure(edges, series, levels={})
    assert _life(resolved, "main_2", "sub_1") == "confirmed"
    assert _life(resolved, "main_1", "sub_2") == "confirmed"
    assert _life(resolved, "main_1", "sub_1") == "provisional"
    assert _life(resolved, "main_2", "sub_2") == "provisional"
    assert all("structural" in text for text in transitions)
    assert len(transitions) == 2


def test_structural_confirmation_proposes_a_version_through_the_view() -> None:
    proposals: list[tuple[list[str], str]] = []

    class Versions:
        def current(self):
            raise RuntimeError("unknown")

        def propose_topology(self, edges, change_event_id):
            proposals.append(([e.edge_id for e in edges], ""))
            return f"topo-{len(proposals)}"

        def promote_topology(self, version_id, reason):
            proposals[-1] = (proposals[-1][0], reason)

    series = _crossed()
    edges = [
        _edge("main_1", "sub_1", PROVISIONAL, support=5),
        _edge("main_2", "sub_1", PROVISIONAL, support=5),
    ]
    view = _view(edges, versions=Versions())
    view = view.observe_group(
        DeltaObservation(group_id="g", deltas={"other": (50.0, 2.0)}, at=T0, cause_id="x"),
        aligned=series,
    )
    assert _life(view, "main_2", "sub_1") == "confirmed"
    assert proposals and "te-inf::main_2::sub_1" in proposals[-1][0]
    assert "structural" in proposals[-1][1]


# --------------------------------------------------------------------------- #
# (e) edge health
# --------------------------------------------------------------------------- #


def test_a_confirmed_edge_whose_parent_stops_following_is_demoted_via_health() -> None:
    edges = [_edge("p", "c", CONFIRMED, support=300)]
    view = _view(edges)
    for i in range(6):
        view = view.observe_group(
            DeltaObservation(group_id=f"g{i}", deltas={"c": (100.0, 2.0)}, at=T0,
                             cause_id=f"c{i}"),
            observed={"p": (2.0, 0.0)},
        )
    edge = view.edge("p", "c")
    # The cumulative ledger alone would keep it (6 / 306 conflicts < 10 %).
    assert edge.evidence.conflict == 6
    assert view.health.status("p", "c") is HealthStatus.BROKEN
    assert _life(view, "p", "c") == "provisional"


# --------------------------------------------------------------------------- #
# (f) duplicate meters
# --------------------------------------------------------------------------- #


def test_a_duplicate_meter_is_never_made_a_child() -> None:
    rng = random.Random(11)
    main = _steps(rng, 800, [600.0, 1200.0, 2500.0, 3200.0], 0.08)
    load = _steps(rng, 800, [0.0, 300.0, 500.0], 0.05)
    series = {
        "meter_a": _noise(rng, main),
        "meter_b": _noise(rng, [v + 60.0 for v in main]),
        "load": _noise(rng, load),
    }
    edges = [
        _edge("meter_a", "meter_b", CONFIRMED),
        _edge("meter_b", "meter_a", PROVISIONAL),
        _edge("meter_a", "load", CONFIRMED),
        _edge("meter_b", "load", CONFIRMED),
    ]
    resolved, _transitions = tr.resolve_structure(edges, series, levels={})
    assert _life(resolved, "meter_a", "meter_b") == "provisional"
    assert _life(resolved, "meter_b", "meter_a") == "provisional"
    # One meter suffices: the larger id is never a parent.
    assert _life(resolved, "meter_a", "load") == "confirmed"
    assert _life(resolved, "meter_b", "load") == "provisional"


# --------------------------------------------------------------------------- #
# Kernels agree with the pure wave 1 modules; capacity while children draw.
# --------------------------------------------------------------------------- #


def test_vectorised_detectors_match_the_pure_modules() -> None:
    series = _hot_tub(n=600)
    series["dup"] = [None if i % 17 == 0 else v + 40.0 for i, v in enumerate(series["main_1"])]
    analysis = tr.analyse(series, decimate=1)
    legs = paired_legs(series)
    assert [(a, b) for a, b, _r in analysis.pairs] == [(a, b) for a, b, _r in legs]
    for (_a, _b, r), (_pa, _pb, pr) in zip(analysis.pairs, legs, strict=True):
        assert abs(r - pr) < 1e-6
    pure = duplicate_meters(series)
    assert [(a, b) for a, b, _r, _o in analysis.duplicates] == [(a, b) for a, b, _r, _o in pure]
    for (_a, _b, r, o), (_pa, _pb, pr, po) in zip(analysis.duplicates, pure, strict=True):
        assert abs(r - pr) < 1e-6 and abs(o - po) < 1e-6
    for parent, child in (("main_1", "leg_a"), ("main_2", "leg_b"), ("leg_a", "main_1")):
        fast = analysis.fit(parent, child)
        slow = diff_regression(series[parent], series[child])
        assert (fast is None) == (slow is None)
        if fast is not None:
            assert abs(fast.r - slow.r) < 1e-9 and abs(fast.slope - slow.slope) < 1e-9


def test_capacity_counts_only_samples_where_children_draw() -> None:
    parent = [100.0] * 97 + [1000.0, 1000.0, 1000.0]
    child = [150.0] * 97 + [1500.0, 900.0, 900.0]
    assert capacity_violation(parent, [child]) == (0.98, 100)
    violation, comparable = capacity_violation(parent, [child], min_children_w=300.0)
    assert comparable == 3 and abs(violation - 1 / 3) < 1e-9
    assert not fits(parent, [child], min_children_w=300.0)


def test_short_or_unaligned_series_keep_the_lowest_level_rule() -> None:
    assert tr.structure_series({"a": [1.0] * 30, "b": [2.0] * 30}) is None
    assert tr.structure_series({"a": [1.0] * 60, "b": [2.0] * 59}) is None
    assert tr.structure_series({"a": [1.0] * 60, "b": [2.0] * 60}) is not None


# --------------------------------------------------------------------------- #
# Performance (ADR-0033): 46 readers x 6 h at 10 s.
# --------------------------------------------------------------------------- #


def test_settle_with_46_readers_and_six_hours_is_fast() -> None:
    rng = random.Random(5)
    n = 6 * 360
    readers = [f"r{i:02d}" for i in range(46)]
    series = {r: _noise(rng, _steps(rng, n, [0.0, 200.0, 800.0, 1500.0], 0.03)) for r in readers}
    edges = []
    for child in readers[4:]:
        for parent in readers[:4]:
            lifecycle = CONFIRMED if rng.random() < 0.3 else PROVISIONAL
            edges.append(_edge(parent, child, lifecycle, support=rng.randint(3, 40)))
    view = _view(edges)
    levels = {r: 500.0 for r in readers}

    started = time.perf_counter()
    view = view.observe_group(
        DeltaObservation(group_id="g0", deltas={"r10": (50.0, 2.0)}, at=T0, cause_id="a"),
        aligned=series, levels=levels,
    )
    cold = time.perf_counter() - started
    started = time.perf_counter()
    view = view.observe_group(
        DeltaObservation(group_id="g1", deltas={"r11": (50.0, 2.0)}, at=T0, cause_id="b"),
        levels=levels,
    )
    warm = time.perf_counter() - started
    print(f"settle 46x{n}: cold {cold * 1000:.0f} ms, warm {warm * 1000:.0f} ms")
    # Spec target: < 200 ms per settle; generous bounds for slow CI runners.
    assert warm < 1.0
    assert cold < 3.0
    # Spec target (ADR-0033): a warm settle reuses the cached analysis,
    # fits and capacities.
    assert warm < 0.2
