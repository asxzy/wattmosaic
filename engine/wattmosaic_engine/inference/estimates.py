"""Estimate assembly, status and reconciliation (I5, HB §8.5/§12.1).

This module turns a joint solution plus an I4 identifiability report into the
published :class:`~wattmosaic_engine.contracts.DeviceEstimate` /
:class:`~wattmosaic_engine.contracts.DeviceGroupEstimate` objects, the
:class:`~wattmosaic_engine.contracts.ReconciliationV3` balance (the
published snapshot is assembled by ``core.snapshot``).  It applies design §7's
HB §8.5 status table exactly:

=========================================================  ==========  =========
condition                                                  status      method
=========================================================  ==========  =========
confirmed single-subject endpoint with a fresh reading      MEASURED    DIRECT_READING
IDENTIFIABLE from the joint solve (delta or absolute)       ESTIMATED   JOINT_*
member of a GROUP_IDENTIFIABLE group                        UNKNOWN     NONE
explicit user allocation rule (none in 3.0, DEC-20)         ALLOCATED   DECLARED
otherwise                                                  UNKNOWN     NONE
=========================================================  ==========  =========

Key correctness rules (task I5 / DEC-05 / DEC-19 / DEC-20 / DEC-24):

* a model-only estimate is ESTIMATED with ``MODEL_PRIOR`` **only** for an
  IDENTIFIABLE device; otherwise it is UNKNOWN;
* ``allocation_rule`` stays ``None`` unless an explicit allocation exists
  (none in 3.0); proportional prior sharing (``engine.py:2393-2401``) is gone;
* the reconciliation identity is
  ``main = Σ identified (each subject once) + Σ group sums + Σ unknown +
  residual``, every term nullable (HB §18);
* provenance is **per estimate**: ``source_reader_ids`` are exactly the rows
  with a non-zero weight on the subject's column, ``evidence_refs`` are the
  reader evidence and the HA events that entered the solve, and versions are
  the operator's triple.  A batch-wide ledger is never copied.

The module is pure: no database and no wall clock (the clock/inputs are
injected).  Production code imports only contracts and runtime *Protocols* from
``runtime.interfaces``; it never imports ``runtime.fakes``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from wattmosaic_engine.contracts import (
    Bounds,
    DeviceEstimate,
    DeviceGroupEstimate,
    EstimateStatus,
    EvidenceRef,
    IdentifiabilityStatus,
    InferenceMethod,
    PowerLevelV3,
    ReconciliationV3,
    UnknownEstimate,
    VersionTriple,
)
from wattmosaic_engine.runtime.interfaces import EntryMeta, ObservationOperator

from .confidence_v3 import ConfidenceInputs, compute_confidence
from .identifiability import IdentifiabilityReport

__all__: list[str] = [
    "DirectReading",
    "EstimateInputs",
    "assemble_status_estimates",
    "contributing_readers",
    "level_from_model",
    "reconciliation_from_estimates",
    "status_for",
]

_NONZERO_TOL = 1e-9


# --------------------------------------------------------------------------- #
# Helpers.
# --------------------------------------------------------------------------- #


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _name(value: Any) -> str:
    if hasattr(value, "value"):
        return str(value.value)
    return str(value) if value is not None else ""


def _row_sigma(operator: ObservationOperator, reader_id: str) -> float | None:
    """The declared noise sigma of ``reader_id``'s operator row, if any."""
    for row in operator.rows:
        if row.reader_id == reader_id:
            return _finite(row.sigma_w)
    return None


# --------------------------------------------------------------------------- #
# Status table (design §7 / HB §8.5).
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class DirectReading:
    """A confirmed single-subject endpoint's fresh reading for one subject."""

    subject_id: str
    reader_id: str
    power_w: float
    observed_at: datetime
    quality: str | None = None


