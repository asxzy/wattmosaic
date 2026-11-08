"""Unit tests for energy aggregation, bucketing and device-run tracking.

What is proven here:

* trapezoidal integration over real elapsed time yields the analytically
  correct kWh for a constant load, a ramp, and a load that changes state
  mid-bucket,
* intervals longer than the stale threshold accrue nothing (an outage is never
  turned into consumed energy), and out-of-order/duplicate results are ignored,
* buckets are keyed by bucket size without double counting: ``energy_total``
  sums exactly one resolution even though every resolution stores the same
  energy,
* an open bucket is persisted as *incomplete* and is not treated as final,
  while the next flush overwrites it rather than adding to it,
* a fresh aggregator instance replays stored inference history and repairs a
  corrupted bucket without double counting,
* device runs open only above the power floor, close after the idle grace (or
  on a measurement gap), carry the right energy/peak/runtime, get segmented
  stages, and are persisted to ``device_runs``/``operating_stages``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from wattmosaic_engine.aggregation import (
    DeviceRunRepository,
    EnergyAggregator,
    EnergyBucket,
    run_from_row,
)
from wattmosaic_engine.contracts import InferenceResult, OperatingStage, Source, StageStatus
from wattmosaic_engine.storage import Database

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


@pytest.fixture()
def db(tmp_db_path):
    database = Database(tmp_db_path)
    try:
        yield database
    finally:
        database.close()


def _result(device_id: str, power_w: float, at: datetime | None = None) -> InferenceResult:
    return InferenceResult(
        device_id=device_id,
        timestamp=at or T0,
        power_w=power_w,
        source=Source.INFERRED,
        confidence=1.0,
    )


def _feed(
    aggregator: EnergyAggregator,
    device_id: str,
    points: list[tuple[datetime, float]],
) -> None:
    for at, power in points:
        aggregator.add_result(_result(device_id, power, at), at)


def _constant(device_id: str, watts: float, seconds: int, step: float = 15.0):
    return [(T0 + timedelta(seconds=offset), watts) for offset in range(0, seconds + 1, int(step))]


def _persisted_rows(db: Database, device_id: str) -> list[dict]:
    with db._lock:  # noqa: SLF001 - read-only inspection in tests
        rows = db._conn.execute(
            "SELECT * FROM energy_aggregates WHERE device_id=? ORDER BY bucket_start, bucket_s",
            (device_id,),
        ).fetchall()
    return [dict(row) for row in rows]


# --------------------------------------------------------------------------- #
# Energy integration
# --------------------------------------------------------------------------- #


def test_constant_one_kilowatt_for_one_hour_is_one_kwh(db: Database) -> None:
    aggregator = EnergyAggregator(db, bucket_sizes=(300, 3600), stale_after_s=120)
    _feed(aggregator, "heater", _constant("heater", 1000.0, 3600))

    aggregator.flush(now=T0 + timedelta(hours=2))

    assert aggregator.energy_total("heater") == pytest.approx(1.0)
    # Each resolution independently integrates the same physical energy.
    assert aggregator.energy_total("heater", 300) == pytest.approx(1.0)
    assert aggregator.energy_total("heater", 3600) == pytest.approx(1.0)


def test_ramp_uses_trapezoidal_rule_not_sample_count(db: Database) -> None:
    aggregator = EnergyAggregator(db, bucket_sizes=(300,), stale_after_s=300)
    # 0 W -> 1000 W linearly over one hour: area under the ramp = 0.5 kWh.
    points = [(T0 + timedelta(seconds=60 * i), 1000.0 * i / 60.0) for i in range(61)]
    _feed(aggregator, "ramp", points)

    aggregator.flush(now=T0 + timedelta(hours=2))

    assert aggregator.energy_total("ramp") == pytest.approx(0.5)


def test_partial_bucket_energy_is_exact_on_flush(db: Database) -> None:
    aggregator = EnergyAggregator(db, bucket_sizes=(300,), stale_after_s=120)
    _feed(aggregator, "dev", _constant("dev", 1000.0, 285))

    buckets = aggregator.flush(now=T0 + timedelta(seconds=290))

    assert len(buckets) == 1
    assert buckets[0].bucket_start == T0
    assert buckets[0].bucket_s == 300
    assert buckets[0].complete is False  # the 5-minute bucket is not over yet
    # 285 s of 1000 W = 285 000 W·s = 0.079166… kWh, not a rounded 0.075.
    assert buckets[0].energy_kwh == pytest.approx(285 * 1000 / 3_600_000)
    assert aggregator.energy_total("dev") == pytest.approx(285 * 1000 / 3_600_000)


def test_power_statistics_are_carried_on_the_bucket(db: Database) -> None:
    aggregator = EnergyAggregator(db, bucket_sizes=(300,), stale_after_s=120)
    _feed(
        aggregator,
        "dev",
        [
            (T0, 100.0),
            (T0 + timedelta(seconds=15), 400.0),
            (T0 + timedelta(seconds=30), 250.0),
        ],
    )

    bucket = aggregator.flush(now=T0 + timedelta(seconds=60))[0]

    assert bucket.mean_power_w == pytest.approx(250.0)
    assert bucket.min_power_w == pytest.approx(100.0)
    assert bucket.max_power_w == pytest.approx(400.0)
    assert bucket.sample_count == 3
    assert bucket.source == Source.INFERRED.value


def test_bucket_is_split_at_the_boundary(db: Database) -> None:
    aggregator = EnergyAggregator(db, bucket_sizes=(300,), stale_after_s=120)
    # Samples straddle the 12:00:00 -> 12:05:00 boundary.  1000 W for 300 s per
    # bucket; the straddling interval is split rather than attributed wholesale.
    _feed(
        aggregator,
        "dev",
        [
            (T0 + timedelta(seconds=270), 1000.0),
            (T0 + timedelta(seconds=330), 1000.0),
        ],
    )
    aggregator.flush(now=T0 + timedelta(seconds=600))

    rows = [(row["bucket_start"], row["energy_kwh"]) for row in _persisted_rows(db, "dev")]
    assert rows == [
        ("2026-01-01T12:00:00+00:00", pytest.approx(30 * 1000 / 3_600_000)),
        ("2026-01-01T12:05:00+00:00", pytest.approx(30 * 1000 / 3_600_000)),
    ]
    assert aggregator.energy_total("dev") == pytest.approx(60 * 1000 / 3_600_000)


def test_gap_accrues_no_energy(db: Database) -> None:
    aggregator = EnergyAggregator(db, bucket_sizes=(300,), stale_after_s=60)
    _feed(
        aggregator,
        "dev",
        [
            (T0, 1000.0),
            (T0 + timedelta(seconds=60), 1000.0),  # integrated: 60 s
            (T0 + timedelta(seconds=660), 1000.0),  # gap of 600 s: ignored
            (T0 + timedelta(seconds=720), 1000.0),  # integrated: 60 s
        ],
    )
    aggregator.flush(now=T0 + timedelta(seconds=2000))

    assert aggregator.energy_total("dev") == pytest.approx(120 * 1000 / 3_600_000)


def test_out_of_order_and_duplicate_results_never_double_count(db: Database) -> None:
    aggregator = EnergyAggregator(db, bucket_sizes=(300,), stale_after_s=120)
    _feed(
        aggregator,
        "dev",
        [
            (T0, 1000.0),
            (T0, 1000.0),  # duplicate timestamp: ignored
            (T0 - timedelta(seconds=15), 1000.0),  # out of order: ignored
            (T0 + timedelta(seconds=60), 1000.0),
        ],
    )
    aggregator.flush(now=T0 + timedelta(seconds=600))

    assert aggregator.energy_total("dev") == pytest.approx(60 * 1000 / 3_600_000)


def test_non_finite_power_is_ignored(db: Database) -> None:
    aggregator = EnergyAggregator(db, bucket_sizes=(300,), stale_after_s=120)
    aggregator.add_result(_result("dev", float("nan"), T0), T0)

    assert not aggregator._devices["dev"].accumulators  # noqa: SLF001 - nothing was recorded
    assert aggregator.energy_total("dev") == 0.0


def test_energy_total_is_not_multiplied_by_bucket_sizes(db: Database) -> None:
    aggregator = EnergyAggregator(db, bucket_sizes=(300, 3600), stale_after_s=120)
    _feed(aggregator, "dev", _constant("dev", 1000.0, 120))
    aggregator.flush(now=T0 + timedelta(seconds=600))

    rows = _persisted_rows(db, "dev")
    assert len(rows) == 2  # one row per bucket size
    assert aggregator.energy_total("dev") == pytest.approx(120 * 1000 / 3_600_000)
    # The raw sum across sizes would be double; the documented behaviour is not.
    with db._lock:  # noqa: SLF001
        raw = db._conn.execute(
            "SELECT SUM(energy_kwh) AS e FROM energy_aggregates WHERE device_id='dev'"
        ).fetchone()["e"]
    assert raw == pytest.approx(2 * aggregator.energy_total("dev"))


def test_aggregator_rejects_empty_bucket_configuration(db: Database) -> None:
    with pytest.raises(ValueError, match="at least one positive size"):
        EnergyAggregator(db, bucket_sizes=())


# --------------------------------------------------------------------------- #
# Running cumulative total (what a ``total_increasing`` HA sensor publishes)
# --------------------------------------------------------------------------- #


def test_current_total_includes_the_open_bucket(db: Database) -> None:
    """The sensor must advance continuously, not only when a bucket closes."""
    aggregator = EnergyAggregator(db, bucket_sizes=(300,), stale_after_s=120)
    _feed(aggregator, "dev", _constant("dev", 1000.0, 120))

    # Nothing persisted yet: energy_total is 0, but the running total is real.
    assert aggregator.energy_total("dev") == 0.0
    assert aggregator.current_total_kwh("dev") == pytest.approx(120 * 1000 / 3_600_000)

    # Feeding more inside the same bucket keeps increasing it.
    _feed(aggregator, "dev", [(T0 + timedelta(seconds=180), 1000.0)])
    assert aggregator.current_total_kwh("dev") == pytest.approx(180 * 1000 / 3_600_000)


def test_current_total_is_continuous_across_flushes(db: Database) -> None:
    aggregator = EnergyAggregator(db, bucket_sizes=(300,), stale_after_s=120)
    _feed(aggregator, "dev", _constant("dev", 1000.0, 270))
    before = aggregator.current_total_kwh("dev")

    aggregator.flush(now=T0 + timedelta(seconds=290))
    after = aggregator.current_total_kwh("dev")

    # Flushing must not double count nor lose energy.
    assert after == pytest.approx(before)
    assert after == pytest.approx(270 * 1000 / 3_600_000)


def test_current_total_never_double_counts_repeated_flushes(db: Database) -> None:
    aggregator = EnergyAggregator(db, bucket_sizes=(300,), stale_after_s=120)
    _feed(aggregator, "dev", _constant("dev", 1000.0, 270))
    for _ in range(4):
        aggregator.flush(now=T0 + timedelta(seconds=290))

    assert aggregator.current_total_kwh("dev") == pytest.approx(270 * 1000 / 3_600_000)
    assert aggregator.energy_total("dev") == pytest.approx(270 * 1000 / 3_600_000)


def test_current_total_matches_persisted_total_once_bucket_closes(db: Database) -> None:
    aggregator = EnergyAggregator(db, bucket_sizes=(300,), stale_after_s=120)
    _feed(aggregator, "dev", _constant("dev", 1000.0, 900))
    aggregator.flush(now=T0 + timedelta(seconds=1000))

    assert aggregator.current_total_kwh("dev") == pytest.approx(
        aggregator.energy_total("dev")
    )


def test_flush_with_wall_clock_now_does_not_drop_live_energy(db: Database) -> None:
    """Regression: a wall-clock flush must not let a bucket restart at zero.

    Production flushes with wall-clock ``now`` while samples carry HA
    timestamps, and inference cycles can lag behind both.  Popping a bucket as
    "complete" purely because ``now`` passed its end let the next sample re-open
    it with an empty accumulator, so the running total fell (and the HA
    ``total_increasing`` sensor would have stalled on the old value).
    """
    aggregator = EnergyAggregator(db, bucket_sizes=(300,), stale_after_s=120)
    totals = []
    for i in range(19):
        at = T0 + timedelta(seconds=15 * i)
        aggregator.add_result(_result("dev", 1000.0, at), at)
        if i == 11:
            # Wall clock far ahead of the samples, as in the real loop.
            aggregator.flush(now=T0 + timedelta(hours=6))
        totals.append(aggregator.current_total_kwh("dev"))

    assert totals == sorted(totals), f"total must never decrease, got {totals}"
    assert totals[-1] == pytest.approx(270 * 1000 / 3_600_000)


def test_late_sample_into_a_past_bucket_keeps_its_energy(db: Database) -> None:
    """A sample arriving after the bucket's end must not erase its contents."""
    aggregator = EnergyAggregator(db, bucket_sizes=(300,), stale_after_s=600)
    _feed(aggregator, "dev", _constant("dev", 1000.0, 120))
    aggregator.flush(now=T0 + timedelta(hours=1))
    before = aggregator.current_total_kwh("dev")

    # Same bucket, later timestamp: an upsert, never a reset.
    aggregator.add_result(_result("dev", 1000.0, T0 + timedelta(seconds=240)), T0 + timedelta(seconds=240))

    assert aggregator.current_total_kwh("dev") > before
    assert aggregator.current_total_kwh("dev") == pytest.approx(240 * 1000 / 3_600_000)


