"""Unit tests for the per-device configuration flow helpers.

``flow_logic`` is deliberately pure Python (no Home Assistant import), so it is
loaded here through a synthetic package the same way
``tests/integration/test_ha_integration.py`` does: importing
``custom_components/wattmosaic/__init__.py`` would pull in Home Assistant, which
is not a test dependency of this repository.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import sys
import types
from pathlib import Path
from typing import Any

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
            "monitoring": "shared",
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


# --------------------------------------------------------------------------- #
# H2 — HA setup flow v3 (pure flow_logic tests, no homeassistant)
# --------------------------------------------------------------------------- #


def _endpoints_payload() -> list[dict]:
    return [
        {
            "endpoint_id": "main_l1",
            "kind": "ct",
            "scope": "circuit",
            "scope_status": "candidate",
            "entity_id": "sensor.main_power",
        },
        {
            "endpoint_id": "sub_l1",
            "kind": "ct",
            "scope": "circuit",
            "scope_status": "candidate",
            "entity_id": "sensor.sub_power",
        },
        {
            "endpoint_id": "plug_kettle",
            "kind": "smart_plug",
            "scope": "single_subject",
            "scope_status": "candidate",
            "scope_subject_id": "dev_kettle",
            "entity_id": "sensor.kettle_power",
        },
    ]


def test_v3_entity_link_bodies_from_a_link_resolution() -> None:
    """DEC-08: device links produce many-to-many ha_entity_links bodies."""
    sl = importlib.import_module(f"{PACKAGE_NAME}.state_links")
    resolution = sl.LinkResolution(
        state_entities={
            "dev_kettle": ("switch.kettle", "binary_sensor.kettle_on"),
            "dev_tub": ("switch.tub",),
        }
    )
    bodies = sl.build_ha_entity_link_bodies(resolution)
    assert bodies == [
        {
            "entity_id": "switch.kettle",
            "target_kind": "device",
            "target_id": "dev_kettle",
            "role": "state",
            "declared_by": "user",
        },
        {
            "entity_id": "binary_sensor.kettle_on",
            "target_kind": "device",
            "target_id": "dev_kettle",
            "role": "state",
            "declared_by": "user",
        },
        {
            "entity_id": "switch.tub",
            "target_kind": "device",
            "target_id": "dev_tub",
            "role": "state",
            "declared_by": "user",
        },
    ]


def test_v3_normalise_endpoints_keeps_absent_values_as_none() -> None:
    endpoints = flow.normalise_endpoints(_endpoints_payload())
    assert [e["endpoint_id"] for e in endpoints] == ["main_l1", "sub_l1", "plug_kettle"]
    assert endpoints[0]["scope_subject_id"] is None
    assert endpoints[2]["scope_subject_id"] == "dev_kettle"
    assert flow.normalise_endpoints({"endpoints": _endpoints_payload()}) == endpoints
    assert flow.normalise_endpoints(None) == []


def test_a_circuit_ct_is_never_a_single_subject_candidate() -> None:
    ct = _endpoints_payload()[0]
    assert flow.is_single_subject_candidate(ct) is False
    assert flow.single_subject_proposal(ct) is None
    # Even a stray scope_subject_id on a circuit CT is ignored (DEC-05).
    assert flow.single_subject_proposal({**ct, "scope_subject_id": "dev_x"}) is None


def test_only_candidate_plugs_are_offered_the_scope_question() -> None:
    plug = _endpoints_payload()[2]
    assert flow.is_single_subject_candidate(plug) is True
    assert flow.single_subject_proposal(plug) == "dev_kettle"
    confirmed = {**plug, "scope_status": "confirmed"}
    assert flow.is_single_subject_candidate(confirmed) is False


def test_engine_provisional_parents_are_suggestions_only() -> None:
    topo_edges = [
        {
            "relation": "CONTAINS",
            "lifecycle": "PROVISIONAL",
            "source_endpoint_id": "main_l1",
            "target_endpoint_id": "sub_l1",
        }
    ]
    suggestions = flow.suggested_parents(topo_edges)
    assert suggestions == {"sub_l1": ["main_l1"]}
    # A confirmed edge is already structure; it is not a suggestion.
    assert flow.suggested_parents([{**topo_edges[0], "lifecycle": "CONFIRMED"}]) == {}
    assert flow.unambiguous_parent("sub_l1", suggestions) == "main_l1"
    assert flow.unambiguous_parent("sub_l1", {"sub_l1": ["a", "b"]}) is None


def test_an_unaccepted_suggestion_is_never_written_as_a_user_edge() -> None:
    """DEC-13: an inferred parent is a suggestion, not a declaration."""
    endpoints = flow.normalise_endpoints(_endpoints_payload())
    suggestions = flow.suggested_parents(
        [
            {
                "relation": "CONTAINS",
                "lifecycle": "PROVISIONAL",
                "source_endpoint_id": "main_l1",
                "target_endpoint_id": "sub_l1",
            }
        ]
    )
    # The form submission carries no parent for sub_l1: the suggestion is shown
    # but not accepted.
    choices = flow.endpoint_choices(endpoints, {}, suggestions=suggestions)
    assert choices["sub_l1"]["suggested_parent"] == "main_l1"
    assert choices["sub_l1"]["parent_accepted"] is False
    assert flow.build_user_edge_plan(choices) == []

    # The user leaves it checked: now it is a user edge.
    accepted = flow.endpoint_choices(
        endpoints,
        {"sub_l1": {"parent_endpoint_id": "main_l1", "parent_accepted": True}},
        suggestions=suggestions,
    )
    plan = flow.build_user_edge_plan(accepted)
    assert plan == [
        {
            "path": "/topology/edges",
            "body": {
                "relation": "CONTAINS",
                "source": "main_l1",
                "target": "sub_l1",
                "declared_by": "user",
            },
        }
    ]


def test_a_user_declared_parent_needs_no_inference() -> None:
    endpoints = flow.normalise_endpoints(_endpoints_payload())
    choices = flow.endpoint_choices(
        endpoints, {"sub_l1": {"parent_endpoint_id": "main_l1", "parent_accepted": True}}
    )
    assert len(flow.build_user_edge_plan(choices)) == 1


def test_scope_confirm_only_when_a_device_is_selected() -> None:
    endpoints = flow.normalise_endpoints(_endpoints_payload())
    # No selection: no scope call at all.
    no_choice = flow.endpoint_choices(endpoints, {})
    assert flow.build_scope_plan(no_choice) == []

    selected = flow.endpoint_choices(
        endpoints, {"plug_kettle": {"scope_subject_id": "dev_kettle"}}
    )
    assert flow.build_scope_plan(selected) == [
        {
            "path": "/endpoints/plug_kettle/scope",
            "body": {"action": "confirm", "subject_id": "dev_kettle"},
        }
    ]
    # A device id on a circuit CT is refused (a CT is never single-subject).
    ct_scope = flow.endpoint_choices(
        endpoints, {"main_l1": {"scope_subject_id": "dev_x"}}
    )
    assert flow.build_scope_plan(ct_scope) == []


def test_v3_plan_has_no_topology_confirm() -> None:
    endpoints = flow.normalise_endpoints(_endpoints_payload())
    choices = flow.endpoint_choices(
        endpoints,
        {
            "sub_l1": {"parent_endpoint_id": "main_l1", "parent_accepted": True},
            "plug_kettle": {"scope_subject_id": "dev_kettle"},
        },
    )
    plan = flow.build_endpoint_setup_plan(choices)
    paths = [item["path"] for item in plan]
    assert paths == ["/topology/edges", "/endpoints/plug_kettle/scope"]
    assert all("/topology/confirm" not in p for p in paths)


def test_entity_links_are_written_only_for_confirmed_plugs() -> None:
    endpoints = flow.normalise_endpoints(_endpoints_payload())
    choices = flow.endpoint_choices(
        endpoints,
        {
            "plug_kettle": {"scope_subject_id": "dev_kettle"},
            "sub_l1": {"parent_endpoint_id": "main_l1", "parent_accepted": True},
        },
    )
    links = flow.build_entity_link_plan(
        choices, entities_by_endpoint={"plug_kettle": ["switch.kettle"]}
    )
    assert links == [
        {
            "path": "/entity-links",
            "body": {
                "entity_id": "switch.kettle",
                "target_kind": "device",
                "target_id": "dev_kettle",
                "declared_by": "user",
            },
        }
    ]


def test_migration_drops_unknown_keys_and_makes_parents_user_edges() -> None:
    legacy = {
        "tub_l1": {
            "role": "branch",
            "phase": "l1",
            "parent_ct_id": "main_l1",
            "device_id": "hot_tub",
            "bogus_key": "drop me",
        }
    }
    choices, dropped = flow.migrate_ct_assignments_to_v3(legacy)
    assert dropped == ["tub_l1.bogus_key"]
    assert choices["tub_l1"]["parent_endpoint_id"] == "main_l1"
    assert choices["tub_l1"]["parent_accepted"] is True
    assert flow.build_user_edge_plan(choices)[0]["body"]["source"] == "main_l1"
    assert flow.build_scope_plan(choices)[0]["body"]["subject_id"] == "hot_tub"


class _RecordingClient:
    """Fake HTTP client recording every v3 POST (no homeassistant, no network)."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []

    async def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        self.requests.append((path, dict(body)))
        return {"ok": True}


