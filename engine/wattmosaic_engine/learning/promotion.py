"""Validation-gated promotion, manual review and rollback.

Rules (spec §11.4, §24):

1. A candidate is only promoted when **all** of these hold:
   * it is not blocked by a frozen model,
   * its validation R² ≥ ``min_validation_r2``,
   * its validation set is large enough,
   * its stability check passed,
   * it beats the active model's R² by ≥ ``promotion_margin`` (or there is no
     active model at all).
2. Otherwise the candidate is preserved with ``ModelStatus.CANDIDATE`` and a
   ``manual_review_required:*`` note.  The active model keeps serving.
3. Rollback restores the previous *active* version and marks the superseded
   one ``ROLLED_BACK``.  Version history is never deleted.

A frozen model blocks promotion entirely (``ModelStatus.FROZEN`` is written to
the candidate as well, so operators see why it stayed).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ..contracts import DeviceModelVersion, LearningSample, ModelStatus
from ..models.base import enum_text
from ..models.registry import ModelRegistry
from ..models.serde import version_from_row, versions_from_rows
from ..storage.database import Database
from .thresholds import LearningThresholds
from .validation import ValidationReport, compare_models, validate_model

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class PromotionDecision:
    promoted: bool
    reason: str
    candidate_version: int | None = None
    active_version: int | None = None
    validation: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    manual_review: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "promoted": self.promoted,
            "reason": self.reason,
            "candidate_version": self.candidate_version,
            "active_version": self.active_version,
            "validation": dict(self.validation),
            "notes": list(self.notes),
            "manual_review": self.manual_review,
        }


class PromotionManager:
    """Promotes / rejects / rolls back persisted model versions."""

    def __init__(
        self,
        database: Database,
        registry: ModelRegistry | None = None,
        thresholds: LearningThresholds | None = None,
    ) -> None:
        self._db = database
        self._registry = registry or ModelRegistry(database)
        self.thresholds = thresholds or LearningThresholds()

    # -- queries ---------------------------------------------------------- #
    def active(self, device_id: str) -> DeviceModelVersion | None:
        row = self._db.active_model(device_id)
        return version_from_row(row) if row else None

    def versions(self, device_id: str) -> list[DeviceModelVersion]:
        return versions_from_rows(self._db.model_versions(device_id))

    def frozen(self, device_id: str) -> bool:
        return self._registry.is_frozen(device_id)

    # -- validation + promotion ------------------------------------------- #
    def validate(
        self,
        candidate: DeviceModelVersion,
        samples: Sequence[LearningSample],
    ) -> ValidationReport:
        from ..models.base import build_model

        model = build_model(
            candidate.model_type,
            candidate.parameters,
            validation_metrics=candidate.validation_metrics,
            confidence=candidate.confidence or None,
        )
        report = validate_model(model, samples, self.thresholds)
        candidate.validation_metrics = dict(report.metrics)
        candidate.notes = [*candidate.notes, *[n for n in report.notes if n not in candidate.notes]]
        return report

    def validate_and_promote(
        self,
        candidate: DeviceModelVersion,
        samples: Sequence[LearningSample] | None = None,
    ) -> PromotionDecision:
        """Validate (when samples are supplied) then promote or flag for review."""
        device_id = candidate.device_id
        notes: list[str] = []
        active = self.active(device_id)

        if candidate.status == ModelStatus.REJECTED.value:
            return PromotionDecision(
                promoted=False,
                reason="candidate_rejected",
                candidate_version=candidate.version,
                active_version=active.version if active else None,
                notes=list(candidate.notes),
            )

        if self.frozen(device_id):
            notes.append("blocked_by_frozen_model")
            candidate.notes = [*candidate.notes, *notes]
            self._db.save_model_version(candidate, activate=False)
            self._registry.invalidate(device_id)
            return PromotionDecision(
                promoted=False,
                reason="frozen",
                candidate_version=candidate.version,
                active_version=active.version if active else None,
                validation=dict(candidate.validation_metrics),
                notes=notes,
                manual_review=True,
            )

        report: ValidationReport | None = None
        if samples is not None:
            report = self.validate(candidate, samples)

        metrics = dict(candidate.validation_metrics)
        if report is not None and not report.stable:
            notes.append("validation_unstable")
            candidate.notes = [*candidate.notes, *[n for n in notes if n not in candidate.notes]]
            self._db.save_model_version(candidate, activate=False)
            self._db.set_model_status(
                device_id, enum_text(candidate.model_type), candidate.version, ModelStatus.CANDIDATE
            )
            self._registry.invalidate(device_id)
            return PromotionDecision(
                promoted=False,
                reason="manual_review_required:unstable",
                candidate_version=candidate.version,
                active_version=active.version if active else None,
                validation=metrics,
                notes=[*notes, *report.notes],
                manual_review=True,
            )

        sample_n = float(metrics.get("sample_count", 0.0))
        if sample_n < self.thresholds.min_validation_samples:
            notes.append(
                f"insufficient_validation_samples:{int(sample_n)}<{self.thresholds.min_validation_samples}"
            )
            return self._review(candidate, active, metrics, notes)

        candidate_r2 = float(metrics.get("r2", 0.0))
        if candidate_r2 < self.thresholds.min_validation_r2:
            notes.append(
                f"r2_below_threshold:{candidate_r2:.3f}<{self.thresholds.min_validation_r2:.2f}"
            )
            return self._review(candidate, active, metrics, notes)

        comparison = compare_models(
            metrics,
            active.validation_metrics if active is not None else None,
            self.thresholds,
        )
        if not comparison["better"]:
            notes.append(comparison["reason"])
            return self._review(candidate, active, metrics, notes)

        notes.append(comparison["reason"])
        candidate.status = ModelStatus.ACTIVE
        candidate.confidence = max(candidate.confidence, min(1.0, candidate_r2))
        candidate.notes = [*candidate.notes, *[n for n in notes if n not in candidate.notes]]
        self._db.save_model_version(candidate, activate=True)
        if active is not None and active.version != candidate.version:
            # Keep the superseded version as history, not ACTIVE.
            self._db.set_model_status(
                device_id, enum_text(active.model_type), active.version, ModelStatus.CANDIDATE
            )
        self._db.set_model_status(
            device_id, enum_text(candidate.model_type), candidate.version, ModelStatus.ACTIVE
        )
        self._registry.invalidate(device_id)
        return PromotionDecision(
            promoted=True,
            reason="promoted",
            candidate_version=candidate.version,
            active_version=candidate.version,
            validation=metrics,
            notes=notes,
        )

    def _review(
        self,
        candidate: DeviceModelVersion,
        active: DeviceModelVersion | None,
        metrics: dict[str, float],
        notes: list[str],
    ) -> PromotionDecision:
        candidate.status = ModelStatus.CANDIDATE
        marker = "manual_review_required:" + (notes[-1] if notes else "quality")
        candidate.notes = [*candidate.notes, *[n for n in notes if n not in candidate.notes], marker]
        self._db.save_model_version(candidate, activate=False)
        self._db.set_model_status(
            candidate.device_id, enum_text(candidate.model_type), candidate.version, ModelStatus.CANDIDATE
        )
        self._registry.invalidate(candidate.device_id)
        return PromotionDecision(
            promoted=False,
            reason=marker,
            candidate_version=candidate.version,
            active_version=active.version if active else None,
            validation=metrics,
            notes=notes,
            manual_review=True,
        )

    # -- rollback / freeze ------------------------------------------------- #
    def rollback(self, device_id: str, model_type: str | None = None) -> int | None:
        """Restore the previously active version.

        Rollback only makes sense when the *newest* version of that model type is
        the one currently active.  If the newest version is a pending candidate
        (never promoted) we do nothing — it was never serving traffic, so
        "rolling it back" would corrupt the version history.
        """
        versions = self.versions(device_id)
        if model_type is None:
            active = next((v for v in versions if v.status == ModelStatus.ACTIVE.value), None)
            if active is None:
                return None
            model_type = enum_text(active.model_type)
        typed = [v for v in versions if enum_text(v.model_type) == model_type]
        if not typed:
            return None
        typed.sort(key=lambda v: v.version, reverse=True)
        active = next((v for v in typed if v.status == ModelStatus.ACTIVE.value), None)
        if active is None:
            return None
        if typed[0].version != active.version:
            # Newest is a candidate; there is nothing to roll back from.
            return None
        restored = self._db.rollback_model(device_id, model_type)
        if restored is None:
            return None
        self._db.set_model_status(device_id, model_type, restored, ModelStatus.ACTIVE)
        self._registry.invalidate(device_id)
        return restored

    def freeze(self, device_id: str, frozen: bool = True) -> None:
        self._db.set_model_frozen(device_id, frozen)
        self._registry.invalidate(device_id)

    def reject(self, candidate: DeviceModelVersion, reason: str = "rejected") -> None:
        candidate.status = ModelStatus.REJECTED
        if reason not in candidate.notes:
            candidate.notes = [*candidate.notes, reason]
        self._db.save_model_version(candidate, activate=False)
        self._db.set_model_status(
            candidate.device_id, enum_text(candidate.model_type), candidate.version, ModelStatus.REJECTED
        )
        self._registry.invalidate(candidate.device_id)


__all__ = ["PromotionDecision", "PromotionManager"]