def status_for(
    *,
    identifiability: IdentifiabilityStatus | str,
    direct: DirectReading | None = None,
    solution_w: float | None = None,
    model_w: float | None = None,
    method: InferenceMethod | str | None = None,
    allocation_rule: str | None = None,
) -> tuple[EstimateStatus, InferenceMethod, float | None]:
    """Apply the HB §8.5 status table to one subject.

    Returns ``(status, method, point_w)``.  ``allocation_rule`` is deliberately
    an input: an ALLOCATED result is only produced when a caller supplies an
    explicit rule, which nothing in 3.0 does (DEC-20).
    """
    ident = IdentifiabilityStatus(identifiability)
    if direct is not None:
        return EstimateStatus.MEASURED, InferenceMethod.DIRECT_READING, float(direct.power_w)
    if allocation_rule is not None:
        # Explicit user allocation only; never derived from a model prior.
        return EstimateStatus.ALLOCATED, InferenceMethod.DECLARED, _finite(solution_w)
    point = _finite(solution_w)
    request = InferenceMethod(method) if method is not None else None
    if point is not None and ident is IdentifiabilityStatus.IDENTIFIABLE:
        eff_method = request if request in (
            InferenceMethod.JOINT_DELTA,
            InferenceMethod.JOINT_ABSOLUTE,
        ) else InferenceMethod.JOINT_DELTA
        return EstimateStatus.ESTIMATED, eff_method, point
    # A model-only estimate is allowed *only* for an identifiable device
    # (DEC-19).  Priors are never bounds or floors.
    prior = _finite(model_w)
    if prior is not None and ident is IdentifiabilityStatus.IDENTIFIABLE:
        return EstimateStatus.ESTIMATED, InferenceMethod.MODEL_PRIOR, prior
    return EstimateStatus.UNKNOWN, InferenceMethod.NONE, None


# --------------------------------------------------------------------------- #
# Provenance.
# --------------------------------------------------------------------------- #


def contributing_readers(
    operator: ObservationOperator, subject_id: str
) -> tuple[str, ...]:
    """Rows with a non-zero coefficient on ``subject_id``'s column.

    One reader per row, in operator order, de-duplicated.  A zero-weight row is
    absent — provenance lists only inputs that changed the estimate.
    """
    columns = {
        subject: index for index, (_kind, subject) in enumerate(operator.subjects)
    }
    column = columns.get(subject_id)
    if column is None:
        return ()
    matrix = operator.H
    readers: list[str] = []
    for index, row in enumerate(operator.rows):
        if index >= matrix.shape[0]:
            break
        if (
            abs(float(matrix[index, column])) > _NONZERO_TOL
            and row.reader_id not in readers
        ):
            readers.append(row.reader_id)
    return tuple(readers)


def _path_edge_lifecycles(
    operator: ObservationOperator, subject_id: str
) -> tuple[str, ...]:
    columns = {
        subject: index for index, (_kind, subject) in enumerate(operator.subjects)
    }
    column = columns.get(subject_id)
    if column is None:
        return ()
    lifecycles: list[str] = []
    for (_row_index, subj_index), entry in operator.entries.items():
        if subj_index != column or not isinstance(entry, EntryMeta):
            continue
        if entry.edge_lifecycle is not None:
            life = _name(entry.edge_lifecycle)
            if life not in lifecycles:
                lifecycles.append(life)
    return tuple(lifecycles)


def _evidence_refs(
    *,
    readers: Sequence[str],
    reader_evidence: Mapping[str, Sequence[str]] | None = None,
    events: Sequence[Any] = (),
) -> list[EvidenceRef]:
    """Reader evidence and HA events that entered the solve (per estimate)."""
    refs: list[EvidenceRef] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, identifier: str) -> None:
        key = (kind, identifier)
        if key in seen:
            return
        seen.add(key)
        refs.append(EvidenceRef(kind=kind, id=identifier))  # type: ignore[arg-type]

    for reader in readers:
        add("observation", reader)
    for _reader, identifiers in (reader_evidence or {}).items():
        for identifier in identifiers:
            add("event_evidence", str(identifier))
    for event in events:
        if isinstance(event, EvidenceRef):
            add(event.kind, event.id)
            continue
        event_id = _name(event)
        if not event_id:
            continue
        ctx = event.get("context") if isinstance(event, dict) else None
        if ctx and isinstance(ctx, dict) and ctx.get("id"):
            add("ha_event", str(ctx["id"]))
        elif event.get("event_id") if isinstance(event, dict) else None:
            add("ha_event", str(event["event_id"]))
        else:
            add("ha_event", event_id)
    return refs


