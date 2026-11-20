"""Topology, mapping-edge, configuration-event, residual and unknown-profile contracts (K03).

The v3 contracts of ``interfaces.md`` §4: lifecycle-tagged
:class:`TopologyEdgeV3` / :class:`MappingEdge` edges and their versions, the
configuration-change vocabulary (:class:`ConfigurationChangeKind`,
:class:`ChangeDetection`, :class:`ChangeStatus`) and
:class:`ConfigurationEvent`, per-reader residual points and unknown-load
profiles.  ``MeasurementBasis`` is reused from :mod:`.measurement`.

Null policy (HB §18): an absent measurement or estimate is ``None``, never zero.
All nullable scores and powers here therefore default to ``None``.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from .foundation import EvidenceRef, MappingVersionId, Model, TopologyVersionId
from .measurement import MeasurementBasis

__all__: list[str] = [
    "ConfigurationChangeKind",
    "ChangeDetection",
    "ChangeStatus",
    # Enums
    "RelationType",
    "EdgeLifecycleV3",
    "TopologyEdgeState",
    "Observability",
    # Evidence
    "EdgeEvidence",
    # Topology edges and versions
    "TopologyEdgeV3",
    "TopologyVersionV3",
    # Mapping edges and versions
    "MappingEdge",
    "MappingVersion",
    # Configuration events
    "ConfigurationEvent",
    # Residual and unknown load
    "ReaderResidualPoint",
    "UnknownLoadProfileV3",
]

# --------------------------------------------------------------------------- #
# Enums (HB §4.2 relation vocabulary and §9.7 observability states).
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Configuration-change enums (HB §14).
# --------------------------------------------------------------------------- #


class ConfigurationChangeKind(str, Enum):
    """The nine kinds of configuration change (spec 09 §7.6).

    Exactly nine members, and the set is not extensible: a residual baseline
    drift is **not** a configuration change, so the change surface's
    discriminator is a separate, wider vocabulary (09 §7.10).
    """

    SMART_PLUG_RELOCATION = "smart_plug_relocation"
    """A smart plug was moved to a different reader."""

    CT_READER_RELOCATION = "ct_reader_relocation"
    """A CT reader was moved to a different conductor."""

    READER_REPLACEMENT = "reader_replacement"
    """A reader was replaced by another."""

    CT_DIRECTION_CHANGE = "ct_direction_change"
    """A reader's clamp direction was reversed."""

    CALIBRATION_CHANGE = "calibration_change"
    """A reader's calibration was corrected."""

    CIRCUIT_RECONFIGURATION = "circuit_reconfiguration"
    """The circuit a reader belongs to was reconfigured."""

    READER_INSTALLATION = "reader_installation"
    """A new reader was installed."""

    READER_REMOVAL = "reader_removal"
    """A reader was removed; its history is retained, not deleted."""

    MEASUREMENT_SCOPE_CHANGE = "measurement_scope_change"
    """The scope a reader measures changed."""


class ChangeDetection(str, Enum):
    """How a configuration change was learned (spec 09 §7.6)."""

    DECLARED = "declared"
    """The user stated it."""

    OBSERVED = "observed"
    """The engine saw it happen."""

    INFERRED = "inferred"
    """The engine concluded it from indirect evidence."""


class ChangeStatus(str, Enum):
    """Whether a recorded configuration change is established (spec 09 §7.6).

    Contract 2.4.0 adds this as a vocabulary distinct from ``ChangeDetection``:
    ``detection`` says *how* the engine learned of the change, while ``status``
    says *whether* the change is established.  A ``SUSPECTED`` change is an
    uncertain, not-auto-confirmed proposal that leaves the live configuration
    untouched until validated (REQ-CHANGE-002/005); only ``CONFIRMED`` reads
    as established.
    """

    SUSPECTED = "suspected"
    """An uncertain proposal, not auto-confirmed; live configuration untouched."""

    CONFIRMED = "confirmed"
    """The change was validated and is established."""


class RelationType(str, Enum):
    """The five reader↔reader relation kinds of HB §4.2 (DEC-10, DEC-11)."""

    CONTAINS = "contains"
    SHARED_LOAD = "shared_load"
    PAIRED_LEG = "paired_leg"
    CORRELATED = "correlated"
    SAME_CONDUCTOR = "same_conductor"


class EdgeLifecycleV3(str, Enum):
    """Lifecycle of a single v3 topology or mapping edge (HB §4.3).

    A seven-state vocabulary.  One event never moves an edge past CANDIDATE
    (DEC-10).  Topology edges use :class:`TopologyEdgeState` instead; this enum
    is retained for mapping edges, whose confirmation is a different concept.
    """

    CANDIDATE = "candidate"
    PROVISIONAL = "provisional"
    CONFIRMED = "confirmed"
    CONFLICTED = "conflicted"
    SUSPECTED_CHANGE = "suspected_change"
    REJECTED = "rejected"
    RETIRED = "retired"


