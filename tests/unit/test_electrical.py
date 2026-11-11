"""Unit tests for the electrical topology, measurement matrix and solver.

All expectations are derived from synthetic ground truth constructed in this
file.  No test relies on a stub implementation.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from wattmosaic_engine.contracts import (
    CTChannel,
    CTType,
    DeviceTopology,
    Phase,
    Polarity,
    QualityStatus,
    Topology,
    TopologyStatus,
)
from wattmosaic_engine.disaggregation.confidence import (
    ConfidenceInputs,
    compute_confidence,
)
from wattmosaic_engine.disaggregation.events import detect_changes, detect_step
from wattmosaic_engine.electrical import (
    DEFAULT_CADENCE_S,
    ObservationWindow,
    analyse_identifiability,
    build_measurement_matrix,
    coerce_window,
    direct_power_w,
    main_power_w,
    resolve_direct,
    solve_constrained_nnls,
)
from wattmosaic_engine.electrical.topology import build_graph
from wattmosaic_engine.electrical.window import ChannelReading

T0 = datetime(2026, 11, 5, 12, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #
def _ch(
    cid: str,
    ct_type: CTType = CTType.BRANCH,
    *,
    phase: Phase = Phase.L1,
    parent: str | None = None,
    device: str | None = None,
    pair: str | None = None,
    enabled: bool = True,
    resolution: float | None = None,
    reliability: float = 1.0,
    polarity: Polarity = Polarity.NORMAL,
    alias: str | None = None,
    multiplier: float = 1.0,
) -> CTChannel:
    return CTChannel(
        ct_id=cid,
        entity_id=f"sensor.{cid}",
        ct_type=ct_type,
        phase=phase,
        parent_ct_id=parent,
        device_id=device,
        paired_ct_id=pair,
        enabled=enabled,
        resolution_w=resolution,
        reliability=reliability,
        polarity=polarity,
        alias_of_ct_id=alias,
        measurement_multiplier=multiplier,
        topology_status=TopologyStatus.CONFIRMED,
    )


def two_mains(**kwargs) -> list[CTChannel]:
    return [
        _ch("main_l1", CTType.MAIN, phase=Phase.L1, **kwargs),
        _ch("main_l2", CTType.MAIN, phase=Phase.L2, **kwargs),
    ]


def house_topology() -> Topology:
    """Two mains, one branch (`branch_kitchen`) with a nested CT (`branch_fridge`)."""
    return Topology(
        version=3,
        status=TopologyStatus.CONFIRMED,
        channels=[
            *two_mains(),
            _ch("branch_kitchen", parent="main_l1"),
            _ch("branch_fridge", parent="branch_kitchen"),
            _ch("ct_tv", CTType.DEDICATED_DEVICE, device="tv"),
        ],
        devices=[
            DeviceTopology(
                device_id="lights",
                branch_ct_ids=["branch_kitchen"],
                phase=Phase.L1,
                topology_status=TopologyStatus.CONFIRMED,
            ),
            DeviceTopology(
                device_id="fridge",
                branch_ct_ids=["branch_fridge"],
                phase=Phase.L1,
                topology_status=TopologyStatus.CONFIRMED,
            ),
            DeviceTopology(
                device_id="tv",
                dedicated_ct_ids=["ct_tv"],
                phase=Phase.L1,
                topology_status=TopologyStatus.CONFIRMED,
            ),
        ],
    )


# --------------------------------------------------------------------------- #
# Topology validation
# --------------------------------------------------------------------------- #
def test_empty_topology_is_valid():
    assert build_graph(Topology()).validate() == []


def test_cycle_is_detected_and_reported_not_crashing():
    topo = Topology(
        channels=[
            _ch("a", parent="c"),
            _ch("b", parent="a"),
            _ch("c", parent="b"),
        ]
    )
    graph = build_graph(topo)
    problems = graph.validate()
    assert any("cycle" in p for p in problems)
    assert graph.is_consistent() is False


def test_self_parent_is_rejected():
    problems = build_graph(Topology(channels=[_ch("a", parent="a")])).validate()
    assert any("its own parent" in p for p in problems)


def test_unknown_parent_and_duplicate_ids_are_reported():
    topo = Topology(
        channels=[_ch("a", parent="nope"), _ch("a")],
        devices=[
            DeviceTopology(device_id="d"),
            DeviceTopology(device_id="d"),
        ],
    )
    problems = build_graph(topo).validate()
    assert any("duplicate CT id" in p for p in problems)
    assert any("unknown parent_ct_id" in p for p in problems)
    assert any("duplicate device id" in p for p in problems)


def test_nested_main_sums_only_the_outermost_feed():
    """A panel feed behind a service feed is counted once, not twice.

    Nesting is a supported wiring shape, so this is no longer reported as a
    validation problem.  The nested feed must simply not be added again: the
    answer is the outer feed (5000 W), never the double-counted 7000 W.
    """
    topo = Topology(
        channels=[
            _ch("utility", CTType.MAIN, phase=Phase.BOTH),
            _ch("panel", CTType.MAIN, phase=Phase.BOTH, parent="utility"),
        ]
    )
    graph = build_graph(topo)
    assert not any("nested mains" in p for p in graph.validate()), (
        "nesting is a supported shape and must not be a validation problem"
    )
    readings = {"utility": 5000.0, "panel": 2000.0}
    result = main_power_w(graph, readings)
    assert result.errors == ()
    assert result.total_w == 5000.0
    assert result.total_w != 7000.0, "the nested feed must not be double counted"
    assert result.complete is True
    assert result.per_channel_w == {"utility": 5000.0}
    assert "panel" in result.excluded_channels
    assert any(n.startswith("nested_feeds_not_summed") for n in result.notes)


def test_three_level_nesting_sums_only_the_root():
    """Arbitrary depth: service -> panel -> sub-panel contributes once."""
    topo = Topology(
        channels=[
            _ch("service", CTType.MAIN, phase=Phase.BOTH),
            _ch("panel", CTType.MAIN, phase=Phase.BOTH, parent="service"),
            _ch("sub", CTType.MAIN, phase=Phase.BOTH, parent="panel"),
        ]
    )
    graph = build_graph(topo)
    assert graph.effective_feeds() == ["service"]
    result = main_power_w(graph, {"service": 4000.0, "panel": 1500.0, "sub": 800.0})
    assert result.errors == ()
    assert result.total_w == 4000.0


def test_parallel_feeds_under_the_same_parent_are_both_summed():
    """Two panels fed from one service are parallel, not nested."""
    topo = Topology(
        channels=[
            _ch("service", CTType.MAIN, phase=Phase.BOTH),
            _ch("panel_a", CTType.MAIN, phase=Phase.BOTH, parent="service"),
            _ch("panel_b", CTType.MAIN, phase=Phase.BOTH, parent="service"),
        ]
    )
    graph = build_graph(topo)
    assert graph.effective_feeds() == ["service"]
    assert main_power_w(graph, {"service": 4000.0}).total_w == 4000.0


def test_root_level_parallel_feeds_are_summed():
    """Independent services (or L1/L2 roots) are genuinely additive."""
    topo = Topology(
        channels=[
            _ch("l1", CTType.MAIN, phase=Phase.L1),
            _ch("l2", CTType.MAIN, phase=Phase.L2),
        ]
    )
    graph = build_graph(topo)
    assert graph.effective_feeds() == ["l1", "l2"]
    result = main_power_w(graph, {"l1": 1200.0, "l2": 800.0})
    assert result.total_w == 2000.0
    assert result.l1_w == 1200.0
    assert result.l2_w == 800.0


def test_a_nested_feed_that_is_disabled_is_ignored_when_resolving_feeds():
    topo = Topology(
        channels=[
            _ch("service", CTType.MAIN, phase=Phase.BOTH),
            _ch("panel", CTType.MAIN, phase=Phase.BOTH, parent="service", enabled=False),
            _ch("orphan", CTType.MAIN, phase=Phase.BOTH),
        ]
    )
    graph = build_graph(topo)
    assert graph.effective_feeds() == ["orphan", "service"]


def test_an_aliased_main_contributes_once():
    """Two monitors on the same service conductors are one feed, not two."""
    topo = Topology(
        channels=[
            _ch("monitor_a", CTType.MAIN, phase=Phase.BOTH),
            _ch("monitor_b", CTType.MAIN, phase=Phase.BOTH, alias="monitor_a"),
        ]
    )
    graph = build_graph(topo)
    assert graph.effective_feeds() == ["monitor_a"]
    result = main_power_w(graph, {"monitor_a": 2400.0, "monitor_b": 2412.0})
    assert result.total_w == 2400.0, "the duplicate measurement must not be added"
    assert "monitor_b" in result.excluded_channels
    assert any(n.startswith("duplicate_feeds_counted_once") for n in result.notes)


def test_an_alias_chain_contributes_once_through_the_whole_chain():
    """``a -> b -> c`` is one wire; only ``c`` may define the total."""
    topo = Topology(
        channels=[
            _ch("monitor_a", CTType.MAIN, phase=Phase.BOTH, alias="monitor_b"),
            _ch("monitor_b", CTType.MAIN, phase=Phase.BOTH, alias="monitor_c"),
            _ch("monitor_c", CTType.MAIN, phase=Phase.BOTH),
        ]
    )
    graph = build_graph(topo)
    assert graph.effective_feeds() == ["monitor_c"]
    result = main_power_w(
        graph, {"monitor_a": 2400.0, "monitor_b": 2410.0, "monitor_c": 2405.0}
    )
    assert result.total_w == 2405.0
    assert set(result.excluded_channels) == {"monitor_a", "monitor_b"}


def test_double_pole_pair_detection():
    topo = Topology(
        channels=[
            _ch("hot_tub_l1", phase=Phase.L1, pair="hot_tub_l2"),
            _ch("hot_tub_l2", phase=Phase.L2, pair="hot_tub_l1"),
        ],
        devices=[
            DeviceTopology(
                device_id="hot_tub",
                branch_ct_ids=["hot_tub_l1", "hot_tub_l2"],
                phase=Phase.BOTH,
                double_pole=True,
                topology_status=TopologyStatus.CONFIRMED,
            )
        ],
    )
    graph = build_graph(topo)
    assert ("hot_tub_l1", "hot_tub_l2") in graph.double_pole_pairs
    discipline = graph.resolve("hot_tub")
    assert discipline.double_pole
    assert set(graph.attribution_ct_ids("hot_tub")) == {"hot_tub_l1", "hot_tub_l2"}


def test_asymmetric_pairing_is_reported():
    topo = Topology(
        channels=[
            _ch("a", phase=Phase.L1, pair="b"),
            _ch("b", phase=Phase.L2),  # does not point back
        ]
    )
    assert any("not symmetric" in p for p in build_graph(topo).validate())


def test_double_pole_single_phase_declaration_is_reported():
    topo = Topology(
        devices=[
            DeviceTopology(device_id="x", phase=Phase.BOTH, double_pole=False)
        ]
    )
    assert any("phase BOTH but double_pole=False" in p for p in build_graph(topo).validate())


def test_ancestors_and_descendants_order():
    graph = build_graph(house_topology())
    assert graph.ancestors("branch_fridge") == ("branch_kitchen", "main_l1")
    assert graph.descendants("main_l1") == ("branch_kitchen", "branch_fridge")
    assert graph.is_ancestor("main_l1", "branch_fridge")


def test_most_specific_ct_is_the_deepest_branch():
    graph = build_graph(house_topology())
    assert graph.attribution_ct_ids("lights") == ("branch_kitchen",)
    assert graph.attribution_ct_ids("fridge") == ("branch_fridge",)
    assert graph.attribution_ct_ids("tv") == ("ct_tv",)


# --------------------------------------------------------------------------- #
# Measurement matrix
# --------------------------------------------------------------------------- #
def test_matrix_shape_and_metadata():
    graph = build_graph(house_topology())
    matrix = build_measurement_matrix(graph, ["lights", "fridge", "tv"])
    assert matrix.A.shape == (len(matrix.row_channel_ids), 3)
    assert matrix.col_device_ids == ["lights", "fridge", "tv"]
    assert matrix.unobserved_devices == []
    assert set(matrix.row_channel_ids) >= {"main_l1", "branch_kitchen", "branch_fridge", "ct_tv"}
    assert matrix.topology_version == 3


def test_dedicated_device_has_a_one_in_its_own_row():
    graph = build_graph(house_topology())
    matrix = build_measurement_matrix(graph, ["lights", "fridge", "tv"])
    assert matrix.coefficient("ct_tv", "tv") == pytest.approx(1.0)
    # Nothing else is in the dedicated row.
    assert matrix.coefficient("ct_tv", "lights") == 0.0
    assert matrix.coefficient("ct_tv", "fridge") == 0.0


def test_shared_branch_row_contains_both_devices():
    topo = Topology(
        channels=[*two_mains(), _ch("branch_shared", parent="main_l1")],
        devices=[
            DeviceTopology(device_id="a", branch_ct_ids=["branch_shared"], phase=Phase.L1),
            DeviceTopology(device_id="b", branch_ct_ids=["branch_shared"], phase=Phase.L1),
        ],
        status=TopologyStatus.CONFIRMED,
    )
    matrix = build_measurement_matrix(build_graph(topo), ["a", "b"])
    assert matrix.coefficient("branch_shared", "a") == pytest.approx(1.0)
    assert matrix.coefficient("branch_shared", "b") == pytest.approx(1.0)


def test_nested_branch_cts_do_not_double_count():
    graph = build_graph(house_topology())
    matrix = build_measurement_matrix(graph, ["lights", "fridge", "tv"])

    # The parent row does NOT repeat the child device ...
    assert matrix.coefficient("branch_kitchen", "fridge") == 0.0
    assert matrix.coefficient("branch_kitchen", "lights") == pytest.approx(1.0)
    # ... instead the child CT is explicitly subtracted.
    assert matrix.nested_children["branch_kitchen"] == ("branch_fridge",)

    prepared = matrix.prepare(
        {
            "main_l1": 1500.0,
            "branch_kitchen": 900.0,
            "branch_fridge": 200.0,
            "ct_tv": 100.0,
        }
    )
    row = matrix.row_index("branch_kitchen")
    # 900 W total measured on the kitchen branch, minus the 200 W child CT.
    assert prepared.y[row] == pytest.approx(700.0)
    assert any("subtracts_nested_cts" in n for n in matrix.row_notes["branch_kitchen"])


def test_nested_parent_row_is_excluded_when_child_is_missing():
    graph = build_graph(house_topology())
    matrix = build_measurement_matrix(graph, ["lights", "fridge", "tv"])
    prepared = matrix.prepare(
        {"main_l1": 1500.0, "branch_kitchen": 900.0, "ct_tv": 100.0}
    )
    row = matrix.row_index("branch_kitchen")
    assert not bool(prepared.mask[row])
    assert "nested_child_missing" in prepared.excluded_rows["branch_kitchen"]


def test_main_row_contains_everything_below_it():
    graph = build_graph(house_topology())
    matrix = build_measurement_matrix(graph, ["lights", "fridge", "tv"])
    assert matrix.coefficient("main_l1", "lights") == pytest.approx(1.0)
    assert matrix.coefficient("main_l1", "fridge") == pytest.approx(1.0)
    # tv is on a dedicated CT with no declared parent, so it is not under main_l1.
    assert matrix.coefficient("main_l1", "tv") == 0.0


def test_unobserved_device_is_listed_not_silently_zero():
    topo = Topology(
        channels=[*two_mains()],
        devices=[
            DeviceTopology(device_id="orphan", phase=Phase.L1),
            DeviceTopology(device_id="on_main", branch_ct_ids=[], phase=Phase.L1),
        ],
        status=TopologyStatus.CONFIRMED,
    )
    # on_main is placed under main_l1 explicitly via a child CT path.
    graph = build_graph(topo)
    matrix = build_measurement_matrix(graph, ["orphan", "on_main"])
    assert "orphan" in matrix.unobserved_devices
    assert "on_main" in matrix.unobserved_devices
    assert np.allclose(matrix.column("orphan"), 0.0)


def test_phase_coefficients_l1_l2_both():
    topo = Topology(
        channels=[
            _ch("m_l1", CTType.MAIN, phase=Phase.L1),
            _ch("m_l2", CTType.MAIN, phase=Phase.L2),
            _ch("m_both", CTType.MAIN, phase=Phase.BOTH),
            _ch("b1", phase=Phase.L1, parent="m_l1"),
            _ch("b2", phase=Phase.L2, parent="m_l2"),
            _ch("b240", phase=Phase.BOTH, parent="m_both"),
        ],
        devices=[
            DeviceTopology(device_id="l1_load", branch_ct_ids=["b1"], phase=Phase.L1),
            DeviceTopology(device_id="l2_load", branch_ct_ids=["b2"], phase=Phase.L2),
            DeviceTopology(
                device_id="dryer",
                branch_ct_ids=["b240"],
                phase=Phase.BOTH,
                double_pole=True,
            ),
        ],
        status=TopologyStatus.CONFIRMED,
    )
    graph = build_graph(topo)
    assert graph.phase_coefficient("l1_load", Phase.L1) == 1.0
    assert graph.phase_coefficient("l1_load", Phase.L2) == 0.0
    assert graph.phase_coefficient("l1_load", Phase.BOTH) == 1.0
    assert graph.phase_coefficient("dryer", Phase.BOTH) == 1.0
    assert graph.phase_coefficient("dryer", Phase.L1) == pytest.approx(0.5)
    assert graph.phase_coefficient("dryer", Phase.L2) == pytest.approx(0.5)

    # Configurable split is honoured ...
    graph_asym = build_graph(topo, phase_split_l1=0.7)
    assert graph_asym.phase_coefficient("dryer", Phase.L1) == pytest.approx(0.7)
    assert graph_asym.phase_coefficient("dryer", Phase.L2) == pytest.approx(0.3)
    # ... and never applied to single-phase loads.
    assert graph_asym.phase_coefficient("l1_load", Phase.L2) == 0.0


def test_double_pole_split_across_separate_l1_l2_rows():
    topo = Topology(
        channels=[
            _ch("m_l1", CTType.MAIN, phase=Phase.L1),
            _ch("m_l2", CTType.MAIN, phase=Phase.L2),
            _ch("tub_l1", phase=Phase.L1, parent="m_l1", pair="tub_l2"),
            _ch("tub_l2", phase=Phase.L2, parent="m_l2", pair="tub_l1"),
        ],
        devices=[
            DeviceTopology(
                device_id="tub",
                branch_ct_ids=["tub_l1", "tub_l2"],
                phase=Phase.BOTH,
                double_pole=True,
            )
        ],
        status=TopologyStatus.CONFIRMED,
    )
    matrix = build_measurement_matrix(build_graph(topo), ["tub"])
    # Total power is one logical load; each leg receives half by default.
    assert matrix.coefficient("tub_l1", "tub") == pytest.approx(0.5)
    assert matrix.coefficient("tub_l2", "tub") == pytest.approx(0.5)
    assert matrix.coefficient("m_l1", "tub") == pytest.approx(0.5)
    assert matrix.coefficient("m_l2", "tub") == pytest.approx(0.5)


def test_weights_and_sigma_reflect_resolution_and_reliability():
    topo = Topology(
        channels=[
            *two_mains(),
            _ch("b_precise", parent="main_l1", resolution=1.0, reliability=1.0),
            _ch("b_noisy", parent="main_l1", resolution=100.0, reliability=0.5),
        ],
        devices=[
            DeviceTopology(device_id="a", branch_ct_ids=["b_precise"], phase=Phase.L1),
            DeviceTopology(device_id="b", branch_ct_ids=["b_noisy"], phase=Phase.L1),
        ],
    )
    matrix = build_measurement_matrix(build_graph(topo), ["a", "b"])
    assert matrix.row_sigma[matrix.row_index("b_noisy")] > matrix.row_sigma[
        matrix.row_index("b_precise")
    ]
    assert matrix.row_weights[matrix.row_index("b_noisy")] < matrix.row_weights[
        matrix.row_index("b_precise")
    ]


def test_reversed_polarity_is_applied():
    graph = build_graph(
        Topology(
            channels=[
                _ch("m_l1", CTType.MAIN, phase=Phase.L1, polarity=Polarity.INVERTED),
                _ch("m_l2", CTType.MAIN, phase=Phase.L2),
            ]
        )
    )
    result = main_power_w(graph, {"m_l1": 500.0, "m_l2": 100.0})
    assert result.per_channel_w["m_l1"] == pytest.approx(-500.0)
    assert result.total_w == pytest.approx(-400.0)


# --------------------------------------------------------------------------- #
# Main power
# --------------------------------------------------------------------------- #
def test_main_power_sums_l1_and_l2():
    result = main_power_w(build_graph(house_topology()), {"main_l1": 1200.0, "main_l2": 800.0})
    assert result.total_w == pytest.approx(2000.0)
    assert result.l1_w == pytest.approx(1200.0)
    assert result.l2_w == pytest.approx(800.0)
    assert result.complete


def test_main_power_preserves_negative_export():
    result = main_power_w(
        build_graph(house_topology()), {"main_l1": -1500.0, "main_l2": 400.0}
    )
    assert result.total_w == pytest.approx(-1100.0)
    assert result.has_export is True
    assert result.l1_w == pytest.approx(-1500.0)


def test_main_power_missing_channel_is_incomplete_not_zero_filled():
    result = main_power_w(build_graph(house_topology()), {"main_l1": 900.0})
    assert result.missing_channels == ("main_l2",)
    assert result.complete is False
    assert result.total_w == pytest.approx(900.0)


def test_main_power_nan_is_treated_as_missing():
    result = main_power_w(
        build_graph(house_topology()), {"main_l1": float("nan"), "main_l2": 100.0}
    )
    assert "main_l1" in result.missing_channels
    assert result.total_w == pytest.approx(100.0)


def test_main_power_requires_a_main_ct():
    result = main_power_w(build_graph(Topology(channels=[_ch("b")])), {"b": 10.0})
    assert result.errors
    assert result.complete is False


# --------------------------------------------------------------------------- #
# Solver
# --------------------------------------------------------------------------- #
def test_solver_recovers_exact_solution():
    A = np.array([[1.0, 1.0], [1.0, 0.0]])
    y = np.array([500.0, 300.0])
    result = solve_constrained_nnls(A, y, device_ids=["a", "b"])
    assert result.x == pytest.approx([300.0, 200.0])
    assert result.residual_norm == pytest.approx(0.0)


def test_solver_is_non_negative():
    A = np.array([[1.0]])
    y = np.array([-250.0])  # export on a device row is impossible
    result = solve_constrained_nnls(A, y, device_ids=["a"])
    assert result.x[0] == 0.0
    assert "a" in result.at_lower_bound


def test_solver_respects_upper_bounds_from_models():
    A = np.array([[1.0]])
    y = np.array([5000.0])
    result = solve_constrained_nnls(
        A, y, device_ids=["washer"], upper=[2200.0]
    )
    assert result.x[0] == pytest.approx(2200.0)
    assert "washer" in result.at_upper_bound
    assert result.residual_norm == pytest.approx(2800.0)


def test_solver_weights_influence_fit():
    A = np.array([[1.0], [1.0]])
    y = np.array([100.0, 300.0])
    result = solve_constrained_nnls(
        A, y, device_ids=["a"], weights=[10.0, 1.0]
    )
    # Heavily weighted row wins: closer to 100 than the unweighted mean 200.
    assert result.x[0] < 150.0


def test_solver_is_deterministic():
    rng = np.random.default_rng(7)
    A = rng.random((8, 4))
    y = rng.random(8) * 500
    first = solve_constrained_nnls(A, y, device_ids=list("abcd"))
    second = solve_constrained_nnls(A, y, device_ids=list("abcd"))
    assert first.x.tobytes() == second.x.tobytes()


def test_identifiability_rank_and_ambiguous_group():
    # Two devices share one row: only their sum is observable.
    A = np.array([[1.0, 1.0]])
    ident = analyse_identifiability(A, ["a", "b"])
    assert ident.rank == 1
    assert ident.null_space_dimension == 1
    assert ident.unidentifiable == ["a", "b"]
    assert ident.ambiguous_groups == [["a", "b"]]
    assert np.all(ident.null_share > 0)


def test_identifiability_independent_devices_are_unique():
    A = np.eye(3)
    ident = analyse_identifiability(A, ["a", "b", "c"])
    assert ident.rank == 3
    assert ident.null_space_dimension == 0
    assert ident.uniquely_identifiable == ["a", "b", "c"]
    assert ident.unidentifiable == []
    assert ident.condition_number == pytest.approx(1.0)


def test_identifiability_all_zero_matrix():
    ident = analyse_identifiability(np.zeros((2, 3)), ["a", "b", "c"])
    assert ident.rank == 0
    assert ident.unidentifiable == ["a", "b", "c"]


def test_identifiability_handles_empty_device_list():
    ident = analyse_identifiability(np.zeros((2, 0)), [])
    assert ident.n_devices == 0
    assert ident.uniquely_identifiable == []


def test_matrix_prepare_marks_missing_rows():
    matrix = build_measurement_matrix(build_graph(house_topology()), ["lights", "fridge"])
    prepared = matrix.prepare({"main_l1": 1000.0})
    assert prepared.mask.sum() < len(prepared.mask)
    assert "branch_kitchen" in prepared.excluded_rows


def test_prediction_round_trip():
    matrix = build_measurement_matrix(build_graph(house_topology()), ["lights", "fridge", "tv"])
    x = np.array([400.0, 150.0, 60.0])
    y = matrix.predict(x)
    row_main = matrix.row_index("main_l1")
    row_kitchen = matrix.row_index("branch_kitchen")
    assert y[row_main] == pytest.approx(550.0)
    assert y[row_kitchen] == pytest.approx(400.0)  # child excluded from parent row


# --------------------------------------------------------------------------- #
# Direct measurement
# --------------------------------------------------------------------------- #
def _dedicated_topology() -> Topology:
    return Topology(
        channels=[
            _ch("ct_tv", CTType.DEDICATED_DEVICE, device="tv"),
            _ch("ct_tub_l1", CTType.DEDICATED_DEVICE, phase=Phase.L1, device="tub", pair="ct_tub_l2"),
            _ch("ct_tub_l2", CTType.DEDICATED_DEVICE, phase=Phase.L2, device="tub", pair="ct_tub_l1"),
        ],
        devices=[
            DeviceTopology(device_id="tv", dedicated_ct_ids=["ct_tv"], phase=Phase.L1),
            DeviceTopology(
                device_id="tub",
                dedicated_ct_ids=["ct_tub_l1", "ct_tub_l2"],
                phase=Phase.BOTH,
                double_pole=True,
            ),
            DeviceTopology(device_id="unknown_load", phase=Phase.L1),
        ],
        status=TopologyStatus.CONFIRMED,
    )


def test_resolve_direct_uses_dedicated_ct_only():
    graph = build_graph(_dedicated_topology())
    window = ObservationWindow.from_values(T0, {"ct_tv": 75.0, "ct_tub_l1": 0.0, "ct_tub_l2": 0.0})
    readings = resolve_direct(window, graph)
    assert set(readings) == {"tv", "tub"}  # unknown_load has no dedicated CT
    assert readings["tv"].power_w == pytest.approx(75.0)
    assert readings["tv"].usable
    assert readings["tv"].ct_ids == ("ct_tv",)


def _single_leg_multiplier_topology() -> Topology:
    """One CT on one leg of a 240 V load, declared with a x2 circuit multiplier."""
    return Topology(
        channels=[
            _ch("main", CTType.MAIN, phase=Phase.BOTH),
            _ch(
                "ct_tub",
                CTType.DEDICATED_DEVICE,
                phase=Phase.L1,
                device="tub",
                multiplier=2.0,
            ),
        ],
        devices=[
            DeviceTopology(
                device_id="tub",
                dedicated_ct_ids=["ct_tub"],
                phase=Phase.L1,
                double_pole=True,
            )
        ],
    )


def test_default_multiplier_is_one_and_changes_nothing():
    """The common case: the monitor already scales, so the engine must not."""
    graph = build_graph(Topology(channels=[_ch("ct_tv", CTType.DEDICATED_DEVICE, device="tv")]))
    assert graph.multiplier_for("ct_tv") == 1.0
    assert graph.scale_w("ct_tv", 75.0) == 75.0
    assert graph.multiplier_for("not_a_channel") == 1.0
    assert graph.scale_w("not_a_channel", 75.0) == 75.0


def test_scale_w_applies_the_declared_multiplier_once():
    graph = build_graph(_single_leg_multiplier_topology())
    assert graph.multiplier_for("ct_tub") == 2.0
    assert graph.scale_w("ct_tub", 1100.0) == pytest.approx(2200.0)
    # Scaling is a pure function of the input, so calling it again on the raw
    # value cannot accumulate: nothing writes the result back.
    assert graph.scale_w("ct_tub", 1100.0) == pytest.approx(2200.0)
    assert graph.scale_w("ct_tub", None) is None


def test_direct_measurement_applies_the_multiplier_and_reports_the_full_load():
    graph = build_graph(_single_leg_multiplier_topology())
    window = ObservationWindow.from_values(T0, {"main": 3000.0, "ct_tub": 1100.0})
    readings = resolve_direct(window, graph)
    assert readings["tub"].power_w == pytest.approx(2200.0)
    assert readings["tub"].per_leg_w == {"ct_tub": pytest.approx(2200.0)}
    # The raw reading stays unscaled, so a caller cannot apply it a second time.
    assert readings["tub"].raw_power_w is None


def test_main_power_applies_a_multiplier_on_a_nested_feed():
    """A doubled sub-panel feed must not leak into the outer total."""
    topo = Topology(
        channels=[
            _ch("service", CTType.MAIN, phase=Phase.BOTH),
            _ch(
                "panel",
                CTType.MAIN,
                phase=Phase.BOTH,
                parent="service",
                multiplier=2.0,
            ),
        ]
    )
    graph = build_graph(topo)
    result = main_power_w(graph, {"service": 3000.0, "panel": 900.0})
    assert result.total_w == pytest.approx(3000.0)
    assert "panel" in result.excluded_channels


def test_matrix_scales_the_target_but_not_the_raw_reading():
    graph = build_graph(_single_leg_multiplier_topology())
    matrix = build_measurement_matrix(graph, ["tub"])
    assert matrix.channel_multiplier["ct_tub"] == 2.0
    prepared = matrix.prepare({"ct_tub": 1100.0})
    row = matrix.row_channel_ids.index("ct_tub")
    # y is the *load* the row measures, which the device already doubled.
    assert prepared.y[row] == pytest.approx(2200.0)
    assert matrix._raw_reading_w({"ct_tub": 1100.0}, "ct_tub") == pytest.approx(1100.0)
    assert matrix._reading_w({"ct_tub": 1100.0}, "ct_tub") == pytest.approx(2200.0)


def test_step_detection_reports_the_step_in_the_load_not_the_leg():
    """A doubled leg's step must be doubled, and so must its threshold."""
    history = []
    for i in range(20):
        history.append((T0 + timedelta(seconds=15.7 * i), 300.0))
    for i in range(20, 23):
        history.append((T0 + timedelta(seconds=15.7 * i), 800.0))
    at = T0 + timedelta(seconds=15.7 * 22)
    reading = ChannelReading(channel_id="ct_tub", value_w=800.0, history=tuple(history))
    step = detect_step(reading, at, before_s=40.0, after_s=20.0, multiplier=2.0)
    assert step is not None and step.detected
    assert step.delta_w == pytest.approx(1000.0)  # (800-300) * 2
    assert step.after_w == pytest.approx(1600.0)
    assert step.before_w == pytest.approx(600.0)


