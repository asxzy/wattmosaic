"""The schema-11 migration seam: old data must survive the vocabulary change.

**Why this file exists**

``CTType.DEDICATED_DEVICE`` and ``MonitoringLevel.DEDICATED`` are retired.  If
the stored rows still carry the old values when the enum members disappear, three
independent load paths break, and two of them break *silently*:

* ``load_topology`` validates ``ct_type`` through Pydantic, so every startup
  raises;
* ``load_device_configurations`` swallows a validation failure per row
  (``except Exception: continue``), so a stored ``"monitoring": "dedicated"``
  deletes the user's whole configuration -- state entities, expected watts,
  light and controller maps -- with no error anywhere;
* ``InferenceResult.monitoring_level`` is an enum, so historical rows fail to
  validate when they are read back.

A device's ids must also **move** rather than disappear.  ``attribution_ct_ids``
only consults ``dedicated_ct_ids``; once the channel type is ``branch`` that
lookup finds nothing and attribution falls through to
``most_specific_branch_group``, which reads ``branch_ct_ids`` only.

**Why raw ``sqlite3``**

``Database.__init__`` runs the migrations, so it cannot be used to build a
pre-migration fixture: the database would already be at 11 before the test could
write a schema-8 row.  The fixture is therefore built on a plain connection and
stopped at ``user_version = 8``.

**Why the callback is invoked directly**

``apply_migrations`` skips any migration whose version is already applied
(``version <= start``), so running the whole thing twice is a no-op no matter
what the callback does -- it cannot detect a double-add bug.  Idempotency is
tested by calling the version-11 ``apply=`` callback twice on one connection.
"""

from __future__ import annotations

import ast
import asyncio
import importlib
import importlib.util
import json
import sqlite3
import sys
import types
from pathlib import Path
from typing import Any

import pytest
from wattmosaic_engine.contracts import (
    CTType,
    DeviceConfiguration,
    InferenceResult,
    MonitoringLevel,
    Topology,
)
from wattmosaic_engine.electrical.topology import build_graph
from wattmosaic_engine.storage.database import Database
from wattmosaic_engine.storage.migrations import (
    LATEST_VERSION,
    MIGRATIONS,
    apply_migrations,
    current_version,
)

#: The schema version this wave migrates *from*.
PRE_MIGRATION_VERSION = 10

#: Migrations 9 and 10 (audit tables, isolation verdicts) precede the retirement.
_V11 = next((m for m in MIGRATIONS if m.version == 11), None)


def _v11() -> Any:
    """The version-11 migration, with a failure that names the defect.

    Resolved lazily so that on a tree without the migration every test below
    fails on its own assertion instead of the module failing to import.
    """
    assert _V11 is not None, "there is no schema-11 migration; the fixture cannot migrate"
    return _V11

NOW = "2026-11-01T00:00:00+00:00"


# --------------------------------------------------------------------------- #
# A schema-8 database, built without ``Database``
# --------------------------------------------------------------------------- #


def _schema(conn: sqlite3.Connection, upto: int = PRE_MIGRATION_VERSION) -> None:
    """Bring a bare connection to ``upto`` using the shipped migrations.

    Reuses the production statements so the fixture cannot drift from the real
    schema; the point of the raw connection is that it stops *before* version 11,
    which ``Database.__init__`` would immediately apply.
    """
    for migration in MIGRATIONS:
        if migration.version > upto:
            break
        for statement in migration.statements:
            conn.execute(statement)
        if migration.apply is not None:
            migration.apply(conn)
    conn.execute(f"PRAGMA user_version = {upto}")


def _channel(
    conn: sqlite3.Connection,
    ct_id: str,
    ct_type: str,
    *,
    device_id: str | None = None,
    parent_ct_id: str | None = None,
    paired_ct_id: str | None = None,
    enabled: int = 1,
    panel_id: str | None = "main_panel",
) -> None:
    conn.execute(
        """INSERT INTO ct_channels(ct_id, entity_id, name, ct_type, phase, parent_ct_id,
        polarity, device_id, paired_ct_id, enabled, reliability, topology_status,
        metadata_json, panel_id, panel_name, updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            ct_id, f"sensor.{ct_id}_power", ct_id, ct_type, "l1", parent_ct_id,
            "normal", device_id, paired_ct_id, enabled, 1.0, "confirmed", "{}",
            panel_id, "Main Panel", NOW,
        ),
    )


def _device(
    conn: sqlite3.Connection,
    device_id: str,
    *,
    dedicated: list[str],
    branch: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        """INSERT INTO devices(device_id, name, dedicated_ct_ids_json, branch_ct_ids_json,
        phase, double_pole, parent_device_id, topology_status, enabled, metadata_json, updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            device_id, device_id, json.dumps(dedicated), json.dumps(branch or []),
            "l1", 0, None, "confirmed", 1, json.dumps(metadata or {}), NOW,
        ),
    )


