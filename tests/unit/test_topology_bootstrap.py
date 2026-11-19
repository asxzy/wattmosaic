"""Startup endpoint discovery: a fresh install must not silently do nothing.

Defect #39.  The engine shipped a discovery function that the documentation
promised (``docs/installation.md`` §5: "WattMosaic discovers CT channels and
proposes device mappings") but which nothing ever called.  With nothing stored
the engine inferred nothing, that was never logged, and ``/health`` reported
``ok``.  A real installation therefore looked healthy while emitting no device
at all.

Two independent failures had to be fixed together, and both are covered here
because either one alone leaves the product broken:

1. **Nothing bootstrapped measurement endpoints.**  ``bootstrap_topology`` now
   runs once at startup when the database has no
   :class:`~wattmosaic_engine.contracts.MeasurementEndpoint`, persists
   *candidate* endpoints through the v3 repositories, and logs the outcome.
   Health reports ``topology.configured`` and degrades when it is false.

2. **Discovery itself produced an unusable result.**  Its role classifier
   matched the substring ``"main"``, so every load whose name contained it was
   classified as an incoming service feed: on a real 36-channel installation it
   emitted 21 mains.

The fixtures below are synthetic and installation-neutral.  They encode the
*shape* of a real installation (a monitor with a feed pair, a double-pole load
and separate single circuits) without any household identifier.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from wattmosaic_engine.config.settings import EngineConfig
from wattmosaic_engine.contracts import (
    EndpointKind,
    EndpointScope,
    MeasurementEndpoint,
    ScopeStatus,
)
from wattmosaic_engine.ingestion.discovery import (
    devices_for_candidate_endpoints,
    discover_candidate_endpoints,
    links_for_candidate_endpoints,
)
from wattmosaic_engine.ingestion.ha_source import ConnectionState
from wattmosaic_engine.main import (
    TOPOLOGY_BOOTSTRAP_WAIT_S,
    EngineCore,
)
from wattmosaic_engine.storage.database import Database

# --------------------------------------------------------------------------- #
# Fixtures: a synthetic multi-channel monitor
# --------------------------------------------------------------------------- #


def _power(entity_id: str, value: str = "10.0") -> dict[str, Any]:
    return {
        "entity_id": entity_id,
        "state": value,
        "attributes": {
            "unit_of_measurement": "W",
            "device_class": "power",
            "friendly_name": entity_id,
        },
    }


def _energy(entity_id: str, value: str = "1.0") -> dict[str, Any]:
    return {
        "entity_id": entity_id,
        "state": value,
        "attributes": {
            "unit_of_measurement": "kWh",
            "device_class": "energy",
            "friendly_name": entity_id,
        },
    }


#: A monitor named neutrally, with the entity-id prefix deliberately *different*
#: from its display name (real installations do this).
MONITOR_ID = "monitor_device_1"
MONITOR_NAME = "Basement Panel"

#: Object ids: the panel feed pair, a two-pole load, one three-way light family
#: (which must NOT be paired — that is defect #28), and a plain single circuit.
FIXTURE_ENTITY_IDS = [
    "sensor.basement_panel_ct1_power",
    "sensor.basement_panel_ct2_power",
    "sensor.basement_panel_water_heater_ct1_power",
    "sensor.basement_panel_water_heater_ct2_power",
    "sensor.basement_panel_circuit_ct1_power",
    "sensor.basement_panel_circuit_ct2_power",
    "sensor.basement_panel_circuit_ct3_power",
    "sensor.basement_panel_workshop_plug_power",
    "sensor.basement_panel_total_power",
]


def _registries(platform: str = "some_monitor") -> dict[str, Any]:
    return {
        "entities": [
            {
                "entity_id": entity_id,
                "platform": platform,
                "name": entity_id,
                "device_id": MONITOR_ID,
            }
            for entity_id in FIXTURE_ENTITY_IDS
        ],
        "devices": [{"id": MONITOR_ID, "name": MONITOR_NAME}],
    }


def _states() -> list[dict[str, Any]]:
    return [_power(entity_id) for entity_id in FIXTURE_ENTITY_IDS]


def _discover() -> list[MeasurementEndpoint]:
    return discover_candidate_endpoints(_states(), _registries())


def _feeds(endpoints: list[MeasurementEndpoint]) -> list[MeasurementEndpoint]:
    return [e for e in endpoints if e.scope == EndpointScope.PANEL_FEED]


# --------------------------------------------------------------------------- #
# Discovery must produce *usable* endpoints
# --------------------------------------------------------------------------- #


def test_discovery_never_treats_a_load_named_main_as_a_feed() -> None:
    """The original bug: ``"main"`` in an id made a load look like a feed.

    A load whose name merely contains ``main`` (``main_area_ac``) must stay a
    load.  This is what produced 21 mains out of 36 channels in production.
    """
    states = [
        _power("sensor.panel_ct1_power"),
        _power("sensor.panel_ct2_power"),
        _power("sensor.panel_main_area_ac_power"),
        _power("sensor.panel_main_area_pump_power"),
    ]
    registries = {
        "entities": [
            {"entity_id": s["entity_id"], "device_id": MONITOR_ID, "platform": "m"}
            for s in states
        ],
        "devices": [{"id": MONITOR_ID, "name": MONITOR_NAME}],
    }
    endpoints = discover_candidate_endpoints(states, registries)

    feeds = _feeds(endpoints)
    assert len(feeds) == 2, (
        "only the panel's own ct1/ct2 feed pair is a feed; "
        f"got {[(e.endpoint_id, e.scope) for e in endpoints]}"
    )
    assert {e.entity_id for e in feeds} == {
        "sensor.panel_ct1_power",
        "sensor.panel_ct2_power",
    }
    assert all(
        e.scope == EndpointScope.CIRCUIT
        for e in endpoints
        if "main_area" in (e.entity_id or "")
    ), "a load named 'main_area_*' was classified as a service feed"


def test_discovery_emits_exactly_one_feed_pair() -> None:
    feeds = _feeds(_discover())
    assert len(feeds) == 2
    assert {e.endpoint_id for e in feeds} == {"basement_panel_l1", "basement_panel_l2"}
    assert all(e.kind == EndpointKind.CT for e in feeds)


def test_discovery_emits_every_channel_exactly_once() -> None:
    """The panel ``total`` aggregate is not a circuit.

    Counting it would add a phantom channel equal to the panel total and
    double-count every other CT on the monitor.
    """
    entity_ids = [e.entity_id for e in _discover()]
    assert len(entity_ids) == len(set(entity_ids)), "a channel was emitted twice"
    assert "sensor.basement_panel_total_power" not in entity_ids, (
        "the monitor's own total was discovered as a circuit"
    )
    # Feed pair + water heater pair + lights ct1/ct2/ct3 + workshop plug
    assert len(entity_ids) == 2 + 2 + 3 + 1


def test_discovery_endpoint_ids_are_unique() -> None:
    """Every discovered endpoint must have its own id, or persistence loses a leg."""
    endpoint_ids = [e.endpoint_id for e in _discover()]
    assert len(endpoint_ids) == len(set(endpoint_ids)), sorted(endpoint_ids)


def test_discovery_keeps_both_legs_of_a_two_pole_load_as_circuits() -> None:
    by_entity = {e.entity_id: e for e in _discover()}
    l1 = by_entity["sensor.basement_panel_water_heater_ct1_power"]
    l2 = by_entity["sensor.basement_panel_water_heater_ct2_power"]
    assert l1.scope == EndpointScope.CIRCUIT
    assert l2.scope == EndpointScope.CIRCUIT
    assert l1.panel_id == l2.panel_id == "basement_panel"


def test_discovery_does_not_fuse_three_separate_lighting_circuits() -> None:
    """Defect #28: ``circuit_ct1/ct2/ct3`` are three loads sharing a name stem.

    Pairing the first two would fuse two unrelated circuits into one endpoint
    and leave the third colliding with it.
    """
    lights = [e for e in _discover() if "circuit" in (e.entity_id or "")]
    assert len(lights) == 3
    # A single CT measures one conductor, not one appliance: a circuit CT is
    # never auto single-subject (DEC-05).
    assert all(e.scope == EndpointScope.CIRCUIT for e in lights)
    assert all(e.scope_subject_id is None for e in lights)
    # Every lighting circuit keeps its own leg in the id, so ids stay distinct.
    assert len({e.endpoint_id for e in lights}) == 3


def test_ct_endpoints_never_name_a_device_owner() -> None:
    """A shared branch row must not name one device as its owner.

    ``dedicated_device`` would assert exactly one device on the row, which
    discovery cannot know; on the reference install a one-CT lighting circuit
    resolved seven independent fixtures.  CT endpoints therefore stay circuits
    with no subject, and they never mint a Physical Device (DEC-04).
    """
    endpoints = _discover()
    cts = [e for e in endpoints if e.kind == EndpointKind.CT]
    assert cts
    assert all(e.scope_subject_id is None for e in cts)
    assert devices_for_candidate_endpoints(endpoints, _registries()) == []
    assert links_for_candidate_endpoints(endpoints) == []


def test_discovery_never_turns_an_isolated_smart_plug_into_a_ct() -> None:
    """A smart plug is not CT hardware.

    Promoting ``sensor.washer_power`` to a CT channel would fabricate a circuit
    the installation does not have; it is a candidate single-subject endpoint
    whose ingestion stays off until the user confirms it.
    """
    states = [_power("sensor.washer_power")]
    registries = {
        "entities": [
            {"entity_id": "sensor.washer_power", "device_id": "plug1", "platform": "matter"}
        ],
        "devices": [{"id": "plug1", "name": "Washer Plug"}],
    }
    endpoints = discover_candidate_endpoints(states, registries)
    assert [e.kind for e in endpoints] == [EndpointKind.SMART_PLUG]
    assert endpoints[0].scope == EndpointScope.SINGLE_SUBJECT
    assert endpoints[0].enabled is False


def test_discovery_ignores_energy_only_entities() -> None:
    """kWh counters are not CT channels; watts are."""
    states = [_energy("sensor.basement_panel_energy")]
    registries = {
        "entities": [
            {
                "entity_id": "sensor.basement_panel_energy",
                "device_id": MONITOR_ID,
                "platform": "m",
            }
        ],
        "devices": [{"id": MONITOR_ID, "name": MONITOR_NAME}],
    }
    assert discover_candidate_endpoints(states, registries) == []


def test_discovery_never_proposes_wattmosaic_own_estimates() -> None:
    """ADR-0015 / defect #32: the engine must not measure its own answer."""
    states = [
        _power("sensor.panel_ct1_power"),
        _power("sensor.panel_ct2_power"),
        _power("sensor.fridge_estimated_power"),
        _power("sensor.panel_lights_estimated_power"),
    ]
    registries = {
        "entities": [
            {"entity_id": "sensor.panel_ct1_power", "device_id": MONITOR_ID, "platform": "m"},
            {"entity_id": "sensor.panel_ct2_power", "device_id": MONITOR_ID, "platform": "m"},
            {"entity_id": "sensor.fridge_estimated_power", "device_id": "d2", "platform": "wattmosaic"},
            {"entity_id": "sensor.panel_lights_estimated_power", "device_id": "d3", "platform": "wattmosaic"},
        ],
        "devices": [{"id": MONITOR_ID, "name": MONITOR_NAME}],
    }
    ids = {e.entity_id for e in discover_candidate_endpoints(states, registries)}
    assert "sensor.fridge_estimated_power" not in ids
    assert "sensor.panel_lights_estimated_power" not in ids
    assert "sensor.panel_ct1_power" in ids, "a real CT must still be discovered"


