"""The residual and the meter spread are different kinds of number.

``unknown_residual_w`` is ``main_power_w - accounted``: a **bounded accounting
remainder** for the one canonical meter.  ``meter_spread_w`` is
``|canonical total - aliased total|``: an **independent, unbounded** property of
two instruments clamped on the same conductors.

Contract 1.4.0 tried to add the second into the first::

    meter_disagreement_w = |canonical - alias|        # unbounded
    unmonitored_w        = max(0, residual - md)      # then this cannot add

On the reference install the median spread is ~349 W while the median residual is
~166 W, so ``unmonitored_w`` collapsed to 0 and
``unmonitored_w + meter_disagreement_w`` overshot the residual by 233-660 W on
every cycle.  Contract 1.5.0 separates the concepts:

* ``meter_spread_w`` -- the absolute spread, unbounded, diagnostic only;
* ``meter_disagreement_w`` -- the spread's *contribution to the residual*,
  clipped into ``[0, unknown_residual_w]``;
* ``unmonitored_w`` = ``unknown_residual_w - meter_disagreement_w``;
* ``meter_spread_exceeds_residual`` -- True when the whole residual is
  explainable by the instruments, so the residual is *not* evidence of
  unmonitored load.

The rules these tests pin:

* ``unknown_residual_w`` keeps its exact meaning and
  ``accounted + unknown_residual == main_power_w`` still holds;
* ``unmonitored_w + meter_disagreement_w == unknown_residual_w`` **exactly**,
  in every case including a spread larger than the residual;
* no power value changes -- the split is a decomposition, not an estimate;
* no alias, a stale meter, or a half-measured meter point yields
  ``meter_spread_w == meter_disagreement_w == 0.0`` and no claim.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from wattmosaic_engine.contracts import (
    CTChannel,
    CTType,
    DeviceTopology,
    Phase,
    QualityStatus,
    Reconciliation,
    Topology,
    TopologyStatus,
)
from wattmosaic_engine.disaggregation import DisaggregationEngine
from wattmosaic_engine.electrical import ObservationWindow
from wattmosaic_engine.electrical.main_power import meter_spread_w
from wattmosaic_engine.electrical.topology import build_graph

T0 = datetime(2026, 11, 7, 13, 20, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Topology builders.  The live install's shape: one canonical service feed and
# a second panel clamped on the same conductors, declared as an alias.
# --------------------------------------------------------------------------- #
def _chan(
    cid: str,
    ct_type: CTType,
    phase: Phase,
    *,
    parent: str | None = None,
    device: str | None = None,
    alias: str | None = None,
    enabled: bool = True,
) -> CTChannel:
    return CTChannel(
        ct_id=cid,
        entity_id=f"sensor.{cid}",
        ct_type=ct_type,
        phase=phase,
        parent_ct_id=parent,
        device_id=device,
        alias_of_ct_id=alias,
        enabled=enabled,
        topology_status=TopologyStatus.CONFIRMED,
    )


def _aliased_topology() -> Topology:
    """Canonical ``load_manager_*`` feeds plus an aliased ``main_panel_*`` pair."""
    return Topology(
        version=4,
        status=TopologyStatus.CONFIRMED,
        channels=[
            _chan("load_manager_l1", CTType.MAIN, Phase.L1),
            _chan("load_manager_l2", CTType.MAIN, Phase.L2),
            _chan("main_panel_l1", CTType.MAIN, Phase.L1, alias="load_manager_l1"),
            _chan("main_panel_l2", CTType.MAIN, Phase.L2, alias="load_manager_l2"),
            _chan(
                "ct_heater",
                CTType.DEDICATED_DEVICE,
                Phase.L1,
                parent="load_manager_l1",
                device="heater",
            ),
        ],
        devices=[
            DeviceTopology(
                device_id="heater",
                dedicated_ct_ids=["ct_heater"],
                phase=Phase.L1,
                topology_status=TopologyStatus.CONFIRMED,
            )
        ],
    )


def _plain_topology() -> Topology:
    """The same house with no second meter."""
    topo = _aliased_topology()
    return topo.model_copy(
        update={"channels": [c for c in topo.channels if c.alias_of_ct_id is None]}
    )


def _window(values, *, ages=None, qualities=None) -> ObservationWindow:
    return ObservationWindow.from_values(
        T0,
        values,
        ages=ages or {},
        qualities=qualities or {},
        cadence_s=15.7,
    )


def _infer(topo: Topology, win: ObservationWindow, engine=None):
    return (engine or DisaggregationEngine()).infer(
        win,
        device_states={},
        models={},
        topology=topo,
    )


def _accounted(rec: Reconciliation) -> float:
    return (
        rec.direct_measured_w
        + rec.inferred_w
        + rec.model_estimated_w
        + rec.configured_prior_w
    )


def _assert_additive(rec: Reconciliation) -> None:
    """Both identities, exactly, in every case."""
    assert rec.unmonitored_w + rec.meter_disagreement_w == pytest.approx(
        rec.unknown_residual_w, abs=1e-9
    )
    assert _accounted(rec) + rec.unknown_residual_w == pytest.approx(
        rec.main_power_w, abs=1e-9
    )


# --------------------------------------------------------------------------- #
# The live-shaped case: the spread is LARGER than the whole residual
# --------------------------------------------------------------------------- #
def test_live_shape_spread_larger_than_residual():
    """THE production case: residual 166.4 W, spread 348.9 W.

    Before 1.5.0 this produced ``unmonitored = max(0, 166.4 - 348.9) = 0`` and
    ``0 + 348.9 != 166.4`` -- the residual is smaller than the disagreement it
    was supposed to contain, so the identity failed by 182.5 W.

    After 1.5.0 the *contribution* is clipped to the residual (the whole
    residual is explainable by the instruments), ``unmonitored`` is 0, the two
    parts add exactly, and the unbounded spread is still reported honestly.
    """
    topo = _aliased_topology()
    # canonical 1000.0 W, accounted 833.6 W -> residual 166.4 W;
    # alias 651.1 W -> spread 348.9 W.
    win = _window(
        {
            "load_manager_l1": 1000.0,
            "load_manager_l2": 0.0,
            "main_panel_l1": 651.1,
            "main_panel_l2": 0.0,
            "ct_heater": 833.6,
        }
    )
    rec = _infer(topo, win).reconciliation

    assert rec.main_power_w == pytest.approx(1000.0)
    assert rec.unknown_residual_w == pytest.approx(166.4, abs=0.05)
    assert rec.meter_spread_w == pytest.approx(348.9, abs=0.05)
    # The contribution is the whole residual, not the spread.
    assert rec.meter_disagreement_w == pytest.approx(166.4, abs=0.05)
    assert rec.unmonitored_w == pytest.approx(0.0, abs=1e-9)
    assert rec.meter_spread_exceeds_residual is True
    _assert_additive(rec)

    notes = " ".join(rec.explanation)
    assert "meter_spread_exceeds_residual" in notes
    assert "residual_includes_meter_disagreement" in notes


def test_live_shape_diagnostics_name_the_spread_and_the_clip():
    topo = _aliased_topology()
    win = _window(
        {
            "load_manager_l1": 1000.0,
            "load_manager_l2": 0.0,
            "main_panel_l1": 651.1,
            "main_panel_l2": 0.0,
            "ct_heater": 833.6,
        }
    )
    batch = _infer(topo, win)
    diag = batch.diagnostics["meter_disagreement"]
    assert diag["spread_w"] == pytest.approx(348.9, abs=0.05)
    assert diag["disagreement_w"] == pytest.approx(166.4, abs=0.05)
    assert diag["exceeds_residual"] is True
    assert diag["channels"] == ["main_panel_l1", "main_panel_l2"]


# --------------------------------------------------------------------------- #
# The normal case: the spread fits inside the residual
# --------------------------------------------------------------------------- #
def test_normal_case_spread_fits_inside_the_residual():
    """Canonical 1000 W, alias 900 W, accounted 700 W.

    * ``unknown_residual == 300`` (unchanged total, still the whole gap);
    * ``meter_spread_w == 100`` (the raw instrument spread);
    * ``meter_disagreement_w == 100`` (all of it fits inside the residual);
    * ``unmonitored == 200`` (not attributable to any device);
    * the parts add exactly and the flag is False.
    """
    topo = _aliased_topology()
    win = _window(
        {
            "load_manager_l1": 1000.0,
            "load_manager_l2": 0.0,
            "main_panel_l1": 900.0,
            "main_panel_l2": 0.0,
            "ct_heater": 700.0,
        }
    )
    rec = _infer(topo, win).reconciliation

    assert rec.main_power_w == pytest.approx(1000.0)
    assert rec.direct_measured_w == pytest.approx(700.0)
    assert rec.unknown_residual_w == pytest.approx(300.0)
    assert rec.meter_spread_w == pytest.approx(100.0)
    assert rec.meter_disagreement_w == pytest.approx(100.0)
    assert rec.unmonitored_w == pytest.approx(200.0)
    assert rec.meter_spread_exceeds_residual is False
    _assert_additive(rec)


# --------------------------------------------------------------------------- #
# Zero residual: a total gap of zero cannot contain anything
# --------------------------------------------------------------------------- #
def test_zero_residual_contains_no_disagreement_but_still_reports_the_spread():
    """Residual 0 W, spread 250 W.

    The spread is real and reported; its contribution to a zero residual is 0,
    and the flag says the residual cannot possibly explain the disagreement.
    """
    topo = _aliased_topology()
    win = _window(
        {
            "load_manager_l1": 1000.0,
            "load_manager_l2": 0.0,
            "main_panel_l1": 750.0,
            "main_panel_l2": 0.0,
            "ct_heater": 1000.0,
        }
    )
    rec = _infer(topo, win).reconciliation

    assert rec.unknown_residual_w == pytest.approx(0.0, abs=1e-9)
    assert rec.meter_spread_w == pytest.approx(250.0)
    assert rec.meter_disagreement_w == pytest.approx(0.0, abs=1e-9)
    assert rec.unmonitored_w == pytest.approx(0.0, abs=1e-9)
    assert rec.meter_spread_exceeds_residual is True
    _assert_additive(rec)


def test_split_does_not_change_any_power_value():
    """Installing the split must not move a single reported watt."""
    values = {
        "load_manager_l1": 1000.0,
        "load_manager_l2": 0.0,
        "main_panel_l1": 900.0,
        "main_panel_l2": 0.0,
        "ct_heater": 700.0,
    }
    topo = _aliased_topology()
    rec = _infer(topo, _window(values)).reconciliation
    # The device value is the dedicated CT reading, unchanged.
    batch = _infer(topo, _window(values))
    assert batch.get("heater").power_w == pytest.approx(700.0)
    # And the total is the canonical feed, not the alias and not their sum.
    assert rec.main_power_w == pytest.approx(1000.0)


# --------------------------------------------------------------------------- #
# Never guess: no alias, stale meter, half a meter point, bad quality
# --------------------------------------------------------------------------- #
def test_no_alias_means_no_spread_and_no_claim():
    topo = _plain_topology()
    win = _window(
        {
            "load_manager_l1": 1000.0,
            "load_manager_l2": 0.0,
            "ct_heater": 700.0,
        }
    )
    batch = _infer(topo, win)
    rec = batch.reconciliation
    assert rec.meter_spread_w == 0.0
    assert rec.meter_disagreement_w == 0.0
    assert rec.meter_spread_exceeds_residual is False
    assert rec.unmonitored_w == pytest.approx(rec.unknown_residual_w)
    assert rec.unmonitored_w == pytest.approx(300.0)
    assert "meter_disagreement" not in batch.diagnostics
    _assert_additive(rec)


def test_a_stale_alias_leg_claims_no_spread():
    """A leftover alias reading is not a second opinion about this instant."""
    topo = _aliased_topology()
    win = _window(
        {
            "load_manager_l1": 1000.0,
            "load_manager_l2": 0.0,
            "main_panel_l1": 900.0,
            "main_panel_l2": 0.0,
            "ct_heater": 700.0,
        },
        ages={"main_panel_l1": 400.0},
    )
    rec = _infer(topo, win).reconciliation
    assert rec.meter_spread_w == 0.0
    assert rec.meter_disagreement_w == 0.0
    assert rec.meter_spread_exceeds_residual is False
    assert rec.unmonitored_w == pytest.approx(rec.unknown_residual_w)
    _assert_additive(rec)


def test_half_a_meter_point_is_not_a_second_opinion():
    """The alias L1 alone is one leg, not the other meter's total."""
    topo = _aliased_topology()
    win = _window(
        {
            "load_manager_l1": 1000.0,
            "load_manager_l2": 0.0,
            "main_panel_l1": 900.0,
            "main_panel_l2": None,
            "ct_heater": 700.0,
        }
    )
    rec = _infer(topo, win).reconciliation
    assert rec.meter_spread_w == 0.0
    assert rec.meter_disagreement_w == 0.0
    assert rec.meter_spread_exceeds_residual is False
    assert rec.unmonitored_w == pytest.approx(rec.unknown_residual_w)
    _assert_additive(rec)


