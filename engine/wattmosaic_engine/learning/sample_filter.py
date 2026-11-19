"""Learning-sample rejection filter.

Every :class:`~wattmosaic_engine.contracts.SampleRejection` reason is implemented
here with a documented, configurable threshold.  The filter *never* raises; it
returns a :class:`SampleDecision` carrying the accepted flag, reason and a
0–1 quality score.  Rejected samples are still returned to the caller so they
can be persisted with their reason (spec §11.2 "store rejection reasons").
"""

from __future__ import annotations

import re
import statistics
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from ..contracts import (
    EventGroupStatus,
    IdentifiabilityStatus,
    LearningObservation,
    LearningSample,
    SampleRejection,
    utcnow,
)
from .thresholds import LearningThresholds

_NOTE_RE = re.compile(r"^([a-zA-Z0-9_]+):(.*)$")

#: Order in which checks are applied.  Earlier structural problems make later
#: numerical checks meaningless, and it keeps the reported reason deterministic
#: when a sample has several faults (the *first* fault is the diagnosis).
_CHECK_ORDER: tuple[SampleRejection, ...] = (
    SampleRejection.DUPLICATE_STATE,
    SampleRejection.MISSING_MEASUREMENT,
    SampleRejection.TIMESTAMP_UNRELIABLE,
    SampleRejection.STALE_MEASUREMENT,
    SampleRejection.CONCURRENT_EVENTS,
    SampleRejection.AMBIGUOUS_TOPOLOGY,
    SampleRejection.UNCONFIRMED_ASSOCIATION,
    SampleRejection.UNSTABLE_READING,
    SampleRejection.EXCESSIVE_NOISE,
    SampleRejection.INCONSISTENT_CHANGE,
    SampleRejection.INSUFFICIENT_MAGNITUDE,
)


@dataclass(slots=True)
class SampleEvidence:
    """Optional quantitative evidence supplied by the caller/solver.

    Everything is optional; the filter falls back to notes written by the
    collector.  Supplying it explicitly is preferred when an event processor
    already has the window statistics.
    """

    now: datetime | None = None
    now_fn: Any = None
    baseline_sigma_w: float | None = None
    event_sigma_w: float | None = None
    noise_w: float | None = None
    baseline_samples: int | None = None
    event_samples: int | None = None
    concurrent_events: int = 0
    #: Identifiers of the competing physical devices (audit only).
    competing_device_ids: list[str] = field(default_factory=list)
    #: The isolation window the count was measured over, in seconds.
    concurrency_window_s: float | None = None
    topology_confirmed: bool = True
    association_confidence: float | None = None
    timestamp_quality: str | None = None
    measurement_age_s: float | None = None
    state: str | None = None
    expected_sign: int | None = None
    event_count_in_window: int | None = None


@dataclass(slots=True)
class SampleDecision:
    accepted: bool
    reason: SampleRejection | None = None
    quality_score: float = 0.0
    detail: str = ""
    metrics: dict[str, float] = field(default_factory=dict)

    def apply(self, sample: LearningSample) -> LearningSample:
        """Return a copy of ``sample`` carrying the decision."""
        updated = sample.model_copy(deep=True)
        updated.accepted = self.accepted
        updated.rejection_reason = self.reason
        updated.quality_score = self.quality_score
        if self.detail:
            updated.notes = [*updated.notes, f"rejection_detail:{self.detail}"] if not self.accepted else [
                *updated.notes,
                f"quality:{self.quality_score:.2f}",
            ]
        return updated


def sample_notes(sample: LearningSample) -> dict[str, str]:
    """Parse ``key:value`` notes into a dict (first occurrence wins)."""
    out: dict[str, str] = {}
    for note in sample.notes:
        match = _NOTE_RE.match(str(note))
        if match:
            key, value = match.group(1), match.group(2)
            out.setdefault(key, value)
    return out


def _float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out or out in (float("inf"), float("-inf")):
        return None
    return out


def _robust_sigma(values: Sequence[float]) -> float:
    clean = [float(v) for v in values if _float(v) is not None]
    if len(clean) < 2:
        return 0.0
    med = statistics.median(clean)
    return float(1.4826 * statistics.median([abs(v - med) for v in clean]))


