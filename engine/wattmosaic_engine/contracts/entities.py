"""Entity contracts (K01): the four HB §1.1 object kinds.

* :class:`PhysicalDevice` — a real-world appliance or load (DEC-04 preserves
  migrated ids verbatim; new devices use ``pd_<ulid>``).
* :class:`LoadComponent` — an electrically distinguishable part of a device
  (user-declared only, DEC-07).
* :class:`HaEntity` / :class:`HaEntityLink` — Home Assistant evidence and its
  many-to-many links to devices *or* components (DEC-08), each with an
  :class:`HaEntityRole`.
* :class:`MeasurementEndpoint` / :class:`EndpointConfigRevision` — an
  independent measurement identity with append-only configuration history
  (HB §1.1 D).  A CT endpoint keeps the reader id it always had.
* :class:`LoadShape` — the declared load-shape class of a device or component.

``EndpointConfigRevision.measurement_type`` is typed as a ``Literal`` of the
five :class:`~wattmosaic_engine.contracts.MeasurementType` values; with the
shared ``Model`` base's ``use_enum_values=True`` this is behaviourally identical
to an enum-typed field.  ``MeasurementEndpoint.revisions`` is the in-memory list
of loaded revisions, needed to enforce the DEC-05 guard on the endpoint itself;
storage (S13) persists them in ``endpoint_config_revisions``.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import model_validator

from .foundation import Model, _document

__all__: list[str] = [
    "LoadShape",
    "HaEntityRole",
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
# Device / entity enums.
# --------------------------------------------------------------------------- #


class LoadShape(str, Enum):
    """How a device draws power -- a declared property of the appliance.

    The v3 model class (:class:`~wattmosaic_engine.contracts.ModelClass`) is
    chosen from it (DEC-34).  Rule: a load shape is **declared**, never
    inferred: no code path derives it from a device name, an entity id or any
    other naming surface.  ``UNKNOWN`` is the neutral default.
    """

    FIXED = "fixed"
    """Constant watts while running: a binary load on one level."""

    ADJUSTABLE = "adjustable"
    """Power follows a continuous control value; the response may be a curve."""

    DYNAMIC = "dynamic"
    """Power follows a discrete or time-varying programme, not one setting."""

    DELAYED = "delayed"
    """The control signal and the load are separated by a response delay."""

    ALWAYS_ON = "always_on"
    """There is no OFF state; the minimum power is the standby draw."""

    UNKNOWN = "unknown"
    """Not declared, or no model exists yet; no claim is made either way."""


class HaEntityRole(str, Enum):
    """What one Home Assistant entity means for a physical device.

    A role makes "many entities, one appliance" a contract instead of a
    convention, which is what makes double counting preventable.
    """

    LOAD_STATE = "load_state"
    MODE_ONLY = "mode_only"
    POWER_MEASUREMENT = "power_measurement"
    ENERGY_COUNTER = "energy_counter"
    AVAILABILITY = "availability"
    UNKNOWN = "unknown"

_document(
    HaEntityRole,
    LOAD_STATE="The entity's state proves the appliance is running (never watts).",
    MODE_ONLY="Context only: a mode/thermostat signal that proves nothing about load.",
    POWER_MEASUREMENT="The entity reports watts and may be the one power contributor.",
    ENERGY_COUNTER="The entity reports cumulative energy, never instantaneous watts.",
    AVAILABILITY="The entity only reports reachability.",
    UNKNOWN="The role has not been established.",
)


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
