"""Deterministic synthetic household simulator for WattMosaic.

Public API::

    from tools.simulator import HouseholdScenario
    scenario = HouseholdScenario(seed=7, minutes=120)
    for record in scenario.records():
        ...

The simulator models a *synthetic reference household* — two multi-channel CT
panels, named branch circuits, double-pole CT pairs, a nested sub-feed and a
15.7 s median CT cadence — and emits ground truth generated independently of the
noisy measurements. It describes no real building; see
``data/examples/reference_house/`` and ``tools/simulator/topology.py``.

Reusable fixtures (WP2)
-----------------------
``tools.simulator.labhouse``
    ``LabHouse`` / ``build_scenario(name, seed)``: one builder per scenario
    stratum (dedicated, shared, factor2, noisy_legs, …), each with an
    observation view and a ground-truth view that never mix.
``tools.simulator.hot_tub``
    the mandatory Refoss ×2 Hot Tub regression fixture and its variants.
``tools.simulator.factors``
    ``RefossFactorChannel`` / ``FactorTimeline`` / ``FactorEpoch``: a 240 V
    appliance with one metered leg, a factor history, and a *separate* physical
    truth so "what the CT reports" and "what the appliance draws" can disagree.
``tools.simulator.frames``
    the observation/ground-truth seam plus ``find_ground_truth_keys``, the
    structural leak detector.
"""

from .expectations import ExpectedInterpretation
from .factors import FactorEpoch, FactorOwner, FactorTimeline, RefossFactorChannel
from .frames import (
    assert_no_ground_truth_keys,
    find_ground_truth_keys,
    observation_stream,
    observation_view,
    truth_stream,
    truth_view,
)
from .hot_tub import HOT_TUB_BUILDERS, HOT_TUB_VARIANTS, HotTubFixture, hot_tub, hot_tub_list
from .labhouse import (
    STRATA,
    LabHouse,
    StratumPlan,
    available_strata,
    build_scenario,
    validate_strata,
)
from .scenario import HouseholdScenario, SimulatorConfig
from .topology import (
    REAL_HOME_TOPOLOGY,
    HouseholdTopology,
    SimChannel,
    SimDevice,
    SimDeviceType,
    build_real_home_topology,
)

__all__ = [
    # scenario
    "HouseholdScenario",
    "SimulatorConfig",
    # topology
    "REAL_HOME_TOPOLOGY",
    "HouseholdTopology",
    "SimChannel",
    "SimDevice",
    "SimDeviceType",
    "build_real_home_topology",
    # factor semantics
    "FactorEpoch",
    "FactorOwner",
    "FactorTimeline",
    "RefossFactorChannel",
    # frames / leak detection
    "assert_no_ground_truth_keys",
    "find_ground_truth_keys",
    "observation_stream",
    "observation_view",
    "truth_stream",
    "truth_view",
    # strata
    "STRATA",
    "LabHouse",
    "StratumPlan",
    "available_strata",
    "build_scenario",
    "validate_strata",
    # hot tub fixtures
    "HOT_TUB_BUILDERS",
    "HOT_TUB_VARIANTS",
    "ExpectedInterpretation",
    "HotTubFixture",
    "hot_tub",
    "hot_tub_list",
]
