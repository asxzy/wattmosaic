"""Event-driven incremental matrix updater (M2, design §8 / HB §10).

:class:`MatrixUpdater` implements the K08 ``MatrixUpdater`` Protocol: on each
:class:`~wattmosaic_engine.contracts.JointInferenceResult` it recomputes **only
the affected cells** with M1's pure projection and funnels them through
:class:`~wattmosaic_engine.mapping.store.MatrixStore`, which upserts through S15
in one transaction.  The affected set is exactly the design §8 set: every
(subject, reader) pair where the subject appears in the result (as an estimate,
a group or a candidate) and the reader is a row the subject enters in the
operator, plus the group cells.  Cells outside that set are untouched.

A mapping edge changes only when a CONFIRMED match introduces a new
(subject, reader) association; in that case a mapping version is proposed and
promoted through the injected :class:`VersionService`.  Contribution changes
alone never bump the version (rule 20).  Independent events are deduplicated by
``event_group_id`` — repeated samples of one event count once (HB §10.3).

Production code depends only on K00/K03/K04/K05 contracts, the K08 runtime
Protocols and the M1 projection.  ``runtime.fakes`` is never imported.
"""

from __future__ import annotations

from collections.abc import Mapping as MappingABC
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from wattmosaic_engine.contracts import (
    Bounds,
    CellEvents,
    CellMapping,
    CellPower,
    CellProvenance,
    CellTimestamps,
    CellVisual,
    EdgeEvidence,
    EdgeLifecycleV3,
    EstimateStatus,
    IdentifiabilityStatus,
    JointInferenceResult,
    MappingCell,
    MappingEdge,
    MappingStatus,
    MatchState,
    Observability,
    VersionTriple,
)
from wattmosaic_engine.contracts.foundation import ConfidenceVectorV3
from wattmosaic_engine.runtime.interfaces import (
    Clock,
    MatrixDelta,
    ObservationOperator,
    VersionService,
)

from .projection import materialise_cells
from .store import CellKey, MatrixStore

__all__: list[str] = ["MatrixUpdater"]


def _value(item: Any) -> str:
    return str(item.value) if hasattr(item, "value") else str(item)


def _resolve(obj: Any, *names: str) -> Any:
    if isinstance(obj, MappingABC):
        for name in names:
            if name in obj:
                return obj[name]
        return None
    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
    return None


