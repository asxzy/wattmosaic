"""The residual as a first-class, decomposed object (WP7 / Group F).

These tests pin the *arithmetic contract*, not a classifier:

* ``Residual(t) = CircuitPower(t) - Sum(KnownLoadContributions(t))`` is only
  computed when the bases agree and no conductor is counted twice;
* the residual keeps its sign -- a negative residual is a real observation and
  is never clamped to zero;
* ``resolved=False`` is a permanent, visible state, never rendered as ``0 W``;
* the baseline/dynamic split is a decomposition of the *same* number, so the
  two parts add back to the total;
* duplicate and aliased CTs are collapsed through
  ``TopologyGraph.canonical_channel_id`` before anything is subtracted, and a
  contribution already enclosed by another contribution is not subtracted again.

Nothing here re-implements the engine's own balance.  The test that asserts the
existing invariant builds a real ``Reconciliation`` from the real engine and only
then attaches the new object.
"""

from __future__ import annotations

import inspect
import math
from datetime import UTC, datetime, timedelta
from statistics import median
from typing import Any

import pytest
from wattmosaic_engine.contracts import (
    CTChannel,
    CTType,
    DeviceTopology,
    MeasurementBasis,
    Phase,
    QualityStatus,
    Reconciliation,
    ResidualKind,
    ResidualPower,
    Topology,
    TopologyStatus,
    derived_residual_kind,
)
from wattmosaic_engine.disaggregation import DisaggregationEngine
from wattmosaic_engine.electrical import ObservationWindow
from wattmosaic_engine.electrical.topology import build_graph
from wattmosaic_engine.models.residual_power import (
    IncompatibleBasisError,
    ResidualAnalyzer,
    ResidualDecomposition,
    ResidualDecompositionConflict,
    ResidualNotComputed,
    ResidualScope,
    ResidualTrend,
    persist,
)
from wattmosaic_engine.storage.database import Database

T0 = datetime(2026, 11, 7, 13, 20, 0, tzinfo=UTC)
LATER = T0 + timedelta(minutes=15)


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #
def _chan(cid: str, ct_type: CTType, phase: Phase, *, alias: str | None = None) -> CTChannel:
    return CTChannel(
        ct_id=cid,
        entity_id=f"sensor.{cid}",
        ct_type=ct_type,
        phase=phase,
        alias_of_ct_id=alias,
        topology_status=TopologyStatus.CONFIRMED,
    )


def _aliased_graph():
    """One canonical feed pair plus a second monitor clamped on the same wire."""
    topo = Topology(
        version=4,
        status=TopologyStatus.CONFIRMED,
        channels=[
            _chan("feed_l1", CTType.MAIN, Phase.L1),
            _chan("feed_l2", CTType.MAIN, Phase.L2),
            _chan("panel_l1", CTType.MAIN, Phase.L1, alias="feed_l1"),
            _chan("panel_l2", CTType.MAIN, Phase.L2, alias="feed_l2"),
        ],
    )
    return build_graph(topo)


def _plain_graph():
    topo = Topology(
        version=4,
        status=TopologyStatus.CONFIRMED,
        channels=[
            _chan("feed_l1", CTType.MAIN, Phase.L1),
            _chan("feed_l2", CTType.MAIN, Phase.L2),
        ],
    )
    return build_graph(topo)


def _scope(**kwargs: Any) -> ResidualScope:
    base: dict[str, Any] = {
        "scope_id": "whole_home",
        "ct_id": "feed_l1",
        "basis": MeasurementBasis.CT_MEASURED,
        "multiplicity": 1,
        "is_nested_under": None,
        "device_contributions": {},
    }
    base.update(kwargs)
    return ResidualScope(**base)


def _assert_negative_is_preserved(residual: ResidualPower, expected_w: float) -> None:
    """The suite's single no-clamp assertion, reused by the mutation test."""
    assert residual.residual_w == pytest.approx(expected_w, abs=1e-9)
    assert residual.residual_w < 0.0
    assert residual.kind == ResidualKind.NEGATIVE
    # The canonical derivation, not a second opinion.
    assert residual.kind == derived_residual_kind(residual.residual_w)


# --------------------------------------------------------------------------- #
# Group F1/F2/F3 -- baseline, standby, dynamic
# --------------------------------------------------------------------------- #
def test_f1_fixed_baseline_residual_is_the_median_of_the_window():
    analyzer = ResidualAnalyzer()
    # The last five residual observations of a steady 200 W unexplained load.
    scope = _scope(
        device_contributions={"fridge": 800.0},
        window_w=(196.0, 199.0, 200.0, 201.0, 204.0),
    )

    residual = analyzer.compute(scope, 1000.0, now=LATER)

    assert residual.residual_w == pytest.approx(200.0)
    assert residual.baseline_w == pytest.approx(200.0)
    assert residual.dynamic_w == pytest.approx(0.0, abs=1e-9)
    assert residual.kind == ResidualKind.POSITIVE
    assert analyzer.last_evidence["baseline_estimator"] == "median_of_window"


def test_f2_standby_residual_is_a_baseline_not_an_error():
    analyzer = ResidualAnalyzer()
    scope = _scope(
        device_contributions={"fridge": 500.0},
        window_w=(12.0, 12.5, 11.5, 12.0, 12.2),
    )

    residual = analyzer.compute(scope, 512.0)

    assert residual.residual_w == pytest.approx(12.0)
    assert residual.baseline_w == pytest.approx(12.0)
    assert residual.dynamic_w == pytest.approx(0.0, abs=1e-9)
    # 12 W of always-on load is genuinely unexplained load.  It is not "close
    # enough to zero" and it is not evidence that everything is metered.
    assert residual.kind == ResidualKind.POSITIVE
    assert residual.resolved is False
    assert residual.unresolved_since is None  # no clock supplied, so no claim


def test_f3_dynamic_residual_tracks_a_ramp_window():
    analyzer = ResidualAnalyzer()
    # A continuously varying unmetered load: 100 -> 160 -> 220 -> 280 W,
    # with a known 220 W load subtracted from a 500 W circuit.
    scope = _scope(
        device_contributions={"dryer": 220.0},
        window_w=(100.0, 160.0, 220.0, 280.0),
    )

    residual = analyzer.compute(scope, 500.0)

    assert residual.residual_w == pytest.approx(280.0)
    # Median of {100, 160, 220, 280} is 190 -> the moving part is 90 W.
    assert residual.baseline_w == pytest.approx(190.0)
    assert residual.dynamic_w == pytest.approx(90.0)


# --------------------------------------------------------------------------- #
# Group F4/F5/F6 -- sign
# --------------------------------------------------------------------------- #
def test_f4_positive_residual_reports_positive():
    residual = ResidualAnalyzer().compute(_scope(), 1000.0, now=LATER)
    assert residual.residual_w == pytest.approx(1000.0)
    assert residual.kind == ResidualKind.POSITIVE


def test_f5_negative_residual_is_preserved_and_never_clamped():
    analyzer = ResidualAnalyzer()
    scope = _scope(device_contributions={"dryer": 700.0, "oven": 300.0})

    residual = analyzer.compute(scope, 800.0, now=LATER)

    # 800 - 1000 = -200.  Not 0, not abs(), not "near zero".
    _assert_negative_is_preserved(residual, -200.0)
    assert residual.dynamic_w == pytest.approx(0.0, abs=1e-9)