def _configuration(
    conn: sqlite3.Connection, device_id: str, payload: dict[str, Any]
) -> None:
    conn.execute(
        "INSERT INTO device_configurations(device_id, payload_json, updated_at) VALUES(?,?,?)",
        (device_id, json.dumps(payload), NOW),
    )


def _inference(conn: sqlite3.Connection, device_id: str, monitoring: str) -> None:
    conn.execute(
        """INSERT INTO inference_results(device_id, timestamp, power_w, source, confidence,
        quality_status, monitoring_level, confidence_tier, power_behavior, tier_reasons_json)
        VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (device_id, NOW, 1200.0, "inferred", 0.7, "good", monitoring,
         "tier_2_constrained", "fixed", "[]"),
    )


def _build_schema8(path: Path) -> Path:
    """One main channel, a two-leg device, and the rows that used to break.

    ``hot_tub`` is the realistic shape: a double-pole load measured by two
    ``dedicated_device`` CTs that also carry a ``projected_ct_ids`` entry (put
    there by ``project_configuration``), a configuration whose
    ``monitored_by_ct_ids`` names one of them, and a historical inference row at
    ``monitoring_level = "dedicated"``.
    """
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        _schema(conn)
        _channel(conn, "main_l1", "main")
        # The two legs of one double-pole load: paired with each other, exactly
        # as the wizard records a 240 V appliance.
        _channel(
            conn, "tub_l1", "dedicated_device",
            device_id="hot_tub", parent_ct_id="main_l1", paired_ct_id="tub_l2",
        )
        _channel(
            conn, "tub_l2", "dedicated_device",
            device_id="hot_tub", parent_ct_id="main_l1", paired_ct_id="tub_l1",
        )
        _channel(conn, "lights_ct", "branch", parent_ct_id="main_l1")
        _device(
            conn,
            "hot_tub",
            dedicated=["tub_l1", "tub_l2"],
            metadata={"projected_ct_ids": ["tub_l1", "tub_l2"], "materialised_from_config": "user"},
        )
        _configuration(
            conn,
            "hot_tub",
            {
                "device_id": "hot_tub",
                "monitoring": "dedicated",
                "monitored_by_ct_ids": ["tub_l1"],
                "state_entity_ids": ["binary_sensor.hot_tub_running"],
                "expected": {"on_w": 2200.0, "idle_w": 40.0},
                "configured_by": "user",
            },
        )
        _inference(conn, "hot_tub", "dedicated")
        conn.commit()
    finally:
        conn.close()
    return path


@pytest.fixture()
def schema8_db(tmp_path: Path) -> Path:
    return _build_schema8(tmp_path / "schema8.db")


def _rows(path: Path) -> dict[str, Any]:
    """Read the migrated tables back on a plain connection."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return {
            "channels": {r["ct_id"]: dict(r) for r in conn.execute("SELECT * FROM ct_channels")},
            "devices": {r["device_id"]: dict(r) for r in conn.execute("SELECT * FROM devices")},
            "configurations": {
                r["device_id"]: dict(r)
                for r in conn.execute("SELECT * FROM device_configurations")
            },
            "inference": [dict(r) for r in conn.execute("SELECT * FROM inference_results")],
            "version": current_version(conn),
        }
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# The migration itself
# --------------------------------------------------------------------------- #


def test_the_fixture_really_is_a_pre_migration_database(schema8_db: Path) -> None:
    """Guard the fixture: if it were already at 11 the other tests would be vacuous."""
    rows = _rows(schema8_db)
    assert rows["version"] == PRE_MIGRATION_VERSION
    assert LATEST_VERSION == PRE_MIGRATION_VERSION + 1
    assert rows["channels"]["tub_l1"]["ct_type"] == "dedicated_device"


