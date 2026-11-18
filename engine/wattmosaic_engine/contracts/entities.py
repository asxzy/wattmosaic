"""Entity contracts (owned by K01): the four HB §1.1 object kinds.

This module defines the canonical entities for HB §1 and their many-to-many
links (§1.2):

* :class:`PhysicalDevice` — a real-world appliance or load (DEC-04 preserves
  legacy ids such as ``dev_ct_07`` verbatim; new devices use ``pd_<ulid>``).
* :class:`LoadComponent` — an electrically distinguishable part of a device
  (user-declared only, DEC-07).
* :class:`HaEntity` / :class:`HaEntityLink` — Home Assistant evidence and its
  many-to-many links to devices *or* components (DEC-08).
* :class:`MeasurementEndpoint` / :class:`EndpointConfigRevision` — an
  independent measurement identity with append-only configuration history
  (HB §1.1 D).  A CT endpoint's id equals the legacy ``ct_id``.

Naming / reuse notes
--------------------
The existing :class:`~wattmosaic_engine.contracts.legacy.LoadShape`,
:class:`~wattmosaic_engine.contracts.legacy.HaEntityRole` and
:class:`~wattmosaic_engine.contracts.legacy.MeasurementBasis` enums are imported
from ``legacy`` and re-used -- they are deliberately **not** exported from
this module's ``__all__`` because those names already belong to the legacy
surface.  None of the nine new public names below collide with ``legacy``, so
no ``V3`` suffix is needed here.

``interfaces.md`` §2 types ``EndpointConfigRevision.measurement_type`` as
``"MeasurementType"`` (owned by K02, which has not landed yet).  To avoid
defining a parallel public name that would collide with K02's export, the field
is typed as a ``Literal`` of the exact five values from interfaces.md §3.  With
the shared ``Model`` base's ``use_enum_values=True`` this is behaviourally
identical to the future enum-typed field.

The ``MeasurementEndpoint.revisions`` field is the in-memory list of loaded
:class:`EndpointConfigRevision` objects.  It is needed to enforce the DEC-05
guard ("latest revision declared_by == 'user'") on the endpoint itself; storage
(S13) persists these in the ``endpoint_config_revisions`` table.

This module is additive only: CONTRACT_VERSION stays ``2.4.0`` and no legacy
contract is modified.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import model_validator

from .legacy import HaEntityRole, LoadShape, MeasurementBasis, Model  # noqa: F401  (reused)

__all__: list[str] = [
    "EndpointKind",
    "EndpointScope",
    "ScopeStatus",
    "MeasurementEndpoint",
    "EndpointConfigRevision",
    "PhysicalDevice",
    "LoadComponent",
    "HaEntity",
    "HaEntityLink",
]


# --------------------------------------------------------------------------- #
# Endpoint enums (interfaces.md §2).
# --------------------------------------------------------------------------- #


class EndpointKind(str, Enum):
    """What physical instrument produces the measurements (HB §1.1 D)."""

    CT = "ct"
    SMART_PLUG = "smart_plug"
    APPLIANCE_MONITOR = "appliance_monitor"
    PANEL_MAIN = "panel_main"
    VIRTUAL = "virtual"


class EndpointScope(str, Enum):
    """What an endpoint declares it measures (DEC-05).

    A *single-subject* scope is a property of an endpoint that physically
    measures one load (smart plug, appliance energy monitor).  It is a **candidate**
    until the user confirms it; a circuit CT never gets it automatically.
    """

    CIRCUIT = "circuit"
    PANEL_FEED = "panel_feed"
    SINGLE_SUBJECT = "single_subject"
    UNKNOWN = "unknown"


class ScopeStatus(str, Enum):
    """Confirmation lifecycle of an endpoint's declared scope."""

    CANDIDATE = "candidate"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


# --------------------------------------------------------------------------- #
# Measurement endpoint and its append-only configuration history.
# --------------------------------------------------------------------------- #


class EndpointConfigRevision(Model):
    """One immutable, append-only configuration revision of an endpoint.

    ``valid_until > valid_from`` when set (open-ended revisions use ``None``).
    ``declared_by`` records who introduced the revision; DEC-05 requires a
    ``"user"`` declaration before a CT may have a confirmed SINGLE_SUBJECT
    scope.
    """

    endpoint_id: str
    revision: int
    valid_from: datetime
    valid_until: datetime | None = None
    phase: Literal["l1", "l2", "both", "unknown"]
    leg: Literal["l1", "l2"] | None = None
    voltage_reference_v: float | None = None
    polarity: Literal["normal", "inverted"]
    multiplier: float
    calibration_version: str
    measurement_type: Literal[
        "instantaneous",
        "window_average",
        "device_reported",
        "calculated",
        "accumulated_energy",
    ]
    measurement_window_ms: int | None = None
    expected_interval_s: float | None = None
    resolution_w: float | None = None
    declared_by: Literal["user", "discovery", "migration"]

    @model_validator(mode="after")
    def _validate_interval(self) -> EndpointConfigRevision:
        if self.valid_until is not None and self.valid_until <= self.valid_from:
            raise ValueError("valid_until must be strictly after valid_from")
        return self


