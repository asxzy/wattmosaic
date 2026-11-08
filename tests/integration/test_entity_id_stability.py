"""Entity identity must not depend on where the energy sensor is displayed.

**The defect this file pins down**

``v0.1.4`` attached the energy entity to the user's real Home Assistant device
by setting ``entity.device_entry`` in ``__init__``.  Home Assistant derives a
``has_entity_name`` entity's ``entity_id`` from *the device the entity is created
on*: the device name is prepended to the translated entity name.  So the same
engine device produced two different IDs depending on whether its declarations
happened to resolve to a real device:

* bindable   -> ``sensor.<real device>_energy``
* not bindable -> ``sensor.<engine device>_energy``

A device that became bindable *after* its first start therefore changed its
entity ID.  Home Assistant only migrates long-term statistics when an entity is
explicitly renamed; re-deriving the ID while creating the registry entry does
not.  Every existing energy statistic for that device was silently orphaned.

**How this file tests it**

``homeassistant`` is not installed here, so the relevant Home Assistant
functions are transcribed from the 2026.11.0 sources, with the file and line
ranges named at each function.  The transcription is narrow — it models only
what decides our entity IDs — and it is used to drive a model of the entity
registry and of Home Assistant's statistics keys.

The tests then assert the *outcome a user observes*: one stable ID and one
surviving statistics row per engine device, regardless of binding.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

import pytest

# --------------------------------------------------------------------------- #
# Transcription of the Home Assistant code that decides our entity IDs
# --------------------------------------------------------------------------- #
#
# Sources (Home Assistant 2026.11.0):
#   helpers/entity_platform.py  _async_derive_object_ids, _async_get_entity_device
#   helpers/entity_registry.py  async_get_or_create, _async_generate_entity_id,
#                               _async_get_full_entity_name, async_get_available_entity_id
#   helpers/entity.py           Entity.suggested_object_id
#   util/__init__.py            slugify
#
# Deliberately excluded (they do not change a ``has_entity_name`` sensor whose
# integration sets neither ``entity_id`` nor ``suggested_object_id``): floor
# names, child-device parenting, user renames, and translation loading.  The
# translation is passed in as the already-resolved entity name.


def ha_slugify(text: str) -> str:
    """``homeassistant.util.slugify`` with the default separator."""
    if not text:
        return ""
    normalised = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in normalised if not unicodedata.combining(c))
    slug = re.sub(r"[^a-z0-9_]+", "_", stripped.lower()).strip("_")
    return slug or "unknown"


@dataclass(frozen=True)
class Device:
    """The subset of a device-registry entry that names an entity."""

    id: str
    name: str
    area: str | None = None


@dataclass(frozen=True)
class EntityAdd:
    """What the integration hands to ``async_add_entity``.

    ``device`` is the device the entity is created on.  ``entity_id`` mirrors
    ``Entity.entity_id``: Home Assistant treats a value set before the entity is
    added as an integration-chosen ID and does not prefix it.
    """

    unique_id: str
    entity_name: str  # the resolved translation of the entity's translation_key
    device: Device | None
    has_entity_name: bool = True
    entity_id: str | None = None
    #: ``Entity.device_entry`` — used only when ``device_info`` is absent.
    device_entry: Device | None = None
    #: ``Entity._attr_device_info`` — a non-None value wins over ``device_entry``.
    device_info: Device | None = None


@dataclass
class Registry:
    """Entity registry, keyed by entity_id and by (domain, platform, unique_id)."""

    entity_ids: dict[str, str] = field(default_factory=dict)  # unique_id -> entity_id
    devices: dict[str, Device | None] = field(default_factory=dict)  # entity_id -> device

    def get_entity_id(self, unique_id: str) -> str | None:
        return self.entity_ids.get(unique_id)

    def is_available(self, entity_id: str) -> bool:
        return entity_id not in self.devices

    def reserve(self, unique_id: str, entity_id: str, device: Device | None) -> None:
        self.entity_ids[unique_id] = entity_id
        self.devices[entity_id] = device


def resolve_device(add: EntityAdd) -> Device | None:
    """``EntityPlatform._async_get_entity_device``.

    ``device_info`` gets-or-creates a device and wins; ``device_entry`` is used
    only when there is no device info (``device_info`` returning ``None``).
    """
    if add.device_info is not None:
        return add.device_info
    return add.device_entry


def derive_object_ids(add: EntityAdd) -> tuple[str | None, str | None]:
    """``_async_derive_object_ids`` — returns ``(suggested, object_id_base)``.

    An integration that sets ``entity.entity_id`` before adding gets
    ``suggested_object_id`` (never device-prefixed).  Otherwise the entity's own
    name becomes ``object_id_base``, which *is* prefixed by the device name when
    ``has_entity_name`` is true.
    """
    if add.entity_id is not None:
        return add.entity_id.split(".", 1)[1], None
    return None, add.entity_name


def full_entity_name(
    *,
    device: Device | None,
    entity_name: str,
    overridden_name: str | None,
    area_id: str | None,
) -> str:
    """``_async_get_full_entity_name`` for the default ``entity_id_parts``.

    Default parts are ``(AREA, PARENT_DEVICE, DEVICE, ENTITY)``.  Parent-device
    naming only applies to ``ChildDeviceEntry``; the WattMosaic synthetic
    "Inferred load" devices are main devices, so that part is always empty here.
    """
    if overridden_name is not None:
        return overridden_name

    part_names: dict[str, str | None] = {"AREA": None, "DEVICE": None, "ENTITY": entity_name}
    if device is not None:
        part_names["DEVICE"] = device.name
        if area_id is None:
            area_id = device.area
    if area_id is not None:
        part_names["AREA"] = area_id
    return " ".join(name for name in part_names.values() if name)


def generate_entity_id(
    add: EntityAdd,
    device: Device | None,
    registry: Registry,
) -> str:
    """``_async_generate_entity_id`` + ``async_get_available_entity_id``."""
    suggested, base = derive_object_ids(add)
    object_id = full_entity_name(
        device=device,
        entity_name=base or "",
        overridden_name=suggested,
        area_id=None,
    )
    preferred = f"sensor.{ha_slugify(object_id)}"
    candidate = preferred
    suffix = 1
    while not registry.is_available(candidate):
        suffix += 1
        candidate = f"{preferred}_{suffix}"
    return candidate


def add_entity(add: EntityAdd, registry: Registry) -> str:
    """``_async_add_entity`` for an entity with a ``unique_id``.

    Order matters and is the whole point: the device is resolved *before* the
    entity ID is derived, so a device attached at create time changes the ID.
    """
    existing = registry.get_entity_id(add.unique_id)
    if existing is not None:
        # Already registered: HA keeps the existing entity_id and only updates
        # the stored device (``_async_update_entity`` with ``device_id``).
        registry.devices[existing] = resolve_device(add)
        return existing
    device = resolve_device(add)
    entity_id = generate_entity_id(add, device, registry)
    registry.reserve(add.unique_id, entity_id, device)
    return entity_id


# --------------------------------------------------------------------------- #
# Statistics model
# --------------------------------------------------------------------------- #
#
# Recorder sources (2026.11.0):
#   components/recorder/entity_registry.py  _async_entity_id_changed
#   components/recorder/statistics.py       async_update_statistics_metadata
#
# Statistics rows are keyed by statistic_id == entity_id.  A rename migrates
# them.  Removing a config entry deletes the registry entries but leaves the
# statistics behind, so the next add re-derives an ID and starts a *new* series.


class Statistics:
    """Long-term statistics keyed by ``statistic_id`` (== entity_id)."""

    def __init__(self) -> None:
        self.sums: dict[str, float] = {}

    def record(self, entity_id: str, kwh: float) -> None:
        self.sums[entity_id] = self.sums.get(entity_id, 0.0) + kwh

    def rename(self, old: str, new: str) -> None:
        """Migrate on an explicit rename; a no-op if nothing was recorded."""
        if old in self.sums:
            self.sums[new] = self.sums.pop(old)


# --------------------------------------------------------------------------- #
# Fixtures describing the real installation's shape
# --------------------------------------------------------------------------- #

ENGINE_DEVICE = "water_heater"
ENGINE_NAME = "Water Heater"  # engine-reported device name -> synthetic device name
REAL_DEVICE = Device(id="device_real_water_heater", name="Water Heater Thermostat")
SYNTHETIC = Device(id="device_wm_water_heater", name=ENGINE_NAME)
UNIQUE_ID = f"entry123_{ENGINE_DEVICE}_energy"
ENTITY_NAME = "Energy"


def _add(
    device: Device | None,
    *,
    via: str = "device_info",
) -> EntityAdd:
    """A device sensor created on ``device`` the way ``sensor.py`` does it.

    ``via="device_info"`` is the shipped code path: the entity carries
    ``_attr_device_info`` for the synthetic "Inferred load" device.
    ``via="device_entry"`` reproduces the broken v0.1.4 binding, which set
    ``entity.device_entry`` to the real device in ``__init__``.
    """
    kwargs = {via: device}
    return EntityAdd(
        unique_id=UNIQUE_ID, entity_name=ENTITY_NAME, device=device, **kwargs
    )


# --------------------------------------------------------------------------- #
# The regression
# --------------------------------------------------------------------------- #


class TestEntityIdDependsOnCreationDevice:
    """Why binding *before* registration orphans statistics."""

    def test_creating_on_the_real_device_changes_the_id(self) -> None:
        registry = Registry()
        on_synthetic = add_entity(_add(SYNTHETIC), registry)
        assert on_synthetic == "sensor.water_heater_energy"

        other = Registry()
        on_real = add_entity(_add(REAL_DEVICE, via="device_entry"), other)
        assert on_real == "sensor.water_heater_thermostat_energy"
        assert on_real != on_synthetic

    def test_a_device_becoming_bindable_moves_the_series(self) -> None:
        """The measured failure: a device bound after its first start.

        Start 1: ``state_entity_ids`` resolves to nothing, so the energy sensor
        is created on the synthetic device and records 0.47 kWh.  The user then
        links a real device.  Start 2: the old code created the entity on the
        real device, so HA derived a different ID and the accumulated energy was
        left behind under the old statistic_id.
        """
        stats = Statistics()
        registry = Registry()

        first = add_entity(_add(SYNTHETIC), registry)
        stats.record(first, 0.47)
        assert first == "sensor.water_heater_energy"

        # The entity registry entry is gone (the config entry was removed, or
        # this is a brand-new install) while statistics survive.
        registry = Registry()
        second = add_entity(_add(REAL_DEVICE, via="device_entry"), registry)

        assert second == "sensor.water_heater_thermostat_energy"
        # Two series for one physical load: the old one is orphaned.
        assert stats.sums == {"sensor.water_heater_energy": pytest.approx(0.47)}
        assert second not in stats.sums


class TestStableIdentityAfterTheFix:
    """The shipped behaviour: identity is derived without the binding result."""

    def test_id_is_identical_whether_or_not_binding_resolves(self) -> None:
        """``_bind_real_device`` runs after registration, so it cannot matter."""
        bound = Registry()
        # Created on the synthetic device (always), then bound afterwards.
        bound_id = add_entity(_add(SYNTHETIC), bound)
        bound.devices[bound_id] = REAL_DEVICE  # the post-registration move

        unbound = Registry()
        unbound_id = add_entity(_add(SYNTHETIC), unbound)

        assert bound_id == unbound_id == "sensor.water_heater_energy"
        # Only the device changed; the ID did not.
        assert bound.devices[bound_id] == REAL_DEVICE

    def test_statistics_survive_a_late_binding(self) -> None:
        stats = Statistics()
        registry = Registry()

        first = add_entity(_add(SYNTHETIC), registry)
        stats.record(first, 0.47)

        # Bind after registration, exactly like ``async_added_to_hass``.
        registry.devices[first] = REAL_DEVICE

        second = add_entity(_add(SYNTHETIC), registry)
        stats.record(second, 0.03)

        assert first == second
        assert stats.sums == {"sensor.water_heater_energy": pytest.approx(0.50)}
    def test_reload_keeps_the_stored_id(self) -> None:
        """A reload must not re-derive: the unique id already owns an ID.

        This is also why ``_adopt_registered_device`` exists — without adopting
        the stored device, every reload moves the entity back and forth.
        """
        registry = Registry()
        first = add_entity(_add(SYNTHETIC), registry)
        registry.devices[first] = REAL_DEVICE

        # Start again.  Historical HA answered with the registered ID only
        # because the integration kept the device at create time; the entry is
        # still authoritative either way.
        again = add_entity(_add(SYNTHETIC), registry)
        assert again == first


class TestTheModelMatchesTheShippedSource:
    """The model above is only evidence if the shipped code follows it.

    ``test_entity_id_stability.py`` cannot import ``sensor.py`` (it imports Home
    Assistant), so the model and the code could silently drift apart: the model
    would keep proving a property the integration no longer has.  These tests
    read ``sensor.py`` and drive the model with what it actually does, so
    reinstating the ``__init__``-time binding fails here too.
    """

    @staticmethod
    def _sensor_tree():
        import ast
        from pathlib import Path

        source = (
            Path(__file__).resolve().parents[2]
            / "custom_components"
            / "wattmosaic"
            / "sensor.py"
        ).read_text()
        return ast.parse(source), source

    def _init_assignment(self) -> tuple[bool, bool]:
        """``(sets_device_info, sets_device_entry)`` in ``__init__``."""
        import ast

        tree, source = self._sensor_tree()
        cls = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "WattMosaicDeviceSensor"
        )
        init = next(
            node
            for node in cls.body
            if isinstance(node, ast.FunctionDef) and node.name == "__init__"
        )
        device_info = device_entry = False
        for node in ast.walk(init):
            if not isinstance(node, ast.Attribute) or not isinstance(
                node.ctx, ast.Store
            ):
                continue
            if not (isinstance(node.value, ast.Name) and node.value.id == "self"):
                continue
            if node.attr == "_attr_device_info":
                device_info = True
            if node.attr == "device_entry":
                device_entry = True
        assert source  # the file was read
        return device_info, device_entry

    def test_init_creates_on_the_synthetic_device_only(self) -> None:
        """``__init__`` must set ``device_info`` and must not set ``device_entry``.

        Setting ``device_entry`` there is exactly the v0.1.4 defect: it wins over
        nothing but is read by HA when ``device_info`` is absent, and it made the
        derived ID depend on whether a real device happened to resolve.
        """
        sets_info, sets_entry = self._init_assignment()
        assert sets_info, "__init__ must always set _attr_device_info"
        assert not sets_entry, (
            "`self.device_entry` in __init__ binds before registration and makes "
            "the entity ID depend on the binding outcome (ADR-0021)"
        )

    def test_the_model_reproduces_the_shipped_creation_path(self) -> None:
        """Drive the model with ``__init__``'s actual choice of device source."""
        sets_info, sets_entry = self._init_assignment()
        # Mirror ``__init__``: device_info for the synthetic device, plus a
        # device_entry only if the code sets one (it must not).
        add = _add(SYNTHETIC, via="device_info" if sets_info else "device_entry")

        registry = Registry()
        entity_id = add_entity(add, registry)
        assert entity_id == "sensor.water_heater_energy"

        # The post-registration move is the only thing binding may do.
        registry.devices[entity_id] = REAL_DEVICE
        assert registry.devices[entity_id] == REAL_DEVICE

        # On reload, ``_adopt_registered_device`` makes ``__init__`` use the
        # stored device (``device_info=None``, ``device_entry``=stored), which is
        # then written back unchanged rather than flapping to the synthetic one.
        reloaded = EntityAdd(
            unique_id=UNIQUE_ID,
            entity_name=ENTITY_NAME,
            device=REAL_DEVICE,
            device_info=None,
            device_entry=REAL_DEVICE,
        )
        assert add_entity(reloaded, registry) == entity_id
        assert registry.devices[entity_id] == REAL_DEVICE

    def test_the_move_is_inside_async_added_to_hass(self) -> None:
        """``_bind_real_device`` must not be reachable from ``__init__``."""
        import ast

        tree, _ = self._sensor_tree()
        cls = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "WattMosaicDeviceSensor"
        )
        methods = {
            node.name: node
            for node in cls.body
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        init_calls = {
            node.func.attr
            for node in ast.walk(methods["__init__"])
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
        }
        assert "_bind_real_device" not in init_calls

        async_added = methods["async_added_to_hass"]
        added_calls = {
            node.func.attr
            for node in ast.walk(async_added)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
        }
        assert "_bind_real_device" in added_calls

    def test_adoption_is_guarded_by_an_existing_registry_entry(self) -> None:
        """Adoption must return before touching ``device_entry`` when new.

        For an entity HA has not registered, ``entity_id`` is still being
        derived; adopting a device at that moment would reintroduce the defect.
        """
        import ast

        tree, _ = self._sensor_tree()
        cls = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "WattMosaicDeviceSensor"
        )
        adopt = next(
            node
            for node in cls.body
            if isinstance(node, ast.FunctionDef) and node.name == "_adopt_registered_device"
        )
        called = {
            node.func.attr
            for node in ast.walk(adopt)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert "async_get_entity_id" in called
        # Every path that assigns ``device_entry`` comes after that lookup.
        assigns = [
            node.lineno
            for node in ast.walk(adopt)
            if isinstance(node, ast.Attribute)
            and node.attr == "device_entry"
            and isinstance(node.ctx, ast.Store)
        ]
        lookups = [
            node.lineno
            for node in ast.walk(adopt)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "async_get_entity_id"
        ]
        assert assigns and lookups
        assert min(assigns) > max(lookups)


class TestBindingCannotRename:
    """``binding_update`` is the only writer; it must never carry a rename."""

    @pytest.fixture(scope="class")
    def binding_update(self):
        import importlib

        from tests.integration.test_ha_integration import PACKAGE_NAME

        return importlib.import_module(f"{PACKAGE_NAME}.entity_values").binding_update

    def test_payload_is_device_only(self, binding_update) -> None:
        payload = binding_update("device_a", "device_b")
        assert payload == {"device_id": "device_b"}

    @pytest.mark.parametrize("key", ["entity_id", "new_entity_id", "name", "area_id"])
    def test_payload_carries_no_identity_change(self, binding_update, key) -> None:
        assert key not in binding_update("device_a", "device_b")

    def test_no_op_cases(self, binding_update) -> None:
        assert binding_update("device_a", "device_a") is None
        assert binding_update("device_a", None) is None
        assert binding_update("device_a", "") is None
