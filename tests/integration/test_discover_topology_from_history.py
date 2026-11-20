"""Pressing Discover learns topology from the recorder, not only devices."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from wattmosaic_engine.config.settings import EngineConfig
from wattmosaic_engine.main import EngineCore
from wattmosaic_engine.storage.database import Database

from tests.unit.test_topology_history import _house

ENTITY = {
    "feed": "sensor.feed_power",
    "branch_hrv": "sensor.hrv_power",
    "branch_heater": "sensor.heater_power",
    "other_panel_pump": "sensor.pump_power",
}


class _Recorder:
    """Read-only HA double: a state snapshot and a recorder history."""

    connected = True
    last_history_error = None

    def __init__(self, payload: dict[str, list[dict[str, Any]]]) -> None:
        self.payload = payload
        self.calls: list[list[str]] = []

    async def state_snapshot(self) -> list[dict[str, Any]]:
        return []

    async def registries(self) -> dict[str, Any]:
        return {"entities": [], "devices": []}

    async def history(self, entity_ids: Any, *, start: datetime, end: datetime | None = None,
                      include_attributes: bool = False, timeout_s: float | None = None) -> dict:
        wanted = list(entity_ids)
        self.calls.append(wanted)
        return {key: rows for key, rows in self.payload.items() if key in wanted}


def _core(tmp_path: Path, now: datetime) -> EngineCore:
    db = Database(tmp_path / "discover.db")
    core = EngineCore(
        EngineConfig(database_path=str(tmp_path / "discover.db"), ha_token="x"),
        db,
        clock=lambda: now,
    )
    for reader, entity in ENTITY.items():
        db.repos.entities.upsert_endpoint(
            {"endpoint_id": reader, "kind": "ct", "entity_id": entity, "scope": "circuit",
             "scope_status": "candidate", "enabled": True,
             "created_at": now - timedelta(days=2)}
        )
    core._wire_v3_services()  # noqa: SLF001 - the production start path
    return core


async def test_discover_merges_recorded_topology_into_the_live_ledger(tmp_path: Path) -> None:
    house = _house()
    now = max(t for rows in house.values() for t, _ in rows) + timedelta(minutes=1)
    payload = {
        ENTITY[reader]: [{"s": f"{value:.1f}", "lu": t.timestamp()} for t, value in rows]
        for reader, rows in house.items()
    }
    core = _core(tmp_path, now)
    core.hasource = _Recorder(payload)

    result = await core.discover_placements(lookback_hours=8.0)

    topology = result["topology"]
    assert topology["ok"] is True, topology
    assert topology["replay"]["steps"] > 40
    assert {"parent": "feed", "child": "branch_hrv"} in topology["confirmed"]
    assert {"parent": "feed", "child": "branch_heater"} in topology["confirmed"]
    # Into the live ledger and its storage, not only the response.
    assert str(core.topology_inference.edge("feed", "branch_hrv").lifecycle.value) == "confirmed"
    stored = {str(row["edge_id"]): row for row in core.db.repos.topology_v3.edges()}
    assert stored["te-inf::feed::branch_hrv"]["lifecycle"] == "confirmed"
    # The confirmation published a topology version, as a live event would.
    active = core.db.repos.topology_v3.active_version()
    assert "te-inf::feed::branch_hrv" in str(active.get("edge_ids_json"))
    # And the panel hierarchy now nests the branch under its feed.
    tree = core.panel_tree()
    unassigned = [n for n in tree["roots"] if str(n["id"]).startswith("group:Unassigned")]
    left = {c["reader_id"] for n in unassigned for c in n["children"]}
    assert "branch_hrv" not in left

    # Running it again is idempotent: no evidence is counted twice.
    support = core.topology_inference.edge("feed", "branch_hrv").evidence.support
    again = await core.discover_placements(lookback_hours=8.0)
    assert again["topology"]["changed_edges"] == 0
    assert core.topology_inference.edge("feed", "branch_hrv").evidence.support == support


async def test_the_apply_run_does_not_replay_topology_again(tmp_path: Path) -> None:
    house = _house()
    now = max(t for rows in house.values() for t, _ in rows) + timedelta(minutes=1)
    core = _core(tmp_path, now)
    recorder = _Recorder({})
    core.hasource = recorder
    result = await core.discover_placements(lookback_hours=8.0, apply=True)
    assert "topology" not in result
    assert all("sensor.feed_power" not in call for call in recorder.calls)


async def test_discover_hands_the_replay_known_children_and_aligned_history(
    tmp_path: Path, monkeypatch
) -> None:
    # Linking v2 W2: explain-away needs the live CONFIRMED children, and the
    # structural pass gets the history hold-aligned (change-only recorder rows:
    # unlimited hold) on a 10 s grid, capped to the most recent 24 h.
    import wattmosaic_engine.electrical.topology_history as history_module
    import wattmosaic_engine.electrical.topology_regression as regression

    house = _house()
    now = max(t for rows in house.values() for t, _ in rows) + timedelta(minutes=1)
    payload = {
        ENTITY[reader]: [{"s": f"{value:.1f}", "lu": t.timestamp()} for t, value in rows]
        for reader, rows in house.items()
    }
    core = _core(tmp_path, now)
    core.hasource = _Recorder(payload)
    first = await core.discover_placements(lookback_hours=8.0)
    assert {"parent": "feed", "child": "branch_hrv"} in first["topology"]["confirmed"]
    absolute_before = core.topology_inference.absolute

    seen: dict[str, Any] = {}
    real_learn, real_align = history_module.learn_topology_from_history, regression.align

    def learn(view, history, **kwargs):
        seen["known"] = set(kwargs.get("known", ()))
        return real_learn(view, history, **kwargs)

    def align(history, **kwargs):
        seen["align"] = kwargs
        seen["aligned"] = real_align(history, **kwargs)
        return seen["aligned"]

    monkeypatch.setattr(history_module, "learn_topology_from_history", learn)
    monkeypatch.setattr(regression, "align", align)
    again = await core.discover_placements(lookback_hours=48.0)

    assert again["topology"]["ok"] is True
    assert ("feed", "branch_hrv") in seen["known"]
    assert seen["align"]["max_hold_s"] is None
    assert seen["align"]["step_s"] == 10.0
    span = (seen["align"]["end"] - seen["align"]["start"]).total_seconds()
    assert span == 24 * 3600.0
    assert set(seen["aligned"]) == set(ENTITY)
    assert {len(values) for values in seen["aligned"].values()} == {8640}
    # The live view does not keep a day of history (ADR-0033).
    assert core.topology_inference.absolute == absolute_before


_ = UTC
