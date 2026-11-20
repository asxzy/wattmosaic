"""Pure v3 matrix projection (M1, design §8 / HB §9–10).

``materialise_cells`` turns an operator + estimates + mapping + history +
snapshot into the sparse set of :class:`MappingCell` objects, together with the
per-reader and physical totals.  It is a pure function of injected inputs: no
database and no wall clock (the time comes from the snapshot / operator ``at``).

Key rules (design §8, DEC-19, DEC-27):

* a cell exists only when the mapping edge, an EXPECTED ancestor, a match
  history, or a conflicted/retired history justifies it;
* identifiability is its own field, never part of the mapping status;
* contributions come from *current* estimates, never a model's total as a
  stand-in (``mapping.py:430-436`` today);
* ``physical_total`` counts each subject once (from estimates and group sums)
  plus the main residual; reader totals are per column and never summed;
* absent values are ``None``, never ``0.0`` (HB §18).
"""

from __future__ import annotations

from collections.abc import Mapping as MappingABC
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from wattmosaic_engine.contracts import (
    Bounds,
    CellEvents,
    CellMapping,
    CellPower,
    CellProvenance,
    CellTimestamps,
    ConfidenceVectorV3,
    EstimateStatus,
    IdentifiabilityStatus,
    MappingCell,
    MappingStatus,
    Observability,
    PhysicalTotal,
    ReaderColumnSummary,
    RelationType,
)
from wattmosaic_engine.contracts.foundation import _BOUNDS_ALLOW_POINT
from wattmosaic_engine.runtime.interfaces import EntryMeta, ObservationOperator

from .cells import finite_or_none, validate_bounds_bracket, visual_category

__all__: list[str] = [
    "materialise_cells",
    "materialise_matrix",
    "operator_root_readers",
    "physical_total_from_cells",
    "reader_totals_from_cells",
    "totals_from_snapshot",
]

_SUBJECT_DELIMITER = "|"


def operator_root_readers(operator: ObservationOperator) -> tuple[str, ...]:
    """The topology root reader(s) encoded in ``operator`` (never by name).

    H(T, M, t) carries the CONFIRMED CONTAINS ancestry of the topology it was
    built from, in two places:

    * every unknown scope ``S`` lists ``S``'s own row plus its ancestors' rows,
      so each other reader in a scope is an ancestor of ``S``;
    * a subject column's entries walk its CONTAINS path upwards: the most
      specific reader has an empty edge path and the depth-``d`` ancestor a
      path of ``d`` edges.  A column with two entries at the same depth (a
      SHARED_LOAD partner beside a parent) is ambiguous and is skipped.

    A root is a reader that is an ancestor of some reader and has no ancestor
    itself.  No ancestry at all yields ``()``: the root is unknown, so the
    root residual is ``None`` (HB §18), not some reader's residual.
    """
    rows = operator.rows
    ancestors: dict[str, set[str]] = {}
    for scope, indices in operator.unknown_scopes:
        for index in indices:
            if index < len(rows) and rows[index].reader_id != scope:
                ancestors.setdefault(str(scope), set()).add(rows[index].reader_id)
    by_column: dict[int, list[tuple[int, str]]] = {}
    for (row, column), entry in operator.entries.items():
        if row < len(rows):
            depth = len(getattr(entry, "edge_ids", ()) or ())
            by_column.setdefault(column, []).append((depth, rows[row].reader_id))
    for chain in by_column.values():
        depths = [depth for depth, _reader in chain]
        if len(set(depths)) != len(depths) or 0 not in depths:
            continue
        chain.sort()
        for position, (_depth, reader) in enumerate(chain):
            for _higher_depth, higher in chain[position + 1:]:
                ancestors.setdefault(reader, set()).add(higher)
    parents = {parent for found in ancestors.values() for parent in found}
    return tuple(sorted(parents - {r for r, found in ancestors.items() if found}))

_NON_POINT_IDENTIFIABILITY = frozenset(
    {
        IdentifiabilityStatus.NON_IDENTIFIABLE,
        IdentifiabilityStatus.INSUFFICIENT_EVIDENCE,
    }
)


def _subject_key(kind: str, subject_id: str) -> str:
    return f"{kind}{_SUBJECT_DELIMITER}{subject_id}"


def _value(item: Any) -> str:
    return str(item.value) if hasattr(item, "value") else str(item)


# --------------------------------------------------------------------------- #
# Input normalisation: result_or_snapshot and history can be objects or dicts.
# --------------------------------------------------------------------------- #


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


