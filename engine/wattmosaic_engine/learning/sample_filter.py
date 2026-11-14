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
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..contracts import LearningSample, SampleRejection, utcnow
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
        topology_bad = (
            not ev.topology_confirmed
            or topology_quality in {"candidate", "ambiguous", "unconfirmed", "unknown"}
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
    "SampleDecision",
    "SampleEvidence",
    "SampleFilter",
    "rejection_summary",
    "sample_notes",
]
