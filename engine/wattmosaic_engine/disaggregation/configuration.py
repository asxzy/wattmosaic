"""Effective per-device configuration (ADR-0011 / ADR-0012).

The three taxonomy axes are user knowledge, but each one changes what the engine
is *allowed* to do.  This module is the single place that turns a declarative
:class:`~wattmosaic_engine.contracts.DeviceConfiguration` into effective,
engine-usable rules, and it is deliberately pure so the rules are testable
without a solver.

Projection, not a second source of truth
---------------------------------------
``Topology`` is already persisted, versioned and threaded through ingestion,
inference and learning.  Adding a parallel configuration channel would let the
two disagree — a device could be "shared" for the solver and "dedicated" for the
event intake.  So the resolved configuration is **projected into the device
metadata**, and everything downstream reads it from there:

===================  ==========================================================
metadata key          content
===================  ==========================================================
``state_entity_ids``  entities that may prove runtime, as derived from mapping
                      evidence or declared before configuration existed
``config_load_entity_ids``  runtime entities the *user* declared in configuration
``mode_entity_ids``   entities that are context only (ADR-0012)
``state_overrides``   ``{label: on|off}`` corrections (existing key)
``monitoring_level``  resolved :class:`MonitoringLevel`
``power_behavior``    resolved :class:`PowerBehavior`
``connectivity``      resolved :class:`HaConnectivity`
``device_config``     the full configuration, for audit / round-tripping
===================  ==========================================================

``state_entity_ids`` is deliberately **not** overwritten.  It is written by the
evidence-mapping tool (``tools/audit/build_topology.py``) and is the record of
*why* an entity is linked.  Letting user configuration replace it would destroy
that provenance — and removing a configuration would then leave the user's typed
entities behind, indistinguishable from measured evidence.  User declarations
therefore live in their own key and are cleared cleanly on removal.

Rules
-----
1. **Never invent a monitoring level.**  When the user does not state one it is
   derived from the CT graph exactly as before configuration existed, so every
   existing topology keeps its current behaviour.
2. **A declared level overrides the graph.**  If the user says ``shared`` for a
   circuit the graph calls dedicated, the whole row is no longer this device's
   power: siblings exist that are not modelled, so the row is an upper bound.
   That override is the difference between claiming a whole lights circuit for
   one lamp and reporting the lamp honestly.
3. **Mode entities never prove load.**  Only ``state_entity_ids`` and
   ``controller.load_entity_ids`` may indicate runtime.  A label mapped with
   ``role=load`` and ``confirmed=True`` may supply a watt value; anything else
   is context.
4. **A configured watt value is a bounded prior, never a measurement.**  It can
   cap a shared-row estimate and stand in for a value when nothing else exists,
   but it never outranks a CT.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from wattmosaic_engine.contracts import (
    CTType,
    DeviceConfiguration,
    DeviceConfigurationSet,
    DeviceTopology,
    DeviceType,
    HaConnectivity,
    LightType,
    MonitoringLevel,
    Phase,
    PowerBehavior,
    Topology,
    TopologyStatus,
)
from wattmosaic_engine.entity_evidence import classify_entity

from ..electrical.topology import TopologyGraph, build_graph

#: Metadata keys written by :func:`project_configuration`.
CONFIG_LOAD_ENTITIES_KEY = "config_load_entity_ids"
MODE_ENTITIES_KEY = "mode_entity_ids"
MONITORING_KEY = "monitoring_level"
BEHAVIOR_KEY = "power_behavior"
CONNECTIVITY_KEY = "connectivity"
CONFIG_KEY = "device_config"
#: Evidence-derived runtime entities (never rewritten by configuration).
STATE_ENTITIES_KEY = "state_entity_ids"
#: CT ids this module mirrored onto the device's own measurement lists from
#: ``DeviceConfiguration.monitored_by_ct_ids``.  Recorded so a configuration
#: change can retract exactly what it added instead of guessing.
PROJECTED_CTS_KEY = "projected_ct_ids"
#: Set on a device this module created from a configuration alone, because no
#: discovered channel owned it.  Removal deletes the device rather than leaving
#: an unreachable row behind.  The value is the provenance string
#: (``user``/``auto``) so a generated device stays distinguishable from a
#: discovered one.
MATERIALISED_KEY = "materialised_from_config"

#: Dimming is assumed linear in output unless a measured curve is supplied.
#: Documented simplification, never presented as a fitted model.
LINEAR_DIMMING_DEFAULT = True


@dataclass(slots=True, frozen=True)
class EffectiveConfig:
    """What the engine may actually use for one device."""

    device_id: str
    monitoring: MonitoringLevel = MonitoringLevel.UNKNOWN
    behavior: PowerBehavior = PowerBehavior.UNKNOWN
    connectivity: HaConnectivity = HaConnectivity.UNKNOWN
    device_type: DeviceType = DeviceType.UNKNOWN
    #: CTs declared by the user, intersected with CTs that exist.
    monitored_by_ct_ids: tuple[str, ...] = ()
    #: Declared runtime signals (never mode entities).
    load_entity_ids: tuple[str, ...] = ()
    #: Declared mode signals — context only.
    mode_entity_ids: tuple[str, ...] = ()
    #: Confirmed ``label -> watts`` map for complex controllers.
    controller_load_watts: Mapping[str, float] = field(default_factory=dict)
    #: Configured running watts, if any (``expected.on_w`` or light full output).
    expected_on_w: float | None = None
    expected_standby_w: float | None = None
    expected_tolerance_w: float = 2.0
    #: Measured ``(level_fraction, watts)`` points, ascending.  Present only when
    #: the level→watts relationship was actually observed, so a lookup here is
    #: measurement, not an assumption about how a dimmer behaves.
    dimming_curve: tuple[tuple[float, float], ...] = ()
    #: True when the user asserted a shared circuit we cannot fully separate.
    shared_unmodelled: bool = False
    #: True when this device is the only topology device on its attribution
    #: rows, yet those rows are **not** a ``DEDICATED_DEVICE`` CT.  A sole
    #: occupant of a branch CT looks dedicated in fact, but the Level-1 direct
    #: path only ever reads a dedicated channel, so the label must stay
    #: ``shared`` and this flag records why the direct path is not available.
    sole_occupant: bool = False
    #: Reasons surfaced in ``InferenceResult.tier_reasons``.
    notes: tuple[str, ...] = ()

    @property
    def allows_direct_measurement(self) -> bool:
        """A shared/home-total/absent CT must never be read as this device's power."""
        return self.monitoring in (
            MonitoringLevel.DEDICATED,
            MonitoringLevel.UNKNOWN,
        )

    @property
    def config_ceiling_w(self) -> float | None:
        """Upper bound this configuration justifies for the device's power."""
        if self.expected_on_w is None:
            return None
        return max(self.expected_on_w + self.expected_tolerance_w, 0.0)