def test_detect_changes_scales_the_previous_snapshot_fallback():
    readings = {"ct_tub": ChannelReading(channel_id="ct_tub", value_w=400.0)}
    changes = detect_changes(
        readings,
        T0,
        before_s=10.0,
        after_s=5.0,
        previous_values={"ct_tub": 100.0},
        multipliers={"ct_tub": 2.0},
    )
    assert changes["ct_tub"].delta_w == pytest.approx(600.0)  # (400-100) * 2
    assert changes["ct_tub"].after_w == pytest.approx(800.0)


def test_direct_double_pole_sums_both_legs():
    graph = build_graph(_dedicated_topology())
    window = ObservationWindow.from_values(
        T0, {"ct_tv": 0.0, "ct_tub_l1": 1200.0, "ct_tub_l2": 1300.0}
    )
    readings = resolve_direct(window, graph)
    assert readings["tub"].power_w == pytest.approx(2500.0)
    assert readings["tub"].per_leg_w == {"ct_tub_l1": 1200.0, "ct_tub_l2": 1300.0}


def test_direct_measurement_stale_is_not_usable():
    graph = build_graph(_dedicated_topology())
    window = ObservationWindow.from_values(
        T0,
        {"ct_tv": 75.0, "ct_tub_l1": 0.0, "ct_tub_l2": 0.0},
        ages={"ct_tv": 600.0},
    )
    readings = resolve_direct(window, graph, max_age_s=120.0)
    assert readings["tv"].usable is False
    assert any("stale_measurement" in r for r in readings["tv"].reasons)


