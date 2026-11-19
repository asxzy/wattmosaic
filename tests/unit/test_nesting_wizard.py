"""Nesting/parent suggestions in the v3 setup flow (Z2d).

The v2 per-CT channel wizard let a user assign a ``parent_ct_id``, a pair, an
alias and a measurement multiplier to every CT.  That wizard is gone: devices
are automatic (DEC-04) and the flow is connection -> endpoints -> done.

The *intent* it served — "let a user declare which circuit feeds which, without
ever writing an engine-inferred guess as fact" — survives in the v3 endpoint
step:

* engine PROVISIONAL ``CONTAINS`` edges become **suggestions**
  (:func:`suggested_parents` / :func:`unambiguous_parent`);
* a suggestion is posted only when the user explicitly accepts it
  (:func:`endpoint_choice` / :func:`build_user_edge_plan`);
* a declared parent is a ``declared_by=user`` edge (DEC-10), never a silent
  confirmation (DEC-13).

Everything here is pure: no Home Assistant, no engine, no network.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import types
from pathlib import Path
from typing import Any

PACKAGE_DIR = Path(__file__).resolve().parents[2] / "custom_components" / "wattmosaic"
PACKAGE_NAME = "wm_nesting_wizard_under_test"


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


def _endpoints() -> list[dict[str, Any]]:
    """Two nested CTs and a candidate plug, as the v3 endpoints API returns them."""
    return [
        {"endpoint_id": "main_l1", "kind": "ct", "scope": "circuit",
         "entity_id": "sensor.main_power"},
        {"endpoint_id": "sub_l1", "kind": "ct", "scope": "circuit",
         "entity_id": "sensor.sub_power"},
        {"endpoint_id": "plug_kettle", "kind": "smart_plug", "scope": "single_subject",
         "scope_subject_id": "dev_kettle", "entity_id": "sensor.kettle_power"},
    ]


def _edges() -> list[dict[str, Any]]:
    return [
        {"relation": "CONTAINS", "lifecycle": "PROVISIONAL",
         "source_endpoint_id": "main_l1", "target_endpoint_id": "sub_l1"},
    ]


# --------------------------------------------------------------------------- #
# Engine suggestions are proposals, never declarations
# --------------------------------------------------------------------------- #


def test_provisional_contains_edges_become_parent_suggestions() -> None:
    suggestions = flow.suggested_parents(_edges())
    assert suggestions == {"sub_l1": ["main_l1"]}
    # A confirmed edge is already structure, not a proposal to re-declare.
    confirmed = [{**_edges()[0], "lifecycle": "CONFIRMED"}]
    assert flow.suggested_parents(confirmed) == {}


def test_non_contains_edges_are_not_parent_suggestions() -> None:
    edge = {**_edges()[0], "relation": "SAME_CONDUCTOR"}
    assert flow.suggested_parents([edge]) == {}


def test_a_plug_is_never_a_contains_parent() -> None:
    endpoints = flow.normalise_endpoints(_endpoints())
    by_id = {e["endpoint_id"]: e for e in endpoints}
    assert flow.is_container_endpoint(by_id["main_l1"]) is True
    assert flow.is_container_endpoint(by_id["sub_l1"]) is True
    assert flow.is_container_endpoint(by_id["plug_kettle"]) is False


def test_two_competing_suggestions_are_ambiguous() -> None:
    """Pre-selecting either guess would present an inference as a measurement."""
    suggestions = {"sub_l1": ["main_l1", "other_l1"]}
    assert flow.unambiguous_parent("sub_l1", suggestions) is None
    assert flow.unambiguous_parent("sub_l1", {"sub_l1": ["main_l1"]}) == "main_l1"


# --------------------------------------------------------------------------- #
# A suggestion is written only when the user accepts it
# --------------------------------------------------------------------------- #


def test_an_unaccepted_suggestion_is_never_a_user_edge() -> None:
    """DEC-13: a shown-but-unchecked parent must not be posted."""
    endpoints = flow.normalise_endpoints(_endpoints())
    suggestions = flow.suggested_parents(_edges())
    choices = flow.endpoint_choices(endpoints, {}, suggestions=suggestions)
    assert choices["sub_l1"]["suggested_parent"] == "main_l1"
    assert choices["sub_l1"]["parent_accepted"] is False
    assert flow.build_user_edge_plan(choices) == []


def test_an_accepted_suggestion_becomes_a_declared_edge() -> None:
    endpoints = flow.normalise_endpoints(_endpoints())
    suggestions = flow.suggested_parents(_edges())
    choices = flow.endpoint_choices(
        endpoints,
        {"sub_l1": {"parent_endpoint_id": "main_l1", "parent_accepted": True}},
        suggestions=suggestions,
    )
    assert flow.build_user_edge_plan(choices) == [
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
    endpoints = flow.normalise_endpoints(_endpoints())
    choices = flow.endpoint_choices(
        endpoints, {"sub_l1": {"parent_endpoint_id": "main_l1", "parent_accepted": True}}
    )
    assert len(flow.build_user_edge_plan(choices)) == 1


def test_a_self_parent_is_never_written() -> None:
    endpoints = flow.normalise_endpoints(_endpoints())
    choices = flow.endpoint_choices(
        endpoints, {"main_l1": {"parent_endpoint_id": "main_l1", "parent_accepted": True}}
    )
    assert flow.build_user_edge_plan(choices) == []


# --------------------------------------------------------------------------- #
# Legacy declaration migration (H1) keeps the user's intent as user edges
# --------------------------------------------------------------------------- #


def test_legacy_parent_ct_id_becomes_an_accepted_user_edge() -> None:
    """A stored ``parent_ct_id`` migrates to an accepted parent endpoint."""
    choices, dropped = flow.migrate_ct_assignments_to_v3(
        {"sub_l1": {"role": "branch", "phase": "l1", "parent_ct_id": "main_l1"}}
    )
    assert dropped == []
    assert choices["sub_l1"]["parent_accepted"] is True
    edges = flow.build_user_edge_plan(choices)
    assert edges[0]["body"]["source"] == "main_l1"
    assert edges[0]["body"]["target"] == "sub_l1"
    assert edges[0]["body"]["declared_by"] == "user"


def test_migration_drops_unknown_legacy_keys_with_notice() -> None:
    _, dropped = flow.migrate_ct_assignments_to_v3(
        {"sub_l1": {"role": "branch", "bogus_key": "drop me"}}
    )
    assert dropped == ["sub_l1.bogus_key"]


def test_v3_setup_plan_has_no_v2_topology_write() -> None:
    """Only user edges, scope confirmations and entity links are posted."""
    endpoints = flow.normalise_endpoints(_endpoints())
    choices = flow.endpoint_choices(
        endpoints,
        {
            "sub_l1": {"parent_endpoint_id": "main_l1", "parent_accepted": True},
            "plug_kettle": {"scope_subject_id": "dev_kettle"},
        },
        suggestions=flow.suggested_parents(_edges()),
    )
    plan = flow.build_endpoint_setup_plan(choices)
    paths = [item["path"] for item in plan]
    assert paths == ["/topology/edges", "/endpoints/plug_kettle/scope"]
    assert all("/topology/confirm" not in p for p in paths)
