"""Unit tests for ingestion: normalization, parsing, dedup, backoff, filters.

No real network.  WebSocket behaviour is exercised through a fake in-memory
connection, and reconnect backoff is tested as pure arithmetic.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from datetime import UTC, datetime, timedelta

import pytest
from wattmosaic_engine.contracts import (
    DeviceEvent,
    Measurement,
    QualityStatus,
    utcnow,
)
from wattmosaic_engine.ingestion import (
    BACKOFF_MAX_S,
    HASource,
    compute_backoff,
    entity_allowed,
    normalize_unit,
    parse_state_timestamp,
)
from wattmosaic_engine.ingestion.normalize import (
    canonical_unit,
    is_own_entity,
    parse_float,
)

# --------------------------------------------------------------------------- #
# Unit normalization
# --------------------------------------------------------------------------- #


def test_normalize_unit_kw_to_w() -> None:
    assert normalize_unit(1.5, "kW") == (1500.0, "W")
    assert normalize_unit(-2.0, "kW") == (-2000.0, "W")  # sign preserved


def test_normalize_unit_wh_to_kwh() -> None:
    assert normalize_unit(2500.0, "Wh") == (2.5, "kWh")
    assert normalize_unit(-10.0, "Wh") == (-0.01, "kWh")


def test_normalize_unit_passthrough() -> None:
    assert normalize_unit(42.0, "W") == (42.0, "W")
    assert normalize_unit(1.25, "kWh") == (1.25, "kWh")


def test_normalize_unit_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        normalize_unit(1.0, "V")
    with pytest.raises(ValueError):
        normalize_unit(1.0, None)


def test_canonical_unit_aliases() -> None:
    assert canonical_unit("w") == "W"
    assert canonical_unit("KW") == "kW"
    assert canonical_unit("kwh") == "kWh"
    assert canonical_unit("furlongs") is None
    assert canonical_unit(None) is None


def test_parse_float_handles_ha_strings() -> None:
    assert parse_float("12.5") == 12.5
    assert parse_float(" 1,234.5 ") == 1234.5
    assert parse_float("unavailable") is None
    assert parse_float("unknown") is None
    assert parse_float("") is None
    assert parse_float("nan") is None
    assert parse_float("inf") is None
    assert parse_float(None) is None
    assert parse_float(True) is None
    assert parse_float(7) == 7.0


def test_parse_state_timestamp() -> None:
    assert parse_state_timestamp("2026-11-05T12:00:00+00:00") == datetime(
        2026, 11, 5, 12, tzinfo=UTC
    )
    assert parse_state_timestamp("2026-11-05T12:00:00Z") == datetime(2026, 11, 5, 12, tzinfo=UTC)
    naive = parse_state_timestamp("2026-11-05T12:00:00")
    assert naive is not None and naive.tzinfo is UTC
    assert parse_state_timestamp("not-a-date") is None
    assert parse_state_timestamp(None) is None


# --------------------------------------------------------------------------- #
# Entity allow/deny
# --------------------------------------------------------------------------- #


def test_entity_allow_deny() -> None:
    assert entity_allowed("sensor.a_power")
    assert entity_allowed("sensor.a_power", allow=["sensor.*_power"])
    assert not entity_allowed("sensor.a_voltage", allow=["sensor.*_power"])
    assert not entity_allowed("sensor.a_power", deny=["*_power"])
    # Deny wins over allow.
    assert not entity_allowed("sensor.a_power", allow=["sensor.*"], deny=["*_power"])
    # Case-insensitive.
    assert entity_allowed("Sensor.A_Power", allow=["sensor.*_power"])


# --------------------------------------------------------------------------- #
# Reconnect backoff math
# --------------------------------------------------------------------------- #


def test_backoff_is_exponential_and_capped() -> None:
    rng = random.Random(1234)
    delays = [compute_backoff(i, rng=rng, jitter=0.0) for i in range(8)]
    assert delays[0] == 1.0
    assert delays[1] == 2.0
    assert delays[2] == 4.0
    assert delays[3] == 8.0
    assert delays[7] == BACKOFF_MAX_S  # capped
    assert all(0 < d <= BACKOFF_MAX_S for d in delays)


def test_backoff_jitter_is_bounded() -> None:
    rng = random.Random(42)
    for attempt in range(10):
        for _ in range(50):
            d = compute_backoff(attempt, rng=rng, jitter=0.2)
            nominal = min(BACKOFF_MAX_S, 1.0 * (2**attempt))
            assert 0 < d <= BACKOFF_MAX_S
            # Jitter never exceeds 20% above the nominal value (subject to cap).
            assert d <= min(BACKOFF_MAX_S, nominal * 1.2) + 1e-9


def test_backoff_never_exceeds_max_for_large_attempts() -> None:
    assert compute_backoff(10_000, jitter=0.5) <= BACKOFF_MAX_S


# --------------------------------------------------------------------------- #
# Fake WebSocket harness
# --------------------------------------------------------------------------- #


class FakeWebSocket:
    """In-memory websocket that plays the HA handshake then streams frames."""

    def __init__(self, frames: list[dict], *, close_after: bool = True,
                 auto_close_after_drain: bool = False,
                 entity_registry: list[dict] | None = None) -> None:
        self.sent: list[dict] = []
        self._incoming: list[str] = ["__hello__"]
        self._frames = frames
        self._entity_registry = entity_registry or []
        self._closed = asyncio.Event()
        self.closed = False
        self.auto_close_after_drain = auto_close_after_drain
        self._frames_issued = False

    async def send(self, message: str) -> None:
        payload = json.loads(message)
        self.sent.append(payload)
        if payload.get("type") == "auth":
            self._incoming.append(json.dumps({"type": "auth_ok"}))
        elif payload.get("type") == "config/entity_registry/list":
            self._incoming.append(
                json.dumps({
                    "id": payload["id"], "type": "result", "success": True,
                    "result": self._entity_registry,
                })
            )
        elif payload.get("type") == "get_states" or payload.get("type", "").startswith("config/"):
            self._incoming.append(
                json.dumps({"id": payload["id"], "type": "result", "success": True, "result": []})
            )
        elif payload.get("type") == "subscribe_events":
            self._incoming.append(
                json.dumps({"id": payload["id"], "type": "result", "success": True, "result": None})
            )
            for frame in self._frames:
                self._incoming.append(json.dumps(frame))
            self._frames_issued = True

    async def recv(self) -> str:
        while True:
            if self._incoming:
                item = self._incoming.pop(0)
                if item == "__hello__":
                    return json.dumps({"type": "auth_required", "ha_version": "2026.11.0"})
                return item
            if self.closed or (self.auto_close_after_drain and self._frames_issued):
                from websockets.exceptions import ConnectionClosedOK

                raise ConnectionClosedOK(None, None)
            await asyncio.sleep(0.001)

    async def close(self) -> None:
        self.closed = True


class FakeConnector:
    def __init__(self, ws: FakeWebSocket) -> None:
        self.ws = ws
        self.calls = 0

    def __call__(self, _url: str) -> object:
        self.calls += 1

        class _Ctx:
            async def __aenter__(inner) -> FakeWebSocket:  # noqa: N805
                return self.ws

            async def __aexit__(inner, *exc) -> None:  # noqa: N805
                await self.ws.close()

        return _Ctx()


def _state_change(entity_id: str, state: str, *, unit: str = "W", device_class: str = "power",
                  last_updated: str | None = None) -> dict:
    ts = last_updated or utcnow().isoformat()
    return {
        "type": "event",
        "event": {
            "event_type": "state_changed",
            "time_fired": ts,
            "data": {
                "entity_id": entity_id,
                "old_state": {"entity_id": entity_id, "state": "0"},
                "new_state": {
                    "entity_id": entity_id,
                    "state": state,
                    "last_updated": ts,
                    "last_changed": ts,
                    "attributes": {
                        "unit_of_measurement": unit,
                        "device_class": device_class,
                        "friendly_name": entity_id,
                    },
                },
            },
        },
    }


def _run(coro):
    return asyncio.run(coro)


async def _collect(source: HASource, seconds: float = 0.4) -> tuple[list, list]:
    samples: list[Measurement] = []
    events: list[DeviceEvent] = []

    async def on_sample(m: Measurement) -> None:
        samples.append(m)

    async def on_event(e: DeviceEvent) -> None:
        events.append(e)

    await source.start(on_sample=on_sample, on_event=on_event)
    await asyncio.sleep(seconds)
    await source.stop()
    return samples, events


# --------------------------------------------------------------------------- #
# Ingestion behavior
# --------------------------------------------------------------------------- #


def test_websocket_auth_and_read_only_commands() -> None:
    ws = FakeWebSocket([_state_change("sensor.a_power", "100")])
    source = HASource("http://ha.local:8123", "token", connect_factory=FakeConnector(ws))

    async def run():
        await source.start()
        await source.wait_connected(timeout=2.0)
        await asyncio.sleep(0.2)
        await source.stop()

    _run(run())
    types = [m["type"] for m in ws.sent]
    assert "auth" in types
    assert "get_states" in types
    assert "subscribe_events" in types
    # No service calls, ever.
    assert not any(t.startswith("call_service") for t in types)
    assert set(types) <= {
        "auth",
        "get_states",
        "config/entity_registry/list",
        "config/device_registry/list",
        "config/area_registry/list",
        "config/floor_registry/list",
        "subscribe_events",
    }


def test_measurement_normalization_and_timestamps() -> None:
    ws = FakeWebSocket([_state_change("sensor.kw_power", "1.5", unit="kW")])
    source = HASource("http://ha.local:8123", "token", connect_factory=FakeConnector(ws))
    samples, _ = _run(_collect(source))

    matched = [m for m in samples if m.entity_id == "sensor.kw_power"]
    assert matched, "expected a normalized measurement"
    m = matched[-1]
    assert m.value == 1500.0
    assert m.unit == "W"
    assert m.value_w == 1500.0
    assert m.state_timestamp.tzinfo is not None
    assert m.received_at.tzinfo is not None
    assert m.received_at >= m.state_timestamp


def test_energy_measurement_never_gets_value_w() -> None:
    ws = FakeWebSocket([_state_change("sensor.energy", "2.5", unit="kWh", device_class="energy")])
    source = HASource("http://ha.local:8123", "token", connect_factory=FakeConnector(ws))
    samples, _ = _run(_collect(source))
    matched = [m for m in samples if m.entity_id == "sensor.energy"]
    assert matched
    m = matched[-1]
    assert m.unit == "kWh"
    assert m.value == 2.5
    assert m.value_w is None, "energy must never be expressed as watts"


def test_dedup_flags_repeated_values() -> None:
    frames = [
        _state_change("sensor.p", "100"),
        _state_change("sensor.p", "100"),
        _state_change("sensor.p", "101"),
        _state_change("sensor.p", "101"),
    ]
    ws = FakeWebSocket(frames)
    source = HASource("http://ha.local:8123", "token", connect_factory=FakeConnector(ws))
    samples, _ = _run(_collect(source, 0.6))
    values = [(m.value, m.repeated) for m in samples if m.entity_id == "sensor.p"]
    # First observation is not repeated; identical follow-ups are.
    assert (100.0, False) in values
    assert (100.0, True) in values
    assert (101.0, False) in values
    assert (101.0, True) in values


def test_device_events_for_state_bearing_entities() -> None:
    frame = {
        "type": "event",
        "event": {
            "event_type": "state_changed",
            "time_fired": utcnow().isoformat(),
            "data": {
                "entity_id": "light.kitchen",
                "old_state": {"entity_id": "light.kitchen", "state": "off", "attributes": {}},
                "new_state": {
                    "entity_id": "light.kitchen",
                    "state": "on",
                    "last_updated": utcnow().isoformat(),
                    "attributes": {"brightness": 200},
                },
            },
        },
    }
    ws = FakeWebSocket([frame])
    source = HASource("http://ha.local:8123", "token", connect_factory=FakeConnector(ws))
    _, events = _run(_collect(source))
    matched = [e for e in events if e.entity_id == "light.kitchen"]
    assert matched
    assert matched[-1].previous_state == "off"
    assert matched[-1].new_state == "on"
    assert matched[-1].event_type == "state_changed"


def test_malformed_payloads_are_skipped() -> None:
    frames = [
        {"type": "event", "event": {"event_type": "state_changed", "data": None}},
        {"type": "event", "event": {"event_type": "state_changed", "data": {"entity_id": 5}}},
        _state_change("sensor.good_power", "12"),
    ]
    ws = FakeWebSocket(frames)
    source = HASource("http://ha.local:8123", "token", connect_factory=FakeConnector(ws))
    samples, _ = _run(_collect(source))
    assert any(m.entity_id == "sensor.good_power" for m in samples)
    assert source.stats["malformed_skipped"] >= 1
    assert source.connected is False  # stopped cleanly


def test_allow_deny_filters_applied() -> None:
    frames = [
        _state_change("sensor.keep_power", "10"),
        _state_change("sensor.drop_power", "20"),
    ]
    ws = FakeWebSocket(frames)
    source = HASource(
        "http://ha.local:8123",
        "token",
        deny_entities=["sensor.drop_*"],
        connect_factory=FakeConnector(ws),
    )
    samples, _ = _run(_collect(source))
    ids = {m.entity_id for m in samples}
    assert "sensor.keep_power" in ids
    assert "sensor.drop_power" not in ids


def test_own_entities_are_never_ingested() -> None:
    """The engine must not treat its own estimates as physical measurements.

    The integration's device sensors are named after the device
    (``sensor.<device>_estimated_power``), so they carry no marker in the entity
    id.  Only the registry ``platform`` identifies them.  Ingesting them feeds
    the model's own answer back in as evidence and inflates the sample table.
    """
    frames = [
        _state_change("sensor.some_device_estimated_power", "123"),
        _state_change("sensor.panel_a_ct1_power", "456"),
    ]
    ws = FakeWebSocket(
        frames,
        entity_registry=[
            {"entity_id": "sensor.some_device_estimated_power", "platform": "wattmosaic"},
            {"entity_id": "sensor.panel_a_ct1_power", "platform": "some_monitor"},
        ],
    )
    source = HASource("http://ha.local:8123", "token", connect_factory=FakeConnector(ws))

    samples, _ = _run(_collect(source))
    ids = {m.entity_id for m in samples}
    assert "sensor.panel_a_ct1_power" in ids, "a real CT must still be ingested"
    assert "sensor.some_device_estimated_power" not in ids, (
        "the engine's own estimate must never be ingested as a measurement"
    )
    assert source.stats["own_entities_skipped"] >= 1


def test_own_entity_global_sensor_skipped_without_registry() -> None:
    """Before the registry loads, the engine's global sensors are still excluded."""
    frames = [_state_change("sensor.wattmosaic_engine_model_estimated_power", "9")]
    ws = FakeWebSocket(frames)
    source = HASource("http://ha.local:8123", "token", connect_factory=FakeConnector(ws))
    samples, _ = _run(_collect(source))
    assert not [m for m in samples if m.entity_id.startswith("sensor.wattmosaic_")]
    assert source.stats["own_entities_skipped"] >= 1


