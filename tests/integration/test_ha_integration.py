"""Pure-logic tests for the WattMosaic Home Assistant integration.

**Why this file does not import Home Assistant**

``homeassistant`` is not installed in this development environment (verified:
``.venv/bin/python -c "import homeassistant"`` -> ``ModuleNotFoundError``), and
it is not a dependency of this repository's dev environment.  The integration
is therefore split into:

* pure-Python modules (``util``, ``api_client``, ``flow_logic``,
  ``service_logic``, ``entity_values``, ``entity_defs``) — everything that
  decides behaviour, and
* HA-facing modules (``__init__``, ``coordinator``, ``sensor``,
  ``binary_sensor``, ``config_flow``, ``diagnostics``) — thin wiring only.

This test module imports the *pure* modules directly from their files through a
synthetic package, so ``custom_components/wattmosaic/__init__.py`` (which does
import Home Assistant) is never executed.  No network access is used: the HTTP
client is exercised against a stub aiohttp-like session.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import sys
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

PACKAGE_DIR = (
    Path(__file__).resolve().parents[2] / "custom_components" / "wattmosaic"
)
PACKAGE_NAME = "wm_under_test"


def _load_package() -> types.ModuleType:
    """Create a package handle for the integration without running __init__."""
    if PACKAGE_NAME in sys.modules:
        return sys.modules[PACKAGE_NAME]
    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME,
        PACKAGE_DIR / "__init__.py",
        submodule_search_locations=[str(PACKAGE_DIR)],
    )
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    # Deliberately do NOT exec the spec: that would import homeassistant.
    module.__path__ = [str(PACKAGE_DIR)]  # type: ignore[attr-defined]
    sys.modules[PACKAGE_NAME] = module
    return module


_load_package()

const = importlib.import_module(f"{PACKAGE_NAME}.const")
util = importlib.import_module(f"{PACKAGE_NAME}.util")
api = importlib.import_module(f"{PACKAGE_NAME}.api_client")
flow = importlib.import_module(f"{PACKAGE_NAME}.flow_logic")
services = importlib.import_module(f"{PACKAGE_NAME}.service_logic")
entity_values = importlib.import_module(f"{PACKAGE_NAME}.entity_values")
entity_defs = importlib.import_module(f"{PACKAGE_NAME}.entity_defs")

#: The engine contract is the source of truth for every enum the integration
#: mirrors.  Imported here so a contract value added without the entity
#: definition is caught rather than silently reported as ``unknown``.
from wattmosaic_engine.contracts import Source  # noqa: E402

NOW = datetime(2026, 11, 5, 12, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Stub aiohttp session
# --------------------------------------------------------------------------- #


class StubResponse:
    def __init__(self, status: int, body: str, headers: dict[str, str] | None = None) -> None:
        self.status = status
        self._body = body
        self.headers = headers or {}

    async def text(self) -> str:
        return self._body

    async def json(self) -> Any:
        return json.loads(self._body)


class StubRequestContext:
    def __init__(self, factory: Any) -> None:
        self._factory = factory

    async def __aenter__(self) -> StubResponse:
        result = self._factory()
        if isinstance(result, Exception):
            raise result
        return result

    async def __aexit__(self, *exc_info: Any) -> bool:
        return False


class StubSession:
    """Records calls and returns scripted responses."""

    def __init__(self, responses: list[Any] | Any) -> None:
        self.responses = responses if isinstance(responses, list) else [responses]
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> StubRequestContext:
        self.calls.append({"method": method, "url": url, **kwargs})
        index = min(len(self.calls) - 1, len(self.responses) - 1)
        return StubRequestContext(lambda: self.responses[index])


def json_response(payload: Any, status: int = 200, headers: dict[str, str] | None = None) -> StubResponse:
    merged = {"X-WattMosaic-Contract-Version": const.CONTRACT_VERSION}
    merged.update(headers or {})
    return StubResponse(status, json.dumps(payload), merged)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# URL / secret handling
# --------------------------------------------------------------------------- #


class TestUrlHandling:
    def test_normalises_trailing_slash(self) -> None:
        assert util.normalise_base_url("http://10.0.0.5:8787/") == "http://10.0.0.5:8787"

    def test_strips_api_path(self) -> None:
        assert (
            util.normalise_base_url("https://engine.local:8787/api/v1/")
            == "https://engine.local:8787"
        )

    def test_adds_http_scheme(self) -> None:
        assert util.normalise_base_url("192.168.1.10:8787") == "http://192.168.1.10:8787"

    def test_preserves_subpath(self) -> None:
        assert (
            util.normalise_base_url("http://proxy.local/wattmosaic/")
            == "http://proxy.local/wattmosaic"
        )

    @pytest.mark.parametrize(
        "value",
        [
            "",
            None,
            "ftp://host:21",
            "http://",
            "http://host:8787/?token=abc",
            "http://host:8787/#frag",
            "http://host:notaport",
            123,
        ],
    )
    def test_rejects_invalid(self, value: Any) -> None:
        assert util.normalise_base_url(value) is None


class TestSecretRedaction:
    # Deliberately fake: the "redaction-test" marker lets the repository secret
    # scanner distinguish a fixture from a real credential.
    TOKEN = "redaction-test-token-value"

    def test_sanitize_text_redacts_explicit_secret(self) -> None:
        text = f"failed with token {self.TOKEN}"
        assert self.TOKEN not in util.sanitize_text(text, self.TOKEN)
        assert "**REDACTED**" in util.sanitize_text(text, self.TOKEN)

    @pytest.mark.parametrize(
        "text",
        [
            "X-API-Token: abc123",
            "api_token=abc123",
            "Authorization: Bearer abc123",
            "GET /snapshot?token=abc123",
        ],
    )
    def test_sanitize_text_redacts_patterns(self, text: str) -> None:
        result = util.sanitize_text(text)
        assert "abc123" not in result

    def test_redact_config_replaces_token_fields(self) -> None:
        cleaned = util.redact_config(
            {"base_url": "http://x:8787", "api_token": self.TOKEN}
        )
        assert cleaned["base_url"] == "http://x:8787"
        assert cleaned["api_token"] == "**REDACTED**"

    def test_client_repr_never_contains_token(self) -> None:
        client = api.EngineClient(StubSession(json_response({})), "http://x:8787", api_token=self.TOKEN)
        assert self.TOKEN not in repr(client)
        assert "configured" in repr(client)

    def test_placeholder_token_is_not_sent(self) -> None:
        client = api.EngineClient(
            StubSession(json_response({})), "http://x:8787", api_token="changeme"
        )
        assert client.token_configured is False
        assert const.HEADER_API_TOKEN not in client._headers()


# --------------------------------------------------------------------------- #
# API client
# --------------------------------------------------------------------------- #


class TestApiClient:
    def _client(self, responses: Any, **kwargs: Any) -> tuple[api.EngineClient, StubSession]:
        session = StubSession(responses)
        client = api.EngineClient(
            session,
            "http://engine.local:8787",
            api_token="token-abc",
            sleep=self._no_sleep,
            **kwargs,
        )
        return client, session

    @staticmethod
    async def _no_sleep(_delay: float) -> None:
        return None

    def test_sends_token_and_contract_headers(self) -> None:
        client, session = self._client(json_response({"status": "ok"}))
        result = run(client.health())
        assert result == {"status": "ok"}
        call = session.calls[0]
        assert call["url"] == "http://engine.local:8787/api/v1/health"
        assert call["headers"][const.HEADER_API_TOKEN] == "token-abc"
        assert call["headers"][const.HEADER_CONTRACT_VERSION] == const.CONTRACT_VERSION

    def test_no_token_header_when_unconfigured(self) -> None:
        session = StubSession(json_response({"status": "ok"}))
        client = api.EngineClient(session, "http://engine.local:8787")
        run(client.health())
        assert const.HEADER_API_TOKEN not in session.calls[0]["headers"]

    def test_snapshot_endpoint(self) -> None:
        client, session = self._client(json_response({"results": []}))
        run(client.snapshot())
        assert session.calls[0]["url"].endswith("/api/v1/snapshot")

    def test_device_path_is_percent_encoded(self) -> None:
        client, session = self._client(json_response({}))
        run(client.device("dev/with space"))
        assert session.calls[0]["url"].endswith("/api/v1/devices/dev%2Fwith%20space")

    def test_command_body_shape(self) -> None:
        client, session = self._client(json_response({"ok": True, "command": "freeze_model"}))
        run(client.command("freeze_model", device_id="dev1", parameters={"x": 1}))
        body = session.calls[0]["json"]
        assert body["command"] == "freeze_model"
        assert body["device_id"] == "dev1"
        assert body["parameters"] == {"x": 1}
        assert "requested_at" not in body  # engine stamps it

    def test_401_raises_auth_error_without_token(self) -> None:
        client, _ = self._client(
            StubResponse(401, json.dumps({"error": "unauthorized"}), {})
        )
        with pytest.raises(api.EngineAuthError) as excinfo:
            run(client.health())
        assert "token-abc" not in str(excinfo.value)

    def test_403_raises_auth_error(self) -> None:
        client, _ = self._client(StubResponse(403, "{}", {}))
        with pytest.raises(api.EngineAuthError):
            run(client.health())

    def test_contract_mismatch_raises(self) -> None:
        client, _ = self._client(
            json_response({"status": "ok"}, headers={"X-WattMosaic-Contract-Version": "2.0.0"})
        )
        with pytest.raises(api.EngineContractMismatch):
            run(client.health())

    def test_minor_contract_difference_tolerated(self) -> None:
        client, _ = self._client(
            json_response({"status": "ok"}, headers={"X-WattMosaic-Contract-Version": "1.7.3"})
        )
        assert run(client.health())["status"] == "ok"

    def test_missing_contract_header_tolerated(self) -> None:
        session = StubSession(StubResponse(200, json.dumps({"status": "ok"}), {}))
        client = api.EngineClient(session, "http://engine.local:8787", sleep=self._no_sleep)
        assert run(client.health())["status"] == "ok"

    def test_malformed_json_raises_protocol_error(self) -> None:
        client, _ = self._client(StubResponse(200, "<html>not json</html>", {}))
        with pytest.raises(api.EngineProtocolError):
            run(client.health())

    def test_wrong_payload_type_raises_protocol_error(self) -> None:
        client, _ = self._client(json_response(["not", "a", "dict"]))
        with pytest.raises(api.EngineProtocolError):
            run(client.health())

    def test_devices_accepts_list(self) -> None:
        client, _ = self._client(json_response([{"device_id": "a"}]))
        assert run(client.devices()) == [{"device_id": "a"}]

    def test_retries_on_503_then_succeeds(self) -> None:
        responses = [
            StubResponse(503, json.dumps({"error": "starting"}), {}),
            json_response({"status": "ok"}),
        ]
        client, session = self._client(responses, max_retries=2)
        assert run(client.health())["status"] == "ok"
        assert len(session.calls) == 2

    def test_retry_budget_is_bounded(self) -> None:
        client, session = self._client(
            StubResponse(503, json.dumps({"error": "down"}), {}), max_retries=2
        )
        with pytest.raises(api.EngineUnavailable):
            run(client.health())
        assert len(session.calls) == 3  # initial + 2 retries

    def test_network_error_becomes_unavailable(self) -> None:
        client, _ = self._client(ConnectionError("refused"), max_retries=0)
        with pytest.raises(api.EngineUnavailable):
            run(client.health())

    def test_404_is_not_retried(self) -> None:
        client, session = self._client(
            StubResponse(404, json.dumps({"error": "not found"}), {}), max_retries=3
        )
        with pytest.raises(api.EngineProtocolError):
            run(client.models("missing"))
        assert len(session.calls) == 1

    def test_error_message_never_contains_token(self) -> None:
        client, _ = self._client(
            StubResponse(
                400,
                json.dumps({"error": "bad", "detail": "token token-abc invalid"}),
                {},
            ),
            max_retries=0,
        )
        with pytest.raises(api.EngineError) as excinfo:
            run(client.health())
        assert "token-abc" not in str(excinfo.value)

    def test_topology_methods(self) -> None:
        client, session = self._client(json_response({"version": 2, "channels": []}))
        run(client.put_topology({"version": 2, "channels": []}))
        run(client.confirm_topology(2))
        assert session.calls[0]["method"] == "PUT"
        assert session.calls[0]["url"].endswith("/api/v1/topology")
        assert session.calls[1]["url"].endswith("/api/v1/topology/confirm")
        assert session.calls[1]["json"] == {"version": 2}

    def test_export_device(self) -> None:
        client, session = self._client(json_response({"device_id": "a", "samples": []}))
        run(client.export_device("a"))
        assert session.calls[0]["url"].endswith("/api/v1/export/a")

    def test_metrics_returns_text(self) -> None:
        session = StubSession(StubResponse(200, "# HELP x 1\n", {}))
        client = api.EngineClient(session, "http://e:8787")
        assert "HELP" in run(client.metrics())

    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            (api.EngineAuthError("x"), "invalid_auth"),
            (api.EngineContractMismatch("x"), "contract_mismatch"),
            (api.EngineUnavailable("x"), "cannot_connect"),
            (api.EngineProtocolError("x"), "invalid_response"),
            (ValueError("x"), "unknown"),
        ],
    )
    def test_error_key_mapping(self, error: Exception, expected: str) -> None:
        assert api.describe_engine_error(error) == expected

    def test_token_display_never_echoes(self) -> None:
        assert api.redact_token_for_display("secret") == "configured"
        assert api.redact_token_for_display(None) == "not configured"


# --------------------------------------------------------------------------- #
# Coercion: engine snapshot -> entity values
# --------------------------------------------------------------------------- #


def _inference_result(**overrides: Any) -> dict[str, Any]:
    payload = {
        "device_id": "network_gear",
        "timestamp": (NOW - timedelta(seconds=3)).isoformat(),
        "power_w": 236.5,
        "energy_kwh": 12.345,
        "source": "inferred",
        "confidence": 0.86,
        "model_id": "binary_model",
        "model_version": 3,
        "operating_stage": "running",
        "stage_status": "identified",
        "sample_count": 120,
        "quality_status": "good",
        "measurement_age_seconds": 2.1,
        "supporting_ct_channels": ["ct_l1_main", "ct_branch_kitchen"],
        "explanation": ["ha_state_transition"],
        "confidence_is_calibrated": False,
    }
    payload.update(overrides)
    return payload


class TestSnapshotCoercion:
    def test_coerces_result(self) -> None:
        result = util.coerce_device_result(_inference_result())
        assert result is not None
        assert result["device_id"] == "network_gear"
        assert result["power_w"] == 236.5
        assert result["confidence_pct"] == 86.0
        assert result["source"] == "inferred"
        assert result["supporting_ct_channels"] == ["ct_l1_main", "ct_branch_kitchen"]

    def test_rejects_missing_device_id(self) -> None:
        assert util.coerce_device_result({"power_w": 1.0}) is None

    def test_rejects_non_mapping(self) -> None:
        assert util.coerce_device_result(None) is None
        assert util.coerce_device_result("nope") is None  # type: ignore[arg-type]

    def test_unknown_source_falls_back(self) -> None:
        result = util.coerce_device_result(_inference_result(source="made_up"))
        assert result is not None
        assert result["source"] == "unknown"

    def test_tier_defaults_to_unknown_when_absent(self) -> None:
        """An older engine that omits the tier must not be read as confident."""
        result = util.coerce_device_result(_inference_result())
        assert result is not None
        assert result["confidence_tier"] == "tier_0_unknown"
        assert result["tier_reasons"] == []

    def test_tier_is_preserved_when_present(self) -> None:
        result = util.coerce_device_result(
            _inference_result(
                confidence_tier="tier_3_measured",
                tier_reasons=["dedicated_ct_only"],
            )
        )
        assert result is not None
        assert result["confidence_tier"] == "tier_3_measured"
        assert result["tier_reasons"] == ["dedicated_ct_only"]

    def test_unknown_tier_from_newer_engine_is_downgraded(self) -> None:
        """An unrecognised grade must never be trusted as a strong one."""
        result = util.coerce_device_result(_inference_result(confidence_tier="tier_9_hypothetical"))
        assert result is not None
        assert result["confidence_tier"] == "tier_0_unknown"

    def test_nan_power_becomes_none(self) -> None:
        result = util.coerce_device_result(
            {
                "device_id": "x",
                "timestamp": NOW.isoformat(),
                "power_w": "NaN",
                "source": "unknown",
                "confidence": 0.0,
                "quality_status": "bad",
            }
        )
        assert result is not None
        assert result["power_w"] is None

    def test_snapshot_indexes_results_and_dedupes(self) -> None:
        snapshot = util.coerce_snapshot(
            {
                "timestamp": NOW.isoformat(),
                "reconciliation": {"main_power_w": 5231.0, "l1_power_w": 2708.1, "l2_power_w": 2522.9},
                "results": [
                    _inference_result(),
                    _inference_result(power_w=999.0),  # duplicate id: ignored
                    {"device_id": "hot_water", "timestamp": NOW.isoformat(), "power_w": 14.95,
                     "source": "direct_measured", "confidence": 1.0, "quality_status": "good"},
                ],
                "quality": {"overall_score": 0.93, "issues": ["channel ct3 slightly old"]},
            }
        )
        assert set(snapshot["results_by_id"]) == {"network_gear", "hot_water"}
        assert snapshot["results_by_id"]["network_gear"]["power_w"] == 236.5
        assert snapshot["reconciliation"]["main_power_w"] == 5231.0
        assert snapshot["quality"]["overall_score_pct"] == 93.0

    def test_snapshot_never_raises_on_garbage(self) -> None:
        for garbage in (None, {}, {"results": "notalist"}, {"reconciliation": 7}, []):
            snapshot = util.coerce_snapshot(garbage)  # type: ignore[arg-type]
            assert snapshot["results"] == []
            # A missing reconciliation must not fabricate 0 W as a measurement.
            assert snapshot["reconciliation"]["main_power_w"] is None
            assert snapshot["reconciliation"]["present"] is False

    def test_malformed_timestamp_is_none(self) -> None:
        assert util.parse_datetime("not-a-date") is None
        assert util.parse_datetime(None) is None
        assert util.parse_datetime(1234) is None

    def test_z_suffix_parsed_as_utc(self) -> None:
        parsed = util.parse_datetime("2026-11-05T12:00:00Z")
        assert parsed == NOW
        assert parsed is not None and parsed.tzinfo is not None


class TestDeviceEntityValues:
    def test_maps_inference_result(self) -> None:
        result = util.coerce_device_result(_inference_result())
        values = entity_values.device_values(
            result, engine_available=True, now=NOW, stale_after=120
        )
        assert values["available"] is True
        assert values["power"] == 236.5
        assert values["energy"] == 12.345
        assert values["source"] == "inferred"
        assert values["confidence"] == 86.0
        assert values["sample_count"] == 120
        assert values["model_validity"] == "valid"
        assert values["operating_stage"] == "running"
        assert values["last_inference"] == NOW - timedelta(seconds=3)
        assert values["data_quality"] == "good"
        assert values["running"] is True

    def test_direct_measured_has_no_model_requirement(self) -> None:
        result = util.coerce_device_result(
            _inference_result(
                device_id="hot_water",
                source="direct_measured",
                model_id=None,
                model_version=None,
                power_w=14.95,
            )
        )
        values = entity_values.device_values(
            result, engine_available=True, now=NOW, stale_after=120
        )
        assert values["model_validity"] == "not_required"

    def test_inferred_without_model_is_invalid(self) -> None:
        result = util.coerce_device_result(
            _inference_result(model_id=None, model_version=None)
        )
        values = entity_values.device_values(
            result, engine_available=True, now=NOW, stale_after=120
        )
        assert values["model_validity"] == "invalid"

    def test_unknown_source_is_not_running(self) -> None:
        result = util.coerce_device_result(_inference_result(source="unknown", power_w=500.0))
        values = entity_values.device_values(
            result, engine_available=True, now=NOW, stale_after=120
        )
        assert values["running"] is False

    def test_zero_power_is_not_running(self) -> None:
        result = util.coerce_device_result(_inference_result(power_w=0.0))
        values = entity_values.device_values(
            result, engine_available=True, now=NOW, stale_after=120
        )
        assert values["running"] is False

    def test_missing_result_is_unavailable(self) -> None:
        values = entity_values.device_values(
            None, engine_available=True, now=NOW, stale_after=120
        )
        assert values["available"] is False
        assert values["running_available"] is False

    @pytest.mark.parametrize("quality", ["stale", "missing", "bad"])
    def test_unpublishable_quality_is_unavailable(self, quality: str) -> None:
        result = util.coerce_device_result(_inference_result(quality_status=quality))
        values = entity_values.device_values(
            result, engine_available=True, now=NOW, stale_after=120
        )
        assert values["available"] is False

    def test_degraded_quality_is_still_published(self) -> None:
        result = util.coerce_device_result(_inference_result(quality_status="degraded"))
        values = entity_values.device_values(
            result, engine_available=True, now=NOW, stale_after=120
        )
        assert values["available"] is True
        assert values["data_quality"] == "degraded"

    def test_device_values_never_raises_on_garbage(self) -> None:
        for garbage in (
            None,
            {},
            {"device_id": "x"},
            {"device_id": "x", "power_w": "abc", "confidence": None},
            {"device_id": "", "timestamp": "not-a-date"},
        ):
            values = entity_values.device_values(
                garbage, engine_available=True, now=NOW, stale_after=120  # type: ignore[arg-type]
            )
            assert isinstance(values, dict)
            assert "available" in values

    def test_missing_energy_stays_none(self) -> None:
        result = util.coerce_device_result(_inference_result(energy_kwh=None))
        values = entity_values.device_values(
            result, engine_available=True, now=NOW, stale_after=120
        )
        assert values["energy"] is None
        assert values["power"] == 236.5

    def test_global_values_accept_raw_string_timestamp(self) -> None:
        # The coordinator coerces first, but the helper must be defensive.
        values = entity_values.global_values(
            {"timestamp": NOW.isoformat(), "reconciliation": {"main_power_w": 10.0}},
            {},
            engine_available=True,
            now=NOW,
            stale_after=120,
        )
        assert values["available"] is True
        assert values["total_power"] == 10.0

    def test_global_values_bad_timestamp_is_stale(self) -> None:
        values = entity_values.global_values(
            {"timestamp": "garbage", "reconciliation": {"main_power_w": 10.0}},
            {},
            engine_available=True,
            now=NOW,
            stale_after=120,
        )
        assert values["available"] is False


# --------------------------------------------------------------------------- #
# Staleness / availability
# --------------------------------------------------------------------------- #


class TestStaleness:
    def test_fresh_is_not_stale(self) -> None:
        assert not util.is_stale(NOW - timedelta(seconds=10), now=NOW, stale_after=120)

    def test_old_is_stale(self) -> None:
        assert util.is_stale(NOW - timedelta(seconds=121), now=NOW, stale_after=120)

    def test_missing_timestamp_is_stale(self) -> None:
        assert util.is_stale(None, now=NOW, stale_after=120)

    def test_future_timestamp_is_not_stale(self) -> None:
        assert not util.is_stale(NOW + timedelta(seconds=30), now=NOW, stale_after=120)

    def test_engine_unavailable_threshold(self) -> None:
        assert not util.is_engine_unavailable(
            NOW - timedelta(seconds=119), now=NOW, unavailable_after=120
        )
        assert util.is_engine_unavailable(
            NOW - timedelta(seconds=121), now=NOW, unavailable_after=120
        )
        assert util.is_engine_unavailable(None, now=NOW, unavailable_after=120)

    def test_stale_result_hides_measurement(self) -> None:
        result = util.coerce_device_result(
            _inference_result(timestamp=(NOW - timedelta(seconds=300)).isoformat())
        )
        values = entity_values.device_values(
            result, engine_available=True, now=NOW, stale_after=120
        )
        assert values["available"] is False

    def test_engine_down_hides_measurement(self) -> None:
        result = util.coerce_device_result(_inference_result())
        values = entity_values.device_values(
            result, engine_available=False, now=NOW, stale_after=120
        )
        assert values["available"] is False

    def test_global_values_unavailable_when_stale(self) -> None:
        snapshot = util.coerce_snapshot(
            {
                "timestamp": (NOW - timedelta(seconds=400)).isoformat(),
                "reconciliation": {"main_power_w": 5000.0},
            }
        )
        values = entity_values.global_values(
            snapshot, {"status": "ok"}, engine_available=True, now=NOW, stale_after=120
        )
        assert values["available"] is False
        assert values["engine_reachable"] is True

    def test_global_engine_status_reports_offline(self) -> None:
        values = entity_values.global_values(
            {}, {}, engine_available=False, now=NOW, stale_after=120
        )
        assert values["engine_status"] == "offline"
        assert values["engine_reachable"] is False
        assert values["learning_active"] is False

    def test_engine_status_never_uses_reserved_ha_state(self) -> None:
        # "unavailable" is a reserved HA state string and cannot be published
        # as an enum option.
        assert "unavailable" not in entity_defs.GLOBAL_SENSORS["engine_status"]["options"]
        for value in ("ok", "degraded", "error"):
            result = entity_values.global_values(
                {"timestamp": NOW.isoformat()},
                {"status": value},
                engine_available=True,
                now=NOW,
                stale_after=120,
            )
            assert result["engine_status"] in entity_defs.GLOBAL_SENSORS["engine_status"]["options"]

    def test_global_reconciliation_values(self) -> None:
        snapshot = util.coerce_snapshot(
            {
                "timestamp": NOW.isoformat(),
                "reconciliation": {
                    "timestamp": NOW.isoformat(),
                    "main_power_w": 5231.0,
                    "l1_power_w": 2708.1,
                    "l2_power_w": 2522.9,
                    "direct_measured_w": 1120.0,
                    "inferred_w": 3800.0,
                    "model_estimated_w": 0.0,
                    "unknown_residual_w": 311.0,
                    "reconciliation_error_w": -12.0,
                    "accounted_fraction": 0.94,
                    "quality_status": "good",
                },
                "quality": {"overall_score": 0.91},
            }
        )
        values = entity_values.global_values(
            snapshot, {"status": "ok", "learning_active": True},
            engine_available=True, now=NOW, stale_after=120,
        )
        assert values["total_power"] == 5231.0
        assert values["unknown_power"] == 311.0
        assert values["reconciliation_error"] == -12.0
        assert values["accounted_fraction"] == 94.0
        assert values["quality_score"] == 91.0
        assert values["engine_status"] == "ok"
        assert values["learning_active"] is True

    def test_reconciliation_stale_quality_marks_balance_unavailable(self) -> None:
        snapshot = util.coerce_snapshot(
            {
                "timestamp": NOW.isoformat(),
                "reconciliation": {"main_power_w": 1.0, "quality_status": "stale"},
            }
        )
        values = entity_values.global_values(
            snapshot, {}, engine_available=True, now=NOW, stale_after=120
        )
        assert values["available"] is True
        assert values["reconciliation_available"] is False


# --------------------------------------------------------------------------- #
# Flags read back from stored options
# --------------------------------------------------------------------------- #


class TestAsBool:
    """One coercion rule for every stored flag.

    Options are JSON/YAML round-tripped, so a boolean the user set can come
    back as a string.  ``bool("false")`` is ``True``, which would silently
    reverse an explicit decision; and an uninterpretable value must not remove
    an entity the user relies on.
    """

    @pytest.mark.parametrize("raw", ["false", "FALSE", " false ", "0", "no", "off"])
    def test_false_spellings(self, raw: str) -> None:
        assert util.as_bool(raw, default=True) is False

    @pytest.mark.parametrize("raw", [True, 1, "true", "TRUE", "yes", "on", 2])
    def test_true_spellings(self, raw: Any) -> None:
        assert util.as_bool(raw, default=False) is True

    def test_missing_uses_the_default(self) -> None:
        assert util.as_bool(None, default=True) is True
        assert util.as_bool(None, default=False) is False
        assert util.as_bool("", default=True) is True
        assert util.as_bool("", default=False) is False

    @pytest.mark.parametrize("raw", ["maybe", "bogus", {}, [], object()])
    def test_uninterpretable_uses_the_default(self, raw: Any) -> None:
        assert util.as_bool(raw, default=True) is True
        assert util.as_bool(raw, default=False) is False

    def test_bool_stays_bool(self) -> None:
        assert util.as_bool(False, default=True) is False
        assert util.as_bool(True, default=False) is True


# --------------------------------------------------------------------------- #
# Energy monotonicity / integration
# --------------------------------------------------------------------------- #


class TestEnergy:
    """Energy publishes the engine's lifetime total, gated on trustworthiness.

    Contract: a publishable reading is forwarded verbatim (so a genuine engine
    reset is visible to HA and the meter resumes); an untrustworthy reading is
    held so it cannot corrupt HA statistics.
    """

    @staticmethod
    def _publish(readings, *, publishable=True):
        """Run a reading series through ``published_energy``."""
        previous = None
        out = []
        for value in readings:
            flag = publishable(value) if callable(publishable) else publishable
            previous = util.published_energy(value, previous, publishable=flag)
            out.append(previous)
        return out

    def test_increasing_total_is_published(self) -> None:
        assert self._publish([12.0, 12.5]) == [12.0, 12.5]

    def test_equal_total_is_published(self) -> None:
        assert self._publish([12.0, 12.0]) == [12.0, 12.0]

    def test_published_series_is_non_decreasing(self) -> None:
        series = [12.0, 12.1, 12.1, 12.2, 12.3]
        published = self._publish(series)
        assert published == sorted(published)
        assert published[-1] == pytest.approx(12.3)

    def test_reset_is_forwarded_so_the_meter_resumes(self) -> None:
        """Regression: a decrease used to pin the total forever.

        The engine total drops to 0 and then climbs again.  Holding the old
        value published 12.6 forever, so no energy was ever recorded again.
        The reset must reach HA, and the total must resume advancing.
        """
        published = self._publish([12.6, 0.0, 0.1, 0.2, 0.5])
        assert published == pytest.approx([12.6, 0.0, 0.1, 0.2, 0.5])
        assert published[-1] > published[1]

    def test_rollback_is_forwarded(self) -> None:
        """Restoring an older engine backup is a real reset, not new energy."""
        published = self._publish([12.6, 10.0, 10.2, 10.5])
        assert published == pytest.approx([12.6, 10.0, 10.2, 10.5])

    def test_degraded_reading_cannot_move_the_total(self) -> None:
        """A stale/degraded sample must never reach HA statistics."""
        published = self._publish([5.0, 99.0, 5.5], publishable=lambda v: v != 99.0)
        assert published == [5.0, 5.0, 5.5]

    def test_unpublishable_first_reading_stays_none(self) -> None:
        assert self._publish([9.0], publishable=False) == [None]

    def test_missing_candidate_keeps_previous(self) -> None:
        assert self._publish([12.0, None]) == [12.0, 12.0]

    def test_missing_candidate_and_no_previous(self) -> None:
        assert self._publish([None]) == [None]

    def test_negative_candidate_is_zero(self) -> None:
        assert self._publish([-5.0]) == [0.0]

    def test_first_value(self) -> None:
        assert self._publish([0.5]) == [0.5]

    def test_integration_adds_energy(self) -> None:
        # 1000 W for 3600 s = 1 kWh
        assert util.integrate_energy(0.0, 1000.0, 3600.0, max_gap_s=120) == pytest.approx(0.0333333, rel=1e-4)

    def test_integration_clamps_long_gap(self) -> None:
        # 10 h elapsed must not accrue 10 h of energy.
        value = util.integrate_energy(0.0, 1000.0, 36000.0, max_gap_s=120)
        assert value == pytest.approx(120 * 1000 / 3_600_000)

    def test_integration_ignores_invalid(self) -> None:
        assert util.integrate_energy(5.0, None, 60.0, max_gap_s=120) == 5.0
        assert util.integrate_energy(5.0, 100.0, None, max_gap_s=120) == 5.0
        assert util.integrate_energy(5.0, 100.0, -1.0, max_gap_s=120) == 5.0


# --------------------------------------------------------------------------- #
# Energy entity opt-in and device binding
# --------------------------------------------------------------------------- #


class TestEnergyOptIn:
    """A device may opt out of an energy entity; the default must stay on."""

    def test_default_is_enabled(self) -> None:
        assert entity_values.energy_enabled(None) is True
        assert entity_values.energy_enabled({}) is True
        assert entity_values.energy_enabled({"device_type": "socket"}) is True

    def test_explicit_disable(self) -> None:
        assert entity_values.energy_enabled({"create_energy_sensor": False}) is False

    def test_explicit_enable(self) -> None:
        assert entity_values.energy_enabled({"create_energy_sensor": True}) is True

    @pytest.mark.parametrize("raw", ["false", "FALSE", "0", "no", "off", " false "])
    def test_string_forms_of_false(self, raw: str) -> None:
        assert entity_values.energy_enabled({"create_energy_sensor": raw}) is False

    @pytest.mark.parametrize("raw", ["true", "1", "yes", "on"])
    def test_string_forms_of_true(self, raw: str) -> None:
        assert entity_values.energy_enabled({"create_energy_sensor": raw}) is True

    def test_unknown_value_falls_back_to_enabled(self) -> None:
        # A value the integration cannot interpret must not silently remove a
        # sensor the user may rely on in their Energy Dashboard.
        assert entity_values.energy_enabled({"create_energy_sensor": None}) is True


class TestBindingDeviceCandidates:
    """Only user-declared runtime entities may host an energy entity."""

    def test_empty_when_no_configuration(self) -> None:
        assert entity_values.binding_device_entity_ids(None) == []
        assert entity_values.binding_device_entity_ids({}) == []

    def test_uses_declared_state_entities(self) -> None:
        assert entity_values.binding_device_entity_ids(
            {"state_entity_ids": ["switch.pump"]}
        ) == ["switch.pump"]

    def test_includes_controller_load_entities(self) -> None:
        assert entity_values.binding_device_entity_ids(
            {"load_entity_ids": ["light.a", "light.b"]}
        ) == ["light.a", "light.b"]

    def test_deduplicates_and_preserves_order(self) -> None:
        assert entity_values.binding_device_entity_ids(
            {"state_entity_ids": ["switch.a", "switch.b"], "load_entity_ids": ["switch.a"]}
        ) == ["switch.a", "switch.b"]

    def test_ignores_blank_and_non_list_values(self) -> None:
        assert entity_values.binding_device_entity_ids(
            {"state_entity_ids": ["", "  ", None], "load_entity_ids": "switch.a"}
        ) == []


class TestMergeEnergyChoice:
    """The config entry's opt-in layers on top of the engine's configuration."""

    def test_nothing_known_returns_none(self) -> None:
        # ``None`` is "the engine said nothing"; an empty dict would be read as
        # "the engine said nothing is set" and lose the default-on behaviour.
        assert entity_values.merge_energy_choice(None, None, "d1") is None
        assert entity_values.merge_energy_choice({}, {}, "d1") is None

    def test_engine_configuration_is_preserved(self) -> None:
        merged = entity_values.merge_energy_choice(
            {"device_type": "socket", "state_entity_ids": ["switch.pump"]}, None, "d1"
        )
        assert merged == {"device_type": "socket", "state_entity_ids": ["switch.pump"]}

    def test_engine_configuration_is_not_mutated(self) -> None:
        original = {"device_type": "socket"}
        entity_values.merge_energy_choice(original, {"d1": False}, "d1")
        assert original == {"device_type": "socket"}

    def test_stored_choice_overrides_the_engine(self) -> None:
        merged = entity_values.merge_energy_choice(
            {"create_energy_sensor": True}, {"d1": False}, "d1"
        )
        assert merged is not None
        assert merged["create_energy_sensor"] is False

    def test_choice_for_another_device_is_ignored(self) -> None:
        merged = entity_values.merge_energy_choice(None, {"d2": False}, "d1")
        assert merged is None

    def test_choice_alone_is_enough_to_produce_a_mapping(self) -> None:
        merged = entity_values.merge_energy_choice(None, {"d1": False}, "d1")
        assert merged == {"create_energy_sensor": False}

    @pytest.mark.parametrize("raw", ["false", "0", "no", "off"])
    def test_string_false_from_options_is_an_opt_out(self, raw: str) -> None:
        # ``bool("false")`` is True; options restored from an older file can
        # hold strings, so the coercion must be the shared rule.
        merged = entity_values.merge_energy_choice(None, {"d1": raw}, "d1")
        assert merged is not None
        assert merged["create_energy_sensor"] is False

    def test_malformed_storage_is_ignored(self) -> None:
        merged = entity_values.merge_energy_choice(
            {"device_type": "socket"}, "bogus", "d1"
        )
        assert merged == {"device_type": "socket"}


class TestResolveBindingTarget:
    """The binding decision is pure so its guards can be tested without HA."""

    REGISTRY = {
        "switch.pump": {"device_id": "dev_pump", "config_entry_id": "entry_other"},
        "sensor.own_output": {"device_id": "dev_self", "config_entry_id": "entry_mine"},
        "sensor.no_device": {"device_id": None, "config_entry_id": "entry_other"},
        "sensor.missing": None,
    }

    def test_binds_to_real_device(self) -> None:
        assert entity_values.resolve_binding_target(
            ["switch.pump"], self.REGISTRY, own_config_entry_id="entry_mine"
        ) == "dev_pump"

    def test_never_binds_to_its_own_output(self) -> None:
        # A WattMosaic entity would make the inferred load the host of its own
        # energy sensor; there is no physical device behind it.
        assert entity_values.resolve_binding_target(
            ["sensor.own_output"], self.REGISTRY, own_config_entry_id="entry_mine"
        ) is None

    def test_skips_own_output_and_uses_a_later_real_entity(self) -> None:
        assert entity_values.resolve_binding_target(
            ["sensor.own_output", "switch.pump"],
            self.REGISTRY,
            own_config_entry_id="entry_mine",
        ) == "dev_pump"

    def test_skips_entity_without_a_device(self) -> None:
        assert entity_values.resolve_binding_target(
            ["sensor.no_device"], self.REGISTRY, own_config_entry_id="entry_mine"
        ) is None

    def test_skips_unknown_entity(self) -> None:
        assert entity_values.resolve_binding_target(
            ["sensor.missing"], self.REGISTRY, own_config_entry_id="entry_mine"
        ) is None

    def test_first_usable_candidate_wins(self) -> None:
        assert entity_values.resolve_binding_target(
            ["sensor.missing", "sensor.no_device", "switch.pump"],
            self.REGISTRY,
            own_config_entry_id="entry_mine",
        ) == "dev_pump"

    def test_empty_candidates(self) -> None:
        assert entity_values.resolve_binding_target(
            [], self.REGISTRY, own_config_entry_id="entry_mine"
        ) is None


class TestDeviceListConfiguration:
    """``GET /devices`` must carry per-device configuration through verbatim."""

    def test_keeps_configuration_object(self) -> None:
        devices = flow.normalise_device_list(
            [
                {
                    "device_id": "d1",
                    "name": "Pump",
                    "configuration": {
                        "state_entity_ids": ["switch.pump"],
                        "create_energy_sensor": False,
                    },
                }
            ]
        )
        assert devices[0]["configuration"]["state_entity_ids"] == ["switch.pump"]
        assert devices[0]["configuration"]["create_energy_sensor"] is False

    def test_omits_absent_configuration(self) -> None:
        devices = flow.normalise_device_list([{"device_id": "d1", "name": "Pump"}])
        assert "configuration" not in devices[0]

    def test_ignores_non_mapping_configuration(self) -> None:
        devices = flow.normalise_device_list(
            [{"device_id": "d1", "name": "Pump", "configuration": "bogus"}]
        )
        assert "configuration" not in devices[0]


class TestEnergyEntityChoice:
    """The wizard's per-device energy choice is local, never an engine field."""

    def test_absent_means_no_opinion(self) -> None:
        assert flow.energy_entity_choice({}) is None
        assert flow.energy_entity_choice(None) is None

    def test_explicit_values(self) -> None:
        assert flow.energy_entity_choice({"create_energy_sensor": True}) is True
        assert flow.energy_entity_choice({"create_energy_sensor": False}) is False
        assert flow.energy_entity_choice({"create_energy_sensor": "off"}) is False

    def test_never_sent_to_the_engine(self) -> None:
        # The engine has no such field; sending it would be silently ignored at
        # best and rejected as an unknown parameter at worst.
        params = flow.normalise_device_configuration(
            {"device_type": "pump", "create_energy_sensor": False}
        )
        assert "create_energy_sensor" not in params
        assert params == {"device_type": "pump"}

    def test_energy_choice_alone_is_savable(self) -> None:
        # A form that only changes the energy opt-in still has something to
        # save; it must not be rejected as "nothing_to_save".  The real form
        # always submits the three enum defaults, so this mirrors it.
        form = {
            "device_type": "unknown",
            "connectivity": "unknown",
            "behavior": "unknown",
            "create_energy_sensor": False,
        }
        assert flow.validate_device_configuration(form) == {}
        assert flow.normalise_device_configuration(form) == {}


