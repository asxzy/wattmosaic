"""Attaching a WattMosaic measurement to an existing Home Assistant device.

Placement is a Home Assistant concern: the engine has no concept of an HA device
id, so every rule here is integration-side and pure.  What these tests pin:

* ``BINDABLE_KEYS`` is exactly the set of entities that describe the *load*
  (power and energy).  Provenance entities -- source, confidence, tier, sample
  count -- stay on the synthetic device so a model's self-assessment is never
  presented as hardware telemetry.
* An explicit link beats entity-derived resolution, and entity-derived remains
  the fallback so an installation that never opened the new step is unchanged.
* Home Assistant replaces an entry's ``options`` wholesale, so every completion
  path must rebuild the whole link map; omitting it would silently detach every
  other device the user linked.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import types
from pathlib import Path

PACKAGE_DIR = (
    Path(__file__).resolve().parents[2] / "custom_components" / "wattmosaic"
)
PACKAGE_NAME = "wm_device_link_under_test"


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

const = importlib.import_module(f"{PACKAGE_NAME}.const")
entity_values = importlib.import_module(f"{PACKAGE_NAME}.entity_values")
flow = importlib.import_module(f"{PACKAGE_NAME}.flow_logic")


# --------------------------------------------------------------------------- #
# Which entities move
# --------------------------------------------------------------------------- #


class TestBindableKeys:
    """Only physical quantities of the load move onto the real device."""

    def test_power_and_energy_are_bindable(self) -> None:
        assert frozenset({"power", "energy"}) == entity_values.BINDABLE_KEYS

    def test_every_bindable_key_is_a_real_device_sensor(self) -> None:
        entity_defs = importlib.import_module(f"{PACKAGE_NAME}.entity_defs")
        assert set(entity_defs.DEVICE_SENSORS) >= entity_values.BINDABLE_KEYS

    def test_provenance_entities_stay_on_the_synthetic_device(self) -> None:
        """A model's self-assessment must never look like device telemetry."""
        for key in (
            "source",
            "confidence",
            "confidence_tier",
            "sample_count",
            "model_validity",
            "operating_stage",
            "last_inference",
            "data_quality",
        ):
            assert key not in entity_values.BINDABLE_KEYS

    def test_the_sensor_uses_the_shared_rule(self) -> None:
        """The rule lives in one place; the entity must not re-derive it."""
        source = (PACKAGE_DIR / "sensor.py").read_text()
        assert "BINDABLE_KEYS" in source
        # The old single-key gate must be gone from the *binding* decisions.
        # ``self._key == "energy"`` still legitimately guards state restoration,
        # so only the binding call site is checked.
        assert "if self._key in BINDABLE_KEYS:\n            self._adopt_registered_device()" in source
        target_start = source.index("def _binding_target(")
        target_end = source.index("def _bind_real_device(", target_start)
        body = source[target_start:target_end]
        assert 'self._key != "energy"' not in body
        assert "if self._key not in BINDABLE_KEYS:" in body


# --------------------------------------------------------------------------- #
# Explicit link vs entity-derived resolution
# --------------------------------------------------------------------------- #


class TestExplicitDeviceLink:
    def test_absent_link_is_not_linked(self) -> None:
        assert entity_values.explicit_device_link(None, "d1") is None
        assert entity_values.explicit_device_link({}, "d1") is None
        assert entity_values.explicit_device_link({"d2": "ha_b"}, "d1") is None

    def test_a_declared_link_is_returned(self) -> None:
        assert (
            entity_values.explicit_device_link({"d1": "ha_device"}, "d1")
            == "ha_device"
        )

    def test_blank_or_non_string_values_are_ignored(self) -> None:
        """A half-written options entry must not detach a measurement."""
        for value in ("", "   ", None, 42, ["ha"]):
            assert entity_values.explicit_device_link({"d1": value}, "d1") is None

    def test_whitespace_is_trimmed(self) -> None:
        assert (
            entity_values.explicit_device_link({"d1": "  ha_device  "}, "d1")
            == "ha_device"
        )

    def test_a_link_cannot_point_at_its_own_synthetic_device(self) -> None:
        """``binding_update`` refuses a self-move, so a self-link is inert."""
        assert entity_values.binding_update("own", "own") is None


# --------------------------------------------------------------------------- #
# Form logic
# --------------------------------------------------------------------------- #


