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

    def test_every_options_completion_path_persists_the_link_map(self) -> None:
        page = self._options_flow_source()
        assert page.count("CONF_DEVICE_LINKS: self._merged_device_links()") == 2, (
            "both options-flow completion paths must rebuild the whole map"
        )

    def test_the_merge_helper_exists_exactly_once(self) -> None:
        page = self._options_flow_source()
        assert page.count("def _merged_device_links(") == 1

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
    """A device with no switch entity must still be linkable.

    A hot tub on its own CT commonly has no state entity at all -- the CT *is*
    how it is observed.  Restricting the picker to devices that own an entity the
    user declared made linking impossible in exactly the case the feature exists
    for, so the picker must also offer the rest of the registry.
    """

    def _body(self) -> str:
        source = (PACKAGE_DIR / "config_flow.py").read_text()
        start = source.index("def _link_candidates(")
        end = source.index("def _device_config_finish(", start)
        return source[start:end]

    def test_the_registry_is_enumerated_beyond_declared_entities(self) -> None:
        body = self._body()
        assert "binding_device_entity_ids(" in body
        assert "for device_entry in device_reg.devices:" in body

    def test_the_registry_view_is_iterated_not_used_as_a_mapping(self) -> None:
        """The mapping-style view is deprecated for custom integrations."""
        body = self._body()
        assert "device_reg.devices.values()" not in body
        assert "device_reg.devices.get(" not in body

    def test_our_own_devices_are_never_offered(self) -> None:
        """A measurement must not be shown on the host that describes it."""
        body = self._body()
        assert "_owns_us(" in body
        assert "DOMAIN" in body

    def test_the_no_link_choice_comes_first(self) -> None:
        body = self._body()
        first = body.index("SelectOptionDict(")
        assert 'value=""' in body[first : first + 200]

    def test_a_stored_link_is_always_represented(self) -> None:
        """Saving another field must not silently drop an existing link."""
        body = self._body()
        assert "if current and not suggestion:" in body


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
