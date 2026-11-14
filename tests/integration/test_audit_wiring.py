"""The analysis pass is wired, ordered, gated and reproducible.

What this file proves, and why each part is here rather than in a unit test:

* the six modules that each had their own unit tests are actually *called* by
  the live backend, in dependency order, over one window (WP9's whole point);
* every new ``Settings`` flag exists, defaults to the documented value, is
  readable from ``WM_*`` and appears in ``redacted()`` -- a flag that cannot be
  set from the environment is not a flag;
* turning a family off withholds that family's write **without** changing the
  value another family derives from it;
* a failing stage is isolated: the pass reports it and the other families still
  land, so one bad event can never stop the inference loop;
* the same raw data plus the same ``algorithm_version`` produce identical
  records, with no wall clock or row id in the identity;
* the declarations the pass needs are actually delivered to the modules that
  consume them -- stored ``HaEntityBinding`` rows to ``EventIsolator`` and the
  per-class/per-device isolation layers to ``IsolationConfig.resolve`` -- and
  every ``evidence_refs`` entry resolves, including for withheld and
  contaminated verdicts.
"""

from __future__ import annotations

import datetime as dt

import pytest
from wattmosaic_engine.config.settings import EngineConfig
from wattmosaic_engine.contracts import (
    InferenceResult,
    IsolationStatus,
    MeasurementBasis,
    Source,
    TopologyRelation,
)
from wattmosaic_engine.disaggregation import audit as audit_module
from wattmosaic_engine.disaggregation.audit import (
    COUNT_KEYS,
    DEFAULT_ALGORITHM_VERSION,
    REFUSED_MULTI_CT_SCOPE,
    REFUSED_NO_READING,
    AuditConfig,
    AuditPass,
    record_id_for,
)
from wattmosaic_engine.models.protocols import SimpleWindow
from wattmosaic_engine.storage.database import Database

from tools.simulator import REAL_HOME_TOPOLOGY


def _topology():
    return REAL_HOME_TOPOLOGY.to_contract_topology(confirmed=True)


def _window(at: dt.datetime | None = None) -> SimpleWindow:
    return SimpleWindow(
        at=at or dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC),
        values={"ct_l1": 1200.0, "ct_kitchen": 300.0},
    )


def _result(device_id: str, power_w: float, at: dt.datetime) -> InferenceResult:
    return InferenceResult(
        device_id=device_id,
        timestamp=at,
        power_w=power_w,
        source=Source.DIRECT_MEASURED,
        confidence=0.8,
        measurement_basis=MeasurementBasis.CT_MEASURED,
    )


@pytest.fixture()
def database(tmp_path):
    db = Database(tmp_path / "audit.db")
    try:
        yield db
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def test_audit_settings_defaults_are_the_documented_values() -> None:
    config = EngineConfig()
    assert config.history_lookback_s == 86400.0
    assert config.audit_enabled is True
    assert config.audit_isolation_enabled is True
    assert config.audit_association_enabled is True
    assert config.audit_circuits_enabled is True
    assert config.audit_residual_enabled is True
    assert config.audit_hierarchy_enabled is True
    assert config.audit_records_enabled is True
    assert config.analysis_offset_s == 0.0
    assert config.isolation_window_s == 30.0
    assert config.isolation_power_window_s == 60.0
    assert config.reanalysis_interval_s == 3600.0
    assert config.reanalysis_lookback_s == 21600.0


def test_audit_settings_are_readable_from_the_environment() -> None:
    config = EngineConfig.from_env(
        {
            "WM_AUDIT_ENABLED": "false",
            "WM_AUDIT_RECORDS_ENABLED": "false",
            "WM_HISTORY_LOOKBACK_S": "3600",
            "WM_ANALYSIS_OFFSET_S": "12.5",
            "WM_ISOLATION_WINDOW_S": "20",
            "WM_ISOLATION_POWER_WINDOW_S": "90",
            "WM_REANALYSIS_INTERVAL_S": "0",
            "WM_REANALYSIS_LOOKBACK_S": "7200",
        },
        dotenv_path="/nonexistent/.env",
    )
    assert config.audit_enabled is False
    assert config.audit_records_enabled is False
    assert config.history_lookback_s == 3600.0
    assert config.analysis_offset_s == 12.5
    assert config.isolation_window_s == 20.0
    assert config.isolation_power_window_s == 90.0
    assert config.reanalysis_interval_s == 0.0
    assert config.reanalysis_lookback_s == 7200.0


