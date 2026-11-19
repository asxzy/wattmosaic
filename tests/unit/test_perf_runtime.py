"""V2a — the synthetic perf benchmark, run in ``--quick`` mode with gates off.

This test asserts only the *shape* of the report, not wall-clock budgets: shared
CI runners cannot deliver a meaningful p99.  It proves the tool drives the real
runtime (fast path, event loop, HTTP probes, background jobs, matrix) and emits
every metric the deployment report consumes.  It stays well under 30 s.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_perf_runtime_quick_report_shape(tmp_path: Path) -> None:
    """``--quick --no-gate`` produces the full, JSON-shaped report and exits 0."""
    script = REPO_ROOT / "tools" / "dev" / "perf_runtime.py"
    out = tmp_path / "perf.json"
    proc = subprocess.run(
        [
            sys.executable, str(script),
            "--quick", "--no-gate", "--json-out", str(out),
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert out.exists(), proc.stdout[-2000:]
    report = json.loads(out.read_text())

    # Every reported section is present and well-formed.
    assert set(report) >= {
        "scale", "fast_path_us_per_sample", "event_loop_ms", "healthz_ms",
        "background_jobs", "db_growth_bytes_per_day",
        "db_growth_by_table_bytes_per_day", "matrix", "gates",
    }
    scale = report["scale"]
    assert scale["readers"] == 40
    assert scale["ha_entities"] == 500
    assert scale["events_per_s"] == 30.0
    assert scale["duration_s"] == 60.0
    assert scale["samples"] > 0 and scale["event_cycles"] > 0
    # Z8: evidence rows stay bounded, never one per delivered sample, and
    # windows that closed are finalized in place.
    assert scale["event_evidence_rows"] < scale["samples"] / 10
    # A 60 s quick run may end before the 120 s post-window closes, so the count
    # may be 0 here; finalization-on-close is asserted in the unit/integration
    # tests instead.
    assert 0 <= scale["event_evidence_finalized_rows"] <= scale["event_evidence_rows"]

    fast = report["fast_path_us_per_sample"]
    assert set(fast) >= {"p50_us", "p95_us", "p99_us", "p50_ms", "p95_ms", "p99_ms"}
    assert 0 < fast["p50_us"] <= fast["p95_us"] <= fast["p99_us"]
    for key in ("event_loop_ms", "healthz_ms"):
        summary = report[key]
        assert set(summary) >= {"p50_ms", "p95_ms", "p99_ms", "count"}
        assert summary["p50_ms"] <= summary["p95_ms"] <= summary["p99_ms"]
        assert summary["count"] > 0

    assert isinstance(report["background_jobs"], dict)
    # V2: a timing wrapper once hid each job's signature from the executor's
    # ``budget=`` probe, so every background job raised TypeError and its
    # "duration" was the time to fail.  The jobs must actually run.
    assert scale["background_jobs_failed"] == 0
    assert report["db_growth_bytes_per_day"] >= 0
    # Per-table growth is measured after the matrix seed (fixture, not growth).
    per_table = report["db_growth_by_table_bytes_per_day"]
    assert isinstance(per_table, dict)
    assert all(isinstance(v, (int, float)) and v >= 0 for v in per_table.values())

    matrix = report["matrix"]
    assert matrix["devices"] == 500 and matrix["readers"] == 100
    assert matrix["payload_cells"] == 50_000
    assert matrix["get_p95_ms"] >= 0
    assert matrix["payload_build_ms"]["p95_ms"] >= 0

    gates = report["gates"]
    assert set(gates) >= {
        "healthz_p99_ms", "healthz_p99_limit_ms", "healthz_p99_ok",
        "fast_path_p99_ms", "fast_path_limit_ms", "fast_path_ok",
    }
    assert gates["healthz_p99_limit_ms"] == 100.0
    assert gates["fast_path_limit_ms"] == 0.2
    assert report["gates_enabled"] is False

    # Human text is on stdout as well as JSON on disk.
    assert "fast path" in proc.stdout and "/healthz" in proc.stdout


def test_perf_runtime_module_builds_a_contract_valid_matrix() -> None:
    """The 500x100 payload is built through the real v3 matrix contracts."""
    sys.path.insert(0, str(REPO_ROOT))
    from wattmosaic_engine.contracts import MatrixPayload

    from tools.dev import perf_runtime

    payload = perf_runtime.build_matrix_payload(500, 100)
    assert isinstance(payload, MatrixPayload)
    assert len(payload.cells) == 50_000
    assert len(payload.columns) == 100 and len(payload.rows) == 500
    assert len({node.reader_id for node in payload.columns}) == 100


def test_timed_jobs_keep_their_signature_for_the_budget_probe() -> None:
    """V2: a timed job without ``budget`` must not be handed ``budget=``.

    The quick run ends before the 60 s jobs fire, so the full-scale defect
    (every background job raising ``TypeError``) is pinned here directly.
    """
    sys.path.insert(0, str(REPO_ROOT))
    from wattmosaic_engine.core.executors import BackgroundExecutor

    from tools.dev import perf_runtime

    calls: list[str] = []

    def prune(now: object = None) -> None:  # no ``budget`` parameter
        calls.append("ran")

    sink: dict[str, list[float]] = {}
    executor = BackgroundExecutor()
    try:
        executor.submit(perf_runtime.timed_job("prune", prune, sink)).result(timeout=5)
    finally:
        executor.shutdown(wait=True)
    assert calls == ["ran"]
    assert executor.failed == 0
    assert len(sink["prune"]) == 1