def test_a_bad_quality_alias_claims_no_spread():
    topo = _aliased_topology()
    win = _window(
        {
            "load_manager_l1": 1000.0,
            "load_manager_l2": 0.0,
            "main_panel_l1": 900.0,
            "main_panel_l2": 0.0,
            "ct_heater": 700.0,
        },
        qualities={"main_panel_l2": QualityStatus.BAD},
    )
    rec = _infer(topo, win).reconciliation
    assert rec.meter_spread_w == 0.0
    assert rec.meter_disagreement_w == 0.0
    assert rec.meter_spread_exceeds_residual is False
    _assert_additive(rec)


def test_an_alias_that_shadows_only_part_of_the_feed_is_not_compared():
    """A second meter on some conductors is not a check on the whole total.

    Comparing its sum to the home total would report the conductors it does not
    watch as "meter disagreement", which is exactly the conflation this split
    exists to remove.
    """
    topo = _aliased_topology().model_copy(
        update={
            "channels": [
                _chan("load_manager_l1", CTType.MAIN, Phase.L1),
                _chan("load_manager_l2", CTType.MAIN, Phase.L2),
                _chan("main_panel_l1", CTType.MAIN, Phase.L1, alias="load_manager_l1"),
            ]
        }
    )
    win = _window(
        {
            "load_manager_l1": 1000.0,
            "load_manager_l2": 500.0,
            "main_panel_l1": 900.0,
        }
    )
    spread, cts = meter_spread_w(
        build_graph(topo), win.readings, canonical_total_w=1500.0
    )
    assert spread == 0.0
    assert cts == ()