def test_is_own_entity_discriminates_on_platform() -> None:
    # platform is authoritative, whatever the id looks like
    assert is_own_entity("sensor.anything_estimated_power", "wattmosaic") is True
    assert is_own_entity("sensor.panel_a_ct1_power", "some_monitor") is False
    # id-prefix fallback covers the engine's own global sensors only
    assert is_own_entity("sensor.wattmosaic_engine_total_incoming_power", None) is True
    assert is_own_entity("sensor.panel_a_main_ct1_power", None) is False


def test_unavailable_state_marks_entity() -> None:
    frame = _state_change("sensor.power", "unavailable")
    ws = FakeWebSocket([frame])
    source = HASource("http://ha.local:8123", "token", connect_factory=FakeConnector(ws))
    _, events = _run(_collect(source))
    assert "sensor.power" in source.unavailable_entities
    assert any(e.event_type == "availability" for e in events if e.entity_id == "sensor.power")


def test_stale_samples_are_flagged() -> None:
    old = (utcnow() - timedelta(seconds=600)).isoformat()
    ws = FakeWebSocket([_state_change("sensor.p", "10", last_updated=old)])
    source = HASource(
        "http://ha.local:8123", "token", stale_after_s=120, connect_factory=FakeConnector(ws)
    )
    samples, _ = _run(_collect(source))
    matched = [m for m in samples if m.entity_id == "sensor.p"]
    assert matched and matched[-1].quality == QualityStatus.STALE


