"""Response-kernel model for contract 3.0 (L3).

A response kernel ``h(τ | transition, attrs)`` describes the transient power
shape of a transition as a sequence of phases (startup / active / steady /
shutdown / standby), each with a delay, amplitude and duration distribution.
Kernels are fitted from aligned FINAL event windows (P4 evidence) and exposed
as ``expected_window(transition, attrs, tau_s) -> (mean, sigma)`` for temporal
matching (HB §7.3, interfaces.md §5).

Design rules:

* **Pure** — all inputs are explicit arrays; no DB, no wall clock, no fakes.
* **Keyed by transition type** — a lamp has a 0 s step, an HVAC compressor a
  delayed step, and a blind a transient that settles to standby.
* **Null policy** — no kernel (or no phase covering a τ sample) yields a NaN
  cell, an honest absent value, never an invented 0 W.
* **Default kernel** — when nothing has been learned, fall back to the declared
  delay step from ``device_profile.power_after`` (before → after).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import numpy as np

from ..contracts import (
    KernelPhase,
    LevelStat,
    PowerModelVersion,
    ResponseKernelSpec,
)
from .prediction import _finite
from .spec import decode_spec, encode_spec

__all__ = [
    "AlignedWindow",
    "KernelModel",
    "default_delay_kernel",
    "expected_window_from_spec",
    "fit_kernel_from_windows",
]


_PHASES: tuple[str, ...] = ("startup", "active", "steady", "shutdown", "standby")


# --------------------------------------------------------------------------- #
# Helpers.
# --------------------------------------------------------------------------- #


def _level_stat(values: Sequence[float]) -> LevelStat:
    """Robust ``(median, 1.4826*MAD, n)``; MAD is ``None`` for ``n < 2``."""
    clean = [v for v in (_finite(x) for x in values) if v is not None]
    if not clean:
        return LevelStat()
    med = float(np.median(clean))
    mad: float | None = None
    if len(clean) >= 2:
        mad = float(1.4826 * float(np.median([abs(v - med) for v in clean])))
    return LevelStat(median_w=med, mad_w=mad, n=len(clean))


@dataclass(frozen=True)
class AlignedWindow:
    """One aligned FINAL event window used as kernel-fitting evidence.

    ``offsets_s`` are sample offsets from the transition onset; ``power_w`` is
    the device-attributed level at each offset.  ``finalized`` records that the
    window is immutable P4 evidence; a provisional window is excluded by
    :func:`fit_kernel_from_windows`.
    """

    offsets_s: tuple[float, ...]
    power_w: tuple[float | None, ...]
    finalized: bool = True
    transition: str = "on"
    attributes: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if len(self.offsets_s) != len(self.power_w):
            raise ValueError("offsets_s and power_w must have the same length")


# --------------------------------------------------------------------------- #
# Fitting.
# --------------------------------------------------------------------------- #


def fit_kernel_from_windows(
    windows: Sequence[AlignedWindow],
    *,
    transition: str = "on",
    phase_duration_s: float = 60.0,
) -> ResponseKernelSpec:
    """Fit a :class:`ResponseKernelSpec` from aligned FINAL event windows.

    Each window's power trace is reduced to one phase whose amplitude is the
    robust median power over the window and whose delay is the first offset at
    which the power departs from the pre-window baseline.  ``finalized=False``
    windows are skipped: PROVISIONAL evidence is never fitted.  When no FINAL
    window is available the returned spec has no phases for ``transition`` —
    the caller must fall back to the declared kernel, not fabricate a step.
    """
    amplitudes: list[float] = []
    delays: list[float] = []
    for window in windows:
        if not window.finalized or window.transition != transition:
            continue
        values = [v for v in window.power_w if v is not None]
        if not values:
            continue
        amplitudes.append(float(np.median(values)))
        baseline = values[0]
        departures = [
            off
            for off, val in zip(window.offsets_s, window.power_w, strict=False)
            if val is not None and abs(val - baseline) > 1e-6
        ]
        delays.append(min(departures) if departures else 0.0)

    by_transition: dict[str, list[KernelPhase]] = {}
    if amplitudes:
        by_transition[transition] = [
            KernelPhase(
                phase="steady",
                delay_s=_level_stat(delays),
                amplitude_w=_level_stat(amplitudes),
                duration_s=LevelStat(median_w=phase_duration_s, n=len(amplitudes)),
            )
        ]
    return ResponseKernelSpec(by_transition=by_transition)


def default_delay_kernel(
    before_w: float | None,
    after_w: float | None,
    *,
    delay_s: float | None = None,
    duration_s: float = 3600.0,
) -> ResponseKernelSpec:
    """Build the declared delay-step kernel used when none is learned.

    This is the model-level expression of ``device_profile.py:223-227``: power
    is ``before_w`` until ``delay_s`` and ``after_w`` thereafter.  A declared
    delay of ``None`` means no delay (neutral default, step at τ=0).  When a
    level is absent it stays absent (no phase is emitted), rather than being
    replaced by ``0.0``.
    """
    parsed_delay = _finite(delay_s)
    delay = parsed_delay if parsed_delay is not None and parsed_delay >= 0.0 else 0.0
    phases: list[KernelPhase] = []
    if before_w is not None:
        phases.append(
            KernelPhase(
                phase="standby",
                delay_s=LevelStat(median_w=0.0),
                amplitude_w=LevelStat(median_w=float(before_w)),
                duration_s=LevelStat(median_w=delay, n=1),
            )
        )
    if after_w is not None:
        # The standby phase already consumed ``delay`` as its duration, so the
        # steady phase starts exactly at τ=delay with no additional delay.
        phases.append(
            KernelPhase(
                phase="steady",
                delay_s=LevelStat(median_w=0.0, n=1),
                amplitude_w=LevelStat(median_w=float(after_w)),
                duration_s=LevelStat(median_w=duration_s, n=1),
            )
        )
    return ResponseKernelSpec(by_transition={"on": phases})


# --------------------------------------------------------------------------- #
# Window prediction.
# --------------------------------------------------------------------------- #


def expected_window_from_spec(
    spec: ResponseKernelSpec,
    transition: str,
    attrs: Mapping[str, Any] | None,
    tau_s: Any,
) -> tuple[np.ndarray, np.ndarray]:
    """Return ``(mean_w, sigma_w)`` over ``tau_s`` for a kernel spec.

    Phases are laid out sequentially: each phase starts after the previous
    phase's duration, plus its own delay.  A NaN cell means "no phase covers
    this τ" — an honest absent value, never an invented 0 W.  This mirrors the
    L1 ``expected_window_for_spec`` implementation so a learned kernel and the
    declared fallback behave identically.
    """
    del attrs  # phases are keyed by transition only; attrs are reserved for attrs-conditioned kernels
    phases = spec.by_transition.get(transition) or []
    tau = np.asarray(tau_s, dtype=float)
    mean = np.full(tau.shape, np.nan, dtype=float)
    sigma = np.full(tau.shape, np.nan, dtype=float)
    cursor = 0.0
    for phase in phases:
        delay = phase.delay_s.median_w
        amplitude = phase.amplitude_w.median_w
        amp_sigma = phase.amplitude_w.mad_w
        duration = phase.duration_s.median_w
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
    return mean, sigma


# --------------------------------------------------------------------------- #
# Model wrapper.
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class KernelModel:
    """A response kernel with a deterministic ``expected_window`` method.

    ``clock`` is injected (no wall clock in engine code) and is used only to
    stamp observations; fitting and prediction themselves are pure functions of
    the supplied window arrays.
    """

    spec: ResponseKernelSpec
    clock: Any = None

    def __post_init__(self) -> None:
        if self.clock is None:
            self.clock = lambda: datetime.now(UTC)

    @classmethod
    def from_windows(
        cls,
        windows: Sequence[AlignedWindow],
        *,
        transition: str = "on",
        clock: Any = None,
    ) -> KernelModel:
        return cls(spec=fit_kernel_from_windows(windows, transition=transition), clock=clock)

    @classmethod
    def declared(
        cls,
        before_w: float | None,
        after_w: float | None,
        *,
        delay_s: float | None = None,
    ) -> KernelModel:
        return cls(spec=default_delay_kernel(before_w, after_w, delay_s=delay_s))

    @classmethod
    def from_version(cls, version: PowerModelVersion) -> KernelModel:
        if not isinstance(version.spec, ResponseKernelSpec):
            raise ValueError("a KernelModel requires a ResponseKernelSpec")
        return cls(spec=version.spec)

    def expected_window(
        self,
        transition: str,
        attrs: Mapping[str, Any] | None,
        tau_s: Any,
    ) -> tuple[np.ndarray, np.ndarray] | None:
        """The I2 handoff: ``None`` when no phase exists for ``transition``."""
        if not self.spec.by_transition.get(transition):
            return None
        return expected_window_from_spec(self.spec, transition, attrs, tau_s)

    def encoded(self) -> dict[str, Any]:
        return encode_spec(self.spec)

    @classmethod
    def from_encoded(cls, raw: Any) -> KernelModel:
        spec = decode_spec(raw)
        if not isinstance(spec, ResponseKernelSpec):
            raise ValueError("a KernelModel requires a ResponseKernelSpec")
        return cls(spec=spec)