# --------------------------------------------------------------------------- #
# A spread above the canonical feed is still a spread
# --------------------------------------------------------------------------- #
def test_the_alias_may_read_above_the_canonical_feed():
    """The spread is absolute, so over-reading is a disagreement too.

    Canonical 1000 W, alias 1400 W -> spread 400 W; residual is 200 W, so the
    contribution is clipped to 200 W and the flag is True.  The total is never
    changed and the identities hold.
    """
    topo = _aliased_topology()
    win = _window(
        {
            "load_manager_l1": 1000.0,
            "load_manager_l2": 0.0,
            "main_panel_l1": 1400.0,
            "main_panel_l2": 0.0,
            "ct_heater": 800.0,
        }
    )
    rec = _infer(topo, win).reconciliation

    assert rec.unknown_residual_w == pytest.approx(200.0)
    assert rec.meter_spread_w == pytest.approx(400.0)
    assert rec.meter_disagreement_w == pytest.approx(200.0)
    assert rec.unmonitored_w == pytest.approx(0.0, abs=1e-9)
    assert rec.meter_spread_exceeds_residual is True
    _assert_additive(rec)


def test_a_negative_residual_contains_no_disagreement_either():
    """Accounted power above the measured main is a sample-vintage artefact.

    A negative remainder cannot contain a positive contribution, so the clipped
    contribution is 0 and ``unmonitored_w`` keeps the (negative) residual
    unchanged -- never clamped, because hiding it would hide a data-quality
    problem.  The spread is still reported honestly.
    """
    topo = _aliased_topology()
    win = _window(
        {
            "load_manager_l1": 1000.0,
            "load_manager_l2": 0.0,
            "main_panel_l1": 900.0,
            "main_panel_l2": 0.0,
            "ct_heater": 1100.0,
        }
    )
    rec = _infer(topo, win).reconciliation

    assert rec.unknown_residual_w == pytest.approx(-100.0)
    assert rec.meter_spread_w == pytest.approx(100.0)
    assert rec.meter_disagreement_w == pytest.approx(0.0, abs=1e-9)
    assert rec.unmonitored_w == pytest.approx(-100.0)
    assert rec.meter_spread_exceeds_residual is True
    _assert_additive(rec)


