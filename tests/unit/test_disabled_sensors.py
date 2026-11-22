"""WM_DISABLED_SENSORS: config parsing, matching, reconcile and helpers.

Synthetic ids only: the rule is driven by the user's list, never by names.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import yaml
from wattmosaic_engine.config.settings import EngineConfig, parse_sensor_patterns
from wattmosaic_engine.contracts import EndpointConfigRevision, MeasurementEndpoint
from wattmosaic_engine.devices.disabled import (
    DISABLED_BY_CONFIG,
    reconcile_disabled_endpoints,
    sensor_matches,
)
from wattmosaic_engine.electrical.topology_memory import StructureMemory
from wattmosaic_engine.electrical.topology_placement import (
    Placement,
    PlacementReport,
    exclude_readers,
)
from wattmosaic_engine.storage.database import Database

T0 = datetime(2026, 1, 1, tzinfo=UTC)
REPO_ROOT = Path(__file__).resolve().parents[2]


# -- config ----------------------------------------------------------------- #


def test_parse_sensor_patterns_splits_commas_spaces_newlines_and_dedupes() -> None:
    assert parse_sensor_patterns(None) == ()
    assert parse_sensor_patterns("") == ()
    assert parse_sensor_patterns(" a, b\nc  d,,a ") == ("a", "b", "c", "d")


def test_engine_config_reads_wm_disabled_sensors(tmp_path: Path) -> None:
    cfg = EngineConfig.from_env(
        {"WM_DISABLED_SENSORS": "meter_b_l1, sensor.*_charger_power"},
        dotenv_path=tmp_path / "absent.env",
    )
    assert cfg.disabled_sensors == ("meter_b_l1", "sensor.*_charger_power")
    assert cfg.redacted()["disabled_sensors"] == ["meter_b_l1", "sensor.*_charger_power"]
    empty = EngineConfig.from_env({}, dotenv_path=tmp_path / "absent.env")
    assert empty.disabled_sensors == ()


def test_sensor_matches_endpoint_or_entity_glob_case_insensitive() -> None:
    assert sensor_matches(("METER_B_*",), "meter_b_l1", "sensor.x_power")
    assert sensor_matches(("sensor.*charger_power",), "plug_1", "sensor.Garage_Charger_Power")
    assert not sensor_matches(("meter_b_*",), "meter_a_l1", "sensor.meter_a_power")
    assert not sensor_matches(("", "  "), "meter_a_l1", None)


# -- addon option parity ----------------------------------------------------- #


def test_addon_option_is_declared_and_mapped_to_the_env_var() -> None:
    cfg = yaml.safe_load((REPO_ROOT / "addon" / "config.yaml").read_text(encoding="utf-8"))
    assert cfg["options"]["disabled_sensors"] == ""
    assert str(cfg["schema"]["disabled_sensors"]).rstrip("?") == "str"
    text = (REPO_ROOT / "addon" / "run.sh").read_text(encoding="utf-8")
    block = re.search(r'readonly OPTION_TABLE="\n(.*?)"\n', text, re.S)
    assert block is not None
    assert "disabled_sensors|WM_DISABLED_SENSORS|" in block.group(1).splitlines()


# -- reconcile --------------------------------------------------------------- #


def _seed(db: Database, endpoint_id: str, entity_id: str, *, enabled: bool = True,
          metadata: dict | None = None) -> None:
    endpoint = MeasurementEndpoint(
        endpoint_id=endpoint_id, kind="ct", entity_id=entity_id, display_name=endpoint_id,
        scope="circuit", scope_status="candidate", enabled=enabled, created_at=T0,
    )
    row = endpoint.model_dump(mode="json")
    row["metadata"] = metadata or {"source": "ha_discovery"}
    db.repos.entities.upsert_endpoint(row)
    db.repos.entities.append_endpoint_revision(
        EndpointConfigRevision(
            endpoint_id=endpoint_id, revision=1, valid_from=T0, phase="unknown",
            polarity="normal", multiplier=1.0, calibration_version="discovery",
            measurement_type="instantaneous", declared_by="discovery",
        )
    )


def _rows(db: Database) -> dict[str, dict]:
    out = {}
    for row in db.repos.entities.endpoints():
        row = dict(row)
        row["meta"] = json.loads(row.get("metadata_json") or "{}")
        out[row["endpoint_id"]] = row
    return out


def test_reconcile_disables_marks_preserves_metadata_and_is_idempotent(tmp_path: Path) -> None:
    db = Database(tmp_path / "d.db")
    try:
        _seed(db, "meter_a_l1", "sensor.meter_a_ct1_power")
        _seed(db, "meter_b_l1", "sensor.meter_b_ct1_power")
        _seed(db, "plug_x", "sensor.garage_charger_power")
        _seed(db, "plug_candidate", "sensor.other_charger_power", enabled=False)
        events: list[tuple[str, str]] = []

        result = reconcile_disabled_endpoints(
            db.repos.entities,
            ("meter_b_*", "sensor.*charger_power", "typo_*"),
            record_event=lambda eid, action: events.append((eid, action)),
        )

        rows = _rows(db)
        assert result.endpoint_ids == {"meter_b_l1", "plug_x", "plug_candidate"}
        assert result.entity_ids == {
            "sensor.meter_b_ct1_power", "sensor.garage_charger_power", "sensor.other_charger_power",
        }
        assert set(result.disabled) == {"meter_b_l1", "plug_x"}
        assert result.unmatched == ("typo_*",)
        assert not rows["meter_b_l1"]["enabled"] and not rows["plug_x"]["enabled"]
        assert rows["meter_b_l1"]["meta"] == {"source": "ha_discovery", "disabled_by": DISABLED_BY_CONFIG}
        # Already disabled for another reason: left alone, never marked.
        assert "disabled_by" not in rows["plug_candidate"]["meta"]
        assert rows["meter_a_l1"]["enabled"]
        assert sorted(events) == [("meter_b_l1", "config.disable"), ("plug_x", "config.disable")]

        events.clear()
        again = reconcile_disabled_endpoints(
            db.repos.entities, ("meter_b_*", "sensor.*charger_power"),
            record_event=lambda eid, action: events.append((eid, action)),
        )
        assert again.disabled == () and again.reenabled == () and events == []
    finally:
        db.close()


def test_reconcile_reenables_only_what_the_config_disabled(tmp_path: Path) -> None:
    db = Database(tmp_path / "d.db")
    try:
        _seed(db, "meter_b_l1", "sensor.meter_b_ct1_power")
        _seed(db, "plug_candidate", "sensor.other_charger_power", enabled=False)
        reconcile_disabled_endpoints(db.repos.entities, ("meter_b_l1", "plug_candidate"))

        result = reconcile_disabled_endpoints(db.repos.entities, ())

        rows = _rows(db)
        assert result.reenabled == ("meter_b_l1",)
        assert result.endpoint_ids == frozenset()
        assert rows["meter_b_l1"]["enabled"]
        assert rows["meter_b_l1"]["meta"] == {"source": "ha_discovery"}
        assert not rows["plug_candidate"]["enabled"], "never enable what config did not disable"
    finally:
        db.close()


# -- placements and memory --------------------------------------------------- #


def test_exclude_readers_drops_disabled_and_rehomes_children() -> None:
    report = PlacementReport(by_child={
        "dup": Placement(child="dup", parent=None),
        "feed": Placement(child="feed", parent=None),
        "sub": Placement(child="sub", parent="dup", band="high", score=0.9,
                         alternatives=(("feed", 0.5), ("other", 0.1))),
        "orphan": Placement(child="orphan", parent="dup", band="low", score=0.2),
        "leg": Placement(child="leg", parent=None, band="medium", both_legs=("dup", "feed")),
        "kept": Placement(child="kept", parent="feed", band="high", score=0.9,
                          alternatives=(("dup", 0.3),)),
    })

    out = exclude_readers(report, frozenset({"dup"}))

    assert "dup" not in out.by_child
    assert out.by_child["sub"].parent == "feed"
    assert out.by_child["sub"].alternatives == (("other", 0.1),)
    assert out.by_child["orphan"].parent is None
    assert out.by_child["leg"].parent == "feed" and out.by_child["leg"].both_legs == ()
    assert out.by_child["kept"].parent == "feed" and out.by_child["kept"].alternatives == ()
    assert report.by_child["sub"].parent == "dup", "input must not be mutated"
    assert exclude_readers(report, frozenset()) is report


def test_structure_memory_forget_drops_tracks() -> None:
    memory = StructureMemory()
    memory.record("a", 1000.0, 5.0)
    memory.record("b", 1000.0, 6.0)
    assert memory.forget(["b", "missing"]) == 1
    assert memory.readers() == ("a",)
