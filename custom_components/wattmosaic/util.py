"""Pure helper functions shared by the WattMosaic integration.

**No Home Assistant imports live in this module.**  Everything here is plain
Python and is covered by ``tests/integration/test_ha_integration.py`` so the
behaviour can be verified on a development machine without HA installed.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .const import (
    CONFIDENCE_TIERS,
    MODEL_STATES,
    QUALITY_STATES,
    REDACTED,
    RUNNING_POWER_THRESHOLD_W,
    SOURCES,
    TOKEN_PLACEHOLDERS,
    UNPUBLISHABLE_QUALITY,
)

# --------------------------------------------------------------------------- #
# Time handling
# --------------------------------------------------------------------------- #


def parse_datetime(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp from the engine into an aware UTC datetime.

    Returns ``None`` for missing, malformed or non-string values instead of
    raising: the engine is a separate process and one bad field must never
    take down the coordinator (master spec §4, "graceful failure").
    """
    if isinstance(value, datetime):
        return ensure_utc(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    # Tolerate a trailing "Z" (Python < 3.11 friendly) and space separators.
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except (ValueError, TypeError):
        return None
    return ensure_utc(parsed)


def ensure_utc(value: datetime) -> datetime:
    """Return an aware UTC datetime; naive values are assumed to be UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def utcnow() -> datetime:
    return datetime.now(UTC)


def age_seconds(timestamp: datetime | None, *, now: datetime | None = None) -> float | None:
    """Seconds elapsed since ``timestamp`` (negative ages clamp to 0)."""
    if timestamp is None:
        return None
    if not isinstance(timestamp, datetime):
        # Defensive: callers may hand us a raw engine string.
        timestamp = parse_datetime(timestamp)
        if timestamp is None:
            return None
    reference = ensure_utc(now) if now else utcnow()
    return max(0.0, (reference - ensure_utc(timestamp)).total_seconds())


# --------------------------------------------------------------------------- #
# Numeric coercion
# --------------------------------------------------------------------------- #


def as_float(value: Any) -> float | None:
    """Best-effort float conversion.  Rejects NaN/inf and booleans."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def as_int(value: Any) -> int | None:
    number = as_float(value)
    if number is None:
        return None
    return int(number)


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def as_bool(value: Any, *, default: bool = True) -> bool:
    """Interpret a config-entry flag, tolerating the strings older options hold.

    ``bool("false")`` is ``True``, so a value that went through JSON or YAML may
    read as enabled when the user wrote the opposite.  This is the single rule
    for every stored flag: booleans and numbers are taken at face value, the
    obvious string spellings of false are false, and anything uninterpretable
    falls back to ``default`` rather than silently changing a user's decision.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if not text:
            return default
        if text in ("false", "0", "no", "off"):
            return False
        if text in ("true", "1", "yes", "on"):
            return True
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def as_percentage(value: Any) -> float | None:
    """Coerce a ``[0, 1]`` confidence/score into a ``[0, 100]`` percentage."""
    number = as_float(value)
    if number is None:
        return None
    return round(clamp(number, 0.0, 1.0) * 100.0, 1)


def round_or_none(value: Any, digits: int = 3) -> float | None:
    number = as_float(value)
    if number is None:
        return None
    return round(number, digits)


# --------------------------------------------------------------------------- #
# Energy
# --------------------------------------------------------------------------- #


def published_energy(
    candidate: Any,
    previous: float | None,
    *,
    publishable: bool = True,
) -> float | None:
    """The kWh value a ``total_increasing`` sensor should publish.

    The engine owns the lifetime total; this function does not invent or
    reshape energy.  It answers one question: is this reading trustworthy
    enough to hand to Home Assistant's statistics engine?

    * ``publishable`` is the quality/staleness gate.  When false the previous
      value is returned unchanged, so a degraded sample never reaches the
      statistics engine.
    * A trustworthy reading is published **verbatim**, including a decrease.

    Whether the entity stays *readable* during an outage is a separate decision
    made by the sensor's ``available``: energy stays readable while the engine
    is merely stale, because a cumulative total is not a current measurement,
    but it goes unavailable when the engine is unreachable — exactly like every
    other time series.

    Why a decrease is published
    ---------------------------
    A decrease means the engine's counter restarted (database reset, restore
    from an older backup).  Home Assistant treats a drop on ``total_increasing``
    as a meter reset and continues the statistics sum from the new value.  That
    is the correct behaviour.

    Holding the previous value instead — which this function used to do — pins
    the sensor *forever*: the engine's new total can never catch up to the held
    value, so the meter never advances again.  A frozen meter silently loses
    all future energy, which is worse than a visible reset.

    Limitation, stated plainly
    --------------------------
    Because a decrease is forwarded, a *transient* dip that recovers (for
    example 12.6 -> 11.0 -> 12.7) is counted by HA as a reset followed by new
    energy.  It relies on the quality gate to withhold such a sample; when a
    dip is marked trustworthy it will be double-counted.  This is Home
    Assistant's own ``total_increasing`` semantics, and it is what powercalc
    does too.
    """
    if not publishable:
        return previous
    raw = as_float(candidate)
    if raw is None:
        return previous
    return raw if raw >= 0.0 else 0.0


def energy_entity_available(
    *,
    engine_available: bool,
    publishable: bool,
    last_value: float | None,
) -> bool:
    """Whether the cumulative ``energy`` entity may report a state.

    A cumulative total is not a *current measurement*, but it is also not
    always readable.  It is available exactly when the engine is reachable and
    there is something to show:

    * a publishable reading (the normal case), **or**
    * a previously stored total (``last_value``), so a gap in engine data does
      not blank a meter that has real history behind it.

    With neither, the entity must be ``unavailable`` rather than re-reporting a
    held number that no longer has any evidence behind it — that is what made a
    frozen 2.041 kWh meter look live while its power sensor was ``unavailable``.
    The held number itself is never lost: the sensor exposes it as the
    ``last_value`` attribute.
    """
    if not engine_available:
        return False
    return bool(publishable) or last_value is not None


def integrate_energy(
    previous: float | None,
    power_w: float | None,
    elapsed_s: float | None,
    *,
    max_gap_s: float,
) -> float | None:
    """Trapezoidal-ish integration used only to fill gaps between polls.

    The engine is the source of truth for energy; this helper exists so the
    integration can keep the total moving between engine samples and it is
    deliberately conservative:

    * no power or no elapsed time -> unchanged total
    * an elapsed time larger than ``max_gap_s`` is clamped (a long outage must
      not create phantom energy, master spec §13 / interface_contracts §3.6)
    """
    if previous is None:
        previous = 0.0
    power = as_float(power_w)
    elapsed = as_float(elapsed_s)
    if power is None or elapsed is None or elapsed <= 0:
        return previous
    elapsed = min(elapsed, max(0.0, as_float(max_gap_s) or 0.0))
    if elapsed <= 0:
        return previous
    return previous + max(0.0, power) * elapsed / 3_600_000.0


# --------------------------------------------------------------------------- #
# Staleness / availability
# --------------------------------------------------------------------------- #


def is_stale(
    timestamp: datetime | None,
    *,
    now: datetime | None = None,
    stale_after: float,
) -> bool:
    """True when ``timestamp`` is older than ``stale_after`` seconds.

    A missing timestamp is treated as stale: never publish an undated value as
    a current measurement.
    """
    age = age_seconds(timestamp, now=now)
    return age is None or age > float(stale_after)


def is_engine_unavailable(
    last_success: datetime | None,
    *,
    now: datetime | None = None,
    unavailable_after: float,
) -> bool:
    """True when the engine has not produced a successful poll recently."""
    age = age_seconds(last_success, now=now)
    return age is None or age > float(unavailable_after)


def publishable_quality(status: Any) -> bool:
    """Whether a quality status allows publishing a *current* value."""
    return str(status or "").strip().lower() not in UNPUBLISHABLE_QUALITY


def device_entities_available(
    *,
    result_present: bool,
    result_timestamp: datetime | None,
    quality_status: Any,
    engine_available: bool,
    now: datetime | None = None,
    stale_after: float,
) -> bool:
    """Decide whether per-device numeric entities should expose a value.

    Stale data is *not* published as a current measurement; the entity becomes
    ``unavailable`` instead (master spec §15.3).
    """
    if not engine_available or not result_present:
        return False
    if not publishable_quality(quality_status):
        return False
    return not is_stale(result_timestamp, now=now, stale_after=stale_after)


def device_is_running(power_w: Any, source: Any) -> bool:
    """Running heuristic.  ``unknown`` sources never assert "running"."""
    if str(source or "").strip().lower() == "unknown":
        return False
    power = as_float(power_w)
    return power is not None and power > RUNNING_POWER_THRESHOLD_W


# --------------------------------------------------------------------------- #
# Snapshot coercion (engine JSON -> flat entity values)
# --------------------------------------------------------------------------- #

_SOURCE_LABELS = frozenset(SOURCES)
_QUALITY_VALUES = frozenset(QUALITY_STATES)
_TIER_VALUES = frozenset(CONFIDENCE_TIERS)


def _coerce_tier(raw: Any) -> str:
    """Return a known evidence tier, defaulting to ``tier_0_unknown``.

    The tier is an ordered grade, not a probability.  An unrecognised value
    from a newer engine must degrade to "unknown" rather than be trusted.
    """
    value = str(raw or "").strip().lower()
    return value if value in _TIER_VALUES else "tier_0_unknown"


def coerce_device_result(raw: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Normalise one ``InferenceResult`` dict into entity-friendly values.

    Unknown keys are preserved in ``extra``; malformed rows return ``None`` so
    a single bad device cannot break the whole snapshot.
    """
    if not isinstance(raw, Mapping):
        return None
    device_id = raw.get("device_id")
    if not isinstance(device_id, str) or not device_id.strip():
        return None

    power = as_float(raw.get("power_w"))
    timestamp = parse_datetime(raw.get("timestamp"))
    source = str(raw.get("source") or "unknown").strip().lower()
    if source not in _SOURCE_LABELS:
        source = "unknown"
    quality = str(raw.get("quality_status") or "missing").strip().lower()
    if quality not in _QUALITY_VALUES:
        quality = "missing"

    return {
        "device_id": device_id.strip(),
        "timestamp": timestamp,
        "power_w": power,
        "energy_kwh": as_float(raw.get("energy_kwh")),
        "source": source,
        "confidence": as_float(raw.get("confidence")),
        "confidence_pct": as_percentage(raw.get("confidence")),
        "confidence_is_calibrated": bool(raw.get("confidence_is_calibrated", False)),
        "confidence_tier": _coerce_tier(raw.get("confidence_tier")),
        "tier_reasons": [
            str(item) for item in raw.get("tier_reasons") or [] if isinstance(item, str)
        ],
        "model_id": raw.get("model_id") if isinstance(raw.get("model_id"), str) else None,
        "model_version": as_int(raw.get("model_version")),
        "operating_stage": raw.get("operating_stage")
        if isinstance(raw.get("operating_stage"), str)
        else None,
        "stage_status": str(raw.get("stage_status") or "unknown").strip().lower(),
        "sample_count": as_int(raw.get("sample_count")) or 0,
        "quality_status": quality,
        "measurement_age_seconds": as_float(raw.get("measurement_age_seconds")),
        "supporting_ct_channels": [
            str(item) for item in raw.get("supporting_ct_channels") or [] if isinstance(item, str)
        ],
        "explanation": [str(item) for item in raw.get("explanation") or [] if isinstance(item, str)],
        "provenance": raw.get("provenance") if isinstance(raw.get("provenance"), Mapping) else {},
    }