class TestDeviceLinkChoice:
    def test_absent_field_is_no_opinion(self) -> None:
        assert flow.device_link_choice({}) is None
        assert flow.device_link_choice(None) is None

    def test_explicit_value_is_the_choice(self) -> None:
        assert flow.device_link_choice({"ha_device_id": "dev-1"}) == "dev-1"

    def test_empty_string_means_unlink(self) -> None:
        """Distinct from "absent": the form always submits the selector."""
        assert flow.device_link_choice({"ha_device_id": ""}) == ""
        assert flow.device_link_choice({"ha_device_id": None}) == ""

    def test_asymmetric_column_labels_cover_both_sides(self) -> None:
        """Broad and narrow labels both map back to the right column."""
        assert flow.device_link_choice({"ha_device_id": "a"}) == "a"
        assert flow.device_link_choice({"ha_device_id": "b"}) == "b"


class TestApplyDeviceLink:
    def test_setting_one_link_preserves_the_others(self) -> None:
        """HA replaces ``options`` wholesale; a partial map detaches devices."""
        applied = flow.apply_device_link({"d1": "ha1", "d2": "ha2"}, "d1", "ha9")
        assert applied == {"d1": "ha9", "d2": "ha2"}

    def test_removing_one_link_preserves_the_others(self) -> None:
        assert flow.apply_device_link({"d1": "ha1", "d2": "ha2"}, "d1", "") == {
            "d2": "ha2"
        }

    def test_no_opinion_leaves_the_map_alone(self) -> None:
        assert flow.apply_device_link({"d1": "ha1"}, "d1", None) == {"d1": "ha1"}

    def test_a_new_link_is_added(self) -> None:
        assert flow.apply_device_link({}, "d1", "ha1") == {"d1": "ha1"}

    def test_malformed_entries_are_dropped_not_propagated(self) -> None:
        applied = flow.apply_device_link(
            {"d1": "", "d2": None, "d3": "ha3"}, "d4", "ha4"
        )
        assert applied == {"d3": "ha3", "d4": "ha4"}

    def test_removing_an_absent_link_is_a_noop(self) -> None:
        assert flow.apply_device_link({}, "d1", "") == {}

    def test_stored_link_is_reported_for_the_form_default(self) -> None:
        assert flow.linked_device_id({"d1": "ha1"}, "d1") == "ha1"
        assert flow.linked_device_id({"d2": "ha1"}, "d1") == ""
        assert flow.linked_device_id(None, "d1") == ""
        assert flow.linked_device_id({"d1": None}, "d1") == ""

    def test_round_trip_through_the_form_helpers(self) -> None:
        stored = flow.apply_device_link({}, "d1", "ha1")
        assert flow.linked_device_id(stored, "d1") == "ha1"
        cleared = flow.apply_device_link(stored, "d1", flow.device_link_choice(
            {"ha_device_id": ""}
        ))
        assert cleared == {}


class TestDeviceLinkEstablished:
    def test_a_real_link_is_established(self) -> None:
        assert flow.device_link_established("d1", "ha1") is True

    def test_blank_sides_are_not_a_link(self) -> None:
        assert flow.device_link_established("", "ha1") is False
        assert flow.device_link_established("d1", "") is False
        assert flow.device_link_established("   ", "ha1") is False
        assert flow.device_link_established("d1", "   ") is False


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #


class TestLinkPersistence:
    """The config-entry shape, checked against the shipped source."""

    def _options_flow_source(self) -> str:
        source = (PACKAGE_DIR / "config_flow.py").read_text()
        return source[source.index("class WattMosaicOptionsFlow") :]

    def test_the_key_is_defined_where_the_coordinator_reads_it(self) -> None:
        assert const.CONF_DEVICE_LINKS == "device_links"
        coordinator = (PACKAGE_DIR / "coordinator.py").read_text()
        assert "explicit_device_link(" in coordinator
        assert "CONF_DEVICE_LINKS" in coordinator

    def test_the_v3_options_flow_preserves_all_stored_options(self) -> None:
        """HA replaces ``options`` wholesale; the v3 flow keeps every map.

        The per-device editor that used to rebuild the link map is gone (devices
        are automatic, DEC-04).  The single v3 completion path now rebuilds save
        data from the whole stored config, so ``device_links`` survives a settings
        save just as the energy choices do.
        """
        page = self._options_flow_source()
        assert "data={**config, **self._settings}" in page
        # The removed helper must not linger: there is no per-device form left.
        assert "def _merged_device_links(" not in page

    def test_the_link_is_never_sent_to_the_engine(self) -> None:
        """An HA device id is a display decision, not electrical knowledge."""
        params = flow.normalise_device_configuration(
            {"device_type": "pump", "ha_device_id": "dev-1"}
        )
        assert "ha_device_id" not in params
        assert params == {"device_type": "pump"}

    def test_a_link_only_submission_is_savable(self) -> None:
        """Linking alone must not be rejected as "nothing to save"."""
        form = {
            "device_type": "unknown",
            "connectivity": "unknown",
            "behavior": "unknown",
            "create_energy_sensor": True,
            "ha_device_id": "dev-1",
        }
        assert flow.validate_device_configuration(form) == {}
        assert flow.normalise_device_configuration(form) == {}
        assert flow.device_link_choice(form) == "dev-1"