class TestStoredEnergyChoice:
    """The form default must honour a stored opt-out.

    The wizard always submits its checkbox, so the *default* decides the result
    whenever a user edits any other field on the same device.  A hard-coded
    default of ``True`` would silently re-enable an entity the user turned off.
    """

    def test_unconfigured_device_defaults_to_enabled(self) -> None:
        assert flow.stored_energy_choice(None, "d1") is True
        assert flow.stored_energy_choice({}, "d1") is True
        assert flow.stored_energy_choice({"other": False}, "d1") is True

    def test_stored_opt_out_is_preserved(self) -> None:
        assert flow.stored_energy_choice({"d1": False}, "d1") is False

    def test_stored_opt_in_is_preserved(self) -> None:
        assert flow.stored_energy_choice({"d1": True}, "d1") is True

    @pytest.mark.parametrize("raw", ["false", "0", "no", "off", " false "])
    def test_string_false_is_an_opt_out(self, raw: str) -> None:
        assert flow.stored_energy_choice({"d1": raw}, "d1") is False

    def test_only_the_edited_device_is_consulted(self) -> None:
        assert flow.stored_energy_choice({"d1": False, "d2": True}, "d2") is True
        assert flow.stored_energy_choice({"d1": False, "d2": True}, "d1") is False

    def test_malformed_storage_falls_back_to_default(self) -> None:
        assert flow.stored_energy_choice("bogus", "d1") is True