def test_v11_demotes_dedicated_channels(schema8_db: Path) -> None:
    _rows_after = Database(schema8_db)
    _rows_after.close()

    rows = _rows(schema8_db)
    assert rows["version"] == 11
    assert rows["channels"]["tub_l1"]["ct_type"] == "branch"
    assert rows["channels"]["tub_l2"]["ct_type"] == "branch"
    # Unrelated roles are untouched.
    assert rows["channels"]["main_l1"]["ct_type"] == "main"
    assert rows["channels"]["lights_ct"]["ct_type"] == "branch"


def test_v11_moves_device_ids(schema8_db: Path) -> None:
    Database(schema8_db).close()

    device = _rows(schema8_db)["devices"]["hot_tub"]
    assert json.loads(device["dedicated_ct_ids_json"]) == []
    assert sorted(json.loads(device["branch_ct_ids_json"])) == ["tub_l1", "tub_l2"]


def test_v11_preserves_other_columns(schema8_db: Path) -> None:
    """The migration owns two JSON columns, not the row."""
    conn = sqlite3.connect(schema8_db)
    try:
        conn.execute(
            "UPDATE ct_channels SET polarity='inverted', phase='l2', paired_ct_id='tub_l2' "
            "WHERE ct_id='tub_l1'"
        )
        conn.execute("UPDATE devices SET phase='both', double_pole=1 WHERE device_id='hot_tub'")
        conn.commit()
    finally:
        conn.close()

    Database(schema8_db).close()

    rows = _rows(schema8_db)
    channel = rows["channels"]["tub_l1"]
    assert channel["polarity"] == "inverted"
    assert channel["phase"] == "l2"
    assert channel["paired_ct_id"] == "tub_l2"
    assert channel["device_id"] == "hot_tub"
    assert channel["parent_ct_id"] == "main_l1"
    assert channel["enabled"] == 1
    device = rows["devices"]["hot_tub"]
    assert device["phase"] == "both"
    assert device["double_pole"] == 1
    assert device["name"] == "hot_tub"


def test_v11_strips_projected_cts(schema8_db: Path) -> None:
    """Demoted ids leave ``projected_ct_ids`` -- unless a configuration names them.

    ``project_configuration`` retracts what it previously projected before
    re-adding it, so a stale entry is corrected on the next projection.  An id a
    surviving configuration still lists in ``monitored_by_ct_ids`` is a live
    user declaration and must be kept.
    """
    conn = sqlite3.connect(schema8_db)
    try:
        # A second device whose only projected id no configuration claims.
        _device(
            conn,
            "well_pump",
            dedicated=["well_ct"],
            metadata={"projected_ct_ids": ["well_ct", "lights_ct"]},
        )
        _channel(conn, "well_ct", "dedicated_device", device_id="well_pump")
        conn.commit()
    finally:
        conn.close()

    Database(schema8_db).close()

    devices = _rows(schema8_db)["devices"]
    # ``tub_l1`` is named by the surviving configuration -> kept.
    assert json.loads(devices["hot_tub"]["metadata_json"])["projected_ct_ids"] == ["tub_l1"]
    # ``well_ct`` is claimed by nobody -> stripped; unrelated ids survive.
    assert json.loads(devices["well_pump"]["metadata_json"])["projected_ct_ids"] == ["lights_ct"]


def test_v11_migrates_device_configuration_payload(schema8_db: Path) -> None:
    Database(schema8_db).close()

    payload = json.loads(_rows(schema8_db)["configurations"]["hot_tub"]["payload_json"])
    assert payload["monitoring"] == "shared"
    # Everything the user declared survives.
    assert payload["monitored_by_ct_ids"] == ["tub_l1"]
    assert payload["state_entity_ids"] == ["binary_sensor.hot_tub_running"]
    assert payload["expected"] == {"on_w": 2200.0, "idle_w": 40.0}
    assert payload["configured_by"] == "user"
    # And the row still validates, which is the whole point of migrating it.
    config = DeviceConfiguration.model_validate(payload)
    # ``Model`` sets ``use_enum_values=True``, so a validated field holds the
    # plain string; the assertion is on the value, not on enum identity.
    assert config.monitoring == MonitoringLevel.SHARED.value
    assert MonitoringLevel(payload["monitoring"]) is MonitoringLevel.SHARED


