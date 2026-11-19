"""Observation operator H(T, M, t) with physics-justified β (T2, design §5).

One pure function turns a topology view, a mapping view, a factor registry and
an instant into the time-scoped measurement operator consumed by I1/I3/I4/M1.
The module reads no clock and no database: ``at`` is supplied by the caller,
which is what makes a factor change reconstructable instead of retroactive
(DEF-06).  It never imports :mod:`wattmosaic_engine.runtime.fakes`.

The operator follows interfaces.md §4:

* a subject's most specific reader comes from :class:`MappingView`;
* CONFIRMED CONTAINS ancestors see the subject as ``EXPECTED`` with the product
  of the path coefficients β (unity for an uncalibrated edge);
* a PAIRED_LEG endpoint carries the verified leg coefficient (0.5);
* a CONFIRMED SHARED_LOAD puts the subject in both readers' rows;
* unknown columns are scope-located and enter a reader's row plus every
  CONFIRMED ancestor row; a CONTAINS child that is not among ``reader_ids`` is
  a *missing child* and gets its own unknown column so the parent row is never
  dropped (DEC-12);
* ``RowMeta.sigma_inflation`` is ``√k`` for correlated (same CONTAINS chain or
  SHARED_LOAD pair) rows that observe a common subject (§8.3, AT-9).

Caching
-------
Building the operator is a per-topology-version cost, not a per-sample cost
(analysis/B trap 8).  :class:`OperatorCache` keys on
``(topology_version_id, mapping_version_id, factor_revision_set)`` and rebuilds
only when one of those changes.  ``reader_ids``/``subjects`` are part of the key
as a safety net: a stable version used with a different column set is a
different operator.  A cache hit with a different ``at`` but the same factor
revision set still returns the same H — only the ``at`` stamp is refreshed.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from types import MappingProxyType
from typing import Any

import numpy as np

from wattmosaic_engine.contracts import (
    EdgeLifecycleV3,
    Observability,
    RelationType,
    TopologyEdgeV3,
)
from wattmosaic_engine.electrical.factors import FactorRegistry
from wattmosaic_engine.runtime.interfaces import (
    AlternativeEdge,
    EntryMeta,
    MappingView,
    ObservationOperator,
    RowMeta,
    TopologyView,
)

__all__: list[str] = [
    "OperatorCache",
    "build_observation_operator",
    "operator_cache",
    "operator_cache_stats",
    "clear_operator_cache",
]

# Contract models use ``use_enum_values=True``: relation/lifecycle fields read
# back as plain strings.  These helpers accept either form.
def _ev(value: Any) -> str:
    return str(getattr(value, "value", value))


def _relation_is(edge: TopologyEdgeV3, relation: RelationType) -> bool:
    return _ev(edge.relation) == _ev(relation)


def _enum_value(value: Any) -> str:
    return _ev(value)


# --------------------------------------------------------------------------- #
# Best-effort reads of optional view capabilities.
# --------------------------------------------------------------------------- #


def _edges_of_lifecycle(
    topology: TopologyView, lifecycle: EdgeLifecycleV3
) -> tuple[TopologyEdgeV3, ...]:
    """Edges of one lifecycle, independent of whether the view filters by it."""
    try:
        return tuple(topology.edges((lifecycle,)))  # type: ignore[arg-type]
    except TypeError:  # a minimal view may accept no argument
        return tuple(e for e in topology.edges() if _ev(e.lifecycle) == _ev(lifecycle))


def _edges_of_relation(topology: TopologyView, relation: RelationType) -> tuple[TopologyEdgeV3, ...]:
    return tuple(e for e in _edges_of_lifecycle(topology, EdgeLifecycleV3.CONFIRMED)
                 if _relation_is(e, relation))


def _parent_edge_map(topology: TopologyView) -> dict[str, TopologyEdgeV3]:
    """child -> the CONFIRMED CONTAINS edge that makes it a child."""
    result: dict[str, TopologyEdgeV3] = {}
    for edge in sorted(_edges_of_relation(topology, RelationType.CONTAINS), key=lambda e: e.edge_id):
        result.setdefault(edge.target_endpoint_id, edge)
    return result


def _leg_coefficient(topology: TopologyView, reader: str) -> float | None:
    """0.5 for a verified complete PAIRED_LEG pair, else ``None`` (absent)."""
    fn = getattr(topology, "leg_coefficient", None)
    if callable(fn):
        try:
            value = fn(reader)
        except Exception:  # pragma: no cover - defensive for odd test doubles
            value = None
        if value is not None:
            return float(value)
    # Fallback: a complete pair needs both directions declared (T1's rule).
    partners = {
        e.source_endpoint_id: e.target_endpoint_id
        for e in _edges_of_relation(topology, RelationType.PAIRED_LEG)
    }
    partner = partners.get(reader)
    if partner is None or partner == reader or partners.get(partner) != reader:
        return None
    return 0.5


def _path_to_root(reader: str, parent_edge: dict[str, TopologyEdgeV3]) -> list[TopologyEdgeV3]:
    """Edges from ``reader`` up to the root, nearest first (cycle-safe)."""
    result: list[TopologyEdgeV3] = []
    seen = {reader}
    current = reader
    while current in parent_edge:
        edge = parent_edge[current]
        if edge.source_endpoint_id in seen:
            break
        result.append(edge)
        seen.add(edge.source_endpoint_id)
        current = edge.source_endpoint_id
    return result


def _ancestor_ids(reader: str, parent_edge: dict[str, TopologyEdgeV3]) -> set[str]:
    """All CONFIRMED CONTAINS ancestors of ``reader``."""
    return {e.source_endpoint_id for e in _path_to_root(reader, parent_edge)}


def _mapping_edge_lifecycle(
    mapping: MappingView, subject: tuple[str, str]
) -> EdgeLifecycleV3 | None:
    """The mapping edge's lifecycle when the view exposes it."""
    fn = getattr(mapping, "edge_lifecycle", None)
    if callable(fn):
        try:
            value = fn(subject)
        except Exception:  # pragma: no cover - defensive for odd test doubles
            value = None
        if value is not None:
            try:
                return EdgeLifecycleV3(value)
            except ValueError:
                return None
    edges_fn = getattr(mapping, "edges", None)
    if callable(edges_fn):
        try:
            edges = tuple(edges_fn())
        except TypeError:  # pragma: no cover - defensive for odd test doubles
            edges = ()
        for edge in edges:
            if (edge.subject_kind, edge.subject_id) == subject:
                return edge.lifecycle
    return None


