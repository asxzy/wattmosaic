"""Deterministic test doubles for the runtime handoffs (K08).

These are **test doubles, not reference implementations**.  They are
intentionally naive and must never be imported by production code — a static
test (``tests/unit/test_runtime_fakes.py``) enforces that no module under
``engine/wattmosaic_engine/`` imports them.  They lived in ``runtime/fakes.py``
until Z3b moved them (and the HB §17 tree) out of the engine.

All fakes are immutable where practical and produce byte-identical results for
identical inputs, so Wave-3/4 tasks can build and test against them in parallel.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any

import numpy as np
from wattmosaic_engine.contracts import (
    ConfigurationEvent,
    DeltaPrior,
    EdgeEvidence,
    EdgeLifecycleV3,
    JointInferenceResult,
    MappingEdge,
    MappingVersionId,
    MatchState,
    ModelSetId,
    Observability,
    PowerLevelV3,
    PowerPrediction,
    PredictionStatus,
    RelationType,
    TopologyEdgeState,
    TopologyEdgeV3,
    TopologyVersionId,
    VersionTriple,
)
from wattmosaic_engine.runtime.interfaces import (
    STRUCTURAL,
    AlternativeEdge,
    Clock,
    EntryMeta,
    HistoryRow,
    MappingView,
    MatrixDelta,
    ObservationOperator,
    PredictionContext,
    RowMeta,
    TopologyView,
    UnknownPrior,
)

__all__: list[str] = [
    "HB17_TREE",
    "FakeTopology",
    "FakeMapping",
    "fake_operator",
    "FakeModelSet",
    "FakeResidual",
    "InMemoryHistory",
    "InMemoryMatchStore",
    "FakeVersionService",
    "RecordingMatrixUpdater",
]

# A fixed origin so fakes are deterministic without needing a clock.
_ORIGIN = datetime(2026, 1, 1, tzinfo=UTC)

# HB §17 reference house: Main ⊃ Subpanel ⊃ {Circuit A, Circuit B}; Main ⊃
# Circuit C.  Values are nested child maps; a leaf is an empty map.
HB17_TREE: dict[str, Any] = {
    "Main": {
        "Subpanel": {
            "Circuit A": {},
            "Circuit B": {},
        },
        "Circuit C": {},
    }
}

# Reader ids of the HB §17 house in a stable order (parents before children).
HB17_READERS: tuple[str, ...] = (
    "Main",
    "Subpanel",
    "Circuit A",
    "Circuit B",
    "Circuit C",
)


def _evidence() -> EdgeEvidence:
    return EdgeEvidence()


def _contains_edge(parent: str, child: str, edge_id: str, *, at: datetime) -> TopologyEdgeV3:
    """Build one STRUCTURAL CONTAINS edge (source = parent, target = child)."""
    return TopologyEdgeV3(
        edge_id=edge_id,
        relation=RelationType.CONTAINS,
        source_endpoint_id=parent,
        target_endpoint_id=child,
        lifecycle=TopologyEdgeState.STRUCTURAL,
        declared_by="user",
        beta=1.0,
        beta_basis="unity",
        evidence=_evidence(),
        valid_from=at,
        pinned=True,
    )


@dataclass(frozen=True)
class FakeTopology:
    """A read-only topology view built from a tree spec or an edge list."""

    edges_all: tuple[TopologyEdgeV3, ...] = ()

    @classmethod
    def from_tree(
        cls,
        spec: Mapping[str, Any],
        *,
        at: datetime = _ORIGIN,
    ) -> FakeTopology:
        """Build the HB §17 house (or any nested tree) in one call.

        ``spec`` maps a reader id to a nested map of its children; a leaf maps
        to an empty map.  Every parent→child pair becomes a STRUCTURAL CONTAINS
        edge with coefficient 1.
        """
        edges: list[TopologyEdgeV3] = []

        def walk(parent: str, children: Mapping[str, Any]) -> None:
            for child, subtree in children.items():
                edges.append(_contains_edge(parent, child, f"e::{parent}->{child}", at=at))
                walk(child, subtree if isinstance(subtree, Mapping) else {})

        for root, subtree in spec.items():
            walk(root, subtree if isinstance(subtree, Mapping) else {})
        return cls(tuple(edges))

    @classmethod
    def from_edges(cls, edges: Sequence[TopologyEdgeV3]) -> FakeTopology:
        return cls(tuple(edges))

    # -- TopologyView -------------------------------------------------------- #

    def edges(
        self, lifecycles: Sequence[TopologyEdgeState] | None = None
    ) -> tuple[TopologyEdgeV3, ...]:
        if lifecycles is None:
            return self.edges_all
        wanted = frozenset(lifecycles)
        return tuple(e for e in self.edges_all if e.lifecycle in wanted)

    def _contains(self, lifecycles: Sequence[TopologyEdgeState]) -> tuple[TopologyEdgeV3, ...]:
        wanted = frozenset(lifecycles)
        return tuple(
            e
            for e in self.edges_all
            if e.relation == RelationType.CONTAINS and e.lifecycle in wanted
        )

    def parent(
        self,
        reader_id: str,
        lifecycles: Sequence[TopologyEdgeState] = STRUCTURAL,
    ) -> str | None:
        for edge in self._contains(lifecycles):
            if edge.target_endpoint_id == reader_id:
                return edge.source_endpoint_id
        return None

    def ancestors(
        self,
        reader_id: str,
        lifecycles: Sequence[TopologyEdgeState] = STRUCTURAL,
    ) -> tuple[str, ...]:
        """Nearest ancestor first, root last."""
        result: list[str] = []
        seen = {reader_id}
        current = self.parent(reader_id, lifecycles)
        while current is not None and current not in seen:
            result.append(current)
            seen.add(current)
            current = self.parent(current, lifecycles)
        return tuple(result)

    def children(
        self,
        reader_id: str,
        lifecycles: Sequence[TopologyEdgeState] = STRUCTURAL,
    ) -> tuple[str, ...]:
        return tuple(
            e.target_endpoint_id
            for e in self._contains(lifecycles)
            if e.source_endpoint_id == reader_id
        )

    def path_from(self, ancestor: str, reader: str) -> tuple[str, ...]:
        """Edge ids on the STRUCTURAL chain from ``ancestor`` down to ``reader``."""
        edges: list[str] = []
        seen: set[str] = set()
        current = reader
        while current != ancestor and current not in seen:
            seen.add(current)
            hits = [
                e
                for e in self._contains(STRUCTURAL)
                if e.target_endpoint_id == current
            ]
            if not hits:
                return ()
            edge = hits[0]
            edges.append(edge.edge_id)
            current = edge.source_endpoint_id
        return tuple(reversed(edges)) if not seen.__contains__(ancestor) or current == ancestor else ()

    def soft_alternatives(self) -> tuple[TopologyEdgeV3, ...]:
        return self.edges((TopologyEdgeState.PROVISIONAL,))


@dataclass(frozen=True)
class FakeMapping:
    """A read-only subject→reader mapping built from explicit pairs."""

    pairs: Mapping[tuple[str, str], str] = field(default_factory=lambda: MappingProxyType({}))

    @classmethod
    def from_pairs(cls, pairs: Mapping[tuple[str, str], str]) -> FakeMapping:
        return cls(MappingProxyType(dict(pairs)))

    def subjects_on(self, reader_id: str) -> tuple[tuple[str, str], ...]:
        return tuple(sorted(s for s, r in self.pairs.items() if r == reader_id))

    def most_specific_reader(self, subject: tuple[str, str]) -> str | None:
        return self.pairs.get(subject)

    def edges(self, *, at: datetime = _ORIGIN) -> tuple[MappingEdge, ...]:
        """Return MappingEdge contracts for the pairs (useful to other fakes)."""
        result: list[MappingEdge] = []
        for (kind, subject_id), endpoint_id in sorted(self.pairs.items()):
            result.append(
                MappingEdge(
                    mapping_edge_id=f"m::{kind}::{subject_id}->{endpoint_id}",
                    subject_kind=kind,  # type: ignore[arg-type]
                    subject_id=subject_id,
                    endpoint_id=endpoint_id,
                    observability=Observability.DIRECT,
                    lifecycle=EdgeLifecycleV3.CONFIRMED,
                    declared_by="user",
                    evidence=_evidence(),
                    valid_from=at,
                )
            )
        return tuple(result)


def fake_operator(
    topology: TopologyView,
    mapping: MappingView,
    reader_ids: Sequence[str],
    subjects: Sequence[tuple[str, str]],
    *,
    at: datetime = _ORIGIN,
    topology_version_id: str = "topo-1",
    mapping_version_id: str = "map-1",
) -> ObservationOperator:
    """A naive-but-correct H for STRUCTURAL CONTAINS chains (coefficient 1).

    A subject mapped to reader ``R`` enters the row of ``R`` (DIRECT) and the
    row of every STRUCTURAL CONTAINS ancestor of ``R`` (EXPECTED), always with
    coefficient 1.  Nothing else is inferred: no residualisation, no unknown
    columns, no leg coefficients.
    """
    rows = tuple(
        RowMeta(
            reader_id=reader,
            kind="reader",
            sigma_w=None,
            sigma_inflation=1.0,
            excluded_reason=None,
        )
        for reader in reader_ids
    )
    row_index = {reader: i for i, reader in enumerate(reader_ids)}
    subj_index = {subject: j for j, subject in enumerate(subjects)}
    H = np.zeros((len(rows), len(subjects)), dtype=float)
    entries: dict[tuple[int, int], EntryMeta] = {}

    for subject, j in subj_index.items():
        reader = mapping.most_specific_reader(subject)
        if reader is None or reader not in row_index:
            continue
        i = row_index[reader]
        H[i, j] = 1.0
        entries[(i, j)] = EntryMeta(
            coefficient=1.0,
            observability=Observability.DIRECT,
            edge_lifecycle=None,
            edge_ids=(),
        )
        ancestors = list(topology.ancestors(reader))
        for ancestor in ancestors:
            if ancestor not in row_index:
                continue
            r = row_index[ancestor]
            H[r, j] = 1.0
            edge_ids = (
                topology.path_from(ancestor, reader)
                if isinstance(topology, FakeTopology)
                else ()
            )
            entries[(r, j)] = EntryMeta(
                coefficient=1.0,
                observability=Observability.EXPECTED,
                edge_lifecycle=TopologyEdgeState.STRUCTURAL,
                edge_ids=edge_ids,
            )

    soft: list[AlternativeEdge] = []
    for edge in topology.soft_alternatives():
        soft.append(
            AlternativeEdge(
                edge_id=edge.edge_id,
                source_endpoint_id=edge.source_endpoint_id,
                target_endpoint_id=edge.target_endpoint_id,
                relation=edge.relation,
                lifecycle=edge.lifecycle,
                beta=edge.beta,
            )
        )

    return ObservationOperator(
        H=H,
        rows=rows,
        subjects=tuple(subjects),
        entries=MappingProxyType(entries),
        unknown_scopes=(),
        soft_alternatives=tuple(soft),
        topology_version_id=topology_version_id,
        mapping_version_id=mapping_version_id,
        at=at,
    )


@dataclass(frozen=True)
class FakeModelSet:
    """A ModelSetView with fixed ΔP priors and optional naive kernels."""

    priors: Mapping[str, DeltaPrior] = field(default_factory=lambda: MappingProxyType({}))
    kernels: Mapping[str, tuple[np.ndarray, np.ndarray]] = field(default_factory=lambda: MappingProxyType({}))
    model_set_id: ModelSetId = "mset-fake"

    @classmethod
    def fixed(
        cls,
        priors: Mapping[str, DeltaPrior],
        *,
        model_set_id: ModelSetId = "mset-fake",
    ) -> FakeModelSet:
        return cls(MappingProxyType(dict(priors)), MappingProxyType({}), model_set_id)

    def predict(self, subject_id: str, ctx: PredictionContext) -> PowerPrediction:
        prior = self.priors.get(subject_id)
        level = ctx.level if ctx.level is not None else PowerLevelV3.UNKNOWN
        if prior is None:
            return PowerPrediction(
                level=level,
                mean_w=None,
                sigma_w=None,
                status=PredictionStatus.UNKNOWN,
                model_version_id=None,
            )
        return PowerPrediction(
            level=level,
            mean_w=prior.mean_w,
            sigma_w=prior.sigma_w,
            status=PredictionStatus.KNOWN,
            model_version_id=prior.model_version_id,
        )

    def transition_prior(
        self, subject_id: str, transition: str, attrs: Mapping[str, Any]
    ) -> DeltaPrior | None:
        return self.priors.get(subject_id)

    def expected_window(
        self,
        subject_id: str,
        transition: str,
        attrs: Mapping[str, Any],
        tau_s: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray] | None:
        kernel = self.kernels.get(subject_id)
        if kernel is None:
            return None
        mean, sigma = kernel
        return np.asarray(mean, dtype=float), np.asarray(sigma, dtype=float)


@dataclass(frozen=True)
class FakeResidual:
    """A ResidualModelView returning one constant UnknownPrior for every scope."""

    prior: UnknownPrior

    def unknown_prior(self, scope_reader_id: str, at: datetime) -> UnknownPrior | None:
        return self.prior


@dataclass
class InMemoryHistory:
    """An append-only IdentifiabilityHistory, deterministic and order-stable."""

    rows: list[HistoryRow]

    def __init__(self, rows: Sequence[HistoryRow] | None = None) -> None:
        self.rows = list(rows or ())

    def add(self, row: HistoryRow) -> None:
        self.rows.append(row)

    def events_touching(
        self, subject_ids: Sequence[str], since: datetime
    ) -> list[HistoryRow]:
        wanted = frozenset(subject_ids)
        return [
            row
            for row in self.rows
            if row.at >= since and wanted.intersection(row.subject_ids)
        ]


@dataclass(frozen=True)
class InMemoryMatchStore:
    """An immutable MatchStore; every apply returns a new store."""

    states: Mapping[str, MatchState] = field(default_factory=lambda: MappingProxyType({}))

    def snapshot(self) -> Mapping[str, MatchState]:
        return MappingProxyType(dict(self.states))

    def apply(self, delta: Mapping[str, MatchState]) -> InMemoryMatchStore:
        merged = dict(self.states)
        merged.update(delta)
        return InMemoryMatchStore(MappingProxyType(merged))


class FakeVersionService:
    """A minimal append-only VersionService for tests.

    Proposals create candidate versions; promotion marks a version active.
    The views returned are the same FakeTopology/FakeMapping objects supplied
    at proposal time (or built from edges for topology proposals).
    """

    triple: VersionTriple
    clock: Clock
    topology_views: dict[str, TopologyView]
    mapping_views: dict[str, MappingView]
    events: list[ConfigurationEvent]
    suspected: list[tuple[str, tuple[str, ...], tuple[str, ...]]]
    changes: list[tuple[datetime, VersionTriple]]
    _active_topo: str
    _active_map: str

    def __init__(
        self,
        triple: VersionTriple,
        *,
        topology_view: TopologyView | None = None,
        mapping_view: MappingView | None = None,
        clock: Clock | None = None,
        at: datetime | None = None,
    ) -> None:
        self.triple = triple
        self.clock = clock or (lambda: _ORIGIN)
        self.topology_views = {}
        self.mapping_views = {}
        self.events = []
        self.suspected = []
        self.changes = []
        self._active_topo = triple.topology_version_id
        self._active_map = triple.mapping_version_id
        if topology_view is not None:
            self.topology_views[self._active_topo] = topology_view
        if mapping_view is not None:
            self.mapping_views[self._active_map] = mapping_view
        self._append(self.triple, at if at is not None else self.clock())

    def _append(self, triple: VersionTriple, at: datetime) -> None:
        self.changes.append((at, triple))

    def current(self) -> VersionTriple:
        return self.triple

    def as_of(self, at: datetime) -> VersionTriple:
        """The most recent triple recorded at or before ``at`` (append-only)."""
        result = self.changes[0][1]
        for recorded_at, triple in self.changes:
            if recorded_at > at:
                break
            result = triple
        return result

    def topology_view(self, version_id: str) -> TopologyView:
        return self.topology_views[version_id]

    def mapping_view(self, version_id: str) -> MappingView:
        return self.mapping_views[version_id]

    def record_configuration_event(self, ev: ConfigurationEvent) -> str:
        self.events.append(ev)
        return ev.event_id

    def mark_suspected(
        self,
        change_event_id: str,
        edge_ids: Sequence[str],
        mapping_edge_ids: Sequence[str],
    ) -> None:
        self.suspected.append((change_event_id, tuple(edge_ids), tuple(mapping_edge_ids)))

    def propose_topology(
        self, edges: Sequence[TopologyEdgeV3], change_event_id: str | None
    ) -> TopologyVersionId:
        number = len(self.topology_views) + 1
        version_id = f"topo-{number}"
        self.topology_views[version_id] = FakeTopology.from_edges(edges)
        return version_id

    def promote_topology(self, version_id: str, reason: str) -> None:
        self._active_topo = version_id
        self.triple = replace(self.triple, topology_version_id=version_id)
        self._append(self.triple, self.clock())

    def propose_mapping(
        self, edges: Sequence[MappingEdge], change_event_id: str | None
    ) -> MappingVersionId:
        number = len(self.mapping_views) + 1
        version_id = f"map-{number}"
        pairs = {(e.subject_kind, e.subject_id): e.endpoint_id for e in edges}
        self.mapping_views[version_id] = FakeMapping.from_pairs(pairs)
        return version_id

    def promote_mapping(self, version_id: str, reason: str) -> None:
        self._active_map = version_id
        self.triple = replace(self.triple, mapping_version_id=version_id)
        self._append(self.triple, self.clock())

    def rollback(self, kind: str, to_version_id: str) -> str:
        number = len(self.topology_views if kind == "topology" else self.mapping_views) + 1
        new_id = f"{kind}-{number}-rollback"
        if kind == "topology":
            self.topology_views[new_id] = self.topology_views[to_version_id]
            return new_id
        self.mapping_views[new_id] = self.mapping_views[to_version_id]
        return new_id


class RecordingMatrixUpdater:
    """A MatrixUpdater that records calls and returns canned deltas."""

    apply_delta: MatrixDelta
    reconcile_delta: MatrixDelta
    applied: list[tuple[JointInferenceResult, ObservationOperator]]
    reconciled: list[str]

    def __init__(
        self,
        *,
        apply_delta: MatrixDelta | None = None,
        reconcile_delta: MatrixDelta | None = None,
    ) -> None:
        self.apply_delta = apply_delta if apply_delta is not None else MatrixDelta()
        self.reconcile_delta = (
            reconcile_delta if reconcile_delta is not None else MatrixDelta()
        )
        self.applied = []
        self.reconciled = []

    def apply(
        self, result: JointInferenceResult, operator: ObservationOperator
    ) -> MatrixDelta:
        self.applied.append((result, operator))
        return self.apply_delta

    def reconcile(self, reason: str) -> MatrixDelta:
        self.reconciled.append(reason)
        return self.reconcile_delta