def derive_monitoring(
    device: DeviceTopology | None,
    graph: TopologyGraph | None,
    *,
    topology: Topology | None = None,
    declared: MonitoringLevel = MonitoringLevel.UNKNOWN,
) -> MonitoringLevel:
    """Monitoring level, honouring an explicit declaration over the CT graph.

    ``DEDICATED`` means one thing and one thing only: the topology graph yields
    a **``DEDICATED_DEVICE`` channel** for the device (``dedicated_channel_for``).
    That is the same gate the Level-1 direct path uses, so the label and the
    capability cannot disagree.

    A device that is the *sole occupant* of a branch CT used to be labelled
    ``DEDICATED`` as well.  It is monitored alone in practice, but the direct
    path can never produce a reading for it, so the label overstated the
    evidence and fed the tiers/confidence with a capability the engine did not
    have.  Such a device stays ``SHARED`` and is flagged separately
    (``EffectiveConfig.sole_occupant``), which narrows a label without changing
    any watts.
    """
    if declared != MonitoringLevel.UNKNOWN:
        return declared
    if device is None:
        return MonitoringLevel.UNKNOWN
    if graph is None:  # pragma: no cover - graph is always supplied in production
        return MonitoringLevel.UNKNOWN
    if graph.dedicated_channel_for(device.device_id) is not None:
        return MonitoringLevel.DEDICATED
    rows = set(graph.attribution_ct_ids(device.device_id))
    if not rows:
        return MonitoringLevel.HOME_TOTAL_ONLY
    # Attribution rows without a dedicated channel are a shared/branch circuit
    # as far as measurement capability goes — including the sole-occupant case.
    return MonitoringLevel.SHARED