def test_discovery_result_is_a_candidate_never_confirmed() -> None:
    endpoints = _discover()
    assert endpoints
    assert all(e.scope_status == ScopeStatus.CANDIDATE for e in endpoints)


# --------------------------------------------------------------------------- #
# Startup wiring: the part that was entirely missing
# --------------------------------------------------------------------------- #


class _StubSource:
    """Stands in for ``HASource`` without touching a socket."""

    def __init__(self, states: list[dict[str, Any]], registries: dict[str, Any]) -> None:
        self._states = states
        self._registries = registries
        self.waited: list[float] = []

    async def state_snapshot(self) -> list[dict[str, Any]]:
        return list(self._states)

    async def registries(self) -> dict[str, Any]:
        return dict(self._registries)

    async def start(self, **_kwargs: Any) -> None:
        return None

    async def wait_connected(self, timeout: float = 30.0) -> bool:
        self.waited.append(timeout)
        return True


class _LateSource(_StubSource):
    """A source whose states only exist after the first connection is primed.

    This reproduces the real startup ordering: ``hasource.start()`` schedules a
    background task and returns immediately, so the first
    ``state_snapshot()`` can legitimately be empty.
    """

    async def wait_connected(self, timeout: float = 30.0) -> bool:
        self.waited.append(timeout)
        self._states = _states()
        self._registries = _registries()
        return True


