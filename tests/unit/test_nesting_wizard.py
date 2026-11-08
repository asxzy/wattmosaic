"""Wizard-side nesting, duplicate-feed and circuit-multiplier logic.

Two things are being pinned here.

**Validation must be able to say no.**  A parent link, a duplicate declaration and
a multiplier are all *user assertions* about wiring.  Getting them wrong is not a
cosmetic error: a parent cycle makes the topology graph refuse to build, a
duplicate that is not really a duplicate silently drops a feed, and a multiplier
below zero reads as export.  So each rule gets a test that fails when the rule is
removed.

**Discovery must never write a wiring fact.**  ``apply_nesting_candidates`` only
attaches *proposals* under ``metadata.nesting_candidates``.  ``parent_ct_id``
stays untouched, which is asserted explicitly rather than assumed.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import types
from pathlib import Path

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


def _topology() -> dict:
    """Two nested panels, each a genuine L1/L2 pair, plus a branch and a load."""
    return {
        "version": 2,
        "channels": [
            {
                "ct_id": "service_l1",
                "name": "Service L1",
                "ct_type": "main",
                "phase": "l1",
                "paired_ct_id": "service_l2",
            },
            {
                "ct_id": "service_l2",
                "name": "Service L2",
                "ct_type": "main",
                "phase": "l2",
                "paired_ct_id": "service_l1",
            },
            {
                "ct_id": "panel_l1",
                "name": "Panel L1",
                "ct_type": "main",
                "phase": "l1",
                "paired_ct_id": "panel_l2",
            },
            {
                "ct_id": "panel_l2",
                "name": "Panel L2",
                "ct_type": "main",
                "phase": "l2",
                "paired_ct_id": "panel_l1",
            },
            {"ct_id": "branch_a", "name": "A", "ct_type": "branch", "phase": "l1"},
            {"ct_id": "tub", "name": "Tub", "ct_type": "dedicated_device", "phase": "l1"},
        ],
    }


def _channels() -> list[dict]:
    return flow.normalise_channels(_topology())


# --------------------------------------------------------------------------- #
# Multiplier normalisation
# --------------------------------------------------------------------------- #
def test_default_multiplier_is_one():
    assert flow.DEFAULT_MULTIPLIER == 1.0
    assert flow.normalise_assignment({})["measurement_multiplier"] == 1.0


def test_an_explicit_multiplier_is_kept():
    assert flow.normalise_assignment({"measurement_multiplier": 2})[
        "measurement_multiplier"
    ] == 2.0
    assert flow.normalise_assignment({"measurement_multiplier": "2.5"})[
        "measurement_multiplier"
    ] == 2.5


def test_unparseable_multiplier_input_falls_back_to_the_default():
    for bad in (None, "", "two", float("nan"), float("inf")):
        assert flow.normalise_assignment({"measurement_multiplier": bad})[
            "measurement_multiplier"
        ] == 1.0


def test_an_out_of_range_multiplier_is_preserved_for_validation_to_reject():
    """Clamping here would hide the error; the validator must be able to see it.

    A typed ``200`` clamped to ``1.0`` would report the circuit at 1/200th of its
    load with nothing shown to the user.
    """
    for bad in (0, -2, 200):
        normalised = flow.normalise_assignment({"measurement_multiplier": bad})
        assert normalised["measurement_multiplier"] == float(bad)


def test_a_multiplier_outside_the_bounds_is_rejected_by_validation():
    assignments = {"branch_a": {"role": "branch", "phase": "l1", "measurement_multiplier": 200}}
    assert any(
        e.startswith("multiplier_out_of_range")
        for e in flow.validate_assignments(assignments, _channels())
    )


# --------------------------------------------------------------------------- #
# Parent links
# --------------------------------------------------------------------------- #
def test_a_valid_deep_parent_chain_is_accepted():
    """service -> panel -> branch is real wiring, so it must not be an error."""
    assignments = {
        "service_l1": {"role": "main", "phase": "l1", "paired_ct_id": "service_l2"},
        "service_l2": {"role": "main", "phase": "l2", "paired_ct_id": "service_l1"},
        "panel_l1": {
            "role": "main",
            "phase": "l1",
            "paired_ct_id": "panel_l2",
            "parent_ct_id": "service_l1",
        },
        "panel_l2": {
            "role": "main",
            "phase": "l2",
            "paired_ct_id": "panel_l1",
            "parent_ct_id": "service_l2",
        },
        "branch_a": {"role": "branch", "phase": "l1", "parent_ct_id": "panel_l1"},
    }
    assert flow.validate_assignments(assignments, _channels()) == []


def test_an_unknown_parent_is_rejected():
    assignments = {"branch_a": {"role": "branch", "phase": "l1", "parent_ct_id": "nope"}}
    errors = flow.validate_assignments(assignments, _channels())
    assert any(e.startswith("parent_unknown") for e in errors)


def test_a_ct_cannot_be_its_own_parent():
    assignments = {"branch_a": {"role": "branch", "phase": "l1", "parent_ct_id": "branch_a"}}
    errors = flow.validate_assignments(assignments, _channels())
    assert any(e.startswith("parent_self") for e in errors)


def test_a_single_device_ct_cannot_be_a_parent():
    """A ``dedicated_device`` CT watches one load; nothing sits behind it."""
    assignments = {
        "tub": {"role": "dedicated_device", "phase": "l1", "device_id": "tub"},
        "branch_a": {"role": "branch", "phase": "l1", "parent_ct_id": "tub"},
    }
    errors = flow.validate_assignments(assignments, _channels())
    assert any(e.startswith("parent_not_container") for e in errors)


def test_a_parent_cycle_is_rejected():
    assignments = {
        "panel_l1": {
            "role": "main",
            "phase": "l1",
            "paired_ct_id": "panel_l2",
            "parent_ct_id": "service_l1",
        },
        "panel_l2": {"role": "main", "phase": "l2", "paired_ct_id": "panel_l1"},
        "service_l1": {
            "role": "main",
            "phase": "l1",
            "paired_ct_id": "service_l2",
            "parent_ct_id": "panel_l1",
        },
        "service_l2": {"role": "main", "phase": "l2", "paired_ct_id": "service_l1"},
    }
    errors = flow.validate_assignments(assignments, _channels())
    assert any(e.startswith("parent_cycle") for e in errors)


def test_a_longer_parent_cycle_is_rejected():
    assignments = {
        "service_l1": {
            "role": "main",
            "phase": "l1",
            "paired_ct_id": "service_l2",
            "parent_ct_id": "branch_a",
        },
        "service_l2": {"role": "main", "phase": "l2", "paired_ct_id": "service_l1"},
        "panel_l1": {
            "role": "main",
            "phase": "l1",
            "paired_ct_id": "panel_l2",
            "parent_ct_id": "service_l1",
        },
        "panel_l2": {"role": "main", "phase": "l2", "paired_ct_id": "panel_l1"},
        "branch_a": {"role": "branch", "phase": "l1", "parent_ct_id": "panel_l1"},
    }
    errors = flow.validate_assignments(assignments, _channels())
    assert any(e.startswith("parent_cycle") for e in errors)


# --------------------------------------------------------------------------- #
# Duplicate feeds
# --------------------------------------------------------------------------- #
def test_a_duplicate_must_exist():
    assignments = {"panel_l1": {"role": "main", "phase": "l1", "alias_of_ct_id": "nope"}}
    errors = flow.validate_assignments(assignments, _channels())
    assert any(e.startswith("alias_unknown") for e in errors)


def test_a_ct_cannot_be_a_duplicate_of_itself():
    assignments = {"panel_l1": {"role": "main", "phase": "l1", "alias_of_ct_id": "panel_l1"}}
    errors = flow.validate_assignments(assignments, _channels())
    assert any(e.startswith("alias_self") for e in errors)


def test_a_duplicate_cycle_is_rejected():
    assignments = {
        "panel_l1": {"role": "main", "phase": "l1", "alias_of_ct_id": "service_l1"},
        "service_l1": {"role": "main", "phase": "l1", "alias_of_ct_id": "panel_l1"},
    }
    errors = flow.validate_assignments(assignments, _channels())
    assert any(e.startswith("alias_cycle") for e in errors)


def test_a_plain_duplicate_declaration_is_accepted():
    assignments = {
        "service_l1": {"role": "main", "phase": "l1", "paired_ct_id": "service_l2"},
        "service_l2": {"role": "main", "phase": "l2", "paired_ct_id": "service_l1"},
        "panel_l1": {
            "role": "main",
            "phase": "l1",
            "paired_ct_id": "panel_l2",
            "alias_of_ct_id": "service_l1",
        },
        "panel_l2": {"role": "main", "phase": "l2", "paired_ct_id": "panel_l1"},
    }
    assert flow.validate_assignments(assignments, _channels()) == []


# --------------------------------------------------------------------------- #
# Building the topology update
# --------------------------------------------------------------------------- #
def test_build_topology_update_carries_parent_alias_and_multiplier():
    assignments = {
        "panel_l1": {
            "role": "main",
            "phase": "l1",
            "paired_ct_id": "panel_l2",
            "parent_ct_id": "service_l1",
            "measurement_multiplier": 2.0,
        },
        "branch_a": {"role": "branch", "phase": "l1", "alias_of_ct_id": "panel_l1"},
    }
    updated = flow.build_topology_update(_topology(), assignments)
    by_id = {c["ct_id"]: c for c in updated["channels"]}
    assert by_id["panel_l1"]["parent_ct_id"] == "service_l1"
    assert by_id["panel_l1"]["measurement_multiplier"] == 2.0
    assert by_id["branch_a"]["alias_of_ct_id"] == "panel_l1"
    # Still a candidate: the wizard never confirms by itself.
    assert by_id["panel_l1"]["topology_status"] == "candidate"


# --------------------------------------------------------------------------- #
# Discovery proposals are never applied
# --------------------------------------------------------------------------- #
def test_apply_nesting_candidates_writes_metadata_only():
    payload = {"candidate_parents": {"panel_l1": ["service_l1"], "branch_a": ["panel_l1"]}}
    updated = flow.apply_nesting_candidates(_topology(), payload)
    by_id = {c["ct_id"]: c for c in updated["channels"]}
    assert by_id["panel_l1"]["metadata"]["nesting_candidates"] == ["service_l1"]
    assert by_id["branch_a"]["metadata"]["nesting_candidates"] == ["panel_l1"]
    # The critical assertion: the wiring fact was NOT written.
    for channel in updated["channels"]:
        assert "parent_ct_id" not in channel or channel["parent_ct_id"] is None


def test_apply_nesting_candidates_does_not_mutate_the_input():
    original = _topology()
    snapshot = {c["ct_id"]: dict(c) for c in original["channels"]}
    flow.apply_nesting_candidates(original, {"candidate_parents": {"panel_l1": ["service_l1"]}})
    for channel in original["channels"]:
        assert channel == snapshot[channel["ct_id"]]


def test_apply_nesting_candidates_without_proposals_is_a_passthrough():
    assert flow.apply_nesting_candidates(_topology(), None)["channels"]


def test_apply_nesting_candidates_accepts_an_older_payload_without_aliases():
    """The integration must keep working against an engine that predates aliases.

    The live install can be pinned to an older engine build, whose response has
    ``candidate_parents`` but no ``candidate_aliases``.  A missing key must be a
    no-op, never a crash and never a silently invented alias.
    """
    updated = flow.apply_nesting_candidates(
        _topology(), {"candidate_parents": {"panel_l1": ["service_l1"]}}
    )
    by_id = {c["ct_id"]: c for c in updated["channels"]}
    assert by_id["panel_l1"]["metadata"]["nesting_candidates"] == ["service_l1"]
    for channel in updated["channels"]:
        assert "alias_candidates" not in (channel.get("metadata") or {})


def test_apply_nesting_candidates_accepts_an_alias_only_payload():
    """An engine that reports only duplicates is still usable."""
    updated = flow.apply_nesting_candidates(
        _topology(), {"candidate_aliases": {"panel_l1": "service_l1"}}
    )
    by_id = {c["ct_id"]: c for c in updated["channels"]}
    assert by_id["panel_l1"]["metadata"]["alias_candidates"] == ["service_l1"]
    for channel in updated["channels"]:
        assert "nesting_candidates" not in (channel.get("metadata") or {})


def test_apply_nesting_candidates_records_alias_proposals_separately():
    """An alias is the same conductor, not a parent: the two never mix."""
    payload = {
        "candidate_parents": {"panel_l1": ["service_l1"]},
        "candidate_aliases": {"branch_a": "panel_l1"},
    }
    updated = flow.apply_nesting_candidates(_topology(), payload)
    by_id = {c["ct_id"]: c for c in updated["channels"]}
    assert by_id["panel_l1"]["metadata"]["nesting_candidates"] == ["service_l1"]
    assert by_id["branch_a"]["metadata"]["alias_candidates"] == ["panel_l1"]
    # An alias must not be written as a parent, and vice versa.
    assert "nesting_candidates" not in by_id["branch_a"]["metadata"]
    for channel in updated["channels"]:
        assert "alias_of_ct_id" not in channel or channel["alias_of_ct_id"] is None


def test_an_alias_proposal_never_becomes_a_parent_link():
    """The critical separation: a duplicate feed is not a nested feed."""
    updated = flow.apply_nesting_candidates(
        _topology(), {"candidate_aliases": {"branch_a": "panel_l1"}}
    )
    by_id = {c["ct_id"]: c for c in updated["channels"]}
    assert by_id["branch_a"].get("parent_ct_id") in (None, "")
    assert by_id["branch_a"]["metadata"]["alias_candidates"] == ["panel_l1"]


# --------------------------------------------------------------------------- #
# Suggestions
# --------------------------------------------------------------------------- #
def test_a_single_unambiguous_candidate_is_suggested():
    channel = {"ct_id": "panel_l1", "metadata": {"nesting_candidates": ["service_l1"]}}
    assert flow.suggested_parent(channel, _channels()) == "service_l1"


def test_two_candidates_are_never_narrowed_to_one():
    """Two candidates mean the evidence is ambiguous; a guess is not a suggestion."""
    channel = {"ct_id": "branch_a", "metadata": {"nesting_candidates": ["service_l1", "panel_l1"]}}
    assert flow.suggested_parent(channel, _channels()) is None


def test_a_descendant_is_never_suggested_as_a_parent():
    """Pre-selecting a descendant would build the cycle the validator rejects."""
    channels = [
        {"ct_id": "outer", "parent_ct_id": None},
        {"ct_id": "inner", "parent_ct_id": "outer"},
    ]
    channel = {"ct_id": "outer", "metadata": {"nesting_candidates": ["inner"]}}
    assert flow.suggested_parent(channel, channels) is None


def test_no_candidates_means_no_suggestion():
    assert flow.suggested_parent({"ct_id": "panel_l1"}, _channels()) is None


def test_a_single_alias_candidate_is_suggested():
    channel = {"ct_id": "branch_a", "metadata": {"alias_candidates": ["panel_l1"]}}
    assert flow.suggested_alias(channel, _channels()) == "panel_l1"


def test_a_declared_fact_suppresses_the_alias_suggestion():
    """A declared pair or parent already answers the question.

    Competing with a stated fact would present the inference as contradicting the
    user, so the suggestion is dropped rather than offered as an alternative.
    """
    paired = {
        "ct_id": "branch_a",
        "paired_ct_id": "branch_b",
        "metadata": {"alias_candidates": ["panel_l1"]},
    }
    assert flow.suggested_alias(paired, _channels()) is None
    parented = {
        "ct_id": "branch_a",
        "parent_ct_id": "panel_l1",
        "metadata": {"alias_candidates": ["panel_l1"]},
    }
    assert flow.suggested_alias(parented, _channels()) is None


def test_an_alias_never_suggests_itself_or_a_descendant():
    itself = {"ct_id": "branch_a", "metadata": {"alias_candidates": ["branch_a"]}}
    assert flow.suggested_alias(itself, _channels()) is None
    channels = [
        {"ct_id": "outer", "parent_ct_id": None},
        {"ct_id": "inner", "parent_ct_id": "outer"},
    ]
    cyclic = {"ct_id": "outer", "metadata": {"alias_candidates": ["inner"]}}
    assert flow.suggested_alias(cyclic, channels) is None


def test_a_parent_proposal_is_not_read_as_an_alias():
    """The two proposal kinds must not be confused in either direction."""
    channel = {"ct_id": "panel_l1", "metadata": {"nesting_candidates": ["service_l1"]}}
    assert flow.suggested_alias(channel, _channels()) is None


def test_descendant_ids_is_transitive():
    channels = [
        {"ct_id": "a", "parent_ct_id": None},
        {"ct_id": "b", "parent_ct_id": "a"},
        {"ct_id": "c", "parent_ct_id": "b"},
    ]
    assert flow.descendant_ids("a", channels) == {"b", "c"}
    assert flow.descendant_ids("c", channels) == set()


def test_descendant_ids_survives_a_cycle_without_hanging():
    channels = [
        {"ct_id": "a", "parent_ct_id": "b"},
        {"ct_id": "b", "parent_ct_id": "a"},
    ]
    # Any result is acceptable as long as it terminates.
    assert flow.descendant_ids("a", channels) <= {"a", "b"}