def test_v11_migrates_historical_inference_rows(schema8_db: Path) -> None:
    Database(schema8_db).close()

    rows = _rows(schema8_db)["inference"]
    assert [r["monitoring_level"] for r in rows] == ["shared"]
    # The historical row must still validate against the contract.
    result = InferenceResult(
        device_id=rows[0]["device_id"],
        timestamp=rows[0]["timestamp"],
        power_w=rows[0]["power_w"],
        source=rows[0]["source"],
        confidence=rows[0]["confidence"],
        monitoring_level=rows[0]["monitoring_level"],
    )
    assert result.monitoring_level == MonitoringLevel.SHARED.value
    assert MonitoringLevel(result.monitoring_level) is MonitoringLevel.SHARED


def test_apply_callback_is_idempotent(schema8_db: Path) -> None:
    """Invoke the v11 ``apply=`` callback twice on one connection.

    A second full migration run cannot prove this: ``apply_migrations`` skips a
    version that is already recorded.  A callback that appends instead of
    unioning would duplicate the ids on a retry (an interrupted migration, or a
    database restored from a partial backup) and every device would then report
    its power twice.
    """
    migration = _v11()
    assert migration.apply is not None
    conn = sqlite3.connect(schema8_db)
    try:
        migration.apply(conn)
        first = json.loads(
            conn.execute(
                "SELECT branch_ct_ids_json FROM devices WHERE device_id='hot_tub'"
            ).fetchone()[0]
        )
        migration.apply(conn)
        second = json.loads(
            conn.execute(
                "SELECT branch_ct_ids_json FROM devices WHERE device_id='hot_tub'"
            ).fetchone()[0]
        )
        metadata = json.loads(
            conn.execute("SELECT metadata_json FROM devices WHERE device_id='hot_tub'").fetchone()[0]
        )
        payload = json.loads(
            conn.execute(
                "SELECT payload_json FROM device_configurations WHERE device_id='hot_tub'"
            ).fetchone()[0]
        )
    finally:
        conn.close()

    assert first == ["tub_l1", "tub_l2"]
    assert second == first, "a second pass duplicated the device's ids"
    assert metadata["projected_ct_ids"] == ["tub_l1"]
    assert payload["monitoring"] == "shared"


def test_pre_change_database_loads_after_upgrade(schema8_db: Path) -> None:
    """AC-2: the database opens, migrates and loads without error."""
    db = Database(schema8_db)
    try:
        assert db.schema_version == LATEST_VERSION
        topology = db.load_topology()
        assert topology is not None
        channels = {c.ct_id: c for c in topology.channels}
        assert channels["tub_l1"].ct_type == CTType.BRANCH.value
        assert channels["main_l1"].ct_type == CTType.MAIN.value
        devices = {d.device_id: d for d in topology.devices}
        assert sorted(devices["hot_tub"].branch_ct_ids) == ["tub_l1", "tub_l2"]
        # The configuration set must not have lost the row.
        configurations = db.load_device_configurations()
        assert [c.device_id for c in configurations.devices] == ["hot_tub"]
    finally:
        db.close()


def test_no_attribution_loss(schema8_db: Path) -> None:
    """AC-3: every device keeps at least one attribution row.

    This is the failure the id move exists to prevent: with the channel demoted
    and nothing in ``branch_ct_ids``, ``attribution_ct_ids`` resolves to ``()``
    and the device silently reports nothing forever.
    """
    db = Database(schema8_db)
    try:
        topology = db.load_topology()
    finally:
        db.close()
    assert isinstance(topology, Topology)
    graph = build_graph(topology)

    assert graph.device_ids, "the fixture must contain a device"
    for device_id in graph.device_ids:
        assert graph.attribution_ct_ids(device_id) != (), (
            f"device {device_id} lost every attribution row"
        )


def test_migration_does_not_fake_a_topology_version(schema8_db: Path) -> None:
    """A migration is not a client-visible topology revision."""
    conn = sqlite3.connect(schema8_db)
    try:
        before = conn.execute("SELECT COUNT(*) FROM topology_versions").fetchone()[0]
    finally:
        conn.close()

    Database(schema8_db).close()

    conn = sqlite3.connect(schema8_db)
    try:
        after = conn.execute("SELECT COUNT(*) FROM topology_versions").fetchone()[0]
    finally:
        conn.close()
    assert before == after == 0


def test_migrations_reach_nine_without_gaps() -> None:
    versions = [m.version for m in MIGRATIONS]
    assert versions == list(range(1, len(versions) + 1))
    assert LATEST_VERSION == 11
    assert _v11().name == "retire_dedicated_device_role"


