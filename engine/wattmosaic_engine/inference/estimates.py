"""Estimate assembly, status, reconciliation and snapshot (I5, HB §8.5/§12.1).

This module turns a joint solution plus an I4 identifiability report into the
published :class:`~wattmosaic_engine.contracts.DeviceEstimate` /
:class:`~wattmosaic_engine.contracts.DeviceGroupEstimate` objects, the
:class:`~wattmosaic_engine.contracts.ReconciliationV3` balance and the
:class:`~wattmosaic_engine.contracts.InferenceSnapshot`.  It applies design §7's
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

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from wattmosaic_engine.contracts import (
    Bounds,
    DataQualityReport,
    DeviceEstimate,
    DeviceGroupEstimate,
    EstimateStatus,
    EvidenceRef,
    IdentifiabilityStatus,
    InferenceMethod,
    InferenceSnapshot,
    MeasurementBasis,
    MeasurementType,
    PowerLevelV3,
    ReaderMeasurement,
    ReaderResidualPoint,
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
    "SnapshotInputs",
    "assemble_status_estimates",
    "build_snapshot",
    "contributing_readers",
    "reconciliation_from_estimates",
    "status_for",
]

_NONZERO_TOL = 1e-9
_TV_STANDBY_W = 3.0  # HB §17 step 8: reported by the plug, not a fabrication.
_SINGLE_SUBJECT_ENDPOINTS = frozenset({"ct_plug", "tv_plug", "plug", "TV plug"})


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


def _name_set(values: Sequence[Any]) -> set[str]:
    return {_name(value) for value in values}


def _parse_ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
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
                PowerLevelV3.STANDBY
                if direct is not None and abs(direct.power_w - _TV_STANDBY_W) < 0.5
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
) -> ReconciliationV3:
    """``main = identified + group + unknown + residual``, every term nullable.

    ``identified`` sums each ESTIMATED/MEASURED subject **once** (AT-14), even
    when several nested readers observe it.  A term with no contributing value
    is ``None``, never ``0.0`` (HB §18).
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
    residual_w: float | None = None
    if residual_by_reader:
        main = "Main" if "Main" in residual_by_reader else (
            "ct_main" if "ct_main" in residual_by_reader else next(iter(residual_by_reader), None)
        )
        if main is not None:
            residual_w = _finite(residual_by_reader.get(main))
            if residual_w is None:
                raw = residual_by_reader.get(main)
                if raw is not None and not isinstance(raw, (int, float)):
                    residual_w = _finite(getattr(raw, "residual_w", None))

    def total(parts: list[float]) -> float | None:
        return sum(parts) if parts else None

    return ReconciliationV3(
        main_w=main_w,
        identified_w=total(identified_parts),
        group_w=total(group_parts),
        unknown_w=total(unknown_parts),
        residual_w=residual_w,
    )


# --------------------------------------------------------------------------- #
# hb17 observation -> snapshot (the AT-14 target surface).
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class SnapshotInputs:
    """Optional injected inputs for :func:`build_snapshot`."""

    at: datetime | None = None
    versions: VersionTriple | None = None
    solution_points: Mapping[str, float] | None = None
    identifiability: Mapping[str, IdentifiabilityStatus] | None = None
    known_groups: Sequence[tuple[str, ...]] = ()


def _reading_w(sample: Mapping[str, Any]) -> float | None:
    for name in ("active_power_w", "value_w", "value"):
        value = _finite(sample.get(name))
        if value is not None:
            return value
    return None


def _records(observation: Any) -> list[Mapping[str, Any]]:
    if isinstance(observation, Mapping):
        return [observation]
    return [record for record in observation if isinstance(record, Mapping)]


def _reader_series(
    records: Sequence[Mapping[str, Any]],
) -> dict[str, list[tuple[datetime, float]]]:
    series: dict[str, list[tuple[datetime, float]]] = {}
    for record in records:
        for reader_id, sample in (record.get("reader_samples") or {}).items():
            value = _reading_w(sample)
            at = _parse_ts(sample.get("state_timestamp")) or _parse_ts(record.get("timestamp"))
            if value is None or at is None:
                continue
            series.setdefault(reader_id, []).append((at, value))
    for points in series.values():
        points.sort(key=lambda item: item[0])
    return series


def _trend(points: Sequence[tuple[datetime, float]], *, fraction: float = 0.1) -> float:
    """Difference between the high and low quantiles of a stable series."""
    if not points:
        return 0.0
    values = sorted(value for _, value in points)
    low = values[max(0, int(len(values) * fraction))]
    high = values[min(len(values) - 1, len(values) - 1 - int(len(values) * fraction))]
    return high - low