def test_f6_near_zero_residual_is_exactly_zero_and_resolved():
    scope = _scope(device_contributions={"tv": 90.0, "lamp": 10.0})
    residual = ResidualAnalyzer().compute(scope, 100.0, now=LATER)

    assert residual.residual_w == 0.0
    assert residual.kind == ResidualKind.NEAR_ZERO
    assert residual.resolved is True
    assert residual.unresolved_since is None


def test_a_residual_of_one_watt_is_not_near_zero():
    residual = ResidualAnalyzer().compute(_scope(), 1.0)
    assert residual.residual_w == pytest.approx(1.0)
    assert residual.kind == ResidualKind.POSITIVE


# --------------------------------------------------------------------------- #
# Group F7/F8/F9 -- decomposition
# --------------------------------------------------------------------------- #
def _history(values: list[float], start: datetime = T0) -> list[dict[str, object]]:
    """DB-shaped residual history, **newest first**, like ``residual_history``."""
    rows: list[dict[str, object]] = []
    for index, value in enumerate(values):
        rows.append(
            {
                "timestamp": (start + timedelta(minutes=15 * index)).isoformat(),
                "residual_w": value,
                "kind": derived_residual_kind(value).value,
                "resolved": False,
                "unresolved_since": start.isoformat(),
                "explained_by_device_w": {},
                "quality_status": QualityStatus.GOOD.value,
                "basis": MeasurementBasis.CT_MEASURED.value,
                "baseline_w": None,
                "dynamic_w": None,
            }
        )
    return list(reversed(rows))


def test_f7_a_new_device_explains_part_of_the_residual():
    analyzer = ResidualAnalyzer()
    history = _history([400.0, 400.0, 400.0])

    decomposition = analyzer.decompose(history, {"dryer": 250.0}, now=LATER)

    assert decomposition.before_w == pytest.approx(400.0)
    assert decomposition.after_w == pytest.approx(150.0)
    assert decomposition.explained_by_device_w == {"dryer": pytest.approx(250.0)}
    assert decomposition.unexplained_w == pytest.approx(150.0)
    # Partly explained is still unresolved: 150 W remains unattributed.
    assert decomposition.resolved is False
    assert decomposition.conflict is False
    # The history keeps before/after, so a shrink is visible rather than silent.
    assert decomposition.evidence["before_w"] == pytest.approx(400.0)
    assert decomposition.evidence["after_w"] == pytest.approx(150.0)

    carried = decomposition.to_residual_power(now=LATER)
    assert carried.residual_w == pytest.approx(150.0)
    assert carried.explained_by_device_w == {"dryer": pytest.approx(250.0)}
    assert carried.resolved is False


def test_f7b_a_candidate_that_explains_all_of_it_resolves_it():
    decomposition = ResidualAnalyzer().decompose(_history([150.0]), {"dryer": 150.0})
    assert decomposition.after_w == 0.0
    assert decomposition.resolved is True
    assert decomposition.unexplained_w == 0.0


def test_f8_a_residual_that_cannot_be_disaggregated_stays_unresolved():
    analyzer = ResidualAnalyzer()
    history = _history([400.0, 380.0])

    decomposition = analyzer.decompose(history, {}, now=LATER)

    assert decomposition.explained_by_device_w == {}
    assert decomposition.unexplained_w == pytest.approx(380.0)
    assert decomposition.after_w == pytest.approx(380.0)
    assert decomposition.resolved is False
    assert decomposition.conflict is False
    assert decomposition.reason_code == "residual_not_disaggregable"


def test_f8b_a_candidate_that_does_not_fit_is_refused_not_clipped():
    decomposition = ResidualAnalyzer().decompose(_history([200.0]), {"heat_pump": 900.0})
    assert decomposition.conflict is True
    assert decomposition.explained_by_device_w == {}
    assert decomposition.resolved is False


def test_f9_over_explaining_candidates_flag_a_conflict_without_forcing_resolution():
    analyzer = ResidualAnalyzer()
    history = _history([300.0])

    decomposition = analyzer.decompose(history, {"dryer": 300.0, "oven": 200.0}, now=LATER)

    assert decomposition.conflict is True
    assert decomposition.reason_code == "candidates_over_explain_residual"
    # Nothing is invented: a conflict resolves nothing.
    assert decomposition.explained_by_device_w == {}
    assert decomposition.unexplained_w == pytest.approx(300.0)
    assert decomposition.resolved is False
    assert isinstance(decomposition, ResidualDecomposition)

    with pytest.raises(ResidualDecompositionConflict):
        decomposition.require_resolved()


def test_f9b_a_positive_claim_on_a_negative_residual_is_a_conflict():
    decomposition = ResidualAnalyzer().decompose(_history([-120.0]), {"dryer": 100.0})
    assert decomposition.conflict is True
    assert decomposition.reason_code == "residual_is_negative"
    assert decomposition.explained_by_device_w == {}
    assert decomposition.before_w == pytest.approx(-120.0)


def test_a_negative_candidate_is_not_a_load():
    decomposition = ResidualAnalyzer().decompose(_history([300.0]), {"export": -50.0})
    assert decomposition.explained_by_device_w == {}
    assert decomposition.resolved is False
    assert any(item["reason"] == "not_a_load" for item in decomposition.evidence["excluded"])


# --------------------------------------------------------------------------- #
# Group F10 -- duplicate / aliased CTs
# --------------------------------------------------------------------------- #
def test_f10_a_duplicate_ct_is_not_subtracted_twice():
    graph = _aliased_graph()
    analyzer = ResidualAnalyzer(topology=graph)
    # Two "devices" whose readings come from the two CTs on ONE conductor.
    scope = _scope(
        ct_id="feed_l1",
        multiplicity=2,
        ct_ids=("feed_l1", "panel_l1"),
        readings={"feed_l1": 1000.0, "panel_l1": 1000.0},
        device_contributions={"dryer_a": 250.0, "dryer_b": 250.0},
        contribution_cts={"dryer_a": "feed_l1", "dryer_b": "panel_l1"},
    )

    residual = analyzer.compute(scope, 1000.0, now=LATER)

    # 1000 - 250 (counted once), not 1000 - 2*250 = 500.
    assert residual.residual_w == pytest.approx(750.0)
    assert residual.residual_w != pytest.approx(500.0)
    assert analyzer.last_evidence["collapsed_aliases"] == {
        "feed_l1": ("feed_l1", "panel_l1")
    }
    assert analyzer.last_evidence["duplicate_contributions"] == {
        "dryer_b": "same_conductor_as:dryer_a"
    }


def test_f10b_a_duplicated_scope_reading_is_counted_once_not_summed():
    graph = _aliased_graph()
    analyzer = ResidualAnalyzer(topology=graph)
    scope = _scope(
        ct_id="feed_l1",
        multiplicity=2,
        ct_ids=("feed_l1", "panel_l1"),
        readings={"feed_l1": 1000.0, "panel_l1": 1020.0},
    )

    residual = analyzer.compute(scope, 1000.0)

    # Both CTs are on one conductor: the 20 W difference is a meter spread, not
    # 2020 W of load and not 20 W of extra residual.
    assert residual.residual_w == pytest.approx(1000.0)
    assert analyzer.last_evidence["alias_spread_w"] == pytest.approx(20.0)


