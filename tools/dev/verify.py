"""Orchestrator verification harness.

Runs every gate that decides whether the project may be called delivered, and
writes a machine-readable + human-readable result. No network access, no
Home Assistant, no device control.

Usage:
    python tools/dev/verify.py [--json out.json] [--md out.md]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


@dataclass
class Gate:
    name: str
    command: list[str]
    required: bool = True
    timeout: int = 900
    status: str = "NOT_RUN"
    returncode: int | None = None
    duration_s: float = 0.0
    output_tail: str = ""
    notes: str = ""
    skipped_reason: str = ""


def _scrub(text: str) -> str:
    """Remove host-identifying detail from captured command output.

    ``data/verification.json`` and ``docs/verification_report.md`` are tracked,
    so the raw output of a gate must never carry the operator's Home Assistant
    URL, host name or LAN address.  Gate output is diagnostic only; the exit
    code is the actual evidence.
    """
    text = re.sub(r"(?i)\bhttps?://[^\s\"',}]+", "<redacted-url>", text)
    text = re.sub(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b", "<redacted-ip>", text)
    # Absolute filesystem paths name the operator's checkout (and, for a
    # worktree, a directory they may not have chosen to publish).  Traces and
    # warnings are the usual source.  Keep the file name and line so the
    # message stays diagnostic.
    text = re.sub(
        r"(?<![\w.-])/(?:Users|home)/[^\s:\"'`]+/([^\s/:\"'`]+)",
        r"<redacted-path>/\1",
        text,
    )
    return text


def _run(gate: Gate) -> Gate:
    exe = shutil.which(gate.command[0]) or gate.command[0]
    cmd = [exe, *gate.command[1:]]
    if gate.command[0] == "python":
        cmd = [sys.executable, *gate.command[1:]]
    # Resolve the engine from *this* checkout.  A git worktree usually shares
    # its parent's virtualenv, whose editable install names the main checkout,
    # so `python -m wattmosaic_engine...` would exercise a different tree and
    # the gate could pass against code that is not being verified.
    env = dict(os.environ)
    engine_root = ROOT / "engine"
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        f"{engine_root}{os.pathsep}{existing}" if existing else str(engine_root)
    )
    start = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=gate.timeout,
            check=False,
            env=env,
        )
        gate.returncode = proc.returncode
        combined = (proc.stdout or "") + (proc.stderr or "")
        gate.output_tail = _scrub("\n".join(combined.strip().splitlines()[-25:]))
        gate.status = "PASS" if proc.returncode == 0 else "FAIL"
    except subprocess.TimeoutExpired:
        gate.status = "TIMEOUT"
        gate.returncode = -1
        gate.output_tail = f"timed out after {gate.timeout}s"
    except FileNotFoundError as exc:
        gate.status = "SKIP"
        gate.skipped_reason = str(exc)
    gate.duration_s = round(time.monotonic() - start, 2)
    return gate


def gates() -> list[Gate]:
    return [
        Gate("contract_schema_sync", ["python", "tools/dev/export_schemas.py"]),
        Gate("secret_scan", ["python", "tools/dev/check_secrets.py"]),
        # CI lints before it tests.  Without this gate the harness could report
        # "all required gates pass" for a tree CI then rejects on style, which
        # is exactly what happened to the release-contract test file.
        Gate("lint", ["ruff", "check", "engine", "tools", "tests"]),
        Gate("unit_tests", ["python", "-m", "pytest", "tests/unit", "-q"]),
        Gate("integration_tests", ["python", "-m", "pytest", "tests/integration", "-q"]),
        Gate("simulation_tests", ["python", "-m", "pytest", "tests/simulation", "-q"]),
        Gate("end_to_end_tests", ["python", "-m", "pytest", "tests/end_to_end", "-q"]),
        Gate("manifest_valid", ["python", "tools/dev/check_manifests.py"]),
        Gate("release_valid", ["python", "tools/dev/check_release.py"]),
        Gate("docs_fresh", ["python", "tools/dev/render_validation_docs.py", "--check"]),
        Gate("deployment_valid", ["bash", "scripts/validate_addon.sh", "--no-build"]),
        Gate("hacs_valid", ["python", "tools/dev/check_hacs.py"]),
        Gate("config_valid", ["python", "-m", "wattmosaic_engine.main", "--check-config"],
             required=False, notes="requires HA_TOKEN in environment"),
    ]


def summarize(results: list[Gate]) -> dict:
    required = [g for g in results if g.required]
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "gates": [asdict(g) for g in results],
        "required_total": len(required),
        "required_passed": sum(1 for g in required if g.status == "PASS"),
        "required_failed": [g.name for g in required if g.status == "FAIL"],
        "required_skipped": [g.name for g in required if g.status == "SKIP"],
        "all_required_pass": all(g.status == "PASS" for g in required),
    }


def to_markdown(summary: dict) -> str:
    lines = [
        "# Verification Report",
        "",
        f"Generated: `{summary['generated_at']}`",
        "",
        f"**Required gates passed: {summary['required_passed']}/{summary['required_total']}**",
        "",
        "| Gate | Status | Duration (s) | Notes |",
        "| --- | --- | --- | --- |",
    ]
    for g in summary["gates"]:
        note = g["skipped_reason"] or g["notes"] or ""
        lines.append(f"| {g['name']} | **{g['status']}** | {g['duration_s']} | {note} |")
    lines += ["", "## Failures", ""]
    failures = [g for g in summary["gates"] if g["status"] in ("FAIL", "TIMEOUT")]
    if not failures:
        lines.append("None.")
    for g in failures:
        lines += [
            f"### {g['name']} (exit {g['returncode']})",
            "",
            "```text",
            g["output_tail"],
            "```",
            "",
        ]
    lines += [
        "",
        "## How this is produced",
        "",
        "```bash",
        "python tools/dev/verify.py --json data/verification.json --md docs/verification_report.md",
        "```",
        "",
        "A gate is only `PASS` if the command exited 0 in the orchestrator's own run.",
        "Agent reports are not accepted as evidence.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="data/verification.json")
    ap.add_argument("--md", default="docs/verification_report.md")
    ap.add_argument("--only", nargs="*", default=None)
    args = ap.parse_args()

    results: list[Gate] = []
    for gate in gates():
        if args.only and gate.name not in args.only:
            continue
        print(f"running {gate.name} ...", flush=True)
        results.append(_run(gate))

    summary = summarize(results)
    jpath = ROOT / args.json
    jpath.parent.mkdir(parents=True, exist_ok=True)
    jpath.write_text(json.dumps(summary, indent=2))
    mpath = ROOT / args.md
    mpath.parent.mkdir(parents=True, exist_ok=True)
    mpath.write_text(to_markdown(summary))

    for g in summary["gates"]:
        print(f"  {g['status']:8s} {g['name']} ({g['duration_s']}s)")
    print(f"required gates: {summary['required_passed']}/{summary['required_total']}")
    if summary["required_failed"]:
        print("failed:", ", ".join(summary["required_failed"]))
    return 0 if summary["all_required_pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