class TopologyEdgeState(str, Enum):
    """Lifecycle of a single topology edge (HB §4.3, contract 4.0).

    A seven-state vocabulary.  One event never moves an edge past CANDIDATE
    (DEC-10).  ``STRUCTURAL`` replaces the old ``CONFIRMED`` and means only that
    the observation operator H uses the edge right now; it is not a terminal
    truth.  Mapping edges keep :class:`EdgeLifecycleV3` and its ``CONFIRMED``.
    """

    CANDIDATE = "candidate"
    PROVISIONAL = "provisional"
    STRUCTURAL = "structural"
    CONFLICTED = "conflicted"
    SUSPECTED_CHANGE = "suspected_change"
    REJECTED = "rejected"
    RETIRED = "retired"


class Observability(str, Enum):
    """The seven HB §9.7 observability states (in the handbook's order)."""

    DIRECT = "direct"
    EXPECTED = "expected"
    INFERRED = "inferred"
    PARTIAL = "partial"
    UNKNOWN = "unknown"
    NOT_OBSERVABLE = "not_observable"
    CONFLICTED = "conflicted"


# --------------------------------------------------------------------------- #
# Edge evidence ledger.
# --------------------------------------------------------------------------- #


class EdgeEvidence(Model):
    """Immutable-style counters of support/conflict for one edge (design §5).

    ``parent_only`` events are neutral: they are recorded as residual evidence
    and **never counted against an edge** (HB §4.3, AT-2).  The derived
    :attr:`conflict_ratio` therefore uses only the decisive support/conflict
    counts and returns ``0.0`` when there is no decisive evidence yet.
    """

    support: int = 0
    conflict: int = 0
    parent_only: int = 0
    independent_causes: int = 0
    last_support_at: datetime | None = None
    last_conflict_at: datetime | None = None

    @property
    def conflict_ratio(self) -> float:
        """Share of decisive evidence that conflicts (parent-only is neutral)."""
        decisive = self.support + self.conflict
        if decisive <= 0:
            return 0.0
        return self.conflict / decisive


# --------------------------------------------------------------------------- #
# Topology edges and versions.
# --------------------------------------------------------------------------- #


class TopologyEdgeV3(Model):
    """A typed, time-scoped, lifecycle-bearing reader↔reader edge (§4).

    Only readers (measurement endpoints) are endpoints.  For a CONTAINS edge,
    ``source_endpoint_id`` is the parent and ``target_endpoint_id`` the child.
    """

    edge_id: str
    relation: RelationType
    source_endpoint_id: str
    target_endpoint_id: str
    lifecycle: TopologyEdgeState
    declared_by: Literal["user", "inference", "migration"]
    beta: float | None = None
    beta_basis: Literal["unity", "recorded_factor", "estimated"] | None = None
    evidence: EdgeEvidence
    valid_from: datetime
    valid_until: datetime | None = None
    pinned: bool

    @field_validator("lifecycle", mode="before")
    @classmethod
    def _legacy_confirmed(cls, value: Any) -> Any:
        """Old stored JSON / version payloads used ``confirmed``; map to
        ``structural`` so they still load (D1 is behaviour-preserving)."""
        if isinstance(value, str) and value.lower() == "confirmed":
            return TopologyEdgeState.STRUCTURAL.value
        if isinstance(value, Enum) and value.name == "CONFIRMED":
            return TopologyEdgeState.STRUCTURAL.value
        return value

    @model_validator(mode="after")
    def _retired_edges_carry_an_end(self) -> TopologyEdgeV3:
        """A RETIRED edge must say *when* it closed (its validity interval)."""
        if self.lifecycle == TopologyEdgeState.RETIRED and self.valid_until is None:
            raise ValueError("a RETIRED edge must carry a valid_until")
        return self

    @model_validator(mode="after")
    def _endpoints_are_distinct(self) -> TopologyEdgeV3:
        """An edge cannot connect an endpoint to itself."""
        if self.source_endpoint_id == self.target_endpoint_id:
            raise ValueError("an edge cannot connect an endpoint to itself")
        return self

    @model_validator(mode="after")
    def _beta_and_basis_agree(self) -> TopologyEdgeV3:
        """``beta_basis`` is set iff ``beta`` is set."""
        if (self.beta is None) != (self.beta_basis is None):
            raise ValueError("beta_basis is set iff beta is set")
        return self


class TopologyVersionV3(Model):
    """An append-only, rollbackable snapshot of a topology edge set (§4)."""

    version_id: TopologyVersionId
    number: int
    parent_version_id: TopologyVersionId | None = None
    edge_ids: list[str] = Field(default_factory=list)
    status: Literal["candidate", "active", "superseded", "rolled_back"]
    change_event_id: str | None = None
    created_at: datetime
    digest: str