def test_direct_measurement_missing_channel_is_not_zero():
    graph = build_graph(_dedicated_topology())
    window = ObservationWindow.from_values(T0, {"ct_tub_l1": 10.0, "ct_tub_l2": 10.0})
    readings = resolve_direct(window, graph)
    assert readings["tv"].power_w is None
    assert readings["tv"].usable is False
    assert direct_power_w(window, graph) == {"tub": pytest.approx(20.0)}


def test_direct_measurement_quality_bad_is_unusable():
    graph = build_graph(_dedicated_topology())
    window = ObservationWindow.from_values(
        T0,
        {"ct_tv": 75.0, "ct_tub_l1": 0.0, "ct_tub_l2": 0.0},
        qualities={"ct_tv": QualityStatus.BAD},
    )
    assert resolve_direct(window, graph)["tv"].usable is False


def test_negative_direct_reading_is_clipped_to_zero():
    """A device cannot consume negative power; noise around zero is clipped.

    ``docs/electrical_model.md`` §9.5: a negative device-level reading is
    export (a main property) or unmetered generation, and must not be reported
    as a negatively consuming appliance.  The constrained solver enforces this
    with ``x >= 0``; the Level-1 direct path must match.
    """
    graph = build_graph(_dedicated_topology())
    window = ObservationWindow.from_values(
        T0, {"ct_tv": -0.065, "ct_tub_l1": 0.0, "ct_tub_l2": 0.0}
    )
    reading = resolve_direct(window, graph)["tv"]
    assert reading.power_w == 0.0
    # The raw signed value is preserved for diagnostics, never for reporting.
    assert reading.raw_power_w == pytest.approx(-0.065)
    assert reading.usable is True
    assert "negative_device_power_clipped" in reading.reasons