def _state_key(sample: LearningSample) -> str | None:
    notes = sample_notes(sample)
    return notes.get("state") or notes.get("ha_state")


ON_STATE_LABELS = frozenset(
    {"on", "open", "running", "active", "heating", "cooling", "true", "1"}
)
OFF_STATE_LABELS = frozenset(
    {"off", "closed", "idle", "standby", "inactive", "false", "0"}
)


def _is_on_state(
    state: str | None, overrides: Mapping[str, str] | None = None
) -> bool | None:
    """Does this HA state mean the device is drawing power?

    ``overrides`` carries the per-device corrections declared in the topology
    (``DeviceTopology.metadata["state_overrides"]``).  They exist because the
    default label sets cannot describe every integration: a spa in a typical
    installation reports ``Cooling`` while drawing 22 W, so treating ``cooling``
    as ON would make every standby sample look like a heater event and teach the
    model the wrong sign.
    """
    if state is None:
        return None
    key = state.strip().lower()
    if overrides:
        override = overrides.get(key)
        if override == "on":
            return True
        if override == "off":
            return False
    if key in ON_STATE_LABELS:
        return True
    if key in OFF_STATE_LABELS:
        return False
    return None


class SampleFilter:
    """Applies all rejection rules with configurable thresholds."""

    def __init__(self, thresholds: LearningThresholds | None = None) -> None:
        self.thresholds = thresholds or LearningThresholds()
        self._last_state: dict[tuple[str, str], datetime] = {}
        #: ``device_id -> {label: on|off}``, set from the declared topology.
        self.state_overrides: dict[str, dict[str, str]] = {}

    def reset(self) -> None:
        self._last_state.clear()

    def set_state_overrides(self, overrides: Mapping[str, Mapping[str, str]]) -> None:
        """Apply per-device state-label corrections from the topology."""
        self.state_overrides = {
            str(device): {
                str(label).strip().lower(): str(meaning).strip().lower()
                for label, meaning in (labels or {}).items()
                if str(meaning).strip().lower() in {"on", "off"}
            }
            for device, labels in (overrides or {}).items()
        }

    # -- public API ------------------------------------------------------- #
    def evaluate(
        self,
        sample: LearningSample,
        evidence: SampleEvidence | None = None,
        *,
        baseline_values: Sequence[float] | None = None,
        event_values: Sequence[float] | None = None,
    ) -> SampleDecision:
        ev = evidence or SampleEvidence()
        notes = sample_notes(sample)
        now = ev.now or self._now(ev)
        metrics: dict[str, float] = {}

        # Derive numeric evidence from notes and optional raw values.
        noise = _float(ev.noise_w if ev.noise_w is not None else notes.get("noise_w"))
        baseline_sigma = _float(ev.baseline_sigma_w)
        event_sigma = _float(ev.event_sigma_w)
        if baseline_sigma is None and baseline_values:
            baseline_sigma = _robust_sigma(baseline_values)
        if event_sigma is None and event_values:
            event_sigma = _robust_sigma(event_values)
        if noise is None:
            candidates = [s for s in (baseline_sigma, event_sigma) if s is not None]
            noise = max(candidates) if candidates else 0.0
        if baseline_sigma is not None and event_sigma is not None:
            noise = max(noise, baseline_sigma, event_sigma)
        metrics["noise_w"] = float(noise)
        baseline_samples = ev.baseline_samples if ev.baseline_samples is not None else int(
            _float(notes.get("baseline_samples")) or 0
        )
        event_samples = ev.event_samples if ev.event_samples is not None else int(
            _float(notes.get("event_samples")) or 0
        )
        metrics["baseline_samples"] = float(baseline_samples)
        metrics["event_samples"] = float(event_samples)

        state = ev.state or _state_key(sample)
        association = ev.association_confidence
        if association is None:
            association = _float(notes.get("association_confidence"))
        timestamp_quality = (ev.timestamp_quality or notes.get("timestamp_quality") or "").lower()
        topology_quality = (notes.get("topology_quality") or "").lower()

        checks: dict[SampleRejection, tuple[bool, str]] = {}

        # -- duplicate state -------------------------------------------- #
        duplicate = False
        duplicate_detail = ""
        state_key = _state_key(sample) or ""
        if state_key:
            key = (sample.device_id, state_key)
            previous = self._last_state.get(key)
            if previous is not None:
                gap = abs((sample.captured_at - previous).total_seconds())
                if gap <= self.thresholds.duplicate_window_s:
                    duplicate = True
                    duplicate_detail = f"same_state_within_{gap:.1f}s"
        checks[SampleRejection.DUPLICATE_STATE] = (
            duplicate,
            duplicate_detail or "state_repeated_inside_duplicate_window",
        )

        # -- missing measurement ---------------------------------------- #
        missing = (
            not sample.supporting_ct_ids
            or "no_measurement" in notes
            or baseline_samples < self.thresholds.min_baseline_samples
            or event_samples < self.thresholds.min_event_samples
        )
        if not sample.supporting_ct_ids:
            missing_detail = "no_supporting_ct"
        elif "no_measurement" in notes:
            missing_detail = "collector_found_no_window_measurement"
        elif baseline_samples < self.thresholds.min_baseline_samples:
            missing_detail = f"baseline_samples={baseline_samples}<{self.thresholds.min_baseline_samples}"
        elif event_samples < self.thresholds.min_event_samples:
            missing_detail = f"event_samples={event_samples}<{self.thresholds.min_event_samples}"
        else:
            missing_detail = "measurement_missing"
        checks[SampleRejection.MISSING_MEASUREMENT] = (missing, missing_detail)

        # -- timestamp unreliable --------------------------------------- #
        ts_problem = ""
        if sample.captured_at is None:
            ts_problem = "captured_at_missing"
        else:
            captured = sample.captured_at
            if captured.tzinfo is None:
                ts_problem = "captured_at_naive"
            else:
                future_skew = (captured - now).total_seconds()
                if future_skew > self.thresholds.max_timestamp_skew_s:
                    ts_problem = f"timestamp_in_future_by_{future_skew:.1f}s"
                elif timestamp_quality in {"unreliable", "bad", "missing", "unknown"}:
                    ts_problem = f"timestamp_quality={timestamp_quality}"
        checks[SampleRejection.TIMESTAMP_UNRELIABLE] = (
            bool(ts_problem),
            ts_problem or "timestamp_ok",
        )

        # -- stale measurement ------------------------------------------ #
        # "Stale" means the *reading* was already old when the sample was
        # captured — it is a property of the observation, not of when the
        # sample is later replayed.  Deriving it from wall-clock ``now`` would
        # make every historical sample unusable the moment it aged past the
        # threshold, so rebuilding a model from stored history (or importing a
        # backfill) would silently reject the entire dataset.  Use only the
        # age that was recorded at capture time; an unknown age is not stale.
        age = ev.measurement_age_s
        if age is None:
            recorded = _float(notes.get("measurement_age_s"))
            age = recorded if recorded is not None else 0.0
        age = float(age or 0.0)
        metrics["measurement_age_s"] = age
        stale = age > self.thresholds.stale_after_s
        checks[SampleRejection.STALE_MEASUREMENT] = (
            stale,
            f"measurement_age={age:.1f}s>{self.thresholds.stale_after_s:.1f}s" if stale else "fresh",
        )

        # -- concurrent events ------------------------------------------ #
        concurrent = int(ev.concurrent_events or 0)
        metrics["concurrent_events"] = float(concurrent)
        checks[SampleRejection.CONCURRENT_EVENTS] = (
            concurrent > self.thresholds.max_concurrent_events,
            f"concurrent_events={concurrent}>{self.thresholds.max_concurrent_events}",
        )

        # -- ambiguous topology ----------------------------------------- #
        # Owner decision (OD-51, 2026-11-18): topology is *always* authoritative
        # for inference and learning.  A candidate/unconfirmed quality is no
        # longer a reason to reject a sample -- the topology confidence penalty
        # (``disaggregation/confidence.py:TOPOLOGY_SCORE``) carries the reduced
        # certainty instead.  Reject only genuine contradictory evidence: more
        # than one candidate association for the same entity (``ambiguous``
        # quality), which means the topology cannot attribute the sample at all.
        topology_bad = (
            topology_quality == "ambiguous"
            or int(_float(notes.get("topology_candidates")) or 0) > 1
        )
        checks[SampleRejection.AMBIGUOUS_TOPOLOGY] = (
            topology_bad,
            f"topology_quality={topology_quality or ('candidate' if not ev.topology_confirmed else 'confirmed')}",
        )

        # -- unconfirmed association ------------------------------------ #
        association_bad = association is not None and association < self.thresholds.min_association_confidence
        if association is None:
            # Absence of an association score is not proof of a bad
            # association: many dedicated-CT events are obvious.  Only fail
            # when the sample also lacks a directly measured supporting CT.
            association_bad = False
        association_detail = (
            f"association_confidence={association:.2f}<{self.thresholds.min_association_confidence:.2f}"
            if association_bad
            else "association_ok"
        )
        checks[SampleRejection.UNCONFIRMED_ASSOCIATION] = (association_bad, association_detail)

        # -- unstable reading ------------------------------------------- #
        level = max(abs(sample.baseline_power_w), abs(sample.event_power_w), 1.0)
        relative = noise / level
        metrics["relative_noise"] = relative
        unstable = (
            bool(baseline_values)
            and bool(event_values)
            and (
                _robust_sigma(baseline_values) > self.thresholds.max_relative_sigma * max(abs(sample.baseline_power_w), 1.0)
                or _robust_sigma(event_values) > self.thresholds.max_relative_sigma * max(abs(sample.event_power_w), 1.0)
            )
        )
        if not unstable and baseline_sigma is not None and event_sigma is not None:
            unstable = (
                baseline_sigma > self.thresholds.max_relative_sigma * max(abs(sample.baseline_power_w), 1.0)
                or event_sigma > self.thresholds.max_relative_sigma * max(abs(sample.event_power_w), 1.0)
            )
        if not unstable and noise > 0:
            # A very large absolute noise relative to the observed level makes
            # the "level" itself meaningless.
            unstable = noise > max(self.thresholds.min_noise_floor_w, self.thresholds.max_relative_sigma * level * 2.0)
        checks[SampleRejection.UNSTABLE_READING] = (
            unstable,
            f"relative_noise={relative:.3f}>{self.thresholds.max_relative_sigma:.2f}" if unstable else "stable",
        )

        # -- excessive noise -------------------------------------------- #
        excessive = noise > self.thresholds.max_absolute_sigma_w
        checks[SampleRejection.EXCESSIVE_NOISE] = (
            excessive,
            f"noise={noise:.1f}W>{self.thresholds.max_absolute_sigma_w:.1f}W" if excessive else "noise_ok",
        )

        # -- inconsistent change ---------------------------------------- #
        inconsistent = False
        inconsistent_detail = ""
        if abs(sample.delta_power_w) > self.thresholds.max_delta_w:
            inconsistent = True
            inconsistent_detail = f"delta={sample.delta_power_w:.0f}W>max={self.thresholds.max_delta_w:.0f}W"
        elif abs(sample.event_power_w - (sample.baseline_power_w + sample.delta_power_w)) > max(
            2.0, 0.01 * max(abs(sample.event_power_w), 1.0)
        ):
            inconsistent = True
            inconsistent_detail = "event_power_mismatch"
        else:
            on_flag = _is_on_state(
                state, self.state_overrides.get(sample.device_id)
            )
            if on_flag is not None and abs(sample.delta_power_w) > self.thresholds.min_delta_w:
                if on_flag and sample.delta_power_w < 0:
                    inconsistent = True
                    inconsistent_detail = "state_on_but_power_decreased"
                elif not on_flag and sample.delta_power_w > 0:
                    inconsistent = True
                    inconsistent_detail = "state_off_but_power_increased"
            if (
                ev.expected_sign is not None
                and abs(sample.delta_power_w) > self.thresholds.min_delta_w
                and sample.delta_power_w * ev.expected_sign < 0
            ):
                inconsistent = True
                inconsistent_detail = "delta_sign_contradicts_expected"
        checks[SampleRejection.INCONSISTENT_CHANGE] = (inconsistent, inconsistent_detail or "consistent")

        # -- insufficient magnitude ------------------------------------- #
        required = max(self.thresholds.min_delta_w, self.thresholds.magnitude_noise_multiple * noise)
        magnitude = abs(sample.delta_power_w)
        metrics["magnitude_w"] = magnitude
        metrics["required_magnitude_w"] = required
        insufficient = magnitude < required
        checks[SampleRejection.INSUFFICIENT_MAGNITUDE] = (
            insufficient,
            f"delta={magnitude:.1f}W<required={required:.1f}W" if insufficient else "magnitude_ok",
        )

        # -- decide ------------------------------------------------------ #
        decided: SampleRejection | None = None
        detail = ""
        for reason in _CHECK_ORDER:
            failed, reason_detail = checks[reason]
            if failed:
                decided = reason
                detail = reason_detail
                break

        quality = self._quality_score(sample, noise=noise, association=association, metrics=metrics)
        if decided is not None:
            quality = min(quality, 0.25)
        else:
            # Only update duplicate bookkeeping for accepted, non-duplicate
            # samples so a burst of repeated states does not re-arm itself.
            if state_key:
                self._last_state[(sample.device_id, state_key)] = sample.captured_at
        return SampleDecision(
            accepted=decided is None,
            reason=decided,
            quality_score=quality,
            detail=detail,
            metrics=metrics,
        )

    def filter(
        self,
        samples: Iterable[LearningSample],
        evidence: SampleEvidence | None = None,
    ) -> list[LearningSample]:
        """Evaluate a batch, returning annotated copies (accepted and rejected)."""
        out: list[LearningSample] = []
        for sample in samples:
            decision = self.evaluate(sample, evidence)
            out.append(decision.apply(sample))
        return out

    # -- helpers ---------------------------------------------------------- #
    def _now(self, evidence: SampleEvidence) -> datetime:
        if callable(evidence.now_fn):
            try:
                value = evidence.now_fn()
                if isinstance(value, datetime):
                    return value if value.tzinfo else value.replace(tzinfo=UTC)
            except Exception:  # pragma: no cover - defensive
                pass
        return utcnow()

    def _quality_score(
        self,
        sample: LearningSample,
        *,
        noise: float,
        association: float | None,
        metrics: dict[str, float],
    ) -> float:
        """Engineering quality score in [0,1] — not a calibrated probability."""
        level = max(abs(sample.event_power_w), abs(sample.delta_power_w), 1.0)
        components = [
            1.0 - min(1.0, noise / max(self.thresholds.max_absolute_sigma_w, 1.0)),
            min(1.0, abs(sample.delta_power_w) / max(self.thresholds.min_delta_w * 5.0, 1.0)),
            min(1.0, min(metrics.get("baseline_samples", 0.0), metrics.get("event_samples", 0.0)) / 6.0),
        ]
        if association is not None:
            components.append(max(0.0, min(1.0, association)))
        else:
            components.append(0.7)
        components.append(1.0 - min(1.0, abs(noise) / (level * 2.0)))
        return max(0.0, min(1.0, sum(components) / len(components)))


