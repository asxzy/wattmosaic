"""Reusable, documented synthetic fixtures for WattMosaic tests.

``tests.fixtures`` is the stable surface other test agents import.  It is a thin,
typed wrapper over ``tools.simulator`` so a consumer never has to know how a
stratum is assembled:

    from tests.fixtures import LabHouse, build_scenario, hot_tub

    house = build_scenario("factor2", seed=42)
    house.observations()   # what an algorithm may see
    house.ground_truth()   # what a test asserts against

Nothing here mutates global state, reads the clock or touches the network.
"""

from tools.simulator.expectations import ExpectedInterpretation
from tools.simulator.factors import FactorEpoch, FactorOwner, FactorTimeline, RefossFactorChannel
from tools.simulator.frames import (
    assert_no_ground_truth_keys,
    find_ground_truth_keys,
    observation_view,
    truth_view,
)
from tools.simulator.hot_tub import (
    HOT_TUB_BUILDERS,
    HOT_TUB_VARIANTS,
    HotTubFixture,
    hot_tub,
    hot_tub_list,
)
from tools.simulator.labhouse import (
    STRATA,
    LabHouse,
    StratumPlan,
    available_strata,
    build_scenario,
    validate_strata,
)

__all__ = [
    "HOT_TUB_BUILDERS",
    "HOT_TUB_VARIANTS",
    "STRATA",
    "ExpectedInterpretation",
    "FactorEpoch",
    "FactorOwner",
    "FactorTimeline",
    "HotTubFixture",
    "LabHouse",
    "RefossFactorChannel",
    "StratumPlan",
    "assert_no_ground_truth_keys",
    "available_strata",
    "build_scenario",
    "find_ground_truth_keys",
    "hot_tub",
    "hot_tub_list",
    "observation_view",
    "truth_view",
    "validate_strata",
]