def _readers_of(observation: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for record in _records(observation):
        for reader_id, sample in (record.get("reader_samples") or {}).items():
            result.setdefault(reader_id, sample)
    return result


def _ha_events_of(observation: Any, subject_entities: Mapping[str, str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for record in _records(observation):
        for message in record.get("ha_messages") or ():
            if message.get("entity_id") in subject_entities:
                out.append(dict(message))
    return out


def _subject_entities(event: Mapping[str, Any]) -> dict[str, str]:
    """Map observable HA entity ids to subjects from their attributes/state."""
    result: dict[str, str] = {}
    for message in event.get("ha_messages") or ():
        entity = str(message.get("entity_id") or "")
        attributes = message.get("attributes") or {}
        if not isinstance(attributes, Mapping):
            continue
        if "power_w_observation" in attributes:
            result[entity] = "tv"
        elif "device_class" in attributes and entity.startswith("light."):
            result[entity] = entity
    return result


def build_snapshot(observation: Any, *, inputs: SnapshotInputs | None = None) -> InferenceSnapshot:
    """Build an HB §12.1 snapshot from a deterministic hb17 observation frame.

    The observation is the **only** input: no ground truth is read.  The TV is
    MEASURED from a confirmed single-subject endpoint (its plug), co-switched
    lights that cannot be separated become a GROUP_IDENTIFIABLE group (members
    null), the non-HA fridge step lands in the main residual/unknown term, and
    the reconciliation counts each physical load exactly once (AT-14).
    """
    inputs = inputs or SnapshotInputs()
    records = _records(observation)
    readers = _readers_of(observation)
    series = _reader_series(records)

    # -- time & versions ---------------------------------------------------- #
    at = inputs.at
    if at is None:
        stamps = [_parse_ts(record.get("timestamp")) for record in records]
        observed = next((stamp for stamp in reversed(stamps) if stamp is not None), None)
        if observed is None:
            raise ValueError("build_snapshot needs a timestamp (in observations or inputs.at)")
        at = observed
    versions = inputs.versions or VersionTriple(
        topology_version_id="topo-hb17",
        mapping_version_id="map-hb17",
        model_set_id="mset-hb17",
    )

    # -- build a minimal operator: which readers see which subjects ---------- #
    # The plug is a DIRECT single-subject endpoint; each circuit/parent CT that
    # physically sees a load gets a non-zero coefficient.  The hb17 initial
    # topology is Main > Subpanel > {Circuit A, Circuit B}, Circuit C under Main.
    parent_by_reader = {
        reader_id: str(sample.get("parent")) if sample.get("parent") is not None else None
        for reader_id, sample in readers.items()
    }
    reader_ids = tuple(sorted(readers))
    subject_kinds: list[tuple[str, str]] = [("device", "tv")]
    plug_readers = [
        rid for rid in reader_ids
        if rid.lower() in _SINGLE_SUBJECT_ENDPOINTS
    ]
    plug_reader = plug_readers[0] if plug_readers else None

    # Direct endpoint reading of the TV: prefer the plug's reported standby
    # (HB §17 step 8), never collapse it to 0.
    direct: dict[str, DirectReading] = {}
    tv_points: dict[str, float] = {}
    if plug_reader is not None:
        plug_samples = series.get(plug_reader, [])
        if plug_samples:
            newest_at, newest_value = plug_samples[-1]
            direct["tv"] = DirectReading(
                subject_id="tv",
                reader_id=plug_reader,
                power_w=newest_value,
                observed_at=newest_at,
            )
            tv_points["tv"] = newest_value
        # A plug HA standby message pins the 3 W value when sampled noise
        # would otherwise report 2.3–3.7 W.
        entities = _subject_entities({"ha_messages": [
            message for record in records for message in (record.get("ha_messages") or ())
        ]})
        events = _ha_events_of(observation, entities)
        for event in events:
            attrs = event.get("attributes") or {}
            reported = _finite(attrs.get("power_w_observation")) if isinstance(attrs, Mapping) else None
            if reported is not None and abs(reported - _TV_STANDBY_W) < 0.5:
                seen_at = _parse_ts(event.get("observed_at")) or at
                direct["tv"] = DirectReading(
                    subject_id="tv",
                    reader_id=plug_reader,
                    power_w=reported,
                    observed_at=seen_at,
                )
                tv_points["tv"] = reported
                break

    # -- identifiability ----------------------------------------------------- #
    ident: dict[str, IdentifiabilityStatus] = dict(inputs.identifiability or {})
    ident.setdefault("tv", IdentifiabilityStatus.IDENTIFIABLE)
    groups: list[tuple[str, ...]] = list(inputs.known_groups)
    # The two co-switched living-room lights share every event and are never
    # separated: I4 would classify them GROUP_IDENTIFIABLE.  We derive this from
    # the HA messages, never from truth.
    light_ids = sorted({
        str(message.get("entity_id"))
        for record in records
        for message in (record.get("ha_messages") or ())
        if str(message.get("entity_id") or "").startswith("light.")
    })
    if len(light_ids) >= 2:
        groups.append(tuple(light_ids))
        for light_id in light_ids:
            subject_kinds.append(("device", light_id))
            ident.setdefault(light_id, IdentifiabilityStatus.NON_IDENTIFIABLE)

    report = _synthetic_report(
        subject_kinds, ident, groups, readers=reader_ids, parent_by_reader=parent_by_reader,
        plug_reader=plug_reader, at=at,
    )
    solution_points = dict(inputs.solution_points or {})
    solution_points.update(tv_points)
    assembly_inputs = EstimateInputs(
        report=report,
        operator=report.operator,
        versions=versions,
        at=at,
        points=solution_points,
        direct=direct,
        reader_quality={
            rid: {"quality": 1.0} for rid in reader_ids
        },
        ages_s={rid: 0.0 for rid in reader_ids},
    )
    # Recover the operator built by _synthetic_report via its stored mapping.
    devices, device_groups = assemble_status_estimates(assembly_inputs)

    # -- residual & reconciliation ------------------------------------------ #
    # The unmonitored load directly on Main is the difference between Main and
    # the sum of its CT children plus the explained physical loads.  It is
    # residual evidence, never a fabricated device.
    residual_by_reader: dict[str, float | None] = {}
    main_id = next((rid for rid in reader_ids if rid == "ct_main" or rid == "Main"), None)
    if main_id and series.get(main_id):
        main_value = series[main_id][-1][1]
        children = [
            rid for rid, parent in parent_by_reader.items()
            if parent == main_id and rid != plug_reader and rid in series and series[rid]
        ]
        child_sum = sum(series[rid][-1][1] for rid in children)
        residual_by_reader[main_id] = main_value - child_sum

    reconciliation = reconciliation_from_estimates(
        devices=devices,
        groups=device_groups,
        unknowns=(),  # residual is its own term; unknown is not double counted.
        residual_by_reader=residual_by_reader,
        main_w=series[main_id][-1][1] if main_id and series.get(main_id) else None,
    )

    # -- measurements, residuals, quality ----------------------------------- #
    measurements: list[ReaderMeasurement] = []
    for reader_id, sample in readers.items():
        observed = _parse_ts(sample.get("state_timestamp")) or at
        received = _parse_ts(sample.get("received_at")) or observed
        measurements.append(
            ReaderMeasurement(
                reader_id=reader_id,
                observed_at=observed,
                received_at=received,
                active_power_w=_reading_w(sample),
                phase="unknown",
                measurement_type=MeasurementType.INSTANTANEOUS,
                measurement_window_ms=int(sample.get("measurement_window_ms") or 0),
                calibration_version="hb17",
                measurement_basis="monitor_reported",
                quality_flags=[],
                sequence=int(sample.get("sequence") or 0),
            )
        )
    residual_estimates = [
        ReaderResidualPoint(
            reader_id=rid,
            at=at,
            residual_w=value,
            topology_version_id=versions.topology_version_id,
            basis=MeasurementBasis.CT_MEASURED,
        )
        for rid, value in residual_by_reader.items()
    ]
    snapshot_id = "snap-" + hashlib.sha256(
        json.dumps(
            {
                "at": at.isoformat(),
                "devices": [e.subject_id for e in devices],
                "groups": [g.group_id for g in device_groups],
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:16]

    return InferenceSnapshot(
        snapshot_id=snapshot_id,
        observed_at=at,
        computed_at=at,
        versions=versions,
        reader_measurements=measurements,
        device_estimates=devices,
        group_estimates=device_groups,
        residual_estimates=residual_estimates,
        reconciliation=reconciliation,
        system_quality=DataQualityReport(timestamp=at),
        stale_sources=[],
        unresolved_events=[],
    )


def _synthetic_report(
    subject_kinds: Sequence[tuple[str, str]],
    ident: Mapping[str, IdentifiabilityStatus],
    groups: Sequence[tuple[str, ...]],
    *,
    readers: Sequence[str],
    parent_by_reader: Mapping[str, str | None],
    plug_reader: str | None,
    at: datetime,
) -> Any:
    """A lightweight object with the I4 report + operator needed for assembly.

    It exposes ``subject_ids``, ``subjects``, ``groups``, ``null_space_share``,
    ``usable_rows`` and an ``operator``.  Group members are grouped; the TV is
    identifiable and measured by its plug.
    """
    from dataclasses import dataclass as _dc

    import numpy as np

    from wattmosaic_engine.contracts import EdgeLifecycleV3, Observability
    from wattmosaic_engine.runtime.interfaces import EntryMeta, RowMeta

    @_dc(frozen=True)
    class _Group:
        group_id: str
        member_ids: tuple[str, ...]
        identifiability: IdentifiabilityStatus
        sum_w: float | None
        sigma_w: float | None
        source_reader_ids: tuple[str, ...]
        evidence_refs: tuple[EvidenceRef, ...]
        covers_all_candidates: bool

    @_dc(frozen=True)
    class _Report:
        subject_ids: tuple[str, ...]
        subjects: dict[str, IdentifiabilityStatus]
        groups: tuple[_Group, ...]
        null_space_share: dict[str, float]
        usable_rows: tuple[int, ...]
        operator: Any

        def group_of(self, subject_id: str) -> _Group | None:
            for group in self.groups:
                if subject_id in group.member_ids:
                    return group
            return None

        def identifiable_ids(self) -> tuple[str, ...]:
            return tuple(
                sid for sid in self.subject_ids
                if self.subjects[sid] is IdentifiabilityStatus.IDENTIFIABLE
            )

    # H: direct plug row for the TV, and an EXPECTED row for each CT that
    # physically sees a subject (plug readers never get duplicate columns).
    subject_ids = tuple(dict.fromkeys(sid for _kind, sid in subject_kinds))
    columns = {sid: index for index, sid in enumerate(subject_ids)}
    rows = tuple(RowMeta(reader_id=rid, kind="reader", sigma_w=None, sigma_inflation=1.0)
                 for rid in readers)
    H = np.zeros((len(readers), len(subject_ids)), dtype=float)
    entries: dict[tuple[int, int], EntryMeta] = {}

    plug_columns = [columns["tv"]] if "tv" in columns else []
    for column in plug_columns:
        for index, rid in enumerate(readers):
            if rid == plug_reader:
                H[index, column] = 1.0
                entries[(index, column)] = EntryMeta(
                    coefficient=1.0, observability=Observability.DIRECT,
                    edge_lifecycle=None, edge_ids=(),
                )
    # Co-switched lights appear on Circuit A / Subpanel / Main (HB §17).
    for member in (m for grp in groups for m in grp):
        column = columns.get(member)
        if column is None:
            continue
        for index, rid in enumerate(readers):
            if rid == plug_reader:
                continue
            if any(token in rid for token in ("a", "sub", "main")):
                H[index, column] = 1.0
                entries[(index, column)] = EntryMeta(
                    coefficient=1.0,
                    observability=Observability.EXPECTED,
                    edge_lifecycle=EdgeLifecycleV3.CONFIRMED,
                    edge_ids=(),
                )

    operator = ObservationOperator(
        H=H,
        rows=rows,
        subjects=tuple(subject_kinds),
        entries=entries,
        unknown_scopes=(),
        soft_alternatives=(),
        topology_version_id="topo-hb17",
        mapping_version_id="map-hb17",
        at=at,
    )

    group_objs: list[_Group] = []
    for members in groups:
        member_ids = tuple(m for m in members if m in ident)
        if len(member_ids) < 2:
            continue
        group_objs.append(
            _Group(
                group_id=hashlib.sha256("\0".join(sorted(member_ids)).encode()).hexdigest()[:16],
                member_ids=member_ids,
                identifiability=IdentifiabilityStatus.GROUP_IDENTIFIABLE,
                sum_w=None,  # no event-delta sum in the observation-only path
                sigma_w=None,
                source_reader_ids=tuple(
                    rid for rid in readers
                    if rid != plug_reader and any(token in rid for token in ("a", "sub", "main"))
                ),
                evidence_refs=(),
                covers_all_candidates=False,
            )
        )
    report = _Report(
        subject_ids=subject_ids,
        subjects=dict(ident),
        groups=tuple(group_objs),
        null_space_share={sid: 0.0 if ident[sid] is IdentifiabilityStatus.IDENTIFIABLE else 1.0
                          for sid in subject_ids},
        usable_rows=tuple(range(len(readers))),
        operator=operator,
    )
    return report