def _device_estimates(result_or_snapshot: Any) -> list[Any]:
    return list(_resolve(result_or_snapshot, "device_estimates") or ())


def _group_estimates(result_or_snapshot: Any) -> list[Any]:
    return list(_resolve(result_or_snapshot, "group_estimates") or ())


def _unknown_estimates(result_or_snapshot: Any) -> list[Any]:
    return list(_resolve(result_or_snapshot, "unknown_estimates") or ())


def _residual_by_reader(result_or_snapshot: Any) -> MappingABC[str, Any]:
    value = _resolve(result_or_snapshot, "residual_w_by_reader", "reader_residuals")
    return value if isinstance(value, MappingABC) else {}


def _versions(result_or_snapshot: Any, operator: ObservationOperator) -> tuple[str, str, str | None]:
    versions = _resolve(result_or_snapshot, "versions")
    topo = _resolve(versions, "topology_version_id") if versions is not None else None
    mapping = _resolve(versions, "mapping_version_id") if versions is not None else None
    model = _resolve(versions, "model_set_id") if versions is not None else None
    # Fall back to the operator for version ids when the snapshot has none.
    topo = topo or operator.topology_version_id
    mapping = mapping or operator.mapping_version_id
    if model is None:
        model = _resolve(result_or_snapshot, "model_version_id")
    return str(topo), str(mapping), str(model) if model is not None else None


def _at(result_or_snapshot: Any, operator: ObservationOperator) -> datetime:
    for name in ("computed_at", "observed_at", "at"):
        value = _resolve(result_or_snapshot, name)
        if isinstance(value, datetime):
            return value
    return operator.at


# --------------------------------------------------------------------------- #
# History lookup (match history + conflicted/retired history).
# --------------------------------------------------------------------------- #


def _history_entries(history: Any, subject_id: str, reader_id: str) -> list[Any]:
    """Find history rows relevant to a (subject, reader) pair."""
    if history is None:
        return []
    getter = getattr(history, "events_touching", None)
    if callable(getter):
        try:
            return list(getter((subject_id,), datetime.min))  # type: ignore[arg-type]
        except TypeError:
            return []
    if isinstance(history, MappingABC):
        rows = history.get("rows", history.get("events", ()))
        return [
            row
            for row in rows
            if isinstance(row, MappingABC)
            and subject_id in (row.get("subject_ids") or ())
            and reader_id in (row.get("reader_ids") or ())
        ]
    return []


def _history_counts(entries: Sequence[Any]) -> tuple[int, int]:
    """(independent match count, conflicted/retired count) from history rows."""
    matched = 0
    terminal = 0
    for entry in entries:
        status = _resolve(entry, "status", "match_state", "lifecycle")
        status_text = _value(status) if status is not None else ""
        if status_text in ("confirmed", "provisional", "matched", "final"):
            matched += 1
        if status_text in ("conflicted", "retired", "rejected", "suspected_change"):
            terminal += 1
    return matched, terminal


# --------------------------------------------------------------------------- #
# Estimate and observability helpers.
# --------------------------------------------------------------------------- #


def _estimate_point(est: Any) -> float | None:
    """The current point estimate, never a model total stand-in (HB §18)."""
    point = finite_or_none(_resolve(est, "point_w", "estimated_power_w", "value_w"))
    if point is None:
        return None
    # DEC-19: a non-identifiable member never gets a number.
    ident = _resolve(est, "identifiability")
    if ident is not None and _value(ident) in {_value(x) for x in _NON_POINT_IDENTIFIABILITY}:
        return None
    return point


def _estimate_status(est: Any) -> EstimateStatus:
    status = _resolve(est, "status")
    if status is None:
        return EstimateStatus.UNKNOWN
    try:
        return EstimateStatus(_value(status))
    except ValueError:
        return EstimateStatus.UNKNOWN


def _estimate_identifiability(est: Any) -> IdentifiabilityStatus:
    ident = _resolve(est, "identifiability")
    if ident is None:
        return IdentifiabilityStatus.INSUFFICIENT_EVIDENCE
    try:
        return IdentifiabilityStatus(_value(ident))
    except ValueError:
        return IdentifiabilityStatus.INSUFFICIENT_EVIDENCE


