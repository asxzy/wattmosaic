"""Read-only measurement-endpoint candidate discovery from HA states/registries.

D2 (endpoint discovery)
-----------------------
:func:`discover_candidate_endpoints` proposes Measurement Endpoints (DEC-04/05/06):
CT channels from a multi-channel monitor, and single-load power entities as
*candidate single-subject* endpoints whose scope must be user-confirmed before
ingestion starts.  It never mints ``dev_<ct>`` devices.

Why this module exists
----------------------
A freshly installed engine has an empty database and no measurement endpoints,
so the integration would show nothing at all.  Startup discovery
(``EngineCore.bootstrap_topology``) persists these candidates through the v3
repositories.  Every endpoint is a *candidate*: nothing here is committed as
fact; the user confirms scopes and declares topology edges in the integration.

What counts as a CT channel
---------------------------
Only power readings that belong to a **multi-channel energy monitor** — a
hardware device that exposes several power entities at once.  This is the one
honest anchor available:

* a bare ``sensor.washing_machine_power`` is a smart plug measuring *one* load.
  Turning it into a CT channel fabricates a circuit the installation does not
  have;
* the multi-channel monitor behind 18 ``*_power`` entities *is* the CT
  hardware.

The anchor is structural (a device that reports several power entities, at least
two of them shaped like ``..._ctN_power``), never a brand name, so it works for
any monitor and stays free of installation identifiers.

Roles are derived from the *panel*, not from English substrings
--------------------------------------------------------------
An earlier version classified a channel as a main when its id merely contained
``"main"``.  That matched every load whose name contained the word (a circuit
named ``main_area_ac``, for instance), and on a real installation it produced
21 mains out of 36 channels — a topology in which two thirds of the loads were
claimed to be incoming service feeds.  Roles are therefore derived from position
within the monitor instead:

* the **shortest** ``..._ct1`` / ``..._ct2`` stem in a panel is that panel's
  incoming pair (``ct1`` is the feed, ``water_heater_ct1`` is a load);
* a load with both ``_ct1`` and ``_ct2`` legs is a double-pole 240 V circuit and
  becomes **one** device owning both legs;
* any other channel is a dedicated (single-load) circuit.

Entity ids are split into ``panel prefix`` + ``local stem`` first.  Without that
split the "shortest stem" rule is meaningless: ``panel`` and ``panel_lights``
both reduce to a single word, and several unrelated circuits collapse onto the
same device.

A channel's role decides how a device owns it.  Filing a ``branch`` CT under
``dedicated_ct_ids`` makes ``dedicated_channel_for`` reject it, and the device
then has no attribution rows at all — it becomes invisible while a real CT still
measures it.  That mistake silently dropped two lighting circuits once already.

Nothing here calls a service, writes to Home Assistant, or actuates a device.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from ..contracts import (
    EndpointKind,
    EndpointScope,
    HaEntityLink,
    HaEntityRole,
    MeasurementEndpoint,
    PhysicalDevice,
    ScopeStatus,
    utcnow,
)
from .normalize import canonical_unit, entity_allowed, is_own_entity

#: A hardware device must expose at least this many power entities before it can
#: be treated as a multi-channel CT monitor.
MIN_MONITOR_POWER_ENTITIES = 2

#: ``..._ct1_power`` / ``..._ct2_power`` — one leg of a double-pole load, or one
#: phase of a panel feed.  The stem may be empty: a monitor's own feed entities
#: are ``<panel>_ct1_power``.
_CT_LEG = re.compile(r"^(?P<stem>.*?)_?ct(?P<leg>[0-9]+)_power$")

#: Normalised unit spellings that constitute a *power* reading (not energy).
_POWER_UNITS = ("W", "kW")

#: Object-id suffixes that describe a monitor aggregate rather than a single CT.
#: Counting these as channels would add a phantom circuit equal to the panel
#: total and double-count every other CT on the monitor.
_AGGREGATE_SUFFIXES = ("_total_power", "_total", "_sum_power")


def _object_id(entity_id: str) -> str:
    return entity_id.split(".", 1)[-1]


def _panel_prefix(panel: str) -> str:
    """Stable, generic ct-id prefix for a panel name (``Panel A`` -> ``panel_a``)."""
    cleaned = re.sub(r"[^a-z0-9]+", "_", panel.lower()).strip("_")
    return cleaned or "panel"


def _is_aggregate(object_id: str) -> bool:
    return object_id.endswith(_AGGREGATE_SUFFIXES)


def _power_entities_by_device(
    states: list[Mapping[str, Any]],
    entities: Mapping[str, Mapping[str, Any]],
) -> dict[str, list[tuple[str, dict[str, Any]]]]:
    """Group power readings by the hardware device that publishes them."""
    grouped: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for state in states:
        entity_id = state.get("entity_id")
        if not isinstance(entity_id, str):
            continue
        attributes = state.get("attributes")
        if not isinstance(attributes, Mapping):
            continue
        if canonical_unit(attributes.get("unit_of_measurement")) not in _POWER_UNITS:
            continue
        entry = entities.get(entity_id) or {}
        # The engine's own estimates are never channels: offering them back would
        # let a circuit measure its own answer (ADR-0015 / defect #32).
        if is_own_entity(entity_id, entry.get("platform")):
            continue
        if _is_aggregate(_object_id(entity_id)):
            continue
        hardware_id = entry.get("device_id")
        if not isinstance(hardware_id, str) or not hardware_id:
            continue
        # Carry the *observed* reading along: it is the only evidence available
        # for polarity.  Discovery is a snapshot, not a history, so this is a
        # weaker signal than a windowed audit — hence ``candidate`` below.
        reading: float | None
        try:
            reading = float(state.get("state"))
        except (TypeError, ValueError):
            reading = None
        grouped.setdefault(hardware_id, []).append(
            (entity_id, {**dict(attributes), "_reading_w": reading})
        )
    return grouped


def _is_monitor(power: list[tuple[str, dict[str, Any]]]) -> bool:
    """True when a hardware device looks like a multi-channel energy monitor.

    Two independent signals, either sufficient: several power entities, or at
    least two channels shaped like ``..._ctN_power``.  The second keeps a 2-CT
    monitor (one feed pair) discoverable while still rejecting a single smart
    plug that happens to publish two watt entities.
    """
    if len(power) >= 3:
        return True
    if len(power) >= MIN_MONITOR_POWER_ENTITIES:
        ct_like = sum(1 for entity_id, _ in power if _CT_LEG.match(_object_id(entity_id)))
        return ct_like >= 2
    return False


def _common_prefix(object_ids: list[str]) -> str:
    """Longest common ``_``-terminated prefix of the monitor's object ids.

    ``["panel_ct1_power", "panel_circuit_ct1_power"]`` yields ``"panel_"``.
    Derived from the entities themselves, not from a device name, because HA
    device names and entity-id prefixes often disagree (``basement_panel_*`` on a
    device named ``Main Panel``).
    """
    if not object_ids:
        return ""
    candidate = min(object_ids, key=len)
    for other in object_ids:
        limit = 0
        for index, char in enumerate(candidate):
            if index >= len(other) or other[index] != char:
                break
            limit = index + 1
        candidate = candidate[:limit]
    cut = candidate.rfind("_")
    return candidate[: cut + 1] if cut >= 0 else ""


def _split_local(object_id: str, prefix: str) -> str:
    local = object_id[len(prefix) :] if prefix and object_id.startswith(prefix) else object_id
    return local.removesuffix("_power")


def _leg_stem(local: str) -> tuple[str, int] | None:
    """``("circuit", 1)`` for ``circuit_ct1``; ``("", 1)`` for a panel feed ``ct1``."""
    match = re.match(r"^(?P<stem>.*?)_?ct(?P<leg>[0-9]+)$", local)
    if not match:
        return None
    return match.group("stem"), int(match.group("leg"))


def _subject_id_for_ha_device(ha_device_id: str) -> str:
    """The HA-device-derived Physical Device id for a plug/appliance monitor.

    A deterministic, reversible slug keeps repeated discovery runs stable and
    lets D2's device service create the matching :class:`PhysicalDevice`.  The
    legacy ``dev_<ct>`` form is deliberately not used: this is a device that
    owns a load-evidence entity (DEC-04), not a circuit.
    """
    cleaned = re.sub(r"[^a-z0-9]+", "_", ha_device_id.lower()).strip("_")
    if cleaned.startswith("ha_"):
        cleaned = cleaned[3:]
    return f"pd_from_ha_{cleaned or 'device'}"


def discover_candidate_endpoints(
    states: Iterable[dict[str, Any]],
    registries: dict[str, Any] | None = None,
    *,
    allow_entities: Iterable[str] | None = None,
    deny_entities: Iterable[str] | None = None,
    clock: Any = None,
    enabled: bool = True,
) -> list[MeasurementEndpoint]:
    """Propose candidate Measurement Endpoints from HA states/registries.

    Two kinds are proposed (DEC-04/05/06):

    * each power entity of a **multi-channel monitor** becomes a ``CT`` endpoint
      (panel feeds ``PANEL_FEED``, circuits ``CIRCUIT``), ingested immediately;
    * a bare ``sensor.*_power`` whose HA device is **not** a multi-channel
      monitor (an appliance or plug) becomes a **candidate single-subject**
      endpoint whose subject is the HA-device-derived Physical Device.  Its
      ``scope_status`` is ``CANDIDATE`` and it is **disabled**: ingestion of
      candidate endpoints stays off until the user confirms the scope (DEC-06).

    This is a pure function: ``clock`` is an injected callable; no DB, no wall
    clock.  It never creates a device — devices come from HA devices that own
    load-evidence entities, user declaration, or promoted unknown profiles.
    """
    now = clock() if clock is not None else utcnow()
    registries = registries or {}
    entities: dict[str, Mapping[str, Any]] = {}
    raw_entities = registries.get("entities") if isinstance(registries, Mapping) else None
    for entry in raw_entities or []:
        if isinstance(entry, Mapping) and entry.get("entity_id"):
            entities[str(entry["entity_id"])] = entry
    devices_by_id: dict[str, Mapping[str, Any]] = {}
    raw_devices = registries.get("devices") if isinstance(registries, Mapping) else None
    for entry in raw_devices or []:
        if isinstance(entry, Mapping) and entry.get("id"):
            devices_by_id[str(entry["id"])] = entry

    state_list = [
        s
        for s in states
        if isinstance(s, Mapping)
        and entity_allowed(
            str(s.get("entity_id") or ""), allow=allow_entities, deny=deny_entities
        )
    ]
    grouped = _power_entities_by_device(state_list, entities)

    endpoints: list[MeasurementEndpoint] = []
    for hardware_id in sorted(grouped):
        power = sorted(grouped[hardware_id])
        hardware = devices_by_id.get(hardware_id) or {}
        panel = str(hardware.get("name_by_user") or hardware.get("name") or hardware_id)
        slug = _panel_prefix(panel)
        if _is_monitor(power):
            # CT channels from a multi-channel monitor: one endpoint per channel.
            prefix = _common_prefix([_object_id(entity_id) for entity_id, _ in power])
            locals_by_entity = {
                entity_id: _split_local(_object_id(entity_id), prefix)
                for entity_id, _ in power
            }
            legs: dict[str, dict[int, str]] = {}
            for entity_id, local in locals_by_entity.items():
                parsed = _leg_stem(local)
                if parsed is not None:
                    stem, leg = parsed
                    legs.setdefault(stem, {})[leg] = entity_id
            feed_candidates = [
                (len(stem), stem)
                for stem, pair in legs.items()
                if 1 in pair and 2 in pair
            ]
            main_stem = min(feed_candidates)[1] if feed_candidates else None
            consumed: set[str] = set()
            if main_stem is not None:
                for leg, entity_id in sorted(legs[main_stem].items()):
                    consumed.add(entity_id)
                    endpoints.append(
                        _ct_endpoint(
                            entity_id,
                            endpoint_id=f"{slug}_l{leg}",
                            scope=EndpointScope.PANEL_FEED,
                            panel_id=slug,
                            enabled=enabled,
                            created_at=now,
                        )
                    )
            for entity_id, local in sorted(locals_by_entity.items()):
                if entity_id in consumed:
                    continue
                stem, leg = _leg_stem(local) or (local, 0)
                local_stem = stem or local
                if stem and len(legs.get(stem) or {}) > 2 and leg:
                    local_stem = f"{stem}_ct{leg}"
                consumed.add(entity_id)
                endpoints.append(
                    _ct_endpoint(
                        entity_id,
                        endpoint_id=f"{slug}_{local_stem}",
                        scope=EndpointScope.CIRCUIT,
                        panel_id=slug,
                        enabled=enabled,
                        created_at=now,
                    )
                )
        else:
            # Single-load power entity on an appliance/plug HA device: candidate
            # single-subject endpoint.  One endpoint per HA device (if several
            # power entities share a plug, the first is the candidate).
            subject = _subject_id_for_ha_device(hardware_id)
            entity_id, _ = power[0]
            endpoints.append(
                MeasurementEndpoint(
                    endpoint_id=f"plug_{hardware_id}",
                    kind=EndpointKind.SMART_PLUG,
                    entity_id=entity_id,
                    energy_entity_id=None,
                    display_name=str(hardware.get("name_by_user") or hardware.get("name") or entity_id),
                    scope=EndpointScope.SINGLE_SUBJECT,
                    scope_status=ScopeStatus.CANDIDATE,
                    scope_subject_id=subject,
                    panel_id=None,
                    enabled=False,  # candidate ingestion stays off until confirm (DEC-06)
                    created_at=now,
                )
            )
    return endpoints


def _ct_endpoint(
    entity_id: str,
    *,
    endpoint_id: str,
    scope: EndpointScope,
    panel_id: str,
    enabled: bool,
    created_at: datetime,
) -> MeasurementEndpoint:
    """Build one CT endpoint from a monitor channel."""
    return MeasurementEndpoint(
        endpoint_id=endpoint_id,
        kind=EndpointKind.CT,
        entity_id=entity_id,
        energy_entity_id=None,
        display_name=_display_name(endpoint_id, panel_id),
        scope=scope,
        scope_status=ScopeStatus.CANDIDATE,
        scope_subject_id=None,
        panel_id=panel_id,
        enabled=bool(enabled),
        created_at=created_at,
    )


# HA devices that own a candidate endpoint imply a Physical Device (DEC-04).
# The following two helpers turn discovered endpoints into the canonical entity
# contracts; they are pure and injected with a clock by the caller.


def devices_for_candidate_endpoints(
    endpoints: Iterable[MeasurementEndpoint],
    registries: dict[str, Any] | None = None,
    *,
    clock: Any = None,
) -> list[PhysicalDevice]:
    """Physical Devices implied by candidate plug/appliance endpoints.

    Only non-CT single-subject candidates create a device: CT endpoints never
    mint a device (DEC-04).  The device origin is ``ha_device`` and its id is the
    HA-device-derived id from :func:`discover_candidate_endpoints`.
    """
    now = clock() if clock is not None else utcnow()
    registries = registries or {}
    devices_by_id: dict[str, Mapping[str, Any]] = {}
    raw_devices = registries.get("devices") if isinstance(registries, Mapping) else None
    for entry in raw_devices or []:
        if isinstance(entry, Mapping) and entry.get("id"):
            devices_by_id[str(entry["id"])] = entry
    result: list[PhysicalDevice] = []
    seen: set[str] = set()
    for endpoint in endpoints:
        if endpoint.kind == EndpointKind.CT or endpoint.scope_subject_id is None:
            continue
        subject = endpoint.scope_subject_id
        if subject in seen:
            continue
        seen.add(subject)
        hardware = devices_by_id.get(subject.replace("pd_from_ha_", "")) or {}
        result.append(
            PhysicalDevice(
                device_id=subject,
                name=endpoint.display_name or subject,
                device_type=None,
                ha_device_id=hardware.get("id"),
                area_id=None,
                origin="ha_device",
                created_at=now,
            )
        )
    return result


def links_for_candidate_endpoints(
    endpoints: Iterable[MeasurementEndpoint],
    *,
    clock: Any = None,
) -> list[HaEntityLink]:
    """HA entity links that bind a candidate endpoint's power entity to its subject.

    The role is ``power_measurement`` so a later confirm can create the
    DIRECT mapping edge that yields MEASURED (DEC-05).  Pure: the clock is
    injected.
    """
    now = clock() if clock is not None else utcnow()
    result: list[HaEntityLink] = []
    for endpoint in endpoints:
        if endpoint.kind == EndpointKind.CT or endpoint.entity_id is None:
            continue
        result.append(
            HaEntityLink(
                entity_id=endpoint.entity_id,
                target_kind="device",
                target_id=endpoint.scope_subject_id,
                role=HaEntityRole.POWER_MEASUREMENT,
                valid_from=now,
                declared_by="discovery",
            )
        )
    return result


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def _display_name(stem: str, slug: str) -> str:
    """A readable name built only from the local stem (never a hardware brand)."""
    text = stem or ""
    if slug and text.startswith(f"{slug}_"):
        text = text[len(slug) + 1 :]
    text = re.sub(r"_ct[0-9]+$", "", text)
    return text.replace("_", " ").strip()


__all__ = [
    "MIN_MONITOR_POWER_ENTITIES",
    "discover_candidate_endpoints",
    "devices_for_candidate_endpoints",
    "links_for_candidate_endpoints",
]
