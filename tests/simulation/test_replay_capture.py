"""The replay tool itself, exercised on the X07 simulator's HB §17 house.

``tools/audit/replay_capture.py`` is the bridge between the simulator and a real
installation, and its whole value is that it goes through the **production v3**
path (``observation frame`` -> ``replay_week`` -> ``Reconciler`` ->
``infer_group``). If it were only ever run against an operator's database, it
would be untested in CI and could rot silently.

So this file replays the deterministic HB §17 household through the same code.
That gives the tool a ground truth and a versioned, immutable derived run to
assert on. Nothing here needs the real database, the network or Home Assistant.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime

import pytest

from tools.simulator import build_hb17


def _replay_module():
    """Import ``tools.audit.replay_capture`` (the package has no ``__init__``)."""
    from tests.conftest import REPO_ROOT

    audit = str(REPO_ROOT / "tools" / "audit")
    if audit not in sys.path:
        sys.path.insert(0, audit)
    import replay_capture

    return replay_capture


def _capture_from_house(house):
    """Lift a simulated HB §17 house into a ``Capture`` exactly as a collector would."""
    module = _replay_module()
    samples = []
    for record in house.records:
        for reader_id, sample in (record.get("reader_samples") or {}).items():
            if sample.get("quality") in ("missing", "stale"):
                continue
            samples.append(
                module.CaptureSample(
                    ct_id=reader_id,
                    entity_id=sample["entity_id"],
                    value_w=float(sample["value_w"]),
                    state_timestamp=datetime.fromisoformat(sample["state_timestamp"]),
                    received_at=datetime.fromisoformat(sample["received_at"]),
                    quality=str(sample.get("quality", "good")),
                    repeated=False,
                    sequence=int(sample.get("sequence", 0) or 0),
                )
            )
    samples.sort(key=lambda s: s.received_at)
    return module.Capture(
        samples=samples,
        source="hb17-simulator",
        window_start=samples[0].received_at,
        window_end=samples[-1].received_at,
        ha_messages=list(house.ha_messages),
    )


@pytest.fixture(scope="module")
def house():
    return build_hb17(seed=7, minutes=25, interval_s=10.0)


@pytest.fixture(scope="module")
def replayed(house):
    module = _replay_module()
    capture = _capture_from_house(house)
    return module.replay(capture, variant="nested", tick_s=10.0, models="v1")


def test_replay_returns_a_derived_run(replayed) -> None:
    """The v3 replay surface produces a versioned, retrievable DerivedRun."""
    from wattmosaic_engine.learning.reconciliation import DerivedRun
    from wattmosaic_engine.replay import get_derived_run

    assert isinstance(replayed, DerivedRun)
    assert replayed.derived_run_id.startswith("derived-")
    assert replayed.versions.model_set_id.startswith("mset-")
    assert replayed.chunks >= 1
    assert get_derived_run(replayed.derived_run_id).derived_estimates == replayed.derived_estimates


def test_replay_accepts_a_model_set_object(house) -> None:
    """``models=ModelSet`` (a ModelSetView) is accepted, not just a builtin id."""
    from wattmosaic_engine.runtime.fakes import FakeModelSet

    module = _replay_module()
    capture = _capture_from_house(house)
    models = FakeModelSet.fixed({}, model_set_id="mset-hb17-test")
    run = module.replay(capture, models=models, tick_s=10.0)
    assert run.versions.model_set_id == "mset-hb17-test"


def test_replay_estimates_are_honest_about_unknowns(replayed) -> None:
    """An absent estimate is ``None``; the run carries no fabricated truth."""
    assert replayed.derived_estimates
    for estimate in replayed.derived_estimates:
        # Null policy: point_w is a float or None, never a placeholder 0.0 for
        # a non-identifiable subject.
        assert estimate.point_w is None or isinstance(estimate.point_w, float)
        assert estimate.status in {
            "unknown",
            "measured",
            "estimated",
            "model_estimated",
            "ambiguous",
        }


def test_replay_is_deterministic(house) -> None:
    """Same inputs and model set give the same derived run id and fingerprint."""
    module = _replay_module()
    capture = _capture_from_house(house)
    first = module.replay(capture, tick_s=10.0, models="v1")
    second = module.replay(capture, tick_s=10.0, models="v1")
    assert first.derived_run_id == second.derived_run_id
    assert first.fingerprint == second.fingerprint


def test_evaluation_shape_is_machine_readable(replayed) -> None:
    """The report shape matches the simulator's and stays empty of truth strata."""
    module = _replay_module()
    payload = module.evaluation_shape(replayed, topology_version=1)
    assert payload["per_device"] == {}
    assert payload["per_source"] == {}
    assert payload["invariants"]["per_device_error_available"] is False
    assert any("no independent ground truth" in note for note in payload["notes"])
    assert payload["replay"]["derived_run_id"] == replayed.derived_run_id
    assert json.loads(json.dumps(payload))["replay"]["chunks"] == replayed.chunks


def test_deadline_aborts_a_runaway_replay() -> None:
    """The hard deadline must fire, and must not be catchable by a bare loop."""
    module = _replay_module()
    with pytest.raises(module.ReplayTimeout), module._Deadline(0.2, hard_exit=False):
        total = 0
        for i in range(50_000_000):
            total += i


def test_deadline_is_inert_when_disabled() -> None:
    """``seconds <= 0`` means "no deadline", not "abort immediately"."""
    module = _replay_module()
    with module._Deadline(0.0, hard_exit=False):
        pass


def test_aligner_key_strips_the_domain_prefix() -> None:
    """A capture may store either form; both must map to the same channel."""
    module = _replay_module()
    assert module.aligner_key("sensor.panel_a_ct1_power") == "panel_a_ct1_power"
    assert module.aligner_key("panel_a_ct1_power") == "panel_a_ct1_power"


def test_jsonl_loader_reads_the_collector_format(tmp_path) -> None:
    """``ct_events.jsonl`` round-trips into a capture, unmapped rows are noted."""
    module = _replay_module()
    path = tmp_path / "ct_events.jsonl"
    rows = [
        {"type": "state", "entity_id": "sensor.a", "state": "100.0",
         "last_updated": "2026-01-01T00:00:15+00:00",
         "received_at": "2026-01-01T00:00:16+00:00"},
        {"type": "state", "entity_id": "sensor.a", "state": "140.0",
         "last_updated": "2026-01-01T00:00:30+00:00",
         "received_at": "2026-01-01T00:00:31+00:00"},
        {"type": "state", "entity_id": "sensor.unknown", "state": "1.0",
         "last_updated": "2026-01-01T00:00:30+00:00",
         "received_at": "2026-01-01T00:00:31+00:00"},
        {"type": "snapshot"},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    capture = module.load_capture_jsonl(
        path, minutes=60, entity_to_ct={"sensor.a": "ct_a"}
    )
    assert [s.ct_id for s in capture.samples] == ["ct_a", "ct_a"]
    assert [s.value_w for s in capture.samples] == [100.0, 140.0]
    assert any("channel not in topology" in note for note in capture.notes)


def test_observation_frames_skip_empty_ticks(house) -> None:
    """Frames only exist where a reader sample had arrived; no 0 W invention."""
    module = _replay_module()
    capture = _capture_from_house(house)
    frames = module.observation_frames(capture, tick_s=10.0)
    assert frames
    for frame in frames:
        assert frame["reader_samples"]
        for sample in frame["reader_samples"].values():
            assert sample["value_w"] is not None