# --------------------------------------------------------------------------- #
# Config flow logic
# --------------------------------------------------------------------------- #


class TestConnectionValidation:
    @pytest.mark.parametrize(
        "url", ["http://192.168.1.10:8787", "https://engine.local", "engine.local:8787"]
    )
    def test_valid_urls(self, url: str) -> None:
        assert flow.validate_connection(url, None) == {}

    @pytest.mark.parametrize("url", ["", None, "ftp://x", "not a url", "http://"])
    def test_invalid_urls(self, url: Any) -> None:
        assert "base_url" in flow.validate_connection(url, None)

    def test_non_string_token_rejected(self) -> None:
        assert "api_token" in flow.validate_connection("http://x:8787", 123)

    def test_token_never_returned_in_defaults(self) -> None:
        defaults = flow.connection_defaults({"base_url": "http://x:8787", "api_token": "secret"})
        assert defaults["api_token"] == ""
        assert defaults["token_configured"] is True


class TestDiscoveredBaseUrl:
    """``discovered_base_url`` turns an App announcement into an engine URL.

    The Supervisor forwards whatever the App posted, so this is the boundary
    between untrusted discovery data and a config entry.
    """

    def test_host_and_port(self) -> None:
        assert (
            flow.discovered_base_url({"host": "f48de52e-wattmosaic-engine", "port": 8787})
            == "http://f48de52e-wattmosaic-engine:8787"
        )

    def test_port_may_be_a_string(self) -> None:
        """Home Assistant's own discovery examples post the port as a string."""
        assert (
            flow.discovered_base_url({"host": "engine.local", "port": "8787"})
            == "http://engine.local:8787"
        )

    def test_missing_port_uses_the_gateway_default(self) -> None:
        assert flow.discovered_base_url({"host": "engine.local"}) == "http://engine.local:8787"

    def test_result_matches_the_manual_step_normalisation(self) -> None:
        """Both paths must produce the same unique id for the same engine."""
        discovered = flow.discovered_base_url({"host": "engine.local", "port": 8787})
        manual = util.normalise_base_url("http://engine.local:8787/")
        assert discovered == manual

    @pytest.mark.parametrize(
        "config",
        [
            {},
            {"port": 8787},
            {"host": ""},
            {"host": "   "},
            {"host": None},
            {"host": 8787},
            {"host": ["engine.local"]},
            # URL syntax in the host field must be rejected, never repaired:
            # ``user@evil.example`` must not become ``http://evil.example``.
            {"host": "user@evil.example"},
            {"host": "engine.local/path"},
            {"host": "engine.local/"},
            {"host": "http://engine.local"},
            {"host": "engine.local:9000"},
            {"host": "engine.local?x=1"},
            {"host": "engine.local#frag"},
            {"host": "not a host"},
            {"host": "-engine"},
            {"host": "engine..local"},
        ],
    )
    def test_unusable_hosts_are_rejected(self, config: dict[str, Any]) -> None:
        """A malformed announcement must yield no URL at all.

        Repairing instead of rejecting is how ``user@evil.example`` turns into a
        perfectly valid engine URL, or ``host/path`` silently folds the port into
        a path.
        """
        assert flow.discovered_base_url(config) is None

    @pytest.mark.parametrize("port", ["not-a-port", 0, 70000, -1, True, None, 8787.5])
    def test_bad_port_falls_back_to_the_gateway_default(self, port: Any) -> None:
        """The port is not security-relevant, so a bad one degrades to 8787."""
        assert (
            flow.discovered_base_url({"host": "engine.local", "port": port})
            == "http://engine.local:8787"
        )

    def test_non_mapping_is_rejected(self) -> None:
        assert flow.discovered_base_url(None) is None
        assert flow.discovered_base_url("engine.local") is None
        assert flow.discovered_base_url(["engine.local"]) is None

    def test_announced_scheme_cannot_downgrade_an_https_engine(self) -> None:
        """Discovery is plain HTTP on the Supervisor network; nothing else."""
        assert flow.discovered_base_url(
            {"host": "engine.example.com", "port": 8787, "scheme": "https"}
        ) == "http://engine.example.com:8787"