def test_current_total_survives_a_restart(db: Database) -> None:
    """A restart must not reset (or double) the cumulative total.

    Storage keeps the buckets; a resumed open bucket is rebuilt from
    ``inference_results`` by ``recompute_incomplete_buckets`` (which the backend
    calls on construction), so the running total continues where it stopped.
    """
    first = EnergyAggregator(db, bucket_sizes=(300,), stale_after_s=120)
    points = _constant("dev", 1000.0, 270)
    _feed(first, "dev", points)
    first.flush(now=T0 + timedelta(seconds=280))
    expected = first.current_total_kwh("dev")
    assert expected == pytest.approx(270 * 1000 / 3_600_000)

    # Stored history is what makes crash recovery possible.
    db.insert_inference_results([_result("dev", power, at) for at, power in points])

    restarted = EnergyAggregator(db, bucket_sizes=(300,), stale_after_s=120)
    restarted.recompute_incomplete_buckets(now=T0 + timedelta(seconds=290), lookback_hours=24)
    assert restarted.current_total_kwh("dev") == pytest.approx(expected)

    # And continuing to feed does not restart the counter at zero.
    restarted.add_result(
        _result("dev", 1000.0, T0 + timedelta(seconds=285)), T0 + timedelta(seconds=285)
    )
    restarted.add_result(
        _result("dev", 1000.0, T0 + timedelta(seconds=295)), T0 + timedelta(seconds=295)
    )
    assert restarted.current_total_kwh("dev") > expected


