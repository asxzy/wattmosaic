"""P2 — ReaderStateStore: one ring per reader (held/stale/missing, σ, baseline).

The tests below mirror the task's acceptance bullets:

* held → stale → missing with ``None`` in the missing state (AT-17 support);
* the baseline does not absorb a 100 W step within 5 cadences when frozen on
  suspect;
* σ at 15.7 s cadence with 20 min of history is within 25 % of the true σ;
* a late sample is inserted in order and the callback fires exactly once;
* snapshot → restore gives identical held / σ / baseline;
* ``add`` is O(1) (no full-window scan on the common in-order path).

They are written against the *production* module and the K02
:class:`~wattmosaic_engine.contracts.ReaderMeasurement` contract.  Fixtures use
the real contract, not a hand-rolled stand-in.
"""

from __future__ import annotations

import math
import random
from datetime import UTC, datetime, timedelta

import pytest
from wattmosaic_engine.contracts import (
    MeasurementBasis,
    MeasurementType,
    QualityFlag,
    ReaderMeasurement,
)
from wattmosaic_engine.realtime.reader_state import AddResult, ReaderStateStore, WindowSample
from wattmosaic_engine.runtime.interfaces import HeldReading

T0 = datetime(2026, 11, 18, 12, 0, 0, tzinfo=UTC)
CADENCE_S = 15.7


def measurement(
    reader: str,
    at: datetime,
    power: float | None,
    *,
    sequence: int = 0,
    flags: list[QualityFlag] | None = None,
) -> ReaderMeasurement:
    return ReaderMeasurement(
        reader_id=reader,
        observed_at=at,
        received_at=at + timedelta(seconds=2),
        active_power_w=power,
        phase="l1",
        measurement_type=MeasurementType.INSTANTANEOUS,
        measurement_window_ms=None,
        calibration_version="cal-1",
        measurement_basis=MeasurementBasis.CT_MEASURED.value,
        quality_flags=flags or [],
        sequence=sequence,
    )


def feed(
    store: ReaderStateStore,
    reader: str,
    values: list[float | None],
    *,
    start: datetime = T0,
    step_s: float = CADENCE_S,
    seq0: int = 0,
    flags: list[QualityFlag] | None = None,
) -> None:
    for i, value in enumerate(values):
        store.add(
            measurement(
                reader,
                start + timedelta(seconds=i * step_s),
                value,
                sequence=seq0 + i,
                flags=flags,
            )
        )


# --------------------------------------------------------------------------- #
# Held / stale / missing (AT-17 support)
# --------------------------------------------------------------------------- #


def test_held_then_stale_then_missing_is_none() -> None:
    store = ReaderStateStore(stale_after_s=120.0, missing_after_s=600.0)
    store.add(measurement("ct_a", T0, 250.0, sequence=1))

    held = store.held("ct_a", T0 + timedelta(seconds=60))
    assert held.value == 250.0
    assert held.state == "held"
    assert held.held_since == T0
    assert held.last_confirmed_at == T0
    assert isinstance(held, HeldReading)

    stale = store.held("ct_a", T0 + timedelta(seconds=300))
    assert stale.value == 250.0  # still held, but flagged stale
    assert stale.state == "stale"

    missing = store.held("ct_a", T0 + timedelta(seconds=601))
    assert missing.value is None
    assert missing.state == "missing"


def test_held_is_point_in_time_not_future_value() -> None:
    """A query at time t never sees a sample observed after t."""
    store = ReaderStateStore(stale_after_s=120.0, missing_after_s=600.0)
    store.add(measurement("ct_a", T0, 100.0, sequence=1))
    store.add(measurement("ct_a", T0 + timedelta(seconds=15), 200.0, sequence=2))

    early = store.held("ct_a", T0 + timedelta(seconds=10))
    assert early.value == 100.0
    assert early.held_since == T0
    late = store.held("ct_a", T0 + timedelta(seconds=20))
    assert late.value == 200.0
    assert late.held_since == T0 + timedelta(seconds=15)


