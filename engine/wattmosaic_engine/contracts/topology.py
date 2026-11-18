"""Topology, mapping-edge, residual and unknown-profile contracts (owned by K03).

These are the v3 contracts of ``interfaces.md`` §4.  They are additive only:
``CONTRACT_VERSION`` stays ``2.4.0`` and the legacy surface is left untouched.

Naming: the contracts facade (``contracts/__init__.py``) star-imports this module
alongside ``legacy``.  Four names from §4 already exist on that legacy surface,
so the v3 types carry a ``V3`` suffix here:

* legacy ``EdgeLifecycle``            -> :class:`EdgeLifecycleV3`
* legacy ``TopologyEdge``             -> :class:`TopologyEdgeV3`
* legacy ``TopologyVersion``          -> :class:`TopologyVersionV3`
* legacy ``UnknownLoadProfile``       -> :class:`UnknownLoadProfileV3`

Every other §4 name is new.  In particular the config-event enums
(``ConfigurationChangeKind``, ``ChangeDetection``, ``ChangeStatus``) and
``MeasurementBasis`` are *reused* from :mod:`~wattmosaic_engine.contracts.legacy`
rather than redefined, as the task requires.

Null policy (HB §18): an absent measurement or estimate is ``None``, never zero.
All nullable scores and powers here therefore default to ``None``.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import Field, model_validator

from .foundation import EvidenceRef, MappingVersionId, TopologyVersionId
from .legacy import ChangeDetection, ChangeStatus, ConfigurationChangeKind, MeasurementBasis, Model

__all__: list[str] = [
    # Enums
    "RelationType",
    "EdgeLifecycleV3",
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


class RelationType(str, Enum):
    """The five reader↔reader relation kinds of HB §4.2 (DEC-10, DEC-11)."""

    CONTAINS = "contains"
    SHARED_LOAD = "shared_load"
    PAIRED_LEG = "paired_leg"
    CORRELATED = "correlated"
    SAME_CONDUCTOR = "same_conductor"


class EdgeLifecycleV3(str, Enum):
    """Lifecycle of a single v3 topology or mapping edge (HB §4.3).

    This is a *new* seven-state vocabulary.  The legacy :class:`EdgeLifecycle`
    has a different member set (it includes ``UNDER_REVIEW`` and omits nothing,
    but is not identical); it is deliberately left alone.  One event never moves
    an edge past CANDIDATE (DEC-10).
    """

    CANDIDATE = "candidate"
    PROVISIONAL = "provisional"
    CONFIRMED = "confirmed"
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
    lifecycle: EdgeLifecycleV3
    declared_by: Literal["user", "inference", "migration"]
    beta: float | None = None
    beta_basis: Literal["unity", "recorded_factor", "estimated"] | None = None
    evidence: EdgeEvidence
    valid_from: datetime
    valid_until: datetime | None = None
    pinned: bool

    @model_validator(mode="after")
    def _retired_edges_carry_an_end(self) -> TopologyEdgeV3:
        """A RETIRED edge must say *when* it closed (its validity interval)."""
        if self.lifecycle == EdgeLifecycleV3.RETIRED and self.valid_until is None:
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
# Configuration events (reusing the legacy change enums).
# --------------------------------------------------------------------------- #


class ConfigurationEvent(Model):
    """A declared or inferred configuration change (HB §14, §4).

    The ``kind``, ``detection`` and ``status`` enums are the existing v2 ones,
    reused verbatim from ``contracts.legacy``.
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