def _cell_power(*, bounds: Bounds | None, **fields: Any) -> CellPower:
    """Build a :class:`CellPower`, carrying an I5 point interval through.

    A MEASURED / IDENTIFIABLE estimate's zero-width :class:`Bounds` can only be
    made by the private ``Bounds._point`` path.  ``CellPower.bounds`` is
    ``Bounds | None``, and pydantic re-runs the interval validator on a union
    member, so the already-validated point used to raise and abort the whole
    matrix update.  Only an existing ``Bounds`` instance is let through.
    """
    point = (
        isinstance(bounds, Bounds)
        and bounds.lower_w is not None
        and bounds.lower_w == bounds.upper_w
    )
    token = _BOUNDS_ALLOW_POINT.set(True) if point else None
    try:
        return CellPower(bounds=bounds, **fields)
    finally:
        if token is not None:
            _BOUNDS_ALLOW_POINT.reset(token)


def _estimate_bounds(est: Any) -> Bounds | None:
    bounds = _resolve(est, "bounds")
    if bounds is None:
        return None
    if isinstance(bounds, Bounds):
        return bounds
    if isinstance(bounds, MappingABC):
        return Bounds(
            lower_w=finite_or_none(bounds.get("lower_w")),
            upper_w=finite_or_none(bounds.get("upper_w")),
            basis=bounds.get("basis"),
        )
    lower = finite_or_none(_resolve(bounds, "lower_w"))
    upper = finite_or_none(_resolve(bounds, "upper_w"))
    basis = _resolve(bounds, "basis")
    if lower is None and upper is None and basis is None:
        return None
    return Bounds(
        lower_w=lower,
        upper_w=upper,
        basis=str(basis) if basis is not None else None,
    )


def _estimate_confidence(est: Any) -> ConfidenceVectorV3:
    confidence = _resolve(est, "confidence")
    if isinstance(confidence, ConfidenceVectorV3):
        return confidence
    if isinstance(confidence, MappingABC):
        fields = ConfidenceVectorV3.model_fields
        data = {key: confidence.get(key) for key in fields if key in confidence}
        return ConfidenceVectorV3(**data)
    return ConfidenceVectorV3()


# --------------------------------------------------------------------------- #
# Observability / mapping status derivation.
# --------------------------------------------------------------------------- #


def _observability_and_status(
    entry_meta: EntryMeta,
) -> tuple[Observability, MappingStatus, RelationType | None]:
    """Derive (observability, mapping_status, relation_type) for an H entry.

    ``EntryMeta.observability`` (DIRECT / EXPECTED / …) from the operator is the
    authority.  The mapping status is *independent* of identifiability: it is
    CONFIRMED for DIRECT/CONFIRMED-edge entries, PROVISIONAL for soft edges and
    otherwise UNKNOWN.  A MEASURED estimate with a DIRECT entry is CONFIRMED.
    """
    observability = entry_meta.observability
    lifecycle = entry_meta.edge_lifecycle
    if isinstance(observability, str):
        try:
            observability = Observability(observability)
        except ValueError:
            observability = Observability.UNKNOWN
    if lifecycle is not None and hasattr(lifecycle, "value"):
        lifecycle_value = str(lifecycle.value)
    else:
        lifecycle_value = str(lifecycle) if lifecycle is not None else None

    relation_type: RelationType | str | None = None
    if observability is Observability.DIRECT:
        # Set precisely by the caller (DIRECT_ENDPOINT for a single-subject
        # endpoint, CONTAINS otherwise).
        relation_type = None
        status = MappingStatus.CONFIRMED
    elif lifecycle_value == "provisional":
        status = MappingStatus.PROVISIONAL
    elif lifecycle_value == "conflicted":
        status = MappingStatus.CONFLICTED
    elif lifecycle_value == "retired":
        status = MappingStatus.RETIRED
    elif lifecycle_value == "suspected_change":
        status = MappingStatus.SUSPECTED_CHANGE
    elif lifecycle_value == "confirmed":
        # A CONFIRMED CONTAINS path makes the EXPECTED ancestor mapping
        # CONFIRMED too (design §8: ESTIMATED + CONFIRMED -> CONFIRMED_INFERRED).
        status = MappingStatus.CONFIRMED
    else:
        status = MappingStatus.UNKNOWN
    return observability, status, relation_type


# --------------------------------------------------------------------------- #
# Main materialiser.
# --------------------------------------------------------------------------- #