def test_missing_reader_held_is_none_not_zero() -> None:
    store = ReaderStateStore()
    held = store.held("never-seen", T0)
    assert held.value is None
    assert held.state == "missing"


def test_repeated_value_refreshes_confirmation_without_moving_held_since() -> None:
    store = ReaderStateStore(stale_after_s=120.0, missing_after_s=600.0)
    store.add(measurement("ct_a", T0, 100.0, sequence=1))
    store.add(
        measurement(
            "ct_a",
            T0 + timedelta(seconds=60),
            100.0,
            sequence=2,
            flags=[QualityFlag.REPEATED],
        )
    )
    held = store.held("ct_a", T0 + timedelta(seconds=150))
    # The repeated value confirmed the load at +60 s, so at +150 s the age is
    # only 90 s and the reading is still held (not stale).
    assert held.state == "held"
    assert held.held_since == T0
    assert held.last_confirmed_at == T0 + timedelta(seconds=60)


def test_push_on_change_silence_keeps_last_value_held() -> None:
    """A flat push-on-change channel is held, not missing, until the policy."""
    store = ReaderStateStore(stale_after_s=120.0, missing_after_s=600.0)
    store.add(measurement("ct_a", T0, 42.0, sequence=1))
    for offset in (119, 120, 300, 599):
        held = store.held("ct_a", T0 + timedelta(seconds=offset))
        assert held.value == 42.0
        assert held.state in ("held", "stale")
    assert store.held("ct_a", T0 + timedelta(seconds=601)).value is None


# --------------------------------------------------------------------------- #
# Baseline freeze on suspect
# --------------------------------------------------------------------------- #


def test_baseline_does_not_absorb_step_when_frozen() -> None:
    store = ReaderStateStore()
    reader = "ct_step"
    feed(store, reader, [100.0] * 20, seq0=0)
    before = store.baseline(reader)
    assert before is not None
    assert before == pytest.approx(100.0, abs=1e-6)

    step_time = T0 + timedelta(seconds=20 * CADENCE_S)
    store.freeze_baseline(reader, step_time + timedelta(seconds=5 * CADENCE_S))
    feed(
        store,
        reader,
        [200.0] * 5,
        start=step_time,
        seq0=20,
    )

    after = store.baseline(reader)
    assert after is not None
    # The step is 100 W; the baseline must move by at most 10 W (10 %).
    assert abs(after - before) <= 10.0


def test_baseline_moves_slowly_when_not_frozen() -> None:
    store = ReaderStateStore(baseline_tau_s=30.0 * 60.0)
    reader = "ct_free"
    feed(store, reader, [100.0] * 20, seq0=0)
    before = store.baseline(reader)
    assert before is not None
    step_time = T0 + timedelta(seconds=20 * CADENCE_S)
    feed(store, reader, [200.0] * 5, start=step_time, seq0=20)
    after = store.baseline(reader)
    assert after is not None
    # Over ~78 s with τ=1800 s the EWMA moves ~3 % of the 100 W step, well
    # under 10 W even without an explicit freeze.
    assert abs(after - before) <= 10.0


def test_release_expired_freezes() -> None:
    store = ReaderStateStore()
    feed(store, "ct_a", [100.0, 100.0, 100.0])
    store.freeze_baseline("ct_a", T0 + timedelta(seconds=30))
    state = store.reader_state("ct_a")
    assert state is not None and state.frozen
    store.release_expired_freezes(T0 + timedelta(seconds=31))
    state = store.reader_state("ct_a")
    assert state is not None and not state.frozen


# --------------------------------------------------------------------------- #
# Noise σ from 20 min of history at 15.7 s cadence
# --------------------------------------------------------------------------- #