class MatrixUpdater:
    """Recompute only affected cells per result; bump mapping versions on edges."""

    def __init__(
        self,
        store: MatrixStore | None = None,
        versions: VersionService | None = None,
        *,
        clock: Clock | None = None,
        history: Any | None = None,
    ) -> None:
        self.store = store or MatrixStore(clock=clock)
        self.versions = versions
        # No wall clock in production: an injected clock is required to produce
        # replay-identical matrices (AT-19).  The fallback is a fixed origin,
        # never ``datetime.now()``.
        self.clock: Clock = clock or (lambda: datetime(2026, 1, 1, tzinfo=UTC))
        self.history = history

    # -- MatrixUpdater ----------------------------------------------------- #

    def apply(
        self, result: JointInferenceResult, operator: ObservationOperator
    ) -> MatrixDelta:
        """Recompute the affected cells of one inference result."""
        at = _resolve(result, "computed_at") or self.clock()
        versions: VersionTriple = _resolve(result, "versions") or VersionTriple(
            topology_version_id=operator.topology_version_id,
            mapping_version_id=operator.mapping_version_id,
            model_set_id="mset-unknown",
        )
        map_view = self._mapping_view(versions)
        matches = _resolve(result, "matches") or {}

        cells, _, _ = materialise_cells(
            operator, result, map_view, matches, self.history
        )

        affected = self._affected_keys(result, operator)
        # M1's projection marks members GROUP_ONLY but does not emit a GROUP row
        # cell; M2 synthesises those cells here (HB §17 step 6 "the relevant
        # group-level cell").
        cells.extend(self._group_cells(result, operator, versions, at))
        changed = [cell for cell in cells if self.store.key(cell) in affected]

        new_edges = self._new_confirmed_edges(result, operator, map_view, matches)
        mapping_changed = bool(new_edges)
        if mapping_changed and self.versions is not None:
            self._promote_new_edges(new_edges, result)

        event_group_id = _resolve(result, "event_group_id")
        written = self.store.apply_cells(
            changed, at=at, reason="apply", event_group_id=event_group_id
        )
        return MatrixDelta(
            upserted=written, retired=(), reason="apply"
        )

    def reconcile(self, reason: str) -> MatrixDelta:
        """Deterministic, audited reconciliation (delegate to the reconciler)."""
        from .reconcile import Reconciler

        return Reconciler(self.store, self.versions, clock=self.clock).reconcile(reason)

    # -- internals ---------------------------------------------------------- #

    def _mapping_view(self, versions: VersionTriple) -> Any:
        if self.versions is None:
            return None
        try:
            return self.versions.mapping_view(versions.mapping_version_id)
        except (KeyError, ValueError):
            return None

    @staticmethod
    def _subjects_in_result(result: JointInferenceResult) -> set[tuple[str, str]]:
        subjects: set[tuple[str, str]] = set()
        for estimate in _resolve(result, "device_estimates") or ():
            subjects.add(
                (_value(_resolve(estimate, "subject_kind", "kind") or "device"),
                 str(_resolve(estimate, "subject_id")))
            )
        for group in _resolve(result, "group_estimates") or ():
            for member in _resolve(group, "member_ids") or ():
                subjects.add(("device", str(member)))
        for candidate in _resolve(result, "candidates") or ():
            kind = _value(_resolve(candidate, "subject_kind") or "device")
            if kind in ("device", "component", "unknown_profile"):
                subjects.add((kind, str(_resolve(candidate, "subject_id"))))
        return subjects

    def _affected_keys(
        self, result: JointInferenceResult, operator: ObservationOperator
    ) -> set[CellKey]:
        subjects = self._subjects_in_result(result)
        affected: set[CellKey] = set()
        for (row_index, subject_index), _entry in operator.entries.items():
            if row_index >= len(operator.rows) or subject_index >= len(operator.subjects):
                continue
            kind, subject_id = operator.subjects[subject_index]
            if (str(kind), str(subject_id)) not in subjects:
                continue
            reader = operator.rows[row_index].reader_id
            row_kind = "component" if str(kind) == "component" else "device"
            affected.add((row_kind, str(subject_id), reader))
        # Group cells: one per group in the result (HB §17 step 6 "the relevant
        # group-level cell"), on the most-specific reader its members enter.
        for group in _resolve(result, "group_estimates") or ():
            reader = self._group_reader(group, operator)
            if reader is not None:
                affected.add(("group", str(_resolve(group, "group_id")), reader))
        return affected

    @staticmethod
    def _group_reader(group: Any, operator: ObservationOperator) -> str | None:
        """The most-specific reader in the operator that a group's members enter."""
        members = {str(m) for m in (_resolve(group, "member_ids") or ())}
        for (row_index, subject_index), entry in sorted(operator.entries.items()):
            if row_index >= len(operator.rows) or subject_index >= len(operator.subjects):
                continue
            _, sid = operator.subjects[subject_index]
            if str(sid) not in members:
                continue
            # DIRECT entries are the most-specific reader; EXPECTED entries are
            # ancestors.  Prefer the DIRECT one so the group cell sits on the
            # leaf, not a parent.
            if _value(getattr(entry, "observability", "")) == Observability.DIRECT.value:
                return operator.rows[row_index].reader_id
        for (row_index, subject_index), _entry in sorted(operator.entries.items()):
            if row_index < len(operator.rows) and subject_index < len(operator.subjects):
                _, sid = operator.subjects[subject_index]
                if str(sid) in members:
                    return operator.rows[row_index].reader_id
        return None

    @staticmethod
    def _group_cells(
        result: JointInferenceResult,
        operator: ObservationOperator,
        versions: VersionTriple,
        at: datetime,
    ) -> list[MappingCell]:
        """Synthesise one GROUP cell per group (HB §17 step 6)."""
        cells: list[MappingCell] = []
        for group in _resolve(result, "group_estimates") or ():
            reader = MatrixUpdater._group_reader(group, operator)
            if reader is None:
                continue
            group_id = str(_resolve(group, "group_id"))
            point = _resolve(group, "point_w")
            ident = _resolve(group, "identifiability") or IdentifiabilityStatus.GROUP_IDENTIFIABLE
            bounds = _resolve(group, "bounds")
            if bounds is None:
                bounds = Bounds(lower_w=None if point is None else 0.0, upper_w=None)
            cells.append(
                MappingCell(
                    row_kind="group",
                    row_id=group_id,
                    reader_id=reader,
                    mapping=CellMapping(
                        observability=Observability.INFERRED,
                        relation_type=None,
                        mapping_status=MappingStatus.CONFIRMED,
                        valid_from=at,
                    ),
                    power=CellPower(
                        measurement_status=(
                            EstimateStatus.ESTIMATED
                            if point is not None
                            else EstimateStatus.UNKNOWN
                        ),
                        estimated_power_w=point,
                        bounds=bounds,
                    ),
                    identifiability=ident,
                    group_id=group_id,
                    event_association=CellEvents(),
                    confidence=ConfidenceVectorV3(),
                    provenance=CellProvenance(
                        topology_version_id=versions.topology_version_id,
                        mapping_version_id=versions.mapping_version_id,
                        model_version_id=versions.model_set_id,
                    ),
                    timestamps=CellTimestamps(
                        first_observed_at=at, last_observed_at=at, updated_at=at
                    ),
                    visual=CellVisual.CONFIRMED_INFERRED,
                )
            )
        return cells

    def _new_confirmed_edges(
        self,
        result: JointInferenceResult,
        operator: ObservationOperator,
        map_view: Any,
        matches: MappingABC[str, Any],
    ) -> list[MappingEdge]:
        """CONFIRMED matches that introduce a new (subject, reader) association."""
        confirmed_subjects: set[str] = set()
        for candidate in _resolve(result, "candidates") or ():
            cid = str(_resolve(candidate, "candidate_id"))
            if _value(matches.get(cid)) == _value(MatchState.CONFIRMED):
                confirmed_subjects.add(str(_resolve(candidate, "subject_id")))

        new_edges: list[MappingEdge] = []
        for estimate in _resolve(result, "device_estimates") or ():
            subject_id = str(_resolve(estimate, "subject_id"))
            if subject_id not in confirmed_subjects:
                continue
            kind = str(_resolve(estimate, "subject_kind", "kind") or "device")
            readers = list(_resolve(estimate, "source_reader_ids") or ())
            reader = readers[0] if readers else None
            if reader is None:
                continue
            if map_view is not None and hasattr(map_view, "most_specific_reader"):
                existing = map_view.most_specific_reader((kind, subject_id))
                if existing == reader:
                    continue
            new_edges.append(
                MappingEdge(
                    mapping_edge_id=f"m::{kind}::{subject_id}->{reader}",
                    subject_kind=kind,  # type: ignore[arg-type]
                    subject_id=subject_id,
                    endpoint_id=reader,
                    observability=Observability.DIRECT,
                    lifecycle=EdgeLifecycleV3.CONFIRMED,
                    declared_by="inference",
                    evidence=EdgeEvidence(),
                    valid_from=_resolve(result, "computed_at") or self.clock(),
                )
            )
        return new_edges

    def _promote_new_edges(
        self, new_edges: Sequence[MappingEdge], result: JointInferenceResult
    ) -> None:
        assert self.versions is not None
        current = self.versions.current().mapping_version_id
        edges: list[MappingEdge] = []
        map_view = self.versions.mapping_view(current)
        getter = getattr(map_view, "edges", None)
        if callable(getter):
            try:
                edges = list(getter())
            except TypeError:
                edges = []
        known = {e.mapping_edge_id for e in edges}
        for edge in new_edges:
            if edge.mapping_edge_id not in known:
                edges.append(edge)
        version_id = self.versions.propose_mapping(
            edges, _resolve(result, "event_group_id")
        )
        self.versions.promote_mapping(version_id, "confirmed mapping edge")
