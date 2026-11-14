"""The audit pass must *use* the isolation layers it advertises.

Two requirements were only true of the shipped inference modules, never of the
production pass that calls them:

* "the isolation offset and window must be configurable; do not assume one fixed
  duration is optimal for every device" -- ``IsolationConfig`` resolves
  ``per-device > device-class > global``, but ``AuditConfig.isolation`` built a
  flat config with no ``defaults``/``per_device``, so every device got the
  global window;
* "events from multiple HA Entities belonging to the same physical device must
  not automatically be treated as multiple independent physical-device
  events" -- ``EventIsolator(..., bindings=...)`` collapses roles, but the pass
  constructed it without bindings, so a mode-only sibling of *another* device
  still read as a competing load change.

These tests are behavioural on purpose: they run the real ``AuditPass`` and
assert the window the verdict was actually decided in, and the verdict itself --
not merely that a settings dict parsed.  The stub tracker supplies CT history so
coverage is measurable and ``ISOLATED`` is reachable; without it every clean
event correctly lands on ``coverage_unknown``.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import pytest
from wattmosaic_engine.config.settings import (
    ISOLATION_TIMING_KEYS,
    EngineConfig,
)
from wattmosaic_engine.contracts import (
    CTChannel,
    CTType,
    DeviceConfiguration,
    DeviceConfigurationSet,
    DeviceTopology,
    DeviceType,
    HaEntityBinding,
    HaEntityRole,
    InferenceResult,
    IsolationStatus,
    MeasurementBasis,
    Source,
    Topology,
    TopologyStatus,
)
from wattmosaic_engine.disaggregation.audit import (
    DEFAULT_ALGORITHM_VERSION,
    AuditConfig,
    AuditPass,
    record_id_for,
)
from wattmosaic_engine.disaggregation.isolation import (
    REASON_CONCURRENT,
    REASON_INSUFFICIENT_SAMPLES,
    REASON_ISOLATED,
)
from wattmosaic_engine.learning.thresholds import (
    ISOLATION_TIMING_KEYS as THRESHOLD_TIMING_KEYS,
)
from wattmosaic_engine.storage.database import Database

BASE = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC)
#: Long enough that a ``BASE`` event has elapsed its window and is analysable.
NOW = BASE + dt.timedelta(minutes=5)


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #
@dataclass
class _Window:
    """The minimum a live pass reads: an instant and its per-CT readings."""

    at: dt.datetime
    readings: dict[str, float]


class _Tracker:
    """Stand-in for ``StepTracker``: fixed, explicit CT history."""

    def __init__(self, points: dict[str, list[tuple[dt.datetime, float]]]) -> None:
        self._points = points

    def history(self, ct_id: str) -> list[tuple[dt.datetime, float]]:
        return list(self._points.get(ct_id, ()))


def _tracker(start: dt.datetime = BASE, seconds: int = 240, step: int = 5) -> _Tracker:
    """Readings every ``step`` s on both channels, so coverage is measurable."""
    return _Tracker(
        {
            "ct_a": [
                (start + dt.timedelta(seconds=offset), 100.0 + offset)
                for offset in range(0, seconds, step)
            ],
            "ct_b": [
                (start + dt.timedelta(seconds=offset), 40.0)
                for offset in range(0, seconds, step)
            ],
        }
    )


def _topology() -> Topology:
    status = TopologyStatus.CONFIRMED
    return Topology(
        version=1,
        status=status,
        channels=[
            CTChannel(
                ct_id="ct_a",
                entity_id="sensor.a_power",
                ct_type=CTType.DEDICATED_DEVICE,
                device_id="a",
                topology_status=status,
            ),
            CTChannel(
                ct_id="ct_b",
                entity_id="sensor.b_power",
                ct_type=CTType.DEDICATED_DEVICE,
                device_id="b",
                topology_status=status,
            ),
        ],
        devices=[
            DeviceTopology(
                device_id="a", dedicated_ct_ids=["ct_a"], topology_status=status
            ),
            DeviceTopology(
                device_id="b", dedicated_ct_ids=["ct_b"], topology_status=status
            ),
        ],
    )


def _event(
    device_id: str,
    entity_id: str,
    at: dt.datetime,
    *,
    state: str = "on",
) -> dict[str, object]:
    return {
        "device_id": device_id,
        "entity_id": entity_id,
        "new_state": state,
        "occurred_at": at,
    }


def _result(device_id: str, power_w: float, at: dt.datetime) -> InferenceResult:
    return InferenceResult(
        device_id=device_id,
        timestamp=at,
        power_w=power_w,
        source=Source.DIRECT_MEASURED,
        confidence=0.8,
        measurement_basis=MeasurementBasis.CT_MEASURED,
    )


def _run(
    config: AuditConfig,
    events: list[dict[str, object]],
    *,
    database: Database | None = None,
    bindings: object = ...,
    now: dt.datetime = NOW,
) -> object:
    """Run one incremental pass and return its report."""
    kwargs: dict[str, object] = {"config": config, "tracker": _tracker()}
    if bindings is not ...:
        kwargs["bindings"] = bindings
    pass_ = AuditPass(database, **kwargs)  # type: ignore[arg-type]
    return pass_.analyze(
        _Window(at=now, readings={"ct_a": 120.0, "ct_b": 40.0}),
        topology=_topology(),
        results=[],
        events=events,
        now=now,
    )


def _verdict(report: object, device_id: str) -> object:
    matches = [row for row in report.isolations if row.device_id == device_id]  # type: ignore[attr-defined]
    assert len(matches) == 1, matches
    return matches[0]


def _shape(report: object) -> list[tuple[object, ...]]:
    return [
        (
            row.device_id,
            str(getattr(row.status, "value", row.status)),
            row.reason,
            row.isolation_window_s,
            row.window_start,
            row.window_end,
        )
        for row in sorted(
            report.isolations,  # type: ignore[attr-defined]
            key=lambda row: (row.device_id, row.occurred_at, row.event_id),
        )
    ]


@pytest.fixture()
def database(tmp_path):
    db = Database(tmp_path / "wiring.db")
    try:
        yield db
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# A device-class window changes the window *used at runtime*
# --------------------------------------------------------------------------- #
def test_a_device_class_override_widens_the_window_the_pass_decides_in() -> None:
    """A compressor gets 90 s; a lamp keeps 30 s -- proven by the verdict.

    The competing device switches 70 s after the subject.  Under the global
    window (30 s tight / 60 s wide) that competitor is outside both, so the
    event is clean; under the class window it is inside the tight window and the
    verdict is ``CONTAMINATED``.
    """
    events = [
        _event("a", "switch.a", BASE),
        _event("b", "switch.b", BASE + dt.timedelta(seconds=70)),
    ]

    global_verdict = _verdict(_run(AuditConfig(), events), "a")
    assert global_verdict.isolation_window_s == 30.0
    assert global_verdict.evidence["config_source"] == "global"
    assert global_verdict.status == IsolationStatus.ISOLATED
    assert global_verdict.reason == REASON_ISOLATED

    class_report = _run(
        AuditConfig(
            isolation_by_class={
                "hot_tub": {"isolation_window_s": 90.0, "power_window_s": 90.0}
            },
            isolation_device_classes={"a": ("hot_tub",)},
        ),
        events,
    )
    class_verdict = _verdict(class_report, "a")
    assert class_verdict.isolation_window_s == 90.0
    assert class_verdict.evidence["config_source"] == "class"
    assert class_verdict.status == IsolationStatus.CONTAMINATED
    assert class_verdict.reason == REASON_CONCURRENT
    assert class_verdict.window_start == BASE
    assert class_verdict.window_end == BASE + dt.timedelta(seconds=90)


def test_a_device_class_override_narrows_the_window_the_pass_decides_in() -> None:
    """A lamp's 5 s window must *not* see a sibling step 10 s later."""
    events = [
        _event("a", "switch.a", BASE),
        _event("b", "switch.b", BASE + dt.timedelta(seconds=10)),
    ]

    global_verdict = _verdict(_run(AuditConfig(), events), "a")
    assert global_verdict.isolation_window_s == 30.0
    assert global_verdict.status == IsolationStatus.CONTAMINATED

    narrow = _verdict(
        _run(
            AuditConfig(
                isolation_by_class={
                    "light": {"isolation_window_s": 5.0, "power_window_s": 5.0}
                },
                isolation_device_classes={"a": ("light",)},
            ),
            events,
        ),
        "a",
    )
    assert narrow.isolation_window_s == 5.0
    assert narrow.evidence["config_source"] == "class"
    # Not contaminated any more -- the sibling is outside the configured window.
    assert narrow.status == IsolationStatus.INSUFFICIENT_DATA
    assert narrow.reason == REASON_INSUFFICIENT_SAMPLES