# --------------------------------------------------------------------------- #
# Mapping edges and versions (independent of topology — rule 20).
# --------------------------------------------------------------------------- #


class MappingEdge(Model):
    """A subject↔reader mapping edge (§4), with its own lifecycle and version.

    ``endpoint_id`` is the *most specific* reader; ancestors are derived through
    topology.  An unknown-profile subject may only be mapped through an
    inferred or partial observation.
    """

    mapping_edge_id: str
    subject_kind: Literal["device", "component", "unknown_profile"]
    subject_id: str
    endpoint_id: str
    observability: Observability
    lifecycle: EdgeLifecycleV3
    declared_by: Literal["user", "auto_placement", "inference", "migration"]
    evidence: EdgeEvidence
    valid_from: datetime
    valid_until: datetime | None = None

    @model_validator(mode="after")
    def _unknown_profile_observability(self) -> MappingEdge:
        """An unknown-profile subject needs INFERRED or PARTIAL observability."""
        if self.subject_kind == "unknown_profile" and self.observability not in (
            Observability.INFERRED,
            Observability.PARTIAL,
        ):
            raise ValueError(
                "subject_kind='unknown_profile' requires observability "
                "INFERRED or PARTIAL"
            )
        return self


class MappingVersion(Model):
    """An append-only snapshot of mapping edges (independent of topology)."""

    version_id: MappingVersionId
    number: int
    parent_version_id: MappingVersionId | None = None
    mapping_edge_ids: list[str] = Field(default_factory=list)
    status: Literal["candidate", "active", "superseded", "rolled_back"]
    change_event_id: str | None = None
    created_at: datetime
    digest: str


# --------------------------------------------------------------------------- #
# Configuration events.
# --------------------------------------------------------------------------- #


class ConfigurationEvent(Model):
    """A declared or inferred configuration change (HB §14, §4).

    ``kind``, ``detection`` and ``status`` use the configuration-change enums
    defined above.
    """

    event_id: str
    kind: ConfigurationChangeKind
    detection: ChangeDetection
    status: ChangeStatus
    affected_endpoint_ids: list[str] = Field(default_factory=list)
    affected_edge_ids: list[str] = Field(default_factory=list)
    affected_mapping_edge_ids: list[str] = Field(default_factory=list)
    candidate_topology_version_id: str | None = None
    candidate_mapping_version_id: str | None = None
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    created_at: datetime
    resolved_at: datetime | None = None


# --------------------------------------------------------------------------- #
# Residual and unknown load (HB §9.4 D–E).
# --------------------------------------------------------------------------- #


class ReaderResidualPoint(Model):
    """One signed residual sample for one reader (HB §9.4 E, DEF-05).

    The point is keyed by reader and carries the topology version it was
    computed under, so per-reader baselines no longer share a single
    install-wide window.  ``residual_w`` is signed and never clamped; ``None``
    means unknown (HB §18), never 0 W.
    """

    reader_id: str
    at: datetime
    residual_w: float | None = None
    baseline_w: float | None = None
    dynamic_w: float | None = None
    noise_sigma_w: float | None = None
    topology_version_id: TopologyVersionId
    basis: MeasurementBasis


class UnknownLoadProfileV3(Model):
    """A recurring, un-attributed load signature (HB §9.4 D).

    Its lifecycle has no ``confirmed`` state: an unknown load is observed,
    characterised, offered as a candidate, promoted by a user, or retired —
    never auto-confirmed by the engine.  A retired profile is retained.
    """

    profile_id: str
    scope_endpoint_id: str
    lifecycle: Literal["observing", "characterised", "candidate", "promoted", "retired"]
    signature: dict[str, float] = Field(default_factory=dict)
    event_count: int = 0
    independent_event_count: int = 0
    first_seen: datetime
    last_seen: datetime

    @model_validator(mode="before")
    @classmethod
    def _no_confirmed_state(cls, data: Any) -> Any:
        """Reject any attempt (even a planted one) to confirm a profile."""
        if isinstance(data, dict):
            lifecycle = data.get("lifecycle")
            if isinstance(lifecycle, str) and "confirm" in lifecycle.lower():
                raise ValueError("an unknown-load profile can never be CONFIRMED")
        return data


# --------------------------------------------------------------------------- #
# Generated JSON-schema exports (see tools/dev/export_schemas.py).
# --------------------------------------------------------------------------- #

SCHEMA_EXPORTS: list[tuple[str, type]] = [
    ("edge_evidence_v3", EdgeEvidence),
    ("topology_edge_v3", TopologyEdgeV3),
    ("topology_version_v3", TopologyVersionV3),
    ("mapping_edge", MappingEdge),
    ("mapping_version", MappingVersion),
    ("configuration_event", ConfigurationEvent),
    ("reader_residual_point", ReaderResidualPoint),
    ("unknown_load_profile_v3", UnknownLoadProfileV3),
]
