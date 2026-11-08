"""End-to-end learning pipeline (interface contract §3.5).

``LearningPipeline`` wires collector → filter → trainer → validator → promoter
and persists accepted/rejected samples.  Each public method is defensive: one
bad sample or one device with corrupt state must not stop the rest of the house
(spec §21).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from ..contracts import (
    DeviceModelVersion,
    LearningSample,
    ModelStatus,
    utcnow,
)
from ..models.base import enum_text
from ..models.registry import ModelRegistry
from ..storage.database import Database
from .promotion import PromotionDecision, PromotionManager
from .sample_collector import SampleCollector
from .sample_filter import SampleFilter
from .thresholds import LearningThresholds
from .trainer import Trainer, TrainingResult

_LOGGER = logging.getLogger(__name__)


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

    # -- sample collection ------------------------------------------------ #
    def consider(self, events: Sequence[Any], window: Any) -> list[LearningSample]:
        """Build, filter and persist candidate samples for ``events``.

        Returns the *accepted* samples.  Rejected samples are persisted with
        their reason and are also available through ``last_rejected``.
        """
        candidates = self.collector.consider(events, window)
        self.last_rejected = []
        accepted: list[LearningSample] = []
        for sample in candidates:
            try:
                decision = self.filter.evaluate(sample)
            except Exception as exc:  # pragma: no cover - defensive
                _LOGGER.warning("sample filter failed: %s", exc)
                continue
            annotated = decision.apply(sample)
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