# --------------------------------------------------------------------------- #
# β resolution and the factor revision set.
# --------------------------------------------------------------------------- #


def _edge_beta(edge: TopologyEdgeV3, factors: FactorRegistry | None, at: datetime) -> float:
    """Coefficient justified by the edge metadata and the factor covering ``t``.

    ``unity`` (or an unlabelled edge) is 1.  An ``estimated`` β is used as
    recorded on the edge — it is a partial observation and is *not* used to
    close or absorb the parent residual.  A ``recorded_factor`` β is resolved
    from the interval covering ``at`` on the child reader, via the registry's
    ``applied_multiplier_at``: a calibration change applies only to samples
    after it, and a factor the monitor already applied is not applied twice
    (DEF-06).  When the semantics are unknown the registry returns 1.0 — the
    same "never guess" policy — which is what we use.
    """
    if edge.beta_basis == "estimated":
        return 1.0 if edge.beta is None else float(edge.beta)
    if edge.beta_basis == "recorded_factor" and factors is not None:
        return float(factors.applied_multiplier_at(edge.target_endpoint_id, at))
    return 1.0


def _factor_revision_set(
    factors: FactorRegistry | None, reader_ids: Sequence[str], at: datetime
) -> tuple[tuple[str, str, float, str, str, str], ...]:
    """Identity of every factor in force at ``at`` for the given readers."""
    if factors is None:
        return ()
    revision: list[tuple[str, str, float, str, str, str]] = []
    for reader in reader_ids:
        factor = factors.factor_at(reader, at)
        if factor is None:
            continue
        revision.append(
            (
                str(reader),
                factor.effective_from.isoformat(),
                float(factor.multiplier),
                str(factor.applied_by),
                str(factor.declared_by),
                "" if factor.note is None else str(factor.note),
            )
        )
    return tuple(sorted(revision))