# --------------------------------------------------------------------------- #
# Binding precedence in the shipped sensor
# --------------------------------------------------------------------------- #


class TestBindingPrecedenceInSource:
    def _binding_target_body(self) -> str:
        source = (PACKAGE_DIR / "sensor.py").read_text()
        start = source.index("def _binding_target(")
        end = source.index("def _bind_real_device(", start)
        return source[start:end]

    def test_explicit_link_is_resolved_before_entity_fallback(self) -> None:
        body = self._binding_target_body()
        explicit = body.index("explicit_device_link(")
        fallback = body.index("binding_device_entity_ids(")
        assert explicit < fallback, (
            "an explicit declaration must win over an entity's registry entry"
        )
        assert "if explicit is not None:" in body

    def test_diagnostics_are_excluded_before_any_resolution(self) -> None:
        body = self._binding_target_body()
        assert "BINDABLE_KEYS" in body
        gate = body.index("BINDABLE_KEYS")
        assert gate < body.index("explicit_device_link(")

    def test_the_docstring_does_not_promise_energy_only(self) -> None:
        body = self._binding_target_body()
        assert "Only energy is bound" not in body


# --------------------------------------------------------------------------- #
# The candidate list must not be a dead end
# --------------------------------------------------------------------------- #


class TestLinkCandidatesAreReachable:
    """Device links are now written through the v3 entity-link API.

    Z2d removed the v2 ``_link_candidates`` picker (and the whole per-device
    form that hosted it).  A plug's single-subject scope is captured on the v3
    endpoints step and its state entities are written as ``ha_entity_links``
    with ``declared_by=user`` (DEC-08).  The pure link rules remain testable
    without Home Assistant.
    """

    def test_v3_form_has_no_removed_link_picker(self) -> None:
        source = (PACKAGE_DIR / "config_flow.py").read_text()
        for removed in (
            "def _link_candidates(",
            "def _device_config_finish(",
            "async_step_device_config_details",
        ):
            assert removed not in source, removed

    def test_v3_entity_links_declare_the_user_as_source(self) -> None:
        sl = importlib.import_module(f"{PACKAGE_NAME}.state_links")
        resolution = sl.LinkResolution(state_entities={"dev_kettle": ("switch.kettle",)})
        bodies = sl.build_ha_entity_link_bodies(resolution)
        assert bodies == [
            {
                "entity_id": "switch.kettle",
                "target_kind": "device",
                "target_id": "dev_kettle",
                "role": "state",
                "declared_by": "user",
            }
        ]

    def test_a_stored_link_is_not_dropped_by_the_flow(self) -> None:
        """The merge helper still rebuilds the whole map for existing entries."""
        stored = flow.apply_device_link({"d1": "ha1", "d2": "ha2"}, "d1", None)
        assert stored == {"d1": "ha1", "d2": "ha2"}
        assert flow.linked_device_id(stored, "d2") == "ha2"