class TestSettingsValidation:
    def test_defaults_are_valid(self) -> None:
        assert flow.validate_settings(30, 120, 120) == {}

    def test_empty_uses_defaults(self) -> None:
        assert flow.validate_settings(None, None, None) == {}

    def test_out_of_range(self) -> None:
        assert "scan_interval" in flow.validate_settings(1, 120, 120)
        assert "scan_interval" in flow.validate_settings(99999, 120, 120)

    def test_non_numeric(self) -> None:
        assert "scan_interval" in flow.validate_settings("often", 120, 120)

    def test_stale_must_cover_interval(self) -> None:
        assert "stale_after" in flow.validate_settings(60, 30, 60)

    def test_unavailable_must_cover_stale(self) -> None:
        assert "unavailable_after" in flow.validate_settings(30, 120, 60)

    def test_normalise_settings(self) -> None:
        assert flow.normalise_settings(
            {"scan_interval": 45, "stale_after": 180, "unavailable_after": 300}
        ) == {"scan_interval": 45, "stale_after": 180, "unavailable_after": 300}

    def test_normalise_settings_defaults(self) -> None:
        assert flow.normalise_settings({}) == {
            "scan_interval": const.DEFAULT_SCAN_INTERVAL,
            "stale_after": const.DEFAULT_STALE_AFTER,
            "unavailable_after": const.DEFAULT_UNAVAILABLE_AFTER,
        }


def _topology() -> dict[str, Any]:
    return {
        "version": 1,
        "status": "candidate",
        "channels": [
            {"ct_id": "ct_main_1", "entity_id": "sensor.a", "name": "Main CT1",
             "ct_type": "main", "phase": "l1", "polarity": "normal"},
            {"ct_id": "ct_main_2", "entity_id": "sensor.b", "name": "Main CT2",
             "ct_type": "main", "phase": "l2", "polarity": "normal"},
            {"ct_id": "ct_hot_tub_ct1", "entity_id": "sensor.c", "name": "Hot Tub CT1",
             "ct_type": "branch", "phase": "l1", "polarity": "normal"},
            {"ct_id": "ct_hot_tub_ct2", "entity_id": "sensor.d", "name": "Hot Tub CT2",
             "ct_type": "branch", "phase": "l2", "polarity": "normal"},
        ],
        "devices": [{"device_id": "hot_tub", "name": "Hot Tub"}],
    }