def test_negative_direct_reading_on_a_paired_ct_keeps_the_signed_sum():
    """Legs are summed with sign, then the *device total* is clamped once."""
    graph = build_graph(_dedicated_topology())
    window = ObservationWindow.from_values(
        T0, {"ct_tv": 0.0, "ct_tub_l1": 50.0, "ct_tub_l2": -120.0}
    )
    reading = resolve_direct(window, graph)["tub"]
    assert reading.power_w == 0.0
    assert reading.raw_power_w == pytest.approx(-70.0)
    assert reading.per_leg_w == {"ct_tub_l1": 50.0, "ct_tub_l2": -120.0}
    assert "negative_device_power_signed_leg_sum" in reading.reasons


def test_positive_direct_reading_is_never_clamped():
    graph = build_graph(_dedicated_topology())
    window = ObservationWindow.from_values(
        T0, {"ct_tv": 0.4, "ct_tub_l1": 0.0, "ct_tub_l2": 0.0}
    )
    reading = resolve_direct(window, graph)["tv"]
    assert reading.power_w == pytest.approx(0.4)
    assert reading.raw_power_w is None
    assert "negative_device_power_clipped" not in reading.reasons


# --------------------------------------------------------------------------- #
# Window / cadence
# --------------------------------------------------------------------------- #
def test_cadence_is_derived_from_observed_history_not_hardcoded():
    history = tuple((T0 - timedelta(seconds=15.7 * (5 - i)), 100.0) for i in range(6))
    window = ObservationWindow.from_values(
        T0, {"c": 100.0}, histories={"c": history}
    )
    assert window.cadence() == pytest.approx(15.7, rel=1e-3)
    before, after = window.before_after_seconds()
    # Derived from 15.7 s, definitely not a 2 s assumption.
    assert before > 2.0
    assert after >= 2.5