class _HealthySource(_StubSource):
    """A source that reports connected, so topology is the only variable."""

    def __init__(self) -> None:
        super().__init__([], {})
        self.connected = True
        self.last_event_at = None
        self.state = ConnectionState.CONNECTED


def _core(tmp_path: Path) -> tuple[EngineCore, Database]:
    db = Database(tmp_path / "wm.db")
    config = EngineConfig(database_path=str(tmp_path / "wm.db"), ha_token="not-used")
    return EngineCore(config, db), db


def _stored_endpoints(tmp_path: Path) -> list[dict[str, Any]]:
    db = Database(tmp_path / "wm.db")
    try:
        return db.repos.entities.endpoints()
    finally:
        db.close()


def _bootstrap(core: EngineCore, db: Database, tmp_path: Path) -> dict[str, Any]:
    core.hasource = _StubSource(_states(), _registries())  # type: ignore[assignment]
    try:
        return asyncio.run(core.bootstrap_topology())
    finally:
        db.close()


def test_bootstrap_stores_candidates_when_the_database_is_empty(tmp_path: Path) -> None:
    core, db = _core(tmp_path)
    result = _bootstrap(core, db, tmp_path)

    assert result.get("status") == "candidate", result
    assert result["discovered"] == 8 and result["ct"] == 8, result

    stored = _stored_endpoints(tmp_path)
    assert stored, "bootstrap did not persist any measurement endpoint"
    assert all(str(row["scope_status"]) == ScopeStatus.CANDIDATE.value for row in stored)
    assert {row["endpoint_id"] for row in stored} >= {"basement_panel_l1", "basement_panel_l2"}


