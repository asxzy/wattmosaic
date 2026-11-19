"""I6: versioned event inference records (HB §12.2, AT-18/AT-19 support).

Covers the task's test bullets:

* revision 1 is PROVISIONAL from Main only; a Circuit A sample 20 s late gives
  revision 2 REVISED with changed attribution; revision 1 and its evidence stay
  byte-identical (AT-18);
* a replay with a new model version appends a ``replay`` revision (AT-19 support);
* window closure appends CONFIRMED / UNRESOLVED;
* append-only persistence through an injected S16-shaped sink;
* null semantics: an absent delta is ``None``, never 0;
* the read APIs (``get``/``latest``/``history``/``evidence_bytes``).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from wattmosaic_engine.contracts import (
    EstimateStatus,
    EventGroupStatus,
    IdentifiabilityStatus,
    InferenceMethod,
    VersionTriple,
)
from wattmosaic_engine.inference import records

T0 = datetime(2026, 1, 1, tzinfo=UTC)
V1 = VersionTriple(
    topology_version_id="topo-1", mapping_version_id="map-1", model_set_id="mset-a"
)
V2 = VersionTriple(
    topology_version_id="topo-1", mapping_version_id="map-1", model_set_id="mset-b"
)


class RecordingSink:
    """Minimal S16 ``put_record`` sink; appends only, never edits."""

    def __init__(self) -> None:
        self.records: list[object] = []

    def put_record(self, record: object) -> None:
        self.records.append(record)


def test_initial_record_is_provisional_with_null_deltas() -> None:
    store = records.EventRecordStore()
    rev1 = store.open_provisional(["ct_main"], at=T0, versions=V1)
    assert rev1.interpretation_revision == 1
    assert rev1.previous_record_id is None
    assert rev1.reason == "initial"
    assert rev1.status == EventGroupStatus.PROVISIONAL
    assert rev1.status == rev1.result.status
    assert rev1.group_id == rev1.event_group_id
    assert all(u.delta_w is None for u in rev1.result.unknown_estimates)
    assert all(v is None for v in rev1.result.residual_w_by_reader.values())


def test_late_sample_revises_without_rewriting_history() -> None:
    store = records.EventRecordStore()
    rev1 = store.open_provisional(["ct_main"], at=T0, versions=V1)
    rev1_bytes = rev1.model_dump_json()
    evidence1 = store.evidence_bytes(rev1.record_id)

    rev2 = store.apply_late_sample(
        rev1.group_id, reader="ct_a", delta_w=45.0, late_by_s=20.0, at=T0 + timedelta(seconds=20)
    )
    assert rev2.status == EventGroupStatus.REVISED
    assert rev2.reason == "late_data"
    assert rev2.interpretation_revision == 2
    assert rev2.previous_record_id == rev1.record_id
    assert rev2.result != rev1.result
    assert len(rev2.result.device_estimates) == 1
    assert rev2.result.device_estimates[0].point_w == pytest.approx(45.0)

    # Revision 1 and its evidence are byte-identical after the append.
    assert store.get(rev1.record_id).model_dump_json() == rev1_bytes
    assert store.evidence_bytes(rev1.record_id) == evidence1
    assert store.latest(rev1.group_id).record_id == rev2.record_id


def test_history_is_ordered_oldest_first_and_traceable() -> None:
    store = records.EventRecordStore()
    r1 = store.open_provisional(["ct_main"], at=T0)
    r2 = store.apply_late_sample(r1.group_id, reader="ct_a", delta_w=45.0, late_by_s=20.0, at=T0)
    r3 = store.close_window(r1.group_id, at=T0, status=EventGroupStatus.CONFIRMED)
    history = store.history(r1.group_id)
    assert [r.interpretation_revision for r in history] == [1, 2, 3]
    assert [r.record_id for r in history] == [r1.record_id, r2.record_id, r3.record_id]
    assert history[1].previous_record_id == r1.record_id
    assert history[2].previous_record_id == r2.record_id


def test_close_window_confirmed_and_unresolved() -> None:
    for status in (EventGroupStatus.CONFIRMED, EventGroupStatus.UNRESOLVED):
        store = records.EventRecordStore()
        r1 = store.open_provisional(["ct_main"], at=T0)
        closed = store.close_window(r1.group_id, at=T0, status=status)
        assert closed.reason == "window_closed"
        assert closed.status == status
        assert closed.result.status == status
    with pytest.raises(ValueError):
        store.close_window(r1.group_id, at=T0, status=EventGroupStatus.PROVISIONAL)


def test_replay_with_new_model_version_appends_replay_revision() -> None:
    """AT-19 support: a newer model version gives a new interpretation version."""
    store = records.EventRecordStore()
    r1 = store.open_provisional(["ct_main"], at=T0, versions=V1)
    r1_bytes = r1.model_dump_json()

    replay = store.revise_for_update(
        r1.group_id, reason="replay", at=T0, versions=V2
    )
    assert replay.reason == "replay"
    assert replay.interpretation_revision == 2
    assert replay.previous_record_id == r1.record_id
    assert replay.result.versions.model_set_id == V2.model_set_id
    assert store.get(r1.record_id).model_dump_json() == r1_bytes


def test_model_and_topology_updates_append_revisions() -> None:
    store = records.EventRecordStore()
    r1 = store.open_provisional(["ct_main"], at=T0)
    for reason in ("model_update", "topology_update"):
        updated = store.revise_for_update(r1.group_id, reason=reason, at=T0)  # type: ignore[arg-type]
        assert updated.reason == reason
    assert [r.reason for r in store.history(r1.group_id)] == [
        "initial",
        "model_update",
        "topology_update",
    ]


def test_revision_chain_validator_rejects_bad_chain() -> None:
    result = records.build_initial_result(group_id="g", readers=["ct_main"], at=T0)
    with pytest.raises(ValueError, match="previous_record_id"):
        records.VersionedEventRecord(
            record_id="bad",
            event_group_id="g",
            interpretation_revision=1,
            previous_record_id="rec-0",
            result=result,
            reason="initial",
            created_at=T0,
            status=EventGroupStatus.PROVISIONAL,
        )
    with pytest.raises(ValueError, match="status"):
        records.VersionedEventRecord(
            record_id="bad",
            event_group_id="g",
            interpretation_revision=2,
            previous_record_id="rec-0",
            result=result,
            reason="late_data",
            created_at=T0,
            status=EventGroupStatus.CONFIRMED,
        )


def test_sink_receives_every_append_in_order() -> None:
    sink = RecordingSink()
    store = records.EventRecordStore(sink=sink)
    r1 = store.open_provisional(["ct_main"], at=T0)
    r2 = store.apply_late_sample(r1.group_id, reader="ct_a", delta_w=45.0, late_by_s=20.0, at=T0)
    r3 = store.close_window(r1.group_id, at=T0)
    assert [r.interpretation_revision for r in sink.records] == [1, 2, 3]  # type: ignore[attr-defined]
    assert [r.record_id for r in sink.records] == [  # type: ignore[attr-defined]
        r1.record_id,
        r2.record_id,
        r3.record_id,
    ]


def test_revise_result_does_not_mutate_previous() -> None:
    previous = records.build_initial_result(group_id="g", readers=["ct_main"], at=T0)
    before = previous.model_dump_json()
    revised = records.revise_result(previous, reader="ct_a", delta_w=45.0, at=T0)
    assert previous.model_dump_json() == before
    assert revised.result_id != previous.result_id
    assert len(revised.device_estimates) == 1


def test_revise_result_uses_justified_bounds_and_identifiability() -> None:
    previous = records.build_initial_result(group_id="g", readers=["ct_main"], at=T0)
    revised = records.revise_result(
        previous,
        reader="ct_a",
        delta_w=45.0,
        at=T0,
        model_version_id="mdl-ct_a-1",
    )
    estimate = revised.device_estimates[0]
    assert estimate.status == EstimateStatus.ESTIMATED
    assert estimate.method == InferenceMethod.JOINT_DELTA
    assert estimate.identifiability == IdentifiabilityStatus.IDENTIFIABLE
    assert estimate.model_version_id == "mdl-ct_a-1"
    assert estimate.bounds.lower_w is not None and estimate.bounds.upper_w is not None
    assert estimate.bounds.lower_w <= 45.0 <= estimate.bounds.upper_w  # type: ignore[operator]


def test_writer_direct_append() -> None:
    sink = RecordingSink()
    writer = records.RecordWriter(group_id="g", sink=sink)
    result = records.build_initial_result(group_id="g", readers=["ct_main"], at=T0)
    first = writer.write(result, "initial", previous=None, at=T0)
    second = writer.write(result, "late_data", previous=first, at=T0)
    assert first.interpretation_revision == 1
    assert second.interpretation_revision == 2
    assert second.previous_record_id == first.record_id
    assert len(sink.records) == 2


def test_unknown_group_revision_raises() -> None:
    store = records.EventRecordStore()
    with pytest.raises(KeyError):
        store.apply_late_sample("missing", reader="ct_a", delta_w=1.0, late_by_s=1.0, at=T0)
    with pytest.raises(KeyError):
        store.close_window("missing", at=T0)
    with pytest.raises(KeyError):
        store.revise_for_update("missing", reason="replay", at=T0)


def test_get_missing_record_returns_none() -> None:
    store = records.EventRecordStore()
    assert store.get("nope") is None
    assert store.latest("nope") is None
    assert store.evidence_bytes("nope") == b""


def test_deterministic_ids_under_replay() -> None:
    a = records.EventRecordStore()
    b = records.EventRecordStore()
    ra = a.open_provisional(["ct_main"], at=T0, group_id="g", versions=V1)
    rb = b.open_provisional(["ct_main"], at=T0, group_id="g", versions=V1)
    assert ra.record_id == rb.record_id
    assert ra.model_dump_json() == rb.model_dump_json()



# --------------------------------------------------------------------------- #
# Read API handlers (GET /events, GET /events/{group_id}) — I6 owns these routes.
# --------------------------------------------------------------------------- #


def test_events_routes_are_registered() -> None:
    from aiohttp import web
    from wattmosaic_engine.api import routes

    router = web.UrlDispatcher()
    names = routes.register_all(router, "/api/v1", deps=None)
    assert "events" in names
    canonical = {r.resource.canonical for r in router.routes() if getattr(r, "resource", None)}
    assert "/api/v1/events" in canonical
    assert "/api/v1/events/{group_id}" in canonical


def test_event_handler_returns_group_and_revision_chain() -> None:
    from aiohttp import web
    from aiohttp.test_utils import make_mocked_request
    from wattmosaic_engine.api.routes import events as event_routes

    store = records.EventRecordStore()
    r1 = store.open_provisional(["ct_main"], at=T0)
    store.apply_late_sample(r1.group_id, reader="ct_a", delta_w=45.0, late_by_s=20.0, at=T0)

    from wattmosaic_engine.api.server import FACADE_KEY, NullFacade

    app = web.Application()
    app[FACADE_KEY] = NullFacade()
    app[event_routes.EVENT_RECORD_STORE_KEY] = store

    request = make_mocked_request(
        "GET", f"/api/v1/events/{r1.group_id}", app=app, match_info={"group_id": r1.group_id}
    )
    response = asyncio.run(event_routes.event(request))
    assert response.status == 200
    import json

    body = json.loads(response.text)
    assert body["group_id"] == r1.group_id
    assert [r["interpretation_revision"] for r in body["records"]] == [1, 2]


def test_events_handler_lists_empty_without_a_backend() -> None:
    from aiohttp import web
    from aiohttp.test_utils import make_mocked_request
    from wattmosaic_engine.api.routes import events as event_routes
    from wattmosaic_engine.api.server import FACADE_KEY, NullFacade

    app = web.Application()
    app[FACADE_KEY] = NullFacade()
    request = make_mocked_request("GET", "/api/v1/events", app=app)
    response = asyncio.run(event_routes.events(request))
    assert response.status == 200
