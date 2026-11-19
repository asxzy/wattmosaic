"""Rehearse the engine's real startup migration on a COPY of a database.

The migration rehearsal is the release gate for HB §16 / DEC-02: before an
operator upgrades a real install we must show, on a copy of their database, that

* the engine's real startup path (``Database(path)``, the same constructor
  ``main.py`` uses) takes the schema from the old version to the latest;
* its automatic pre-migration backup (DEC-02) was created;
* every pre-existing row count was preserved (the legacy tables archived by
  v025 are renamed to ``*_legacy`` but stay readable and unchanged);
* primary ids for endpoints/devices/HA tables and HA unique ids are unchanged;
* rolling back -- restoring the backup over the copy and starting the previous
  version's schema -- recovers the original ``schema_version`` and row counts.

The input database is opened strictly read-only for the snapshot and is never
modified: a temporary copy is made with ``shutil.copy2`` and that copy is the
one the engine migrates.  With ``--synthesize`` the tool builds a pre-3.0
(``user_version < 13``) database from the engine's own released migration
statements, inserts representative rows through SQL, and rehearses against that
-- so the whole procedure runs in CI with no real data.

Usage::

    .venv/bin/python tools/dev/rehearse_migration.py --synthesize
    .venv/bin/python tools/dev/rehearse_migration.py --db /path/to/copy.db

No operator path or real data is ever written to the report: only the database
basename and schema facts are printed.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "engine") not in sys.path:
    sys.path.insert(0, str(ROOT / "engine"))

from tools.dev import _perfsupport as support  # noqa: E402

NOW = "2026-01-01T00:00:00+00:00"
#: Pre-3.0 schemas are strictly below migration 13 (13 is the first canonical
#: model rebuild and the first version that triggers a pre-migration backup).
PRE_30_VERSION = 11


def _engine():
    """Import the engine lazily so ``--help`` never needs the dependencies."""
    from wattmosaic_engine.storage.database import Database  # noqa: PLC0415
    from wattmosaic_engine.storage.migrations import (  # noqa: PLC0415
        LATEST_VERSION,
        MIGRATIONS,
        current_version,
    )

    return Database, LATEST_VERSION, MIGRATIONS, current_version


def synthesize(db_path: Path, *, upto: int = PRE_30_VERSION) -> dict[str, Any]:
    """Build a pre-3.0 database from the real migration statements + SQL seeds.

    Uses the same ``MIGRATIONS`` objects the engine applies at startup, stopping
    at ``upto`` (default 11, i.e. strictly before migration 13).  Representative
    legacy rows are inserted through plain SQL so the v013-v019 backfills have
    real data to preserve and so v025 has a legacy table to archive.
    """
    _db, latest, migrations, current = _engine()
    assert upto < 13, "a pre-3.0 schema must be below migration 13"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        for migration in migrations:
            if migration.version > upto:
                break
            for statement in migration.statements:
                conn.execute(statement)
            if migration.apply is not None:
                migration.apply(conn)
        conn.execute(f"PRAGMA user_version = {upto}")
        seeds = {
            "engine_state": (
                "INSERT INTO engine_state(key, value, updated_at) VALUES('sentinel', 'kept', ?)",
                (NOW,),
            ),
            "ct_channels": (
                "INSERT INTO ct_channels(ct_id, entity_id, ct_type, updated_at) "
                "VALUES('ct_main', 'sensor.ct_main_power', 'main', ?)",
                (NOW,),
            ),
            "devices": (
                "INSERT INTO devices(device_id, name, branch_ct_ids_json, updated_at) "
                "VALUES('dev_a', 'Device A', '[\"ct_main\"]', ?)",
                (NOW,),
            ),
            "topology_versions": (
                "INSERT INTO topology_versions(created_at, status, payload_json) "
                "VALUES(?, 'candidate', '{\"channels\": []}')",
                (NOW,),
            ),
            "topology_relations": (
                "INSERT INTO topology_relations(relation_id, kind, subject_id, object_id, "
                "created_at) VALUES('rel_1', 'ELECTRICAL_PARENT_CHILD', 'ct_main', 'dev_a', ?)",
                (NOW,),
            ),
            "device_model_versions": (
                "INSERT INTO device_model_versions(device_id, model_type, version, status, "
                "created_at) VALUES('dev_a', 'constant', 1, 'active', ?)",
                (NOW,),
            ),
            "device_models": (
                "INSERT INTO device_models(model_id, device_id, model_type, active_version, "
                "created_at, updated_at) VALUES('m1', 'dev_a', 'constant', 1, ?, ?)",
                (NOW, NOW),
            ),
            "learning_samples": (
                "INSERT INTO learning_samples(sample_id, device_id, captured_at, created_at) "
                "VALUES('s1', 'dev_a', ?, ?)",
                (NOW, NOW),
            ),
            "residual_power": (
                "INSERT INTO residual_power(timestamp, residual_w) VALUES(?, 12.5)", (NOW,)
            ),
            "device_events": (
                "INSERT INTO device_events(event_id, device_id, entity_id, event_type, "
                "occurred_at, received_at) VALUES('e1', 'dev_a', 'switch.dev_a', "
                "'state_changed', ?, ?)",
                (NOW, NOW),
            ),
            "ha_entity_bindings": (
                "INSERT INTO ha_entity_bindings(entity_id, device_id) "
                "VALUES('switch.dev_a', 'dev_a')",
                (),
            ),
            "ct_samples": (
                "INSERT INTO ct_samples(ct_id, entity_id, value_w, raw_value, unit, "
                "state_timestamp, received_at, quality) "
                "VALUES('ct_main', 'sensor.ct_main_power', 100, 100, 'W', ?, ?, 'good')",
                (NOW, NOW),
            ),
            "inference_results": (
                "INSERT INTO inference_results(device_id, timestamp, power_w, source, "
                "confidence, sample_count, quality_status, measurement_age_seconds) "
                "VALUES('dev_a', ?, 50, 'test', 1.0, 1, 'good', 0)",
                (NOW,),
            ),
        }
        for sql, params in seeds.values():
            conn.execute(sql, params)
        conn.commit()
        version = current(conn)
    finally:
        conn.close()
    return {
        "synthesized": True,
        "schema_version": version,
        "target_version": latest,
        "seed_tables": sorted(seeds),
        "path_name": support.input_path_scrub(db_path),
    }


def _open_readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def _snapshot(path: Path) -> dict[str, Any]:
    conn = _open_readonly(path)
    try:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        return {
            "schema_version": version,
            "row_counts": support.row_counts(conn),
            "ids": support.primary_ids(conn),
            "ha_unique_ids": support.ha_unique_ids(conn),
        }
    finally:
        conn.close()


def _find_backup(db_path: Path) -> Path | None:
    """Locate the pre-migration backup created by ``Database.__init__``."""
    matches = sorted(
        db_path.parent.glob(f"{db_path.name}.pre-v*.bak"),
        key=lambda p: (p.stat().st_mtime_ns, p.name),
    )
    return matches[-1] if matches else None


def rehearse(input_db: Path, workdir: Path) -> dict[str, Any]:
    """Run the full forward + rollback rehearsal on a copy of ``input_db``.

    Returns a JSON-safe report.  A failed assertion is recorded under
    ``report['checks'][name] = {'ok': False, 'failures': [...]}``; the CLI turns
    any failed check into a non-zero exit.
    """
    Database, latest, _migrations, current = _engine()
    report: dict[str, Any] = {
        "input_name": support.input_path_scrub(input_db),
        "checks": {},
        "errors": [],
    }
    if not input_db.exists():
        report["errors"].append(f"no database at {support.input_path_scrub(input_db)}")
        report["ok"] = False
        return report

    workdir.mkdir(parents=True, exist_ok=True)
    copy_path = workdir / "rehearsal.db"
    shutil.copy2(input_db, copy_path)
    # A source DB left in WAL mode may have uncheckpointed rows; checkpoint the
    # copy so its rows and its backup are self-contained.
    src = sqlite3.connect(str(copy_path))
    try:
        src.execute("PRAGMA journal_mode=DELETE")
        src.commit()
    finally:
        src.close()

    before = _snapshot(copy_path)
    report["before_schema_version"] = before["schema_version"]

    # -- forward: the engine's real startup migration path ------------------
    db = Database(copy_path)
    db.close()
    after = _snapshot(copy_path)
    report["after_schema_version"] = after["schema_version"]
    backup = _find_backup(copy_path)
    report["backup_name"] = support.input_path_scrub(backup) if backup else None

    checks = report["checks"]
    checks["backup_created"] = {
        "ok": backup is not None,
        "failures": [] if backup else ["no pre-migration backup file was created"],
    }
    checks["schema_advanced"] = {
        "ok": after["schema_version"] >= latest and after["schema_version"] > before["schema_version"],
        "failures": (
            [] if after["schema_version"] >= latest and after["schema_version"] > before["schema_version"]
            else [f"schema {before['schema_version']} -> {after['schema_version']} (latest {latest})"]
        ),
    }
    ok, failures = support.reconcile_counts(before["row_counts"], after["row_counts"])
    checks["row_counts_preserved"] = {"ok": ok, "failures": failures}

    # The canonical id-bearing tables are *created* by migration 13/14/15 and
    # backfilled from the legacy rows, so they do not exist "before".  The
    # invariant is that their ids are exactly the legacy ids that were carried
    # over (migrations preserve ids, they do not mint new ones for existing
    # entities).  Compare the legacy source ids in the pre-migration snapshot
    # with their canonical counterparts after migration.
    id_failures = support.legacy_to_canonical_id_failures(before["ids"], after["ids"])
    checks["ids_preserved"] = {"ok": not id_failures, "failures": id_failures}

    # HA unique ids: the legacy ``ha_entity_bindings`` rows become
    # ``ha_entity_bindings_legacy`` (archived read-only, still readable) and
    # seed ``ha_entities`` / ``ha_entity_links``.  No HA entity id may be lost
    # or rewritten in that process.
    ha_failures = support.ha_unique_id_failures(
        before["ha_unique_ids"], after["ha_unique_ids"]
    )
    checks["ha_unique_ids_unchanged"] = {"ok": not ha_failures, "failures": ha_failures}

    # The archived legacy tables must still be readable, with the same rows.
    archived_readable: list[str] = []
    from wattmosaic_engine.storage.migrations.v025_legacy_readonly import (  # noqa: PLC0415
        LEGACY_TABLES,
        archived_name,
    )
    for table in LEGACY_TABLES:
        if table in before["row_counts"]:
            archived = archived_name(table)
            if archived not in after["row_counts"]:
                archived_readable.append(f"{archived}: missing (read-only archive)")
            elif after["row_counts"][archived] != before["row_counts"][table]:
                archived_readable.append(
                    f"{archived}: {before['row_counts'][table]} -> "
                    f"{after['row_counts'][archived]} rows"
                )
    checks["legacy_archives_readable"] = {
        "ok": not archived_readable,
        "failures": archived_readable,
    }

    # -- rollback: restore the backup over the copy and assert parity --------
    restored = workdir / "restored.db"
    if backup is not None:
        shutil.copy2(backup, restored)
        rb = sqlite3.connect(str(restored))
        try:
            rb.execute("PRAGMA journal_mode=DELETE")
            rb.commit()
        finally:
            rb.close()
        rolled = _snapshot(restored)
        report["rollback_schema_version"] = rolled["schema_version"]
        version_ok = rolled["schema_version"] == before["schema_version"]
        ok_counts, count_failures = support.reconcile_counts(
            before["row_counts"], rolled["row_counts"]
        )
        checks["rollback_schema_restored"] = {
            "ok": version_ok,
            "failures": [] if version_ok else [
                f"restored schema {rolled['schema_version']} != {before['schema_version']}"
            ],
        }
        checks["rollback_counts_restored"] = {
            "ok": ok_counts,
            "failures": count_failures,
        }
    else:
        checks["rollback_schema_restored"] = {
            "ok": False, "failures": ["cannot roll back: no backup was created"]
        }
        checks["rollback_counts_restored"] = {
            "ok": False, "failures": ["cannot roll back: no backup was created"]
        }

    report["ok"] = all(check["ok"] for check in checks.values())
    return report


def _print_report(report: dict[str, Any]) -> None:
    print("Migration rehearsal")
    print(f"  input             : {report.get('input_name', '?')}")
    print(f"  schema before     : {report.get('before_schema_version', '?')}")
    print(f"  schema after      : {report.get('after_schema_version', '?')}")
    print(f"  pre-migration bak : {report.get('backup_name') or 'MISSING'}")
    print(f"  schema rolled back: {report.get('rollback_schema_version', '?')}")
    for name, check in report["checks"].items():
        status = "OK" if check["ok"] else "FAIL"
        print(f"  [{status:4s}] {name}")
        for failure in check.get("failures", []):
            print(f"           - {failure}")
    print(f"  RESULT            : {'PASS' if report.get('ok') else 'FAIL'}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=None, help="database COPY to rehearse (never modified)")
    ap.add_argument(
        "--synthesize",
        action="store_true",
        help="build a pre-3.0 database from the real migrations + SQL seeds and rehearse it",
    )
    ap.add_argument(
        "--pre-version",
        type=int,
        default=PRE_30_VERSION,
        help=f"schema version for --synthesize (default {PRE_30_VERSION}; must be < 13)",
    )
    ap.add_argument("--json-out", type=Path, default=None, help="write the JSON report here")
    args = ap.parse_args(argv)

    if not args.synthesize and args.db is None:
        print("rehearse_migration: pass --db PATH or --synthesize", file=sys.stderr)
        return 2
    if args.synthesize and args.pre_version >= 13:
        print("rehearse_migration: --pre-version must be < 13 (pre-3.0)", file=sys.stderr)
        return 2

    import tempfile  # noqa: PLC0415

    with tempfile.TemporaryDirectory(prefix="wm-rehearse-") as tmp:
        workdir = Path(tmp)
        if args.synthesize:
            synth = synthesize(workdir / "synthetic.db", upto=args.pre_version)
            report = rehearse(workdir / "synthetic.db", workdir)
            report["synthesize"] = synth
        else:
            report = rehearse(args.db, workdir)

    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(report, indent=2, default=str) + "\n")
    print(json.dumps(report, indent=2, default=str, sort_keys=True))
    _print_report(report)
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
