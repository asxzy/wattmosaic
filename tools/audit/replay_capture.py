"""Replay a captured CT/state stream through the production inference path.

This is the bridge between the synthetic simulator and a real installation. The
simulator proves the engine behaves correctly on a house with the *shape* of the
reference install; this tool proves it behaves correctly on that install's
actual measurements. It is the only way to tell a guard that works from a guard
that only satisfies a fixture.

What it does, in order:

1. Load a contract ``Topology`` (the shipped one from the database, or a variant
   with the sub-panel feed declared as nested).
2. Load a captured stream: either ``ct_samples`` rows from the engine database or
   a ``ct_events.jsonl`` written by ``tools/audit/collect_samples.py``.
3. Feed each sample through the **production** path:
   ``Measurement`` -> ``SampleAligner`` -> ``DisaggregationEngine.infer``.
4. Emit a report shaped like ``tests.simulation.metrics.EvaluationReport``.

Two honest limits are stated rather than papered over:

* **There is no ground truth for a real house.** The report therefore carries no
  per-device error statistics: ``per_device`` stays empty and the report says so
  in ``notes``. What *is* computable from measurements alone is reported:
  whole-home power, the reconciliation balance, per-device attribution and the
  nested-double-count invariants. Grading the engine against its own output
  would be the exact failure mode ``docs/testing.md`` warns about.
* **The shipped topology can hang the solver.** On the reference installation
  the six mains (two load-manager legs, two main-panel legs, two sub-panel legs)
  are all declared as independent feeds, so the panel tree is not expressed and
  the bounded least-squares problem is ill-conditioned. The tool enforces a hard
  wall-clock deadline and reports the timeout as a result instead of hanging a
  CI run.

Usage::

    .venv/bin/python tools/audit/replay_capture.py --minutes 60 --topology-nested
    .venv/bin/python tools/audit/replay_capture.py --db data/wattmosaic.db --minutes 30
    .venv/bin/python tools/audit/replay_capture.py \\
        --jsonl data/raw/ct_events.jsonl --topology-as-shipped --out /tmp/replay.json

The database is opened ``mode=ro``: this tool never writes to a capture.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import sys
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "engine"))

DEFAULT_DB = ROOT / "data" / "wattmosaic.db"
DEFAULT_JSONL = ROOT / "data" / "raw" / "ct_events.jsonl"

#: The audited CT cadence.  Used when the capture itself does not supply one.
DEFAULT_CADENCE_S = 15.7


class ReplayTimeout(RuntimeError):
    """Raised when a replay exceeds its hard deadline."""


# --------------------------------------------------------------------------- #
# Hard deadline
# --------------------------------------------------------------------------- #


class _Deadline:
    """A wall-clock deadline that cannot be ignored by a blocking solver.

    Two independent mechanisms, because one is not enough:

    * ``SIGALRM`` interrupts Python bytecode in the main thread.  ``scipy``'s
      bounded solver releases the GIL but still returns to the interpreter
      between iterations, so this fires in practice.
    * A watchdog thread calls ``os._exit`` if the signal did not stop the
      process.  A solver wedged inside a C loop can swallow the signal, and a CI
      job must still terminate.  This is deliberately blunt: the process is
      about to be killed, so there is nothing to clean up.

    ``armed`` is a no-op on platforms without ``SIGALRM``; the watchdog still
    applies.
    """

    def __init__(self, seconds: float, *, hard_exit: bool = True) -> None:
        self.seconds = float(seconds)
        self.hard_exit = hard_exit
        self._watchdog: threading.Timer | None = None
        self._previous: Any = None
        self._armed = False

    def _on_alarm(self, signum: int, frame: Any) -> None:  # noqa: ARG002
        raise ReplayTimeout(f"replay exceeded {self.seconds:.0f}s")

    def _on_watchdog(self) -> None:
        if self.hard_exit:
            print(
                f"replay_capture: hard deadline {self.seconds:.0f}s exceeded and "
                "SIGALRM did not interrupt the solver; exiting 124",
                file=sys.stderr,
                flush=True,
            )
            os._exit(124)
        raise ReplayTimeout(f"replay exceeded {self.seconds:.0f}s")

    def __enter__(self) -> _Deadline:
        if self.seconds <= 0:
            return self
        if hasattr(signal, "SIGALRM") and threading.current_thread() is threading.main_thread():
            self._previous = signal.signal(signal.SIGALRM, self._on_alarm)
            signal.setitimer(signal.ITIMER_REAL, self.seconds)
            self._armed = True
        self._watchdog = threading.Timer(self.seconds + 1.0, self._on_watchdog)
        self._watchdog.daemon = True
        self._watchdog.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._armed:
            signal.setitimer(signal.ITIMER_REAL, 0.0)
            if self._previous is not None:
                signal.signal(signal.SIGALRM, self._previous)
            self._armed = False
        if self._watchdog is not None:
            self._watchdog.cancel()
            self._watchdog = None


# --------------------------------------------------------------------------- #
# Capture loading
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class CaptureSample:
    """One power observation, already mapped to a topology ct-id."""

    ct_id: str
    entity_id: str
    value_w: float
    state_timestamp: datetime
    received_at: datetime
    quality: str = "good"
    repeated: bool = False
    sequence: int = 0


@dataclass(slots=True)
class Capture:
    samples: list[CaptureSample]
    source: str
    window_start: datetime | None = None
    window_end: datetime | None = None
    notes: list[str] = field(default_factory=list)


def _as_utc(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def aligner_key(entity_id: str) -> str:
    """The key the engine's aligner uses for a channel.

    Production maps a CT's ``entity_id`` to a window key by dropping the domain
    prefix (``sensor.panel_a_ct1_power`` -> ``panel_a_ct1_power``).  A capture
    may store either the entity id or that key, so every lookup goes through
    this one function.
    """
    text = str(entity_id or "")
    return text.split(".", 1)[-1] if "." in text else text


def load_topology(db_path: Path, *, nested: bool) -> Any:
    """Load the shipped contract topology, optionally with nesting declared.

    ``nested=False`` returns the topology exactly as stored -- the "as shipped"
    variant.  ``nested=True`` declares the wiring the engine's own nesting
    detector proposes from the same measurements (see
    ``tools/dev/replay_nesting.py``): the sub-panel feed is a child of the load
    manager, and the main panel feed is the same conductors as the load manager
    (an alias), not a second independent feed.  This is a *hypothesis under
    test*, not a fact written back to the database.
    """
    from wattmosaic_engine.contracts import Topology

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT payload_json FROM topology_versions ORDER BY version DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise SystemExit(f"no topology_versions row in {db_path}")
    topology = Topology.model_validate(json.loads(row[0]))
    if not nested:
        return topology

    # Structure proposed by infer_nesting on 120 min of this capture:
    #   sub_panel_l1 -> load_manager_l1, sub_panel_l2 -> load_manager_l2 (0.9979
    #   containment, one direction only)
    #   main_panel_l1/l2 are a duplicate feed of load_manager_l1/l2 (median
    #   relative difference 0.117, activity ratio 1.0)
    # Applied by *structure* (main feed whose name marks a sub panel), not by a
    # hard-coded id list, so the same rule works on another installation.
    channels = []
    sub_ids = [
        ch.ct_id
        for ch in topology.channels
        if "sub" in str(ch.name or "").lower() or "sub" in str(ch.ct_id).lower()
    ]
    for ch in topology.channels:
        ctype = str(getattr(ch.ct_type, "value", ch.ct_type))
        update: dict[str, Any] = {}
        if ctype == "main" and ch.ct_id in sub_ids:
            phase = str(getattr(ch.phase, "value", ch.phase))
            parent = _pick_parent(topology, phase=phase, exclude=set(sub_ids))
            if parent is not None:
                update["parent_ct_id"] = parent
        elif ctype == "main" and ch.ct_id not in sub_ids:
            # A main feed that another main already measures is an alias, not a
            # parallel feed.  Only the load manager carries the service entry on
            # this install; the panel monitors are duplicates of it.
            if "load" in str(ch.name or "").lower():
                channels.append(ch)
                continue
            twin = _pick_parent(topology, phase=str(getattr(ch.phase, "value", ch.phase)),
                                exclude=set(sub_ids) | {ch.ct_id})
            if twin is not None:
                update["alias_of_ct_id"] = twin
        channels.append(ch.model_copy(update=update) if update else ch)
    return topology.model_copy(update={"channels": channels})


def _pick_parent(topology: Any, *, phase: str, exclude: set[str]) -> str | None:
    """The load-manager main on the same phase, if the topology has one."""
    for ch in topology.channels:
        ctype = str(getattr(ch.ct_type, "value", ch.ct_type))
        if ctype != "main" or ch.ct_id in exclude:
            continue
        if "load" not in str(ch.name or "").lower():
            continue
        if str(getattr(ch.phase, "value", ch.phase)) == phase:
            return ch.ct_id
    return None


def load_capture_db(
    db_path: Path, *, minutes: float, entity_to_ct: dict[str, str]
) -> Capture:
    """Read the tail of ``ct_samples`` from the engine database (read-only)."""
    if not db_path.exists():
        raise SystemExit(f"no database at {db_path}")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        end = _as_utc(
            conn.execute("SELECT MAX(received_at) FROM ct_samples").fetchone()[0]
        )
        if end is None:
            raise SystemExit(f"ct_samples is empty in {db_path}")
        start = end - timedelta(minutes=float(minutes))
        rows = conn.execute(
            "SELECT ct_id, entity_id, value_w, state_timestamp, received_at, "
            "quality, repeated, sequence FROM ct_samples "
            "WHERE received_at >= ? AND received_at <= ? ORDER BY received_at, id",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    finally:
        conn.close()

    samples: list[CaptureSample] = []
    unmapped: dict[str, int] = defaultdict(int)
    for ct_key, entity_id, value, state_ts, received_at, quality, repeated, seq in rows:
        ct_id = entity_to_ct.get(str(entity_id)) or entity_to_ct.get(str(ct_key))
        state_dt = _as_utc(state_ts)
        recv_dt = _as_utc(received_at)
        if ct_id is None or state_dt is None or recv_dt is None:
            unmapped[str(ct_key)] += 1
            continue
        samples.append(
            CaptureSample(
                ct_id=ct_id,
                entity_id=str(entity_id),
                value_w=float(value),
                state_timestamp=state_dt,
                received_at=recv_dt,
                quality=str(quality or "good"),
                repeated=bool(repeated),
                sequence=int(seq or 0),
            )
        )
    notes = []
    if unmapped:
        top = sorted(unmapped.items(), key=lambda kv: -kv[1])[:5]
        notes.append(
            f"{sum(unmapped.values())} rows skipped: channel not in topology "
            f"(e.g. {', '.join(f'{k} x{v}' for k, v in top)})"
        )
    return Capture(samples=samples, source=str(db_path), window_start=start,
                   window_end=end, notes=notes)


def load_capture_jsonl(
    path: Path, *, minutes: float, entity_to_ct: dict[str, str]
) -> Capture:
    """Read ``ct_events.jsonl`` (the collector's output format)."""
    if not path.exists():
        raise SystemExit(f"no capture at {path}")
    raw: list[tuple[str, str, float, str, str]] = []
    for line in path.read_text().splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("type") != "state":
            continue
        try:
            value = float(rec.get("state"))
        except (TypeError, ValueError):
            continue
        raw.append(
            (
                str(rec.get("entity_id") or ""),
                str(rec.get("state")),
                value,
                str(rec.get("last_updated") or rec.get("received_at") or ""),
                str(rec.get("received_at") or ""),
            )
        )
    if not raw:
        raise SystemExit(f"no state records in {path}")

    parsed: list[CaptureSample] = []
    unmapped: dict[str, int] = defaultdict(int)
    for entity_id, _state, value, state_ts, received_at in raw:
        ct_id = entity_to_ct.get(entity_id)
        state_dt = _as_utc(state_ts)
        recv_dt = _as_utc(received_at) or state_dt
        if ct_id is None or state_dt is None or recv_dt is None:
            unmapped[entity_id] += 1
            continue
        parsed.append(
            CaptureSample(
                ct_id=ct_id,
                entity_id=entity_id,
                value_w=value,
                state_timestamp=state_dt,
                received_at=recv_dt,
                quality="good",
            )
        )
    parsed.sort(key=lambda s: s.received_at)
    end = parsed[-1].received_at
    start = end - timedelta(minutes=float(minutes))
    samples = [s for s in parsed if s.received_at >= start]
    notes = []
    if unmapped:
        top = sorted(unmapped.items(), key=lambda kv: -kv[1])[:5]
        notes.append(
            f"{sum(unmapped.values())} rows skipped: channel not in topology "
            f"(e.g. {', '.join(f'{k} x{v}' for k, v in top)})"
        )
    return Capture(samples=samples, source=str(path), window_start=start,
                   window_end=end, notes=notes)


# --------------------------------------------------------------------------- #
# Replay
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class ReplayResult:
    topology_variant: str
    n_samples: int
    n_windows: int
    n_records: int
    main_power_w: float
    residual_w: float
    accounted_w: float
    feeds_used: list[str]
    sum_of_all_mains_w: float
    per_device_power_w: dict[str, float]
    per_source: dict[str, float]
    notes: list[str] = field(default_factory=list)
    timed_out: bool = False
    elapsed_s: float = 0.0
    invariants: dict[str, Any] = field(default_factory=dict)
    reconciliation: dict[str, Any] = field(default_factory=dict)
    coverage: dict[str, Any] = field(default_factory=dict)


def _measurement(sample: CaptureSample) -> Any:
    from wattmosaic_engine.contracts import Measurement, QualityStatus

    try:
        quality = QualityStatus(str(sample.quality))
    except ValueError:
        quality = QualityStatus.GOOD
    return Measurement(
        entity_id=sample.entity_id,
        ct_id=sample.ct_id,
        value=float(sample.value_w),
        unit="W",
        state_timestamp=sample.state_timestamp,
        received_at=sample.received_at,
        quality=quality,
        repeated=bool(sample.repeated),
        sequence=int(sample.sequence or 0),
    )


def replay(
    capture: Capture,
    topology: Any,
    *,
    variant: str,
    max_age_s: float = 300.0,
    cadence_s: float = DEFAULT_CADENCE_S,
    tick_s: float | None = None,
    states: dict[str, Any] | None = None,
) -> ReplayResult:
    """Feed a capture through ``Measurement`` -> ``SampleAligner`` -> engine.

    The engine is called once per **inference tick**, not once per sample.
    Production runs the solver on a cadence; calling it once per arriving sample
    would both misrepresent the workload (the aligner is a point-in-time view,
    so consecutive samples of one channel add nothing) and take hours on a
    day-long capture.  ``tick_s`` defaults to the audited CT cadence.
    """
    from wattmosaic_engine.disaggregation import DisaggregationEngine
    from wattmosaic_engine.electrical.topology import build_graph
    from wattmosaic_engine.synchronization.aligner import SampleAligner

    started = time.monotonic()
    tick = float(tick_s if tick_s is not None else cadence_s)
    graph = build_graph(topology)
    feeds = list(graph.effective_feeds())
    all_mains = [
        ch.ct_id
        for ch in topology.channels
        if str(getattr(ch.ct_type, "value", ch.ct_type)) == "main"
        and bool(getattr(ch, "enabled", True))
    ]
    panel = _panel_map(topology)

    aligner = SampleAligner(max_age_s=max_age_s)
    engine = DisaggregationEngine()
    device_power: dict[str, float] = defaultdict(float)
    source_power: dict[str, float] = defaultdict(float)
    main_values: list[float] = []
    residuals: list[float] = []
    accounted_values: list[float] = []
    nested_checked = 0
    nested_double = 0
    negative_residual = 0
    notes: list[str] = []

    samples = capture.samples
    start = capture.window_start or samples[0].received_at
    end = capture.window_end or samples[-1].received_at
    ticks: list[datetime] = []
    cursor = start
    while cursor <= end:
        ticks.append(cursor)
        cursor += timedelta(seconds=tick)
    if not ticks or ticks[-1] < end:
        ticks.append(end)

    n_windows = 0
    index = 0
    for at in ticks:
        # Feed everything that had arrived by this tick, in capture order.
        while index < len(samples) and samples[index].received_at <= at:
            aligner.add(_measurement(samples[index]))
            index += 1
        window = aligner.window(at=at, max_age_s=max_age_s)
        if not any(ch.value_w is not None for ch in window.channels.values()):
            continue
        n_windows += 1
        batch = engine.infer(
            window,
            device_states=dict(states or {}),
            models=None,
            topology=topology,
        )
        rec = batch.reconciliation
        main_values.append(float(rec.main_power_w))
        residuals.append(float(rec.unknown_residual_w))
        accounted_values.append(float(rec.main_power_w) - float(rec.unknown_residual_w))
        for result in batch.results:
            device_power[result.device_id] += float(result.power_w)
            source_power[str(result.source)] += float(result.power_w)
        # Measurement-side nested check: the same definition metrics.evaluate
        # uses, minus the truth comparison a real house cannot provide.
        if panel["root"] and panel["sub"]:
            root_sum = sum(
                float(window.channels[cid].value_w)
                for cid in panel["root"]
                if cid in window.channels and window.channels[cid].value_w
            )
            sub_sum = sum(
                float(window.channels[cid].value_w)
                for cid in panel["sub"]
                if cid in window.channels and window.channels[cid].value_w
            )
            if sub_sum > max(0.03 * root_sum, 50.0):
                nested_checked += 1
                if abs(float(rec.main_power_w) - (root_sum + sub_sum)) <= max(
                    0.02 * sub_sum, 25.0
                ):
                    nested_double += 1
        if accounted_values[-1] > float(rec.main_power_w) + max(
            0.02 * abs(float(rec.main_power_w)), 5.0
        ):
            negative_residual += 1

    # "Sum of all mains" is a real measurement from the final window, not a mean
    # of means.
    final_window = aligner.window(at=end, max_age_s=max_age_s)
    all_mains_values = [
        float(final_window.channels[cid].value_w)
        for cid in all_mains
        if cid in final_window.channels and final_window.channels[cid].value_w is not None
    ]

    n = max(1, len(main_values))
    elapsed = time.monotonic() - started
    if not main_values:
        notes.append("no inference tick had a usable measurement; nothing was inferred")
    return ReplayResult(
        topology_variant=variant,
        n_samples=len(capture.samples),
        n_windows=n_windows,
        n_records=n_windows,
        main_power_w=sum(main_values) / n if main_values else 0.0,
        residual_w=sum(residuals) / n if residuals else 0.0,
        accounted_w=sum(accounted_values) / n if accounted_values else 0.0,
        feeds_used=feeds,
        sum_of_all_mains_w=sum(all_mains_values),
        per_device_power_w={k: v / n for k, v in sorted(device_power.items())},
        per_source={k: v / n for k, v in sorted(source_power.items())},
        notes=notes + list(capture.notes),
        elapsed_s=elapsed,
        invariants={
            "nested_check_records": nested_checked,
            "nested_double_count_records": nested_double,
            "negative_residual_records": negative_residual,
            "checked_records": n_windows,
            # Per-device error is not computable: a real house has no truth.
            "per_device_error_available": False,
        },
        reconciliation={
            "n": n_windows,
            "main_power_w": sum(main_values) / n if main_values else 0.0,
            "unknown_residual_w": sum(residuals) / n if residuals else 0.0,
            "accounted_w": sum(accounted_values) / n if accounted_values else 0.0,
        },
        coverage={
            "n": n_windows,
            "attributed_w": sum(accounted_values) / n if accounted_values else 0.0,
            "coverage": (
                (sum(accounted_values) / sum(main_values))
                if sum(main_values)
                else 0.0
            ),
            "unknown_load_pct": (
                (sum(residuals) / sum(main_values)) if sum(main_values) else 0.0
            ),
            "false_attribution_rate": 0.0,
        },
    )


def _panel_map(topology: Any) -> dict[str, list[str]]:
    """Root/sub panel feeds, reusing the harness definition verbatim."""
    from tests.simulation.metrics import panel_channel_map

    return panel_channel_map(topology)


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #


def evaluation_shape(result: ReplayResult, *, topology_version: int) -> dict[str, Any]:
    """``EvaluationReport.as_dict()``-shaped payload, with honest gaps.

    Every top-level key the harness emits is present so a consumer can treat a
    real replay and a simulated run with the same code.  The strata that require
    ground truth are empty and ``notes`` says why: an empty dict is a truthful
    "not measurable here", while a filled one would be the engine grading
    itself.
    """
    return {
        "n_records": result.n_records,
        "scenario": {
            "seed": None,
            "minutes": None,
            "interval_s": DEFAULT_CADENCE_S,
            "quality": None,
            "source": "real capture replay",
            "topology_variant": result.topology_variant,
            "topology_version": topology_version,
        },
        "per_device": {},
        "per_device_detection": {},
        "per_source": {},
        "per_source_detection": {},
        "per_device_type": {},
        "per_concurrency": {},
        "per_quality": {},
        "reconciliation": result.reconciliation,
        "coverage": result.coverage,
        "invariants": result.invariants,
        "notes": [
            "per_device error statistics are empty: a real installation has no "
            "independent ground truth. Grading against the engine's own output "
            "would prove nothing (docs/testing.md §1).",
            *result.notes,
        ],
        "replay": {
            "n_samples": result.n_samples,
            "n_windows": result.n_windows,
            "main_power_w": result.main_power_w,
            "residual_w": result.residual_w,
            "accounted_w": result.accounted_w,
            "feeds_used": result.feeds_used,
            "sum_of_all_mains_w": result.sum_of_all_mains_w,
            "per_device_power_w": result.per_device_power_w,
            "per_source_power_w": result.per_source,
            "elapsed_s": round(result.elapsed_s, 3),
            "timed_out": result.timed_out,
        },
    }


def _print_report(result: ReplayResult, *, delta: dict[str, Any]) -> None:
    print(f"topology variant      : {result.topology_variant}")
    print(f"windows / samples     : {result.n_windows} / {result.n_samples}")
    print(f"feeds used            : {', '.join(result.feeds_used) or '(none)'}")
    print(f"main_power_w          : {result.main_power_w:.1f}")
    print(f"sum of all mains (W)  : {result.sum_of_all_mains_w:.1f}")
    print(f"accounted_w           : {result.accounted_w:.1f}")
    print(f"residual_w            : {result.residual_w:.1f}")
    print(f"nested check records  : {result.invariants['nested_check_records']} "
          f"(double counts: {result.invariants['nested_double_count_records']})")
    print(f"elapsed               : {result.elapsed_s:.2f}s")
    if result.per_device_power_w:
        print("per-device power (W):")
        for device_id, watts in result.per_device_power_w.items():
            print(f"  {device_id:44s} {watts:9.1f}")
    print(f"nested-vs-shipped     : {delta.get('summary', 'unavailable')}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=None, help="engine SQLite database (read-only)")
    ap.add_argument("--jsonl", type=Path, default=None, help="captured ct_events.jsonl")
    ap.add_argument("--minutes", type=float, default=60.0, help="window length from the capture end")
    ap.add_argument(
        "--topology-as-shipped",
        dest="variant",
        action="store_const",
        const="as-shipped",
        help="use the stored topology exactly as shipped (default)",
    )
    ap.add_argument(
        "--topology-nested",
        dest="variant",
        action="store_const",
        const="nested",
        help="declare the sub-panel feed as nested under the service feed",
    )
    ap.add_argument("--out", type=Path, default=None, help="write the report JSON here")
    ap.add_argument(
        "--timeout-s",
        type=float,
        default=120.0,
        help="hard wall-clock deadline for the whole run (never hangs CI)",
    )
    ap.add_argument(
        "--no-compare",
        action="store_true",
        help="skip the nested-vs-shipped comparison run",
    )
    ap.add_argument(
        "--compare-timeout-s",
        type=float,
        default=20.0,
        help="deadline for the comparison run only",
    )
    ap.add_argument("--max-age-s", type=float, default=300.0, help="aligner staleness bound")
    args = ap.parse_args()

    ap.set_defaults(variant="as-shipped")
    variant = getattr(args, "variant", None) or "as-shipped"

    db_path = args.db
    jsonl_path = args.jsonl
    if db_path is None and jsonl_path is None:
        db_path = DEFAULT_DB if DEFAULT_DB.exists() else None
        jsonl_path = None if db_path else (DEFAULT_JSONL if DEFAULT_JSONL.exists() else None)
    if db_path is None and jsonl_path is None:
        print(
            "replay_capture: no capture available. Pass --db <sqlite> or --jsonl <file>; "
            f"neither {DEFAULT_DB} nor {DEFAULT_JSONL} exists.",
            file=sys.stderr,
        )
        return 2
    if db_path is not None and not db_path.exists():
        print(f"replay_capture: no database at {db_path}", file=sys.stderr)
        return 2
    if jsonl_path is not None and not jsonl_path.exists():
        print(f"replay_capture: no capture at {jsonl_path}", file=sys.stderr)
        return 2

    # The topology always comes from the database: a JSONL capture does not
    # carry channel wiring.
    topology_db = db_path or DEFAULT_DB
    if not topology_db.exists():
        print(
            "replay_capture: a --jsonl capture needs --db to supply the topology "
            f"({topology_db} does not exist)",
            file=sys.stderr,
        )
        return 2

    started = time.monotonic()
    with _Deadline(args.timeout_s):
        shipped = load_topology(topology_db, nested=False)
        entity_to_ct = {str(ch.entity_id): ch.ct_id for ch in shipped.channels}
        entity_to_ct.update({aligner_key(ch.entity_id): ch.ct_id for ch in shipped.channels})

        try:
            if db_path is not None:
                capture = load_capture_db(db_path, minutes=args.minutes,
                                          entity_to_ct=entity_to_ct)
            else:
                capture = load_capture_jsonl(jsonl_path, minutes=args.minutes,
                                             entity_to_ct=entity_to_ct)
        except ReplayTimeout:
            print(f"replay_capture: loading exceeded {args.timeout_s:.0f}s", file=sys.stderr)
            return 124

        if not capture.samples:
            print(
                "replay_capture: no samples mapped into the topology window "
                f"({args.minutes} min). Nothing to replay.",
                file=sys.stderr,
            )
            return 3

        topology = load_topology(topology_db, nested=(variant == "nested"))
        try:
            result = replay(
                capture, topology, variant=variant, max_age_s=args.max_age_s
            )
        except ReplayTimeout as exc:
            print(
                f"replay_capture: {variant} replay aborted after "
                f"{time.monotonic() - started:.1f}s: {exc}",
                file=sys.stderr,
            )
            return 124

        delta: dict[str, Any] = {"summary": "skipped (--no-compare)"}
        if not args.no_compare:
            other_variant = "nested" if variant == "as-shipped" else "as-shipped"
            other_topology = load_topology(topology_db, nested=(other_variant == "nested"))
            remaining = min(args.compare_timeout_s, args.timeout_s - (time.monotonic() - started))
            try:
                with _Deadline(max(1.0, remaining), hard_exit=False):
                    other = replay(
                        capture, other_topology, variant=other_variant,
                        max_age_s=args.max_age_s,
                    )
            except ReplayTimeout:
                delta = {
                    "summary": (
                        f"{other_variant} run exceeded {max(1.0, remaining):.0f}s and was "
                        "abandoned; the shipped topology does not terminate on this "
                        "capture (defect: independent mains instead of a nested tree)"
                    ),
                    "other_variant": other_variant,
                    "timed_out": True,
                }
            else:
                delta = _compare(result, other)

        payload = evaluation_shape(result, topology_version=int(getattr(topology, "version", 0)))
        payload["delta_vs_other_topology"] = delta
        if args.out is not None:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(payload, indent=2) + "\n")
            print(f"wrote {args.out}")

    _print_report(result, delta=delta)
    if delta.get("timed_out"):
        print(
            "replay_capture: NOTE the comparison run did not terminate; see "
            "delta_vs_other_topology.summary",
            file=sys.stderr,
        )
        return 124
    return 0


def _compare(primary: ReplayResult, other: ReplayResult) -> dict[str, Any]:
    """Nested-vs-shipped difference, expressed in watts and as a ratio."""
    nested, shipped = (
        (primary, other) if primary.topology_variant == "nested" else (other, primary)
    )
    shipped_main = shipped.main_power_w
    nested_main = nested.main_power_w
    delta_w = shipped_main - nested_main
    ratio = (shipped_main / nested_main) if nested_main else 0.0
    return {
        "shipped_main_power_w": round(shipped_main, 3),
        "nested_main_power_w": round(nested_main, 3),
        "delta_w": round(delta_w, 3),
        "ratio": round(ratio, 4),
        "shipped_feeds": shipped.feeds_used,
        "nested_feeds": nested.feeds_used,
        "agreement_within_5pct": bool(
            nested_main and abs(delta_w) <= 0.05 * abs(nested_main)
        ),
        "summary": (
            f"shipped {shipped_main:.1f} W vs nested {nested_main:.1f} W "
            f"(delta {delta_w:+.1f} W, ratio {ratio:.3f})"
        ),
    }


if __name__ == "__main__":
    raise SystemExit(main())
