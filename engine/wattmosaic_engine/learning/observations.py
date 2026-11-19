"""Device-attributed learning observations (L4, HB §3.2/§13.1).

This module replaces the collector's whole-circuit absolute samples with
observations derived from **immutable** event-group evidence plus the joint
inference assignment:

* one observation per ``(subject, endpoint)`` pair that carries a delta;
* ``level_w`` (an ``ABSOLUTE_ANCHOR`` basis) is set **only** for a confirmed
  single-subject endpoint for that subject -- a circuit CT never gets a level,
  only its device-attributed ΔP (DEC-05, ADR-0026, analysis F/C1);
* identifiers are content-derived and deterministic, so replaying the same
  event twice produces the same id and no duplicate rows (F4, AT-19).

A non-identifiable member gets *no* per-device observation: it contributes to
the group only (DEC-19, AT-07 learning side).  :func:`build_group_observations`
produces that group-only evidence separately.

The module is pure: no database and no wall clock -- the clock is injected as
``clock: Callable[[], datetime]``.  Production code imports only contracts and
runtime Protocols, never ``runtime.fakes``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

from wattmosaic_engine.contracts import (
    DeviceEstimate,
    EstimateStatus,
    EvidenceRef,
    IdentifiabilityStatus,
    JointInferenceResult,
    LearningObservation,
    ModelBasis,
    VersionTriple,
    observation_id,
)
from wattmosaic_engine.contracts.measurement import EventGroup

__all__ = [
    "build_group_observations",
    "build_observations",
    "observation_for_estimate",
]

#: A device estimate is individually identifiable only for these states.  A
#: member of a group, a partially identifiable subject and an evidence-starved
#: subject never licences a per-device numeric update (DEC-19, HB §8.4).
_INDIVIDUALLY_IDENTIFIABLE: frozenset[IdentifiabilityStatus] = frozenset(
    {IdentifiabilityStatus.IDENTIFIABLE}
)


def _name(value: Any) -> str:
    """Contract enums are stored as their string value (``use_enum_values``)."""
    return str(getattr(value, "value", value)) if value is not None else ""


def _is_individually_identifiable(estimate: DeviceEstimate) -> bool:
    return _name(estimate.identifiability) in {s.value for s in _INDIVIDUALLY_IDENTIFIABLE}


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and out not in (float("inf"), float("-inf")) else None


def _transition(estimate: DeviceEstimate) -> str:
    """A human-readable transition label for the observation.

    The joint inference result does not carry a per-estimate transition string;
    the level (when known) is the best available description.  Kept stable so
    replay is deterministic.
    """
    level = getattr(estimate, "level", None)
    if level is not None:
        return _name(level)
    return "delta"


def _attributes(
    estimate: DeviceEstimate,
    result: JointInferenceResult,
    group: EventGroup | None,
    versions: VersionTriple,
) -> dict[str, Any]:
    return {
        "topology_version_id": str(versions.topology_version_id),
        "mapping_version_id": str(versions.mapping_version_id),
        "model_set_id": str(versions.model_set_id),
        "event_group_status": _name(result.status),
        "status_source": _name(estimate.status),
        "method": _name(estimate.method),
        "provisional": bool(getattr(estimate, "provisional", False)),
        "revision": int(group.revision) if group is not None else (
            int(result.event_group_revision) if result.event_group_revision is not None else 1
        ),
    }


def _anchor_subject(
    single_subject_endpoints: Mapping[str, str] | None,
) -> Mapping[str, str]:
    return single_subject_endpoints or {}


def _default_clock(
    result: JointInferenceResult, group: EventGroup | None
) -> Callable[[], datetime]:
    """A deterministic clock derived from immutable evidence (no wall clock).

    The task's literal signature is ``build_observations(result, group,
    versions)`` with no clock argument.  Deriving the default creation time from
    the group (or the inference result) keeps the builder pure and makes a replay
    byte-identical (AT-19), while still allowing a caller to inject a clock when
    it needs to stamp a specific instant.
    """
    instant: datetime | None = None
    if group is not None:
        instant = getattr(group, "opened_at", None)
    if instant is None:
        instant = getattr(result, "computed_at", None)
    if instant is None:
        from datetime import UTC

        instant = datetime(1970, 1, 1, tzinfo=UTC)
    fixed = instant

    def clock() -> datetime:
        return fixed

    return clock


def observation_for_estimate(
    result: JointInferenceResult,
    estimate: DeviceEstimate,
    endpoint_id: str,
    versions: VersionTriple,
    *,
    clock: Callable[[], datetime] | None = None,
    group: EventGroup | None = None,
    single_subject_endpoints: Mapping[str, str] | None = None,
) -> LearningObservation | None:
    """Build one observation for a (subject, endpoint) pair with a delta.

    ``None`` is returned when the subject has no usable point value or is not
    individually identifiable -- a non-identifiable member never gets a number.
    An absolute level is attached only for a confirmed single-subject endpoint
    for *this* subject (DEC-05); a shared circuit carries ΔP alone (HB §18).
    """
    delta = _finite(estimate.point_w)
    if delta is None or not _is_individually_identifiable(estimate):
        return None

    anchors = _anchor_subject(single_subject_endpoints)
    anchored = anchors.get(endpoint_id) == estimate.subject_id
    # A direct reading is MEASURED only from a single-subject endpoint; the
    # declaration must be confirmed (the caller supplies only confirmed anchors).
    is_anchor = anchored and _name(estimate.status) == EstimateStatus.MEASURED.value
    level = _finite(estimate.point_w) if is_anchor else None
    basis = ModelBasis.ABSOLUTE_ANCHOR if is_anchor else ModelBasis.DELTA

    group_id = result.event_group_id or (group.group_id if group is not None else "")
    revision = (
        group.revision
        if group is not None
        else (result.event_group_revision if result.event_group_revision is not None else 1)
    )
    provisional = bool(getattr(estimate, "provisional", False)) or bool(
        getattr(result, "status", None) and _name(result.status)
        in {"provisional", "detected", "matching"}
    )

    return LearningObservation(
        observation_id=observation_id(
            group_id, revision, estimate.subject_id, endpoint_id, versions.topology_version_id
        ),
        subject_kind="device",
        subject_id=estimate.subject_id,
        endpoint_id=endpoint_id,
        event_group_ref=EvidenceRef(kind="event_group", id=str(group_id)),
        transition=_transition(estimate),
        attributes=_attributes(estimate, result, group, versions),
        delta_w=delta,
        level_w=level,
        status="provisional" if provisional else "confirmed",
        rejection_reason=None,
        identifiability=estimate.identifiability,
        basis=basis,
        created_at=(clock or _default_clock(result, group))(),
    )


def build_observations(
    result: JointInferenceResult,
    group: EventGroup,
    versions: VersionTriple,
    *,
    clock: Callable[[], datetime] | None = None,
    single_subject_endpoints: Mapping[str, str] | None = None,
) -> list[LearningObservation]:
    """Build device-attributed observations from a joint inference result.

    One observation per ``(subject, endpoint)`` pair that carries a delta.
    Subjects that are not individually identifiable (group members included)
    are skipped entirely -- they feed the group, not a per-device model.
    """
    out: list[LearningObservation] = []
    seen: set[tuple[str, str, str]] = set()
    for estimate in result.device_estimates:
        endpoints: Sequence[str] = tuple(estimate.source_reader_ids)
        if not endpoints:
            endpoints = ("unknown",)
        for endpoint_id in endpoints:
            obs = observation_for_estimate(
                result,
                estimate,
                str(endpoint_id),
                versions,
                clock=clock,
                group=group,
                single_subject_endpoints=single_subject_endpoints,
            )
            if obs is None:
                continue
            key = (obs.subject_id, obs.endpoint_id, obs.observation_id)
            if key in seen:
                continue
            seen.add(key)
            out.append(obs)
    return out


def build_group_observations(
    result: JointInferenceResult,
    versions: VersionTriple,
    *,
    clock: Callable[[], datetime] | None = None,
) -> list[LearningObservation]:
    """Group-only evidence for a non-separable group (DEC-19 / AT-07).

    A group observation carries the group's identified sum as its delta and
    never a level.  It is marked ``group_only`` in ``attributes`` so a learner
    cannot mistake it for a per-device update.
    """
    out: list[LearningObservation] = []
    group_id = result.event_group_id or ""
    revision = result.event_group_revision if result.event_group_revision is not None else 1
    provisional = _name(result.status) in {"provisional", "detected", "matching"}
    for group in result.group_estimates:
        delta = _finite(group.point_w)
        if delta is None:
            continue
        endpoint_id = group.group_id
        out.append(
            LearningObservation(
                observation_id=observation_id(
                    group_id, revision, group.group_id, endpoint_id, versions.topology_version_id
                ),
                subject_kind="device",
                subject_id=group.group_id,
                endpoint_id=endpoint_id,
                event_group_ref=EvidenceRef(kind="event_group", id=str(group_id)),
                transition="group_sum",
                attributes={
                    "group_only": True,
                    "member_ids": list(group.member_ids),
                    "topology_version_id": str(versions.topology_version_id),
                    "revision": int(revision),
                },
                delta_w=delta,
                level_w=None,
                status="provisional" if provisional else "confirmed",
                rejection_reason=None,
                identifiability=group.identifiability,
                basis=ModelBasis.DELTA,
                created_at=(clock or _default_clock(result, None))(),
            )
        )
    return out