def test_bootstrap_never_overwrites_existing_endpoints(tmp_path: Path) -> None:
    core, db = _core(tmp_path)
    existing = _discover()[0].model_copy(update={"display_name": "operator-supplied"})
    db.repos.entities.upsert_endpoint(existing)

    result = _bootstrap(core, db, tmp_path)
    assert result.get("skipped") == "measurement endpoints already configured", result

    stored = _stored_endpoints(tmp_path)
    assert [row["endpoint_id"] for row in stored] == [existing.endpoint_id]
    assert stored[0]["display_name"] == "operator-supplied", (
        "bootstrap replaced an endpoint that already existed"
    )


def test_bootstrap_accepts_a_monitor_with_only_its_feed_pair(tmp_path: Path) -> None:
    """A monitor exposing only its feed pair is not "no monitor installed"."""
    core, db = _core(tmp_path)
    feeds = ["sensor.panel_ct1_power", "sensor.panel_ct2_power"]
    core.hasource = _StubSource(
        [_power(e) for e in feeds],
        {
            "entities": [
                {"entity_id": e, "device_id": "m1", "platform": "m"} for e in feeds
            ],
            "devices": [{"id": "m1", "name": "Panel"}],
        },
    )  # type: ignore[assignment]
    try:
        result = asyncio.run(core.bootstrap_topology(wait_connected_s=0.0))
    finally:
        db.close()

    assert result.get("status") == "candidate", result
    assert result["ct"] == 2
    assert core.topology_bootstrap_error is None