def materialise_cells(
    operator: ObservationOperator,
    result_or_snapshot: Any,
    mapping_view: Any,
    match_view: Any,
    history: Any,
    *,
    root_reader_ids: Sequence[str] | None = None,
) -> tuple[list[MappingCell], dict[str, ReaderColumnSummary], PhysicalTotal]:
    """Materialise the sparse cells, reader totals and physical total.

    Pure and deterministic: for identical inputs the returned cells serialise
    byte-identically via :func:`wattmosaic_engine.mapping.cells.stable_json`.

    ``root_reader_ids`` names the active topology's root reader(s) whose
    residual enters the physical total; when omitted they are read from the
    operator's own ancestry (:func:`operator_root_readers`).

    Parameters
    ----------
    operator:
        The time-scoped ``ObservationOperator`` (T2/K08).  Its ``entries`` map
        ``(row_index, subject_index)`` to ``EntryMeta``; only non-zero entries
        produce cells, which is what makes the result sparse.
    result_or_snapshot:
        A ``JointInferenceResult`` / ``InferenceSnapshot`` (or a mapping) with
        ``device_estimates``, ``group_estimates``, ``unknown_estimates`` and
        ``residual_w_by_reader``.
    mapping_view:
        A ``MappingView`` (or duck type).  Used to decide DIRECT_ENDPOINT vs
        CONTAINS relation types.
    match_view:
        A mapping of ``subject_id -> MatchState`` (or a MatchStore snapshot).
    history:
        An ``IdentifiabilityHistory`` (or mapping) used for the match and
        conflicted/retired history that independently justifies a cell.
    """
    topo_version, map_version, model_version = _versions(result_or_snapshot, operator)
    at = _at(result_or_snapshot, operator)

    estimates = _device_estimates(result_or_snapshot)
    groups = _group_estimates(result_or_snapshot)
    unknowns = _unknown_estimates(result_or_snapshot)
    residual_by_reader = _residual_by_reader(result_or_snapshot)

    estimates_by_key = {
        _subject_key(_value(_resolve(e, "subject_kind", "kind") or "device"),
                     str(_resolve(e, "subject_id"))): e
        for e in estimates
    }

    matches: MappingABC[str, Any] = {}
    if isinstance(match_view, MappingABC):
        matches = match_view
    elif match_view is not None and callable(getattr(match_view, "snapshot", None)):
        matches = match_view.snapshot()

    cells: list[MappingCell] = []
    seen: set[tuple[str, str, str]] = set()  # (row_kind, row_id, reader_id)

    # 1. Device/component cells from the operator's non-zero H entries.  This
    #    propagates EXPECTED observability to ancestor readers because the fake
    #    and real operators both include those entries (design §8).
    for (row_index, subject_index), entry in sorted(operator.entries.items()):
        if row_index >= len(operator.rows) or subject_index >= len(operator.subjects):
            continue
        row_meta = operator.rows[row_index]
        reader_id = row_meta.reader_id
        kind, subject_id = operator.subjects[subject_index]
        kind_text = _value(kind)
        key = _subject_key(kind_text, subject_id)

        est = estimates_by_key.get(key)
        estimate_status = _estimate_status(est) if est is not None else EstimateStatus.UNKNOWN
        ident = _estimate_identifiability(est) if est is not None else IdentifiabilityStatus.INSUFFICIENT_EVIDENCE
        point = _estimate_point(est) if est is not None else None
        bounds = _estimate_bounds(est) if est is not None else None
        confidence = _estimate_confidence(est) if est is not None else ConfidenceVectorV3()
        group_id = str(_resolve(est, "group_id")) if est is not None and _resolve(est, "group_id") else None
        evidence_refs = list(_resolve(est, "evidence_refs") or ()) if est is not None else []

        history_entries = _history_entries(history, subject_id, reader_id)
        matched_count, terminal_count = _history_counts(history_entries)
        match_state = matches.get(subject_id) or matches.get(key)
        match_state_text = _value(match_state) if match_state is not None else None

        obs, mapping_status, relation_type = _observability_and_status(entry)

        # A MEASURED estimate is DIRECT; otherwise DIRECT_ENDPOINT only when the
        # subject's most-specific reader is this reader (a single-subject endpoint).
        most_specific = None
        if mapping_view is not None and callable(getattr(mapping_view, "most_specific_reader", None)):
            most_specific = mapping_view.most_specific_reader((kind_text, subject_id))
        if obs is Observability.DIRECT:
            relation_type = (
                "direct_endpoint"  # Literal allowed by CellMapping.relation_type (DEC-05)
                if str(most_specific) == reader_id
                else RelationType.CONTAINS
            )

        # Group members are GROUP_ONLY and never carry a number (DEC-19).
        is_group_member = group_id is not None and ident in (
            IdentifiabilityStatus.NON_IDENTIFIABLE,
            IdentifiabilityStatus.GROUP_IDENTIFIABLE,
        )
        row_kind = "component" if kind_text == "component" else "device"
        visual = visual_category(
            measurement_status=estimate_status,
            mapping_status=mapping_status,
            observability=obs,
            group_id=group_id,
            identifiability=ident,
        )
        emitted_point = None if is_group_member else point
        emitted_status = EstimateStatus.UNKNOWN if is_group_member else estimate_status

        power = _cell_power(
            measurement_status=emitted_status,
            estimated_power_w=emitted_point,
            bounds=bounds,
            operating_state=_resolve(est, "level") if est is not None else None,
            uncertainty_w=finite_or_none(_resolve(est, "uncertainty_w")) if est is not None else None,
        )
        cell = MappingCell(
            row_kind=row_kind,  # type: ignore[arg-type]
            row_id=subject_id,
            load_component_id=subject_id if kind_text == "component" else None,
            reader_id=reader_id,
            mapping=CellMapping(
                observability=obs,
                relation_type=relation_type,
                mapping_status=mapping_status,
                mapping_confidence=None,
                valid_from=at,
            ),
            power=power,
            identifiability=ident,
            group_id=group_id,
            event_association=CellEvents(
                matched_event_count=matched_count,
                confirmed_match_count=matched_count,
                provisional_match_count=0,
                last_matched_at=(
                    _resolve(history_entries[-1], "at") if history_entries else None
                ),
            ),
            confidence=confidence,
            provenance=CellProvenance(
                sources_used=[],
                evidence_refs=evidence_refs,
                topology_version_id=topo_version,
                model_version_id=model_version,
                mapping_version_id=map_version,
            ),
            timestamps=CellTimestamps(
                first_observed_at=at,
                last_observed_at=at,
                updated_at=at,
            ),
            visual=visual,
        )
        # The contract forbids a GROUP_ONLY cell with a point value; the
        # validator check is defensive but cheap.
        if not validate_bounds_bracket(emitted_point, bounds):
            raise ValueError(f"bounds do not bracket estimate for {key} on {reader_id}")
        identity = (row_kind, subject_id, reader_id)
        if identity not in seen:
            seen.add(identity)
            cells.append(cell)

    # 2. A match-history-only cell: a subject with an entry on a reader not in
    #    the operator still gets a cell if history justifies it (match or a
    #    conflicted/retired history).  Observability is INFERRED there.
    for subject_id, match_state in matches.items():
        match_state_text = _value(match_state)
        if match_state_text not in ("provisional", "confirmed", "conflicted", "expired", "rejected"):
            continue
        est = estimates_by_key.get(_subject_key("device", subject_id))
        if est is None:
            continue
        # Find readers for this subject from history; if none, skip (we cannot
        # invent a reader column).
        for entry in _history_entries(history, subject_id, "*"):
            reader_id = str(_resolve(entry, "reader_id") or "")
            if not reader_id:
                continue
            identity = ("device", subject_id, reader_id)
            if identity in seen:
                continue
            seen.add(identity)
            point = _estimate_point(est)
            ident = _estimate_identifiability(est)
            est_status = _estimate_status(est)
            status = (
                MappingStatus.CONFLICTED if match_state_text in ("conflicted", "rejected")
                else MappingStatus.PROVISIONAL if match_state_text == "provisional"
                else MappingStatus.CONFIRMED
            )
            cells.append(
                MappingCell(
                    row_kind="device",
                    row_id=subject_id,
                    reader_id=reader_id,
                    mapping=CellMapping(
                        observability=Observability.INFERRED,
                        relation_type=None,
                        mapping_status=status,
                        valid_from=at,
                    ),
                    power=CellPower(
                        measurement_status=est_status,
                        estimated_power_w=point,
                        bounds=_estimate_bounds(est),
                    ),
                    identifiability=ident,
                    group_id=str(_resolve(est, "group_id")) if _resolve(est, "group_id") else None,
                    event_association=CellEvents(),
                    confidence=_estimate_confidence(est),
                    provenance=CellProvenance(
                        topology_version_id=topo_version,
                        mapping_version_id=map_version,
                        model_version_id=model_version,
                    ),
                    timestamps=CellTimestamps(first_observed_at=at, last_observed_at=at, updated_at=at),
                    visual=visual_category(
                        measurement_status=est_status,
                        mapping_status=status,
                        observability=Observability.INFERRED,
                        group_id=str(_resolve(est, "group_id")) if _resolve(est, "group_id") else None,
                        identifiability=ident,
                    ),
                )
            )

    # 3. Reader-residual and unknown-profile cells (special rows).
    for reader_id, residual in residual_by_reader.items():
        residual_w = finite_or_none(residual)
        if residual is not None and not isinstance(residual, (int, float)):
            residual_w = finite_or_none(_resolve(residual, "residual_w", "point_w"))
        identity = ("reader_residual", str(reader_id), str(reader_id))
        if identity in seen:
            continue
        seen.add(identity)
        cells.append(
            MappingCell(
                row_kind="reader_residual",
                row_id=str(reader_id),
                reader_id=str(reader_id),
                mapping=CellMapping(
                    observability=Observability.EXPECTED,
                    relation_type=None,
                    mapping_status=MappingStatus.CONFIRMED,
                    valid_from=at,
                ),
                power=CellPower(
                    measurement_status=EstimateStatus.ESTIMATED,
                    estimated_power_w=residual_w,
                ),
                identifiability=IdentifiabilityStatus.IDENTIFIABLE,
                group_id=None,
                event_association=CellEvents(),
                confidence=ConfidenceVectorV3(),
                provenance=CellProvenance(
                    topology_version_id=topo_version,
                    mapping_version_id=map_version,
                    model_version_id=model_version,
                ),
                timestamps=CellTimestamps(first_observed_at=at, last_observed_at=at, updated_at=at),
                visual=visual_category(
                    measurement_status=EstimateStatus.ESTIMATED,
                    mapping_status=MappingStatus.CONFIRMED,
                    observability=Observability.EXPECTED,
                ),
            )
        )

    for unknown in unknowns:
        scope = str(_resolve(unknown, "scope_reader_id", "reader_id") or "")
        if not scope:
            continue
        profile_id = str(_resolve(unknown, "profile_id") or scope)
        identity = ("unknown_profile", profile_id, scope)
        if identity in seen:
            continue
        seen.add(identity)
        point = finite_or_none(_resolve(unknown, "point_w", "delta_w"))
        cells.append(
            MappingCell(
                row_kind="unknown_profile",
                row_id=profile_id,
                reader_id=scope,
                mapping=CellMapping(
                    observability=Observability.INFERRED,
                    relation_type=None,
                    mapping_status=MappingStatus.PROVISIONAL,
                    valid_from=at,
                ),
                power=CellPower(
                    measurement_status=EstimateStatus.ESTIMATED if point is not None else EstimateStatus.UNKNOWN,
                    estimated_power_w=point,
                ),
                identifiability=IdentifiabilityStatus.INSUFFICIENT_EVIDENCE,
                group_id=None,
                event_association=CellEvents(),
                confidence=ConfidenceVectorV3(),
                provenance=CellProvenance(
                    topology_version_id=topo_version,
                    mapping_version_id=map_version,
                    model_version_id=model_version,
                ),
                timestamps=CellTimestamps(first_observed_at=at, last_observed_at=at, updated_at=at),
                visual=visual_category(
                    measurement_status=EstimateStatus.ESTIMATED if point is not None else EstimateStatus.UNKNOWN,
                    mapping_status=MappingStatus.PROVISIONAL,
                    observability=Observability.INFERRED,
                ),
            )
        )

    reader_totals = reader_totals_from_cells(cells, result_or_snapshot)
    roots = operator_root_readers(operator) if root_reader_ids is None else tuple(root_reader_ids)
    total = physical_total_from_cells(
        cells, estimates, groups, residual_by_reader, root_reader_ids=roots
    )
    return cells, reader_totals, total


