"""V2a — migration rehearsal end to end on a synthesized pre-3.0 database.

The tool copies the input, runs the engine's *real* startup migration path,
checks the DEC-02 backup / schema / counts / ids / HA unique ids, then restores
the backup and checks the rollback.  ``--synthesize`` builds a real pre-3.0
schema from the engine's own migrations plus SQL seeds, so the entire procedure
runs with no real operator data.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_rehearse_migration_synthesize_end_to_end(tmp_path: Path) -> None:
    """``--synthesize`` rehearses forward and back, with every check passing."""
    script = REPO_ROOT / "tools" / "dev" / "rehearse_migration.py"
    out = tmp_path / "rehearsal.json"
    proc = subprocess.run(
        [
            sys.executable, str(script),
            "--synthesize", "--json-out", str(out),
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:] + proc.stdout[-2000:]
    assert out.exists()
    report = json.loads(out.read_text())

    assert report["synthesize"]["synthesized"] is True
    assert report["synthesize"]["schema_version"] < 13  # genuinely pre-3.0
    assert report["before_schema_version"] < 13
    assert report["after_schema_version"] >= report["synthesize"]["target_version"]
    assert report["backup_name"]  # DEC-02 backup created
    assert report["rollback_schema_version"] == report["before_schema_version"]

    checks = report["checks"]
    assert set(checks) >= {
        "backup_created", "schema_advanced", "row_counts_preserved",
        "ids_preserved", "ha_unique_ids_unchanged", "legacy_archives_readable",
        "rollback_schema_restored", "rollback_counts_restored",
    }
    for name, check in checks.items():
        assert check["ok"] is True, f"{name} failed: {check.get('failures')}"
    assert report["ok"] is True

    assert "RESULT" in proc.stdout and "PASS" in proc.stdout


def test_rehearse_never_modifies_the_input_database(tmp_path: Path) -> None:
    """The rehearsal works on a copy: the synthesized input's bytes are stable."""
    sys.path.insert(0, str(REPO_ROOT))
    from tools.dev import rehearse_migration

    input_db = tmp_path / "input.db"
    rehearse_migration.synthesize(input_db)
    before = input_db.read_bytes()
    report = rehearse_migration.rehearse(input_db, tmp_path / "work")
    assert report["ok"] is True
    assert input_db.read_bytes() == before
