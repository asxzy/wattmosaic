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

import pytest
from wattmosaic_engine import contracts
from wattmosaic_engine.contracts import (
    ChangeStatus,
    HaEntityRole,
    MeasurementBasis,
    MeasurementType,
    ReaderMeasurement,
    ServiceCommand,
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
    """``tools/dev/export_schemas.py`` as a module, for its model maps."""
    path = ROOT / "tools" / "dev" / "export_schemas.py"
    spec = importlib.util.spec_from_file_location("wm_export_schemas_contracts", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_contract_version_is_3_0_0() -> None:
    """The canonical pin: the authoritative version is the one this task bumped.

    Other version assertions compare against this exported constant so they
    stay correct across future additive bumps; the pin lives here.
    """
    assert contracts.CONTRACT_VERSION == "3.0.0"


def test_schema_files_match_contract_version() -> None:
    index = json.loads((ROOT / "data" / "schemas" / "index.json").read_text())
    assert index["contract_version"] == contracts.CONTRACT_VERSION
    assert "inference_snapshot" in index["schemas"]


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
    # v3 transport envelopes with each v3 submodule's ``SCHEMA_EXPORTS``.  New
    # additive contracts (K00..K06) land in the latter.
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


def test_contracts_forbid_unknown_fields() -> None:
    with pytest.raises(ValueError):
        ServiceCommand(command="start_learning", bogus_field=1)  # type: ignore[call-arg]


def test_service_command_enum_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        ServiceCommand(command="delete_everything")  # type: ignore[arg-type]


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
    unique and at most the registry's latest (v026 after Z8).
    """
    versions = [m.version for m in MIGRATIONS]
    assert versions == sorted(versions)
    assert versions[:11] == list(range(1, 12))
    assert len(versions) == len(set(versions)), f"duplicate version: {versions}"
    assert max(versions) == migrations_module.LATEST_VERSION
    reserved = set(migrations_module.RESERVED_VERSIONS)
    spare = set(migrations_module.SPARE_VERSIONS)
    assert reserved == {12}
    assert spare == set(range(23, 25))  # 21 D5, 22 ZF (nullable confidence); 26 Z8
    assert 12 not in versions
    # Upper bound is the registry's own latest (Z8 added v026 after Z1's v025).
    assert all(13 <= version <= migrations_module.LATEST_VERSION for version in versions if version > 11)
    assert not reserved & set(versions)
    assert not spare & set(versions)


def test_wal_enabled(tmp_path: Path) -> None:
    db = Database(tmp_path / "w.db")
    try:
        mode = db._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
    finally:
        db.close()


def _reading(reader_id: str, power_w: float, at: datetime, sequence: int) -> ReaderMeasurement:
    return ReaderMeasurement(
        reader_id=reader_id,
        observed_at=at,
        received_at=at,
        active_power_w=power_w,
        phase="l1",
        measurement_type=MeasurementType.INSTANTANEOUS,
        calibration_version="cal-1",
        measurement_basis="ct_measured",
        sequence=sequence,
    )


def test_ct_sample_insert_and_latest(tmp_path: Path) -> None:
    db = Database(tmp_path / "s.db")
    now = datetime.now(UTC)
    try:
        n = db.repos.samples.insert_readings(
            [
                _reading("main_l1", 100.0, now, 1),
                _reading("main_l1", 120.0, now + timedelta(seconds=15), 2),
            ]
        )
        assert n == 2
        latest = db.repos.samples.latest_ct_sample("main_l1")
        assert latest is not None
        assert latest["value_w"] == pytest.approx(120.0)
        reading = db.repos.samples.latest_reading("main_l1")
        assert reading is not None
        assert reading.active_power_w == pytest.approx(120.0)
    finally:
        db.close()


def test_freezing_model(tmp_path: Path) -> None:
    db = Database(tmp_path / "f.db")
    try:
        db.repos.models.set_model_frozen("dev1", True)
        assert db.repos.models.is_model_frozen("dev1") is True
        db.repos.models.set_model_frozen("dev1", False)
        assert db.repos.models.is_model_frozen("dev1") is False
    finally:
        db.close()


def test_prune_removes_old_rows_only(tmp_path: Path) -> None:
    db = Database(tmp_path / "p.db")
    old = datetime.now(UTC) - timedelta(days=400)
    new = datetime.now(UTC)
    try:
        db.repos.samples.insert_readings(
            [_reading("c", 1.0, old, 1), _reading("c", 2.0, new, 2)]
        )
        removed = db.prune(raw_days=30, aggregate_days=730)
        assert removed["ct_samples"] == 1
        window = db.repos.samples.ct_samples_between(
            new - timedelta(days=1), new + timedelta(days=1)
        )
        assert len(window) == 1
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
# Shared enums carried into v3 (moved out of the deleted legacy module)
# --------------------------------------------------------------------------- #


def test_shared_enums_keep_their_vocabulary() -> None:
    assert {member.value for member in HaEntityRole} == {
        "load_state",
        "mode_only",
        "power_measurement",
        "energy_counter",
        "availability",
        "unknown",
    }
    assert {member.value for member in MeasurementBasis} == {
        "ct_measured",
        "monitor_reported",
        "factor_adjusted",
        "estimated_appliance_total",
        "unknown",
    }


def test_shared_enums_are_documented_per_member() -> None:
    """Each member must explain itself; the taxonomy is the product surface.

    A bare string after an enum member is not retained at runtime, so the docs
    are attached to ``member.__doc__`` explicitly -- otherwise "documented" would
    mean "visible in the source and nowhere else".
    """
    for enum_cls in (MeasurementBasis, HaEntityRole):
        assert enum_cls.__doc__, enum_cls
        for member in enum_cls:
            assert isinstance(member, str)
            assert member.value, member
            assert member.__doc__, f"{enum_cls.__name__}.{member.name} has no docstring"


def test_change_status_has_the_suspected_confirmed_vocabulary() -> None:
    """The status enum carries exactly the SUSPECTED/CONFIRMED vocabulary."""
    assert {member.value for member in ChangeStatus} == {"suspected", "confirmed"}