# --------------------------------------------------------------------------- #
# Aggregation (design §8: reader totals never summed; physical total once).
# --------------------------------------------------------------------------- #


def reader_totals_from_cells(
    cells: Sequence[MappingCell], result_or_snapshot: Any = None
) -> dict[str, ReaderColumnSummary]:
    """Per-reader totals.  Each reader is independent; nothing is summed across."""
    totals: dict[str, ReaderColumnSummary] = {}
    residual_by_reader = _residual_by_reader(result_or_snapshot) if result_or_snapshot is not None else {}
    readers = {cell.reader_id for cell in cells}
    readers.update(str(r) for r in residual_by_reader)
    for reader_id in sorted(readers):
        measured: float | None = None
        known: float | None = None
        unknown: float | None = None
        residual: float | None = None
        measured_parts: list[float] = []
        known_parts: list[float] = []
        unknown_parts: list[float] = []
        for cell in cells:
            if cell.reader_id != reader_id:
                continue
            point = cell.power.estimated_power_w
            if cell.row_kind == "reader_residual":
                if point is not None:
                    residual = point
                continue
            if cell.row_kind == "unknown_profile":
                if point is not None:
                    unknown_parts.append(point)
                continue
            if point is None:
                continue
            if cell.power.measurement_status == EstimateStatus.MEASURED:
                measured_parts.append(point)
            elif cell.row_kind in ("device", "component"):
                known_parts.append(point)
        if measured_parts:
            measured = sum(measured_parts)
        if known_parts:
            known = sum(known_parts)
        if unknown_parts:
            unknown = sum(unknown_parts)
        res_raw = residual_by_reader.get(reader_id)
        if residual is None and res_raw is not None:
            residual = finite_or_none(res_raw)
            if residual is None and not isinstance(res_raw, (int, float)):
                residual = finite_or_none(_resolve(res_raw, "residual_w", "point_w"))
        totals[reader_id] = ReaderColumnSummary(
            measured_w=measured,
            known_contrib_w=known,
            residual_w=residual,
            unknown_w=unknown,
        )
    return totals


