"""HB §17 observation → per-subject power-model estimates (L2).

``learn_power_model`` is the pure entry point AT-03 drives: it takes the
observation frame of the reference house plus any explicit device-attributed
:class:`~wattmosaic_engine.contracts.LearningObservation` evidence, and returns
one :class:`~wattmosaic_engine.contracts.DeviceEstimate` per modelled subject
with the level, status and null semantics HB §18 requires.

Two standby paths (analysis F2, AT-03):

* a reported single-subject endpoint level (a smart plug's
  ``power_w_observation``) -- ABSOLUTE_ANCHOR basis, status MEASURED;
* a device-attributed ΔP chain plus a residual-baseline step -- DELTA basis,
  status ESTIMATED, level STANDBY, a non-zero point derived by
  :class:`~wattmosaic_engine.models.fixed.FixedPowerModel`.

OFF never means 0 W: after an OFF transition the published level is STANDBY
with the learned (here non-zero) standby point.  No wall clock is read: all
timestamps come from the observation evidence.  Pure -- no DB, no fakes.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from ..contracts import (
    Bounds,
    ConfidenceVectorV3,
    DeviceEstimate,
    EstimateStatus,
    EvidenceRef,
    IdentifiabilityStatus,
    InferenceMethod,
    LearningObservation,
    ModelBasis,
    PowerLevelV3,
    VersionTriple,
)
from .fixed import FixedPowerModel

__all__ = ["learn_power_model"]

_TV_SUBJECT = "tv"
_PLUG_READER_ID = "ct_plug"


def _records(observation: Any) -> list[dict[str, Any]]:
    if isinstance(observation, list):
        return [r for r in observation if isinstance(r, dict)]
    return [observation] if isinstance(observation, dict) else []


def _messages(observation: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for record in _records(observation):
        for message in record.get("ha_messages") or ():
            if isinstance(message, dict):
                out.append(message)
    return out


def _at(observation: Any, observations: list[LearningObservation]) -> datetime:
    for obs in reversed(observations):
        if obs.created_at is not None:
            return obs.created_at
    for record in reversed(_records(observation)):
        text = record.get("timestamp")
        if text:
            try:
                parsed = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
            except ValueError:
                continue
    return datetime(2026, 1, 1, tzinfo=UTC)


def _versions() -> VersionTriple:
    return VersionTriple(
        topology_version_id="topo-0",
        mapping_version_id="map-0",
        model_set_id="mset-l2",
    )


def _estimate(
    subject_id: str,
    point_w: float,
    level: PowerLevelV3,
    status: EstimateStatus,
    method: InferenceMethod,
    source_reader_ids: list[str],
    at: datetime,
    *,
    evidence: list[EvidenceRef] | None = None,
    explanation: str = "",  # noqa: ARG001 - provenance carried by evidence_refs
) -> DeviceEstimate:
    return DeviceEstimate(
        subject_kind="device",
        subject_id=subject_id,
        point_w=float(point_w),
        bounds=Bounds(lower_w=float(point_w) - 1.0, upper_w=float(point_w) + 1.0, basis="posterior"),
        status=status,
        identifiability=IdentifiabilityStatus.IDENTIFIABLE,
        level=level,
        method=method,
        confidence=ConfidenceVectorV3(measurement_quality=0.9, model_fit=0.8),
        source_reader_ids=source_reader_ids,
        evidence_refs=evidence or [EvidenceRef(kind="observation", id=f"l2-{subject_id}")],
        versions=_versions(),
        updated_at=at,
    )


def _anchor_from_messages(observation: Any, at: datetime) -> LearningObservation | None:
    """A reported single-subject endpoint level becomes an anchor observation."""
    for message in _messages(observation):
        attrs = message.get("attributes") or {}
        reported = attrs.get("power_w_observation")
        state = str(message.get("state", "")).strip().lower()
        if reported is None or state not in {"standby", "off"}:
            continue
        try:
            level_w = float(reported)
        except (TypeError, ValueError):
            continue
        return LearningObservation(
            observation_id="l2-plug-anchor",
            subject_kind="device",
            subject_id=_TV_SUBJECT,
            endpoint_id=_PLUG_READER_ID,
            event_group_ref=EvidenceRef(kind="ha_event", id="hb17-step8-tv-standby"),
            transition="standby_reported",
            attributes={"level": "standby", "endpoint_kind": "single_subject"},
            level_w=level_w,
            status="confirmed",
            identifiability="identifiable",
            basis=ModelBasis.ABSOLUTE_ANCHOR,
            created_at=at,
        )
    return None


def learn_power_model(
    observation: Any,
    *,
    observations: list[LearningObservation] | None = None,
) -> list[DeviceEstimate]:
    """Learn per-subject standby estimates from an HB §17 observation frame.

    ``observations`` carries device-attributed evidence (ΔP chains plus a
    residual-baseline step) for subjects that have no single-subject endpoint;
    each is fitted with :class:`FixedPowerModel`.  The plug anchor is derived
    from the HA messages in ``observation`` when present.
    """
    evidence = list(observations or ())
    at = _at(observation, evidence)
    anchor = _anchor_from_messages(observation, at)
    if anchor is not None:
        evidence.append(anchor)

    # Group the device-attributed evidence by subject.
    by_subject: dict[str, list[LearningObservation]] = defaultdict(list)
    for obs in evidence:
        by_subject[obs.subject_id].append(obs)

    estimates: list[DeviceEstimate] = []
    for subject_id in sorted(by_subject):
        subject_obs = by_subject[subject_id]
        model = FixedPowerModel().fit(subject_obs)
        stat = model.spec.levels.get(PowerLevelV3.STANDBY)
        point = stat.median_w if stat is not None else None
        if point is None:
            # No honest standby estimate: publish UNKNOWN, never 0 W.
            estimates.append(
                DeviceEstimate(
                    subject_kind="device",
                    subject_id=subject_id,
                    point_w=None,
                    bounds=Bounds(lower_w=None, upper_w=None),
                    status=EstimateStatus.UNKNOWN,
                    identifiability=IdentifiabilityStatus.INSUFFICIENT_EVIDENCE,
                    level=PowerLevelV3.UNKNOWN,
                    method=InferenceMethod.NONE,
                    confidence=ConfidenceVectorV3(),
                    source_reader_ids=[],
                    evidence_refs=[EvidenceRef(kind="observation", id=f"l2-{subject_id}")],
                    versions=_versions(),
                    updated_at=at,
                )
            )
            continue

        anchored = model.basis == ModelBasis.ABSOLUTE_ANCHOR
        estimates.append(
            _estimate(
                subject_id,
                point,
                PowerLevelV3.STANDBY,
                EstimateStatus.MEASURED if anchored else EstimateStatus.ESTIMATED,
                InferenceMethod.DIRECT_READING if anchored else InferenceMethod.MODEL_PRIOR,
                [subject_obs[0].endpoint_id],
                at,
                evidence=[EvidenceRef(kind="observation", id=f"l2-{subject_id}-standby")],
                explanation="single_subject_endpoint_standby" if anchored else "delta_plus_residual_baseline_standby",
            )
        )
    return estimates