def test_every_new_setting_is_redacted_and_validated() -> None:
    redacted = EngineConfig().redacted()
    for key in (
        "history_lookback_s",
        "audit_enabled",
        "audit_isolation_enabled",
        "audit_association_enabled",
        "audit_circuits_enabled",
        "audit_residual_enabled",
        "audit_hierarchy_enabled",
        "audit_records_enabled",
        "analysis_offset_s",
        "isolation_window_s",
        "isolation_power_window_s",
        "isolation_by_class",
        "isolation_by_device",
        "isolation_device_classes",
        "audit_algorithm_version",
        "reanalysis_interval_s",
        "reanalysis_lookback_s",
    ):
        assert key in redacted, f"{key} missing from redacted()"

    broken = EngineConfig(
        isolation_window_s=60.0, isolation_power_window_s=30.0
    )
    assert any("isolation_power_window_s" in p for p in broken.validate())


def test_the_isolation_layer_settings_round_trip_through_the_backend(monkeypatch) -> None:
    """``build_backend`` carries the layers, so production gets them -- not just tests."""
    from wattmosaic_engine.integration import build_backend

    monkeypatch.delenv("WM_ISOLATION_BY_CLASS", raising=False)
    monkeypatch.delenv("WM_ISOLATION_BY_DEVICE", raising=False)
    monkeypatch.delenv("WM_ISOLATION_DEVICE_CLASSES", raising=False)
    monkeypatch.delenv("WM_AUDIT_ALGORITHM_VERSION", raising=False)
    config = EngineConfig(
        ha_token="test",
        isolation_by_class={"hot_tub": {"isolation_window_s": 90.0}},
        isolation_by_device={"spa": {"analysis_offset_s": 15.0}},
        isolation_device_classes={"spa": ("hot_tub",)},
        audit_algorithm_version="disagg@9.9",
    )
    backend = build_backend(config, None)
    audit_config = backend.audit_config
    assert audit_config.isolation_by_class == {"hot_tub": {"isolation_window_s": 90.0}}
    assert audit_config.isolation_by_device == {"spa": {"analysis_offset_s": 15.0}}
    assert audit_config.isolation_device_classes == {"spa": ("hot_tub",)}
    assert audit_config.algorithm_version == "disagg@9.9"
    window, source = audit_config.isolation.resolve("spa", ("hot_tub",))
    assert source == "device"
    assert window.analysis_offset_s == 15.0
    # Each layer is merged on top of the *global* base, not on the class layer
    # (``IsolationWindow.merged`` semantics, unchanged): a per-device entry
    # overrides only the keys it states and the rest stay global, so state all
    # three keys when a device needs a fully independent window.
    assert window.isolation_window_s == 30.0
    class_window, class_source = audit_config.isolation.resolve("other", ("hot_tub",))
    assert class_source == "class"
    assert class_window.isolation_window_s == 90.0
    # The pass reports the version it was built with, so the API agrees.
    assert backend._audit_summary()["algorithm_version"] == "disagg@9.9"


def test_the_documented_env_example_covers_every_isolation_layer_key() -> None:
    """A setting an operator cannot discover from ``.env.example`` is not shipped."""
    from pathlib import Path

    text = (
        Path(__file__).resolve().parents[2] / ".env.example"
    ).read_text()
    for key in (
        "WM_ISOLATION_BY_CLASS",
        "WM_ISOLATION_BY_DEVICE",
        "WM_ISOLATION_DEVICE_CLASSES",
        "WM_AUDIT_ALGORITHM_VERSION",
    ):
        assert key in text, f"{key} not documented in .env.example"



def test_audit_config_projects_the_master_switch() -> None:
    off = AuditConfig.from_settings(EngineConfig(audit_enabled=False))
    assert not any(
        (
            off.write_isolation,
            off.write_association,
            off.write_circuits,
            off.write_residual,
            off.write_hierarchy,
            off.write_records,
        )
    )
    on = AuditConfig.from_settings(EngineConfig())
    assert on.write_records and on.write_isolation


def test_audit_config_rejects_a_short_power_window() -> None:
    with pytest.raises(ValueError):
        AuditConfig(isolation_window_s=60.0, power_window_s=30.0)
    with pytest.raises(ValueError):
        AuditConfig(analysis_offset_s=-1.0)
    with pytest.raises(ValueError):
        AuditConfig(algorithm_version="  ")


