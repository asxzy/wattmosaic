"""Tests for the read-only audit tooling.

These run entirely offline against synthetic raw files and the published
synthetic reference household (``data/examples/reference_house``); they verify
the audit pipeline's arithmetic and the candidate-only nature of its topology
builder. They never touch a real installation, the network or Home Assistant.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "audit"))

#: Published fixture standing in for a real installation (see
#: tools/dev/make_reference_house.py).  The repository never ships a real
#: household inventory.
REFERENCE = ROOT / "data" / "examples" / "reference_house"


def test_normalize_base_url_adds_default_port() -> None:
    from ha_client import normalize_base_url

    # RFC 5737 documentation addresses: deliberately not a real household.
    assert normalize_base_url("192.0.2.50") == "http://192.0.2.50:8123"
    assert normalize_base_url("192.0.2.50:8124") == "http://192.0.2.50:8124"
    assert normalize_base_url("https://ha.example.com") == "https://ha.example.com"


def test_connection_settings_requires_both_values() -> None:
    from ha_client import connection_settings

    with pytest.raises(RuntimeError):
        connection_settings({"HA_HOST": "127.0.0.1"})
    with pytest.raises(RuntimeError):
        connection_settings({"HA_TOKEN": "x"})
    url, token = connection_settings({"HA_HOST": "127.0.0.1", "HA_TOKEN": "t"})
    assert url.endswith(":8123")
    assert token == "t"


def test_load_env_does_not_mutate_environment(tmp_path: Path, monkeypatch) -> None:
    from ha_client import load_env

    env_file = tmp_path / ".env"
    env_file.write_text("WM_TEST_VALUE=abc\n# comment\n\nOTHER=1\n")
    monkeypatch.delenv("WM_TEST_VALUE", raising=False)
    parsed = load_env(env_file)
    assert parsed["WM_TEST_VALUE"] == "abc"
    assert parsed["OTHER"] == "1"
    import os

    assert "WM_TEST_VALUE" not in os.environ


def _write_samples(path: Path, rows: list[dict]) -> None:
    with path.open("w") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")


def test_profile_detects_repeated_values(tmp_path: Path) -> None:
    from profile_samples import profile

    base = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    rows = []
    values = [100.0, 100.0, 100.0, 120.0, 120.0]
    for i, v in enumerate(values):
        ts = (base + timedelta(seconds=15 * i)).isoformat()
        rows.append(
            {
                "type": "state",
                "entity_id": "sensor.ct1_power",
                "state": str(v),
                "last_updated": ts,
                "received_at": ts,
            }
        )
    path = tmp_path / "raw.jsonl"
    _write_samples(path, rows)
    result = profile(path)
    channel = result["channels"]["sensor.ct1_power"]
    assert channel["observations"] == 5
    assert channel["repeated_values"] == 3
    assert channel["repeat_ratio"] == pytest.approx(0.75)
    assert channel["last_updated"]["median_s"] == pytest.approx(15.0)
    assert channel["resolution"]["min_nonzero_delta"] == pytest.approx(20.0)


def test_profile_detects_batching(tmp_path: Path) -> None:
    from profile_samples import profile

    base = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    rows = []
    for i, eid in enumerate(("a", "b", "c")):
        ts = (base + timedelta(milliseconds=10 * i)).isoformat()
        rows.append({"type": "state", "entity_id": f"sensor.{eid}", "state": "1",
                     "last_updated": ts, "received_at": ts})
    for i, eid in enumerate(("a", "b")):
        ts = (base + timedelta(seconds=15, milliseconds=10 * i)).isoformat()
        rows.append({"type": "state", "entity_id": f"sensor.{eid}", "state": "2",
                     "last_updated": ts, "received_at": ts})
    path = tmp_path / "raw.jsonl"
    _write_samples(path, rows)
    batching = profile(path)["batching"]
    assert batching["bursts"] == 2
    assert batching["multi_entity_bursts"] == 2
    assert batching["max_entities_per_burst"] == 3
    assert batching["burst_span_max_s"] < 1.0


def test_two_second_assumption_flag(tmp_path: Path) -> None:
    from profile_samples import profile

    base = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    rows = []
    for i in range(5):
        ts = (base + timedelta(seconds=15 * i)).isoformat()
        rows.append({"type": "state", "entity_id": "sensor.ct", "state": str(i),
                     "last_updated": ts, "received_at": ts})
    path = tmp_path / "raw.jsonl"
    _write_samples(path, rows)
    summary = profile(path)["summary"]
    assert summary["two_second_assumption_supported"] is False


def test_profile_missing_file(tmp_path: Path) -> None:
    from profile_samples import profile

    with pytest.raises(SystemExit):
        profile(tmp_path / "nope.jsonl")


def test_audit_identifies_monitors_structurally_not_by_brand() -> None:
    """A CT monitor is several power entities on one device, whatever it is called."""
    import audit_environment as audit

    power = [
        *({"device_id": "monitor", "entity_id": f"sensor.m_ct{i}_power"} for i in range(1, 9)),
        {"device_id": "plug", "entity_id": "sensor.washer_power"},
        {"device_id": "plug2", "entity_id": "sensor.dryer_power"},
    ]
    monitors = audit.identify_monitors(power)
    assert monitors == {"monitor"}, monitors
    assert "plug" not in monitors and "plug2" not in monitors


def test_audit_monitor_threshold_needs_more_than_a_plug() -> None:
    """Two power entities can be one smart plug with a secondary reading."""
    import audit_environment as audit

    power = [
        {"device_id": "plug", "entity_id": "sensor.plug_power"},
        {"device_id": "plug", "entity_id": "sensor.plug_power_2"},
    ]
    assert audit.identify_monitors(power) == set()


def test_derive_state_entities_port_uses_v3_on_states(tmp_path: Path) -> None:
    """The ported state-link tool uses the v3 ON vocabulary (no deleted import)."""
    import derive_state_entities as dse

    overrides = dse.state_overrides(["Heating", "Running"], ["Cooling", "off"])
    # ``Heating``/``Running`` are ON in v3; ``cooling`` is ON globally so an
    # override flips it OFF; ``off`` is already OFF so no override needed.
    assert overrides.get("Cooling") == "off"
    assert "off" not in overrides
    assert overrides.get("Heating") is None
    assert overrides.get("Running") is None
