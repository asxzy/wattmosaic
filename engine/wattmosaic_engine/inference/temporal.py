"""Temporal response kernels for event-window matching (HB §7.3, I2).

This module is deliberately **pure**: no database, no wall clock, no global
state.  Every time and every input is supplied by the caller.  It depends only
on the K04/K06 contracts, the K08 ``ModelSetView`` port and the existing
``DevicePowerProfile`` declaration (whose ``response_delay_s`` is the declared
fallback delay).

The expected response is expressed as a *delta* power ``ΔP(t)`` relative to the
event's baseline, because HB §7.3 defines ``h_i(τ, transition, attrs)`` as the
expected power **change**.  A value of ``0.0`` before the onset is the
mathematical zero of "no change yet", not an absent measurement; a genuinely
unknown response is represented by ``mean_w is None`` (HB §18).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
from wattmosaic_engine.contracts import CandidateHypothesis, DeltaPrior
from wattmosaic_engine.models.device_profile import DevicePowerProfile
from wattmosaic_engine.runtime.interfaces import ModelSetView

__all__ = [
    "ResponseWindow",
    "expected_response",
    "onset_offsets",
    "unit_step",
]

#: Number of uniform samples used when marginalising an uncertain onset.
ONSET_GRID_POINTS = 11

ResponseSource = Literal["kernel", "declared_delay", "instantaneous", "unknown"]


@dataclass(frozen=True)
class ResponseWindow:
    """Expected delta-power response of one candidate over an event window.

    ``tau_s`` is an increasing array of elapsed seconds since the nominal
    onset.  ``mean_w``/``sigma_w`` are aligned to it and are ``None`` when the
    model cannot state an expected response (no kernel and no prior).  For a
    step response the exact timing is kept in ``delay_s``/``amplitude_w`` so
    :meth:`evaluate` does not depend on the resolution of ``tau_s``.
    """

    tau_s: np.ndarray
    mean_w: np.ndarray | None
    sigma_w: np.ndarray | None
    source: ResponseSource
    delay_s: float | None = None
    amplitude_w: float | None = None
    step_sigma_w: float | None = None

    def evaluate(
        self, elapsed_s: np.ndarray, onset_offset_s: float = 0.0
    ) -> tuple[np.ndarray | None, np.ndarray | None]:
        """Evaluate ``ΔP(t - onset_offset)`` at the given elapsed seconds.

        For a kernel response the stored samples are interpolated (edge-held
        outside the sampled range).  For a step response the step is evaluated
        analytically, so its edge stays exact regardless of ``tau_s`` density.
        An unknown response yields ``(None, None)`` -- never fabricated zeros.
        """
        q = np.asarray(elapsed_s, dtype=float) - float(onset_offset_s)
        if self.source == "kernel":
            if self.mean_w is None:
                return None, None
            mean = np.interp(q, self.tau_s, self.mean_w)
            sigma = (
                np.interp(q, self.tau_s, self.sigma_w)
                if self.sigma_w is not None
                else None
            )
            return mean, sigma
        if self.amplitude_w is None or self.delay_s is None:
            return None, None
        mean = np.where(q >= self.delay_s, self.amplitude_w, 0.0)
        sigma = (
            np.full(q.shape, self.step_sigma_w, dtype=float)
            if self.step_sigma_w is not None
            else None
        )
        return mean, sigma


def _finite_float(value: Any) -> float | None:
    """Return a finite float or ``None``; never quietly coerce a bad value."""
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _clean_array(values: Any, length: int) -> np.ndarray | None:
    """Validate a model-supplied array against the requested grid length."""
    if values is None:
        return None
    try:
        arr = np.asarray(values, dtype=float).reshape(-1)
    except (TypeError, ValueError):
        return None
    if arr.size != length or not bool(np.all(np.isfinite(arr))):
        return None
    return arr


def _subject_id(candidate: CandidateHypothesis | str) -> str:
    """Accept either a candidate hypothesis or a bare subject id."""
    if isinstance(candidate, CandidateHypothesis):
        return candidate.subject_id
    if isinstance(candidate, str):
        return candidate
    raise TypeError("candidate must be a CandidateHypothesis or a subject id string")


def unit_step(tau_s: np.ndarray, delay_s: float) -> np.ndarray:
    """A unit-height step at ``delay_s`` (0 before, 1 at/after).

    The pre-onset zero is the known zero of a not-yet-happened change.  It is
    not an absent measurement and is not used as a placeholder for one.
    """
    tau = np.asarray(tau_s, dtype=float)
    return np.where(tau >= float(delay_s), 1.0, 0.0)


def onset_offsets(
    timestamp_uncertainty_s: float | None, points: int = ONSET_GRID_POINTS
) -> np.ndarray:
    """Uniform grid of onset offsets in ``[-u, +u]``.

    An absent or non-positive uncertainty collapses to the single exact onset
    (``[0.0]``).  ``points`` is forced to at least one.
    """
    uncertainty = _finite_float(timestamp_uncertainty_s)
    count = max(1, int(points))
    if uncertainty is None or uncertainty <= 0.0:
        return np.zeros(1, dtype=float)
    return np.linspace(-uncertainty, uncertainty, count)


def expected_response(
    candidate: CandidateHypothesis | str,
    transition: str | None,
    attrs: Mapping[str, Any],
    tau_grid: np.ndarray,
    *,
    models: ModelSetView,
    profile: DevicePowerProfile | None = None,
) -> ResponseWindow:
    """Build the expected delta-power response for ``tau_grid``.

    Priority (I2 step 1):

    1. ``models.expected_window`` when a response kernel exists (L3);
    2. a step at the ``response_delay_s`` declared by ``profile`` (or by
       ``attrs["response_delay_s"]`` when no profile is declared);
    3. an instantaneous step when no delay is declared.

    The step amplitude and its sigma come from ``models.transition_prior``.
    When neither a kernel nor a prior is available the response is ``unknown``
    (``mean_w is None``), never a fabricated zero-amplitude step.
    """
    tau = np.asarray(tau_grid, dtype=float).reshape(-1)
    if tau.size == 0:
        return ResponseWindow(tau, None, None, "unknown")
    order = np.argsort(tau, kind="stable")
    tau = tau[order]
    subject_id = _subject_id(candidate)
    attributes: Mapping[str, Any] = attrs if attrs is not None else {}

    # 1. Model-supplied response kernel (L3).
    if transition is not None:
        window = models.expected_window(subject_id, transition, attributes, tau)
        if window is not None:
            mean, sigma = window
            clean_mean = _clean_array(mean, tau.size)
            clean_sigma = _clean_array(sigma, tau.size)
            # A L3 kernel is authoritative even when it cannot state numbers;
            # declaring "instantaneous" here would fabricate a response.
            if clean_mean is not None:
                return ResponseWindow(
                    tau_s=tau,
                    mean_w=clean_mean,
                    sigma_w=clean_sigma,
                    source="kernel",
                )

    # 2/3. Step response: timing from the declaration, amplitude from the prior.
    declared = _finite_float(profile.response_delay_s) if profile is not None else None
    if declared is None:
        declared = _finite_float(attributes.get("response_delay_s"))
    delay_s = max(0.0, declared) if declared is not None else 0.0
    has_declaration = declared is not None and delay_s > 0.0

    prior: DeltaPrior | None = None
    if transition is not None:
        prior = models.transition_prior(subject_id, transition, attributes)
    amplitude = _finite_float(prior.mean_w) if prior is not None else None
    step_sigma = _finite_float(prior.sigma_w) if prior is not None else None
    if step_sigma is not None and step_sigma <= 0.0:
        step_sigma = None

    if amplitude is None:
        return ResponseWindow(
            tau_s=tau,
            mean_w=None,
            sigma_w=None,
            source="unknown",
            delay_s=delay_s,
        )

    source: ResponseSource = "declared_delay" if has_declaration else "instantaneous"
    mean_w = np.where(tau >= delay_s, amplitude, 0.0)
    sigma_w = np.full(tau.size, step_sigma, dtype=float) if step_sigma is not None else None
    return ResponseWindow(
        tau_s=tau,
        mean_w=mean_w,
        sigma_w=sigma_w,
        source=source,
        delay_s=delay_s,
        amplitude_w=amplitude,
        step_sigma_w=step_sigma,
    )