def is_sole_occupant(
    device: DeviceTopology | None,
    graph: TopologyGraph | None,
    *,
    topology: Topology | None = None,
) -> bool:
    """True when no other topology device shares this device's attribution rows.

    Purely informational: it explains why a device is labelled ``SHARED`` even
    though nothing else draws on its circuit, and why the direct path still
    produced 0 W.  It never grants a measurement.
    """
    if device is None or graph is None or graph.dedicated_channel_for(device.device_id) is not None:
        return False
    topology = topology if topology is not None else graph.topology
    rows = set(graph.attribution_ct_ids(device.device_id))
    if not rows:
        return False
    for other in topology.devices:
        if other.device_id == device.device_id:
            continue
        if rows & (set(other.dedicated_ct_ids) | set(other.branch_ct_ids)):
            return False
    return True


def _declared_light_watts(config: DeviceConfiguration) -> float | None:
    light = config.light
    if light is None:
        return None
    full = light.full_on_w()
    if full is not None:
        return full
    if config.expected.on_w is not None:
        return float(config.expected.on_w)
    return None


def _measured_curve(
    config: DeviceConfiguration | None,
) -> tuple[tuple[float, float], ...]:
    """Measured ``(level, watts)`` points from the light configuration.

    ``LightConfiguration.dimming_curve`` is stored as *fraction of full output*
    for the same reason it exists: the contract describes a fixture, not one
    installation's wattage.  Wattage is what attribution needs, so the fraction
    is scaled by the configured full-on watts here, at the one boundary where
    both are known.  Without a stated full-on value the fractions are not
    convertible and the curve is not used -- an unlabelled curve would report a
    fraction as if it were watts.
    """
    if config is None or config.light is None or not config.light.dimming_curve:
        return ()
    full = config.light.full_on_w()
    if full is None or full <= 0.0:
        return ()
    points: list[tuple[float, float]] = []
    for raw_level, raw_fraction in config.light.dimming_curve:
        level = float(raw_level)
        fraction = float(raw_fraction)
        if not (0.0 <= level <= 1.0) or not (0.0 <= fraction <= 1.0):
            # A point outside the contract range cannot describe output; drop it
            # rather than clamp, which would invent a shape the data lacks.
            continue
        points.append((level, fraction * full))
    points.sort()
    return tuple(points)


def _light_notes(config: DeviceConfiguration) -> list[str]:
    light = config.light
    if light is None:
        return []
    notes: list[str] = []
    if light.bulb_count > 1:
        notes.append(f"light_group_of_{light.bulb_count}")
    if light.light_type != LightType.UNKNOWN:
        notes.append(f"light_type_{light.light_type}")
    if light.dimming_curve:
        notes.append("measured_dimming_curve")
    elif light.watts_per_bulb_on is not None:
        notes.append("linear_dimming_assumed")
    if light.standby_w:
        notes.append("smart_bulb_standby_configured")
    return notes


def _split_entities(
    config: DeviceConfiguration,
) -> tuple[list[str], list[str], list[str]]:
    """``(load, mode, reclassified)`` for the declared state entity list.

    A ``climate``/``water_heater`` entity is a mode by domain (ADR-0012), so if a
    user lists one as a *state* entity it is moved to mode context and the move
    is reported.  This is transparent and deterministic: the same declaration
    always produces the same split and the reason is recorded.
    """
    load: list[str] = list(config.state_entity_ids)
    mode: list[str] = list(config.mode_entity_ids)
    if config.controller is not None:
        load.extend(config.controller.load_entity_ids)
        if config.controller.mode_entity_id:
            mode.append(config.controller.mode_entity_id)
    # Controller-confirmed load labels may rely on an entity the domain rules
    # would call a mode; only an *explicit* controller load entity gets that
    # exemption, never a bare state declaration.
    explicit_load = set(config.controller.load_entity_ids) if config.controller else set()
    kept: list[str] = []
    reclassified: list[str] = []
    for entity_id in load:
        evidence = classify_entity(entity_id, declared_load=entity_id in explicit_load)
        if evidence.is_load:
            kept.append(entity_id)
        else:
            mode.append(entity_id)
            reclassified.append(entity_id)
    deduped_kept = list(dict.fromkeys(kept))
    deduped_mode = [e for e in dict.fromkeys(mode) if e not in deduped_kept]
    return deduped_kept, deduped_mode, reclassified