class MeasurementEndpoint(Model):
    """A source of electrical measurements with its own identity and scope.

    There is deliberately **no** ``device_id`` foreign key: one reader may
    observe many devices and many readers may observe the same device
    (HB §1.2).  ``scope_subject_id`` is set iff ``scope == SINGLE_SUBJECT``.
    """

    endpoint_id: str  # == legacy ct_id for CTs (preserved)
    kind: EndpointKind
    entity_id: str | None  # HA power entity
    energy_entity_id: str | None = None
    display_name: str | None = None
    scope: EndpointScope
    scope_status: ScopeStatus
    scope_subject_id: str | None = None
    panel_id: str | None = None
    enabled: bool
    created_at: datetime
    revisions: list[EndpointConfigRevision] = []

    @model_validator(mode="after")
    def _validate_scope(self) -> MeasurementEndpoint:
        subject = self.scope_subject_id
        if self.scope == EndpointScope.SINGLE_SUBJECT:
            if subject is None:
                raise ValueError("scope_subject_id is required for SINGLE_SUBJECT scope")
            if self.kind == EndpointKind.CT:
                # DEC-05: a circuit CT is never auto single-subject.  Only an
                # explicitly user-declared, confirmed scope is allowed.
                latest = self.latest_revision
                if self.scope_status != ScopeStatus.CONFIRMED or latest is None:
                    raise ValueError(
                        "a CT with SINGLE_SUBJECT scope requires CONFIRMED status "
                        "and a user-declared latest revision"
                    )
                if latest.declared_by != "user":
                    raise ValueError(
                        "a CT with SINGLE_SUBJECT scope requires a user-declared latest revision"
                    )
        elif subject is not None:
            raise ValueError("scope_subject_id is only valid for SINGLE_SUBJECT scope")
        return self

    @property
    def latest_revision(self) -> EndpointConfigRevision | None:
        """The revision with the highest ``revision`` number, or ``None``."""
        if not self.revisions:
            return None
        return max(self.revisions, key=lambda r: r.revision)


# --------------------------------------------------------------------------- #
# Physical device and its components.
# --------------------------------------------------------------------------- #


class PhysicalDevice(Model):
    """A real-world appliance or load (HB §1.1 A).

    ``device_id`` is opaque and preserved verbatim for legacy devices; new ids
    follow ``pd_<ulid>`` (DEC-04).  HA connectivity is optional: some devices
    have no HA entity at all (HB §1.2).
    """

    device_id: str
    name: str
    device_type: str | None = None
    load_shape: LoadShape | None = None
    sign_class: Literal["consumer", "producer", "bidirectional"] = "consumer"
    ha_device_id: str | None = None
    area_id: str | None = None  # display grouping only (DEC-27)
    origin: Literal["user", "ha_device", "unknown_profile_promotion", "migration"]
    created_at: datetime
    retired_at: datetime | None = None


class LoadComponent(Model):
    """A distinguishable electrical component of a device (HB §1.1 B).

    Components are user-declared only (DEC-07); learning may suggest but never
    creates one.  One physical device may have many components.
    """

    component_id: str  # "lc_<ulid>"
    device_id: str
    name: str
    load_shape: LoadShape | None = None
    sign_class: Literal["consumer", "producer", "bidirectional"] = "consumer"


# --------------------------------------------------------------------------- #
# HA entity and its many-to-many links.
# --------------------------------------------------------------------------- #


class HaEntity(Model):
    """A Home Assistant entity or control interface (HB §1.1 C).

    An entity is behavioural evidence, not automatically a physical load.
    """

    entity_id: str
    domain: str
    ha_device_id: str | None = None
    first_seen: datetime
    last_seen: datetime


class HaEntityLink(Model):
    """A many-to-many link from an HA entity to a device or component (DEC-08).

    One entity may link to several subjects; ``target_kind`` selects which id
    space ``target_id`` belongs to.  ``valid_until > valid_from`` when set.
    """

    entity_id: str
    target_kind: Literal["device", "component"]
    target_id: str
    role: HaEntityRole
    valid_from: datetime
    valid_until: datetime | None = None
    declared_by: Literal["user", "ha_device_link", "discovery", "migration"]

    @model_validator(mode="after")
    def _validate_interval(self) -> HaEntityLink:
        if self.valid_until is not None and self.valid_until <= self.valid_from:
            raise ValueError("valid_until must be strictly after valid_from")
        return self


# --------------------------------------------------------------------------- #
# Generated JSON-schema exports (see tools/dev/export_schemas.py).
# --------------------------------------------------------------------------- #

SCHEMA_EXPORTS: list[tuple[str, type]] = [
    ("measurement_endpoint", MeasurementEndpoint),
    ("endpoint_config_revision", EndpointConfigRevision),
    ("physical_device", PhysicalDevice),
    ("load_component", LoadComponent),
    ("ha_entity", HaEntity),
    ("ha_entity_link", HaEntityLink),
]