def test_two_independent_cts_claiming_one_scope_are_refused():
    graph = _plain_graph()
    analyzer = ResidualAnalyzer(topology=graph)
    scope = _scope(
        ct_id="feed_l1",
        multiplicity=2,
        ct_ids=("feed_l1", "feed_l2"),
        readings={"feed_l1": 1000.0, "feed_l2": 900.0},
    )

    with pytest.raises(ResidualNotComputed) as excinfo:
        analyzer.compute(scope, 1000.0)
    assert excinfo.value.reason_code == "scope_spans_multiple_channels"

    attempt = analyzer.try_compute(scope, 1000.0)
    assert attempt.residual is None
    assert attempt.refused is True
    assert attempt.reason_code == "scope_spans_multiple_channels"


def test_an_aliased_pair_inside_a_scope_is_counted_once():
    graph = _aliased_graph()
    assert graph.canonical_channel_id("panel_l1") == "feed_l1"

    analyzer = ResidualAnalyzer(topology=graph)
    scope = _scope(
        ct_id="panel_l1",  # the alias as the declared scope
        multiplicity=2,
        ct_ids=("feed_l1", "panel_l1"),
        readings={"feed_l1": 640.0, "panel_l1": 640.0},
        device_contributions={"dryer": 140.0},
        contribution_cts={"dryer": "panel_l1"},
    )

    residual = analyzer.compute(scope, 640.0)

    # Re-canonicalised to the channel that owns the wire, and counted once:
    # 640 - 140, not 640 - 280 and not 1280 - something.
    assert residual.residual_w == pytest.approx(500.0)
    assert analyzer.last_evidence["canonical_channel_id"] == "feed_l1"
    assert analyzer.last_evidence["counted_channels"] == ["feed_l1"]
    assert analyzer.last_evidence["collapsed_aliases"] == {
        "feed_l1": ("feed_l1", "panel_l1")
    }


def test_contribution_measured_by_an_enclosing_ct_is_not_added_again():
    topo = Topology(
        version=1,
        status=TopologyStatus.CONFIRMED,
        channels=[
            _chan("feed_l1", CTType.MAIN, Phase.L1),
            CTChannel(
                ct_id="branch_kitchen",
                entity_id="sensor.branch_kitchen",
                ct_type=CTType.BRANCH,
                phase=Phase.L1,
                parent_ct_id="feed_l1",
                topology_status=TopologyStatus.CONFIRMED,
            ),
            CTChannel(
                ct_id="ct_fridge",
                entity_id="sensor.ct_fridge",
                ct_type=CTType.BRANCH,
                phase=Phase.L1,
                parent_ct_id="branch_kitchen",
                device_id="fridge",
                topology_status=TopologyStatus.CONFIRMED,
            ),
        ],
    )
    analyzer = ResidualAnalyzer(topology=build_graph(topo))
    scope = _scope(
        ct_id="feed_l1",
        device_contributions={"kitchen_total": 800.0, "fridge": 120.0},
        contribution_cts={"kitchen_total": "branch_kitchen", "fridge": "ct_fridge"},
    )

    residual = analyzer.compute(scope, 1000.0)

    # ``branch_kitchen`` already contains ``ct_fridge``; only the enclosing
    # total is subtracted, exactly once (the ``effective_feeds`` rule).
    assert residual.residual_w == pytest.approx(200.0)
    assert analyzer.last_evidence["excluded_contributions"] == {
        "fridge": "enclosed_by:kitchen_total"
    }


# --------------------------------------------------------------------------- #
# Basis compatibility
# --------------------------------------------------------------------------- #
def test_incompatible_basis_is_refused():
    analyzer = ResidualAnalyzer()
    scope = _scope(
        device_contributions={"dryer": 300.0},
        contribution_bases={"dryer": MeasurementBasis.FACTOR_ADJUSTED},
    )

    with pytest.raises(IncompatibleBasisError) as excinfo:
        analyzer.compute(scope, 1000.0)
    assert excinfo.value.reason_code == "incompatible_basis"
    assert "dryer" in str(excinfo.value)

    attempt = analyzer.try_compute(scope, 1000.0)
    assert attempt.refused is True
    assert attempt.residual is None
    assert attempt.reason_code == "incompatible_basis"
    assert attempt.evidence["scope_basis"] == MeasurementBasis.CT_MEASURED.value
    assert attempt.evidence["contribution_basis"]["dryer"] == (
        MeasurementBasis.FACTOR_ADJUSTED.value
    )


def test_an_explicit_conversion_makes_the_bases_comparable():
    analyzer = ResidualAnalyzer()
    scope = _scope(
        device_contributions={"dryer_leg": 150.0},
        contribution_bases={"dryer_leg": MeasurementBasis.FACTOR_ADJUSTED},
        conversions={"dryer_leg": 2.0},
    )

    residual = analyzer.compute(scope, 500.0)

    # The recorded x2 is applied exactly once, so the contribution is 300 W.
    assert residual.residual_w == pytest.approx(200.0)
    assert analyzer.last_evidence["conversions"] == {"dryer_leg": 2.0}


def test_unknown_scope_basis_is_never_computed():
    analyzer = ResidualAnalyzer()
    scope = _scope(basis=MeasurementBasis.UNKNOWN)

    with pytest.raises(IncompatibleBasisError):
        analyzer.compute(scope, 1000.0)


def test_unestablished_contribution_basis_inherits_the_scope_basis():
    scope = _scope(device_contributions={"dryer": 250.0})
    residual = ResidualAnalyzer().compute(scope, 500.0)
    assert residual.residual_w == pytest.approx(250.0)


def test_two_different_physical_bases_are_compatible():
    scope = _scope(
        basis=MeasurementBasis.CT_MEASURED,
        device_contributions={"washer": 250.0},
        contribution_bases={"washer": MeasurementBasis.MONITOR_REPORTED},
    )
    residual = ResidualAnalyzer().compute(scope, 500.0)
    assert residual.residual_w == pytest.approx(250.0)


# --------------------------------------------------------------------------- #
# Resolution state
# --------------------------------------------------------------------------- #
def test_resolved_false_is_never_rendered_as_zero():
    analyzer = ResidualAnalyzer()
    residual = analyzer.compute(_scope(), 400.0, now=LATER)
    assert residual.resolved is False
    assert residual.residual_w == pytest.approx(400.0)
    assert residual.unresolved_since == LATER

    decomposition = analyzer.decompose(_history([400.0]), {}, now=LATER)
    payload = decomposition.as_dict()
    assert payload["residual_w"] == pytest.approx(400.0)
    assert payload["resolved"] is False
    text = " ".join(decomposition.explanation)
    assert "unresolved" in text
    assert "400.0" in text


def test_unresolved_since_is_preserved_across_cycles():
    first = ResidualAnalyzer().compute(_scope(), 400.0, now=T0)
    later = ResidualAnalyzer().compute(
        _scope(unresolved_since=first.unresolved_since), 400.0, now=LATER
    )
    assert first.unresolved_since == T0
    assert later.unresolved_since == T0

    resolved = ResidualAnalyzer().compute(
        _scope(unresolved_since=first.unresolved_since), 0.0, now=LATER
    )
    assert resolved.resolved is True
    assert resolved.unresolved_since is None


