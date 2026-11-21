"""Measurement, event and data-quality contracts (K02).

Implements ``interfaces.md`` §3: the HB §2.1 :class:`ReaderMeasurement`, the
device-free :class:`HAEvent`, and the stage-4/5/6 records
:class:`MeasurementEvent`, :class:`EventEvidence`, :class:`SubEvent`,
:class:`ReaderDelta` and :class:`EventGroup`, with their lifecycle enums.  It
also owns the measurement basis of a watt value (:class:`MeasurementBasis`),
the per-reader data-quality status and report (:class:`QualityStatus`,
:class:`DataQualityReport`) and time-scoped measurement factors
(:class:`MeasurementFactor`).

``EvidenceRef``, ``VersionTriple`` and the single Pydantic ``Model`` base
(``extra="forbid"``, ``use_enum_values=True``) come from :mod:`.foundation`.

Absent values are ``None``, never ``0`` (HB §18): a reader with no usable data
in its window keeps ``delta_w is None`` with ``completeness="missing"``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from .foundation import EvidenceRef, Model, VersionTriple, _document, utcnow

__all__: list[str] = [
    # Enums
    "MeasurementBasis",
    "QualityStatus",
    "MeasurementType",
    "QualityFlag",
    "StepClass",
    "SubEventKind",
    "EventGroupStatus",
    # Records
    "ReaderMeasurement",
    "HAEvent",
    "MeasurementEvent",
    "EventEvidence",
    "SubEvent",
    "ReaderDelta",
    "EventGroup",
    # Data quality and measurement factors
    "DataQualityReport",
    "MeasurementFactor",
    "validate_factor_intervals",
    # Content-hash helpers
    "ha_event_id",
    "measurement_event_id",
]


# --------------------------------------------------------------------------- #
# Measurement basis, data quality and per-reader factors.
# --------------------------------------------------------------------------- #


class MeasurementBasis(str, Enum):
    """What produced the watt value, independent of how much we trust it.

    ``Source`` answers *"how was the value obtained"*; this answers *"what kind
    of instrument produced the watts"*.  A ``DIRECT_MEASURED`` result can be
    ``FACTOR_ADJUSTED``, and a ``MODEL_ESTIMATED`` result can never be
    ``CT_MEASURED``.
    """

    CT_MEASURED = "ct_measured"
    MONITOR_REPORTED = "monitor_reported"
    FACTOR_ADJUSTED = "factor_adjusted"
    ESTIMATED_APPLIANCE_TOTAL = "estimated_appliance_total"
    UNKNOWN = "unknown"

_document(
    MeasurementBasis,
    CT_MEASURED="A physical CT reading of the conductor, used as reported.",
    MONITOR_REPORTED="The monitor published the whole load itself (e.g. Refoss).",
    FACTOR_ADJUSTED="A per-circuit scaling factor was applied to the reading.",
    ESTIMATED_APPLIANCE_TOTAL=(
        "An appliance total estimated from a model or a configured prior, not measured."
    ),
    UNKNOWN="The basis is not established; no scaling or interpretation is claimed.",
)


class QualityStatus(str, Enum):
    GOOD = "good"
    DEGRADED = "degraded"
    STALE = "stale"
    BAD = "bad"
    MISSING = "missing"


class DataQualityReport(Model):
    timestamp: datetime = Field(default_factory=utcnow)
    overall_score: float = 0.0
    channels: dict[str, dict[str, Any]] = Field(default_factory=dict)
    issues: list[str] = Field(default_factory=list)


class MeasurementFactor(Model):
    """A per-circuit scaling factor with a validity interval.

    A bare float on ``CTChannel`` cannot answer *"what factor was in force when
    this reading was taken"*, so a changed factor makes history
    unreconstructable.  A reading is resolved against the interval covering its
    ``state_timestamp``; today's factor is never applied to a past sample.
    """

    ct_id: str
    """The CT the factor belongs to."""

    multiplier: float = Field(gt=0.0)
    """Strictly positive scaling applied to the reading.  Zero would hide the
    circuit and a negative value would look like export."""

    effective_from: datetime
    """Inclusive start of the interval."""

    effective_to: datetime | None = None
    """Exclusive end of the interval; ``None`` means "still in force"."""

    applied_by: Literal["monitor", "user", "unknown"] = "unknown"
    """Who applied the factor.  ``monitor`` means the reported power already
    includes it and must never be multiplied again."""

    declared_by: Literal["user", "import", "default", "inferred"] = "user"
    """Where the declaration came from, for audit."""

    note: str | None = None
    """Free-text reason, e.g. ``"Refoss publishes the whole load"``."""

    @field_validator("multiplier")
    @classmethod
    def _positive_finite(cls, v: float) -> float:
        value = float(v)
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("multiplier must be finite")
        if value <= 0.0:
            raise ValueError("multiplier must be greater than zero")
        return value

    @model_validator(mode="after")
    def _interval_is_forward(self) -> MeasurementFactor:
        if self.effective_to is not None and self.effective_to <= self.effective_from:
            raise ValueError(
                "effective_to must be strictly after effective_from; an empty "
                "interval covers no reading"
            )
        return self


def validate_factor_intervals(factors: Iterable[MeasurementFactor]) -> None:
    """Reject overlapping validity intervals for the same ``ct_id``.

    Intervals are half-open (``[from, to)``), so a factor change at ``t`` is
    expressible: the old interval ends exactly where the new one begins.
    Different CTs may overlap freely.  Raises ``ValueError`` on the first
    conflict; returns ``None`` when every interval is disjoint per channel.
    """
    by_ct: dict[str, list[MeasurementFactor]] = {}
    for factor in factors:
        by_ct.setdefault(str(factor.ct_id), []).append(factor)
    for ct_id, items in by_ct.items():
        ordered = sorted(items, key=lambda f: f.effective_from)
        for earlier, later in zip(ordered, ordered[1:], strict=False):
            if earlier.effective_to is None or later.effective_from < earlier.effective_to:
                raise ValueError(
                    f"overlapping measurement factors for ct_id={ct_id!r}: "
                    f"{earlier.effective_from.isoformat()}.."
                    f"{earlier.effective_to.isoformat() if earlier.effective_to else 'open'} "
                    f"overlaps {later.effective_from.isoformat()}"
                )


# --------------------------------------------------------------------------- #
# Enumerations (interfaces.md §3).
# --------------------------------------------------------------------------- #


class MeasurementType(str, Enum):
    """What kind of value a reader measurement represents (HB §2.1)."""

    INSTANTANEOUS = "instantaneous"
    WINDOW_AVERAGE = "window_average"
    DEVICE_REPORTED = "device_reported"
    CALCULATED = "calculated"
    ACCUMULATED_ENERGY = "accumulated_energy"


class QualityFlag(str, Enum):
    """Why a measurement may be unfit for inference (HB §6.1, §18)."""

    UNIT_INVALID = "unit_invalid"
    IMPLAUSIBLE_RANGE = "implausible_range"
    REPEATED = "repeated"
    STALE_ON_ARRIVAL = "stale_on_arrival"
    CLOCK_SKEW_FUTURE = "clock_skew_future"
    PRIMED = "primed"
    OUT_OF_ORDER = "out_of_order"


class StepClass(str, Enum):
    """Character of a detected reader change (P3)."""

    SUSTAINED = "sustained"
    TRANSIENT = "transient"
    PERIODIC = "periodic"
    DRIFT = "drift"
    DATA_GAP = "data_gap"


class SubEventKind(str, Enum):
    """What kind of atomic change a sub-event records."""

    HA_STATE = "ha_state"
    HA_ATTRIBUTE = "ha_attribute"
    READER_STEP = "reader_step"
    PLUG_STEP = "plug_step"


class EventGroupStatus(str, Enum):
    """The six HB §12.3 states, in order."""

    DETECTED = "detected"
    MATCHING = "matching"
    PROVISIONAL = "provisional"
    CONFIRMED = "confirmed"
    REVISED = "revised"
    UNRESOLVED = "unresolved"


# --------------------------------------------------------------------------- #
# Deterministic content ids.
# --------------------------------------------------------------------------- #


def _sha256_id(prefix: str, payload: dict[str, Any]) -> str:
    """A stable id: sha256 of a canonical JSON encoding of ``payload``."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{prefix}-{digest[:16]}"


