"""Turn correlation evidence into v3 device placements (ADR-0024, contract 3.0).

:mod:`wattmosaic_engine.ingestion.correlation` answers *which CT row a switch is
on*.  This module is the only place that turns that answer into something the
engine acts on, and it is deliberately a separate step for three reasons:

1. **Acceptance is not a side effect.**  Correlation is pure and tests can read
   its verdict without a database.  Placement is a write, so it stays behind an
   explicit call the facade makes only when the user opted in.
2. **Conflicts must be reportable, not silently dropped.**  A reader already
   owned by a single-subject declaration cannot hold a second device (the
   circuit would be counted twice).  Returning the reason lets the UI tell the
   user exactly which endpoint to change instead of pretending nothing happened.
3. **User knowledge always wins.**  An entity a person already linked is never
   rewritten by a guess, however good the correlation looked.

What an applied placement writes
--------------------------------
Only v3 rows, each marked as an inference so nothing downstream mistakes it for
a person's assertion:

* a :class:`~wattmosaic_engine.contracts.PhysicalDevice` with a stable id derived
  from the entity id (:func:`auto_device_id`);
* a ``load_state`` :class:`~wattmosaic_engine.contracts.HaEntityLink` from the
  fixture's entity to that device, ``declared_by="discovery"``;
* a CANDIDATE / INFERRED :class:`~wattmosaic_engine.contracts.MappingEdge` from
  the device to the reader it was observed on, ``declared_by="auto_placement"``
  (:func:`mapping_edges_for_plan`).
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import datetime

from wattmosaic_engine.contracts import (
    EdgeEvidence,
    HaEntityLink,
    HaEntityRole,
    MappingEdge,
    Observability,
    PhysicalDevice,
    TopologyEdgeState,
)
from wattmosaic_engine.ingestion.correlation import CorrelationReport

#: Prefix for a device this module creates, so a reader can tell a correlated
#: fixture from a declared device without consulting any other store.
AUTO_DEVICE_PREFIX = "dev_auto_"

#: Reader roles (see :func:`reader_roles`).  Only a ``branch`` reader may hold
#: a placed fixture; a ``main`` reader sees the whole installation.
ROLE_MAIN = "main"
ROLE_BRANCH = "branch"
ROLE_OTHER = "other"

_SLUG_RE = re.compile(r"[^a-z0-9]+")


@dataclass(slots=True, frozen=True)
class Placement:
    """One accepted correlation, ready to be written."""

    device_id: str
    entity_id: str
    reader_id: str
    #: The measured on-step of the fixture, in watts (always positive).
    on_w: float
    #: Measured ``(level, fraction_of_full_on)`` points for a dimmer, or ``None``.
    dimming_curve: tuple[tuple[float, float], ...] | None = None
    #: Human-readable justification.  Shown, never parsed.
    evidence: tuple[str, ...] = field(default_factory=tuple)
    #: Display name the state snapshot reported for this fixture.  ``None`` means
    #: no name was supplied -- the device then keeps its id as its name.
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
    restarts, or every re-run would create a second device for the same lamp.
    """
    stem = str(entity_id).strip()
    domain, _, object_id = stem.partition(".")
    basis = object_id if domain and object_id else stem
    slug = _SLUG_RE.sub("_", basis.lower()).strip("_")
    return f"{AUTO_DEVICE_PREFIX}{slug or 'fixture'}"


def reader_roles(endpoints: Collection[object]) -> dict[str, str]:
    """``endpoint_id -> role`` for v3 measurement endpoints.

    A panel-main endpoint or a ``panel_feed`` scope is ``main``; any other CT is
    a ``branch`` (a circuit row a fixture may sit on); everything else (plugs,
    appliance monitors, virtual readers) is ``other``.
    """

    def _text(value: object) -> str:
        return str(getattr(value, "value", value) or "")

    out: dict[str, str] = {}
    for endpoint in endpoints:
        get = endpoint.get if isinstance(endpoint, Mapping) else (
            lambda key, _e=endpoint: getattr(_e, key, None)
        )
        endpoint_id = get("endpoint_id")
        if not endpoint_id:
            continue
        kind = _text(get("kind"))
        scope = _text(get("scope"))
        if kind == "panel_main" or scope == "panel_feed":
            out[str(endpoint_id)] = ROLE_MAIN
        elif kind == "ct":
            out[str(endpoint_id)] = ROLE_BRANCH
        else:
            out[str(endpoint_id)] = ROLE_OTHER
    return out


