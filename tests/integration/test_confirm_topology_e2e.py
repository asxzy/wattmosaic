"""End-to-end: the confirm button actually unblocks model learning.

**Why this file exists**

Round 2 added ``button.wattmosaic_confirm_topology`` to fix a live dead end: the
topology was a ``candidate``, so every learning sample was rejected as
``ambiguous_topology``, ``device_models`` stayed empty and
``model_estimated_power`` stayed 0 W forever.  ``POST /topology/confirm`` had
always existed, but nothing could flip ``CONF_TOPOLOGY_CONFIRMED`` after setup.

Each link of that chain was tested in isolation (``test_topology_push.py`` pins
the replay, ``test_learning_status.py`` pins the decision logic against a
recording fake).  What was *not* proven is that the links compose: a press on the
real HA client, against the real engine HTTP server, writing the real SQLite
database, producing a status the real sample filter accepts.

This module closes that gap.  It runs a real ``aiohttp`` server on an ephemeral
loopback port with the real ``EngineCore`` and database; the only fake is Home
Assistant itself, which is not installed here.  No external network.

The chain under test::

    press
      -> async_confirm_topology   (custom_components/wattmosaic/topology_confirm.py)
      -> async_replay_topology    (PUT /topology with confirmed=True)
      -> POST /topology/confirm
      -> engine saves a CONFIRMED topology (persisted, reloadable)
      -> confirmed_entry_updates sets CONF_TOPOLOGY_CONFIRMED=True
      -> the engine's learning-quality stamp becomes "confirmed"
      -> the sample filter stops rejecting as ambiguous_topology
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import sys
import types
from pathlib import Path
from typing import Any

import aiohttp
import pytest
from wattmosaic_engine import contracts
from wattmosaic_engine.contracts import (
    CTChannel,
    CTType,
    DeviceTopology,
    Phase,
    Topology,
    TopologyStatus,
)
from wattmosaic_engine.storage import Database

PACKAGE_DIR = (
    Path(__file__).resolve().parents[2] / "custom_components" / "wattmosaic"
)
PACKAGE_NAME = "wm_confirm_e2e_under_test"
TOKEN = "confirm-e2e-token"


def _load_package() -> types.ModuleType:
    """A handle to the integration package without executing ``__init__``.

    ``__init__.py`` imports ``homeassistant``, which is not installed in this
    environment.  The pure modules this test exercises do not.
    """
    if PACKAGE_NAME in sys.modules:
        return sys.modules[PACKAGE_NAME]
    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME,
        PACKAGE_DIR / "__init__.py",
        submodule_search_locations=[str(PACKAGE_DIR)],
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [str(PACKAGE_DIR)]  # type: ignore[attr-defined]
    sys.modules[PACKAGE_NAME] = module
    return module


_load_package()

flow = importlib.import_module(f"{PACKAGE_NAME}.flow_logic")
confirm = importlib.import_module(f"{PACKAGE_NAME}.topology_confirm")
client_module = importlib.import_module(f"{PACKAGE_NAME}.api_client")


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# The production shape: nested mains stored as parentless candidates
# --------------------------------------------------------------------------- #


def _stored_topology() -> Topology:
    """Six physically nested mains, all parentless -- the measured live defect.

    Two panels are aliases of the load manager on the same conductor, and two
    sub-panels are children of it.  Without those links the engine sums six
    parallel mains and reports roughly double the real draw.
    """
    tailor = dict(ct_type=CTType.MAIN)
    return Topology(
        status=TopologyStatus.CANDIDATE,
        channels=[
            CTChannel(ct_id="main_load_manager_l1", entity_id="sensor.mm_l1",
                      phase=Phase.L1, **tailor),
            CTChannel(ct_id="main_load_manager_l2", entity_id="sensor.mm_l2",
                      phase=Phase.L2, **tailor),
            CTChannel(ct_id="refoss_main_panel_l1", entity_id="sensor.pm_l1",
                      phase=Phase.L1, **tailor),
            CTChannel(ct_id="refoss_main_panel_l2", entity_id="sensor.pm_l2",
                      phase=Phase.L2, **tailor),
            CTChannel(ct_id="refoss_sub_panel_l1", entity_id="sensor.sp_l1",
                      phase=Phase.L1, **tailor),
            CTChannel(ct_id="refoss_sub_panel_l2", entity_id="sensor.sp_l2",
                      phase=Phase.L2, **tailor),
        ],
        devices=[DeviceTopology(device_id="dev_1", name="Load manager")],
    )


def _assignments() -> dict[str, dict[str, Any]]:
    """The user's declaration, exactly as the config entry stores it."""
    return {
        "main_load_manager_l1": {"role": "main", "phase": "l1"},
        "main_load_manager_l2": {"role": "main", "phase": "l2"},
        "refoss_main_panel_l1": {
            "role": "main", "phase": "l1",
            "alias_of_ct_id": "main_load_manager_l1",
        },
        "refoss_main_panel_l2": {
            "role": "main", "phase": "l2",
            "alias_of_ct_id": "main_load_manager_l2",
        },
        "refoss_sub_panel_l1": {
            "role": "main", "phase": "l1",
            "parent_ct_id": "main_load_manager_l1",
        },
        "refoss_sub_panel_l2": {
            "role": "main", "phase": "l2",
            "parent_ct_id": "main_load_manager_l2",
        },
    }