# --------------------------------------------------------------------------- #
# Estimate assembly from a report + solution.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class EstimateInputs:
    """Injected inputs to :func:`assemble_status_estimates`."""

    report: IdentifiabilityReport
    operator: ObservationOperator
    versions: VersionTriple
    at: datetime
    points: Mapping[str, float | None] = field(default_factory=dict)
    models: Mapping[str, float | None] = field(default_factory=dict)
    method: InferenceMethod | str = InferenceMethod.JOINT_DELTA
    bounds: Mapping[str, Bounds] = field(default_factory=dict)
    direct: Mapping[str, DirectReading] = field(default_factory=dict)
    #: Per-subject reader quality scores (weakest link -> measurement_quality).
    reader_quality: Mapping[str, Mapping[str, float | None]] = field(default_factory=dict)
    #: Per-subject match states / counts (I6; a fake before then).
    match_states: Mapping[str, Sequence[Any]] = field(default_factory=dict)
    match_counts: Mapping[str, int] = field(default_factory=dict)
    #: Independent-event counts for sample_coverage (HB §10.3).
    coverage_counts: Mapping[str, int] = field(default_factory=dict)
    #: Age of each subject's newest reading, seconds.
    ages_s: Mapping[str, float] = field(default_factory=dict)
    #: Reader evidence identifiers that entered the solve.
    reader_evidence: Mapping[str, Sequence[str]] = field(default_factory=dict)
    #: HA events / EvidenceRefs that entered the solve.
    events: Sequence[Any] = ()
    #: Explicit allocation rules (none in 3.0, DEC-20).
    allocations: Mapping[str, str] = field(default_factory=dict)
    #: Per-subject power-model levels, ``{subject: {level: (mean_w, sigma_w)}}``,
    #: from the subject's active model (see ``orchestrator._model_levels``).  A
    #: subject without a model has no entry and its published level is None.
    model_levels: Mapping[str, Mapping[PowerLevelV3, tuple[float, float | None]]] = field(
        default_factory=dict
    )


#: A published value is at a model level when it lies within this many
#: standard deviations of the level's mean -- the same 3σ consistency gate the
#: steady pass uses for reader conflicts.
_LEVEL_MATCH_SIGMAS = 3.0


def level_from_model(
    point_w: float | None,
    levels: Mapping[PowerLevelV3, tuple[float, float | None]] | None,
    *,
    reading_sigma_w: float | None = None,
) -> PowerLevelV3 | None:
    """The subject's model level that ``point_w`` sits at, or ``None``.

    The level comes only from the subject's own power model: each candidate
    level is its model mean with the model's spread (combined in quadrature
    with the reading's sigma when known).  The nearest level within
    ``_LEVEL_MATCH_SIGMAS`` wins; a level without a known spread can match
    only exactly.  No model, no point, or no consistent level -> ``None``
    (HB §18); never a scenario constant.
    """
    point = _finite(point_w)
    if point is None or not levels:
        return None
    reading_var = (_finite(reading_sigma_w) or 0.0) ** 2
    best: tuple[float, str, PowerLevelV3] | None = None
    for level, (mean_w, sigma_w) in levels.items():
        mean = _finite(mean_w)
        if mean is None:
            continue
        spread = math.sqrt((_finite(sigma_w) or 0.0) ** 2 + reading_var)
        gap = abs(point - mean)
        if spread > 0.0:
            score = gap / spread
            if score > _LEVEL_MATCH_SIGMAS:
                continue
        elif gap != 0.0:
            continue
        else:
            score = 0.0
        candidate = (score, _name(level), PowerLevelV3(level))
        if best is None or candidate[:2] < best[:2]:
            best = candidate
    return best[2] if best is not None else None