# --------------------------------------------------------------------------- #
# Open vs. completed buckets
# --------------------------------------------------------------------------- #


def test_open_bucket_is_persisted_as_incomplete_and_overwritten(db: Database) -> None:
    aggregator = EnergyAggregator(db, bucket_sizes=(300,), stale_after_s=120)
    _feed(aggregator, "dev", _constant("dev", 1000.0, 180))

    first = aggregator.flush(now=T0 + timedelta(seconds=200))
    assert [(b.complete, round(b.energy_kwh, 6)) for b in first] == [(False, 0.05)]
    assert aggregator.energy_total("dev") == pytest.approx(0.05)

    # More samples land in the same open bucket; the next flush *replaces* the
    # stored row (upsert), it does not add a second row.
    _feed(aggregator, "dev", _constant("dev", 1000.0, 600)[13:])
    second = aggregator.flush(now=T0 + timedelta(seconds=700))

    assert second[-1].bucket_start == T0 + timedelta(seconds=600)
    assert second[-1].complete is False
    assert aggregator.energy_total("dev") == pytest.approx(600 * 1000 / 3_600_000)
    assert len([row for row in _persisted_rows(db, "dev") if row["bucket_start"] == "2026-01-01T12:00:00+00:00"]) == 1


def test_completed_buckets_are_finalised_during_ingestion(db: Database) -> None:
    aggregator = EnergyAggregator(db, bucket_sizes=(300,), stale_after_s=120)
    # Nothing is persisted until a sample crosses into the next bucket.
    _feed(aggregator, "dev", _constant("dev", 1000.0, 180))
    assert _persisted_rows(db, "dev") == []

    aggregator.add_result(_result("dev", 1000.0, T0 + timedelta(seconds=400)), T0 + timedelta(seconds=400))

    persisted = _persisted_rows(db, "dev")
    assert len(persisted) == 1
    assert persisted[0]["bucket_start"] == "2026-01-01T12:00:00+00:00"
    assert persisted[0]["energy_kwh"] == pytest.approx(180 * 1000 / 3_600_000)