class _Harness:
    """A real engine (core + SQLite + HTTP) plus a real integration client."""

    def __init__(self, db: Database, core: Any, server: Any, client: Any, base_url: str) -> None:
        self.db = db
        self.core = core
        self.server = server
        self.client = client
        self.base_url = base_url

    async def aclose(self) -> None:
        await self.server.stop()


async def _build_harness(tmp_path: Path) -> _Harness:
    from wattmosaic_engine.api.server import APIServer
    from wattmosaic_engine.config.settings import EngineConfig
    from wattmosaic_engine.main import EngineCore

    db = Database(tmp_path / "confirm.db")
    db.save_topology(_stored_topology())

    config = EngineConfig(
        database_path=str(tmp_path / "confirm.db"), ha_token="not-used", api_token=TOKEN
    )
    core = EngineCore(config, db)
    core.topology_cache = db.load_topology()

    server = APIServer(core, host="127.0.0.1", port=0, api_token=TOKEN)
    await server.start()
    base_url = f"http://127.0.0.1:{server.port}"
    session = aiohttp.ClientSession()
    client = client_module.EngineClient(session, base_url, api_token=TOKEN)
    return _Harness(db, core, server, client, base_url)


def _with_harness(tmp_path: Path, scenario) -> Any:
    async def main() -> Any:
        harness = await _build_harness(tmp_path)
        try:
            return await scenario(harness)
        finally:
            session = getattr(harness.client, "_session", None)
            await harness.aclose()
            if session is not None:
                await session.close()
            harness.db.close()

    return run(main())


# --------------------------------------------------------------------------- #
# The chain
# --------------------------------------------------------------------------- #


def test_a_press_confirms_the_topology_on_the_real_engine(tmp_path: Path) -> None:
    """press -> PUT -> POST /topology/confirm -> a CONFIRMED stored topology."""

    async def scenario(harness: _Harness) -> Any:
        state = flow.TopologyPushState()

        # Ordinary coordinator polls first: the declaration is replayed as a
        # candidate (the entry was never confirmed), and the replay settles.
        # This is the state the live install was stuck in.
        for _ in range(3):
            observed = (harness.core.topology().model_dump(mode='json'))["version"]
            await flow.async_replay_topology(
                harness.client,
                _assignments(),
                confirmed=False,
                stored_version=1,
                observed_version=observed,
                state=state,
            )
        before = harness.core.topology().model_dump(mode='json')
        assert before["status"] == "candidate", (
            "a non-confirming poll must never confirm the topology"
        )

        # The user presses button.wattmosaic_confirm_topology.
        result = await confirm.async_confirm_topology(
            harness.client,
            _assignments(),
            stored_version=1,
            state=state,
            observed_version=before["version"],
        )

        after = harness.core.topology().model_dump(mode='json')
        reread = harness.db.load_topology()
        health = harness.core.health()
        return result, before, after, reread, health

    result, before, after, reread, health = _with_harness(tmp_path, scenario)

    assert result.ok is True, result.message
    assert result.entry_confirmed is True
    assert result.status == "confirmed"
    assert after["status"] == "confirmed"
    assert after["version"] > before["version"], "the confirmation created a revision"

    # Persisted, not merely in memory: a restart must not undo it.
    assert reread is not None
    assert reread.status == TopologyStatus.CONFIRMED

    # And the health payload the HA coordinator reads agrees.
    assert health.details["topology"]["status"] == "confirmed"
    assert health.details["topology"]["version"] == after["version"]