@pytest.mark.parametrize(
    ("config", "expected_window", "expected_source"),
    [
        (AuditConfig(), 30.0, "global"),
        (
            AuditConfig(
                isolation_by_class={"hot_tub": {"isolation_window_s": 90.0}},
                isolation_device_classes={"a": ("hot_tub",)},
            ),
            90.0,
            "class",
        ),
        (
            AuditConfig(
                isolation_by_class={"hot_tub": {"isolation_window_s": 90.0}},
                isolation_device_classes={"a": ("hot_tub",)},
                isolation_by_device={"a": {"isolation_window_s": 45.0}},
            ),
            45.0,
            "device",
        ),
    ],
)
def test_precedence_device_beats_class_beats_global_through_the_pass(
    config: AuditConfig, expected_window: float, expected_source: str
) -> None:
    report = _run(config, [_event("a", "switch.a", BASE)])
    verdict = _verdict(report, "a")
    assert verdict.isolation_window_s == pytest.approx(expected_window)
    assert verdict.evidence["config_source"] == expected_source
    assert verdict.window_end == BASE + dt.timedelta(seconds=expected_window)


def test_a_per_device_override_beats_the_class_override_in_the_verdict() -> None:
    """The precedence is behavioural, not just a resolved number."""
    events = [
        _event("a", "switch.a", BASE),
        _event("b", "switch.b", BASE + dt.timedelta(seconds=70)),
    ]
    base = {"hot_tub": {"isolation_window_s": 90.0, "power_window_s": 90.0}}

    class_only = _verdict(
        _run(
            AuditConfig(
                isolation_by_class=base, isolation_device_classes={"a": ("hot_tub",)}
            ),
            events,
        ),
        "a",
    )
    assert class_only.status == IsolationStatus.CONTAMINATED

    device_wins = _verdict(
        _run(
            AuditConfig(
                isolation_by_class=base,
                isolation_device_classes={"a": ("hot_tub",)},
                isolation_by_device={
                    "a": {"isolation_window_s": 30.0, "power_window_s": 60.0}
                },
            ),
            events,
        ),
        "a",
    )
    assert device_wins.isolation_window_s == 30.0
    assert device_wins.evidence["config_source"] == "device"
    assert device_wins.status == IsolationStatus.ISOLATED


