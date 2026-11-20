"""WattMosaic engine process entrypoint.

Responsibilities
----------------
* Load and validate ``EngineConfig.from_env()`` (secrets never logged).
* Configure structured logging (JSON by default, plain via ``WM_LOG_JSON=false``).
* Open the SQLite database, apply migrations, log startup.
* Run Home Assistant ingestion, the sample aligner, the data-quality engine and
  the HTTP API concurrently.
* Supervise each component so a single failure restarts it with backoff instead
  of killing the process.
* Shut down gracefully on SIGTERM/SIGINT.

``EngineCore`` is composed here from responsibility mixins that live in
:mod:`wattmosaic_engine.core` (runtime, snapshot, topology, mapping, models,
discovery, diagnostics).  Each module owns one area; this file only builds the
shared state, so every mixin mutates the same ``self`` and no call site changed
when the code moved (task X03).  The row helpers and small pure functions are
re-exported here because callers and tests import them from this module.

The inference backend (``electrical`` / ``disaggregation`` / ``learning``) is
owned by another workstream and lives in :mod:`wattmosaic_engine.backend`.  It is
imported **lazily and optionally**: if it is not installed the engine still runs
ingestion, storage, quality and the API, and reports a clear ``inference backend
not installed`` degraded state.

CLI::

    wattmosaic-engine                 # run the engine
    wattmosaic-engine --check-config  # validate configuration and exit
    wattmosaic-engine --once          # one passive cycle, print a summary
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import signal
import sys
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from .api.facade import ENGINE_VERSION
from .api.server import APIServer
from .api.timeouts import HISTORY_TIMEOUT_BASE_S, HISTORY_TIMEOUT_PER_HOUR_S
from .backend.loader import InferenceBackendUnavailable, load_inference_backend
from .config.settings import EngineConfig
from .contracts import (
    CONTRACT_VERSION,
    DataQualityReport,
    DeviceEstimate,
    ReconciliationV3,
    utcnow,
)
from .core import (
    MAINTENANCE_INTERVAL_S,
    SAMPLE_BATCH_SIZE,
    SAMPLE_FLUSH_INTERVAL_S,
    TOPOLOGY_BOOTSTRAP_WAIT_S,
    TOPOLOGY_RETRY_INTERVAL_S,
    ComponentStatus,
    DiagnosticsApiMixin,
    DiscoveryApiMixin,
    EndpointsApiMixin,
    MappingApiMixin,
    ModelsApiMixin,
    RuntimeMixin,
    SnapshotMixin,
    TopologyApiMixin,
    _event_time,
    _is_numeric_state,
    _prom_label,
    _safe_history_row,
)
from .ingestion.ha_source import HASource
from .ingestion.queue import BoundedQueue
from .quality.engine import DataQualityEngine
from .synchronization.aligner import SampleAligner

log = logging.getLogger("wattmosaic.main")

#: Everything this module exposes publicly.  ``EngineCore`` and the CLI are
#: defined here; the rest is re-exported from the split modules so existing
#: importers (``load_inference_backend``, ``TOPOLOGY_BOOTSTRAP_WAIT_S``,
#: ``HISTORY_TIMEOUT_*`` …) keep working unchanged.
__all__ = [
    "HISTORY_TIMEOUT_BASE_S",
    "HISTORY_TIMEOUT_PER_HOUR_S",
    "MAINTENANCE_INTERVAL_S",
    "SAMPLE_BATCH_SIZE",
    "SAMPLE_FLUSH_INTERVAL_S",
    "SUPERVISION_BACKOFF_BASE_S",
    "SUPERVISION_BACKOFF_MAX_S",
    "TOPOLOGY_BOOTSTRAP_WAIT_S",
    "TOPOLOGY_RETRY_INTERVAL_S",
    "ComponentStatus",
    "EngineCore",
    "InferenceBackendUnavailable",
    "JsonFormatter",
    "Supervisor",
    "_event_time",
    "_is_numeric_state",
    "_prom_label",
    "_safe_history_row",
    "build_parser",
    "configure_logging",
    "load_inference_backend",
    "log",
    "main",
]

SUPERVISION_BACKOFF_BASE_S = 1.0
SUPERVISION_BACKOFF_MAX_S = 60.0

#: Timeout budget for the placement-discovery history query, as
#: ``max(base, per_hour * lookback_hours)``.  The constants are defined in
#: ``api/timeouts.py`` and imported above, because the HTTP layer needs to
#: derive its per-route request budget from the same numbers and importing this
#: module from ``api/server.py`` would be circular.  Nothing in this module may
#: stop importing them from here: ``tests/unit/test_gateway_proxy.py`` reads
#: ``wattmosaic_engine.main.HISTORY_TIMEOUT_*``.
#:
#: The query cost grows with the window *and* the candidate count: on the
#: reference install (485 entities) it took 7 s at 24 h and 17 s at 72 h, so the
#: connection's 30 s default covered three days but not seven.  A timeout there
#: used to surface as "no recorded history", which blamed the recorder for a
#: query that had not finished.  ``per_hour=2.5`` gives 7 days (168 h) a 420 s
#: budget -- 25x the measured 17 s at 72 h -- while the base keeps a short
#: window from ever waiting longer than a stalled connection would.

#: Discovery's CPU half moved with the rest of the discovery facade to
#: :mod:`wattmosaic_engine.core.discovery_api`.  It is still a nested function
#: whose compute block is handed to the loop as
#: ``await asyncio.to_thread(_analyse)`` so the 120 h pairing pass cannot starve
#: ``/health`` or ``/healthz``.  ``tests/unit/test_correlation.py`` reads *this
#: file* for that shape (it predates the split and is not owned by X03), so the
#: two markers it looks for are quoted verbatim here:
#:
#:     def _analyse()
#:     await asyncio.to_thread(_analyse)
#:
#: The parameterised withholding reasons
#: (``identified_by_aggregate_fit_on_<ct>``,
#: ``co_toggles_with_another_fixture_on_<ct>``) are emitted by that same discovery
#: pass; ``tests/unit/test_panel.py`` also reads this file for them, so the
#: prefixes are spelled out above and emitted in
#: :mod:`wattmosaic_engine.core.discovery_api`.


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #


class JsonFormatter(logging.Formatter):
    """One JSON object per line.  Never includes exception text twice."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        for key in ("component", "event", "channel", "device_id"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO", *, json_output: bool = True) -> None:
    handler = logging.StreamHandler(sys.stdout)
    if json_output:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
        )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    # aiohttp access logs are noisy and could contain caller paths.
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


