"""Configuration loading with validation.

Secrets are read from the environment (or a .env file) and are never logged.
``redacted()`` produces a log-safe view.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

DEFAULT_HA_PORT = 8123

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
    retention_days_raw: int = 30
    retention_days_aggregates: int = 730

    # Behaviour
    learning_enabled: bool = True
    default_sample_interval_s: float = 10.0
    stale_after_s: float = 120.0
    inference_interval_s: float = 5.0
    queue_max_size: int = 5000
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
    #: Wall-clock interval between periodic reanalysis passes.  Reanalysis runs
    #: on the existing inference loop; it never starts a thread or a task.
    reanalysis_interval_s: float = 3600.0
    #: How much stored raw history a reanalysis pass reads.
    reanalysis_lookback_s: float = 21600.0
    #: Emit the deprecation notices for a manual ``dedicated_device``
    #: declaration (ADR-0029).  Diagnostics only: this silences the notice, it
    #: never changes watts, source, tier or confidence, and the direct
    #: measurement path keeps working either way.
    deprecate_dedicated_declaration: bool = True

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
        cfg.retention_days_raw = int(get("WM_RETENTION_DAYS_RAW", default=str(cfg.retention_days_raw)) or 30)
        cfg.retention_days_aggregates = int(
            get("WM_RETENTION_DAYS_AGGREGATES", default=str(cfg.retention_days_aggregates)) or 730
        )
        cfg.learning_enabled = _bool(get("WM_LEARNING_ENABLED"), True)
        cfg.stale_after_s = float(get("WM_STALE_AFTER_S", default=str(cfg.stale_after_s)) or 120)
        cfg.inference_interval_s = float(
            get("WM_INFERENCE_INTERVAL_S", default=str(cfg.inference_interval_s)) or 5
        )
        cfg.inference_interval_s = max(1.0, cfg.inference_interval_s)
        cfg.queue_max_size = int(get("WM_QUEUE_MAX_SIZE", default=str(cfg.queue_max_size)) or 5000)
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
        cfg.deprecate_dedicated_declaration = _bool(
            get("WM_DEPRECATE_DEDICATED_DECLARATION", default="true"), True
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
        if self.inference_interval_s <= 0:
            problems.append("inference_interval_s must be positive")
        if self.queue_max_size < 100:
            problems.append("queue_max_size too small")
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
            "learning_enabled": self.learning_enabled,
            "auto_placement": self.auto_placement,
            "stale_after_s": self.stale_after_s,
            "inference_interval_s": self.inference_interval_s,
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
            "deprecate_dedicated_declaration": self.deprecate_dedicated_declaration,
        }
