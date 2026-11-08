"""Aggregation value objects (energy buckets and appliance runs)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ..contracts import OperatingStage, Source, StageStatus


@dataclass(slots=True)
class EnergyBucket:
    device_id: str
    bucket_start: datetime
    bucket_s: int
    energy_kwh: float
    mean_power_w: float = 0.0
    min_power_w: float = 0.0
    max_power_w: float = 0.0
    sample_count: int = 0
    complete: bool = False
    source: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "device_id": self.device_id,
            "bucket_start": self.bucket_start.isoformat(),
            "bucket_s": self.bucket_s,
            "energy_kwh": self.energy_kwh,
            "mean_power_w": self.mean_power_w,
            "min_power_w": self.min_power_w,
            "max_power_w": self.max_power_w,
            "sample_count": self.sample_count,
            "complete": self.complete,
            "source": self.source,
        }


@dataclass(slots=True)
class DeviceRun:
    """One detected device run (a period of meaningful consumption)."""

    run_id: str
    device_id: str
    started_at: datetime
    ended_at: datetime | None = None
    energy_kwh: float = 0.0
    peak_power_w: float = 0.0
    mean_power_w: float = 0.0
    stages: list[OperatingStage] = field(default_factory=list)
    stage_status: StageStatus = StageStatus.UNKNOWN
    source: Source = Source.INFERRED
    sample_count: int = 0

    @property
    def runtime_s(self) -> float:
        if self.ended_at is None:
            return 0.0
        return max(0.0, (self.ended_at - self.started_at).total_seconds())

    def as_row(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "device_id": self.device_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "energy_kwh": self.energy_kwh,
            "peak_power_w": self.peak_power_w,
            "mean_power_w": self.mean_power_w,
            "stages": [stage.model_dump(mode="json") for stage in self.stages],
            "source": self.source.value if hasattr(self.source, "value") else str(self.source),
        }


@dataclass(slots=True)
class _RunTracker:
    """Mutable in-progress run state."""

    device_id: str
    started_at: datetime
    last_at: datetime
    last_active_at: datetime
    energy_kwh: float = 0.0
    #: energy accrued up to and including ``last_active_at``.  A run closes at
    #: its last *active* sample, so this — not ``energy_kwh`` — is the energy
    #: attributable to the reported runtime.  Without it the ramp-down interval
    #: would be counted against a shorter runtime and ``mean_power_w`` could
    #: exceed ``peak_power_w``.
    active_energy_kwh: float = 0.0
    peak_power_w: float = 0.0
    sample_count: int = 0
    last_power_w: float = 0.0
    points: list[tuple[datetime, float]] = field(default_factory=list)
    source: Source = Source.INFERRED


__all__ = ["DeviceRun", "EnergyBucket"]
