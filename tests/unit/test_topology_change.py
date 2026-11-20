"""T7 — configuration change and self-healing workflow (HB §14).

Covers:
* the four pure inferred detectors of design §5;
* the workflow: SUSPECTED → mark → promote after N events → reject/restore;
* power models are never modified (L1 repository patched to raise on write);
* DEC-10: a user-declared CONFIRMED CONTAINS edge downgrades to CONFLICTED
  under strong, persistent contradiction (never deleted).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from wattmosaic_engine.contracts import (
    ChangeDetection,
    ChangeStatus,
    ConfigurationChangeKind,
    ConfigurationEvent,
    EdgeLifecycleV3,
    RelationType,
    TopologyEdgeV3,
    VersionTriple,
)
from wattmosaic_engine.electrical import topology_change
from wattmosaic_engine.electrical.topology_inference import (
    DeltaObservation,
    TopologyEvidence,
    TopologyInferenceView,
)
from wattmosaic_engine.versioning.workflow import (
    CONFIRMED_EVENTS,
    ConfigurationWorkflow,
    NoModelWriteError,
    assert_no_model_write,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# 1. Inferred detectors (design §5).
# --------------------------------------------------------------------------- #


def test_persistent_sign_inversion_detected() -> None:
    steps = [-30.0] * 16 + [-25.0] + [5.0, 2.0, -1.0]  # 17 of last 20 negative
    on = [True] * 20
    changes = topology_change.detect_inferred_changes(
        reader_steps={"ct_a": steps}, on_states={"ct_a": on}, now=T0
    )
    kinds = [c.kind for c in changes]
    assert ConfigurationChangeKind.CT_DIRECTION_CHANGE in kinds
    change = next(c for c in changes if c.kind == ConfigurationChangeKind.CT_DIRECTION_CHANGE)
    assert change.subject == "ct_a"


def test_sign_inversion_needs_persistence() -> None:
    steps = [-30.0] * 8 + [30.0] * 12  # only 40 % negative
    changes = topology_change.detect_inferred_changes(
        reader_steps={"ct_a": steps}, on_states={"ct_a": [True] * 20}, now=T0
    )
    assert not any(c.kind == ConfigurationChangeKind.CT_DIRECTION_CHANGE for c in changes)


def test_calibration_ratio_step_detected() -> None:
    # A child/parent ratio drifting upward over >= 10 events.
    child = [2.0, 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9]
    parent = [1.0] * 10
    changes = topology_change.detect_inferred_changes(
        reader_steps={"ct_a/ct_main": child, "ct_main": parent}, now=T0
    )
    assert any(c.kind == ConfigurationChangeKind.CALIBRATION_CHANGE for c in changes)


def test_relocation_detected_after_five_events() -> None:
    history = ["ct_a"] + ["ct_c"] * 5  # 1 old + 5 new = 6 observations
    changes = topology_change.detect_inferred_changes(
        association_history={"device:tv": history}, now=T0
    )
    assert any(c.kind == ConfigurationChangeKind.SMART_PLUG_RELOCATION for c in changes)
    change = next(c for c in changes if c.kind == ConfigurationChangeKind.SMART_PLUG_RELOCATION)
    assert change.from_id == "ct_a" and change.to_id == "ct_c"


def test_reader_removal_detected_after_silence() -> None:
    changes = topology_change.detect_reader_removals(
        reader_last_seen={"ct_b": T0 - timedelta(seconds=1000)},
        now=T0,
        missing_after_s=60.0,  # cutoff = 360 s
    )
    assert len(changes) == 1
    assert changes[0].kind == ConfigurationChangeKind.READER_REMOVAL


def test_reader_removal_ignores_recent_reader() -> None:
    changes = topology_change.detect_reader_removals(
        reader_last_seen={"ct_b": T0 - timedelta(seconds=10)},
        now=T0,
        missing_after_s=60.0,
    )
    assert changes == ()


# --------------------------------------------------------------------------- #
# 2. Workflow: SUSPECTED -> mark -> promote -> reject/restore.
# --------------------------------------------------------------------------- #


def _event(event_id: str = "evt-1") -> ConfigurationEvent:
    return ConfigurationEvent(
        event_id=event_id,
        kind="ct_direction_change",
        detection=ChangeDetection.DECLARED,
        status=ChangeStatus.SUSPECTED,
        affected_endpoint_ids=["ct_c"],
        created_at=T0,
    )


class _RecordingVersionService:
    """A faithful in-memory VersionService double (no pydantic/dataclass mix)."""

    def __init__(self) -> None:
        self.triple = VersionTriple(
            topology_version_id="topo-1",
            mapping_version_id="map-1",
            model_set_id="mset-1",
        )
        self.events: list[ConfigurationEvent] = []
        self.suspected: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = []
        self._topo_n = 1
        self._map_n = 1

    def current(self) -> VersionTriple:
        return self.triple

    def as_of(self, at: Any) -> VersionTriple:
        return self.triple

    def topology_view(self, version_id: str) -> Any:
        return None

    def mapping_view(self, version_id: str) -> Any:
        return None

    def record_configuration_event(self, ev: ConfigurationEvent) -> str:
        self.events.append(ev)
        return ev.event_id

    def mark_suspected(self, cid: str, edge_ids: Any, mapping_edge_ids: Any) -> None:
        self.suspected.append((cid, tuple(edge_ids), tuple(mapping_edge_ids)))

    def propose_topology(self, edges: Any, change_event_id: Any) -> str:
        self._topo_n += 1
        return f"topo-{self._topo_n}"

    def promote_topology(self, version_id: str, reason: str) -> None:
        self.triple = VersionTriple(
            topology_version_id=version_id,
            mapping_version_id=self.triple.mapping_version_id,
            model_set_id=self.triple.model_set_id,
        )

    def propose_mapping(self, edges: Any, change_event_id: Any) -> str:
        self._map_n += 1
        return f"map-{self._map_n}"

    def promote_mapping(self, version_id: str, reason: str) -> None:
        self.triple = VersionTriple(
            topology_version_id=self.triple.topology_version_id,
            mapping_version_id=version_id,
            model_set_id=self.triple.model_set_id,
        )

    def rollback(self, kind: str, to_version_id: str) -> str:
        return f"{kind}-rollback"


def _service() -> _RecordingVersionService:
    return _RecordingVersionService()


def test_workflow_promotes_after_n_events() -> None:
    svc = _service()
    workflow = ConfigurationWorkflow(svc, clock=lambda: T0)
    record = workflow.record(_event())
    workflow.begin(record.event_id, edge_ids=["e1"], mapping_edge_ids=["m1"])
    assert svc.suspected  # mark_suspected was called

    for i in range(CONFIRMED_EVENTS):
        result = workflow.observe_confirming_event(
            record.event_id, source=f"source-{i}"
        )
    assert result.status == "confirmed"
    assert svc.current().topology_version_id != "topo-1"
    assert svc.current().model_set_id == "mset-1"


def test_workflow_records_inferred_change_as_suspected() -> None:
    svc = _service()
    workflow = ConfigurationWorkflow(svc, clock=lambda: T0)
    change = topology_change.InferredChange(
        ConfigurationChangeKind.CT_DIRECTION_CHANGE, "ct_a"
    )
    record = workflow.record_inferred(change)
    assert record.event.detection == ChangeDetection.INFERRED
    assert record.event.status == ChangeStatus.SUSPECTED
    assert svc.events  # recorded through the VersionService protocol


def test_workflow_rejects_expired_and_restores() -> None:
    svc = _service()
    clock = {"now": T0}
    workflow = ConfigurationWorkflow(
        svc, clock=lambda: clock["now"], reject_after=timedelta(days=7)
    )
    record = workflow.record(_event())
    workflow.begin(record.event_id, mapping_edge_ids=["m1"])

    clock["now"] = T0 + timedelta(days=8)
    result = workflow.reject_expired(record.event_id)
    assert result.status == "rejected"


def test_workflow_does_not_reject_before_timeout() -> None:
    svc = _service()
    workflow = ConfigurationWorkflow(svc, clock=lambda: T0)
    record = workflow.record(_event())
    workflow.begin(record.event_id)
    result = workflow.reject_expired(record.event_id)
    assert result.status == "suspected"


# --------------------------------------------------------------------------- #
# 3. Power models are never touched.
# --------------------------------------------------------------------------- #


def test_model_write_api_is_denied() -> None:
    workflow = ConfigurationWorkflow(_service(), clock=lambda: T0)
    assert isinstance(workflow, ConfigurationWorkflow)

    class GuardedModelRepo:
        def save_model_version(self, *args: Any, **kwargs: Any) -> None:
            pytest.fail("T7 must never call a power-model write API")

        def append_version(self, *args: Any, **kwargs: Any) -> None:
            pytest.fail("T7 must never call a power-model write API")

    with pytest.raises(NoModelWriteError):
        assert_no_model_write(GuardedModelRepo())


def test_workflow_promotion_leaves_model_set_id_unchanged() -> None:
    svc = _service()
    workflow = ConfigurationWorkflow(svc, clock=lambda: T0)
    record = workflow.record(_event())
    before = svc.current().model_set_id
    for i in range(CONFIRMED_EVENTS):
        workflow.observe_confirming_event(record.event_id, source=f"src-{i}")
    assert svc.current().model_set_id == before


# --------------------------------------------------------------------------- #
# 4. DEC-10: user-declared CONFIRMED edge can move to CONFLICTED.
# --------------------------------------------------------------------------- #


def _user_edge() -> TopologyEdgeV3:
    return TopologyEdgeV3(
        edge_id="te-user",
        relation=RelationType.CONTAINS,
        source_endpoint_id="main",
        target_endpoint_id="sub",
        lifecycle=EdgeLifecycleV3.CONFIRMED,
        declared_by="user",
        beta=1.0,
        beta_basis="unity",
        evidence=TopologyEvidence(support=8, independent_causes=2),
        valid_from=T0,
        pinned=False,
    )


def test_user_declared_conflicted_edge_downgrades_not_deleted() -> None:
    view = TopologyInferenceView(
        edges=(_user_edge(),),
        absolute={"main": [1000.0] * 30, "sub": [500.0] * 30},
    )
    # Seven independent events where the child moves alone: conflict ratio 1.0.
    for i in range(7):
        view = view.observe_group(
            DeltaObservation(
                group_id=f"g{i}",
                deltas={"main": (0.0, 2.0), "sub": (25.0, 2.0)},
                cause_id=f"cause-{i % 2}",
            )
        )
    edge = view.edge("main", "sub")
    assert edge is not None
    assert edge.lifecycle == EdgeLifecycleV3.CONFLICTED
    # The edge row is retained, not deleted.
    assert any(e.edge_id == "te-user" for e in view.edges)


class _ParentCheckingVersionService(_RecordingVersionService):
    """Promotion refuses a candidate proposed on a since-superseded parent."""

    def __init__(self) -> None:
        super().__init__()
        self.parent_of: dict[str, str] = {}

    def propose_topology(self, edges: Any, change_event_id: Any) -> str:
        version_id = super().propose_topology(edges, change_event_id)
        self.parent_of[version_id] = self.triple.topology_version_id
        return version_id

    def promote_topology(self, version_id: str, reason: str) -> None:
        from wattmosaic_engine.versioning.service import VersionConflictError

        expected, actual = self.parent_of[version_id], self.triple.topology_version_id
        if expected != actual:
            raise VersionConflictError("topology", version_id, expected, actual)
        super().promote_topology(version_id, reason)


def test_workflow_rebases_a_candidate_whose_parent_moved_on() -> None:
    # Live dev server: T5 promoted newer topologies between a suspected change's
    # first corroboration (candidate proposed) and its last (promote), and every
    # detector cycle failed with "promoted on stale parent".
    svc = _ParentCheckingVersionService()
    workflow = ConfigurationWorkflow(svc, clock=lambda: T0)
    record = workflow.record(_event())
    workflow.begin(record.event_id, edge_ids=["e1"], mapping_edge_ids=["m1"])
    workflow.observe_confirming_event(record.event_id, source="source-0")
    stale = record.candidate_topology_version_id
    # T5 promotes its own version meanwhile.
    svc.promote_topology(svc.propose_topology([], "t5"), "T5 confirmed edge")
    for i in range(1, CONFIRMED_EVENTS):
        result = workflow.observe_confirming_event(record.event_id, source=f"source-{i}")
    assert result.status == "confirmed"
    assert result.candidate_topology_version_id != stale
    assert svc.current().topology_version_id == result.candidate_topology_version_id


def test_a_suspicion_below_the_threshold_leaves_no_orphan_candidate_version() -> None:
    # Live: a history replay's promotion re-ran the detectors; the first
    # corroboration proposed a candidate topology that was never promoted.
    svc = _RecordingVersionService()
    workflow = ConfigurationWorkflow(svc, clock=lambda: T0)
    record = workflow.record(_event())
    workflow.begin(record.event_id, edge_ids=["e1"], mapping_edge_ids=["m1"])
    for i in range(CONFIRMED_EVENTS - 1):
        workflow.observe_confirming_event(record.event_id, source=f"source-{i}")
    assert record.candidate_topology_version_id is None
    assert record.candidate_mapping_version_id is None
    assert (svc._topo_n, svc._map_n) == (1, 1)  # noqa: SLF001 - nothing proposed
    result = workflow.observe_confirming_event(record.event_id, source="last")
    assert result.status == "confirmed"
    assert (svc._topo_n, svc._map_n) == (2, 2)  # noqa: SLF001 - one proposal each, promoted
    assert svc.current().topology_version_id == result.candidate_topology_version_id
