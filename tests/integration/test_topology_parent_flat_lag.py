"""P6: 'parent flat' means flat (not net zero), and cross-device lag is allowed.

A concurrent opposite step inside the parent's window can net to zero while the
parent was not flat: that is neutral, never conflict.  A parent on another
device that reports the step one of its own sample intervals later must still be
measured as following the child.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from wattmosaic_engine.config.settings import EngineConfig
from wattmosaic_engine.contracts import (
    EventGroup,
    EventGroupStatus,
    EvidenceRef,
    ReaderDelta,
    ReaderMeasurement,
    VersionTriple,
)
from wattmosaic_engine.electrical.topology_inference import DeltaObservation
from wattmosaic_engine.main import EngineCore
from wattmosaic_engine.storage.database import Database

T0 = datetime(2026, 11, 20, 8, 0, 0, tzinfo=UTC)
FEED, BRANCH = "feed_leg", "branch_ckt"
CADENCE_S = 18.0
STEP_S = 1500.0
STEP_W = 500.0


def _reading(reader: str, at: datetime, watts: float, seq: int) -> ReaderMeasurement:
    return ReaderMeasurement(
        reader_id=reader,
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
    db = Database(tmp_path / "flat.db")
    core = EngineCore(
        EngineConfig(database_path=str(tmp_path / "flat.db"), ha_token="x"),
        db,
        clock=lambda: now[0],
    )
    for reader in (FEED, BRANCH):
        db.repos.entities.upsert_endpoint(
            {
                "endpoint_id": reader,
                "kind": "ct",
                "entity_id": f"sensor.{reader}_power",
                "scope": "circuit",
                "scope_status": "candidate",
                "enabled": True,
                "created_at": T0 - timedelta(days=1),
            }
        )
    core._wire_v3_services()  # noqa: SLF001
    return core


def _branch_w(t: float) -> float:
    return (40.0 if t < STEP_S else 40.0 + STEP_W) + (0.4 if int(t / CADENCE_S) % 2 else -0.4)


def _feed_w(
    t: float, *, lag_s: float, other_load_at: float | None, opposite_step_at: float | None
) -> float:
    # The feed sees the branch's step one of its own intervals late; an
    # unrelated load changes a little after the step (a busy main).  An
    # opposite step nets the parent's wide-window move back toward zero.
    effective = t - lag_s
    extra = 300.0 if other_load_at is not None and t >= other_load_at else 0.0
    if opposite_step_at is not None and t >= opposite_step_at:
        extra -= 500.0
    return 1000.0 + _branch_w(effective) + extra


async def _feed_history(
    core: EngineCore,
    now: list[datetime],
    *,
    feed_lag_s: float,
    other_load_at: float | None,
    until_s: float,
    opposite_step_at: float | None = None,
) -> None:
    readings: list[ReaderMeasurement] = []
    t, seq = 0.0, 0
    while t <= until_s:
        readings.append(_reading(BRANCH, T0 + timedelta(seconds=t), _branch_w(t), seq))
        t += CADENCE_S
        seq += 1
    t = 7.0
    while t <= until_s:
        readings.append(
            _reading(
                FEED,
                T0 + timedelta(seconds=t),
                _feed_w(
                    t,
                    lag_s=feed_lag_s,
                    other_load_at=other_load_at,
                    opposite_step_at=opposite_step_at,
                ),
                seq,
            )
        )
        t += CADENCE_S
        seq += 1
    for reading in sorted(readings, key=lambda r: r.observed_at):
        now[0] = reading.observed_at
        await core.on_reading(reading)


def _group(core: EngineCore, *, onset_s: float) -> EventGroup:
    steps = core._t5_steps[BRANCH]  # noqa: SLF001
    evidence_id, _lo, _hi, delta = steps[-1]
    assert abs(delta - STEP_W) < 50.0
    return EventGroup(
        group_id="grp-flat",
        revision=1,
        status=EventGroupStatus.CONFIRMED,
        opened_at=T0 + timedelta(seconds=onset_s),
        closed_at=T0 + timedelta(seconds=onset_s + 30.0),
        reader_deltas=[
            ReaderDelta(
                reader_id=BRANCH,
                delta_w=delta,
                sigma_w=1.0,
                completeness="complete",
                evidence_ref=EvidenceRef(kind="event_evidence", id=evidence_id or "evd-x"),
            )
        ],
        versions=VersionTriple(
            topology_version_id="topo-0", mapping_version_id="map-0", model_set_id="mset-0"
        ),
    )


def _seed(core: EngineCore) -> None:
    core.topology_inference = core.topology_inference.observe_group(
        DeltaObservation(
            group_id="seed",
            deltas={FEED: (STEP_W, 1.0), BRANCH: (STEP_W, 1.0)},
            at=T0,
            cause_id="seed",
        )
    )


async def test_a_late_parent_step_still_gives_support(tmp_path: Path) -> None:
    now = [T0]
    core = _core(tmp_path, now)
    _seed(core)
    # The feed reports the branch step one of its own intervals (18 s) late.
    await _feed_history(
        core, now, feed_lag_s=CADENCE_S, other_load_at=STEP_S + 100.0, until_s=STEP_S + 400.0
    )
    assert core._t5_steps.get(BRANCH)  # noqa: SLF001
    core._fold_group_ledger(_group(core, onset_s=STEP_S + 90.0))  # noqa: SLF001
    edge = core.topology_inference.edge(FEED, BRANCH)
    assert edge is not None
    assert (edge.evidence.support, edge.evidence.conflict) == (2, 0)


async def test_a_net_zero_parent_window_with_opposite_step_is_neutral(tmp_path: Path) -> None:
    now = [T0]
    core = _core(tmp_path, now)
    _seed(core)
    # An opposite -500 step inside the parent's pre window makes that window
    # non-flat even though the wide-window move nets to ~0.  The old code read
    # the net-zero as conflict; P6 requires neutral (neither support nor
    # conflict) when the parent's own window proves it was not flat.
    await _feed_history(
        core,
        now,
        feed_lag_s=0.0,
        other_load_at=None,
        until_s=STEP_S + 400.0,
        opposite_step_at=STEP_S - 70.0,
    )
    assert core._t5_steps.get(BRANCH)  # noqa: SLF001
    core._fold_group_ledger(_group(core, onset_s=STEP_S + 90.0))  # noqa: SLF001
    edge = core.topology_inference.edge(FEED, BRANCH)
    assert edge is not None
    assert edge.evidence.conflict == 0
    assert edge.evidence.support == 1  # seed only: the non-flat window is neutral