def test_read_only_command_guard() -> None:
    ws = FakeWebSocket([])
    source = HASource("http://ha.local:8123", "token", connect_factory=FakeConnector(ws))

    async def run():
        await source.start()
        await source.wait_connected(timeout=2.0)
        try:
            await source._request("call_service", domain="light", service="turn_on")
        finally:
            await source.stop()
        return True

    with pytest.raises(PermissionError):
        _run(run())


def test_callback_exception_does_not_kill_ingestion() -> None:
    frames = [_state_change("sensor.p", "1"), _state_change("sensor.p", "2")]
    ws = FakeWebSocket(frames)
    source = HASource("http://ha.local:8123", "token", connect_factory=FakeConnector(ws))
    seen: list[float] = []

    async def bad_callback(m: Measurement) -> None:
        seen.append(m.value)
        raise RuntimeError("consumer bug")

    async def run():
        await source.start(on_sample=bad_callback)
        await asyncio.sleep(0.5)
        stats = dict(source.stats)
        await source.stop()
        return stats

    stats = _run(run())
    assert len(seen) >= 1
    assert stats["callbacks_failed"] >= 1


# --------------------------------------------------------------------------- #
# Shutdown must be silent
# --------------------------------------------------------------------------- #


def test_stop_during_open_connection_logs_no_error(caplog) -> None:
    """A normal shutdown must not emit ERROR records or unretrieved-task noise."""
    frames = [_state_change("sensor.p", "1")]
    ws = FakeWebSocket(frames)
    source = HASource("http://ha.local:8123", "token", connect_factory=FakeConnector(ws))

    with caplog.at_level(logging.DEBUG):
        async def run():
            await source.start()
            await source.wait_connected(timeout=2.0)
            await asyncio.sleep(0.1)
            await source.stop()
            # Let aiohttp/asyncio settle any done-callbacks.
            await asyncio.sleep(0.05)

        _run(run())

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors == [], f"unexpected ERROR on shutdown: {[r.getMessage() for r in errors]}"


def test_stop_is_idempotent() -> None:
    ws = FakeWebSocket([])

    async def run():
        source = HASource("http://ha.local:8123", "token", connect_factory=FakeConnector(ws))
        await source.start()
        await source.wait_connected(timeout=2.0)
        await source.stop()
        await source.stop()
        await source.stop()

    _run(run())


def test_connection_closed_by_peer_reconnects_without_error_log(caplog) -> None:
    ws = FakeWebSocket([], auto_close_after_drain=True)
    connector = FakeConnector(ws)
    source = HASource(
        "http://ha.local:8123",
        "token",
        connect_factory=connector,
        backoff_base_s=0.01,
        backoff_max_s=0.02,
        backoff_jitter=0.0,
    )

    with caplog.at_level(logging.DEBUG):
        async def run():
            await source.start()
            await asyncio.sleep(0.2)
            await source.stop()

        _run(run())

    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors == []
    assert connector.calls >= 2  # it attempted to reconnect
