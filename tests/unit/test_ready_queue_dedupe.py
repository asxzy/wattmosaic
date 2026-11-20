"""The ready queue holds one slot per group; unchanged groups are not re-inferred.

Regression for the live install, where ``event group queue full; dropped the
oldest group`` fired 4,595 times in 10 minutes: every HA change that joined a
MATCHING/PROVISIONAL group re-delivered it (P5 ``_refresh_ready``), each
delivery took a new slot of the 1,000-slot drop-oldest queue, and unrelated
groups were pushed out before inference ever saw them.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from wattmosaic_engine.config.settings import EngineConfig
from wattmosaic_engine.contracts import (
    EventGroup,
    EventGroupStatus,
    EvidenceRef,
    ReaderDelta,
    SubEvent,
    SubEventKind,
    VersionTriple,
)
from wattmosaic_engine.ingestion.queue import BoundedQueue
from wattmosaic_engine.main import EngineCore
from wattmosaic_engine.storage.database import Database

T0 = datetime(2026, 11, 18, 3, 0, 0, tzinfo=UTC)


def _group(group_id: str, n_subs: int, status: EventGroupStatus = EventGroupStatus.MATCHING) -> EventGroup:
    subs = [
        SubEvent(
            sub_event_id=f"sub-ha-{group_id}-{i}",
            kind=SubEventKind.HA_STATE,
            ref=EvidenceRef(kind="ha_event", id=f"hae-{group_id}-{i}"),
            observed_at=T0 + timedelta(seconds=i),
            timestamp_uncertainty_s=0.0,
        )
        for i in range(n_subs)
    ]
    return EventGroup(
        group_id=group_id,
        revision=1,
        status=status,
        opened_at=T0,
        sub_events=subs,
        reader_deltas=[
            ReaderDelta(reader_id="ct_a", delta_w=100.0, sigma_w=2.0, completeness="complete")
        ],
        grouping_reasons=[],
        versions=VersionTriple(topology_version_id="topo-1", mapping_version_id="map-1", model_set_id="mset-x"),
    )


def test_put_or_replace_keeps_one_slot_per_key() -> None:
    queue: BoundedQueue[EventGroup] = BoundedQueue(3)
    for i in range(50):
        queue.put_or_replace(_group("grp-a", i + 1), lambda g: g.group_id)
    queue.put_or_replace(_group("grp-b", 1), lambda g: g.group_id)
    assert queue.stats()["depth"] == 2
    assert queue.stats()["dropped"] == 0
    first = queue.get_nowait()
    assert first is not None and first.group_id == "grp-a" and len(first.sub_events) == 50


def test_rereadied_group_does_not_flood_the_queue_and_unchanged_is_not_reinferred() -> None:
    now = [T0]
    db = Database(":memory:")
    config = EngineConfig(
        database_path=":memory:", ha_token="t", api_token="t", learning_enabled=False, event_queue_max=10
    )
    core = EngineCore(config, db, clock=lambda: now[0])
    core._wire_v3_services()
    try:
        # A group re-delivered 5,000 times (an HA change joining it each time)
        # plus 5 other groups: nothing is dropped.
        for i in range(5000):
            core.on_group_ready(_group("grp-busy", 1 + i % 7))
        for k in range(5):
            core.on_group_ready(_group(f"grp-other-{k}", 1))
        stats = core.groups_ready.stats()
        assert stats["dropped"] == 0
        assert core.groups_dropped == 0
        assert stats["depth"] == 6

        # Inferring a group publishes it once; the same content and lifecycle
        # re-delivered is skipped, a lifecycle change is inferred again.
        group = core.groups_ready.get_nowait()
        assert group is not None
        bundle = core._build_input_bundle(group)
        assert bundle is not None
        core._publish_group_result(bundle["group"], core._run_infer_group(bundle), bundle)
        assert core._build_input_bundle(group) is None
        assert core.groups_unchanged_skipped == 1
        confirmed = group.model_copy(update={"status": EventGroupStatus.CONFIRMED})
        assert core._build_input_bundle(confirmed) is not None
    finally:
        core.background_executor.shutdown(wait=False)
        core.inference_executor.shutdown(wait=False)
        db.close()