def test_default_cadence_matches_measured_environment():
    window = ObservationWindow.from_values(T0, {"c": 1.0})
    assert window.cadence() == pytest.approx(DEFAULT_CADENCE_S)
    assert pytest.approx(15.7) == DEFAULT_CADENCE_S


def test_window_coercion_accepts_raw_mapping_and_measurements():
    from wattmosaic_engine.contracts import Measurement

    m = Measurement(
        entity_id="sensor.x",
        ct_id="x",
        value=1.5,
        unit="kW",
        state_timestamp=T0,
        polarization=Polarity.INVERTED,
    )
    coerced = coerce_window({"x": m})
    assert coerced.readings["x"].value_w == pytest.approx(-1500.0)
    coerced_plain = coerce_window({"x": 42.0}, at=T0)
    assert coerced_plain.readings["x"].value_w == pytest.approx(42.0)


# --------------------------------------------------------------------------- #
# Event detection
# --------------------------------------------------------------------------- #
def test_step_detection_uses_mad_and_detects_activation():
    history = []
    for i in range(20):
        t = T0 + timedelta(seconds=15.7 * i)
        history.append((t, 500.0 + (2.0 if i % 2 else -2.0)))  # noise sigma ~3 W
    for i in range(20, 23):
        history.append((T0 + timedelta(seconds=15.7 * i), 1500.0))
    at = T0 + timedelta(seconds=15.7 * 22)
    reading = ChannelReading(channel_id="b", value_w=1500.0, history=tuple(history))
    step = detect_step(reading, at, before_s=40.0, after_s=20.0)
    assert step is not None
    assert step.detected
    assert step.delta_w > 900.0
    assert step.before_samples >= 2
    assert step.threshold_w >= 5.0


