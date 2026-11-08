"""Shared generic fixtures for the WattMosaic test suite.

Scope rules for this file:
* Only generic, side-effect-free fixtures.
* No pytest plugins, hooks that alter global behaviour, or collection changes.
* No imports from files owned by other test agents.

Fixtures provided:
* ``repo_root`` / ``simulator_root`` — paths, and the repo is added to
  ``sys.path`` so ``tools.simulator`` imports work from any test directory.
* ``rng`` — seeded ``random.Random``.
* ``tmp_db_path`` — isolated SQLite path.
* ``sample_topology`` — minimal two-panel topology built from the simulator.
* ``fast_scenario`` — small deterministic ``HouseholdScenario`` (10 min).
* ``household_scenario`` — factory for parameterised scenarios.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

ENGINE_ROOT = REPO_ROOT / "engine"
if ENGINE_ROOT.is_dir() and str(ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(ENGINE_ROOT))


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def simulator_root() -> Path:
    return REPO_ROOT / "tools" / "simulator"


@pytest.fixture()
def rng() -> random.Random:
    """Deterministic RNG; tests must not depend on global random state."""
    return random.Random(20261105)


@pytest.fixture()
def tmp_db_path(tmp_path: Path) -> Path:
    return tmp_path / "wattmosaic_test.db"


@pytest.fixture(scope="session")
def sample_topology():
    """Small, audit-shaped topology shared by read-only topology tests."""
    from tools.simulator.topology import build_real_home_topology

    topo = build_real_home_topology()
    channels = [c for c in topo.channels if c.ct_id in {
        "ct_main_l1", "ct_main_l2", "ct_sub_l1", "ct_sub_l2",
        "ct_sub_network_cabinet", "ct_main_water_heater_ct1", "ct_main_water_heater_ct2",
    }]
    devices = [d for d in topo.devices if d.device_id in {
        "network_cabinet", "water_heater", "unknown_load_l1",
    }]
    from tools.simulator.topology import HouseholdTopology

    # A rename in the simulator must not silently empty this fixture: an empty
    # topology makes the tests that use it pass vacuously.
    assert channels, "sample_topology selected no channels"
    assert devices, "sample_topology selected no devices"

    return HouseholdTopology(channels=channels, devices=devices, notes=list(topo.notes))


@pytest.fixture(scope="session")
def fast_scenario():
    from tools.simulator import HouseholdScenario

    return HouseholdScenario(seed=1234, minutes=10, interval=15)


@pytest.fixture(scope="session")
def household_scenario():
    from tools.simulator import HouseholdScenario

    def _make(seed: int = 42, minutes: float = 20.0, interval: float = 15.0, **kwargs):
        return HouseholdScenario(seed=seed, minutes=minutes, interval=interval, **kwargs)

    return _make
