"""Diagnostics facade: health, status, quality, metrics and audit reads.

Owned by D5 after X03 (retention and storage health)."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

from ..api.facade import ENGINE_VERSION
from ..contracts import CONTRACT_VERSION, DataQualityReport, HealthResponse
from ..diagnostics.report import redact, run_diagnostics

log = logging.getLogger("wattmosaic.main")


class DiagnosticsApiMixin:
    """Behaviour for one responsibility of :class:`EngineCore`."""

    def verify_integrity(self) -> dict[str, Any]:
        """Run the expensive whole-file integrity check once and remember it.

        Called at startup (and by diagnostics).  The result is cached so the
        frequently-polled health endpoint can report it without re-scanning a
        multi-hundred-megabyte database on the event loop.
        """
        report: dict[str, Any] = {}
        try:
            report = self.db.integrity_check()
            self._integrity_ok = report.get("integrity") == "ok"
            self._integrity_error = None if self._integrity_ok else str(
                report.get("integrity", "error")
            )
        except Exception as exc:  # noqa: BLE001 - never fail startup on the probe
            self._integrity_ok = False
            self._integrity_error = f"{type(exc).__name__}: {exc}"
            report = {"integrity": self._integrity_error}
        if self._integrity_ok is False:
            log.error("database integrity check failed: %s", self._integrity_error)
        else:
            log.info("database integrity check passed")
        return report

    def health(self) -> HealthResponse:
        ha_connected = self.hasource.connected
        db_ok = True
        try:
            # Cheap liveness probe only. ``integrity_check`` is a whole-file
            # scan (seconds on a real database); running it here would block the
            # event loop on every health poll, and the App watchdog polls
            # ``/healthz`` -> this method every few seconds.  The full check runs
            # once at startup and on demand from diagnostics.
            self.db.ping()
        except Exception as exc:  # noqa: BLE001
            database = f"error: {type(exc).__name__}"
            db_ok = False
        else:
            if self._integrity_ok is False:
                # A startup integrity failure stays reported: the file is
                # readable enough to open, but it must not look healthy.
                database = str(self._integrity_error or "error")
                db_ok = False
            else:
                database = "ok"

        status = "ok"
        topology = self._topology_health()
        if self.inference_backend is None or not ha_connected or not db_ok:
            status = "degraded"
        # A topology-less engine answers every probe, ingests every sample and
        # emits nothing.  It must not report "ok": that is exactly the state that
        # made a fresh install look healthy in the bug report.
        if not topology["configured"]:
            status = "degraded"
        if not db_ok:
            status = "error"
        queue_depth = len(self.persist_queue)
        if queue_depth > max(1, self.config.queue_max_size // 2):
            status = "degraded"

        return HealthResponse(
            status=status,  # type: ignore[arg-type]
            contract_version=CONTRACT_VERSION,
            engine_version=ENGINE_VERSION,
            database=database,
            ha_connected=ha_connected,
            ha_last_event_at=self.hasource.last_event_at,
            uptime_seconds=round(time.monotonic() - self.started_at, 3),
            queue_depth=queue_depth,
            inference_last_run_at=self.inference_last_run_at,
            learning_active=self.learning_active,
            details={
                "inference_backend": (
                    type(self.inference_backend).__name__
                    if self.inference_backend is not None
                    else None
                ),
                "inference_error": self.inference_error,
                "ha_state": self.hasource.state.value,
                "topology": topology,
                # The honest answer to "is learning actually learning?".  It is
                # in ``details`` (an additive, schema-free object) rather than a
                # new top-level field so an older HA integration keeps parsing
                # the response unchanged.
                "learning_status": self._learning_status_payload(),
                "samples_ingested": self.samples_ingested,
                "events_ingested": self.events_ingested,
                "queue": self.persist_queue.stats(),
                "aligner": self.aligner.stats(),
                "components": {
                    name: {
                        "running": st.running,
                        "restarts": st.restarts,
                        "last_error": st.last_error,
                    }
                    for name, st in self.component_status.items()
                },
            },
        )

    def _learning_status_payload(self) -> dict[str, Any]:
        """The backend's learning counters + ``effective_state``, or a reason.

        Never raises: ``/health`` is polled every few seconds by the App
        watchdog, and a backend accounting bug must not turn that into a 500.
        """
        backend = self.inference_backend
        if backend is None:
            return {
                "enabled": False,
                "active": False,
                "effective_state": "off",
                "summary": "No inference backend is installed.",
            }
        status_fn = getattr(backend, "learning_status", None)
        if not callable(status_fn):
            return {
                "enabled": bool(getattr(backend, "learning", None) is not None),
                "active": bool(getattr(backend, "learning_active", False)),
                "effective_state": "off",
                "summary": "This inference backend does not report learning accounting.",
            }
        try:
            payload = status_fn()
        except Exception as exc:  # noqa: BLE001 - health must never fail
            return {
                "enabled": bool(getattr(backend, "learning", None) is not None),
                "active": bool(getattr(backend, "learning_active", False)),
                "effective_state": "off",
                "summary": f"learning accounting failed: {type(exc).__name__}",
            }
        data = dict(payload) if isinstance(payload, dict) else {}
        self._add_blocked_reason_detail(data)
        return data

    @staticmethod
    def _add_blocked_reason_detail(status: dict[str, Any]) -> None:
        """Publish an informational note for a dynamic topology, without gating.

        Owner decision (OD-51, 2026-11-18): topology is *always* authoritative
        for inference and learning.  A candidate topology no longer blocks
        learning, so this no longer names a confirmation "cause" or remedy.  It
        only adds an informational ``topology_note`` describing the reduced
        confidence.  The topology is **never** auto-confirmed here (contract §6),
        because the wiring association is the user's assertion to make -- the
        no-auto-confirm invariant is kept, just not used as a learning gate.
        """
        from ..learning.reporting import DYNAMIC_TOPOLOGY_NOTE, DYNAMIC_TOPOLOGY_QUALITIES

        quality = str(status.get("topology_quality") or "").strip().lower()
        if quality in DYNAMIC_TOPOLOGY_QUALITIES and not status.get("topology_note"):
            # Informational only: a candidate topology is authoritative for
            # learning, carrying a reduced confidence, never a block/remedy.
            status["topology_note"] = DYNAMIC_TOPOLOGY_NOTE

    def inference_backend_status(self) -> dict[str, Any] | None:
        """Backend self-description for ``/status`` and diagnostics.

        Only plain metadata is returned; the backend never sees credentials and
        any nested secrets are redacted before leaving the core.
        """
        backend = self.inference_backend
        if backend is None:
            return None
        status_fn = getattr(backend, "status", None)
        data: dict[str, Any] = {"backend": type(backend).__name__}
        if callable(status_fn):
            try:
                reported = status_fn()
                if hasattr(reported, "model_dump"):
                    reported = reported.model_dump(mode="json")
                if isinstance(reported, dict):
                    data.update(reported)
            except Exception as exc:  # noqa: BLE001 - status is informational
                data["status_error"] = f"{type(exc).__name__}: {exc}"
        redacted = redact(data)
        return redacted if isinstance(redacted, dict) else data

    def status(self) -> dict[str, Any]:
        health = self.health()
        try:
            db_stats = self.db.stats()
        except Exception:  # noqa: BLE001
            db_stats = {"error": "unavailable"}
        quality = self.latest_quality
        learning_status = self._learning_status_payload()
        return {
            "status": health.status,
            "contract_version": CONTRACT_VERSION,
            "engine_version": ENGINE_VERSION,
            "started_at": self.started_wall,
            "uptime_seconds": health.uptime_seconds,
            "ha_connected": health.ha_connected,
            "ha_last_event_at": health.ha_last_event_at,
            "queue_depth": health.queue_depth,
            "inference": {
                "backend": health.details.get("inference_backend"),
                "degraded": self.inference_backend is None,
                "error": self.inference_error,
                "last_run_at": self.inference_last_run_at,
                "cycles": self.cycles,
                "backend_status": self.inference_backend_status(),
            },
            "learning": {
                "active": self.learning_active,
                "effective_state": learning_status.get("effective_state"),
                # Informational only (OD-51): a dynamic topology is authoritative
                # for learning with reduced confidence, never a block/remedy.
                "topology_note": learning_status.get("topology_note"),
                "remedy": learning_status.get("remedy"),
            },
            "database": db_stats,
            "quality": (
                {"overall_score": quality.overall_score, "issues": len(quality.issues)}
                if quality is not None
                else None
            ),
            "ingestion": {
                "samples_ingested": self.samples_ingested,
                "events_ingested": self.events_ingested,
                "samples_persisted": self.samples_persisted,
                "events_persisted": self.events_persisted,
                "events_evicted_before_maturity": self.events_evicted_before_maturity,
                "unlinked_events_ignored": self.unlinked_events_ignored,
                "power_events_not_state": self.power_events_not_state,
                "persistence_errors": self.persistence_errors,
                "queue": self.persist_queue.stats(),
            },
        }

    def quality_report(self) -> DataQualityReport:
        if self.latest_quality is None:
            return self._evaluate_quality()
        return self.latest_quality

    def quality(self) -> DataQualityReport:
        return self.quality_report()

    def circuits(self) -> list[dict[str, Any]]:
        """Stored circuit scopes, derived by the analysis pass.

        Read-only: nothing here classifies or writes.  A circuit only appears
        after a pass has had the evidence to name a class, and its
        ``classification_status`` stays ``candidate`` until a user confirms it.
        """
        try:
            rows = self.db.circuits_all()
        except Exception:  # noqa: BLE001 - a read failure is an empty answer
            return []
        return [self._jsonable(row) for row in rows]

    @staticmethod
    def _jsonable(value: Any) -> Any:
        """JSON-safe copy of a stored row (enums/datetimes to plain values)."""
        if isinstance(value, dict):
            return {str(k): DiagnosticsApiMixin._jsonable(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [DiagnosticsApiMixin._jsonable(item) for item in value]
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, Enum):
            return value.value
        return value

    def inference_records(
        self, device_id: str, *, limit: int = 200
    ) -> list[dict[str, Any]]:
        """The append-only revision chain for one device, oldest first."""
        if not device_id:
            return []
        try:
            rows = self.db.inference_records_for_device(
                device_id, limit=max(1, int(limit))
            )
        except Exception:  # noqa: BLE001
            return []
        return [self._jsonable(row) for row in rows]

    def residual_components(self, *, hours: float = 24.0, limit: int = 500) -> dict[str, Any]:
        """Recent residual observations, never re-signed or zeroed."""
        since = self._clock() - timedelta(hours=max(0.0, float(hours)))
        try:
            rows = self.db.residual_history(since=since, limit=max(1, int(limit)))
        except Exception:  # noqa: BLE001
            return {"window_hours": float(hours), "count": 0, "components": []}
        return {
            "window_hours": float(hours),
            "count": len(rows),
            "components": [self._jsonable(row) for row in rows],
            "note": (
                "A residual is signed: a positive value is unaccounted load, a "
                "negative value is measured load that no device explains. "
                "Neither is clamped to zero."
            ),
        }

    def isolation_status(self) -> dict[str, Any]:
        """Isolation verdict counts from the last analysis pass."""
        backend = self.inference_backend
        method = getattr(backend, "isolation_status", None)
        if method is None:
            return {
                "source": "no_analysis_pass",
                "counts": {
                    "analysed": 0,
                    "isolated": 0,
                    "contaminated": 0,
                    "insufficient_data": 0,
                },
                "verdicts": [],
            }
        try:
            return method()
        except Exception:  # noqa: BLE001
            return {"source": "unavailable", "counts": {}, "verdicts": []}

    def diagnostics(self) -> dict[str, Any]:
        report = run_diagnostics(
            config=self.config,
            database=self.db,
            hasource=self.hasource,
            aligner=self.aligner,
            quality=self.quality_engine,
            core=self,
        )
        # Adapter self-description (never credentials).
        report["inference_backend"] = self.inference_backend_status()
        return report

    def metrics(self) -> str:
        health = self.health()
        queue = self.persist_queue.stats()
        aligner_stats = self.aligner.stats()
        quality = self.latest_quality
        lines = [
            "# HELP wattmosaic_up Engine process is up",
            "# TYPE wattmosaic_up gauge",
            "wattmosaic_up 1",
            "# HELP wattmosaic_engine_status Engine status label",
            "# TYPE wattmosaic_engine_status gauge",
            f'wattmosaic_engine_status{{status="{_prom_label(health.status)}"}} 1',
            "# HELP wattmosaic_ha_connected Home Assistant WebSocket connected",
            "# TYPE wattmosaic_ha_connected gauge",
            f"wattmosaic_ha_connected {1 if health.ha_connected else 0}",
            "# HELP wattmosaic_inference_backend_present Inference backend attached",
            "# TYPE wattmosaic_inference_backend_present gauge",
            f"wattmosaic_inference_backend_present {1 if self.inference_backend is not None else 0}",
            "# HELP wattmosaic_uptime_seconds Process uptime",
            "# TYPE wattmosaic_uptime_seconds counter",
            f"wattmosaic_uptime_seconds {health.uptime_seconds:.3f}",
            "# HELP wattmosaic_queue_depth Persistence queue depth",
            "# TYPE wattmosaic_queue_depth gauge",
            f"wattmosaic_queue_depth {queue['depth']}",
            "# HELP wattmosaic_queue_dropped_total Samples dropped by backpressure",
            "# TYPE wattmosaic_queue_dropped_total counter",
            f"wattmosaic_queue_dropped_total {queue['dropped']}",
            "# HELP wattmosaic_samples_ingested_total Samples received from Home Assistant",
            "# TYPE wattmosaic_samples_ingested_total counter",
            f"wattmosaic_samples_ingested_total {self.samples_ingested}",
            "# HELP wattmosaic_events_ingested_total Device events received",
            "# TYPE wattmosaic_events_ingested_total counter",
            f"wattmosaic_events_ingested_total {self.events_ingested}",
            "# HELP wattmosaic_power_events_not_state_total Declared power-entity events not used as device state",
            "# TYPE wattmosaic_power_events_not_state_total counter",
            f"wattmosaic_power_events_not_state_total {self.power_events_not_state}",
            "# HELP wattmosaic_samples_persisted_total Samples written to SQLite",
            "# TYPE wattmosaic_samples_persisted_total counter",
            f"wattmosaic_samples_persisted_total {self.samples_persisted}",
            "# HELP wattmosaic_persistence_errors_total Failed persistence batches",
            "# TYPE wattmosaic_persistence_errors_total counter",
            f"wattmosaic_persistence_errors_total {self.persistence_errors}",
            "# HELP wattmosaic_channels_tracked Channels in the aligner",
            "# TYPE wattmosaic_channels_tracked gauge",
            f"wattmosaic_channels_tracked {aligner_stats['channels']}",
            "# HELP wattmosaic_quality_score Overall data quality score",
            "# TYPE wattmosaic_quality_score gauge",
            f"wattmosaic_quality_score {quality.overall_score if quality else 0.0}",
            "# HELP wattmosaic_contract_info Contract and engine version",
            "# TYPE wattmosaic_contract_info gauge",
            f'wattmosaic_contract_info{{version="{CONTRACT_VERSION}",engine="{ENGINE_VERSION}"}} 1',
        ]
        return "\n".join(lines) + "\n"

    def runtime_stats(self) -> dict[str, Any]:
        return {
            "samples_ingested": self.samples_ingested,
            "events_ingested": self.events_ingested,
            "samples_persisted": self.samples_persisted,
            "events_persisted": self.events_persisted,
            "persistence_errors": self.persistence_errors,
            "cycles": self.cycles,
            "queue": self.persist_queue.stats(),
            "components": {
                name: {
                    "running": st.running,
                    "restarts": st.restarts,
                    "last_error": st.last_error,
                }
                for name, st in self.component_status.items()
            },
        }

    def last_successful_processing_at(self) -> datetime | None:
        return self.last_successful_processing_at


def _prom_label(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
