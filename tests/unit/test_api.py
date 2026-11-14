"""Unit tests for the HTTP API: auth, contract header, errors, endpoints.

Uses a real local server on an ephemeral port (no external network) and an
in-memory ``StubFacade``/``NullFacade``; the inference engine is never imported.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

import aiohttp
from wattmosaic_engine.api.facade import (
    FacadeUnavailable,
    InvalidRequest,
    NullFacade,
    StubFacade,
)
from wattmosaic_engine.api.server import APIServer, create_app
from wattmosaic_engine.contracts import (
    CONTRACT_VERSION,
    ErrorResponse,
    HealthResponse,
    ServiceCommand,
    ServiceResult,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

TOKEN = "test-token"
HEADERS = {"X-API-Token": TOKEN}


class FailingFacade(StubFacade):
    """A facade whose methods raise, to exercise error mapping."""

    def __init__(self) -> None:
        super().__init__()
        self.fail_mode = "unavailable"

    def status(self) -> Any:
        if self.fail_mode == "unavailable":
            raise FacadeUnavailable("backend missing")
        if self.fail_mode == "invalid":
            raise InvalidRequest("bad input")
        raise RuntimeError("boom")


def run(coro):
    return asyncio.run(coro)


async def _with_server(facade, fn, *, token: str = TOKEN, **server_kwargs):
    server = APIServer(facade, host="127.0.0.1", port=0, api_token=token, **server_kwargs)
    await server.start()
    port = server.port
    assert port > 0
    try:
        async with aiohttp.ClientSession() as session:
            return await fn(session, f"http://127.0.0.1:{port}/api/v1")
    finally:
        await server.stop()


# --------------------------------------------------------------------------- #
# Health / auth / headers
# --------------------------------------------------------------------------- #


def test_health_ok_and_contract_header() -> None:
    async def fn(session, base):
        async with session.get(f"{base}/health", headers=HEADERS) as resp:
            assert resp.status == 200
            assert resp.headers["X-WattMosaic-Contract-Version"] == CONTRACT_VERSION
            body = await resp.json()
            HealthResponse.model_validate(body)
            assert body["status"] == "ok"
        return True

    assert run(_with_server(StubFacade(), fn))


def test_no_token_configured_allows_anonymous() -> None:
    async def fn(session, base):
        async with session.get(f"{base}/health") as resp:
            assert resp.status == 200
            assert resp.headers["X-WattMosaic-Contract-Version"] == CONTRACT_VERSION
        return True

    assert run(_with_server(StubFacade(), fn, token=""))


def test_missing_and_wrong_token_rejected() -> None:
    async def fn(session, base):
        async with session.get(f"{base}/health") as resp:
            assert resp.status == 401
            body = await resp.json()
            ErrorResponse.model_validate(body)
            assert body["error"] == "unauthorized"
            assert resp.headers["X-WattMosaic-Contract-Version"] == CONTRACT_VERSION
        async with session.get(f"{base}/health", headers={"X-API-Token": "nope"}) as resp:
            assert resp.status == 401
        return True

    assert run(_with_server(StubFacade(), fn))


def test_token_compare_is_constant_time() -> None:
    """A same-length near-miss token is still rejected (behavioural check)."""
    near = TOKEN[:-1] + "X"

    async def fn(session, base):
        async with session.get(f"{base}/health", headers={"X-API-Token": near}) as resp:
            assert resp.status == 401
        return True

    assert run(_with_server(StubFacade(), fn))


# --------------------------------------------------------------------------- #
# Null facade → 503
# --------------------------------------------------------------------------- #


def test_null_facade_returns_503_but_health_degrades() -> None:
    async def fn(session, base):
        async with session.get(f"{base}/health", headers=HEADERS) as resp:
            assert resp.status == 200
            body = await resp.json()
            assert body["status"] == "degraded"
            assert body["details"]["reason"] == "inference backend not installed"
        async with session.get(f"{base}/snapshot", headers=HEADERS) as resp:
            assert resp.status == 503
            body = await resp.json()
            ErrorResponse.model_validate(body)
            assert body["retryable"] is True
        return True

    assert run(_with_server(NullFacade(), fn))


# --------------------------------------------------------------------------- #
# Read endpoints
# --------------------------------------------------------------------------- #


def test_status_snapshot_devices_quality_diagnostics() -> None:
    async def fn(session, base):
        for path in ("status", "snapshot", "devices", "quality", "diagnostics", "topology"):
            async with session.get(f"{base}/{path}", headers=HEADERS) as resp:
                assert resp.status == 200, f"{path}: {await resp.text()}"
                await resp.json()
        return True

    assert run(_with_server(StubFacade(), fn))


def test_devices_returns_list() -> None:
    async def fn(session, base):
        async with session.get(f"{base}/devices", headers=HEADERS) as resp:
            assert resp.status == 200
            assert isinstance(await resp.json(), list)
        return True

    assert run(_with_server(StubFacade(), fn))


def test_unknown_device_returns_structured_404() -> None:
    async def fn(session, base):
        async with session.get(f"{base}/devices/nope", headers=HEADERS) as resp:
            assert resp.status == 404
            body = await resp.json()
            ErrorResponse.model_validate(body)
            assert body["error"] == "not_found"
            assert resp.headers["X-WattMosaic-Contract-Version"] == CONTRACT_VERSION
        return True

    assert run(_with_server(StubFacade(), fn))


def test_device_returning_none_is_404_not_null() -> None:
    class NoneFacade(StubFacade):
        def device(self, device_id: str) -> None:
            return None

        def models(self, device_id: str) -> None:
            return None

    async def fn(session, base):
        for path in ("devices/anything", "models/anything"):
            async with session.get(f"{base}/{path}", headers=HEADERS) as resp:
                assert resp.status == 404, path
                body = await resp.json()
                assert "error" in body
                assert body["error"] == "not_found"
                assert body.get("detail")
        return True

    assert run(_with_server(NoneFacade(), fn))


def test_models_endpoint_stub() -> None:
    async def fn(session, base):
        async with session.get(f"{base}/models/dev1", headers=HEADERS) as resp:
            assert resp.status == 200
            body = await resp.json()
            assert body["device_id"] == "dev1"
            assert "versions" in body
        return True

    assert run(_with_server(StubFacade(), fn))


def test_export_endpoint() -> None:
    async def fn(session, base):
        async with session.get(f"{base}/export/dev1", headers=HEADERS) as resp:
            assert resp.status == 200
            body = await resp.json()
            assert body["device_id"] == "dev1"
        return True

    assert run(_with_server(StubFacade(), fn))


# --------------------------------------------------------------------------- #
# Topology
# --------------------------------------------------------------------------- #


def test_topology_get_put_confirm_roundtrip() -> None:
    async def fn(session, base):
        async with session.get(f"{base}/topology", headers=HEADERS) as resp:
            topology = await resp.json()
        assert topology["status"] == "candidate"
        topology["notes"] = ["updated"]
        async with session.put(f"{base}/topology", json=topology, headers=HEADERS) as resp:
            assert resp.status == 200, await resp.text()
            updated = await resp.json()
        assert updated["notes"] == ["updated"]
        async with session.post(f"{base}/topology/confirm", headers=HEADERS) as resp:
            assert resp.status == 200
            confirmed = await resp.json()
        assert confirmed["status"] == "confirmed"
        return True

    assert run(_with_server(StubFacade(), fn))


def test_topology_put_invalid_body_is_400() -> None:
    async def fn(session, base):
        async with session.put(
            f"{base}/topology", json={"channels": "not-a-list"}, headers=HEADERS
        ) as resp:
            assert resp.status == 400
            body = await resp.json()
            ErrorResponse.model_validate(body)
            assert body["error"] == "bad_request"
            assert body["detail"]
        return True

    assert run(_with_server(StubFacade(), fn))


def test_topology_put_malformed_json_is_400() -> None:
    async def fn(session, base):
        async with session.put(
            f"{base}/topology",
            data="{not json",
            headers={**HEADERS, "Content-Type": "application/json"},
        ) as resp:
            assert resp.status == 400
            body = await resp.json()
            assert body["error"] == "bad_request"
        return True

    assert run(_with_server(StubFacade(), fn))


def test_topology_put_empty_object_is_400() -> None:
    """An empty body must not silently replace the topology (defaults exist)."""

    async def fn(session, base):
        async with session.get(f"{base}/topology", headers=HEADERS) as resp:
            before = await resp.json()
        async with session.put(f"{base}/topology", json={}, headers=HEADERS) as resp:
            assert resp.status == 400
            body = await resp.json()
            ErrorResponse.model_validate(body)
            assert body["error"] == "bad_request"
            assert body["detail"]
        # The stored topology must be untouched.
        async with session.get(f"{base}/topology", headers=HEADERS) as resp:
            assert await resp.json() == before
        return True

    assert run(_with_server(StubFacade(), fn))


def test_topology_put_empty_channels_is_400() -> None:
    async def fn(session, base):
        async with session.put(
            f"{base}/topology",
            json={"channels": [], "devices": [{"device_id": "d1"}]},
            headers=HEADERS,
        ) as resp:
            assert resp.status == 400
            body = await resp.json()
            ErrorResponse.model_validate(body)
            assert body["error"] == "bad_request"
            assert "channel" in body["detail"]
        return True

    assert run(_with_server(StubFacade(), fn))


def test_topology_put_without_main_channel_is_400() -> None:
    payload = {
        "channels": [
            {"ct_id": "br1", "entity_id": "sensor.br1", "ct_type": "branch"},
        ],
        "devices": [{"device_id": "d1", "branch_ct_ids": ["br1"]}],
    }

    async def fn(session, base):
        async with session.put(f"{base}/topology", json=payload, headers=HEADERS) as resp:
            assert resp.status == 400
            body = await resp.json()
            assert body["error"] == "bad_request"
            assert "main" in body["detail"]
        return True

    assert run(_with_server(StubFacade(), fn))


def test_topology_put_without_devices_is_400() -> None:
    payload = {
        "channels": [{"ct_id": "main_l1", "entity_id": "sensor.m1", "ct_type": "main"}],
        "devices": [],
    }

    async def fn(session, base):
        async with session.put(f"{base}/topology", json=payload, headers=HEADERS) as resp:
            assert resp.status == 400
            body = await resp.json()
            assert body["error"] == "bad_request"
            assert "device" in body["detail"]
        return True

    assert run(_with_server(StubFacade(), fn))


def test_topology_put_accepts_valid_minimal_topology() -> None:
    payload = {
        "channels": [
            {"ct_id": "main_l1", "entity_id": "sensor.m1", "ct_type": "main", "phase": "l1"},
            {"ct_id": "br_fridge", "entity_id": "sensor.br1", "ct_type": "branch",
             "phase": "l1", "parent_ct_id": "main_l1"},
        ],
        "devices": [{"device_id": "dev_fridge", "branch_ct_ids": ["br_fridge"]}],
    }

    async def fn(session, base):
        async with session.put(f"{base}/topology", json=payload, headers=HEADERS) as resp:
            assert resp.status == 200, await resp.text()
            stored = await resp.json()
        assert [c["ct_id"] for c in stored["channels"]] == ["main_l1", "br_fridge"]
        assert stored["devices"][0]["device_id"] == "dev_fridge"
        return True

    assert run(_with_server(StubFacade(), fn))


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def test_command_roundtrip_and_stub_recording() -> None:
    facade = StubFacade()

    async def fn(session, base):
        async with session.post(
            f"{base}/commands", json={"command": "start_learning"}, headers=HEADERS
        ) as resp:
            assert resp.status == 200
            body = await resp.json()
            ServiceResult.model_validate(body)
            assert body["ok"] is True
            assert body["command"] == "start_learning"
        return True

    assert run(_with_server(facade, fn))
    assert facade.commands and isinstance(facade.commands[0], ServiceCommand)


def test_command_invalid_value_is_400_bad_request() -> None:
    async def fn(session, base):
        async with session.post(
            f"{base}/commands", json={"command": "not_a_command"}, headers=HEADERS
        ) as resp:
            assert resp.status == 400
            body = await resp.json()
            ErrorResponse.model_validate(body)
            assert body["error"] == "bad_request"
            assert "detail" in body
        return True

    assert run(_with_server(StubFacade(), fn))


def test_command_missing_body_is_400() -> None:
    async def fn(session, base):
        async with session.post(f"{base}/commands", headers=HEADERS) as resp:
            assert resp.status == 400
            body = await resp.json()
            assert body["error"] == "bad_request"
        return True

    assert run(_with_server(StubFacade(), fn))


def test_command_validation_never_returns_422() -> None:
    async def fn(session, base):
        payloads = [
            {"command": "nope"},
            {"command": "rebuild_model", "parameters": "not-an-object"},
            {"device_id": "x"},
        ]
        for payload in payloads:
            async with session.post(f"{base}/commands", json=payload, headers=HEADERS) as resp:
                assert resp.status == 400, (payload, resp.status)
                assert resp.status != 422
        return True

    assert run(_with_server(StubFacade(), fn))


# --------------------------------------------------------------------------- #
# Error mapping
# --------------------------------------------------------------------------- #


def test_facade_unavailable_maps_to_503() -> None:
    async def fn(session, base):
        async with session.get(f"{base}/status", headers=HEADERS) as resp:
            assert resp.status == 503
            body = await resp.json()
            assert body["error"] == "engine_unavailable"
            assert body["retryable"] is True
        return True

    assert run(_with_server(FailingFacade(), fn))


def test_invalid_request_maps_to_400() -> None:
    facade = FailingFacade()
    facade.fail_mode = "invalid"

    async def fn(session, base):
        async with session.get(f"{base}/status", headers=HEADERS) as resp:
            assert resp.status == 400
            body = await resp.json()
            assert body["error"] == "bad_request"
        return True

    assert run(_with_server(facade, fn))


def test_unexpected_error_maps_to_500_without_leaking() -> None:
    facade = FailingFacade()
    facade.fail_mode = "boom"

    async def fn(session, base):
        async with session.get(f"{base}/status", headers=HEADERS) as resp:
            assert resp.status == 500
            body = await resp.json()
            assert body["error"] == "internal_error"
            assert "boom" not in (body.get("detail") or "")
        return True

    assert run(_with_server(facade, fn))


def test_unknown_route_is_json_error() -> None:
    async def fn(session, base):
        async with session.get(f"{base}/does-not-exist", headers=HEADERS) as resp:
            assert resp.status == 404
            assert resp.headers["X-WattMosaic-Contract-Version"] == CONTRACT_VERSION
        return True

    assert run(_with_server(StubFacade(), fn))


def test_wrong_method_is_json_error() -> None:
    async def fn(session, base):
        async with session.delete(f"{base}/health", headers=HEADERS) as resp:
            assert resp.status == 405
            body = await resp.json()
            assert "error" in body
        return True

    assert run(_with_server(StubFacade(), fn))


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #


def test_metrics_text_format() -> None:
    async def fn(session, base):
        async with session.get(f"{base}/metrics", headers=HEADERS) as resp:
            assert resp.status == 200
            assert resp.content_type.startswith("text/plain")
            text = await resp.text()
            assert "wattmosaic_up" in text
            assert "# HELP" in text and "# TYPE" in text
        return True

    assert run(_with_server(StubFacade(), fn))


def test_metrics_falls_back_when_facade_fails() -> None:
    class NoMetrics(StubFacade):
        def metrics(self) -> str:
            raise FacadeUnavailable("nope")

    async def fn(session, base):
        async with session.get(f"{base}/metrics", headers=HEADERS) as resp:
            assert resp.status == 200
            text = await resp.text()
            assert "wattmosaic_up" in text
            assert CONTRACT_VERSION in text
        return True

    assert run(_with_server(NoMetrics(), fn))


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #


def test_server_ephemeral_port_and_idempotent_stop() -> None:
    async def run_test():
        server = APIServer(StubFacade(), host="127.0.0.1", port=0, api_token=TOKEN)
        port = await server.start()
        assert port > 0
        assert server.port == port
        assert server.bound_port == port
        assert server.started
        await server.stop()
        await server.stop()
        assert not server.started

    run(run_test())


def test_create_app_is_import_safe_and_routable() -> None:
    app = create_app(StubFacade(), api_token="")
    routes = {(r.method, r.resource.canonical) for r in app.router.routes() if r.method != "HEAD"}
    expected = {
        ("GET", "/api/v1/health"),
        ("GET", "/api/v1/status"),
        ("GET", "/api/v1/snapshot"),
        ("GET", "/api/v1/devices"),
        ("GET", "/api/v1/devices/{device_id}"),
        ("GET", "/api/v1/confidence_tiers"),
        ("GET", "/api/v1/topology"),
        ("PUT", "/api/v1/topology"),
        ("POST", "/api/v1/topology/confirm"),
        ("GET", "/api/v1/quality"),
        ("GET", "/api/v1/diagnostics"),
        ("GET", "/api/v1/models/{device_id}"),
        ("POST", "/api/v1/commands"),
        ("GET", "/api/v1/export/{device_id}"),
        ("GET", "/api/v1/metrics"),
    }
    assert expected <= routes


def test_all_documented_endpoints_reachable() -> None:
    """Every endpoint in interface_contracts §2.2 exists and answers."""

    async def fn(session, base):
        checks = [
            ("GET", "health", None),
            ("GET", "status", None),
            ("GET", "snapshot", None),
            ("GET", "devices", None),
            ("GET", "topology", None),
            ("GET", "quality", None),
            ("GET", "diagnostics", None),
            ("GET", "metrics", None),
            ("POST", "topology/confirm", None),
            ("POST", "commands", {"command": "run_diagnostics"}),
        ]
        for method, path, payload in checks:
            async with session.request(
                method, f"{base}/{path}", json=payload, headers=HEADERS
            ) as resp:
                assert resp.status in (200, 201), (method, path, resp.status)
        return True

    assert run(_with_server(StubFacade(), fn))


def test_error_responses_are_always_contract_errors() -> None:
    async def fn(session, base):
        for method, path, payload in [
            ("GET", "devices/missing", None),
            ("POST", "commands", {}),
            ("PUT", "topology", {"channels": "not-a-list"}),
        ]:
            async with session.request(
                method, f"{base}/{path}", headers=HEADERS, json=payload
            ) as resp:
                assert resp.status >= 400
                ErrorResponse.model_validate(await resp.json())
        return True

    assert run(_with_server(StubFacade(), fn))


def test_concurrency_limit_returns_503(monkeypatch) -> None:
    """A saturated semaphore must answer 503 rather than queue unboundedly."""
    app = create_app(StubFacade(), api_token="", max_concurrency=1)
    from wattmosaic_engine.api import server as server_mod

    semaphore = app[server_mod.SEMAPHORE_KEY]

    async def fn():
        await semaphore.acquire()  # hold the only slot

        APIServer.__new__(APIServer)
        # Build a minimal server around the prepared app.
        from aiohttp import web

        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = runner.addresses[0][1]
        try:
            async with (
                aiohttp.ClientSession() as session,
                session.get(f"http://127.0.0.1:{port}/api/v1/health") as resp,
            ):
                assert resp.status == 503
                body = await resp.json()
                assert body["error"] == "server_busy"
        finally:
            await runner.cleanup()
            semaphore.release()

    run(fn())


def test_no_deprecation_warnings_from_app_keys() -> None:
    """Guard against aiohttp NotAppKeyWarning regressions."""
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        create_app(StubFacade(), api_token="x")


# --------------------------------------------------------------------------- #
# Per-route timeouts
# --------------------------------------------------------------------------- #
#
# Placement discovery is the second of two 30 s caps that broke long requests
# on production ``0.2.1-dev.18`` through the ingress panel::
#
#     lookback   result
#     24 h       HTTP 200 in 9.8 s
#     48 h       HTTP 502 after 30.02 s
#     72 h       HTTP 502 after 30.02 s
#     168 h      HTTP 502 after 30.05 s
#
# The gateway cap (``addon/gateway.py``) was the first and is fixed separately.
# The second was this server's unconditional
# ``asyncio.timeout(request_timeout_s)`` with ``request_timeout_s=30``.  The
# fix keeps that default for every ordinary endpoint and gives
# ``POST /api/v1/discover/placements`` the budget derived from the engine's own
# history formula.


def test_discovery_budget_exceeds_history_budget_for_every_allowed_lookback() -> None:
    """The discovery budget must cover the engine's history budget plus fitting.

    This is the *second* of two 30 s caps fixed for production ``0.2.1-dev.18``
    (the gateway's ``sock_read=30`` was the first).  Through ingress, 24 h
    answered HTTP 200 in 9.8 s while 48 h, 72 h and 168 h were killed at
    30.02 s, 30.02 s and 30.05 s respectively -- by this middleware, even though
    the engine itself granted 168 h a 420 s history budget.

    The numbers are not chosen here: both budgets are derived from
    ``api/timeouts.py``, which is the single source of truth that ``main.py``
    re-exports.  The assertions pin three things: the default cap is still 30 s,
    the discovery budget covers the largest window the API accepts (2160 h), and
    it matches the gateway's ``ANALYSIS_TIMEOUT_S`` semantics so neither cap can
    cut the request short.
    """
    from wattmosaic_engine.api import server as server_mod
    from wattmosaic_engine.api.timeouts import (
        DISCOVERY_FIT_OVERHEAD_S,
        HISTORY_TIMEOUT_BASE_S,
        HISTORY_TIMEOUT_PER_HOUR_S,
        MAX_LOOKBACK_HOURS,
        discovery_request_timeout_s,
        history_timeout_s,
    )
    from wattmosaic_engine.main import HISTORY_TIMEOUT_BASE_S as MAIN_BASE_S
    from wattmosaic_engine.main import HISTORY_TIMEOUT_PER_HOUR_S as MAIN_PER_HOUR_S

    # ``main`` re-exports the shared constants rather than owning copies.
    assert MAIN_BASE_S == HISTORY_TIMEOUT_BASE_S == 60.0
    assert MAIN_PER_HOUR_S == HISTORY_TIMEOUT_PER_HOUR_S == 2.5
    assert MAX_LOOKBACK_HOURS == 2160.0

    # The 30 s default is unchanged and still exported: the fix must not become
    # "remove all timeouts".
    assert server_mod.DEFAULT_REQUEST_TIMEOUT_S == 30.0
    assert "DEFAULT_REQUEST_TIMEOUT_S" in server_mod.__all__
    assert server_mod.DEFAULT_ROUTE_TIMEOUTS == {
        "/api/v1/discover/placements": server_mod.DISCOVERY_REQUEST_TIMEOUT_S
    }

    budget = server_mod.DISCOVERY_REQUEST_TIMEOUT_S
    # 2160 h * 2.5 s/h = 5400 s of history + 1200 s of fitting = 6600 s, the
    # same bound the gateway uses for its socket-read budget.
    assert budget == 6600.0
    assert budget == MAX_LOOKBACK_HOURS * HISTORY_TIMEOUT_PER_HOUR_S + DISCOVERY_FIT_OVERHEAD_S
    assert budget >= 6600.0
    assert discovery_request_timeout_s() == budget

    for hours in (24, 48, 72, 168, 720, 2160):
        engine_budget = max(HISTORY_TIMEOUT_BASE_S, HISTORY_TIMEOUT_PER_HOUR_S * hours)
        assert engine_budget == history_timeout_s(hours), hours
        assert budget > engine_budget, hours
        # The history query is not the whole request: fitting follows it, so the
        # middleware must leave room beyond the history budget alone.
        assert budget - engine_budget >= DISCOVERY_FIT_OVERHEAD_S, hours

    # The 24 h case now has >200x the time that used to be allowed, and 168 h is
    # no longer cut at 30 s.
    assert budget > 30.0 * 100


async def _slow_discovery(self, **_kwargs: Any) -> Any:
    await asyncio.sleep(0.3)
    return {"ok": True, "accepted": [], "refused": {}, "groups": []}


def test_default_route_still_times_out_and_maps_to_504() -> None:
    """A slow handler on a normal route is still killed and reports 504.

    The budget is scaled down by 1/10 (0.05 s default) so the test runs in
    milliseconds, but the *mechanism* is production's: the real middleware, a
    real socket, a real ``asyncio.timeout`` and the real 504 mapping.
    ``/status`` has no override, so it must use the injected default.
    """

    class SlowStatus(StubFacade):
        async def status(self) -> Any:  # type: ignore[override]
            await asyncio.sleep(0.3)
            return {"status": "ok", "facade": "stub", "contract_version": CONTRACT_VERSION}

    async def fn(session, base):
        async with session.get(f"{base}/status", headers=HEADERS) as resp:
            assert resp.status == 504
            body = await resp.json()
            assert body["error"] == "timeout"
            assert body["retryable"] is True
            assert resp.headers["X-WattMosaic-Contract-Version"] == CONTRACT_VERSION
        return True

    assert run(_with_server(SlowStatus(), fn, request_timeout_s=0.05))


def test_discovery_route_uses_its_own_larger_budget() -> None:
    """A slow discovery survives the default cap but is still bounded by its own.

    Same scaled-down pair as the test above: the injected default is 0.05 s and
    the discovery override is 0.6 s, while the handler sleeps 0.3 s.  Under the
    old code (one unconditional budget) this request would die at 0.05 s; it now
    completes because the middleware consults the route table by exact path.

    This is a real proof rather than a tautology: it exercises the middleware
    end to end over a real socket, and the production magnitudes are pinned
    separately by
    ``test_discovery_budget_exceeds_history_budget_for_every_allowed_lookback``.
    The scaled pair preserves the ordering that matters (handler sleep <
    discovery budget, handler sleep > default) without sleeping for minutes.
    """

    class SlowDiscovery(StubFacade):
        discover_placements = _slow_discovery

    from wattmosaic_engine.api import server as server_mod

    async def fn(session, base):
        async with session.post(
            f"{base}/discover/placements", json={"lookback_hours": 168}, headers=HEADERS
        ) as resp:
            assert resp.status == 200, await resp.text()
            body = await resp.json()
            assert body["ok"] is True
        return True

    assert run(
        _with_server(
            SlowDiscovery(),
            fn,
            request_timeout_s=0.05,
            route_timeouts={server_mod.DISCOVERY_PLACEMENTS_PATH: 0.6},
        )
    )


def test_route_timeout_defaults_cover_discovery_and_leave_others_alone() -> None:
    """The app ships the discovery override; ordinary paths keep the default.

    Explicit ``route_timeouts`` are a deliberate override (used by the timing
    test above to scale the budgets down), so they win over the built-in table.
    What this pins is the production shape: a caller that passes nothing --
    ``main._run_engine`` passes nothing -- gets the derived discovery budget and
    the untouched 30 s default everywhere else.
    """
    from wattmosaic_engine.api import server as server_mod

    app = create_app(StubFacade(), api_token="")
    timeouts = app[server_mod.TIMEOUTS_KEY]
    assert timeouts == {
        server_mod.DISCOVERY_PLACEMENTS_PATH: server_mod.DISCOVERY_REQUEST_TIMEOUT_S
    }
    assert "/api/v1/status" not in timeouts
    assert app[server_mod.CONFIG_KEY]["request_timeout_s"] == 30.0

    overridden = create_app(StubFacade(), api_token="", route_timeouts={"/api/v1/health": 1.0})
    assert overridden[server_mod.TIMEOUTS_KEY] == {
        server_mod.DISCOVERY_PLACEMENTS_PATH: server_mod.DISCOVERY_REQUEST_TIMEOUT_S,
        "/api/v1/health": 1.0,
    }



# --------------------------------------------------------------------------- #
# A slow command must not inherit the 30 s default
# --------------------------------------------------------------------------- #


async def _slow_discovery_command(self, **_kwargs: Any) -> Any:
    await asyncio.sleep(0.3)
    return {"ok": True, "accepted": [], "refused": {}, "groups": []}


def test_a_slow_discovery_survives_when_called_through_commands() -> None:
    """``discover_placements`` reaches the same work through ``/commands``.

    The panel posts to ``/api/v1/discover/placements``, but the Home Assistant
    integration's ``discover_placements`` service posts the identical command to
    ``/api/v1/commands``.  ``DEFAULT_ROUTE_TIMEOUTS`` is keyed by *exact path*, so
    the commands route kept the 30 s default while the gateway had already
    granted the analysis 6600 s.  A 72 h run needs tens of seconds, so the
    engine answered 504 ``timeout`` -- from the middleware, not from the
    analysis, which is the same class of defect the 0.2.1-dev.19 fix addressed
    for the direct route.

    The scaled pair is the production shape: handler 0.3 s, default 0.05 s,
    analysis budget 0.6 s.  If ``/commands`` is not in the route table this
    request dies at 0.05 s.
    """
    from wattmosaic_engine.api import server as server_mod

    class SlowCommand(StubFacade):
        async def command(self, command: Any) -> Any:  # type: ignore[override]
            self.commands.append(command)
            return await _slow_discovery_command(self)

    async def fn(session, base):
        async with session.post(
            f"{base}/commands",
            json={"command": "discover_placements", "parameters": {"lookback_hours": 72}},
            headers=HEADERS,
        ) as resp:
            assert resp.status == 200, await resp.text()
            body = await resp.json()
            assert body["ok"] is True
        return True

    assert run(
        _with_server(
            SlowCommand(),
            fn,
            request_timeout_s=0.05,
            route_timeouts={server_mod.DISCOVERY_PLACEMENTS_PATH: 0.6},
        )
    )


def test_ordinary_commands_keep_the_default_budget() -> None:
    """The slow-command escape hatch must not widen every command.

    ``start_learning`` and the rest answer quickly; giving them the 6600 s
    analysis budget would mean a hung command pins a request for nearly two
    hours.  Only the commands in ``SLOW_COMMANDS`` borrow it.
    """

    class SlowAnyCommand(StubFacade):
        async def command(self, command: Any) -> Any:  # type: ignore[override]
            self.commands.append(command)
            await asyncio.sleep(0.3)
            return {"ok": True}

    async def fn(session, base):
        async with session.post(
            f"{base}/commands", json={"command": "start_learning"}, headers=HEADERS
        ) as resp:
            assert resp.status == 504, await resp.text()
        return True

    from wattmosaic_engine.api import server as server_mod

    assert run(
        _with_server(
            SlowAnyCommand(),
            fn,
            request_timeout_s=0.05,
            route_timeouts={server_mod.DISCOVERY_PLACEMENTS_PATH: 0.6},
        )
    )


def test_every_layer_agrees_on_which_commands_are_slow() -> None:
    """The engine, the gateway and the integration must name the same commands.

    A command that is slow in one layer and fast in another is how the 504
    happened: the gateway granted ``discover_placements`` 6600 s, the engine gave
    the ``/commands`` route 30 s, and the client gave it 10 s.  Each layer is a
    separate deployment unit, so the lists are duplicated on purpose -- this
    guard makes a divergence fail loudly instead of showing up as a timeout.
    """
    from wattmosaic_engine.api import server as server_mod

    # ``custom_components`` imports Home Assistant at module scope, so the
    # constant is read from source -- the same static approach ``test_panel.py``
    # uses for ``app.js``.
    component_const = (
        REPO_ROOT / "custom_components" / "wattmosaic" / "const.py"
    ).read_text(encoding="utf-8")
    match = re.search(r'^SLOW_COMMANDS = frozenset\((\{[^}]*\})\)', component_const, re.MULTILINE)
    assert match is not None, "custom_components/wattmosaic/const.py has no SLOW_COMMANDS"
    assert set(re.findall(r'"([^"]+)"', match.group(1))) == set(server_mod.SLOW_COMMANDS)

    assert frozenset({"discover_placements"}) == server_mod.SLOW_COMMANDS

    # The gateway hard-codes the same single command in ``_analysis_request``.
    gateway = (REPO_ROOT / "addon" / "gateway.py").read_text(encoding="utf-8")
    for command in server_mod.SLOW_COMMANDS:
        assert f'payload.get("command") == "{command}"' in gateway, (
            f"gateway._analysis_request does not treat {command} as an analysis request"
        )
