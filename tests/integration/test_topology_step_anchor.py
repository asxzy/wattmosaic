"""T5 measures a parent across the child's *own* step, not the group's onset.

Live dev server (2026-11-20): P5 groups open on an HA event, and a member's step
sits -61 s .. +103 s from that onset (10th-90th percentile).  The parent's level
was measured over 120 s / 150 s windows around the onset, so a busy feed mixed in
unrelated changes: true parents read flat (conflict) or off-size (neutral), and
a reader that could not be measured at all counted as "did not move".  Only a
handful of edges ever confirmed.
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
from wattmosaic_engine.main import EngineCore
from wattmosaic_engine.storage.database import Database

T0 = datetime(2026, 11, 20, 8, 0, 0, tzinfo=UTC)
FEED, BRANCH = "feed_leg", "branch_ckt"
CADENCE_S = 18.0
STEP_S = 1500.0  # after P3's sigma warm-up
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
    db = Database(tmp_path / "anchor.db")
    core = EngineCore(
        EngineConfig(database_path=str(tmp_path / "anchor.db"), ha_token="x"),
        db,
        clock=lambda: now[0],
    )
    for reader in (FEED, BRANCH):
        db.repos.entities.upsert_endpoint(
            {"endpoint_id": reader, "kind": "ct", "entity_id": f"sensor.{reader}_power",
             "scope": "circuit", "scope_status": "candidate", "enabled": True,
             "created_at": T0 - timedelta(days=1)}
        )
    core._wire_v3_services()  # noqa: SLF001 - the production start path
    return core


def _branch_w(t: float) -> float:
    return (40.0 if t < STEP_S else 40.0 + STEP_W) + (0.4 if int(t / CADENCE_S) % 2 else -0.4)


def _feed_w(t: float, *, other_load_at: float | None) -> float:
    # The feed carries the branch plus 1 kW of other loads; one of them adds
    # 300 W a little after the branch step (concurrent activity on a busy main).
    extra = 300.0 if other_load_at is not None and t >= other_load_at else 0.0
    return 1000.0 + _branch_w(t) + extra


async def _feed_history(core: EngineCore, now: list[datetime], *, feed_cadence_s: float,
                        other_load_at: float | None, until_s: float) -> None:
    readings: list[ReaderMeasurement] = []
    t, seq = 0.0, 0
    while t <= until_s:
        readings.append(_reading(BRANCH, T0 + timedelta(seconds=t), _branch_w(t), seq))
        t += CADENCE_S
        seq += 1
    # A different device: its own phase (7 s later) and possibly its own cadence.
    t = 7.0
    while t <= until_s:
        readings.append(
            _reading(FEED, T0 + timedelta(seconds=t), _feed_w(t, other_load_at=other_load_at), seq)
        )
        t += feed_cadence_s
        seq += 1
    for reading in sorted(readings, key=lambda r: r.observed_at):
        now[0] = reading.observed_at
        await core.on_reading(reading)


def _group(core: EngineCore, *, onset_s: float) -> EventGroup:
    steps = core._t5_steps[BRANCH]  # noqa: SLF001 - the P3 step the runtime recorded
    evidence_id, _lo, _hi, delta = steps[-1]
    assert abs(delta - STEP_W) < 50.0
    return EventGroup(
        group_id="grp-anchor",
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


async def test_a_parent_is_measured_across_the_childs_own_step(tmp_path: Path) -> None:
    now = [T0]
    core = _core(tmp_path, now)
    await _feed_history(core, now, feed_cadence_s=CADENCE_S, other_load_at=STEP_S + 100.0,
                        until_s=STEP_S + 400.0)
    assert core._t5_steps.get(BRANCH), "P3 never recorded the branch step"  # noqa: SLF001

    # The group opened on an HA event 90 s after the branch step; around that
    # onset the feed shows the unrelated +300 W, not the branch's +500 W.
    core._fold_group_ledger(_group(core, onset_s=STEP_S + 90.0))  # noqa: SLF001
    edge = core.topology_inference.edge(FEED, BRANCH)
    assert edge is not None
    assert (edge.evidence.support, edge.evidence.conflict) == (1, 0)


async def test_a_parent_that_could_not_be_measured_is_no_evidence(tmp_path: Path) -> None:
    now = [T0]
    core = _core(tmp_path, now)
    # The feed reports only every 100 s, so neither the step-anchored nor the
    # onset windows hold enough samples: its silence is unknown, not "flat".
    await _feed_history(core, now, feed_cadence_s=100.0, other_load_at=None,
                        until_s=STEP_S + 60.0)
    # Make the pair exist first so a conflict would have somewhere to land.
    from wattmosaic_engine.electrical.topology_inference import DeltaObservation

    core.topology_inference = core.topology_inference.observe_group(
        DeltaObservation(group_id="seed", deltas={FEED: (500.0, 1.0), BRANCH: (500.0, 1.0)},
                         at=T0, cause_id="seed")
    )
    core._fold_group_ledger(_group(core, onset_s=STEP_S + 10.0))  # noqa: SLF001
    edge = core.topology_inference.edge(FEED, BRANCH)
    assert edge is not None
    assert edge.evidence.conflict == 0