def test_a_scope_may_declare_its_own_explanation():
    scope = _scope(
        device_contributions={"dryer": 250.0},
        explained_by_device_w={"dryer": 250.0},
    )
    residual = ResidualAnalyzer().compute(scope, 250.0)
    assert residual.residual_w == 0.0
    assert residual.resolved is True


# --------------------------------------------------------------------------- #
# Baseline/dynamic split
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("window", "circuit", "contributions"),
    [
        ((120.0, 120.0, 120.0), 1000.0, {}),
        ((120.0, 900.0, 40.0), 500.0, {"dryer": 100.0}),
        ((-30.0, -30.0, -400.0), 100.0, {"oven": 300.0}),
        ((), 250.0, {}),
    ],
)
def test_baseline_and_dynamic_add_back_to_the_total(window, circuit, contributions):
    residual = ResidualAnalyzer().compute(
        _scope(window_w=window, device_contributions=contributions), circuit
    )
    assert residual.baseline_w is not None
    assert residual.dynamic_w is not None
    assert residual.baseline_w + residual.dynamic_w == pytest.approx(
        residual.residual_w, abs=1e-9
    )


def test_the_baseline_is_robust_to_a_minority_of_spikes():
    steady = ResidualAnalyzer().compute(
        _scope(window_w=(119.0, 120.0, 120.0, 121.0, 120.0)), 1000.0
    )
    spiked = ResidualAnalyzer().compute(
        _scope(window_w=(119.0, 120.0, 120.0, 121.0, 120.0, 5000.0)), 1000.0
    )
    # A mean would move by ~813 W; the median does not move at all.
    assert spiked.baseline_w == pytest.approx(120.0)
    assert spiked.baseline_w == pytest.approx(steady.baseline_w)


# --------------------------------------------------------------------------- #
# Trend & history
# --------------------------------------------------------------------------- #
def test_trend_orders_the_history_and_reports_current_and_typical():
    analyzer = ResidualAnalyzer()
    history = _history([100.0, 200.0, 300.0, 400.0])  # newest first

    trend = analyzer.trend(history)

    assert trend.current_w == pytest.approx(400.0)
    assert trend.typical_w == pytest.approx(250.0)
    assert trend.baseline_w == pytest.approx(250.0)
    assert trend.dynamic_w == pytest.approx(150.0)
    assert trend.kind == ResidualKind.POSITIVE
    assert trend.sample_count == 4
    assert len(trend.curve) == 4
    # Ascending: the curve is a time series, not the DB's newest-first order.
    stamps = [instant for instant, _ in trend.curve]
    assert stamps == sorted(stamps)
    assert [value for _, value in trend.curve] == [100.0, 200.0, 300.0, 400.0]


def test_trend_confidence_is_bounded_and_grows_with_history():
    short = ResidualAnalyzer().trend(_history([300.0]))
    long = ResidualAnalyzer().trend(_history([300.0] * 24))
    assert short.confidence is not None
    assert long.confidence is not None
    assert 0.0 <= short.confidence <= 1.0
    assert 0.0 <= long.confidence <= 1.0
    assert long.confidence > short.confidence


def test_trend_reports_the_age_of_an_unresolved_residual():
    trend = ResidualAnalyzer().trend(_history([300.0, 320.0]))
    assert trend.resolved is False
    assert trend.unresolved_since is not None
    assert trend.unresolved_since == T0


def test_trend_keeps_the_decomposition_history():
    analyzer = ResidualAnalyzer()
    history = _history([400.0, 300.0, 200.0])  # newest first; latest is 200 W
    first = analyzer.decompose(history, {"dryer": 100.0})
    second = analyzer.decompose(history, {"dryer": 100.0, "oven": 100.0})

    trend = analyzer.trend(history, decompositions=(first, second))

    assert trend.decompositions == (first, second)
    assert trend.decompositions[0].before_w == pytest.approx(200.0)
    assert trend.decompositions[0].after_w == pytest.approx(100.0)
    assert trend.decompositions[1].after_w == pytest.approx(0.0)


def test_an_empty_history_is_an_honest_empty_trend():
    trend = ResidualAnalyzer().trend([])
    assert trend.sample_count == 0
    assert trend.confidence is None
    # "Not measured" must not be rendered as a 0 W surface: a consumer that
    # prints ``0 W`` for unknown is publishing a fabricated measurement.
    assert trend.current_w is None
    assert trend.typical_w is None
    assert trend.baseline_w is None
    assert trend.dynamic_w is None
    assert trend.kind is None
    assert trend.measured is False
    assert trend.as_dict()["current_w"] is None
    assert trend.as_dict()["kind"] is None
    assert trend.reason_code == "no_residual_history"


def test_persist_round_trips_the_signed_residual(tmp_path):
    db = Database(tmp_path / "residual_power.db")
    try:
        residual = ResidualAnalyzer().compute(_scope(), 800.0, now=T0)
        written = persist([residual], db, timestamp=T0)
        assert written == 1

        rows = db.residual_history()
        assert len(rows) == 1
        assert rows[0]["residual_w"] == pytest.approx(800.0)
        assert rows[0]["kind"] == ResidualKind.POSITIVE.value
        assert rows[0]["resolved"] is False
        assert rows[0]["basis"] == MeasurementBasis.CT_MEASURED.value

        negative = ResidualAnalyzer().compute(
            _scope(device_contributions={"oven": 1200.0}), 800.0, now=LATER
        )
        persist([negative], db, timestamp=LATER)
        rows = db.residual_history()
        assert rows[0]["residual_w"] == pytest.approx(-400.0)
        assert rows[0]["kind"] == ResidualKind.NEGATIVE.value
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# TASK-RESIDUAL-001 -- the guarantees, pinned by name
# --------------------------------------------------------------------------- #
def test_signed_residual_is_never_clamped():
    """Scenario ``T15``: the negative residual survives every stage.

    The residual is computed, decomposed, carried into the frozen contract and
    persisted; none of those stages may clamp it.  ``abs()`` and ``max(0.0, ...)``
    are checked explicitly because they are the two ways a surplus becomes
    "unattributed load" -- a fact about the wrong direction.
    """
    analyzer = ResidualAnalyzer()
    scope = _scope(
        device_contributions={"dryer": 700.0, "oven": 300.0},
        window_w=(-180.0, -200.0, -220.0),
    )

    residual = analyzer.compute(scope, 800.0, now=LATER)
    _assert_negative_is_preserved(residual, -200.0)

    # The decomposition refuses to explain a deficit but must not flatten it:
    # ``before_w`` keeps the sign and the conflict names the reason.
    decomposition = analyzer.decompose(_history([-200.0]), {}, now=LATER)
    assert decomposition.before_w == pytest.approx(-200.0)
    assert decomposition.conflict is True
    assert decomposition.reason_code == "residual_is_negative"
    assert decomposition.unexplained_w == pytest.approx(-200.0)

    # A carried decomposition (resolved) never carries a sign-flattened value.
    carried = analyzer.decompose(_history([200.0]), {"dryer": 200.0}).require_resolved()
    assert carried.residual_w == 0.0
    assert carried.kind == ResidualKind.NEAR_ZERO

    # The stored row and the contract serialisation never turn it positive.
    payload = residual.model_dump(mode="json")
    assert payload["residual_w"] == pytest.approx(-200.0)
    assert payload["kind"] == ResidualKind.NEGATIVE.value
    assert payload["baseline_w"] + payload["dynamic_w"] == pytest.approx(-200.0)

    # The single subtraction routine is the only arithmetic and is unclamped.
    from wattmosaic_engine.models.residual_power import _signed_residual

    assert _signed_residual(800.0, 1000.0) == -200.0
    body = inspect.getsource(_signed_residual)
    # Strip the docstring: it *names* the two operations it refuses, so the
    # check is on the executable body, not on the prose that documents them.
    body = body.split('"""', 2)[-1]
    assert "max(0" not in body
    assert "abs(" not in body