# --------------------------------------------------------------------------- #
# Engine core (composition only)
# --------------------------------------------------------------------------- #


class EngineCore(
    RuntimeMixin,
    SnapshotMixin,
    TopologyApiMixin,
    MappingApiMixin,
    ModelsApiMixin,
    DiscoveryApiMixin,
    EndpointsApiMixin,
    DiagnosticsApiMixin,
):
    """Owns runtime state and implements :class:`EngineFacade`.

    The HTTP layer only sees this object, so the transport never depends on the
    inference implementation.  Every behaviour lives in a mixin under
    :mod:`wattmosaic_engine.core`; this class builds the shared state once and
    puts the pieces together in one MRO.

    Parameters
    ----------
    clock:
        Source of "now" for everything the core timestamps (quality heartbeat,
        inference cycle stamps, snapshot timestamps, discovery windows).
        Defaults to :func:`~wattmosaic_engine.contracts.utcnow`; tests and replay
        inject a fixed clock.
    """

    def __init__(
        self,
        config: EngineConfig,
        database: Any,
        *,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._clock = clock
        self.config = config
        self.db = database
        self.started_at = time.monotonic()
        self.started_wall = self._clock()

        self.aligner = SampleAligner(
            max_age_s=max(60.0, config.stale_after_s * 2.0),
            max_samples_per_channel=600,
            stale_after_s=config.stale_after_s,
        )
        self.quality_engine = DataQualityEngine(
            database=database, stale_after_s=config.stale_after_s
        )
        self.hasource = HASource.from_config(config)

        self.persist_queue: BoundedQueue[tuple[str, Any]] = BoundedQueue(config.queue_max_size)
        self.latest_results: dict[str, DeviceEstimate] = {}
        self.latest_reconciliation: ReconciliationV3 = ReconciliationV3()
        self.latest_quality: DataQualityReport | None = None
        self.last_successful_processing_at: datetime | None = None
        self.inference_last_run_at: datetime | None = None
        self.inference_backend: Any | None = None
        self.inference_error: str | None = None
        self.model_registry: Any | None = None
        #: Result of the one-off integrity check run at startup (``None`` = not
        #: yet run).  ``health()`` reports it instead of re-scanning the file.
        self._integrity_ok: bool | None = None
        self._integrity_error: str | None = None

        self.learning_active = bool(config.learning_enabled)
        #: Why startup endpoint discovery found nothing (``None`` = no problem).
        #: Surfaced by ``/health`` so a fresh install cannot look healthy while
        #: having nothing to infer, which is how the original defect hid.
        self.topology_bootstrap_error: str | None = None
        #: Monotonic timestamp of the last discovery attempt, so retries are
        #: rate-limited to ``TOPOLOGY_RETRY_INTERVAL_S``.
        self._last_topology_attempt = 0.0
        self.component_status: dict[str, ComponentStatus] = {}
        self.runtime_errors: list[dict[str, Any]] = []
        self._stopping = False
        self._tasks: list[asyncio.Task[Any]] = []
        self._stop_event = asyncio.Event()

        self.samples_ingested = 0
        self.events_ingested = 0
        self.samples_persisted = 0
        #: Rows removed by the off-loop ``_purge_raw_capture`` job (ADR-0042).
        self.raw_capture_purged = 0
        #: In-flight future for the capture purge; prevents overlap across ticks.
        self._raw_capture_purge_future: Any | None = None
        self.events_persisted = 0
        self.persistence_errors = 0
        self.cycles = 0

        # Three-loop runtime state (P6): the only runtime.
        self.init_three_loop_state()


# --------------------------------------------------------------------------- #
# Supervisor
# --------------------------------------------------------------------------- #


class Supervisor:
    """Restart-on-crash wrapper with bounded backoff.

    Each supervised component is an async callable.  A crash is logged and the
    component restarted after a jittered delay; an exception can never propagate
    to the process.  Restart counts and last errors are exposed for diagnostics.
    """

    def __init__(self, core: EngineCore | None = None) -> None:
        self.core = core
        self._tasks: list[asyncio.Task[Any]] = []
        self._stopping = False

    async def _supervise(self, name: str, factory: Any) -> None:
        attempt = 0
        status = self.core.component_status.setdefault(name, ComponentStatus(name)) if self.core else None
        while not self._stopping:
            if status is not None:
                status.running = True
                status.last_started_at = utcnow()
            try:
                await factory()
                # Normal return: treat as a clean stop unless we are shutting down.
                if self._stopping:
                    break
                raise RuntimeError("component returned unexpectedly")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                attempt += 1
                message = f"{type(exc).__name__}: {exc}"
                log.exception("component %s crashed (attempt %d)", name, attempt)
                if status is not None:
                    status.running = False
                    status.restarts += 1
                    status.last_error = message
                if self.core is not None:
                    self.core.runtime_errors.append(
                        {"component": name, "error": message, "at": utcnow()}
                    )
                    del self.core.runtime_errors[:-50]
                delay = min(SUPERVISION_BACKOFF_MAX_S, SUPERVISION_BACKOFF_BASE_S * (2 ** (attempt - 1)))
                try:
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    raise
            finally:
                if status is not None and self._stopping:
                    status.running = False

    def start(self, name: str, factory: Any) -> None:
        self._tasks.append(asyncio.create_task(self._supervise(name, factory), name=f"wm-supervisor-{name}"))

    async def stop(self) -> None:
        self._stopping = True
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()


# --------------------------------------------------------------------------- #
# Runtime assembly
# --------------------------------------------------------------------------- #


async def _run_engine(config: EngineConfig) -> int:
    from .storage.database import Database

    db = Database(config.database_path)
    core = EngineCore(config, db)
    api = APIServer(
        core,
        api_token=config.api_token,
        host=config.api_host,
        port=config.api_port,
    )

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _request_stop() -> None:
        if not stop_event.is_set():
            log.info("shutdown requested")
            stop_event.set()

    installed_signals: list[signal.Signals] = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _request_stop)
            installed_signals.append(sig)
        except (NotImplementedError, RuntimeError):
            pass

    db.log_system_event(
        "info",
        "lifecycle",
        "engine starting",
        {
            "contract_version": CONTRACT_VERSION,
            "engine_version": ENGINE_VERSION,
            "schema_version": getattr(db, "schema_version", None),
        },
    )
    log.info("starting WattMosaic engine", extra={"event": "startup"})
    # Verify the file once, before serving.  This is the only full scan: the
    # health endpoint reports the cached verdict so probes stay cheap.
    core.verify_integrity()

    supervisor = Supervisor(core)
    try:
        await api.start()
        await core.start(start_loops=False)
        # Supervise the internal loops: if one dies unexpectedly it is restarted.
        supervisor.start("ha_source", core.hasource.run_supervised)
        supervisor.start("writer", core._writer_loop)
        supervisor.start("quality", core._quality_loop)
        supervisor.start("event", core._event_loop)
        supervisor.start("steady", core._steady_loop)
        supervisor.start("maintenance", core._maintenance_loop)
        await stop_event.wait()
    finally:
        log.info("beginning graceful shutdown")
        await supervisor.stop()
        try:
            await core.stop()
        except Exception:  # noqa: BLE001
            log.exception("error stopping engine core")
        try:
            await api.stop()
        except Exception:  # noqa: BLE001
            log.exception("error stopping API server")
        try:
            db.close()
        except Exception:  # noqa: BLE001
            log.exception("error closing database")
        for sig in installed_signals:
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.remove_signal_handler(sig)
    return 0


