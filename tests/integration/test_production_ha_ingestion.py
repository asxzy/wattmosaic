"""The production HA source delivers readings for discovered endpoints.

Found on a live HA: the engine connected and discovered 40 CT endpoints, yet
stored no reading and published no snapshot.  Two defects stacked:

* ``main.py`` builds the source with ``HASource.from_config``, which passes no
  revision lookup, so P1's normalizer was never created and every reading was
  dropped.  Every test built its own wired source, so none caught it.
* Readings were keyed by the entity id minus its domain, while discovery names
  endpoints independently (``sensor.server_room_vec_2_ct1_power`` is endpoint
  ``vec_2_l1``), so even a wired normalizer quarantined them (NO_REVISION).

A reader entity's state change was also emitted as an unlinked HA event, which
turned every power sample into a P5 group.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from wattmosaic_engine.config.settings import EngineConfig
from wattmosaic_engine.ingestion.ha_source import HASource
from wattmosaic_engine.main import EngineCore
from wattmosaic_engine.storage.database import Database

T0 = datetime(2026, 11, 20, 3, 0, 0, tzinfo=UTC)
ENTITY = "sensor.server_room_vec_2_ct1_power"
ENDPOINT = "vec_2_l1"


def _core(tmp_path: Path, now: list[datetime]) -> EngineCore:
    config = EngineConfig(database_path=str(tmp_path / "prod.db"), ha_token="x")
    db = Database(tmp_path / "prod.db")
    core = EngineCore(config, db, clock=lambda: now[0])
    # Exactly what main.py does in production.
    core.hasource = HASource.from_config(config)
    entities = db.repos.entities
    entities.upsert_endpoint(
        {"endpoint_id": ENDPOINT, "kind": "ct", "entity_id": ENTITY, "scope": "panel_feed",
         "scope_status": "candidate", "enabled": True, "created_at": T0 - timedelta(days=1)}
    )
    entities.append_endpoint_revision(
        {"endpoint_id": ENDPOINT, "revision": 1, "valid_from": T0 - timedelta(days=1),
         "multiplier": 1.0, "calibration_version": "discovery", "declared_by": "discovery"}
    )
    return core


def _state(value: str, at: datetime) -> dict[str, Any]:
    return {
        "entity_id": ENTITY,
        "state": value,
        "attributes": {"unit_of_measurement": "W", "device_class": "power"},
        "last_updated": at.isoformat(),
        "context": {"id": f"ctx-{value}"},
    }


async def test_production_source_delivers_readings_under_the_endpoint_id(tmp_path: Path) -> None:
    now = [T0]
    core = _core(tmp_path, now)
    core._wire_v3_services()  # noqa: SLF001 - the production start path calls this

    readings: list[Any] = []
    ha_events: list[Any] = []

    async def on_reading(reading: Any) -> None:
        readings.append(reading)

    async def on_ha_event(event: Any) -> None:
        ha_events.append(event)

    source = core.hasource
    source._on_reading = on_reading  # noqa: SLF001 - what start() installs
    source._on_ha_event = on_ha_event  # noqa: SLF001
    await source._handle_state(_state("412.5", T0))  # noqa: SLF001

    assert [r.reader_id for r in readings] == [ENDPOINT]
    assert readings[0].active_power_w == 412.5
    # A measurement endpoint's sample is a reading, not an HA context event.
    assert ha_events == []


async def test_unknown_entities_still_emit_ha_events(tmp_path: Path) -> None:
    now = [T0]
    core = _core(tmp_path, now)
    core._wire_v3_services()  # noqa: SLF001
    ha_events: list[Any] = []

    async def on_ha_event(event: Any) -> None:
        ha_events.append(event)

    source = core.hasource
    source._on_reading = lambda _r: None  # noqa: SLF001
    source._on_ha_event = on_ha_event  # noqa: SLF001
    await source._handle_state(  # noqa: SLF001
        {"entity_id": "switch.dishwasher", "state": "on", "attributes": {},
         "last_updated": T0.isoformat(), "context": {"id": "ctx-dw"}}
    )
    assert [e.entity_id for e in ha_events] == ["switch.dishwasher"]