def physical_total_from_cells(
    cells: Sequence[MappingCell],
    estimates: Sequence[Any] = (),
    groups: Sequence[Any] = (),
    residual_by_reader: MappingABC[str, Any] | None = None,
    root_reader_ids: Sequence[str] | None = None,
) -> PhysicalTotal:
    """The household physical total, counting each subject exactly once.

    Identified device estimates are summed once each (from the current
    estimates, never from a model total).  Group sums add once per group.  The
    root residual -- the residual of the topology's ``root_reader_ids`` -- is
    added once.  A missing term is ``None``, never ``0.0`` (HB §18).
    """
    measured_parts: list[float] = []
    identified_parts: list[float] = []
    group_parts: list[float] = []
    unknown_parts: list[float] = []

    seen_subjects: set[str] = set()
    for est in estimates:
        subject_id = str(_resolve(est, "subject_id") or "")
        if subject_id in seen_subjects:
            continue
        seen_subjects.add(subject_id)
        point = _estimate_point(est)
        if point is None:
            continue
        status = _estimate_status(est)
        if status is EstimateStatus.MEASURED:
            measured_parts.append(point)
        elif status is EstimateStatus.ESTIMATED:
            identified_parts.append(point)

    for group in groups:
        point = finite_or_none(_resolve(group, "point_w"))
        if point is not None:
            group_parts.append(point)
        else:
            bounds = _estimate_bounds(group)
            if bounds is not None and bounds.lower_w is not None and bounds.upper_w is not None:
                # A justified interval with no point: no fabricated number.
                pass

    for cell in cells:
        if cell.row_kind == "unknown_profile" and cell.power.estimated_power_w is not None:
            unknown_parts.append(cell.power.estimated_power_w)

    # The root residual: the residual of the topology's root reader(s), summed
    # over parallel roots / verified legs.  No root, or a root without a
    # residual, leaves the term None -- never another reader's residual.
    residual_w: float | None = None
    roots = tuple(str(r) for r in (root_reader_ids or ()))
    if residual_by_reader and roots:
        parts: list[float | None] = []
        for root in roots:
            raw = residual_by_reader.get(root)
            value = finite_or_none(raw)
            if value is None and raw is not None and not isinstance(raw, (int, float)):
                value = finite_or_none(_resolve(raw, "residual_w", "point_w"))
            parts.append(value)
        if all(part is not None for part in parts):
            residual_w = sum(part for part in parts if part is not None)

    def total_of(parts: list[float]) -> float | None:
        return sum(parts) if parts else None

    measured = total_of(measured_parts)
    identified = total_of(identified_parts)
    group = total_of(group_parts)
    unknown = total_of(unknown_parts)

    terms = [measured, identified, group, unknown, residual_w]
    total = sum(t for t in terms if t is not None) if any(t is not None for t in terms) else None

    return PhysicalTotal(
        measured_w=measured,
        identified_w=identified,
        group_w=group,
        unknown_w=unknown,
        residual_w=residual_w,
        total_w=total,
    )