class TestUnlinkReturnsTheEntity:
    """Clearing a link must move the entity back, not just stop moving it.

    Binding is the only thing that writes ``device_id``, so a no-op on "no
    target" would pin the entity to the old real device forever after the user
    unlinked it or deleted the state entity that justified the binding.
    """

    def _binding_body(self) -> str:
        source = (PACKAGE_DIR / "sensor.py").read_text()
        start = source.index("def _bind_real_device(")
        end = source.index("async def async_added_to_hass(", start)
        return source[start:end]

    def test_no_target_triggers_a_return(self) -> None:
        body = self._binding_body()
        assert "if target is None:" in body
        assert "_return_to_own_device()" in body

    def test_the_return_is_registry_keyed_and_never_renames(self) -> None:
        source = (PACKAGE_DIR / "sensor.py").read_text()
        start = source.index("def _return_to_own_device(")
        end = source.index("async def async_added_to_hass(", start)
        body = source[start:end]
        assert "async_update_entity(self.entity_id, device_id=own)" in body
        assert "new_entity_id" not in body
        assert "entity_id=" not in body

    def test_the_return_is_idempotent(self) -> None:
        source = (PACKAGE_DIR / "sensor.py").read_text()
        start = source.index("def _return_to_own_device(")
        end = source.index("async def async_added_to_hass(", start)
        body = source[start:end]
        assert "if own is None or current.device_id == own:" in body

    def test_the_coordinator_resolves_our_own_device(self) -> None:
        coordinator = (PACKAGE_DIR / "coordinator.py").read_text()
        assert "def device_id_for_identifier(" in coordinator
        assert "for device in registry.devices:" in coordinator
        assert "registry.devices.get(" not in coordinator


class TestStaleLinksAreValidated:
    """A stored link is a claim about a device that may have since vanished.

    Links live in the config entry indefinitely.  The device can be deleted, or
    replaced by a different registry entry with a new id, so the link must be
    re-validated against the registry on every start rather than trusted.
    """

    def _source(self) -> str:
        return (PACKAGE_DIR / "sensor.py").read_text()

    def test_the_declared_link_is_validated_before_use(self) -> None:
        source = self._source()
        start = source.index("def _binding_target(")
        end = source.index("def _bind_real_device(", start)
        body = source[start:end]
        assert "self._linkable(explicit)" in body

    def test_a_link_to_a_missing_device_is_rejected(self) -> None:
        source = self._source()
        start = source.index("def _linkable(")
        end = source.index("def _binding_target(", start)
        body = source[start:end]
        assert "if device_entry is None:" in body
        assert "return False" in body

    def test_a_link_to_our_own_device_is_rejected(self) -> None:
        source = self._source()
        start = source.index("def _linkable(")
        end = source.index("def _binding_target(", start)
        body = source[start:end]
        assert "DOMAIN" in body
        assert "identifiers" in body


# --------------------------------------------------------------------------- #
# Declaring which CTs meter a device (the shared-circuit case)
# --------------------------------------------------------------------------- #