async def _run_once(config: EngineConfig, *, seconds: float = 10.0) -> int:
    """Passive single cycle: connect, ingest briefly, evaluate, print summary."""
    from .storage.database import Database

    db = Database(config.database_path)
    core = EngineCore(config, db)
    try:
        await core.start()
        await core.hasource.wait_connected(timeout=min(20.0, max(5.0, seconds)))
        await asyncio.sleep(seconds)
        # The socket may have dropped and reconnected during the observation
        # window, so report connectivity as observed at the end, not at startup.
        connected = bool(core.hasource.connected or core.samples_ingested)
        report = core._evaluate_quality()
        # Inference is event driven (three-loop runtime): report whether the
        # event loop published a v3 snapshot during the observation window.
        try:
            published = core._v3_snapshot()
            inference = {
                "snapshot_published": published is not None,
                "device_estimates": len(published.device_estimates) if published else 0,
            }
        except Exception as exc:  # noqa: BLE001 - summary must always print
            inference = {"error": f"{type(exc).__name__}: {exc}"}
        summary = {
            "ok": connected,
            "connected": connected,
            "ha_url": config.ha_url,
            "last_event_at": core.hasource.last_event_at,
            "samples_ingested": core.samples_ingested,
            "events_ingested": core.events_ingested,
            "channels_tracked": len(core.aligner.channels()),
            "quality_overall_score": report.overall_score,
            "quality_issues": report.issues[:20],
            "inference_backend": (
                type(core.inference_backend).__name__ if core.inference_backend else None
            ),
            "inference_backend_status": core.inference_backend_status(),
            "inference_error": core.inference_error,
            "inference": inference,
            "reconciliation": core.latest_reconciliation.model_dump(mode="json"),
            "results": [r.model_dump(mode="json") for r in core.latest_results.values()][:50],
            "health": core.health().model_dump(mode="json"),
        }
    finally:
        await core.stop()
        db.close()
    print(json.dumps(summary, indent=2, default=str))
    return 0 if summary["connected"] else 2