@pytest.mark.parametrize(
    ("window", "circuit", "contributions", "expected"),
    [
        ((), 0.0, {}, 0.0),  # exactly zero: no window, no dynamic claim
        ((), 1000.0, {}, 1000.0),  # positive, no window
        ((196.0, 199.0, 200.0, 201.0, 204.0), 1000.0, {"fridge": 800.0}, 200.0),
        ((120.0, 900.0, 40.0), 500.0, {"dryer": 100.0}, 400.0),
        ((-30.0, -30.0, -400.0), 100.0, {"oven": 300.0}, -200.0),  # negative
        ((5000.0, 5000.0, 5000.0), 0.0, {}, 0.0),  # an all-spike window, zero total
    ],
)
def test_baseline_plus_dynamic_equals_residual(window, circuit, contributions, expected):
    """Scenario ``T03``: the identity holds for every published point.

    ``baseline_w`` and ``dynamic_w`` are a decomposition of the *same* signed
    number, so their sum is the residual exactly -- including for a zero residual
    and a negative one, which are the two cases a clamp would break.
    """
    residual = ResidualAnalyzer().compute(
        _scope(window_w=window, device_contributions=contributions), circuit
    )
    assert residual.residual_w == pytest.approx(expected)
    assert residual.baseline_w is not None
    assert residual.dynamic_w is not None
    assert residual.baseline_w + residual.dynamic_w == pytest.approx(
        residual.residual_w, abs=1e-9
    )
    # The zero case is a real published point, not an absent one.
    if expected == 0.0:
        assert residual.resolved is True
        assert residual.kind == ResidualKind.NEAR_ZERO
        # With a window the dynamic part is ``residual - median``; with none it is
        # exactly ``0.0`` (``no_window_current_only``).
        if window:
            assert residual.dynamic_w == pytest.approx(0.0 - median(window), abs=1e-9)
        else:
            assert residual.dynamic_w == pytest.approx(0.0, abs=1e-9)


def test_baseline_plus_dynamic_survives_the_persistence_round_trip(tmp_path):
    """The identity is asserted on the *published* point, after storage.

    An explicit column list silently dropping a component is the defect class
    this repo has been bitten by, so the identity is checked on the row that
    actually comes back from the database, not only on the in-memory object.
    """
    db = Database(tmp_path / "identity.db")
    try:
        residual = ResidualAnalyzer().compute(
            _scope(window_w=(190.0, 195.0, 200.0, 205.0, 210.0), device_contributions={"dryer": 800.0}),
            1000.0,
            now=T0,
        )
        assert residual.baseline_w is not None and residual.dynamic_w is not None
        persist([residual], db, timestamp=T0)

        rows = db.residual_history()
        assert len(rows) == 1
        row = rows[0]
        assert row["baseline_w"] is not None
        assert row["dynamic_w"] is not None
        assert row["baseline_w"] + row["dynamic_w"] == pytest.approx(
            row["residual_w"], abs=1e-9
        )
    finally:
        db.close()


def test_residual_trend_publishes_its_pattern():
    """Scenarios ``T03``/``T10``: the pattern is published, with a derived confidence.

    The confidence is ``min(1.0, n / (n + 8))``: it is a function of the sample
    count, never a hardcoded 1.0 and never a fabricated 0.0.
    """
    analyzer = ResidualAnalyzer()
    history = _history([100.0, 200.0, 300.0, 400.0])  # newest first

    trend = analyzer.trend(history)

    payload = trend.as_dict()
    assert payload["current_w"] == pytest.approx(400.0)
    assert payload["typical_w"] == pytest.approx(250.0)
    assert payload["baseline_w"] == pytest.approx(250.0)
    assert payload["dynamic_w"] == pytest.approx(150.0)
    assert payload["kind"] == ResidualKind.POSITIVE.value
    assert payload["sample_count"] == 4
    assert payload["evidence"]["cause_counts"] == {
        "positive": 4,
        "negative": 0,
        "near_zero": 0,
    }
    assert payload["evidence"]["sign_changes"] == 0
    assert trend.confidence == pytest.approx(min(1.0, 4 / (4 + 8.0)))
    assert trend.confidence is not None
    assert trend.confidence > 0.0

    # The curve is a time series in ascending order, so a consumer can plot it.
    assert [value for _, value in trend.curve] == [100.0, 200.0, 300.0, 400.0]


def test_residual_trend_confidence_is_none_without_history():
    """No history is not ``0.0``: the confidence is ``None``, never a number.

    A fabricated ``0.0`` would read as "the trend is certainly not supported"
    while a fabricated ``1.0`` would read as "certain"; the honest answer is
    that no trend was computed at all, and the reason code says so.
    """
    trend = ResidualAnalyzer().trend([])

    assert trend.confidence is None
    assert trend.as_dict()["confidence"] is None
    assert trend.confidence != 0.0
    assert trend.reason_code == "no_residual_history"
    assert trend.sample_count == 0


def test_unresolved_residual_is_none_not_zero():
    """An unresolved residual is ``null`` with an age, never ``0 W``.

    The consumer-visible surface is the serialised payload: a residual that was
    never measured must be absent, not a number a consumer can read as zero
    power.  ``ResidualPower`` itself is a contract that carries a number, so
    the honest "no measurement" case lives in the decomposition/trend payloads
    and in the refusal path.
    """
    analyzer = ResidualAnalyzer()

    # No history at all: the decomposition has nothing to carry, and the
    # serialised form must be absent rather than 0.0.
    decomposition = analyzer.decompose([], {"dryer": 100.0})
    payload = decomposition.as_dict()
    assert decomposition.before_w is None
    assert decomposition.unexplained_w is None
    assert decomposition.after_w is None
    assert payload["residual_w"] is None
    assert payload["before_w"] is None
    assert payload["after_w"] is None
    assert payload["resolved"] is False
    assert payload["quality_status"] == QualityStatus.MISSING.value
    assert payload["reason_code"] == "no_residual_history"
    # A consumer that reads the payload as a number must not get 0.0.
    assert payload["residual_w"] != 0.0
    assert payload["before_w"] != 0.0

    # The trend is absent too, and carries the age of nothing.
    trend = analyzer.trend([])
    assert trend.current_w is None
    assert trend.confidence is None
    assert trend.unresolved_since is None
    assert trend.as_dict()["current_w"] is None

    # An unresolved residual that *was* measured keeps its age and its value:
    # "unresolved" is a state of the number, not a missing number.
    unresolved = analyzer.compute(_scope(), 400.0, now=LATER)
    assert unresolved.resolved is False
    assert unresolved.unresolved_since == LATER
    assert unresolved.residual_w == pytest.approx(400.0)
    assert unresolved.resolved is not True
    carried = analyzer.decompose(_history([400.0]), {}, now=LATER)
    assert carried.unexplained_w == pytest.approx(400.0)
    assert carried.resolved is False
    assert carried.unexplained_w != 0.0


