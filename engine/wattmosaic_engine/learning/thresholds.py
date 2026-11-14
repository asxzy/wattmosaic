"""Learning pipeline configuration.

Every numeric threshold used by the sample filter, trainer, validator and
promotion manager lives here (or is derived from :class:`EngineConfig`), so
behaviour is inspectable and configurable rather than scattered as magic
numbers.  See ``docs/learning.md`` for the justification of each default.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..config.settings import EngineConfig

#: Keys accepted in an isolation override map.  Anything else is ignored so a
#: typo in one key cannot silently change an unrelated timing.
ISOLATION_TIMING_KEYS: tuple[str, ...] = (
    "analysis_offset_s",
    "isolation_window_s",
    "power_window_s",
)


@dataclass(slots=True)
class LearningThresholds:
    # -- sampling / windowing ------------------------------------------- #
    #: a typical CT cadence is 15.7 s (see docs/testing.md).
    #: We never assume 2 s.  Windows are derived from the observed cadence but
    #: clamped so a brief cadence glitch cannot create minutes-long windows.
    assumed_cadence_s: float = 15.7
    min_window_s: float = 8.0
    max_window_s: float = 180.0
    #: how many median cadences make up one side of an event window
    window_cadences: float = 4.0
    #: ignore readings immediately around the transition while CTs settle
    settle_s: float = 5.0
    #: minimum readings required in baseline and event windows
    min_baseline_samples: int = 3
    min_event_samples: int = 3

    # -- staleness / timestamps ----------------------------------------- #
    #: a reading older than this is not evidence (matches engine stale_after)
    stale_after_s: float = 120.0
    #: allowed clock skew between HA state timestamp and engine receipt time
    max_timestamp_skew_s: float = 30.0
    #: event window timestamps must be within this of the event
    max_event_alignment_s: float = 120.0

    # -- event isolation ------------------------------------------------- #
    #: two state transitions closer than this are concurrent for our purposes.
    #: This is the *global default*: an appliance whose physical response lags
    #: (a compressor, a water heater) needs a wider window than a lamp, so
    #: resolve per device through :meth:`isolation_timings` rather than reading
    #: this field directly.
    concurrency_window_s: float = 10.0
    #: wide window inspected for a *later* sibling step that pollutes the
    #: measured delta.  Never narrower than ``concurrency_window_s``.
    power_analysis_window_s: float = 60.0
    #: shift between the HA event and the analysis window, for a device whose
    #: physical response trails its control signal.
    analysis_offset_s: float = 0.0
    #: maximum plausible number of *other physical devices* changing in the
    #: window.  ``1`` tolerates one sibling (its effect is separable); two or
    #: more makes the CT delta unattributable.
    max_concurrent_events: int = 1
    #: ``class label -> timing overrides`` (middle layer).  Labels are whatever
    #: the caller uses; ``PowerBehavior``/``DeviceType`` values work unchanged.
    isolation_by_class: dict[str, dict[str, float]] = field(default_factory=dict)
    #: ``device_id -> timing overrides`` (strongest layer).
    isolation_by_device: dict[str, dict[str, float]] = field(default_factory=dict)
    #: ``device_id -> class labels`` used to look up ``isolation_by_class``.
    isolation_device_classes: dict[str, tuple[str, ...]] = field(default_factory=dict)

    # -- signal quality -------------------------------------------------- #
    #: minimum robust sigma (W) used as the "unstable" floor
    min_noise_floor_w: float = 3.0
    #: baseline/event sigma above this fraction of level is unstable
    max_relative_sigma: float = 0.20
    #: absolute robust sigma above this is excessive noise
    max_absolute_sigma_w: float = 60.0
    #: required separation between baseline and event levels vs noise
    magnitude_noise_multiple: float = 3.0
    #: absolute minimum delta treated as a real load (CT resolution floor)
    min_delta_w: float = 10.0
    #: delta above this is implausible for a single residential device
    max_delta_w: float = 25_000.0

    # -- association ----------------------------------------------------- #
    #: minimum association confidence supplied by the electrical solver
    min_association_confidence: float = 0.60

    # -- duplicates ------------------------------------------------------ #
    #: two events with identical (device, state) closer than this are duplicates
    duplicate_window_s: float = 30.0

    # -- training / validation / promotion ------------------------------- #
    min_samples_for_model: int = 12
    #: fraction of accepted samples held out; split by time, not randomly
    holdout_fraction: float = 0.25
    min_validation_samples: int = 3
    min_validation_r2: float = 0.70
    #: a candidate must beat the active model's R² by at least this margin
    promotion_margin: float = 0.05
    #: minimum fraction of accepted samples (guard against training on garbage)
    max_sample_rejection_ratio: float = 0.60
    #: stability: max allowed spread of fold-R² before the candidate is treated
    #: as unstable and sent to manual review
    max_r2_fold_spread: float = 0.35
    #: number of time folds for the stability check
    stability_folds: int = 3

    @classmethod
    def from_engine_config(cls, config: EngineConfig) -> LearningThresholds:
        return cls(
            stale_after_s=float(config.stale_after_s),
            min_samples_for_model=int(config.min_samples_for_model),
            min_validation_r2=float(config.min_validation_r2),
            promotion_margin=float(config.promotion_margin),
            max_sample_rejection_ratio=float(config.max_sample_rejection_ratio),
        )

    def window_s(self, cadence_s: float | None = None) -> float:
        cadence = float(cadence_s or self.assumed_cadence_s)
        if cadence <= 0 or cadence != cadence:
            cadence = self.assumed_cadence_s
        raw = cadence * self.window_cadences
        return max(self.min_window_s, min(self.max_window_s, raw))

    # -- event isolation ------------------------------------------------- #
    def isolation_timings(
        self,
        device_id: str | None = None,
        device_classes: Sequence[str] = (),
    ) -> dict[str, float]:
        """Resolve the isolation timings for one device.

        Layering is ``device_id`` override > first matching class label >
        global default, so the global ``concurrency_window_s`` stays the
        documented default instead of a universal answer.  The returned dict
        always carries all :data:`ISOLATION_TIMING_KEYS`.
        """
        timings: dict[str, float] = {
            "analysis_offset_s": float(self.analysis_offset_s),
            "isolation_window_s": float(self.concurrency_window_s),
            "power_window_s": float(self.power_analysis_window_s),
        }
        labels = device_classes or (self.isolation_device_classes.get(device_id or "") or ())
        for label in labels:
            override = self.isolation_by_class.get(str(label))
            if override:
                timings.update(_clean_timing_map(override))
                break
        if device_id:
            override = self.isolation_by_device.get(str(device_id))
            if override:
                timings.update(_clean_timing_map(override))
        if timings["power_window_s"] < timings["isolation_window_s"]:
            timings["power_window_s"] = timings["isolation_window_s"]
        return timings

    def concurrency_window_for(
        self,
        device_id: str | None = None,
        device_classes: Sequence[str] = (),
    ) -> float:
        """The window actually applied to ``device_id``, in seconds."""
        return self.isolation_timings(device_id, device_classes)["isolation_window_s"]


def _clean_timing_map(values: Mapping[str, Any]) -> dict[str, float]:
    """Keep only known timing keys with finite, non-negative values."""
    out: dict[str, float] = {}
    for key in ISOLATION_TIMING_KEYS:
        if key not in values:
            continue
        try:
            number = float(values[key])
        except (TypeError, ValueError):
            continue
        if number != number or number in (float("inf"), float("-inf")) or number < 0.0:
            continue
        out[key] = number
    return out


__all__ = ["ISOLATION_TIMING_KEYS", "LearningThresholds"]
