"""The App option that makes the Discover card's **Apply** button reachable.

The engine gates a placement write on two independent consents:

    allow_apply = apply is True and bool(self.config.auto_placement)

``config.auto_placement`` is read from ``WM_AUTO_PLACEMENT``, which the App is
responsible for setting from its own options.  It was never set, so on every App
install the second consent was permanently ``false``: pressing **Apply** sent
``{"apply": true}`` and the engine answered "auto placement is disabled in the
integration options; enable it ..." — pointing at a switch that existed nowhere
the user could reach.

These tests pin the *wiring* that was missing, not the gate itself.  The gate is
already covered on the engine side: see
``tests/unit/test_placement_discovery.py::test_apply_true_without_the_opt_in_writes_nothing_and_explains_why``
(``apply: true`` with the flag off writes nothing) and
``::test_apply_and_opt_in_writes_and_reports_applied`` (with it on, it writes).

Everything here is static file parsing plus ``EngineConfig.from_env``; no server,
no database, no network.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from wattmosaic_engine.config.settings import EngineConfig

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_YAML = REPO_ROOT / "addon" / "config.yaml"
RUN_SH = REPO_ROOT / "addon" / "run.sh"


def _config() -> dict:
    return yaml.safe_load(CONFIG_YAML.read_text(encoding="utf-8"))


def _option_table() -> dict[str, tuple[str, str]]:
    """Parse ``run.sh``'s ``OPTION_TABLE`` without executing it.

    Same shape as the check in ``scripts/validate_addon.sh``: option | env |
    default, one entry per line.
    """
    text = RUN_SH.read_text(encoding="utf-8")
    block = re.search(r'readonly OPTION_TABLE="\n(.*?)"\n', text, re.S)
    assert block is not None, "OPTION_TABLE not found in addon/run.sh"
    table: dict[str, tuple[str, str]] = {}
    for line in block.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("|")
        assert len(parts) == 3, f"malformed OPTION_TABLE line: {line}"
        table[parts[0]] = (parts[1], parts[2])
    return table


# --------------------------------------------------------------------------- #
# The App manifest exposes the option, defaulting to off
# --------------------------------------------------------------------------- #


def test_config_yaml_exposes_auto_placement_in_options_and_schema() -> None:
    """An option the Supervisor does not know about cannot be set by the user."""
    cfg = _config()

    assert "auto_placement" in cfg["options"], "options: must carry the option"
    assert "auto_placement" in cfg["schema"], "schema: must declare its type"
    assert str(cfg["schema"]["auto_placement"]).rstrip("?") == "bool"


def test_config_yaml_defaults_auto_placement_to_false() -> None:
    """It writes device configurations, so it must fail closed."""
    cfg = _config()

    assert cfg["options"]["auto_placement"] is False


# --------------------------------------------------------------------------- #
# The entrypoint maps it to the variable the engine actually reads
# --------------------------------------------------------------------------- #


def test_run_sh_maps_auto_placement_to_wm_auto_placement() -> None:
    """Without this line the App option is inert — the defect this fixes."""
    table = _option_table()

    assert "auto_placement" in table, "run.sh OPTION_TABLE must map the option"
    env_name, default = table["auto_placement"]
    assert env_name == "WM_AUTO_PLACEMENT"
    assert default == "false", "the mapped default must match config.yaml (off)"


def test_every_config_option_has_a_run_sh_mapping() -> None:
    """Keep the two files in step; ``validate_addon.sh`` enforces this in CI too."""
    cfg = _config()
    table = _option_table()

    assert set(cfg["options"]) == set(cfg["schema"])
    assert set(table) == set(cfg["options"])


# --------------------------------------------------------------------------- #
# The engine still reads the variable and defaults it to off
# --------------------------------------------------------------------------- #


def test_engine_reads_wm_auto_placement_and_defaults_to_false(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A clean environment must not enable a write."""
    monkeypatch.delenv("WM_AUTO_PLACEMENT", raising=False)
    monkeypatch.delenv("WM_HA_URL", raising=False)
    monkeypatch.delenv("WM_HA_TOKEN", raising=False)

    # ``dotenv_path`` points at a file that does not exist so a developer's
    # ``.env`` cannot leak into the assertion.
    cfg = EngineConfig.from_env({}, dotenv_path=tmp_path / "absent.env")

    assert cfg.auto_placement is False
    assert EngineConfig().auto_placement is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("true", True),
        ("1", True),
        ("yes", True),
        ("false", False),
        ("0", False),
        ("nonsense", False),
    ],
)
def test_engine_parses_wm_auto_placement(
    raw: str, expected: bool, tmp_path: Path
) -> None:
    """The App writes ``true``/``false``; anything unrecognised stays off."""
    cfg = EngineConfig.from_env(
        {"WM_AUTO_PLACEMENT": raw}, dotenv_path=tmp_path / "absent.env"
    )

    assert cfg.auto_placement is expected


def test_the_option_is_reported_in_the_redacted_config() -> None:
    """An operator can confirm the setting without reading the source."""
    assert "auto_placement" in EngineConfig().redacted()