def test_the_residual_is_never_redistributed_onto_a_device():
    """``REQ-RESIDUAL-007``: no device power moves because a residual existed.

    The residual is a *view* over the engine's balance: the device rows the
    engine produced are byte-identical before and after the residual is
    attached, and no field on ``Reconciliation`` can express a per-device
    distribution of the unknown remainder.
    """
    topo = _aliased_engine_topology()
    window = ObservationWindow.from_values(
        T0,
        {
            "load_manager_l1": 1000.0,
            "load_manager_l2": 0.0,
            "main_panel_l1": 651.1,
            "main_panel_l2": 0.0,
            "ct_heater": 833.6,
        },
        cadence_s=15.7,
    )
    reconciliation = DisaggregationEngine().infer(
        window, device_states={}, models={}, topology=topo
    ).reconciliation

    analyzer = ResidualAnalyzer(topology=build_graph(topo))
    residual = analyzer.compute(
        _scope(
            scope_id="whole_home",
            ct_id="load_manager_l1",
            device_contributions={"heater": reconciliation.inferred_w},
        ),
        reconciliation.main_power_w,
        now=reconciliation.timestamp,
    )

    before = reconciliation.model_dump(mode="json")
    attached = reconciliation.model_copy(update={"residual": residual})
    after = attached.model_dump(mode="json")

    # The device-facing components are unchanged: the residual was not pushed
    # into any device's estimate.
    for field in (
        "direct_measured_w",
        "inferred_w",
        "model_estimated_w",
        "configured_prior_w",
        "main_power_w",
        "meter_disagreement_w",
        "unmonitored_w",
        "unknown_residual_w",
    ):
        assert after[field] == pytest.approx(before[field], abs=1e-9)
    # The unknown remainder is still the authoritative total.
    assert attached.unknown_residual_w == pytest.approx(
        reconciliation.unknown_residual_w, abs=1e-9
    )
    # No contract field can express "residual distributed to a device".
    assert not any("redistribut" in name for name in Reconciliation.model_fields)


def test_the_noise_component_is_not_publishable_without_a_contract_change():
    """``05-…`` §3.3.4 / ``G74``: the third component has no contract home.

    The measured per-Reader noise floor exists (``main.py:1093`` publishes
    ``channel_noise_floor_w``), but ``ResidualPower`` has no field to carry it,
    so the residual decomposition cannot publish a noise term today.  This test
    records that boundary: it fails if a noise field appears on the contract
    without the additive-and-optional protocol, and it is the check the later
    wave must update.
    """
    fields = ResidualPower.model_fields
    assert "baseline_w" in fields
    assert "dynamic_w" in fields
    assert "noise_w" not in fields
    assert "noise_floor_w" not in fields
    assert "noise_source" not in fields
    # The decomposition still sums to the residual without a noise term: the
    # split is a two-part decomposition until the contract grows a third.
    residual = ResidualAnalyzer().compute(_scope(window_w=(190.0, 200.0, 210.0)), 1200.0)
    assert residual.baseline_w + residual.dynamic_w == pytest.approx(
        residual.residual_w, abs=1e-9
    )


def test_persist_round_trips_every_decomposition_component(tmp_path):
    """The write path must not silently drop a component.

    ``Database.insert_residual_power`` carries an explicit column list, which is
    exactly the shape that drops a field when a contract grows one.  The three
    published components -- the signed value, ``baseline_w`` and ``dynamic_w`` --
    are asserted on the row that comes back, for a negative residual and for a
    zero one.
    """
    db = Database(tmp_path / "components.db")
    try:
        analyzer = ResidualAnalyzer()
        cases = (
            analyzer.compute(
                _scope(window_w=(190.0, 195.0, 200.0, 205.0, 210.0)), 1000.0, now=T0
            ),
            analyzer.compute(
                _scope(window_w=(190.0, 195.0, 200.0, 205.0, 210.0), device_contributions={"dryer": 1200.0}),
                1000.0,
                now=LATER,
            ),
            analyzer.compute(_scope(device_contributions={"tv": 100.0}), 100.0, now=LATER),
        )
        for residual in cases:
            assert residual.baseline_w is not None
            assert residual.dynamic_w is not None
            persist([residual], db)

        rows = db.residual_history()
        assert len(rows) == 3
        # ``residual_history`` is newest first; the writes were in ``cases``
        # order, so the reversed rows line up one-to-one with their source.
        for row, residual in zip(reversed(rows), cases, strict=True):
            assert row["residual_w"] == pytest.approx(residual.residual_w)
            assert row["baseline_w"] == pytest.approx(residual.baseline_w)
            assert row["dynamic_w"] == pytest.approx(residual.dynamic_w)
            assert row["baseline_w"] + row["dynamic_w"] == pytest.approx(
                row["residual_w"], abs=1e-9
            )
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# The existing engine invariant must not move
# --------------------------------------------------------------------------- #
def _aliased_engine_topology() -> Topology:
    return Topology(
        version=4,
        status=TopologyStatus.CONFIRMED,
        channels=[
            _chan("load_manager_l1", CTType.MAIN, Phase.L1),
            _chan("load_manager_l2", CTType.MAIN, Phase.L2),
            _chan("main_panel_l1", CTType.MAIN, Phase.L1, alias="load_manager_l1"),
            _chan("main_panel_l2", CTType.MAIN, Phase.L2, alias="load_manager_l2"),
            CTChannel(
                ct_id="ct_heater",
                entity_id="sensor.ct_heater",
                ct_type=CTType.BRANCH,
                phase=Phase.L1,
                parent_ct_id="load_manager_l1",
                device_id="heater",
                topology_status=TopologyStatus.CONFIRMED,
            ),
        ],
        devices=[
            DeviceTopology(
                device_id="heater",
                branch_ct_ids=["ct_heater"],
                phase=Phase.L1,
                topology_status=TopologyStatus.CONFIRMED,
            )
        ],
    )


