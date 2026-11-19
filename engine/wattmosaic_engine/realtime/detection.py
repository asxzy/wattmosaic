r"""Streaming change detector with identity, hysteresis and CUSUM (P3).

This is the stage-4 detector of the real-time pipeline.  It is a per-reader
state machine::

    STABLE -> SUSPECT -> CONFIRMED_STEP (SUSTAINED / PERIODIC)
                     \-> TRANSIENT_REJECTED
                     \-> DRIFT

It reads the rolling baseline / σ / rings maintained by
:class:`~wattmosaic_engine.realtime.reader_state.ReaderStateStore` (P2) and
emits at most **one** :class:`~wattmosaic_engine.contracts.MeasurementEvent`
per physical step to a caller-supplied callback.  The event id is
``hash(reader_id, onset_lo, detector_version)`` (K02), so twenty identical
samples after a step cannot re-emit a second event.

Two independent detectors drive the SUSPECT transition:

* a noise-aware ``z = (Y - b) / max(σ, σ_min)`` threshold with hysteresis
  (enter at ``|z| >= enter_z``, exit below ``exit_z``) plus an absolute
  ``|Y - b| >= max(resolution_ticks * resolution, min_delta_w)`` floor;
* a two-sided CUSUM (``k = cusum_k_sigma * σ``, ``h = cusum_h_sigma * σ``)
  that catches small *sustained* shifts which never reach the enter threshold.

A slow, monotone drift below the event threshold is classified ``DRIFT``: it
feeds the baseline instead of producing a step event.  A periodic tag is
attached when several same-sign steps of similar amplitude recur with a stable
period.  A reader that goes missing emits a single ``DATA_GAP`` quality event.

The module is **pure**: there is no database access and no wall clock.  All
"now" values are explicit arguments (usually the ``at`` of :meth:`detect`),
and production code never imports :mod:`wattmosaic_engine.runtime.fakes`.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from statistics import fmean, pstdev

from ..contracts import (
    MeasurementEvent,
    QualityFlag,
    StepClass,
    measurement_event_id,
)
from ..disaggregation.events import confirm_streaming_step
from .reader_state import ReaderStateStore, WindowSample

__all__ = [
    "DETECTOR_VERSION",
    "ChangeDetector",
    "DetectorConfig",
    "DetectorState",
    "DetectionSink",
    "build_measurement_event",
]

#: Bumped only when a change would alter event identity (K02).  Part of the
#: content hash, so a version change deliberately re-identifies old steps.
DETECTOR_VERSION = "3.0.0"

DetectionSink = Callable[[MeasurementEvent], None]


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class DetectorConfig:
    """Every tunable of the state machine (design §3 / HB §6.3–6.4).

    Defaults follow design §3; a caller (P6 via ``config/settings.py``) may
    override any of them.  Absent quantities stay ``None`` -- in particular
    ``resolution_w`` is never defaulted to ``0`` and σ is never fabricated when
    the reader-state store has no estimate.
    """

    # Hysteresis / absolute floor.
    enter_z: float = 4.0
    exit_z: float = 2.0
    min_delta_w: float = 2.0
    min_resolution_ticks: float = 3.0
    sigma_min_w: float = 1.0

    # Two-sided CUSUM (multiples of σ).
    cusum_k_sigma: float = 0.5
    cusum_h_sigma: float = 5.0

    # Confirmation / settling / transients.
    confirm_samples: int = 2
    min_transient_samples: int = 2
    confirm_s: float = 20.0
    settle_s: float = 120.0
    transient_sigma: float = 2.0

    # Drift (a drift is not a step event).
    drift_s: float = 3600.0
    drift_fraction_sigma: float = 0.3

    # Periodic tags.
    periodic_min_steps: int = 4
    periodic_amplitude_tol: float = 0.2
    periodic_cv_max: float = 0.3
    periodic_max_periods: int = 12

    # Quality score (design §3).
    z_score_cap: float = 8.0
    q_min_pre_post: int = 2
    pre_window_s: float = 30.0
    onset_padding_s: float = 30.0

    # Minimum observations before the detector trusts σ (P2 recomputes its MAD
    # noise estimate every 64 innovations; before that it sits at the 1 W
    # floor, which a genuinely noisy reader would turn into false alarms).
    sigma_warmup_samples: int = 64

    # Quality flags / data gaps.
    gap_cooldown_s: float = 300.0
    timestamp_penalty: float = 0.7

    def __post_init__(self) -> None:
        if self.enter_z <= 0 or self.exit_z <= 0 or self.exit_z > self.enter_z:
            raise ValueError("require 0 < exit_z <= enter_z")
        if self.cusum_k_sigma < 0 or self.cusum_h_sigma <= 0:
            raise ValueError("CUSUM constants must be non-negative / positive")
        if self.confirm_samples < 1 or self.min_transient_samples < 1:
            raise ValueError("sample counts must be >= 1")
        if self.periodic_min_steps < 2:
            raise ValueError("periodic_min_steps must be >= 2")
        if not 0.0 <= self.timestamp_penalty <= 1.0:
            raise ValueError("timestamp_penalty must be in [0, 1]")

    def enter_threshold_w(self, sigma_w: float, resolution_w: float | None) -> float:
        """Absolute |Y - b| required to open SUSPECT (no global watt threshold)."""
        floor = (
            self.min_resolution_ticks * resolution_w
            if resolution_w is not None and math.isfinite(resolution_w)
            else 0.0
        )
        # ``sigma_min_w`` is a noise floor, not an invented zero measurement.
        return max(self.enter_z * max(sigma_w, self.sigma_min_w), floor, self.min_delta_w)

    def exit_threshold_w(self, sigma_w: float, resolution_w: float | None) -> float:
        floor = (
            self.min_resolution_ticks * resolution_w
            if resolution_w is not None and math.isfinite(resolution_w)
            else 0.0
        )
        return max(self.exit_z * max(sigma_w, self.sigma_min_w), floor, self.min_delta_w)


# --------------------------------------------------------------------------- #
# Per-reader state machine
# --------------------------------------------------------------------------- #


class DetectorState(str, Enum):
    """The three transient detector states (confirmation is an event class)."""

    STABLE = "stable"
    SUSPECT = "suspect"
    SETTLING = "settling"


@dataclass(slots=True)
class _ReaderSM:
    state: DetectorState = DetectorState.STABLE
    baseline_at_suspect: float | None = None
    sigma_at_suspect: float | None = None
    last_pre: WindowSample | None = None
    first_post: WindowSample | None = None
    onset_lo: datetime | None = None
    last_sample_at: datetime | None = None
    last_level_at: datetime | None = None
    last_level: float | None = None
    last_deviation: float | None = None
    stable_sigma: float | None = None
    post_count: int = 0
    cusum_hi: float = 0.0
    cusum_lo: float = 0.0
    drift_dir: int = 0
    drift_start: datetime | None = None
    drift_emitted: bool = False
    drift_suppress_until: datetime | None = None
    last_gap_at: datetime | None = None
    last_emitted_onset_lo: datetime | None = None
    bad_timestamp: bool = False
    gap_emitted: bool = False
    observation_count: int = 0
    suppression_level: float | None = None
    suppress_until: datetime | None = None
    post_step_baseline: float | None = None
    step_history: deque[tuple[datetime, float, float]] = field(
        default_factory=lambda: deque(maxlen=64)
    )


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _usable(sample: WindowSample) -> bool:
    if sample.active_power_w is None or not math.isfinite(sample.active_power_w):
        return False
    return not (
        QualityFlag.UNIT_INVALID in sample.quality_flags
        or QualityFlag.IMPLAUSIBLE_RANGE in sample.quality_flags
        or QualityFlag.OUT_OF_ORDER in sample.quality_flags
        or QualityFlag.PRIMED in sample.quality_flags
    )


def build_measurement_event(
    *,
    reader_id: str,
    delta_w: float,
    sigma_w: float,
    z_score: float,
    step_class: StepClass,
    onset_lo: datetime,
    onset_hi: datetime,
    pre_level_w: float | None,
    post_level_w: float | None,
    quality_score: float,
    detected_at: datetime,
) -> MeasurementEvent:
    """Build a :class:`MeasurementEvent` with its deterministic identity."""
    onset_lo, onset_hi = _aware(onset_lo), _aware(onset_hi)
    if onset_hi < onset_lo:
        raise ValueError("onset_hi must not precede onset_lo")
    return MeasurementEvent(
        event_id=measurement_event_id(reader_id, onset_lo, DETECTOR_VERSION),
        reader_id=reader_id,
        delta_w=float(delta_w),
        sigma_w=float(sigma_w),
        z_score=float(z_score),
        step_class=step_class,
        onset_lo=onset_lo,
        onset_hi=onset_hi,
        pre_level_w=pre_level_w,
        post_level_w=post_level_w,
        quality_score=min(1.0, max(0.0, float(quality_score))),
        detected_at=_aware(detected_at),
    )


# --------------------------------------------------------------------------- #
# Detector
# --------------------------------------------------------------------------- #


class ChangeDetector:
    """Per-reader streaming detector built on a P2 :class:`ReaderStateStore`.

    Parameters
    ----------
    store:
        The P2 reader-state store.  The detector *reads* baseline, σ, rings and
        hold state from it and calls ``freeze_baseline`` / ``unfreeze_baseline``
        to protect the pre-step level while a change is pending.
    emit:
        Callback receiving one :class:`MeasurementEvent` per physical step (and
        one ``DATA_GAP`` quality event per gap episode).
    config:
        All tunables; defaults implement design §3.
    """

    def __init__(
        self,
        store: ReaderStateStore,
        emit: DetectionSink,
        config: DetectorConfig | None = None,
    ) -> None:
        self.store = store
        self.emit = emit
        self.config = config or DetectorConfig()
        self.detector_version = DETECTOR_VERSION
        self._states: dict[str, _ReaderSM] = {}

    def state(self, reader: str) -> DetectorState:
        """Current state machine state for ``reader``."""
        sm = self._states.get(reader)
        return DetectorState.STABLE if sm is None else sm.state

    def process(self, reader: str, at: datetime) -> None:
        """Advance ``reader``'s detector using the held reading at ``at``.

        Driven by an explicit sample or a fast-loop tick; it never reads a wall
        clock.  A reader with no usable value is handled as a possible data gap;
        a primed value seeds state but never opens SUSPECT.
        """
        at = _aware(at)
        sm = self._states.setdefault(reader, _ReaderSM())
        self.store.release_expired_freezes(at)

        held = self.store.held(reader, at)
        # A sample observed exactly at ``at`` is fresh evidence even when the
        # zero-order hold would report "missing" after a long silence (a gap
        # followed by recovery).  Use it; otherwise handle hold state.
        fresh = self._sample_at(reader, at)
        has_fresh = fresh.active_power_w is not None and fresh.sequence >= 0
        if held.state == "missing" and not has_fresh:
            self._on_missing(reader, sm, at)
            return
        if held.state == "stale" and not has_fresh:
            if sm.last_gap_at is None:
                sm.last_gap_at = held.last_confirmed_at
            return
        if has_fresh:
            sm.last_gap_at = None
            sm.gap_emitted = False

        value = fresh.active_power_w if has_fresh else held.value
        if value is None or not math.isfinite(value):
            return

        sample_at = at if has_fresh else _aware(held.held_since)
        if self._is_primed(reader, sample_at):
            self._reset_cusum(sm)
            sm.last_pre = self._sample_at(reader, sample_at)
            sm.last_deviation = None
            return

        cadence_s = self._cadence_s(reader)
        sm.bad_timestamp = bool(
            self._flags_at(reader, sample_at)
            & {QualityFlag.CLOCK_SKEW_FUTURE, QualityFlag.OUT_OF_ORDER}
        )

        previous_at = sm.last_sample_at
        gap_s = cadence_s if previous_at is None else max(0.0, (sample_at - previous_at).total_seconds())
        staleness = min(1.0, max(0.0, (gap_s - cadence_s) / max(1.0, self.store.stale_after_s)))
        sm.last_sample_at = sample_at
        sm.observation_count += 1

        if sm.state is DetectorState.SUSPECT:
            self._in_suspect(reader, sm, value, sample_at, at, staleness)
        elif sm.state is DetectorState.SETTLING:
            self._in_settling(reader, sm, value, sample_at)
        else:
            self._in_stable(reader, sm, value, sample_at, at, staleness)
            sm.last_pre = self._sample_at(reader, sample_at)

    # The natural verb for the event-loop caller (P6); keep both spellings.
    detect = process

    # -- state handlers ------------------------------------------------------ #

    def _in_stable(
        self,
        reader: str,
        sm: _ReaderSM,
        value: float,
        sample_at: datetime,
        at: datetime,
        staleness: float,
    ) -> None:
        baseline = self.store.baseline(reader)
        sigma = self._sigma(reader)
        if baseline is None or sigma is None:
            sm.last_deviation = value - baseline if baseline is not None else None
            return
        # After a confirmed step, P2's EWMA baseline lags the new level for a
        # while (τ = 30 min).  Use the last settled level as the effective
        # baseline until P2's baseline converges, so the next genuine step is
        # measured from the right reference.
        if sm.post_step_baseline is not None:
            if abs(baseline - sm.post_step_baseline) <= max(
                self.config.exit_z * sigma, self.config.min_delta_w
            ):
                sm.post_step_baseline = None
            else:
                baseline = sm.post_step_baseline
        resolution = self._resolution(reader)
        if sm.observation_count < self.config.sigma_warmup_samples:
            # Before σ is trustworthy, let P2 own the baseline and track the
            # level, but do not open SUSPECT on a still-floor noise estimate.
            sm.stable_sigma = sigma
            sm.last_deviation = value - baseline
            self._reset_cusum(sm)
            return
        # After a confirmed step the baseline adopts the new level.  Suppress
        # any residual step signal until the level moves materially again, so
        # trailing identical samples never re-open SUSPECT.
        suppress = sm.suppress_until is not None and sample_at < sm.suppress_until
        if sm.suppression_level is not None:
            # Identical trailing samples at the new level are the same physical
            # step; never re-open SUSPECT on them (noise-based tolerance only --
            # P2's resolution can be inflated by the step's own innovation).
            tol = max(self.config.exit_z * sigma, self.config.min_delta_w)
            if abs(value - sm.suppression_level) < tol:
                suppress = True
        if suppress:
            sm.last_deviation = value - baseline
            sm.last_pre = self._sample_at(reader, sample_at)
            self._reset_cusum(sm)
            return
        if sm.suppress_until is not None:
            sm.suppress_until = None
            sm.suppression_level = None
        deviation = value - baseline
        # P2's σ can be inflated by the step's own innovation.  Use the last
        # quiet-period σ for the significance test so a real step is not
        # hidden by its own noise contribution.  Adopt a jump in σ only when the
        # current level is unremarkable -- a genuine upward convergence (noisy
        # reader after warm-up) happens on a quiet sample, not on the step.
        if (
            sm.stable_sigma is not None
            and sigma > 1.5 * sm.stable_sigma
            and abs(deviation) <= self.config.exit_z * sigma
        ):
            sm.stable_sigma = sigma
        detect_sigma = sm.stable_sigma if sm.stable_sigma is not None else sigma
        z = deviation / detect_sigma

        self._track_drift(reader, sm, deviation, z, sample_at, baseline, detect_sigma)
        if sm.drift_dir != 0:
            # A recognised slow drift feeds the baseline: no step detection.
            self._reset_cusum(sm)
            return

        if abs(z) >= self.config.enter_z and abs(deviation) >= self.config.enter_threshold_w(
            detect_sigma, resolution
        ):
            self._open_suspect(reader, sm, value, sample_at, baseline, detect_sigma)
            return

        # Refresh the quiet-period σ when the current sample is unremarkable
        # against P2's *own* σ.  A step's innovation inflates P2's σ; by not
        # adopting that inflated value we keep the pre-step noise scale for the
        # next few samples (until the step is confirmed or rejected).
        if abs(deviation) <= self.config.exit_z * sigma:
            sm.stable_sigma = sigma

        k = self.config.cusum_k_sigma * detect_sigma
        h = self.config.cusum_h_sigma * detect_sigma
        sm.cusum_hi = max(0.0, sm.cusum_hi + deviation - k)
        sm.cusum_lo = max(0.0, sm.cusum_lo - deviation - k)
        if sm.cusum_hi > h or sm.cusum_lo > h:
            self._open_suspect(reader, sm, value, sample_at, baseline, detect_sigma)

    def _track_drift(
        self,
        reader: str,
        sm: _ReaderSM,
        deviation: float,
        z: float,
        sample_at: datetime,
        baseline: float,
        sigma: float,
    ) -> None:
        """Flag a slow, monotone, sub-event drift; it is not an event."""
        previous = sm.last_deviation
        sm.last_deviation = deviation
        if sm.drift_suppress_until is not None and sample_at < sm.drift_suppress_until:
            self._clear_drift(sm)
            return
        if previous is None:
            return
        increment = deviation - previous
        if abs(increment) >= self.config.enter_threshold_w(sigma, None):
            # An abrupt level change is a step, not drift.
            self._clear_drift(sm)
            return
        if abs(increment) < 1e-9:
            # A constant offset from baseline is a step that has already
            # happened (or a steady level); it is not a drift in progress.
            self._clear_drift(sm)
            return
        if abs(deviation) <= self.config.drift_fraction_sigma * sigma:
            self._clear_drift(sm)
            return
        direction = 1 if increment > 0 else -1
        if sm.drift_dir != direction:
            sm.drift_dir = direction
            sm.drift_start = sample_at
            sm.drift_emitted = False
            return
        if sm.drift_emitted or sm.drift_start is None:
            return
        if (sample_at - sm.drift_start).total_seconds() < self.config.drift_s:
            return

        sm.drift_emitted = True
        onset_lo = sample_at - timedelta(seconds=self.config.drift_s)
        event = build_measurement_event(
            reader_id=reader,
            delta_w=deviation,
            sigma_w=sigma,
            z_score=z,
            step_class=StepClass.DRIFT,
            onset_lo=onset_lo,
            onset_hi=sample_at,
            pre_level_w=baseline,
            post_level_w=baseline + deviation,
            quality_score=min(1.0, abs(z) / self.config.z_score_cap),
            detected_at=sample_at,
        )
        # Drift feeds the baseline: P2's EWMA already follows it; release any
        # hold that could keep the baseline pinned to the old level.
        self.store.unfreeze_baseline(reader)
        self.emit(event)

    def _clear_drift(self, sm: _ReaderSM) -> None:
        sm.drift_dir = 0
        sm.drift_start = None
        sm.drift_emitted = False

    def _open_suspect(
        self,
        reader: str,
        sm: _ReaderSM,
        value: float,
        sample_at: datetime,
        baseline: float,
        sigma: float,
    ) -> None:
        last_pre = sm.last_pre
        onset_lo = (last_pre.observed_at if last_pre else sample_at) - timedelta(
            seconds=self.config.onset_padding_s
        )
        sm.state = DetectorState.SUSPECT
        sm.baseline_at_suspect = baseline
        sm.sigma_at_suspect = sigma
        sm.onset_lo = _aware(onset_lo)
        sm.first_post = None
        sm.last_level = value
        sm.last_level_at = sample_at
        sm.post_count = 0
        self._reset_cusum(sm)
        self._clear_drift(sm)
        # Freeze the baseline so the EWMA cannot absorb the persistent step
        # while it is being confirmed (design §3, HB §6.3).
        self.store.freeze_baseline(
            reader, sample_at + timedelta(seconds=self.config.confirm_s + self.config.settle_s)
        )

    def _in_suspect(
        self,
        reader: str,
        sm: _ReaderSM,
        value: float,
        sample_at: datetime,
        at: datetime,
        staleness: float,
    ) -> None:
        baseline = sm.baseline_at_suspect
        sigma = sm.sigma_at_suspect
        if baseline is None or sigma is None:
            self._abort(reader, sm, at)
            return
        deviation = value - baseline
        z = deviation / sigma
        within_band = abs(deviation) <= self.config.transient_sigma * sigma

        if sm.first_post is None:
            if within_band:
                # A single-sample excursion is noise, not a short transient.
                self._abort(reader, sm, at)
                return
            sm.first_post = self._sample_at(reader, sample_at)
            sm.post_count = 1
        else:
            sm.post_count += 1
        sm.last_level = value
        sm.last_level_at = sample_at

        if within_band:
            if sm.post_count >= self.config.min_transient_samples:
                self._finish_event(
                    reader, sm, StepClass.TRANSIENT, value, sample_at, at, staleness
                )
            else:
                self._abort(reader, sm, at)
            return

        held_for = (sample_at - sm.first_post.observed_at).total_seconds()
        if sm.post_count >= self.config.confirm_samples or held_for >= self.config.confirm_s:
            self._confirm(reader, sm, value, sample_at, at, staleness)
            return
        if abs(z) < self.config.exit_z:
            self._abort(reader, sm, at)

    def _in_settling(
        self, reader: str, sm: _ReaderSM, value: float, sample_at: datetime
    ) -> None:
        baseline = sm.baseline_at_suspect
        if baseline is None:
            self._abort(reader, sm, sample_at)
            return
        sigma = sm.sigma_at_suspect
        if sigma is not None:
            sm.last_deviation = value - baseline
        last_at = sm.last_level_at or sample_at
        if (sample_at - last_at).total_seconds() >= self.config.settle_s:
            sm.state = DetectorState.STABLE
            sm.last_level_at = sample_at
            self.store.unfreeze_baseline(reader)
            sm.drift_suppress_until = sample_at + timedelta(
                seconds=2.0 * self.store.baseline_tau_s
            )
            self._clear_drift(sm)
            sm.baseline_at_suspect = None
            sm.sigma_at_suspect = None

    # -- confirmation / emission -------------------------------------------- #

    def _confirm(
        self,
        reader: str,
        sm: _ReaderSM,
        value: float,
        sample_at: datetime,
        at: datetime,
        staleness: float,
    ) -> None:
        baseline = sm.baseline_at_suspect
        sigma = sm.sigma_at_suspect
        if baseline is None or sigma is None:
            self._abort(reader, sm, at)
            return
        pre, post = self._pre_post_samples(reader, sm, sample_at)
        step = confirm_streaming_step(
            pre,
            post,
            channel_id=reader,
            sigma_w=sigma,
            min_delta_w=self.config.min_delta_w,
            resolution_w=self._resolution(reader),
        )
        delta_w = step.delta_w if step is not None else value - baseline
        if abs(delta_w) < self.config.min_delta_w:
            self._finish_event(reader, sm, StepClass.TRANSIENT, value, sample_at, at, staleness)
            return
        step_class = (
            StepClass.PERIODIC
            if self._is_periodic(reader, sm, sm.onset_lo, delta_w)
            else StepClass.SUSTAINED
        )
        self._emit_step(reader, sm, delta_w, step_class, value, sample_at, at, staleness)

    def _emit_step(
        self,
        reader: str,
        sm: _ReaderSM,
        delta_w: float,
        step_class: StepClass,
        value: float,
        sample_at: datetime,
        at: datetime,
        staleness: float,
    ) -> None:
        baseline = sm.baseline_at_suspect
        sigma = sm.sigma_at_suspect
        assert baseline is not None and sigma is not None
        onset_lo, onset_hi = self._onset_bounds(sm, sample_at)
        z = delta_w / sigma

        pre, post = self._pre_post_samples(reader, sm, sample_at)
        coverage = min(1.0, min(len(pre), len(post)) / max(1, self.config.q_min_pre_post))
        ts_factor = self.config.timestamp_penalty if sm.bad_timestamp else 1.0
        quality = (
            min(1.0, abs(z) / self.config.z_score_cap)
            * coverage
            * max(0.0, 1.0 - staleness)
            * ts_factor
        )

        if sm.last_emitted_onset_lo != onset_lo:
            self.emit(
                build_measurement_event(
                    reader_id=reader,
                    delta_w=delta_w,
                    sigma_w=sigma,
                    z_score=z,
                    step_class=step_class,
                    onset_lo=onset_lo,
                    onset_hi=onset_hi,
                    pre_level_w=baseline,
                    post_level_w=value,
                    quality_score=quality,
                    detected_at=at,
                )
            )
            sm.last_emitted_onset_lo = onset_lo

        # Settling keeps the baseline frozen briefly, then releases it so the
        # EWMA can adopt the new level (task step 2).
        sm.state = DetectorState.SETTLING
        sm.last_level_at = sample_at
        sm.last_level = value
        sm.suppression_level = value
        sm.post_step_baseline = value
        # Suppress further step signals only until the post-step level has
        # produced a couple of samples; after that the detector's own baseline
        # (post_step_baseline) keeps the reference correct until P2 catches up.
        sm.suppress_until = sample_at + timedelta(
            seconds=max(2.0 * self._cadence_s(reader), self.config.settle_s / 2.0)
        )
        # Release the pre-step freeze immediately: the baseline must adopt the
        # new level during settling, otherwise the EWMA stays pinned to the old
        # level and re-detects the same step (extra events).  SETTLING itself
        # suppresses further detections until the new level is established.
        self.store.unfreeze_baseline(reader)

    def _finish_event(
        self,
        reader: str,
        sm: _ReaderSM,
        step_class: StepClass,
        value: float,
        sample_at: datetime,
        at: datetime,
        staleness: float,
    ) -> None:
        baseline = sm.baseline_at_suspect
        sigma = sm.sigma_at_suspect
        if baseline is None or sigma is None:
            self._abort(reader, sm, at)
            return
        onset_lo, onset_hi = self._onset_bounds(sm, sample_at)
        delta_w = (sm.last_level - baseline) if sm.last_level is not None else (value - baseline)
        z = delta_w / sigma
        quality = min(1.0, abs(z) / self.config.z_score_cap) * max(0.0, 1.0 - staleness)
        if sm.bad_timestamp:
            quality *= self.config.timestamp_penalty
        if sm.last_emitted_onset_lo != onset_lo:
            self.emit(
                build_measurement_event(
                    reader_id=reader,
                    delta_w=delta_w,
                    sigma_w=sigma,
                    z_score=z,
                    step_class=step_class,
                    onset_lo=onset_lo,
                    onset_hi=onset_hi,
                    pre_level_w=baseline,
                    post_level_w=baseline if step_class is StepClass.TRANSIENT else value,
                    quality_score=quality,
                    detected_at=at,
                )
            )
            sm.last_emitted_onset_lo = onset_lo
        self._abort(reader, sm, at, settling=True)

    # -- data gaps ----------------------------------------------------------- #

    def _on_missing(self, reader: str, sm: _ReaderSM, at: datetime) -> None:
        if sm.gap_emitted:
            return
        gap_start = sm.last_gap_at or sm.last_sample_at
        if gap_start is None:
            # The reader has never produced a value: nothing to call a gap.
            self._abort(reader, sm, at)
            return
        if (at - gap_start).total_seconds() < self.config.gap_cooldown_s:
            return
        baseline = self.store.baseline(reader)
        sigma = self._sigma(reader)
        event = build_measurement_event(
            reader_id=reader,
            # A gap carries no delta measurement; the contract requires a float,
            # so 0.0 is a sentinel for "no delta", never a measured value.
            delta_w=0.0,
            sigma_w=sigma if sigma is not None else self.config.sigma_min_w,
            z_score=0.0,
            step_class=StepClass.DATA_GAP,
            onset_lo=gap_start,
            onset_hi=at,
            pre_level_w=baseline,
            post_level_w=None,
            quality_score=0.0,
            detected_at=at,
        )
        self._abort(reader, sm, at)
        sm.gap_emitted = True
        sm.last_gap_at = at
        sm.last_emitted_onset_lo = gap_start
        self.emit(event)

    # -- periodic tagging ---------------------------------------------------- #

    def _is_periodic(
        self, reader: str, sm: _ReaderSM, onset_lo: datetime | None, delta_w: float
    ) -> bool:
        if onset_lo is None or self.config.periodic_min_steps < 3:
            return False
        amplitude = abs(delta_w)
        prior_times: list[datetime] = []
        max_span = timedelta(seconds=self.config.periodic_max_periods * 3600.0)
        for prior_at, prior_delta, prior_amp in reversed(sm.step_history):
            if prior_at >= onset_lo:
                continue
            if onset_lo - prior_at > max_span:
                break
            same_sign = (prior_delta > 0) == (delta_w > 0)
            similar = (
                prior_amp > 0.0
                and amplitude > 0.0
                and abs(amplitude - prior_amp) / prior_amp <= self.config.periodic_amplitude_tol
            )
            if same_sign and similar:
                prior_times.append(prior_at)
                if len(prior_times) >= self.config.periodic_min_steps - 1:
                    break
        sm.step_history.append((onset_lo, delta_w, amplitude))
        # Need the current step plus ``periodic_min_steps - 1`` matching prior
        # same-sign steps.  Opposite-sign steps (the OFF half of a cycle) are
        # ignored, not treated as breaking the chain.
        if len(prior_times) < self.config.periodic_min_steps - 1:
            return False
        all_times = [*reversed(prior_times), onset_lo]  # chronological order
        intervals = [
            (b - a).total_seconds()
            for a, b in zip(all_times, all_times[1:], strict=False)
            if b > a
        ]
        if len(intervals) < self.config.periodic_min_steps - 1:
            return False
        mean = fmean(intervals)
        return mean > 0.0 and (pstdev(intervals) / mean) <= self.config.periodic_cv_max

    # -- helpers ------------------------------------------------------------- #

    def _abort(
        self,
        reader: str,
        sm: _ReaderSM,
        at: datetime,
        *,
        settling: bool = False,
    ) -> None:
        sm.state = DetectorState.SETTLING if settling else DetectorState.STABLE
        sm.baseline_at_suspect = None
        sm.sigma_at_suspect = None
        sm.first_post = None
        sm.onset_lo = None
        sm.post_count = 0
        self._reset_cusum(sm)
        self.store.unfreeze_baseline(reader)
        if settling:
            sm.last_level_at = at
            sm.drift_suppress_until = at + timedelta(seconds=2.0 * self.store.baseline_tau_s)
            self._clear_drift(sm)

    def _reset_cusum(self, sm: _ReaderSM) -> None:
        sm.cusum_hi = 0.0
        sm.cusum_lo = 0.0

    def _sigma(self, reader: str) -> float | None:
        measured = self.store.sigma(reader)
        return max(measured, self.config.sigma_min_w) if measured is not None else None

    def _resolution(self, reader: str) -> float | None:
        state = self.store.reader_state(reader)
        return None if state is None else state.resolution_w

    def _cadence_s(self, reader: str) -> float:
        cadence = self.store.cadence(reader)
        return max(1.0, cadence.seconds) if cadence is not None else 15.7

    def _flags_at(self, reader: str, at: datetime) -> set[QualityFlag]:
        state = self.store.reader_state(reader)
        if state is None:
            return set()
        for sample in reversed(state.samples):
            if sample.observed_at == at:
                return set(sample.quality_flags)
        return set()

    def _is_primed(self, reader: str, at: datetime) -> bool:
        return QualityFlag.PRIMED in self._flags_at(reader, at)

    def _sample_at(self, reader: str, at: datetime) -> WindowSample:
        state = self.store.reader_state(reader)
        if state is not None:
            for sample in reversed(state.samples):
                if sample.observed_at == at:
                    return sample
        # A held value always corresponds to a retained sample; keep a valid
        # (absent-value) fallback rather than fabricating a measurement.
        return WindowSample(at, at, None, (), -1)

    def _pre_post_samples(
        self, reader: str, sm: _ReaderSM, sample_at: datetime
    ) -> tuple[list[tuple[datetime, float]], list[tuple[datetime, float]]]:
        onset_lo = sm.onset_lo or sample_at
        onset_hi = sm.first_post.observed_at if sm.first_post else sample_at
        pre_window = timedelta(seconds=max(self.config.pre_window_s, 2.0 * self._cadence_s(reader)))
        pre = [
            (s.observed_at, s.active_power_w)
            for s in self.store.window(reader, onset_lo - pre_window, onset_lo)
            if _usable(s) and s.active_power_w is not None
        ]
        post = [
            (s.observed_at, s.active_power_w)
            for s in self.store.window(reader, onset_hi, sample_at)
            if _usable(s) and s.active_power_w is not None
        ]
        return pre, post

    def _onset_bounds(self, sm: _ReaderSM, sample_at: datetime) -> tuple[datetime, datetime]:
        onset_lo = sm.onset_lo or sample_at
        onset_hi = sm.first_post.observed_at if sm.first_post else sample_at
        return _aware(onset_lo), _aware(onset_hi)
