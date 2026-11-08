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

from collections.abc import Iterable, Mapping
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
    PowerBehavior,
    Topology,
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
    #: True when the user asserted a shared circuit we cannot fully separate.
    shared_unmodelled: bool = False
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

    ``CTType`` alone is not enough: several appliances in a typical installation
    sit on *branch* CTs that only they occupy.  Calling those ``shared`` would
    wrongly demote a correct dedicated measurement.  What matters is whether
    another topology device shares the same attribution rows.
    """
    if declared != MonitoringLevel.UNKNOWN:
        return declared
    if device is None:
        return MonitoringLevel.UNKNOWN
    if graph is None:  # pragma: no cover - graph is always supplied in production
        return MonitoringLevel.UNKNOWN
    if graph.dedicated_channel_for(device.device_id) is not None:
        return MonitoringLevel.DEDICATED
    topology = topology if topology is not None else graph.topology
    rows = set(graph.attribution_ct_ids(device.device_id))
    if not rows:
        return MonitoringLevel.HOME_TOTAL_ONLY
    for other in topology.devices:
        if other.device_id == device.device_id:
            continue
        other_rows = set(other.dedicated_ct_ids) | set(other.branch_ct_ids)
        if rows & other_rows:
            return MonitoringLevel.SHARED
    return MonitoringLevel.DEDICATED


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
    if monitoring == MonitoringLevel.SHARED:
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
            shared_unmodelled = bool(observed) and not siblings
        else:  # pragma: no cover - topology is always present in production
            shared_unmodelled = bool(observed)
    if shared_unmodelled:
        notes.append("shared_circuit_without_modelled_siblings")

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
        notes=tuple(notes),
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
    devices: list[DeviceTopology] = []
    for device in topology.devices:
        config = by_id.get(device.device_id)
        metadata = dict(device.metadata or {})
        if config is None:
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
        devices.append(device.model_copy(update={"metadata": metadata}))
    return topology.model_copy(update={"devices": devices, "version": topology.version + 1})


def configured_power(
    config: EffectiveConfig, state: str | None, *, state_is_on: bool
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
    if config.expected_on_w is not None:
        return float(config.expected_on_w), "configured_running_power"
    return None


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
    "is_mode_only",
    "project_configuration",
]