def relative_dimming_curve(
    curve: tuple[tuple[float, float], ...],
) -> tuple[tuple[float, float], ...] | None:
    """Express measured watts as a ``fraction_of_full_on`` curve.

    The largest measured point becomes "full on" and every other point is
    expressed relative to it.  Returns ``None`` when the curve cannot describe a
    ratio -- a single point, a non-positive maximum, or levels outside
    ``(0, 1]``.  A malformed curve is dropped rather than repaired, because a
    repaired curve would report a shape that was never measured.
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
    return tuple(sorted((level, min(watts / top, 1.0)) for level, watts in points))


def plan_placements(
    *,
    report: CorrelationReport,
    readers: Mapping[str, str],
    user_entities: Collection[str] = (),
    owned_readers: Mapping[str, str] | None = None,
    labels: Mapping[str, str] | None = None,
) -> PlacementPlan:
    """Decide which accepted candidates may become devices.

    ``readers`` maps a reader id to its role (:func:`reader_roles`).  A
    candidate is placed only when all of these hold:

    * its reader exists and is a **branch** reader.  A ``main`` reader sees the
      whole installation; placing a fixture on it would misattribute power;
    * the reader is **not owned** by a single-subject declaration
      (``owned_readers``: reader id -> the subject it measures);
    * the entity is **not already linked by a person** (``user_entities``);
    * the measured watts are **positive**.  An on/off transition that moved no
      measured power is not evidence of a load.

    Everything else is reported in :attr:`PlacementPlan.refused` with the reason.
    ``labels`` maps ``entity_id -> display name``; it only ever labels a
    placement and never decides one.
    """
    owned = dict(owned_readers or {})
    known_entities = set(user_entities)
    label_for = labels or {}
    accepted: list[Placement] = []
    refused: dict[str, str] = {}

    for candidate in report.candidates:
        entity_id = candidate.entity_id
        reader_id = candidate.ct_id
        role = readers.get(reader_id)
        if role is None:
            refused[entity_id] = f"CT row {reader_id!r} no longer exists"
            continue
        if role == ROLE_MAIN:
            refused[entity_id] = (
                f"CT row {reader_id!r} measures the whole installation; "
                "a fixture cannot be placed on a main row"
            )
            continue
        if role != ROLE_BRANCH:
            refused[entity_id] = f"CT row {reader_id!r} is not a load row"
            continue
        owner = owned.get(reader_id)
        if owner is not None:
            refused[entity_id] = (
                f"CT row {reader_id!r} is already owned by device {owner!r}; "
                "convert that endpoint to a shared circuit first"
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
        accepted.append(
            Placement(
                device_id=auto_device_id(entity_id),
                entity_id=entity_id,
                reader_id=reader_id,
                on_w=float(candidate.typical_w),
                dimming_curve=relative_dimming_curve(candidate.dimming_curve),
                evidence=(*candidate.evidence, f"measured {candidate.typical_w:.2f} W"),
                name=label_for.get(entity_id) or None,
            )
        )

    return PlacementPlan(accepted=tuple(accepted), refused=refused)


def devices_for_plan(plan: PlacementPlan, *, created_at: datetime) -> tuple[PhysicalDevice, ...]:
    """The :class:`PhysicalDevice` rows an applied plan implies."""
    return tuple(
        PhysicalDevice(
            device_id=placement.device_id,
            name=placement.name or placement.device_id,
            origin="ha_device",
            created_at=created_at,
        )
        for placement in plan.accepted
    )


def links_for_plan(plan: PlacementPlan, *, valid_from: datetime) -> tuple[HaEntityLink, ...]:
    """One ``load_state`` link per placed fixture, marked as discovered."""
    return tuple(
        HaEntityLink(
            entity_id=placement.entity_id,
            target_kind="device",
            target_id=placement.device_id,
            role=HaEntityRole.LOAD_STATE,
            valid_from=valid_from,
            declared_by="discovery",
        )
        for placement in plan.accepted
    )


def mapping_edges_for_plan(
    plan: PlacementPlan,
    *,
    valid_from: datetime,
) -> tuple[MappingEdge, ...]:
    """Build the v3 :class:`MappingEdge` rows an applied plan implies.

    Auto placement is an inference, not a user declaration: every edge it
    writes is ``declared_by="auto_placement"`` and starts at CANDIDATE with
    INFERRED observability (HB §4.3 — one event never promotes an edge past
    CANDIDATE).
    """
    edges = [
        MappingEdge(
            mapping_edge_id=f"me::{placement.device_id}->{placement.reader_id}",
            subject_kind="device",
            subject_id=placement.device_id,
            endpoint_id=placement.reader_id,
            observability=Observability.INFERRED,
            lifecycle=TopologyEdgeState.CANDIDATE,
            declared_by="auto_placement",
            evidence=EdgeEvidence(
                support=len(placement.evidence),
                independent_causes=1,
            ),
            valid_from=valid_from,
        )
        for placement in plan.accepted
    ]
    return tuple(sorted(edges, key=lambda edge: edge.mapping_edge_id))


__all__ = [
    "AUTO_DEVICE_PREFIX",
    "Placement",
    "PlacementPlan",
    "ROLE_BRANCH",
    "ROLE_MAIN",
    "ROLE_OTHER",
    "auto_device_id",
    "devices_for_plan",
    "links_for_plan",
    "mapping_edges_for_plan",
    "plan_placements",
    "reader_roles",
    "relative_dimming_curve",
]
