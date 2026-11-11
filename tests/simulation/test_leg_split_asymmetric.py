"""Asymmetric 240 V leg splits: what the split coefficient is, and is not.

A 240 V load is measured either by two CTs (one per leg) or by one CT on one leg.

* **Two legs.** The sum of the legs is the device power, whatever the physical
  asymmetry. An asymmetric split (70 % / 30 %) must therefore be summed
  correctly, and the *declared* ``phase_split_l1`` must not change that sum.
  This is the property that matters: a symmetric default must never be used to
  "correct" a pair of genuine leg readings.
* **One leg.** The single CT carries only its leg's share. The engine reports
  what it measures — it does not invent the missing leg — so the declared split
  coefficient is the only thing that can recover the total. Declaring the wrong
  split is a large, systematic error in the same direction as the mistake, and
  the test pins both the right and the wrong coefficient so the coefficient
  cannot silently stop being applied.

Measured ratios on the pinned scenario (``seed=202``, 60 min, 15 s, good):

===========================================  =========  ==========
case                                          median     ratio range
===========================================  =========  ==========
paired legs, physical 0.5/0.5                 1.0000     0.999 - 1.001
paired legs, physical 0.7/0.3                 0.9999     0.999 - 1.001
one branch CT, physical 0.7, declared 0.5     1.4000     1.399 - 1.402
one branch CT, physical 0.7, declared 0.7     1.0000     0.945 - 1.001
one branch CT, physical 0.5, declared 0.7     0.7143     0.659 - 0.715
===========================================  =========  ==========

The ranges exclude the first ON record after a transition (it still carries the
previous, low sample); the tests score only *settled* ON records for that
reason.
"""

from __future__ import annotations

import statistics

import pytest

from tests.simulation.metrics import EngineAdapter
from tools.simulator import HouseholdScenario, SimulatorConfig
from tools.simulator.sensors import QualityLevel
from tools.simulator.topology import CTType, build_real_home_topology

SEED = 202
MINUTES = 60
INTERVAL = 15
DEVICE = "water_heater"
LEG_L1 = "ct_main_water_heater_ct1"
LEG_L2 = "ct_main_water_heater_ct2"
#: An ON sample is only scored once the previous record was also ON, so the
#: transition record (which legitimately still carries the old sample) does not
#: dominate the mean.
ON_THRESHOLD_W = 1000.0


def _paired_topology(split_l1: float):
    """Both legs measured; ``split_l1`` is the physical share on L1."""
    topo = build_real_home_topology()
    for dev in topo.devices:
        if dev.device_id == DEVICE:
            dev.circuits = {LEG_L1: split_l1, LEG_L2: 1.0 - split_l1}
    topo.assign_seed_keys()
    return topo


def _one_leg_topology(split_l1: float):
    """One *branch* CT on the L1 leg, no partner. The second CT does not exist."""
    topo = build_real_home_topology()
    for dev in topo.devices:
        if dev.device_id != DEVICE:
            continue
        dev.circuits = {LEG_L1: split_l1}
        dev.dedicated_ct = False
        dev.metadata.pop("pair", None)
    channels = topo.channel_map()
    channels[LEG_L1].paired_ct_id = None
    channels[LEG_L1].ct_type = CTType.BRANCH
    topo.channels = [c for c in topo.channels if c.ct_id != LEG_L2]
    topo.assign_seed_keys()
    return topo


def _settled_ratios(topology, *, declared_split_l1: float) -> list[float]:
    """``predicted / truth`` on ON records that follow another ON record."""
    from wattmosaic_engine.disaggregation import DisaggregationEngine

    contract = topology.to_contract_topology()
    adapter = EngineAdapter(contract)
    adapter.engine = DisaggregationEngine(phase_split_l1=declared_split_l1)
    records = HouseholdScenario(
        SimulatorConfig(
            seed=SEED, minutes=MINUTES, interval=INTERVAL, quality=QualityLevel.GOOD
        ),
        topology=topology,
    ).records()

    ratios: list[float] = []
    previous_on = False
    for record in records:
        truth = record["ground_truth"]["devices"][DEVICE]["power_w"]
        on = truth > ON_THRESHOLD_W
        if on and previous_on:
            predicted = adapter(record)["devices"].get(DEVICE, {}).get("power_w", 0.0)
            ratios.append(predicted / truth)
        previous_on = on
    return ratios


def _topology_problems(contract) -> list[str]:
    from wattmosaic_engine.electrical.topology import build_graph

    return build_graph(contract).problems