# --------------------------------------------------------------------------- #
# Version id / content fingerprints for the cache key.
# --------------------------------------------------------------------------- #


def _digest(parts: Sequence[Any]) -> str:
    canonical = repr(tuple(parts)).encode("utf-8")
    return "h-" + hashlib.sha256(canonical).hexdigest()[:16]


def _topology_version(topology: TopologyView) -> str:
    for attr in ("topology_version_id", "version_id", "version"):
        value = getattr(topology, attr, None)
        if isinstance(value, str) and value:
            return value
    parts = [
        (
            e.edge_id,
            _ev(e.relation),
            e.source_endpoint_id,
            e.target_endpoint_id,
            _ev(e.lifecycle),
            e.beta,
            e.beta_basis,
            e.valid_from.isoformat(),
            None if e.valid_until is None else e.valid_until.isoformat(),
        )
        for e in topology.edges()
    ]
    return _digest(sorted(parts, key=lambda p: p[0]))


def _mapping_version(mapping: MappingView) -> str:
    for attr in ("mapping_version_id", "version_id", "version"):
        value = getattr(mapping, attr, None)
        if isinstance(value, str) and value:
            return value
    edges_fn = getattr(mapping, "edges", None)
    parts: list[Any] = []
    if callable(edges_fn):
        try:
            rows = tuple(edges_fn())
        except TypeError:  # pragma: no cover - defensive for odd test doubles
            rows = ()
        parts = [
            (
                e.mapping_edge_id,
                e.subject_kind,
                e.subject_id,
                e.endpoint_id,
                _ev(e.lifecycle),
                e.valid_from.isoformat(),
            )
            for e in rows
        ]
    else:
        pairs = getattr(mapping, "pairs", None)
        if pairs is not None:
            parts = [(k[0], k[1], v) for k, v in sorted(dict(pairs).items())]
    return _digest(sorted(parts, key=repr))


# --------------------------------------------------------------------------- #
# The pure builder.
# --------------------------------------------------------------------------- #