def test_a_per_device_window_is_what_coverage_was_measured_in() -> None:
    """Coverage is counted inside the same window the verdict cites.

    A per-device 60 s window holds 24 baseline and 24 event readings on two
    channels at a 5 s cadence.  Counting them under the global 30 s window would
    report 12 and 24, which is a different measurement of the same event.
    """
    config = AuditConfig(
        isolation_by_device={
            "a": {"isolation_window_s": 60.0, "power_window_s": 120.0}
        }
    )
    report = _run(config, [_event("a", "switch.a", BASE)])
    verdict = _verdict(report, "a")
    assert verdict.isolation_window_s == 60.0
    coverage = verdict.evidence["coverage"]
    assert coverage["baseline_samples"] == 24
    assert coverage["event_samples"] == 24
    assert coverage["sufficient"] is True


def test_a_wide_override_holds_its_event_until_its_own_window_elapsed() -> None:
    """The hold horizon follows the longest configured window, not the global one.

    With a 200 s class window, an event 100 s old is *not* mature.  Analysing it
    under the global 60 s horizon would reach a verdict about a window nobody
    observed and report ``window_not_elapsed`` -- a self-inflicted failure.
    """
    events = [_event("a", "switch.a", BASE)]
    early = BASE + dt.timedelta(seconds=100)
    config = AuditConfig(
        isolation_by_class={
            "hot_tub": {"isolation_window_s": 200.0, "power_window_s": 200.0}
        },
        isolation_device_classes={"a": ("hot_tub",)},
    )
    held = _run(config, events, now=early)
    assert held.counts["events"] == 0
    assert held.stages["pending_events"] == {"waiting": 1}

    global_held = _run(AuditConfig(), events, now=early)
    # The global horizon is 60 s, so the same event matures immediately.
    assert global_held.counts["events"] == 1
    assert _verdict(global_held, "a").reason == REASON_ISOLATED

    mature = _run(config, events, now=BASE + dt.timedelta(seconds=400))
    assert mature.counts["events"] == 1
    assert _verdict(mature, "a").isolation_window_s == 200.0


