"""Read API for per-device configuration and confidence tiers.

These tests use the real :class:`~wattmosaic_engine.main.EngineCore` (real SQLite,
real topology, real configuration projection) over an ephemeral HTTP port.  No
Home Assistant and no external network are involved, and no inference backend is
required: the configuration surface is derived from the topology alone.
"""

from __future__ import annotations

import asyncio

import aiohttp
import pytest
from wattmosaic_engine.api.server import APIServer
from wattmosaic_engine.config.settings import EngineConfig
from wattmosaic_engine.contracts import (
    CTChannel,
    CTType,
    DeviceConfiguration,
    DeviceTopology,
    Phase,
    Source,
    Topology,
    TopologyStatus,
)
from wattmosaic_engine.disaggregation.configuration import project_configuration
from wattmosaic_engine.main import EngineCore
from wattmosaic_engine.storage.database import Database

TOKEN = "config-token"
HEADERS = {"X-API-Token": TOKEN}


def _topology() -> Topology:
    """One main CT, one dedicated CT, one shared branch CT."""
    return Topology(
        status=TopologyStatus.CANDIDATE,
        channels=[
            CTChannel(
                ct_id="main_l1",
                entity_id="sensor.main_l1",
                ct_type=CTType.MAIN,
                phase=Phase.L1,
            ),
            CTChannel(
                ct_id="ct_hot_tub",
                entity_id="sensor.hot_tub_power",
                ct_type=CTType.DEDICATED_DEVICE,
                phase=Phase.L1,
                parent_ct_id="main_l1",
                device_id="hot_tub",
            ),
            CTChannel(
                ct_id="br_kitchen",
                entity_id="sensor.kitchen_lights_power",
                ct_type=CTType.BRANCH,
                phase=Phase.L1,
                parent_ct_id="main_l1",
            ),
        ],
        devices=[
            DeviceTopology(
                device_id="hot_tub",
                name="Hot tub",
                dedicated_ct_ids=["ct_hot_tub"],
                phase=Phase.L1,
            ),
            DeviceTopology(
                device_id="kitchen_lights",
                name="Kitchen lights",
                branch_ct_ids=["br_kitchen"],
                phase=Phase.L1,
            ),
        ],
    )


@pytest.fixture()
def core(tmp_db_path):
    db = Database(tmp_db_path)
    db.save_topology(_topology())
    config = EngineConfig(
        database_path=str(tmp_db_path), ha_token="not-used", api_token=TOKEN
    )
    engine = EngineCore(config, db)
    engine.topology_cache = db.load_topology()
    try:
        yield engine
    finally:
        db.close()


async def _with_server(core, fn):
    server = APIServer(core, host="127.0.0.1", port=0, api_token=TOKEN)
    await server.start()
    try:
        url = f"http://127.0.0.1:{server.port}/api/v1"
        async with aiohttp.ClientSession(headers=HEADERS) as session:
            return await fn(session, url)
    finally:
        await server.stop()


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# /devices enrichment
# --------------------------------------------------------------------------- #


def test_devices_carry_configuration_and_tier(core) -> None:
    async def fn(session, base):
        async with session.get(f"{base}/devices") as resp:
            assert resp.status == 200
            devices = await resp.json()
        assert isinstance(devices, list), "the endpoint must stay a bare list"
        assert {d["device_id"] for d in devices} == {"hot_tub", "kitchen_lights"}
        for device in devices:
            # Existing keys are never removed.
            assert {"device_id", "name", "latest_result", "model"} <= set(device)
            # New keys.
            assert "confidence_tier" in device
            configuration = device["configuration"]
            assert isinstance(configuration, dict)
            assert set(configuration) == {
                "device_type",
                "connectivity",
                "monitoring",
                "behavior",
                "monitored_by_ct_ids",
                "load_entity_ids",
                "mode_entity_ids",
                "expected_on_w",
                "expected_standby_w",
                "shared_unmodelled",
                "notes",
            }
            # Values are plain JSON scalars, never enum objects.
            assert isinstance(configuration["device_type"], str)
            assert isinstance(configuration["monitoring"], str)
            assert isinstance(configuration["load_entity_ids"], list)
            assert isinstance(configuration["shared_unmodelled"], bool)
        return True

    assert run(_with_server(core, fn))