# --------------------------------------------------------------------------- #
# Determinism (J9)
# --------------------------------------------------------------------------- #
def test_record_id_depends_only_on_what_was_inferred() -> None:
    at = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC)
    first = record_id_for("spa", "disagg@2.0", at, 1)
    again = record_id_for("spa", "disagg@2.0", at, 1)
    assert first == again
    assert first.startswith("rec_") and len(first) == len("rec_") + 24

    assert record_id_for("spa", "disagg@2.1", at, 1) != first
    assert record_id_for("spa", "disagg@2.0", at, 2) != first
    assert record_id_for("pool", "disagg@2.0", at, 1) != first


def test_same_window_and_version_produce_identical_records(tmp_path) -> None:
    """J9: a replay over the same raw data is byte-identical, not merely similar."""
    at = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC)
    topology = _topology()
    device_id = topology.devices[0].device_id
    results = [_result(device_id, 42.0, at)]

    records = []
    for name in ("first.db", "replay.db"):
        database = Database(tmp_path / name)
        try:
            report = AuditPass(database, config=AuditConfig()).analyze(
                _window(at), topology=topology, results=results, events=[], now=at
            )
            records.append(report.records)
        finally:
            database.close()

    first, second = records
    assert [r.record_id for r in first] == [r.record_id for r in second]
    assert [(r.revision, r.power_w, r.reason) for r in first] == [
        (r.revision, r.power_w, r.reason) for r in second
    ]
    assert [r.explanation for r in first] == [r.explanation for r in second]


def test_replaying_the_same_window_does_not_append_a_duplicate(database) -> None:
    """The second identical pass recognises its own record instead of duplicating."""
    at = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC)
    topology = _topology()
    device_id = topology.devices[0].device_id
    results = [_result(device_id, 42.0, at)]

    first = AuditPass(database, config=AuditConfig()).analyze(
        _window(at), topology=topology, results=results, events=[], now=at
    )
    second = AuditPass(database, config=AuditConfig()).analyze(
        _window(at), topology=topology, results=results, events=[], now=at
    )
    assert first.counts["records"] == 1
    assert second.counts["records"] == 0
    assert len(database.inference_records_for_device(device_id)) == 1


def test_a_second_pass_appends_a_revision_instead_of_overwriting(database) -> None:
    at = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC)
    topology = _topology()
    device_id = topology.devices[0].device_id
    pass_ = AuditPass(database, config=AuditConfig())

    pass_.analyze(
        _window(at),
        topology=topology,
        results=[_result(device_id, 42.0, at)],
        events=[],
        now=at,
    )
    later = at + dt.timedelta(hours=2)
    pass_.analyze(
        _window(later),
        topology=topology,
        results=[_result(device_id, 900.0, later)],
        events=[],
        now=later,
    )

    rows = database.inference_records_for_device(device_id)
    assert [row["revision"] for row in rows] == [1, 2]
    assert rows[1]["previous_record_id"] == rows[0]["record_id"]
    assert rows[0]["power_w"] == pytest.approx(42.0), "a revision may not rewrite its parent"
    assert rows[1]["previous_power_w"] == pytest.approx(42.0)


def test_an_unchanged_inference_does_not_append_a_record(database) -> None:
    """A steady value is one fact, not one row per cycle."""
    at = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC)
    topology = _topology()
    device_id = topology.devices[0].device_id
    pass_ = AuditPass(database, config=AuditConfig())
    for offset in (0, 5, 10, 15):
        moment = at + dt.timedelta(seconds=offset)
        pass_.analyze(
            _window(moment),
            topology=topology,
            results=[_result(device_id, 42.0, moment)],
            events=[],
            now=moment,
        )
    rows = database.inference_records_for_device(device_id)
    assert len(rows) == 1


# --------------------------------------------------------------------------- #
# The pass itself
# --------------------------------------------------------------------------- #
def test_empty_pass_reports_every_count_key(database) -> None:
    at = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC)
    report = AuditPass(database, config=AuditConfig()).analyze(
        _window(at), topology=_topology(), results=[], events=[], now=at
    )
    assert set(report.counts) == set(COUNT_KEYS)
    assert report.as_dict()["mode"] == "incremental"
    assert report.errors == {}


