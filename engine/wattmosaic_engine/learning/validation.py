"""Hold-out validation, error metrics, stability and model comparison."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ..contracts import DeviceModelVersion, LearningSample
from ..models.base import error_metrics, model_class_for
from .thresholds import LearningThresholds


@dataclass(slots=True)
class ValidationReport:
    metrics: dict[str, float] = field(default_factory=dict)
    fold_r2: list[float] = field(default_factory=list)
    stable: bool = True
    notes: list[str] = field(default_factory=list)

    @property
    def r2(self) -> float:
        return float(self.metrics.get("r2", 0.0))

    @property
    def mae(self) -> float:
        return float(self.metrics.get("mae", 0.0))

    @property
    def rmse(self) -> float:
        return float(self.metrics.get("rmse", 0.0))


def split_holdout(
    samples: Sequence[LearningSample],
    holdout_fraction: float = 0.25,
    min_validation: int = 3,
) -> tuple[list[LearningSample], list[LearningSample]]:
    """Time-ordered split: earliest samples train, latest validate.

    A random split leaks information across a time series (nearby samples are
    highly correlated), so we always hold out the *tail*.  If the tail is too
    small we grow it to ``min_validation`` samples while keeping at least one
    training sample.
    """
    ordered = sorted((s for s in samples if s.accepted), key=lambda s: (s.captured_at, s.sample_id))
    if len(ordered) < 2:
        return ordered, []
    holdout_count = max(min_validation, int(math.ceil(len(ordered) * max(0.0, min(0.9, holdout_fraction)))))
    holdout_count = min(holdout_count, len(ordered) - 1)
    split = len(ordered) - holdout_count
    return ordered[:split], ordered[split:]


def _r2(actual: Sequence[float], predicted: Sequence[float]) -> float:
    return float(error_metrics(actual, predicted).get("r2", 0.0))


def validate_model(
    model: Any,
    samples: Sequence[LearningSample],
    thresholds: LearningThresholds | None = None,
    *,
    training_samples: Sequence[LearningSample] | None = None,
) -> ValidationReport:
    """Validate ``model`` on held-out ``samples`` and check temporal stability.

    Stability is checked by splitting the held-out samples into
    ``thresholds.stability_folds`` contiguous time folds and computing R² per
    fold.  A large spread means the model fits one period well and another
    poorly (e.g. a seasonal or one-off cycle), which must not be promoted
    automatically.
    """
    cfg = thresholds or LearningThresholds()
    evaluated = [s for s in samples if s.accepted]
    if not evaluated:
        return ValidationReport(metrics=error_metrics([], []), stable=False, notes=["no_validation_samples"])

    actual: list[float] = []
    predicted: list[float] = []
    from ..models.base import context_from_sample

    errors: list[str] = []
    for sample in evaluated:
        try:
            predicted.append(float(model.predict(context_from_sample(sample))))
            actual.append(float(sample.event_power_w))
        except Exception as exc:  # isolate a single bad context
            errors.append(f"predict_failed:{type(exc).__name__}")
    metrics = error_metrics(actual, predicted)
    metrics["sample_count"] = float(len(evaluated))
    if training_samples is not None:
        metrics["training_sample_count"] = float(len(training_samples))

    ordered = sorted(evaluated, key=lambda s: (s.captured_at, s.sample_id))
    folds = max(2, int(cfg.stability_folds))
    fold_r2: list[float] = []
    if len(ordered) >= folds * 2:
        chunk = math.ceil(len(ordered) / folds)
        for start in range(0, len(ordered), chunk):
            fold = ordered[start : start + chunk]
            if len(fold) < 2:
                continue
            fold_actual: list[float] = []
            fold_pred: list[float] = []
            for sample in fold:
                try:
                    fold_pred.append(float(model.predict(context_from_sample(sample))))
                    fold_actual.append(float(sample.event_power_w))
                except Exception:
                    continue
            if len(fold_actual) >= 2:
                fold_r2.append(_r2(fold_actual, fold_pred))

    stable = True
    notes = list(errors)
    if fold_r2:
        spread = max(fold_r2) - min(fold_r2)
        metrics["r2_fold_min"] = float(min(fold_r2))
        metrics["r2_fold_max"] = float(max(fold_r2))
        metrics["r2_fold_spread"] = float(spread)
        if spread > cfg.max_r2_fold_spread:
            stable = False
            notes.append(f"unstable_across_time:r2_spread={spread:.2f}>{cfg.max_r2_fold_spread:.2f}")
        if min(fold_r2) < cfg.min_validation_r2 - cfg.promotion_margin:
            stable = False
            notes.append(f"weak_fold:min_r2={min(fold_r2):.2f}<{cfg.min_validation_r2 - cfg.promotion_margin:.2f}")
    else:
        notes.append("stability_not_estimable:too_few_folds")
        # Not a hard failure; promotion additionally checks sample count.
    if metrics.get("n", 0) < cfg.min_validation_samples:
        notes.append(f"insufficient_validation_samples:{int(metrics.get('n', 0))}<{cfg.min_validation_samples}")

    return ValidationReport(metrics=metrics, fold_r2=fold_r2, stable=stable, notes=notes)


def compare_models(
    candidate_metrics: dict[str, float],
    active_metrics: dict[str, float] | None,
    thresholds: LearningThresholds | None = None,
) -> dict[str, Any]:
    """Compare candidate vs active model on the same validation set.

    Returns ``better`` (candidate R² > active R² + margin), the delta, and a
    human-readable reason.  When there is no active model the candidate wins by
    default (subject to absolute quality gates applied by the promoter).
    """
    cfg = thresholds or LearningThresholds()
    candidate_r2 = float(candidate_metrics.get("r2", 0.0))
    candidate_mae = float(candidate_metrics.get("mae", float("inf")))
    if not active_metrics:
        return {
            "better": True,
            "delta_r2": candidate_r2,
            "delta_mae": None,
            "reason": "no_active_model",
            "candidate_r2": candidate_r2,
        }
    active_r2 = float(active_metrics.get("r2", 0.0))
    active_mae = float(active_metrics.get("mae", float("inf")))
    delta = candidate_r2 - active_r2
    better = delta >= cfg.promotion_margin
    # MAE is a secondary guard: do not promote a model that improves R² by
    # overfitting a few points while being worse in absolute watts.
    if better and active_mae not in (float("inf"),) and candidate_mae > active_mae * 1.10:
        better = False
        reason = f"mae_worse:candidate={candidate_mae:.1f}>active={active_mae:.1f}"
    else:
        reason = (
            f"r2_improved:{delta:.3f}>={cfg.promotion_margin:.2f}"
            if better
            else f"r2_improvement_insufficient:{delta:.3f}<{cfg.promotion_margin:.2f}"
        )
    return {
        "better": better,
        "delta_r2": delta,
        "delta_mae": candidate_mae - active_mae if active_mae != float("inf") else None,
        "reason": reason,
        "candidate_r2": candidate_r2,
        "active_r2": active_r2,
    }


def rehydrate_and_validate(
    version: DeviceModelVersion,
    samples: Sequence[LearningSample],
    thresholds: LearningThresholds | None = None,
) -> ValidationReport:
    """Re-build the model object from persisted parameters, then validate it."""
    model = model_class_for(version.model_type)(
        version.parameters,
        validation_metrics=version.validation_metrics,
        confidence=version.confidence or None,
    )
    return validate_model(model, samples, thresholds)


__all__ = [
    "ValidationReport",
    "compare_models",
    "rehydrate_and_validate",
    "split_holdout",
    "validate_model",
]
