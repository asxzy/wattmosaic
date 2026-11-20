"""T01 — the ``raw_capture_days`` engine setting (ADR-0042).

0 disables persistence of canonical CT readings entirely (the HA recorder is
the production history); N in 1-90 keeps full-rate debug capture for N days.
This module pins the default, env parsing (including the explicit-``0`` trap),
validation bounds and the log-safe ``redacted()`` view.
"""

from __future__ import annotations

from wattmosaic_engine.config.settings import (
    RAW_CAPTURE_DAYS_RANGE,
    EngineConfig,
)


def _config(**env: str) -> EngineConfig:
    base = {"WM_HA_URL": "http://homeassistant:8123", "WM_HA_TOKEN": "test-token"}
    base.update(env)
    return EngineConfig.from_env(base, dotenv_path="/nonexistent.env")


def test_default_is_zero() -> None:
    assert EngineConfig().raw_capture_days == 0
    assert _config().raw_capture_days == 0


def test_env_sets_days() -> None:
    assert _config(WM_RAW_CAPTURE_DAYS="5").raw_capture_days == 5


def test_env_zero_is_preserved() -> None:
    # Regression guard for the `get(...) or default` trap: an explicit 0 must
    # not fall back to the shipped default.
    assert _config(WM_RAW_CAPTURE_DAYS="0").raw_capture_days == 0


def test_env_empty_uses_default() -> None:
    assert _config(WM_RAW_CAPTURE_DAYS="").raw_capture_days == 0


def test_validate_rejects_out_of_range() -> None:
    message = "raw_capture_days must be in 0-90"
    for bad in (-1, 91):
        assert message in _config(WM_RAW_CAPTURE_DAYS=str(bad)).validate()
    for good in (0, 90):
        assert message not in _config(WM_RAW_CAPTURE_DAYS=str(good)).validate()


def test_redacted_includes_key() -> None:
    cfg = _config(WM_RAW_CAPTURE_DAYS="3", WM_API_TOKEN="secret-token")
    redacted = cfg.redacted()
    assert redacted["raw_capture_days"] == 3
    # redacted() is the log-safe view: credentials stay masked.
    assert redacted["api_token"] == "***"
    assert "secret-token" not in redacted.values()


def test_retention_days_raw_unchanged() -> None:
    assert EngineConfig().retention_days_raw == 7
    assert RAW_CAPTURE_DAYS_RANGE == (0, 90)