# --------------------------------------------------------------------------- #
# Entity bindings collapse siblings; unbound behaviour is unchanged
# --------------------------------------------------------------------------- #
def test_a_bound_mode_entity_of_another_device_is_not_a_competing_load() -> None:
    """The false contamination this wiring removes.

    ``b`` has two entities: a load switch and a mode/thermostat signal.  The mode
    signal changing is a real HA event but says nothing about watts.  Unbound it
    is conservatively load evidence (the documented rule), so it contaminates
    ``a``.  Bound ``MODE_ONLY`` it is not a physical load change at all.
    """
    events = [
        _event("a", "switch.a", BASE),
        _event("b", "switch.b_mode", BASE + dt.timedelta(seconds=5), state="heat"),
    ]

    unbound = _verdict(_run(AuditConfig(), events, bindings=[]), "a")
    assert unbound.status == IsolationStatus.CONTAMINATED
    assert unbound.reason == REASON_CONCURRENT

    bound = _verdict(
        _run(
            AuditConfig(),
            events,
            bindings=[
                HaEntityBinding(
                    entity_id="switch.b_mode", device_id="b", role=HaEntityRole.MODE_ONLY
                )
            ],
        ),
        "a",
    )
    assert bound.status == IsolationStatus.ISOLATED
    assert bound.reason == REASON_ISOLATED


def test_many_entities_of_one_appliance_are_one_competing_device() -> None:
    """Two entities on device ``b`` are one physical event, not two devices."""
    report = _run(
        AuditConfig(),
        [
            _event("a", "switch.a", BASE),
            _event("b", "switch.b1", BASE + dt.timedelta(seconds=5)),
            _event("b", "switch.b2", BASE + dt.timedelta(seconds=5)),
        ],
        bindings=[
            HaEntityBinding(
                entity_id="switch.b1", device_id="b", role=HaEntityRole.LOAD_STATE
            ),
            HaEntityBinding(
                entity_id="switch.b2", device_id="b", role=HaEntityRole.LOAD_STATE
            ),
        ],
    )
    verdict = _verdict(report, "a")
    assert verdict.status == IsolationStatus.CONTAMINATED
    assert verdict.competing_device_ids == ["b"]
    assert len(verdict.competing_event_ids) == 1, verdict.competing_event_ids
    assert verdict.evidence["competing_device_count"] == 1


def test_db_rows_collapse_a_sibling_entity_without_explicit_bindings(tmp_path) -> None:
    """The stored declaration is the source; the caller passes nothing."""
    events = [
        _event("a", "switch.a", BASE),
        _event("b", "switch.b_mode", BASE + dt.timedelta(seconds=5), state="heat"),
    ]
    bound_db = Database(tmp_path / "bound.db")
    try:
        bound_db.save_device_configurations(
            DeviceConfigurationSet(
                devices=[
                    DeviceConfiguration(
                        device_id="b",
                        entity_bindings=[
                            HaEntityBinding(
                                entity_id="switch.b_mode",
                                device_id="b",
                                role=HaEntityRole.MODE_ONLY,
                            )
                        ],
                    )
                ]
            )
        )
        # The projection migration 9 creates really holds the declaration.
        projected = bound_db._conn.execute(
            "SELECT entity_id, role FROM ha_entity_bindings ORDER BY entity_id"
        ).fetchall()
        assert [(row["entity_id"], row["role"]) for row in projected] == [
            ("switch.b_mode", "mode_only")
        ]

        report = _run(AuditConfig(), events, database=bound_db)
        verdict = _verdict(report, "a")
        assert verdict.status == IsolationStatus.ISOLATED
        assert report.stages["isolation"]["bindings"] == 1
    finally:
        bound_db.close()

    unbound_db = Database(tmp_path / "unbound.db")
    try:
        report = _run(AuditConfig(), events, database=unbound_db)
        verdict = _verdict(report, "a")
        assert verdict.status == IsolationStatus.CONTAMINATED
        assert report.stages["isolation"]["bindings"] == 0
    finally:
        unbound_db.close()


def test_an_empty_bindings_table_keeps_the_conservative_behaviour(tmp_path) -> None:
    """No declaration means "every entity is a possible load change" -- exactly."""
    events = [
        _event("a", "switch.a", BASE),
        _event("b", "switch.b_mode", BASE + dt.timedelta(seconds=5), state="heat"),
    ]
    explicit_empty = _run(AuditConfig(), events, bindings=[])
    db = Database(tmp_path / "empty.db")
    try:
        from_db = _run(AuditConfig(), events, database=db)
        assert from_db.stages["isolation"]["bindings"] == 0
    finally:
        db.close()

    assert _shape(from_db) == _shape(explicit_empty)
    # And explicitly what that conservative reading is.
    verdict = _verdict(from_db, "a")
    assert verdict.status == IsolationStatus.CONTAMINATED
    assert verdict.reason == REASON_CONCURRENT


