"""Cross-area runtime handoffs for contract 3.0 (K08).

This module is the *typed seam* between the Wave-3/4 tasks.  Every value that
crosses an area boundary is either a frozen dataclass or a ``Protocol`` here;
the contract models themselves live in :mod:`wattmosaic_engine.contracts` and
are re-used, never redefined.

Shapes are normative from ``interfaces.md`` (§4, §5, §6, §7, §8).  Nothing in
this module imports :mod:`wattmosaic_engine.runtime.fakes`: the fakes are test
doubles, not part of the runtime surface.

Null policy (HB §18): an absent reading or estimate is ``None``, never 0.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable

import numpy as np

from wattmosaic_engine.contracts import (
    ConfigurationEvent,
    DeltaPrior,
    EventGroup,
    JointInferenceResult,
    MappingEdge,
    MappingVersionId,
    MatchState,
    ModelSetId,
    Observability,
    PowerLevelV3,
    PowerPrediction,
    RelationType,
    TopologyEdgeState,
    TopologyEdgeV3,
    TopologyVersionId,
    VersionTriple,
)

__all__: list[str] = [
    "Clock",
    "EntryMeta",
    "RowMeta",
    "AlternativeEdge",
    "ObservationOperator",
    "TopologyView",
    "MappingView",
    "UnknownPrior",
    "ResidualModelView",
    "PredictionContext",
    "ModelSetView",
    "JointSolution",
    "HistoryRow",
    "IdentifiabilityHistory",
    "MatchStore",
    "EstablishedStructure",
    "HeldReading",
    "VersionService",
    "MatrixDelta",
    "MatrixUpdater",
    "InferGroup",
    "InferSteady",
]

# The default lifecycle set for structural graph walks: only STRUCTURAL edges
# are hard structure (PROVISIONAL edges are soft alternatives).
STRUCTURAL: tuple[TopologyEdgeState, ...] = (TopologyEdgeState.STRUCTURAL,)

Clock = Callable[[], datetime]


# --------------------------------------------------------------------------- #
# Observation operator (§4 runtime) — T2 produces, I1/I3/I4/M1 consume.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class EntryMeta:
    """Metadata for one non-zero entry of :attr:`ObservationOperator.H`."""

    coefficient: float
    observability: Observability
    edge_lifecycle: TopologyEdgeState | None  # weakest edge on the path; None for DIRECT
    edge_ids: tuple[str, ...]


@dataclass(frozen=True)
class RowMeta:
    """Metadata for one row of :attr:`ObservationOperator.H`."""

    reader_id: str
    kind: Literal["reader", "residualised_parent"]
    sigma_w: float | None
    sigma_inflation: float  # >= 1, covariance-conservative weighting (I3)
    excluded_reason: str | None = None


@dataclass(frozen=True)
class AlternativeEdge:
    """A PROVISIONAL (soft) edge listed on the operator but never applied."""

    edge_id: str
    source_endpoint_id: str
    target_endpoint_id: str
    relation: RelationType
    lifecycle: TopologyEdgeState
    beta: float | None = None


@dataclass(frozen=True)
class ObservationOperator:
    """The time-scoped measurement operator H(T, M, t).

    ``H`` has shape ``(len(rows), len(subjects))``.  Unknown columns are kept
    separately in ``unknown_scopes`` so they never double-count subject power.
    """

    H: np.ndarray
    rows: tuple[RowMeta, ...]
    subjects: tuple[tuple[str, str], ...]  # (subject_kind, subject_id)
    entries: Mapping[tuple[int, int], EntryMeta]
    unknown_scopes: tuple[tuple[str, tuple[int, ...]], ...]  # (scope reader, row indices)
    soft_alternatives: tuple[AlternativeEdge, ...]
    topology_version_id: str
    mapping_version_id: str
    at: datetime


@runtime_checkable
class TopologyView(Protocol):
    """A read-only topology graph filtered by edge lifecycle."""

    def edges(
        self, lifecycles: Collection[TopologyEdgeState] | None = None
    ) -> tuple[TopologyEdgeV3, ...]: ...

    def parent(
        self, reader_id: str, lifecycles: Collection[TopologyEdgeState] = STRUCTURAL
    ) -> str | None: ...

    def ancestors(
        self, reader_id: str, lifecycles: Collection[TopologyEdgeState] = STRUCTURAL
    ) -> tuple[str, ...]: ...

    def children(
        self, reader_id: str, lifecycles: Collection[TopologyEdgeState] = STRUCTURAL
    ) -> tuple[str, ...]: ...

    def soft_alternatives(self) -> tuple[TopologyEdgeV3, ...]: ...


@runtime_checkable
class MappingView(Protocol):
    """A read-only subject↔reader mapping (most-specific reader only)."""

    def subjects_on(self, reader_id: str) -> tuple[tuple[str, str], ...]: ...

    def most_specific_reader(self, subject: tuple[str, str]) -> str | None: ...


# --------------------------------------------------------------------------- #
# Residual / unknown prior (§4 runtime) — T3 produces, I3 consumes.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class UnknownPrior:
    """The learned unknown-load prior for one reader scope."""

    scope_reader_id: str
    mu_w: float
    sigma_w: float
    lambda_u: float
    step_rate_per_h: float | None = None


@runtime_checkable
class ResidualModelView(Protocol):
    def unknown_prior(self, scope_reader_id: str, at: datetime) -> UnknownPrior | None: ...


# --------------------------------------------------------------------------- #
# Power models (§5 registry) — L1 produces, I1/I2/I3/M1 consume.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PredictionContext:
    """Inputs to a power-model prediction at a point in time."""

    at: datetime
    level: PowerLevelV3 | None = None
    transition: str | None = None
    attributes: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class ModelSetView(Protocol):
    model_set_id: ModelSetId

    def predict(self, subject_id: str, ctx: PredictionContext) -> PowerPrediction: ...

    def transition_prior(
        self, subject_id: str, transition: str, attrs: Mapping[str, Any]
    ) -> DeltaPrior | None: ...

    def expected_window(
        self,
        subject_id: str,
        transition: str,
        attrs: Mapping[str, Any],
        tau_s: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray] | None:  # (mean_w, sigma_w)
        ...


# --------------------------------------------------------------------------- #
# Inference (§6 runtime).
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class JointSolution:
    """I3 → I4/I5: the raw joint solve result."""

    x: np.ndarray
    cov: np.ndarray | None  # N x N posterior covariance (None if singular)
    u: np.ndarray  # unknown columns
    active_rows: tuple[int, ...]
    row_residual_w: np.ndarray
    objective: float
    converged: bool
    mode: Literal["delta", "absolute"]


@dataclass(frozen=True)
class HistoryRow:
    """One independent historical event touching a set of subjects (I4)."""

    group_id: str
    at: datetime
    subject_ids: tuple[str, ...] = ()
    reader_ids: tuple[str, ...] = ()
    delta_w_by_reader: Mapping[str, float | None] = field(default_factory=dict)
    revision: int = 0


@runtime_checkable
class IdentifiabilityHistory(Protocol):
    def events_touching(
        self, subject_ids: Collection[str], since: datetime
    ) -> list[HistoryRow]: ...


@runtime_checkable
class MatchStore(Protocol):
    """An immutable snapshot of match states with functional updates.

    ``apply`` returns a *new* store; snapshots handed to ``infer_group`` are
    never mutated, which is what makes replay deterministic (L7, AT-19).
    """

    def snapshot(self) -> Mapping[str, MatchState]: ...  # candidate_id -> state

    def apply(self, delta: Mapping[str, MatchState]) -> MatchStore: ...


@runtime_checkable
class EstablishedStructure(Protocol):
    """CONFIRMED groups and per-subject identity constraints (DEC-22)."""

    def groups(self) -> Mapping[str, tuple[str, ...]]: ...  # group_id -> member ids

    def identities(self) -> Mapping[tuple[str, str], float]: ...  # subject -> level W


@dataclass(frozen=True)
class HeldReading:
    """A zero-order-held reader value with explicit staleness (P2 → T3/I7)."""

    value: float | None
    held_since: datetime
    last_confirmed_at: datetime
    state: Literal["held", "stale", "missing"]


# --------------------------------------------------------------------------- #
# Version service (§7) and matrix updater (§8).
# --------------------------------------------------------------------------- #


@runtime_checkable
class VersionService(Protocol):
    def current(self) -> VersionTriple: ...

    def as_of(self, at: datetime) -> VersionTriple: ...

    def topology_view(self, version_id: str) -> TopologyView: ...

    def mapping_view(self, version_id: str) -> MappingView: ...

    def record_configuration_event(self, ev: ConfigurationEvent) -> str: ...

    def mark_suspected(
        self,
        change_event_id: str,
        edge_ids: Sequence[str],
        mapping_edge_ids: Sequence[str],
    ) -> None: ...

    def propose_topology(
        self, edges: Sequence[TopologyEdgeV3], change_event_id: str | None
    ) -> TopologyVersionId: ...

    def promote_topology(self, version_id: str, reason: str) -> None: ...

    def propose_mapping(
        self, edges: Sequence[MappingEdge], change_event_id: str | None
    ) -> MappingVersionId: ...

    def promote_mapping(self, version_id: str, reason: str) -> None: ...

    def rollback(self, kind: Literal["topology", "mapping"], to_version_id: str) -> str: ...


@dataclass(frozen=True)
class MatrixDelta:
    """The result of a matrix update or reconciliation (§8)."""

    upserted: tuple[tuple[str, str, str], ...] = ()  # (row_kind, row_id, reader_id)
    retired: tuple[tuple[str, str, str], ...] = ()
    reason: str = ""


@runtime_checkable
class MatrixUpdater(Protocol):
    def apply(self, result: JointInferenceResult, operator: ObservationOperator) -> MatrixDelta: ...

    def reconcile(self, reason: str) -> MatrixDelta: ...


# --------------------------------------------------------------------------- #
# Pure entry points (§6) as callable Protocols.
# --------------------------------------------------------------------------- #


@runtime_checkable
class InferGroup(Protocol):
    def __call__(
        self,
        group: EventGroup,
        operator: ObservationOperator,
        models: ModelSetView,
        residual: ResidualModelView,
        history: IdentifiabilityHistory,
        matches: MatchStore,
        versions: VersionTriple,
        clock: Clock,
    ) -> JointInferenceResult: ...


@runtime_checkable
class InferSteady(Protocol):
    def __call__(
        self,
        readings: Mapping[str, HeldReading],
        operator: ObservationOperator,
        models: ModelSetView,
        residual: ResidualModelView,
        established: EstablishedStructure,
        versions: VersionTriple,
        clock: Clock,
    ) -> JointInferenceResult: ...