def assemble_status_estimates(
    inputs: EstimateInputs,
) -> tuple[list[DeviceEstimate], list[DeviceGroupEstimate]]:
    """Build per-subject estimates applying the HB §8.5 status table.

    Bounds for identified values come from I4's justified-bound computation via
    ``assemble_estimates`` when no explicit bounds are supplied; a point bound
    is only accepted for an IDENTIFIABLE subject.  Group members are always
    UNKNOWN with ``point_w=None``.
    """
    report = inputs.report
    operator = inputs.operator
    columns = {
        subject: index for index, (_kind, subject) in enumerate(operator.subjects)
    }
    # Reuse I4's assembly for justified bounds only; its status/method choices
    # are overridden here by the full status table (MEASURED / ALLOCATED / model).
    from .identifiability import assemble_estimates as i4_assemble

    base = i4_assemble(
        report,
        operator,
        versions=inputs.versions,
        at=inputs.at,
        points=inputs.points,
        models=None,
    )
    base_bounds = {e.subject_id: e.bounds for e in base.device_estimates}

    def _reusable(bounds: Bounds) -> Bounds:
        """A point bound needs `allow_point`; re-create via the private ctor.

        I4's assembly calls ``Bounds._point`` which marks the instance as a
        legitimate point; the contract validator runs on construction only, so
        re-validating the same object through Pydantic fails.  Rebuild here.
        """
        low, high, basis = bounds.lower_w, bounds.upper_w, bounds.basis
        if low is not None and high is not None and low == high and basis is not None:
            return Bounds._point(low, basis)  # type: ignore[arg-type]
        return bounds

    devices: list[DeviceEstimate] = []
    for subject_id in report.subject_ids:
        ident = report.subjects[subject_id]
        column = columns.get(subject_id, 0)
        kind = operator.subjects[column][0] if column < len(operator.subjects) else "device"
        direct = inputs.direct.get(subject_id)
        status, method, point = status_for(
            identifiability=ident,
            direct=direct,
            solution_w=inputs.points.get(subject_id),
            model_w=inputs.models.get(subject_id),
            method=inputs.method,
            allocation_rule=inputs.allocations.get(subject_id),
        )
        readers = contributing_readers(operator, subject_id) if direct is None else (
            (direct.reader_id,)
        )
        refs = _evidence_refs(
            readers=readers,
            reader_evidence=inputs.reader_evidence,
            events=inputs.events,
        )
        share = report.null_space_share.get(subject_id)
        conf_inputs = ConfidenceInputs(
            reader_quality=inputs.reader_quality.get(subject_id, {}),
            match_states=inputs.match_states.get(subject_id, ()),
            match_history_count=inputs.match_counts.get(subject_id),
            edge_lifecycles=_path_edge_lifecycles(operator, subject_id),
            null_space_share=share,
            independent_event_count=inputs.coverage_counts.get(subject_id),
            age_s=inputs.ages_s.get(subject_id),
            overrides={"identifiability": 1.0 - share if share is not None else None},
        )
        computed_bounds = base_bounds.get(subject_id)
        bounds = inputs.bounds.get(
            subject_id,
            _reusable(computed_bounds) if computed_bounds is not None else Bounds(),
        )
        is_point = (
            bounds.lower_w is not None
            and bounds.upper_w is not None
            and bounds.lower_w == bounds.upper_w
        )
        if (
            direct is not None
            and point is not None
            and str(getattr(status, "value", status)) == EstimateStatus.MEASURED.value
            and not (is_point and bounds.lower_w == point)
        ):
            # A MEASURED estimate *is* its direct reading (HB §8.5): its interval
            # is that reading, whatever the absolute solve's posterior said.  A
            # steady solve can return a [0, 0] posterior for a plug reading 3 W;
            # publishing that interval would not bracket the published point.
            bounds = Bounds._point(float(point), bounds.basis or "posterior")  # type: ignore[arg-type]
            is_point = True
        if (
            is_point
            and point is not None
            and bounds.basis is not None
            and bounds.lower_w != point
            and abs(float(bounds.lower_w) - float(point)) <= 1e-6 * max(1.0, abs(float(point)))
        ):
            # The point bound came from I4's posterior solve and the point from
            # the direct reading; they differ only by float round-off.  Pin the
            # bound to the published point so the interval brackets it exactly
            # (M2's MappingCell rejects a point outside its own bounds).
            bounds = Bounds._point(float(point), bounds.basis)  # type: ignore[arg-type]
        group = report.group_of(subject_id)
        member_group_id = (
            group.group_id
            if group is not None and group.identifiability is IdentifiabilityStatus.GROUP_IDENTIFIABLE
            else None
        )
        estimate = DeviceEstimate(
            subject_kind="component" if kind == "component" else "device",  # type: ignore[arg-type]
            subject_id=subject_id,
            point_w=point,
            # A point interval cannot pass nested Pydantic validation (the
            # `allow_point` gate is only open in the private constructor), so
            # attach the real bounds through that constructor below.
            bounds=Bounds(basis=bounds.basis) if is_point else bounds,
            status=status,
            identifiability=ident,
            group_id=member_group_id,
            level=(
                level_from_model(
                    point,
                    inputs.model_levels.get(subject_id),
                    reading_sigma_w=_row_sigma(operator, direct.reader_id) if direct else None,
                )
                if status in (EstimateStatus.MEASURED, EstimateStatus.ESTIMATED)
                else None
            ),
            method=method,
            confidence=compute_confidence(conf_inputs),
            source_reader_ids=list(readers),
            evidence_refs=refs,
            versions=inputs.versions,
            updated_at=inputs.at,
        )
        if is_point:
            estimate.bounds = bounds
        devices.append(estimate)

    groups: list[DeviceGroupEstimate] = []
    for group in report.groups:
        if group.sum_w is None or group.identifiability is not IdentifiabilityStatus.GROUP_IDENTIFIABLE:
            continue
        groups.append(
            DeviceGroupEstimate(
                group_id=group.group_id,
                member_ids=list(group.member_ids),
                point_w=group.sum_w,
                bounds=(
                    Bounds(basis="group_sum")
                    if group.sigma_w is None
                    else Bounds(
                        lower_w=group.sum_w - 3.0 * group.sigma_w,
                        upper_w=group.sum_w + 3.0 * group.sigma_w,
                        basis="group_sum",
                    )
                ),
                identifiability=group.identifiability,
                source_reader_ids=list(group.source_reader_ids),
                evidence_refs=list(group.evidence_refs),
                versions=inputs.versions,
                updated_at=inputs.at,
            )
        )
    return devices, groups


