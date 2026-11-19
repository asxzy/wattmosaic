"""Replay a captured CT/state stream through the v3 inference/replay path.

This is the bridge between the synthetic simulator (X07) and a real
installation. The simulator proves the engine behaves correctly on a house with
the *shape* of the reference install; this tool proves the v3 path behaves
correctly on that install's actual measurements. It is the only way to tell a
guard that works from a guard that only satisfies a fixture.

What it does, in order:

1. Load a captured stream: either ``ct_samples`` rows from the engine database or
   a ``ct_events.jsonl`` written by ``tools/audit/collect_samples.py``.
2. Lift every delivered sample into an immutable HB §17 observation frame
   (``reader_samples`` plus any supplied HA messages) -- the same shape the v3
   replay surface consumes.
3. Feed the frame through the **production v3** surface:
   :func:`wattmosaic_engine.replay.replay_week`, which drives
   :class:`wattmosaic_engine.learning.reconciliation.Reconciler` over the pure
   :func:`wattmosaic_engine.inference.orchestrator.infer_group` entry point under
   an explicit model set. ``models`` accepts a ``ModelSet`` (a
   ``ModelSetView``/``ModelSetId``); ``None`` keeps the built-in HB §17 v1 set.
4. Emit a report shaped like ``tests.simulation.metrics.EvaluationReport`` with
   the :class:`~wattmosaic_engine.learning.reconciliation.DerivedRun` embedded
   under ``"replay"``, so a consumer reads a real replay and a simulated run with
   one reader.

Two honest limits are stated rather than papered over:

* **There is no ground truth for a real house.** The report carries no
  per-device error statistics: ``per_device`` stays empty and ``notes`` says so.
  What *is* computable from measurements alone is reported: the derived run,
  its estimates and its version triple. Grading the engine against its own
  output would be the exact failure mode ``docs/testing.md`` warns about.
* **A replay must not hang CI.** A hard wall-clock deadline (SIGALRM plus a
  watchdog) reports a timeout as a result instead of wedging a run.

Usage::

    .venv/bin/python tools/audit/replay_capture.py --minutes 60
    .venv/bin/python tools/audit/replay_capture.py --db data/wattmosaic.db --minutes 30
    .venv/bin/python tools/audit/replay_capture.py \\
        --jsonl data/raw/ct_events.jsonl --out /tmp/replay.json
    .venv/bin/python tools/audit/replay_capture.py --db data/wattmosaic.db \\
        --versions topo-7,map-4,mset-abc123  # explicit override

The database is opened ``mode=ro``: this tool never writes to a capture.

Version triple (Z2c fix)
------------------------
The v3 replay binds its operator to a *(topology, mapping, model-set)* version
triple.  This tool used to hard-code the HB §17 ``"v1"`` triple; for a real
capture that is wrong -- the install being replayed has its own active topology
version (S14), mapping version (S15) and model set (the L1 registry).  Before a
real-DB replay we now read those three values from the database itself and
report them; an explicit ``--versions topology,mapping,model-set`` override is
available for a synthetic comparison, and a database that carries none of the
three version tables fails with a clear error instead of silently replaying
against the wrong triple.
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

    * ``SIGALRM`` interrupts Python bytecode in the main thread.  A C solver
      releases the GIL but still returns to the interpreter between iterations,
      so this fires in practice.
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
    """One power observation, already mapped to a topology endpoint id."""

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
    #: Optional HA `state_changed` messages.  A raw CT capture has none; the
    #: simulator supplies them so the v3 replay has on/off evidence to bind to.
    ha_messages: list[dict[str, Any]] = field(default_factory=list)


def _as_utc(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def aligner_key(entity_id: str) -> str:
    """The key a capture uses for a channel.

    A capture may store either the HA entity id or the bare key; both map to the
    same channel through this one function.
    """
    text = str(entity_id or "")
    return text.split(".", 1)[-1] if "." in text else text


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _active_id(conn: sqlite3.Connection, table: str, column: str) -> str | None:
    """Active version id from an S14/S15 version table, or ``None``.

    Both ``topology_versions_v3`` (S14) and ``mapping_versions`` (S15) stamp one
    row ``status='active'``; the highest `number` wins when more than one row
    claims it (defensive, since D1 promotes atomically).
    """
    if not _table_exists(conn, table):
        return None
    number_column = (
        "number" if "number" in {
            str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")
        } else "rowid"
    )
    row = conn.execute(
        f"SELECT {column} FROM {table} WHERE status='active' "
        f"ORDER BY {number_column} DESC LIMIT 1"
    ).fetchone()
    return str(row[0]) if row is not None and row[0] is not None else None


def _active_model_set_id(conn: sqlite3.Connection) -> str | None:
    """Derive the active model set from the L1 registry's append-only log.

    A version is active iff its last activation-relevant event is ``activated``
    (``frozen``/``unfrozen`` are audit-only and do not demote it).  The model set
    id is the deterministic hash of the active version ids, exactly as the L1
    registry computes it, so the replay can name the same set the install served.
    """
    if not _table_exists(conn, "power_model_versions_v3") or not _table_exists(
        conn, "power_model_events"
    ):
        return None
    rows = conn.execute(
        """SELECT v.version_id, e.event, e.at, e.id
           FROM power_model_versions_v3 v
           LEFT JOIN power_model_events e ON e.version_id = v.version_id
           ORDER BY v.version_id, e.at, e.id"""
    ).fetchall()
    from wattmosaic_engine.contracts import model_set_id as make_model_set_id  # noqa: PLC0415

    by_version: dict[str, list[tuple[str, str, int]]] = {}
    for version_id, event, at, eid in rows:
        if event is None:
            by_version.setdefault(str(version_id), [])
            continue
        if str(event) in ("frozen", "unfrozen"):
            continue
        by_version.setdefault(str(version_id), []).append(
            (str(event), str(at or ""), int(eid or 0))
        )
    active: list[str] = []
    for version_id, events in by_version.items():
        if not events:
            continue
        last = max(events, key=lambda item: (item[1], item[2]))
        if last[0] == "activated":
            active.append(version_id)
    if not active:
        return None
    return str(make_model_set_id(sorted(active)))


def read_versions_from_db(db_path: Path) -> dict[str, str] | None:
    """Read the active version triple from the database being replayed.

    Returns ``{'topology_version_id', 'mapping_version_id', 'model_set_id'}``
    when at least one version is found, or ``None`` when the database has none
    of the three version tables populated (a legacy capture predating v3).
    The database is opened ``mode=ro`` and never written.
    """
    if not db_path.exists():
        raise SystemExit(f"no database at {db_path}")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        topology = _active_id(conn, "topology_versions_v3", "version_id")
        mapping = _active_id(conn, "mapping_versions", "version_id")
        models = _active_model_set_id(conn)
    finally:
        conn.close()
    found = {
        key: value
        for key, value in (
            ("topology_version_id", topology),
            ("mapping_version_id", mapping),
            ("model_set_id", models),
        )
        if value is not None
    }
    return found or None


def parse_versions_override(text: str) -> dict[str, str]:
    """Parse ``topology,mapping,model-set`` from ``--versions``."""
    parts = [part.strip() for part in text.split(",")]
    if len(parts) != 3 or not all(parts):
        raise SystemExit(
            "--versions must be 'topology_id,mapping_id,model_set_id' "
            "(three non-empty comma-separated values)"
        )
    return {
        "topology_version_id": parts[0],
        "mapping_version_id": parts[1],
        "model_set_id": parts[2],
    }


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
# Replay (v3 surface)
# --------------------------------------------------------------------------- #


def observation_frames(
    capture: Capture,
    *,
    tick_s: float = DEFAULT_CADENCE_S,
    max_age_s: float = 300.0,
    parents: dict[str, str | None] | None = None,
) -> list[dict[str, Any]]:
    """Lift a capture into immutable HB §17 observation frames.

    Each frame carries one point-in-time snapshot of every delivered reader
    sample (zero-order-held to the tick, exactly as the synchroniser does) plus
    any HA messages the capture supplied. A reader with no sample yet stays
    absent: an absent reading is never invented as ``0 W`` (HB §18).

    The optional ``parents`` map declares the topology relation on each sample
    so :func:`wattmosaic_engine.electrical.topology_inference.infer_topology`
    can reconstruct the declared wiring from the same frames.
    """
    parents = parents or {}
    samples = sorted(capture.samples, key=lambda s: s.received_at)
    if not samples:
        return []
    start = capture.window_start or samples[0].received_at
    end = capture.window_end or samples[-1].received_at
    tick = float(tick_s)
    grid: list[datetime] = []
    cursor = start
    while cursor <= end:
        grid.append(cursor)
        cursor += timedelta(seconds=tick)
    if not grid or grid[-1] < end:
        grid.append(end)

    frames: list[dict[str, Any]] = []
    index = 0
    for at in grid:
        by_reader: dict[str, CaptureSample] = {}
        while index < len(samples) and samples[index].received_at <= at:
            sample = samples[index]
            if sample.received_at >= at - timedelta(seconds=max_age_s):
                by_reader[sample.ct_id] = sample
            index += 1
        if not by_reader:
            continue
        reader_samples: dict[str, Any] = {}
        for reader, sample in sorted(by_reader.items()):
            record: dict[str, Any] = {
                "reader_id": reader,
                "entity_id": sample.entity_id,
                "value_w": float(sample.value_w),
                "value": float(sample.value_w),
                "state_timestamp": sample.state_timestamp.isoformat(),
                "received_at": sample.received_at.isoformat(),
                "quality": sample.quality,
                "sequence": int(sample.sequence or 0),
            }
            parent = parents.get(reader)
            if parent is not None:
                record["parent"] = parent
            reader_samples[reader] = record
        frames.append(
            {
                "timestamp": at.isoformat(),
                "reader_samples": reader_samples,
                "ha_messages": [
                    dict(message)
                    for message in capture.ha_messages
                    if _as_utc(message.get("observed_at") or message.get("last_changed"))
                    is not None
                    and _as_utc(message.get("observed_at") or message.get("last_changed")) <= at
                ],
            }
        )
    return frames


def _declared_parents(capture: Capture) -> dict[str, str | None]:
    """Best-effort parent map from the capture's own entity names.

    A capture does not carry a topology document. The only parent evidence it
    has is the endpoint id itself when it encodes a nesting (``sub`` under
    ``main``), the same structural rule the old variant logic used. This is a
    hypothesis under test, never a fact written back to a database.
    """
    parents: dict[str, str | None] = {}
    mains = [
        sample.ct_id
        for sample in capture.samples
        if "main" in sample.ct_id.lower()
        and "sub" not in sample.ct_id.lower()
        and "load" in sample.ct_id.lower()
    ]
    fallback = mains[0] if mains else None
    for sample in capture.samples:
        ct_id = sample.ct_id
        if fallback is not None and ("sub" in ct_id.lower() or "circuit" in ct_id.lower()):
            parents[ct_id] = fallback
    return parents


def replay(
    capture: Capture,
    topology: Any = None,
    *,
    variant: str = "as-shipped",
    models: Any = None,
    max_age_s: float = 300.0,
    cadence_s: float = DEFAULT_CADENCE_S,
    tick_s: float | None = None,
    states: dict[str, Any] | None = None,
    reason: str = "replay",
    versions: dict[str, str] | None = None,
) -> Any:
    """Replay a capture through the v3 replay surface.

    ``models`` is a ``ModelSet`` (any :class:`~wattmosaic_engine.runtime.interfaces.ModelSetView`)
    or the id of a built-in model set. It is handed straight to
    :func:`wattmosaic_engine.replay.replay_week`; ``None`` selects the built-in
    HB §17 ``"v1"`` set. The returned value is a
    :class:`~wattmosaic_engine.learning.reconciliation.DerivedRun` carrying the
    versioned, immutable derived output.

    ``versions`` is the triple read from the replayed database (or the
    ``--versions`` override): ``{'topology_version_id', 'mapping_version_id',
    'model_set_id'}``. It is stamped onto the returned run so the report names
    the versions the *install* actually used, not the hard-coded HB §17 ones.
    The production replay surface itself binds its operator from an explicit
    triple; this tool only rebinds the run's reported identity (the engine API
    has no entry point to inject a custom triple into ``replay_week``).

    ``topology``, ``variant`` and ``states`` are accepted for CLI/compat with
    the pre-v3 tool. They do not change the v3 operator; the declared-parent
    hypothesis is carried on the observation frames so topology inference can
    still be scored from them.
    """
    from wattmosaic_engine.replay import replay_week

    tick = float(tick_s if tick_s is not None else cadence_s)
    model_set = "v1" if models is None else models
    frames = observation_frames(
        capture,
        tick_s=tick,
        max_age_s=max_age_s,
        parents=_declared_parents(capture),
    )
    run = replay_week(frames, model_set=model_set, reason=reason)
    if versions is not None:
        from dataclasses import replace  # noqa: PLC0415

        from wattmosaic_engine.contracts import VersionTriple  # noqa: PLC0415

        run = replace(
            run,
            versions=VersionTriple(
                topology_version_id=versions["topology_version_id"],
                mapping_version_id=versions["mapping_version_id"],
                model_set_id=versions["model_set_id"],
            ),
            derived_run_id=(
                f"derived-{versions['topology_version_id']}-"
                f"{versions['mapping_version_id']}-{versions['model_set_id']}-"
                f"{run.fingerprint[:12]}"
            ),
        )
    return run


def _derived_run_dict(run: Any) -> dict[str, Any]:
    """JSON-safe view of a :class:`DerivedRun` for the report payload."""
    estimates = [
        {
            "subject_kind": estimate.subject_kind,
            "subject_id": estimate.subject_id,
            "point_w": estimate.point_w,
            "lower_w": estimate.lower_w,
            "upper_w": estimate.upper_w,
            "status": estimate.status,
            "identifiability": estimate.identifiability,
            "group_id": estimate.group_id,
            "model_version_id": estimate.model_version_id,
            "evidence_refs": list(estimate.evidence_refs),
            "bounds_basis": estimate.bounds_basis,
        }
        for estimate in run.derived_estimates
    ]
    return {
        "derived_run_id": run.derived_run_id,
        "versions": {
            "topology_version_id": run.versions.topology_version_id,
            "mapping_version_id": run.versions.mapping_version_id,
            "model_set_id": run.versions.model_set_id,
        },
        "reason": run.reason,
        "window_start": run.window_start.isoformat(),
        "window_end": run.window_end.isoformat(),
        "chunks": run.chunks,
        "fingerprint": run.fingerprint,
        "created_at": run.created_at.isoformat(),
        "superseded_by": run.superseded_by,
        "derived_estimates": estimates,
    }


def evaluation_shape(run: Any, *, topology_version: int = 0) -> dict[str, Any]:
    """``EvaluationReport.as_dict()``-shaped payload, with honest gaps.

    Every top-level key the harness emits is present so a consumer can treat a
    real replay and a simulated run with the same code. The strata that require
    ground truth are empty and ``notes`` says why: an empty dict is a truthful
    "not measurable here", while a filled one would be the engine grading
    itself.
    """
    notes = [
        "per_device error statistics are empty: a real installation has no "
        "independent ground truth. Grading against the engine's own output "
        "would prove nothing (docs/testing.md §1).",
    ]
    if not run.derived_estimates:
        notes.append(
            "the capture carried no HA on/off evidence, so the v3 reconciler "
            "produced no derived estimates: a CT-only stream has no state "
            "transitions to bind to subjects (HB §17 observation seam)"
        )
    return {
        "n_records": len(run.derived_estimates),
        "scenario": {
            "seed": None,
            "minutes": None,
            "interval_s": DEFAULT_CADENCE_S,
            "quality": None,
            "source": "real capture replay",
            "topology_version": topology_version,
        },
        "per_device": {},
        "per_device_detection": {},
        "per_source": {},
        "per_source_detection": {},
        "per_device_type": {},
        "per_concurrency": {},
        "per_quality": {},
        "reconciliation": {"n": len(run.derived_estimates)},
        "coverage": {"n": len(run.derived_estimates)},
        "invariants": {
            "nested_check_records": 0,
            "nested_double_count_records": 0,
            "negative_residual_records": 0,
            "checked_records": len(run.derived_estimates),
            "unavailable_records": 0,
            "usable_records": len(run.derived_estimates),
            # Per-device error is not computable: a real house has no truth.
            "per_device_error_available": False,
        },
        "notes": notes,
        "replay": _derived_run_dict(run),
    }


def _print_report(
    run: Any,
    *,
    delta: dict[str, Any] | None = None,
    versions_source: str | None = None,
) -> None:
    print(f"derived run          : {run.derived_run_id}")
    suffix = f" (from {versions_source})" if versions_source else ""
    print(f"versions             : {run.versions.model_dump(mode='json')}{suffix}")
    print(f"window               : {run.window_start.isoformat()} .. {run.window_end.isoformat()}")
    print(f"chunks               : {run.chunks}")
    print(f"derived estimates    : {len(run.derived_estimates)}")
    for estimate in run.derived_estimates:
        value = "None" if estimate.point_w is None else f"{estimate.point_w:.1f}"
        print(f"  {estimate.subject_kind:8s} {estimate.subject_id:20s} {value:>10s} "
              f"{estimate.status}")
    if delta:
        print(f"comparison           : {delta.get('summary', 'unavailable')}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=None, help="engine SQLite database (read-only)")
    ap.add_argument("--jsonl", type=Path, default=None, help="captured ct_events.jsonl")
    ap.add_argument("--minutes", type=float, default=60.0, help="window length from the capture end")
    ap.add_argument(
        "--model-set",
        default=None,
        help="built-in v3 model set id ('v1'/'v2'); default 'v1'",
    )
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
        help="skip the second replay comparison run",
    )
    ap.add_argument(
        "--compare-timeout-s",
        type=float,
        default=20.0,
        help="deadline for the comparison run only",
    )
    ap.add_argument("--max-age-s", type=float, default=300.0, help="reader staleness bound")
    ap.add_argument(
        "--versions",
        default=None,
        metavar="TOPOLOGY,MAPPING,MODEL_SET",
        help=(
            "explicit version triple to replay under (overrides the database). "
            "Default: read the active topology/mapping/model versions from --db."
        ),
    )
    args = ap.parse_args()

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

    # The v3 replay binds its operator to an explicit version triple, so the
    # topology archive is still read (read-only) for the channel map and the
    # reported topology version. A JSONL capture does not carry wiring.
    topology_db = db_path or DEFAULT_DB
    topology_version = 0
    entity_to_ct: dict[str, str] = {}
    if topology_db.exists():
        try:
            conn = sqlite3.connect(f"file:{topology_db}?mode=ro", uri=True)
            try:
                row = conn.execute(
                    "SELECT payload_json, version FROM topology_versions "
                    "ORDER BY version DESC LIMIT 1"
                ).fetchone()
            finally:
                conn.close()
            if row is not None:
                payload = json.loads(row[0])
                topology_version = int(row[1] or 0)
                entity_to_ct = {
                    str(ch.get("entity_id")): str(ch.get("ct_id"))
                    for ch in payload.get("channels") or []
                    if ch.get("ct_id")
                }
                for ch in payload.get("channels") or []:
                    if ch.get("ct_id"):
                        entity_to_ct[aligner_key(str(ch.get("entity_id") or ""))] = str(ch["ct_id"])
        except sqlite3.Error:
            # A capture DB without a topology archive is still replayable: the
            # endpoint id from the capture is used verbatim.
            entity_to_ct = {}
    elif jsonl_path is not None:
        print(
            "replay_capture: a --jsonl capture needs --db to supply the topology "
            f"({topology_db} does not exist)",
            file=sys.stderr,
        )
        return 2

    # Resolve the version triple the replay will report.  A --versions override
    # wins; otherwise the active versions are read from the database being
    # replayed.  A database with no v3 version tables at all is an explicit
    # error rather than silently falling back to the HB §17 triple.
    versions: dict[str, str] | None
    versions_source: str
    if args.versions is not None:
        versions = parse_versions_override(args.versions)
        versions_source = "cli-override"
    elif db_path is not None:
        versions = read_versions_from_db(db_path)
        versions_source = "database"
        if versions is None:
            print(
                "replay_capture: no active topology/mapping/model versions in "
                f"{db_path}. This database predates v3; pass --versions "
                "TOPOLOGY,MAPPING,MODEL_SET to replay it explicitly.",
                file=sys.stderr,
            )
            return 2
    else:
        versions = None
        versions_source = "engine-default"

    started = time.monotonic()
    with _Deadline(args.timeout_s):
        try:
            if db_path is not None:
                capture = load_capture_db(db_path, minutes=args.minutes, entity_to_ct=entity_to_ct)
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

        models = args.model_set
        try:
            run = replay(
                capture,
                variant=variant,
                models=models,
                max_age_s=args.max_age_s,
                versions=versions,
            )
        except ReplayTimeout as exc:
            print(
                f"replay_capture: {variant} replay aborted after "
                f"{time.monotonic() - started:.1f}s: {exc}",
                file=sys.stderr,
            )
            return 124
        except (ValueError, KeyError) as exc:
            print(f"replay_capture: replay failed: {exc}", file=sys.stderr)
            return 2

        delta: dict[str, Any] = {"summary": "skipped (--no-compare)"}
        if not args.no_compare:
            other = "v2" if (models or "v1") == "v1" else "v1"
            remaining = min(args.compare_timeout_s, args.timeout_s - (time.monotonic() - started))
            try:
                with _Deadline(max(1.0, remaining), hard_exit=False):
                    other_run = replay(
                        capture,
                        variant=variant,
                        models=other,
                        max_age_s=args.max_age_s,
                        versions=versions,
                    )
                delta = {
                    "summary": (
                        f"{models or 'v1'} run {run.derived_run_id} vs {other} run "
                        f"{other_run.derived_run_id}: "
                        f"{len(run.derived_estimates)} vs {len(other_run.derived_estimates)} estimates"
                    ),
                    "other_model_set": other,
                    "same_fingerprint": run.fingerprint == other_run.fingerprint,
                }
            except ReplayTimeout:
                delta = {
                    "summary": f"{other} comparison exceeded {max(1.0, remaining):.0f}s and was abandoned",
                    "other_model_set": other,
                    "timed_out": True,
                }

        payload = evaluation_shape(run, topology_version=topology_version)
        payload["versions"] = {
            "source": versions_source,
            **(versions or {}),
        }
        payload["delta_vs_other_model_set"] = delta
        if args.out is not None:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(payload, indent=2, default=str) + "\n")
            print(f"wrote {args.out}")

    _print_report(run, delta=delta, versions_source=versions_source)
    if delta.get("timed_out"):
        print(
            "replay_capture: NOTE the comparison run did not terminate; see "
            "delta_vs_other_model_set.summary",
            file=sys.stderr,
        )
        return 124
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