# --------------------------------------------------------------------------- #
# Restart recovery
# --------------------------------------------------------------------------- #


def test_restart_recovery_repairs_a_corrupt_bucket_without_double_counting(
    db: Database,
) -> None:
    first = EnergyAggregator(db, bucket_sizes=(300,), stale_after_s=120)
    points = _constant("dev", 1200.0, 600)
    _feed(first, "dev", points)
    first.flush(now=T0 + timedelta(seconds=700))

    # The engine must persist the raw inferences it integrated (it does this in
    # production); recovery replays exactly this history.
    db.insert_inference_results([_result("dev", 1200.0, at) for at, _ in points])

    expected = aggregator_total = first.energy_total("dev")
    assert aggregator_total == pytest.approx(600 * 1200 / 3_600_000)

    # Simulate a torn write: the second bucket is zeroed on disk.
    with db._lock:  # noqa: SLF001
        db._conn.execute(
            "UPDATE energy_aggregates SET energy_kwh=0 WHERE device_id='dev' AND bucket_start=?",
            ((T0 + timedelta(seconds=300)).isoformat(),),
        )
    assert first.energy_total("dev") < expected

    restarted = EnergyAggregator(db, bucket_sizes=(300,), stale_after_s=120)
    rebuilt = restarted.recompute_incomplete_buckets(
        now=T0 + timedelta(seconds=2000), lookback_hours=24
    )

    assert rebuilt
    assert restarted.energy_total("dev") == pytest.approx(expected)