def test_bootstrap_reports_when_no_monitor_is_present(tmp_path: Path) -> None:
    """No metering hardware is a real condition the user must be told about."""
    core, db = _core(tmp_path)
    core.hasource = _StubSource([_energy("sensor.washer_energy")], {  # type: ignore[assignment]
        "entities": [
            {"entity_id": "sensor.washer_energy", "device_id": "plug1", "platform": "matter"}
        ],
        "devices": [{"id": "plug1", "name": "Washer Plug"}],
    })
    try:
        result = asyncio.run(core.bootstrap_topology())
    finally:
        db.close()

    assert "skipped" in result, result
    assert "monitor" in str(result["skipped"]).lower()
    assert core.topology_bootstrap_error, "the reason must be retained for /health"


def test_bootstrap_can_be_disabled_by_configuration(tmp_path: Path) -> None:
    db = Database(tmp_path / "wm.db")
    config = EngineConfig(
        database_path=str(tmp_path / "wm.db"),
        ha_token="not-used",
        topology_bootstrap=False,
    )
    core = EngineCore(config, db)
    core.hasource = _StubSource(_states(), _registries())  # type: ignore[assignment]
    try:
        result = asyncio.run(core.bootstrap_topology())
    finally:
        db.close()

    assert result == {"skipped": "topology bootstrap disabled by configuration"}
    assert _stored_endpoints(tmp_path) == [], "disabled bootstrap must not store endpoints"


def test_health_reports_a_missing_topology_as_degraded(tmp_path: Path) -> None:
    """The engine must not say ``ok`` while it has nothing to infer.

    Everything else is made healthy here — backend attached, Home Assistant
    connected, database fine — so the *only* possible cause of a non-ok status is
    the missing measurement endpoints.
    """
    core, db = _core(tmp_path)
    try:
        core.inference_backend = object()
        core.hasource = _HealthySource()  # type: ignore[assignment]
        health = core.health()
        assert health.details["topology"]["configured"] is False
        assert health.status != "ok", (
            "an endpoint-less engine reported healthy even though it has nothing "
            "to infer; that is the silent failure the defect was about"
        )
    finally:
        db.close()


def test_health_reports_a_configured_topology(tmp_path: Path) -> None:
    """Positive control: the same check must pass once endpoints exist."""
    core, db = _core(tmp_path)
    try:
        for endpoint in _discover():
            db.repos.entities.upsert_endpoint(endpoint)
        health = core.health()
        topology = health.details["topology"]
        assert topology["configured"] is True
        assert topology["status"] == "candidate"
        assert topology["endpoints"] > 0
    finally:
        db.close()


def test_bootstrap_is_idempotent_across_restarts(tmp_path: Path) -> None:
    """A restart must reuse the discovered endpoints, not duplicate them."""
    core, db = _core(tmp_path)
    first = _bootstrap(core, db, tmp_path)
    assert first.get("status") == "candidate"
    before = _stored_endpoints(tmp_path)

    core2, db2 = _core(tmp_path)
    core2.hasource = _StubSource(_states(), _registries())  # type: ignore[assignment]
    try:
        second = asyncio.run(core2.bootstrap_topology())
    finally:
        db2.close()
    assert second.get("skipped") == "measurement endpoints already configured"
    assert len(_stored_endpoints(tmp_path)) == len(before)


def test_engine_start_runs_topology_bootstrap(tmp_path: Path) -> None:
    """The regression that mattered: startup never called discovery at all.

    Every other test here calls ``bootstrap_topology`` directly, so without this
    one the method could exist and never run — which is exactly the shipped
    defect.
    """
    core, db = _core(tmp_path)
    core.hasource = _StubSource(_states(), _registries())  # type: ignore[assignment]
    try:
        asyncio.run(core.start(start_loops=False))
        assert _stored_endpoints(tmp_path), (
            "engine.start() never bootstrapped measurement endpoints; a fresh "
            "install would ingest every sample and emit nothing"
        )
    finally:
        db.close()


def test_engine_start_waits_for_the_connection_before_discovering(tmp_path: Path) -> None:
    """The regression that mattered at startup: discovery ran too early.

    ``hasource.start()`` returns as soon as the connection task is *scheduled*,
    so an immediate discovery call reads an empty cache.  This drives the real
    ``start()`` path and asserts the bounded wait was actually requested, which
    pinning ``bootstrap_topology`` in isolation cannot catch.
    """
    core, db = _core(tmp_path)
    source = _LateSource([], {})
    core.hasource = source  # type: ignore[assignment]
    try:
        asyncio.run(core.start(start_loops=False))
        assert _stored_endpoints(tmp_path), (
            "engine.start() ran discovery before the first state snapshot arrived"
        )
        assert source.waited, "engine.start() never waited for the first connection"
        assert source.waited[0] > 0, "engine.start() waited for zero seconds"
    finally:
        db.close()