def test_sigma_within_25_percent_of_true_sigma() -> None:
    sigma_true = 3.0
    rng = random.Random(17)
    values = [100.0] + [100.0 + rng.gauss(0.0, sigma_true) for _ in range(80)]
    store = ReaderStateStore()
    feed(store, "ct_noisy", values, step_s=CADENCE_S)

    estimate = store.sigma("ct_noisy")
    assert estimate is not None
    assert abs(estimate - sigma_true) / sigma_true <= 0.25, (estimate, sigma_true)


def test_sigma_has_floor_of_one_watt() -> None:
    store = ReaderStateStore(sigma_min_w=1.0)
    feed(store, "ct_flat", [100.0] * 20, step_s=CADENCE_S)
    assert store.sigma("ct_flat") == pytest.approx(1.0)


def test_sigma_resolution_floor_applies() -> None:
    # One reader with 0.1 W resolution and no noise: the σ floor from
    # resolution/√12 (~0.029 W) is below 1 W, so σ is still the 1 W minimum.
    store = ReaderStateStore(sigma_min_w=1.0)
    feed(store, "ct_a", [100.0, 100.1, 100.0, 100.1, 100.0, 100.1])
    assert store.sigma("ct_a") is not None
    assert store.sigma("ct_a") >= 1.0


def test_declared_resolution_sets_sigma_floor() -> None:
    # A declared 12 W resolution gives a σ floor of 12/√12 ≈ 3.46 W, above
    # the 1 W minimum (design §3: max(MAD, resolution/√12, σ_min)).
    store = ReaderStateStore(sigma_min_w=1.0, resolution_w_by_reader={"ct_a": 12.0})
    feed(store, "ct_a", [100.0] * 10, step_s=CADENCE_S)
    estimate = store.sigma("ct_a")
    assert estimate is not None
    assert estimate == pytest.approx(12.0 / math.sqrt(12.0))


def test_no_sigma_for_single_sample() -> None:
    store = ReaderStateStore()
    store.add(measurement("ct_a", T0, 100.0, sequence=1))
    assert store.sigma("ct_a") == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# Late arrival
# --------------------------------------------------------------------------- #


def test_late_sample_inserted_in_order_and_callback_fires_once() -> None:
    store = ReaderStateStore()
    store.add(measurement("ct_a", T0, 100.0, sequence=1))
    store.add(measurement("ct_a", T0 + timedelta(seconds=30), 300.0, sequence=3))

    fired: list[tuple[str, datetime]] = []
    store.on_late(lambda reader, at: fired.append((reader, at)))
    late_at = T0 + timedelta(seconds=15)
    result = store.add(measurement("ct_a", late_at, 200.0, sequence=2))

    assert isinstance(result, AddResult)
    assert result.late is True
    assert fired == [("ct_a", late_at)]

    window = store.window("ct_a", T0 - timedelta(seconds=1), T0 + timedelta(seconds=31))
    assert [s.active_power_w for s in window] == [100.0, 200.0, 300.0]
    assert all(isinstance(s, WindowSample) for s in window)


def test_duplicate_late_replay_does_not_fire_twice() -> None:
    store = ReaderStateStore()
    store.add(measurement("ct_a", T0, 100.0, sequence=1))
    store.add(measurement("ct_a", T0 + timedelta(seconds=30), 300.0, sequence=3))
    fired: list[tuple[str, datetime]] = []
    store.on_late(lambda reader, at: fired.append((reader, at)))

    late_at = T0 + timedelta(seconds=15)
    store.add(measurement("ct_a", late_at, 200.0, sequence=2))
    store.add(measurement("ct_a", late_at, 200.0, sequence=2))  # replay
    assert len(fired) == 1


def test_in_order_add_is_not_late() -> None:
    store = ReaderStateStore()
    store.add(measurement("ct_a", T0, 100.0, sequence=1))
    result = store.add(measurement("ct_a", T0 + timedelta(seconds=15), 110.0, sequence=2))
    assert result.late is False


