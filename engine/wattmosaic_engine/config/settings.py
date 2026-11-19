"""Configuration loading with validation.

Secrets are read from the environment (or a .env file) and are never logged.
``redacted()`` produces a log-safe view.
"""

from __future__ import annotations

import json
import logging
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

DEFAULT_HA_PORT = 8123

log = logging.getLogger("wattmosaic.config")

#: DEC-03: the old timer-driven full-recompute interval is accepted (for one
#: release, so add-on config validation keeps working) but ignored.  The three
#: loops in :mod:`wattmosaic_engine.core.runtime` decide when work happens.
DEPRECATED_INFERENCE_INTERVAL_NOTE = (
    "WM_INFERENCE_INTERVAL_S is deprecated and ignored: the three-loop runtime "
    "runs inference per event group, not on a fixed timer"
)

#: The only timing keys an isolation override may set.  Declared here (and
#: mirrored by ``learning.thresholds.ISOLATION_TIMING_KEYS``) so a typo in one
#: key cannot silently change an unrelated timing.  ``config`` is the lowest
#: layer of the package and imports nothing from it, which is why the canonical
#: tuple lives here.
ISOLATION_TIMING_KEYS: tuple[str, ...] = (
    "analysis_offset_s",
    "isolation_window_s",
    "power_window_s",
)

#: Accepted range for the L1 raw-sample horizon, in days.  ``REQ-STORAGE-002``
#: states a 7-14 d band; ``OD-31`` (13-open-decisions-and-assumptions.md §3)
#: requires the validator to be a **superset** of ``{7, 14, 30}``.  This is that
#: superset: the documented band is accepted, so is the old 30 d default, and
#: anything outside 1-90 is refused at startup.  The add-on schema's
#: ``retention_days_raw`` bound is pinned to this range by a parity test.  The
#: shipped default is now 7 d (the V2 DB-growth finding: raw samples are ~88 %
#: of growth, ~21-28 GB at 30 d on a Pi-class host).
RETENTION_DAYS_RAW_RANGE: tuple[int, int] = (1, 90)

#: Accepted range for every derived-table retention horizon, in days.  The
#: add-on does not expose these (they are engine/env settings), but the same
#: 1-3650 band is used so an operator cannot disable pruning with a nonsense
#: value or request an unbounded horizon the prune layer cannot honour.
RETENTION_DAYS_DERIVED_RANGE: tuple[int, int] = (1, 3650)

#: Accepted range for the L3 aggregate horizon, in days.
RETENTION_DAYS_AGGREGATES_RANGE: tuple[int, int] = (30, 36500)

#: Accepted range for an explicit L2/L4 horizon.  ``None`` disables it; a set
#: value must be at least as long as the raw horizon, because evidence that does
#: not outlive the raw window it explains cannot be audited.
RETENTION_DAYS_HORIZON_MIN = 1

#: Default stamped onto every audit artifact.  The analysis pass owns the
#: constant (``disaggregation.audit.DEFAULT_ALGORITHM_VERSION``); importing that
#: module from configuration would invert the dependency, so the value is
#: repeated here and a test pins the two together.
DEFAULT_AUDIT_ALGORITHM_VERSION = "disagg@2.0"


def _with_default_port(url: str) -> str:
    """Append ``:8123`` only to a bare host that has no port and no path.

    Home Assistant is normally reached at ``http://host:8123``, so a bare host
    such as ``homeassistant`` or ``192.0.2.10`` is completed with the default
    port.  A URL that already carries a path is left untouched: the Supervisor
    Core proxy is ``http://supervisor/core``, and appending a port produced
    ``http://supervisor/core:8123`` (host ``supervisor``, path
    ``/core:8123``), which the Supervisor rejects with HTTP 401.  The same
    applies to a reverse-proxy prefix such as ``https://ha.example/ha``.
    """
    parts = urlsplit(url)
    try:
        has_port = parts.port is not None
    except ValueError:  # malformed port: leave the URL for validate() to reject
        return url
    if has_port or parts.path not in ("", "/"):
        return url
    return urlunsplit(
        (parts.scheme, f"{parts.netloc}:{DEFAULT_HA_PORT}", parts.path, parts.query, parts.fragment)
    )