def test_step_detection_does_not_flag_noise_only():
    history = []
    for i in range(40):
        t = T0 + timedelta(seconds=15.7 * i)
        history.append((t, 500.0 + (3.0 if i % 2 else -3.0)))
    at = T0 + timedelta(seconds=15.7 * 40)
    reading = ChannelReading(channel_id="b", value_w=501.0, history=tuple(history))
    step = detect_step(reading, at, before_s=40.0, after_s=20.0, k_mad=4.0)
    assert step is not None
    assert step.detected is False


def test_step_detection_needs_two_samples():
    reading = ChannelReading(channel_id="b", value_w=10.0, history=((T0, 10.0),))
    assert detect_step(reading, T0, before_s=10.0, after_s=5.0) is None


def test_detect_changes_falls_back_to_previous_snapshot():
    readings = {
        "b": ChannelReading(channel_id="b", value_w=400.0),
    }
    changes = detect_changes(
        readings, T0, before_s=10.0, after_s=5.0, previous_values={"b": 100.0}
    )
    assert changes["b"].detected
    assert changes["b"].source == "previous_snapshot"
    assert changes["b"].delta_w == pytest.approx(300.0)


# --------------------------------------------------------------------------- #
# Confidence
# --------------------------------------------------------------------------- #
def test_confidence_bounds_and_direct_dominance():
    from wattmosaic_engine.contracts import Source

    direct = compute_confidence(
        ConfidenceInputs(
            source=Source.DIRECT_MEASURED,
            measurement_qualities=[QualityStatus.GOOD],
            measurement_ages_s=[1.0],
            supporting_ct_count=2,
            model_quality=0.9,
            event_isolation=1.0,
            topology_status="confirmed",
        )
    )
    unknown = compute_confidence(
        ConfidenceInputs(
            source=Source.UNKNOWN,
            measurement_qualities=[QualityStatus.GOOD],
            measurement_ages_s=[0.0],
            supporting_ct_count=2,
            topology_status="confirmed",
        )
    )
    assert 0.0 <= unknown.score < direct.score <= 1.0
    assert direct.as_dict()["calibrated"] is False


