"""The manual ``dedicated_device`` declaration is deprecated; its measurement is not.

Three storage locations describe the same fact — ``CTChannel.device_id``,
``DeviceTopology.dedicated_ct_ids`` and ``DeviceConfiguration.monitored_by_ct_ids``
— so the notice is one entry per ``ct_id``, not one per place it was written.

The hard invariant is that this change is diagnostics only: watts, ``source``,
``tier`` and ``confidence`` must be byte-identical with
``WM_DEPRECATE_DEDICATED_DECLARATION`` on and off, and ``resolve_direct`` must
keep returning the same reading.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

import pytest
from wattmosaic_engine.config.settings import EngineConfig
from wattmosaic_engine.contracts import (
    CTChannel,
    CTType,
    DeviceConfiguration,
    DeviceConfigurationSet,
    DeviceTopology,
    InferenceResult,
    MonitoringLevel,
    Phase,
    Source,
    Topology,
    TopologyStatus,
)
from wattmosaic_engine.deprecations import (
    DEDICATED_DECLARATION,
    DEPRECATED_DECLARATIONS,
    Deprecation,
    collect,
    reset_warnings,
    warn_once,
)
from wattmosaic_engine.diagnostics.report import collect_diagnostics
from wattmosaic_engine.disaggregation.engine import DisaggregationEngine
from wattmosaic_engine.electrical.direct import resolve_direct
from wattmosaic_engine.electrical.topology import build_graph
from wattmosaic_engine.electrical.window import ObservationWindow

AT = datetime(2026, 11, 13, 12, 0, tzinfo=UTC)

SCHEMA_KEYS = {"code", "ct_id", "device_id", "since", "replacement", "message"}


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #
def _ch(
    ct_id: str,
    ct_type: CTType = CTType.BRANCH,
    *,
    phase: Phase = Phase.L1,
    device: str | None = None,
) -> CTChannel:
    return CTChannel(
        ct_id=ct_id,
        entity_id=f"sensor.{ct_id}",
        ct_type=ct_type,
        phase=phase,
        device_id=device,
        topology_status=TopologyStatus.CONFIRMED,
    )


def _topology(*, dedicated: bool = True) -> Topology:
    """One main pair, one branch row and (optionally) one dedicated TV row."""
    channels = [
        _ch("main_l1", CTType.MAIN, phase=Phase.L1),
        _ch("main_l2", CTType.MAIN, phase=Phase.L2),
        _ch("ct_lights", CTType.BRANCH),
    ]
    devices = [
        DeviceTopology(
            device_id="lights",
            branch_ct_ids=["ct_lights"],
            phase=Phase.L1,
            topology_status=TopologyStatus.CONFIRMED,
        )
    ]
    if dedicated:
        channels.append(_ch("ct_tv", CTType.DEDICATED_DEVICE, device="tv"))
        devices.append(
            DeviceTopology(
                device_id="tv",
                dedicated_ct_ids=["ct_tv"],
                phase=Phase.L1,
                topology_status=TopologyStatus.CONFIRMED,
            )
        )
    return Topology(
        version=3,
        status=TopologyStatus.CONFIRMED,
        channels=channels,
        devices=devices,
    )


def _configs() -> DeviceConfigurationSet:
    """The third copy of the same fact: a per-device CT declaration."""
    return DeviceConfigurationSet(
        devices=[
            DeviceConfiguration(
                device_id="tv",
                monitoring=MonitoringLevel.DEDICATED,
                monitored_by_ct_ids=["ct_tv"],
            )
        ]
    )


def _window() -> ObservationWindow:
    return ObservationWindow.from_values(
        AT,
        {"main_l1": 400.0, "main_l2": 300.0, "ct_lights": 120.0, "ct_tv": 75.0},
    )


class _FakeCore:
    """The two members ``collect_diagnostics`` needs from an engine core."""

    def __init__(self, topology: Topology) -> None:
        self._topology = topology

    def health(self) -> dict[str, object]:
        return {"status": "ok", "contract_version": "1.6.0", "engine_version": "test"}

    def topology(self) -> Topology:
        return self._topology


# --------------------------------------------------------------------------- #
# 1. One entry per CT, even when all three locations describe it
# --------------------------------------------------------------------------- #
def test_all_three_storage_locations_collapse_to_one_deprecation() -> None:
    topology = _topology()

    # Precondition: the same fact really is stored three times.
    assert topology.channel_map()["ct_tv"].device_id == "tv"
    assert topology.device_map()["tv"].dedicated_ct_ids == ["ct_tv"]
    assert _configs().get("tv").monitored_by_ct_ids == ["ct_tv"]  # type: ignore[union-attr]

    notices = collect(topology, _configs())

    assert len(notices) == 1
    notice = notices[0]
    assert isinstance(notice, Deprecation)
    assert notice.ct_id == "ct_tv"
    assert notice.device_id == "tv"
    assert notice.code == DEDICATED_DECLARATION == "dedicated_declaration"
    assert notice.since == "1.7.0"
    # The replacement is named, and it is explicitly not applied.
    assert "branch" in notice.replacement
    assert "CircuitClass.DEDICATED" in notice.replacement
    assert "CONFIRMED" in notice.replacement
    assert "auto-converted" in notice.replacement


def test_two_dedicated_cts_produce_two_entries() -> None:
    topology = _topology()
    topology.channels.append(_ch("ct_tub", CTType.DEDICATED_DEVICE, device="tub"))
    topology.devices.append(
        DeviceTopology(device_id="tub", dedicated_ct_ids=["ct_tub"], phase=Phase.L1)
    )

    assert [n.ct_id for n in collect(topology)] == ["ct_tv", "ct_tub"]


def test_only_dedicated_declarations_are_reported() -> None:
    # A branch CT is a circuit, not a private measurement, even when one device
    # is declared on it.
    configs = DeviceConfigurationSet(
        devices=[
            DeviceConfiguration(
                device_id="lights",
                monitoring=MonitoringLevel.SHARED,
                monitored_by_ct_ids=["ct_lights"],
            )
        ]
    )
    assert collect(_topology(dedicated=False), configs) == []


def test_message_names_the_replacement_and_the_non_conversion() -> None:
    notice = collect(_topology())[0]
    assert "ct_tv" in notice.message
    assert "branch" in notice.message
    assert "CircuitClass.DEDICATED" in notice.message
    assert "auto-converted" in notice.message


def test_registry_documents_the_code() -> None:
    assert DEDICATED_DECLARATION in DEPRECATED_DECLARATIONS
    # Nothing collects an unregistered code: the registry is the vocabulary.
    assert {n.code for n in collect(_topology())} <= set(DEPRECATED_DECLARATIONS)


# --------------------------------------------------------------------------- #
# 2. The hard invariant: inference output is byte-identical either way
# --------------------------------------------------------------------------- #
def _fingerprint(results: list[InferenceResult]) -> dict[str, tuple[object, ...]]:
    return {
        r.device_id: (
            r.power_w,
            r.source.value if hasattr(r.source, "value") else r.source,
            r.confidence,
            r.confidence_tier.value
            if hasattr(r.confidence_tier, "value")
            else r.confidence_tier,
        )
        for r in results
    }


def _infer_with_flag(flag: bool) -> tuple[dict[str, tuple[object, ...]], list[dict]]:
    config = EngineConfig(deprecate_dedicated_declaration=flag)
    topology = _topology()
    # Everything a caller may touch when the flag is on is included here, so a
    # leak from the notice path into inference would show up in the fingerprint.
    notices: list[dict] = []
    if config.deprecate_dedicated_declaration:
        found = collect(topology, _configs())
        warn_once(found, logging.getLogger("test.deprecations"))
        notices = [n.as_dict() for n in found]

    batch = DisaggregationEngine().infer(
        _window(),
        device_states={"lights": "on", "tv": "on"},
        models=None,
        topology=topology,
    )
    return _fingerprint(batch.results), notices


def test_inference_output_is_identical_with_the_flag_on_and_off() -> None:
    on, notices_on = _infer_with_flag(True)
    off, notices_off = _infer_with_flag(False)

    assert notices_on, "the dedicated declaration must produce a notice when enabled"
    assert notices_off == []
    assert on == off, f"flag changed inference output: {on} != {off}"
    # Not a vacuous comparison: the dedicated device is in the fingerprint and
    # is still a direct measurement.
    assert on["tv"][1] == Source.DIRECT_MEASURED.value
    assert on["tv"][0] == pytest.approx(75.0)


# --------------------------------------------------------------------------- #
# 3. The capability survives: resolve_direct is unchanged
# --------------------------------------------------------------------------- #
def test_resolve_direct_still_measures_a_dedicated_ct() -> None:
    graph = build_graph(_topology())
    readings = resolve_direct(_window(), graph)

    assert set(readings) == {"tv"}
    reading = readings["tv"]
    assert reading.power_w == pytest.approx(75.0)
    assert reading.usable is True
    assert reading.ct_ids == ("ct_tv",)
    assert reading.quality.value == "good"


# --------------------------------------------------------------------------- #
# 4. warn_once: once per ct_id across calls, never in a sample loop
# --------------------------------------------------------------------------- #
def test_warn_once_emits_one_warning_per_ct_across_repeated_calls(caplog) -> None:
    reset_warnings()
    notices = collect(_topology())
    logger = logging.getLogger("test.deprecations.once")

    with caplog.at_level(logging.WARNING, logger=logger.name):
        assert warn_once(notices, logger) == 1
        # A per-sample loop: the same topology is re-scanned 250 times.
        for _ in range(250):
            assert warn_once(collect(_topology()), logger) == 0

    records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(records) == 1
    assert "ct_tv" in records[0].getMessage()


def test_warn_once_does_not_log_below_warning(caplog) -> None:
    reset_warnings()
    logger = logging.getLogger("test.deprecations.level")
    with caplog.at_level(logging.INFO, logger=logger.name):
        warn_once(collect(_topology()), logger)
    assert [r.levelno for r in caplog.records] == [logging.WARNING]


def test_reset_warnings_makes_the_notice_emittable_again(caplog) -> None:
    logger = logging.getLogger("test.deprecations.reset")
    reset_warnings()
    with caplog.at_level(logging.WARNING, logger=logger.name):
        warn_once(collect(_topology()), logger)
        reset_warnings()
        warn_once(collect(_topology()), logger)
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2


# --------------------------------------------------------------------------- #
# 5. Diagnostics: exact schema, additive key, empty when nothing is declared
# --------------------------------------------------------------------------- #
def _diagnostics(topology: Topology) -> dict:
    return collect_diagnostics(config=EngineConfig(), core=_FakeCore(topology))


def test_diagnostics_carries_the_frozen_schema() -> None:
    report = _diagnostics(_topology())
    deprecations = report["engine_health"]["deprecations"]

    assert isinstance(deprecations, list)
    assert len(deprecations) == 1
    entry = deprecations[0]
    assert set(entry) == SCHEMA_KEYS
    assert entry["code"] == "dedicated_declaration"
    assert entry["ct_id"] == "ct_tv"
    assert entry["device_id"] == "tv"
    assert entry["since"] == "1.7.0"
    assert isinstance(entry["replacement"], str) and entry["replacement"]
    assert isinstance(entry["message"], str) and "branch" in entry["message"]
    # Still JSON-serialisable at the API boundary.
    json.dumps(report)


def test_diagnostics_deprecations_are_empty_without_a_declaration() -> None:
    report = _diagnostics(_topology(dedicated=False))
    assert report["engine_health"]["deprecations"] == []


def test_disabling_the_setting_silences_diagnostics_but_keeps_the_key() -> None:
    report = collect_diagnostics(
        config=EngineConfig(deprecate_dedicated_declaration=False),
        core=_FakeCore(_topology()),
    )
    assert report["engine_health"]["deprecations"] == []


def test_health_details_expose_the_same_list(tmp_path) -> None:
    from wattmosaic_engine.main import EngineCore
    from wattmosaic_engine.storage.database import Database

    db = Database(tmp_path / "deprecations.db")
    try:
        db.save_topology(_topology())
        core = EngineCore(EngineConfig(ha_token="not-used"), db)
        health = core.health()
        deprecations = health.details["deprecations"]
        assert [d["ct_id"] for d in deprecations] == ["ct_tv"]
        assert set(deprecations[0]) == SCHEMA_KEYS

        core.config.deprecate_dedicated_declaration = False
        assert core.health().details["deprecations"] == []
    finally:
        db.close()


def test_health_probe_does_not_read_configurations_without_a_declaration(tmp_path) -> None:
    """The probe runs every few seconds; it must not read the DB to say "nothing"."""
    from wattmosaic_engine.deprecations import declares_dedicated
    from wattmosaic_engine.main import EngineCore
    from wattmosaic_engine.storage.database import Database

    assert declares_dedicated(_topology()) is True
    assert declares_dedicated(_topology(dedicated=False)) is False
    # A stored configuration alone cannot make a branch row a declaration.
    assert declares_dedicated(_topology(dedicated=False)) is False

    db = Database(tmp_path / "probe.db")
    try:
        db.save_topology(_topology(dedicated=False))
        core = EngineCore(EngineConfig(ha_token="not-used"), db)
        statements: list[str] = []
        db._conn.set_trace_callback(statements.append)
        try:
            assert core.health().details["deprecations"] == []
        finally:
            db._conn.set_trace_callback(None)
        assert not any("device_configurations" in s.lower() for s in statements), statements
    finally:
        db.close()


def test_health_probe_does_not_read_configurations_even_with_a_declaration(
    tmp_path,
) -> None:
    """The positive case must be as cheap as the negative one.

    ``/healthz`` proxies ``/api/v1/health`` and the container ``HEALTHCHECK``
    and Supervisor watchdog poll it every 5 s.  ``health()`` runs on the event
    loop, so any table read here blocks ingestion — the defect recorded in
    ``test_health_probe_cost.py``.  A declaration must therefore be answered
    from the in-memory topology and the already-cached probe value, never by
    reading ``device_configurations`` per poll.
    """
    from wattmosaic_engine.main import EngineCore
    from wattmosaic_engine.storage.database import Database

    db = Database(tmp_path / "probe_positive.db")
    try:
        db.save_topology(_topology())
        core = EngineCore(EngineConfig(ha_token="not-used"), db)
        # Prime the cache so the read below is the *repeat* poll, not the first.
        core.health()

        statements: list[str] = []
        db._conn.set_trace_callback(statements.append)
        try:
            health = core.health()
        finally:
            db._conn.set_trace_callback(None)

        assert [d["ct_id"] for d in health.details["deprecations"]] == ["ct_tv"]
        offenders = [s for s in statements if "device_configurations" in s.lower()]
        assert not offenders, (
            "health() read the configuration table on a probe path; that blocks "
            f"the event loop every poll. Statements: {offenders}"
        )
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# 6. A pre-change topology (no ``deprecations`` key anywhere) still loads
# --------------------------------------------------------------------------- #
def test_a_pre_change_topology_round_trips_and_still_reports(tmp_path) -> None:
    from wattmosaic_engine.main import EngineCore
    from wattmosaic_engine.storage.database import Database

    legacy = {
        "version": 3,
        "status": "confirmed",
        "channels": [
            {
                "ct_id": "ct_tv",
                "entity_id": "sensor.tv_power",
                "ct_type": "dedicated_device",
                "phase": "l1",
                "device_id": "tv",
                "topology_status": "confirmed",
            }
        ],
        "devices": [
            {
                "device_id": "tv",
                "dedicated_ct_ids": ["ct_tv"],
                "phase": "l1",
                "topology_status": "confirmed",
            }
        ],
    }
    assert "deprecations" not in json.dumps(legacy)

    topology = Topology.model_validate(legacy)
    assert [n.ct_id for n in collect(topology)] == ["ct_tv"]

    db = Database(tmp_path / "legacy.db")
    try:
        db.save_topology(topology)
        loaded = db.load_topology()
        assert loaded is not None
        core = EngineCore(EngineConfig(ha_token="not-used"), db)
        assert core.health().status in ("ok", "degraded")
        assert [d["ct_id"] for d in core.health().details["deprecations"]] == ["ct_tv"]
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Setting plumbing
# --------------------------------------------------------------------------- #
def test_setting_defaults_true_and_is_read_from_the_environment(monkeypatch, tmp_path) -> None:
    assert EngineConfig().deprecate_dedicated_declaration is True
    assert EngineConfig().redacted()["deprecate_dedicated_declaration"] is True

    missing = tmp_path / "no-such.env"
    monkeypatch.setenv("WM_DEPRECATE_DEDICATED_DECLARATION", "false")
    assert EngineConfig.from_env(dotenv_path=missing).deprecate_dedicated_declaration is False
    monkeypatch.setenv("WM_DEPRECATE_DEDICATED_DECLARATION", "true")
    assert EngineConfig.from_env(dotenv_path=missing).deprecate_dedicated_declaration is True
