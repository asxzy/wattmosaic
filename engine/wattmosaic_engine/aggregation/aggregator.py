"""Energy / power aggregation and device-run tracking.

Integration rules
-----------------
* Energy is integrated with the **trapezoidal rule over actual elapsed time**
  between consecutive inference results for a device.  No sample rate is
  assumed; timestamp jitter is handled exactly.
* An interval longer than ``stale_after_s`` is a *gap*: no energy is accrued
  across it.  This prevents a sensor/engine outage from being silently turned
  into consumed kWh.
* The interval is split at bucket boundaries so a 5-minute bucket never
  inherits energy from a neighbouring bucket, even when results are sparse.
* Buckets are persisted through the storage layer's public
  ``upsert_energy_aggregate`` / ``upsert_power_aggregate`` methods.
* Runs are persisted through :class:`DeviceRunRepository` (see its docstring
  for why it owns a second WAL connection).

Restart safety
--------------
``recompute_incomplete_buckets()`` discovers the last *complete* persisted
bucket for each device and bucket size, replays stored ``inference_results``
from that boundary, and re-persists the affected buckets.  Because the
integration is deterministic, this repairs any partially written bucket after
a crash without double counting.
"""

from __future__ import annotations

import logging
import math
import uuid
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from ..contracts import InferenceResult, OperatingStage, Source, StageStatus
from ..storage.database import Database
from .models import DeviceRun, EnergyBucket, _RunTracker
from .repository import DeviceRunRepository

_LOGGER = logging.getLogger(__name__)

DEFAULT_BUCKET_SIZES: tuple[int, ...] = (300, 3600)


@dataclass(slots=True)
class _Accumulator:
    bucket_start: datetime
    bucket_s: int
    energy_kwh: float = 0.0
    power_sum: float = 0.0
    power_count: int = 0
    power_min: float = math.inf
    power_max: float = -math.inf
    sample_count: int = 0
    source: str | None = None

    def add_power(self, power_w: float, source: str | None) -> None:
        if power_w != power_w:
            return
        self.power_sum += power_w
        self.power_count += 1
        self.power_min = min(self.power_min, power_w)
        self.power_max = max(self.power_max, power_w)
        self.sample_count += 1
        if self.source is None and source is not None:
            self.source = source

    @property
    def mean_power_w(self) -> float:
        return self.power_sum / self.power_count if self.power_count else 0.0

    def to_bucket(self, device_id: str, *, complete: bool) -> EnergyBucket:
        return EnergyBucket(
            device_id=device_id,
            bucket_start=self.bucket_start,
            bucket_s=self.bucket_s,
            energy_kwh=self.energy_kwh,
            mean_power_w=self.mean_power_w,
            min_power_w=0.0 if self.power_count == 0 else self.power_min,
            max_power_w=0.0 if self.power_count == 0 else self.power_max,
            sample_count=self.sample_count,
            complete=complete,
            source=self.source,
        )


@dataclass(slots=True)
class _DeviceState:
    device_id: str
    last_at: datetime | None = None
    last_power_w: float = 0.0
    last_source: str | None = None
    accumulators: dict[int, dict[datetime, _Accumulator]] = field(
        default_factory=lambda: defaultdict(dict)
    )
    run: _RunTracker | None = None
    runs: list[DeviceRun] = field(default_factory=list)


def _floor_bucket(at: datetime, bucket_s: int) -> datetime:
    epoch = at.timestamp()
    floored = math.floor(epoch / bucket_s) * bucket_s
    return datetime.fromtimestamp(floored, tz=UTC)


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