def effective_config(
    device_id: str,
    *,
    topology: Topology | None = None,
    graph: TopologyGraph | None = None,
    device: DeviceTopology | None = None,
) -> EffectiveConfig:
    """Resolve one device's usable configuration from the topology metadata.

    Reading from metadata (rather than from a separate configuration object) is
    what guarantees ingestion, inference and learning cannot disagree.
    """
    if topology is None and graph is not None:
        topology = graph.topology
    graph = graph if graph is not None else (build_graph(topology) if topology else None)
    if device is None and topology is not None:
        device = topology.device_map().get(device_id)
    metadata: Mapping[str, Any] = (
        device.metadata if device is not None and isinstance(device.metadata, Mapping) else {}
    )
    raw_config = metadata.get(CONFIG_KEY)
    config = None
    if isinstance(raw_config, Mapping):
        try:
            config = DeviceConfiguration.model_validate({"device_id": device_id, **raw_config})
        except Exception:  # noqa: BLE001 - a bad projection must not break inference
            config = None

    declared = MonitoringLevel.UNKNOWN
    try:
        declared = MonitoringLevel(str(metadata.get(MONITORING_KEY, "unknown")))
    except ValueError:
        declared = MonitoringLevel.UNKNOWN
    monitoring = derive_monitoring(device, graph, topology=topology, declared=declared)

    behavior = PowerBehavior.UNKNOWN
    try:
        behavior = PowerBehavior(str(metadata.get(BEHAVIOR_KEY, "unknown")))
    except ValueError:
        behavior = PowerBehavior.UNKNOWN
    connectivity = HaConnectivity.UNKNOWN
    try:
        connectivity = HaConnectivity(str(metadata.get(CONNECTIVITY_KEY, "unknown")))
    except ValueError:
        connectivity = HaConnectivity.UNKNOWN

    load_entities: tuple[str, ...] = ()
    mode_entities: tuple[str, ...] = ()
    declared_cts: tuple[str, ...] = ()
    controller_watts: dict[str, float] = {}
    expected_on_w: float | None = None
    expected_standby: float | None = None
    tolerance = 2.0
    notes: list[str] = []

    # Evidence-derived links are always present (written by the mapping tool);
    # user declarations are merged on top so a configured device keeps whatever
    # honest links already existed instead of losing them.
    evidence_load = _metadata_entities(metadata.get(STATE_ENTITIES_KEY))
    load_entities = tuple(dict.fromkeys(evidence_load))
    mode_entities = tuple(_metadata_entities(metadata.get(MODE_ENTITIES_KEY)))

    if config is not None:
        load_list, mode_list, reclassified = _split_entities(config)
        load_entities = tuple(dict.fromkeys([*load_entities, *load_list]))
        mode_entities = tuple(
            e for e in dict.fromkeys([*mode_entities, *mode_list]) if e not in load_entities
        )
        notes.extend(f"mode_entity_reclassified:{e}" for e in reclassified)
        if config.monitored_by_ct_ids:
            declared_cts = tuple(
                cid
                for cid in config.monitored_by_ct_ids
                if graph is None or cid in graph.channels
            )
        controller_watts = dict(
            config.controller.load_states() if config.controller is not None else {}
        )
        expected_on_w = _declared_light_watts(config)
        if expected_on_w is None and config.expected.on_w is not None:
            expected_on_w = float(config.expected.on_w)
        expected_standby = (
            config.expected.standby_w
            if config.expected.standby_w is not None
            else (config.light.standby_w if config.light is not None else None)
        )
        tolerance = config.expected.tolerance()
        if config.device_type != DeviceType.UNKNOWN:
            notes.append(f"device_type_{config.device_type}")
        notes.extend(_light_notes(config))
        if config.behavior == PowerBehavior.COMPLEX:
            notes.append("complex_controller_mode_is_context_only")
        if config.controller is not None and config.controller.obeys_control is False:
            notes.append("control_signal_not_obeyed")
        if config.controller is not None and not controller_watts:
            notes.append("no_confirmed_controller_watt_map")

    shared_unmodelled = False
    # A device that used to be labelled ``DEDICATED`` only because it was the
    # sole occupant of a *branch* CT is now ``SHARED``.  That relabel must not
    # change the solver: the row was already treated as fully attributable
    # (no unknown slack) when the label was ``DEDICATED``, and it must stay
    # that way.  Only the *label* narrows; the watts are untouched.
    derived_dedicated_downgrade = (
        declared == MonitoringLevel.UNKNOWN
        and monitoring == MonitoringLevel.SHARED
        and is_sole_occupant(device, graph, topology=topology)
    )
    if monitoring == MonitoringLevel.SHARED and not derived_dedicated_downgrade:
        observed = set(declared_cts)
        if not observed and device is not None:
            observed = set(device.dedicated_ct_ids) | set(device.branch_ct_ids)
        if topology is not None:
            siblings = [
                other.device_id
                for other in topology.devices
                if other.device_id != device_id
                and observed.intersection([*other.dedicated_ct_ids, *other.branch_ct_ids])
            ]
            # A circuit row may carry fixtures nobody declared, so a claimant may
            # only be credited the whole row when its draw is bounded.
            #
            # ``not siblings`` keeps the existing protection for a device alone
            # on its row.  The added clause fires only for a *declared* device
            # (``config is not None``) on a row it shares with another declared
            # fixture, and only when it states no wattage.
            #
            # That is the fix for a measured defect.  With the earlier rule,
            # declaring ONE fixture on `lights_ct2` (881.72 W) held the row as an
            # upper bound and reported `unknown`, while declaring a SECOND
            # fixture -- neither stating a wattage -- let the solver hand the
            # whole 881.72 W to whichever one was ON, graded `tier_3_measured`.
            # Declaring more of the truth made the answer less safe.
            #
            # The reasoning is ADR-0023's own: "the single lamp that is on owns
            # the row" is only sound when the declared fixtures are the *whole*
            # circuit.  This installation is the counter-example -- most CTs feed
            # more lights than are enumerated -- so an un-wattaged claimant
            # cannot prove the remainder is zero.  A stated wattage can: ADR-0025
            # then caps it, and the surplus stays unexplained instead of being
            # credited to a lamp that may not have drawn it.
            #
            # Topology-only devices (no configuration) keep the old behaviour, so
            # a discovered row shared by two placeholders is unchanged.
            shared_unmodelled = bool(observed) and (
                not siblings
                or (config is not None and bool(siblings) and expected_on_w is None)
            )
        else:  # pragma: no cover - topology is always present in production
            shared_unmodelled = bool(observed)
    if shared_unmodelled:
        notes.append("shared_circuit_without_modelled_siblings")

    sole_occupant = is_sole_occupant(device, graph, topology=topology)
    if derived_dedicated_downgrade:
        notes.append("sole_occupant_branch_ct_not_direct_measurable")

    return EffectiveConfig(
        device_id=device_id,
        monitoring=monitoring,
        behavior=behavior,
        connectivity=connectivity,
        device_type=DeviceType(config.device_type) if config is not None else DeviceType.UNKNOWN,
        monitored_by_ct_ids=declared_cts,
        load_entity_ids=load_entities,
        mode_entity_ids=mode_entities,
        controller_load_watts=controller_watts,
        expected_on_w=expected_on_w,
        expected_standby_w=expected_standby,
        expected_tolerance_w=tolerance,
        shared_unmodelled=shared_unmodelled,
        sole_occupant=sole_occupant,
        notes=tuple(notes),
        dimming_curve=_measured_curve(config),
    )


