"""Power-model contracts (K06).

A discriminated, fully serialisable power-model spec (``PowerModelSpec``), its
append-only versions and status-event log, a prediction type that can say
"unknown" without ever fabricating a zero, deterministic delta priors, learning
observations and the operating stages of a running device.

:class:`PowerLevelV3` keeps ``OFF`` and ``STANDBY`` distinct (HB §11.5).
``Model``, ``EvidenceRef`` and ``IdentifiabilityStatus`` come from
:mod:`.foundation`.

Null policy (HB §18): a prediction or observation with no value carries
``None``, never ``0``.  An ``UNKNOWN`` prediction has ``mean_w is None``.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from .foundation import EvidenceRef, IdentifiabilityStatus, Model, ModelVersionId

__all__: list[str] = [
    "StageStatus",
    "OperatingStage",
    # Enums / vocabulary
    "ModelBasis",
    "ModelClass",
    "PowerLevelV3",
    "PredictionStatus",
    # Spec components and the discriminated union
    "LevelStat",
    "FixedSpec",
    "AdjustableSpec",
    "DynamicSpec",
    "KernelPhase",
    "ResponseKernelSpec",
    "ComponentSumSpec",
    "PowerModelSpec",
    # Versions and the status-event log
    "PowerModelVersion",
    "PowerModelEvent",
    # Prediction / priors / observations
    "PowerPrediction",
    "DeltaPrior",
    "LearningObservation",
    # Helpers / constants
    "observation_id",
    "SPEC_SCHEMA_VERSION",
]

# The spec schema version is intentionally 1 (this is the first v3 spec).  The
# top-level CONTRACT_VERSION stays "2.4.0" until Z1.
SPEC_SCHEMA_VERSION = 1


# --------------------------------------------------------------------------- #
# Enums / vocabulary.
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Operating stages of a running device.
# --------------------------------------------------------------------------- #


class StageStatus(str, Enum):
    IDENTIFIED = "identified"
    PROBABLE = "probable"
    UNKNOWN = "unknown"


class OperatingStage(Model):
    name: str
    status: StageStatus = StageStatus.UNKNOWN
    power_mean_w: float | None = None
    power_std_w: float | None = None
    min_duration_s: float | None = None
    max_duration_s: float | None = None
    notes: list[str] = Field(default_factory=list)


class ModelBasis(str, Enum):
    """What a model is fitted on, and how its numbers must be interpreted."""

    DELTA = "DELTA"
    """Fitted on device-attributed delta power (HB §3.2, ADR-0026)."""

    ABSOLUTE_ANCHOR = "ABSOLUTE_ANCHOR"
    """Fitted on an absolute level from a single-subject (exclusive) endpoint."""

    LEGACY_ABSOLUTE = "LEGACY_ABSOLUTE"
    """Read-only legacy absolute-row model (DEC-31); never served as validated."""


class ModelClass(str, Enum):
    """The discriminated model class (DEC-34: the declared ``LoadShape``)."""

    FIXED = "FIXED"
    ADJUSTABLE = "ADJUSTABLE"
    DYNAMIC = "DYNAMIC"
    RESPONSE_KERNEL = "RESPONSE_KERNEL"
    COMPONENT_SUM = "COMPONENT_SUM"


class PowerLevelV3(str, Enum):
    """Operating level of a published value (HB §11.5).

    ``OFF`` and ``STANDBY`` are distinct members: OFF is a transition endpoint, not a claim of 0 W (HB §11.5,
    rule 6).  ``UNKNOWN`` is the neutral default.
    """

    OFF = "off"
    STANDBY = "standby"
    IDLE = "idle"
    ACTIVE = "active"
    TRANSIENT = "transient"
    UNKNOWN = "unknown"


class PredictionStatus(str, Enum):
    """Whether a :class:`PowerPrediction` carries a usable value."""

    KNOWN = "KNOWN"
    UNKNOWN = "UNKNOWN"


# --------------------------------------------------------------------------- #
# Level statistics and the discriminated spec union.
# --------------------------------------------------------------------------- #


class LevelStat(Model):
    """A robust statistic of a level: nullable median/MAD plus a sample count."""

    median_w: float | None = None
    mad_w: float | None = None
    n: int = 0


class FixedSpec(Model):
    """Fixed-power model: per-level stats plus named transition deltas."""

    kind: Literal["fixed"] = "fixed"
    levels: dict[PowerLevelV3, LevelStat] = Field(default_factory=dict)
    transition_delta: dict[str, LevelStat] = Field(default_factory=dict)


class AdjustableSpec(Model):
    """Adjustable P=f(x) model on a declared control variable."""

    kind: Literal["adjustable"] = "adjustable"
    control_variable: str
    knots_x: list[float] = Field(default_factory=list)
    knots_w: list[float] = Field(default_factory=list)
    monotone: bool = False
    x_coverage: tuple[float, float] | None = None
    standby: LevelStat = Field(default_factory=LevelStat)

    @model_validator(mode="after")
    def _validate_knots(self) -> AdjustableSpec:
        if len(self.knots_x) != len(self.knots_w):
            raise ValueError("knots_x and knots_w must have the same length")
        if self.knots_x != sorted(self.knots_x):
            raise ValueError("knots_x must be sorted")
        return self


class DynamicSpec(Model):
    """Hidden-state model: state distributions, HA mapping, transitions, durations."""

    kind: Literal["dynamic"] = "dynamic"
    states: dict[str, LevelStat] = Field(default_factory=dict)
    ha_state_to_states: dict[str, list[str]] = Field(default_factory=dict)
    transition_counts: dict[str, dict[str, int]] = Field(default_factory=dict)
    duration_s: dict[str, LevelStat] = Field(default_factory=dict)


class KernelPhase(Model):
    """One phase of a response kernel (delay / amplitude / duration)."""

    phase: Literal["startup", "active", "steady", "shutdown", "standby"]
    delay_s: LevelStat = Field(default_factory=LevelStat)
    amplitude_w: LevelStat = Field(default_factory=LevelStat)
    duration_s: LevelStat = Field(default_factory=LevelStat)


class ResponseKernelSpec(Model):
    """Transient response kernel, keyed by transition type."""

    kind: Literal["response_kernel"] = "response_kernel"
    by_transition: dict[str, list[KernelPhase]] = Field(default_factory=dict)


class ComponentSumSpec(Model):
    """A device model that is the sum of its component models."""

    kind: Literal["component_sum"] = "component_sum"
    component_ids: list[str] = Field(default_factory=list)


PowerModelSpec = Annotated[
    FixedSpec | AdjustableSpec | DynamicSpec | ResponseKernelSpec | ComponentSumSpec,
    Field(discriminator="kind"),
]


# --------------------------------------------------------------------------- #
# Versions and the status-event log.
# --------------------------------------------------------------------------- #


class PowerModelVersion(Model):
    """One append-only version of a subject's power model."""

    version_id: ModelVersionId
    subject_kind: Literal["device", "component"]
    subject_id: str
    number: int
    model_class: ModelClass
    basis: ModelBasis
    spec: PowerModelSpec
    schema_version: int = SPEC_SCHEMA_VERSION
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    topology_version_at_training: str | None = None
    trainer_version: str
    parent_version_id: str | None = None
    created_at: datetime