# --------------------------------------------------------------------------- #
# Learning gate v2 (L4, design §9 / HB §13.1).
#
# The legacy ``SampleFilter`` above still serves the realtime sample path until
# Z2.  The v2 gate below operates on device-attributed ``LearningObservation``
# objects and applies the seven checks in design §9's order.  It is pure: every
# external input -- including T4's ``edges_confirmed_for`` callable and the
# observation's timestamp -- is injected, and it never imports runtime.fakes.
# --------------------------------------------------------------------------- #


class GateRejection(str, Enum):
    """Rejection reasons of the v2 gate, in design §9 check order."""

    MEASUREMENT_QUALITY = "measurement_quality"
    GROUP_NOT_CONFIRMED = "group_not_confirmed"
    NON_IDENTIFIABLE = "non_identifiable"
    UNKNOWN_ALTERNATIVE_SHARE = "unknown_alternative_share"
    RELATIONSHIP_SUSPECTED_CHANGE = "relationship_suspected_change"
    NOT_INDEPENDENT = "not_independent"
    MODEL_INCONSISTENT = "model_inconsistent"

    @classmethod
    def order(cls) -> tuple[GateRejection, ...]:
        """The ordered rejection ledger (design §9)."""
        return (
            cls.MEASUREMENT_QUALITY,
            cls.GROUP_NOT_CONFIRMED,
            cls.NON_IDENTIFIABLE,
            cls.UNKNOWN_ALTERNATIVE_SHARE,
            cls.RELATIONSHIP_SUSPECTED_CHANGE,
            cls.NOT_INDEPENDENT,
            cls.MODEL_INCONSISTENT,
        )


