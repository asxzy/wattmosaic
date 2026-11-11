"""The residual decomposed into meter disagreement and unmonitored load.

``unknown_residual_w`` is ``main_power_w - accounted``.  That single number
mixes two physically different quantities when the home is metered twice on the
same conductors: two instruments disagreeing about one wire, and load that no CT
observes.  On the reference install the aliased Refoss main panel reads ~8.9 %
below the canonical load manager, so of a ~600 W residual roughly 166 W is
instrument disagreement.

Contract 1.4.0 adds two additive fields.  The rules these tests pin:

* ``unknown_residual_w`` keeps its exact meaning and
  ``accounted + unknown_residual == main_power_w`` still holds;
* ``unmonitored_w + meter_disagreement_w == unknown_residual_w`` for a
  non-negative residual (``unmonitored_w`` is clamped at 0);
* no power value changes -- the split is a decomposition, not an estimate;
* no alias, a stale meter, or a half-measured meter point yields
  ``meter_disagreement_w == 0`` and no claim.
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
from wattmosaic_engine.electrical.main_power import meter_disagreement_w
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


# --------------------------------------------------------------------------- #
# The required arithmetic, on a real-shaped input
# --------------------------------------------------------------------------- #
def test_split_residual_on_a_real_shaped_house():
    """Canonical 1000 W, alias 900 W, accounted 700 W.

    * ``unknown_residual == 300`` (unchanged total, still the whole gap);
    * ``meter_disagreement == 100`` (the two meters disagree by exactly that);
    * ``unmonitored == 200`` (what is left after the instruments are compared);
    * the three reconcile exactly.
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
    assert rec.meter_disagreement_w == pytest.approx(100.0)
    assert rec.unmonitored_w == pytest.approx(200.0)

    # Backward-compatible balance identity, untouched by the split.
    accounted = (
        rec.direct_measured_w
        + rec.inferred_w
        + rec.model_estimated_w
        + rec.configured_prior_w
    )
    assert accounted + rec.unknown_residual_w == pytest.approx(rec.main_power_w)
    # And the split is additive to the same total.
    assert rec.unmonitored_w + rec.meter_disagreement_w == pytest.approx(
        rec.unknown_residual_w
    )


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


def test_unmonitored_is_clamped_but_the_total_is_not():
    """The alias may read *above* the canonical feed.  The total stays exact.

    A negative ``unmonitored_w`` would be a claim that the instruments explain
    more than the whole residual, which is not a quantity that exists; it is
    clamped at 0, and the total still satisfies both identities.
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
    assert rec.meter_disagreement_w == pytest.approx(400.0)
    assert rec.unmonitored_w == 0.0
    accounted = rec.direct_measured_w + rec.inferred_w + rec.model_estimated_w
    assert accounted + rec.unknown_residual_w == pytest.approx(rec.main_power_w)


# --------------------------------------------------------------------------- #
# Never guess: no alias, stale meter, half a meter point
# --------------------------------------------------------------------------- #
def test_no_alias_means_no_disagreement():
    topo = _plain_topology()
    win = _window(
        {
            "load_manager_l1": 1000.0,
            "load_manager_l2": 0.0,
            "ct_heater": 700.0,
        }
    )
    rec = _infer(topo, win).reconciliation
    assert rec.meter_disagreement_w == 0.0
    assert rec.unmonitored_w == pytest.approx(rec.unknown_residual_w)
    assert rec.unmonitored_w == pytest.approx(300.0)


def test_a_stale_alias_leg_claims_no_disagreement():
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
    assert rec.meter_disagreement_w == 0.0
    assert rec.unmonitored_w == pytest.approx(rec.unknown_residual_w)


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
    assert rec.meter_disagreement_w == 0.0
    assert rec.unmonitored_w == pytest.approx(rec.unknown_residual_w)


def test_a_bad_quality_alias_claims_no_disagreement():
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
    assert rec.meter_disagreement_w == 0.0


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
    disagreement, cts = meter_disagreement_w(
        build_graph(topo), win.readings, canonical_total_w=1500.0
    )
    assert disagreement == 0.0
    assert cts == ()


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
    # The diagnostic names the meters it compared.
    batch = _infer(topo, win)
    diag = batch.diagnostics["meter_disagreement"]
    assert diag["disagreement_w"] == pytest.approx(100.0)
    assert diag["channels"] == ["main_panel_l1", "main_panel_l2"]


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
    assert "meter_disagreement" not in batch.diagnostics


# --------------------------------------------------------------------------- #
# Contract shape
# --------------------------------------------------------------------------- #
def test_reconciliation_fields_are_additive_with_zero_defaults():
    rec = Reconciliation()
    assert rec.unmonitored_w == 0.0
    assert rec.meter_disagreement_w == 0.0
    # Old readers construct without them and get a valid object.
    assert rec.unknown_residual_w == 0.0


def test_contract_version_is_1_4_0():
    from wattmosaic_engine import contracts

    assert contracts.CONTRACT_VERSION == "1.4.0"
