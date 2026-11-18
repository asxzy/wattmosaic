"""Inference contracts (owned by K04).

First-class device and group estimates, candidate/match states, versioned
event-inference records and the HB §12.1 :class:`InferenceSnapshot`.

This module is additive only: it introduces no name that already exists in
:mod:`wattmosaic_engine.contracts.legacy`.  Where ``interfaces.md`` §6 uses a
name that the legacy surface already owns, the v3 name is suffixed with ``V3``:

* ``Reconciliation`` (legacy, non-nullable balance) -> :class:`ReconciliationV3`
  here (every term nullable, HB §18; ``main = identified + groups + unknown +
  residual`` with a ``balance_error_w`` diagnostic).
* ``DataQualityReport`` (legacy) -> re-used verbatim from the legacy module for
  the snapshot's ``system_quality`` field (it is not a new public name here).
* ``PowerLevel`` (legacy) -> superseded by ``PowerLevelV3`` from K06's
  ``power_models`` module for the estimate's ``level`` field; the legacy enum
  does not distinguish OFF / STANDBY / IDLE (HB §11.5).  ``PowerLevelV3`` is
  imported from :mod:`contracts.power_models`, not redefined here.
* ``EventGroupStatus`` (§3, owned by K02) and ``ReaderMeasurement`` (K02) ->
  imported from :mod:`contracts.measurement`; ``ReaderResidualPoint`` (K03) ->
  imported from :mod:`contracts.topology`.  These canonical types are re-used
  rather than duplicated or suffixed with ``V3`` here.

The shared foundation types (``VersionTriple``, ``EvidenceRef``, ``Bounds``,
``EstimateStatus``, ``IdentifiabilityStatus``, ``InferenceMethod``,
``ConfidenceVectorV3``) are re-used from :mod:`contracts.foundation`.

Null policy (HB §18): an absent power is ``None``, never ``0``.  Every watt
field here defaults to ``None`` and a non-identifiable member never carries a
point value (DEC-19).
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import Field, model_validator

from .foundation import (
    Bounds,
    ConfidenceVectorV3,
    EstimateStatus,
    EvidenceRef,
    IdentifiabilityStatus,
    InferenceMethod,
    SnapshotId,
    VersionTriple,
)
from .legacy import DataQualityReport, Model
from .measurement import EventGroupStatus, ReaderMeasurement
from .power_models import PowerLevelV3
from .topology import ReaderResidualPoint

__all__: list[str] = [
    # Enums
    "MatchState",
    "CandidateSource",
    # Models
    "CandidateHypothesis",
    "DeviceEstimate",
    "DeviceGroupEstimate",
    "UnknownEstimate",
    "JointInferenceResult",
    "EventInferenceRecord",
    "ReconciliationV3",
    "InferenceSnapshot",
]

# --------------------------------------------------------------------------- #
# Enums (HB §7.6, §7.1, §12.3).
# --------------------------------------------------------------------------- #


class MatchState(str, Enum):
    """State of a candidate-to-subject match (HB §7.6)."""

    UNMATCHED = "unmatched"
    PROPOSED = "proposed"
    PROVISIONAL = "provisional"
    CONFIRMED = "confirmed"
    AMBIGUOUS = "ambiguous"
    CONFLICTED = "conflicted"
    REJECTED = "rejected"
    EXPIRED = "expired"


class CandidateSource(str, Enum):
    """Where a candidate hypothesis came from (HB §7.1, ten sources)."""

    HA_STATE = "ha_state"
    HA_ATTRIBUTE = "ha_attribute"
    LEARNED_PROFILE = "learned_profile"
    PRIOR_MAPPING = "prior_mapping"
    PLUG = "plug"
    UNKNOWN_PROFILE = "unknown_profile"
    TOPOLOGY_VISIBLE = "topology_visible"
    DELAYED_RESPONSE = "delayed_response"
    DEVICE_GROUP = "device_group"
    UNKNOWN_SCOPE = "unknown_scope"


# --------------------------------------------------------------------------- #
# Candidate hypotheses (HB §7).
# --------------------------------------------------------------------------- #


class CandidateHypothesis(Model):
    """One hypothesis explaining an observed event (HB §7, evidence A..H)."""

    candidate_id: str
    subject_kind: Literal["device", "component", "group", "unknown_profile", "unknown_scope"]
    subject_id: str
    sources: list[CandidateSource]
    transition: str | None = None
    evidence_terms: dict[str, float]
    filtered: bool = False
    filter_reason: str | None = None


# --------------------------------------------------------------------------- #
# Device estimates (HB §8.4 / §8.5).
# --------------------------------------------------------------------------- #


_NON_POINT_IDENTIFIABILITY = frozenset(
    {
        IdentifiabilityStatus.NON_IDENTIFIABLE,
        IdentifiabilityStatus.INSUFFICIENT_EVIDENCE,
    }
)


class DeviceEstimate(Model):
    """A first-class estimate of one device or load component.

    A non-identifiable member has ``point_w is None`` and justified bounds,
    never a fabricated number (DEC-19, HB §8.4).
    """

    subject_kind: Literal["device", "component"]
    subject_id: str
    point_w: float | None = None
    bounds: Bounds
    status: EstimateStatus
    identifiability: IdentifiabilityStatus
    group_id: str | None = None
    level: PowerLevelV3 | None = None
    method: InferenceMethod
    allocation_rule: str | None = None
    confidence: ConfidenceVectorV3
    source_reader_ids: list[str]
    evidence_refs: list[EvidenceRef]
    versions: VersionTriple
    model_version_id: str | None = None
    updated_at: datetime
    provisional: bool = False

    @model_validator(mode="after")
    def _validate_status_and_identifiability(self) -> DeviceEstimate:
        status = EstimateStatus(self.status)
        ident = IdentifiabilityStatus(self.identifiability)
        method = InferenceMethod(self.method)

        # ALLOCATED iff allocation_rule is set.
        if status is EstimateStatus.ALLOCATED:
            if not self.allocation_rule:
                raise ValueError("ALLOCATED estimates require an allocation_rule")
        elif self.allocation_rule is not None:
            raise ValueError("allocation_rule is only allowed when status is ALLOCATED")

        # MEASURED means one direct reader.
        if status is EstimateStatus.MEASURED:
            if method is not InferenceMethod.DIRECT_READING:
                raise ValueError("MEASURED estimates require method DIRECT_READING")
            if len(self.source_reader_ids) != 1:
                raise ValueError("MEASURED estimates require exactly one source_reader_id")

        # Non-identifiable states never have a point value.
        if ident in _NON_POINT_IDENTIFIABILITY and self.point_w is not None:
            raise ValueError(f"{ident.value} estimates must have point_w = None")

        # A group member that is non-identifiable must not have a point.
        if (
            self.group_id is not None
            and ident is IdentifiabilityStatus.NON_IDENTIFIABLE
            and self.point_w is not None
        ):
            raise ValueError("a non-identifiable group member must have point_w = None")

        # Absent point => UNKNOWN status (group members are UNKNOWN too).
        if self.point_w is None and status is not EstimateStatus.UNKNOWN:
            raise ValueError("an estimate with point_w = None must have status UNKNOWN")
        return self


class DeviceGroupEstimate(Model):
    """The identified sum of a non-separable device group (HB §8.4)."""

    group_id: str
    member_ids: list[str]
    point_w: float | None = None
    bounds: Bounds
    identifiability: IdentifiabilityStatus
    source_reader_ids: list[str]
    evidence_refs: list[EvidenceRef]
    versions: VersionTriple
    updated_at: datetime

    @model_validator(mode="after")
    def _validate_point_and_bounds(self) -> DeviceGroupEstimate:
        if self.point_w is not None:
            if IdentifiabilityStatus(self.identifiability) is not IdentifiabilityStatus.GROUP_IDENTIFIABLE:
                raise ValueError("a group point_w requires identifiability GROUP_IDENTIFIABLE")
            lower, upper = self.bounds.lower_w, self.bounds.upper_w
            if lower is not None and self.point_w < lower:
                raise ValueError("group point_w is below the lower bound")
            if upper is not None and self.point_w > upper:
                raise ValueError("group point_w is above the upper bound")
        return self


class UnknownEstimate(Model):
    """An unmonitored/unknown-scope competitor on a branch row (HB §8)."""

    scope_reader_id: str
    delta_w: float | None = None
    point_w: float | None = None
    sigma_w: float | None = None
    profile_id: str | None = None


# --------------------------------------------------------------------------- #
# Joint inference result and versioned records.
# --------------------------------------------------------------------------- #


def _estimate_fingerprint(estimate: Any) -> str:
    """Stable canonical fingerprint of one device estimate (model or dict)."""
    if isinstance(estimate, dict):
        return (
            f"{estimate.get('subject_kind')}:{estimate.get('subject_id')}:{estimate.get('point_w')}:"
            f"{estimate.get('status')}:{estimate.get('identifiability')}"
        )
    return (
        f"{estimate.subject_kind}:{estimate.subject_id}:{estimate.point_w}:"
        f"{EstimateStatus(estimate.status).value}:{IdentifiabilityStatus(estimate.identifiability).value}"
    )


class JointInferenceResult(Model):
    """The typed handoff I7 -> M2/L4/D4/P6 (§6).

    ``result_id`` is a content hash of (group id, revision, versions, sorted
    estimates); it is independent of estimate list order so a replay is
    byte-identical (AT-19).
    """

    result_id: str
    event_group_id: str | None = None
    event_group_revision: int | None = None
    status: EventGroupStatus
    device_estimates: list[DeviceEstimate]
    group_estimates: list[DeviceGroupEstimate]
    unknown_estimates: list[UnknownEstimate]
    candidates: list[CandidateHypothesis]
    matches: dict[str, MatchState]
    residual_w_by_reader: dict[str, float | None]
    versions: VersionTriple
    computed_at: datetime

    @model_validator(mode="before")
    @classmethod
    def _derive_result_id(cls, data: Any) -> Any:
        if isinstance(data, dict) and not data.get("result_id"):
            versions = data.get("versions")
            v_text = (
                f"{getattr(versions, 'topology_version_id', versions)}|"
                f"{getattr(versions, 'mapping_version_id', versions)}|"
                f"{getattr(versions, 'model_set_id', versions)}"
                if not isinstance(versions, dict)
                else f"{versions.get('topology_version_id')}|{versions.get('mapping_version_id')}|"
                f"{versions.get('model_set_id')}"
            )
            estimates = sorted(_estimate_fingerprint(e) for e in data.get("device_estimates", []))
            canonical = "\0".join(
                (
                    str(data.get("event_group_id")),
                    str(data.get("event_group_revision")),
                    v_text,
                    "\0".join(estimates),
                )
            )
            digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            data = {**data, "result_id": f"res-{digest[:16]}"}
        return data


class EventInferenceRecord(Model):
    """A versioned event interpretation; evidence immutable, result versioned.

    ``interpretation_revision == 1`` iff ``previous_record_id is None`` — the
    same chain rule :class:`legacy.InferenceRecord` applies to its revision.
    """

    record_id: str
    event_group_id: str
    interpretation_revision: int = Field(ge=1)
    previous_record_id: str | None = None
    result: JointInferenceResult
    reason: Literal["initial", "late_data", "window_closed", "model_update", "topology_update", "replay"]
    created_at: datetime

    @model_validator(mode="after")
    def _validate_revision_chain(self) -> EventInferenceRecord:
        is_first = self.interpretation_revision == 1
        has_previous = self.previous_record_id is not None
        if is_first == has_previous:
            raise ValueError(
                "interpretation_revision == 1 iff previous_record_id is None"
            )
        return self


# --------------------------------------------------------------------------- #
# Reconciliation + snapshot (HB §12.1).
# --------------------------------------------------------------------------- #


class ReconciliationV3(Model):
    """Home-level balance with every term nullable (HB §18).

    ``main = identified + group + unknown + residual`` where identified sums
    each device once (AT-14).  ``balance_error_w`` is the signed mismatch and
    is ``None`` when any term is absent, never a fabricated zero.
    """

    main_w: float | None = None
    identified_w: float | None = None
    group_w: float | None = None
    unknown_w: float | None = None
    residual_w: float | None = None
    export_w: float | None = None
    meter_spread_w: float | None = None

    @property
    def balance_error_w(self) -> float | None:
        terms = (self.main_w, self.identified_w, self.group_w, self.unknown_w, self.residual_w)
        if any(term is None for term in terms):
            return None
        assert self.main_w is not None
        accounted = (self.identified_w or 0.0) + (self.group_w or 0.0) + (self.unknown_w or 0.0) + (
            self.residual_w or 0.0
        )
        return self.main_w - accounted


class InferenceSnapshot(Model):
    """HB §12.1 snapshot; the latest published interpretation of measurements."""

    snapshot_id: SnapshotId
    observed_at: datetime
    computed_at: datetime
    versions: VersionTriple
    reader_measurements: list[ReaderMeasurement]
    device_estimates: list[DeviceEstimate]
    group_estimates: list[DeviceGroupEstimate]
    residual_estimates: list[ReaderResidualPoint]
    reconciliation: ReconciliationV3
    system_quality: DataQualityReport
    stale_sources: list[str]
    unresolved_events: list[str]


# --------------------------------------------------------------------------- #
# Generated JSON-schema exports (see tools/dev/export_schemas.py).
# --------------------------------------------------------------------------- #

SCHEMA_EXPORTS: list[tuple[str, type]] = [
    ("candidate_hypothesis", CandidateHypothesis),
    ("device_estimate_v3", DeviceEstimate),
    ("device_group_estimate", DeviceGroupEstimate),
    ("unknown_estimate", UnknownEstimate),
    ("joint_inference_result", JointInferenceResult),
    ("event_inference_record", EventInferenceRecord),
    ("reconciliation_v3", ReconciliationV3),
    ("inference_snapshot", InferenceSnapshot),
]