def test_apply_migrations_is_a_no_op_on_a_migrated_database(schema8_db: Path) -> None:
    Database(schema8_db).close()
    conn = sqlite3.connect(schema8_db)
    try:
        assert apply_migrations(conn) == LATEST_VERSION
        branch = json.loads(
            conn.execute(
                "SELECT branch_ct_ids_json FROM devices WHERE device_id='hot_tub'"
            ).fetchone()[0]
        )
    finally:
        conn.close()
    assert branch == ["tub_l1", "tub_l2"]


# --------------------------------------------------------------------------- #
# The Home Assistant config-entry migration
# --------------------------------------------------------------------------- #

PACKAGE_DIR = Path(__file__).resolve().parents[2] / "custom_components" / "wattmosaic"


def _migrate_entry(*, into_options: bool = False) -> tuple[dict[str, Any], dict[str, Any], int]:
    """Run the shipped ``async_migrate_entry`` against a fake config entry.

    ``homeassistant`` is not installed here, so ``__init__.py`` cannot be
    imported.  Rather than assert on the source text, the function's own source
    is extracted with ``ast``, compiled and executed: this drives the real
    implementation, including its persistence call, without the HA import.
    The sibling imports it references (``CONF_CT_ASSIGNMENTS``) are the only
    names supplied.
    """
    tree = ast.parse((PACKAGE_DIR / "__init__.py").read_text())
    function = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_migrate_entry"
        ),
        None,
    )
    assert function is not None, "async_migrate_entry is missing from __init__.py"
    module = ast.Module(body=[function], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace: dict[str, Any] = {"CONF_CT_ASSIGNMENTS": "ct_assignments"}
    exec(compile(module, "<__init__.py:async_migrate_entry>", "exec"), namespace)  # noqa: S102
    migrate = namespace["async_migrate_entry"]

    assignment = {
        "role": "dedicated_device",
        "phase": "l1",
        "polarity": "normal",
        "device_id": "hot_tub",
        "paired_ct_id": "tub_l2",
        "parent_ct_id": "main_l1",
        "alias_of_ct_id": None,
        "enabled": True,
    }

    class Entry:
        def __init__(self) -> None:
            self.version = 1
            self.data: dict[str, Any] = (
                {} if into_options else {"ct_assignments": {"tub_l1": dict(assignment)}}
            )
            self.options: dict[str, Any] = (
                {"ct_assignments": {"tub_l1": dict(assignment)}} if into_options else {}
            )

    class ConfigEntries:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def async_update_entry(self, entry: Entry, **kwargs: Any) -> None:
            self.calls.append(kwargs)
            if "data" in kwargs:
                entry.data = kwargs["data"]
            if "options" in kwargs:
                entry.options = kwargs["options"]
            if "version" in kwargs:
                entry.version = kwargs["version"]

    class Hass:
        def __init__(self) -> None:
            self.config_entries = ConfigEntries()

    entry = Entry()
    hass = Hass()
    assert asyncio.run(migrate(hass, entry)) is True
    return entry.data, entry.options, entry.version


def test_legacy_entry_migrates_roles() -> None:
    """AC-5: a stored ``dedicated_device`` role becomes ``branch``."""
    data, options, version = _migrate_entry()

    assignment = data["ct_assignments"]["tub_l1"]
    assert assignment["role"] == "branch"
    # Every other declared field is preserved.
    assert assignment["device_id"] == "hot_tub"
    assert assignment["phase"] == "l1"
    assert assignment["polarity"] == "normal"
    assert assignment["paired_ct_id"] == "tub_l2"
    assert assignment["parent_ct_id"] == "main_l1"
    assert assignment["alias_of_ct_id"] is None
    assert assignment["enabled"] is True
    assert options == {}
    # The version must be persisted, not merely assigned.
    assert version == 2


def test_legacy_entry_in_options_is_migrated() -> None:
    data, options, version = _migrate_entry(into_options=True)

    assert options["ct_assignments"]["tub_l1"]["role"] == "branch"
    assert options["ct_assignments"]["tub_l1"]["device_id"] == "hot_tub"
    assert data == {}
    assert version == 2


def test_legacy_entry_migration_does_not_run_twice() -> None:
    """A version-2 entry is left alone: no rewrites, no version churn."""
    # Drive the extracted function with an already-migrated entry.
    tree = ast.parse((PACKAGE_DIR / "__init__.py").read_text())
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "async_migrate_entry"
    )
    module = ast.Module(body=[function], type_ignores=[])
    ast.fix_missing_locations(module)
    namespace: dict[str, Any] = {"CONF_CT_ASSIGNMENTS": "ct_assignments"}
    exec(compile(module, "<__init__.py:async_migrate_entry>", "exec"), namespace)  # noqa: S102
    migrate = namespace["async_migrate_entry"]

    class Entry:
        version = 2
        data = {"ct_assignments": {"tub_l1": {"role": "branch"}}}
        options: dict[str, Any] = {}

    class ConfigEntries:
        calls: list[dict[str, Any]] = []

        def async_update_entry(self, entry: Entry, **kwargs: Any) -> None:
            self.calls.append(kwargs)

    class Hass:
        def __init__(self) -> None:
            self.config_entries = ConfigEntries()

    hass = Hass()
    assert asyncio.run(migrate(hass, Entry())) is True
    assert hass.config_entries.calls == []