def _iso(value: datetime) -> str:
    """Canonical timestamp form used in content hashes."""
    return value.isoformat()


def ha_event_id(
    entity_id: str,
    observed_at: datetime,
    to_state: str | None,
    attributes_delta: dict[str, Any],
) -> str:
    """Content hash of (entity_id, observed_at, to_state, sorted attributes_delta).

    ``attributes_delta`` is canonicalised by sorting its keys, so two dicts with
    the same items in a different insertion order produce the same id.  A
    different ``to_state`` (or entity/time/attribute) produces a different id.
    """
    return _sha256_id(
        "hae",
        {
            "entity_id": str(entity_id),
            "observed_at": _iso(observed_at),
            "to_state": None if to_state is None else str(to_state),
            "attributes_delta": attributes_delta,
        },
    )


def measurement_event_id(reader_id: str, onset_lo: datetime, detector_version: str) -> str:
    """Stable id of a detected reader change: hash of its three-part identity."""
    return _sha256_id(
        "mev",
        {
            "reader_id": str(reader_id),
            "onset_lo": _iso(onset_lo),
            "detector_version": str(detector_version),
        },
    )


# --------------------------------------------------------------------------- #
# Records (interfaces.md §3).
# --------------------------------------------------------------------------- #


class ReaderMeasurement(Model):
    """HB §2.1 normalized reading, produced only by P1 (polarity applied once).

    Unsupported measurements stay ``None`` -- the contract never fabricates a
    field (HB §2.1) and never uses ``0`` to mean "unknown" (HB §18).
    """

    reader_id: str
    observed_at: datetime
    received_at: datetime
    active_power_w: float | None = None
    current_rms_a: float | None = None
    voltage_rms_v: float | None = None
    reactive_power_var: float | None = None
    power_factor: float | None = None
    energy_wh: float | None = None
    phase: Literal["l1", "l2", "both", "unknown"]
    leg: Literal["l1", "l2"] | None = None
    voltage_reference_v: float | None = None
    measurement_type: MeasurementType
    measurement_window_ms: int | None = None
    calibration_version: str
    measurement_basis: str
    quality_flags: list[QualityFlag] = []
    sequence: int