# --------------------------------------------------------------------------- #
# Two legs: the sum must be the device power
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("physical_split_l1", [0.5, 0.7, 0.9])
def test_paired_legs_sum_to_the_device_power(physical_split_l1: float) -> None:
    """An asymmetric pair must be summed, not averaged or halved.

    The declared split is the *default* (0.5) in every case, which is wrong for
    two of the three parametrisations. A correct two-leg sum is unaffected by
    that: each leg is measured, so the total is observed rather than modelled.
    """
    topology = _paired_topology(physical_split_l1)
    contract = topology.to_contract_topology()
    assert _topology_problems(contract) == []

    ratios = _settled_ratios(topology, declared_split_l1=0.5)
    assert len(ratios) >= 20, (
        f"only {len(ratios)} settled ON samples; the scenario no longer cycles "
        "the load often enough for this gate"
    )
    median = statistics.median(ratios)
    assert median == pytest.approx(1.0, abs=0.01), (
        f"physical split {physical_split_l1:.2f}/"
        f"{1 - physical_split_l1:.2f} reported at {median:.4f}x truth with a "
        "0.5 declared split; the two legs are not being summed into the device"
    )


def test_declared_split_does_not_perturb_a_paired_measurement() -> None:
    """Declaring an asymmetric split must not change a two-leg total.

    If it did, the coefficient would be scaling a measurement that is already
    complete — double counting one leg and under-reporting the other.
    """
    topology = _paired_topology(0.5)
    assert _topology_problems(topology.to_contract_topology()) == []
    symmetric = statistics.median(_settled_ratios(topology, declared_split_l1=0.5))
    asymmetric = statistics.median(_settled_ratios(topology, declared_split_l1=0.7))
    assert symmetric == pytest.approx(1.0, abs=0.01)
    assert asymmetric == pytest.approx(symmetric, abs=0.02), (
        f"declaring phase_split_l1=0.7 moved a paired 0.5/0.5 measurement from "
        f"{symmetric:.4f}x to {asymmetric:.4f}x: the split is being applied to "
        "leg readings that are already complete"
    )


# --------------------------------------------------------------------------- #
# One leg: the coefficient is the only way to recover the total
# --------------------------------------------------------------------------- #


def test_single_leg_row_is_solved_through_the_declared_split() -> None:
    """The coefficient enters the model as ``row = split * P_device``.

    So the reported power is ``measured_leg / declared_split``, not the raw leg
    reading. Physical 70 % on L1 with a declared 0.5 therefore reports
    ``0.7 / 0.5 = 1.4x`` — it over-reports by exactly the ratio between the
    declared and physical splits. Asserted explicitly because either behaviour
    ("report the raw leg", "divide by the coefficient") is a defensible design,
    and only one of them can be true. The engine must not silently switch.
    """
    topology = _one_leg_topology(0.7)
    assert _topology_problems(topology.to_contract_topology()) == []
    ratios = _settled_ratios(topology, declared_split_l1=0.5)
    assert len(ratios) >= 20
    assert statistics.median(ratios) == pytest.approx(1.4, abs=0.03), (
        "a single branch CT on a 240 V leg is no longer divided by the declared "
        "split: expected ~1.4x (0.7 measured / 0.5 declared), got "
        f"{statistics.median(ratios):.3f}x"
    )


def test_declared_split_recovers_the_missing_leg() -> None:
    """Declaring the physical split is what makes the one-leg case correct.

    Same measurements, two declarations: 0.5 over-reports by 0.7/0.5 = 1.4x and
    0.7 is exact. Both are asserted, so a regression that ignores the
    coefficient fails here rather than silently producing plausible numbers.
    """
    topology = _one_leg_topology(0.7)
    assert _topology_problems(topology.to_contract_topology()) == []

    wrong = statistics.median(_settled_ratios(topology, declared_split_l1=0.5))
    right = statistics.median(_settled_ratios(topology, declared_split_l1=0.7))

    assert wrong == pytest.approx(0.7 / 0.5, abs=0.03), (
        f"declared 0.5 with a physical 0.7 split reported {wrong:.4f}x; expected "
        "~1.40x. The split coefficient is no longer applied to a single leg."
    )
    assert right == pytest.approx(1.0, abs=0.02), (
        f"declared 0.7 with a physical 0.7 split reported {right:.4f}x; the "
        "declared split is not recovering the unmeasured leg"
    )


@pytest.mark.parametrize(
    ("physical_split_l1", "declared_split_l1"),
    [(0.7, 0.5), (0.5, 0.7)],
)
def test_wrong_split_errs_in_the_direction_of_the_mistake(
    physical_split_l1: float, declared_split_l1: float
) -> None:
    """The error size follows ``physical / declared``; the sign follows the gap."""
    topology = _one_leg_topology(physical_split_l1)
    assert _topology_problems(topology.to_contract_topology()) == []
    expected_ratio = physical_split_l1 / declared_split_l1
    median = statistics.median(_settled_ratios(topology, declared_split_l1=declared_split_l1))
    assert median == pytest.approx(expected_ratio, abs=0.03), (
        f"physical {physical_split_l1:.1f} declared {declared_split_l1:.1f}: "
        f"expected {expected_ratio:.3f}x, measured {median:.3f}x"
    )