def test_bootstrap_waits_for_the_first_connection_before_discovering(tmp_path: Path) -> None:
    """Startup must wait for the first state snapshot.

    ``hasource.start()`` only schedules the connection and returns, so an
    immediate discovery call sees an empty cache.  Without the bounded wait and
    the deferred outcome, a genuine fresh install would report "no monitor" even
    though one is configured, and would stay endpoint-less until a restart.
    """
    core, db = _core(tmp_path)
    core.hasource = _LateSource([], {})  # type: ignore[assignment]
    try:
        result = asyncio.run(
            core.bootstrap_topology(wait_connected_s=TOPOLOGY_BOOTSTRAP_WAIT_S)
        )
    finally:
        db.close()

    assert result.get("status") == "candidate", result
    assert result["discovered"] > 0
    assert core.hasource.waited, "startup did not wait for the first connection"


def test_bootstrap_defers_instead_of_claiming_no_monitor(tmp_path: Path) -> None:
    """An empty state list is a connection problem, not missing hardware.

    Reporting "no multi-channel energy monitor" here would send the user hunting
    for a CT monitor they already own.  The two conditions must be distinguishable.
    """
    core, db = _core(tmp_path)
    core.hasource = _StubSource([], {})  # type: ignore[assignment]
    try:
        result = asyncio.run(core.bootstrap_topology(wait_connected_s=0.0))
    finally:
        db.close()

    assert "deferred" in result, result
    assert "monitor" not in str(result["deferred"]).lower(), (
        "an unreachable Home Assistant was reported as missing hardware"
    )
    assert "home assistant" in str(result["deferred"]).lower()


def test_discovery_is_retried_once_the_connection_appears(tmp_path: Path) -> None:
    """Endpoints must not be lost forever because startup lost the race.

    The engine keeps serving while Home Assistant is down or slow.  Once states
    become available, the maintenance retry must bootstrap rather than stay
    silent indefinitely.
    """
    core, db = _core(tmp_path)
    # Startup: Home Assistant is not up yet, so discovery defers.
    core.hasource = _StubSource([], {})  # type: ignore[assignment]
    try:
        asyncio.run(core.bootstrap_topology(wait_connected_s=0.0))
        assert db.repos.entities.endpoints() == []
        assert core.topology_bootstrap_error

        # The connection comes up later; the next retry must recover.
        core.hasource = _StubSource(_states(), _registries())  # type: ignore[assignment]
        core._last_topology_attempt = -1e12  # bypass the rate limit for this retry
        result = asyncio.run(core.maybe_bootstrap_topology())

        assert result is not None and result.get("status") == "candidate", result
        assert db.repos.entities.endpoints(), (
            "the engine never recovered after Home Assistant came up"
        )
        assert core.topology_bootstrap_error is None
    finally:
        db.close()


def test_discovery_retry_is_rate_limited(tmp_path: Path) -> None:
    """Discovery scans every state, so it must not run on every 5 s cycle."""
    core, db = _core(tmp_path)
    core.hasource = _StubSource([], {})  # type: ignore[assignment]
    try:
        first = asyncio.run(core.maybe_bootstrap_topology())
        second = asyncio.run(core.maybe_bootstrap_topology())
        assert first is not None, "the first attempt must run"
        assert second is None, "retries were not rate limited"
    finally:
        db.close()


def test_retry_is_a_no_op_once_endpoints_exist(tmp_path: Path) -> None:
    core, db = _core(tmp_path)
    core.hasource = _StubSource(_states(), _registries())  # type: ignore[assignment]
    try:
        db.repos.entities.upsert_endpoint(_discover()[0])
        assert asyncio.run(core.maybe_bootstrap_topology()) is None
    finally:
        db.close()
