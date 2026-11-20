"""T10 — cross-unit parity for the ``raw_capture_days`` setting (ADR-0042).

Wave 1 coded each unit against ``interfaces.md``; these tests can only exist
once the App option, the engine setting and the storage tier have all landed.
They mirror the ``retention_days_raw`` parity checks in
``test_derived_retention.py`` so the App↔engine↔storage contract cannot drift.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from wattmosaic_engine.config.settings import (
    RAW_CAPTURE_DAYS_RANGE,
    EngineConfig,
)
from wattmosaic_engine.storage import tiers

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_addon_and_engine_capture_bounds_match() -> None:
    """The App option must accept exactly what the engine validator accepts."""
    cfg = yaml.safe_load((REPO_ROOT / "addon" / "config.yaml").read_text(encoding="utf-8"))
    declaration = str(cfg["schema"]["raw_capture_days"])
    match = re.search(r"int\((\d+),\s*(\d+)\)", declaration)
    assert match is not None, f"unexpected schema declaration: {declaration!r}"
    addon_low, addon_high = int(match.group(1)), int(match.group(2))
    engine_low, engine_high = RAW_CAPTURE_DAYS_RANGE
    assert (addon_low, addon_high) == (engine_low, engine_high)


def test_addon_capture_default_matches_engine() -> None:
    cfg = yaml.safe_load((REPO_ROOT / "addon" / "config.yaml").read_text(encoding="utf-8"))
    run_sh = (REPO_ROOT / "addon" / "run.sh").read_text(encoding="utf-8")
    assert cfg["options"]["raw_capture_days"] == EngineConfig().raw_capture_days == 0
    assert "raw_capture_days|WM_RAW_CAPTURE_DAYS|0" in run_sh


def test_tier_env_name_matches_settings() -> None:
    """The storage tier and the settings loader agree on the one env var name."""
    assert tiers.RAW_CAPTURE_ENV == "WM_RAW_CAPTURE_DAYS"
    env_name = tiers.RAW_CAPTURE_ENV
    cfg = EngineConfig.from_env(
        {"WM_HA_URL": "http://homeassistant:8123", "WM_HA_TOKEN": "t", env_name: "9"},
        dotenv_path="/nonexistent.env",
    )
    assert cfg.raw_capture_days == 9