def test_restart_recovery_is_idempotent(db: Database) -> None:
    first = EnergyAggregator(db, bucket_sizes=(300,), stale_after_s=120)
    points = _constant("dev", 900.0, 900)
    _feed(first, "dev", points)
    first.flush(now=T0 + timedelta(seconds=1000))
    db.insert_inference_results([_result("dev", 900.0, at) for at, _ in points])
    expected = first.energy_total("dev")

    for _ in range(3):
        restarted = EnergyAggregator(db, bucket_sizes=(300,), stale_after_s=120)
        restarted.recompute_incomplete_buckets(now=T0 + timedelta(seconds=2000), lookback_hours=24)
        assert restarted.energy_total("dev") == pytest.approx(expected)


def test_restart_recovery_without_history_is_a_no_op(db: Database) -> None:
    aggregator = EnergyAggregator(db, bucket_sizes=(300,), stale_after_s=120)

    assert aggregator.recompute_incomplete_buckets(now=T0, lookback_hours=24) == []


# --------------------------------------------------------------------------- #
# Device runs
# --------------------------------------------------------------------------- #


def _run_points(on_seconds: int = 120, off_seconds: int = 240, step: int = 15):
    points = [(T0 + timedelta(seconds=i), 1000.0) for i in range(0, on_seconds + 1, step)]
    points += [
        (T0 + timedelta(seconds=on_seconds + i), 0.0) for i in range(step, off_seconds + 1, step)
    ]
    return points


def test_device_run_opens_and_closes_with_correct_energy(db: Database) -> None:
    aggregator = EnergyAggregator(db, bucket_sizes=(300,), stale_after_s=120, run_end_grace_s=90)
    _feed(aggregator, "washer", _run_points(on_seconds=120, off_seconds=240))

    runs = aggregator.take_closed_runs()

    assert len(runs) == 1
    run = runs[0]
    assert run.device_id == "washer"
    assert run.started_at == T0
    # The run ends at the last active sample, not when the grace expired.
    assert run.ended_at == T0 + timedelta(seconds=120)
    assert run.runtime_s == pytest.approx(120.0)
    assert run.peak_power_w == pytest.approx(1000.0)
    # Energy is attributed to the reported runtime only: 120 s at 1000 W.  The
    # trailing ON->OFF ramp happens *after* ``ended_at``, so counting it here
    # would make the mean power exceed the observed peak.
    assert run.energy_kwh == pytest.approx(1000.0 * 120 / 3_600_000)
    assert run.mean_power_w == pytest.approx(1000.0)
    assert run.mean_power_w <= run.peak_power_w
    assert aggregator.take_closed_runs() == []