def _stale_readers(snapshot: Any) -> set[str]:
    """Reader ids the snapshot lists as stale (``reader`` or ``reader: reason``)."""
    out: set[str] = set()
    for entry in _resolve(snapshot, "stale_sources") or ():
        reader = str(entry).split(":", 1)[0].strip()
        if reader:
            out.add(reader)
    return out


def totals_from_snapshot(
    cells: Sequence[MappingCell],
    snapshot: Any,
    *,
    root_reader_ids: Sequence[str] = (),
) -> tuple[dict[str, ReaderColumnSummary], PhysicalTotal]:
    """Per-reader and physical totals for the *current* matrix view.

    The cell-only aggregation knows the contributions but not what each reader
    measured or what it left unexplained, so a served matrix built from cells
    alone had null measured/residual columns and a null physical total.  This
    folds in the published snapshot (HB §12.1, HB §19.4):

    * ``measured_w`` is the reader's own normalised reading;
    * ``residual_w`` is the reader's residual point (a residual cell wins);
    * ``known_contrib_w`` sums the estimated and directly measured device
      cells; ``unknown_w`` stays the unknown-profile cell sum;
    * the physical total counts each estimate once, each group sum once and
      the residual of the topology's root reader(s) once (design §8).

    A stale reader keeps null measured and residual totals -- its reason lives
    in ``stale_sources`` -- never a held or fabricated 0 W (HB §18, AT-17).
    """
    stale = _stale_readers(snapshot)
    readings: dict[str, float | None] = {}
    for measurement in _resolve(snapshot, "reader_measurements") or ():
        reader = _resolve(measurement, "reader_id")
        if reader is not None:
            readings[str(reader)] = finite_or_none(_resolve(measurement, "active_power_w"))
    residuals: dict[str, float | None] = {}
    for point in _resolve(snapshot, "residual_estimates") or ():
        reader = _resolve(point, "reader_id")
        if reader is not None:
            residuals[str(reader)] = finite_or_none(_resolve(point, "residual_w"))
    for reader in stale:
        readings[reader] = None
        residuals[reader] = None

    base = reader_totals_from_cells(cells, None)
    totals: dict[str, ReaderColumnSummary] = {}
    for reader in sorted(set(base) | set(readings) | set(residuals)):
        cell_totals = base.get(reader) or ReaderColumnSummary()
        residual = None if reader in stale else (
            cell_totals.residual_w if cell_totals.residual_w is not None else residuals.get(reader)
        )
        # The cell-only ``measured_w`` is the sum of directly measured device
        # cells: with the reader's own reading in its place, those are known
        # contributions too (a plug's device is known, not residual).
        known_parts = [v for v in (cell_totals.known_contrib_w, cell_totals.measured_w) if v is not None]
        totals[reader] = ReaderColumnSummary(
            measured_w=readings.get(reader),
            known_contrib_w=sum(known_parts) if known_parts else None,
            residual_w=residual,
            unknown_w=cell_totals.unknown_w,
        )

    total = physical_total_from_cells(
        cells,
        _device_estimates(snapshot),
        list(_resolve(snapshot, "group_estimates") or ()),
        residuals,
        root_reader_ids=root_reader_ids,
    )
    return totals, total