class EnergyAggregator:
    """Accumulates per-device energy/power buckets and detects device runs."""

    def __init__(
        self,
        database: Database,
        *,
        bucket_sizes: Sequence[int] = DEFAULT_BUCKET_SIZES,
        stale_after_s: float = 120.0,
        #: power below this ends / does not start a run (standby/off floor)
        min_run_power_w: float = 5.0,
        #: a run ends after this long below ``min_run_power_w``
        run_end_grace_s: float = 90.0,
        #: never accrue more than this across one interval without a sample
        max_interval_s: float | None = None,
        repository: DeviceRunRepository | None = None,
    ) -> None:
        self._db = database
        self.bucket_sizes = tuple(sorted({int(b) for b in bucket_sizes if int(b) > 0}))
        if not self.bucket_sizes:
            raise ValueError("bucket_sizes must contain at least one positive size")
        self.stale_after_s = float(stale_after_s)
        self.min_run_power_w = float(min_run_power_w)
        self.run_end_grace_s = float(run_end_grace_s)
        self.max_interval_s = float(max_interval_s) if max_interval_s else self.stale_after_s
        self.repository = repository or DeviceRunRepository(database)
        self._devices: dict[str, _DeviceState] = {}
        self._closed_runs: list[DeviceRun] = []
        self._persist_sink: list[EnergyBucket] | None = None
        # Cumulative-total bookkeeping (see ``current_total_kwh``).  Kept in
        # memory so reporting a running total does not open one SQLite
        # connection per device per inference cycle.
        self._persisted_energy: dict[tuple[str, int, datetime], float] = {}
        self._base_totals: dict[tuple[str, int], float] = {}

    # ------------------------------------------------------------------ #
    # Ingestion
    # ------------------------------------------------------------------ #
    def add_result(self, result: InferenceResult, at: datetime | None = None) -> None:
        """Ingest one inference result.

        Out-of-order samples are ignored for integration (the interval would be
        negative); they still contribute no energy.  This keeps replay
        deterministic.
        """
        at = _as_utc(at or result.timestamp)
        device = self._devices.get(result.device_id)
        if device is None:
            device = _DeviceState(device_id=result.device_id)
            self._devices[result.device_id] = device
        power = float(result.power_w)
        if power != power or math.isinf(power):
            return
        source = result.source.value if hasattr(result.source, "value") else str(result.source)

        if device.last_at is None:
            self._seed(device, result.device_id, at, power, source)
            return

        previous_at = device.last_at
        if at <= previous_at:
            # Duplicate/out-of-order: do not accrue, but keep the newest power
            # for the next forward interval if it is genuinely newer.
            return
        elapsed = (at - previous_at).total_seconds()
        if elapsed <= self.max_interval_s:
            self._accrue(device, result.device_id, previous_at, device.last_power_w, at, power)
        # else: gap — deliberately no energy across it.

        for bucket_s in self.bucket_sizes:
            start = _floor_bucket(at, bucket_s)
            accum = device.accumulators[bucket_s].setdefault(start, _Accumulator(start, bucket_s))
            accum.add_power(power, source)
            self._finalize_before(device, bucket_s, start)

        self._track_run(device, result, at, power, source)
        device.last_at = at
        device.last_power_w = power
        device.last_source = source

        # Safety: never keep unbounded bucket history in memory.
        self._trim(device)

    def _seed(self, device: _DeviceState, device_id: str, at: datetime, power: float, source: str) -> None:
        for bucket_s in self.bucket_sizes:
            start = _floor_bucket(at, bucket_s)
            accum = device.accumulators[bucket_s].setdefault(start, _Accumulator(start, bucket_s))
            accum.add_power(power, source)
        device.last_at = at
        device.last_power_w = power
        device.last_source = source
        self._start_run_if_needed(device, device_id, at, power, source)

    def _accrue(
        self,
        device: _DeviceState,
        device_id: str,
        t0: datetime,
        p0: float,
        t1: datetime,
        p1: float,
    ) -> None:
        total_s = (t1 - t0).total_seconds()
        if total_s <= 0 or total_s > self.max_interval_s:
            return

        def power_at(offset_s: float) -> float:
            if total_s <= 0:
                return p1
            frac = max(0.0, min(1.0, offset_s / total_s))
            return p0 + (p1 - p0) * frac

        for bucket_s in self.bucket_sizes:
            cursor = t0
            while cursor < t1:
                start = _floor_bucket(cursor, bucket_s)
                end = start + timedelta(seconds=bucket_s)
                segment_end = min(t1, end)
                dt = (segment_end - cursor).total_seconds()
                if dt > 0:
                    e0 = power_at((cursor - t0).total_seconds())
                    e1 = power_at((segment_end - t0).total_seconds())
                    accum = device.accumulators[bucket_s].setdefault(start, _Accumulator(start, bucket_s))
                    accum.energy_kwh += max(0.0, (e0 + e1) / 2.0) * dt / 3_600_000.0
                cursor = segment_end
            # If the interval ends exactly on a boundary, advance cursor safely.
            if t1 > cursor:
                cursor = t1

    def _finalize_before(self, device: _DeviceState, bucket_s: int, current_start: datetime) -> None:
        pending = device.accumulators[bucket_s]
        # Only finalize buckets that are complete AND no longer the newest one:
        # the bucket that just received this sample may still gain more samples,
        # so persisting it now would be premature.  A bucket strictly before the
        # current bucket is guaranteed complete.
        completed = [start for start in pending if start < current_start]
        for start in sorted(completed):
            accum = pending.pop(start)
            bucket = accum.to_bucket(device.device_id, complete=True)
            self._persist_bucket(bucket)

    def _persist_bucket(self, bucket: EnergyBucket) -> None:
        if self._persist_sink is not None:
            self._persist_sink.append(bucket)
        # Snapshot the pre-write state so a failed write cannot desynchronise the
        # cumulative total: should the upsert raise, nothing is recorded.  The
        # repository reads happen at most once per (device, bucket size) or per
        # bucket; steady-state cycles do no SQL for this bookkeeping at all.
        key = (bucket.device_id, bucket.bucket_s, bucket.bucket_start)
        totals_key = (bucket.device_id, bucket.bucket_s)
        previous = self._persisted_energy.get(key)
        if previous is None:
            previous = self._bucket_total(*key)
        base = self._base_totals.get(totals_key)
        if base is None:
            base = self._db_total(*totals_key)
        try:
            self._db.upsert_energy_aggregate(
                bucket.device_id, bucket.bucket_start, bucket.bucket_s, bucket.energy_kwh
            )
            self._db.upsert_power_aggregate(
                bucket.device_id,
                bucket.bucket_start,
                bucket.bucket_s,
                bucket.mean_power_w,
                bucket.min_power_w,
                bucket.max_power_w,
                bucket.sample_count,
            )
        except Exception as exc:  # never let persistence kill the inference loop
            _LOGGER.warning("failed to persist %ss bucket for %s: %s", bucket.bucket_s, bucket.device_id, exc)
            return
        # The same bucket row is overwritten (upsert) whenever an open bucket is
        # flushed again, so only the delta contributes to the running total.
        self._persisted_energy[key] = float(bucket.energy_kwh)
        self._base_totals[totals_key] = base + float(bucket.energy_kwh) - previous

    def _trim(self, device: _DeviceState, keep: int = 6) -> None:
        for accumulators in device.accumulators.values():
            if len(accumulators) <= keep:
                continue
            for start in sorted(accumulators)[: len(accumulators) - keep]:
                accumulators.pop(start, None)

    # ------------------------------------------------------------------ #
    # Runs
    # ------------------------------------------------------------------ #
    def _start_run_if_needed(
        self, device: _DeviceState, device_id: str, at: datetime, power: float, source: str
    ) -> None:
        if device.run is not None:
            return
        if power < self.min_run_power_w:
            return
        device.run = _RunTracker(
            device_id=device_id,
            started_at=at,
            last_at=at,
            last_active_at=at,
            last_power_w=power,
            peak_power_w=power,
            sample_count=1,
            points=[(at, power)],
            source=_source_enum(source),
        )

    def _track_run(
        self, device: _DeviceState, result: InferenceResult, at: datetime, power: float, source: str
    ) -> None:
        run = device.run
        if run is None:
            self._start_run_if_needed(device, result.device_id, at, power, source)
            return
        gap = (at - run.last_at).total_seconds()
        if gap > self.stale_after_s:
            # A gap ends the run: the appliance may have finished and restarted
            # while we were blind.  Close at the last active sample.
            self._close_run(device, ended_at=run.last_active_at, reason="gap")
            self._start_run_if_needed(device, result.device_id, at, power, source)
            return
        # trapezoidal energy for the run
        run.energy_kwh += max(0.0, (run.last_power_w + power) / 2.0) * gap / 3_600_000.0
        run.sample_count += 1
        run.points.append((at, power))
        run.peak_power_w = max(run.peak_power_w, power)
        run.last_power_w = power
        run.last_at = at
        if power >= self.min_run_power_w:
            run.last_active_at = at
            # Snapshot the energy accrued up to the last active sample.  The run
            # is reported as ending here, so the ramp-down interval that follows
            # must not be attributed to the shorter reported runtime.
            run.active_energy_kwh = run.energy_kwh
        elif (at - run.last_active_at).total_seconds() >= self.run_end_grace_s:
            self._close_run(device, ended_at=run.last_active_at, reason="idle")
        # bound memory for very long runs
        if len(run.points) > 20_000:
            run.points = run.points[-10_000:]

    def _close_run(self, device: _DeviceState, *, ended_at: datetime, reason: str) -> DeviceRun | None:
        tracker = device.run
        if tracker is None:
            return None
        device.run = None
        runtime = max(1.0, (ended_at - tracker.started_at).total_seconds())
        # Attribute only the energy accrued while the device was actually
        # active.  Tracking the truncated-decay tail would inflate the mean
        # above the observed peak.
        energy = max(tracker.active_energy_kwh, 0.0)
        stages: list[OperatingStage] = []
        stage_status = StageStatus.UNKNOWN
        if len(tracker.points) >= 4:
            try:
                from ..learning.stages import recognize_stages

                stages = recognize_stages(tracker.points)
                if stages and all(stage.status != StageStatus.UNKNOWN.value for stage in stages):
                    stage_status = StageStatus.IDENTIFIED
                elif stages:
                    stage_status = StageStatus.UNKNOWN
            except Exception:
                stages = []
        run = DeviceRun(
            run_id=uuid.uuid4().hex,
            device_id=tracker.device_id,
            started_at=tracker.started_at,
            ended_at=ended_at,
            energy_kwh=energy,
            peak_power_w=tracker.peak_power_w,
            mean_power_w=energy * 3_600_000.0 / runtime,
            stages=stages,
            stage_status=stage_status,
            source=tracker.source,
            sample_count=tracker.sample_count,
        )
        if reason == "gap":
            run.stages = [
                *stages,
                OperatingStage(
                    name="run_end",
                    status=StageStatus.UNKNOWN,
                    power_mean_w=tracker.last_power_w,
                    notes=["ended_on_measurement_gap"],
                ),
            ]
        device.runs.append(run)
        self._closed_runs.append(run)
        try:
            self.repository.save_run(run)
        except Exception as exc:
            _LOGGER.warning("failed to persist run for %s: %s", tracker.device_id, exc)
        return run

    def close_open_runs(self, now: datetime | None = None) -> list[DeviceRun]:
        """Close runs whose idle grace has elapsed (call periodically)."""
        now = _as_utc(now or datetime.now(UTC))
        closed: list[DeviceRun] = []
        for device in self._devices.values():
            run = device.run
            if run is None:
                continue
            if (now - run.last_active_at).total_seconds() >= self.run_end_grace_s:
                result = self._close_run(device, ended_at=run.last_active_at, reason="idle")
                if result is not None:
                    closed.append(result)
        return closed

    # ------------------------------------------------------------------ #
    # Flushing
    # ------------------------------------------------------------------ #
    def flush(self, now: datetime | None = None) -> list[EnergyBucket]:
        """Persist completed buckets; persist the open one as incomplete.

        Returns all buckets written during this call (completed first, then
        the still-open bucket for each device/size).  Persisting the open
        bucket is what makes a crash recoverable: ``recompute_incomplete_buckets``
        will overwrite it from ``inference_results`` on the next start.

        A bucket is only *dropped from memory* when it is complete **and** the
        device can no longer feed it.  Completeness is judged against ``now``,
        which in production is wall-clock time while samples carry HA timestamps
        and inference cycles can lag.  Dropping purely on ``now`` would let a
        later sample re-open the bucket with a fresh accumulator, silently
        discarding the energy already accrued in it.
        """
        now = _as_utc(now or datetime.now(UTC))
        written: list[EnergyBucket] = []
        for device_id, device in list(self._devices.items()):
            last_at = device.last_at
            for bucket_s, accumulators in device.accumulators.items():
                for start in sorted(list(accumulators)):
                    accum = accumulators[start]
                    end = start + timedelta(seconds=bucket_s)
                    complete = end <= now
                    # Keep the accumulator alive while it could still receive a
                    # sample; the persisted row is an upsert, so keeping it is
                    # free of double counting.
                    if complete and last_at is not None and last_at >= end:
                        accumulators.pop(start, None)
                    bucket = accum.to_bucket(device_id, complete=complete)
                    self._persist_bucket(bucket)
                    written.append(bucket)
        self.close_open_runs(now)
        return written

    # ------------------------------------------------------------------ #
    # Restart recovery
    # ------------------------------------------------------------------ #
    def recompute_incomplete_buckets(
        self,
        now: datetime | None = None,
        *,
        device_ids: Sequence[str] | None = None,
        lookback_hours: float = 72.0,
        recovery_buckets: int = 4,
    ) -> list[EnergyBucket]:
        """Rebuild buckets that may have been torn by a crash.

        For each device and bucket size we replay from ``recovery_buckets``
        buckets before the most recently persisted bucket start (clamped to
        ``lookback_hours``).  Replaying the last few buckets — not only the most
        recent one — is what repairs a write that was torn while several
        buckets were being flushed; the integration is deterministic, so every
        replayed bucket is recomputed to exactly the value it would have had.
        Upserts overwrite in place, so this never double counts.

        ``flush`` persists the still-open bucket too, which is why the replay
        window is anchored on the last *persisted* row rather than on a
        completeness flag (``energy_aggregates`` has no such column).
        """
        now = _as_utc(now or datetime.now(UTC))
        since = now - timedelta(hours=max(1.0, lookback_hours))
        devices = list(device_ids) if device_ids else self._discover_devices(since)
        extra = max(0, int(recovery_buckets))
        rebuilt: list[EnergyBucket] = []
        for device_id in devices:
            history = self._db.inference_history(device_id, since, limit=100_000)
            if not history:
                continue
            points = self._history_points(history)
            if not points:
                continue
            replays: dict[int, datetime] = {}
            for bucket_s in self.bucket_sizes:
                last_bucket = self._last_complete_bucket(device_id, bucket_s)
                if last_bucket is not None:
                    offset = timedelta(seconds=bucket_s * extra)
                    replays[bucket_s] = max(since, last_bucket - offset)
                else:
                    replays[bucket_s] = since
            replay_from = min(replays.values())
            # Seed with the last sample at/before replay_from for continuity.
            seed = [p for p in points if p[0] <= replay_from]
            replay = [p for p in points if p[0] >= replay_from]
            if seed and (not replay or seed[-1][0] != replay[0][0]):
                replay = [seed[-1], *replay]

            device = self._devices.get(device_id) or _DeviceState(device_id=device_id)
            self._devices[device_id] = device
            device.last_at = None
            device.accumulators = defaultdict(dict)
            device.run = None
            for at, power, source in replay:
                result = InferenceResult(
                    device_id=device_id,
                    timestamp=at,
                    power_w=power,
                    source=_source_enum(source),
                    confidence=0.0,
                )
                self.add_result(result, at)
            sink: list[EnergyBucket] = []
            self._persist_sink = sink
            try:
                for accumulators in device.accumulators.values():
                    for _start, accum in sorted(accumulators.items()):
                        self._persist_bucket(accum.to_bucket(device_id, complete=False))
            finally:
                self._persist_sink = None
            rebuilt.extend(sink)
        return rebuilt

    def _discover_devices(self, since: datetime) -> list[str]:
        try:
            return self.repository.distinct_inference_devices(since)
        except Exception:
            return list(self._devices)

    def _last_complete_bucket(self, device_id: str, bucket_s: int) -> datetime | None:
        try:
            return self.repository.last_bucket(device_id, bucket_s)
        except Exception:
            return None

    @staticmethod
    def _history_points(history: Iterable[dict[str, Any]]) -> list[tuple[datetime, float, str]]:
        points: list[tuple[datetime, float, str]] = []
        for row in history:
            raw = row.get("timestamp")
            if hasattr(raw, "tzinfo"):
                at = _as_utc(raw)  # type: ignore[arg-type]
            else:
                try:
                    at = _as_utc(datetime.fromisoformat(str(raw).replace("Z", "+00:00")))
                except (TypeError, ValueError):
                    continue
            try:
                power = float(row.get("power_w"))
            except (TypeError, ValueError):
                continue
            source = str(row.get("source") or Source.INFERRED.value)
            points.append((at, power, source))
        points.sort(key=lambda item: item[0])
        return points

    # ------------------------------------------------------------------ #
    # Query helpers
    # ------------------------------------------------------------------ #
    def energy_total(self, device_id: str, bucket_s: int | None = None) -> float:
        """Total persisted energy for a device.

        ``energy_aggregates`` is keyed by ``(device_id, bucket_start, bucket_s)``,
        so the raw table stores the *same* energy once per requested bucket
        size.  Summing across sizes would double count.  This method therefore
        sums exactly one bucket size (default: the smallest configured size,
        i.e. the most detailed series) and documents that behaviour.
        """
        size = int(bucket_s) if bucket_s is not None else self.bucket_sizes[0]
        return self.repository.energy_total(device_id, size)

    def current_total_kwh(self, device_id: str, bucket_s: int | None = None) -> float:
        """Running lifetime total for ``device_id``, including the open bucket.

        This is what a ``total_increasing`` HA sensor must publish.  It is
        distinct from :meth:`energy_total`, which only sums *persisted* rows:
        the bucket currently being filled has not been finalized yet, so
        publishing ``energy_total`` alone would freeze the sensor for up to one
        bucket duration.

        Cost is O(open buckets) after the first call: the persisted base is
        seeded once per ``(device, bucket size)`` and thereafter maintained from
        upsert deltas, so this does not open a SQLite connection per cycle.
        """
        size = int(bucket_s) if bucket_s is not None else self.bucket_sizes[0]
        key = (device_id, size)
        base = self._base_totals.get(key)
        if base is None:
            base = self._db_total(device_id, size)
        device = self._devices.get(device_id)
        live = 0.0  # accumulated energy of buckets still open in memory
        already_persisted = 0.0  # last value written for those same buckets
        if device is not None:
            for start, accum in device.accumulators.get(size, {}).items():
                live += accum.energy_kwh
                already_persisted += self._persisted_energy.get((device_id, size, start), 0.0)
        # ``base`` already counts every row written so far, including the stale
        # value of each open bucket; replace those with the live value.
        return max(0.0, base - already_persisted + live)

    def _db_total(self, device_id: str, bucket_s: int) -> float:
        try:
            return float(self.repository.energy_total(device_id, bucket_s))
        except Exception:  # noqa: BLE001 - a reporting read must not raise
            return 0.0

    def _bucket_total(self, device_id: str, bucket_s: int, bucket_start: datetime) -> float:
        try:
            return float(self.repository.bucket_energy(device_id, bucket_start, bucket_s))
        except Exception:  # noqa: BLE001 - a reporting read must not raise
            return 0.0

    def runs(self, device_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        return self.repository.runs(device_id, limit=limit)

    def take_closed_runs(self) -> list[DeviceRun]:
        """Drain the in-memory closed-run log (for tests / API publication)."""
        out, self._closed_runs = self._closed_runs, []
        return out


def _source_enum(value: str) -> Source:
    try:
        return Source(str(value))
    except ValueError:
        return Source.INFERRED


__all__ = ["DEFAULT_BUCKET_SIZES", "DeviceRun", "EnergyAggregator", "EnergyBucket"]
