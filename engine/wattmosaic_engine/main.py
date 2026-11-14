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

The inference backend (``electrical`` / ``disaggregation`` / ``learning``) is
owned by another workstream.  It is imported **lazily and optionally**: if it is
not installed the engine still runs ingestion, storage, quality and the API, and
reports a clear ``inference backend not installed`` degraded state.

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
import math
import signal
import sys
import time
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any

from .api.facade import (
    ENGINE_VERSION,
    InvalidRequest,
    NotFound,
    confidence_tier_vocabulary,
    maybe_await,
)
from .api.server import APIServer
from .api.timeouts import HISTORY_TIMEOUT_BASE_S, HISTORY_TIMEOUT_PER_HOUR_S
from .config.settings import EngineConfig
from .contracts import (
    CONTRACT_VERSION,
    CTType,
    DataQualityReport,
    DeviceConfiguration,
    DeviceConfigurationSet,
    HealthResponse,
    InferenceResult,
    Measurement,
    Reconciliation,
    RelationKind,
    ServiceCommand,
    ServiceResult,
    SnapshotResponse,
    Topology,
    TopologyStatus,
    utcnow,
)
from .deprecations import collect as collect_deprecations
from .deprecations import declares_dedicated, warn_once
from .device_links import (
    device_for_entity,
    state_entity_for_device,
)
from .diagnostics.report import redact, run_diagnostics
from .electrical.nesting import infer_nesting
from .electrical.panel_tree import TREE_VERSION, build_panel_tree
from .ingestion.correlation import level_from_attributes
from .ingestion.discovery import discover_candidate_topology
from .ingestion.ha_source import HASource
from .ingestion.queue import BoundedQueue
from .quality.engine import DataQualityEngine
from .synchronization.aligner import (
    AlignedChannel,
    AlignedWindow,
    ChannelHealth,
    EnergyChannel,
    EnergyWindow,
    SampleAligner,
)

log = logging.getLogger("wattmosaic.main")

SAMPLE_BATCH_SIZE = 100
SAMPLE_FLUSH_INTERVAL_S = 2.0
MAINTENANCE_INTERVAL_S = 60.0

#: How long startup waits for the first Home Assistant subscription before
#: running topology discovery.  Bounded so a down Home Assistant cannot delay
#: the API and probes from coming up; 30 s matches ``HASource.wait_connected``.
TOPOLOGY_BOOTSTRAP_WAIT_S = 30.0

#: Minimum gap between discovery retries while none is configured.  Discovery is
#: a full state scan, so retrying every inference cycle (5 s) would be wasteful.
TOPOLOGY_RETRY_INTERVAL_S = 60.0
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

#: Downsample the nesting comparison grid to the channels' declared sampling
#: cadence instead of using the union of every distinct timestamp.
#:
#: **Off by default, and deliberately so.**  The union is 29–38× larger than the
#: data it describes (76 893 instants for 2 615 samples per channel over 12 h),
#: so resampling is a large CPU win, and the set of candidate pairs it produces is
#: identical on the reference installation.  But ``instants``, ``joint_instants``,
#: ``containment`` and the median evidence in the payload all describe *how many
#: instants were compared*, and those genuinely change.  Since the contract for
#: this change requires the payload to be byte-identical, enabling this by default
#: would silently alter a published response.  ``_sample_grid`` documents the
#: argument and the guard rails; turn it on only with a contract update that
#: accepts the changed evidence counts.
NESTING_RESAMPLE_GRID = False


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
# Helpers
# --------------------------------------------------------------------------- #


def _json_list(raw: Any, default: list[Any] | None = None) -> list[Any]:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, list) else (default or [])
        except ValueError:
            return default or []
    return default or []


