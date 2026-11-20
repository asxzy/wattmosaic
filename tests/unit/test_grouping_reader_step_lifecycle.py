"""P5 regressions from the live install: reader-step groups must reach disk
with their deltas and must reach CONFIRMED (design §4, HB §12.3).

* A reader step that joins a group whose revision is already stored (an
  HA-only group persisted at creation) changes the group's content.  Stored
  revisions are immutable, so the grown group is a new ``revision + 1`` row;
  before, only the lifecycle column moved and every stored group kept its lone
  HA sub-event with ``reader_deltas == []``.
* P4's first post-sample makes a reader group PROVISIONAL.  A quiet
  PROVISIONAL group *with* reader deltas is CONFIRMED (FINAL), never
  UNRESOLVED; only a group without watts is UNRESOLVED.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from wattmosaic_engine.contracts import (
    EventGroupStatus,
    HAEvent,
    MeasurementEvent,
    StepClass,
    VersionTriple,
)
from wattmosaic_engine.realtime.grouping import DEFAULT_QUIET_S, GroupingEngine
from wattmosaic_engine.storage.database import Database
from wattmosaic_engine.storage.repositories.evidence import EvidenceRepository

T0 = datetime(2026, 11, 18, 12, 0, 0, tzinfo=UTC)
VERSIONS = VersionTriple(topology_version_id="topo-1", mapping_version_id="map-1", model_set_id="mset-x")


def _ha(entity: str, at: datetime) -> HAEvent:
    return HAEvent.make(
        entity_id=entity,
        observed_at=at,
        received_at=at,
        from_state="off",
        to_state="on",
        linked=False,
    )


def _step(reader: str, at: datetime, delta_w: float = 220.0) -> MeasurementEvent:
    return MeasurementEvent(
        event_id=f"mev-{reader}-{at.strftime('%H%M%S')}",
        reader_id=reader,
        delta_w=delta_w,
        sigma_w=2.0,
        z_score=9.0,
        step_class=StepClass.SUSTAINED,
        onset_lo=at,
        onset_hi=at + timedelta(seconds=10),
        pre_level_w=100.0,
        post_level_w=100.0 + delta_w,
        quality_score=0.9,
        detected_at=at + timedelta(seconds=20),
    )


@pytest.fixture()
def repo(tmp_path: Any) -> EvidenceRepository:
    return EvidenceRepository(Database(tmp_path / "groups.db"))


def test_step_joining_a_stored_ha_group_is_persisted_as_a_new_revision(repo: EvidenceRepository) -> None:
    ready: list[Any] = []
    engine = GroupingEngine(versions=VERSIONS, repository=repo, on_ready=ready.append)
    engine.on_ha_event(_ha("switch.hot_tub", T0), now=T0)
    first = engine.groups()[0]
    stored_v1 = repo.latest_group(first.group_id)
    assert stored_v1 is not None and stored_v1.reader_deltas == []

    at = T0 + timedelta(seconds=2)
    engine.on_reader_step(_step("ct_hot_tub", at), now=at, evidence_id="evd-hot-tub-1")
    engine.on_reader_step(_step("ct_main", at), now=at, evidence_id="evd-main-1")

    stored = repo.latest_group(first.group_id)
    assert stored is not None
    assert stored.revision > stored_v1.revision
    assert [d.reader_id for d in stored.reader_deltas] == ["ct_hot_tub"]
    assert stored.reader_deltas[0].evidence_ref.id == "evd-hot-tub-1"
    assert "reader_step" in {str(getattr(s.kind, "value", s.kind)) for s in stored.sub_events}
    # Revision 1 on disk is untouched (append-only).
    assert repo.group_revision(first.group_id, stored_v1.revision).model_dump_json() == stored_v1.model_dump_json()
    assert ready and ready[0].reader_deltas

    # The group goes quiet: the CONFIRMED revision on disk carries both steps.
    quiet = at + timedelta(seconds=DEFAULT_QUIET_S + 1)
    engine.tick(quiet)
    final = repo.latest_group(first.group_id)
    assert final is not None
    assert final.status == EventGroupStatus.CONFIRMED
    assert sorted(d.reader_id for d in final.reader_deltas) == ["ct_hot_tub", "ct_main"]


def test_provisional_reader_group_is_confirmed_not_unresolved(repo: EvidenceRepository) -> None:
    engine = GroupingEngine(versions=VERSIONS, repository=repo)
    engine.on_reader_step(_step("ct_a", T0), now=T0, evidence_id="evd-a")
    group = engine.on_provisional(SimpleNamespace(evidence_id="evd-a"))  # type: ignore[arg-type]
    assert group is not None and group.status == EventGroupStatus.PROVISIONAL

    changed = engine.tick(T0 + timedelta(seconds=DEFAULT_QUIET_S + 30))
    assert [g.status for g in changed] == [EventGroupStatus.CONFIRMED]
    stored = repo.latest_group(group.group_id)
    assert stored is not None and stored.status == EventGroupStatus.CONFIRMED
    assert stored.reader_deltas and stored.reader_deltas[0].reader_id == "ct_a"


def test_quiet_ha_only_group_is_still_unresolved(repo: EvidenceRepository) -> None:
    engine = GroupingEngine(versions=VERSIONS, repository=repo)
    engine.on_ha_event(_ha("switch.lamp", T0), now=T0)
    changed = engine.tick(T0 + timedelta(seconds=DEFAULT_QUIET_S + 1))
    assert [g.status for g in changed] == [EventGroupStatus.UNRESOLVED]


def test_a_flapping_entity_does_not_chain_one_endless_group(repo: EvidenceRepository) -> None:
    """Pairwise overlap must not chain an automation firing every 3 s into one
    group that never goes quiet (the live install's groups never confirmed)."""
    engine = GroupingEngine(versions=VERSIONS, repository=repo)
    for i in range(200):  # 10 minutes of changes 3 s apart
        at = T0 + timedelta(seconds=3 * i)
        engine.on_ha_event(_ha(f"automation.sync_{i % 2}", at), now=at)
    spans = [
        max(s.observed_at for s in g.sub_events) - g.opened_at for g in engine.groups()
    ]
    assert len(spans) >= 3
    assert max(spans) <= timedelta(seconds=engine.config.max_span_s)
    # Every group but the newest one goes quiet and closes.
    end = T0 + timedelta(seconds=600 + DEFAULT_QUIET_S + 1)
    engine.tick(end)
    assert all(g.status == EventGroupStatus.UNRESOLVED for g in engine.groups())