def _metadata_entities(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, Iterable) and not isinstance(value, (bytes, Mapping)):
        return [str(v).strip() for v in value if str(v).strip()]
    return []


def effective_configs(
    topology: Topology | None,
    *,
    graph: TopologyGraph | None = None,
) -> dict[str, EffectiveConfig]:
    """Resolve every device in the topology."""
    if topology is None:
        return {}
    graph = graph if graph is not None else build_graph(topology)
    return {
        device.device_id: effective_config(
            device.device_id, topology=topology, graph=graph, device=device
        )
        for device in topology.devices
    }


def _phase_for_cts(
    cts: list[str], channels: Mapping[str, Any]
) -> Phase:
    """The phase a device placed on these CT rows must carry.

    A device whose phase is ``UNKNOWN`` is not neutral: ``phase_coefficient``
    returns ``0.5`` for a single-line row, so one 19.57 W row would credit
    39.14 W to a single ON claimant -- the row counted twice.  A device created
    from a declaration therefore inherits the phase of the CT it sits on, which
    is the only placement knowledge available and is exactly right for the
    common case (a fixture behind one line).

    When the CTs disagree, or none is known, the phase stays ``UNKNOWN`` and the
    validation calls it out rather than inventing a line.
    """
    phases = set()
    for ct_id in cts:
        channel = channels.get(ct_id)
        if channel is None:
            continue
        phase = Phase(channel.phase)
        if phase != Phase.UNKNOWN:
            phases.add(phase)
    if len(phases) == 1:
        return phases.pop()
    return Phase.UNKNOWN