def _build(
    topology: TopologyView,
    mapping: MappingView,
    factors: FactorRegistry | None,
    reader_ids: Sequence[str],
    subjects: Sequence[tuple[str, str]],
    at: datetime,
    *,
    topology_version_id: str,
    mapping_version_id: str,
) -> ObservationOperator:
    """Build H from scratch.  No clock, no cache, no I/O."""
    readers = tuple(reader_ids)
    reader_index = {reader: i for i, reader in enumerate(readers)}
    unique_subjects = tuple(dict.fromkeys(subjects))
    subject_index = {subject: j for j, subject in enumerate(unique_subjects)}

    parent_edge = _parent_edge_map(topology)
    shared_edges = _edges_of_relation(topology, RelationType.SHARED_LOAD)

    # Shared-load partners, keyed by either endpoint.
    shared_partners: dict[str, list[TopologyEdgeV3]] = {}
    for edge in sorted(shared_edges, key=lambda e: e.edge_id):
        shared_partners.setdefault(edge.source_endpoint_id, []).append(edge)
        shared_partners.setdefault(edge.target_endpoint_id, []).append(edge)

    H = np.zeros((len(readers), len(unique_subjects)), dtype=float)
    entries: dict[tuple[int, int], EntryMeta] = {}

    for subject in unique_subjects:
        column = subject_index[subject]
        own_reader = mapping.most_specific_reader(subject)
        if own_reader is None or own_reader not in reader_index:
            continue  # subject has no row: absent, never an all-zero guess
        own_row = reader_index[own_reader]

        # A verified leg carries half the load (a partial observation); an
        # ordinary endpoint carries all of it (a direct observation).
        leg = _leg_coefficient(topology, own_reader)
        base_coeff = 1.0 if leg is None else leg
        base_obs = Observability.DIRECT if leg is None else Observability.PARTIAL

        path = _path_to_root(own_reader, parent_edge)  # nearest ancestor first

        def put(row: int, col: int, coeff: float, obs: Observability,
                lifecycle: EdgeLifecycleV3 | None, edge_ids: tuple[str, ...]) -> None:
            """Write one non-zero entry; a subject appears at most once per row."""
            key = (row, col)
            if not np.isfinite(coeff) or coeff == 0.0 or key in entries:
                return
            H[row, col] = coeff
            entries[key] = EntryMeta(
                coefficient=coeff,
                observability=obs,
                edge_lifecycle=lifecycle,
                edge_ids=edge_ids,
            )

        # (1) The most specific reader observes the subject directly.
        put(own_row, column, base_coeff, base_obs, None, ())

        # (2) CONFIRMED CONTAINS ancestors see it as EXPECTED with β^path.
        coeff = base_coeff
        for depth, edge in enumerate(path, start=1):
            coeff *= _edge_beta(edge, factors, at)
            ancestor = edge.source_endpoint_id
            if ancestor not in reader_index:
                continue
            path_slice = tuple(path[:depth])
            partial = any(e.beta_basis == "estimated" for e in path_slice)
            put(
                reader_index[ancestor],
                column,
                coeff,
                Observability.PARTIAL if partial else Observability.EXPECTED,
                EdgeLifecycleV3.CONFIRMED,
                tuple(e.edge_id for e in path_slice),
            )

        # (3) SHARED_LOAD puts the subject in the partner's row as well.
        for edge in shared_partners.get(own_reader, ()):
            partner = (
                edge.target_endpoint_id
                if edge.source_endpoint_id == own_reader
                else edge.source_endpoint_id
            )
            if partner not in reader_index:
                continue
            put(
                reader_index[partner],
                column,
                base_coeff,
                Observability.EXPECTED,
                edge.lifecycle,
                (edge.edge_id,),
            )

    # -- rows (residualised parents) ----------------------------------------- #
    children_of: dict[str, list[str]] = {}
    for edge in _edges_of_relation(topology, RelationType.CONTAINS):
        children_of.setdefault(edge.source_endpoint_id, []).append(edge.target_endpoint_id)

    rows: list[RowMeta] = []
    for reader in readers:
        has_present_children = any(c in reader_index for c in children_of.get(reader, ()))
        rows.append(
            RowMeta(
                reader_id=reader,
                kind="residualised_parent" if has_present_children else "reader",  # type: ignore[arg-type]
                sigma_w=None,  # absent sigma is None, never 0.0
                sigma_inflation=1.0,  # filled in below once H is complete
                excluded_reason=None,
            )
        )

    # -- unknown scopes ------------------------------------------------------- #
    # One column per reader scope, entering that reader's row and all its
    # CONFIRMED ancestors.  A CONTAINS child that is not a row is a missing
    # child: its own unknown column is scoped to the parent chain so the parent
    # row is never dropped (DEC-12; the soft prior value comes from T3/I3).
    unknown_scopes: dict[str, tuple[int, ...]] = {}

    def scope_rows(reader: str) -> tuple[int, ...]:
        involved = [reader, *_ancestor_ids(reader, parent_edge)]
        return tuple(sorted({reader_index[r] for r in involved if r in reader_index}))

    for reader in readers:
        indices = scope_rows(reader)
        if indices:
            unknown_scopes[reader] = indices

    for parent, children in children_of.items():
        if parent not in reader_index:
            continue
        for child in children:
            if child in reader_index:
                continue
            involved = [parent, *_ancestor_ids(parent, parent_edge)]
            indices = tuple(sorted({reader_index[r] for r in involved if r in reader_index}))
            if indices:
                unknown_scopes[child] = indices

    unknown = tuple(sorted(unknown_scopes.items()))

    # -- conservative σ inflation for correlated rows (§8.3, AT-9) ----------- #
    supports = [set(np.flatnonzero(H[row]).tolist()) for row in range(H.shape[0])]
    final_rows: list[RowMeta] = []
    for row, reader in enumerate(readers):
        ancestors = _ancestor_ids(reader, parent_edge)
        k = 1
        for other_row, other in enumerate(readers):
            if other_row == row:
                continue
            same_chain = other in ancestors or reader in _ancestor_ids(other, parent_edge)
            is_shared = any(
                e.source_endpoint_id == other or e.target_endpoint_id == other
                for e in shared_partners.get(reader, ())
            )
            if (same_chain or is_shared) and supports[row] & supports[other_row]:
                k += 1
        final_rows.append(replace(rows[row], sigma_inflation=math.sqrt(k)))

    # -- soft alternatives (PROVISIONAL edges, listed but never applied) ----- #
    try:
        soft_edges = topology.soft_alternatives()
    except Exception:  # pragma: no cover - defensive for odd test doubles
        soft_edges = ()
    soft = tuple(
        AlternativeEdge(
            edge_id=edge.edge_id,
            source_endpoint_id=edge.source_endpoint_id,
            target_endpoint_id=edge.target_endpoint_id,
            relation=edge.relation,
            lifecycle=edge.lifecycle,
            beta=edge.beta,
        )
        for edge in sorted(soft_edges, key=lambda e: e.edge_id)
    )

    return ObservationOperator(
        H=H,
        rows=tuple(final_rows),
        subjects=unique_subjects,
        entries=MappingProxyType(entries),
        unknown_scopes=unknown,
        soft_alternatives=soft,
        topology_version_id=topology_version_id,
        mapping_version_id=mapping_version_id,
        at=at,
    )


