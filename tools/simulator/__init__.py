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
"""

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
    "HouseholdScenario",
    "SimulatorConfig",
    "REAL_HOME_TOPOLOGY",
    "HouseholdTopology",
    "SimChannel",
    "SimDevice",
    "SimDeviceType",
    "build_real_home_topology",
]