class HAEvent(Model):
    """A Home Assistant state change, with no device binding.

    Binding is a *matching output*, never an input: this record deliberately has
    no device/component field.  ``event_id`` is a content hash so the same
    change ingested twice has the same id.
    """

    event_id: str
    entity_id: str
    observed_at: datetime
    received_at: datetime
    from_state: str | None = None
    to_state: str | None = None
    attributes_delta: dict[str, Any] = {}
    context_id: str | None = None
    context_parent_id: str | None = None
    context_user_id: str | None = None
    linked: bool

    @classmethod
    def make(
        cls,
        *,
        entity_id: str,
        observed_at: datetime,
        received_at: datetime,
        from_state: str | None = None,
        to_state: str | None = None,
        attributes_delta: dict[str, Any] | None = None,
        context_id: str | None = None,
        context_parent_id: str | None = None,
        context_user_id: str | None = None,
        linked: bool,
    ) -> HAEvent:
        """Build an event, deriving ``event_id`` from its content."""
        delta = dict(attributes_delta or {})
        return cls(
            event_id=ha_event_id(entity_id, observed_at, to_state, delta),
            entity_id=entity_id,
            observed_at=observed_at,
            received_at=received_at,
            from_state=from_state,
            to_state=to_state,
            attributes_delta=delta,
            context_id=context_id,
            context_parent_id=context_parent_id,
            context_user_id=context_user_id,
            linked=linked,
        )