def test_run_mean_power_never_exceeds_observed_peak(db: Database) -> None:
    aggregator = EnergyAggregator(db, bucket_sizes=(300,), stale_after_s=120, run_end_grace_s=45)
    _feed(
        aggregator,
        "oven",
        [
            (T0, 200.0),
            (T0 + timedelta(seconds=15), 1600.0),
            (T0 + timedelta(seconds=30), 1600.0),
            (T0 + timedelta(seconds=45), 0.0),
            (T0 + timedelta(seconds=60), 0.0),
            (T0 + timedelta(seconds=75), 0.0),
            (T0 + timedelta(seconds=90), 0.0),
        ],
    )

    run = aggregator.take_closed_runs()[0]

    # 200 W -> 1600 W over 15 s (avg 900 W), then 1600 W for 15 s.  The
    # 1600 W -> 0 W interval starts after the last active sample (t=30 s), so
    # it is outside the reported runtime and contributes nothing.
    assert run.energy_kwh == pytest.approx((900 + 1600) * 15 / 3_600_000)
    assert run.runtime_s == pytest.approx(30.0)
    assert run.mean_power_w == pytest.approx(1250.0)
    assert run.mean_power_w <= run.peak_power_w


def test_run_does_not_open_below_the_power_floor(db: Database) -> None:
    aggregator = EnergyAggregator(db, bucket_sizes=(300,), min_run_power_w=5.0)

    aggregator.add_result(_result("tv", 3.0, T0), T0)
    assert aggregator._devices["tv"].run is None  # noqa: SLF001

    aggregator.add_result(_result("tv", 6.0, T0 + timedelta(seconds=15)), T0 + timedelta(seconds=15))
    assert aggregator._devices["tv"].run is not None  # noqa: SLF001
    assert aggregator._devices["tv"].run.started_at == T0 + timedelta(seconds=15)  # noqa: SLF001


def test_close_open_runs_respects_idle_grace(db: Database) -> None:
    aggregator = EnergyAggregator(db, bucket_sizes=(300,), run_end_grace_s=90)
    aggregator.add_result(_result("pump", 800.0, T0), T0)

    assert aggregator.close_open_runs(now=T0 + timedelta(seconds=30)) == []
    closed = aggregator.close_open_runs(now=T0 + timedelta(seconds=91))

    assert len(closed) == 1
    assert closed[0].ended_at == T0
    # A single-sample run has no elapsed time: the reported runtime is zero, and
    # the mean-power calculation guards against division by zero internally.
    assert closed[0].runtime_s == pytest.approx(0.0)
    assert closed[0].mean_power_w == pytest.approx(0.0)


def test_measurement_gap_closes_a_run_and_starts_a_new_one(db: Database) -> None:
    aggregator = EnergyAggregator(db, bucket_sizes=(300,), stale_after_s=60)
    _feed(
        aggregator,
        "dryer",
        [
            (T0, 900.0),
            (T0 + timedelta(seconds=15), 900.0),
            (T0 + timedelta(seconds=30), 900.0),
            # 670 s of blindness, then the device is still running.
            (T0 + timedelta(seconds=700), 900.0),
        ],
    )

    runs = aggregator.take_closed_runs()

    assert len(runs) == 1
    assert runs[0].ended_at == T0 + timedelta(seconds=30)
    # A gap-terminated run carries an explicit UNKNOWN run_end marker.
    marker = runs[0].stages[-1]
    assert marker.name == "run_end"
    assert marker.status == StageStatus.UNKNOWN
    assert "ended_on_measurement_gap" in marker.notes
    assert aggregator._devices["dryer"].run is not None  # noqa: SLF001 - a new run is open
    assert aggregator._devices["dryer"].run.started_at == T0 + timedelta(seconds=700)  # noqa: SLF001