def test_a_press_after_settled_polls_is_not_swallowed(tmp_path: Path) -> None:
    """The exact live sequence, over HTTP.

    The button shares the coordinator's ``TopologyPushState``.  Once the
    declaration matches, polls settle on ``reason="engine_matches"`` and the
    idempotency guard caches that verdict.  If the guard ignored the
    ``confirmed=True`` intent of the press, the button would report "nothing was
    written", the engine would stay ``candidate`` and learning would stay
    blocked -- the defect this round exists to remove.
    """

    async def scenario(harness: _Harness) -> Any:
        state = flow.TopologyPushState()
        # Replay the declaration once, so the engine matches it and the guard
        # has something to settle on.
        first = harness.core.topology().model_dump(mode='json')
        await flow.async_replay_topology(
            harness.client,
            _assignments(),
            confirmed=False,
            stored_version=1,
            observed_version=first["version"],
            state=state,
        )
        settled = harness.core.topology().model_dump(mode='json')
        for _ in range(4):
            await flow.async_replay_topology(
                harness.client,
                _assignments(),
                confirmed=False,
                stored_version=1,
                observed_version=settled["version"],
                state=state,
            )
        reason_before_press = state.reason

        result = await confirm.async_confirm_topology(
            harness.client,
            _assignments(),
            stored_version=1,
            state=state,
            observed_version=settled["version"],
        )
        return result, reason_before_press, harness.core.topology().model_dump(mode='json')

    result, reason_before_press, after = _with_harness(tmp_path, scenario)

    # The guard has settled on this exact revision: a cached verdict exists and
    # it is not "needs to write".  That is precisely the cache the press must
    # not be answered from.
    assert reason_before_press in ("engine_matches", "in_sync", "declaration_missing")
    assert result.ok is True, result.message
    assert result.attempted is True, "the press must actually write"
    assert after["status"] == "confirmed"


def test_a_second_press_writes_nothing(tmp_path: Path) -> None:
    """Idempotent: a confirmed engine is never rewritten."""

    async def scenario(harness: _Harness) -> Any:
        state = flow.TopologyPushState()
        first = harness.core.topology().model_dump(mode='json')
        one = await confirm.async_confirm_topology(
            harness.client, _assignments(), stored_version=1, state=state,
            observed_version=first["version"],
        )
        version_after_first = (harness.core.topology().model_dump(mode='json'))["version"]
        two = await confirm.async_confirm_topology(
            harness.client, _assignments(), stored_version=1, state=state,
            observed_version=version_after_first,
        )
        return one, two, version_after_first, (harness.core.topology().model_dump(mode='json'))["version"]

    one, two, version_after_first, version_after_two = _with_harness(tmp_path, scenario)
    assert one.ok is True
    assert two.ok is True
    assert two.entry_confirmed is True, "the entry flag stays true on a repeat press"
    assert version_after_two == version_after_first, "a second press created a revision"


def test_the_confirmed_topology_unblocks_the_sample_filter(tmp_path: Path) -> None:
    """The last link: the engine stamps ``confirmed`` and samples are accepted.

    ``WattMosaicBackend._topology_quality`` decides what every learning sample
    is stamped with.  It reads the topology status the engine just confirmed.
    A ``candidate`` stamp is rejected as ``ambiguous_topology``; ``confirmed``
    is not.  This asserts the two facts the whole round depends on, using the
    value the engine really produces after the press.
    """

    async def scenario(harness: _Harness) -> Any:
        state = flow.TopologyPushState()
        first = harness.core.topology().model_dump(mode='json')
        await confirm.async_confirm_topology(
            harness.client, _assignments(), stored_version=1, state=state,
            observed_version=first["version"],
        )
        return harness.core.topology().model_dump(mode='json')

    topology_view = _with_harness(tmp_path, scenario)
    assert topology_view["status"] == "confirmed"

    from wattmosaic_engine.learning import LearningPipeline
    from wattmosaic_engine.learning.sample_filter import SampleFilter

    contract_topology = Topology.model_validate(topology_view)
    assert contract_topology.status == TopologyStatus.CONFIRMED

    # The same helper the backend uses to stamp samples.
    from wattmosaic_engine.integration import WattMosaicBackend

    quality = WattMosaicBackend._topology_quality(contract_topology)
    assert quality == "confirmed", "the sample stamp follows the engine's status"

    # And the filter accepts a sample carrying that stamp, while the candidate
    # stamp of the same sample is rejected.  The rejection *rules* are
    # untouched -- only the input changed, which is the point.
    from tests.unit.conftest import BASE_TIME, make_sample

    sample = make_sample(
        "dev", 0, baseline=10.0, event=110.0, state="on", captured_at=BASE_TIME
    )
    confirmed_sample = sample.model_copy(
        update={"notes": [*sample.notes, "topology_quality:confirmed"]}
    )
    candidate_sample = sample.model_copy(
        update={"notes": [*sample.notes, "topology_quality:candidate"]}
    )
    # A fresh filter per evaluation: ``SampleFilter`` remembers the last state it
    # saw, so reusing one instance would reject the second sample as a
    # ``duplicate_state`` before it ever looked at the topology.
    assert SampleFilter().evaluate(confirmed_sample).accepted is True
    assert (
        SampleFilter().evaluate(candidate_sample).reason
        == contracts.SampleRejection.AMBIGUOUS_TOPOLOGY
    )

    # A real pipeline carries the counters the health endpoint reports.
    db = Database(Path(str(tmp_path)) / "learning-accounting.db")
    try:
        pipeline = LearningPipeline(db)
        accepted, rejected = pipeline.ingest_samples([confirmed_sample])
        assert len(accepted) == 1 and rejected == []
        snapshot = pipeline.accounting.snapshot()
        assert snapshot["samples_accepted"] == 1
        assert snapshot["samples_rejected"] == 0
    finally:
        db.close()