#: Backward-compatible ordered ledger; mirrors ``GateRejection.order()``.
_GATE_CHECK_ORDER: tuple[GateRejection, ...] = GateRejection.order()


@dataclass(slots=True)
class GateInputs:
    """External evidence the gate needs, injected by the caller.

    ``edges_confirmed_for`` is T4's per-subject relationship-confirmation
    callable.  It is injected as a plain callable so production depends only on
    that seam, not on T4's not-yet-merged module.  ``edges_confirmed`` is a
    scalar fallback for callers that have already resolved it for the subject;
    when both are present the callable wins.
    """

    group_status: EventGroupStatus | str
    measurement_quality_ok: bool = True
    unknown_share: float | None = None
    edges_confirmed: bool | None = None
    relationship_suspected: bool = False
    independent_event: bool = True
    model_consistent: bool = True
    model_abs_error_w: float | None = None
    model_sigma_w: float | None = None
    edges_confirmed_for: Callable[[str], bool] | None = None


@dataclass(slots=True)
class GateDecision:
    """Outcome of one gate evaluation."""

    accepted: bool
    status: str = "confirmed"
    rejection_reason: GateRejection | None = None
    detail: str = ""
    group_only: bool = False

    def apply(self, observation: LearningObservation) -> LearningObservation:
        """Return a copy of ``observation`` stamped with this decision."""
        updated = observation.model_copy(deep=True)
        updated.status = self.status
        updated.rejection_reason = (
            self.rejection_reason.value if self.rejection_reason is not None else None
        )
        note = f"gate:{self.status}" if self.accepted else f"gate_rejected:{self.rejection_reason.value}"
        updated.attributes = {**updated.attributes, "gate_note": note}
        return updated