def test_confidence_penalties_reduce_score():
    from wattmosaic_engine.contracts import Source

    base = ConfidenceInputs(
        source=Source.INFERRED,
        measurement_qualities=[QualityStatus.GOOD],
        measurement_ages_s=[0.0],
        supporting_ct_count=2,
        model_quality=0.8,
        event_isolation=1.0,
        topology_status="confirmed",
    )
    clean = compute_confidence(base)
    stale = compute_confidence(
        dataclasses.replace(base, stale=True, ambiguous=True, missing_rows=True)
    )
    assert stale.score < clean.score
    assert "stale" in stale.penalties
    assert "ambiguous" in stale.penalties


def test_a_pair_spanning_two_devices_is_not_summed_into_either() -> None:
    """Defect #57: two circuits paired as if they were one double-pole load.

    ``paired_ct_id`` means "the other leg of a single double-pole load", but the
    field is free text: the discovery heuristic pairs anything named
    ``..._ct1``/``..._ct2``, and a user can pair any two channels by hand.  The
    engine used to sum a declared pair unconditionally, so two independent
    circuits -- each already its own device -- reported **each other's power as
    well**: the same watts attributed twice, and no error anywhere.

    A pair is only a double-pole leg when the partner names the same device.
    """
    topo = Topology(
        channels=[
            _ch("lights_ct1", CTType.DEDICATED_DEVICE, phase=Phase.L1,
                device="lights_1", pair="lights_ct2"),
            _ch("lights_ct2", CTType.DEDICATED_DEVICE, phase=Phase.L2,
                device="lights_2", pair="lights_ct1"),
        ],
        devices=[
            DeviceTopology(device_id="lights_1", dedicated_ct_ids=["lights_ct1"], phase=Phase.L1),
            DeviceTopology(device_id="lights_2", dedicated_ct_ids=["lights_ct2"], phase=Phase.L2),
        ],
        status=TopologyStatus.CONFIRMED,
    )
    graph = build_graph(topo)
    window = ObservationWindow.from_values(T0, {"lights_ct1": 17.7, "lights_ct2": 238.7})
    readings = resolve_direct(window, graph)

    assert readings["lights_1"].power_w == pytest.approx(17.7)
    assert readings["lights_2"].power_w == pytest.approx(238.7)
    # Neither device may claim the other's row.
    assert readings["lights_1"].ct_ids == ("lights_ct1",)
    assert readings["lights_2"].ct_ids == ("lights_ct2",)


def test_a_genuine_double_pole_pair_still_sums_both_legs() -> None:
    """The complement of defect #57: a real pair must keep working.

    Both legs belong to the same device, so the sum *is* the device's power.
    Dropping the partner here would report half the load.
    """
    graph = build_graph(_dedicated_topology())
    window = ObservationWindow.from_values(
        T0, {"ct_tv": 0.0, "ct_tub_l1": 1200.0, "ct_tub_l2": 1300.0}
    )
    readings = resolve_direct(window, graph)

    assert readings["tub"].power_w == pytest.approx(2500.0)
    assert set(readings["tub"].ct_ids) == {"ct_tub_l1", "ct_tub_l2"}


def test_attribution_never_borrows_a_cross_device_partner() -> None:
    """The same rule at the topology level, which feeds totals and nesting."""
    topo = Topology(
        channels=[
            _ch("fridge_1", CTType.DEDICATED_DEVICE, device="fridge_1", pair="fridge_2"),
            _ch("fridge_2", CTType.DEDICATED_DEVICE, device="fridge_2", pair="fridge_1"),
        ],
        devices=[
            DeviceTopology(device_id="fridge_1", dedicated_ct_ids=["fridge_1"]),
            DeviceTopology(device_id="fridge_2", dedicated_ct_ids=["fridge_2"]),
        ],
        status=TopologyStatus.CONFIRMED,
    )
    graph = build_graph(topo)
    assert graph.attribution_ct_ids("fridge_1") == ("fridge_1",)
    assert graph.attribution_ct_ids("fridge_2") == ("fridge_2",)


# --------------------------------------------------------------------------- #
# W2: an undeclared device phase must not halve its own measurement
# --------------------------------------------------------------------------- #
def test_undeclared_device_on_its_own_ct_is_not_phase_split():
    """A device whose phase nobody declared still owns its whole reading.

    ``phase_coefficient`` splits a *double-pole* device across the legs, because
    a single-phase row only sees half of it.  A device of unknown phase has no
    declared leg to split, so on its own attribution row the CT is the only
    observation there is: halving it would fabricate a missing 50 %.
    """
    topo = Topology(
        channels=[
            *two_mains(),
            _ch("ct_lamp", CTType.DEDICATED_DEVICE, device="lamp"),
        ],
        devices=[
            # phase deliberately left at the default (UNKNOWN)
            DeviceTopology(device_id="lamp", dedicated_ct_ids=["ct_lamp"]),
        ],
        status=TopologyStatus.CONFIRMED,
    )
    graph = build_graph(topo)
    matrix = build_measurement_matrix(graph, ["lamp"])
    j = matrix.col_index("lamp")
    row = matrix.row_index("ct_lamp")
    assert matrix.A[row, j] == pytest.approx(1.0)

    # And the direct path reports the measured value, not half of it.
    window = ObservationWindow.from_values(T0, {"ct_lamp": 240.0})
    readings = resolve_direct(window, graph)
    assert readings["lamp"].power_w == pytest.approx(240.0)


def test_solo_flag_controls_the_unknown_phase_split():
    topo = Topology(
        channels=[*two_mains(), _ch("ct_lamp", CTType.DEDICATED_DEVICE, device="lamp")],
        devices=[DeviceTopology(device_id="lamp", dedicated_ct_ids=["ct_lamp"])],
        status=TopologyStatus.CONFIRMED,
    )
    graph = build_graph(topo)
    assert graph.device_phase("lamp") == Phase.L1  # derived from its own CT leg
    # A shared row still apportions, because the total has to be divided somehow.
    assert graph.phase_coefficient("lamp", Phase.L1, solo=False) == pytest.approx(1.0)
    assert graph.phase_coefficient("lamp", Phase.L2, solo=False) == pytest.approx(0.0)