def test_confidence_tier_is_null_without_a_result_and_mirrors_one(core) -> None:
    """A device with no inference result reports ``None``, never a fake tier."""

    async def fn(session, base):
        async with session.get(f"{base}/devices") as resp:
            before = {d["device_id"]: d for d in await resp.json()}
        assert before["hot_tub"]["confidence_tier"] is None
        assert before["hot_tub"]["latest_result"] is None
        return True

    assert run(_with_server(core, fn))

    # Inject a result carrying a tier and re-read.
    from datetime import UTC, datetime

    from wattmosaic_engine.contracts import ConfidenceTier, InferenceResult

    core.latest_results["hot_tub"] = InferenceResult(
        device_id="hot_tub",
        timestamp=datetime.now(UTC),
        power_w=1800.0,
        source=Source.DIRECT_MEASURED,
        confidence=0.9,
        confidence_tier=ConfidenceTier.TIER_3_MEASURED,
    )

    async def after(session, base):
        async with session.get(f"{base}/devices") as resp:
            devices = {d["device_id"]: d for d in await resp.json()}
        assert devices["hot_tub"]["confidence_tier"] == "tier_3_measured"
        assert devices["hot_tub"]["latest_result"]["confidence_tier"] == "tier_3_measured"
        return True

    assert run(_with_server(core, after))


def test_device_endpoint_carries_the_same_configuration(core) -> None:
    async def fn(session, base):
        async with session.get(f"{base}/devices/hot_tub") as resp:
            assert resp.status == 200
            one = await resp.json()
        assert one["configuration"]["monitoring"] == "dedicated"
        assert one["confidence_tier"] is None
        return True

    assert run(_with_server(core, fn))


def test_configuration_reflects_a_projected_device_configuration(core) -> None:
    """A user-stated configuration must surface through the read API."""
    configuration = DeviceConfiguration(
        device_id="kitchen_lights",
        device_type="light_group",
        monitoring="shared",
        behavior="variable",
        state_entity_ids=["light.kitchen"],
        expected={"on_w": 45.0, "standby_w": 0.0},
    )
    projected = project_configuration(_topology(), [configuration])
    core.db.save_topology(projected)
    core.topology_cache = core.db.load_topology()

    async def fn(session, base):
        async with session.get(f"{base}/devices") as resp:
            devices = {d["device_id"]: d for d in await resp.json()}
        config = devices["kitchen_lights"]["configuration"]
        assert config["device_type"] == "light_group"
        assert config["monitoring"] == "shared"
        assert config["behavior"] == "variable"
        assert config["load_entity_ids"] == ["light.kitchen"]
        assert config["expected_on_w"] == 45.0
        assert config["expected_standby_w"] == 0.0
        # The dedicated device keeps the graph-derived level.
        assert devices["hot_tub"]["configuration"]["monitoring"] == "dedicated"
        return True

    assert run(_with_server(core, fn))


# --------------------------------------------------------------------------- #
# /confidence_tiers
# --------------------------------------------------------------------------- #


def test_confidence_tiers_vocabulary(core) -> None:
    async def fn(session, base):
        async with session.get(f"{base}/confidence_tiers") as resp:
            assert resp.status == 200
            body = await resp.json()
        tiers = body["tiers"]
        assert [t["rank"] for t in tiers] == [0, 1, 2, 3, 4]
        assert [t["tier"] for t in tiers] == [
            "tier_0_unknown",
            "tier_1_modelled",
            "tier_2_inferred",
            "tier_3_measured",
            "tier_4_verified",
        ]
        for tier in tiers:
            assert tier["description"].strip(), tier["tier"]
            assert tier["label"].strip(), tier["tier"]
            assert tier["calibrated"] is False
        # A tier is a grade, never a calibrated probability: said once at the
        # top level too so a client cannot render it as a confidence percentage.
        assert body["calibrated"] is False
        return True

    assert run(_with_server(core, fn))


def test_confidence_tiers_route_is_registered_and_authenticated(core) -> None:
    async def fn(session, base):
        async with (
            aiohttp.ClientSession() as anonymous,
            anonymous.get(f"{base}/confidence_tiers") as resp,
        ):
            assert resp.status == 401
        return True

    assert run(_with_server(core, fn))


# --------------------------------------------------------------------------- #
# Monitoring derived from the CT graph
# --------------------------------------------------------------------------- #


def test_unconfigured_device_monitoring_comes_from_the_ct_graph(core) -> None:
    async def fn(session, base):
        async with session.get(f"{base}/devices") as resp:
            devices = {d["device_id"]: d for d in await resp.json()}
        # A dedicated CT is a dedicated measurement, not "unknown".
        assert devices["hot_tub"]["configuration"]["monitoring"] == "dedicated"
        # A shared branch with an unrelated sibling is not dedicated.
        assert devices["hot_tub"]["configuration"]["device_type"] == "unknown"
        assert devices["hot_tub"]["configuration"]["behavior"] == "unknown"
        return True

    assert run(_with_server(core, fn))