class TestSharedCircuitIsExpressible:
    """A bank of dimmers on one circuit was unreachable through the product.

    The engine has always understood ``DeviceConfiguration.monitored_by_ct_ids``
    and its tests cover the single-ON-claimant rule, but nothing in the
    integration ever sent the field.  The channel wizard writes
    ``CTChannel.device_id``, which holds exactly one id, so the second lamp on
    the same circuit had nowhere to go -- the feature existed and could not be
    configured.  These tests pin the product path, not the engine behaviour
    (which ``test_device_attachment.py`` covers).
    """

    def test_the_form_emits_the_ct_list(self) -> None:
        parameters = flow.normalise_device_configuration(
            {
                "device_type": "light",
                "connectivity": "connected",
                "monitoring": "shared",
                "behavior": "variable",
                "state_entity_ids": "light.lamp_a",
                "monitored_by_ct_ids": "ct_lights",
            }
        )
        assert parameters["monitored_by_ct_ids"] == ["ct_lights"]
        assert parameters["monitoring"] == "shared"

    def test_several_cts_are_kept_in_order_and_deduplicated(self) -> None:
        parameters = flow.normalise_device_configuration(
            {"monitored_by_ct_ids": "ct_lights, ct_basement ,ct_lights"}
        )
        assert parameters["monitored_by_ct_ids"] == ["ct_lights", "ct_basement"]

    def test_an_absent_field_states_nothing(self) -> None:
        """A caller that does not know about the field must change nothing."""
        assert "monitored_by_ct_ids" not in flow.normalise_device_configuration(
            {"device_type": "light"}
        )

    def test_an_unchanged_selection_states_nothing(self) -> None:
        """The widget is pre-filled, so resubmitting it is not an instruction."""
        assert "monitored_by_ct_ids" not in flow.normalise_device_configuration(
            {"monitored_by_ct_ids": ["ct_lights"]}, stored_cts=["ct_lights"]
        )

    def test_clearing_the_selection_is_an_instruction(self) -> None:
        """A wrong declaration must be removable.

        The picker shows what is stored.  If empty were read as "no opinion" the
        declaration could never be undone -- the same dead end the device link
        had before ``_return_to_own_device``.
        """
        parameters = flow.normalise_device_configuration(
            {"monitored_by_ct_ids": []}, stored_cts=["ct_lights"]
        )
        assert parameters == {"monitored_by_ct_ids": []}

    def test_clearing_is_not_rejected_as_nothing_to_save(self) -> None:
        errors = flow.validate_device_configuration(
            {
                "device_type": "unknown",
                "connectivity": "unknown",
                "behavior": "unknown",
                "monitored_by_ct_ids": [],
            },
            stored_cts=["ct_lights"],
        )
        assert errors == {}

    def test_resubmitting_an_empty_unchanged_form_is_still_refused(self) -> None:
        errors = flow.validate_device_configuration(
            {
                "device_type": "unknown",
                "connectivity": "unknown",
                "behavior": "unknown",
                "monitored_by_ct_ids": [],
            },
            stored_cts=[],
        )
        assert errors.get("base") == "nothing_to_save"

    def test_an_entity_id_is_not_a_ct_id(self) -> None:
        """Accepting an entity id would declare a channel that does not exist.

        The engine drops unknown ct ids without an error, so the user would see
        a saved configuration and no change in behaviour.
        """
        errors = flow.validate_device_configuration(
            {
                "device_type": "light",
                "connectivity": "connected",
                "behavior": "variable",
                "monitored_by_ct_ids": "sensor.lights_power",
            }
        )
        assert errors.get("monitored_by_ct_ids") == "invalid_ct_id"

    def test_a_plain_ct_id_passes_validation(self) -> None:
        errors = flow.validate_device_configuration(
            {
                "device_type": "light",
                "connectivity": "connected",
                "behavior": "variable",
                "monitored_by_ct_ids": "lights_branch_ct",
            }
        )
        assert "monitored_by_ct_ids" not in errors

    def test_the_removed_channel_picker_is_gone(self) -> None:
        """The v2 CT multi-select lived on a per-device form that no longer exists.

        Shared-circuit declarations are now inferred by the engine; the setup
        flow only edits endpoints and their optional parents (DEC-04).
        """
        source = _config_flow_source()
        for removed in (
            "def _ct_options(",
            "def _stored_monitored_cts(",
            "async_step_device_config_details",
        ):
            assert removed not in source, removed

    def test_shared_cts_are_still_normalised_without_the_form(self) -> None:
        """The pure normaliser still accepts several CTs for one declaration."""
        params = flow.normalise_device_configuration(
            {"monitored_by_ct_ids": "ct_lights, ct_basement"}
        )
        assert params["monitored_by_ct_ids"] == ["ct_lights", "ct_basement"]

    def test_a_v3_endpoint_parent_is_a_user_declared_edge(self) -> None:
        """The v3 flow expresses a parent as a CONTAINS edge, not a form field."""
        endpoints = flow.normalise_endpoints(
            [
                {
                    "endpoint_id": "main_l1",
                    "kind": "ct",
                    "scope": "circuit",
                    "entity_id": "sensor.main_power",
                },
                {
                    "endpoint_id": "lights_l1",
                    "kind": "ct",
                    "scope": "circuit",
                    "entity_id": "sensor.lights_power",
                },
            ]
        )
        choices = flow.endpoint_choices(
            endpoints, {"lights_l1": {"parent_endpoint_id": "main_l1", "parent_accepted": True}}
        )
        edge = flow.build_user_edge_plan(choices)[0]
        assert edge["body"]["source"] == "main_l1"
        assert edge["body"]["target"] == "lights_l1"
        assert edge["body"]["declared_by"] == "user"

    def test_an_unaccepted_parent_is_never_written(self) -> None:
        """An inferred parent shown as a suggestion is not a declaration."""
        endpoints = flow.normalise_endpoints(
            [{"endpoint_id": "lights_l1", "kind": "ct", "scope": "circuit"}]
        )
        choices = flow.endpoint_choices(
            endpoints,
            {"lights_l1": {"parent_endpoint_id": "main_l1"}},
            suggestions={"lights_l1": ["main_l1"]},
        )
        assert choices["lights_l1"]["parent_accepted"] is False
        assert flow.build_user_edge_plan(choices) == []


def _config_flow_source() -> str:
    return (PACKAGE_DIR / "config_flow.py").read_text()