# --------------------------------------------------------------------------- #
# The explanation says which quantity is which
# --------------------------------------------------------------------------- #
def test_explanation_names_the_split():
    topo = _aliased_topology()
    win = _window(
        {
            "load_manager_l1": 1000.0,
            "load_manager_l2": 0.0,
            "main_panel_l1": 900.0,
            "main_panel_l2": 0.0,
            "ct_heater": 700.0,
        }
    )
    rec = _infer(topo, win).reconciliation
    notes = " ".join(rec.explanation)
    assert "residual_includes_meter_disagreement" in notes
    assert "unmonitored_residual_excludes_meter_disagreement" in notes
    # A spread inside the residual is not an alarm.
    assert "meter_spread_exceeds_residual" not in notes


def test_a_clean_residual_is_not_annotated():
    """No alias means no split note: the residual is just the residual."""
    topo = _plain_topology()
    win = _window(
        {"load_manager_l1": 1000.0, "load_manager_l2": 0.0, "ct_heater": 700.0}
    )
    batch = _infer(topo, win)
    assert not any(
        "meter_disagreement" in note for note in batch.reconciliation.explanation
    )
    assert not any(
        "meter_spread" in note for note in batch.reconciliation.explanation
    )
    assert "meter_disagreement" not in batch.diagnostics


# --------------------------------------------------------------------------- #
# Contract shape
# --------------------------------------------------------------------------- #
def test_reconciliation_fields_are_additive_with_zero_defaults():
    rec = Reconciliation()
    assert rec.unmonitored_w == 0.0
    assert rec.meter_disagreement_w == 0.0
    assert rec.meter_spread_w == 0.0
    assert rec.meter_spread_exceeds_residual is False
    # Old readers construct without them and get a valid object.
    assert rec.unknown_residual_w == 0.0


def test_contract_version_is_1_6_0():
    from wattmosaic_engine import contracts

    assert contracts.CONTRACT_VERSION == "1.6.0"