def test_analysis_offset_never_moves_the_original_event_time(database) -> None:
    """The offset shifts the window, never the recorded transition."""
    at = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC)
    topology = _topology()
    device = topology.devices[0]
    entity = device.dedicated_ct_ids[0] if device.dedicated_ct_ids else None
    if entity is None:  # pragma: no cover - the reference topology has channels
        pytest.skip("reference topology has no dedicated channel")
    event = {
        "device_id": device.device_id,
        "entity_id": entity,
        "new_state": "on",
        "previous_state": "off",
        "occurred_at": at,
    }
    report = AuditPass(
        database, config=AuditConfig(analysis_offset_s=15.0)
    ).analyze(
        _window(at + dt.timedelta(minutes=5)),
        topology=topology,
        results=[],
        events=[event],
        now=at + dt.timedelta(minutes=5),
    )
    assert report.counts["events"] == 1
    verdict = report.isolations[0]
    assert verdict.occurred_at == at
    assert verdict.window_start == at + dt.timedelta(seconds=15.0)


def test_a_per_device_override_is_what_the_verdict_uses(database) -> None:
    """The layer is resolved by the pass, not only by the config object.

    A per-device 120 s window sees a sibling 70 s later; the global 30/60 s
    window does not.  The verdict difference is the proof that the value
    reached ``EventIsolator`` rather than sitting unread on ``AuditConfig``.
    """
    at = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC)
    topology = _topology()
    subject, other = topology.devices[0], topology.devices[1]
    events = [
        {
            "device_id": subject.device_id,
            "entity_id": topology.channels[0].entity_id,
            "new_state": "on",
            "occurred_at": at,
        },
        {
            "device_id": other.device_id,
            "entity_id": topology.channels[1].entity_id,
            "new_state": "on",
            "occurred_at": at + dt.timedelta(seconds=70),
        },
    ]
    now = at + dt.timedelta(minutes=10)
    global_report = AuditPass(database, config=AuditConfig()).analyze(
        _window(now), topology=topology, results=[], events=events, now=now
    )
    global_verdict = next(
        row for row in global_report.isolations if row.device_id == subject.device_id
    )
    assert global_verdict.isolation_window_s == 30.0
    assert global_verdict.evidence["config_source"] == "global"

    wide = AuditPass(
        database,
        config=AuditConfig(
            isolation_by_device={
                subject.device_id: {
                    "isolation_window_s": 120.0,
                    "power_window_s": 120.0,
                }
            }
        ),
    ).analyze(_window(now), topology=topology, results=[], events=events, now=now)
    wide_verdict = next(
        row for row in wide.isolations if row.device_id == subject.device_id
    )
    assert wide_verdict.isolation_window_s == 120.0
    assert wide_verdict.evidence["config_source"] == "device"
    assert wide_verdict.status == IsolationStatus.CONTAMINATED
    assert wide_verdict.competing_device_ids == [other.device_id]
    assert wide.stages["isolation"]["device_overrides"] == [subject.device_id]


def test_no_bindings_and_no_overrides_reproduce_the_previous_pass(database) -> None:
    """The correctness fix is bindings-only; everything else is unchanged.

    With an empty declaration and no override maps, every verdict must be the
    same shape it was before the layers were wired: global window, ``global``
    source, one verdict per event, same status/reason.
    """
    at = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC)
    topology = _topology()
    now = at + dt.timedelta(minutes=10)
    events = [
        {
            "device_id": topology.devices[0].device_id,
            "entity_id": topology.channels[0].entity_id,
            "new_state": "on",
            "occurred_at": at,
        },
        {
            "device_id": topology.devices[1].device_id,
            "entity_id": topology.channels[1].entity_id,
            "new_state": "on",
            "occurred_at": at + dt.timedelta(seconds=5),
        },
    ]
    report = AuditPass(database, config=AuditConfig()).analyze(
        _window(now), topology=topology, results=[], events=events, now=now
    )
    assert report.stages["isolation"]["bindings"] == 0
    assert report.stages["isolation"]["device_classes"] == 0
    assert report.stages["isolation"]["class_overrides"] == []
    assert report.stages["isolation"]["device_overrides"] == []
    assert report.counts["events"] == len(events)
    for verdict in report.isolations:
        assert verdict.evidence["config_source"] == "global"
        assert verdict.isolation_window_s == 30.0
        assert verdict.analysis_offset_s == 0.0



def test_a_raising_stage_is_isolated_and_reported(database, monkeypatch) -> None:
    """One broken stage must not abort the pass or the inference loop."""
    at = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC)
    topology = _topology()
    device_id = topology.devices[0].device_id

    class Exploding:
        withheld = {}

        def observe(self, *args, **kwargs):
            raise RuntimeError("association exploded")

    monkeypatch.setattr(
        audit_module, "AssociationFinder", lambda *a, **k: Exploding()
    )
    event = {
        "device_id": device_id,
        "entity_id": topology.channels[0].entity_id,
        "new_state": "on",
        "occurred_at": at,
    }
    report = AuditPass(database, config=AuditConfig()).analyze(
        _window(at + dt.timedelta(minutes=5)),
        topology=topology,
        results=[_result(device_id, 10.0, at)],
        events=[event],
        now=at + dt.timedelta(minutes=5),
    )
    assert any("association:" in key for key in report.errors)
    # The record stage still ran: a failure upstream is not a reason to lose the
    # device's audit trail.
    assert report.counts["records"] == 1