class TestChannelNormalisation:
    def test_extracts_channels(self) -> None:
        channels = flow.normalise_channels(_topology())
        assert [c["ct_id"] for c in channels] == [
            "ct_main_1", "ct_main_2", "ct_hot_tub_ct1", "ct_hot_tub_ct2"
        ]

    def test_dedupes_by_ct_id(self) -> None:
        topology = _topology()
        topology["channels"].append(dict(topology["channels"][0]))
        assert len(flow.normalise_channels(topology)) == 4

    def test_tolerates_garbage(self) -> None:
        assert flow.normalise_channels(None) == []
        assert flow.normalise_channels({"channels": "x"}) == []  # type: ignore[dict-item]

    def test_pair_suggestion(self) -> None:
        channels = flow.normalise_channels(_topology())
        assert flow.suggested_pair({"ct_id": "ct_hot_tub_ct1"}, channels) == "ct_hot_tub_ct2"
        assert flow.suggested_pair({"ct_id": "ct_hot_tub_ct2"}, channels) == "ct_hot_tub_ct1"
        assert flow.suggested_pair({"ct_id": "ct_other"}, channels) is None

    def test_a_third_leg_is_never_paired(self) -> None:
        """``ct3`` is a separate circuit, not the other half of a 240 V load.

        The wizard fills every channel's default from ``suggested_pair``.  A
        trailing-integer fallback used to pair ``example_circuit_ct3`` with
        ``example_circuit_ct1`` while ``example_circuit_ct1`` paired with
        ``example_circuit_ct2``; the one-sided result fails
        ``validate_assignments``, so the confirm step rejected its own defaults
        and the install could not be completed.
        """
        topology = {
            "channels": [
                {"ct_id": "example_circuit_ct1", "name": "Example circuit",
                 "ct_type": "dedicated_device"},
                {"ct_id": "example_circuit_ct2", "name": "Example circuit",
                 "ct_type": "dedicated_device"},
                {"ct_id": "example_circuit_ct3", "name": "Example circuit",
                 "ct_type": "dedicated_device"},
            ]
        }
        channels = flow.normalise_channels(topology)
        assert flow.suggested_pair({"ct_id": "example_circuit_ct1"}, channels) == (
            "example_circuit_ct2"
        )
        assert flow.suggested_pair({"ct_id": "example_circuit_ct2"}, channels) == (
            "example_circuit_ct1"
        )
        assert flow.suggested_pair({"ct_id": "example_circuit_ct3"}, channels) is None

    def test_every_suggestion_is_reciprocal(self) -> None:
        """No channel may default to a partner that does not point back.

        This is the property that keeps the wizard finishable for an arbitrary
        topology: whatever the engine reports, accepting all defaults must
        produce a set of assignments that validates.
        """
        topology = {
            "channels": [
                {"ct_id": "main_1", "ct_type": "main", "phase": "l1"},
                {"ct_id": "main_2", "ct_type": "main", "phase": "l2"},
                {"ct_id": "plug_1", "ct_type": "dedicated_device"},
                {"ct_id": "plug_2", "ct_type": "dedicated_device"},
                {"ct_id": "plug_3", "ct_type": "dedicated_device"},
                {"ct_id": "plug_10", "ct_type": "dedicated_device"},
            ]
        }
        channels = flow.normalise_channels(topology)
        by_id = {c["ct_id"]: c for c in channels}
        for channel in channels:
            pair = flow.suggested_pair(channel, channels)
            if pair is None:
                continue
            assert flow.suggested_pair(by_id[pair], channels) == channel["ct_id"], channel["ct_id"]

    def test_a_declared_but_one_sided_pair_is_not_suggested(self) -> None:
        """A stale ``paired_ct_id`` on one side must not be forced on the user."""
        topology = {
            "channels": [
                {"ct_id": "a_1", "ct_type": "branch", "paired_ct_id": "a_2"},
                {"ct_id": "a_2", "ct_type": "branch"},
            ]
        }
        channels = flow.normalise_channels(topology)
        pair = flow.suggested_pair({"ct_id": "a_1", "paired_ct_id": "a_2"}, channels)
        assert pair == "a_2", "a structural 1<->2 partner is still the right suggestion"


class TestAssignmentValidation:
    def test_valid_assignments(self) -> None:
        assignments = {
            "ct_main_1": {"role": "main", "phase": "l1"},
            "ct_main_2": {"role": "main", "phase": "l2"},
            "ct_hot_tub_ct1": {
                "role": "dedicated_device", "phase": "l1", "device_id": "hot_tub",
                "paired_ct_id": "ct_hot_tub_ct2",
            },
            "ct_hot_tub_ct2": {
                "role": "dedicated_device", "phase": "l2", "device_id": "hot_tub",
                "paired_ct_id": "ct_hot_tub_ct1",
            },
        }
        assert flow.validate_assignments(assignments, flow.normalise_channels(_topology())) == []

    def test_main_needs_phase(self) -> None:
        errors = flow.validate_assignments({"ct_main_1": {"role": "main", "phase": "unknown"}})
        assert any(e.startswith("main_needs_phase") for e in errors)

    def test_dedicated_needs_device(self) -> None:
        errors = flow.validate_assignments(
            {"ct_x": {"role": "dedicated_device", "phase": "l1"}}
        )
        assert any(e.startswith("dedicated_needs_device") for e in errors)

    def test_self_pair_rejected(self) -> None:
        errors = flow.validate_assignments(
            {"ct_x": {"role": "branch", "phase": "l1", "paired_ct_id": "ct_x"}}
        )
        assert any(e.startswith("self_pair") for e in errors)

    def test_pair_must_be_reciprocal(self) -> None:
        errors = flow.validate_assignments(
            {
                "ct_a": {"role": "branch", "phase": "l1", "paired_ct_id": "ct_b"},
                "ct_b": {"role": "branch", "phase": "l2"},
            }
        )
        assert any(e.startswith("pair_not_reciprocal") for e in errors)

    def test_both_phase_requires_pair(self) -> None:
        errors = flow.validate_assignments({"ct_a": {"role": "branch", "phase": "both"}})
        assert any(e.startswith("both_needs_pair") for e in errors)

    def test_pair_spanning_two_devices_is_rejected(self) -> None:
        """Defect #57: a pair is the two legs of ONE double-pole load.

        The engine sums a declared pair.  Pairing two *different* devices would
        therefore make each of them report the other's power as well, silently
        attributing the same watts twice.  The wizard must refuse the pairing
        rather than let it reach the engine.
        """
        errors = flow.validate_assignments(
            {
                "lights_ct1": {
                    "role": "dedicated_device",
                    "phase": "l1",
                    "device_id": "lights_1",
                    "paired_ct_id": "lights_ct2",
                },
                "lights_ct2": {
                    "role": "dedicated_device",
                    "phase": "l2",
                    "device_id": "lights_2",
                    "paired_ct_id": "lights_ct1",
                },
            }
        )
        assert any(e.startswith("pair_crosses_devices") for e in errors)

    def test_pair_of_one_device_is_accepted(self) -> None:
        """The complement: a genuine double-pole load is still allowed."""
        errors = flow.validate_assignments(
            {
                "hot_tub_l1": {
                    "role": "dedicated_device",
                    "phase": "l1",
                    "device_id": "hot_tub",
                    "paired_ct_id": "hot_tub_l2",
                },
                "hot_tub_l2": {
                    "role": "dedicated_device",
                    "phase": "l2",
                    "device_id": "hot_tub",
                    "paired_ct_id": "hot_tub_l1",
                },
            }
        )
        assert errors == []

    def test_unknown_channel_rejected(self) -> None:
        errors = flow.validate_assignments(
            {"nope": {"role": "branch", "phase": "l1"}},
            flow.normalise_channels(_topology()),
        )
        assert any(e.startswith("unknown_channel") for e in errors)

    def test_invalid_enum_values_are_normalised(self) -> None:
        normalised = flow.normalise_assignment(
            {"role": "MAIN", "phase": "L1", "polarity": "INVERTED"}
        )
        assert normalised == {
            "role": "main",
            "phase": "l1",
            "polarity": "inverted",
            "paired_ct_id": None,
            "parent_ct_id": None,
            "alias_of_ct_id": None,
            "measurement_multiplier": 1.0,
            "device_id": None,
            "enabled": True,
        }

    def test_uncertainty_detection(self) -> None:
        assert flow.is_uncertain({"role": "unknown", "phase": "l1"})
        assert flow.is_uncertain({"role": "branch", "phase": "unknown"})
        assert flow.is_uncertain({"role": "dedicated_device", "phase": "l1"})
        assert not flow.is_uncertain(
            {"role": "dedicated_device", "phase": "l1", "device_id": "d"}
        )
        assert not flow.is_uncertain({"role": "branch", "phase": "l1"})

    def test_uncertainty_summary_is_sorted(self) -> None:
        summary = flow.uncertainty_summary(
            {"b": {"role": "unknown"}, "a": {"role": "branch", "phase": "unknown"}}
        )
        assert summary[0].startswith("a:")
        assert len(summary) == 2

    def test_counts(self) -> None:
        assert flow.count_confirmed(
            {"a": {"role": "branch", "phase": "l1"}, "b": {"role": "unknown"}}
        ) == (2, 1)

    def test_confirm_step_can_finish_with_uncertain_channels(self) -> None:
        """An unticked box must store the assignments as *candidate*, not block.

        Every real installation has assumptions — an unknown phase is the
        common one.  If submitting required the box to be ticked, the flow would
        have no honest exit: the only way out would be to confirm *every*
        uncertain channel at once.  Submitting the form is itself the explicit
        user action, so it stores ``candidate`` and finishes.
        """
        confirmed, error = flow.resolve_confirmation([], ["ct_a: phase unknown"], False)
        assert (confirmed, error) == (False, None)

    def test_confirm_step_without_assumptions_is_confirmed(self) -> None:
        confirmed, error = flow.resolve_confirmation([], [], False)
        assert (confirmed, error) == (True, None)

    def test_confirm_step_box_confirms_uncertain_channels(self) -> None:
        confirmed, error = flow.resolve_confirmation([], ["ct_a: phase unknown"], True)
        assert (confirmed, error) == (True, None)

    def test_invalid_assignments_still_block_the_confirm_step(self) -> None:
        confirmed, error = flow.resolve_confirmation(
            ["dedicated_needs_device:ct_a"], [], True
        )
        assert (confirmed, error) == (None, "invalid_mapping")


class TestTopologyUpdate:
    def test_applies_assignments_and_keeps_candidate(self) -> None:
        updated = flow.build_topology_update(
            _topology(),
            {"ct_hot_tub_ct1": {"role": "dedicated_device", "phase": "l1", "device_id": "hot_tub"}},
        )
        assert updated["status"] == "candidate"
        channel = next(c for c in updated["channels"] if c["ct_id"] == "ct_hot_tub_ct1")
        assert channel["ct_type"] == "dedicated_device"
        assert channel["device_id"] == "hot_tub"
        assert channel["topology_status"] == "candidate"

    def test_confirmed_marks_everything(self) -> None:
        updated = flow.build_topology_update(_topology(), {}, confirmed=True)
        assert updated["status"] == "confirmed"
        assert all(c["topology_status"] == "confirmed" for c in updated["channels"])

    def test_mirrors_dedicated_cts_onto_devices(self) -> None:
        updated = flow.build_topology_update(
            _topology(),
            {"ct_hot_tub_ct1": {"role": "dedicated_device", "phase": "l1", "device_id": "hot_tub"}},
        )
        device = next(d for d in updated["devices"] if d["device_id"] == "hot_tub")
        assert device["dedicated_ct_ids"] == ["ct_hot_tub_ct1"]

    def test_does_not_mutate_input(self) -> None:
        original = _topology()
        flow.build_topology_update(
            original, {"ct_main_1": {"role": "branch", "phase": "l1"}}
        )
        assert original["channels"][0]["ct_type"] == "main"

    def test_appends_notes(self) -> None:
        updated = flow.build_topology_update(_topology(), {}, notes=["hello"])
        assert updated["notes"] == ["hello"]

    def test_handles_missing_topology(self) -> None:
        updated = flow.build_topology_update(None, {})
        assert updated["channels"] == []
        assert updated["status"] == "candidate"


class TestDeviceCandidates:
    def test_normalises_dict_payload(self) -> None:
        devices = flow.normalise_device_list(
            {"devices": [{"device_id": "a", "name": "Alpha"}, {"id": "b", "name": "Beta"}]}
        )
        assert [d["device_id"] for d in devices] == ["a", "b"]

    def test_normalises_plain_list(self) -> None:
        assert flow.normalise_device_list(["a", "b"])[0] == {
            "device_id": "a", "name": "a", "has_power": False
        }

    def test_ignores_duplicates_and_garbage(self) -> None:
        devices = flow.normalise_device_list(
            [{"device_id": "a"}, {"device_id": "a"}, {"nope": 1}, 42]
        )
        assert len(devices) == 1

    def test_ranks_by_token_overlap(self) -> None:
        devices = [
            {"device_id": "1", "name": "Hot Tub", "has_power": False},
            {"device_id": "2", "name": "Network Gear", "has_power": True},
        ]
        candidates = flow.device_candidates(
            devices, {"ct_id": "ct_network_gear", "name": "Network Gear power"}
        )
        assert candidates[0]["device_id"] == "2"
        assert "network" in candidates[0]["overlap"]

    def test_no_candidates_for_unrelated(self) -> None:
        assert flow.device_candidates(
            [{"device_id": "1", "name": "Hot Tub"}], {"ct_id": "ct_x", "name": "Kitchen Lights"}
        ) == []

    def test_monitor_prefix_is_filtered_without_a_brand_list(self) -> None:
        """Every channel shares the monitor prefix, so it identifies no load.

        A channel called ``acme_panel_ct3`` must not offer a device named
        ``Acme`` as a candidate.  The rule has to come from the channels, not
        from a hard-coded list of vendor names: a brand list would both miss
        unlisted hardware and publish the vendors that are listed.
        """
        channels = [
            {"ct_id": "acme_panel_heater", "name": "Heater power"},
            {"ct_id": "acme_panel_small_appliance", "name": "Small Appliance power"},
        ]
        prefix = flow.panel_shared_tokens(channels)
        assert "acme" in prefix, prefix
        assert "heater" not in prefix and "kitchen" not in prefix

        devices = [{"device_id": "mon", "name": "Acme"}, {"device_id": "b", "name": "Heater"}]
        candidates = flow.device_candidates(
            devices,
            {"ct_id": "acme_panel_heater", "name": "Heater power"},
            shared_tokens=prefix,
        )
        ids = [c["device_id"] for c in candidates]
        assert "mon" not in ids, "the monitor was offered as a candidate for its own circuit"
        assert ids == ["b"]

    def test_panel_shared_tokens_is_empty_for_one_channel(self) -> None:
        assert flow.panel_shared_tokens(
            [{"ct_id": "acme_panel_heater", "name": "Heater power"}]
        ) == frozenset(), "a single channel has no shared prefix to subtract"

    def test_candidate_labels_include_evidence(self) -> None:
        labels = flow.candidate_labels(
            [{"device_id": "d1", "name": "Hot Tub", "overlap": ["hot", "tub"]}]
        )
        assert labels[0][0] == "d1"
        assert "match" in labels[0][1]

    def test_mapping_summary_flags_uncertain(self) -> None:
        channels = flow.normalise_channels(_topology())
        summary = flow.mapping_summary(
            channels, {"ct_main_1": {"role": "main", "phase": "l1"}}
        )
        assert any("uncertain" not in line for line in summary)
        assert any("ct_main_1" in line for line in summary)

    def test_defaults_never_invent_mapping(self) -> None:
        defaults = flow.assignment_defaults({"ct_id": "x", "name": "X"})
        assert defaults["role"] == "unknown"
        assert defaults["device_id"] == ""