class MeasurementEvent(Model):
    """One detected reader change (P3), with a stable, identity-based id.

    A ``DATA_GAP`` carries *no* measurement: ``delta_w``, ``sigma_w`` and
    ``z_score`` are ``None``, never a ``0.0`` sentinel (HB §18).  Every other
    step class must carry all three numbers.  The invariant is enforced by
    :meth:`_null_measurements_match_step_class`.
    """

    event_id: str
    reader_id: str
    delta_w: float | None = None  # signed, HB §2.3; None iff DATA_GAP
    sigma_w: float | None = None  # None iff DATA_GAP
    z_score: float | None = None  # None iff DATA_GAP
    step_class: StepClass
    onset_lo: datetime
    onset_hi: datetime  # timestamp uncertainty = onset_hi - onset_lo
    pre_level_w: float | None = None
    post_level_w: float | None = None
    quality_score: float  # 0..1, documented formula in P3
    detected_at: datetime

    @model_validator(mode="after")
    def _onset_order(self) -> MeasurementEvent:
        if self.onset_lo > self.onset_hi:
            raise ValueError("onset_lo must not be after onset_hi")
        return self

    @model_validator(mode="after")
    def _null_measurements_match_step_class(self) -> MeasurementEvent:
        is_gap = self.step_class == StepClass.DATA_GAP
        measured = (self.delta_w, self.sigma_w, self.z_score)
        if is_gap and any(value is not None for value in measured):
            raise ValueError("a DATA_GAP event must have delta_w, sigma_w and z_score = None")
        if not is_gap and any(value is None for value in measured):
            raise ValueError("a non-DATA_GAP event must carry delta_w, sigma_w and z_score")
        return self


class EventEvidence(Model):
    """Immutable event window (P4), HB §6.5.

    ``finalized=True`` does not freeze the object at the Pydantic level, but
    :meth:`with_sample` always returns a *new* record with a new id: evidence is
    immutable by construction, and a revision never mutates its predecessor.
    """

    evidence_id: str
    reader_id: str
    measurement_event_id: str
    window_start: datetime
    window_end: datetime
    samples: list[tuple[datetime, float | None]]  # retained per DEC-30
    baseline_w: float | None = None
    sigma_w: float | None = None
    delta_w: float
    quality_flags: list[QualityFlag] = []
    topology_version_id: str
    finalized: bool  # False = PROVISIONAL window still open

    def with_sample(self, at: datetime, value_w: float | None) -> EventEvidence:
        """Return a new evidence record with one more sample and a fresh id."""
        payload = self.model_dump()
        payload["samples"] = [*self.samples, (at, value_w)]
        payload["evidence_id"] = _sha256_id("evd", payload)
        return type(self)(**payload)


class SubEvent(Model):
    """An atomic change within an event group, with its own timestamp."""

    sub_event_id: str
    kind: SubEventKind
    ref: EvidenceRef  # ha_event or event_evidence
    observed_at: datetime
    timestamp_uncertainty_s: float


class ReaderDelta(Model):
    """A reader's signed contribution to an event group.

    ``delta_w is None`` iff ``completeness == "missing"``: a reader with no
    usable data keeps ``None`` and is never reported as ``0`` (HB §18).
    """

    reader_id: str
    delta_w: float | None = None
    sigma_w: float | None = None
    completeness: Literal["complete", "pre_missing", "post_pending", "missing"]
    evidence_ref: EvidenceRef | None = None

    @model_validator(mode="after")
    def _completeness_matches_delta(self) -> ReaderDelta:
        missing = self.completeness == "missing"
        if missing != (self.delta_w is None):
            if missing:
                raise ValueError("a missing reader delta must have delta_w=None, not 0")
            raise ValueError("a non-missing reader delta must carry a delta_w, not None")
        return self


class EventGroup(Model):
    """A cross-reader event group (P5); prior revisions are immutable."""

    group_id: str
    revision: int  # REVISED creates revision+1; prior revisions immutable
    status: EventGroupStatus
    opened_at: datetime
    closed_at: datetime | None = None
    sub_events: list[SubEvent] = []
    reader_deltas: list[ReaderDelta] = []
    grouping_reasons: list[str] = []
    versions: VersionTriple

    @field_validator("revision")
    @classmethod
    def _revision_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("event group revision must be >= 1")
        return value


# --------------------------------------------------------------------------- #
# Generated JSON-schema exports (see tools/dev/export_schemas.py).
# --------------------------------------------------------------------------- #

SCHEMA_EXPORTS: list[tuple[str, type]] = [
    ("reader_measurement", ReaderMeasurement),
    ("ha_event", HAEvent),
    ("measurement_event", MeasurementEvent),
    ("event_evidence", EventEvidence),
    ("sub_event", SubEvent),
    ("reader_delta", ReaderDelta),
    ("event_group", EventGroup),
]