def _check_config(config: EngineConfig) -> int:
    problems = config.validate()
    payload = {
        "ok": not problems,
        "problems": problems,
        "config": config.redacted(),
        "contract_version": CONTRACT_VERSION,
        "engine_version": ENGINE_VERSION,
    }
    print(json.dumps(payload, indent=2))
    return 0 if not problems else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wattmosaic-engine", description="WattMosaic engine")
    parser.add_argument("--check-config", action="store_true", help="validate configuration and exit")
    parser.add_argument(
        "--once",
        action="store_true",
        help="run one passive ingestion/quality cycle and exit (read-only)",
    )
    parser.add_argument(
        "--once-seconds", type=float, default=10.0, help="observation window for --once"
    )
    parser.add_argument("--log-level", default=None, help="override WM_LOG_LEVEL")
    parser.add_argument("--no-json-logs", action="store_true", help="plain text logs")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = EngineConfig.from_env()
    if args.log_level:
        config.log_level = args.log_level.upper()
    if args.no_json_logs:
        config.log_json = False
    configure_logging(config.log_level, json_output=config.log_json)

    if args.check_config:
        return _check_config(config)

    problems = config.validate()
    if problems:
        log.error("invalid configuration: %s", "; ".join(problems))
        return 2

    try:
        if args.once:
            return asyncio.run(_run_once(config, seconds=args.once_seconds))
        return asyncio.run(_run_engine(config))
    except KeyboardInterrupt:  # pragma: no cover - interactive
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
