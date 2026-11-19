"""Frozen-evidence validation and per-class acceptance criteria (L5).

Validation is keyed by observation id so a candidate and the active model are
always scored on the same frozen evidence set (DEC-32).  Per-class criteria
replace the single R² gate: a fixed load with a flat holdout and small MAE can
promote (DEF-03).  Pure: no database and no wall clock.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..contracts import ModelClass

# --------------------------------------------------------------------------- #
# L5: frozen-evidence validation, observation-id holdout, per-class criteria.
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class ClassCriteria:
    """Per-class acceptance thresholds from design §9.

    These live here (not in :mod:`thresholds`) so the v2 learner is additive
    and the legacy thresholds stay byte-identical for the old pipeline.
    """

    fixed_max_relative_mae: float = 0.15
    fixed_max_level_ci_width_ratio: float = 0.20
    adjustable_max_relative_mae: float = 0.15
    adjustable_min_coverage: float = 0.50
    dynamic_min_state_support: int = 5
    kernel_max_nrmse: float = 0.30
    min_frozen_samples: int = 5


@dataclass(slots=True)
class FrozenEvaluation:
    """Validation of one spec against its frozen evidence set."""

    metrics: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def split_by_observation_id(
    observations: Sequence[Any],
    holdout_fraction: float = 0.25,
    min_validation: int = 3,
) -> tuple[list[str], list[str]]:
    """Time-ordered holdout keyed by observation id (design §9 step 2).

    Observations are ordered by ``(created_at, observation_id)`` so the split is
    deterministic under replay; the *tail* is held out to avoid leaking nearby
    (correlated) samples across the split.  Returns the train and holdout id
    sets, not the objects themselves, so the evidence-id set can be recorded
    directly on a model version.
    """
    ordered = sorted(
        observations,
        key=lambda o: (getattr(o, "created_at", None) or datetime.min, str(getattr(o, "observation_id", ""))),
    )
    ids = [str(getattr(o, "observation_id", "")) for o in ordered]
    if len(ids) < 2:
        return ids, []
    count = max(min_validation, int(math.ceil(len(ids) * max(0.0, min(0.9, holdout_fraction)))))
    count = min(count, len(ids) - 1)
    return ids[: len(ids) - count], ids[len(ids) - count :]


def evidence_ids(observations: Sequence[Any]) -> frozenset[str]:
    """The frozen set of confirmed observation ids."""
    return frozenset(
        str(getattr(o, "observation_id", ""))
        for o in observations
        if _name(getattr(o, "status", "")) == "confirmed"
    )


def _name(value: Any) -> str:
    return str(getattr(value, "value", value)) if value is not None else ""


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and out not in (float("inf"), float("-inf")) else None


def evaluate_frozen(
    spec: Any,
    observations: Sequence[Any],
    model_class: Any,
) -> FrozenEvaluation:
    """Compute per-class validation metrics on the frozen evidence set.

    Every observation is predicted from the persisted ``spec``; absent
    predictions (HB §18) are excluded rather than treated as zero, which is
    exactly why a constant all-ON load is no longer trapped by R².
    """
    from .incremental import predict_spec

    actual: list[float] = []
    predicted: list[float] = []
    notes: list[str] = []
    for observation in observations:
        value = _finite(getattr(observation, "delta_w", None))
        prediction = predict_spec(spec, observation)
        if value is None or prediction is None:
            notes.append("unscorable_observation")
            continue
        actual.append(value)
        predicted.append(prediction)

    metrics: dict[str, float] = {"n": float(len(actual))}
    if not actual:
        notes.append("no_scorable_evidence")
        return FrozenEvaluation(metrics=metrics, notes=notes)

    errors = [p - a for a, p in zip(actual, predicted, strict=True)]
    abs_errors = [abs(e) for e in errors]
    mae = sum(abs_errors) / len(abs_errors)
    rmse = math.sqrt(sum(e * e for e in errors) / len(errors))
    mean_abs = sum(abs(a) for a in actual) / len(actual)
    mean_actual = sum(actual) / len(actual)
    ss_tot = sum((a - mean_actual) ** 2 for a in actual)
    ss_res = sum(e * e for e in errors)
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 1e-12 else (1.0 if ss_res <= 1e-12 else 0.0)
    span = max(actual) - min(actual)
    metrics.update(
        {
            "mae": mae,
            "rmse": rmse,
            "r2": r2,
            "mean_abs_w": mean_abs,
            "relative_mae": mae / mean_abs if mean_abs > 1e-12 else 0.0,
            "span": span,
            "nrmse": rmse / span if span > 1e-12 else 0.0,
            "max_error": max(abs_errors),
        }
    )

    kind = _name(model_class)
    if kind == ModelClass.FIXED.value:
        levels = getattr(spec, "levels", {}) or {}
        stats = [s for s in levels.values() if s.median_w]
        if stats and any(s.median_w for s in stats):
            medians = [float(s.median_w) for s in stats if s.median_w is not None]
            mads = [float(s.mad_w) for s in stats if s.mad_w is not None]
            numerator = 2.0 * 1.4826 * (sum(mads) / len(mads) if mads else 0.0)
            denominator = sum(medians) / len(medians)
            metrics["level_ci_width_ratio"] = (
                numerator / denominator if denominator > 1e-12 else float("inf")
            )
    elif kind == ModelClass.ADJUSTABLE.value:
        xs = list(getattr(spec, "knots_x", []) or [])
        controls = [
            _finite((getattr(o, "attributes", {}) or {}).get("control_value"))
            for o in observations
        ]
        covered = [c for c in controls if c is not None and xs and xs[0] <= c <= xs[-1]]
        metrics["coverage"] = len(covered) / len(controls) if controls else 0.0
    elif kind == ModelClass.DYNAMIC.value:
        states = getattr(spec, "states", {}) or {}
        metrics["min_state_support"] = float(min((s.n for s in states.values()), default=0))
        # A Gaussian held-out log-likelihood using each state's robust sigma.
        sigmas = [
            max(float(s.mad_w) if s.mad_w is not None else 1e-6, 1e-6)
            for s in states.values()
            if s.median_w is not None
        ]
        if sigmas:
            sigma = sum(sigmas) / len(sigmas)
            metrics["log_likelihood"] = float(
                sum(-0.5 * (e / sigma) ** 2 - math.log(sigma) for e in errors)
            )
    return FrozenEvaluation(metrics=metrics, notes=notes)


def per_class_accepts(
    model_class: Any,
    metrics: Mapping[str, float],
    active_metrics: Mapping[str, float] | None = None,
    criteria: ClassCriteria | None = None,
) -> tuple[bool, str]:
    """Apply the design §9 promotion criterion for one model class."""
    cfg = criteria or ClassCriteria()
    kind = _name(model_class)

    if kind == ModelClass.FIXED.value:
        relative = float(metrics.get("relative_mae", float("inf")))
        if relative > cfg.fixed_max_relative_mae:
            return False, f"fixed_relative_mae:{relative:.3f}>{cfg.fixed_max_relative_mae:.2f}"
        width = float(metrics.get("level_ci_width_ratio", 0.0))
        if width > cfg.fixed_max_level_ci_width_ratio:
            return False, f"fixed_level_ci_width:{width:.3f}>{cfg.fixed_max_level_ci_width_ratio:.2f}"
        return True, "fixed_criteria_met"

    if kind == ModelClass.ADJUSTABLE.value:
        span = float(metrics.get("span", 0.0))
        mae = float(metrics.get("mae", float("inf")))
        if span > 0 and mae / span > cfg.adjustable_max_relative_mae:
            return False, f"adjustable_mae_over_span:{mae / span:.3f}>{cfg.adjustable_max_relative_mae:.2f}"
        coverage = float(metrics.get("coverage", 0.0))
        if coverage < cfg.adjustable_min_coverage:
            return False, f"adjustable_coverage:{coverage:.2f}<{cfg.adjustable_min_coverage:.2f}"
        return True, "adjustable_criteria_met"

    if kind == ModelClass.DYNAMIC.value:
        support = int(metrics.get("min_state_support", 0))
        if support < cfg.dynamic_min_state_support:
            return False, f"dynamic_state_support:{support}<{cfg.dynamic_min_state_support}"
        if active_metrics is not None:
            candidate_ll = metrics.get("log_likelihood")
            active_ll = active_metrics.get("log_likelihood")
            if candidate_ll is not None and active_ll is not None and candidate_ll < active_ll:
                return False, "dynamic_log_likelihood_worse_than_active"
        return True, "dynamic_criteria_met"

    if kind == ModelClass.RESPONSE_KERNEL.value:
        nrmse = float(metrics.get("nrmse", float("inf")))
        if nrmse > cfg.kernel_max_nrmse:
            return False, f"kernel_nrmse:{nrmse:.3f}>{cfg.kernel_max_nrmse:.2f}"
        return True, "kernel_criteria_met"

    if kind == ModelClass.COMPONENT_SUM.value:
        # "via components" (design §9): each component is validated on its own;
        # there is no independent numeric criterion at the sum level.
        return True, "component_sum_via_components"

    return False, f"unknown_model_class:{kind}"


def compare_frozen(
    candidate_metrics: Mapping[str, float],
    active_metrics: Mapping[str, float] | None,
) -> dict[str, Any]:
    """Compare candidate vs active on the same frozen set.

    Unlike the legacy R²-margin comparison, v2 allows a candidate that *beats
    or equals* the active model (DEF-03): equality is not a reason to block.
    """
    candidate_mae = float(candidate_metrics.get("mae", float("inf")))
    if active_metrics is None:
        return {"better": True, "reason": "no_active_model", "delta_mae": None}

    active_mae = float(active_metrics.get("mae", float("inf")))
    delta_mae = candidate_mae - active_mae if active_mae != float("inf") else None
    if candidate_mae <= active_mae:
        return {
            "better": True,
            "reason": "candidate_beats_or_equals_active",
            "delta_mae": delta_mae,
        }
    return {
        "better": False,
        "reason": f"candidate_not_better:candidate_mae={candidate_mae:.3f}>active_mae={active_mae:.3f}",
        "delta_mae": delta_mae,
    }


__all__ = [
    "ClassCriteria",
    "FrozenEvaluation",
    "compare_frozen",
    "evaluate_frozen",
    "evidence_ids",
    "per_class_accepts",
    "split_by_observation_id",
]