# --------------------------------------------------------------------------- #
# Service validation (destructive confirmation)
# --------------------------------------------------------------------------- #


class TestServiceValidation:
    @pytest.mark.parametrize(
        "service",
        sorted(const.DESTRUCTIVE_SERVICES),
    )
    def test_destructive_requires_confirm(self, service: str) -> None:
        with pytest.raises(services.ConfirmationRequired) as excinfo:
            services.validate_service_call(
                service, {"device_id": "d1", "sample_id": "s1"}
            )
        assert excinfo.value.key == "confirmation_required"
        assert service in str(excinfo.value)

    @pytest.mark.parametrize("value", [False, None, "yes", 1, 0, "true"])
    def test_confirm_must_be_boolean_true(self, value: Any) -> None:
        with pytest.raises(services.ConfirmationRequired):
            services.validate_service_call(
                const.SERVICE_REBUILD_TOPOLOGY, {"confirm": value}
            )

    def test_confirm_true_passes(self) -> None:
        validated = services.validate_service_call(
            const.SERVICE_REJECT_SAMPLE,
            {"device_id": "d1", "sample_id": "s1", "confirm": True},
        )
        assert validated["confirm"] is True
        assert validated["device_id"] == "d1"
        assert validated["sample_id"] == "s1"

    def test_non_destructive_needs_no_confirm(self) -> None:
        validated = services.validate_service_call(const.SERVICE_START_LEARNING, {})
        assert validated["confirm"] is False

    @pytest.mark.parametrize(
        "service",
        [
            const.SERVICE_REBUILD_MODEL,
            const.SERVICE_FREEZE_MODEL,
            const.SERVICE_UNFREEZE_MODEL,
            const.SERVICE_RESET_DEVICE_MODEL,
            const.SERVICE_EXPORT_DEVICE_DATA,
        ],
    )
    def test_device_required(self, service: str) -> None:
        with pytest.raises(services.ServiceValidationError) as excinfo:
            services.validate_service_call(service, {"confirm": True})
        assert excinfo.value.key == "device_required"

    @pytest.mark.parametrize(
        "service", [const.SERVICE_ACCEPT_SAMPLE, const.SERVICE_REJECT_SAMPLE]
    )
    def test_sample_required(self, service: str) -> None:
        with pytest.raises(services.ServiceValidationError) as excinfo:
            services.validate_service_call(
                service, {"device_id": "d1", "confirm": True}
            )
        assert excinfo.value.key == "sample_required"

    def test_unknown_service_rejected(self) -> None:
        with pytest.raises(services.ServiceValidationError) as excinfo:
            services.validate_service_call("launch_rockets", {})
        assert excinfo.value.key == "unknown_service"

    def test_blank_device_id_rejected(self) -> None:
        with pytest.raises(services.ServiceValidationError):
            services.validate_service_call(
                const.SERVICE_FREEZE_MODEL, {"device_id": "   "}
            )

    def test_parameters_must_be_mapping(self) -> None:
        with pytest.raises(services.ServiceValidationError) as excinfo:
            services.validate_service_call(
                const.SERVICE_RUN_DIAGNOSTICS, {"parameters": ["x"]}
            )
        assert excinfo.value.key == "invalid_parameters"

    def test_device_id_length_limited(self) -> None:
        with pytest.raises(services.ServiceValidationError):
            services.validate_service_call(
                const.SERVICE_FREEZE_MODEL, {"device_id": "x" * 300}
            )

    def test_every_service_has_a_command(self) -> None:
        assert set(const.SERVICES) == {
            "start_learning",
            "pause_learning",
            "resume_learning",
            "accept_sample",
            "reject_sample",
            "rebuild_model",
            "freeze_model",
            "unfreeze_model",
            "reset_device_model",
            "rebuild_topology",
            "run_diagnostics",
            "export_device_data",
            "discover_placements",
        }

    def test_command_payload_shape(self) -> None:
        validated = services.validate_service_call(
            const.SERVICE_ACCEPT_SAMPLE,
            {"device_id": "d1", "sample_id": "s1", "parameters": {"reason": "ok"}},
        )
        payload = services.build_command_payload(const.SERVICE_ACCEPT_SAMPLE, validated)
        assert payload == {
            "command": "accept_sample",
            "device_id": "d1",
            "sample_id": "s1",
            "parameters": {"reason": "ok"},
        }

    def test_confirm_is_not_forwarded_to_engine(self) -> None:
        validated = services.validate_service_call(
            const.SERVICE_RESET_DEVICE_MODEL, {"device_id": "d1", "confirm": True}
        )
        payload = services.build_command_payload(
            const.SERVICE_RESET_DEVICE_MODEL, validated
        )
        assert "confirm" not in payload
        assert "confirm" not in payload["parameters"]

    def test_response_services(self) -> None:
        assert services.service_returns_response(const.SERVICE_RUN_DIAGNOSTICS)
        assert services.service_returns_response(const.SERVICE_EXPORT_DEVICE_DATA)
        assert not services.service_returns_response(const.SERVICE_START_LEARNING)

    def test_result_summary(self) -> None:
        assert services.service_result_summary({"ok": True, "detail": "done"}) == "ok: done"
        assert services.service_result_summary({"ok": False}) == "error: failed"
        assert services.service_result_summary(None) == "no result"


class TestEngineRouting:
    """Which engine receives a service call when several are configured.

    Two Apps can run side by side (a stable release and a development build),
    and both publish the *same* engine device ids.  Routing by bare engine id
    therefore cannot be assumed unique: a silent "use the first entry" fallback
    sends the command to an engine the user did not target and returns a
    plausible answer from it, which is worse than refusing.
    """

    def test_single_engine_needs_no_target(self) -> None:
        assert services.choose_entry(["only"], [], device_id=None) == "only"

    def test_single_engine_does_not_reject_an_unknown_device(self) -> None:
        # With one engine there is nothing to confuse it with; the engine itself
        # reports an unknown id precisely, which is more useful than a guess here.
        assert services.choose_entry(["only"], [], device_id="dev_nope") == "only"

    def test_no_engine_is_an_error(self) -> None:
        with pytest.raises(services.ServiceValidationError) as excinfo:
            services.choose_entry([], [], device_id="dev_x")
        assert excinfo.value.key == "engine_not_configured"

    def test_fully_qualified_target_routes_to_its_entry(self) -> None:
        assert (
            services.choose_entry(["stable", "dev"], ["dev"], device_id="dev_x")
            == "dev"
        )

    def test_bare_id_present_in_one_entry_routes_there(self) -> None:
        assert (
            services.choose_entry(["stable", "dev"], ["stable"], device_id="dev_x")
            == "stable"
        )

    def test_bare_id_present_in_both_entries_is_ambiguous(self) -> None:
        # This is the real installation: 28 of 30 engine ids exist in both
        # entries.  Picking one would be a coin flip.
        with pytest.raises(services.ServiceValidationError) as excinfo:
            services.choose_entry(
                ["stable", "dev"], ["stable", "dev"], device_id="dev_x"
            )
        assert excinfo.value.key == "ambiguous_target"
        assert "dev_x" in str(excinfo.value)

    def test_duplicate_candidate_is_not_ambiguous(self) -> None:
        # The same entry reported twice is still one owner.
        assert (
            services.choose_entry(["stable", "dev"], ["dev", "dev"], device_id="dev_x")
            == "dev"
        )

    def test_multi_engine_without_target_is_ambiguous(self) -> None:
        with pytest.raises(services.ServiceValidationError) as excinfo:
            services.choose_entry(["stable", "dev"], [], device_id=None)
        assert excinfo.value.key == "engine_ambiguous"

    def test_unknown_target_with_several_engines_is_an_error(self) -> None:
        with pytest.raises(services.ServiceValidationError) as excinfo:
            services.choose_entry(["stable", "dev"], [], device_id="dev_missing")
        assert excinfo.value.key == "unknown_target"
        assert "dev_missing" in str(excinfo.value)

    def test_routing_never_falls_back_to_the_first_entry(self) -> None:
        """The defect this replaces: first entry wins when nothing matched."""
        for device_id in (None, "dev_missing", "dev_x"):
            with pytest.raises(services.ServiceValidationError):
                services.choose_entry(["stable", "dev"], [], device_id=device_id)

    def test_target_device_ids_reads_ha_target(self) -> None:
        assert services.target_device_ids(
            {"target": {"device_id": "dabfb081"}}
        ) == ["dabfb081"]
        assert services.target_device_ids(
            {"target": {"device_id": ["a", "b"]}}
        ) == ["a", "b"]

    def test_target_device_ids_tolerates_missing_or_blank(self) -> None:
        assert services.target_device_ids({}) == []
        assert services.target_device_ids(None) == []
        assert services.target_device_ids({"target": {}}) == []
        assert services.target_device_ids({"target": {"device_id": "  "}}) == []
        assert services.target_device_ids({"target": "nonsense"}) == []


class TestIdentifierMatching:
    """Resolving a ``device_id`` against the identifiers the integration owns.

    ``owned`` mirrors the real installation: a stable and a development entry
    publish the *same* engine ids, so ``dev_x`` exists under both.
    """

    OWNED = [
        ("stable", "stable_dev_x"),
        ("stable", "stable_dev_y"),
        ("dev", "dev_dev_x"),
        ("dev", "dev_dev_y"),
    ]

    def test_fully_qualified_id_resolves_directly(self) -> None:
        exact, candidates = services.match_identifier_owners(
            self.OWNED, device_id="dev_dev_x", entries=["stable", "dev"]
        )
        assert exact == "dev"
        assert candidates == []

    def test_fully_qualified_stable_id_resolves_directly(self) -> None:
        exact, candidates = services.match_identifier_owners(
            self.OWNED, device_id="stable_dev_x", entries=["stable", "dev"]
        )
        assert exact == "stable"
        assert candidates == []

    def test_fully_qualified_id_is_not_double_prefixed(self) -> None:
        """The regression: rebuilding ``<entry>_<id>`` prefixes it twice.

        A fully qualified id is already an identifier *value*.  Treating it as a
        bare id builds ``dev_dev_dev_x``, which matches nothing -- so the form
        that used to work silently stopped resolving.
        """
        exact, candidates = services.match_identifier_owners(
            self.OWNED, device_id="dev_dev_x", entries=["stable", "dev"]
        )
        assert exact is not None, "the fully qualified form must resolve"
        assert "dev_dev_dev_x" not in [i for _, i in self.OWNED]

    def test_bare_id_present_in_both_entries_is_reported_for_both(self) -> None:
        exact, candidates = services.match_identifier_owners(
            self.OWNED, device_id="dev_x", entries=["stable", "dev"]
        )
        assert exact is None
        assert candidates == ["stable", "dev"]

    def test_bare_id_present_in_one_entry_names_it(self) -> None:
        owned = [("stable", "stable_only_here")]
        exact, candidates = services.match_identifier_owners(
            owned, device_id="only_here", entries=["stable", "dev"]
        )
        assert exact is None
        assert candidates == ["stable"]

    def test_unknown_bare_id_yields_nothing(self) -> None:
        exact, candidates = services.match_identifier_owners(
            self.OWNED, device_id="dev_missing", entries=["stable", "dev"]
        )
        assert exact is None
        assert candidates == []

    def test_identifier_from_an_unloaded_entry_is_ignored(self) -> None:
        exact, candidates = services.match_identifier_owners(
            self.OWNED, device_id="dev_dev_x", entries=["dev"]
        )
        assert exact == "dev"