def cts_claimed_by_dedicated(
    topology: Topology, ct_ids: Iterable[str]
) -> tuple[str, str] | None:
    """``(device_id, ct_id)`` of a device that already owns one of these rows.

    A ``dedicated_device`` CT is a *direct measurement* of exactly one device:
    the whole row is that device's power.  Placing a second device on the same
    row and inferring its share would count the row twice -- the direct reading
    plus the inferred slice -- which is the double counting the electrical model
    exists to prevent.  So the caller must convert the row to a branch (or
    shared circuit) before another device may be placed on it, and this function
    is what detects the conflict.

    Both the device's declared list and the channel's own role are consulted:
    the two are normally consistent, but a half-applied edit can leave one
    saying ``dedicated`` and the other not, and trusting only one side would
    miss the conflict.
    """
    wanted = {str(cid) for cid in ct_ids}
    if not wanted:
        return None
    for device in topology.devices:
        for cid in device.dedicated_ct_ids:
            if cid in wanted:
                return device.device_id, cid
    channels = topology.channel_map()
    for cid in wanted:
        channel = channels.get(cid)
        if channel is None:
            continue
        if channel.ct_type == CTType.DEDICATED_DEVICE and channel.device_id:
            return str(channel.device_id), cid
    return None


def materialisation_blockers(
    topology: Topology,
    configurations: Iterable[DeviceConfiguration],
) -> dict[str, str]:
    """Configurations the topology cannot honour, with the reason why.

    Returned as ``{device_id: reason}`` so the caller can tell the user exactly
    what to change.  Nothing is created or re-pointed for these; the
    configuration is still stored by the caller, which keeps the request visible
    instead of silently discarded.
    """
    items = (
        configurations.devices
        if isinstance(configurations, DeviceConfigurationSet)
        else list(configurations)
    )
    known = {device.device_id for device in topology.devices}
    out: dict[str, str] = {}
    for config in items:
        owner = cts_claimed_by_dedicated(topology, config.monitored_by_ct_ids)
        if owner is None:
            continue
        owner_device, ct_id = owner
        if owner_device == config.device_id:
            # Its own dedicated row: the normal direct-measurement path.
            continue
        out[config.device_id] = (
            f"CT row {ct_id!r} is a dedicated measurement of device "
            f"{owner_device!r}; convert that channel to a shared circuit "
            "before another device can be placed on it"
        )
        if config.device_id in known:
            out[config.device_id] += (
                " (declaration stored, but it does not change attribution yet)"
            )
    return out


def _materialise_device(
    device_id: str,
    config: DeviceConfiguration,
    channels: Mapping[str, Any],
    topology: Topology | None = None,
) -> DeviceTopology | None:
    """Create the topology row for a device that only a declaration defines.

    A fixture behind a circuit has no channel of its own, so discovery never
    emits a device for it and ``project_configuration`` -- which walks
    ``topology.devices`` -- could never see the configuration at all.  Declaring
    "this switch is on that CT row" is enough to know a device exists there, so
    the row is created here.

    The CTs are *not* attached yet: the caller projects them through the same
    path as every other device, so provenance, retraction and the
    dedicated-vs-branch rule stay in one place.
    """
    cts = [
        cid
        for cid in config.monitored_by_ct_ids
        if cid in channels and getattr(channels[cid], "enabled", True)
    ]
    if not cts:
        # A declaration that names no usable CT cannot place the device.  Keep it
        # out of the topology rather than creating a row nothing can measure.
        return None
    if topology is not None and cts_claimed_by_dedicated(topology, cts) is not None:
        # Creating this row would put a second claim on a dedicated measurement
        # and count the circuit twice.  Refuse; the caller reports the conflict.
        return None
    return DeviceTopology(
        device_id=device_id,
        name=device_id,
        phase=_phase_for_cts(cts, channels),
        topology_status=TopologyStatus.CANDIDATE,
        metadata={
            MATERIALISED_KEY: str(config.configured_by),
            "note": "created from a device configuration; no discovered channel owned it",
        },
    )