def test_run_is_persisted_with_stage_rows(db: Database) -> None:
    aggregator = EnergyAggregator(db, bucket_sizes=(300,), run_end_grace_s=60)
    points = [(T0 + timedelta(seconds=15 * i), 200.0 if i < 8 else 1600.0) for i in range(16)]
    points += [(T0 + timedelta(seconds=15 * i), 0.0) for i in range(16, 30)]
    _feed(aggregator, "oven", points)

    runs = aggregator.take_closed_runs()
    assert len(runs) == 1

    stored = aggregator.runs("oven")
    assert len(stored) == 1
    assert stored[0]["run_id"] == runs[0].run_id
    assert stored[0]["device_id"] == "oven"
    assert stored[0]["energy_kwh"] == pytest.approx(runs[0].energy_kwh)
    assert stored[0]["stages"]

    stage_rows = aggregator.repository.stages("oven")
    assert len(stage_rows) == len(stored[0]["stages"])
    assert aggregator.repository.run_count("oven") == 1

    round_tripped = run_from_row(stored[0])
    assert round_tripped is not None
    assert round_tripped.run_id == runs[0].run_id
    assert round_tripped.energy_kwh == pytest.approx(runs[0].energy_kwh)
    assert [stage.power_mean_w for stage in round_tripped.stages] == pytest.approx(
        [stage.power_mean_w for stage in runs[0].stages]
    )


def test_run_stages_are_unknown_when_the_device_never_changes_level(db: Database) -> None:
    aggregator = EnergyAggregator(db, bucket_sizes=(300,), run_end_grace_s=60)
    points = [(T0 + timedelta(seconds=15 * i), 0.0) for i in range(20)]
    _feed(aggregator, "flat", points)

    assert aggregator.take_closed_runs() == []
    assert aggregator.repository.run_count("flat") == 0


def test_repository_save_run_upserts_and_replaces_stages(db: Database) -> None:
    repository = DeviceRunRepository(db)
    run = run_from_row(
        {
            "run_id": "run-1",
            "device_id": "dev",
            "started_at": T0.isoformat(),
            "ended_at": (T0 + timedelta(minutes=5)).isoformat(),
            "energy_kwh": 0.25,
            "peak_power_w": 1000.0,
            "mean_power_w": 500.0,
            "source": Source.INFERRED.value,
            "stages": [
                OperatingStage(
                    name="high",
                    status=StageStatus.IDENTIFIED,
                    power_mean_w=1000.0,
                    max_duration_s=300_000.0,
                ).model_dump(mode="json")
            ],
        }
    )
    assert run is not None
    repository.save_run(run)

    updated = run_from_row(
        {
            **run.as_row(),
            "energy_kwh": 0.5,
            "stages": [
                OperatingStage(name="low", status=StageStatus.PROBABLE, power_mean_w=400.0).model_dump(
                    mode="json"
                )
            ],
        }
    )
    assert updated is not None
    repository.save_run(updated)

    rows = repository.runs("dev")
    assert len(rows) == 1
    assert rows[0]["energy_kwh"] == pytest.approx(0.5)
    assert [stage["name"] for stage in rows[0]["stages"]] == ["low"]
    assert [row["stage_name"] for row in repository.stages("dev")] == ["low"]

    repository.delete_run("run-1")
    assert repository.run_count("dev") == 0
    assert repository.stages("dev") == []


# --------------------------------------------------------------------------- #
# Value objects
# --------------------------------------------------------------------------- #


def test_energy_bucket_and_device_run_row_shapes() -> None:
    bucket = EnergyBucket(
        device_id="dev",
        bucket_start=T0,
        bucket_s=300,
        energy_kwh=0.1,
        mean_power_w=1200.0,
        min_power_w=1000.0,
        max_power_w=1400.0,
        sample_count=20,
        complete=True,
        source=Source.INFERRED.value,
    )
    data = bucket.as_dict()
    assert data["bucket_start"] == T0.isoformat()
    assert data["complete"] is True
    assert data["energy_kwh"] == pytest.approx(0.1)

    run = run_from_row(
        {
            "run_id": "r",
            "device_id": "dev",
            "started_at": T0.isoformat(),
            "ended_at": (T0 + timedelta(seconds=90)).isoformat(),
            "energy_kwh": 0.02,
            "peak_power_w": 800.0,
            "mean_power_w": 800.0,
            "source": "inferred",
            "stages": [],
        }
    )
    assert run is not None
    assert run.runtime_s == pytest.approx(90.0)
    assert run.as_row()["source"] == "inferred"

    assert run_from_row({}) is None
