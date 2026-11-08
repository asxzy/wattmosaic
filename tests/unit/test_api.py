"""Unit tests for the HTTP API: auth, contract header, errors, endpoints.

Uses a real local server on an ephemeral port (no external network) and an
in-memory ``StubFacade``/``NullFacade``; the inference engine is never imported.
"""

from __future__ import annotations

import asyncio
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


async def _with_server(facade, fn, *, token: str = TOKEN):
    server = APIServer(facade, host="127.0.0.1", port=0, api_token=token)
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
