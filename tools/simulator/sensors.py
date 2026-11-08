"""Measurement imperfection layer (applied *after* ground truth).

The ordering matters for testability: ground truth is computed first, then a
channel-specific pipeline degrades it into what an observer would actually see:

    truth -> gain/drift/polarity -> additive noise -> resolution quantization
          -> delayed delivery -> staleness/repeats -> dropouts/outages

Nothing here may write back into ground truth.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from enum import Enum

from wattmosaic_engine.contracts import Polarity, QualityStatus

from .topology import SimChannel


class QualityLevel(str, Enum):
    """Coarse scenario quality class, used for stratified metric reporting."""

    EXCELLENT = "excellent"
    GOOD = "good"
    DEGRADED = "degraded"
    POOR = "poor"


QUALITY_PRESETS: dict[QualityLevel, dict[str, float]] = {
    # noise_sigma: relative noise std at 1 kW
    # drop_prob: per-sample missing probability
    # stale_prob: probability a delivered sample is a repeated previous value
    # delay_s: mean delivery delay
    # jitter_s: sampling interval jitter (fraction of interval)
    QualityLevel.EXCELLENT: {
        "noise_sigma": 0.004, "drop_prob": 0.0, "stale_prob": 0.0,
        "delay_s": 0.0, "jitter_s": 0.01,
    },
    QualityLevel.GOOD: {
        "noise_sigma": 0.010, "drop_prob": 0.01, "stale_prob": 0.02,
        "delay_s": 0.4, "jitter_s": 0.05,
    },
    QualityLevel.DEGRADED: {
        "noise_sigma": 0.030, "drop_prob": 0.05, "stale_prob": 0.08,
        "delay_s": 2.0, "jitter_s": 0.20,
    },
    QualityLevel.POOR: {
        "noise_sigma": 0.080, "drop_prob": 0.15, "stale_prob": 0.20,
        "delay_s": 6.0, "jitter_s": 0.40,
    },
}


@dataclass(slots=True)
class ChannelImpairment:
    ct_id: str
    quality: QualityLevel = QualityLevel.GOOD
    noise_sigma: float = 0.010
    drop_prob: float = 0.01
    stale_prob: float = 0.02
    delay_s: float = 0.4
    jitter_s: float = 0.05
    gain: float = 1.0            # static calibration error
    drift_ppm_per_hour: float = 0.0  # slow gain drift
    polarity: Polarity = Polarity.NORMAL
    resolution_w: float = 0.001
    outage_windows: list[tuple[float, float]] = field(default_factory=list)

    @classmethod
    def from_quality(cls, channel: SimChannel, quality: QualityLevel) -> ChannelImpairment:
        preset = QUALITY_PRESETS[quality]
        return cls(
            ct_id=channel.ct_id,
            quality=quality,
            noise_sigma=preset["noise_sigma"],
            drop_prob=preset["drop_prob"],
            stale_prob=preset["stale_prob"],
            delay_s=preset["delay_s"],
            jitter_s=preset["jitter_s"],
            polarity=channel.polarity,
            resolution_w=channel.resolution_w,
        )


def _quantize(value: float, resolution: float) -> float:
    if resolution <= 0:
        return value
    return round(value / resolution) * resolution


def _sensor_rng(seed: int, seed_key: str) -> random.Random:
    h = 0
    for ch in seed_key:
        h = (h * 131 + ord(ch)) & 0xFFFFFFFF
    return random.Random((seed * 7_919_999) ^ h)


@dataclass(slots=True)
class Observation:
    """What the simulated sensor stack reports for one physical sample time."""

    ct_id: str
    entity_id: str
    sample_time: float          # physical sample time (sim seconds)
    iot_truth_w: float          # pre-impairment channel truth
    reported_w: float           # after noise/gain/polarity/quantization
    state_timestamp: float      # HA state timestamp (sim seconds)
    received_at: float          # engine receipt (sim seconds)
    physical_timestamp: float | None
    repeated: bool
    quality: QualityStatus
    sequence: int
    noise_w: float
    stale: bool = False
    dropped_by_outage: bool = False

    @property
    def value_w(self) -> float:
        """Polarity-corrected value (what the engine should use)."""
        return self.reported_w


class SensorPipeline:
    """Deterministic per-channel degradation of ground-truth series."""

    def __init__(self, seed: int) -> None:
        self.seed = seed
        self._rngs: dict[str, random.Random] = {}
        self._sequence: dict[str, int] = {}

    def rng(self, seed_key: str) -> random.Random:
        r = self._rngs.get(seed_key)
        if r is None:
            r = _sensor_rng(self.seed, seed_key)
            self._rngs[seed_key] = r
        return r

    def observe(
        self,
        channel: SimChannel,
        impairment: ChannelImpairment,
        sample_time: float,
        truth_w: float,
        *,
        previous_reported: float | None = None,
        outage: bool = False,
    ) -> Observation | None:
        """Produce one observation, or ``None`` if the sample was dropped."""
        rng = self.rng(channel.seed_key or channel.ct_id)
        hour = sample_time / 3600.0

        gain = impairment.gain * (1.0 + impairment.drift_ppm_per_hour * 1e-6 * hour)
        # Noise scales with sqrt(power) so small standby loads are not swamped
        # by a constant absolute error, plus a small fixed term.
        sigma = impairment.noise_sigma * math.sqrt(max(abs(truth_w), 1.0)) * 3.0 + 0.05
        noise = rng.gauss(0.0, sigma)
        value = truth_w * gain + noise

        sign = -1.0 if impairment.polarity == Polarity.INVERTED else 1.0
        value *= sign
        value = _quantize(value, impairment.resolution_w)

        seq = self._sequence.get(channel.ct_id, 0)
        self._sequence[channel.ct_id] = seq + 1

        delay = max(0.0, rng.expovariate(1.0 / impairment.delay_s)) if impairment.delay_s > 0 else 0.0
        state_t = sample_time + delay
        received = state_t + 0.05 + rng.uniform(0, 0.15)
        physical = sample_time if rng.random() < 0.25 else None

        quality = QualityStatus.GOOD
        if outage:
            quality = QualityStatus.MISSING
            return Observation(
                ct_id=channel.ct_id, entity_id=channel.entity_id, sample_time=sample_time,
                iot_truth_w=truth_w, reported_w=previous_reported if previous_reported is not None else value,
                state_timestamp=state_t, received_at=received, physical_timestamp=physical,
                repeated=True, quality=quality, sequence=seq, noise_w=0.0,
                stale=True, dropped_by_outage=True,
            )

        if rng.random() < impairment.drop_prob:
            return None

        repeated = False
        stale = False
        if previous_reported is not None and rng.random() < impairment.stale_prob:
            value = previous_reported
            repeated = True
            stale = True
            quality = QualityStatus.STALE
        elif previous_reported is not None and abs(value - previous_reported) < impairment.resolution_w * 0.5:
            repeated = True
            if impairment.quality in (QualityLevel.DEGRADED, QualityLevel.POOR):
                quality = QualityStatus.DEGRADED

        # Delivered samples inherit the quality of their impairment.
        if quality == QualityStatus.GOOD and impairment.quality == QualityLevel.POOR:
            quality = QualityStatus.DEGRADED

        return Observation(
            ct_id=channel.ct_id, entity_id=channel.entity_id, sample_time=sample_time,
            iot_truth_w=truth_w, reported_w=value, state_timestamp=state_t,
            received_at=received, physical_timestamp=physical, repeated=repeated,
            quality=quality, sequence=seq, noise_w=noise, stale=stale,
        )


def sampled_times(
    interval_s: float, horizon_s: float, *, rng: random.Random, jitter: float, phase: float = 0.0
) -> list[float]:
    """Generate sampling instants with bounded jitter (never reordered)."""
    out: list[float] = []
    t = phase
    while t < horizon_s:
        out.append(t)
        step = interval_s * (1.0 + rng.uniform(-jitter, jitter))
        t += max(1.0, step)
    return out
