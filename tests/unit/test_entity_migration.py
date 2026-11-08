"""Entity-id migration must preserve long-term statistics.

Renaming a translated entity name silently changes the ``entity_id`` Home
Assistant derives, and an existing install keeps the old registry entry.  The
recorder migrates statistics only on an explicit ``async_update_entity`` rename;
deleting and re-creating leaves the old series behind.

These tests pin the *decision* (which entities to rename and which to leave
alone), which is the part that can silently orphan a year of energy history.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import types
from pathlib import Path

import pytest

PACKAGE_DIR = Path(__file__).resolve().parents[2] / "custom_components" / "wattmosaic"
PACKAGE_NAME = "wm_entity_migration_under_test"


def _load_package() -> types.ModuleType:
    if PACKAGE_NAME in sys.modules:
        return sys.modules[PACKAGE_NAME]
    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME,
        PACKAGE_DIR / "__init__.py",
        submodule_search_locations=[str(PACKAGE_DIR)],
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    # Do not exec the spec: that would import homeassistant.
    module.__path__ = [str(PACKAGE_DIR)]  # type: ignore[attr-defined]
    sys.modules[PACKAGE_NAME] = module
    return module


_load_package()
migration = importlib.import_module(f"{PACKAGE_NAME}.entity_migration")
migrated_object_id = migration.migrated_object_id
planned_renames = migration.planned_renames


def _entry(entity_id: str, *, name: str | None = None, unique_id: str = "u") -> dict:
    return {"entity_id": entity_id, "name": name, "unique_id": unique_id}


def test_estimated_energy_is_renamed():
    assert migrated_object_id("water_heater_estimated_energy") == "water_heater_energy"


def test_estimated_power_is_renamed():
    assert migrated_object_id("ev_charger_estimated_power") == "ev_charger_power"


def test_model_estimated_power_uses_the_longer_rule_first():
    """``_estimated_power`` must not match inside ``_model_estimated_power``."""
    assert migrated_object_id("total_model_estimated_power") == "total_model_power"


def test_a_name_that_merely_contains_the_word_is_untouched():
    """The rewrite is suffix-only; an interior occurrence is a device name."""
    assert migrated_object_id("estimated_energy_shed_energy") is None


def test_the_engine_devices_own_entities_are_left_alone():
    assert migrated_object_id("wattmosaic_engine_status") is None


def test_only_our_unrenamed_sensor_entities_are_candidates():
    entities = [
        _entry("sensor.a_estimated_energy"),
        _entry("sensor.b_estimated_energy", name="My own name"),
        _entry("binary_sensor.c_estimated_energy"),
        _entry("sensor.d_other"),
    ]
    renames = planned_renames(entities)
    assert renames == [("sensor.a_estimated_energy", "sensor.a_energy")]


def test_a_target_that_is_already_taken_is_skipped():
    """Renaming onto an occupied id would fail or steal another entity."""
    entities = [_entry("sensor.a_estimated_energy")]
    renames = planned_renames(
        entities, existing_entity_ids=["sensor.a_energy"]
    )
    assert renames == []


def test_an_already_migrated_entity_is_not_renamed_again():
    entities = [_entry("sensor.a_energy")]
    assert planned_renames(entities) == []


def test_an_empty_entity_id_never_matches_the_domain_prefix():
    assert planned_renames([_entry("")]) == []


def test_two_stale_entities_cannot_be_renamed_onto_one_target():
    entities = [
        _entry("sensor.a_estimated_energy", unique_id="u1"),
        _entry("sensor.a_estimated_energy_2", unique_id="u2"),
    ]
    renames = planned_renames(entities)
    assert len({new for _, new in renames}) == len(renames) or renames == []


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("sensor.lights_estimated_energy", "sensor.lights_energy"),
        ("sensor.lights_estimated_energy_2", "sensor.lights_energy_2"),
        ("sensor.lights_model_estimated_power", "sensor.lights_model_power"),
    ],
)
def test_real_world_ids(old, new):
    assert planned_renames([_entry(old)]) == [(old, new)]