# --------------------------------------------------------------------------- #
# Device classes come from the stored declaration too
# --------------------------------------------------------------------------- #
def test_a_stored_device_type_selects_the_class_layer(tmp_path) -> None:
    db = Database(tmp_path / "typed.db")
    try:
        db.save_device_configurations(
            DeviceConfigurationSet(
                devices=[
                    DeviceConfiguration(
                        device_id="a", device_type=DeviceType.HOT_TUB
                    )
                ]
            )
        )
        report = _run(
            AuditConfig(isolation_by_class={"hot_tub": {"isolation_window_s": 120.0}}),
            [_event("a", "switch.a", BASE)],
            database=db,
        )
        verdict = _verdict(report, "a")
        assert verdict.isolation_window_s == 120.0
        assert verdict.evidence["config_source"] == "class"
        assert "hot_tub" in verdict.evidence["device_class"]
    finally:
        db.close()


def test_settings_device_classes_and_db_device_types_both_apply(tmp_path) -> None:
    """An operator label and a stored type are two sources for the same layer."""
    db = Database(tmp_path / "both.db")
    try:
        db.save_device_configurations(
            DeviceConfigurationSet(
                devices=[
                    DeviceConfiguration(
                        device_id="a", device_type=DeviceType.HOT_TUB
                    )
                ]
            )
        )
        report = _run(
            AuditConfig(
                isolation_by_class={
                    "hot_tub": {"isolation_window_s": 120.0},
                    "spa": {"isolation_window_s": 240.0},
                },
                isolation_device_classes={"a": ("spa",)},
            ),
            [_event("a", "switch.a", BASE)],
            database=db,
        )
        verdict = _verdict(report, "a")
        # The operator's label is looked up first.
        assert verdict.isolation_window_s == 240.0
        assert verdict.evidence["device_class"][0] == "spa"
        assert "hot_tub" in verdict.evidence["device_class"]
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Settings that make the layers configurable
# --------------------------------------------------------------------------- #
def test_isolation_layer_settings_have_conservative_defaults() -> None:
    config = EngineConfig(ha_token="test")
    assert config.isolation_by_class == {}
    assert config.isolation_by_device == {}
    assert config.isolation_device_classes == {}
    assert config.audit_algorithm_version == DEFAULT_ALGORITHM_VERSION
    assert config.validate() == []
    # One spelling of the timing keys, so a config typo cannot mean two things.
    assert set(ISOLATION_TIMING_KEYS) == set(THRESHOLD_TIMING_KEYS)


def test_isolation_layer_settings_are_readable_from_the_environment() -> None:
    config = EngineConfig.from_env(
        {
            "WM_ISOLATION_BY_CLASS": '{"hot_tub": {"isolation_window_s": 90}}',
            "WM_ISOLATION_BY_DEVICE": '{"spa": {"analysis_offset_s": 12.5}}',
            "WM_ISOLATION_DEVICE_CLASSES": '{"spa": ["hot_tub", "complex"]}',
            "WM_AUDIT_ALGORITHM_VERSION": "disagg@7.7",
            "WM_HA_TOKEN": "test",
        },
        dotenv_path="/nonexistent/.env",
    )
    assert config.isolation_by_class == {"hot_tub": {"isolation_window_s": 90}}
    assert config.isolation_by_device == {"spa": {"analysis_offset_s": 12.5}}
    assert config.isolation_device_classes == {"spa": ("hot_tub", "complex")}
    assert config.audit_algorithm_version == "disagg@7.7"
    assert config.validate() == []


def test_isolation_layer_settings_are_redacted_and_validated() -> None:
    redacted = EngineConfig().redacted()
    for key in (
        "isolation_by_class",
        "isolation_by_device",
        "isolation_device_classes",
        "audit_algorithm_version",
    ):
        assert key in redacted, f"{key} missing from redacted()"

    assert any(
        "isolation_by_class" in problem
        for problem in EngineConfig(
            isolation_by_class={"hot_tub": {"isolation_window_s": -5.0}}
        ).validate()
    )
    # A key that is not a timing is a typo, not something to silently ignore.
    assert any(
        "isolation_by_device" in problem
        for problem in EngineConfig(
            isolation_by_device={"spa": {"window_s": 30.0}}
        ).validate()
    )
    assert any(
        "audit_algorithm_version" in problem
        for problem in EngineConfig(audit_algorithm_version="   ").validate()
    )
    # A malformed JSON payload degrades to the documented default, it does not
    # produce a half-parsed window.
    broken = EngineConfig.from_env(
        {"WM_ISOLATION_BY_CLASS": "not json", "WM_HA_TOKEN": "test"},
        dotenv_path="/nonexistent/.env",
    )
    assert broken.isolation_by_class == {}
    assert broken.validate() == []


