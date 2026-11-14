"""End-to-end learning pipeline (interface contract §3.5).

``LearningPipeline`` wires collector → filter → trainer → validator → promoter
and persists accepted/rejected samples.  Each public method is defensive: one
bad sample or one device with corrupt state must not stop the rest of the house
(spec §21).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from ..contracts import (
    DeviceModelVersion,
    EventIsolation,
    HaEntityBinding,
    LearningSample,
    ModelStatus,
    utcnow,
)
from ..disaggregation.isolation import (
    EventIsolator,
    IsolationConfig,
    IsolationWindow,
    SampleCoverage,
)
from ..models.base import enum_text
from ..models.protocols import window_time as _window_time
from ..models.registry import ModelRegistry
from ..storage.database import Database
from .promotion import PromotionDecision, PromotionManager
from .reporting import FILTER_ERROR_LABEL, LearningAccounting
from .sample_collector import SampleCollector
from .sample_filter import SampleEvidence, SampleFilter, sample_notes
from .thresholds import LearningThresholds
from .trainer import Trainer, TrainingResult

_LOGGER = logging.getLogger(__name__)


def _event_device(event: Any) -> str | None:
    raw = event.get("device_id") if isinstance(event, Mapping) else getattr(event, "device_id", None)
    return None if raw is None else str(raw)


def _event_time(event: Any) -> datetime | None:
    raw = (
        event.get("occurred_at")
        if isinstance(event, Mapping)
        else getattr(event, "occurred_at", None)
    )
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
    return None


def _neighbourhood(events: Sequence[Any], window: Any) -> list[Any]:
    """All known transitions around this batch, deduplicated by event id.

    ``consider`` is called with the events that have matured for learning.
    The aligned inference window additionally carries the transitions observed
    in the *current* cycle (``main.py`` attaches them), and a sibling event may
    have matured in an earlier batch.  Both are legitimate competitors, so the
    isolator sees the union rather than only this call's slice.  Identity is the
    event id when present; duplicates are dropped so one transition is never
    counted twice.
    """
    seen: dict[str, Any] = {}
    ordered: list[Any] = []
    candidates = list(events)
    transitions = _field(window, "state_transitions") if window is not None else None
    if transitions:
        candidates.extend(transitions)
    for item in candidates:
        key = _event_id(item)
        if key is not None:
            if key in seen:
                continue
            seen[key] = item
        ordered.append(item)
    return ordered


def _field(obj: Any, name: str) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


def _event_id(event: Any) -> str | None:
    raw = _field(event, "event_id")
    return None if raw in (None, "") else str(raw)


def _coverage_from_sample(sample: LearningSample) -> SampleCoverage:
    """Read the collector's own sample counts back out of the notes.

    The collector already recorded how many readings the baseline and event
    windows held; re-reading them here keeps one measurement path instead of a
    second, guessed one.
    """
    notes = sample_notes(sample)
    return SampleCoverage(
        baseline_samples=_int_or_none(notes.get("baseline_samples")),
        event_samples=_int_or_none(notes.get("event_samples")),
    )


def _int_or_none(value: Any) -> int | None:
    try:
        return None if value is None else int(float(value))
    except (TypeError, ValueError):
        return None


class LearningPipeline:
    """Coordinates sample collection, training, validation and promotion."""

    def __init__(
        self,
        database: Database,
        *,
        thresholds: LearningThresholds | None = None,
        registry: ModelRegistry | None = None,
        cadence_s: float | None = None,
    ) -> None:
        self._db = database
        self.thresholds = thresholds or LearningThresholds()
        self.collector = SampleCollector(self.thresholds, cadence_s=cadence_s)
        self.filter = SampleFilter(self.thresholds)
        self.trainer = Trainer(self.thresholds)
        self.registry = registry or ModelRegistry(database)
        self.promoter = PromotionManager(database, self.registry, self.thresholds)
        #: Monotonic accepted/rejected accounting for the "is learning actually
        #: learning?" question.  O(1) per sample, no per-sample storage.
        self.accounting = LearningAccounting()
        #: Rejected samples from the last ``consider`` call (kept for callers
        #: that report one batch; the accounting above is cumulative).
        self.last_rejected: list[LearningSample] = []
        #: Event isolation verdicts from the last ``consider`` call, keyed by
        #: ``sample_id``.  Retained (never dropped) so a contaminated event can
        #: be audited instead of silently disappearing.
        self.last_isolations: dict[str, EventIsolation] = {}
        #: Entity bindings the isolator uses to collapse many HA entities of one
        #: appliance into one physical device.  Empty by default: an unbound
        #: entity then counts as load evidence, which is the safe default.
        self.entity_bindings: list[HaEntityBinding] = []
        self._isolator = self._build_isolator()

    # -- event isolation evidence ----------------------------------------- #
    def _isolation_config(self) -> IsolationConfig:
        """Translate the learning thresholds into isolator timings.

        The global ``concurrency_window_s`` is only the base layer; class and
        per-device overrides are resolved per sample inside the isolator.
        """
        t = self.thresholds
        base = IsolationWindow(
            analysis_offset_s=t.analysis_offset_s,
            isolation_window_s=t.concurrency_window_s,
            power_window_s=t.power_analysis_window_s,
        )
        return IsolationConfig(
            analysis_offset_s=base.analysis_offset_s,
            isolation_window_s=base.isolation_window_s,
            power_window_s=base.analysis_window_s,
            min_baseline_samples=t.min_baseline_samples,
            min_event_samples=t.min_event_samples,
            defaults={
                str(label): IsolationWindow.merged(base, values)
                for label, values in t.isolation_by_class.items()
            },
            per_device={
                str(device): IsolationWindow.merged(base, values)
                for device, values in t.isolation_by_device.items()
            },
        )

    def _build_isolator(self) -> EventIsolator:
        return EventIsolator(
            self._isolation_config(),
            bindings=self.entity_bindings,
            device_classes=self.thresholds.isolation_device_classes,
        )

    def set_entity_bindings(self, bindings: Sequence[HaEntityBinding]) -> None:
        """Declare the typed HA entity roles used to collapse one appliance.

        Without bindings an unbound entity still counts as load evidence, so a
        caller that never declares roles gets the conservative behaviour: every
        entity is a potential device change.
        """
        self.entity_bindings = list(bindings)
        self._isolator = self._build_isolator()

    def isolation_evidence(
        self, sample: LearningSample, events: Sequence[Any], window: Any = None
    ) -> SampleEvidence:
        """Build the :class:`SampleEvidence` the filter must see.

        ``concurrent_events`` is the number of **other physical devices** that
        changed inside this device's isolation window -- not the number of HA
        entities and not a whole-window transition count.  This is the repair
        for the dead guard: before WP4 the caller passed no evidence at all, so
        ``SampleEvidence.concurrent_events`` stayed ``0`` and
        ``SampleRejection.CONCURRENT_EVENTS`` could not fire in production.
        """
        timings = self.thresholds.isolation_timings(sample.device_id)
        now = _window_time(window) if window is not None else utcnow()
        source = self._source_event(sample, events)
        evidence = SampleEvidence(
            now=now,
            concurrency_window_s=float(timings["isolation_window_s"]),
        )
        if source is None:
            return evidence
        try:
            isolation = self._isolator.classify(
                source,
                _neighbourhood(events, window),
                now=now,
                coverage=_coverage_from_sample(sample),
            )
        except Exception as exc:  # pragma: no cover - defensive
            _LOGGER.warning("event isolation failed for %s: %s", sample.sample_id, exc)
            return evidence
        self.last_isolations[sample.sample_id] = isolation
        # The guard asks a narrower question than the verdict does.  ``classify``
        # answers "was anything else changing in the window?"; the learning guard
        # must ask "could that change have moved *this sample's* measured delta?".
        # A device on a dedicated CT is attributable during a scene even though a
        # sibling changed -- rejecting it discards the only clean samples such a
        # device ever produces, which is why the concurrency guard must not
        # simply mirror ``competing_device_ids``.
        competing = self._interfering_devices(isolation)
        evidence.concurrent_events = len(competing)
        evidence.competing_device_ids = competing
        evidence.concurrency_window_s = float(timings["isolation_window_s"])
        return evidence

    @staticmethod
    def _interfering_devices(isolation: EventIsolation) -> list[str]:
        """Competitors whose own CT could have polluted this sample's delta.

        ``ct_separated_device_ids`` is the isolator's positive finding that a
        competitor names a CT disjoint from the subject's, so it cannot have
        moved the reading.  Everything else -- including competitors whose
        scope was never declared -- keeps the conservative treatment.
        """
        competing = list(isolation.competing_device_ids)
        if not competing:
            return []
        separated = set(isolation.evidence.get("ct_separated_device_ids") or ())
        if not separated:
            return competing
        return [device for device in competing if device not in separated]

    @staticmethod
    def _source_event(sample: LearningSample, events: Sequence[Any]) -> Any:
        """Find the event a sample was built from (device + event time)."""
        for candidate in events:
            if _event_device(candidate) != sample.device_id:
                continue
            occurred = _event_time(candidate)
            if occurred is None or occurred == sample.captured_at:
                return candidate
        return None

    # -- sample collection ------------------------------------------------ #
    def consider(self, events: Sequence[Any], window: Any) -> list[LearningSample]:
        """Build, filter and persist candidate samples for ``events``.

        Returns the *accepted* samples.  Rejected samples are persisted with
        their reason and are also available through ``last_rejected``.
        Isolation verdicts are kept in ``last_isolations`` (contaminated events
        are retained, only excluded from clean attribution).
        """
        candidates = self.collector.consider(events, window)
        self.last_rejected = []
        self.last_isolations = {}
        accepted: list[LearningSample] = []
        for sample in candidates:
            try:
                evidence = self.isolation_evidence(sample, events, window)
                decision = self.filter.evaluate(sample, evidence)
            except Exception as exc:  # pragma: no cover - defensive
                _LOGGER.warning("sample filter failed: %s", exc)
                self.accounting.record(False, FILTER_ERROR_LABEL)
                continue
            annotated = decision.apply(sample)
            self.accounting.record(annotated.accepted, annotated.rejection_reason)
            if not annotated.accepted:
                self.last_rejected.append(annotated)
            try:
                self._db.insert_learning_sample(annotated)
            except Exception as exc:
                _LOGGER.warning("failed to persist learning sample: %s", exc)
            if annotated.accepted:
                accepted.append(annotated)
        return accepted

    def ingest_samples(self, samples: Sequence[LearningSample]) -> tuple[list[LearningSample], list[LearningSample]]:
        """Filter and persist externally-built samples. Returns (accepted, rejected)."""
        accepted: list[LearningSample] = []
        rejected: list[LearningSample] = []
        for sample in samples:
            decision = self.filter.evaluate(sample)
            annotated = decision.apply(sample)
            self.accounting.record(annotated.accepted, annotated.rejection_reason)
            (accepted if annotated.accepted else rejected).append(annotated)
            try:
                self._db.insert_learning_sample(annotated)
            except Exception as exc:
                _LOGGER.warning("failed to persist learning sample: %s", exc)
        return accepted, rejected

    # -- training --------------------------------------------------------- #
    def train(
        self,
        device_id: str,
        samples: Sequence[LearningSample] | None = None,
        *,
        device: Any = None,
        model_type: Any = None,
        power_series: Any = None,
        force: bool = False,
    ) -> DeviceModelVersion | None:
        """Train a candidate if gating allows; returns the candidate version."""
        samples = list(samples) if samples is not None else self.stored_samples(device_id)
        versions = self.registry.versions(device_id)
        if not force:
            ready, reason = self.trainer.should_train(device_id, samples, versions)
            if not ready:
                return None
        result: TrainingResult = self.trainer.train(
            device_id,
            samples,
            device,
            model_type=model_type,
            versions=versions,
            power_series=power_series,
        )
        if result.version is None:
            return None
        result.version.status = ModelStatus.CANDIDATE
        self._db.save_model_version(result.version, activate=False)
        self._db.set_model_status(
            device_id, enum_text(result.version.model_type), result.version.version, ModelStatus.CANDIDATE
        )
        self.registry.invalidate(device_id)
        return result.version

    def validate_and_promote(
        self,
        candidate: DeviceModelVersion,
        samples: Sequence[LearningSample] | None = None,
    ) -> PromotionDecision:
        sample_set = list(samples) if samples is not None else self.stored_samples(candidate.device_id)
        accepted = [s for s in sample_set if s.accepted]
        # Validate on a time-ordered tail, consistent with training.
        from .validation import split_holdout

        _, holdout = split_holdout(
            accepted, self.thresholds.holdout_fraction, self.thresholds.min_validation_samples
        )
        decision = self.promoter.validate_and_promote(candidate, holdout or None)
        self.registry.invalidate(candidate.device_id)
        return decision

    def retrain_device(
        self,
        device_id: str,
        *,
        device: Any = None,
        model_type: Any = None,
        power_series: Any = None,
    ) -> tuple[DeviceModelVersion | None, PromotionDecision | None]:
        candidate = self.train(
            device_id,
            device=device,
            model_type=model_type,
            power_series=power_series,
        )
        if candidate is None:
            return None, None
        return candidate, self.validate_and_promote(candidate)

    # -- lifecycle -------------------------------------------------------- #
    def freeze(self, device_id: str, frozen: bool = True) -> None:
        self.promoter.freeze(device_id, frozen)

    def rollback(self, device_id: str, model_type: str | None = None) -> int | None:
        return self.promoter.rollback(device_id, model_type)

    # -- storage helpers --------------------------------------------------- #
    def stored_samples(self, device_id: str, accepted_only: bool = True) -> list[LearningSample]:
        rows = self._db.learning_samples(device_id, accepted_only=accepted_only)
        return [self._sample_from_row(row) for row in rows]

    @staticmethod
    def _sample_from_row(row: Mapping[str, Any]) -> LearningSample:
        import json
        from datetime import UTC

        from .sample_filter import SampleRejection

        def parse(value: Any) -> datetime:
            if isinstance(value, datetime):
                return value
            try:
                parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                return utcnow()
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

        def loads(value: Any, default: Any) -> Any:
            if isinstance(value, (list, dict)):
                return value
            try:
                return json.loads(value) if value else default
            except (TypeError, ValueError):
                return default

        reason_raw = row.get("rejection_reason")
        reason = None
        if reason_raw:
            try:
                reason = SampleRejection(str(reason_raw))
            except ValueError:
                reason = None
        return LearningSample(
            sample_id=str(row["sample_id"]),
            device_id=str(row["device_id"]),
            captured_at=parse(row.get("captured_at")),
            duration_s=float(row.get("duration_s") or 0.0),
            baseline_power_w=float(row.get("baseline_power_w") or 0.0),
            event_power_w=float(row.get("event_power_w") or 0.0),
            delta_power_w=float(row.get("delta_power_w") or 0.0),
            control_value=float(row["control_value"]) if row.get("control_value") is not None else None,
            control_variable=row.get("control_variable"),
            supporting_ct_ids=loads(row.get("supporting_ct_ids_json"), []),
            quality_score=float(row.get("quality_score") or 0.0),
            accepted=bool(row.get("accepted", 1)),
            rejection_reason=reason,
            notes=loads(row.get("notes_json"), []),
        )


__all__ = ["LearningPipeline"]
