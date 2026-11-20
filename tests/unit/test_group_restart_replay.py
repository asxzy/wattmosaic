"""P5 survives a restart that re-derives groups already on disk.

Group ids are a hash of the member sub-event ids, and the "already persisted"
set lives in memory.  A restarted engine replays HA states and backfills
history, re-deriving the same ``(group_id, revision)``; appending it again
violated the ``event_groups`` primary key, so every HA event in the fast loop
and every steady pass raised ``sqlite3.IntegrityError`` and no data flowed
(seen on a live HA after the second start on the same volume).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from wattmosaic_engine.contracts import (
    EventGroupStatus,
    HAEvent,
    MeasurementEvent,
    StepClass,
    VersionTriple,
)
from wattmosaic_engine.realtime.grouping import GroupingEngine
from wattmosaic_engine.storage.database import Database
from wattmosaic_engine.storage.repositories.evidence import EvidenceRepository

from tests.support.fakes import FakeTopology

T0 = datetime(2026, 11, 20, 3, 0, 0, tzinfo=UTC)
TRIPLE = VersionTriple(topology_version_id="topo-1", mapping_version_id="map-1", model_set_id="mset-1")
TREE = {"feed_l1": {"kitchen_ckt": {}}}


def _step(reader: str, onset: datetime) -> MeasurementEvent:
    return MeasurementEvent(
        event_id=f"mev-{reader}-{onset.strftime('%H%M%S')}",
        reader_id=reader,
        delta_w=850.0,
        sigma_w=2.0,
        z_score=12.0,
        step_class=StepClass.SUSTAINED,
        onset_lo=onset,
        onset_hi=onset + timedelta(seconds=5),
        pre_level_w=300.0,
        post_level_w=1150.0,
        quality_score=0.9,
        detected_at=onset + timedelta(seconds=10),
    )


def _ha(at: datetime) -> HAEvent:
    return HAEvent.make(
        entity_id="switch.dishwasher",
        observed_at=at,
        received_at=at + timedelta(seconds=1),
        from_state="off",
        to_state="on",
        context_id="ctx-dw",
        context_parent_id=None,
        linked=True,
    )


def _engine(db: Database) -> GroupingEngine:
    return GroupingEngine(
        versions=TRIPLE,
        repository=EvidenceRepository(db),
        topology=FakeTopology.from_tree(TREE),
    )


def test_a_restarted_engine_adopts_stored_revisions_instead_of_raising(tmp_path: Path) -> None:
    db = Database(tmp_path / "restart.db")
    first = _engine(db)
    stored = first.on_reader_step(_step("kitchen_ckt", T0), now=T0)
    first.on_ha_event(_ha(T0), now=T0)

    # Process restart: same database, fresh in-memory bookkeeping, same inputs.
    second = _engine(db)
    replayed = second.on_reader_step(_step("kitchen_ckt", T0), now=T0)
    second.on_ha_event(_ha(T0), now=T0)
    assert replayed.group_id == stored.group_id

    # The steady loop's tick finalizes the group; that lifecycle move must be
    # recorded on the stored row rather than raising on a re-insert.
    second.tick(T0 + timedelta(minutes=10))
    rows = db._conn.execute(  # noqa: SLF001 - assert on the stored rows
        "SELECT revision, status, closed_at FROM event_groups WHERE group_id=?",
        (stored.group_id,),
    ).fetchall()
    revisions = [row[0] for row in rows]
    assert len(revisions) == len(set(revisions)), "a revision was stored twice"
    final = second.current(stored.group_id)
    if final is not None and final.status in (EventGroupStatus.CONFIRMED, EventGroupStatus.UNRESOLVED):
        latest = max(rows, key=lambda row: row[0])
        assert latest[1] == final.status.value
        assert latest[2] is not None


def test_has_group_revision_reports_stored_rows(tmp_path: Path) -> None:
    db = Database(tmp_path / "has.db")
    engine = _engine(db)
    group = engine.on_reader_step(_step("kitchen_ckt", T0), now=T0)
    repo = EvidenceRepository(db)
    assert repo.has_group_revision(group.group_id, group.revision)
    assert not repo.has_group_revision(group.group_id, group.revision + 1)
    assert not repo.has_group_revision("grp-unknown", 1)
