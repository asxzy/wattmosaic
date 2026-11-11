"""Read-only topology candidate discovery from Home Assistant states/registries.

Why this module exists
----------------------
A freshly installed engine has an empty database.  ``EngineCore.run_inference_once``
returns ``{"skipped": "no topology configured"}`` and never emits a result, so the
integration shows nothing at all.  Before this module was wired into startup the
documented behaviour ("WattMosaic discovers CT channels and proposes device
mappings", ``docs/installation.md`` §5) did not exist: the function below was
reachable only from its own unit tests.

The result is always ``TopologyStatus.CANDIDATE``.  Nothing here is committed as
fact; the integration's confirmation flow (or ``POST /topology/confirm``) is what
turns a candidate into a confirmed topology.

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
from typing import Any

from ..contracts import (
    CTChannel,
    CTType,
    DeviceTopology,
    Phase,
    Polarity,
    Topology,
    TopologyStatus,
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


def _phase_for_leg(leg: int) -> Phase:
    if leg == 1:
        return Phase.L1
    if leg == 2:
        return Phase.L2
    return Phase.UNKNOWN


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


def discover_candidate_topology(
    states: Iterable[dict[str, Any]],
    registries: dict[str, Any] | None = None,
    *,
    allow_entities: Iterable[str] | None = None,
    deny_entities: Iterable[str] | None = None,
    enabled: bool = True,
) -> Topology:
    """Build a candidate ``Topology`` from Home Assistant states.

    Only power entities published by a multi-channel monitor become channels, and
    devices are emitted for every load channel so the result passes the API's
    semantic validation (at least one main, at least one device) and the engine
    can attribute power immediately.

    Returns an empty topology (zero channels, zero devices) when no monitor is
    present; the caller must treat that as "nothing to do", not as an error.
    """
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

    channels: list[CTChannel] = []
    devices: list[DeviceTopology] = []

    for hardware_id in sorted(grouped):
        power = sorted(grouped[hardware_id])
        if not _is_monitor(power):
            continue

        hardware = devices_by_id.get(hardware_id) or {}
        panel = str(hardware.get("name_by_user") or hardware.get("name") or hardware_id)
        prefix = _common_prefix([_object_id(entity_id) for entity_id, _ in power])
        slug = _panel_prefix(panel)

        locals_by_entity = {
            entity_id: _split_local(_object_id(entity_id), prefix) for entity_id, _ in power
        }
        attributes_by_entity = {entity_id: attributes for entity_id, attributes in power}
        if len(set(locals_by_entity.values())) != len(locals_by_entity):
            # Duplicate local stems would collide in ct_id space; skip the monitor
            # rather than emit ambiguous circuit ids.
            continue

        # Local stems grouped by leg, so ``ct1``/``ct2`` pairs can be found.
        legs: dict[str, dict[int, str]] = {}
        for entity_id, local in locals_by_entity.items():
            parsed = _leg_stem(local)
            if parsed is None:
                continue
            stem, leg = parsed
            legs.setdefault(stem, {})[leg] = entity_id

        # The panel's own feed is the shortest stem that has both legs.  A feed
        # stem is typically empty (``ct1``/``ct2`` alone).
        feed_candidates = [
            (len(stem), stem)
            for stem, pair in legs.items()
            if 1 in pair and 2 in pair
        ]
        main_stem: str | None = None
        if feed_candidates:
            feed_candidates.sort()
            main_stem = feed_candidates[0][1]

        consumed: set[str] = set()

        if main_stem is not None:
            pair = legs[main_stem]
            ct_ids = {
                1: f"{slug}_l1",
                2: f"{slug}_l2",
            }
            for leg, entity_id in sorted(pair.items()):
                consumed.add(entity_id)
                channels.append(
                    _channel(
                        entity_id,
                        ct_type=CTType.MAIN,
                        ct_id=ct_ids[leg],
                        phase=_phase_for_leg(leg),
                        paired_ct_id=ct_ids[2] if leg == 1 else ct_ids[1],
                        enabled=enabled,
                        panel=panel,
                        attributes=attributes_by_entity.get(entity_id),
                        evidence=[
                            "panel feed CT (shortest paired ct1/ct2 stem on this monitor)"
                        ],
                    )
                )

        # Loads: a resolvable leg pair is one double-pole device, anything else is
        # a single-channel circuit.  "Single channel" is *not* evidence of a
        # single device: a lighting circuit with one CT routinely feeds many
        # fixtures.  See the classification note on the fallback branch below.
        for entity_id, local in sorted(locals_by_entity.items()):
            if entity_id in consumed:
                continue
            stem, leg = _leg_stem(local) or (local, 0)
            mate: str | None = None
            if leg in (1, 2) and _is_genuine_pair(stem, legs):
                other_leg = 2 if leg == 1 else 1
                mate = legs.get(stem, {}).get(other_leg)

            if mate is not None and mate not in consumed:
                consumed.update({entity_id, mate})
                base = f"{slug}_{stem}" if stem else slug
                ct_l1, ct_l2 = f"{base}_l1", f"{base}_l2"
                channels.extend(
                    [
                        _channel(
                            entity_id if leg == 1 else mate,
                            ct_type=CTType.BRANCH,
                            ct_id=ct_l1,
                            phase=Phase.L1,
                            paired_ct_id=ct_l2,
                            enabled=enabled,
                            panel=panel,
                            attributes=attributes_by_entity.get(
                                entity_id if leg == 1 else mate
                            ),
                            evidence=["paired leg CT1"],
                        ),
                        _channel(
                            mate if leg == 1 else entity_id,
                            ct_type=CTType.BRANCH,
                            ct_id=ct_l2,
                            phase=Phase.L2,
                            paired_ct_id=ct_l1,
                            enabled=enabled,
                            panel=panel,
                            attributes=attributes_by_entity.get(
                                mate if leg == 1 else entity_id
                            ),
                            evidence=["paired leg CT2"],
                        ),
                    ]
                )
                devices.append(
                    DeviceTopology(
                        device_id=f"dev_{base}",
                        name=_display_name(stem, slug) or panel,
                        branch_ct_ids=[ct_l1, ct_l2],
                        phase=Phase.BOTH,
                        double_pole=True,
                        topology_status=TopologyStatus.CANDIDATE,
                        metadata={
                            "note": "two-pole load; combined power is the sum of both legs",
                            "ct_group": [ct_l1, ct_l2],
                            "pair_confidence": "name_derived",
                            "requires_confirmation": True,
                        },
                    )
                )
                continue

            consumed.add(entity_id)
            local_stem = stem or local
            # A stem with three or more legs is a family of separate circuits that
            # happen to share a name.  Keeping the leg suffix is what stops
            # `circuit_ct3` from colliding with `circuit_ct1`/`ct2` (defect #28).
            if stem and len(legs.get(stem) or {}) > 2 and leg:
                local_stem = f"{stem}_ct{leg}"
            ct_id = f"{slug}_{local_stem}"
            device_id = f"dev_{ct_id}"
            # A single CT measures one *conductor*, not one appliance.  Calling it
            # ``dedicated_device`` asserts that exactly one logical device sits on
            # it, and discovery has no evidence for that: the only discriminator
            # available here is "this channel was not part of a ct1/ct2 pair",
            # which is true of every one-CT lighting circuit in existence.  That
            # assertion was wrong on the reference installation -- three lighting
            # rows declared ``dedicated`` each resolved 7, 3 and 2 independent
            # fixtures, and one peaked at 221% of what its claimants summed to.
            #
            # ``branch`` states the truth discovery can actually support: a
            # circuit that may contain zero or more devices.  It also makes the
            # row usable, because a ``dedicated_device`` row is refused by
            # auto-placement as "already another device's measurement".
            #
            # The device is still emitted, so the topology passes validation and
            # the circuit is visible immediately; it simply owns the row as a
            # *shared* circuit rather than as a private measurement.  A person
            # who knows the circuit feeds one appliance can still declare it
            # dedicated in the wizard, which is the only source of that fact.
            channels.append(
                _channel(
                    entity_id,
                    ct_type=CTType.BRANCH,
                    ct_id=ct_id,
                    phase=_phase_for_leg(leg),
                    enabled=enabled,
                    panel=panel,
                    attributes=attributes_by_entity.get(entity_id),
                    evidence=[
                        "single-channel circuit on a multi-channel monitor",
                        "may feed zero or more fixtures; not a private measurement",
                    ],
                )
            )
            devices.append(
                DeviceTopology(
                    device_id=device_id,
                    name=_display_name(local_stem, slug),
                    branch_ct_ids=[ct_id],
                    phase=_phase_for_leg(leg),
                    topology_status=TopologyStatus.CANDIDATE,
                    metadata={
                        "note": (
                            "single CT on one conductor; the circuit may feed "
                            "several fixtures -- requires confirmation"
                        ),
                        "requires_confirmation": True,
                        "shared_circuit": True,
                    },
                )
            )

    notes = [
        "Discovered passively from Home Assistant states; classification is heuristic.",
        "Only multi-channel energy monitors are treated as CT hardware.",
        "All entries are candidates. Confirm before relying on inference.",
        "Panel nesting is NOT assumed: each monitor is a separate measurement group.",
        "Double-pole pairs derived from *_ct1/*_ct2 naming only.",
        "A device's state entity is not guessed; link it in the integration wizard.",
    ]
    return Topology(
        version=1,
        created_at=utcnow(),
        status=TopologyStatus.CANDIDATE,
        channels=channels,
        devices=devices,
        notes=notes,
    )


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def _is_genuine_pair(stem: str, legs: Mapping[str, Mapping[int, str]]) -> bool:
    """True only for a real 240 V load: exactly two legs, no third.

    Separate circuits named ``circuit_ct1``/``circuit_ct2``/``circuit_ct3``
    are three distinct loads that share a name stem.  Pairing the first two would
    fuse two unrelated circuits into one device and leave the third as a solo
    device with the same id (defect #28).  The same rule keeps a lone ``_ct1``
    with no mate from being paired.
    """
    pair = legs.get(stem) or {}
    return set(pair) == {1, 2}


def _channel(
    entity_id: str,
    *,
    ct_type: CTType,
    ct_id: str,
    phase: Phase,
    enabled: bool,
    panel: str,
    evidence: list[str],
    paired_ct_id: str | None = None,
    device_id: str | None = None,
    attributes: Mapping[str, Any] | None = None,
) -> CTChannel:
    polarity, polarity_evidence = _polarity_from_reading(attributes)
    metadata: dict[str, Any] = {
        "evidence": [*evidence, f"entity={entity_id}", f"panel={panel}"],
        "discovered": True,
    }
    if polarity_evidence is not None:
        metadata["polarity_evidence"] = polarity_evidence
    return CTChannel(
        ct_id=ct_id,
        entity_id=entity_id,
        name=_display_name(ct_id, _panel_prefix(panel)),
        ct_type=ct_type,
        phase=phase,
        polarity=polarity,
        paired_ct_id=paired_ct_id,
        device_id=device_id,
        enabled=bool(enabled),
        topology_status=TopologyStatus.CANDIDATE,
        metadata=metadata,
        panel_id=_panel_prefix(panel),
        panel_name=panel,
    )


def _polarity_from_reading(
    attributes: Mapping[str, Any] | None,
) -> tuple[Polarity, str | None]:
    """Derive polarity from the observed sign, never from a guess.

    A CT installed backwards reports a negative wattage.  With the default
    ``normal`` polarity that reading is clipped to zero and the device is
    published as ``direct_measured`` 0 W with ``quality: good`` — a silent lie
    that also hides its whole share of the residual.  The stored topology of the
    reference installation had three such channels.

    This is a single snapshot, so the evidence is weaker than the windowed audit
    used to build the reference topology: a load that happens to be exporting at
    the instant of discovery would look reversed.  It is therefore recorded as
    evidence on a *candidate* channel, which the user confirms or corrects, and
    the reason is written into ``metadata`` so the decision is auditable.
    """
    if not attributes:
        return Polarity.NORMAL, None
    raw = attributes.get("_reading_w")
    if not isinstance(raw, (int, float)):
        return Polarity.NORMAL, None
    if raw < 0:
        return (
            Polarity.INVERTED,
            f"observed reading {raw:.1f} W is negative; treated as reversed CT "
            "(single snapshot — confirm before relying on it)",
        )
    return Polarity.NORMAL, None


def _display_name(stem: str, slug: str) -> str:
    """A readable name built only from the local stem (never a hardware brand)."""
    text = stem or ""
    if slug and text.startswith(f"{slug}_"):
        text = text[len(slug) + 1 :]
    text = re.sub(r"_ct[0-9]+$", "", text)
    return text.replace("_", " ").strip()


__all__ = ["MIN_MONITOR_POWER_ENTITIES", "discover_candidate_topology"]