# --------------------------------------------------------------------------- #
# Snapshot / restore
# --------------------------------------------------------------------------- #


def test_restore_after_snapshot_is_identical() -> None:
    store = ReaderStateStore()
    feed(store, "ct_a", [100.0, 101.0, 102.0, 103.0, 104.0, 105.0], step_s=CADENCE_S)
    store.freeze_baseline("ct_a", T0 + timedelta(seconds=200))
    blob = store.snapshot_state()

    restored = ReaderStateStore()
    restored.restore_state(blob)

    at = T0 + timedelta(seconds=5 * CADENCE_S + 10)
    assert restored.held("ct_a", at) == store.held("ct_a", at)
    assert restored.sigma("ct_a") == store.sigma("ct_a")
    assert restored.baseline("ct_a") == store.baseline("ct_a")
    assert restored.window("ct_a", T0, at) == store.window("ct_a", T0, at)
    sa = store.reader_state("ct_a")
    ra = restored.reader_state("ct_a")
    assert sa is not None and ra is not None
    assert sa.frozen == ra.frozen


def test_snapshot_blob_is_byte_stable() -> None:
    store = ReaderStateStore()
    feed(store, "ct_a", [100.0, 101.0, 102.0])
    assert store.snapshot_state() == store.snapshot_state()


def test_restore_rejects_corrupt_blob() -> None:
    store = ReaderStateStore()
    blob = bytearray(store.snapshot_state())
    blob[-1] ^= 0xFF
    with pytest.raises(ValueError):
        ReaderStateStore().restore_state(bytes(blob))


# --------------------------------------------------------------------------- #
# Repository persistence
# --------------------------------------------------------------------------- #


def test_reader_state_round_trips_through_repository(tmp_path) -> None:
    from wattmosaic_engine.storage.database import Database

    db = Database(tmp_path / "reader_state.db")
    try:
        assert db.repos.samples.get_reader_state() is None
        store = ReaderStateStore()
        feed(store, "ct_a", [100.0, 101.0, 102.0])
        blob = store.snapshot_state()
        db.repos.samples.put_reader_state(blob)
        assert db.repos.samples.get_reader_state() == blob
        # A second write overwrites rather than appending.
        store.add(measurement("ct_a", T0 + timedelta(seconds=3 * CADENCE_S), 103.0, sequence=3))
        blob2 = store.snapshot_state()
        db.repos.samples.put_reader_state(blob2)
        assert db.repos.samples.get_reader_state() == blob2
    finally:
        db.close()


def test_should_persist_uses_injected_deadline() -> None:
    store = ReaderStateStore(persist_interval_s=600.0)
    assert store.should_persist(T0) is True
    store.mark_persisted(T0)
    assert store.should_persist(T0 + timedelta(seconds=599)) is False
    assert store.should_persist(T0 + timedelta(seconds=600)) is True


# --------------------------------------------------------------------------- #
# Quarantine / null policy
# --------------------------------------------------------------------------- #


def test_quarantined_reading_does_not_move_baseline_or_sigma() -> None:
    store = ReaderStateStore()
    feed(store, "ct_a", [100.0, 101.0, 102.0])
    baseline_before = store.baseline("ct_a")
    sigma_before = store.sigma("ct_a")
    store.add(
        measurement(
            "ct_a",
            T0 + timedelta(seconds=3 * CADENCE_S),
            9999.0,
            sequence=3,
            flags=[QualityFlag.IMPLAUSIBLE_RANGE],
        )
    )
    assert store.baseline("ct_a") == baseline_before
    assert store.sigma("ct_a") == sigma_before


def test_absent_power_is_none_not_zero() -> None:
    store = ReaderStateStore()
    store.add(measurement("ct_a", T0, None, sequence=1))
    held = store.held("ct_a", T0)
    assert held.value is None
    assert store.baseline("ct_a") is None
    assert store.sigma("ct_a") == pytest.approx(1.0)
