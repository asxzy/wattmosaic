"""Pure, spec-driven power prediction (L1).

One prediction path for every model class.  The legacy parameter-only
interpreter (``electrical/models.py:127-170``) is deliberately *not* ported:
the v3 path always goes through a :data:`PowerModelSpec` decoded by the L1
codec, so a reloaded model predicts exactly what it did before persistence
(DEF-02).

Null policy (HB §18): an unknown level/transition/control value yields a
:class:`PowerPrediction` with ``status=UNKNOWN`` and ``mean_w is None`` —
never ``0.0``.  Component sums keep their submodels: the prediction for a
component is resolved from the component registry passed in, never collapsed
into the parent's number.

This module is pure: no DB, no wall clock, no runtime fakes.  All inputs are
explicit.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import numpy as np

from ..contracts import (
    AdjustableSpec,
    ComponentSumSpec,
    DeltaPrior,
    DynamicSpec,
    FixedSpec,
    LevelStat,
    PowerLevelV3,
    PowerModelVersion,
    PowerPrediction,
    PredictionStatus,
    ResponseKernelSpec,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..runtime.interfaces import PredictionContext

__all__ = [
    "predict_with_spec",
    "transition_prior_for_spec",
    "expected_window_for_spec",
]


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(out) or math.isinf(out):
        return None
    return out


def _stat(stat: LevelStat | None) -> tuple[float | None, float | None]:
    """Return ``(median_w, sigma_w)`` from a level statistic, never 0.0."""
    if stat is None:
        return None, None
    return _finite(stat.median_w), _finite(stat.mad_w)


def _key(value: Any) -> str:
    """Normalise a contract enum / bare string to its lowercase value."""
    return str(getattr(value, "value", value) or "").strip().lower()


def _unknown(ctx: PredictionContext, *, version_id: str | None = None) -> PowerPrediction:
    """An honest unknown prediction (HB §18: absent is None, not 0.0)."""
    level = ctx.level if isinstance(ctx.level, PowerLevelV3) else PowerLevelV3.UNKNOWN
    return PowerPrediction(
        level=level,
        mean_w=None,
        sigma_w=None,
        status=PredictionStatus.UNKNOWN,
        model_version_id=version_id,
    )


def _known(
    ctx: PredictionContext,
    mean: float | None,
    sigma: float | None,
    *,
    version_id: str | None,
) -> PowerPrediction:
    if mean is None:
        return _unknown(ctx, version_id=version_id)
    level = ctx.level if isinstance(ctx.level, PowerLevelV3) else PowerLevelV3.UNKNOWN
    return PowerPrediction(
        level=level,
        mean_w=mean,
        sigma_w=sigma,
        status=PredictionStatus.KNOWN,
        model_version_id=version_id,
    )


def _interp(x: float, xs: list[float], ys: list[float]) -> float | None:
    """Clamped piecewise-linear interpolation over the declared knots."""
    if not xs or not ys or len(xs) != len(ys):
        return None
    pts = sorted(zip(xs, ys, strict=False))
    if x <= pts[0][0]:
        return _finite(pts[0][1])
    if x >= pts[-1][0]:
        return _finite(pts[-1][1])
    for (x0, y0), (x1, y1) in zip(pts, pts[1:], strict=False):
        if x0 <= x <= x1:
            if x1 == x0:
                return _finite(y1)
            frac = (x - x0) / (x1 - x0)
            return _finite(y0 + frac * (y1 - y0))
    return None


def predict_with_spec(
    version: PowerModelVersion,
    ctx: PredictionContext,
    *,
    component_predictions: Mapping[str, PowerPrediction] | None = None,
) -> PowerPrediction:
    """Predict one subject's power from its versioned spec.

    ``component_predictions`` supplies the submodel predictions for a
    :class:`ComponentSumSpec`.  Each component is looked up by id and a missing
    one yields ``None`` for the whole sum — a partial sum would invent a number
    for a non-identifiable member (HB §8.4).
    """
    spec = version.spec
    version_id = version.version_id

    if isinstance(spec, FixedSpec):
        level = ctx.level if ctx.level is not None else PowerLevelV3.UNKNOWN
        stat = spec.levels.get(level) or spec.levels.get(_key(level))
        if stat is None:
            return _unknown(ctx, version_id=version_id)
        mean, sigma = _stat(stat)
        return _known(ctx, mean, sigma, version_id=version_id)

    if isinstance(spec, AdjustableSpec):
        x_value = _finite(
            ctx.attributes.get(spec.control_variable)
            if isinstance(ctx.attributes, Mapping)
            else None
        )
        if x_value is None:
            return _unknown(ctx, version_id=version_id)
        mean = _interp(x_value, spec.knots_x, spec.knots_w)
        _, standby_mad = _stat(spec.standby)
        return _known(ctx, mean, standby_mad, version_id=version_id)

    if isinstance(spec, DynamicSpec):
        state: str | None = None
        if isinstance(ctx.attributes, Mapping):
            raw_state = ctx.attributes.get("state") or ctx.attributes.get("ha_state")
            state = _key(raw_state) if raw_state is not None else None
        candidates = spec.ha_state_to_states.get(state, []) if state else []
        # Lowest key match when multiple physical states share one HA state; the
        # mapping lists them in priority order, so take the first with a stat.
        stat = None
        for candidate in candidates:
            stat = spec.states.get(candidate) or spec.states.get(_key(candidate))
            if stat is not None:
                break
        if stat is None:
            return _unknown(ctx, version_id=version_id)
        mean, sigma = _stat(stat)
        return _known(ctx, mean, sigma, version_id=version_id)

    if isinstance(spec, ResponseKernelSpec):
        transition = ctx.transition or ""
        phases = spec.by_transition.get(transition) or []
        if not phases:
            return _unknown(ctx, version_id=version_id)
        # The steady-phase amplitude is the power the device settles at; if the
        # kernel has no steady phase, the last phase's amplitude is used.
        steady = next((p for p in phases if p.phase == "steady"), phases[-1])
        mean, sigma = _stat(steady.amplitude_w)
        return _known(ctx, mean, sigma, version_id=version_id)

    if isinstance(spec, ComponentSumSpec):
        if component_predictions is None:
            return _unknown(ctx, version_id=version_id)
        means: list[float] = []
        sigmas_sq: list[float] = []
        for component_id in spec.component_ids:
            prediction = component_predictions.get(component_id)
            if prediction is None or prediction.status != PredictionStatus.KNOWN:
                return _unknown(ctx, version_id=version_id)
            mean = _finite(prediction.mean_w)
            if mean is None:
                return _unknown(ctx, version_id=version_id)
            means.append(mean)
            sigma = _finite(prediction.sigma_w)
            if sigma is not None:
                sigmas_sq.append(sigma * sigma)
        total = sum(means)
        sigma_total = math.sqrt(sum(sigmas_sq)) if sigmas_sq else None
        return _known(ctx, total, sigma_total, version_id=version_id)

    return _unknown(ctx, version_id=version_id)


def transition_prior_for_spec(
    version: PowerModelVersion,
    transition: str,
    attrs: Mapping[str, Any],
) -> DeltaPrior | None:
    """Return the deterministic signed delta prior for ``transition``.

    ``None`` means the spec has no learned delta for that transition; the
    caller must not substitute 0.0.
    """
    spec = version.spec
    if not isinstance(spec, FixedSpec):
        return None
    stat = spec.transition_delta.get(transition) or spec.transition_delta.get(_key(transition))
    if stat is None:
        return None
    mean, sigma = _stat(stat)
    if mean is None or sigma is None:
        return None
    return DeltaPrior(
        mean_w=mean,
        sigma_w=sigma,
        sign="pos" if mean > 0 else "neg" if mean < 0 else "any",
        model_version_id=version.version_id,
    )


def expected_window_for_spec(
    version: PowerModelVersion,
    transition: str,
    attrs: Mapping[str, Any],
    tau_s: np.ndarray,
) -> tuple[np.ndarray, np.ndarray] | None:
    """Return ``(mean_w, sigma_w)`` over ``tau_s`` for a response kernel.

    ``None`` means no kernel is available for this transition (the caller must
    not fabricate a flat zero window).
    """
    spec = version.spec
    if not isinstance(spec, ResponseKernelSpec):
        return None
    phases = spec.by_transition.get(transition) or []
    if not phases:
        return None
    tau = np.asarray(tau_s, dtype=float)
    mean = np.full(tau.shape, math.nan, dtype=float)
    sigma = np.full(tau.shape, math.nan, dtype=float)
    cursor = 0.0
    for phase in phases:
        delay, _ = _stat(phase.delay_s)
        amplitude, amp_sigma = _stat(phase.amplitude_w)
        duration, _ = _stat(phase.duration_s)
        if delay is None:
            delay = 0.0
        start = cursor + delay
        end = start + (duration if duration is not None else float("inf"))
        mask = (tau >= start) & (tau < end)
        if amplitude is not None:
            mean = np.where(mask, amplitude, mean)
        if amp_sigma is not None:
            sigma = np.where(mask, amp_sigma, sigma)
        if duration is not None:
            cursor = end
    # A NaN cell means "no phase covers this tau" — an honest absent value,
    # never an invented 0 W.
    return mean, sigma
