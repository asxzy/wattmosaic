"""Guard the post-deploy smoke test contract (TASK-DEPLOY-003 / REQ-DEPLOY-005).

The smoke script is the last gate of the unified workflow. A structural or
silent-no-op regression there (missing check, swallowed failure) would let a
broken deploy report success, so this file pins both its shape and its actual
exit behaviour against a stub server.
"""

from __future__ import annotations

import os
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SMOKE = ROOT / "scripts" / "smoke_test.sh"
WORKFLOW = ROOT / "scripts" / "run_workflow.sh"
PYTHON = ROOT / ".venv" / "bin" / "python"

REQUIRED_CHECKS = (
    "health",
    "status",
    "panel",
    "migrations",
    "residuals",
    "matrix",
    "snapshot",
    "topology",
    "devices",
    "metrics",
)


def _run(args: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, check=False, env=env)


def test_smoke_script_exists_and_is_executable() -> None:
    assert SMOKE.is_file(), SMOKE
    assert os.access(SMOKE, os.X_OK), f"{SMOKE} is not executable"


def test_smoke_script_passes_bash_syntax_check() -> None:
    result = _run(["bash", "-n", str(SMOKE)])
    assert result.returncode == 0, result.stderr


def test_workflow_script_exists_executable_and_parses() -> None:
    assert WORKFLOW.is_file(), WORKFLOW
    assert os.access(WORKFLOW, os.X_OK), f"{WORKFLOW} is not executable"
    result = _run(["bash", "-n", str(WORKFLOW)])
    assert result.returncode == 0, result.stderr


def test_smoke_registers_every_required_check_and_fails_on_any() -> None:
    """Each required check is defined AND invoked; failures are never swallowed."""
    script = SMOKE.read_text()
    for name in REQUIRED_CHECKS:
        assert f"check_{name}()" in script, f"missing definition for check {name}"
        assert f"  check_{name}\n" in script, f"check {name} is not invoked by main()"
    # The aggregate is non-zero when any check failed, and curl -f turns HTTP
    # errors into failures rather than silent successes.
    assert "FAILURES=$((FAILURES + 1))" in script
    assert "return 1" in script
    assert "-f" in script, "curl must fail on HTTP >= 400"
    # Distinct from the watchdog probe: it checks the full API surface, not
    # just the gateway's /healthz liveness endpoint.
    assert "/api/v1/health" in script
    assert "/api/v1/residuals" in script
    assert "/api/v1/matrix" in script
    assert "/api/v1/snapshot" in script
    assert "/api/v1/topology/v3" in script
    # The legacy routes were removed at the 3.0 cutover.
    assert "/api/v1/mapping" not in script
    assert "/api/v1/topology/tree" not in script
    assert "/api/v1/metrics" in script


class _StubHandler(BaseHTTPRequestHandler):
    """Minimal engine stub returning a valid payload for every smoke check."""

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        payloads: dict[str, tuple[str, bytes]] = {
            "/api/v1/health": ("application/json", b'{"status":"ok"}'),
            "/api/v1/status": ("application/json", b'{"status":"ok"}'),
            "/": ("text/html", b"<!doctype html><html>WattMosaic panel</html>"),
            "/api/v1/residuals": ("application/json", b"{}"),
            "/api/v1/matrix": ("application/json", b"{}"),
            "/api/v1/snapshot": ("application/json", b'{"device_estimates":[]}'),
            "/api/v1/topology/v3": ("application/json", b"{}"),
            "/api/v1/devices": ("application/json", b"[]"),
            "/api/v1/metrics": ("text/plain", b"wattmosaic_queue_depth 0\n"),
        }
        content_type, body = payloads.get(self.path, ("text/plain", b"{}"))
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        return  # quiet


def _closed_port() -> int:
    """Reserve then release a port so it is reliably closed for the test."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _env_with_db(db_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["WM_DATABASE_PATH"] = str(db_path)
    return env


def test_smoke_passes_when_all_checks_succeed(tmp_path: Path) -> None:
    """All checks green against a stub -> exit 0 (no false negatives)."""
    if not PYTHON.is_file():  # pragma: no cover - environment guard
        return
    # A real database at the latest schema so the local migration check passes.
    db_path = tmp_path / "wattmosaic.db"
    make_db = _run(
        [
            str(PYTHON),
            "-c",
            "import sys; sys.path.insert(0, 'engine'); "
            "from wattmosaic_engine.storage.database import Database; Database(sys.argv[1])",
            str(db_path),
        ]
    )
    assert make_db.returncode == 0, make_db.stderr

    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        result = _run(
            [str(SMOKE), f"http://{host}:{port}"],
            env=_env_with_db(db_path),
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    for name in REQUIRED_CHECKS:
        assert f"PASS  {name}" in result.stdout, f"check {name} did not report PASS"


def test_smoke_exits_non_zero_when_checks_fail(tmp_path: Path) -> None:
    """A dead endpoint makes every check FAIL and the aggregate exit non-zero."""
    if not PYTHON.is_file():  # pragma: no cover - environment guard
        return
    port = _closed_port()
    result = _run(
        [str(SMOKE), f"http://127.0.0.1:{port}"],
        env=_env_with_db(tmp_path / "missing.db"),
    )
    assert result.returncode != 0, "smoke must not swallow failures"
    combined = result.stdout + result.stderr
    assert "FAIL  health" in combined
    assert "check(s) FAILED" in combined