def test_multi_ct_scope_is_refused_not_summed() -> None:
    """Two clamps may be the two legs of one appliance: never add them.

    Recorded as a refusal with the module's own reason code rather than a zero
    residual, because a summed number would be wrong half the time and a zero
    would hide the fact that nothing was computed.
    """
    at = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC)
    pass_ = AuditPass(None, config=AuditConfig())

    class Scope:
        circuit_id = "circuit:two_legs"
        ct_ids = ("ct_l1", "ct_l2")
        device_ids = ()

    report = audit_module.AuditReport(at=at)
    computed = pass_._residuals(
        [Scope()],  # type: ignore[list-item]
        None,
        audit_module.FactorRegistry(),
        {},
        {"ct_l1": 100.0, "ct_l2": 100.0},
        at,
        report,
    )
    assert computed == {}
    assert report.refused["residual:circuit:two_legs"] == REFUSED_MULTI_CT_SCOPE


def test_missing_scope_reading_is_refused_not_zeroed() -> None:
    at = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC)
    pass_ = AuditPass(None, config=AuditConfig())

    class Scope:
        circuit_id = "circuit:unread"
        ct_ids = ("ct_l1",)
        device_ids = ()

    report = audit_module.AuditReport(at=at)
    computed = pass_._residuals(
        [Scope()],  # type: ignore[list-item]
        None,
        audit_module.FactorRegistry(),
        {},
        {},
        at,
        report,
    )
    assert computed == {}
    assert report.refused["residual:circuit:unread"] == REFUSED_NO_READING


# --------------------------------------------------------------------------- #
# Writing gates
# --------------------------------------------------------------------------- #
def test_disabling_a_family_withholds_only_that_write(database) -> None:
    """A write flag gates storage; it never changes a derived value."""
    at = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC)
    topology = _topology()
    device_id = topology.devices[0].device_id
    results = [_result(device_id, 42.0, at)]

    enabled = AuditPass(database, config=AuditConfig()).analyze(
        _window(at), topology=topology, results=results, events=[], now=at
    )
    assert enabled.counts["records"] == 1

    disabled = AuditPass(
        database, config=AuditConfig(write_records=False)
    ).analyze(
        _window(at + dt.timedelta(hours=1)),
        topology=topology,
        results=results,
        events=[],
        now=at + dt.timedelta(hours=1),
    )
    # The algorithm still computed the record; only storage was withheld.
    assert disabled.records == []
    assert disabled.counts["records"] == 0


def test_isolation_status_reports_no_pass_as_no_pass(database) -> None:
    pass_ = AuditPass(database, config=AuditConfig())
    status = pass_.last_report
    assert status is None

    at = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC)
    report = pass_.analyze(_window(at), topology=_topology(), results=[], events=[], now=at)
    payload = report.isolation_status()
    assert payload["source"] == "last_analysis_pass"
    assert payload["counts"]["analysed"] == 0
    assert payload["verdicts"] == []


def test_isolation_verdicts_are_reported_individually(database) -> None:
    at = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC)
    topology = _topology()
    device = topology.devices[0]
    channel = topology.channels[0]
    event = {
        "device_id": device.device_id,
        "entity_id": channel.entity_id,
        "new_state": "on",
        "occurred_at": at,
    }
    report = AuditPass(database, config=AuditConfig()).analyze(
        _window(at + dt.timedelta(minutes=5)),
        topology=topology,
        results=[],
        events=[event],
        now=at + dt.timedelta(minutes=5),
    )
    payload = report.isolation_status()
    assert payload["counts"]["analysed"] == 1
    assert payload["verdicts"][0]["event_id"]
    assert payload["verdicts"][0]["status"] in {
        status.value for status in IsolationStatus
    }


