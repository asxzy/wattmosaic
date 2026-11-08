"""Tests for the read-only audit tooling (orchestrator-owned).

These run entirely offline against synthetic raw files and verify the audit
pipeline's arithmetic, especially the repeated-value and batching logic that the
engine's staleness handling depends on.
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


def _reference_topology():
    """Build the candidate topology from the synthetic reference household.

    The builder reads ``<DATA>/ha_environment_audit.json``; pointing it at the
    fixture keeps these tests offline and free of any real installation data.
    """
    import build_topology

    if not (REFERENCE / "ha_environment_audit.json").exists():
        pytest.skip("reference fixture not generated")
    original = build_topology.DATA
    build_topology.DATA = REFERENCE
    try:
        return build_topology.build()
    finally:
        build_topology.DATA = original


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
    # Wave 1: three entities within 50 ms (one poll cycle).
    for i, eid in enumerate(("a", "b", "c")):
        ts = (base + timedelta(milliseconds=10 * i)).isoformat()
        rows.append({"type": "state", "entity_id": f"sensor.{eid}", "state": "1",
                     "last_updated": ts, "received_at": ts})
    # Wave 2: two entities later.
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


def test_topology_candidate_builder_is_candidate_only() -> None:
    """Every auto-derived element must be 'candidate', never 'confirmed'."""
    topology = _reference_topology()
    assert topology.status == "candidate"
    assert topology.channels
    assert all(c.topology_status == "candidate" for c in topology.channels)
    assert all(d.topology_status == "candidate" for d in topology.devices)
    # Mains must never be summed implicitly: three separate groups exist.
    mains = [c for c in topology.channels if c.ct_type == "main"]
    assert len(mains) >= 4
    # No channel may claim a parent that does not exist.
    ids = {c.ct_id for c in topology.channels}
    for c in topology.channels:
        if c.parent_ct_id:
            assert c.parent_ct_id in ids
        if c.paired_ct_id:
            assert c.paired_ct_id in ids


def test_topology_pairing_creates_single_double_pole_device() -> None:
    topology = _reference_topology()
    doubles = [d for d in topology.devices if d.double_pole]
    assert doubles, "expected the *_ct1/*_ct2 pairs to be recognised"
    for dev in doubles:
        assert len(dev.branch_ct_ids) == 2
        assert dev.phase == "both"
        assert not dev.dedicated_ct_ids


def test_every_device_owns_a_ct_its_role_allows() -> None:
    """A device must not file a `branch` CT under `dedicated_ct_ids`.

    ``dedicated_channel_for`` only accepts ``CTType.DEDICATED_DEVICE``, so a
    mis-filed branch CT leaves the device with **zero** attribution rows: it
    becomes invisible to inference while a real CT is still measuring it.  Two
    lighting circuits were silently in that state.
    """
    topology = _reference_topology()
    channels = topology.channel_map()
    for dev in topology.devices:
        for cid in dev.dedicated_ct_ids:
            assert channels[cid].ct_type == "dedicated_device", (
                f"{dev.device_id} files {cid} ({channels[cid].ct_type}) as dedicated"
            )
        for cid in dev.branch_ct_ids:
            assert channels[cid].ct_type != "main", f"{dev.device_id} files a main CT as branch"


def test_branch_owned_single_channel_device_gets_branch_rows() -> None:
    """A single-channel device on a `branch` CT must own it via `branch_ct_ids`.

    This is the precise shape of defect #28: `circuit_ct1`/`circuit_ct2` were
    measured by real branch-typed CTs but filed under `dedicated_ct_ids`, which
    the resolver rejects, so both devices had zero attribution rows and reported
    `home_total_only`.  The reference household contains exactly that shape, so
    this test would have caught it before it reached a live database.
    """
    from wattmosaic_engine.electrical.topology import build_graph

    topology = _reference_topology()
    channels = topology.channel_map()
    branch_owned = [
        dev
        for dev in topology.devices
        if len(dev.dedicated_ct_ids) + len(dev.branch_ct_ids) == 1
        and channels[(dev.dedicated_ct_ids + dev.branch_ct_ids)[0]].ct_type == "branch"
    ]
    assert branch_owned, "reference fixture no longer exercises a branch-typed single CT"
    for dev in branch_owned:
        assert dev.branch_ct_ids, f"{dev.device_id} lost its branch ownership"
        assert not dev.dedicated_ct_ids, f"{dev.device_id} filed a branch CT as dedicated"
        graph = build_graph(topology)
        assert graph.attribution_ct_ids(dev.device_id) == tuple(dev.branch_ct_ids)


def test_every_device_has_an_attribution_row() -> None:
    """No device may be measured by nothing without being placed in the total."""
    from wattmosaic_engine.contracts import Topology
    from wattmosaic_engine.disaggregation.configuration import effective_configs
    from wattmosaic_engine.electrical.topology import build_graph

    payload = json.loads((REFERENCE / "topology_candidates.json").read_text())
    topology = Topology.model_validate(payload)
    graph = build_graph(topology)
    invisible = [d.device_id for d in topology.devices if not graph.attribution_ct_ids(d.device_id)]
    assert invisible == [], f"devices with no attribution CT: {invisible}"
    # Every own-CT device must resolve to a real monitoring level, not UNKNOWN.
    for device_id, effective in effective_configs(topology).items():
        assert effective.monitoring.value != "unknown", device_id


def test_example_topology_validates() -> None:
    from wattmosaic_engine.contracts import Topology

    payload = json.loads((ROOT / "data" / "examples" / "topology.example.json").read_text())
    topology = Topology.model_validate(payload)
    assert any(d.double_pole for d in topology.devices)
    assert any(c.ct_type == "main" for c in topology.channels)


def test_audit_identifies_monitors_structurally_not_by_brand() -> None:
    """A CT monitor is several power entities on one device, whatever it is called.

    The audit tool used to match a hard-coded list of vendor names.  That both
    missed unrecognised hardware and published the vendor names, and it
    over-matched badly on a real installation (one entry was the ordinary English
    word ``sense``, which matched 33 devices instead of the 4 real monitors).
    """
    sys.path.insert(0, str(ROOT / "tools" / "audit"))
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
    sys.path.insert(0, str(ROOT / "tools" / "audit"))
    import audit_environment as audit

    power = [
        {"device_id": "plug", "entity_id": "sensor.plug_power"},
        {"device_id": "plug", "entity_id": "sensor.plug_power_2"},
    ]
    assert audit.identify_monitors(power) == set()
