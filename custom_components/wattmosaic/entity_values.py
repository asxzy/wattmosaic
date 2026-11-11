"""Pure mapping from normalised engine data to entity values.

No Home Assistant imports.  ``sensor.py``/``binary_sensor.py`` call these
functions and only add HA plumbing (device registry, units, availability).
Tested directly by ``tests/integration/test_ha_integration.py``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from .util import (
    as_bool,
    as_float,
    as_int,
    as_percentage,
    device_entities_available,
    device_is_running,
    is_stale,
    model_validity,
    parse_datetime,
)


def device_values(
    result: Mapping[str, Any] | None,
    *,
    engine_available: bool,
    now: datetime | None,
    stale_after: float,
) -> dict[str, Any]:
    """All per-device entity values, or ``None`` when not publishable.

    The returned dict is keyed by the sensor keys in ``const.DEVICE_SENSOR_KEYS``
    plus ``running``.  A ``None`` value means "unknown" (the entity stays
    available but without a state); ``available=False`` means the entity must
    be ``unavailable``.
    """
    available = device_entities_available(
        result_present=result is not None,
        result_timestamp=result.get("timestamp") if result else None,
        quality_status=result.get("quality_status") if result else None,
        engine_available=engine_available,
        now=now,
        stale_after=stale_after,
    )
    if result is None:
        return {"available": False, "running_available": False}

    quality = str(result.get("quality_status") or "missing")
    # `running` is a binary state, so it is allowed to be published even when
    # quality is "degraded"; but never from a stale/unknown result.
    running_available = available

    return {
        "available": available,
        "running_available": running_available,
        "power": round(as_float(result.get("power_w")), 3)
        if as_float(result.get("power_w")) is not None
        else None,
        "energy": as_float(result.get("energy_kwh")),
        "source": result.get("source") or "unknown",
        "confidence": as_percentage(result.get("confidence")),
        "confidence_tier": result.get("confidence_tier") or "tier_0_unknown",
        "sample_count": result.get("sample_count") or 0,
        "model_validity": model_validity(result),
        "operating_stage": result.get("operating_stage"),
        "last_inference": result.get("timestamp"),
        "data_quality": quality,
        "running": device_is_running(result.get("power_w"), result.get("source")),
    }


def learning_values(
    health: Mapping[str, Any] | None,
    *,
    engine_available: bool,
) -> dict[str, Any]:
    """Honest learning state for the ``active_learning`` binary sensor.

    ``learning_active`` used to be "a learning loop exists".  A live install
    reported it ON while the topology was a ``candidate``: every sample was
    rejected as ``ambiguous_topology``, no model was ever promoted, and
    ``model_estimated_power`` stayed at 0 W forever.  That is the difference
    between *running* and *learning*, so the binary state now derives from the
    engine's ``effective_state``:

    * ``on``      only when ``effective_state == "learning"`` (samples accepted);
    * ``off``     when ``blocked`` (running, rejecting every sample) or ``off``.

    An engine that predates the accounting (no ``details.learning_status``)
    falls back to its own ``learning_active`` flag; ``accounting_available``
    says which happened, so the attribute set is never ambiguous.

    Pure Python: no Home Assistant import, tested directly.
    """
    health = health or {}
    details = health.get("details")
    status = details.get("learning_status") if isinstance(details, Mapping) else None
    loop_active = bool(health.get("learning_active", False)) if engine_available else False

    if not isinstance(status, Mapping):
        # Legacy engine: the only signal is the loop flag.  Never *invent* a
        # blocked verdict from missing data; say plainly that the accounting is
        # unavailable so a dashboard can show the difference.
        return {
            "learning_active": loop_active,
            "effective_state": "learning" if loop_active else "off",
            "dominant_rejection_reason": None,
            "samples_seen": None,
            "samples_accepted": None,
            "samples_rejected": None,
            "acceptance_rate": None,
            "topology_quality": None,
            "learning_summary": (
                "The engine does not report learning accounting; "
                "the state is the loop flag only."
            ),
            "learning_remedy": None,
            "accounting_available": False,
        }

    effective = str(status.get("effective_state") or "").strip().lower()
    if effective not in ("learning", "blocked", "off"):
        effective = "learning" if loop_active else "off"
    if not engine_available:
        # Nothing is running while the engine is unreachable.  Keep the counts
        # (they are the last known values) but never claim learning.
        effective = "off"

    return {
        "learning_active": effective == "learning",
        "effective_state": effective,
        "dominant_rejection_reason": status.get("dominant_rejection_reason"),
        "samples_seen": as_int(status.get("samples_seen")),
        "samples_accepted": as_int(status.get("samples_accepted")),
        "samples_rejected": as_int(status.get("samples_rejected")),
        "acceptance_rate": as_percentage(status.get("acceptance_rate")),
        "topology_quality": status.get("topology_quality"),
        "learning_summary": status.get("summary"),
        "learning_remedy": status.get("remedy"),
        "accounting_available": True,
    }


def global_values(
    snapshot: Mapping[str, Any] | None,
    health: Mapping[str, Any] | None,
    *,
    engine_available: bool,
    now: datetime | None,
    stale_after: float,
) -> dict[str, Any]:
    """All global entity values plus availability flags."""
    reconciliation = (snapshot or {}).get("reconciliation") or {}
    quality = (snapshot or {}).get("quality") or {}
    health = health or {}

    timestamp = parse_datetime((snapshot or {}).get("timestamp"))
    stale = is_stale(timestamp, now=now, stale_after=stale_after)
    # The engine's own reconciliation timestamp can be newer than the envelope
    # when it has not recomputed; prefer the envelope timestamp for staleness.
    available = engine_available and not stale

    reconciliation_available = (
        available
        and bool(reconciliation.get("present"))
        and str(reconciliation.get("quality_status") or "").lower()
        not in ("stale", "missing", "bad")
    )

    engine_status = "offline"
    if engine_available:
        engine_status = str(health.get("status") or "ok").lower()
        if engine_status not in ("ok", "degraded", "error"):
            engine_status = "ok"

    learning = learning_values(health, engine_available=engine_available)

    return {
        "available": available,
        "reconciliation_available": reconciliation_available,
        "total_power": as_float(reconciliation.get("main_power_w")),
        "l1_power": as_float(reconciliation.get("l1_power_w")),
        "l2_power": as_float(reconciliation.get("l2_power_w")),
        "direct_measured_power": as_float(reconciliation.get("direct_measured_w")),
        "inferred_power": as_float(reconciliation.get("inferred_w")),
        "model_estimated_power": as_float(reconciliation.get("model_estimated_w")),
        "unknown_power": as_float(reconciliation.get("unknown_residual_w")),
        "reconciliation_error": as_float(reconciliation.get("reconciliation_error_w")),
        "accounted_fraction": as_percentage(reconciliation.get("accounted_fraction")),
        "topology_confirmed": bool(reconciliation.get("topology_confirmed", False)),
        "quality_score": as_percentage(quality.get("overall_score")),
        "engine_status": engine_status,
        "last_update": timestamp,
        "engine_reachable": engine_available,
        **learning,
    }


#: Global entity keys sourced from ``Reconciliation``.  These require a
#: reconciliation that is itself present and not stale.
RECONCILIATION_KEYS: frozenset[str] = frozenset(
    {
        "total_power",
        "l1_power",
        "l2_power",
        "direct_measured_power",
        "inferred_power",
        "model_estimated_power",
        "unknown_power",
        "reconciliation_error",
        "accounted_fraction",
    }
)


def engine_device_available(engine_available: bool) -> bool:
    """The engine device is available whenever the last poll succeeded."""
    return bool(engine_available)


# --------------------------------------------------------------------------- #
# Energy entity opt-in and device binding
# --------------------------------------------------------------------------- #

#: Devices that opt *out* of an energy entity.  Absent means "create it", which
#: matches the behaviour before the option existed and powercalc's default.
ENERGY_ENABLED_KEY = "create_energy_sensor"

#: Entity keys that describe *the measured load itself* and therefore belong on
#: the real Home Assistant device when one can be resolved.
#:
#: ``power`` and ``energy`` are physical quantities of the appliance; a user
#: looking at their hot tub's page should see its watts and kWh there.  Every
#: other key is about how WattMosaic reached the number (``source``,
#: ``confidence``, ``confidence_tier``, ``sample_count``, ``model_validity``,
#: ``operating_stage``, ``last_inference``, ``data_quality``).  Those stay on the
#: synthetic inferred-load device: they are inference provenance, and putting
#: them on the appliance would present a model's self-assessment as if it were
#: telemetry from the hardware.
BINDABLE_KEYS: frozenset[str] = frozenset({"power", "energy"})


def energy_enabled(device_config: Mapping[str, Any] | None) -> bool:
    """Whether an energy entity should exist for this engine device.

    Default ``True``: energy existed for every device before this option, and
    silently withdrawing 28 entities on upgrade would break statistics and
    dashboards.  Only an explicit ``False`` suppresses it, and a value that
    cannot be interpreted also keeps the entity rather than removing it.
    """
    if not isinstance(device_config, Mapping):
        return True
    return as_bool(device_config.get(ENERGY_ENABLED_KEY), default=True)


def merge_energy_choice(
    engine_configuration: Any,
    stored_choices: Any,
    device_id: str,
) -> dict[str, Any] | None:
    """The device configuration with the config entry's energy opt-in on top.

    The engine owns everything it knows about a device; the energy opt-in is a
    Home Assistant concern and never travels over the API, so it lives in the
    config entry's options and is layered on here.

    ``None`` means "nothing is known about this device" — including when the
    engine reported nothing and no choice is stored.  That is deliberately
    distinct from ``{}``: every caller treats missing configuration as "use the
    default", and an empty mapping would have to be special-cased everywhere.

    A stored value is coerced through :func:`energy_enabled`'s rule rather than
    ``bool()``, because options deserialised from older files can hold strings:
    ``bool("false")`` is ``True``, which would silently re-enable the entity.
    """
    configuration = (
        engine_configuration if isinstance(engine_configuration, Mapping) else None
    )
    merged = dict(configuration) if configuration else {}
    if isinstance(stored_choices, Mapping) and device_id in stored_choices:
        merged[ENERGY_ENABLED_KEY] = energy_enabled(
            {ENERGY_ENABLED_KEY: stored_choices[device_id]}
        )
    return merged or None


def explicit_device_link(
    stored_links: Any,
    device_id: str,
) -> str | None:
    """The HA device id the user linked to this engine device, if any.

    An explicit link is the unambiguous form: several devices can share one
    branch CT (a bank of Lutron dimmers), and several entities can belong to the
    same appliance.  Resolving a host from an entity's registry entry works only
    when the entity happens to be a member of the intended device; a declaration
    names the device directly and is therefore preferred.

    Returns ``None`` for "not linked", never for "linked to nothing": an empty
    or non-string value is ignored so a half-written options entry cannot detach
    a measurement by accident.
    """
    if not isinstance(stored_links, Mapping):
        return None
    value = stored_links.get(device_id)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def binding_device_entity_ids(device_config: Mapping[str, Any] | None) -> list[str]:
    """Entities that justify attaching a measurement to a *real* HA device.

    Only entities the user explicitly declared as runtime evidence for *this*
    device count.  The engine's own output entities are never candidates: they
    belong to the synthetic WattMosaic device, and binding to them would make
    the load its own host.
    """
    if not isinstance(device_config, Mapping):
        return []
    out: list[str] = []
    for key in ("state_entity_ids", "load_entity_ids"):
        raw = device_config.get(key)
        if not isinstance(raw, (list, tuple)):
            continue
        for item in raw:
            text = str(item or "").strip()
            if text and text not in out:
                out.append(text)
    return out


def resolve_binding_target(
    candidates: Iterable[str],
    registry: Mapping[str, Mapping[str, Any] | None],
    *,
    own_config_entry_id: str | None,
) -> str | None:
    """Pick the first candidate entity's ``device_id`` that may host energy.

    ``registry`` maps ``entity_id`` to a registry-like mapping carrying
    ``device_id`` and ``config_entry_id`` (``None`` for an unknown entity).
    Pure so the rules are testable without Home Assistant.

    Two rejections matter:

    * An entity this integration owns would make an inferred load the host of
      its own energy sensor — a self-reference with no physical meaning.
    * An entity with no device, or an unknown entity id, cannot host anything.
    """
    for entity_id in candidates:
        entry = registry.get(entity_id)
        if not entry:
            continue
        device_id = entry.get("device_id")
        if not isinstance(device_id, str) or not device_id:
            continue
        if own_config_entry_id is not None and (
            entry.get("config_entry_id") == own_config_entry_id
        ):
            continue
        return device_id
    return None


def binding_update(
    current_device_id: Any,
    target_device_id: Any,
) -> dict[str, str] | None:
    """The entity-registry update that moves an entity to another device.

    Returns ``None`` when there is nothing to do: no resolved target, or the
    entity is already on it.  That makes the caller idempotent, so a reload
    does not rewrite the registry (and fire registry events) on every start.

    Only ``device_id`` is ever returned.  ``entity_id`` is deliberately absent:
    renaming an entity is what orphans its long-term statistics, because the
    recorder migrates statistics on a rename but not on a re-derive at create
    time.  Placement decides where the entity is *shown*, never what it is
    called.
    """
    if not isinstance(target_device_id, str) or not target_device_id:
        return None
    if current_device_id == target_device_id:
        return None
    return {"device_id": target_device_id}


def via_device_kwargs(
    *,
    supports_via_device_id: bool,
    hub_device_id: Any,
    hub_identifier: tuple[str, str],
) -> dict[str, Any]:
    """The keyword mapping that links a child device to the engine device.

    Home Assistant 2026.8 deprecated the ``via_device=(domain, identifier)``
    form in favour of ``via_device_id=<registry id>``.  Passing *both* raises
    ``HomeAssistantError``, and passing only the deprecated pair reports the
    calling integration -- a report that is raised (not logged) when it happens
    inside a strict frame, aborting entity creation.

    So on a version that understands ``via_device_id`` the id form is used, and
    the deprecated pair is never emitted.  If the hub id is missing there, the
    link is dropped rather than falling back: a device without a parent is
    harmless, while the deprecated pair can take every entity down.

    On older versions the pair form is the only one accepted, and it is correct.
    """
    if supports_via_device_id:
        if isinstance(hub_device_id, str) and hub_device_id:
            return {"via_device_id": hub_device_id}
        return {}
    return {"via_device": hub_identifier}


__all__ = [
    "BINDABLE_KEYS",
    "ENERGY_ENABLED_KEY",
    "RECONCILIATION_KEYS",
    "binding_device_entity_ids",
    "binding_update",
    "device_values",
    "energy_enabled",
    "engine_device_available",
    "explicit_device_link",
    "global_values",
    "merge_energy_choice",
    "resolve_binding_target",
    "via_device_kwargs",
]