def project_configuration(
    topology: Topology,
    configurations: DeviceConfigurationSet | Iterable[DeviceConfiguration],
) -> Topology:
    """Return a copy of ``topology`` with configuration projected into metadata.

    The input topology is not mutated: callers persist the result through the
    normal topology path, so a configuration change is a new topology version and
    every consumer sees it atomically.
    """
    items = (
        configurations.devices
        if isinstance(configurations, DeviceConfigurationSet)
        else list(configurations)
    )
    by_id = {c.device_id: c for c in items}
    channels = topology.channel_map()

    # A configuration may describe a device that no channel owns -- a fixture
    # behind a shared circuit.  Discovery only ever emits devices from monitor
    # channels, so such a device does not exist and the loop below would skip the
    # configuration entirely: the user saved it, the version bumped, and nothing
    # changed.  Materialise those rows first so they take the normal path.
    known = {device.device_id for device in topology.devices}
    extra: list[DeviceTopology] = []
    for device_id, config in by_id.items():
        if device_id in known:
            continue
        created = _materialise_device(device_id, config, channels, topology)
        if created is not None:
            extra.append(created)

    devices: list[DeviceTopology] = []
    for device in [*topology.devices, *extra]:
        config = by_id.get(device.device_id)
        metadata = dict(device.metadata or {})
        dedicated = list(device.dedicated_ct_ids)
        branch = list(device.branch_ct_ids)
        # Retract CTs a previous projection added for this device.  Only ids this
        # module put there are removed, so a CT the user assigned through the
        # channel wizard (``build_topology_update`` mirrors dedicated CTs itself)
        # survives untouched.
        previously = metadata.get(PROJECTED_CTS_KEY)
        if isinstance(previously, list):
            added = {str(c) for c in previously}
            dedicated = [c for c in dedicated if c not in added]
            branch = [c for c in branch if c not in added]
            metadata.pop(PROJECTED_CTS_KEY, None)
        if config is None:
            if metadata.get(MATERIALISED_KEY):
                # This row exists only because a declaration asked for it.  With
                # the declaration gone it is unreachable: no channel owns it, so
                # nothing can ever measure it again.  Drop it instead of leaving a
                # permanent zero in every device list.
                continue
            # A configuration removal must clear the projection, not leave a
            # stale mode or monitoring level behind.
            for key in (
                CONFIG_LOAD_ENTITIES_KEY,
                MODE_ENTITIES_KEY,
                MONITORING_KEY,
                BEHAVIOR_KEY,
                CONNECTIVITY_KEY,
                CONFIG_KEY,
            ):
                metadata.pop(key, None)
        else:
            load_list, mode_list, _ = _split_entities(config)
            # ``state_entity_ids`` is evidence-owned and is never rewritten here
            # (see module docstring); user declarations get their own key so a
            # removal cannot leave them behind looking like measured evidence.
            if load_list:
                metadata[CONFIG_LOAD_ENTITIES_KEY] = load_list
            else:
                metadata.pop(CONFIG_LOAD_ENTITIES_KEY, None)
            if mode_list:
                metadata[MODE_ENTITIES_KEY] = mode_list
            else:
                metadata.pop(MODE_ENTITIES_KEY, None)
            metadata[MONITORING_KEY] = str(config.monitoring)
            metadata[BEHAVIOR_KEY] = str(config.behavior)
            metadata[CONNECTIVITY_KEY] = str(config.connectivity)
            metadata[CONFIG_KEY] = config.model_dump(mode="json", exclude={"device_id"})
            # Declared CT placement is projected too so the solver and the event
            # intake agree about what this device sits under.
            if config.monitored_by_ct_ids:
                metadata["monitored_by_ct_ids"] = list(config.monitored_by_ct_ids)
            else:
                metadata.pop("monitored_by_ct_ids", None)
            # A declaration is not just metadata: the measurement matrix reads
            # ``DeviceTopology.dedicated_ct_ids`` / ``branch_ct_ids`` and
            # ``TopologyGraph`` derives every attribution row from them.  Writing
            # only ``metadata["monitored_by_ct_ids"]`` left the graph unchanged,
            # so declaring "this CT measures that device" produced no direct
            # measurement at all -- the exact case a user configures by hand.
            for cid in config.monitored_by_ct_ids:
                channel = channels.get(cid)
                if channel is None or not channel.enabled:
                    # Unknown or disabled channels are dropped by
                    # ``effective_config`` as well; never invent an attribution.
                    continue
                if channel.ct_type == CTType.DEDICATED_DEVICE:
                    # A dedicated CT may only name its own device.  A dedicated CT
                    # assigned elsewhere is a conflict, not an attribution.
                    if channel.device_id not in (None, device.device_id):
                        continue
                    if cid not in dedicated:
                        dedicated.append(cid)
                elif channel.ct_type != CTType.MAIN:
                    # A branch role (or an unclassified channel the user declared)
                    # becomes a branch attribution row for this device.
                    if cid not in branch:
                        branch.append(cid)
                else:
                    continue
                if cid not in metadata.setdefault(PROJECTED_CTS_KEY, []):
                    metadata[PROJECTED_CTS_KEY].append(cid)
            if not metadata.get(PROJECTED_CTS_KEY):
                metadata.pop(PROJECTED_CTS_KEY, None)
        devices.append(
            device.model_copy(
                update={
                    "metadata": metadata,
                    "dedicated_ct_ids": dedicated,
                    "branch_ct_ids": branch,
                }
            )
        )
    return topology.model_copy(update={"devices": devices, "version": topology.version + 1})