def test_the_entry_flag_is_written_where_it_lives(tmp_path: Path) -> None:
    """``CONF_TOPOLOGY_CONFIRMED=True`` lands in the container that holds it.

    The coordinator reads ``{**data, **options}``, so writing only the wrong
    container would be silently overridden and the reload would replay the
    declaration as a candidate again.
    """

    async def scenario(harness: _Harness) -> Any:
        state = flow.TopologyPushState()
        first = harness.core.topology().model_dump(mode='json')
        return await confirm.async_confirm_topology(
            harness.client, _assignments(), stored_version=1, state=state,
            observed_version=first["version"],
        )

    result = _with_harness(tmp_path, scenario)
    assert result.entry_confirmed is True

    const = importlib.import_module(f"{PACKAGE_NAME}.const")
    data, options = confirm.confirmed_entry_updates(
        {"topology_confirmed": False, "topology_version": 1}, {}, version=result.version
    )
    assert data[const.CONF_TOPOLOGY_CONFIRMED] is True
    assert data[const.CONF_TOPOLOGY_VERSION] == result.version
    assert const.CONF_TOPOLOGY_CONFIRMED not in options

    data2, options2 = confirm.confirmed_entry_updates(
        {}, {"topology_confirmed": False, "topology_version": 1}, version=result.version
    )
    assert options2[const.CONF_TOPOLOGY_CONFIRMED] is True
    assert options2[const.CONF_TOPOLOGY_VERSION] == result.version
    assert const.CONF_TOPOLOGY_CONFIRMED not in data2


def test_a_reload_does_not_downgrade_the_confirmed_topology(tmp_path: Path) -> None:
    """entry reload -> replay with ``confirmed=True`` -> no write, still confirmed.

    After the press, ``__init__``'s update listener reloads the entry and the
    coordinator replays the declaration with the now-true flag.  That replay
    must be a no-op on a confirmed engine: rewriting would create a needless
    revision, and demoting would re-block learning.
    """

    async def scenario(harness: _Harness) -> Any:
        state = flow.TopologyPushState()
        first = harness.core.topology().model_dump(mode='json')
        await confirm.async_confirm_topology(
            harness.client, _assignments(), stored_version=1, state=state,
            observed_version=first["version"],
        )
        confirmed = harness.core.topology().model_dump(mode='json')
        replay_state = flow.TopologyPushState()
        # The fresh coordinator state after a reload, with the entry flag true.
        for _ in range(3):
            await flow.async_replay_topology(
                harness.client,
                _assignments(),
                confirmed=True,
                stored_version=confirmed["version"],
                observed_version=confirmed["version"],
                state=replay_state,
            )
        return confirmed, harness.core.topology().model_dump(mode='json'), replay_state

    confirmed, after, replay_state = _with_harness(tmp_path, scenario)
    assert after["status"] == "confirmed"
    assert after["version"] == confirmed["version"], "the reload created a revision"
    assert replay_state.error is None


@pytest.mark.parametrize("path", ["/topology", "/health"])
def test_the_engine_contract_version_is_1_4_0(tmp_path: Path, path: str) -> None:
    """One contract version across the engine, its schemas and the client."""

    async def scenario(harness: _Harness) -> Any:
        async with harness.client._session.get(
            f"{harness.base_url}/api/v1{path}", headers={"X-API-Token": TOKEN}
        ) as response:
            return response.headers.get("X-WattMosaic-Contract-Version"), await response.json()

    header, body = _with_harness(tmp_path, scenario)
    assert header == contracts.CONTRACT_VERSION == "1.5.0"
    if "contract_version" in body:
        assert body["contract_version"] == "1.5.0"
