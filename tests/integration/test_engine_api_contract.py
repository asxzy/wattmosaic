"""Orchestrator's independent integration test: engine API + facade.

These tests do not use Home Assistant at all. They build a real SQLite database,
a real topology, a real API server bound to an ephemeral port, and exercise the
public contract over HTTP exactly as the HA integration does.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import aiohttp
import pytest
from wattmosaic_engine.api.facade import EngineFacade
from wattmosaic_engine.api.server import APIServer
from wattmosaic_engine.contracts import (
    CONTRACT_VERSION,
    CTChannel,
    CTType,
    DeviceTopology,
    HealthResponse,
    InferenceResult,
    Phase,
    Reconciliation,
    Source,
    Topology,
    TopologyStatus,
)
from wattmosaic_engine.storage import Database


def _topology() -> Topology:
    return Topology(
        status=TopologyStatus.CANDIDATE,
        channels=[
            CTChannel(ct_id="main_l1", entity_id="sensor.m1", ct_type=CTType.MAIN, phase=Phase.L1),
            CTChannel(ct_id="br_fridge", entity_id="sensor.br1", ct_type=CTType.BRANCH,
                      phase=Phase.L1, parent_ct_id="main_l1"),
        ],
        devices=[DeviceTopology(device_id="dev_fridge", name="Fridge", dedicated_ct_ids=["br_fridge"])],
    )


class Facade(EngineFacade):
    """Minimal concrete facade for API contract tests."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self._topology = _topology()

    async def health(self) -> HealthResponse:
        return HealthResponse(
            status="ok",
            contract_version=CONTRACT_VERSION,
            engine_version="0.1.3",
            database="ok",
            ha_connected=True,
            uptime_seconds=1.0,
        )

    async def status(self) -> dict:
        return {"engine": "test", "database": self.db.stats()}

    async def snapshot(self):
        from wattmosaic_engine.contracts import SnapshotResponse

        return SnapshotResponse(
            reconciliation=Reconciliation(main_power_w=500.0, direct_measured_w=120.0,
                                          unknown_residual_w=380.0),
            results=[
                InferenceResult(
                    device_id="dev_fridge",
                    timestamp=datetime.now(UTC),
                    power_w=120.0,
                    source=Source.DIRECT_MEASURED,
                    confidence=0.9,
                    supporting_ct_channels=["br_fridge"],
                    explanation=["direct_ct_measurement"],
                )
            ],
        )

    async def devices(self) -> list[dict]:
        return [{"device_id": "dev_fridge", "name": "Fridge", "power_w": 120.0}]

    async def device(self, device_id: str) -> dict | None:
        return {"device_id": device_id, "power_w": 120.0} if device_id == "dev_fridge" else None

    async def topology(self) -> Topology:
        return self._topology

    async def set_topology(self, topology: Topology) -> dict:
        self._topology = topology
        version = self.db.save_topology(topology)
        return {"version": version, "status": topology.status}

    async def confirm_topology(self) -> dict:
        self._topology.status = TopologyStatus.CONFIRMED
        return {"status": "confirmed"}

    async def quality(self):
        from wattmosaic_engine.contracts import DataQualityReport

        return DataQualityReport(overall_score=0.9, issues=[])

    async def diagnostics(self) -> dict:
        return {"status": "ok", "secrets": "redacted"}

    async def models(self, device_id: str) -> dict:
        return {"device_id": device_id, "versions": [], "active": None}

    async def command(self, command):
        from wattmosaic_engine.contracts import ServiceResult

        return ServiceResult(ok=True, command=command.command, detail="accepted")

    async def export(self, device_id: str) -> dict:
        return self.db.export_device_data(device_id)

    async def metrics(self) -> str:
        return "# HELP wattmosaic_up\nwattmosaic_up 1\n"


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "api.db")
    database.save_topology(_topology())
    yield database
    database.close()


async def _with_server(db: Database, fn):
    facade = Facade(db)
    server = APIServer(facade, host="127.0.0.1", port=0, api_token="test-token")
    await server.start()
    try:
        port = server.port
        base = f"http://127.0.0.1:{port}/api/v1"
        headers = {"X-API-Token": "test-token"}
        async with aiohttp.ClientSession() as session:
            return await fn(session, base, headers)
    finally:
        await server.stop()


def test_health_requires_token(db: Database) -> None:
    async def run(session, base, headers):
        async with session.get(f"{base}/health") as resp:
            assert resp.status == 401
            body = await resp.json()
            assert "error" in body
        async with session.get(f"{base}/health", headers=headers) as resp:
            assert resp.status == 200
            body = await resp.json()
            assert body["status"] == "ok"
            assert resp.headers["X-WattMosaic-Contract-Version"] == CONTRACT_VERSION
            HealthResponse.model_validate(body)
        return True

    assert asyncio.run(_with_server(db, run))