def test_commit_log_contains_only_user_accepted_writes() -> None:
    endpoints = flow.normalise_endpoints(_endpoints_payload())
    suggestions = flow.suggested_parents(
        [
            {
                "relation": "CONTAINS",
                "lifecycle": "PROVISIONAL",
                "source_endpoint_id": "main_l1",
                "target_endpoint_id": "sub_l1",
            }
        ]
    )
    # Only the plug scope is selected; the provisional parent is NOT accepted.
    choices = flow.endpoint_choices(
        endpoints,
        {"plug_kettle": {"scope_subject_id": "dev_kettle"}},
        suggestions=suggestions,
    )
    plan = flow.build_endpoint_setup_plan(choices)
    client = _RecordingClient()
    log = asyncio.run(flow.async_commit_endpoint_setup(client, plan))
    assert all(record["ok"] for record in log)
    # No inferred parent edge and no topology confirm were sent.
    assert [path for path, _ in client.requests] == ["/endpoints/plug_kettle/scope"]
    assert client.requests[0][1] == {"action": "confirm", "subject_id": "dev_kettle"}


def test_commit_log_posts_accepted_parent_edge() -> None:
    endpoints = flow.normalise_endpoints(_endpoints_payload())
    choices = flow.endpoint_choices(
        endpoints, {"sub_l1": {"parent_endpoint_id": "main_l1", "parent_accepted": True}}
    )
    client = _RecordingClient()
    asyncio.run(flow.async_commit_endpoint_setup(client, flow.build_endpoint_setup_plan(choices)))
    assert client.requests == [
        (
            "/topology/edges",
            {
                "relation": "CONTAINS",
                "source": "main_l1",
                "target": "sub_l1",
                "declared_by": "user",
            },
        )
    ]