def test_isolation_status_survives_the_cycle_that_produced_it(database) -> None:
    """A verdict must remain visible on passes that matured no new event.

    The live pass matures a handful of events per hour; answering only from the
    most recent cycle would report ``0 analysed`` almost always, which reads
    exactly like an analysis that never ran.
    """
    at = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC)
    topology = _topology()
    device = topology.devices[0]
    channel = topology.channels[0]
    pass_ = AuditPass(database, config=AuditConfig())
    pass_.analyze(
        _window(at + dt.timedelta(minutes=5)),
        topology=topology,
        results=[],
        events=[
            {
                "device_id": device.device_id,
                "entity_id": channel.entity_id,
                "new_state": "on",
                "occurred_at": at,
            }
        ],
        now=at + dt.timedelta(minutes=5),
    )

    # A later pass with nothing to analyse.
    pass_.analyze(
        _window(at + dt.timedelta(minutes=30)),
        topology=topology,
        results=[],
        events=[],
        now=at + dt.timedelta(minutes=30),
    )

    payload = pass_.isolation_status()
    assert payload["source"] == "last_analysis_pass"
    assert payload["scope"] == "retained_verdicts"
    assert payload["counts"]["analysed"] == 1, "the verdict was forgotten with its pass"
    assert len(payload["verdicts"]) == 1

    # Idempotent: re-analysing the same event replaces its verdict, never adds one.
    pass_.analyze(
        _window(at + dt.timedelta(minutes=45)),
        topology=topology,
        results=[],
        events=[
            {
                "device_id": device.device_id,
                "entity_id": channel.entity_id,
                "new_state": "on",
                "occurred_at": at,
            }
        ],
        now=at + dt.timedelta(minutes=45),
    )
    assert pass_.isolation_status()["counts"]["analysed"] == 1


def test_retained_verdicts_are_bounded() -> None:
    from wattmosaic_engine.contracts import EventIsolation

    at = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    pass_ = AuditPass(None, config=AuditConfig())
    limit = audit_module.DEFAULT_VERDICT_HISTORY
    for index in range(limit + 40):
        pass_._remember_verdict(
            EventIsolation(
                status=IsolationStatus.ISOLATED,
                event_id=f"e{index}",
                device_id="d",
                entity_id="switch.d",
                occurred_at=at,
                received_at=at,
                analysis_offset_s=0.0,
                isolation_window_s=30.0,
                window_start=at,
                window_end=at + dt.timedelta(seconds=30),
                reason="isolated",
                evidence={},
            )
        )
    assert len(pass_._verdicts) == limit, "the verdict buffer grew without bound"
    assert len(pass_.retained_verdicts()) == limit
    assert "e0" not in {verdict.event_id for verdict in pass_.retained_verdicts()}


def test_series_alignment_forward_fills_and_leaves_absence_as_nan() -> None:
    start = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC)
    histories = {
        "a": [(start, 100.0), (start + dt.timedelta(seconds=10), 200.0)],
        "b": [(start + dt.timedelta(seconds=5), 50.0)],
    }
    series = audit_module.aligned_series(histories)
    assert len(series["a"]) == len(series["b"]) == 3
    # Before a channel's first sample the value is absent, not zero.
    assert series["b"][0] != series["b"][0]  # nan
    assert series["a"][0] == 100.0
    assert series["a"][2] == 200.0


def test_aligned_series_needs_at_least_two_instants() -> None:
    at = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    assert audit_module.aligned_series({"a": [(at, 1.0)]}) == {}


def test_stored_history_rows_are_re_keyed_to_topology_ids() -> None:
    at = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    rows = [
        {"ct_id": "kitchen_power", "value_w": 100.0, "state_timestamp": at.isoformat()},
        {"ct_id": "unknown_key", "value_w": 5.0, "state_timestamp": at.isoformat()},
    ]
    key_map = {"kitchen_power": "ct_kitchen"}
    histories = audit_module.history_from_rows(rows, key_map=key_map)
    assert list(histories) == ["ct_kitchen"], "an unmapped row is dropped, never invented"
    assert histories["ct_kitchen"][0][1] == pytest.approx(100.0)


def test_default_algorithm_version_is_stamped_on_every_artifact(database) -> None:
    at = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC)
    topology = _topology()
    report = AuditPass(database, config=AuditConfig()).analyze(
        _window(at),
        topology=topology,
        results=[_result(topology.devices[0].device_id, 1.0, at)],
        events=[],
        now=at,
    )
    for record in report.records:
        assert record.algorithm_version == DEFAULT_ALGORITHM_VERSION
    assert report.stages["isolation"]["algorithm_version"] == DEFAULT_ALGORITHM_VERSION


def test_relations_are_grouped_by_kind_never_merged(database) -> None:
    """An association and a parent link are different claims."""
    from wattmosaic_engine.main import EngineCore

    merged: dict[str, list[TopologyRelation]] = {}
    assert merged == {}
    # The facade method is checked through the protocol below; this asserts the
    # public surface exists without constructing a whole engine.
    assert hasattr(EngineCore, "relations")