def test_the_reconciliation_invariant_is_unchanged_by_the_new_path():
    topo = _aliased_engine_topology()
    window = ObservationWindow.from_values(
        T0,
        {
            "load_manager_l1": 1000.0,
            "load_manager_l2": 0.0,
            "main_panel_l1": 651.1,
            "main_panel_l2": 0.0,
            "ct_heater": 833.6,
        },
        cadence_s=15.7,
    )
    reconciliation = DisaggregationEngine().infer(
        window, device_states={}, models={}, topology=topo
    ).reconciliation

    analyzer = ResidualAnalyzer(topology=build_graph(topo))
    residual = analyzer.compute(
        _scope(
            scope_id="whole_home",
            ct_id="load_manager_l1",
            device_contributions={"heater": reconciliation.inferred_w},
        ),
        reconciliation.main_power_w,
        now=reconciliation.timestamp,
    )

    # Same number, same sign: the new object is a *view* of the balance.
    assert residual.residual_w == pytest.approx(reconciliation.unknown_residual_w, abs=1e-9)
    assert residual.kind == derived_residual_kind(reconciliation.unknown_residual_w)

    attached = reconciliation.model_copy(update={"residual": residual})
    assert isinstance(attached, Reconciliation)
    # The two existing identities, exactly, after the new object is attached.
    assert attached.unmonitored_w + attached.meter_disagreement_w == pytest.approx(
        attached.unknown_residual_w, abs=1e-9
    )
    assert (
        attached.direct_measured_w
        + attached.inferred_w
        + attached.model_estimated_w
        + attached.configured_prior_w
        + attached.unknown_residual_w
        == pytest.approx(attached.main_power_w, abs=1e-9)
    )
    # Nothing was redistributed into the device rows.
    assert attached.direct_measured_w == reconciliation.direct_measured_w
    assert attached.meter_disagreement_w == reconciliation.meter_disagreement_w


# --------------------------------------------------------------------------- #
# The no-clamp rule, proven by mutation
# --------------------------------------------------------------------------- #
def test_a_clamping_implementation_is_detected(monkeypatch):
    """Mutation: ``max(0.0, ...)`` must break the suite's own assertion.

    ``_signed_residual`` is the only place the difference is formed.  Patching
    it with a *clamping* implementation is exactly the mutation "someone added
    ``max(0.0, ...)``", and the assertion below is the one ``test_f5`` relies
    on -- so the mutation is caught by the suite, not by a bespoke check.
    """
    from wattmosaic_engine.models import residual_power as module

    assert "_signed_residual" in inspect.getsource(module)
    real = module._signed_residual
    monkeypatch.setattr(
        module, "_signed_residual", lambda circuit, known: max(0.0, real(circuit, known))
    )

    scope = _scope(device_contributions={"dryer": 700.0, "oven": 300.0})
    mutated = ResidualAnalyzer().compute(scope, 800.0)

    assert mutated.residual_w == 0.0  # the mutation took effect
    with pytest.raises(AssertionError):
        _assert_negative_is_preserved(mutated, -200.0)


def test_the_error_types_form_one_typed_family():
    assert issubclass(ResidualDecompositionConflict, ResidualNotComputed)
    assert issubclass(IncompatibleBasisError, ResidualNotComputed)
    assert "clamp" in (ResidualAnalyzer.compute.__doc__ or "").lower()


def test_the_scope_validates_its_multiplicity():
    with pytest.raises(ValueError):
        ResidualScope(scope_id="x", ct_id="c", multiplicity=0)
    with pytest.raises(ValueError):
        # multiplicity > 1 must name the CTs, so a duplicate is described.
        ResidualScope(scope_id="x", ct_id="c", multiplicity=2)
    with pytest.raises(ValueError):
        ResidualScope(scope_id="x", ct_id="c", multiplicity=2, ct_ids=("c",))


def test_median_baseline_is_used_not_a_mean():
    # A single 5000 W spike in six samples: the mean is 850, the median is 120.
    residual = ResidualAnalyzer().compute(
        _scope(window_w=(120.0, 120.0, 120.0, 120.0, 120.0, 5000.0)), 900.0
    )
    assert residual.baseline_w == pytest.approx(120.0)
    assert not math.isclose(residual.baseline_w or 0.0, 850.0)


def test_decomposition_of_an_empty_history_invents_nothing():
    decomposition = ResidualAnalyzer().decompose([], {"dryer": 100.0})
    assert decomposition.explained_by_device_w == {}
    assert decomposition.resolved is False
    # Not measured is not "measured as 0 W": the fields are absent and the
    # reason says why, so nothing downstream can publish a zero residual.
    assert decomposition.before_w is None
    assert decomposition.unexplained_w is None
    assert decomposition.after_w is None
    assert decomposition.measured is False
    assert decomposition.as_dict()["before_w"] is None
    assert decomposition.reason_code == "no_residual_history"
    with pytest.raises(ResidualNotComputed):
        decomposition.to_residual_power()


def test_trend_carries_the_basis_and_quality_of_the_history():
    trend = ResidualAnalyzer().trend(_history([10.0, 20.0]))
    assert isinstance(trend, ResidualTrend)
    assert trend.basis == MeasurementBasis.CT_MEASURED
    payload = trend.as_dict()
    assert payload["current_w"] == pytest.approx(20.0)
    assert payload["baseline_w"] == pytest.approx(15.0)
    assert payload["dynamic_w"] == pytest.approx(5.0)
    assert payload["kind"] == ResidualKind.POSITIVE.value


# --------------------------------------------------------------------------- #
# Refusals that are not about bases
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf")])
def test_a_non_finite_circuit_power_is_refused(bad):
    attempt = ResidualAnalyzer().try_compute(_scope(), bad)
    assert attempt.refused is True
    assert attempt.reason_code == "non_finite_circuit_power"
    assert attempt.residual is None
    assert attempt.as_dict()["refused"] is True


def test_a_non_numeric_circuit_power_is_refused_not_coerced():
    attempt = ResidualAnalyzer().try_compute(_scope(), None)  # type: ignore[arg-type]
    assert attempt.refused is True
    assert attempt.reason_code == "non_finite_circuit_power"
    assert attempt.evidence["circuit_power_w"] == "None"


def test_an_invalid_conversion_is_refused():
    scope = _scope(
        device_contributions={"leg": 150.0},
        contribution_bases={"leg": MeasurementBasis.FACTOR_ADJUSTED},
        conversions={"leg": 0.0},
    )
    attempt = ResidualAnalyzer().try_compute(scope, 500.0)
    assert attempt.refused is True
    assert attempt.reason_code == "invalid_conversion"


def test_a_contribution_that_is_not_a_number_is_excluded_not_subtracted():
    attempt = ResidualAnalyzer().try_compute(
        _scope(device_contributions={"bad": float("nan"), "good": 100.0}), 500.0
    )
    assert attempt.refused is False
    assert attempt.evidence["excluded_contributions"] == {"bad": "not_finite"}
    assert attempt.residual is not None
    assert attempt.residual.residual_w == pytest.approx(400.0)


def test_the_scope_requires_identity_fields():
    with pytest.raises(ValueError):
        ResidualScope(scope_id="", ct_id="c")
    with pytest.raises(ValueError):
        ResidualScope(scope_id="s", ct_id="")


def test_a_bare_residual_model_is_a_valid_history_entry():
    first = ResidualAnalyzer().compute(_scope(), 100.0)
    second = ResidualAnalyzer().compute(_scope(), 300.0)
    trend = ResidualAnalyzer().trend([first, second])
    assert trend.sample_count == 2
    # Supplied order is preserved; the model carries no timestamp of its own.
    assert [value for _, value in trend.curve] == [100.0, 300.0]
    assert trend.current_w == pytest.approx(300.0)


def test_persist_accepts_a_single_residual(tmp_path):
    db = Database(tmp_path / "one.db")
    try:
        assert persist(ResidualAnalyzer().compute(_scope(), 50.0), db) == 1
        assert len(db.residual_history()) == 1
    finally:
        db.close()