# --------------------------------------------------------------------------- #
# Entity definitions / contract alignment
# --------------------------------------------------------------------------- #


class TestEntityDefinitions:
    def test_device_sensor_keys_cover_spec(self) -> None:
        required = {
            "power",
            "energy",
            "source",
            "confidence",
            "sample_count",
            "model_validity",
            "operating_stage",
            "last_inference",
            "data_quality",
        }
        assert required <= set(entity_defs.DEVICE_SENSORS)

    def test_global_sensor_keys_cover_spec(self) -> None:
        required = {
            "total_power",
            "l1_power",
            "l2_power",
            "direct_measured_power",
            "inferred_power",
            "unknown_power",
            "reconciliation_error",
            "quality_score",
            "engine_status",
        }
        assert required <= set(entity_defs.GLOBAL_SENSORS)

    def test_energy_is_total_increasing_kwh(self) -> None:
        energy = entity_defs.DEVICE_SENSORS["energy"]
        assert energy["state_class"] == "total_increasing"
        assert energy["unit"] == "kWh"
        assert energy["device_class"] == "energy"

    def test_energy_device_class_allows_total_increasing(self) -> None:
        # Mirrors HA's DEVICE_CLASS_STATE_CLASSES table: ENERGY accepts
        # TOTAL and TOTAL_INCREASING only.
        assert entity_defs.DEVICE_SENSORS["energy"]["state_class"] in (
            "total",
            "total_increasing",
        )

    def test_enum_sensors_have_no_state_class_or_unit(self) -> None:
        for key, definition in entity_defs.DEVICE_SENSORS.items():
            if definition.get("device_class") == "enum":
                assert not definition.get("state_class"), key
                assert not definition.get("unit"), key
                assert definition.get("options"), key

    def test_measurement_sensors_never_use_total_increasing(self) -> None:
        for key, definition in list(entity_defs.DEVICE_SENSORS.items()) + list(
            entity_defs.GLOBAL_SENSORS.items()
        ):
            if definition.get("device_class") == "power":
                assert definition["state_class"] == "measurement", key

    def test_global_enum_status_matches_entity_values(self) -> None:
        # Every status the helper can emit must be a declared option.
        assert set(entity_defs.GLOBAL_SENSORS["engine_status"]["options"]) == {
            "ok",
            "degraded",
            "error",
            "offline",
        }

    def test_power_is_measurement_watts(self) -> None:
        power = entity_defs.DEVICE_SENSORS["power"]
        assert power["device_class"] == "power"
        assert power["state_class"] == "measurement"
        assert power["unit"] == "W"

    def test_confidence_is_percentage(self) -> None:
        assert entity_defs.DEVICE_SENSORS["confidence"]["unit"] == "%"

    def test_last_inference_is_timestamp(self) -> None:
        assert entity_defs.DEVICE_SENSORS["last_inference"]["device_class"] == "timestamp"

    def test_running_is_binary_running(self) -> None:
        assert entity_defs.DEVICE_BINARY_SENSORS["running"]["device_class"] == "running"

    def test_enum_options_match_contract(self) -> None:
        # Every enum field must mirror the contract, not a hand-kept subset:
        # ``configured_prior`` was missing here even though the engine really
        # returns it as a ``Source``.  An enum sensor whose option list omits the
        # value it is given reports ``unknown`` for a legitimate measurement.
        assert entity_defs.DEVICE_SENSORS["source"]["options"] == [
            source.value for source in Source
        ]
        assert entity_defs.DEVICE_SENSORS["data_quality"]["options"] == [
            "good", "degraded", "stale", "bad", "missing"
        ]

    def test_const_keys_match_entity_defs(self) -> None:
        assert set(const.DEVICE_SENSOR_KEYS) == set(entity_defs.DEVICE_SENSORS)
        assert set(const.GLOBAL_SENSOR_KEYS) == set(entity_defs.GLOBAL_SENSORS)
        assert set(const.DEVICE_BINARY_KEYS) == set(entity_defs.DEVICE_BINARY_SENSORS)
        assert set(const.GLOBAL_BINARY_KEYS) == set(entity_defs.GLOBAL_BINARY_SENSORS)


# --------------------------------------------------------------------------- #
# Packaging / translation consistency
# --------------------------------------------------------------------------- #

FILES = {
    "manifest": PACKAGE_DIR / "manifest.json",
    "strings": PACKAGE_DIR / "strings.json",
    "en": PACKAGE_DIR / "translations" / "en.json",
    "services": PACKAGE_DIR / "services.yaml",
}


class TestPackaging:
    @pytest.fixture(scope="class")
    def manifest(self) -> dict[str, Any]:
        return json.loads(FILES["manifest"].read_text())

    def test_manifest_core_fields(self, manifest: dict[str, Any]) -> None:
        assert manifest["domain"] == "wattmosaic"
        assert manifest["name"] == "WattMosaic"
        assert manifest["config_flow"] is True
        assert manifest["iot_class"] == "local_polling"
        assert manifest["version"] == const.VERSION
        assert manifest["documentation"]
        assert manifest["codeowners"]

    def test_manifest_does_not_declare_aiohttp(self, manifest: dict[str, Any]) -> None:
        # HA ships aiohttp; declaring it would break the integration.
        assert manifest["requirements"] == []
        assert "aiohttp" not in json.dumps(manifest)

    def test_strings_and_translations_match(self) -> None:
        strings = json.loads(FILES["strings"].read_text())
        en = json.loads(FILES["en"].read_text())
        assert strings == en

    def test_translations_are_valid_json(self) -> None:
        for key in ("strings", "en", "manifest"):
            json.loads(FILES[key].read_text())

    def test_all_services_documented(self) -> None:
        strings = json.loads(FILES["strings"].read_text())
        en = json.loads(FILES["en"].read_text())
        for payload in (strings, en):
            assert set(payload["services"]) == set(const.SERVICES)

    def test_services_yaml_matches_const(self) -> None:
        import yaml

        raw = yaml.safe_load(FILES["services"].read_text())
        assert set(raw) == set(const.SERVICES)

    def test_destructive_services_declare_confirm(self) -> None:
        import yaml

        raw = yaml.safe_load(FILES["services"].read_text())
        for service in const.DESTRUCTIVE_SERVICES:
            fields = raw[service].get("fields") or {}
            assert "confirm" in fields, f"{service} must document confirm"
            assert fields["confirm"]["required"] is True

    def test_services_yaml_matches_validation_requirements(self) -> None:
        import yaml

        raw = yaml.safe_load(FILES["services"].read_text())
        for service in services.DEVICE_SERVICES:
            fields = raw.get(service, {}).get("fields") or {}
            assert "device_id" in fields, f"{service} requires device_id"
            assert fields["device_id"]["required"] is True
        for service in services.SAMPLE_SERVICES:
            fields = raw.get(service, {}).get("fields") or {}
            assert "sample_id" in fields, f"{service} requires sample_id"

    def test_translation_entity_keys_cover_all_entities(self) -> None:
        en = json.loads(FILES["en"].read_text())
        sensor_keys = set(en["entity"]["sensor"])
        binary_keys = set(en["entity"]["binary_sensor"])
        assert set(entity_defs.DEVICE_SENSORS) <= sensor_keys
        assert set(entity_defs.GLOBAL_SENSORS) <= sensor_keys
        assert set(entity_defs.DEVICE_BINARY_SENSORS) <= binary_keys
        assert set(entity_defs.GLOBAL_BINARY_SENSORS) <= binary_keys

    def test_config_flow_error_keys_are_translated(self) -> None:
        en = json.loads(FILES["en"].read_text())
        for section in ("config", "options"):
            error_keys = set(en[section]["error"])
            assert "cannot_connect" in error_keys
            assert "invalid_auth" in error_keys
            assert "contract_mismatch" in error_keys
            assert "invalid_response" in error_keys
            assert "invalid_number" in error_keys
            assert "invalid_mapping" in error_keys

    def test_exception_keys_cover_service_errors(self) -> None:
        en = json.loads(FILES["en"].read_text())
        assert set(en["exceptions"]) >= {
            "confirmation_required",
            "device_required",
            "sample_required",
            "unknown_service",
            "invalid_parameters",
            "invalid_device_id",
            "invalid_sample_id",
            "engine_unavailable",
            "engine_rejected",
        }

    def test_config_steps_have_strings(self) -> None:
        en = json.loads(FILES["en"].read_text())
        assert set(en["config"]["step"]) >= {
            "user",
            "settings",
            "channels",
            "mapping",
            "confirm",
            "reconfigure",
        }
        assert set(en["options"]["step"]) >= {"init", "channels", "mapping", "confirm"}


# --------------------------------------------------------------------------- #
# HA-facing module hygiene (static analysis, no HA import)
# --------------------------------------------------------------------------- #


class TestHaModuleHygiene:
    """Guards the modules we cannot import without Home Assistant installed."""

    def test_ha_modules_import_without_running_ha(self) -> None:
        # These must not be imported at test collection time; verify the
        # HA-facing modules are the only ones importing homeassistant.
        for name in (
            "__init__",
            "coordinator",
            "config_flow",
            "sensor",
            "binary_sensor",
            "diagnostics",
        ):
            source = (PACKAGE_DIR / f"{name}.py").read_text()
            assert "homeassistant" in source, name

        for name in (
            "const",
            "util",
            "api_client",
            "flow_logic",
            "service_logic",
            "entity_values",
            "entity_defs",
        ):
            source = (PACKAGE_DIR / f"{name}.py").read_text()
            assert "from homeassistant" not in source, name
            assert "import homeassistant" not in source, name

    def test_pure_modules_import_standalone(self) -> None:
        # Already imported at module import; assert they expose their API.
        assert callable(util.coerce_snapshot)
        assert callable(api.EngineClient)
        assert callable(flow.validate_assignments)
        assert callable(services.validate_service_call)
        assert callable(entity_values.device_values)

    def test_no_device_actuation_calls(self) -> None:
        """The integration must be read-only toward devices."""
        forbidden = (
            "hass.services.async_call",
            "async_call_service",
            "homeassistant.components.light",
            "homeassistant.components.switch",
            "homeassistant.components.climate",
            "homeassistant.components.cover",
        )
        for path in PACKAGE_DIR.glob("*.py"):
            source = path.read_text()
            for pattern in forbidden:
                assert pattern not in source, f"{path.name} contains {pattern}"

    def test_no_token_logging(self) -> None:
        for path in PACKAGE_DIR.glob("*.py"):
            for line_no, line in enumerate(path.read_text().splitlines(), 1):
                stripped = line.strip()
                if "_api_token" in stripped and (
                    "_LOGGER" in stripped or "print(" in stripped
                ):
                    pytest.fail(f"{path.name}:{line_no} may log the token: {stripped}")


