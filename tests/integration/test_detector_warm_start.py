"""A restart must not leave P3 blind while it re-learns noise.

Live, HA reports these CTs about every 18 s, so the detector's 64-sample σ
warm-up kept step detection off for ~19 minutes after every restart.  The
runtime now seeds P2 from recently stored readings and primes P3.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from wattmosaic_engine.config.settings import EngineConfig
from wattmosaic_engine.contracts import ReaderMeasurement
from wattmosaic_engine.main import EngineCore
from wattmosaic_engine.storage.database import Database

T0 = datetime(2026, 11, 20, 6, 0, 0, tzinfo=UTC)
READER = "kitchen_ckt"


def _reading(at: datetime, watts: float, seq: int) -> ReaderMeasurement:
    return ReaderMeasurement(
        reader_id=READER,
        observed_at=at,
        received_at=at,
        active_power_w=watts,
        sequence=seq,
        phase="unknown",
        measurement_type="instantaneous",
        calibration_version="discovery",
        measurement_basis="ct_measured",
    )


def _core(tmp_path: Path, now: list[datetime]) -> EngineCore:
    db = Database(tmp_path / "warm.db")
    core = EngineCore(EngineConfig(database_path=str(tmp_path / "warm.db"), ha_token="x"),
                      db, clock=lambda: now[0])
    db.repos.entities.upsert_endpoint(
        {"endpoint_id": READER, "kind": "ct", "entity_id": "sensor.kitchen_power",
         "scope": "circuit", "scope_status": "candidate", "enabled": True,
         "created_at": T0 - timedelta(days=1)}
    )
    return core


def _quiet_history(start: datetime, n: int) -> list[ReaderMeasurement]:
    # 18 s cadence with sub-watt noise around 300 W.
    return [_reading(start + timedelta(seconds=18 * i), 300.0 + (0.4 if i % 2 else -0.4), i)
            for i in range(n)]


async def _feed(core: EngineCore, readings: list[ReaderMeasurement], events: list[Any]) -> None:
    core.change_detector.emit = events.append
    for reading in readings:
        await core.on_reading(reading)


async def test_restart_detects_a_step_without_relearning_noise(tmp_path: Path) -> None:
    history = _quiet_history(T0, 80)
    restart_at = history[-1].observed_at + timedelta(seconds=30)
    now = [restart_at]
    core = _core(tmp_path, now)
    core.db.repos.samples.insert_readings(history)

    core._wire_v3_services()  # noqa: SLF001 - the production start path
    core._warm_start_readers()  # noqa: SLF001

    events: list[Any] = []
    live = [_reading(restart_at + timedelta(seconds=18 * i), 300.0, 100 + i) for i in range(2)]
    live += [_reading(restart_at + timedelta(seconds=36 + 18 * i), 1150.0, 200 + i) for i in range(5)]
    await _feed(core, live, events)
    assert any(abs(e.delta_w - 850.0) < 50.0 for e in events), [e.delta_w for e in events]


async def test_old_history_is_not_used_after_a_long_outage(tmp_path: Path) -> None:
    history = _quiet_history(T0, 80)
    now = [history[-1].observed_at + timedelta(hours=3)]
    core = _core(tmp_path, now)
    core.db.repos.samples.insert_readings(history)
    core._wire_v3_services()  # noqa: SLF001
    core._warm_start_readers()  # noqa: SLF001
    state = core.change_detector._states.get(READER)  # noqa: SLF001
    assert state is None or state.observation_count == 0