def test_require_resolved_refuses_a_partial_explanation():
    decomposition = ResidualAnalyzer().decompose(_history([400.0]), {"dryer": 100.0})
    assert decomposition.resolved is False
    assert decomposition.explained_total_w == pytest.approx(100.0)
    with pytest.raises(ResidualDecompositionConflict):
        decomposition.require_resolved()


def test_a_numeric_string_contribution_is_not_coerced():
    attempt = ResidualAnalyzer().try_compute(
        _scope(device_contributions={"dryer": "250"}), 1000.0  # type: ignore[dict-item]
    )
    assert attempt.refused is False
    assert attempt.evidence["excluded_contributions"] == {"dryer": "not_a_number"}
    assert attempt.residual is not None
    assert attempt.residual.residual_w == pytest.approx(1000.0)


def test_decompose_excludes_a_non_numeric_candidate():
    decomposition = ResidualAnalyzer().decompose(
        _history([300.0]), {"bad": "lots", "good": 100.0}  # type: ignore[dict-item]
    )
    assert decomposition.explained_by_device_w == {"good": pytest.approx(100.0)}
    assert any(item["reason"] == "not_a_number" for item in decomposition.evidence["excluded"])


def test_trend_counts_the_signs_and_sign_changes():
    trend = ResidualAnalyzer().trend(_history([100.0, -50.0, -20.0, 0.0]))
    assert trend.evidence["cause_counts"] == {"positive": 1, "negative": 2, "near_zero": 1}
    # positive -> negative, then negative -> exactly zero: two kind transitions.
    assert trend.evidence["sign_changes"] == 2
    assert trend.current_w == 0.0
    assert trend.kind == ResidualKind.NEAR_ZERO


def test_the_trend_dynamic_component_can_be_negative():
    trend = ResidualAnalyzer().trend(_history([400.0, 100.0]))
    # Typical 250, current 100: the moving part is -150 W, not clamped.
    assert trend.typical_w == pytest.approx(250.0)
    assert trend.dynamic_w == pytest.approx(-150.0)
    assert trend.baseline_w + trend.dynamic_w == pytest.approx(trend.current_w)


def test_a_contribution_basis_of_unknown_inherits_the_scope_basis():
    scope = _scope(
        device_contributions={"dryer": 100.0},
        contribution_bases={"dryer": MeasurementBasis.UNKNOWN},
    )
    residual = ResidualAnalyzer().compute(scope, 400.0)
    assert residual.residual_w == pytest.approx(300.0)


def test_a_conflicting_decomposition_documents_itself():
    decomposition = ResidualAnalyzer().decompose(_history([300.0]), {"a": 200.0, "b": 200.0})
    assert decomposition.conflict is True
    text = decomposition.explanation
    assert any(item.startswith("residual_decomposition_conflict:") for item in text)
    assert any(item == "residual_before:300.0W" for item in text)
    assert decomposition.as_dict()["conflict"] is True


def test_a_resolved_decomposition_documents_itself():
    decomposition = ResidualAnalyzer().decompose(_history([100.0]), {"dryer": 100.0})
    assert decomposition.resolved is True
    assert "residual_resolved" in decomposition.explanation
    carried = decomposition.require_resolved()
    assert carried.residual_w == 0.0
    assert carried.resolved is True


def test_history_rows_accept_iso_strings_and_naive_datetimes():
    rows = [
        {"timestamp": "2026-11-07T13:20:00Z", "residual_w": 100.0, "basis": "ct_measured"},
        {"timestamp": datetime(2026, 11, 7, 13, 35), "residual_w": 300.0, "basis": "ct_measured"},
    ]
    trend = ResidualAnalyzer().trend(rows)
    assert trend.sample_count == 2
    # Both timestamps are normalised to UTC-aware datetimes and sorted.
    assert all(instant.tzinfo is not None for instant, _ in trend.curve)
    assert trend.current_w == pytest.approx(300.0)


def test_without_a_topology_no_ancestor_claim_is_made():
    analyzer = ResidualAnalyzer()
    scope = _scope(
        device_contributions={"kitchen_total": 800.0, "fridge": 120.0},
        contribution_cts={"kitchen_total": "branch_kitchen", "fridge": "ct_fridge"},
    )
    # Nothing declares the nesting, so nothing is dropped: an analyzer without
    # a topology cannot see the enclosure and says so rather than guessing.
    residual = analyzer.compute(scope, 1000.0)
    assert residual.residual_w == pytest.approx(80.0)
    assert analyzer.last_evidence["excluded_contributions"] == {}
    assert analyzer.last_evidence["canonicalization"] == "declared_only"


def test_a_scope_declaring_a_channel_outside_its_ct_list_is_recorded():
    graph = _aliased_graph()
    # An inconsistent declaration: the scope says ``feed_l2`` but its CT list
    # names ``feed_l1``.  The CT list is what was actually assembled, so that is
    # what is computed -- and the disagreement is recorded, not hidden.
    scope = _scope(ct_id="feed_l2", ct_ids=("feed_l1",))
    analyzer = ResidualAnalyzer(topology=graph)
    residual = analyzer.compute(scope, 100.0)
    assert residual.residual_w == pytest.approx(100.0)
    assert analyzer.last_evidence["canonical_channel_id"] == "feed_l1"
    assert analyzer.last_evidence["declared_channel_canonical_id"] == "feed_l2"


def test_an_alias_declared_as_the_scope_channel_matches_the_ct_list():
    graph = _aliased_graph()
    scope = _scope(ct_id="panel_l1", ct_ids=("feed_l1", "panel_l1"), multiplicity=2)
    analyzer = ResidualAnalyzer(topology=graph)
    analyzer.compute(scope, 100.0)
    # No disagreement: the alias names the same conductor as the CT list.
    assert "declared_channel_canonical_id" not in analyzer.last_evidence


def test_a_numpy_scalar_is_a_valid_watt_value():
    np = pytest.importorskip("numpy")
    residual = ResidualAnalyzer().compute(_scope(), np.float64(250.0))
    assert residual.residual_w == pytest.approx(250.0)
    assert isinstance(residual.residual_w, float)


def test_a_decimal_watt_value_is_accepted_through_its_float_protocol():
    from decimal import Decimal

    residual = ResidualAnalyzer().compute(_scope(), Decimal("250.5"))
    assert residual.residual_w == pytest.approx(250.5)


def test_an_overflowing_sum_is_refused_rather_than_returned():
    scope = _scope(device_contributions={"a": 1.7e308, "b": 1.7e308})
    attempt = ResidualAnalyzer().try_compute(scope, -1.7e308)
    assert attempt.refused is True
    assert attempt.reason_code == "non_finite_residual"
    assert attempt.residual is None


def test_a_history_row_without_a_timestamp_is_placed_first():
    rows = [
        {"residual_w": 100.0},
        {"timestamp": T0.isoformat(), "residual_w": 300.0},
    ]
    trend = ResidualAnalyzer().trend(rows)
    assert trend.sample_count == 2
    assert trend.current_w == pytest.approx(300.0)
    assert [value for _, value in trend.curve] == [100.0, 300.0]