def test_a_bad_env_override_is_reported_not_silently_dropped() -> None:
    """A value the runtime cannot honour must be a config error, not a default.

    ``IsolationWindow.merged`` drops an unusable number so a typo cannot create
    a window that never matches; that is the right runtime behaviour but a
    terrible way to learn about a typo, so ``validate()`` names it.
    """
    config = EngineConfig.from_env(
        {
            "WM_HA_TOKEN": "test",
            "WM_ISOLATION_BY_CLASS": (
                '{"hot_tub": {"isolation_window_s": 90},'
                ' "light": {"isolation_windw_s": 4, "power_window_s": -1}}'
            ),
        },
        dotenv_path="/nonexistent/.env",
    )
    # Parsed and inspectable rather than discarded.
    assert config.isolation_by_class["hot_tub"] == {"isolation_window_s": 90}
    problems = config.validate()
    assert any(
        "isolation_by_class['light']" in problem and "isolation_windw_s" in problem
        for problem in problems
    ), problems
    assert any(
        "isolation_by_class['light'].power_window_s" in problem for problem in problems
    ), problems
    # And the usable override still reaches the pass.
    assert AuditConfig.from_settings(config).isolation.resolve("x", ("hot_tub",))[
        0
    ].isolation_window_s == 90.0


def test_a_non_numeric_override_does_not_stop_the_pass_being_built() -> None:
    """A typo is reported by ``validate()``; it must not raise at construction."""
    config = EngineConfig(
        ha_token="test", isolation_by_class={"hot_tub": {"isolation_window_s": "wide"}}
    )
    assert any("isolation_by_class" in problem for problem in config.validate())
    pass_config = AuditConfig.from_settings(config)
    window, source = pass_config.isolation.resolve("x", ("hot_tub",))
    # The unusable entry is dropped entirely; the documented global default answers.
    assert source == "global"
    assert window.isolation_window_s == 30.0


# --------------------------------------------------------------------------- #
# GAP-2: the audit algorithm version is a real, reproducible setting
# --------------------------------------------------------------------------- #
def test_the_algorithm_version_setting_reaches_the_emitted_record(tmp_path) -> None:
    db = Database(tmp_path / "version.db")
    try:
        configured = EngineConfig(
            ha_token="test", audit_algorithm_version="disagg@7.7"
        )
        assert configured.validate() == []
        pass_ = AuditPass(db, config=AuditConfig.from_settings(configured))
        report = pass_.analyze(
            _Window(at=NOW, readings={"ct_a": 120.0}),
            topology=_topology(),
            results=[_result("a", 42.0, NOW)],
            events=[],
            now=NOW,
        )
        assert [record.algorithm_version for record in report.records] == ["disagg@7.7"]
        assert report.records[0].record_id == record_id_for("a", "disagg@7.7", NOW, 1)
        stored = db.inference_records_for_device("a")
        assert [row["algorithm_version"] for row in stored] == ["disagg@7.7"]
        assert [row["record_id"] for row in stored] == [report.records[0].record_id]

        # A change to the setting is visible in the record identity as well as
        # its version field, which is what makes a replay comparable.
        bumped = AuditPass(
            db,
            config=AuditConfig.from_settings(
                EngineConfig(ha_token="test", audit_algorithm_version="disagg@7.8")
            ),
        )
        later = NOW + dt.timedelta(hours=2)
        changed = bumped.analyze(
            _Window(at=later, readings={"ct_a": 120.0}),
            topology=_topology(),
            results=[_result("a", 900.0, later)],
            events=[],
            now=later,
        )
        assert [record.algorithm_version for record in changed.records] == ["disagg@7.8"]
        assert changed.records[0].record_id != report.records[0].record_id
    finally:
        db.close()


def test_the_default_algorithm_version_is_what_an_unconfigured_engine_uses() -> None:
    config = AuditConfig.from_settings(EngineConfig())
    assert config.algorithm_version == DEFAULT_ALGORITHM_VERSION
    # And the pass reports it, so a replay can be compared against it.
    report = _run(config, [])
    assert report.stages["isolation"]["algorithm_version"] == DEFAULT_ALGORITHM_VERSION
