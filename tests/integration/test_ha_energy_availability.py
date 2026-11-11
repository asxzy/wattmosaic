"""The HA ``energy`` entity must not re-report a frozen total as live.

Measured defect: ``sensor.ensuite_in_floor_heating_energy`` read 2.041 kWh and
kept growing while its power sensor was ``unavailable``.  Two mechanisms
combined:

* the engine kept aligning the CT's last value for up to ``stale_after_s`` and
  integrated the frozen watts (fixed in the aggregator, see
  ``tests/unit/test_energy_quality.py``);
* the HA ``energy`` entity was hard-coded ``available = True``, so it kept
  publishing the held total with no evidence behind it.

``sensor.py`` imports Home Assistant (not installed here), so this module pins
the *decision* through the pure helper it calls, plus the shape of the wiring
that must use it.  ``last_value`` carries the held number so it is never lost.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import types
from pathlib import Path

import pytest

PACKAGE_DIR = Path(__file__).resolve().parents[2] / "custom_components" / "wattmosaic"
PACKAGE_NAME = "wm_energy_availability_under_test"


def _load_package() -> types.ModuleType:
    """Create a package handle without executing ``__init__`` (it imports HA)."""
    if PACKAGE_NAME in sys.modules:
        return sys.modules[PACKAGE_NAME]
    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME,
        PACKAGE_DIR / "__init__.py",
        submodule_search_locations=[str(PACKAGE_DIR)],
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [str(PACKAGE_DIR)]  # type: ignore[attr-defined]
    sys.modules[PACKAGE_NAME] = module
    return module


_load_package()
util = importlib.import_module(f"{PACKAGE_NAME}.util")


class TestEnergyEntityAvailability:
    def test_unavailable_with_no_reading_and_no_history(self) -> None:
        assert (
            util.energy_entity_available(
                engine_available=True, publishable=False, last_value=None
            )
            is False
        )

    def test_available_with_a_publishable_reading(self) -> None:
        assert (
            util.energy_entity_available(
                engine_available=True, publishable=True, last_value=None
            )
            is True
        )

    def test_available_with_history_even_when_stale(self) -> None:
        """A meter with real history stays readable through a gap."""
        assert (
            util.energy_entity_available(
                engine_available=True, publishable=False, last_value=2.041
            )
            is True
        )

    def test_unavailable_when_the_engine_is_unreachable(self) -> None:
        """An unreachable engine makes it unavailable, like every time series."""
        for publishable in (False, True):
            for last_value in (None, 2.041):
                assert (
                    util.energy_entity_available(
                        engine_available=False,
                        publishable=publishable,
                        last_value=last_value,
                    )
                    is False
                )

    def test_power_keeps_its_current_semantics(self) -> None:
        """Only ``energy`` changed: power is still a current measurement."""
        values = util.device_entities_available(
            result_present=True,
            result_timestamp=None,  # stale: no timestamp
            quality_status="good",
            engine_available=True,
            stale_after=120,
        )
        assert values is False


class TestEnergySensorWiring:
    """The sensor must call the tested rule, not re-implement availability."""

    def _source(self) -> str:
        return (PACKAGE_DIR / "sensor.py").read_text()

    def test_energy_is_not_in_the_always_readable_set(self) -> None:
        source = self._source()
        start = source.index("class WattMosaicDeviceSensor")
        body = source[source.index("    def available(self)", start) :]
        body = body[: body.index("    @property", 1)]
        assert '"energy",' not in body

    def test_energy_uses_the_tested_availability_rule(self) -> None:
        source = self._source()
        assert "energy_entity_available(" in source
        assert "publishable=self._publishable(result)" in source
        assert "last_value=self._energy" in source

    def test_energy_exposes_the_held_total_as_last_value(self) -> None:
        source = self._source()
        assert 'attributes["last_value"] = self._energy' in source


@pytest.mark.parametrize("value", [0.0, 0.001, 2.041, 12345.678])
def test_last_value_never_lost(value: float) -> None:
    """The held total is preserved for every value the engine can publish."""
    assert (
        util.energy_entity_available(
            engine_available=True, publishable=False, last_value=value
        )
        is True
    )