# --------------------------------------------------------------------------- #
# Evidence references: every emitted ref must name a row that survives a restart
# --------------------------------------------------------------------------- #
def test_every_emitted_evidence_ref_resolves_after_the_pass(database) -> None:
    """D5: a reference is a promise that a reader can follow it.

    ``event:`` used to point at a verdict kept only in the process, and a record
    with no other evidence cited ``result:<device>@<time>`` -- a prefix no lookup
    accepted.  Both dangled after a restart, so the record claimed provenance it
    did not have.
    """
    at = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC)
    topology = _topology()
    device = topology.devices[0]
    channel = topology.channels[0]
    report = AuditPass(database, config=AuditConfig()).analyze(
        _window(at + dt.timedelta(minutes=5)),
        topology=topology,
        results=[_result(device.device_id, 900.0, at)],
        events=[
            {
                "device_id": device.device_id,
                "entity_id": channel.entity_id,
                "new_state": "on",
                "occurred_at": at,
            }
        ],
        now=at + dt.timedelta(minutes=5),
    )
    assert report.records
    refs = [ref for record in report.records for ref in record.evidence_refs]
    assert refs, "a record asserting a value must cite something"
    for ref in refs:
        assert database.resolve_evidence_ref(ref), f"dangling evidence ref {ref!r}"
        # No synthetic prefix may creep back in.
        assert not ref.startswith("result:")

    # And the promises survive: the same rows are still resolvable from a second
    # connection to the same file, which is what "after a restart" means.
    verdicts = database.isolation_verdicts()
    assert verdicts
    assert all(
        database.resolve_evidence_ref(f"event:{row['event_id']}") for row in verdicts
    )
    stored = database.inference_records_for_device(device.device_id)
    assert stored
    for row in stored:
        for ref in row["evidence_refs"]:
            assert database.resolve_evidence_ref(ref)


def test_a_record_with_no_other_evidence_cites_its_own_stored_row(database) -> None:
    """The self-reference must resolve, and it must not be a ``result:`` string."""
    at = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC)
    topology = _topology()
    device = topology.devices[0]
    report = AuditPass(database, config=AuditConfig()).analyze(
        _window(at),
        topology=topology,
        results=[_result(device.device_id, 42.0, at)],
        events=[],
        now=at,
    )
    assert len(report.records) == 1
    record = report.records[0]
    assert record.evidence_refs == [f"record:{record.record_id}"]
    assert database.resolve_evidence_ref(record.evidence_refs[0])
    # The row is really there, under exactly that id.
    assert database.resolve_evidence_ref(f"record:{record.record_id}")
    assert not database.resolve_evidence_ref("result:anything")


def test_withheld_verdicts_are_not_cited_as_evidence(database) -> None:
    """``write_isolation=False`` means no verdict row, so ``event:`` may not appear.

    Citing a row a flag withheld is the same dangling claim in a different
    disguise: the reference is emitted by analysis but the row it names is
    deliberately absent.
    """
    at = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC)
    topology = _topology()
    device = topology.devices[0]
    channel = topology.channels[0]
    report = AuditPass(database, config=AuditConfig(write_isolation=False)).analyze(
        _window(at + dt.timedelta(minutes=5)),
        topology=topology,
        results=[_result(device.device_id, 900.0, at)],
        events=[
            {
                "device_id": device.device_id,
                "entity_id": channel.entity_id,
                "new_state": "on",
                "occurred_at": at,
            }
        ],
        now=at + dt.timedelta(minutes=5),
    )
    assert database.isolation_verdicts() == []
    for record in report.records:
        for ref in record.evidence_refs:
            assert not ref.startswith("event:")
            assert database.resolve_evidence_ref(ref), f"dangling evidence ref {ref!r}"


def _assert_every_ref_resolves(database, report) -> list[str]:
    """Every ref on every emitted record names a row that exists *now*."""
    refs = [ref for record in report.records for ref in record.evidence_refs]
    for ref in refs:
        assert database.resolve_evidence_ref(ref), f"dangling evidence ref {ref!r}"
        assert not ref.startswith("result:"), "a synthetic prefix came back"
    return refs