def configured_power(
    config: EffectiveConfig,
    state: str | None,
    *,
    state_is_on: bool,
    level: float | None = None,
) -> tuple[float, str] | None:
    """Power justified purely by configuration, plus the reason.

    Returns ``None`` when the configuration does not itself establish a value.

    Two rules here are load-bearing (ADR-0012):

    1. **A confirmed controller label may supply watts on its own.**  That is the
       opt-in assertion path: the user has said "this exact label draws this many
       watts" with ``role=load`` and ``confirmed=True``.  It is label-specific, so
       it does not need a separate load entity.
    2. **A generic ``expected.on_w`` requires a declared load signal that is
       positively ON.**  Without this, a device whose only declared entity is a
       *mode* reports its full running watts for every mode string forever — a
       thermostat emits ``heat_cool`` in 1845 of 1848 rows, so a 4.5 kW burner
       would be reported constantly, and an ``off`` state would still report it.
       A mode proves nothing about load, so it cannot unlock a watt value.

    ``state_is_on`` is the caller's verdict *after* per-device overrides (a spa
    reports ``Cooling`` at 22 W, which this installation maps to off).  It is
    passed in rather than recomputed here so configuration logic never guesses
    at integration-specific state semantics.

    ``level`` is the *measured* dimmer output, as a fraction of full scale, when
    the control entity reported one.  A measured curve interpolates to the watts
    this fixture draws at that level, which is a better answer than full-on
    watts whenever the light is dimmed.  Without a level (or without a curve)
    the flat ``expected_on_w`` is used and the reason says so, so a consumer can
    tell the two apart.
    """
    if state is None:
        return None
    if config.controller_load_watts:
        label = str(state).strip().lower()
        watts = config.controller_load_watts.get(label)
        if watts is not None:
            return float(watts), "confirmed_controller_state_power"
    if not config.load_entity_ids:
        # Only mode/context entities are declared: nothing proves runtime.
        return None
    if not state_is_on:
        return None
    if config.dimming_curve and level is not None:
        watts = interpolate_curve(config.dimming_curve, level)
        if watts is not None:
            return float(watts), "measured_dimming_curve"
    if config.expected_on_w is not None:
        return float(config.expected_on_w), "configured_running_power"
    return None


def interpolate_curve(
    curve: Sequence[tuple[float, float]], x: float
) -> float | None:
    """Piecewise-linear read of a measured ``(x, y)`` curve.

    Values outside the measured range are clamped to the nearest measured point
    rather than extrapolated: a dimmer observed between 40 % and 100 % says
    nothing about 5 %, and inventing that number would report watts the fixture
    was never seen to draw.  Exact points win, which keeps a lookup at a level
    that was actually measured bit-identical to the measurement.
    """
    if not curve:
        return None
    pts = sorted((float(a), float(b)) for a, b in curve)
    if x <= pts[0][0]:
        return pts[0][1]
    if x >= pts[-1][0]:
        return pts[-1][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:], strict=False):
        if x0 <= x <= x1:
            if x1 == x0:
                return y1
            frac = (x - x0) / (x1 - x0)
            return y0 + frac * (y1 - y0)
    return pts[-1][1]


def is_mode_only(config: EffectiveConfig) -> bool:
    """True when every declared signal is a mode, i.e. nothing proves runtime."""
    return bool(config.mode_entity_ids) and not config.load_entity_ids


def has_ct_type(topology: Topology | None, ct_id: str, ct_type: CTType) -> bool:
    if topology is None:
        return False
    channel = topology.channel_map().get(ct_id)
    return channel is not None and channel.ct_type == ct_type


__all__ = [
    "BEHAVIOR_KEY",
    "CONFIG_KEY",
    "CONNECTIVITY_KEY",
    "EffectiveConfig",
    "LINEAR_DIMMING_DEFAULT",
    "MODE_ENTITIES_KEY",
    "MONITORING_KEY",
    "configured_power",
    "derive_monitoring",
    "effective_config",
    "effective_configs",
    "has_ct_type",
    "interpolate_curve",
    "is_mode_only",
    "is_sole_occupant",
    "project_configuration",
]