_NON_INDIVIDUAL_IDENTIFIABILITY: frozenset[str] = frozenset(
    {
        IdentifiabilityStatus.NON_IDENTIFIABLE.value,
        IdentifiabilityStatus.INSUFFICIENT_EVIDENCE.value,
        IdentifiabilityStatus.GROUP_IDENTIFIABLE.value,
        IdentifiabilityStatus.PARTIALLY_IDENTIFIABLE.value,
    }
)

_CONFIRMED_GROUP_STATUSES: frozenset[str] = frozenset(
    {EventGroupStatus.CONFIRMED.value, EventGroupStatus.REVISED.value}
)


def evaluate_gate(
    observation: LearningObservation,
    inputs: GateInputs,
    *,
    clock: Callable[[], datetime],
    thresholds: LearningThresholds | None = None,
) -> GateDecision:
    """Apply the v2 learning gate in design §9 order.

    The gate never raises on a malformed observation: one bad observation must
    not stop a batch (spec §21).  A missing numeric measurement is rejected as
    ``measurement_quality`` rather than silently treated as zero (HB §18).
    ``clock`` is injected for future time-based checks and to keep the entry
    point free of wall-clock reads; the observation's own ``created_at`` is the
    immutable evidence timestamp and is never re-derived.
    """
    t = thresholds or LearningThresholds()
    _ = clock  # injected for symmetry with the rest of v3; no wall-clock reads.

    group_status = _name(inputs.group_status)
    checks: dict[GateRejection, tuple[bool, str, str]] = {}

    # 1. Measurement quality -- a missing delta is not a zero delta.
    measurements_ok = bool(inputs.measurement_quality_ok) and observation.delta_w is not None
    if observation.delta_w is None:
        mq_detail = "delta_missing"
    elif not inputs.measurement_quality_ok:
        mq_detail = "measurement_quality_failed"
    else:
        mq_detail = "ok"
    checks[GateRejection.MEASUREMENT_QUALITY] = (
        not measurements_ok,
        mq_detail,
        "rejected",
    )

    # 2. Group CONFIRMED (not PROVISIONAL).
    group_ok = group_status in _CONFIRMED_GROUP_STATUSES
    checks[GateRejection.GROUP_NOT_CONFIRMED] = (
        not group_ok,
        f"group_status={group_status}",
        "provisional" if not group_ok else "confirmed",
    )

    # 3. Subject IDENTIFIABLE -- otherwise the observation feeds the group only.
    ident = _name(observation.identifiability)
    non_individual = ident in _NON_INDIVIDUAL_IDENTIFIABILITY
    checks[GateRejection.NON_IDENTIFIABLE] = (
        non_individual,
        f"identifiability={ident}",
        "confirmed",
    )

    # 4. Unknown-alternative share must be below the configured ceiling.
    share = inputs.unknown_share
    share_bad = share is not None and share >= t.max_unknown_share
    checks[GateRejection.UNKNOWN_ALTERNATIVE_SHARE] = (
        share_bad,
        "unknown_share>=limit" if share_bad else "unknown_share_ok",
        "rejected",
    )

    # 5. Relationship edges not SUSPECTED_CHANGE (T4's per-subject callable).
    # A declared suspicion and a confirmed-false relationship are equivalent:
    # neither may update a confirmed model, both are held PROVISIONAL.
    relationship_bad = bool(inputs.relationship_suspected) or (
        inputs.edges_confirmed is not None and not inputs.edges_confirmed
    )
    if inputs.edges_confirmed_for is not None:
        # The per-subject callable is authoritative for relationship validity;
        # an explicit SUSPECTED_CHANGE declaration still holds provisional.
        relationship_bad = relationship_bad or not inputs.edges_confirmed_for(
            observation.subject_id
        )
    checks[GateRejection.RELATIONSHIP_SUSPECTED_CHANGE] = (
        relationship_bad,
        "relationship_suspected_change" if relationship_bad else "relationship_confirmed",
        "provisional" if relationship_bad else "confirmed",
    )

    # 6. Independent event (no unexplained sibling sub-event on the same readers).
    checks[GateRejection.NOT_INDEPENDENT] = (
        not inputs.independent_event,
        "concurrent_unexplained_sub_event" if not inputs.independent_event else "independent",
        "rejected",
    )

    # 7. Consistency with the active model within ``sigma`` sigmas.
    consistent = bool(inputs.model_consistent)
    error = inputs.model_abs_error_w
    sigma = inputs.model_sigma_w
    if error is not None and sigma is not None:
        consistent = error <= t.model_consistency_sigma * abs(sigma)
    checks[GateRejection.MODEL_INCONSISTENT] = (
        not consistent,
        "outside_model_interval" if not consistent else "within_model_interval",
        "rejected",
    )

    decided: GateRejection | None = None
    detail = ""
    status = "rejected"
    for reason in _GATE_CHECK_ORDER:
        failed, reason_detail, failed_status = checks[reason]
        if failed:
            decided = reason
            detail = reason_detail
            # A group still PROVISIONAL or a relationship in SUSPECTED_CHANGE
            # is *held* PROVISIONAL (retained for later reconciliation), not
            # rejected outright.  Hard failures keep status ``rejected``.
            status = failed_status
            break

    if decided is GateRejection.NON_IDENTIFIABLE:
        return GateDecision(
            accepted=False,
            status="rejected",
            rejection_reason=decided,
            detail=detail,
            group_only=True,
        )

    accepted = decided is None
    return GateDecision(
        accepted=accepted,
        status="confirmed" if accepted else status,
        rejection_reason=decided,
        detail=detail,
    )


def _name(value: Any) -> str:
    return str(getattr(value, "value", value)) if value is not None else ""


def rejection_summary(samples: Sequence[LearningSample]) -> dict[str, int]:
    """Count samples per rejection reason (plus ``accepted``)."""
    out: dict[str, int] = {}
    for sample in samples:
        if sample.accepted:
            key = "accepted"
        elif sample.rejection_reason is None:
            key = "rejected_no_reason"
        else:
            key = sample.rejection_reason.value if hasattr(sample.rejection_reason, "value") else str(sample.rejection_reason)
        out[key] = out.get(key, 0) + 1
    return out


__all__ = [
    "GateDecision",
    "GateInputs",
    "GateRejection",
    "SampleDecision",
    "SampleEvidence",
    "SampleFilter",
    "evaluate_gate",
    "rejection_summary",
    "sample_notes",
]