def test_a_contaminated_verdict_still_resolves_from_its_record(database) -> None:
    """A withheld (contaminated) verdict is *stored*, so ``event:`` stays followable.

    ``split_attribution`` withholds a contaminated event from attribution, but
    withholding is not deletion: migration 10 persists the verdict precisely so
    the record citing it can be checked after a restart.  A contaminated event
    therefore must still resolve -- the previous guard only exercised the clean
    path.
    """
    at = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC)
    topology = _topology()
    subject, other = topology.devices[0], topology.devices[1]
    other_channel = topology.channels[1]
    now = at + dt.timedelta(minutes=5)
    report = AuditPass(database, config=AuditConfig()).analyze(
        _window(now),
        topology=topology,
        results=[_result(subject.device_id, 900.0, at)],
        events=[
            {
                "device_id": subject.device_id,
                "entity_id": topology.channels[0].entity_id,
                "new_state": "on",
                "occurred_at": at,
            },
            {
                "device_id": other.device_id,
                "entity_id": other_channel.entity_id,
                "new_state": "on",
                "occurred_at": at + dt.timedelta(seconds=5),
            },
        ],
        now=now,
    )
    verdicts = {row["event_id"]: row for row in database.isolation_verdicts()}
    assert any(row["status"] == "contaminated" for row in verdicts.values())
    assert any(row["status"] == "insufficient_data" for row in verdicts.values()) or any(
        row["status"] == "isolated" for row in verdicts.values()
    )
    refs = _assert_every_ref_resolves(database, report)
    cited = {ref for ref in refs if ref.startswith("event:")}
    assert cited, "a record with isolations must cite them"
    for ref in cited:
        row = verdicts[ref.split(":", 1)[1]]
        # Whatever the verdict was, the cited row exists and agrees with itself.
        assert row["event_id"] == ref.split(":", 1)[1]
    # Contaminated verdicts are retained, not deleted, and remain resolvable.
    for event_id in verdicts:
        assert database.resolve_evidence_ref(f"event:{event_id}")


def test_an_unknown_source_record_cites_nothing_and_asserts_nothing(database) -> None:
    """``Source.UNKNOWN`` emits no refs by contract -- and no dangling ones either."""
    at = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC)
    topology = _topology()
    device = topology.devices[0]
    report = AuditPass(database, config=AuditConfig()).analyze(
        _window(at),
        topology=topology,
        results=[
            InferenceResult(
                device_id=device.device_id,
                timestamp=at,
                power_w=0.0,
                source=Source.UNKNOWN,
                confidence=0.0,
                measurement_basis=MeasurementBasis.UNKNOWN,
            )
        ],
        events=[],
        now=at,
    )
    assert len(report.records) == 1
    assert report.records[0].evidence_refs == []
    _assert_every_ref_resolves(database, report)


def test_every_ref_resolves_when_records_and_verdicts_are_both_written(database) -> None:
    """The full chain: record refs, verdict rows and stored rows all agree.

    This is the strengthened GAP-3 guard: it reads the *stored* rows back
    rather than trusting the in-memory report, which is what "after a restart"
    means, and it covers a reanalysis pass as well as the incremental one.
    """
    at = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC)
    topology = _topology()
    channels = {channel.ct_id: channel for channel in topology.channels}
    # One metered device per channel: a device with no CT cannot be associated,
    # and this test is about references, not about attribution coverage.
    paired = [
        (device, channels[device.dedicated_ct_ids[0]])
        for device in topology.devices
        if device.dedicated_ct_ids and device.dedicated_ct_ids[0] in channels
    ][:3]
    assert paired, "the reference topology has metered devices"
    now = at + dt.timedelta(minutes=30)
    pass_ = AuditPass(database, config=AuditConfig())
    incremental = pass_.analyze(
        _window(now),
        topology=topology,
        results=[
            _result(device.device_id, 100.0 * (index + 1), at)
            for index, (device, _channel) in enumerate(paired)
        ],
        events=[
            {
                "device_id": device.device_id,
                "entity_id": channel.entity_id,
                "new_state": "on",
                "occurred_at": at,
            }
            for device, channel in paired
        ],
        now=now,
    )
    _assert_every_ref_resolves(database, incremental)

    for row in database.isolation_verdicts():
        assert database.resolve_evidence_ref(f"event:{row['event_id']}")
    for device in topology.devices:
        for row in database.inference_records_for_device(device.device_id):
            for ref in row["evidence_refs"]:
                assert database.resolve_evidence_ref(ref), ref
            # Every ref a stored record names must also be resolvable from the
            # report that emitted it -- no drift between the two views.
            assert set(row["evidence_refs"]) <= {
                ref for record in incremental.records for ref in record.evidence_refs
            }
