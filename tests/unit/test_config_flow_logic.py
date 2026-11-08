"""Unit tests for the per-device configuration flow helpers.

``flow_logic`` is deliberately pure Python (no Home Assistant import), so it is
loaded here through a synthetic package the same way
``tests/integration/test_ha_integration.py`` does: importing
``custom_components/wattmosaic/__init__.py`` would pull in Home Assistant, which
is not a test dependency of this repository.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import types
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parents[2] / "custom_components" / "wattmosaic"
PACKAGE_NAME = "wm_flow_logic_under_test"


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
flow = importlib.import_module(f"{PACKAGE_NAME}.flow_logic")


# --------------------------------------------------------------------------- #
# parse_entity_list
# --------------------------------------------------------------------------- #


def test_parse_entity_list_comma_separated() -> None:
    assert flow.parse_entity_list("light.kitchen,light.hall") == [
        "light.kitchen",
        "light.hall",
    ]


def test_parse_entity_list_trims_whitespace_and_newlines() -> None:
    assert flow.parse_entity_list("  light.kitchen ,\n switch.pump \t") == [
        "light.kitchen",
        "switch.pump",
    ]


def test_parse_entity_list_drops_empty_entries() -> None:
    assert flow.parse_entity_list("light.a,,  ,light.b,") == ["light.a", "light.b"]
    assert flow.parse_entity_list("") == []
    assert flow.parse_entity_list("   ") == []
    assert flow.parse_entity_list(None) == []


def test_parse_entity_list_deduplicates_keeping_first_order() -> None:
    assert flow.parse_entity_list("light.b,light.a,light.b") == ["light.b", "light.a"]


def test_parse_entity_list_accepts_an_iterable() -> None:
    assert flow.parse_entity_list(["light.a", " light.b "]) == ["light.a", "light.b"]
    assert flow.parse_entity_list(("light.a", "")) == ["light.a"]


def test_parse_entity_list_rejects_unusable_input() -> None:
    assert flow.parse_entity_list(42) == []
    assert flow.parse_entity_list({"light": "a"}) == []


# --------------------------------------------------------------------------- #
# normalise_device_configuration
# --------------------------------------------------------------------------- #


def test_normalise_drops_empty_fields() -> None:
    assert flow.normalise_device_configuration({}) == {}
    assert flow.normalise_device_configuration(None) == {}
    assert (
        flow.normalise_device_configuration(
            {
                "device_type": "unknown",
                "connectivity": "unknown",
                "monitoring": "unknown",
                "behavior": "unknown",
                "state_entity_ids": "",
                "mode_entity_ids": "   ",
                "expected_on_w": "",
                "expected_standby_w": None,
            }
        )
        == {}
    )


def test_normalise_never_sends_device_id() -> None:
    params = flow.normalise_device_configuration(
        {"device_id": "hot_tub", "device_type": "hot_tub"}
    )
    assert "device_id" not in params
    assert params == {"device_type": "hot_tub"}


def test_normalise_keeps_zero_litres_but_drops_none() -> None:
    """``0.0`` is a stated value (a device that draws nothing); ``None`` is not."""
    params = flow.normalise_device_configuration(
        {"expected_on_w": 0.0, "expected_standby_w": None}
    )
    assert params == {"expected": {"on_w": 0.0}}

    params = flow.normalise_device_configuration(
        {"expected_on_w": "", "expected_standby_w": 2.5}
    )
    assert params == {"expected": {"standby_w": 2.5}}


def test_normalise_rejects_negative_watts() -> None:
    assert flow.normalise_device_configuration({"expected_on_w": -5}) == {}


def test_normalise_coerces_bulb_count_to_int() -> None:
    params = flow.normalise_device_configuration(
        {"device_type": "light_group", "bulb_count": "5"}
    )
    assert params["light"]["bulb_count"] == 5
    assert isinstance(params["light"]["bulb_count"], int)


def test_normalise_clamps_and_floors_bulb_count() -> None:
    # A zero/negative count is clamped to one bulb; the block still carries the
    # multi-bulb signal only when the count is meaningful.
    assert flow.normalise_device_configuration(
        {"device_type": "light", "bulb_count": 0}
    ) == {"device_type": "light"}
    assert flow.normalise_device_configuration(
        {"device_type": "light", "bulb_count": 3}
    )["light"]["bulb_count"] == 3
    assert flow.normalise_device_configuration(
        {"device_type": "light", "bulb_count": 1000}
    )["light"]["bulb_count"] == 200


def test_normalise_omits_a_default_light_block() -> None:
    # One unknown bulb and no stated watts carries no information.
    assert flow.normalise_device_configuration(
        {"device_type": "light", "light_type": "unknown", "bulb_count": 1}
    ) == {"device_type": "light"}


def test_normalise_keeps_meaningful_light_detail() -> None:
    params = flow.normalise_device_configuration(
        {
            "device_type": "light_group",
            "light_type": "led",
            "bulb_count": 5,
            "watts_per_bulb_on": 9.0,
        }
    )
    assert params["device_type"] == "light_group"
    assert params["light"] == {
        "light_type": "led",
        "bulb_count": 5,
        "watts_per_bulb_on": 9.0,
    }


def test_normalise_drops_light_detail_for_non_light_devices() -> None:
    params = flow.normalise_device_configuration(
        {"device_type": "hot_tub", "bulb_count": 4, "watts_per_bulb_on": 9}
    )
    assert "light" not in params


def test_normalise_entity_and_controller_blocks() -> None:
    params = flow.normalise_device_configuration(
        {
            "device_type": "hvac_combined",
            "connectivity": "connected",
            "monitoring": "shared",
            "behavior": "complex",
            "state_entity_ids": "sensor.ac_power",
            "mode_entity_ids": "climate.thermostat",
            "controller_mode_entity_id": " climate.thermostat ",
            "controller_load_entity_ids": "binary_sensor.fan_running,binary_sensor.heat",
        }
    )
    assert params == {
        "device_type": "hvac_combined",
        "connectivity": "connected",
        "monitoring": "shared",
        "behavior": "complex",
        "state_entity_ids": ["sensor.ac_power"],
        "mode_entity_ids": ["climate.thermostat"],
        "controller": {
            "mode_entity_id": "climate.thermostat",
            "load_entity_ids": ["binary_sensor.fan_running", "binary_sensor.heat"],
        },
    }


def test_normalise_maps_an_unknown_select_value_to_unknown() -> None:
    """An unrecognised value degrades to the contract default and is dropped."""
    assert flow.normalise_device_configuration({"monitoring": "nonsense"}) == {}


# --------------------------------------------------------------------------- #
# validate_device_configuration
# --------------------------------------------------------------------------- #


def test_validate_minimal_form_is_clean() -> None:
    assert flow.validate_device_configuration(
        {
            "device_type": "appliance",
            "connectivity": "not_connected",
            "monitoring": "dedicated",
            "behavior": "fixed",
        }
    ) == {}


def test_validate_refuses_an_all_default_form() -> None:
    """An empty submission would be read by the engine as "remove configuration"."""
    errors = flow.validate_device_configuration(
        {
            "device_type": "unknown",
            "connectivity": "unknown",
            "monitoring": "unknown",
            "behavior": "unknown",
            "state_entity_ids": "",
            "mode_entity_ids": "",
            "expected_on_w": None,
            "expected_standby_w": None,
            "bulb_count": None,
            "light_type": "unknown",
            "watts_per_bulb_on": None,
            "controller_mode_entity_id": "",
            "controller_load_entity_ids": "",
        }
    )
    assert errors == {"base": "nothing_to_save"}


def test_validate_requires_the_three_axes() -> None:
    errors = flow.validate_device_configuration({})
    assert set(errors) == {"device_type", "connectivity", "behavior"}


def test_validate_rejects_state_entities_for_a_non_connected_device() -> None:
    errors = flow.validate_device_configuration(
        {
            "device_type": "appliance",
            "connectivity": "not_connected",
            "behavior": "fixed",
            "state_entity_ids": "light.hall",
        }
    )
    assert errors["state_entity_ids"] == "not_connected_with_state_entities"


def test_validate_rejects_malformed_entity_ids() -> None:
    errors = flow.validate_device_configuration(
        {
            "device_type": "light",
            "connectivity": "connected",
            "behavior": "fixed",
            "state_entity_ids": "not-an-entity",
        }
    )
    assert errors["state_entity_ids"] == "invalid_entity_id"


def test_validate_complex_requires_a_controller() -> None:
    base = {
        "device_type": "hvac_combined",
        "connectivity": "connected",
        "monitoring": "home_total_only",
    }
    assert "behavior" in flow.validate_device_configuration(
        {**base, "behavior": "complex"}
    )
    assert flow.validate_device_configuration(
        {
            **base,
            "behavior": "complex",
            "controller_mode_entity_id": "climate.thermostat",
        }
    ) == {}


def test_validate_rejects_negative_and_non_numeric_watts() -> None:
    base = {"device_type": "fan", "connectivity": "connected", "behavior": "fixed"}
    assert flow.validate_device_configuration(
        {**base, "expected_on_w": -1}
    )["expected_on_w"] == "invalid_number"
    assert flow.validate_device_configuration(
        {**base, "expected_on_w": "lots"}
    )["expected_on_w"] == "invalid_number"
    assert flow.validate_device_configuration({**base, "bulb_count": 0})[
        "bulb_count"
    ] == "invalid_number"


# --------------------------------------------------------------------------- #
# device_configuration_command
# --------------------------------------------------------------------------- #


def test_command_body_shape() -> None:
    body = flow.device_configuration_command(" hot_tub ", {"device_type": "hot_tub"})
    assert body == {
        "command": "set_device_configuration",
        "device_id": "hot_tub",
        "parameters": {"device_type": "hot_tub"},
    }