def test_normalise_assignment_no_longer_aliases_the_legacy_role() -> None:
    """The temporary migration-window alias is gone (task-13).

    ``normalise_assignment`` used to map ``dedicated_device`` to ``branch`` so a
    stored entry that had escaped ``async_migrate_entry`` kept its attribution.
    That shim outlived its window and made the retired role reachable through the
    wizard, so it is removed: the migration is now the *only* thing that rewrites
    a legacy role (``test_legacy_entry_migrates_roles`` pins that end), and an
    un-migrated payload falls through to ``unknown`` -- an honest "not assigned"
    rather than an invented branch claim.
    """
    flow = _flow_logic()
    normalised = flow.normalise_assignment({"role": "dedicated_device", "phase": "l1"})
    assert normalised["role"] == "unknown"
    # An actually-unknown role is still normalised to ``unknown``.
    assert flow.normalise_assignment({"role": "not_a_role"})["role"] == "unknown"


def test_migrated_entry_replays_without_error(schema8_db: Path) -> None:
    """AC-5 end to end: a legacy entry replays onto a migrated engine topology.

    Before the migration the stored role was ``dedicated_device`` while the
    engine's channel said ``branch``: ``assignment_matches_channel`` compared the
    two, ``plan_topology_push`` returned ``declaration_missing``, and the replay
    PUT the old role straight back -- which the engine rejects with 400 once the
    enum drops the value.
    """
    flow = _flow_logic()
    db = Database(schema8_db)
    try:
        topology = db.load_topology()
    finally:
        db.close()
    assert topology is not None
    payload = json.loads(json.dumps(topology.model_dump(mode="json"), default=str))

    data, _, _ = _migrate_entry()
    assignments = data["ct_assignments"]

    plan = flow.plan_topology_push(payload, assignments, confirmed=True, stored_version=None)
    assert plan.reason != "declaration_missing"
    assert plan.push is True

    updated = flow.build_topology_update(payload, assignments, confirmed=True)
    # What the engine would receive must still be a valid topology: no 400.
    Topology.model_validate(updated)
    channel = next(c for c in updated["channels"] if c["ct_id"] == "tub_l1")
    assert channel["ct_type"] == "branch"
    device = next(d for d in updated["devices"] if d["device_id"] == "hot_tub")
    assert "tub_l1" in device["branch_ct_ids"]
    assert sorted(device["branch_ct_ids"]) == ["tub_l1", "tub_l2"]

    # And a second replay is a no-op: the declaration now matches the engine.
    second = flow.plan_topology_push(updated, assignments, confirmed=True, stored_version=None)
    assert second.push is False
    assert second.reason in {"in_sync", "engine_matches"}


# --------------------------------------------------------------------------- #
# Loading ``flow_logic`` without Home Assistant
# --------------------------------------------------------------------------- #


def _flow_logic() -> types.ModuleType:
    """Import the pure ``flow_logic`` module behind a synthetic package handle."""
    name = "wm_migrations_under_test"
    if f"{name}.flow_logic" in sys.modules:
        return sys.modules[f"{name}.flow_logic"]
    spec = importlib.util.spec_from_file_location(
        name,
        PACKAGE_DIR / "__init__.py",
        submodule_search_locations=[str(PACKAGE_DIR)],
    )
    assert spec is not None
    package = importlib.util.module_from_spec(spec)
    # Do not exec the spec: that would import homeassistant.
    package.__path__ = [str(PACKAGE_DIR)]  # type: ignore[attr-defined]
    sys.modules[name] = package
    return importlib.import_module(f"{name}.flow_logic")
