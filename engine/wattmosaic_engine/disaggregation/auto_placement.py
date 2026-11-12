"""Turn correlation evidence into device placements (ADR-0024).

:mod:`wattmosaic_engine.ingestion.correlation` answers *which CT row a switch is
on*.  This module is the only place that turns that answer into something the
inference engine acts on, and it is deliberately a separate step for three
reasons:

1. **Acceptance is not a side effect.**  Correlation is pure and tests can read
   its verdict without a database.  Placement is a write, so it stays behind an
   explicit call the facade makes only when the user opted in.
2. **Conflicts must be reportable, not silently dropped.**  A row already owned
   by a dedicated measurement cannot hold a second device (the circuit would be
   counted twice).  Returning the reason lets the UI tell the user exactly which
   channel to convert instead of pretending nothing happened.
3. **User knowledge always wins.**  An entity a person already configured is
   never rewritten by a guess, however good the correlation looked.

What the produced configuration claims -- and what it does not
-------------------------------------------------------------
An auto placement states three things it actually observed:

* *this entity is a runtime signal for this device* -- it moved, repeatedly;
* *this CT row is where it is* -- the row answered, repeatedly, in the right
  direction, and no competing row did;
* *this is what it draws* -- the measured step, and for a dimmer the measured
  level→watts curve rather than one full-on number.

It states them with ``configured_by="auto"``, so nothing downstream can mistake
them for a person's assertion, and the confidence tier is never raised by them.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from wattmosaic_engine.contracts import (
    CTType,
    DeviceConfiguration,
    DeviceConfigurationSet,
    ExpectedPower,
    HaConnectivity,
    LightConfiguration,
    MonitoringLevel,
    Topology,
)
from wattmosaic_engine.disaggregation.configuration import cts_claimed_by_dedicated
from wattmosaic_engine.ingestion.correlation import CorrelationReport

#: Prefix for a device this module creates.  Kept distinct from discovery's
#: ``dev_`` rows so a reader can tell a correlated fixture from a CT-derived
#: placeholder without consulting the configuration store.
AUTO_DEVICE_PREFIX = "dev_auto_"

_SLUG_RE = re.compile(r"[^a-z0-9]+")


@dataclass(slots=True, frozen=True)
class Placement:
    """One accepted correlation, ready to be written."""

    device_id: str
    configuration: DeviceConfiguration
    #: Human-readable justification.  Shown, never parsed.
    evidence: tuple[str, ...] = field(default_factory=tuple)
    #: Display name the state snapshot reported for this fixture.  A device's
    #: display name belongs to the topology row (``DeviceTopology.name``), not to
    #: the persisted configuration, so it travels on the placement and is handed
    #: to materialisation.  ``None`` means no name was supplied -- the engine then
    #: keeps the device id rather than inventing one from the slug.
    name: str | None = None


@dataclass(slots=True, frozen=True)
class PlacementPlan:
    """What auto placement would do, and why it declines the rest."""

    accepted: tuple[Placement, ...] = field(default_factory=tuple)
    #: ``entity_id -> reason`` for candidates that were refused.
    refused: Mapping[str, str] = field(default_factory=dict)

    def device_ids(self) -> tuple[str, ...]:
        return tuple(p.device_id for p in self.accepted)


def auto_device_id(entity_id: str) -> str:
    """Stable device id for a fixture, derived only from its entity id.

    Stable is the point: the same switch must map to the same device across
    restarts, or every re-run would create a second device for the same lamp and
    the accumulated energy statistics would split.
    """
    stem = str(entity_id).strip()
    domain, _, object_id = stem.partition(".")
    basis = object_id if domain and object_id else stem
    slug = _SLUG_RE.sub("_", basis.lower()).strip("_")
    return f"{AUTO_DEVICE_PREFIX}{slug or 'fixture'}"


def _dimming_curve_config(
    curve: tuple[tuple[float, float], ...],
) -> LightConfiguration | None:
    """Express measured watts as the ``fraction_of_full_on`` curve the contract holds.

    ``LightConfiguration.dimming_curve`` is a *fixture* description, so it stores
    output as a fraction of full-on watts.  Correlation measured absolute watts,
    so the two are reconciled here: the largest measured point becomes "full on"
    and every other point is expressed relative to it.

    Returns ``None`` when the curve cannot describe a ratio -- a single point, a
    non-positive maximum, or levels outside ``[0, 1]``.  A malformed curve is
    dropped rather than repaired, because a repaired curve would report a shape
    that was never measured.
    """
    points = [
        (float(level), float(watts))
        for level, watts in curve
        if 0.0 < float(level) <= 1.0 and float(watts) > 0.0
    ]
    if len(points) < 2:
        return None
    top = max(watts for _, watts in points)
    if top <= 0.0:
        return None
    relative = sorted((level, min(watts / top, 1.0)) for level, watts in points)
    return LightConfiguration(
        bulb_count=1,
        watts_per_bulb_on=top,
        dimming_curve=relative,
    )


def _existing_state_entities(
    configurations: DeviceConfigurationSet | Iterable[DeviceConfiguration],
) -> set[str]:
    """Entities the *user* has already declared, ignoring prior auto placements.

    Auto placements are excluded on purpose.  They are re-derived on every run;
    counting them here would make the second run see its own output as a user
    declaration, refuse the candidate, and then withdraw the device it created a
    minute earlier.  Only a person's assertion blocks a correlation.
    """
    items = (
        configurations.devices
        if isinstance(configurations, DeviceConfigurationSet)
        else list(configurations)
    )
    out: set[str] = set()
    for config in items:
        if not config.is_user_asserted:
            continue
        out.update(str(e) for e in config.state_entity_ids)
        if config.controller is not None:
            out.update(str(e) for e in config.controller.load_entity_ids)
    return out


def plan_placements(
    *,
    report: CorrelationReport,
    topology: Topology,
    configurations: DeviceConfigurationSet | Iterable[DeviceConfiguration],
    assume_owned: bool = False,
    labels: Mapping[str, str] | None = None,
) -> PlacementPlan:
    """Decide which accepted candidates may become devices.

    A candidate is placed only when all of these hold:

    * its row is a **branch** CT.  A ``main`` row sees the whole installation and
      a ``dedicated_device`` row is already one device's direct measurement;
      placing a fixture on either would misattribute power;
    * the row is **not already claimed** by a dedicated measurement
      (``assume_owned`` bypasses this for a caller that has just converted the
      channel and deliberately re-runs -- it never invents the conversion);
    * the entity is **not already configured**.  A person's declaration always
      wins over a correlation;
    * the measured watts are **positive**.  A zero step cannot justify a device,
      because an on/off transition that moved no measured power is not evidence
      of a load.

    Everything else is reported in :attr:`PlacementPlan.refused` with the reason,
    so the caller can surface it rather than silently discarding the evidence.

    ``labels`` maps ``entity_id -> display name`` (the friendly name the state
    snapshot reported).  It only ever *labels* a placement; it never decides one,
    because a name is display data and a slug is not a name.  Without a label the
    created device keeps its id.
    """
    channels = topology.channel_map()
    known_entities = _existing_state_entities(configurations)
    existing_devices = {device.device_id for device in topology.devices}
    label_for = labels or {}
    accepted: list[Placement] = []
    refused: dict[str, str] = {}

    for candidate in report.candidates:
        entity_id = candidate.entity_id
        channel = channels.get(candidate.ct_id)
        if channel is None:
            refused[entity_id] = f"CT row {candidate.ct_id!r} no longer exists"
            continue
        if channel.ct_type == CTType.MAIN:
            refused[entity_id] = (
                f"CT row {candidate.ct_id!r} measures the whole installation; "
                "a fixture cannot be placed on a main row"
            )
            continue
        if channel.ct_type == CTType.DEDICATED_DEVICE and not assume_owned:
            refused[entity_id] = (
                f"CT row {candidate.ct_id!r} is a dedicated measurement of another "
                "device; convert that channel to a shared circuit first"
            )
            continue
        if channel.ct_type not in (CTType.BRANCH, CTType.DEDICATED_DEVICE):
            refused[entity_id] = f"CT row {candidate.ct_id!r} is not a load row"
            continue
        if not assume_owned:
            owner = cts_claimed_by_dedicated(topology, [candidate.ct_id])
            if owner is not None:
                refused[entity_id] = (
                    f"CT row {candidate.ct_id!r} is a dedicated measurement of "
                    f"{owner[0]!r}; convert that channel to a shared circuit first"
                )
                continue
        if entity_id in known_entities:
            refused[entity_id] = "already configured by the user; left unchanged"
            continue
        if candidate.typical_w <= 0.0:
            refused[entity_id] = (
                "the measured step was not positive, so no load can be attributed"
            )
            continue

        device_id = auto_device_id(entity_id)
        if device_id in existing_devices:
            # Idempotent re-run: the device already exists, so its configuration
            # will be re-projected rather than created twice.
            pass

        light = _dimming_curve_config(candidate.dimming_curve)
        configuration = DeviceConfiguration(
            device_id=device_id,
            connectivity=HaConnectivity.CONNECTED,
            monitoring=MonitoringLevel.SHARED,
            state_entity_ids=[entity_id],
            monitored_by_ct_ids=[candidate.ct_id],
            configured_by="auto",
            expected=ExpectedPower(
                on_w=float(candidate.typical_w),
                # The measured step is the load; a fixture switching on does not
                # draw more than what the row showed.  The tolerance is wide
                # because the CT row is shared and a sibling's step can overlap.
                tolerance_fraction=0.25,
            ),
            light=light,
            notes=[
                f"auto_placement:{candidate.corroborations} corroborations",
                f"agreement:{candidate.agreement:.2f}",
                *("measured_dimming_curve" for _ in [0] if light is not None),
            ],
        )
        accepted.append(
            Placement(
                device_id=device_id,
                configuration=configuration,
                evidence=(*candidate.evidence, f"measured {candidate.typical_w:.2f} W"),
                name=label_for.get(entity_id) or None,
            )
        )

    return PlacementPlan(accepted=tuple(accepted), refused=refused)


def merge_placements(
    configurations: DeviceConfigurationSet,
    plan: PlacementPlan,
) -> DeviceConfigurationSet:
    """Return a new configuration set with the plan applied.

    Auto placements are replaced wholesale on each run so a fixture that stops
    correlating is withdrawn, while user declarations are never touched.  This
    asymmetry is the point: an inference may be revised, an assertion may not.
    """
    by_device = {
        device_id: config
        for device_id, config in configurations.by_device().items()
        if config.configured_by != "auto"
    }
    for placement in plan.accepted:
        by_device[placement.device_id] = placement.configuration
    return DeviceConfigurationSet(
        version=configurations.version + 1,
        updated_at=configurations.updated_at,
        devices=list(by_device.values()),
    )


__all__ = [
    "AUTO_DEVICE_PREFIX",
    "Placement",
    "PlacementPlan",
    "auto_device_id",
    "merge_placements",
    "plan_placements",
]
