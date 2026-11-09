"""Configuration loading with validation.

Secrets are read from the environment (or a .env file) and are never logged.
``redacted()`` produces a log-safe view.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

DEFAULT_HA_PORT = 8123


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
        }
