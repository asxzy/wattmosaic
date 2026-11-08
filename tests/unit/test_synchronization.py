"""Unit tests for the sample aligner: windows, staleness, repeats, health."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest
from wattmosaic_engine.contracts import Measurement
from wattmosaic_engine.synchronization import SampleAligner, median, p95, robust_noise_mad
from wattmosaic_engine.synchronization.aligner import percentile

T0 = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


def _m(
    ct_id: str,
    value: float,
    *,
    seconds: float = 0.0,
    offset: float = 0.0,
    repeated: bool = False,
    unit: str = "W",
    quality: str = "good",
    sequence: int = 0,
    received_at: datetime | None = None,
) -> Measurement:
    recv = received_at or (T0 + timedelta(seconds=seconds))
    return Measurement(
        entity_id=f"sensor.{ct_id}",
        ct_id=ct_id,
        value=value,
        unit=unit,  # type: ignore[arg-type]
        value_w=value if unit in ("W", "kW") else None,
        state_timestamp=recv - timedelta(seconds=offset),
        received_at=recv,
        repeated=repeated,
        quality=quality,  # type: ignore[arg-type]
        sequence=sequence,
    )


# --------------------------------------------------------------------------- #
# Statistics helpers
# --------------------------------------------------------------------------- #


def test_median_and_percentile() -> None:
    assert median([]) is None
    assert median([3.0, 1.0, 2.0]) == 2.0
    assert median([4.0, 1.0, 3.0, 2.0]) == 2.5
    assert percentile([1, 2, 3, 4, 5], 50) == 3
    assert percentile([1, 2, 3, 4, 5], 0) == 1
    assert percentile([1, 2, 3, 4, 5], 100) == 5
    assert p95([]) is None
    # Linear interpolation, numpy-style: rank = (100-1)*0.95 = 94.05
    assert p95(list(range(1, 101))) == pytest.approx(95.05)


def test_robust_noise_mad() -> None:
    assert robust_noise_mad([]) is None
    assert robust_noise_mad([5.0]) is None
    assert robust_noise_mad([10.0, 10.0, 10.0]) == 0.0
    # MAD of [1,2,3,4,5] is 1; scaled by 1.4826
    assert robust_noise_mad([1, 2, 3, 4, 5]) == pytest.approx(1.4826)


# --------------------------------------------------------------------------- #
# Windowing
# --------------------------------------------------------------------------- #


def test_window_returns_latest_value_per_channel() -> None:
    aligner = SampleAligner(max_age_s=300, stale_after_s=60)
    aligner.add(_m("l1", 100.0, seconds=0))
    aligner.add(_m("l1", 110.0, seconds=10))
    aligner.add(_m("l2", 200.0, seconds=5))

    window = aligner.window(at=T0 + timedelta(seconds=20), channels=["l1", "l2"], max_age_s=60)
    assert window["l1"].value_w == 110.0
    assert window["l1"].age_seconds == pytest.approx(10.0)
    assert window["l2"].value_w == 200.0
    assert window["l1"].quality == "good"
    assert window.complete


def test_window_uses_only_samples_at_or_before_time() -> None:
    aligner = SampleAligner(max_age_s=300)
    aligner.add(_m("l1", 100.0, seconds=10))
    window = aligner.window(at=T0, channels=["l1"])
    assert window["l1"].value_w is None
    assert window["l1"].quality == "missing"


def test_window_marks_stale_channels() -> None:
    aligner = SampleAligner(max_age_s=300, stale_after_s=60)
    aligner.add(_m("l1", 100.0, seconds=0))
    window = aligner.window(at=T0 + timedelta(seconds=100), channels=["l1"], max_age_s=50)
    assert window["l1"].value_w == 100.0
    assert window["l1"].quality == "stale"
    assert not window["l1"].fresh
    assert window.stale_channels == ["l1"]


def test_window_age_uses_state_timestamp_not_arrival() -> None:
    """A replayed old state must not look fresh just because it just arrived.

    ADR-0003: staleness is measured from ``state_timestamp``.  ``_prime``
    replays every state on each reconnect with a current ``received_at`` while
    Home Assistant's ``last_updated`` may be hours old.  Measuring age from
    arrival would present that old value as a current measurement.
    """
    aligner = SampleAligner(max_age_s=10_000, stale_after_s=120)
    # Arrived now, but HA recorded the value two hours ago.
    aligner.add(_m("replayed", 0.0, seconds=0, offset=7200.0))

    window = aligner.window(at=T0, channels=["replayed"], max_age_s=120)
    channel = window["replayed"]

    assert channel.value_w == 0.0
    assert channel.age_seconds == pytest.approx(7200.0)
    assert channel.quality == "stale"
    assert channel.fresh is False

    # The solver must not treat a two-hour-old reading as usable.
    from wattmosaic_engine.contracts import QualityStatus
    from wattmosaic_engine.electrical.window import ChannelReading

    reading = ChannelReading(
        channel_id="replayed",
        value_w=channel.value_w,
        age_s=channel.age_seconds,
        quality=QualityStatus(channel.quality),
    )
    assert reading.is_usable(120.0) is False


def test_channel_health_age_uses_state_timestamp_not_arrival() -> None:
    """``ChannelHealth.missing`` must follow the same rule as the window."""
    aligner = SampleAligner(max_age_s=10_000, stale_after_s=120)
    aligner.add(_m("replayed", 0.0, seconds=0, offset=7200.0))

    health = aligner.channel_health_one("replayed", now=T0, stale_after_s=120)
    assert health.last_update_age_s == pytest.approx(7200.0)
    assert health.missing is True
    assert health.stale is True


def test_window_marks_missing_channels() -> None:
    aligner = SampleAligner()
    aligner.add(_m("l1", 100.0))
    window = aligner.window(at=T0, channels=["l1", "l2"])
    assert window["l2"].value_w is None
    assert window["l2"].quality == "missing"
    assert window.missing_channels == ["l2"]
    assert not window.complete


def test_repeated_values_are_exposed_but_not_fresh() -> None:
    aligner = SampleAligner(max_age_s=300)
    aligner.add(_m("l1", 100.0, seconds=0, sequence=1))
    aligner.add(_m("l1", 100.0, seconds=15, repeated=True, sequence=2))

    window = aligner.window(at=T0 + timedelta(seconds=20), channels=["l1"])
    ch = window["l1"]
    assert ch.value_w == 100.0  # current value still exposed
    assert ch.repeated is True
    assert ch.fresh is False  # but not usable as a new measurement
    assert window.fresh_values_w == {}
    assert window.repeated_channels == ["l1"]


def test_fresh_values_include_only_new_measurements() -> None:
    aligner = SampleAligner(max_age_s=300)
    aligner.add(_m("l1", 100.0, seconds=0))
    aligner.add(_m("l2", 200.0, seconds=0, repeated=True))
    window = aligner.window(at=T0 + timedelta(seconds=2), channels=["l1", "l2"])
    assert window.fresh_values_w == {"l1": 100.0}
    assert window.values_w == {"l1": 100.0, "l2": 200.0}


def test_window_without_channel_list_uses_power_channels_only() -> None:
    aligner = SampleAligner()
    aligner.add(_m("l1", 100.0))
    aligner.add(_m("energy", 5.0, unit="kWh"))
    window = aligner.window(at=T0)
    assert list(window.channels) == ["l1"]


# --------------------------------------------------------------------------- #
# Bounded buffers
# --------------------------------------------------------------------------- #


def test_ring_buffer_is_bounded_by_count() -> None:
    aligner = SampleAligner(max_age_s=10_000, max_samples_per_channel=5)
    for i in range(50):
        aligner.add(_m("l1", float(i), seconds=i))
    assert aligner.stats()["buffer_sizes"]["l1"] == 5
    assert aligner.stats()["dropped"] > 0
    # Oldest retained sample is 45 (latest 5 of 0..49).
    window = aligner.window(at=T0 + timedelta(seconds=49), channels=["l1"])
    assert window["l1"].value_w == 49.0


def test_ring_buffer_is_bounded_by_age() -> None:
    aligner = SampleAligner(max_age_s=30)
    for i in range(10):
        aligner.add(_m("l1", float(i), seconds=i * 10))
    # Only samples within 30s of the newest (90s) survive: 70, 80, 90.
    assert aligner.stats()["buffer_sizes"]["l1"] <= 4


def test_prune_removes_old_channels() -> None:
    aligner = SampleAligner(max_age_s=10)
    aligner.add(_m("l1", 1.0, seconds=0))
    emptied = aligner.prune(now=T0 + timedelta(seconds=100))
    assert emptied == 1
    assert aligner.channels() == []


def test_out_of_order_sampling_stays_ordered() -> None:
    aligner = SampleAligner(max_age_s=300)
    aligner.add(_m("l1", 2.0, seconds=20, sequence=2))
    aligner.add(_m("l1", 1.0, seconds=10, sequence=1))
    aligner.add(_m("l1", 3.0, seconds=30, sequence=3))
    window = aligner.window(at=T0 + timedelta(seconds=25), channels=["l1"])
    assert window["l1"].value_w == 2.0


# --------------------------------------------------------------------------- #
# Energy domain separation
# --------------------------------------------------------------------------- #


def test_energy_never_enters_power_window() -> None:
    aligner = SampleAligner()
    aligner.add(_m("l1", 100.0, unit="W"))
    aligner.add(_m("meter", 12.5, unit="kWh"))
    aligner.add(_m("meter_wh", 500.0, unit="Wh"))

    power = aligner.window(at=T0, channels=["l1", "meter", "meter_wh"])
    assert power["l1"].value_w == 100.0
    assert power["meter"].value_w is None
    assert power["meter_wh"].value_w is None
    assert power["meter"].quality == "missing"

    energy = aligner.energy_window(at=T0)
    assert energy["meter"].value_kwh == pytest.approx(12.5)
    assert energy["meter_wh"].value_kwh == pytest.approx(0.5)
    assert energy["meter"].unit == "kWh"
    assert "meter" not in aligner.channels()
    assert set(aligner.energy_channels()) == {"meter", "meter_wh"}


def test_energy_window_reports_kwh_only() -> None:
    aligner = SampleAligner()
    aligner.add(_m("meter", 2500.0, unit="Wh"))
    energy = aligner.energy_window(at=T0, channels=["meter"])
    data = energy["meter"].to_dict()
    assert data["value_kwh"] == pytest.approx(2.5)
    assert data["value_w"] is None
    assert data["unit"] == "kWh"


def test_energy_health_detects_counter_reset() -> None:
    aligner = SampleAligner(max_age_s=10_000)
    aligner.add(_m("meter", 10.0, unit="kWh", seconds=0))
    aligner.add(_m("meter", 10.5, unit="kWh", seconds=10))
    aligner.add(_m("meter", 0.2, unit="kWh", seconds=20))  # meter reset
    aligner.add(_m("meter", 0.4, unit="kWh", seconds=30))
    health = aligner.energy_health(now=T0 + timedelta(seconds=31))["meter"]
    assert health.reset_count == 1
    assert health.monotonic is False
    assert health.max_drop_kwh == pytest.approx(10.3)
    assert health.total_increase_kwh == pytest.approx(0.7)
    assert health.quality in ("degraded", "stale", "bad")


def test_energy_health_monotonic_counter() -> None:
    aligner = SampleAligner(max_age_s=10_000)
    for i, v in enumerate([1.0, 2.0, 3.5]):
        aligner.add(_m("meter", v, unit="kWh", seconds=i * 10))
    health = aligner.energy_health(now=T0 + timedelta(seconds=21))["meter"]
    assert health.monotonic is True
    assert health.reset_count == 0
    assert health.total_increase_kwh == pytest.approx(2.5)


def test_non_power_non_energy_units_are_rejected() -> None:
    aligner = SampleAligner()
    # The Measurement contract only allows W/kW/kWh/Wh.  Tamper past validation
    # to exercise the aligner's defensive branch.
    m = _m("volts", 120.0).model_copy(update={"unit": "V"})
    aligner.add(m)
    assert aligner.stats()["ignored_non_power"] == 1
    assert aligner.channels() == []


# --------------------------------------------------------------------------- #
# Channel health
# --------------------------------------------------------------------------- #


def test_channel_health_intervals_and_rate() -> None:
    aligner = SampleAligner(max_age_s=10_000, stale_after_s=60)
    for i in range(6):
        aligner.add(_m("l1", float(i), seconds=i * 10))
    health = aligner.channel_health(now=T0 + timedelta(seconds=50))["l1"]
    assert health.sample_count == 6
    assert health.interval_median_s == pytest.approx(10.0)
    assert health.interval_max_s == pytest.approx(10.0)
    assert health.effective_rate_hz == pytest.approx(0.1)
    assert health.last_update_age_s == pytest.approx(0.0)
    assert health.missing is False
    assert health.quality == "good"


def test_channel_health_repeat_ratio_and_missing() -> None:
    aligner = SampleAligner(max_age_s=10_000, stale_after_s=20)
    aligner.add(_m("l1", 5.0, seconds=0, sequence=1))
    aligner.add(_m("l1", 5.0, seconds=10, repeated=True, sequence=2))
    health = aligner.channel_health(now=T0 + timedelta(seconds=100))["l1"]
    assert health.repeat_count == 1
    assert health.repeat_ratio == pytest.approx(0.5)
    assert health.missing is True
    assert health.stale is True


def test_channel_health_for_unknown_channel() -> None:
    aligner = SampleAligner()
    health = aligner.channel_health_one("nope")
    assert health.sample_count == 0
    assert health.missing is True
    assert health.quality == "missing"


def test_channel_health_noise_and_resolution() -> None:
    aligner = SampleAligner(max_age_s=10_000)
    for i, v in enumerate([100.0, 101.0, 100.0, 101.0, 100.0]):
        aligner.add(_m("l1", v, seconds=i * 10))
    health = aligner.channel_health(now=T0 + timedelta(seconds=40))["l1"]
    assert health.resolution_w == pytest.approx(1.0)
    assert health.noise_mad_w is not None and health.noise_mad_w > 0


def test_window_repeated_is_not_counted_as_fresh_measurement() -> None:
    """Learning must see zero fresh samples when every value is a repeat."""
    aligner = SampleAligner(max_age_s=300)
    for i in range(5):
        aligner.add(_m("l1", 50.0, seconds=i * 15, repeated=i > 0, sequence=i))
    window = aligner.window(at=T0 + timedelta(seconds=60), channels=["l1"])
    assert window["l1"].value_w == 50.0
    assert window.fresh_channels == []


def test_latest_and_clear() -> None:
    aligner = SampleAligner()
    aligner.add(_m("l1", 42.0))
    latest = aligner.latest("l1")
    assert latest is not None and latest.value_w == 42.0
    assert aligner.latest("missing") is None
    aligner.clear()
    assert aligner.channels() == []
    assert aligner.energy_channels() == []


def test_rejects_non_finite_measurement() -> None:
    aligner = SampleAligner()
    # Pydantic forbids non-finite values, so construct then tamper via model_copy.
    m = _m("l1", 1.0).model_copy(update={"value": math.inf, "value_w": math.inf})
    aligner.add(m)
    assert aligner.stats()["rejected"] == 1
    assert aligner.channels() == []
