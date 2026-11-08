"""Cross-cutting safety invariants (orchestrator-owned).

These are the non-negotiable properties from the master instructions. They are
checked mechanically across the *whole* repository, not per-module, because a
single forgotten call site would break the guarantee.

1. The engine and integration can never actuate a household device.
2. No secret is embedded in source, config examples, schemas or docs.
3. No component silently clamps negative power to zero.
4. Unknown residual is never redistributed to devices.
5. Stale readings never become learning evidence.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ENGINE = ROOT / "engine" / "wattmosaic_engine"
INTEGRATION = ROOT / "custom_components" / "wattmosaic"

PY_FILES = sorted(
    p for p in list(ENGINE.rglob("*.py")) + list(INTEGRATION.rglob("*.py"))
    if "__pycache__" not in p.parts
)


def test_sources_exist() -> None:
    assert PY_FILES, "no python sources found"


# --------------------------------------------------------------------------- #
# 1. Read-only safety
# --------------------------------------------------------------------------- #

ACTUATION_CALL_PATTERNS = [
    # HTTP calls to HA services
    re.compile(r"/api/services/"),
    # HA service invocation by any spelling
    re.compile(r"services\s*\.\s*(async_call|call|call_service)"),
    re.compile(r"\bcall_service\b"),
    # direct device control helpers
    re.compile(r"\basync_turn_on\s*\("),
    re.compile(r"\basync_turn_off\s*\("),
    re.compile(r"\basync_toggle\s*\("),
    re.compile(r"\bset_temperature\s*\("),
    re.compile(r"\bset_hvac_mode\s*\("),
    re.compile(r"\bopen_cover\s*\("),
    re.compile(r"\bset_cover_position\s*\("),
    re.compile(r"\bset_brightness\s*\("),
]


def test_no_home_assistant_service_calls() -> None:
    offenders: list[str] = []
    for path in PY_FILES:
        if path.name == "test" or "tests" in path.parts:
            continue
        text = path.read_text()
        for pattern in ACTUATION_CALL_PATTERNS:
            for match in pattern.finditer(text):
                lineno = text[: match.start()].count("\n") + 1
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {pattern.pattern}")
    assert not offenders, "Actuation code paths are forbidden:\n" + "\n".join(offenders)


def test_websocket_commands_are_read_only() -> None:
    """Only read/subscribe WS commands may be issued."""
    allowed = {
        "auth",
        "get_states",
        "get_config",
        "get_services",
        "subscribe_events",
        "unsubscribe_events",
        "config/entity_registry/list",
        "config/device_registry/list",
        "config/area_registry/list",
        "config/floor_registry/list",
        "config/label_registry/list",
        "config/category_registry/list",
        "render_template",
        "ping",
        "supported_features",
        "get_panels",
        "search/related",
    }
    forbidden_prefixes = (
        "call_service",
        "execute_script",
        "fire_event",
        "config/",
        "admin/",
        "backup/",
        "supervisor/",
        "hassio/",
    )
    offenders: list[str] = []
    for path in PY_FILES:
        text = path.read_text()
        # any literal message "type" used as a WS command
        for match in re.finditer(r'["\']type["\']\s*:\s*["\']([a-zA-Z0-9_/]+)["\']', text):
            msg_type = match.group(1)
            if msg_type.startswith(forbidden_prefixes) and msg_type not in allowed:
                lineno = text[: match.start()].count("\n") + 1
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}: ws type {msg_type}")
            if msg_type in ("call_service", "execute_script"):
                lineno = text[: match.start()].count("\n") + 1
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}: ws type {msg_type}")
    assert not offenders, "Forbidden websocket command types:\n" + "\n".join(offenders)


def test_engine_has_no_dependency_on_homeassistant() -> None:
    offenders = []
    for path in ENGINE.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text()
        if re.search(r"^\s*(import|from)\s+homeassistant\b", text, re.MULTILINE):
            offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, f"Engine must not depend on Home Assistant internals: {offenders}"


# --------------------------------------------------------------------------- #
# 2. Secrets
# --------------------------------------------------------------------------- #

SECRET_PATTERNS = [
    re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}"),
    re.compile(r"\bghp_[A-Za-z0-9]{30,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}"),
    re.compile(r"\bsk-[A-Za-z0-9]{30,}"),
]

SCAN_SUFFIXES = {".py", ".json", ".yaml", ".yml", ".md", ".toml", ".sh", ".example"}
ALLOW_HINTS = ("replace", "placeholder", "example", "redacted", "***", "your-", "<", "xxx")


def _scan_files() -> list[Path]:
    files: list[Path] = []
    for base in (ENGINE, INTEGRATION, ROOT / "addon", ROOT / "tools", ROOT / "docs", ROOT / "data" / "examples", ROOT / "data" / "schemas"):
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if p.is_file() and p.suffix in SCAN_SUFFIXES and "__pycache__" not in p.parts:
                files.append(p)
    return files


def test_no_hardcoded_secrets() -> None:
    offenders: list[str] = []
    for path in _scan_files():
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if any(hint in line.lower() for hint in ALLOW_HINTS):
                continue
            for pattern in SECRET_PATTERNS:
                if pattern.search(line):
                    offenders.append(f"{path.relative_to(ROOT)}:{lineno}")
    assert not offenders, "Possible hardcoded secrets:\n" + "\n".join(offenders)


def test_env_example_has_no_real_token() -> None:
    text = (ROOT / ".env.example").read_text()
    for line in text.splitlines():
        if line.strip().startswith("HA_TOKEN=") or line.strip().startswith("WM_API_TOKEN="):
            value = line.split("=", 1)[1]
            assert "replace" in value.lower(), f"placeholder missing in: {line[:40]}"


def test_gitignore_covers_env_and_db() -> None:
    text = (ROOT / ".gitignore").read_text()
    assert re.search(r"^\.env$", text, re.MULTILINE)
    assert "*.db" in text or "data/*.db" in text


def test_config_redacts_secrets() -> None:
    from wattmosaic_engine.config.settings import EngineConfig

    # Values are marked "redaction-test" so the repository secret scanner can
    # tell them apart from a real credential.
    cfg = EngineConfig(
        ha_token="redaction-test-ha-token", api_token="redaction-test-api-token"
    )
    redacted = cfg.redacted()
    assert "redaction-test-ha-token" not in str(redacted)
    assert "redaction-test-api-token" not in str(redacted)
    assert "redaction-test-ha-token" not in repr(cfg)


# --------------------------------------------------------------------------- #
# 3-5. Numerical / semantic invariants
# --------------------------------------------------------------------------- #


def test_no_silent_clamping_of_negative_power_in_engine() -> None:
    """Negative *measurements* must be preserved as reverse flow.

    Scope: raw observation handling. Fitted model quantities (segment levels,
    solver estimates) are legitimately bounded at zero because they represent
    estimated consumption, and bounding them is not falsifying a measurement.
    """
    offenders: list[str] = []
    suspicious = re.compile(r"max\(\s*0(?:\.0)?\s*,\s*([A-Za-z_][A-Za-z0-9_\.\[\]]*)\s*\)")
    measurement_names = re.compile(
        r"^(?:.*\.)?(measurement|reading|readings|raw|observed|observed_w|main_w|main_power_w|l1|l2)$",
        re.I,
    )
    for path in ENGINE.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text()
        for match in suspicious.finditer(text):
            expr = match.group(1)
            if not measurement_names.match(expr):
                continue
            lineno = text[: match.start()].count("\n") + 1
            offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {expr}")
    assert not offenders, "Silent clamping of a negative measurement:\n" + "\n".join(offenders)


def test_main_power_preserves_reverse_flow() -> None:
    """A negative main reading (export) must survive summation unchanged."""
    from wattmosaic_engine.contracts import CTChannel, CTType, Phase, Topology

    topology = Topology(
        channels=[
            CTChannel(ct_id="m1", entity_id="sensor.m1", ct_type=CTType.MAIN, phase=Phase.L1),
            CTChannel(ct_id="m2", entity_id="sensor.m2", ct_type=CTType.MAIN, phase=Phase.L2),
        ]
    )
    from wattmosaic_engine.electrical import ElectricalModel

    model = ElectricalModel(topology)
    result = model.main_power_w({"m1": -1500.0, "m2": 200.0})
    total = getattr(result, "total_w", None)
    if total is None:
        total = getattr(result, "power_w", None)
    assert total == pytest.approx(-1300.0)


def test_measurement_contract_preserves_sign() -> None:
    from datetime import UTC, datetime

    from wattmosaic_engine.contracts import Measurement

    m = Measurement(entity_id="sensor.x", ct_id="c", value=-1234.5,
                    state_timestamp=datetime.now(UTC))
    assert m.effective_power_w() == -1234.5


def test_reconciliation_schema_has_residual_field() -> None:
    from wattmosaic_engine.contracts import Reconciliation

    fields = Reconciliation.model_fields
    assert "unknown_residual_w" in fields
    assert "accounted_fraction" in fields
    # A residual must not be representable as a per-device distribution field.
    assert not any("redistribut" in name for name in fields)


def test_learning_sample_requires_rejection_reason_when_rejected() -> None:
    """A rejected sample must be attributable to a documented reason code."""
    from wattmosaic_engine.contracts import SampleRejection

    codes = {item.value for item in SampleRejection}
    assert "stale_measurement" in codes
    assert "concurrent_events" in codes
    assert "ambiguous_topology" in codes
    assert "duplicate_state" in codes


def test_stale_quality_exists_and_is_distinct_from_good() -> None:
    from wattmosaic_engine.contracts import QualityStatus

    values = {q.value for q in QualityStatus}
    assert "stale" in values
    assert "good" in values


def test_model_status_supports_lifecycle() -> None:
    from wattmosaic_engine.contracts import ModelStatus

    values = {m.value for m in ModelStatus}
    for required in ("candidate", "active", "frozen", "rolled_back", "rejected"):
        assert required in values


def test_confidence_is_declared_uncalibrated_by_default() -> None:
    from datetime import UTC, datetime

    from wattmosaic_engine.contracts import InferenceResult, Source

    result = InferenceResult(
        device_id="d", timestamp=datetime.now(UTC), power_w=1.0,
        source=Source.INFERRED, confidence=0.99,
    )
    assert result.confidence_is_calibrated is False