# --------------------------------------------------------------------------- #
# Cache.
# --------------------------------------------------------------------------- #

CacheKey = tuple[
    str,
    str,
    tuple[tuple[str, str, float, str, str, str], ...],
    tuple[str, ...],
    tuple[tuple[str, str], ...],
]


@dataclass
class OperatorCache:
    """Caches built operators by topology/mapping version and factor revision."""

    def __init__(self) -> None:
        self._entries: dict[CacheKey, ObservationOperator] = {}
        self.hits = 0
        self.misses = 0

    def build(
        self,
        topology: TopologyView,
        mapping: MappingView,
        factors: FactorRegistry | None,
        reader_ids: Sequence[str],
        subjects: Sequence[tuple[str, str]],
        at: datetime,
    ) -> ObservationOperator:
        topo_version = _topology_version(topology)
        map_version = _mapping_version(mapping)
        revisions = _factor_revision_set(factors, reader_ids, at)
        key: CacheKey = (topo_version, map_version, revisions, tuple(reader_ids), tuple(subjects))

        cached = self._entries.get(key)
        if cached is not None:
            self.hits += 1
            if cached.at == at:
                return cached
            # Same structure and factor revision, different instant: refresh the
            # stamp without rebuilding H (still a cache hit).
            return replace(cached, at=at)

        self.misses += 1
        operator = _build(
            topology,
            mapping,
            factors,
            reader_ids,
            subjects,
            at,
            topology_version_id=topo_version,
            mapping_version_id=map_version,
        )
        self._entries[key] = operator
        return operator

    def clear(self) -> None:
        self._entries.clear()
        self.hits = 0
        self.misses = 0

    def stats(self) -> dict[str, float]:
        total = self.hits + self.misses
        return {
            "hits": self.hits,
            "misses": self.misses,
            "size": len(self._entries),
            "hit_rate": 1.0 if total == 0 else self.hits / total,
        }


_DEFAULT_CACHE = OperatorCache()


def build_observation_operator(
    topology: TopologyView,
    mapping: MappingView,
    factors: FactorRegistry | None,
    reader_ids: Sequence[str],
    subjects: Sequence[tuple[str, str]],
    at: datetime,
) -> ObservationOperator:
    """Build (or fetch from cache) the observation operator H(T, M, at)."""
    return _DEFAULT_CACHE.build(topology, mapping, factors, reader_ids, subjects, at)


def operator_cache() -> OperatorCache:
    """The shared cache instance (exposed for tests and cache metrics)."""
    return _DEFAULT_CACHE


def operator_cache_stats() -> dict[str, float]:
    return _DEFAULT_CACHE.stats()


def clear_operator_cache() -> None:
    _DEFAULT_CACHE.clear()
