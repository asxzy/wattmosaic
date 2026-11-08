"""Which HA entity actually reports a device's *runtime*? (ADR-0012)

The problem this solves
-----------------------
Home Assistant entities attached to a device are not interchangeable evidence.
In a typical installation:

* ``climate.hallway_thermostat`` reports ``heat_cool`` in **1845 of 1848**
  recorded rows.  That is the *setpoint mode*.  It says nothing about
  whether the compressor or the burner is running.
* ``water_heater.dhw_tank`` reports ``comfort`` in **239 of 249** rows,
  for the same reason.  The signal that actually tracked the CT was a
  ``binary_sensor`` zone heating entity.
* ``water_heater.pool_heater`` reports ``Heating``/``Cooling``/``Idle`` and
  *does* track load — but ``Cooling`` is ~22 W while ``Heating`` is ~4.5 kW, so
  the two are not equivalent "on".

Treating any of those as "the device is on" fabricates load.  This module makes
the discrimination explicit and declarative:

1. **A mode entity is never evidence of runtime.**  It is context only.
2. **A load entity is.**  It is named by the user (``state_entity_ids`` or
   ``controller.load_entity_ids``).
3. **Domain defaults are a last resort, and they are conservative.**  A
   ``binary_sensor`` with ``device_class`` in ``running``/``heat``/``occupancy``
   is a runtime signal; a ``climate``/``water_heater`` *entity state* is not
   (its ``hvac_action`` attribute would be, but this collector never sees
   attributes reliably enough to rely on it).

Nothing here consults a friendly name, an HA area or the registry.
"""

from __future__ import annotations

from dataclasses import dataclass

from wattmosaic_engine.contracts import DeviceType

#: Domains whose *state* describes a mode/setpoint rather than load.
MODE_ONLY_DOMAINS = frozenset(
    {
        "climate",
        "water_heater",
        "select",
        "input_select",
        "scene",
        "script",
        "automation",
        "button",
        "event",
        "update",
    }
)

#: Domains whose state normally *is* the device's runtime.
LOAD_DOMAINS = frozenset(
    {
        "binary_sensor",
        "light",
        "switch",
        "fan",
        "humidifier",
        "media_player",
        "vacuum",
        "valve",
        "cover",
        "input_boolean",
    }
)

#: ``binary_sensor`` device classes that indicate active operation.
LOAD_DEVICE_CLASSES = frozenset(
    {
        "running",
        "heat",
        "cool",
        "heating",
        "cooling",
        "power",
        "plug",
        "motion",
        "occupancy",
        "opening",
    }
)

#: Device types for which a climate-like entity is *expected* to be a mode.
COMPLEX_DEVICE_TYPES = frozenset(
    {
        DeviceType.HVAC_HEAT,
        DeviceType.HVAC_COOL,
        DeviceType.HVAC_COMBINED,
        DeviceType.WATER_HEATER,
        DeviceType.SAUNA,
        DeviceType.HOT_TUB,
    }
)


@dataclass(slots=True, frozen=True)
class EntityEvidence:
    """One candidate entity, judged for its evidentiary role."""

    entity_id: str
    role: str  # "load" | "mode" | "unknown"
    reason: str

    @property
    def is_load(self) -> bool:
        return self.role == "load"


def domain_of(entity_id: str | None) -> str:
    if not entity_id or "." not in entity_id:
        return ""
    return entity_id.split(".", 1)[0].lower()


def classify_entity(
    entity_id: str,
    *,
    device_class: str | None = None,
    declared_load: bool = False,
    declared_mode: bool = False,
) -> EntityEvidence:
    """Decide whether *this* entity may prove runtime.

    Explicit user declaration always wins, including the pathological case where
    a user correctly marks a ``climate`` entity as a load signal for a device
    whose own integration exposes runtime that way.
    """
    if declared_load:
        return EntityEvidence(entity_id, "load", "declared_by_user")
    if declared_mode:
        return EntityEvidence(entity_id, "mode", "declared_mode_by_user")

    domain = domain_of(entity_id)
    if domain in MODE_ONLY_DOMAINS:
        return EntityEvidence(
            entity_id,
            "mode",
            f"{domain}_state_is_a_mode_not_runtime",
        )
    if domain == "binary_sensor":
        dc = (device_class or "").strip().lower()
        if dc in LOAD_DEVICE_CLASSES or not dc:
            return EntityEvidence(
                entity_id,
                "load",
                f"binary_sensor_device_class_{dc or 'unset'}",
            )
        return EntityEvidence(entity_id, "unknown", f"binary_sensor_device_class_{dc}")
    if domain in LOAD_DOMAINS:
        return EntityEvidence(entity_id, "load", f"{domain}_state_is_runtime")
    return EntityEvidence(entity_id, "unknown", f"unrecognised_domain_{domain or 'none'}")


def select_runtime_entities(
    candidates: list[str],
    *,
    declared_load: list[str] | None = None,
    declared_mode: list[str] | None = None,
    device_types: dict[str, str | None] | None = None,
    device_classes: dict[str, str | None] | None = None,
) -> tuple[list[EntityEvidence], list[EntityEvidence]]:
    """Split candidates into ``(load_evidence, mode_context)``.

    Order is preserved so the caller's declaration order remains meaningful when
    two runtime signals disagree.
    """
    load_set = {e for e in (declared_load or [])}
    mode_set = {e for e in (declared_mode or [])}
    device_classes = device_classes or {}
    loads: list[EntityEvidence] = []
    modes: list[EntityEvidence] = []
    for entity_id in candidates:
        evidence = classify_entity(
            entity_id,
            device_class=device_classes.get(entity_id),
            declared_load=entity_id in load_set,
            declared_mode=entity_id in mode_set,
        )
        if evidence.is_load:
            loads.append(evidence)
        else:
            modes.append(evidence)
    return loads, modes


def is_mode_domain(entity_id: str | None) -> bool:
    return domain_of(entity_id) in MODE_ONLY_DOMAINS


__all__ = [
    "COMPLEX_DEVICE_TYPES",
    "EntityEvidence",
    "LOAD_DEVICE_CLASSES",
    "LOAD_DOMAINS",
    "MODE_ONLY_DOMAINS",
    "classify_entity",
    "domain_of",
    "is_mode_domain",
    "select_runtime_entities",
]