def _json_obj(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            return {}
    return {}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _as_utc(value: Any) -> datetime | None:
    """Parse a stored timestamp into an aware UTC ``datetime``.

    ``ct_samples`` keeps timestamps as ISO **text**, so a ``datetime`` check is
    never true for a database row.  Naive values are treated as UTC because that
    is what the writer guarantees (``storage.database.iso``).
    """
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    return None


def _sampling_cadence_s(channels: Iterable[Any]) -> float | None:
    """The channels' declared sampling cadence, in seconds, or ``None``.

    Uses the median of the enabled channels' ``expected_interval_s`` so one
    mis-declared channel cannot change the grid.  Deliberately *not* a hardcoded
    15.7 s: the cadence is a property of the installation, and the reference
    install happens to declare 15.7 s while its cheapest channel samples every
    10 s.
    """
    declared = [
        float(ch.expected_interval_s)
        for ch in channels
        if getattr(ch, "enabled", False)
        and getattr(ch, "expected_interval_s", None)
        and float(ch.expected_interval_s) > 0
    ]
    if not declared:
        return None
    declared.sort()
    mid = len(declared) // 2
    return declared[mid] if len(declared) % 2 else (declared[mid - 1] + declared[mid]) / 2.0


def _sample_grid(
    samples: Mapping[str, Sequence[tuple[datetime, float]]],
    *,
    cadence_s: float | None,
    window_hours: float,
) -> list[datetime]:
    """Alignment instants for the nesting comparison.

    Why this is not simply the union of every timestamp
    ------------------------------------------------
    The union is not a sampling grid.  On the reference installation a 12 h
    window contains **76 893** distinct timestamps for 38 channels that each
    carry only ~2 615 samples: the union is dominated by per-channel clock jitter,
    so it is 29–38× larger than the data it describes.  Every instant then costs
    one comparison per pair in :func:`~wattmosaic_engine.electrical.nesting.infer_nesting`
    and one cell in every aligned series, for no new information: forward-filling
    a channel between two of its own samples cannot tell you anything the earlier
    sample did not.

    What it does instead
    --------------------
    Bucket the union into fixed ``cadence_s`` slots (anchored at the first
    timestamp so the result is deterministic) and keep the **latest real sample**
    in each slot.  Keeping a real timestamp rather than the bucket edge means
    :func:`_forward_fill` still sees true sample times, so no reading is ever
    invented or shifted earlier than it happened.

    The guard rails
    ---------------
    * ``cadence_s is None`` (nothing declared) → the union, i.e. the old
      behaviour.  Never guess a cadence from nothing.
    * Never coarser than the median inter-sample gap of the *fastest* channel:
      a cadence above the real sampling interval would throw away real readings
      and replace a measured series with a piecewise-constant one.  The step is
      therefore ``min(declared cadence, fastest median gap)``.
    * Never when the window holds fewer than two cadence periods: with less than
      that, the grid would collapse to one or two instants and containment would
      lose all meaning, so the union is kept.

    Equivalence
    -----------
    The **set of candidate pairs** (``kind``, ``child_ct_id``, ``parent_ct_id``)
    is byte-identical to the union grid at hours ∈ {2, 6, 12, 24} on the reference
    installation, and the whole payload is byte-identical when this returns the
    union.  It is **not** byte-identical when it resamples: ``instants``,
    ``joint_instants``, ``containment`` and the median evidence describe *how many
    instants were compared*, and comparing 2 526 instead of 76 893 genuinely
    changes those numbers.  That is a real, visible change to the response, which
    is why :data:`NESTING_RESAMPLE_GRID` defaults to ``False``.
    """
    union = sorted({stamp for series in samples.values() for stamp, _ in series})
    if cadence_s is None or not union:
        return union

    # Median inter-sample gap across channels: the fastest real cadence.
    gaps: list[float] = []
    for series in samples.values():
        ordered = sorted(series)
        deltas = [
            (later[0] - earlier[0]).total_seconds()
            for earlier, later in zip(ordered, ordered[1:], strict=False)
        ]
        deltas = [delta for delta in deltas if delta > 0]
        if deltas:
            deltas.sort()
            mid = len(deltas) // 2
            gaps.append(
                deltas[mid] if len(deltas) % 2 else (deltas[mid - 1] + deltas[mid]) / 2.0
            )
    if gaps:
        gaps.sort()
        mid = len(gaps) // 2
        fastest_gap = gaps[mid] if len(gaps) % 2 else (gaps[mid - 1] + gaps[mid]) / 2.0
        # Never stride past the fastest real sampling: that would discard real
        # readings rather than merely de-duplicating jittered ones.
        cadence_s = min(cadence_s, fastest_gap)

    if window_hours > 0:
        periods = (window_hours * 3600.0) / cadence_s
        if periods < 2.0:
            # Too short a window to resample: keep every instant.
            return union

    anchor = union[0]
    grid: list[datetime] = []
    current_slot: int | None = None
    last_stamp: datetime | None = None
    for stamp in union:
        slot = int((stamp - anchor).total_seconds() // cadence_s)
        if slot != current_slot:
            if last_stamp is not None:
                grid.append(last_stamp)
            current_slot = slot
        last_stamp = stamp
    if last_stamp is not None:
        grid.append(last_stamp)
    return grid


def _forward_fill(
    series: list[tuple[datetime, float]], grid: list[datetime]
) -> list[float]:
    """Project a sparse ``(timestamp, watts)`` series onto an aligned grid.

    A channel that did not update at an instant is represented by its last known
    value, which is what the synchroniser does for a point-in-time snapshot.  A
    value is *never* invented before the first sample: those slots are ``nan``,
    and ``infer_nesting`` skips non-finite samples rather than treating an
    absent reading as zero (ADR-0015 / defect #33).
    """
    out: list[float] = []
    index = 0
    current = math.nan
    ordered = sorted(series)
    for stamp in grid:
        while index < len(ordered) and ordered[index][0] <= stamp:
            current = ordered[index][1]
            index += 1
        out.append(current)
    return out


def _direct_load_series(
    topology: Topology, aligned: Mapping[str, list[float]]
) -> dict[str, list[float | None]]:
    """Per-container series of its *own* dedicated loads' sum, for closure.

    A container's "own loads" are the dedicated CTs whose canonical parent chain
    reaches it without passing through another feed, plus those dedicated CTs
    that declare no parent.  Feeds are deliberately excluded: closure asks
    whether *this child* is explained by the parent, so other feeds inside it
    would double count.
    """
    by_id = {ch.ct_id: ch for ch in topology.channels}
    groups: dict[str, list[str]] = {}
    for ch in topology.channels:
        if str(ch.ct_type) != "dedicated_device" or not ch.enabled:
            continue
        parent = ch.parent_ct_id
        if parent is None:
            continue
        # Walk up to the nearest container that is a feed; a dedicated load may
        # hang directly off a branch or off a main.
        seen: set[str] = set()
        current = parent
        while current is not None and current not in seen and current in by_id:
            seen.add(current)
            kind = str(by_id[current].ct_type)
            if kind in ("main", "branch"):
                groups.setdefault(current, []).append(ch.ct_id)
                break
            current = by_id[current].parent_ct_id

    out: dict[str, list[float | None]] = {}
    length = len(next(iter(aligned.values()))) if aligned else 0
    for container, loads in groups.items():
        total: list[float | None] = []
        for i in range(length):
            values = [
                aligned[cid][i]
                for cid in loads
                if cid in aligned and math.isfinite(aligned[cid][i])
            ]
            total.append(sum(values) if values else None)
        out[container] = total
    return out


def _event_time(event: Any) -> datetime | None:
    """``occurred_at`` of an HA device event, tolerant of mapping or object."""
    raw = (
        event.get("occurred_at")
        if isinstance(event, dict)
        else getattr(event, "occurred_at", None)
    )
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, str) and raw:
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def row_to_inference_result(row: dict[str, Any]) -> InferenceResult:
    return InferenceResult(
        device_id=row["device_id"],
        timestamp=row["timestamp"],
        power_w=_safe_float(row.get("power_w")),
        energy_kwh=None if row.get("energy_kwh") is None else _safe_float(row["energy_kwh"]),
        source=row.get("source") or "unknown",
        confidence=_safe_float(row.get("confidence")),
        model_id=row.get("model_id"),
        model_version=row.get("model_version"),
        operating_stage=row.get("operating_stage"),
        stage_status=row.get("stage_status") or "unknown",
        sample_count=int(row.get("sample_count") or 0),
        quality_status=row.get("quality_status") or "good",
        measurement_age_seconds=_safe_float(row.get("measurement_age_seconds")),
        supporting_ct_channels=_json_list(row.get("supporting_ct_channels_json")),
        explanation=_json_list(row.get("explanation_json")),
        provenance=_json_obj(row.get("provenance_json")),
        confidence_tier=row.get("confidence_tier") or "tier_0_unknown",
        monitoring_level=row.get("monitoring_level") or "unknown",
        power_behavior=row.get("power_behavior") or "unknown",
        tier_reasons=_json_list(row.get("tier_reasons_json")),
    )


def row_to_reconciliation(row: dict[str, Any] | None) -> Reconciliation:
    if not row:
        return Reconciliation()
    return Reconciliation(
        timestamp=row.get("timestamp") or utcnow(),
        topology_version=int(row.get("topology_version") or 1),
        main_power_w=_safe_float(row.get("main_power_w")),
        l1_power_w=None if row.get("l1_power_w") is None else _safe_float(row["l1_power_w"]),
        l2_power_w=None if row.get("l2_power_w") is None else _safe_float(row["l2_power_w"]),
        direct_measured_w=_safe_float(row.get("direct_measured_w")),
        inferred_w=_safe_float(row.get("inferred_w")),
        model_estimated_w=_safe_float(row.get("model_estimated_w")),
        configured_prior_w=_safe_float(row.get("configured_prior_w")),
        unknown_residual_w=_safe_float(row.get("unknown_residual_w")),
        #: Contract 1.4.0 residual split.  Rows written before schema 7 have no
        #: value (NULL) -- they genuinely never recorded an answer, so they read
        #: as the model default ``0.0`` rather than a fabricated split.
        unmonitored_w=_safe_float(row.get("unmonitored_w")),
        meter_disagreement_w=_safe_float(row.get("meter_disagreement_w")),
        #: Contract 1.5.0.  ``meter_spread_w`` is the unbounded meter-to-meter
        #: spread, distinct from the residual-clipped ``meter_disagreement_w``.
        #: A pre-schema-7 row has NULL and reads as the model default ``0.0``.
        meter_spread_w=_safe_float(row.get("meter_spread_w")),
        #: SQLite stores the flag as 0/1; NULL (an older row) is not a claim
        #: that the spread exceeded the residual.
        meter_spread_exceeds_residual=bool(row.get("meter_spread_exceeds_residual")),
        reconciliation_error_w=_safe_float(row.get("reconciliation_error_w")),
        reconciliation_error_ratio=_safe_float(row.get("reconciliation_error_ratio")),
        accounted_fraction=_safe_float(row.get("accounted_fraction")),
        measurement_uncertainty_w=_safe_float(row.get("measurement_uncertainty_w")),
        #: SQLite stores the flag as 0/1; NULL (a pre-schema-7 row) is not
        #: "confirmed" -- absent must never read as ``True``.
        topology_confirmed=bool(row.get("topology_confirmed")),
        quality_status=row.get("quality_status") or "good",
        explanation=_json_list(row.get("explanation_json")),
    )


def _linkable_state_entities(topology: Topology) -> set[str]:
    """Entities the topology declares as runtime signals for some device.

    Auto placement may only reason about entities the engine already links.  An
    undeclared entity is unknown to the topology -- it could be a scene, a
    template, or another integration's shadow -- and treating it as a load would
    be a guess about a device that does not exist yet.  The mapping wizard (and
    the user) decide what is linkable; this only reads that decision.

    Candidate *population* is now the state snapshot (see
    :mod:`wattmosaic_engine.ingestion.candidate_entities`); this remains the
    declaration read used by the learning/event paths and is kept as the single
    definition both places share.
    """
    from .ingestion.candidate_entities import declared_state_entities

    return declared_state_entities(topology)


def _prom_label(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


# --------------------------------------------------------------------------- #
# Inference backend (lazy, optional)
# --------------------------------------------------------------------------- #


class InferenceBackendUnavailable(RuntimeError):
    """No usable inference backend is installed."""


def load_inference_backend(config: EngineConfig, database: Any | None = None) -> Any:
    """Build the inference backend delivered by the orchestrator's adapter.

    This function is the *only* place that touches ``integration`` /
    ``disaggregation`` and it uses a guarded import so the engine always starts.
    ``integration.WattMosaicBackend`` exposes
    ``process(window, *, device_states, topology, models)`` (sync or async)
    returning an object/dict with ``results``, ``reconciliation`` and optional
    ``diagnostics``.

    ``integration.py`` is owned by another workstream; importing it lazily keeps
    the engine importable (and testable) when the model layer is absent.
    """
    try:
        from .integration import build_backend
    except Exception as exc:  # noqa: BLE001 - optional dependency by design
        raise InferenceBackendUnavailable(
            "inference backend not installed (wattmosaic_engine.integration): "
            f"{type(exc).__name__}: {exc}"
        ) from None
    try:
        return build_backend(config, database)
    except Exception as exc:  # noqa: BLE001
        raise InferenceBackendUnavailable(f"inference backend failed to initialise: {exc}") from exc


# --------------------------------------------------------------------------- #
# Engine core
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class ComponentStatus:
    name: str
    running: bool = False
    restarts: int = 0
    last_error: str | None = None
    last_started_at: datetime | None = None


class EngineCore:
    """Owns runtime state and implements :class:`EngineFacade`.

    The HTTP layer only sees this object, so the transport never depends on the
    inference implementation.
    """

    def __init__(self, config: EngineConfig, database: Any) -> None:
        self.config = config
        self.db = database
        self.started_at = time.monotonic()
        self.started_wall = utcnow()

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
        self.device_states: dict[str, Any] = {}
        #: Latest *measured* dimmer output per device, as a fraction of full
        #: scale, taken from the control entity's attributes.  Separate from
        #: ``device_states`` because the state is ``on`` while the level is what
        #: says how much: keeping them apart means a light that never reports
        #: brightness simply has no entry, rather than an implied 100 %.
        self.device_levels: dict[str, float] = {}
        self.latest_results: dict[str, InferenceResult] = {}
        self.latest_reconciliation: Reconciliation = Reconciliation()
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
        self._pending_backend_events: deque[Any] = deque(maxlen=512)
        #: Count of declared-link events evicted before they could mature.
        #: A silent drop here is invisible data loss: the sample never appears and
        #: nothing in the status surface explains why.  Reported instead.
        self.events_evicted_before_maturity = 0
        #: How many events arrived for entities no device declares.  A count, not
        #: a dict: retaining one entry per undeclared entity would grow without
        #: bound (~500 distinct entities in a typical installation) and would let
        #: the engine derive transitions for devices that do not exist.  The count
        #: still surfaces a topology gap without pretending the entity is
        #: attributable.
        self.unlinked_events_ignored = 0
        #: Events that *were* for a declared device but came from a CT power entity
        #: rather than a runtime state entity.  They are persisted as observations
        #: but must not become the device's state (see ``on_event``); the count
        #: makes that distinction visible instead of silent.
        self.power_events_not_state = 0
        #: Declared-link state transitions awaiting the next inference window.
        #: Bounded by the number of declared devices, not by HA's event rate.
        self._pending_transitions: deque[dict[str, Any]] = deque(maxlen=4096)
        self.topology_cache: Topology | None = None
        #: Why startup discovery produced no topology (``None`` = no problem).
        #: Surfaced by ``/health`` so a fresh install cannot look healthy while
        #: having nothing to infer, which is how the original defect hid.
        self.topology_bootstrap_error: str | None = None
        #: One-shot guard so the "no topology" warning is emitted once, not every
        #: inference interval.
        self._topology_skip_logged = False
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
        self.events_persisted = 0
        self.persistence_errors = 0
        self.cycles = 0

    # -- lifecycle -------------------------------------------------------- #
    async def start(self, *, start_loops: bool = True) -> None:
        """Start ingestion (and optionally the internal loops).

        ``_run_engine`` passes ``start_loops=False`` because the supervisor owns
        those loops so it can restart them.  Tests and ``--once`` use the default.
        """
        backend = None
        try:
            backend = load_inference_backend(self.config, self.db)
            self.inference_backend = backend
            self.inference_error = None
            # The adapter builds the model registry; share it with the core so
            # /models and model commands use the database.
            registry = getattr(backend, "registry", None)
            if registry is not None:
                self.model_registry = registry
            log.info("inference backend loaded: %s", type(backend).__name__)
            self._sync_learning_state(backend)
        except InferenceBackendUnavailable as exc:
            self.inference_error = str(exc)
            self.learning_active = False
            log.warning("running in degraded mode: %s", exc)

        await self.hasource.start(on_sample=self.on_sample, on_event=self.on_event)
        # The first state snapshot arrives asynchronously from ``_prime()``;
        # waiting a bounded time here is what makes discovery see real entities
        # instead of an empty cache on a genuine fresh install.
        await self.bootstrap_topology(wait_connected_s=TOPOLOGY_BOOTSTRAP_WAIT_S)
        # One notice per deprecated CT, at the one place a topology becomes
        # active.  Never in the inference loop: ``warn_once`` deduplicates, but a
        # per-sample call would still walk the topology every cycle (ADR-0029).
        self._announce_deprecations()
        if start_loops:
            self._tasks = [
                asyncio.create_task(self._writer_loop(), name="wm-writer"),
                asyncio.create_task(self._quality_loop(), name="wm-quality"),
                asyncio.create_task(self._inference_loop(), name="wm-inference"),
                asyncio.create_task(self._maintenance_loop(), name="wm-maintenance"),
            ]

    async def bootstrap_topology(self, *, wait_connected_s: float = 0.0) -> dict[str, Any]:
        """Discover a candidate topology when the database has none.

        A fresh install used to look healthy while doing nothing: with no
        topology, ``run_inference_once`` returns ``{"skipped": "no topology
        configured"}``, that return value was never logged, and the integration
        published only global entities.  Discovery is therefore run once at
        startup when no topology is stored, and the outcome is logged so the
        condition is never silent again.

        ``hasource.start()`` only *schedules* the connection; the first states
        arrive later from ``_prime()``.  Calling discovery immediately would
        therefore read an empty cache and conclude "no monitor", which is why
        ``wait_connected_s`` bounds how long startup waits for the first
        successful subscription.  A zero wait is used by the retry path, which
        runs after the connection is expected to be up.

        This only ever *creates a candidate*.  It never overwrites an existing
        topology, never confirms one, and never invents device state links: a
        candidate is what the user reviews in the integration wizard.  A failure
        here is recorded and logged but must not stop the engine.
        """
        if not self.config.topology_bootstrap:
            return {"skipped": "topology bootstrap disabled by configuration"}
        if self._active_topology() is not None:
            return {"skipped": "topology already configured"}

        if wait_connected_s > 0:
            waiter = getattr(self.hasource, "wait_connected", None)
            if waiter is not None:
                try:
                    await maybe_await(waiter(wait_connected_s))
                except Exception as exc:  # noqa: BLE001 - never block startup
                    log.debug("waiting for Home Assistant failed: %s", exc)

        try:
            states = await self.hasource.state_snapshot()
            registries = await self.hasource.registries()
            discovered = discover_candidate_topology(states, registries)
        except Exception as exc:  # noqa: BLE001 - never block startup
            self.topology_bootstrap_error = f"{type(exc).__name__}: {exc}"
            log.warning("topology discovery failed: %s", self.topology_bootstrap_error)
            return {"error": self.topology_bootstrap_error}

        if not states:
            # Not the same condition as "no monitor installed": Home Assistant
            # simply has not answered yet.  Reporting it as "no monitor" would
            # send the user looking for hardware instead of a connection problem.
            message = (
                "Home Assistant returned no states; topology discovery will retry "
                "once the connection is up"
            )
            self.topology_bootstrap_error = message
            log.info("%s", message)
            return {"deferred": message}

        if not discovered.channels or not discovered.devices:
            # Distinguish "no CT hardware" from "CT hardware, no load circuits".
            # A monitor that exposes only its own feed pair produces channels but
            # no devices, and telling the user to install a monitor they already
            # own would send them to the wrong problem.
            if discovered.channels:
                message = (
                    "found a CT monitor but no load circuits on it; only panel feeds "
                    "were discovered, so there is nothing to attribute. Check that the "
                    "monitor's circuit entities are enabled in Home Assistant."
                )
            else:
                message = (
                    "no multi-channel energy monitor found in Home Assistant; "
                    "configure a CT monitor or import a topology before inference can run"
                )
            self.topology_bootstrap_error = message
            log.warning("%s", message)
            return {"skipped": message, "channels": len(discovered.channels)}

        try:
            self.db.save_topology(discovered, note="startup: candidate discovery")
        except Exception as exc:  # noqa: BLE001 - a write failure must not crash startup
            self.topology_bootstrap_error = f"{type(exc).__name__}: {exc}"
            log.warning(
                "topology discovery could not be stored: %s", self.topology_bootstrap_error
            )
            return {"error": self.topology_bootstrap_error}

        self.topology_cache = self.db.load_topology()
        self.topology_bootstrap_error = None
        self.db.log_system_event(
            "info",
            "topology",
            "candidate topology discovered at startup",
            {"channels": len(discovered.channels), "devices": len(discovered.devices)},
        )
        log.info(
            "discovered candidate topology: %d channels, %d devices "
            "(unconfirmed; review it in the WattMosaic integration)",
            len(discovered.channels),
            len(discovered.devices),
        )
        return {
            "channels": len(discovered.channels),
            "devices": len(discovered.devices),
            "status": "candidate",
        }

    async def maybe_bootstrap_topology(self) -> dict[str, Any] | None:
        """Retry discovery while none is configured, at most once per interval.

        Startup cannot always win the race against the first Home Assistant
        connection (a slow App start, a Supervisor restart, a Core restart while
        the engine is already running).  Without a retry the engine would sit
        topology-less until the next process restart, which is the same silent
        no-op the defect was about.  This is cheap when a topology exists: it
        returns before doing any work.
        """
        if not self.config.topology_bootstrap:
            return None
        if self._active_topology() is not None:
            return None
        now = time.monotonic()
        if now - self._last_topology_attempt < TOPOLOGY_RETRY_INTERVAL_S:
            return None
        self._last_topology_attempt = now
        return await self.bootstrap_topology()

    def _deprecations_payload(self) -> list[dict[str, Any]]:
        """Deprecated manual declarations from the active topology (ADR-0029).

        Additive diagnostics only: ``warn_once`` is *not* called here, because
        ``/health`` is polled every few seconds.  The one warning per CT is
        emitted where a topology is installed (see ``_announce_deprecations``).
        """
        if not self.config.deprecate_dedicated_declaration:
            return []
        topology = self._active_topology()
        if topology is None or not declares_dedicated(topology):
            # Cheap negative on the in-memory topology: the probe path must not
            # read the configuration table just to prove there is nothing to say.
            return []
        try:
            configurations = self.db.load_device_configurations()
        except Exception:  # noqa: BLE001 - a read failure must not break /health
            configurations = None
        try:
            return [notice.as_dict() for notice in collect_deprecations(topology, configurations)]
        except Exception:  # noqa: BLE001 - diagnostics must never fail
            return []
    def _announce_deprecations(self) -> None:
        """Log one ``WARNING`` per deprecated CT for the current topology.

        Called from topology installation only.  ``warn_once`` deduplicates across
        calls for the life of the process, so repeated installations (and the
        per-cycle inference path, which never calls this) cannot flood the log.
        """
        if not self.config.deprecate_dedicated_declaration:
            return
        topology = self._active_topology()
        if topology is None:
            return
        try:
            configurations = self.db.load_device_configurations()
            notices = collect_deprecations(topology, configurations)
        except Exception:  # noqa: BLE001 - announcement must never break startup
            return
        warn_once(notices, log)

    def _topology_health(self) -> dict[str, Any]:
        """Topology presence for ``/health``; a missing topology is not healthy."""
        topology = self._active_topology()
        if topology is None:
            return {
                "configured": False,
                "reason": self.topology_bootstrap_error or "no topology configured",
            }
        raw_status = getattr(topology.status, "value", topology.status)
        return {
            "configured": True,
            "status": str(raw_status),
            "version": topology.version,
            "channels": len(topology.channels),
            "devices": len(topology.devices),
        }

    async def stop(self) -> None:
        self._stopping = True
        self._stop_event.set()
        log.info("stopping engine components")
        try:
            await self.hasource.stop()
        except Exception:  # noqa: BLE001 - shutdown must complete
            log.exception("error stopping HA source")
        self.persist_queue.close()
        # Stop workers first so nothing else enqueues, then drain what is left.
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        try:
            await asyncio.wait_for(self._drain_persist_queue(), timeout=10.0)
        except TimeoutError:
            log.warning("persistence queue did not drain within 10s")
        except Exception:  # noqa: BLE001 - shutdown must still complete
            log.exception("failed to drain the persistence queue during shutdown")
        # Persist whatever energy buckets / runs are still open so a restart does
        # not lose the tail of the current bucket.
        close_backend = getattr(self.inference_backend, "close", None)
        if callable(close_backend):
            try:
                close_backend()
            except Exception:  # noqa: BLE001
                log.exception("failed to flush the inference backend on shutdown")
        try:
            self.db.checkpoint()
        except Exception:  # noqa: BLE001
            log.exception("database checkpoint failed")
        try:
            self.db.log_system_event("info", "lifecycle", "engine shutdown complete")
        except Exception:  # noqa: BLE001
            log.debug("shutdown system-event write failed", exc_info=True)
        self.latest_quality = self.latest_quality  # keep last value for diagnostics
        log.info("engine stopped")

    async def _drain_persist_queue(self) -> None:
        """Persist everything left in the queue, on the event-loop thread.

        The SQLite connection is created on the loop thread (and the writer loop
        uses it there), so draining must not use ``asyncio.to_thread`` — doing so
        raises ``sqlite3.ProgrammingError``.  Instead, persist in bounded chunks
        and yield to the loop between them so shutdown stays responsive.
        """
        while True:
            batch: list[tuple[str, Any]] = []
            while len(batch) < SAMPLE_BATCH_SIZE:
                item = self.persist_queue.get_nowait()
                if item is None:
                    break
                batch.append(item)
            if not batch:
                return
            self._persist_batch(batch)
            await asyncio.sleep(0)

    # -- ingestion callbacks --------------------------------------------- #
    async def on_sample(self, measurement: Measurement) -> None:
        self.samples_ingested += 1
        self.aligner.add(measurement)
        self.persist_queue.put_nowait(("sample", measurement))

    async def on_event(self, event: Any) -> None:
        entity_id = getattr(event, "entity_id", None)
        # Only entities the topology actually declares can produce attribution.
        #
        # HA emits ~30 state changes/second in a typical installation, but only 40
        # entities are declared link targets.  Every undeclared entity used to be
        # buffered for the learning pass and turned into a sample keyed by its
        # raw entity id, which:
        #   * filled the bounded buffer with noise (~2 000 events are needed to
        #     cover the 68 s maturity window, against a 512-event cap, so the
        #     buffer was permanently full and *every* event was evicted before
        #     it could mature — so every sample was rejected);
        #   * wrote ~100 000 `device_events` rows in 5 minutes for entities no
        #     device owns.
        # An unlinked entity is still a real observation, so it is counted — but
        # it is **not** persisted.  Home Assistant emits ~30 state changes/second
        # across ~500 entities in a typical installation, and only 40 are declared
        # link targets.  Writing every undeclared change grew `device_events` at
        # ~1.5 GB/day (99.98 % of all rows) for entities that no device owns and
        # that nothing ever reads: `recent_device_events` is not consulted in
        # production.  The spec requires avoiding unbounded growth, and a run
        # left recording for days must not fill the disk with codebook noise.
        # The counter is the audit signal: it still shows how much traffic the
        # topology fails to explain.
        topology = self._active_topology()
        link = device_for_entity(topology, entity_id) if entity_id else None
        self.events_ingested += 1
        if link is None:
            self.unlinked_events_ignored += 1
            return
        device_id, _ct_ids = link
        # ``device_states`` is consumed by the disaggregation engine, which keys it
        # by ``DeviceTopology.device_id`` — not by the HA entity id.  Storing the
        # entity id made every state lookup miss, so state-transition inference was
        # silently dead in production.
        #
        # Only a *runtime state* entity may write this slot.  A CT power entity
        # (``sensor.water_heater_power``) is linked for measurement, but it is not the
        # device's state: numeric readings never get here (``HASource`` consumes
        # them as measurements), yet availability changes do.  Writing
        # ``"unavailable"`` here replaced ``"off"``, and the next real ``"off"``
        # then read as a transition — one bridge blip manufactured two phantom
        # transitions.  The event is still persisted below; it just cannot claim
        # to be the device's state.
        if not state_entity_for_device(topology, device_id, entity_id):
            self.power_events_not_state += 1
            self.persist_queue.put_nowait(("event", event))
            return
        previous = self.device_states.get(device_id)
        self.device_states[device_id] = event.new_state
        # Record the measured output level alongside the state.  A dimmer's
        # state stays ``on`` across a brightness change, so this is the only
        # place the *amount* is known.  Absent brightness publishes nothing
        # (rather than 100 %), which keeps a non-dimmable fixture indistinguishable
        # from a dimmer that has not dimmed yet.
        level = level_from_attributes(getattr(event, "attributes", None))
        if level is None:
            self.device_levels.pop(device_id, None)
        else:
            self.device_levels[device_id] = level
        # Persist every declared event: the audit trail wants the observation even
        # when it is not a transition.
        self.persist_queue.put_nowait(("event", event))
        if previous is None:
            # First observation of this device in this process.  ``HASource``
            # replays the whole current snapshot at startup, carrying each
            # entity's *old* ``last_updated``; those entries are a state, not a
            # transition.  Queueing them produced samples timestamped hours in
            # the past whose baseline/event window had no CT readings at all, so
            # every one was rejected ``missing_measurement``.  A restart must not
            # invent a transition it did not observe.
            return
        if previous == event.new_state:
            # Attribute churn: not a transition.  The level (above) still moves.
            return
        self._pending_transitions.append(
            {
                "device_id": device_id,
                "previous_state": previous,
                "new_state": event.new_state,
                "occurred_at": _event_time(event),
            }
        )
        # Buffer for the inference backend's learning pass.  Only declared links
        # reach this point, so the buffer holds a handful of events rather than
        # the whole house's noise, and each one can actually mature.
        pending = getattr(self, "_pending_backend_events", None)
        if pending is None:
            pending = self._pending_backend_events = deque(maxlen=512)
        if len(pending) >= (pending.maxlen or 0):
            # The deque silently discards the oldest item.  Count it rather than
            # losing the evidence: a full buffer means samples are being dropped
            # and the status surface must say so.
            self.events_evicted_before_maturity += 1
        pending.append(event)

    # -- background loops -------------------------------------------------- #
    async def _writer_loop(self) -> None:
        while not self._stopping:
            try:
                item = await asyncio.wait_for(self.persist_queue.get(), SAMPLE_FLUSH_INTERVAL_S)
            except TimeoutError:
                continue
            batch: list[tuple[str, Any]] = [] if item is None else [item]
            deadline = time.monotonic() + SAMPLE_FLUSH_INTERVAL_S
            while len(batch) < SAMPLE_BATCH_SIZE:
                nxt = self.persist_queue.get_nowait()
                if nxt is None:
                    break
                batch.append(nxt)
                if time.monotonic() > deadline:
                    break
            if batch:
                try:
                    self._persist_batch(batch)
                except Exception:  # noqa: BLE001 - one failed batch is not fatal
                    self.persistence_errors += 1
                    log.exception("failed to persist a batch of %d records", len(batch))

    def _persist_batch(self, batch: list[tuple[str, Any]]) -> None:
        sample_rows: list[dict[str, Any]] = []
        events: list[Any] = []
        for kind, payload in batch:
            if kind == "sample":
                sample_rows.append(
                    {
                        "ct_id": payload.ct_id,
                        "entity_id": payload.entity_id,
                        "value_w": payload.effective_power_w(),
                        "raw_value": payload.value,
                        "unit": payload.unit,
                        "state_timestamp": payload.state_timestamp,
                        "received_at": payload.received_at,
                        "physical_timestamp": payload.physical_timestamp,
                        "quality": payload.quality,
                        "repeated": payload.repeated,
                        "sequence": payload.sequence,
                    }
                )
            else:
                events.append(payload)
        if sample_rows:
            self.samples_persisted += self.db.insert_ct_samples(sample_rows)
        for event in events:
            self.db.insert_device_event(
                {
                    "event_id": event.event_id,
                    "device_id": event.device_id,
                    "entity_id": event.entity_id,
                    "event_type": event.event_type,
                    "previous_state": event.previous_state,
                    "new_state": event.new_state,
                    "attributes": event.attributes,
                    "occurred_at": event.occurred_at,
                    "received_at": event.received_at,
                }
            )
            self.events_persisted += 1

    def _active_topology(self) -> Topology | None:
        if self.topology_cache is None:
            self.topology_cache = self.db.load_topology()
        return self.topology_cache

    # -- window construction ----------------------------------------------- #
    @staticmethod
    def _aligner_key(channel: Any) -> str:
        """Aligner buffer key for a topology channel (entity id without domain)."""
        entity_id = str(getattr(channel, "entity_id", "") or "")
        return entity_id.split(".", 1)[-1] if "." in entity_id else entity_id

    @staticmethod
    def _topology_energy_channel(channel: Any) -> bool:
        """True when a topology channel carries a cumulative energy counter."""
        metadata = getattr(channel, "metadata", None) or {}
        unit = str(metadata.get("unit", "")) if isinstance(metadata, dict) else ""
        if unit in ("kWh", "Wh"):
            return True
        ct_id = str(getattr(channel, "ct_id", "") or "")
        return ct_id.endswith(("_energy", "_return_energy"))

    def _power_window(self, topology: Topology) -> AlignedWindow:
        """Aligned power window keyed by topology ``ct_id``.

        The aligner buffers are keyed by the Home Assistant entity id without its
        domain prefix (``sensor.room_a_power`` → ``room_a_power``), while the topology
        refers to channels by ``ct_id``.  This method joins the two so the
        inference backend and the quality engine both see topology channel ids,
        with the originating ``entity_id`` attached to each reading.

        Energy counters are excluded: they live in :meth:`energy_window` and must
        never be scored or solved as power.
        """
        energy_keys = set(self.aligner.energy_channels())
        pairs = [
            (channel.ct_id, self._aligner_key(channel))
            for channel in topology.channels
            if not self._topology_energy_channel(channel)
        ]
        pairs = [(ct_id, key) for ct_id, key in pairs if key not in energy_keys]
        raw = self.aligner.window(
            channels=[key for _, key in pairs], max_age_s=self.config.stale_after_s
        )
        channels: dict[str, AlignedChannel] = {}
        for ct_id, key in pairs:
            channel = raw.channels.get(key)
            if channel is None:
                continue
            channels[ct_id] = replace(channel, channel=ct_id)
        return AlignedWindow(at=raw.at, max_age_s=raw.max_age_s, channels=channels)

    def _channel_health_for(self, topology: Topology) -> dict[str, ChannelHealth]:
        """Channel health keyed by topology ``ct_id`` (aligner uses entity names)."""
        health = self.aligner.channel_health()
        out: dict[str, ChannelHealth] = {}
        for channel in topology.channels:
            if self._topology_energy_channel(channel):
                continue
            key = self._aligner_key(channel)
            entry = health.get(key)
            if entry is not None:
                out[channel.ct_id] = replace(entry, channel=channel.ct_id)
        return out

    def _channel_noise_floors(self) -> dict[str, float]:
        """Measured innovation MAD per topology ``ct_id``, for CT step detection.

        The quality layer already computes this for every channel
        (``SampleAligner.channel_health().noise_mad_w``, a scaled MAD of
        consecutive-sample differences).  Recomputing it here would be a second
        definition of the same quantity and could disagree with the number the
        quality report shows, so it is read back and only re-keyed from the
        aligner's entity name to the topology's ``ct_id``.

        A channel with no health entry is simply absent: the detector then uses
        its configured scalar floor, which is the historical behaviour.
        """
        topology = self._active_topology()
        if topology is None:
            return {}
        by_key = {
            self._aligner_key(channel): channel.ct_id
            for channel in topology.channels
            if not self._topology_energy_channel(channel)
        }
        if not by_key:
            return {}
        floors: dict[str, float] = {}
        for key, health in self.aligner.channel_health().items():
            cid = by_key.get(key)
            if cid is None or health.noise_mad_w is None:
                continue
            value = float(health.noise_mad_w)
            if value >= 0.0 and value == value:  # finite, non-negative
                floors[cid] = value
        return floors

    def _energy_window_for(
        self, topology: Topology | None, max_age_s: float
    ) -> EnergyWindow:
        """Energy window keyed by topology ``ct_id`` where a channel is known.

        Energy stays in its own domain; this only fixes naming so quality output
        uses the same identifiers as the topology.
        """
        raw = self.aligner.energy_window(max_age_s=max_age_s)
        if topology is None:
            return raw
        by_key = {self._aligner_key(c): c.ct_id for c in topology.channels}
        if not by_key:
            return raw
        channels: dict[str, EnergyChannel] = {}
        for key, channel in raw.channels.items():
            ct_id = by_key.get(key, key)
            channels[ct_id] = replace(channel, channel=ct_id)
        return EnergyWindow(at=raw.at, max_age_s=raw.max_age_s, channels=channels)

    def _evaluate_quality(self) -> DataQualityReport:
        topology = self._active_topology()
        if topology is not None:
            window = self._power_window(topology)
            health = self._channel_health_for(topology)
        else:
            window = self.aligner.window(max_age_s=self.config.stale_after_s)
            health = self.aligner.channel_health()
        energy_window = self._energy_window_for(topology, self.config.stale_after_s)
        energy_health = self.aligner.energy_health()
        report = self.quality_engine.evaluate(
            window,
            health,
            topology=topology,
            reconciliation=self.latest_reconciliation,
            unavailable=self.hasource.unavailable_entities,
            energy_window=energy_window,
            energy_health=energy_health,
        )
        self.latest_quality = report
        return report

    async def _quality_loop(self) -> None:
        while not self._stopping:
            try:
                self._evaluate_quality()
                self.last_successful_processing_at = utcnow()
            except Exception:  # noqa: BLE001
                log.exception("data quality evaluation failed")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), max(1.0, self.config.inference_interval_s)
                )
                return
            except TimeoutError:
                continue

    async def _inference_loop(self) -> None:
        while not self._stopping:
            if self.inference_backend is not None:
                try:
                    await self.run_inference_once()
                except Exception:  # noqa: BLE001 - never kill the loop
                    log.exception("inference cycle failed")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), max(1.0, self.config.inference_interval_s)
                )
                return
            except TimeoutError:
                continue

    async def run_inference_once(self) -> dict[str, Any]:
        """Run one inference cycle if a backend is attached.  Returns a summary."""
        topology = self._active_topology()
        if topology is None:
            # Startup discovery may have lost the race against the first Home
            # Assistant connection, or the database may have been empty and the
            # connection appeared later.  Keep trying so the engine cannot sit
            # permanently inert; this is a no-op once a topology exists.
            retry = await self.maybe_bootstrap_topology()
            if retry is not None and retry.get("status") == "candidate":
                topology = self._active_topology()
        if topology is None:
            # Never silent: this is the condition that made a healthy-looking
            # install emit nothing.  Log once per process, not once per 5 s.
            if not self._topology_skip_logged:
                self._topology_skip_logged = True
                log.warning(
                    "inference is not running: no topology configured "
                    "(discovery found nothing and none was imported). "
                    "Open the WattMosaic integration to configure CT channels."
                )
            return {"skipped": "no topology configured"}
        self._topology_skip_logged = False
        window = self._power_window(topology)
        # Per-cycle context the disaggregation engine reads back off the window.
        # Built in one dict so the frozen window is replaced once.
        #
        # ``device_levels`` publishes each device's last measured output level.
        # Attribution reads it to interpolate a measured dimming curve, so a
        # light at 40 % reports its watts at 40 % rather than its full-on value.
        # Devices with no reported level are absent from the mapping, never
        # defaulted to 100 %.
        #
        # ``channel_noise_floor_w`` publishes the quality layer's per-channel
        # measured MAD (innovation sigma).  A single scalar floor cannot serve a
        # 0.3 W inverter CT and a 30 W load-manager CT at once: too small turns
        # every tick into an event, too large hides a real step.  Without this
        # the detector's per-channel mapping was never reachable in production.
        metadata = dict(window.metadata)
        if self.device_levels:
            metadata["device_levels"] = dict(self.device_levels)
        noise_floors = self._channel_noise_floors()
        if noise_floors:
            metadata["channel_noise_floor_w"] = noise_floors
        if metadata != dict(window.metadata):
            window = replace(window, metadata=metadata)
        # Attach the declared-link transitions observed since the last cycle.
        # Without this the engine's ``_collect_transitions`` sees neither
        # ``state_transitions`` nor ``previous_states`` and reports no events at
        # all, so state-transition inference never runs in production.
        if self._pending_transitions:
            window = replace(
                window,
                state_transitions=tuple(self._pending_transitions),
                previous_states={
                    str(item["device_id"]): str(item["previous_state"])
                    for item in self._pending_transitions
                    if item.get("previous_state") is not None
                },
            )
            self._pending_transitions.clear()
        backend = self.inference_backend
        if backend is None:
            return {"skipped": self.inference_error or "inference backend not installed"}
        process = getattr(backend, "process", None)
        if process is None:
            raise InferenceBackendUnavailable("inference backend has no process() method")
        events = list(self._pending_backend_events)
        self._pending_backend_events.clear()
        batch = await maybe_await(
            process(
                window,
                device_states=self.device_states,
                topology=topology,
                models=getattr(backend, "models", None),
                events=events,
            )
        )
        results = list(
            getattr(batch, "results", [])
            or (batch.get("results", []) if isinstance(batch, dict) else [])
        )
        reconciliation = getattr(batch, "reconciliation", None)
        if reconciliation is None and isinstance(batch, dict):
            reconciliation = batch.get("reconciliation")
        if results:
            self.latest_results.update({r.device_id: r for r in results})
            self.db.insert_inference_results(results)
        if reconciliation is not None:
            if isinstance(reconciliation, dict):
                reconciliation = Reconciliation.model_validate(reconciliation)
            self.latest_reconciliation = reconciliation
            self.db.insert_reconciliation(reconciliation)
        # Track what the backend actually did so ``learning_active`` never
        # advertises a pipeline that is not running.
        self._sync_learning_state(backend)
        self.inference_last_run_at = utcnow()
        self.last_successful_processing_at = self.inference_last_run_at
        self.cycles += 1
        diagnostics = getattr(batch, "diagnostics", None)
        return {
            "results": len(results),
            "reconciliation": bool(reconciliation),
            "backend": diagnostics if isinstance(diagnostics, dict) else {},
        }

    def _sync_learning_state(self, backend: Any) -> None:
        active = getattr(backend, "learning", None) is not None
        self.learning_active = bool(active and getattr(backend, "learning_active", True))

    async def _maintenance_loop(self) -> None:
        while not self._stopping:
            try:
                self.aligner.prune()
                self.db.prune(self.config.retention_days_raw, self.config.retention_days_aggregates)
            except Exception:  # noqa: BLE001
                log.exception("maintenance cycle failed")
            try:
                await asyncio.wait_for(self._stop_event.wait(), MAINTENANCE_INTERVAL_S)
                return
            except TimeoutError:
                continue

    # -- facade: health & status ------------------------------------------- #
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
                # Deprecated manual ``dedicated_device`` declarations (ADR-0029).
                # Additive and schema-free, like ``learning_status`` above: the
                # objects themselves match the frozen schema.  The direct
                # measurement path is untouched, so this list is information
                # only and never a refusal.
                "deprecations": self._deprecations_payload(),
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
        """Name *why* learning is blocked, without changing what the engine does.

        A user reading ``blocked`` cannot act on a reason label like
        ``ambiguous_topology``: the cause is that the topology is still a
        candidate, and the fix is to confirm it.  This only adds the explanation
        -- the topology is **never** auto-confirmed here (contract §6), because
        the wiring association is the user's assertion to make.
        """
        from .learning.reporting import CONFIRM_REMEDY

        dominant = status.get("dominant_rejection_reason")
        if status.get("effective_state") == "blocked" and dominant == "ambiguous_topology":
            # ``ambiguous_topology`` is only ever stamped because the wiring
            # association is unverified, so the detail names that directly.
            status["blocked_reason_detail"] = "topology_not_confirmed"
            status.setdefault("remedy", CONFIRM_REMEDY)

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
                # ``blocked_reason_detail`` names the *cause* of a silent block
                # (contract §6); it never changes what the engine does.
                "blocked_reason_detail": learning_status.get("blocked_reason_detail"),
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

    def snapshot(self) -> SnapshotResponse:
        rows = self.db.latest_inference_results()
        results = [row_to_inference_result(r) for r in rows]
        rec_row = self.db.latest_reconciliation()
        return SnapshotResponse(
            timestamp=utcnow(),
            reconciliation=row_to_reconciliation(rec_row),
            results=results,
            quality=self.latest_quality,
        )

    # -- facade: devices ---------------------------------------------------- #
    def _effective_configurations(
        self, topology: Topology | None
    ) -> dict[str, Any]:
        """Resolve every device's effective configuration once per call.

        The configuration layer is part of the optional inference backend, so the
        import is guarded: a plain engine (no disaggregation package) still serves
        ``/devices`` and simply omits the resolved configuration.
        """
        if topology is None:
            return {}
        try:
            from .disaggregation.configuration import effective_configs

            return effective_configs(topology)
        except Exception:  # noqa: BLE001 - optional backend must not break reads
            return {}

    @staticmethod
    def _configuration_payload(config: Any) -> dict[str, Any] | None:
        """JSON-ready view of one :class:`EffectiveConfig` (enums as strings)."""
        if config is None:
            return None

        def _text(value: Any) -> Any:
            return getattr(value, "value", value)

        def _list(value: Any) -> list[Any]:
            return [str(item) for item in (value or ())]

        return {
            "device_type": _text(getattr(config, "device_type", None)),
            "connectivity": _text(getattr(config, "connectivity", None)),
            "monitoring": _text(getattr(config, "monitoring", None)),
            "behavior": _text(getattr(config, "behavior", None)),
            "monitored_by_ct_ids": _list(getattr(config, "monitored_by_ct_ids", ())),
            "load_entity_ids": _list(getattr(config, "load_entity_ids", ())),
            "mode_entity_ids": _list(getattr(config, "mode_entity_ids", ())),
            "expected_on_w": getattr(config, "expected_on_w", None),
            "expected_standby_w": getattr(config, "expected_standby_w", None),
            "shared_unmodelled": bool(getattr(config, "shared_unmodelled", False)),
            "notes": _list(getattr(config, "notes", ())),
        }

    @staticmethod
    def _tier_text(result: InferenceResult | None) -> str | None:
        if result is None:
            return None
        tier = getattr(result, "confidence_tier", None)
        if tier is None:
            return None
        return str(getattr(tier, "value", tier))

    def _latest_results_map(self) -> dict[str, Any]:
        """Latest known result per device: in-memory first, stored rows second.

        ``latest_inference_results()`` is a whole-table GROUP BY over
        ``inference_results``.  Asking it once per device is an N+1 query:
        measured at ~670 ms for 28 devices against a 343 560-row table, which
        would stall the event loop on every caller.  Fetch it once, index it,
        and let the in-memory map win, so a result computed since the last
        persist is not shadowed by an older row.

        The database fallback matters for more than speed.  Right after a
        restart ``latest_results`` is empty until the first inference cycle, so
        a reader that consulted only the in-memory map would report every
        device as unknown/0 W while ``/devices`` reported real power -- the
        same install showing two different answers on two tabs.

        Cost note: ``latest_inference_results`` is a covering-index scan whose
        price grows with the number of retained rows (~24 ms warm and ~590 ms
        cold at the production 343k).  It is deliberately not bounded by
        timestamp: a device that stopped reporting a month ago still has a
        legitimate latest row, and dropping it would be a correctness change,
        not an optimisation.  Callers that can be slow therefore run this in a
        thread -- see the ``devices`` and ``topology_tree`` routes.
        """
        try:
            latest_rows = self.db.latest_inference_results()
        except Exception:  # noqa: BLE001
            # Not silent: right after a restart the in-memory map is empty, so a
            # swallowed error here makes every device read as unknown on both
            # ``/devices`` and ``/topology/tree`` with nothing in the log to
            # explain it.  Degrade, but say so.
            log.warning(
                "latest results unavailable; serving in-memory results only",
                exc_info=True,
            )
            latest_rows = []
        merged: dict[str, Any] = {}
        for row in latest_rows:
            device_key = row.get("device_id")
            if not device_key:
                continue
            try:
                merged[str(device_key)] = row_to_inference_result(row)
            except Exception:  # noqa: BLE001
                continue
        merged.update(self.latest_results)
        return merged

    def devices(self) -> list[dict[str, Any]]:
        topology = self._active_topology()
        devices = topology.devices if topology else []
        configurations = self._effective_configurations(topology)
        recorded_results = self._latest_results_map()
        out: list[dict[str, Any]] = []
        for dev in devices:
            recorded = recorded_results.get(dev.device_id)
            out.append(
                {
                    "device_id": dev.device_id,
                    "name": dev.name,
                    "phase": dev.phase,
                    "double_pole": dev.double_pole,
                    "topology_status": dev.topology_status,
                    "dedicated_ct_ids": list(dev.dedicated_ct_ids),
                    "branch_ct_ids": list(dev.branch_ct_ids),
                    "latest_result": (
                        recorded.model_dump(mode="json") if recorded is not None else None
                    ),
                    "confidence_tier": self._tier_text(recorded),
                    "configuration": self._configuration_payload(
                        configurations.get(dev.device_id)
                    ),
                    "model": self._active_model_summary(dev.device_id),
                }
            )
        return out

    def _latest_result_from_db(self, device_id: str) -> InferenceResult | None:
        try:
            rows = self.db.latest_inference_results()
        except Exception:  # noqa: BLE001
            return None
        for row in rows:
            if row.get("device_id") == device_id:
                try:
                    return row_to_inference_result(row)
                except Exception:  # noqa: BLE001
                    return None
        return None

    def _active_model_summary(self, device_id: str) -> dict[str, Any] | None:
        try:
            row = self.db.active_model(device_id)
        except Exception:  # noqa: BLE001
            return None
        if not row:
            return None
        return {
            "model_type": row.get("model_type"),
            "version": row.get("version"),
            "status": row.get("status"),
            "confidence": row.get("confidence"),
            "sample_count": row.get("sample_count"),
            "trained_to": row.get("trained_to"),
        }

    def device(self, device_id: str) -> dict[str, Any]:
        topology = self._active_topology()
        dev = topology.device_map().get(device_id) if topology else None
        result = self.latest_results.get(device_id) or self._latest_result_from_db(device_id)
        if dev is None and result is None:
            raise NotFound(f"device {device_id!r} not found")
        configuration = None
        if dev is not None:
            configurations = self._effective_configurations(topology)
            configuration = self._configuration_payload(configurations.get(device_id))
        return {
            "device_id": device_id,
            "name": dev.name if dev else None,
            "topology": dev.model_dump(mode="json") if dev else None,
            "latest_result": result.model_dump(mode="json") if result else None,
            "confidence_tier": self._tier_text(result),
            "configuration": configuration,
            "model": self._active_model_summary(device_id),
            "history": self._device_history(device_id),
        }

    def _device_history(self, device_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """Recent inference results for one device, newest first.

        ``Database.inference_history`` filters with ``timestamp >= since``.  The
        pre-1.6.0 call passed ``since=utcnow()``, which excludes every row that
        has already been written -- and a stored result is always in the past.
        The endpoint therefore always reported ``history: []`` while the table
        held rows (``docs/assessment_gap_analysis.md`` G17).  The lower bound is
        now an explicit, configured lookback.
        """
        try:
            since = utcnow() - timedelta(
                seconds=max(1.0, float(self.config.history_lookback_s))
            )
            rows = self.db.inference_history(device_id, since=since, limit=limit)
        except Exception:  # noqa: BLE001
            return []
        return [_safe_history_row(r) for r in rows]

    def confidence_tiers(self) -> dict[str, Any]:
        """Static ordered tier vocabulary (labels clients must not hard-code)."""
        return confidence_tier_vocabulary()

    # -- facade: topology --------------------------------------------------- #
    def topology(self) -> Topology:
        topology = self._active_topology()
        if topology is None:
            return Topology(version=0, status=TopologyStatus.CANDIDATE, notes=["no topology configured"])
        return topology

    def set_topology(self, topology: Any) -> Topology:
        if not isinstance(topology, Topology):
            topology = Topology.model_validate(topology)
        if topology.status == TopologyStatus.CONFIRMED:
            # Replacing a confirmed topology creates a candidate revision unless
            # the caller explicitly marks it confirmed.
            pass
        self.db.save_topology(topology, note="api: replace topology")
        self.topology_cache = self.db.load_topology()
        self._announce_deprecations()
        self.db.log_system_event(
            "info",
            "topology",
            "topology replaced via API",
            {"channels": len(topology.channels), "devices": len(topology.devices)},
        )
        return self.topology_cache or topology

    def confirm_topology(self) -> Topology:
        topology = self._active_topology()
        if topology is None:
            raise NotFound("no topology to confirm")
        confirmed = topology.model_copy(
            update={
                "status": TopologyStatus.CONFIRMED,
                "devices": [
                    d.model_copy(update={"topology_status": TopologyStatus.CONFIRMED})
                    for d in topology.devices
                ],
            }
        )
        self.db.save_topology(confirmed, note="api: confirm topology")
        self.topology_cache = self.db.load_topology()
        self._announce_deprecations()
        self.db.log_system_event("info", "topology", "topology confirmed via API")
        return self.topology_cache or confirmed

    def panel_tree(self, *, include_nesting: bool = False) -> dict[str, Any]:
        """Read-time panel → circuits → devices hierarchy (contract §1).

        Cheap by construction: it reads the already-cached topology and one
        indexed query for the latest result per device (the same ~25 ms query
        ``/devices`` makes), and it never scans CT samples.  The expensive
        nesting inference runs **only** when ``include_nesting`` is explicitly
        requested; even then it can only merge duplicate panels or add advisory
        suggestions, never change leaf counts.
        """
        topology = self._active_topology()
        if topology is None:
            return {
                "version": TREE_VERSION,
                "status": "candidate",
                "generated_at": utcnow().isoformat().replace("+00:00", "Z"),
                "topology_version": 0,
                "stats": {
                    "panels": 0,
                    "circuits": 0,
                    "devices": 0,
                    "unplaced_devices": 0,
                    "unplaced_circuits": 0,
                    "max_depth": 0,
                    "duplicate_feeds": 0,
                },
                "roots": [],
                "flat": {"panels": [], "circuits": [], "devices": []},
                "notes": ["no topology configured"],
            }
        nesting = self.nesting_candidates() if include_nesting else None
        return build_panel_tree(
            topology,
            latest_results=self._latest_results_map(),
            nesting=nesting,
        )

    def nesting_candidates(self, *, hours: float = 12.0) -> dict[str, Any]:
        """Propose parent links and duplicate feeds from stored history.

        This is a *read*: it never writes a parent link.  The evidence is
        measured containment (a child cannot exceed its feed) plus closure (the
        parent's own loads plus the child must not over-explain the parent), and
        every result is a candidate the integration wizard asks the user to
        confirm.
        """
        topology = self._active_topology()
        if topology is None:
            return {"candidates": [], "reason": "no topology configured"}
        channels = topology.channels
        if not channels:
            return {"candidates": [], "reason": "topology has no channels"}

        end = utcnow()
        start = end - timedelta(hours=max(0.5, float(hours)))
        # ``ct_samples.ct_id`` is the *aligner* key (the entity object id), not the
        # topology's ``ct_id``: the ingestion path derives it with
        # ``entity_id.split(".", 1)[1]``.  Querying the topology id directly
        # silently returns nothing, which would look exactly like "no history
        # yet" and leave the wizard with no proposals forever.
        key_to_ct: dict[str, str] = {}
        for ch in channels:
            if not ch.enabled:
                continue
            key_to_ct[self._aligner_key(ch)] = ch.ct_id
        rows = self.db.ct_samples_between(start, end, ct_ids=sorted(key_to_ct))
        if not rows:
            return {
                "candidates": [],
                "reason": "no stored CT samples in the comparison window",
                "window_hours": float(hours),
            }

        samples: dict[str, list[tuple[datetime, float]]] = {}
        for row in rows:
            value = row.get("value_w")
            if not isinstance(value, (int, float)):
                continue
            if not math.isfinite(float(value)):
                continue
            # ``state_timestamp`` / ``received_at`` are stored as ISO text.
            stamp = _as_utc(row.get("state_timestamp")) or _as_utc(row.get("received_at"))
            if stamp is None:
                continue
            # Re-key from the aligner key back to the topology ``ct_id`` so the
            # rest of this method (and the caller) speaks one namespace.
            ct_id = key_to_ct.get(str(row["ct_id"]))
            if ct_id is None:
                continue
            samples.setdefault(ct_id, []).append((stamp, float(value)))
        if len(samples) < 2:
            return {"candidates": [], "reason": "fewer than two channels have samples"}

        grid = (
            _sample_grid(
                samples,
                cadence_s=_sampling_cadence_s(channels),
                window_hours=max(0.5, float(hours)),
            )
            if NESTING_RESAMPLE_GRID
            else sorted({stamp for series in samples.values() for stamp, _ in series})
        )
        if len(grid) < 10:
            return {
                "candidates": [],
                "reason": "too few distinct instants to compare",
                "instants": len(grid),
            }
        aligned = {
            cid: _forward_fill([entry for entry in series], grid)
            for cid, series in samples.items()
        }

        by_id = {ch.ct_id: ch for ch in channels}
        container_ids = [
            ch.ct_id
            for ch in channels
            if ch.enabled and str(ch.ct_type) in ("main", "branch")
        ]
        direct_loads = _direct_load_series(topology, aligned)

        report: dict[str, Any] = {}
        candidates = infer_nesting(
            {
                cid: {
                    "ct_type": str(ch.ct_type),
                    # Declared relationships are *facts the user has already
                    # stated*: the two legs of a declared double-pole pair are
                    # one circuit, not one wire measured twice, and a declared
                    # parent is not re-proposed.  ``phase`` is passed so the
                    # written-back leg is readable, but it is never used to
                    # reject a candidate -- on the reference installation the
                    # phase labels are contradicted by the measurements.
                    "phase": str(ch.phase),
                    "paired_ct_id": ch.paired_ct_id,
                    "parent_ct_id": ch.parent_ct_id,
                }
                for cid, ch in by_id.items()
            },
            {cid: values for cid, values in aligned.items() if cid in by_id},
            container_ct_ids=container_ids,
            direct_loads=direct_loads,
            report=report,
        )

        # Attach the candidate parent(s) to each channel as *proposals* so the
        # wizard can pre-select a single unambiguous one without ever writing it.
        proposals: dict[str, list[str]] = {}
        nested = [c for c in candidates if c.kind == "nested"]
        for candidate in nested:
            proposals.setdefault(candidate.child_ct_id, []).append(candidate.parent_ct_id)
        # Duplicate feeds are the same *conductor* seen twice, not a nesting.
        # They are offered separately because they answer a different question:
        # "is this CT an alias of another one?" rather than "what feeds it?".
        alias_proposals: dict[str, str] = {}
        for candidate in candidates:
            if candidate.kind == "duplicate":
                alias_proposals[candidate.child_ct_id] = candidate.parent_ct_id

        return {
            "window_hours": float(hours),
            "instants": len(grid),
            "channels_compared": len(aligned),
            "candidates": [c.as_dict() for c in candidates],
            "candidate_parents": {cid: sorted(p) for cid, p in proposals.items()},
            "candidate_aliases": dict(sorted(alias_proposals.items())),
            # What was considered and rejected, so the wizard can say "three
            # channels are ambiguous" rather than silently showing nothing.
            "ambiguous": report.get("ambiguous", []),
            "suppressed": report.get("suppressed", []),
            # Loads whose level measurement cannot establish.  Reported so the
            # UI can ask the user instead of presenting a guess as evidence.
            "not_inferable": report.get("not_inferable", []),
            "nodes": report.get("nodes", []),
            "notes": [
                "Candidates only: no parent link is written until the user confirms it.",
                "Containment proves a child never exceeds its feed; it does not by "
                "itself prove which feed.",
                "Only a panel feed can be placed by measurement. A load is contained "
                "by every panel while idle, so loads are never proposed and the "
                "wizard asks the user.",
                "A child whose level cannot be established is listed as ambiguous "
                "and is never pre-selected.",
                "A duplicate feed is the same conductor measured twice: it is offered "
                "as an alias, never summed as a second feed.",
            ],
        }

    # -- facade: quality & diagnostics -------------------------------------- #
    def quality_report(self) -> DataQualityReport:
        if self.latest_quality is None:
            return self._evaluate_quality()
        return self.latest_quality

    def quality(self) -> DataQualityReport:
        return self.quality_report()

    # -- facade: audit artifacts (read-only) -------------------------------- #
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

    def relations(self, kind: str | None = None) -> dict[str, Any]:
        """Stored typed edges, grouped by kind.

        ``kind`` filters to one relation kind; without it every kind is
        reported separately, because ``DEVICE_CT_ASSOCIATION``,
        ``CT_MEASUREMENT_OVERLAP`` and ``ELECTRICAL_PARENT_CHILD`` answer
        different questions and must never be merged into one list.
        """
        out: dict[str, list[dict[str, Any]]] = {}
        for relation_kind in RelationKind:
            if kind is not None and str(getattr(relation_kind, "value", relation_kind)) != str(
                kind
            ):
                continue
            try:
                rows = self.db.relations_of_kind(relation_kind)
            except Exception:  # noqa: BLE001
                rows = []
            if rows or kind is not None:
                out[str(getattr(relation_kind, "value", relation_kind))] = [
                    self._jsonable(r) for r in rows
                ]
        return {
            "kinds": out,
            "counts": {name: len(rows) for name, rows in out.items()},
            "requires_confirmation": sorted(
                {
                    str(row.get("subject_id"))
                    for rows in out.values()
                    for row in rows
                    if row.get("requires_confirmation")
                }
            ),
        }

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
        since = utcnow() - timedelta(hours=max(0.0, float(hours)))
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

    @staticmethod
    def _jsonable(value: Any) -> Any:
        """JSON-safe copy of a stored row (enums/datetimes to plain values)."""
        if isinstance(value, dict):
            return {str(k): EngineCore._jsonable(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [EngineCore._jsonable(item) for item in value]
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, Enum):
            return value.value
        return value

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

    # -- facade: models / commands / export --------------------------------- #
    @staticmethod
    def _model_dump(value: Any) -> Any:
        if value is None:
            return None
        if hasattr(value, "as_dict"):
            return value.as_dict()
        if hasattr(value, "model_dump"):
            return value.model_dump(mode="json")
        return value

    def models(self, device_id: str) -> dict[str, Any]:
        """Model versions come from the shared registry when it is available.

        The registry is created by the inference adapter, so this never
        constructs a second one; it only reuses ``self.model_registry``.  Falls
        back to the database for a facade used without a backend.
        """
        registry = self.model_registry
        try:
            if registry is not None:
                active = self._model_dump(registry.active(device_id))
                versions = [self._model_dump(v) for v in registry.versions(device_id)]
            else:
                active = self.db.active_model(device_id)
                versions = self.db.model_versions(device_id)
        except Exception:  # noqa: BLE001
            active, versions = None, []
        if active is None and not versions:
            raise NotFound(f"no models for device {device_id!r}")
        return {"device_id": device_id, "active": active, "versions": versions}

    def models_summary(self) -> dict[str, Any]:
        topology = self._active_topology()
        out: dict[str, Any] = {}
        for dev in topology.devices if topology else []:
            active = self._active_model_summary(dev.device_id)
            if active:
                out[dev.device_id] = active
        return {"devices": out, "count": len(out)}

    async def command(self, command: ServiceCommand) -> ServiceResult:
        """Handle a ``ServiceCommand``.

        Destructive operations are refused with HTTP 409 semantics handled by the
        facade raising ``InvalidRequest`` only for bad input; unsupported
        commands report ``ok=False`` rather than crashing the API.
        """
        name = command.command
        if name == "discover_placements":
            lookback = command.parameters.get("lookback_hours", 24.0)
            try:
                hours = float(lookback)
            except (TypeError, ValueError):
                raise InvalidRequest("lookback_hours must be a number") from None
            apply = command.parameters.get("apply")
            include_groups = command.parameters.get("include_groups", True)
            max_candidates = command.parameters.get("max_candidates", 200)
            # The bounds and the type checks live in ``discover_placements`` so the
            # route and the command cannot disagree; only the coercion that turns
            # JSON scalars into the declared parameter types happens here, and a
            # bad value is the caller's error, not a crash.
            if not isinstance(include_groups, bool):
                raise InvalidRequest("include_groups must be a boolean")
            if isinstance(max_candidates, bool) or not isinstance(max_candidates, int):
                raise InvalidRequest("max_candidates must be an integer")
            data = await self.discover_placements(
                lookback_hours=hours,
                apply=None if apply is None else bool(apply),
                include_groups=include_groups,
                max_candidates=max_candidates,
            )
            return ServiceResult(ok=bool(data.get("ok")), command=name, data=data)
        if name in ("start_learning", "resume_learning", "pause_learning"):
            active = name != "pause_learning"
            backend = self.inference_backend
            setter = getattr(backend, "set_learning_active", None)
            if callable(setter):
                setter(active)
            if active and (backend is None or getattr(backend, "learning", None) is None):
                return ServiceResult(
                    ok=False,
                    command=name,
                    detail="learning pipeline is unavailable; enable it and restart the engine",
                )
            self.learning_active = active
            self.db.log_system_event(
                "info", "learning", f"learning {'resumed' if active else 'paused'} via API"
            )
            return ServiceResult(
                ok=True,
                command=name,
                detail=f"learning {'active' if active else 'paused'}",
            )
        if name == "set_device_configuration":
            return self._set_device_configuration(command)
        if name == "run_diagnostics":
            return ServiceResult(ok=True, command=name, data=self.diagnostics())
        if name == "export_device_data":
            if not command.device_id:
                raise InvalidRequest("export_device_data requires device_id")
            try:
                data = self.export(command.device_id)
            except NotFound as exc:
                return ServiceResult(ok=False, command=name, detail=str(exc))
            return ServiceResult(ok=True, command=name, data=data)
        if name in ("freeze_model", "unfreeze_model"):
            if not command.device_id:
                raise InvalidRequest(f"{name} requires device_id")
            frozen = name == "freeze_model"
            self.db.set_model_frozen(command.device_id, frozen)
            self.db.log_system_event("info", "models", f"{name} {command.device_id}")
            return ServiceResult(
                ok=True, command=name, detail=f"model {'frozen' if frozen else 'unfrozen'}"
            )
        if name in ("rebuild_topology",):
            return ServiceResult(
                ok=False,
                command=name,
                detail="rebuild_topology requires the inference backend; no automatic rebuild performed",
            )
        if name in (
            "accept_sample",
            "reject_sample",
            "rebuild_model",
            "reset_device_model",
        ):
            return ServiceResult(
                ok=False,
                command=name,
                detail=f"{name} requires the learning backend, which is not installed",
            )
        return ServiceResult(ok=False, command=name, detail="unsupported command")

    def _set_device_configuration(self, command: ServiceCommand) -> ServiceResult:
        """Persist one device's declarative configuration and project it.

        An empty ``parameters`` mapping means *remove* this device's
        configuration: deletion must clear the topology projection too, otherwise
        a stale monitoring level or mode entity would outlive the declaration.
        """
        from .disaggregation.configuration import project_configuration

        name = command.command
        if not command.device_id:
            raise InvalidRequest("set_device_configuration requires device_id")
        device_id = command.device_id

        topology = self._active_topology()
        if topology is None:
            raise NotFound("no topology to project the device configuration onto")

        current = self.db.load_device_configurations()
        by_device = current.by_device()
        parameters = dict(command.parameters or {})

        if not parameters:
            by_device.pop(device_id, None)
            new_set = DeviceConfigurationSet(
                version=current.version + 1,
                updated_at=utcnow(),
                devices=list(by_device.values()),
            )
            self.db.save_device_configurations(new_set)
            projected = project_configuration(topology, new_set)
            self.db.save_topology(projected, note=f"api: remove configuration {device_id}")
            self.topology_cache = projected
            self.db.log_system_event(
                "info", "configuration", f"set_device_configuration {device_id} (removed)"
            )
            return ServiceResult(
                ok=True,
                command=name,
                data={"device_id": device_id, "removed": True},
            )

        # ``device_id`` comes from the command, never from the payload: a caller
        # must not be able to write another device's row through ``parameters``,
        # so any copy supplied there is dropped before validation.
        payload = {k: v for k, v in parameters.items() if k != "device_id"}
        try:
            config = DeviceConfiguration.model_validate({"device_id": device_id, **payload})
        except Exception as exc:  # noqa: BLE001 - invalid input is a result, not a crash
            return ServiceResult(
                ok=False, command=name, detail=f"invalid configuration: {exc}"
            )

        by_device[device_id] = config
        new_set = DeviceConfigurationSet(
            version=current.version + 1,
            updated_at=utcnow(),
            devices=list(by_device.values()),
        )
        self.db.save_device_configurations(new_set)
        projected = project_configuration(topology, new_set)
        self.db.save_topology(projected, note=f"api: set configuration {device_id}")
        self.topology_cache = projected
        self.db.log_system_event("info", "configuration", f"set_device_configuration {device_id}")
        return ServiceResult(
            ok=True,
            command=name,
            data={
                "device_id": device_id,
                "configuration": config.model_dump(mode="json"),
                "topology_version": projected.version,
            },
        )

    # -- facade: auto placement (ADR-0024) ---------------------------------- #
    @staticmethod
    def _candidate_state_entities(
        states: Iterable[Mapping[str, Any]],
        registries: Mapping[str, Any] | None,
        topology: Topology,
    ) -> tuple[list[Any], dict[str, str]]:
        """The entities this run may reason about, from states + registries.

        Candidate *population* is deliberately not derived from the topology
        (contract §2): doing that starved auto placement to one device's
        entities, so a shared lighting circuit could never be resolved.  The
        topology is still consulted, but only to subtract what it already
        declares.  Kept as a thin seam so the population rule can be exercised
        without a recorder or a database.
        """
        from .ingestion.candidate_entities import candidate_state_entities

        return candidate_state_entities(states, registries, topology)

    async def discover_placements(
        self,
        *,
        lookback_hours: float = 24.0,
        apply: bool | None = None,
        include_groups: bool = True,
        max_candidates: int = 200,
    ) -> dict[str, Any]:
        """Learn which fixtures sit on which CT row from recorded history.

        This is the only facade entry point that creates devices from an
        inference, so every guard that makes that safe is visible here:

        * **opt-in** -- ``config.auto_placement`` (or an explicit ``apply=True``)
          gates the write.  Analysing without writing is always allowed, because
          it changes nothing;
        * **candidate population is structural** -- every ``light``/``switch``/
          ``fan``/``input_boolean`` state is considered, minus entities the user
          already declared and WattMosaic's own output.  Names, areas and
          manufacturers are never used to decide membership (contract §2);
        * **read-only history** -- the recorder is queried, no service is called;
        * **branch rows only** -- a main row or a dedicated measurement refuses
          the placement with a reason instead of double counting the circuit;
        * **user declarations win** -- an entity a person configured is never
          rewritten;
        * **no fabrication** -- a transition that did not move measured power is
          not placed.  Fixtures whose individual wattage is not identifiable are
          reported as an advisory *group*, never as a device (tier B).

        Returns the plan, the refusals, the advisory groups and whether anything
        was applied, so a caller can show the user exactly what was learned and
        what it declined to believe.
        """
        # Group fitting lands in a separate workstream; guard the import so this
        # module stays importable (and testable) before it exists.
        try:
            from .disaggregation.group_fit import fit_shared_rows
        except ImportError:  # group fitting lands separately
            fit_shared_rows = None

        # Validate here as well as in the transport: the facade is a public entry
        # point (the ``discover_placements`` service command reaches it directly),
        # and an infinite lookback would otherwise overflow when building the
        # window.
        if isinstance(lookback_hours, bool) or not isinstance(lookback_hours, (int, float)):
            raise InvalidRequest("lookback_hours must be a number")
        lookback_hours = float(lookback_hours)
        if not math.isfinite(lookback_hours) or lookback_hours <= 0 or lookback_hours > 2160:
            raise InvalidRequest("lookback_hours must be greater than 0 and at most 2160")
        if isinstance(max_candidates, bool) or not isinstance(max_candidates, int):
            raise InvalidRequest("max_candidates must be an integer")
        if max_candidates < 1:
            raise InvalidRequest("max_candidates must be at least 1")

        topology = self._active_topology()
        if topology is None:
            return {
                "ok": False,
                "detail": "no topology to place devices on",
                "applied": False,
                "lookback_hours": lookback_hours,
                "topology_version": None,
                "stats": {
                    "candidate_entities": 0,
                    "refused_entities": 0,
                    "branch_rows": 0,
                    "observations": 0,
                    "accepted": 0,
                    "groups": 0,
                    "withheld": 0,
                },
                "accepted": [],
                "refused": {},
                "groups": [],
                "withheld": {},
                "skipped": {},
                "notes": ["no topology to place devices on"],
            }

        from .disaggregation.auto_placement import merge_placements, plan_placements
        from .disaggregation.configuration import project_configuration
        from .ingestion.correlation import (
            correlate,
            estimate_row_noise,
            observations_from_history,
            parse_history_payload,
        )

        states_payload = await maybe_await(self.hasource.state_snapshot())
        registries = await maybe_await(self.hasource.registries())
        candidates, candidate_refused = self._candidate_state_entities(
            states_payload or [], registries, topology
        )
        found = len(candidates)
        candidates = candidates[:max_candidates]
        entity_ids = [candidate.entity_id for candidate in candidates]

        row_channels = {
            channel.ct_id: channel
            for channel in topology.channels
            if channel.enabled and channel.ct_type in (CTType.BRANCH, CTType.DEDICATED_DEVICE)
        }
        # Contract §4: a group is never emitted for a main row or a
        # dedicated-device row.  A main aggregate contains every downstream
        # fixture, so fitting it would "identify" wattages that were never
        # separately measured -- exactly the fabrication tier B must not do.
        excluded_ct_ids = frozenset(
            channel.ct_id
            for channel in topology.channels
            if channel.ct_type in (CTType.MAIN, CTType.DEDICATED_DEVICE)
        )
        notes: list[str] = []
        stats: dict[str, Any] = {
            "candidate_entities": len(candidates),
            "refused_entities": len(candidate_refused),
            "branch_rows": len(row_channels),
            "observations": 0,
            "accepted": 0,
            "groups": 0,
            "withheld": 0,
        }
        if found > len(candidates):
            # Say it out loud: a truncated run is not "the engine found nothing
            # else", and the omitted entities were never analysed.
            notes.append(
                f"analysed the first {len(candidates)} of {found} candidate entities "
                f"(max_candidates={max_candidates}); increase it to consider the rest"
            )

        from .entity_evidence import MODE_ONLY_DOMAINS, domain_of
        from .ingestion.correlation import CANDIDATE_DOMAINS

        def _named_refusals() -> dict[str, str]:
            """Candidate refusals worth naming individually.

            Only domains a user could plausibly have meant as a fixture are
            listed: a whole-house snapshot contains hundreds of sensors, and
            naming each one would bury the interesting refusals.  The count of
            *every* entity looked at and declined stays in
            ``stats.refused_entities`` and is restated in ``notes``, so a refusal
            is never rounded into silence.
            """
            listed = CANDIDATE_DOMAINS | MODE_ONLY_DOMAINS
            return {
                entity_id: reason
                for entity_id, reason in candidate_refused.items()
                if domain_of(entity_id) in listed
            }

        def _short_circuit(reason: str, skipped: dict[str, Any]) -> dict[str, Any]:
            """The frozen response shape with an honest "nothing was analysed"."""
            return {
                "ok": True,
                "applied": False,
                "lookback_hours": lookback_hours,
                "topology_version": topology.version,
                "stats": stats,
                "accepted": [],
                "refused": _named_refusals(),
                "groups": [],
                "withheld": {},
                "skipped": skipped,
                "notes": [reason, *notes],
            }

        if not row_channels or not entity_ids:
            return _short_circuit(
                "no branch CT rows or no candidate state entities",
                {
                    "reason": "no branch CT rows or no candidate state entities",
                    "channels": len(row_channels),
                    "entities": len(entity_ids),
                },
            )

        end = utcnow()
        start = end - timedelta(hours=max(lookback_hours, 0.1))
        wanted = [*entity_ids, *(ch.entity_id for ch in row_channels.values())]
        # The history query is the only command whose cost grows with the window
        # *and* the candidate count, and ``history()`` cannot tell a timeout from
        # an empty recorder -- both come back as ``{}``.  Give it a budget derived
        # from the window it was asked for (a 7-day whole-house request takes 17 s
        # on the reference install against a 30 s default) and refuse rather than
        # report "no recorded history" when the answer is incomplete.
        #
        # That budget is the budget for the *whole* window: ``history()`` splits
        # a window longer than its slice threshold into bounded slices and divides
        # this total across them.  It has to, because one whole-window frame with
        # attributes is 79.6 MB at 48 h and 282.5 MB at 168 h against a 64 MiB
        # websocket frame cap -- the connection was dropped, not answered.  The
        # slicing is a property of the source (it knows the cap); discovery only
        # says how much time the window is worth.
        history_timeout_s = max(HISTORY_TIMEOUT_BASE_S, HISTORY_TIMEOUT_PER_HOUR_S * lookback_hours)
        payload = await self.hasource.history(
            wanted,
            start=start,
            end=end,
            include_attributes=True,
            timeout_s=history_timeout_s,
        )
        if not payload:
            if getattr(self.hasource, "last_history_error", None):
                return _short_circuit(
                    "history query did not complete",
                    {
                        "reason": "history query did not complete",
                        "lookback_hours": lookback_hours,
                        # Names the failed slice and the transport error, e.g.
                        # "slice 3 of 14 [..) failed: TimeoutError: ...".  A
                        # partial answer is never returned as if it were the whole
                        # window, so the only honest report is which part of the
                        # window could not be read.
                        "detail": str(self.hasource.last_history_error),
                    },
                )
            return _short_circuit(
                "no recorded history",
                {"reason": "no recorded history", "lookback_hours": lookback_hours},
            )

        state_history, readings, levels = parse_history_payload(payload)
        # Readings are keyed by entity id in the payload; remap to ct_id so the
        # correlation (and the placement) speak the topology's vocabulary.
        by_entity = {
            str(ch.entity_id): ch.ct_id for ch in topology.channels if ch.entity_id
        }
        row_readings = {
            by_entity[entity]: rows for entity, rows in readings.items() if entity in by_entity
        }
        row_noise = estimate_row_noise(row_readings)
        observations = observations_from_history(
            entity_states={e: rows for e, rows in state_history.items() if e in entity_ids},
            row_readings=row_readings,
            # Levels are deliberately *not* re-filtered here.  A level is only
            # ever read for an entity that ``entity_states`` yielded, so the
            # filter above already decides which entities can contribute one;
            # a second filter would be unprovable protection against a refactor
            # rather than a guard that any test can distinguish.
            entity_levels=levels,
        )
        stats["observations"] = len(observations)
        report = correlate(observations, now=end, row_noise_w=row_noise)

        configurations = self.db.load_device_configurations()
        # Contract §3.1: a write needs **both** the request's consent and the
        # stored opt-in.  ``apply`` defaults to false, so ``None`` is read-only;
        # an explicit ``apply=True`` cannot override a disabled opt-in, because
        # that switch is the user's standing decision.
        allow_apply = apply is True and bool(self.config.auto_placement)
        channels = topology.channel_map()
        labels = {candidate.entity_id: candidate.friendly_name for candidate in candidates}
        plan = plan_placements(
            report=report, topology=topology, configurations=configurations, labels=labels
        )

        def _channel_label(ct_id: str) -> tuple[str, str | None]:
            channel = channels.get(ct_id)
            if channel is None:
                return ct_id, None
            return channel.name or ct_id, channel.panel_name

        groups: list[dict[str, Any]] = []
        withheld: dict[str, str] = {}
        # Entities tier A is about to place.  Computed before the group fit so the
        # advisory pass can exclude them: the same fixture must not appear with
        # both a measured wattage (Placed) and a fitted one (Group).
        placed_entity_ids = {
            placement.configuration.state_entity_ids[0]
            for placement in plan.accepted
            if placement.configuration.state_entity_ids
        }
        if include_groups and fit_shared_rows is not None:
            # Contract §4 forbids a group on a main or dedicated-device row, and
            # keeps those rows out of the fit entirely: a main aggregate contains
            # every downstream fixture, so fitting it would "identify" wattages
            # that were never separately measured.  The exclusion is applied
            # here, to the *inputs*, so the call itself stays exactly the frozen
            # §4 signature (no extra keyword for the other workstream to honour).
            #
            # CT rows are filtered to *numeric* states first.  A recorder really
            # does return ``unavailable`` for a CT row (a Refoss bridge restart
            # during the 7-day window on the reference install does exactly
            # that), and a non-numeric state is not a measurement -- the same
            # rule ``parse_history_payload`` already applies when building
            # ``readings`` above, which is why correlation never saw this.  The
            # fit's numeric parser raises ``ValueError`` on such a row instead of
            # skipping it, and that only became reachable once 168 h started
            # returning at all: the old code dropped the connection first.  The
            # real fix belongs in ``group_fit``'s row parser; filtering here keeps
            # discovery from handing a wattage fit a row that has no wattage.
            fitted = fit_shared_rows(
                row_readings={
                    channel.ct_id: [
                        dict(row)
                        for row in payload.get(channel.entity_id, ())
                        if isinstance(row, Mapping) and _is_numeric_state(row)
                    ]
                    for channel in row_channels.values()
                    if channel.ct_id not in excluded_ct_ids and channel.entity_id
                },
                entity_states={
                    entity: [dict(row) for row in rows if isinstance(row, Mapping)]
                    for entity, rows in payload.items()
                    if entity in entity_ids
                },
                entity_levels={
                    entity: {stamp: float(fraction) for stamp, fraction in rows}
                    for entity, rows in levels.items()
                    if entity in entity_ids
                },
                row_noise_w=row_noise,
            )
            for group in fitted:
                ct_label, panel_name = _channel_label(group.ct_id)
                # A fixture that tier A placed already owns a device with a
                # measured wattage.  Leaving it in a group as well would report
                # the same watts twice (once measured, once fitted) and would
                # make the advisory card contradict the Placed card.  The
                # aggregate fit is the weaker claim, so it yields.
                members = [
                    {
                        "entity_id": member.entity_id,
                        "name": labels.get(member.entity_id, member.entity_id),
                        "estimated_w": member.estimated_w,
                    }
                    for member in group.members
                    if member.entity_id not in placed_entity_ids
                ]
                if not members:
                    continue
                kept = [m for m in group.members if m.entity_id not in placed_entity_ids]
                for member in kept:
                    # A single-fixture group is *identified* by the aggregate
                    # fit, so "it co-toggles with another fixture" would be
                    # false.  The honest reason is that the aggregate fit found
                    # it while transition-pair correlation did not: that is a
                    # weaker claim, so it still does not become a device.
                    #
                    # The test is the *original* group size, not the surviving
                    # members: a two-fixture group whose other member was placed
                    # solo still co-toggles, and saying otherwise would invent a
                    # reason the evidence does not support.
                    withheld[member.entity_id] = (
                        f"identified_by_aggregate_fit_on_{group.ct_id}"
                        if len(group.members) == 1
                        else f"co_toggles_with_another_fixture_on_{group.ct_id}"
                    )
                groups.append(
                    {
                        "ct_id": group.ct_id,
                        "ct_label": ct_label,
                        "panel_name": panel_name,
                        "total_w": group.total_w,
                        "r2": group.r2,
                        "r2_null_p95": group.r2_null_p95,
                        "r2_margin": group.r2_margin,
                        # Truthful, not a constant: ``GroupFit`` derives this from
                        # whether any member carries a coefficient the fit
                        # identified.  Measured on the production install at 72 h
                        # of history, 16 of 20 groups have a single member and 14
                        # of those 20 carry an identified coefficient, so they
                        # *do* have a measured wattage; reporting them all as "not
                        # individually identifiable" would understate evidence
                        # the engine actually holds, and would make the UI warn
                        # about numbers that are sound.
                        "individually_identifiable": group.individually_identifiable,
                        "members": members,
                        "evidence": list(group.evidence),
                    }
                )
        elif include_groups:
            notes.append(
                "group fitting is not installed; fixtures that co-toggle cannot be "
                "reported even as advisory groups"
            )

        accepted: list[dict[str, Any]] = []
        for placement in plan.accepted:
            configuration = placement.configuration
            ct_id = configuration.monitored_by_ct_ids[0]
            entity_id = configuration.state_entity_ids[0]
            ct_label, panel_name = _channel_label(ct_id)
            candidate = next(
                (c for c in report.candidates if c.entity_id == entity_id), None
            )
            accepted.append(
                {
                    "device_id": placement.device_id,
                    "entity_id": entity_id,
                    "name": labels.get(entity_id, entity_id),
                    "ct_id": ct_id,
                    "ct_label": ct_label,
                    "panel_name": panel_name,
                    "on_w": (
                        getattr(candidate, "typical_on_w", None)
                        if candidate is not None
                        else None
                    )
                    or configuration.expected.on_w,
                    "off_w": getattr(candidate, "typical_off_w", None)
                    if candidate is not None
                    else None,
                    "symmetry": getattr(candidate, "symmetry", None),
                    "agreement": getattr(candidate, "agreement", None),
                    "corroborations": getattr(candidate, "corroborations", None),
                    "specificity_z": getattr(candidate, "specificity_z", None),
                    "dimmable": bool(
                        configuration.light is not None
                        and configuration.light.dimming_curve
                    ),
                    "evidence": list(placement.evidence),
                    "applied": False,
                }
            )

        stats["accepted"] = len(accepted)
        stats["groups"] = len(groups)
        stats["withheld"] = len(withheld)

        # Candidate-level refusals ("this entity can never prove runtime") and
        # placement-level refusals ("this evidence may not become a device") are
        # both reported.
        refused: dict[str, Any] = dict(plan.refused)
        for entity_id, reason in _named_refusals().items():
            refused.setdefault(entity_id, reason)
        accepted_ids = {item["entity_id"] for item in accepted}
        withheld = {
            key: value
            for key, value in withheld.items()
            if key not in refused and key not in accepted_ids
        }
        stats["withheld"] = len(withheld)

        notes.extend(
            f"{entity_id}: {reason}" for entity_id, reason in report.unresolved.items()
        )
        if stats["refused_entities"]:
            notes.append(
                f"{stats['refused_entities']} state entities are not candidates "
                "(a mode entity, or a domain whose state does not prove runtime)"
            )

        result: dict[str, Any] = {
            "ok": True,
            "applied": False,
            "lookback_hours": lookback_hours,
            "topology_version": topology.version,
            "stats": stats,
            "accepted": accepted,
            "refused": refused,
            "groups": groups,
            "withheld": withheld,
            "skipped": dict(report.skipped),
            # Additive to the frozen shape: kept from the pre-§3.3 payload so a
            # caller that already explained "why was this not placed?" keeps
            # working.  ``notes`` repeats them in human-readable form.
            "unresolved": dict(report.unresolved),
            "row_noise_w": row_noise,
            "notes": notes,
        }
        if not plan.accepted:
            return result
        if not allow_apply:
            if not self.config.auto_placement:
                result["detail"] = (
                    "auto placement is disabled: turn on the 'auto_placement' "
                    "App option (Add-on → Configuration) and send apply=true "
                    "to write these placements"
                )
            else:
                result["detail"] = (
                    "apply is false; pass apply=true to write these placements"
                )
            return result

        merged = merge_placements(configurations, plan)
        # The friendly name the placement reported is a display name, so it is
        # written to the topology row rather than to the persisted configuration.
        projected = project_configuration(
            topology,
            merged,
            display_names={
                placement.device_id: placement.name
                for placement in plan.accepted
                if placement.name
            },
        )
        self.db.save_device_configurations(merged)
        self.db.save_topology(projected, note="auto placement from observed co-movement")
        self.topology_cache = projected
        self.db.log_system_event(
            "info",
            "configuration",
            f"auto placement created {len(plan.accepted)} device(s)",
        )
        # ``applied`` is per accepted item and only true once the write landed.
        for item in result["accepted"]:
            item["applied"] = True
        result["applied"] = True
        result["topology_version"] = projected.version
        return result

    def export(self, device_id: str) -> dict[str, Any]:
        try:
            data = self.db.export_device_data(device_id)
        except Exception as exc:  # noqa: BLE001
            raise InvalidRequest(f"export failed: {exc}") from exc
        if not data.get("inference_results") and not data.get("learning_samples") and not data.get("model_versions"):
            topology = self._active_topology()
            known = topology.device_map() if topology else {}
            if device_id not in known:
                raise NotFound(f"device {device_id!r} not found")
        return data

    # -- facade: metrics ---------------------------------------------------- #
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

    # -- diagnostics helpers ------------------------------------------------ #
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


def _safe_history_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "timestamp": row.get("timestamp"),
        "power_w": row.get("power_w"),
        "source": row.get("source"),
        "confidence": row.get("confidence"),
        "quality_status": row.get("quality_status"),
        "operating_stage": row.get("operating_stage"),
    }


def _is_numeric_state(row: Mapping[str, Any]) -> bool:
    """True when a recorder row's state is a number, i.e. an actual measurement.

    A CT row legitimately reports ``unavailable`` while its bridge restarts
    (observed on the reference install inside a 7-day window).  That row carries
    no watts, and the group fit's row parser raises on it rather than skipping
    it.  ``parse_history_payload`` already drops such rows when building
    ``readings``, so this keeps the fit's input consistent with the evidence the
    correlation used instead of inventing a second interpretation.
    """
    raw = row.get("s", row.get("state"))
    if raw is None or isinstance(raw, bool):
        return False
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return False
    return math.isfinite(value)


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
        supervisor.start("inference", core._inference_loop)
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
        # Exercise the real inference backend once so the summary proves the
        # adapter is wired (results, reconciliation, or a clear reason).
        try:
            inference = await core.run_inference_once()
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
