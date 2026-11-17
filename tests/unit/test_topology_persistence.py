"""Round-trip persistence for the topology hierarchy fields.

The panel identity is the only structural fact that makes the Topology tab a
hierarchy rather than a list.  It is therefore worth a dedicated regression
test, because the failure mode is silent: ``save_topology`` writes an explicit
column list and ``load_topology`` rebuilds channels by attribute name, so a
field that has no column disappears on every write with no error anywhere.

That is not hypothetical.  ``alias_of_ct_id`` and ``measurement_multiplier``
were carried by the contract for a whole release before migration 6 gave them
columns; before that a user's alias declaration never survived a restart.  The
``panel_id``/``panel_name`` pair is the same shape of defect, so it is pinned
here rather than discovered later in production.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
from wattmosaic_engine.contracts import (
    CTChannel,
    CTType,
    DeviceTopology,
    EdgeLifecycle,
    Phase,
    RelationKind,
    Topology,
    TopologyRelation,
    TopologyStatus,
)
from wattmosaic_engine.electrical.topology import (
    EdgeView,
    diff_topologies,
    make_version,
)
from wattmosaic_engine.storage.database import Database
from wattmosaic_engine.storage.migrations import LATEST_VERSION


def _channel(ct_id: str, **kw) -> CTChannel:
    return CTChannel(
        ct_id=ct_id,
        entity_id=f"sensor.{ct_id}_power",
        name=ct_id,
        ct_type=kw.pop("ct_type", CTType.BRANCH),
        phase=kw.pop("phase", Phase.L1),
        **kw,
    )


def _topology() -> Topology:
    return Topology(
        status=TopologyStatus.CANDIDATE,
        channels=[
            _channel(
                "main_panel_l1",
                ct_type=CTType.MAIN,
                phase=Phase.L1,
                panel_id="main_panel",
                panel_name="Main Panel",
            ),
            _channel(
                "sub_panel_l1",
                ct_type=CTType.MAIN,
                phase=Phase.L1,
                parent_ct_id="main_panel_l1",
                panel_id="sub_panel",
                panel_name="Sub Panel",
            ),
            _channel(
                "upper_humidifier_ct",
                parent_ct_id="sub_panel_l1",
                panel_id="sub_panel",
                panel_name="Sub Panel",
            ),
        ],
        devices=[
            DeviceTopology(
                device_id="dev_upper_humidifier",
                name="Upper Humidifier",
                branch_ct_ids=["upper_humidifier_ct"],
            )
        ],
    )


def test_panel_identity_survives_a_save_and_reload(tmp_path):
    """The whole point: a restart must not flatten the tree to one board."""
    db = Database(tmp_path / "wm.db")
    db.save_topology(_topology())

    reloaded = db.load_topology()

    assert reloaded is not None
    channels = {c.ct_id: c for c in reloaded.channels}
    assert channels["main_panel_l1"].panel_id == "main_panel"
    assert channels["main_panel_l1"].panel_name == "Main Panel"
    assert channels["sub_panel_l1"].panel_id == "sub_panel"
    assert channels["upper_humidifier_ct"].panel_id == "sub_panel"
    # Declared containment must survive alongside it.
    assert channels["sub_panel_l1"].parent_ct_id == "main_panel_l1"


def test_reloaded_topology_still_renders_two_panels(tmp_path):
    """A persisted topology must produce the same board structure as in memory."""
    from wattmosaic_engine.electrical.panel_tree import build_panel_tree

    before = build_panel_tree(_topology())

    db = Database(tmp_path / "wm.db")
    db.save_topology(_topology())
    reloaded = db.load_topology()
    assert reloaded is not None
    after = build_panel_tree(reloaded)

    assert after["stats"]["panels"] == before["stats"]["panels"] == 2
    assert after["stats"]["circuits"] == before["stats"]["circuits"]
    assert after["stats"]["devices"] == before["stats"]["devices"] == 1
    # No "we had to guess an implicit panel" note: the identity came back.
    assert not any("no panel declared" in note for note in after.get("notes", []))


def test_topology_without_panels_still_round_trips(tmp_path):
    """An older topology that never declared a panel must not become invalid."""
    legacy = Topology(
        status=TopologyStatus.CANDIDATE,
        channels=[_channel("only_ct")],
        devices=[DeviceTopology(device_id="d1", branch_ct_ids=["only_ct"])],
    )
    db = Database(tmp_path / "wm.db")
    db.save_topology(legacy)

    reloaded = db.load_topology()
    assert reloaded is not None
    assert reloaded.channels[0].panel_id is None
    assert reloaded.channels[0].panel_name is None


def test_panel_columns_exist_at_the_latest_schema_version(tmp_path):
    """The columns must be part of the schema, not just of the Python model."""
    db = Database(tmp_path / "wm.db")
    columns = {
        row["name"] for row in db._conn.execute("PRAGMA table_info(ct_channels)").fetchall()
    }
    assert {"panel_id", "panel_name"} <= columns
    assert LATEST_VERSION >= 8


def _core_with_power(tmp_path):
    """An EngineCore in the post-restart state: topology loaded, no inference yet."""
    from datetime import UTC, datetime

    from wattmosaic_engine.config.settings import EngineConfig
    from wattmosaic_engine.contracts import InferenceResult
    from wattmosaic_engine.main import EngineCore
    from wattmosaic_engine.storage.database import Database

    database = Database(tmp_path / "wm.db")
    database.save_topology(_topology())
    database.insert_inference_results(
        [
            InferenceResult(
                device_id="dev_upper_humidifier",
                timestamp=datetime.now(UTC),
                power_w=42.5,
                energy_kwh=0.0,
                source="direct_measured",
                confidence=0.9,
                quality_status="good",
            )
        ]
    )
    config = EngineConfig(database_path=str(tmp_path / "wm.db"), ha_token="not-used")
    return EngineCore(config, database)


def test_tree_reports_stored_power_before_first_inference(tmp_path):
    """A restart must not show 0 W on the tree while /devices shows real power.

    ``latest_results`` is only populated by an inference cycle, so for the first
    seconds after a restart it is empty.  A tree that read only the in-memory map
    reported every device as 0 W, which is a *negative* claim about the house
    rather than an absence of data -- and it disagreed with the Devices tab on
    the same install at the same moment.
    """
    core = _core_with_power(tmp_path)
    assert core.latest_results == {}  # the state this test exists to cover

    tree = core.panel_tree()
    powers = {}

    def walk(node):
        if node["kind"] == "device":
            powers[node["device_id"]] = node["latest_power_w"]
        for child in node.get("children", []):
            walk(child)

    for root in tree["roots"]:
        walk(root)

    assert powers["dev_upper_humidifier"] == pytest.approx(42.5)

    # And the two endpoints must agree, because a user can see both.
    by_device = {d["device_id"]: d for d in core.devices()}
    stored = by_device["dev_upper_humidifier"]["latest_result"]
    assert stored is not None
    assert stored["power_w"] == pytest.approx(powers["dev_upper_humidifier"])


def test_tree_prefers_a_fresh_result_over_the_stored_one(tmp_path):
    """The in-memory map still wins, so a newer result is never shadowed."""
    from datetime import UTC, datetime

    from wattmosaic_engine.contracts import InferenceResult

    core = _core_with_power(tmp_path)
    core.latest_results["dev_upper_humidifier"] = InferenceResult(
        device_id="dev_upper_humidifier",
        timestamp=datetime.now(UTC),
        power_w=99.0,
        energy_kwh=0.0,
        source="direct_measured",
        confidence=0.9,
        quality_status="good",
    )

    tree = core.panel_tree()
    powers = {}

    def walk(node):
        if node["kind"] == "device":
            powers[node["device_id"]] = node["latest_power_w"]
        for child in node.get("children", []):
            walk(child)

    for root in tree["roots"]:
        walk(root)

    assert powers["dev_upper_humidifier"] == pytest.approx(99.0)


def test_panel_columns_are_added_to_a_database_already_at_schema_7(tmp_path):
    """A duplicate migration number would silently skip the columns.

    ``apply_migrations`` skips every migration whose ``version`` is not greater
    than ``user_version``.  When this change first landed it was numbered 7 --
    the same number as ``reconciliation_persisted_fields`` on the main line --
    so any install that had already run that migration sat at ``user_version=7``
    and would never receive ``panel_id``/``panel_name``.  Reads would keep
    working (``load_topology`` tolerates the missing columns) while every write
    failed with "no column named panel_id", which is the worst shape of bug: it
    boots, it renders, and it silently refuses to save anything.

    This builds the exact state such an install is in and asserts that opening
    it completes the migration set.
    """
    from wattmosaic_engine.storage.migrations import apply_migrations, current_version

    path = tmp_path / "prod-like.db"
    db = Database(path)
    db.close()

    # Rewind to the schema-7 shape: drop the two columns and stamp version 7.
    import sqlite3

    raw = sqlite3.connect(path)
    try:
        raw.execute("ALTER TABLE ct_channels DROP COLUMN panel_id")
        raw.execute("ALTER TABLE ct_channels DROP COLUMN panel_name")
        raw.execute("PRAGMA user_version = 7")
        raw.commit()

        assert apply_migrations(raw) > 7
        columns = {row[1] for row in raw.execute("PRAGMA table_info(ct_channels)")}
        assert {"panel_id", "panel_name"} <= columns
        assert current_version(raw) >= 8
    finally:
        raw.close()


def test_saving_a_topology_works_on_a_database_missing_the_panel_columns(tmp_path):
    """Belt and braces: the write path must not depend on the columns existing.

    ``save_topology`` writes an explicit column list.  If that list is ever
    ahead of the schema again, this test names the failure instead of leaving a
    user to discover that their topology silently stopped saving.
    """
    import sqlite3

    from wattmosaic_engine.storage.migrations import apply_migrations

    path = tmp_path / "prod-like.db"
    db = Database(path)
    db.close()

    raw = sqlite3.connect(path)
    try:
        raw.execute("ALTER TABLE ct_channels DROP COLUMN panel_id")
        raw.execute("ALTER TABLE ct_channels DROP COLUMN panel_name")
        raw.execute("PRAGMA user_version = 7")
        raw.commit()
        apply_migrations(raw)
    finally:
        raw.close()

    reopened = Database(path)
    try:
        reopened.save_topology(_topology())
        reloaded = reopened.load_topology()
        assert reloaded is not None
        assert reloaded.channels[0].panel_id == "main_panel"
    finally:
        reopened.close()


# --------------------------------------------------------------------------- #
# TASK-TOPOLOGY-003 -- the versioned topology object and its deterministic diff
# --------------------------------------------------------------------------- #
#
# ``TopologyVersionRecord`` is the computed version of the API contract's
# ``TopologyVersion`` (``09 §7.2``): identity, time, status, parent and the diff
# against the parent.  The store is ``storage``'s, so these tests exercise the
# computation only -- the same function a live pass, a stored read and a route
# all call.  The wire shape of the diff is fixed by ``09 §8.1``.


def _relation(
    relation_id: str,
    *,
    kind: RelationKind = RelationKind.DEVICE_CT_ASSOCIATION,
    subject_id: str = "dev_a",
    object_id: str = "ct_a",
    status: TopologyStatus = TopologyStatus.CANDIDATE,
    lifecycle: EdgeLifecycle = EdgeLifecycle.CANDIDATE,
    confidence: float | None = None,
    evidence: dict | None = None,
    requires_confirmation: bool = True,
    declared_by: str = "inference",
) -> TopologyRelation:
    """One relation; only the fields a diff compares are ever set explicitly."""
    return TopologyRelation(
        relation_id=relation_id,
        kind=kind,
        subject_id=subject_id,
        object_id=object_id,
        status=status,
        lifecycle=lifecycle,
        confidence=confidence,
        evidence=evidence or {},
        requires_confirmation=requires_confirmation,
        declared_by=declared_by,
    )


def _topology_with(*relations: TopologyRelation) -> Topology:
    """A minimal topology carrying only the typed edges under test."""
    return Topology(
        status=TopologyStatus.CANDIDATE,
        channels=[_channel("ct_a"), _channel("ct_b")],
        devices=[
            DeviceTopology(device_id="dev_a", branch_ct_ids=["ct_a"]),
            DeviceTopology(device_id="dev_b", branch_ct_ids=["ct_b"]),
        ],
        relations=list(relations),
    )


#: The exact key set ``09 §8.1`` gives the ``TopologyDiff`` wire shape, minus the
#: two endpoint ``TopologyVersion`` objects a route supplies from its own lookups.
DIFF_WIRE_KEYS = {
    "added_edges",
    "removed_edges",
    "changed_edges",
    "changed_fields",
    "added_count",
    "removed_count",
    "changed_count",
}

#: The fields of ``TopologyEdge`` (``09 §7.1``) a diff projection carries.
VIEW_WIRE_KEYS = {
    "edge_id",
    "kind",
    "subject_id",
    "object_id",
    "status",
    "lifecycle",
    "confidence",
    "evidence",
    "requires_confirmation",
    "declared_by",
}


def _stable_json_of(value: Any) -> str:
    """Canonical JSON of a version record, so two reads can be compared byte-wise."""
    return json.dumps(_plain(value), sort_keys=True, separators=(",", ":"), default=str)


def _plain(value: Any) -> Any:
    """Reduce a dataclass tree to plain JSON-safe containers."""
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _plain(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _view_wire(view: EdgeView) -> dict:
    """The wire form of one edge, restricted to the §7.1 fields a diff carries."""
    return {
        "edge_id": view.id,
        "kind": view.kind,
        "subject_id": view.subject_id,
        "object_id": view.object_id,
        "status": view.status,
        "lifecycle": view.lifecycle,
        "confidence": view.confidence,
        "evidence": dict(view.evidence),
        "requires_confirmation": view.requires_confirmation,
        "declared_by": view.declared_by,
    }


def test_topology_version_diff() -> None:
    """The diff reports the added, removed and changed edges and the counts.

    The two versions differ in one edge of each class: one removed, one added and
    one present in both whose *content* moved.  A re-stamped ``relation_id`` of
    an unchanged assertion is deliberately not a change, and an edge whose only
    difference is the clock is not either.
    """
    old = _topology_with(
        _relation("dca_dev_a|ct_a", confidence=0.4, declared_by="discovery"),
        _relation(
            "dca_dev_b|ct_b",
            subject_id="dev_b",
            object_id="ct_b",
            confidence=0.7,
            declared_by="inference",
        ),
        _relation(
            "dca_dev_c|ct_c",
            subject_id="dev_c",
            object_id="ct_c",
            confidence=0.5,
        ),
    )
    new = _topology_with(
        # Same endpoints, same content: present in both, unchanged.  A fresh
        # relation_id must not read as a change, so every compared field is
        # copied verbatim from the predecessor.
        _relation("dca_dev_a|ct_a_v2", confidence=0.4, declared_by="discovery"),
        # Same endpoints, moved content: the changed edge.
        _relation(
            "dca_dev_b|ct_b",
            subject_id="dev_b",
            object_id="ct_b",
            confidence=0.9,
            declared_by="inference",
        ),
        # Present only in the new version: added.
        _relation(
            "dca_dev_d|ct_d",
            subject_id="dev_d",
            object_id="ct_d",
            confidence=0.6,
        ),
        # ``dev_c``/``ct_c`` is gone: removed.
    )

    diff = diff_topologies(old, new)

    assert diff.added_count == 1
    assert diff.removed_count == 1
    assert diff.changed_count == 1
    assert len(diff.added) == 1
    assert len(diff.removed) == 1
    assert len(diff.changed) == 1

    added = diff.added[0]
    assert (added.kind, added.subject_id, added.object_id) == (
        RelationKind.DEVICE_CT_ASSOCIATION.value,
        "dev_d",
        "ct_d",
    )
    assert added.id == "dca_dev_d|ct_d"

    removed = diff.removed[0]
    assert (removed.kind, removed.subject_id, removed.object_id) == (
        RelationKind.DEVICE_CT_ASSOCIATION.value,
        "dev_c",
        "ct_c",
    )

    changed = diff.changed[0]
    assert (changed.kind, changed.subject_id, changed.object_id) == (
        RelationKind.DEVICE_CT_ASSOCIATION.value,
        "dev_b",
        "ct_b",
    )
    assert changed.edge_id == "dca_dev_b|ct_b"
    assert changed.changed_fields == ("confidence",)

    # The union of differing field names, sorted, exactly as the wire shape says.
    assert diff.changed_fields == ("confidence",)

    # And the serialised form is the contract's key set, nothing more.
    wire = diff.as_dict()
    assert set(wire) == DIFF_WIRE_KEYS
    assert wire["added_count"] == 1
    assert wire["removed_count"] == 1
    assert wire["changed_count"] == 1
    assert wire["changed_fields"] == ["confidence"]
    assert wire["added_edges"][0]["edge_id"] == "dca_dev_d|ct_d"
    assert wire["removed_edges"][0]["edge_id"] == "dca_dev_c|ct_c"
    assert wire["changed_edges"][0]["changed_fields"] == ["confidence"]


def test_previous_payload_is_byte_identical_after_advance() -> None:
    """Creating a second version must not alter the first version's payload.

    ``make_version`` derives the diff from the predecessor it is handed.  The
    guarantee under test is that the predecessor record is never mutated: its
    edge set, its identity and its own diff are the same objects afterwards, so
    a stored row read back is byte-identical to the one written.
    """
    old = _topology_with(_relation("dca_dev_a|ct_a", confidence=0.4))
    new = _topology_with(
        _relation("dca_dev_a|ct_a", confidence=0.9),
        _relation(
            "dca_dev_b|ct_b",
            subject_id="dev_b",
            object_id="ct_b",
            confidence=0.5,
        ),
    )

    first = make_version(
        version_number=1,
        topology=old,
        created_at=datetime(2026, 11, 17, tzinfo=UTC),
        created_by="scheduler",
    )
    first_payload = first.as_dict() if hasattr(first, "as_dict") else None
    first_edges = first.edges
    first_diff = first.diff
    first_json = _stable_json_of(first)

    second = make_version(
        version_number=2,
        topology=new,
        parent=first,
        parent_topology=old,
        created_at=datetime(2026, 11, 17, 0, 1, tzinfo=UTC),
        created_by="scheduler",
    )

    # The predecessor's stored payload is untouched by the advance.
    assert first.version_id == "tv-00000001"
    assert first.parent_version_id is None
    assert first.diff is None
    assert first.edges == first_edges
    assert first.diff is first_diff
    assert _stable_json_of(first) == first_json
    assert first_payload is None or first.as_dict() == first_payload

    # The successor descends from it and carries the diff against it.
    assert second.version_id == "tv-00000002"
    assert second.parent_version_id == first.version_id
    assert second.diff is not None
    assert second.diff.added_count == 1
    assert second.diff.changed_count == 1
    assert second.diff.removed_count == 0

    # And advancing a third time still leaves the second intact: this is an
    # append-only chain, not a rewrite of the previous row.
    third = make_version(
        version_number=3,
        topology=new,
        parent=second,
        parent_topology=new,
        created_at=datetime(2026, 11, 17, 0, 2, tzinfo=UTC),
        created_by="scheduler",
    )
    assert second.diff is not None
    assert second.diff.as_dict() == second.diff.as_dict()
    assert third.diff is not None
    assert third.diff.is_empty
    assert _stable_json_of(first) == first_json
    assert _stable_json_of(second) == _stable_json_of(second)


def test_edge_view_covers_the_contract_endpoint_fields() -> None:
    """``EdgeView``/``TopologyDiff`` expose what ``09 §8.1`` specifies.

    ``09 §8.1`` types the three edge lists as ``list[TopologyEdge]`` and
    ``TopologyEdge`` (``09 §7.1``) carries the endpoint and lifecycle fields.
    The response-only projection is assembled at the API boundary, so the
    engine-side objects must carry every field that shape names -- otherwise the
    route would have to invent one.
    """
    relation = _relation(
        "dca_dev_a|ct_a",
        status=TopologyStatus.CONFIRMED,
        lifecycle=EdgeLifecycle.CONFIRMED,
        confidence=0.75,
        evidence={"events": 3},
        requires_confirmation=False,
        declared_by="user",
    )
    view = EdgeView.from_relation(relation)

    # Endpoint identity and lifecycle, the fields ``TopologyEdge`` names.  The
    # engine-side attribute is ``id``; the wire key is ``edge_id`` (see below).
    for name in (
        "id",
        "kind",
        "subject_id",
        "object_id",
        "status",
        "lifecycle",
        "confidence",
        "evidence",
        "requires_confirmation",
        "declared_by",
    ):
        assert hasattr(view, name), name
    assert view.id == "dca_dev_a|ct_a"
    assert view.kind == RelationKind.DEVICE_CT_ASSOCIATION.value
    assert view.subject_id == "dev_a"
    assert view.object_id == "ct_a"
    assert view.status == "confirmed"
    assert view.lifecycle == "confirmed"
    assert view.confidence == 0.75
    assert view.evidence == {"events": 3}
    assert view.requires_confirmation is False
    assert view.declared_by == "user"

    # The wire projection of a diff carries exactly the §8.1 key set.
    diff = diff_topologies(_topology_with(), _topology_with(relation))
    assert set(diff.as_dict()) == DIFF_WIRE_KEYS
    assert set(_view_wire(diff.added[0])) == VIEW_WIRE_KEYS
    assert _view_wire(diff.added[0]) == {
        "edge_id": "dca_dev_a|ct_a",
        "kind": RelationKind.DEVICE_CT_ASSOCIATION.value,
        "subject_id": "dev_a",
        "object_id": "ct_a",
        "status": "confirmed",
        "lifecycle": "confirmed",
        "confidence": 0.75,
        "evidence": {"events": 3},
        "requires_confirmation": False,
        "declared_by": "user",
    }

    # The version object itself carries the §7.2 fields the task requires:
    # identifier, time, status, parent and diff.
    version = make_version(
        version_number=7,
        topology=_topology_with(relation),
        created_at=datetime(2026, 11, 17, tzinfo=UTC),
        created_by="scheduler",
    )
    for name in (
        "version_id",
        "version_number",
        "parent_version_id",
        "status",
        "created_at",
        "created_by",
        "change_event_id",
        "rollback_of",
        "summary",
        "edges",
        "diff",
    ):
        assert hasattr(version, name), name
    assert version.version_id == "tv-00000007"
    assert version.version_number == 7
    assert version.parent_version_id is None
    assert version.status == TopologyStatus.CANDIDATE.value
    assert version.edge_count == 1
    assert version.edge_ids == ("dca_dev_a|ct_a",)
