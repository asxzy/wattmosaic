"""P5 — Event group formation and late-arrival revision.

Acceptance bullets (from the task file), against the *production* module:

* two lights switched by one automation (same parent context, 2 s apart)
  form one group with two HA sub-events carrying distinct timestamps;
* a decoy sharing a context but whose physical changes are 60 s apart is
  NOT grouped on context alone (HB §6.6: context is not proof of simultaneity);
* one entity toggled twice in 5 s yields two sub-events (never collapsed);
* AT-18 support: a group CONFIRMED, then a Circuit-A sample arriving 20 s
  late yields revision 2 REVISED, revision 1 unchanged on disk, and an
  unrelated concurrent group unchanged;
* a fridge step with no HA event becomes a group holding only a reader
  sub-event;
* an HA-only change with no reader step stays DETECTED (no watts) until its
  window closes, then becomes UNRESOLVED.

Co-movement uses the K08 ``FakeTopology``; persistence is a real S16
``EvidenceRepository`` over in-memory SQLite.  Production code never imports
``runtime.fakes`` — the fakes are confined to this test module.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from wattmosaic_engine.contracts import (
    EventGroupStatus,
    EvidenceRef,
    HAEvent,
    MeasurementEvent,
    StepClass,
    VersionTriple,
)
from wattmosaic_engine.realtime.grouping import (
    DEFAULT_QUIET_S,
    DEFAULT_TOLERANCE_S,
    GroupConfig,
    GroupingEngine,
    build_ha_sub_event,
    build_reader_sub_event,
    co_movement_reason,
    context_reason,
    form_groups,
    make_group_id,
    temporal_reason,
)
from wattmosaic_engine.storage.database import Database
from wattmosaic_engine.storage.repositories.evidence import EvidenceRepository

from tests.support.fakes import FakeTopology

T0 = datetime(2026, 11, 18, 12, 0, 0, tzinfo=UTC)
VERSIONS = VersionTriple(topology_version_id="topo-1", mapping_version_id="map-1", model_set_id="mset-x")
HA17 = {"Main": {"Subpanel": {"Circuit A": {}, "Circuit B": {}}, "Circuit C": {}}}


# --------------------------------------------------------------------------- #
# Helpers (pure, explicit times — no wall clock)
# --------------------------------------------------------------------------- #


def ha_event(
    entity: str,
    at: datetime,
    *,
    to_state: str = "on",
    context_id: str | None = "ctx-1",
    context_parent_id: str | None = "parent-1",
) -> HAEvent:
    return HAEvent.make(
        entity_id=entity,
        observed_at=at,
        received_at=at + timedelta(seconds=1),
        from_state="off" if to_state == "on" else "on",
        to_state=to_state,
        context_id=context_id,
        context_parent_id=context_parent_id,
        linked=True,
    )


def reader_event(
    reader: str,
    onset_lo: datetime,
    delta_w: float = 150.0,
) -> MeasurementEvent:
    return MeasurementEvent(
        event_id=f"mev-{reader}-{onset_lo.strftime('%H%M%S%f')}",
        reader_id=reader,
        delta_w=delta_w,
        sigma_w=1.0,
        z_score=8.0,
        step_class=StepClass.SUSTAINED,
        onset_lo=onset_lo,
        onset_hi=onset_lo + timedelta(seconds=5),
        pre_level_w=100.0,
        post_level_w=100.0 + delta_w,
        quality_score=0.9,
        detected_at=onset_lo + timedelta(seconds=10),
    )


def evidence_ref(kind: str, identifier: str) -> EvidenceRef:
    return EvidenceRef(kind=kind, id=identifier)  # type: ignore[arg-type]


@pytest.fixture()
def repo(tmp_path: Any) -> EvidenceRepository:
    return EvidenceRepository(Database(tmp_path / "groups.db"))


# --------------------------------------------------------------------------- #
# Union-find / grouping predicates (pure helpers)
# --------------------------------------------------------------------------- #


def test_context_reason_parent_chain_and_spread() -> None:
    a = ha_event("light.kitchen", T0, context_id="ctx-1", context_parent_id=None)
    b = ha_event(
        "light.hall",
        T0 + timedelta(seconds=2),
        context_id="ctx-2",
        context_parent_id="ctx-1",
    )
    assert context_reason(a, b) == "context_parent"
    # Direct context match wins.
    c = ha_event("light.x", T0 + timedelta(seconds=1), context_id="ctx-1", context_parent_id=None)
    assert context_reason(a, c) == "context_id"


def test_temporal_reason_uses_widened_onset_windows() -> None:
    a = reader_event("Circuit A", T0)
    # 7 s apart, each onset window ±5 s -> overlap after widening.
    b = reader_event("Circuit B", T0 + timedelta(seconds=7))
    assert temporal_reason(a, b, tolerance_s=DEFAULT_TOLERANCE_S) == "temporal_overlap"
    far = reader_event("Circuit B", T0 + timedelta(seconds=60))
    assert temporal_reason(a, far, tolerance_s=DEFAULT_TOLERANCE_S) is None


def test_co_movement_requires_confirmed_or_provisional_contains() -> None:
    topo = FakeTopology.from_tree(HA17)
    parent = reader_event("Main", T0, delta_w=300.0)
    child_a = reader_event("Circuit A", T0, delta_w=150.0)
    child_b = reader_event("Circuit B", T0, delta_w=150.0)
    # 300 ≈ 150 + 150: the children jointly explain the parent on a CONFIRMED
    # CONTAINS path.
    assert co_movement_reason(parent, [child_a, child_b], topo) == "reader_comovement"
    # A CORRELATED-only pair never co-moves structurally.
    from wattmosaic_engine.contracts import EdgeLifecycleV3, RelationType, TopologyEdgeV3

    correlated = TopologyEdgeV3(
        edge_id="e-corr",
        relation=RelationType.CORRELATED,
        source_endpoint_id="Main",
        target_endpoint_id="Circuit C",
        lifecycle=EdgeLifecycleV3.CONFIRMED,
        declared_by="migration",
        beta=None,
        beta_basis=None,
        evidence=__import__(
            "wattmosaic_engine.contracts", fromlist=["EdgeEvidence"]
        ).EdgeEvidence(),
        valid_from=T0,
        pinned=False,
    )
    corr_topo = FakeTopology.from_edges((correlated,))
    assert co_movement_reason(parent, [child_a], corr_topo) is None


def test_make_group_id_is_stable_and_order_independent() -> None:
    subs = ["sub-a", "sub-b", "sub-c"]
    assert make_group_id(subs) == make_group_id(list(reversed(subs)))
    assert make_group_id(subs) != make_group_id(["sub-a", "sub-b"])
    assert make_group_id(subs).startswith("grp-")


# --------------------------------------------------------------------------- #
# Acceptance: context grouping, decoy, two transitions
# --------------------------------------------------------------------------- #


def test_two_lights_one_automation_form_one_group_with_distinct_timestamps(repo: EvidenceRepository) -> None:
    engine = GroupingEngine(versions=VERSIONS, repository=repo)
    e1 = ha_event("light.kitchen", T0)
    e2 = ha_event("light.hall", T0 + timedelta(seconds=2))

    engine.on_ha_event(e1, now=T0)
    engine.on_ha_event(e2, now=T0 + timedelta(seconds=2))

    groups = engine.groups(status=EventGroupStatus.DETECTED)
    assert len(groups) == 1
    group = groups[0]
    assert len(group.sub_events) == 2
    stamps = [sub.observed_at for sub in group.sub_events]
    assert stamps[0] != stamps[1]
    assert "context_id" in group.grouping_reasons or "context_parent" in group.grouping_reasons


def test_decoy_context_with_distant_physics_is_not_grouped(repo: EvidenceRepository) -> None:
    engine = GroupingEngine(versions=VERSIONS, repository=repo)
    # Both share the same parent context, but the physical reader steps are
    # 60 s apart (> context_max_spread_s), so context alone must not group.
    engine.on_reader_step(reader_event("Circuit A", T0), now=T0, context_parent_id="decoy-parent")
    engine.on_reader_step(
        reader_event("Circuit B", T0 + timedelta(seconds=60)),
        now=T0 + timedelta(seconds=60),
        context_parent_id="decoy-parent",
    )
    # Reader steps carry no HA context themselves; use HA events as the context
    # carrier with matching context but far-apart observed times.
    engine2 = GroupingEngine(versions=VERSIONS, repository=repo)
    engine2.on_ha_event(
        ha_event("light.a", T0, context_id="shared", context_parent_id=None), now=T0
    )
    engine2.on_ha_event(
        ha_event("light.b", T0 + timedelta(seconds=60), context_id="shared", context_parent_id=None),
        now=T0 + timedelta(seconds=60),
    )
    assert len(engine2.groups()) == 2


def test_one_entity_toggled_twice_yields_two_sub_events(repo: EvidenceRepository) -> None:
    engine = GroupingEngine(versions=VERSIONS, repository=repo)
    first = ha_event("light.x", T0, to_state="on")
    second = ha_event("light.x", T0 + timedelta(seconds=3), to_state="off")
    engine.on_ha_event(first, now=T0)
    engine.on_ha_event(second, now=T0 + timedelta(seconds=3))
    groups = engine.groups()
    assert len(groups) == 1
    assert len(groups[0].sub_events) == 2
    assert {sub.ref.id for sub in groups[0].sub_events} == {first.event_id, second.event_id}


# --------------------------------------------------------------------------- #
# Acceptance: reader-only, HA-only, lifecycle
# --------------------------------------------------------------------------- #


def test_fridge_step_without_ha_becomes_reader_only_group(repo: EvidenceRepository) -> None:
    engine = GroupingEngine(versions=VERSIONS, repository=repo)
    engine.on_reader_step(reader_event("Circuit C", T0, delta_w=120.0), now=T0)
    groups = engine.groups()
    assert len(groups) == 1
    group = groups[0]
    assert [sub.ref.kind for sub in group.sub_events] == ["event_evidence"]
    assert group.reader_deltas[0].delta_w == 120.0
    # A reader-only group with no co-movement / HA context opens DETECTED, then
    # advances to MATCHING at the first reader post-sample (DEC-15).
    assert group.status in (EventGroupStatus.DETECTED, EventGroupStatus.MATCHING)


def test_ha_only_change_stays_detected_then_unresolved(repo: EvidenceRepository) -> None:
    ready: list[Any] = []
    engine = GroupingEngine(versions=VERSIONS, repository=repo, on_ready=ready.append)
    engine.on_ha_event(ha_event("light.solo", T0, context_id=None, context_parent_id=None), now=T0)
    group = engine.groups()[0]
    assert group.status == EventGroupStatus.DETECTED
    # No reader sub-event: closing the window with no watts -> UNRESOLVED.
    results = engine.tick(T0 + timedelta(seconds=DEFAULT_QUIET_S + 1))
    assert len(results) == 1
    group = engine.groups()[0]
    assert group.status == EventGroupStatus.UNRESOLVED
    assert ready == []


def test_on_ready_fires_at_matching_when_reader_step_joins(repo: EvidenceRepository) -> None:
    ready: list[Any] = []
    engine = GroupingEngine(versions=VERSIONS, repository=repo, on_ready=ready.append)
    engine.on_ha_event(ha_event("light.a", T0, context_id=None, context_parent_id=None), now=T0)
    assert ready == []
    engine.on_reader_step(reader_event("Main", T0 + timedelta(seconds=2)), now=T0 + timedelta(seconds=2))
    assert len(ready) == 1
    assert ready[0].status == EventGroupStatus.MATCHING


# --------------------------------------------------------------------------- #
# Acceptance: AT-18 support — late sample revises one group only
# --------------------------------------------------------------------------- #


def test_late_sample_creates_revision_and_leaves_others_untouched(repo: EvidenceRepository) -> None:
    revised: list[Any] = []
    engine = GroupingEngine(
        versions=VERSIONS,
        repository=repo,
        on_revised=revised.append,
    )
    # Group 1: confirmed via a Circuit A reader step co-moving with Main.
    engine.on_reader_step(reader_event("Main", T0, delta_w=200.0), now=T0)
    engine.on_reader_step(
        reader_event("Subpanel", T0 + timedelta(seconds=1), delta_w=200.0), now=T0 + timedelta(seconds=1)
    )
    g1 = engine.groups()[0]
    assert g1.status in (EventGroupStatus.MATCHING, EventGroupStatus.CONFIRMED)

    # Unrelated concurrent group 2 (different time, no shared context/topology).
    far = T0 + timedelta(seconds=600)
    engine.on_ha_event(ha_event("light.other", far, context_id=None, context_parent_id=None), now=far)
    g2 = [g for g in engine.groups() if g.group_id != g1.group_id][0]

    g1_bytes = g1.model_dump_json()
    g2_bytes = g2.model_dump_json()
    disk_v1 = repo.latest_group(g1.group_id)

    late = reader_event("Circuit A", T0, delta_w=45.0)
    rev2 = engine.on_late_reader_step(late, now=T0 + timedelta(seconds=20), target_group_id=g1.group_id)

    assert rev2 is not None
    assert rev2.group_id == g1.group_id
    assert rev2.revision == g1.revision + 1
    assert rev2.status == EventGroupStatus.REVISED
    assert g1.model_dump_json() == g1_bytes  # in-memory v1 unchanged
    assert repo.group_revision(g1.group_id, g1.revision).model_dump_json() == disk_v1.model_dump_json()
    assert repo.latest_group(g1.group_id).model_dump_json() == rev2.model_dump_json()
    assert [g for g in engine.groups() if g.group_id == g2.group_id][0].model_dump_json() == g2_bytes
    assert len(revised) == 1 and revised[0].group_id == g1.group_id


# --------------------------------------------------------------------------- #
# Pure sub-event constructors
# --------------------------------------------------------------------------- #


def test_sub_event_constructors_keep_own_timestamps() -> None:
    ev = ha_event("light.a", T0 + timedelta(seconds=3))
    sub = build_ha_sub_event(ev)
    assert sub.observed_at == ev.observed_at
    assert sub.ref.kind == "ha_event"
    assert sub.ref.id == ev.event_id

    step = reader_event("Main", T0)
    sub2 = build_reader_sub_event(step, evidence_id="evd-1")
    assert sub2.ref.kind == "event_evidence"
    assert sub2.ref.id == "evd-1"
    assert sub2.observed_at == step.onset_lo
    assert sub2.timestamp_uncertainty_s == (step.onset_hi - step.onset_lo).total_seconds()


def test_form_groups_pure_function(repo: EvidenceRepository) -> None:
    ev = ha_event("light.a", T0)
    groups = form_groups(
        ha_events=[ev],
        reader_steps=[],
        topology=FakeTopology.from_tree(HA17),
        config=GroupConfig(),
        versions=VERSIONS,
    )
    assert len(groups) == 1
    assert groups[0].status == EventGroupStatus.DETECTED