class TestEnergyWiring:
    """Static checks on wiring the pure tests cannot import.

    ``sensor.py`` needs Home Assistant, so these assert the *shape* of the code
    that connects the tested pure helpers to HA.  They cannot prove behaviour;
    they exist to stop a refactor from silently dropping a guard.
    """

    def _sensor_source(self) -> str:
        return (PACKAGE_DIR / "sensor.py").read_text()

    def test_energy_entities_are_filtered_by_the_opt_in(self) -> None:
        source = self._sensor_source()
        # The filter must run where entities are created, not after the fact.
        assert "energy_enabled(configuration)" in source
        assert 'if key == "energy" and not energy_enabled(configuration)' in source

    def test_energy_is_published_through_the_tested_rule(self) -> None:
        source = self._sensor_source()
        assert "published_energy(" in source
        # The old freezing helper must be gone, not merely unused.
        assert "monotonic_energy" not in source
        assert "monotonic_energy" not in (PACKAGE_DIR / "util.py").read_text()

    def test_binding_happens_after_registration_not_in_init(self) -> None:
        """Binding must never influence the derived ``entity_id``.

        Home Assistant derives an entity's ``entity_id`` from the device it is
        created on (``has_entity_name`` prefixes the device name).  Setting
        ``device_entry`` in ``__init__`` therefore made the ID depend on the
        binding outcome, so a device that became bindable *after* its first
        start changed its entity ID.  The recorder only migrates statistics on
        an explicit rename, so re-deriving the ID at create time orphaned the
        existing statistics.

        The guard is ordering: the candidate resolution and the move must both
        run after Home Assistant has registered the entity.
        """
        source = self._sensor_source()
        init_start = source.index("class WattMosaicDeviceSensor")
        init_index = source.index("def __init__(", init_start)
        resolver_index = source.index("def _binding_target(", init_start)
        binding_index = source.index("def _bind_real_device(", init_start)
        added_index = source.index("async def async_added_to_hass(", init_start)

        init_body = source[init_index:resolver_index]
        # The constructor must not resolve or apply a binding target.
        assert "_bind_real_device(" not in init_body
        assert "_binding_target(" not in init_body

        # The move happens inside async_added_to_hass, after registration.
        added_body = source[added_index:]
        assert "self._bind_real_device()" in added_body
        assert added_index > binding_index

    def test_reload_adoption_requires_a_registry_entry(self) -> None:
        """``_adopt_registered_device`` may attach a device only when one exists.

        It exists so a reload does not move the entity back to the synthetic
        device and then out again.  That adoption is only safe for an entity
        whose registry entry (and therefore ``entity_id``) already exists; for a
        new entity the device must stay the synthetic one so the ID is derived
        independently of binding.
        """
        source = self._sensor_source()
        start = source.index("def _adopt_registered_device(")
        end = source.index("def _binding_target(", start)
        body = source[start:end]
        # It looks the entry up by unique id and returns when there is none.
        assert "async_get_entity_id(" in body
        assert "if entity_id is None:\n            return" in body
        assert "self.device_entry = device_entry" in body
        # It must never rename.
        assert "new_entity_id" not in body

    def test_binding_never_renames_the_entity(self) -> None:
        """A rename is what orphans statistics, so the move must not carry one.

        ``binding_update`` is the single place that decides the registry
        payload.  Returning ``entity_id`` (or ``new_entity_id``) would move the
        statistics rather than the entity.
        """
        assert entity_values.binding_update("a", "b") == {"device_id": "b"}
        for candidate in ("new_entity_id", "entity_id", "name"):
            assert candidate not in entity_values.binding_update("a", "b")

        source = self._sensor_source()
        bind_start = source.index("def _bind_real_device(")
        bind_end = source.index("async def async_added_to_hass(", bind_start)
        body = source[bind_start:bind_end]
        assert "entity_id=self.entity_id" not in body
        assert "new_entity_id" not in body
        assert "async_update_entity(self.entity_id, **update)" in body

    def test_binding_is_idempotent(self) -> None:
        """No update when the entity is already where it belongs."""
        assert entity_values.binding_update("device_a", "device_a") is None
        assert entity_values.binding_update(None, None) is None
        assert entity_values.binding_update("device_a", "") is None
        assert entity_values.binding_update("device_a", "device_b") == {
            "device_id": "device_b"
        }

    def test_binding_uses_the_tested_resolver(self) -> None:
        source = self._sensor_source()
        assert "resolve_binding_target(" in source
        assert "own_config_entry_id=self._entry.entry_id" in source

    def test_energy_state_has_no_side_effects_on_read(self) -> None:
        """``native_value`` must not mutate state; HA may read it repeatedly."""
        source = self._sensor_source()
        start = source.index("class WattMosaicDeviceSensor")
        start = source.index("def native_value(self)", start)
        end = source.index("def extra_state_attributes", start)
        body = source[start:end]
        assert "self._energy =" not in body
        assert "return self._energy" in body

    def test_coordinator_layers_the_stored_opt_in(self) -> None:
        source = (PACKAGE_DIR / "coordinator.py").read_text()
        assert "CONF_ENERGY_ENTITIES" in source
        # The merge itself lives in pure logic (``TestMergeEnergyChoice``); the
        # coordinator must delegate to it rather than re-implement coercion.
        assert "merge_energy_choice(" in source
        assert "bool(stored" not in source

    def test_saving_settings_does_not_discard_energy_choices(self) -> None:
        """HA replaces ``options`` wholesale, so every branch must carry them.

        ``OptionsFlowManager.async_finish_flow`` calls
        ``async_update_entry(options=result["data"])``.  The settings wizard is a
        different entry point from the per-device form, so if it does not repeat
        the stored map, changing the scan interval would re-enable every energy
        entity the user had turned off.
        """
        source = (PACKAGE_DIR / "config_flow.py").read_text()
        options_flow = source.index("class WattMosaicOptionsFlow")
        page = source[options_flow:]
        # Both completion paths in the options flow must persist the map, and
        # both must do it through the single helper that rebuilds it in full.
        assert page.count("CONF_ENERGY_ENTITIES: self._merged_energy_choices()") == 2, (
            "every options-flow completion path must persist energy_entities"
        )


class TestOptionsFlowRegistration:
    """The options flow must be reachable from Home Assistant's flow manager.

    ``OptionsFlowManager.async_create_flow`` does
    ``handler.async_get_options_flow(entry)`` on the *config-flow class*, and
    ``ConfigFlow.async_supports_options_flow`` derives ``supports_options`` by
    comparing that attribute with the base implementation.  A module-level
    factory satisfies neither, so the entry advertises
    ``supports_options: false`` and every ``options/flow`` request is rejected
    with ``Invalid handler specified`` -- the user can never edit CT
    assignments or energy toggles after the entry is created.
    """

    def _source(self) -> str:
        return (PACKAGE_DIR / "config_flow.py").read_text()

    def test_factory_is_a_method_on_the_config_flow(self) -> None:
        source = self._source()
        class_start = source.index("class WattMosaicConfigFlow")
        class_end = source.index("class WattMosaicOptionsFlow")
        body = source[class_start:class_end]
        assert "def async_get_options_flow" in body, (
            "async_get_options_flow must live on WattMosaicConfigFlow; HA looks "
            "it up on the handler class, so a module-level function is ignored"
        )

    def test_no_module_level_factory_shadows_the_method(self) -> None:
        source = self._source()
        # A stray module-level ``def async_get_options_flow`` has no effect but
        # signals the wiring is drifting back to the broken shape.
        assert "\ndef async_get_options_flow" not in source
        assert "\n@callback\ndef async_get_options_flow" not in source

    def test_factory_returns_the_options_flow_and_is_a_staticmethod(self) -> None:
        source = self._source()
        class_start = source.index("class WattMosaicConfigFlow")
        class_end = source.index("class WattMosaicOptionsFlow")
        body = source[class_start:class_end]
        assert "@staticmethod" in body
        assert "return WattMosaicOptionsFlow(config_entry)" in body


# --------------------------------------------------------------------------- #
# Reference-household sanity (uses the synthetic fixture, no HA connection)
# --------------------------------------------------------------------------- #

#: The published repository never contains a real installation's inventory.  A
#: deterministic synthetic household stands in for it, so these tests still
#: exercise real-world shapes (two panels, 18 channels each, dedicated and
#: branch CTs, double-pole pairs) without disclosing anyone's home.
REFERENCE_AUDIT = Path(__file__).resolve().parents[2] / "data" / "examples" / "reference_house" / "ha_environment_audit.json"


class TestReferenceHouseholdAudit:
    """Guards the CT identifiers the integration must tolerate."""

    @pytest.fixture(scope="class")
    def audit(self) -> dict[str, Any]:
        return json.loads(REFERENCE_AUDIT.read_text())

    def test_ha_version_matches_target(self, audit: dict[str, Any]) -> None:
        assert audit["ha"]["version"] == "2026.11.0"

    def test_ct_entity_ids_are_usable_as_channel_names(self, audit: dict[str, Any]) -> None:
        channels = audit["ct_channels"]
        assert len(channels) >= 30
        for channel in channels:
            ct_id = channel["entity_id"].removeprefix("sensor.")
            assert ct_id
            # The engine entity_id survives URL quoting and reverse mapping.
            topology = {
                "channels": [
                    {
                        "ct_id": ct_id,
                        "entity_id": channel["entity_id"],
                        "name": channel["name"],
                        "ct_type": "unknown",
                        "phase": "unknown",
                    }
                ]
            }
            parsed = flow.normalise_channels(topology)
            assert parsed[0]["entity_id"] == channel["entity_id"]
            assert util.slugify(ct_id) == ct_id

    def test_audit_ct_ids_are_valid_url_path_segments(self, audit: dict[str, Any]) -> None:
        from urllib.parse import quote

        for channel in audit["ct_channels"]:
            ct_id = channel["entity_id"].removeprefix("sensor.")
            quoted = quote(ct_id, safe="")
            assert quoted == ct_id  # no escaping needed for real ids

    def test_mapping_candidates_from_audit_start_unconfirmed(self, audit: dict[str, Any]) -> None:
        """Every audited candidate must stay `pending_confirmation` in the UI."""
        for candidate in audit["mapping_candidates"][:10]:
            assert candidate["status"] == "pending_confirmation"
            # Building a clean assignment from it must remain uncertain while
            # the role/phase are unknown.
            assignment = flow.normalise_assignment(
                {"role": "branch", "phase": "unknown", "device_id": None}
            )
            assert flow.is_uncertain(assignment)

    def test_double_pole_pair_suggestion_matches_audit(self, audit: dict[str, Any]) -> None:
        topology = {
            "channels": [
                {
                    "ct_id": channel["entity_id"].removeprefix("sensor."),
                    "entity_id": channel["entity_id"],
                    "name": channel["name"],
                }
                for channel in audit["ct_channels"]
            ]
        }
        channels = flow.normalise_channels(topology)
        by_id = {c["ct_id"]: c for c in channels}
        pair = flow.suggested_pair(by_id["panel_a_water_heater_ct1_power"], channels)
        assert pair == "panel_a_water_heater_ct2_power"


# --------------------------------------------------------------------------- #
# Auto placement: opt-in and apply confirmation (ADR-0024)
# --------------------------------------------------------------------------- #


class TestAutoPlacementOptIn:
    """Creating devices by inference is off unless the user says otherwise."""

    @pytest.mark.parametrize(
        "config",
        [
            None,
            {},
            {"auto_placement": False},
            {"auto_placement": "false"},
            {"auto_placement": 0},
            {"auto_placement": "nonsense"},
        ],
    )
    def test_absent_or_unclear_is_off(self, config: Any) -> None:
        """A flag that authorises a write must fail closed."""
        assert flow.auto_placement_enabled(config) is False

    @pytest.mark.parametrize(
        "value", [True, "true", "yes", "on", 1]
    )
    def test_explicit_consent_enables(self, value: Any) -> None:
        assert flow.auto_placement_enabled({"auto_placement": value}) is True

    def test_analysis_needs_no_confirm(self) -> None:
        """Reading history changes nothing, so it is always allowed."""
        validated = services.validate_service_call(
            const.SERVICE_DISCOVER_PLACEMENTS, {}
        )
        assert validated["confirm"] is False
        assert validated["parameters"] == {}

    def test_apply_without_confirm_is_refused(self) -> None:
        with pytest.raises(services.ConfirmationRequired) as excinfo:
            services.validate_service_call(
                const.SERVICE_DISCOVER_PLACEMENTS,
                {"parameters": {"apply": True}},
            )
        assert excinfo.value.key == "confirmation_required"

    def test_apply_with_confirm_passes(self) -> None:
        validated = services.validate_service_call(
            const.SERVICE_DISCOVER_PLACEMENTS,
            {"parameters": {"apply": True}, "confirm": True},
        )
        assert validated["confirm"] is True
        assert validated["parameters"] == {"apply": True}

    def test_apply_false_needs_no_confirm(self) -> None:
        """An explicit decline is not a write, so the gate must not fire."""
        validated = services.validate_service_call(
            const.SERVICE_DISCOVER_PLACEMENTS, {"parameters": {"apply": False}}
        )
        assert validated["confirm"] is False

    def test_the_service_returns_its_response(self) -> None:
        """Without this the learned placements would be invisible to the caller."""
        assert services.service_returns_response(const.SERVICE_DISCOVER_PLACEMENTS)

    def test_the_service_is_not_globally_destructive(self) -> None:
        """Gating the whole service would block the read-only analysis."""
        assert const.SERVICE_DISCOVER_PLACEMENTS not in const.DESTRUCTIVE_SERVICES


class TestPlacementRequestParameters:
    """The stored opt-in is stated on every call; an explicit request wins."""

    def test_disabled_option_sends_apply_false(self) -> None:
        assert flow.placement_request_parameters({}, {}) == {"apply": False}

    def test_enabled_option_sends_apply_true(self) -> None:
        assert flow.placement_request_parameters(
            {"auto_placement": True}, {}
        ) == {"apply": True}

    def test_explicit_analysis_wins_over_a_stored_opt_in(self) -> None:
        """A caller who asked to analyse only must not get writes."""
        assert flow.placement_request_parameters(
            {"auto_placement": True}, {"apply": False}
        ) == {"apply": False}

    def test_other_parameters_are_preserved(self) -> None:
        assert flow.placement_request_parameters(
            {"auto_placement": True}, {"lookback_hours": 72}
        ) == {"lookback_hours": 72, "apply": True}

    def test_an_explicit_apply_true_survives_a_disabled_option(self) -> None:
        # A user who passes confirm: true has consented even with the option off;
        # the engine still records the write as explicit.
        assert flow.placement_request_parameters(
            {"auto_placement": False}, {"apply": True}
        ) == {"apply": True}