def materialise_matrix(
    operator: ObservationOperator,
    result_or_snapshot: Any,
    mapping_view: Any,
    match_view: Any,
    history: Any,
    *,
    mapping_version_id: str | None = None,
    generated_at: datetime | None = None,
    columns: Sequence[Any] | None = None,
    rows: Sequence[Any] | None = None,
) -> Any:
    """Convenience: build a full ``MatrixPayload`` (K05) from the inputs.

    ``columns``/``rows`` are already-built trees; callers normally use
    :func:`build_columns` and :func:`build_rows` first.  This helper keeps the
    pure projection and the tree building separate so each can be tested alone.
    """
    from wattmosaic_engine.contracts import MatrixPayload, VersionTriple

    cells, reader_totals, total = materialise_cells(
        operator, result_or_snapshot, mapping_view, match_view, history
    )
    topo, mapping, model = _versions(result_or_snapshot, operator)
    versions = VersionTriple(
        topology_version_id=topo,
        mapping_version_id=mapping,
        model_set_id=model or "mset-unknown",
    )
    return MatrixPayload(
        mapping_version_id=mapping_version_id or mapping,
        versions=versions,
        generated_at=generated_at or _at(result_or_snapshot, operator),
        columns=list(columns or ()),
        rows=list(rows or ()),
        cells=cells,
        reader_totals=reader_totals,
        physical_total=total,
    )