def load_dotenv(path: str | Path = ".env") -> dict[str, str]:
    """Parse a simple KEY=VALUE .env file. Does not mutate os.environ."""
    out: dict[str, str] = {}
    p = Path(path)
    if not p.exists():
        return out
    for raw in p.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[7:].strip()
        out[key] = value.strip().strip("'\"")
    return out


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _timing_layers(raw: str | None) -> dict[str, dict[str, Any]]:
    """Parse a ``{label: {timing: seconds}}`` JSON mapping from the environment.

    Malformed JSON degrades to the documented empty default instead of raising:
    a startup that cannot parse an optional tuning hint must still boot, and the
    conservative global window is a correct answer.  Values are kept *as written*
    (minus a structurally impossible entry) so ``validate()`` can report a typo
    or a negative window instead of the value disappearing silently -- an
    override that is present but wrong is a configuration problem, not a default.
    ``IsolationWindow.merged`` still drops any value it cannot use at runtime, so
    a bad entry cannot produce a window that never matches.
    """
    text = (raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return {}
    if not isinstance(parsed, Mapping):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for label, overrides in parsed.items():
        if not isinstance(overrides, Mapping):
            continue
        out[str(label)] = {str(key): value for key, value in overrides.items()}
    return out


def _class_layers(raw: str | None) -> dict[str, tuple[str, ...]]:
    """Parse a ``{device_id: [class label, ...]}`` JSON mapping."""
    text = (raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return {}
    if not isinstance(parsed, Mapping):
        return {}
    out: dict[str, tuple[str, ...]] = {}
    for device_id, labels in parsed.items():
        if isinstance(labels, str):
            labels = [labels]
        if not isinstance(labels, (list, tuple)):
            continue
        cleaned = tuple(str(label).strip() for label in labels if str(label).strip())
        if cleaned:
            out[str(device_id)] = cleaned
    return out


@dataclass(slots=True)
class EngineConfig:
    # Home Assistant
    ha_url: str = "http://homeassistant.local:8123"
    ha_token: str = field(default="", repr=False)
    ha_verify_ssl: bool = True

    # Engine HTTP API
    api_host: str = "0.0.0.0"
    api_port: int = 8787
    api_token: str = field(default="", repr=False)

    # Storage
    database_path: str = "data/wattmosaic.db"
    #: L1 raw-sample horizon, in days.  Reduced from 30 to **7** by the V2
    #: DB-growth finding: raw samples are ~88 % of growth (≈ 710 MB/day at
    #: 30 samples/s), so 30 d meant a 21-28 GB steady state on Pi-class hosts.
    #: ``REQ-STORAGE-002`` asks for a 7-14 d band and ``OD-31`` requires the
    #: validator to be a superset of ``{7, 14, 30}``, so the accepted range is
    #: 1-90 rather than the literal 7-14; see :data:`RETENTION_DAYS_RAW_RANGE`.
    retention_days_raw: int = 7
    #: L3 aggregate horizon, in days.
    retention_days_aggregates: int = 730
    #: L2 event-evidence horizon.  ``None`` means disabled: evidence is
    #: long-term and only an explicit operator action bounds it (spec §3.1).
    retention_days_evidence: int | None = None
    #: L4 version-history horizon.  ``None`` means disabled, for the same reason.
    retention_days_versions: int | None = None

    # Behaviour
    learning_enabled: bool = True
    #: How often a CT reader publishes a *new state*, in seconds.  This is the
    #: last-resort fallback used when the aligner has no usable observed
    #: history; it is **not** a measurement and must never be presented as one
    #: (spec 03 §6.2).  ``WM_DEFAULT_SAMPLE_INTERVAL_S`` overrides it.  The
    #: audited median for the reference install is 15.7 s, which the aligner
    #: derives per channel from its own history rather than from this key.
    default_sample_interval_s: float = 10.0
    stale_after_s: float = 120.0
    #: Period of the v3 data-quality heartbeat loop (P6, DEC-14).  The heartbeat
    #: publishes stale-source aging only; it never runs a full solve.  This is a
    #: genuinely new setting for the three-loop runtime, distinct from the
    #: retired ``inference_interval_s`` (DEC-03), which accepted and ignored the
    #: old 5 s solve cadence.
    quality_interval_s: float = 5.0
    queue_max_size: int = 5000

    # -- three-loop runtime (P6, HB §5) ----------------------------------- #
    #: Maximum number of ready/revised event groups held for the event loop.
    #: Overflow drops the oldest group and counts it in ``groups_dropped``.
    event_queue_max: int = 1000
    #: Seconds between staleness-only heartbeats (DEC-14).  A heartbeat
    #: publishes ``stale_sources``/aging; it never runs a full solve.
    steady_heartbeat_s: float = 60.0
    #: Multiplier on the unknown-load penalty (DEC-23).  1.0 is the neutral
    #: operating point; it is never a measurement and never 0 by default for a
    #: missing value.
    unknown_penalty_scale: float = 1.0
    #: Evidence retention horizon in days (DEC-30).  Evidence is long-term and
    #: only an explicit operator action bounds it.
    evidence_retention_days: int = 400
    #: Raw evidence retention horizon in days; raw samples expire sooner than
    #: the derived evidence that cites them.
    evidence_raw_retention_days: int = 30

    # -- V2 derived-table age retention (Z11) ------------------------------ #
    # These tables were previously never age-pruned (~25 GB/year).  All are
    # bounded by an age key and, crucially, by provenance: a row still cited by
    # the live snapshot, a non-finalized group/evidence row, or an active
    # version is never deleted.  See ``apply_derived_retention`` in
    # ``wattmosaic_engine.storage.tiers`` and docs/configuration.md.
    #: `inference_snapshots` horizon in days (≈ 51-52 MB/day).
    retention_days_snapshots: int = 90
    #: `event_inference_records` horizon in days.
    retention_days_records: int = 90
    #: `ha_events_v3` horizon in days.
    retention_days_ha_events: int = 90
    #: `event_groups` / `event_group_members` horizon in days.
    retention_days_groups: int = 90
    #: `reader_residual_series` horizon in days (cheap change-point series).
    retention_days_residual_series: int = 30

    #: Discover a candidate topology from Home Assistant at startup when the
    #: database has none.  Disabling it leaves a fresh install with nothing to
    #: infer, which ``/health`` now reports instead of failing silently.
    topology_bootstrap: bool = True
    #: Create devices for fixtures whose circuit membership was learned from
    #: observed co-movement (ADR-0024).  Off by default: it writes device
    #: configurations, and a write must be something the user asked for.
    auto_placement: bool = False
    log_level: str = "INFO"
    log_json: bool = True

    # -- versioned audit + analysis pass (contract 1.6.0) ------------------ #
    #: How far back ``GET /api/v1/devices/{id}`` reads inference history.
    #: A history query needs a lower bound: ``since=now`` (the pre-1.6.0
    #: behaviour) matches no stored row at all, so the endpoint always reported
    #: an empty list.  One day is the shortest lookback that still shows a
    #: yesterday-evening load cycle.
    history_lookback_s: float = 86400.0
    #: Master switch for the analysis pass that derives isolation verdicts,
    #: device→CT associations, circuits, residuals, hierarchy edges and the
    #: versioned ``InferenceRecord`` audit trail.  When off, none of those
    #: artifacts are written and the existing inference output is unchanged.
    audit_enabled: bool = True
    #: Per-artifact switches.  Each one only *gates writing that family*; the
    #: algorithms still run so a disabled family cannot silently change a value
    #: another family derives from it.
    audit_isolation_enabled: bool = True
    audit_association_enabled: bool = True
    audit_circuits_enabled: bool = True
    audit_residual_enabled: bool = True
    audit_hierarchy_enabled: bool = True
    audit_records_enabled: bool = True
    #: Seconds between the event and the start of its analysis window.  A CT
    #: that publishes every ~15.7 s has not necessarily reported the *effect* of
    #: an event yet when the event arrives, so analysis can be shifted later.
    analysis_offset_s: float = 0.0
    #: Width of the "was anything else changing?" window, in seconds.
    isolation_window_s: float = 30.0
    #: Width of the window whose readings are attributed to the event.  Must be
    #: at least ``isolation_window_s``.
    isolation_power_window_s: float = 60.0
    #: ``device_class label -> timing overrides`` (the middle isolation layer).
    #: Keys are ``analysis_offset_s`` / ``isolation_window_s`` /
    #: ``power_window_s``.  A hot tub's compressor is not a lamp: one fixed
    #: duration is not optimal for every device, so the layer exists and is
    #: resolved per device by ``IsolationConfig.resolve``.  Empty (the default)
    #: means every device keeps the global timings above.
    isolation_by_class: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: ``device_id -> timing overrides`` (the strongest isolation layer).
    isolation_by_device: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: ``device_id -> class labels`` used to look up ``isolation_by_class``.
    #: Stored ``DeviceConfiguration.device_type`` contributes a label too, so an
    #: install that never sets this still gets the class layer for a typed
    #: device.
    isolation_device_classes: dict[str, tuple[str, ...]] = field(
        default_factory=dict
    )
    #: Version stamped onto every audit artifact.  Part of the audit contract:
    #: a replay is only comparable to a record written under the same value, so
    #: it is a real setting rather than a constant nothing can override.
    audit_algorithm_version: str = DEFAULT_AUDIT_ALGORITHM_VERSION
    #: Realtime learning: reanalysis is event-driven — it fires as soon as new
    #: matured stored evidence exists, on the existing inference loop (no thread
    #: or task).  ``0`` (the default) means immediate.  A positive value is now
    #: only an optional minimum-gap *debounce* between passes, never a periodic
    #: 3600 s trigger.  Retained as a setting for backward compatibility.
    reanalysis_interval_s: float = 0.0
    #: How much stored raw history a reanalysis pass reads.
    reanalysis_lookback_s: float = 21600.0
    #: Realtime learning: training is event-driven on matured samples.  ``0``
    #: (the default) means immediate; a positive value is retained only as an
    #: optional minimum-gap debounce, never a periodic 3600 s throttle.
    train_interval_s: float = 0.0
    # Learning thresholds (documented; not magic constants)
    min_samples_for_model: int = 12
    min_validation_r2: float = 0.70
    promotion_margin: float = 0.05
    max_sample_rejection_ratio: float = 0.60

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None, dotenv_path: str | Path = ".env") -> EngineConfig:
        merged: dict[str, str] = {}
        merged.update(load_dotenv(dotenv_path))
        merged.update(os.environ)
        if env:
            merged.update(env)

        def get(*names: str, default: str | None = None) -> str | None:
            for n in names:
                v = merged.get(n)
                if v is not None and v != "":
                    return v
            return default

        cfg = cls()
        host = get("WM_HA_URL", "HA_URL", "HA_HOST", default=cfg.ha_url) or cfg.ha_url
        if not host.startswith(("http://", "https://")):
            host = f"http://{host}"
        cfg.ha_url = _with_default_port(host).rstrip("/")
        cfg.ha_token = get("WM_HA_TOKEN", "HA_TOKEN", "HA_LONG_LIVED_TOKEN", default="") or ""
        cfg.ha_verify_ssl = _bool(get("WM_HA_VERIFY_SSL"), True)
        cfg.api_host = get("WM_API_HOST", default=cfg.api_host) or cfg.api_host
        cfg.api_port = int(get("WM_API_PORT", default=str(cfg.api_port)) or cfg.api_port)
        cfg.api_token = get("WM_API_TOKEN", default="") or ""
        cfg.database_path = get("WM_DATABASE_PATH", default=cfg.database_path) or cfg.database_path
        cfg.retention_days_raw = int(
            get("WM_RETENTION_DAYS_RAW", default=str(cfg.retention_days_raw))
            or cfg.retention_days_raw
        )
        cfg.retention_days_aggregates = int(
            get("WM_RETENTION_DAYS_AGGREGATES", default=str(cfg.retention_days_aggregates)) or 730
        )
        # An empty value means "disabled" and is read as None, so an operator
        # can turn the horizon off from the App without a sentinel number.
        evidence_raw = get(
            "WM_RETENTION_DAYS_EVIDENCE", default=str(cfg.retention_days_evidence or "")
        )
        cfg.retention_days_evidence = int(evidence_raw) if evidence_raw else None
        versions_raw = get(
            "WM_RETENTION_DAYS_VERSIONS", default=str(cfg.retention_days_versions or "")
        )
        cfg.retention_days_versions = int(versions_raw) if versions_raw else None
        cfg.learning_enabled = _bool(get("WM_LEARNING_ENABLED"), True)
        cfg.default_sample_interval_s = float(
            get("WM_DEFAULT_SAMPLE_INTERVAL_S", default=str(cfg.default_sample_interval_s))
            or cfg.default_sample_interval_s
        )
        cfg.stale_after_s = float(get("WM_STALE_AFTER_S", default=str(cfg.stale_after_s)) or 120)
        raw_interval = get("WM_INFERENCE_INTERVAL_S", default=str(cfg.quality_interval_s))
        cfg.quality_interval_s = float(raw_interval or 5)
        cfg.quality_interval_s = max(1.0, cfg.quality_interval_s)
        if get("WM_INFERENCE_INTERVAL_S") is not None:
            # DEC-03: accepted, ignored, logged once at startup/load.  The value
            # is still parsed so a typo cannot hide, but it never drives a loop.
            log.warning(DEPRECATED_INFERENCE_INTERVAL_NOTE)
        cfg.queue_max_size = int(get("WM_QUEUE_MAX_SIZE", default=str(cfg.queue_max_size)) or 5000)
        cfg.event_queue_max = int(
            get("WM_EVENT_QUEUE_MAX", default=str(cfg.event_queue_max)) or cfg.event_queue_max
        )
        cfg.steady_heartbeat_s = float(
            get("WM_STEADY_HEARTBEAT_S", default=str(cfg.steady_heartbeat_s))
            or cfg.steady_heartbeat_s
        )
        cfg.unknown_penalty_scale = float(
            get("WM_UNKNOWN_PENALTY_SCALE", default=str(cfg.unknown_penalty_scale))
            or cfg.unknown_penalty_scale
        )
        cfg.evidence_retention_days = int(
            get("WM_EVIDENCE_RETENTION_DAYS", default=str(cfg.evidence_retention_days))
            or cfg.evidence_retention_days
        )
        cfg.evidence_raw_retention_days = int(
            get("WM_EVIDENCE_RAW_RETENTION_DAYS", default=str(cfg.evidence_raw_retention_days))
            or cfg.evidence_raw_retention_days
        )
        # V2 derived-table horizons (Z11).  Each defaults to the shipped value
        # and is validated against RETENTION_DAYS_DERIVED_RANGE by validate().
        cfg.retention_days_snapshots = int(
            get("WM_RETENTION_DAYS_SNAPSHOTS", default=str(cfg.retention_days_snapshots))
            or cfg.retention_days_snapshots
        )
        cfg.retention_days_records = int(
            get("WM_RETENTION_DAYS_RECORDS", default=str(cfg.retention_days_records))
            or cfg.retention_days_records
        )
        cfg.retention_days_ha_events = int(
            get("WM_RETENTION_DAYS_HA_EVENTS", default=str(cfg.retention_days_ha_events))
            or cfg.retention_days_ha_events
        )
        cfg.retention_days_groups = int(
            get("WM_RETENTION_DAYS_GROUPS", default=str(cfg.retention_days_groups))
            or cfg.retention_days_groups
        )
        cfg.retention_days_residual_series = int(
            get(
                "WM_RETENTION_DAYS_RESIDUAL_SERIES",
                default=str(cfg.retention_days_residual_series),
            )
            or cfg.retention_days_residual_series
        )
        cfg.topology_bootstrap = _bool(
            get("WM_TOPOLOGY_BOOTSTRAP", default="true"), True
        )
        cfg.auto_placement = _bool(get("WM_AUTO_PLACEMENT", default="false"), False)
        cfg.history_lookback_s = float(
            get("WM_HISTORY_LOOKBACK_S", default=str(cfg.history_lookback_s))
            or cfg.history_lookback_s
        )
        cfg.audit_enabled = _bool(get("WM_AUDIT_ENABLED", default="true"), True)
        cfg.audit_isolation_enabled = _bool(
            get("WM_AUDIT_ISOLATION_ENABLED", default="true"), True
        )
        cfg.audit_association_enabled = _bool(
            get("WM_AUDIT_ASSOCIATION_ENABLED", default="true"), True
        )
        cfg.audit_circuits_enabled = _bool(
            get("WM_AUDIT_CIRCUITS_ENABLED", default="true"), True
        )
        cfg.audit_residual_enabled = _bool(
            get("WM_AUDIT_RESIDUAL_ENABLED", default="true"), True
        )
        cfg.audit_hierarchy_enabled = _bool(
            get("WM_AUDIT_HIERARCHY_ENABLED", default="true"), True
        )
        cfg.audit_records_enabled = _bool(
            get("WM_AUDIT_RECORDS_ENABLED", default="true"), True
        )
        cfg.analysis_offset_s = float(
            get("WM_ANALYSIS_OFFSET_S", default=str(cfg.analysis_offset_s))
            or cfg.analysis_offset_s
        )
        cfg.isolation_window_s = float(
            get("WM_ISOLATION_WINDOW_S", default=str(cfg.isolation_window_s))
            or cfg.isolation_window_s
        )
        cfg.isolation_power_window_s = float(
            get("WM_ISOLATION_POWER_WINDOW_S", default=str(cfg.isolation_power_window_s))
            or cfg.isolation_power_window_s
        )
        cfg.isolation_by_class = _timing_layers(get("WM_ISOLATION_BY_CLASS"))
        cfg.isolation_by_device = _timing_layers(get("WM_ISOLATION_BY_DEVICE"))
        cfg.isolation_device_classes = _class_layers(
            get("WM_ISOLATION_DEVICE_CLASSES")
        )
        cfg.audit_algorithm_version = (
            get(
                "WM_AUDIT_ALGORITHM_VERSION",
                default=cfg.audit_algorithm_version,
            )
            or cfg.audit_algorithm_version
        )
        cfg.reanalysis_interval_s = float(
            get("WM_REANALYSIS_INTERVAL_S", default=str(cfg.reanalysis_interval_s))
            or cfg.reanalysis_interval_s
        )
        cfg.reanalysis_lookback_s = float(
            get("WM_REANALYSIS_LOOKBACK_S", default=str(cfg.reanalysis_lookback_s))
            or cfg.reanalysis_lookback_s
        )
        cfg.train_interval_s = float(
            get("WM_TRAIN_INTERVAL_S", default=str(cfg.train_interval_s))
            or cfg.train_interval_s
        )
        cfg.log_level = (get("WM_LOG_LEVEL", "LOG_LEVEL", default="INFO") or "INFO").upper()
        cfg.log_json = _bool(get("WM_LOG_JSON"), True)
        return cfg

    def validate(self) -> list[str]:
        problems: list[str] = []
        if not self.ha_url.startswith(("http://", "https://")):
            problems.append("ha_url must be an http(s) URL")
        if not self.ha_token:
            problems.append("HA token is not configured (HA_TOKEN)")
        if self.api_port <= 0 or self.api_port > 65535:
            problems.append("api_port out of range")
        if self.quality_interval_s <= 0:
            problems.append("quality_interval_s must be positive")
        if self.default_sample_interval_s <= 0:
            problems.append("default_sample_interval_s must be positive")
        if self.queue_max_size < 100:
            problems.append("queue_max_size too small")
        if self.event_queue_max < 1:
            problems.append("event_queue_max must be positive")
        if self.steady_heartbeat_s <= 0:
            problems.append("steady_heartbeat_s must be positive")
        if not math.isfinite(self.unknown_penalty_scale) or self.unknown_penalty_scale < 0:
            problems.append("unknown_penalty_scale must be finite and non-negative")
        if self.evidence_retention_days < 1:
            problems.append("evidence_retention_days must be positive")
        if self.evidence_raw_retention_days < 1:
            problems.append("evidence_raw_retention_days must be positive")
        raw_low, raw_high = RETENTION_DAYS_RAW_RANGE
        if not raw_low <= self.retention_days_raw <= raw_high:
            problems.append(
                f"retention_days_raw must be in {raw_low}-{raw_high} "
                f"(got {self.retention_days_raw})"
            )
        agg_low, agg_high = RETENTION_DAYS_AGGREGATES_RANGE
        if not agg_low <= self.retention_days_aggregates <= agg_high:
            problems.append(
                f"retention_days_aggregates must be in {agg_low}-{agg_high} "
                f"(got {self.retention_days_aggregates})"
            )
        derived_low, derived_high = RETENTION_DAYS_DERIVED_RANGE
        for name, horizon in (
            ("retention_days_snapshots", self.retention_days_snapshots),
            ("retention_days_records", self.retention_days_records),
            ("retention_days_ha_events", self.retention_days_ha_events),
            ("retention_days_groups", self.retention_days_groups),
            ("retention_days_residual_series", self.retention_days_residual_series),
        ):
            if not derived_low <= horizon <= derived_high:
                problems.append(
                    f"{name} must be in {derived_low}-{derived_high} (got {horizon})"
                )
        for name, horizon in (
            ("retention_days_evidence", self.retention_days_evidence),
            ("retention_days_versions", self.retention_days_versions),
        ):
            if horizon is None:
                continue
            if horizon < RETENTION_DAYS_HORIZON_MIN:
                problems.append(
                    f"{name} must be at least {RETENTION_DAYS_HORIZON_MIN} "
                    f"or unset (got {horizon})"
                )
            elif horizon < self.retention_days_raw:
                problems.append(
                    f"{name} ({horizon}) must not be shorter than "
                    f"retention_days_raw ({self.retention_days_raw}): evidence "
                    "that does not outlive the raw window it explains cannot "
                    "be audited"
                )
        if self.history_lookback_s <= 0:
            problems.append("history_lookback_s must be positive")
        if self.analysis_offset_s < 0:
            problems.append("analysis_offset_s must not be negative")
        if self.isolation_window_s <= 0:
            problems.append("isolation_window_s must be positive")
        if self.isolation_power_window_s < self.isolation_window_s:
            problems.append(
                "isolation_power_window_s must be at least isolation_window_s"
            )
        if self.reanalysis_interval_s < 0:
            problems.append("reanalysis_interval_s must not be negative")
        if self.reanalysis_lookback_s <= 0:
            problems.append("reanalysis_lookback_s must be positive")
        if self.train_interval_s < 0:
            problems.append("train_interval_s must not be negative")
        problems.extend(self._isolation_layer_problems())
        if not str(self.audit_algorithm_version).strip():
            problems.append("audit_algorithm_version must not be empty")
        return problems

    def _isolation_layer_problems(self) -> list[str]:
        """Validate every class/device timing override, and reject typos.

        A negative or non-finite window is a configuration error, not a value to
        clamp: silently repairing it would make the effective window differ from
        the one the operator wrote.  An unknown key is reported because
        ``IsolationWindow.merged`` ignores it, so it would otherwise be a setting
        that appears to do something and does not.
        """
        problems: list[str] = []
        for name, layers in (
            ("isolation_by_class", self.isolation_by_class),
            ("isolation_by_device", self.isolation_by_device),
        ):
            if not isinstance(layers, Mapping):
                problems.append(f"{name} must be a mapping of name -> timings")
                continue
            for key, overrides in layers.items():
                if not isinstance(overrides, Mapping):
                    problems.append(f"{name}[{key!r}] must be a mapping of timings")
                    continue
                unknown = sorted(set(overrides) - set(ISOLATION_TIMING_KEYS))
                if unknown:
                    problems.append(
                        f"{name}[{key!r}] has unknown timing key(s): "
                        f"{', '.join(unknown)}"
                    )
                for timing in ISOLATION_TIMING_KEYS:
                    if timing not in overrides:
                        continue
                    try:
                        number = float(overrides[timing])
                    except (TypeError, ValueError):
                        problems.append(
                            f"{name}[{key!r}].{timing} must be a number"
                        )
                        continue
                    if not math.isfinite(number):
                        problems.append(
                            f"{name}[{key!r}].{timing} must be finite"
                        )
                    elif number < 0.0:
                        problems.append(
                            f"{name}[{key!r}].{timing} must not be negative"
                        )
        if not isinstance(self.isolation_device_classes, Mapping):
            problems.append("isolation_device_classes must be a mapping")
        else:
            for device_id, labels in self.isolation_device_classes.items():
                if isinstance(labels, str) or not isinstance(labels, (list, tuple)):
                    problems.append(
                        f"isolation_device_classes[{device_id!r}] must be a list "
                        "of class labels"
                    )
        return problems

    def redacted(self) -> dict[str, Any]:
        return {
            "ha_url": self.ha_url,
            "ha_token": ("***" if self.ha_token else ""),
            "api_host": self.api_host,
            "api_port": self.api_port,
            "api_token": ("***" if self.api_token else ""),
            "database_path": self.database_path,
            "retention_days_raw": self.retention_days_raw,
            "retention_days_aggregates": self.retention_days_aggregates,
            "retention_days_evidence": self.retention_days_evidence,
            "retention_days_versions": self.retention_days_versions,
            "learning_enabled": self.learning_enabled,
            "auto_placement": self.auto_placement,
            "stale_after_s": self.stale_after_s,
            "default_sample_interval_s": self.default_sample_interval_s,
            "inference_interval_s": self.quality_interval_s,
            "event_queue_max": self.event_queue_max,
            "steady_heartbeat_s": self.steady_heartbeat_s,
            "unknown_penalty_scale": self.unknown_penalty_scale,
            "evidence_retention_days": self.evidence_retention_days,
            "evidence_raw_retention_days": self.evidence_raw_retention_days,
            "retention_days_snapshots": self.retention_days_snapshots,
            "retention_days_records": self.retention_days_records,
            "retention_days_ha_events": self.retention_days_ha_events,
            "retention_days_groups": self.retention_days_groups,
            "retention_days_residual_series": self.retention_days_residual_series,
            "log_level": self.log_level,
            "history_lookback_s": self.history_lookback_s,
            "audit_enabled": self.audit_enabled,
            "audit_isolation_enabled": self.audit_isolation_enabled,
            "audit_association_enabled": self.audit_association_enabled,
            "audit_circuits_enabled": self.audit_circuits_enabled,
            "audit_residual_enabled": self.audit_residual_enabled,
            "audit_hierarchy_enabled": self.audit_hierarchy_enabled,
            "audit_records_enabled": self.audit_records_enabled,
            "analysis_offset_s": self.analysis_offset_s,
            "isolation_window_s": self.isolation_window_s,
            "isolation_power_window_s": self.isolation_power_window_s,
            "isolation_by_class": self.isolation_by_class,
            "isolation_by_device": self.isolation_by_device,
            "isolation_device_classes": {
                str(device): list(labels)
                for device, labels in self.isolation_device_classes.items()
            },
            "audit_algorithm_version": self.audit_algorithm_version,
            "reanalysis_interval_s": self.reanalysis_interval_s,
            "reanalysis_lookback_s": self.reanalysis_lookback_s,
            "train_interval_s": self.train_interval_s,
        }
