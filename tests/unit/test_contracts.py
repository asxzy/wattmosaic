"""Contract and storage tests (orchestrator-owned).

These guard the interfaces every other module depends on.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import get_args

import pytest
from pydantic import ValidationError
from wattmosaic_engine import contracts
from wattmosaic_engine.contracts import (
    ChangeStatus,
    Circuit,
    CircuitClass,
    ConfidenceDimension,
    ConfidenceVector,
    CTChannel,
    CTType,
    DeviceConfiguration,
    DeviceEvent,
    DeviceReaderMappingCell,
    DeviceTopology,
    EdgeLifecycle,
    EventIsolation,
    HaEntityBinding,
    HaEntityRole,
    InferenceResult,
    IsolationStatus,
    MappingCellStatus,
    Measurement,
    MeasurementBasis,
    Phase,
    Polarity,
    Reconciliation,
    RelationKind,
    ResidualKind,
    ResidualPower,
    ServiceCommand,
    Source,
    Topology,
    TopologyRelation,
    TopologyStatus,
)
from wattmosaic_engine.storage import LATEST_VERSION, Database
from wattmosaic_engine.storage import migrations as migrations_module
from wattmosaic_engine.storage.migrations import (
    MIGRATIONS,
    apply_migrations,
    current_version,
)

ROOT = Path(__file__).resolve().parents[2]


def _export_schemas_module():
    """``tools/dev/export_schemas.py`` as a module, for its ``MODELS`` map."""
    path = ROOT / "tools" / "dev" / "export_schemas.py"
    spec = importlib.util.spec_from_file_location("wm_export_schemas_contracts", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_contract_version_is_2_4_0() -> None:
    """The canonical pin: the authoritative version is the one this task bumped.

    Other version assertions compare against this exported constant so they
    stay correct across future additive bumps; the pin lives here.
    """
    assert contracts.CONTRACT_VERSION == "2.4.0"


def test_schema_files_match_contract_version() -> None:
    index = json.loads((ROOT / "data" / "schemas" / "index.json").read_text())
    assert index["contract_version"] == contracts.CONTRACT_VERSION
    assert "inference_result" in index["schemas"]


def test_the_interface_contract_doc_states_the_current_version() -> None:
    """AC-10: the doc's own mandatory protocol is bump, regenerate, update.

    ``docs/interface_contracts.md`` §5 tells a maintainer to bump
    ``CONTRACT_VERSION``, regenerate ``data/schemas/`` and *update this
    document*.  Step 3 is the one nobody notices missing: the doc is prose, so
    a stale version line still reads as authoritative.  This pins the stated
    version against the source of truth.
    """
    doc = (ROOT / "docs" / "interface_contracts.md").read_text(encoding="utf-8")
    stated = re.findall(r"Contract version:\s*`([^`]+)`", doc)
    assert stated == [contracts.CONTRACT_VERSION], (
        f"interface_contracts.md states {stated}, contracts.py says "
        f"{contracts.CONTRACT_VERSION!r}"
    )


def test_the_committed_schemas_are_what_the_contract_generates() -> None:
    """AC-9, at pytest level: the committed schemas are not stale.

    CI runs ``export_schemas.py`` then ``git diff --exit-code data/schemas``,
    and ``verify.py`` has the same gate.  Both only work in a git checkout with
    the tool importable, so this restates the comparison in-process: regenerate
    each model's schema exactly as the tool would and require the committed
    file to match byte-for-byte.  A contract edit that forgets the regeneration
    fails here rather than at review time.
    """
    schemas = ROOT / "data" / "schemas"
    index = json.loads((schemas / "index.json").read_text())
    # The mapping is read from the exporter itself, so this test and the gate
    # can never disagree about which model produces which file.
    exporter = _export_schemas_module()
    checked = 0
    # ``collect_models`` is the exporter's single source of truth: it merges the
    # legacy ``MODELS`` set with each v3 submodule's ``SCHEMA_EXPORTS``.  New
    # additive contracts (K00..K06) land in the latter, not in ``MODELS``.
    all_models = exporter.collect_models()
    for name, filename in index["schemas"].items():
        model = all_models[name]
        expected = model.model_json_schema(mode="serialization")
        expected["$id"] = f"https://wattmosaic.local/schemas/{name}.json"
        expected["x-contract-version"] = contracts.CONTRACT_VERSION
        committed = json.loads((schemas / filename).read_text())
        assert committed == expected, f"data/schemas/{filename} is stale"
        checked += 1
    assert checked == len(index["schemas"]), "not every indexed schema was checked"


def test_measurement_kw_conversion_and_polarity() -> None:
    m = Measurement(
        entity_id="sensor.x",
        ct_id="c",
        value=1.5,
        unit="kW",
        state_timestamp=datetime.now(UTC),
    )
    assert m.effective_power_w() == pytest.approx(1500.0)


def test_measurement_negative_power_preserved() -> None:
    m = Measurement(
        entity_id="sensor.x",
        ct_id="c",
        value=-420.0,
        state_timestamp=datetime.now(UTC),
    )
    assert m.effective_power_w() == -420.0


def test_inverted_polarity_flips_sign() -> None:
    m = Measurement(
        entity_id="sensor.x",
        ct_id="c",
        value=420.0,
        polarization=Polarity.INVERTED,
        state_timestamp=datetime.now(UTC),
    )
    assert m.effective_power_w() == -420.0


def test_measurement_rejects_non_finite() -> None:
    with pytest.raises(ValueError):
        Measurement(
            entity_id="sensor.x",
            ct_id="c",
            value=float("inf"),
            state_timestamp=datetime.now(UTC),
        )


#: The contract's unit whitelist (spec 03 §1.1).  ``Measurement.unit`` is a
#: ``Literal``, so this list and the annotation must agree; the test below reads
#: the annotation rather than restating it, which is what makes the pair a guard.
MEASUREMENT_UNITS: tuple[str, ...] = ("W", "kW", "kWh", "Wh")

#: Models that carry an electrical observation.  ``CTChannel.nominal_voltage`` is
#: the one voltage-named field in the contract and is a declared constant, not a
#: measurement (spec 03 §4.2); see ``DECLARED_CONSTANT_FIELDS``.
MEASUREMENT_SHAPED_MODELS: tuple[type, ...] = (
    Measurement,
    CTChannel,
    DeviceTopology,
    InferenceResult,
    Reconciliation,
    ResidualPower,
)

_UNMEASURABLE_FIELD = re.compile(
    r"(?:^|_)(?:"
    r"voltage|volt|volts|v|"
    r"current|current_a|amp|amps|ampere|amperes|ampere_rms|rms_current|a|"
    r"power_factor|pf|"
    r"reactive|reactive_power|reactive_var|var|vars|"
    r"apparent|apparent_power|apparent_va|va|vas|"
    r"frequency|frequency_hz|freq|hz"
    r")(?:$|_)",
    re.IGNORECASE,
)


def _measurement(unit: str) -> Measurement:
    return Measurement(
        entity_id="sensor.x",
        ct_id="c",
        value=10.0,
        unit=unit,  # type: ignore[arg-type]
        state_timestamp=datetime.now(UTC),
    )


def test_measurement_unit_whitelist() -> None:
    """Only W/kW/kWh/Wh are accepted -- no V, A, PF, VAR, VA or Hz.

    The annotation is read from the model, not restated here: if anyone widens
    the ``Literal`` (say, to admit a computed amperage) this fails at the
    whitelist assertion rather than silently following the code.
    """
    annotation = Measurement.model_fields["unit"].annotation
    assert get_args(annotation) == MEASUREMENT_UNITS

    for unit in MEASUREMENT_UNITS:
        assert _measurement(unit).unit == unit

    for unit in ("V", "A", "var", "Hz"):
        with pytest.raises(ValidationError):
            _measurement(unit)


#: The single voltage-named contract field.  Spec 03 §4.2/§9 establishes it as a
#: *declared constant* on a current-only channel, never a measurement of an
#: instant, so it is explicitly not an unmeasurable-quantity field: the rule is
#: that such quantities have **no** representation, and this one has exactly one
#: and it is a rating, not an observation.
DECLARED_CONSTANT_FIELDS: frozenset[str] = frozenset({"nominal_voltage"})


@pytest.mark.parametrize("model", MEASUREMENT_SHAPED_MODELS, ids=lambda m: m.__name__)
def test_no_unmeasurable_quantity_field(model: type) -> None:
    """No voltage/current/PF/reactive/apparent/frequency field may appear.

    Reflection over ``model_fields`` is the point: adding such a field later
    fails here instead of reaching the wire unnoticed (spec 03 §4.3).
    """
    offenders = sorted(
        name
        for name in model.model_fields
        if _UNMEASURABLE_FIELD.search(name) and name not in DECLARED_CONSTANT_FIELDS
    )
    assert offenders == [], f"{model.__name__} exposes unmeasurable fields: {offenders}"
    # The allowlist itself must stay honest: a declared constant is only ever a
    # declared constant, and any widening has to be justified in the spec first.
    assert set(CTChannel.model_fields) >= DECLARED_CONSTANT_FIELDS
    assert set(DECLARED_CONSTANT_FIELDS) == {"nominal_voltage"}


def test_phase_is_declared_per_reader_and_defaults_to_unknown() -> None:
    """Phase is a declaration on the reader, never inferred from a reading.

    ``CTChannel.phase`` defaults to ``UNKNOWN`` and no validator fills it from
    a sample; the coefficient table consumes it as declared.
    """
    default_channel = CTChannel(ct_id="c", entity_id="sensor.c")
    assert Phase(default_channel.phase) is Phase.UNKNOWN
    assert Phase(DeviceTopology(device_id="d").phase) is Phase.UNKNOWN

    declared = CTChannel(ct_id="c", entity_id="sensor.c", phase=Phase.L1)
    assert declared.phase == Phase.L1.value
    assert set(Phase) == {
        Phase.L1,
        Phase.L2,
        Phase.BOTH,
        Phase.UNKNOWN,
    }


def test_measurement_polarity_applied_once_and_sign_preserved() -> None:
    """The polarity sign is applied exactly once and never clamped.

    * an inverted reader's negative raw reading is negated once, so a reversed
      clamp reports its true positive magnitude rather than a second flip;
    * applying ``effective_power_w`` again does not flip the sign back, because
      the result is a plain number and the correction lives on the model;
    * a ``NORMAL``-polarity negative reading stays negative -- reverse flow and
      a mis-declared polarity are observations, not zero.
    """
    inverted = Measurement(
        entity_id="sensor.x",
        ct_id="c",
        value=-350.0,
        polarization=Polarity.INVERTED,
        state_timestamp=datetime.now(UTC),
    )
    assert inverted.effective_power_w() == 350.0
    # A second call is idempotent: the correction is one multiplication, so the
    # sign never flips back on repeated evaluation.
    assert inverted.effective_power_w() == inverted.effective_power_w()
    # The inverted declaration applied to a positive reading gives a negative
    # effective value -- no clamp-to-zero path can produce that.
    assert (
        Measurement(
            entity_id="sensor.x",
            ct_id="c",
            value=350.0,
            polarization=Polarity.INVERTED,
            state_timestamp=datetime.now(UTC),
        ).effective_power_w()
        == -350.0
    )

    normal_negative = Measurement(
        entity_id="sensor.x",
        ct_id="c",
        value=-420.0,
        state_timestamp=datetime.now(UTC),
    )
    assert normal_negative.effective_power_w() == -420.0


def test_confidence_bounds() -> None:
    with pytest.raises(ValueError):
        InferenceResult(
            device_id="d",
            timestamp=datetime.now(UTC),
            power_w=1.0,
            source=Source.INFERRED,
            confidence=1.5,
        )


def test_confidence_defaults_uncalibrated() -> None:
    r = InferenceResult(
        device_id="d",
        timestamp=datetime.now(UTC),
        power_w=10.0,
        source=Source.INFERRED,
        confidence=0.5,
    )
    assert r.confidence_is_calibrated is False


def test_contracts_forbid_unknown_fields() -> None:
    with pytest.raises(ValueError):
        ServiceCommand(command="start_learning", bogus_field=1)  # type: ignore[call-arg]


def test_service_command_enum_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        ServiceCommand(command="delete_everything")  # type: ignore[arg-type]


def test_topology_helpers() -> None:
    t = Topology(
        channels=[CTChannel(ct_id="a", entity_id="sensor.a", ct_type=CTType.MAIN, phase=Phase.L1)],
        devices=[DeviceTopology(device_id="d1", branch_ct_ids=["a"])],
    )
    assert t.channel_map()["a"].entity_id == "sensor.a"
    assert t.device_map()["d1"].device_id == "d1"


def test_reconciliation_keeps_residual_separate() -> None:
    r = Reconciliation(
        main_power_w=1000.0,
        direct_measured_w=300.0,
        inferred_w=200.0,
        unknown_residual_w=500.0,
    )
    assert r.unknown_residual_w == 500.0
    assert r.direct_measured_w + r.inferred_w + r.unknown_residual_w == r.main_power_w


# --------------------------- storage ------------------------------------- #


def test_migrations_idempotent(tmp_path: Path) -> None:
    db = Database(tmp_path / "a.db")
    try:
        assert db.schema_version == LATEST_VERSION
        assert apply_migrations(db._conn) == LATEST_VERSION
        assert current_version(db._conn) == LATEST_VERSION
    finally:
        db.close()


def test_migration_versions_are_sequential() -> None:
    """Released versions are contiguous; later ones may have gaps.

    Wave-2 migrations land in parallel and out of order, and 21-24 are spare, so
    the old ``list(range(1, N + 1))`` rule breaks as soon as any one of them is
    added.  The real invariant is: 1-11 exist and are contiguous, 12 is never a
    migration (it is the payload-encoding marker), and every later version is
    unique and at most 25.
    """
    versions = [m.version for m in MIGRATIONS]
    assert versions == sorted(versions)
    assert versions[:11] == list(range(1, 12))
    assert len(versions) == len(set(versions)), f"duplicate version: {versions}"
    assert max(versions) == migrations_module.LATEST_VERSION
    reserved = set(migrations_module.RESERVED_VERSIONS)
    spare = set(migrations_module.SPARE_VERSIONS)
    assert reserved == {12}
    assert spare == set(range(22, 25))  # 21 allocated to D5 (evidence supersedes)
    assert 12 not in versions
    assert all(13 <= version <= 25 for version in versions if version > 11)
    assert not reserved & set(versions)
    assert not spare & set(versions)


def test_wal_enabled(tmp_path: Path) -> None:
    db = Database(tmp_path / "w.db")
    try:
        mode = db._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
    finally:
        db.close()


def _topology() -> Topology:
    return Topology(
        status=TopologyStatus.CANDIDATE,
        channels=[
            CTChannel(ct_id="main_l1", entity_id="sensor.main_l1", ct_type=CTType.MAIN, phase=Phase.L1),
            CTChannel(ct_id="br1", entity_id="sensor.br1", ct_type=CTType.BRANCH, phase=Phase.L1,
                      parent_ct_id="main_l1"),
            CTChannel(
                ct_id="panel_l1", entity_id="sensor.panel_l1", ct_type=CTType.MAIN, phase=Phase.L1,
                alias_of_ct_id="main_l1", measurement_multiplier=2.5,
            ),
        ],
        devices=[DeviceTopology(device_id="dev1", branch_ct_ids=["br1"])],
    )


def test_topology_roundtrip_preserves_alias_and_multiplier(tmp_path: Path) -> None:
    """Storage must not silently drop declaration fields it has no column for.

    ``alias_of_ct_id`` and ``measurement_multiplier`` existed on the contract
    but had no ``ct_channels`` column, so ``save_topology`` wrote an explicit
    column list without them and ``load_topology`` rebuilt every channel with
    the defaults.  A user's alias declaration therefore never reached the
    compute path: the whole-home total kept summing a wire measured twice.
    """
    db = Database(tmp_path / "t.db")
    try:
        db.save_topology(_topology())
        loaded = db.load_topology()
        assert loaded is not None
        by_id = loaded.channel_map()
        assert by_id["panel_l1"].alias_of_ct_id == "main_l1"
        assert by_id["panel_l1"].measurement_multiplier == 2.5
        # Undeclared channels keep the neutral default, not a stray value.
        assert by_id["main_l1"].alias_of_ct_id is None
        assert by_id["main_l1"].measurement_multiplier == 1.0
    finally:
        db.close()


def test_saved_alias_removes_the_duplicate_feed(tmp_path: Path) -> None:
    """The persisted declaration must change the computed whole-home total."""
    from wattmosaic_engine.electrical.topology import build_graph

    db = Database(tmp_path / "t.db")
    try:
        db.save_topology(_topology())
        loaded = db.load_topology()
        assert loaded is not None
        graph = build_graph(loaded)
        # ``panel_l1`` is the alias of ``main_l1``: only the canonical wire feeds
        # the total.  Without the fix both mains would appear here.
        assert graph.effective_feeds() == ["main_l1"]
    finally:
        db.close()


def test_save_topology_accepts_child_before_parent(tmp_path: Path) -> None:
    """A nested channel may be listed before its parent.

    ``ct_channels.parent_ct_id`` is a self-referencing foreign key and
    ``PRAGMA foreign_keys=ON`` is set.  Inserting in payload order therefore
    aborted the whole write with ``FOREIGN KEY constraint failed`` whenever a
    sub-panel feed came before the service feed -- which is exactly the order
    the wizard sends channels in.  Nesting could never be saved at all.
    """
    db = Database(tmp_path / "t.db")
    try:
        child_first = Topology(
            status=TopologyStatus.CANDIDATE,
            channels=[
                CTChannel(
                    ct_id="sub_l1", entity_id="sensor.sub_l1", ct_type=CTType.MAIN,
                    phase=Phase.L1, parent_ct_id="service_l1",
                ),
                CTChannel(
                    ct_id="service_l1", entity_id="sensor.service_l1",
                    ct_type=CTType.MAIN, phase=Phase.L1,
                ),
            ],
        )
        db.save_topology(child_first)
        loaded = db.load_topology()
        assert loaded is not None
        assert loaded.channel_map()["sub_l1"].parent_ct_id == "service_l1"
    finally:
        db.close()


def test_save_topology_rejects_a_parent_cycle_without_corrupting_state(tmp_path: Path) -> None:
    """An impossible cycle must fail atomically, leaving the old topology intact.

    ``ct_channels.parent_ct_id`` is a self-referencing foreign key, so a cycle
    cannot be stored.  ``TopologyGraph`` treats it as a problem rather than a
    relationship, and the config wizard already refuses to submit one; what
    matters at the storage boundary is that the rejected write rolls back
    instead of leaving the installation with no topology at all.
    """
    db = Database(tmp_path / "t.db")
    try:
        db.save_topology(_topology())
        before = db.load_topology()
        assert before is not None

        cyclic = Topology(
            status=TopologyStatus.CANDIDATE,
            channels=[
                CTChannel(
                    ct_id="a", entity_id="sensor.a", ct_type=CTType.MAIN,
                    phase=Phase.L1, parent_ct_id="b",
                ),
                CTChannel(
                    ct_id="b", entity_id="sensor.b", ct_type=CTType.MAIN,
                    phase=Phase.L1, parent_ct_id="a",
                ),
            ],
        )
        with pytest.raises(sqlite3.IntegrityError):
            db.save_topology(cyclic)

        after = db.load_topology()
        assert after is not None
        assert {c.ct_id for c in after.channels} == {
            c.ct_id for c in before.channels
        }
    finally:
        db.close()


def test_topology_roundtrip(tmp_path: Path) -> None:
    db = Database(tmp_path / "t.db")
    try:
        version = db.save_topology(_topology())
        assert version >= 1
        loaded = db.load_topology()
        assert loaded is not None
        assert {c.ct_id for c in loaded.channels} == {"main_l1", "br1", "panel_l1"}
        assert loaded.device_map()["dev1"].branch_ct_ids == ["br1"]
    finally:
        db.close()


def test_ct_sample_insert_and_latest(tmp_path: Path) -> None:
    db = Database(tmp_path / "s.db")
    now = datetime.now(UTC)
    try:
        n = db.insert_ct_samples(
            [
                {
                    "ct_id": "main_l1",
                    "entity_id": "sensor.main_l1",
                    "value_w": 100.0,
                    "raw_value": 100.0,
                    "unit": "W",
                    "state_timestamp": now,
                    "received_at": now,
                    "quality": "good",
                    "repeated": False,
                    "sequence": 1,
                },
                {
                    "ct_id": "main_l1",
                    "entity_id": "sensor.main_l1",
                    "value_w": 120.0,
                    "raw_value": 120.0,
                    "unit": "W",
                    "state_timestamp": now + timedelta(seconds=15),
                    "received_at": now + timedelta(seconds=15),
                    "quality": "good",
                    "repeated": False,
                    "sequence": 2,
                },
            ]
        )
        assert n == 2
        latest = db.latest_ct_sample("main_l1")
        assert latest is not None
        assert latest["value_w"] == pytest.approx(120.0)
    finally:
        db.close()


def test_inference_results_roundtrip_and_provenance(tmp_path: Path) -> None:
    db = Database(tmp_path / "i.db")
    now = datetime.now(UTC)
    try:
        db.insert_inference_results(
            [
                InferenceResult(
                    device_id="dev1",
                    timestamp=now,
                    power_w=42.0,
                    source=Source.INFERRED,
                    confidence=0.7,
                    supporting_ct_channels=["br1"],
                    explanation=["branch_power_increase"],
                    provenance={"window_s": 30},
                )
            ]
        )
        rows = db.latest_inference_results()
        assert len(rows) == 1
        assert json.loads(rows[0]["explanation_json"]) == ["branch_power_increase"]
        assert json.loads(rows[0]["provenance_json"])["window_s"] == 30
    finally:
        db.close()


def test_learning_sample_rejection_reason_persisted(tmp_path: Path) -> None:
    db = Database(tmp_path / "l.db")
    try:
        from wattmosaic_engine.contracts import LearningSample, SampleRejection

        sample = LearningSample(
            sample_id="s1",
            device_id="dev1",
            captured_at=datetime.now(UTC),
            delta_power_w=10.0,
            accepted=False,
            rejection_reason=SampleRejection.STALE_MEASUREMENT,
        )
        db.insert_learning_sample(sample)
        all_rows = db.learning_samples("dev1", accepted_only=False)
        assert all_rows[0]["rejection_reason"] == "stale_measurement"
        assert db.count_learning_samples("dev1", accepted_only=True) == 0
    finally:
        db.close()


def test_model_promotion_and_rollback(tmp_path: Path) -> None:
    db = Database(tmp_path / "m.db")
    try:
        from wattmosaic_engine.contracts import DeviceModelVersion, ModelStatus, ModelType

        for version, r2 in ((1, 0.8), (2, 0.9)):
            db.save_model_version(
                DeviceModelVersion(
                    device_id="dev1",
                    model_type=ModelType.BINARY,
                    version=version,
                    status=ModelStatus.CANDIDATE,
                    validation_metrics={"r2": r2},
                ),
                activate=True,
            )
        active = db.active_model("dev1", "binary")
        assert active is not None and active["version"] == 2
        previous = db.rollback_model("dev1", "binary")
        assert previous == 1
        active = db.active_model("dev1", "binary")
        assert active is not None and active["version"] == 1
    finally:
        db.close()


def test_freezing_model(tmp_path: Path) -> None:
    db = Database(tmp_path / "f.db")
    try:
        db.set_model_frozen("dev1", True)
        assert db.is_model_frozen("dev1") is True
        db.set_model_frozen("dev1", False)
        assert db.is_model_frozen("dev1") is False
    finally:
        db.close()


def test_prune_removes_old_rows_only(tmp_path: Path) -> None:
    db = Database(tmp_path / "p.db")
    old = datetime.now(UTC) - timedelta(days=400)
    new = datetime.now(UTC)
    try:
        db.insert_ct_samples(
            [
                {
                    "ct_id": "c", "entity_id": "sensor.c", "value_w": 1.0, "raw_value": 1.0,
                    "unit": "W", "state_timestamp": old, "received_at": old,
                },
                {
                    "ct_id": "c", "entity_id": "sensor.c", "value_w": 2.0, "raw_value": 2.0,
                    "unit": "W", "state_timestamp": new, "received_at": new,
                },
            ]
        )
        removed = db.prune(raw_days=30, aggregate_days=730)
        assert removed["ct_samples"] == 1
        assert len(db.ct_samples_between(new - timedelta(days=1), new + timedelta(days=1))) == 1
    finally:
        db.close()


def test_corrupt_database_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "corrupt.db"
    path.write_bytes(b"not a sqlite database at all" * 10)
    # sqlite3 raises DatabaseError ("file is not a database"); the storage layer
    # must not swallow it, because silently starting on a corrupt file would
    # destroy historical data.
    with pytest.raises(sqlite3.DatabaseError):
        Database(path)


def test_export_device_data_shape(tmp_path: Path) -> None:
    db = Database(tmp_path / "e.db")
    try:
        out = db.export_device_data("nope")
        assert out["device_id"] == "nope"
        assert out["inference_results"] == []
        assert "exported_at" in out
    finally:
        db.close()


def test_integrity_check_reports_ok(tmp_path: Path) -> None:
    db = Database(tmp_path / "ic.db")
    try:
        report = db.integrity_check()
        assert report["integrity"] == "ok"
        assert report["schema_version"] == LATEST_VERSION
    finally:
        db.close()


def test_no_secret_like_columns_in_schema(tmp_path: Path) -> None:
    db = Database(tmp_path / "sec.db")
    try:
        rows = db._conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        names = {r[0] for r in rows}
        assert not any("token" in n.lower() or "secret" in n.lower() for n in names)
        for n in names:
            cols = [c[1].lower() for c in db._conn.execute(f"PRAGMA table_info({n})").fetchall()]
            assert not any("token" in c or "secret" in c for c in cols), (n, cols)
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Contract 1.6.0 — enums, models, validation rules (WP1)
# --------------------------------------------------------------------------- #


def test_new_enums_round_trip_through_the_models() -> None:
    """Every new enum must survive ``model_dump`` -> ``model_validate``.

    A ``str, Enum`` member is serialised as its value; re-validating that value
    must produce an equal model.  This is the guard for "the enum exists but the
    field accepts only the bare string" class of half-implemented addition.
    """
    circuit = Circuit(
        circuit_id="c1",
        classification=CircuitClass.PARTIALLY_UNDERSTOOD,
        classification_status=TopologyStatus.CANDIDATE,
        measurement_basis=MeasurementBasis.FACTOR_ADJUSTED,
        device_ids=["d1", "d2"],
        metadata={"unattributed_load_w": 42.0},
    )
    assert Circuit.model_validate(circuit.model_dump()) == circuit

    relation = TopologyRelation(
        relation_id="r1",
        kind=RelationKind.DEVICE_CT_ASSOCIATION,
        subject_id="d1",
        object_id="ct1",
        status=TopologyStatus.CONFIRMED,
        lifecycle=EdgeLifecycle.CONFIRMED,
        requires_confirmation=False,
        confidence=0.8,
        declared_by="inference",
    )
    assert TopologyRelation.model_validate(relation.model_dump()) == relation

    binding = HaEntityBinding(
        entity_id="sensor.power",
        device_id="d1",
        role=HaEntityRole.POWER_MEASUREMENT,
        is_load_evidence=True,
        contributes_power=True,
    )
    assert HaEntityBinding.model_validate(binding.model_dump()) == binding

    now = datetime.now(UTC)
    isolation = EventIsolation(
        status=IsolationStatus.CONTAMINATED,
        event_id="e1",
        device_id="d1",
        entity_id="switch.d1",
        occurred_at=now,
        received_at=now,
        analysis_offset_s=5.0,
        isolation_window_s=30.0,
        window_start=now,
        window_end=now,
        competing_event_ids=["e2"],
        competing_device_ids=["d2"],
        power_window_contaminated=True,
        reason="simultaneous_switching",
        evidence={"competing": 1},
    )
    assert EventIsolation.model_validate(isolation.model_dump()) == isolation
    # The original timestamp is never mutated by the isolation analysis.
    assert isolation.occurred_at == now

    assert {member.value for member in ConfidenceDimension} == {
        "power",
        "ct_association",
        "topology",
        "circuit_classification",
        "residual",
    }
    assert {member.value for member in ResidualKind} == {
        "positive",
        "negative",
        "near_zero",
    }
    assert {member.value for member in RelationKind} == {
        "device_ct_association",
        "ct_measurement_overlap",
        "circuit_membership",
        "electrical_parent_child",
    }
    assert {member.value for member in HaEntityRole} == {
        "load_state",
        "mode_only",
        "power_measurement",
        "energy_counter",
        "availability",
        "unknown",
    }
    assert {member.value for member in IsolationStatus} == {
        "isolated",
        "contaminated",
        "insufficient_data",
    }
    assert {member.value for member in MeasurementBasis} == {
        "ct_measured",
        "monitor_reported",
        "factor_adjusted",
        "estimated_appliance_total",
        "unknown",
    }
    assert {member.value for member in CircuitClass} == {
        "dedicated",
        "shared",
        "partially_understood",
        "unknown",
    }


def test_new_enums_are_documented_per_member() -> None:
    """Each member must explain itself; the taxonomy is the product surface.

    A bare string after an enum member is not retained at runtime, so the docs
    are attached to ``member.__doc__`` explicitly -- otherwise "documented" would
    mean "visible in the source and nowhere else".
    """
    for enum_cls in (
        CircuitClass,
        MeasurementBasis,
        ConfidenceDimension,
        ResidualKind,
        RelationKind,
        HaEntityRole,
        IsolationStatus,
    ):
        assert enum_cls.__doc__, enum_cls
        for member in enum_cls:
            assert isinstance(member, str)
            assert member.value, member
            assert member.__doc__, f"{enum_cls.__name__}.{member.name} has no docstring"


def test_confidence_vector_has_no_aggregate_and_no_float() -> None:
    """A single blended score is exactly what §4.4 rule 7 forbids.

    The vector must expose the dimensions and nothing that collapses them: no
    aggregate field, no ``__float__``, no arithmetic.
    """
    cv = ConfidenceVector(
        power=0.9,
        ct_association=None,
        topology=0.5,
        circuit_classification=None,
        residual=0.1,
        method="blend_v1",
    )
    assert cv.power == pytest.approx(0.9)
    assert cv.ct_association is None
    with pytest.raises(TypeError):
        float(cv)  # type: ignore[arg-type]
    field_names = set(ConfidenceVector.model_fields)
    assert not {"aggregate", "score", "overall", "total", "blended"} & field_names
    # Guaranteeing the same via the public surface: no numeric dunder exists.
    assert not hasattr(ConfidenceVector, "__float__")


def test_confidence_vector_rejects_out_of_range_dimensions() -> None:
    with pytest.raises(ValueError):
        ConfidenceVector(power=1.5)


def test_residual_power_derives_kind_from_the_sign() -> None:
    negative = ResidualPower(residual_w=-42.0)
    assert negative.kind == ResidualKind.NEGATIVE
    assert negative.residual_w == pytest.approx(-42.0)  # never clamped

    positive = ResidualPower(residual_w=42.0)
    assert positive.kind == ResidualKind.POSITIVE

    near_zero = ResidualPower(residual_w=0.0)
    assert near_zero.kind == ResidualKind.NEAR_ZERO


def test_residual_power_rejects_a_mismatched_stored_kind() -> None:
    with pytest.raises(ValueError):
        ResidualPower(residual_w=-42.0, kind=ResidualKind.POSITIVE)
    with pytest.raises(ValueError):
        ResidualPower(residual_w=0.0, kind=ResidualKind.NEGATIVE)


def test_residual_power_unresolved_is_never_rendered_as_zero() -> None:
    """An unresolved residual keeps its value; it is not rewritten to 0 W."""
    rp = ResidualPower(residual_w=-12.0, resolved=False)
    dump = rp.model_dump()
    assert dump["residual_w"] == pytest.approx(-12.0)
    assert dump["resolved"] is False


def test_ha_entity_bindings_allow_at_most_one_power_contributor() -> None:
    with pytest.raises(ValueError):
        DeviceConfiguration(
            device_id="d1",
            entity_bindings=[
                HaEntityBinding(
                    entity_id="sensor.power_a",
                    device_id="d1",
                    role=HaEntityRole.POWER_MEASUREMENT,
                    contributes_power=True,
                ),
                HaEntityBinding(
                    entity_id="sensor.power_b",
                    device_id="d1",
                    role=HaEntityRole.POWER_MEASUREMENT,
                    contributes_power=True,
                ),
            ],
        )


def test_ha_entity_bindings_allow_one_power_contributor_plus_a_state_entity() -> None:
    config = DeviceConfiguration(
        device_id="d1",
        connectivity="connected",
        state_entity_ids=["binary_sensor.running"],
        entity_bindings=[
            HaEntityBinding(
                entity_id="sensor.power",
                device_id="d1",
                role=HaEntityRole.POWER_MEASUREMENT,
                is_load_evidence=True,
                contributes_power=True,
            ),
            HaEntityBinding(
                entity_id="binary_sensor.running",
                device_id="d1",
                role=HaEntityRole.LOAD_STATE,
                is_load_evidence=True,
            ),
        ],
    )
    assert sum(b.contributes_power for b in config.entity_bindings) == 1


def test_ha_entity_binding_must_belong_to_its_device() -> None:
    with pytest.raises(ValueError):
        DeviceConfiguration(
            device_id="d1",
            entity_bindings=[
                HaEntityBinding(
                    entity_id="sensor.power",
                    device_id="d2",
                    role=HaEntityRole.POWER_MEASUREMENT,
                    contributes_power=True,
                )
            ],
        )


def test_topology_relation_rejects_self_edge() -> None:
    with pytest.raises(ValueError):
        TopologyRelation(
            relation_id="r1",
            kind=RelationKind.ELECTRICAL_PARENT_CHILD,
            subject_id="ct1",
            object_id="ct1",
        )


def test_topology_relation_circuit_membership_requires_a_circuit() -> None:
    with pytest.raises(ValueError):
        TopologyRelation(
            relation_id="r1",
            kind=RelationKind.CIRCUIT_MEMBERSHIP,
            subject_id="d1",
            object_id="ct1",
            evidence={},
        )
    ok = TopologyRelation(
        relation_id="r2",
        kind=RelationKind.CIRCUIT_MEMBERSHIP,
        subject_id="d1",
        object_id="ct1",
        evidence={"circuit_id": "c1"},
    )
    assert ok.kind == RelationKind.CIRCUIT_MEMBERSHIP


def test_parent_child_relation_must_reference_two_known_cts() -> None:
    payload = {
        "relation_id": "r1",
        "kind": "electrical_parent_child",
        "subject_id": "sub_l1",
        "object_id": "service_l1",
    }
    ok = TopologyRelation.model_validate(payload, context={"ct_ids": ["sub_l1", "service_l1"]})
    assert ok.kind == RelationKind.ELECTRICAL_PARENT_CHILD
    with pytest.raises(ValueError):
        TopologyRelation.model_validate(payload, context={"ct_ids": ["sub_l1"]})


def test_parent_child_relation_rejects_a_containment_cycle() -> None:
    """Rule 2: a parent/child edge must not close a loop."""
    existing = TopologyRelation(
        relation_id="r0",
        kind=RelationKind.ELECTRICAL_PARENT_CHILD,
        subject_id="service_l1",
        object_id="sub_l1",
    )
    cycle = TopologyRelation(
        relation_id="r1",
        kind=RelationKind.ELECTRICAL_PARENT_CHILD,
        subject_id="sub_l1",
        object_id="service_l1",
    )
    with pytest.raises(ValueError):
        TopologyRelation.model_validate(
            cycle.model_dump(), context={"relations": [existing]}
        )


def test_partially_understood_circuit_needs_two_devices_and_a_note() -> None:
    with pytest.raises(ValueError):
        Circuit(
            circuit_id="c1",
            classification=CircuitClass.PARTIALLY_UNDERSTOOD,
            device_ids=["d1"],
            metadata={"unattributed_load_w": 5.0},
        )
    with pytest.raises(ValueError):
        Circuit(
            circuit_id="c1",
            classification=CircuitClass.PARTIALLY_UNDERSTOOD,
            device_ids=["d1", "d2"],
            metadata={},
        )
    ok = Circuit(
        circuit_id="c1",
        classification=CircuitClass.PARTIALLY_UNDERSTOOD,
        device_ids=["d1", "d2"],
        metadata={"unattributed_load_w": 5.0},
    )
    assert ok.classification == CircuitClass.PARTIALLY_UNDERSTOOD


def test_dedicated_circuit_needs_exactly_one_device() -> None:
    with pytest.raises(ValueError):
        Circuit(
            circuit_id="c1",
            classification=CircuitClass.DEDICATED,
            device_ids=["d1", "d2"],
        )
    ok = Circuit(circuit_id="c1", classification=CircuitClass.DEDICATED, device_ids=["d1"])
    assert ok.device_ids == ["d1"]


# --------------------------------------------------------------------------- #
# Backward compatibility: a 1.5.0 payload (no new keys) must still validate
# --------------------------------------------------------------------------- #


def test_a_1_5_0_ct_channel_payload_still_validates() -> None:
    payload = {
        "ct_id": "br1",
        "entity_id": "sensor.br1",
        "ct_type": "branch",
        "phase": "l1",
        "measurement_multiplier": 1.0,
        "panel_id": "main",
    }
    channel = CTChannel.model_validate(payload)
    assert channel.measurement_basis == MeasurementBasis.UNKNOWN
    assert channel.circuit_id is None


def test_a_1_5_0_measurement_payload_still_validates() -> None:
    payload = {
        "entity_id": "sensor.br1",
        "ct_id": "br1",
        "value": 120.0,
        "state_timestamp": "2026-11-13T12:00:00+00:00",
    }
    m = Measurement.model_validate(payload)
    assert m.measurement_basis == MeasurementBasis.UNKNOWN
    assert m.multiplier_applied == 1.0


def test_a_1_5_0_inference_result_payload_still_validates() -> None:
    payload = {
        "device_id": "d1",
        "timestamp": "2026-11-13T12:00:00+00:00",
        "power_w": 120.0,
        "source": "inferred",
        "confidence": 0.5,
        "confidence_tier": "tier_2_inferred",
    }
    r = InferenceResult.model_validate(payload)
    assert r.measurement_basis == MeasurementBasis.UNKNOWN
    assert r.confidence_dimensions is None
    assert r.algorithm_version is None
    assert r.record_id is None


def test_a_1_5_0_topology_payload_still_validates() -> None:
    payload = {
        "version": 3,
        "status": "candidate",
        "channels": [
            {"ct_id": "main_l1", "entity_id": "sensor.main_l1", "ct_type": "main", "phase": "l1"}
        ],
        "devices": [{"device_id": "d1", "branch_ct_ids": ["main_l1"]}],
    }
    t = Topology.model_validate(payload)
    assert t.circuits == []
    assert t.relations == []
    assert t.algorithm_version is None


def test_the_retired_dedicated_ct_ids_key_is_rejected() -> None:
    """2.0.0 is a major precisely because this key is gone.

    A 1.5.0 payload that carries ``dedicated_ct_ids`` names a role the contract
    no longer defines, so it must fail loudly rather than be silently ignored:
    a mute drop would tell the caller its declaration was accepted.
    """
    payload = {
        "version": 3,
        "status": "candidate",
        "channels": [],
        "devices": [{"device_id": "d1", "dedicated_ct_ids": ["main_l1"]}],
    }
    with pytest.raises(ValidationError):
        Topology.model_validate(payload)


def test_a_1_5_0_reconciliation_payload_still_validates() -> None:
    payload = {"main_power_w": 1000.0, "unknown_residual_w": 400.0}
    rec = Reconciliation.model_validate(payload)
    assert rec.residual is None


# --------------------------------------------------------------------------- #
# The 2.1.0 additive change: a payload produced *before* it must still validate
# --------------------------------------------------------------------------- #
#
# The 2.1.0 change added the ten §7 contracts, six enums and no field to any
# pre-existing model.  "Additive" is only a claim until a payload that predates
# the change is run through the new models, so the payload below is written
# against the field set the *2.0.0* contract had: every key it carries is a key
# ``git show ee85417:engine/wattmosaic_engine/contracts.py`` already declared.
# Nothing in it is a 2.1.0 addition.
#
# The payload is deliberately the *shipped producer's* shape rather than a
# hand-built minimal one: it is the wire form the engine has always written, so
# a producer still running the old code produces exactly this.


def _pre_change_payload() -> dict:
    """A 2.0.0-era payload: every key existed in the 2.0.0 contract.

    The shape mirrors ``data/examples/topology.example.json`` -- the example the
    repository ships for exactly this purpose -- plus the inference and
    reconciliation rows the engine wrote alongside it.
    """
    return {
        "topology": {
            "version": 1,
            "status": "candidate",
            "created_at": "2026-01-01T00:00:00+00:00",
            "notes": ["written by the 2.0.0 producer"],
            "channels": [
                {
                    "ct_id": "main_l1",
                    "entity_id": "sensor.ct_panel_a_l1_power",
                    "name": "Main L1",
                    "ct_type": "main",
                    "phase": "l1",
                    "polarity": "normal",
                    "enabled": True,
                    "expected_interval_s": 15.0,
                    "reliability": 0.95,
                    "topology_status": "candidate",
                    "metadata": {},
                },
                {
                    "ct_id": "branch_fridge",
                    "entity_id": "sensor.ct_fridge_power",
                    "name": "Fridge",
                    "ct_type": "branch",
                    "phase": "l1",
                    "parent_ct_id": "main_l1",
                    "polarity": "normal",
                    "enabled": True,
                    "topology_status": "candidate",
                    "metadata": {},
                },
            ],
            "devices": [
                {
                    "device_id": "dev_fridge",
                    "name": "Kitchen Fridge",
                    "branch_ct_ids": ["branch_fridge"],
                    "phase": "l1",
                    "double_pole": False,
                    "topology_status": "candidate",
                    "metadata": {},
                }
            ],
        },
        "measurement": {
            "entity_id": "sensor.ct_fridge_power",
            "ct_id": "branch_fridge",
            "value": 120.0,
            "unit": "W",
            "state_timestamp": "2026-01-01T00:00:15+00:00",
            "quality": "good",
            "repeated": False,
            "polarization": "normal",
            "sequence": 1,
        },
        "inference_result": {
            "device_id": "dev_fridge",
            "timestamp": "2026-01-01T00:00:30+00:00",
            "power_w": 118.5,
            "source": "inferred",
            "confidence": 0.72,
            "model_id": "binary_fridge",
            "model_version": 3,
            "operating_stage": "running",
            "stage_status": "identified",
            "sample_count": 12,
            "quality_status": "good",
            "measurement_age_seconds": 15.0,
            "supporting_ct_channels": ["branch_fridge"],
            "explanation": ["branch_power_increase"],
            "provenance": {"window_s": 30},
        },
        "reconciliation": {
            "timestamp": "2026-01-01T00:00:30+00:00",
            "topology_version": 1,
            "main_power_w": 1000.0,
            "l1_power_w": 620.0,
            "l2_power_w": 380.0,
            "direct_measured_w": 300.0,
            "inferred_w": 118.5,
            "model_estimated_w": 0.0,
            "configured_prior_w": 0.0,
            "unknown_residual_w": 581.5,
            "unmonitored_w": 581.5,
            "meter_disagreement_w": 0.0,
            "meter_spread_w": 0.0,
            "meter_spread_exceeds_residual": False,
            "reconciliation_error_w": 0.0,
            "reconciliation_error_ratio": 0.0,
            "accounted_fraction": 0.4185,
            "measurement_uncertainty_w": 12.0,
            "quality_status": "good",
            "explanation": ["unattributed_load"],
            "topology_confirmed": False,
        },
        "device_event": {
            "event_id": "e1",
            "device_id": "dev_fridge",
            "entity_id": "switch.dev_fridge",
            "event_type": "state_changed",
            "previous_state": "off",
            "new_state": "on",
            "attributes": {"power_w": 118.5},
            "occurred_at": "2026-01-01T00:00:10+00:00",
            "received_at": "2026-01-01T00:00:11+00:00",
        },
    }


def test_pre_change_payload_still_validates() -> None:
    """A payload produced before the 2.1.0 change still validates.

    This is the task's central criterion: the contract change is additive, so
    a producer that has not been upgraded must keep working.  The payload is
    built from the 2.0.0 field set only and is checked against **five** of the
    pre-existing models, because "it still validates" proved against one model
    says nothing about the others.
    """
    payload = _pre_change_payload()

    topology = Topology.model_validate(payload["topology"])
    assert [c.ct_id for c in topology.channels] == ["main_l1", "branch_fridge"]
    assert topology.channel_map()["branch_fridge"].parent_ct_id == "main_l1"
    assert topology.device_map()["dev_fridge"].branch_ct_ids == ["branch_fridge"]

    measurement = Measurement.model_validate(payload["measurement"])
    assert measurement.effective_power_w() == pytest.approx(120.0)

    result = InferenceResult.model_validate(payload["inference_result"])
    assert result.power_w == pytest.approx(118.5)
    assert result.source == Source.INFERRED

    reconciliation = Reconciliation.model_validate(payload["reconciliation"])
    assert reconciliation.main_power_w == pytest.approx(1000.0)
    assert reconciliation.unknown_residual_w == pytest.approx(581.5)

    event = DeviceEvent.model_validate(payload["device_event"])
    assert event.new_state == "on"


def test_pre_change_payload_gains_no_new_required_field() -> None:
    """The new models' defaults, not the payload, carry the new information.

    Every field the 2.1.0 change added to a pre-existing model has a neutral
    default, so a pre-change payload validates *and* the reader learns nothing
    it did not state.  A field that had been made mandatory would break the
    first assertion; a field that had been given a non-neutral default would
    break this one.
    """
    payload = _pre_change_payload()
    topology = Topology.model_validate(payload["topology"])
    assert topology.circuits == []
    assert topology.relations == []
    assert topology.algorithm_version is None

    measurement = Measurement.model_validate(payload["measurement"])
    assert measurement.measurement_basis == MeasurementBasis.UNKNOWN
    assert measurement.multiplier_applied == 1.0

    result = InferenceResult.model_validate(payload["inference_result"])
    assert result.measurement_basis == MeasurementBasis.UNKNOWN
    assert result.confidence_dimensions is None
    assert result.algorithm_version is None
    assert result.record_id is None

    reconciliation = Reconciliation.model_validate(payload["reconciliation"])
    assert reconciliation.residual is None


def test_a_topology_relation_without_lifecycle_still_validates() -> None:
    """The lifecycle field is additive-and-optional, so 2.0.0 payloads survive.

    ``lifecycle`` was added to ``TopologyRelation`` without bumping
    ``CONTRACT_VERSION``: it is a second vocabulary from ``status``, not a
    replacement, and it must have a neutral default.  A payload written before
    it existed therefore still validates *and* resolves to the proposal state
    -- never to a confirmation the writer never declared.  This is the property
    that justifies leaving the version at 2.1.0.
    """
    payload = {
        "relation_id": "r1",
        "kind": "device_ct_association",
        "subject_id": "d1",
        "object_id": "ct1",
    }
    relation = TopologyRelation.model_validate(payload)
    assert relation.lifecycle is EdgeLifecycle.CANDIDATE
    assert EdgeLifecycle(relation.lifecycle) is not EdgeLifecycle.CONFIRMED
    assert relation.requires_confirmation is True


def test_a_pre_change_payload_that_was_already_invalid_is_still_invalid() -> None:
    """Additive does not mean lenient.

    The change must not have relaxed a pre-existing rule: a payload the 2.0.0
    contract rejected -- a retired key, an out-of-range confidence -- must still
    be rejected, or "additive" would be hiding a silent behaviour change.
    """
    payload = _pre_change_payload()

    # A key the 2.0.0 contract retired: it is still forbidden.
    retired = json.loads(json.dumps(payload["topology"]))
    retired["devices"][0]["dedicated_ct_ids"] = ["main_l1"]
    with pytest.raises(ValidationError):
        Topology.model_validate(retired)

    # A value the 2.0.0 contract bounded: it is still out of range.
    out_of_range = json.loads(json.dumps(payload["inference_result"]))
    out_of_range["confidence"] = 1.5
    with pytest.raises(ValidationError):
        InferenceResult.model_validate(out_of_range)

    # A key the 2.0.0 contract did not define: it is still forbidden.
    unknown_key = json.loads(json.dumps(payload["reconciliation"]))
    unknown_key["not_a_field"] = 1
    with pytest.raises(ValidationError):
        Reconciliation.model_validate(unknown_key)


# --------------------------------------------------------------------------- #
# The 2.3.0 additive change: the mapping cell represents the mapping matrix
# --------------------------------------------------------------------------- #


def _pre_2_3_mapping_cell_payload() -> dict:
    """A 2.2.0-era mapping cell: only the fields that existed before 2.3.0."""
    return {
        "device_id": "dev_fridge",
        "reader_id": "branch_fridge",
        "reader_configuration_version": "rcv-1",
        "observability": "direct",
        "estimated_contribution_w": 118.5,
        "min_contribution_w": 100.0,
        "max_contribution_w": 140.0,
        "evidence_count": 12,
        "status": "valid",
        "basis": "ct_measured",
    }


def test_mapping_cell_new_fields_round_trip() -> None:
    """2.3.0 payloads with every new field survive a model round-trip."""
    payload = {
        **_pre_2_3_mapping_cell_payload(),
        "relation_type": "one_to_many",
        "sources_used": [
            {"source": "topology", "reason": "containment_edge"},
            {"source": "residual", "reason": "observed_contribution"},
        ],
        "model_version": 3,
        "topology_version": "tv-2",
        "is_unknown_load": False,
        "unknown_load_profile_id": None,
        "status": "learning",
    }
    cell = DeviceReaderMappingCell.model_validate(payload)
    assert cell.relation_type == "one_to_many"
    assert [entry["source"] for entry in cell.sources_used] == ["topology", "residual"]
    assert cell.model_version == 3
    assert cell.topology_version == "tv-2"
    assert cell.is_unknown_load is False
    # Model uses use_enum_values=True, so status is stored as its string value.
    assert cell.status == MappingCellStatus.LEARNING.value

    # Dumping and re-validating preserves every new field.
    reloaded = DeviceReaderMappingCell.model_validate(cell.model_dump())
    assert reloaded.model_dump() == cell.model_dump()


def test_mapping_cell_pre_2_3_payload_gets_neutral_defaults() -> None:
    """A payload written before 2.3.0 still validates and reads neutrally."""
    cell = DeviceReaderMappingCell.model_validate(_pre_2_3_mapping_cell_payload())
    assert cell.relation_type is None
    assert cell.sources_used == []
    assert cell.model_version is None
    assert cell.topology_version is None
    assert cell.is_unknown_load is False
    assert cell.unknown_load_profile_id is None
    # A frozen field's type is unchanged: device_id is still a required str.
    assert isinstance(cell.device_id, str)


def test_mapping_cell_unknown_load_marker_without_nullable_device_id() -> None:
    """Unknown loads use the marker; device_id stays a required opaque str."""
    payload = _pre_2_3_mapping_cell_payload()
    payload["device_id"] = "unknown-load-7"
    payload["is_unknown_load"] = True
    payload["unknown_load_profile_id"] = "ulp-7"
    payload["status"] = "non_identifiable"
    cell = DeviceReaderMappingCell.model_validate(payload)
    assert cell.is_unknown_load is True
    assert cell.device_id == "unknown-load-7"
    assert cell.unknown_load_profile_id == "ulp-7"
    assert cell.status == MappingCellStatus.NON_IDENTIFIABLE.value


def test_mapping_cell_status_has_the_six_state_vocabulary() -> None:
    """The status enum carries the six-state vocabulary, additively."""
    values = {member.value for member in MappingCellStatus}
    assert {
        "unknown",
        "learning",
        "provisional",
        "confirmed",
        "conflicted",
        "non_identifiable",
    } <= values
    # Original members are retained.
    assert {"valid", "stale", "invalid", "unknown"} <= values


# --------------------------------------------------------------------------- #
# The 2.4.0 additive change: typed status and confidence on configuration changes
# --------------------------------------------------------------------------- #


def _pre_2_4_change_event_payload() -> dict:
    """A pre-2.4.0 configuration change: only the fields that already existed."""
    return {
        "change_id": "chg-1",
        "kind": "smart_plug_relocation",
        "detection": "observed",
        "notes": "operator moved the plug",
    }


def test_change_event_new_fields_round_trip() -> None:
    """2.4.0 payloads with status and confidence survive a model round-trip."""
    payload = {
        **_pre_2_4_change_event_payload(),
        "status": "confirmed",
        "confidence": 0.87,
    }
    event = contracts.ConfigurationChangeEvent.model_validate(payload)
    # Model uses use_enum_values=True, so status is stored as its string value.
    assert event.status == ChangeStatus.CONFIRMED.value
    assert event.confidence == 0.87

    # Dumping and re-validating preserves every new field.
    reloaded = contracts.ConfigurationChangeEvent.model_validate(event.model_dump())
    assert reloaded.model_dump() == event.model_dump()


def test_change_event_pre_2_4_payload_gets_neutral_defaults() -> None:
    """A payload written before 2.4.0 still validates and reads neutrally."""
    event = contracts.ConfigurationChangeEvent.model_validate(
        _pre_2_4_change_event_payload()
    )
    assert event.status == ChangeStatus.SUSPECTED.value
    assert event.confidence is None
    # Frozen fields keep their names, types and semantics.
    assert event.detection == "observed"
    assert event.notes == "operator moved the plug"


def test_change_status_has_the_suspected_confirmed_vocabulary() -> None:
    """The status enum carries exactly the SUSPECTED/CONFIRMED vocabulary."""
    assert {member.value for member in ChangeStatus} == {"suspected", "confirmed"}