# --------------------------------------------------------------------------- #
# Reconciliation (design §7 / HB §8.5).
# --------------------------------------------------------------------------- #


def reconciliation_from_estimates(
    *,
    devices: Sequence[DeviceEstimate],
    groups: Sequence[DeviceGroupEstimate] = (),
    unknowns: Sequence[UnknownEstimate] = (),
    residual_by_reader: Mapping[str, float | None] | None = None,
    main_w: float | None = None,
    root_reader_ids: Sequence[str] = (),
) -> ReconciliationV3:
    """``main = identified + group + unknown + residual``, every term nullable.

    ``identified`` sums each ESTIMATED/MEASURED subject **once** (AT-14), even
    when several nested readers observe it.  A term with no contributing value
    is ``None``, never ``0.0`` (HB §18).  ``residual`` is the residual of the
    active topology's ``root_reader_ids`` (``topology_root_readers``); with no
    root it is ``None``.
    """
    identified_parts: list[float] = []
    seen: set[tuple[str, str]] = set()
    for estimate in devices:
        key = (estimate.subject_kind, estimate.subject_id)
        if key in seen or estimate.point_w is None:
            continue
        seen.add(key)
        if estimate.status in (EstimateStatus.ESTIMATED, EstimateStatus.MEASURED):
            identified_parts.append(float(estimate.point_w))

    group_parts = [
        float(group.point_w)
        for group in groups
        if group.point_w is not None
    ]
    unknown_parts = [
        float(value)
        for estimate in unknowns
        if (value := _finite(estimate.delta_w if estimate.delta_w is not None else estimate.point_w))
        is not None
    ]
    # The root residual: summed over the topology's root reader(s); no root,
    # or a root without a residual, is None -- never another reader's value.
    residual_w: float | None = None
    roots = tuple(str(r) for r in root_reader_ids)
    if residual_by_reader and roots:
        parts: list[float | None] = []
        for root in roots:
            raw = residual_by_reader.get(root)
            value = _finite(raw)
            if value is None and raw is not None and not isinstance(raw, (int, float)):
                value = _finite(getattr(raw, "residual_w", None))
            parts.append(value)
        if all(part is not None for part in parts):
            residual_w = sum(part for part in parts if part is not None)

    def total(parts: list[float]) -> float | None:
        return sum(parts) if parts else None

    return ReconciliationV3(
        main_w=main_w,
        identified_w=total(identified_parts),
        group_w=total(group_parts),
        unknown_w=total(unknown_parts),
        residual_w=residual_w,
    )