def test_snapshot_matches_contract(db: Database) -> None:
    async def run(session, base, headers):
        async with session.get(f"{base}/snapshot", headers=headers) as resp:
            assert resp.status == 200
            body = await resp.json()
        from wattmosaic_engine.contracts import SnapshotResponse

        parsed = SnapshotResponse.model_validate(body)
        assert parsed.reconciliation.unknown_residual_w == 380.0
        assert parsed.results[0].source == "direct_measured"
        assert parsed.results[0].confidence_is_calibrated is False
        return True

    assert asyncio.run(_with_server(db, run))


def test_device_404_is_structured(db: Database) -> None:
    async def run(session, base, headers):
        async with session.get(f"{base}/devices/nope", headers=headers) as resp:
            assert resp.status == 404
            body = await resp.json()
            assert "error" in body
        return True

    assert asyncio.run(_with_server(db, run))


def test_topology_put_and_confirm(db: Database) -> None:
    async def run(session, base, headers):
        payload = _topology().model_dump(mode="json")
        payload["status"] = "candidate"
        async with session.put(f"{base}/topology", json=payload, headers=headers) as resp:
            assert resp.status in (200, 201), await resp.text()
        async with session.post(f"{base}/topology/confirm", headers=headers) as resp:
            assert resp.status == 200
        async with session.get(f"{base}/topology", headers=headers) as resp:
            body = await resp.json()
        assert body["channels"]
        return True

    assert asyncio.run(_with_server(db, run))


def test_tampered_token_rejected(db: Database) -> None:
    async def run(session, base, headers):
        async with session.get(f"{base}/health", headers={"X-API-Token": "wrong"}) as resp:
            assert resp.status == 401
        return True

    assert asyncio.run(_with_server(db, run))


def test_metrics_endpoint_text(db: Database) -> None:
    async def run(session, base, headers):
        async with session.get(f"{base}/metrics", headers=headers) as resp:
            assert resp.status == 200
            text = await resp.text()
            assert "wattmosaic_up" in text
        return True

    assert asyncio.run(_with_server(db, run))


def test_command_roundtrip(db: Database) -> None:
    async def run(session, base, headers):
        async with session.post(
            f"{base}/commands", json={"command": "start_learning"}, headers=headers
        ) as resp:
            assert resp.status in (200, 202), await resp.text()
            body = await resp.json()
            assert body["ok"] is True
        async with session.post(
            f"{base}/commands", json={"command": "not_a_command"}, headers=headers
        ) as resp:
            assert resp.status == 400
        return True

    assert asyncio.run(_with_server(db, run))


def test_link_state_entities_survive_the_http_and_sqlite_round_trip(
    db: Database,
) -> None:
    """The whole ``state_entity_ids`` chain, proved over the real wire.

    The integration resolves an HA device id to state entity ids and writes them
    into ``metadata.link_state_entity_ids`` on ``PUT /topology``.  Every other
    test stops before the HTTP+SQLite boundary: the replay tests assert the
    request body, and the intake tests build the database in-process.  A schema
    that silently dropped device ``metadata`` would still pass both and leave
    learning dead in production, which is exactly the class of defect this
    release is fixing.  So drive the real server, a real SQLite file, and the
    engine's own reader.
    """
    from wattmosaic_engine.device_links import (
        runtime_state_entities,
        state_entity_for_device,
    )

    linked = ["switch.hot_tub", "binary_sensor.hot_tub_status"]

    async def run(session, base, headers):
        payload = _topology().model_dump(mode="json")
        payload["status"] = "candidate"
        for device in payload["devices"]:
            if device["device_id"] == "dev_fridge":
                device["metadata"] = {"link_state_entity_ids": list(linked)}
        async with session.put(f"{base}/topology", json=payload, headers=headers) as resp:
            assert resp.status in (200, 201), await resp.text()
        return True

    assert asyncio.run(_with_server(db, run))

    # Re-read from the file, not from the server's in-memory copy: the storage
    # round-trip is the link under test.
    reloaded = Database(db.path)
    try:
        topology = reloaded.load_topology()
        assert topology is not None
        device = topology.device_map()["dev_fridge"]
        assert runtime_state_entities(device) == linked
        assert state_entity_for_device(topology, "dev_fridge", "switch.hot_tub")
        # The device's CT entity is a measurement, never a state source.
        assert not state_entity_for_device(topology, "dev_fridge", "sensor.br1")
    finally:
        reloaded.close()


def test_export_contains_no_secrets(db: Database) -> None:
    async def run(session, base, headers):
        async with session.get(f"{base}/export/dev_fridge", headers=headers) as resp:
            assert resp.status == 200
            text = await resp.text()
        lowered = text.lower()
        assert "token" not in lowered
        assert "authorization" not in lowered
        json.loads(text)
        return True

    assert asyncio.run(_with_server(db, run))