def coerce_reconciliation(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalise a ``Reconciliation`` dict.  Missing fields become ``None``."""
    if not isinstance(raw, Mapping):
        raw = {}
    present = bool(raw)
    quality_status = str(raw.get("quality_status") or "missing").strip().lower()
    if quality_status not in _QUALITY_VALUES:
        quality_status = "missing"

    def _opt(key: str) -> float | None:
        return as_float(raw.get(key))

    return {
        "present": present,
        "timestamp": parse_datetime(raw.get("timestamp")),
        "topology_version": as_int(raw.get("topology_version")) or 1,
        #: Whether the balance came from a user-confirmed topology or a
        #: ``candidate`` hypothesis (contract 1.3.0, additive).  Coerced through
        #: ``as_bool`` because an older engine simply omits the key -- absent
        #: must read as "not confirmed", never as ``True``.
        "topology_confirmed": as_bool(raw.get("topology_confirmed"), default=False),
        "main_power_w": as_float(raw.get("main_power_w")),
        "l1_power_w": _opt("l1_power_w"),
        "l2_power_w": _opt("l2_power_w"),
        "direct_measured_w": as_float(raw.get("direct_measured_w")),
        "inferred_w": as_float(raw.get("inferred_w")),
        "model_estimated_w": as_float(raw.get("model_estimated_w")),
        "unknown_residual_w": as_float(raw.get("unknown_residual_w")),
        #: The contract 1.4.0 decomposition of ``unknown_residual_w``, corrected
        #: in 1.5.0.  All are coerced like every other optional number, so an
        #: engine that predates the split (or a snapshot from before a restart)
        #: degrades to ``None`` instead of a fabricated 0 W -- "not reported"
        #: and "zero watts of instrument disagreement" are different facts.
        #:
        #: ``meter_disagreement_w`` is the part of the residual attributable to
        #: the meters reading differently (clipped into the residual);
        #: ``meter_spread_w`` is the raw, unbounded ``|canonical - alias|``
        #: diagnostic.  They are different quantities and must not be summed.
        "unmonitored_w": _opt("unmonitored_w"),
        "meter_disagreement_w": _opt("meter_disagreement_w"),
        "meter_spread_w": _opt("meter_spread_w"),
        #: True when the spread is larger than the whole residual.  Absent
        #: (an older engine) reads ``False``: no claim, never a fabricated one.
        "meter_spread_exceeds_residual": as_bool(
            raw.get("meter_spread_exceeds_residual"), default=False
        ),
        "reconciliation_error_w": as_float(raw.get("reconciliation_error_w")),
        "reconciliation_error_ratio": as_float(raw.get("reconciliation_error_ratio")),
        "accounted_fraction": as_float(raw.get("accounted_fraction")),
        "measurement_uncertainty_w": as_float(raw.get("measurement_uncertainty_w")),
        "quality_status": quality_status,
        "explanation": [str(item) for item in raw.get("explanation") or [] if isinstance(item, str)],
    }


def coerce_quality(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raw = {}
    channels: dict[str, Any] = {}
    raw_channels = raw.get("channels")
    if isinstance(raw_channels, Mapping):
        channels = {str(k): v for k, v in raw_channels.items()}
    return {
        "timestamp": parse_datetime(raw.get("timestamp")),
        "overall_score": as_float(raw.get("overall_score")),
        "overall_score_pct": as_percentage(raw.get("overall_score")),
        "channels": channels,
        "issues": [str(item) for item in raw.get("issues") or [] if isinstance(item, str)],
    }


def coerce_snapshot(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalise a whole ``SnapshotResponse`` into a safe internal structure.

    Never raises.  Missing ``reconciliation`` is synthesised as an empty one so
    global entities degrade to ``unknown`` rather than crashing the update.
    """
    if not isinstance(raw, Mapping):
        raw = {}

    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    raw_results = raw.get("results")
    if isinstance(raw_results, Iterable) and not isinstance(raw_results, (str, bytes)):
        for item in raw_results:
            coerced = coerce_device_result(item)  # type: ignore[arg-type]
            if coerced is None or coerced["device_id"] in seen:
                continue
            seen.add(coerced["device_id"])
            results.append(coerced)

    quality_raw = raw.get("quality")
    return {
        "timestamp": parse_datetime(raw.get("timestamp")),
        "reconciliation": coerce_reconciliation(raw.get("reconciliation")),  # type: ignore[arg-type]
        "results": results,
        "results_by_id": {item["device_id"]: item for item in results},
        "quality": coerce_quality(quality_raw) if isinstance(quality_raw, Mapping) else None,
    }


# --------------------------------------------------------------------------- #
# Engine URL / secret handling
# --------------------------------------------------------------------------- #


def normalise_base_url(value: Any) -> str | None:
    """Validate and canonicalise an engine base URL.

    Accepts ``http://host:8787`` or ``http://host:8787/`` and an optional
    trailing ``/api/v1``; the API path is stripped because the client appends
    it.  Rejects non-HTTP schemes and URLs with a query/fragment.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if "://" not in text:
        text = "http://" + text
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https"):
        return None
    if not parts.hostname:
        return None
    if not re.fullmatch(r"[A-Za-z0-9._:-]+", parts.hostname):
        return None
    if parts.query or parts.fragment:
        return None
    path = parts.path.rstrip("/")
    if path.endswith("/api/v1"):
        path = path[: -len("/api/v1")]
    path = path.rstrip("/")
    if path and not path.startswith("/"):
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    host = parts.hostname
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    netloc = host if port is None else f"{host}:{port}"
    return urlunsplit((parts.scheme, netloc, path, "", ""))


def is_token_configured(token: Any) -> bool:
    """Real token present (placeholders from ``.env.example`` don't count)."""
    if not isinstance(token, str):
        return False
    text = token.strip()
    if not text:
        return False
    return text.lower() not in {p.lower() for p in TOKEN_PLACEHOLDERS}


def redact(value: Any, secret: str | None = None) -> str:
    """Replace a secret with ``**REDACTED**`` anywhere in ``value``."""
    text = value if isinstance(value, str) else str(value)
    if secret and secret.strip():
        text = text.replace(secret.strip(), REDACTED)
    return text


_TOKEN_PATTERNS = (
    re.compile(r"(?i)(x-api-token\s*[:=]\s*)(\S+)"),
    re.compile(r"(?i)(api[_-]?token\s*[:=]\s*)(\S+)"),
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)(\S+)"),
    re.compile(r"(?i)([?&]token=)([^&\s]+)"),
)


def sanitize_text(text: Any, secret: str | None = None) -> str:
    """Scrub secrets from an arbitrary message before logging or displaying."""
    result = redact(text, secret)
    for pattern in _TOKEN_PATTERNS:
        result = pattern.sub(lambda m: m.group(1) + REDACTED, result)
    return result


def redact_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of a config dict with any token value replaced."""
    cleaned: dict[str, Any] = {}
    for key, value in config.items():
        if "token" in str(key).lower() or "password" in str(key).lower() or "secret" in str(key).lower():
            cleaned[key] = REDACTED if value else ""
        else:
            cleaned[key] = value
    return cleaned


# --------------------------------------------------------------------------- #
# Misc
# --------------------------------------------------------------------------- #


def slugify(value: str) -> str:
    """Stable, HA-safe slug used for unique IDs."""
    text = re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower())
    return text.strip("_") or "unknown"


def model_validity(result: Mapping[str, Any] | None) -> str:
    """Derive a user-facing model validity label from an inference result.

    The engine does not expose a dedicated field, so this is a documented
    *presentation* derivation, not a new contract (see docs/integration.md).
    """
    if not isinstance(result, Mapping):
        return "unknown"
    if result.get("source") == "direct_measured":
        return "not_required"
    if result.get("model_id") is None:
        return "invalid" if result.get("source") in ("inferred", "model_estimated") else "unknown"
    if result.get("model_version") is None:
        return "unknown"
    return "valid"


def is_valid_model_state(value: Any) -> bool:
    """Whether ``value`` is one of the published model-validity states."""
    return str(value) in MODEL_STATES
