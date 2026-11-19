"""Z7 — P5 closes groups formed under a superseded version triple.

A topology/mapping/model promotion must not leave a group formed under the old
triple open: a later step joining it would be re-inferred with the superseded
observation operator (the HB §17 relocated TV credited to the circuit it left).
``GroupingEngine.rebind`` finalizes such groups under their own triple and
makes them terminal; later sub-events form new groups under the new triple.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
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

T0 = datetime(2026, 11, 18, 12, 0, 0, tzinfo=UTC)
OLD = VersionTriple(topology_version_id="topo-1", mapping_version_id="map-2", model_set_id="mset-x")
NEW = VersionTriple(topology_version_id="topo-1", mapping_version_id="map-3", model_set_id="mset-x")
HB17 = {"Main": {"Subpanel": {"Circuit A": {}}, "Circuit C": {}}}


def _step(reader: str, onset: datetime, delta_w: float = -116.0) -> MeasurementEvent:
    return MeasurementEvent(
        event_id=f"mev-{reader}-{onset.strftime('%H%M%S')}",
        reader_id=reader,
        delta_w=delta_w,
        sigma_w=1.0,
        z_score=-8.0,
        step_class=StepClass.SUSTAINED,
        onset_lo=onset,
        onset_hi=onset + timedelta(seconds=5),
        pre_level_w=420.0,
        post_level_w=420.0 + delta_w,
        quality_score=0.9,
        detected_at=onset + timedelta(seconds=10),
    )


def _ha(entity: str, at: datetime, *, context_id: str = "ctx-tv") -> HAEvent:
    return HAEvent.make(
        entity_id=entity,
        observed_at=at,
        received_at=at + timedelta(seconds=1),
        from_state="on",
        to_state="off",
        context_id=context_id,
        context_parent_id=None,
        linked=True,
    )


@pytest.fixture()
def repo(tmp_path: Any) -> EvidenceRepository:
    return EvidenceRepository(Database(tmp_path / "z7.db"))


def test_promotion_closes_an_open_group_under_its_own_triple(repo: EvidenceRepository) -> None:
    ready: list[Any] = []
    topology = FakeTopology.from_tree(HB17)
    engine = GroupingEngine(versions=OLD, repository=repo, topology=topology, on_ready=ready.append)
    old = engine.on_reader_step(_step("Circuit A", T0), now=T0)
    assert old.status == EventGroupStatus.MATCHING and old.versions == OLD

    promoted_at = T0 + timedelta(seconds=20)
    closed = engine.rebind(NEW, topology, now=promoted_at)

    assert [g.group_id for g in closed] == [old.group_id]
    final = engine.current(old.group_id)
    assert final is not None
    # Finalized under its *own* triple, same revision, never re-stamped.
    assert final.status == EventGroupStatus.CONFIRMED
    assert final.closed_at == promoted_at
    assert final.versions == OLD
    assert final.revision == old.revision
    assert final.sub_events == old.sub_events
    # The lifecycle transition reached S16 (the revision row itself is immutable).
    stored = repo.group_revision(old.group_id, old.revision)
    assert stored is not None
    assert stored.status == EventGroupStatus.CONFIRMED
    assert stored.versions == OLD

    # A related, co-temporal step after the promotion would have joined the old
    # group (Main contains Circuit A, 10 s apart); now it forms a new group
    # stamped with the new triple, and the old group is untouched.
    after = engine.on_reader_step(_step("Main", T0 + timedelta(seconds=30)), now=promoted_at)
    assert after.group_id != old.group_id
    assert after.versions == NEW
    assert engine.current(old.group_id).sub_events == old.sub_events  # type: ignore[union-attr]
    assert ready[-1].group_id == after.group_id and ready[-1].versions == NEW


def test_promotion_closes_an_ha_only_group_unresolved_and_it_is_not_reopened(
    repo: EvidenceRepository,
) -> None:
    engine = GroupingEngine(versions=OLD, repository=repo, topology=FakeTopology.from_tree(HB17))
    old = engine.on_ha_event(_ha("switch.tv_plug", T0), now=T0)
    assert old.status == EventGroupStatus.DETECTED

    engine.rebind(NEW, FakeTopology.from_tree(HB17), now=T0 + timedelta(seconds=1))
    assert engine.current(old.group_id).status == EventGroupStatus.UNRESOLVED  # type: ignore[union-attr]

    # Same context two seconds later: not attached to the closed group.
    sibling = engine.on_ha_event(_ha("light.tv_backlight", T0 + timedelta(seconds=2)), now=T0)
    assert sibling.group_id != old.group_id and sibling.versions == NEW
    # A reader step overlapping the old HA change does not re-open it either
    # (an UNRESOLVED HA-only group normally is re-opened by its confirming step).
    step = engine.on_reader_step(_step("Circuit A", T0), now=T0 + timedelta(seconds=3))
    assert step.group_id != old.group_id
    assert step.versions == NEW
    assert engine.current(old.group_id).status == EventGroupStatus.UNRESOLVED  # type: ignore[union-attr]


def test_rebinding_to_the_same_triple_closes_nothing(repo: EvidenceRepository) -> None:
    engine = GroupingEngine(versions=OLD, repository=repo, topology=FakeTopology.from_tree(HB17))
    group = engine.on_reader_step(_step("Circuit A", T0), now=T0)
    assert engine.rebind(OLD, FakeTopology.from_tree(HB17), now=T0 + timedelta(seconds=1)) == []
    joined = engine.on_reader_step(_step("Main", T0 + timedelta(seconds=2)), now=T0 + timedelta(seconds=2))
    assert joined.group_id == group.group_id
    assert engine.current(group.group_id).status == EventGroupStatus.MATCHING  # type: ignore[union-attr]