class PowerModelEvent(Model):
    """A status transition in a version's append-only lifecycle log."""

    version_id: str
    event: Literal[
        "candidate",
        "activated",
        "superseded",
        "rejected",
        "rolled_back",
        "frozen",
        "unfrozen",
    ]
    reason: str
    at: datetime


# --------------------------------------------------------------------------- #
# Prediction, delta prior, learning observations.
# --------------------------------------------------------------------------- #


class PowerPrediction(Model):
    """A per-level prediction that can honestly say "unknown" (never 0 W)."""

    level: PowerLevelV3
    mean_w: float | None = None
    sigma_w: float | None = None
    status: PredictionStatus
    model_version_id: str | None = None

    @model_validator(mode="after")
    def _validate_status(self) -> PowerPrediction:
        if self.status == PredictionStatus.UNKNOWN and self.mean_w is not None:
            raise ValueError("an UNKNOWN prediction must have mean_w is None")
        if self.status == PredictionStatus.KNOWN and self.mean_w is None:
            raise ValueError("a KNOWN prediction must have a non-None mean_w")
        return self


class DeltaPrior(Model):
    """A signed, uncertain delta-power prior handed to inference (interfaces §5)."""

    mean_w: float
    sigma_w: float
    sign: Literal["pos", "neg", "any"]
    model_version_id: str


# Canonical field order for a deterministic observation id.  A unit separator
# keeps composite values unambiguous (ids are opaque strings that may contain
# dashes, but not control characters).
_OBSERVATION_ID_SEPARATOR = "\x1f"


def observation_id(
    group_id: Any,
    group_revision: Any,
    subject_id: Any,
    endpoint_id: Any,
    topology_version_id: Any,
) -> str:
    """Deterministic id for a learning observation.

    ``hash(group_id, group_revision, subject_id, endpoint_id,
    topology_version_id)`` (F4).  Replaying the same event twice produces the
    same id; any input changing produces a different one.
    """
    canonical = _OBSERVATION_ID_SEPARATOR.join(
        str(part)
        for part in (group_id, group_revision, subject_id, endpoint_id, topology_version_id)
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


class LearningObservation(Model):
    """One device-attributed learning observation derived from immutable evidence."""

    observation_id: str
    subject_kind: Literal["device", "component"]
    subject_id: str
    endpoint_id: str
    event_group_ref: EvidenceRef
    transition: str
    attributes: dict[str, Any] = Field(default_factory=dict)
    delta_w: float | None = None
    level_w: float | None = None
    status: Literal["provisional", "confirmed", "rejected"]
    rejection_reason: str | None = None
    identifiability: IdentifiabilityStatus
    basis: ModelBasis = ModelBasis.DELTA
    created_at: datetime

    @model_validator(mode="after")
    def _validate_basis(self) -> LearningObservation:
        # A level (absolute) value is meaningful only on a single-subject
        # endpoint (an ABSOLUTE_ANCHOR basis); otherwise it must be None (HB §18).
        if self.basis != ModelBasis.ABSOLUTE_ANCHOR and self.level_w is not None:
            raise ValueError("level_w may be set only for ABSOLUTE_ANCHOR observations")
        return self


# --------------------------------------------------------------------------- #
# Generated JSON-schema exports (see tools/dev/export_schemas.py).
# --------------------------------------------------------------------------- #

SCHEMA_EXPORTS: list[tuple[str, type]] = [
    ("level_stat", LevelStat),
    ("fixed_spec", FixedSpec),
    ("adjustable_spec", AdjustableSpec),
    ("dynamic_spec", DynamicSpec),
    ("kernel_phase", KernelPhase),
    ("response_kernel_spec", ResponseKernelSpec),
    ("component_sum_spec", ComponentSumSpec),
    ("power_model_version", PowerModelVersion),
    ("power_model_event", PowerModelEvent),
    ("power_prediction", PowerPrediction),
    ("delta_prior", DeltaPrior),
    ("learning_observation", LearningObservation),
]
