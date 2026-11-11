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

import pytest
from wattmosaic_engine.contracts import (
    CTChannel,
    CTType,
    DeviceTopology,
    Phase,
    Topology,
    TopologyStatus,
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
                dedicated_ct_ids=["upper_humidifier_ct"],
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
        devices=[DeviceTopology(device_id="d1", dedicated_ct_ids=["only_ct"])],
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