def test_device_phase_is_derived_from_its_attribution_legs():
    """Discovery leaves ``phase`` unset; the CT it is wired through does not."""
    topo = Topology(
        channels=[
            *two_mains(),
            _ch("ct_a", CTType.DEDICATED_DEVICE, device="a", phase=Phase.L2),
            _ch("ct_pair_l1", CTType.DEDICATED_DEVICE, device="pair", phase=Phase.L1, pair="ct_pair_l2"),
            _ch("ct_pair_l2", CTType.DEDICATED_DEVICE, device="pair", phase=Phase.L2, pair="ct_pair_l1"),
        ],
        devices=[
            DeviceTopology(device_id="a", dedicated_ct_ids=["ct_a"]),
            DeviceTopology(device_id="pair", dedicated_ct_ids=["ct_pair_l1", "ct_pair_l2"]),
            DeviceTopology(device_id="unknown"),
        ],
        status=TopologyStatus.CONFIRMED,
    )
    graph = build_graph(topo)
    assert graph.device_phase("a") == Phase.L2
    assert graph.device_phase("pair") == Phase.BOTH
    assert graph.device_phase("unknown") == Phase.UNKNOWN


# --------------------------------------------------------------------------- #
# W1: aliases and nested mains in the measurement matrix
# --------------------------------------------------------------------------- #
def test_alias_channel_does_not_get_its_own_row_and_is_a_fallback():
    """Two CTs on one wire must not become two equations."""
    topo = Topology(
        channels=[
            _ch("service", CTType.MAIN, phase=Phase.BOTH),
            _ch("service_shadow", CTType.MAIN, phase=Phase.BOTH, alias="service"),
            _ch("branch", parent="service"),
        ],
        devices=[
            DeviceTopology(device_id="lamp", branch_ct_ids=["branch"], phase=Phase.L1)
        ],
        status=TopologyStatus.CONFIRMED,
    )
    graph = build_graph(topo)
    matrix = build_measurement_matrix(graph, ["lamp"])
    assert "service" in matrix.row_channel_ids
    assert "service_shadow" not in matrix.row_channel_ids
    assert matrix.channel_aliases["service"] == ("service_shadow",)

    # Canonical present: canonical wins.
    prepared = matrix.prepare({"service": 900.0, "service_shadow": 940.0, "branch": 40.0})
    assert prepared.y[matrix.row_index("service")] == pytest.approx(900.0)
    # Canonical missing: the alias measures the same wire, so use it.
    fallback = matrix.prepare({"service_shadow": 940.0, "branch": 40.0})
    assert fallback.mask[matrix.row_index("service")]
    assert fallback.y[matrix.row_index("service")] == pytest.approx(940.0)
    assert any(
        note.startswith("alias_fallback:") for note in matrix.row_notes["service"]
    )


def test_a_nested_main_is_not_subtracted_from_its_outer_main():
    """A main row already contains every device below it, sub feeds included."""
    topo = Topology(
        channels=[
            _ch("service", CTType.MAIN, phase=Phase.BOTH),
            _ch("panel", CTType.MAIN, phase=Phase.BOTH, parent="service"),
            _ch("branch", parent="panel"),
        ],
        devices=[
            DeviceTopology(device_id="lamp", branch_ct_ids=["branch"], phase=Phase.L1),
        ],
        status=TopologyStatus.CONFIRMED,
    )
    graph = build_graph(topo)
    matrix = build_measurement_matrix(graph, ["lamp"])
    # The service row carries the lamp...
    assert matrix.A[matrix.row_index("service"), matrix.col_index("lamp")] > 0.0
    # ...and therefore must not subtract the panel that measures it too.
    assert "panel" not in matrix.nested_children.get("service", ())
    # The branch row genuinely omits the lamp, so the panel has nothing to
    # subtract for it either: only *child rows* whose devices are missing from
    # the parent qualify.
    prepared = matrix.prepare({"service": 900.0, "panel": 500.0, "branch": 100.0})
    assert prepared.y[matrix.row_index("service")] == pytest.approx(900.0)


# --------------------------------------------------------------------------- #
# W3: the solver must not hang on duplicated rows
# --------------------------------------------------------------------------- #
def test_duplicate_rows_from_a_partnerless_pair_are_merged():
    """The exact 8x13 system from the real capture that used to hang forever.

    Two ``paired_ct_id`` channels whose ``device_id`` is ``None`` produce
    identical coefficient rows.  scipy's trust-region line search then halves
    ``alpha`` forever on a numerically-zero step.  The solver must merge them,
    report the merge, and return.
    """
    from pathlib import Path

    npz = Path(
        "/private/var/folders/wy/06f64nbx6kz8z88km48c3b3h0000gn/T/opencode/hang_system.npz"
    )
    if not npz.exists():  # pragma: no cover - fixture is local-only
        pytest.skip("saved hanging system is not present")
    saved = np.load(npz)
    A, y, hi = saved["A"], saved["y"], saved["hi"]
    result = solve_constrained_nnls(
        A, y, device_ids=[f"d{i}" for i in range(A.shape[1])], upper=hi
    )
    assert result.converged is True
    assert result.merged_rows == {3: 2, 5: 4}
    assert np.all(np.isfinite(result.x))
    assert np.all(result.x >= 0.0)


def test_solver_reports_duplicate_merges_in_a_tiny_system():
    A = np.array([[1.0, 1.0], [1.0, 1.0], [1.0, 0.0]])
    y = np.array([300.0, 340.0, 120.0])
    result = solve_constrained_nnls(A, y, device_ids=["a", "b"])
    # Rows 0 and 1 are identical: one equation with the combined weight.
    assert result.merged_rows == {1: 0}
    assert result.converged is True
    assert result.x == pytest.approx([120.0, 200.0], abs=1.0)


def test_solver_drops_zero_rows_and_reports_it():
    A = np.array([[0.0, 0.0], [1.0, 0.0]])
    y = np.array([999.0, 250.0])
    result = solve_constrained_nnls(A, y, device_ids=["a", "b"])
    assert result.dropped_rows == [0]
    assert result.x == pytest.approx([250.0, 0.0])


def test_solver_with_no_supported_columns_converges_to_zero():
    A = np.zeros((2, 3))
    y = np.array([100.0, 200.0])
    result = solve_constrained_nnls(A, y, device_ids=["a", "b", "c"])
    assert result.converged is True
    assert np.all(result.x == 0.0)
