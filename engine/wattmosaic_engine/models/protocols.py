"""Narrow, duck-typed protocols shared by the model / learning / aggregation code.

The electrical and disaggregation modules are developed in parallel and must not
be imported here (see the workstream file-scope rules).  Instead this module
declares the *minimum* shapes we consume and provides forgiving adapters:

* ``ModelContext``  - anything that can describe the current HA state of a device.
* ``AlignedWindow`` - anything that exposes per-channel CT values + ages.
* ``WindowReading`` - one channel reading inside an aligned window.

All adapters accept plain mappings, dataclasses, pydantic models and objects with
attributes.  That keeps integration a matter of "pass whatever you have" while
still documenting exactly which information improves estimate quality.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

# --------------------------------------------------------------------------- #
# Protocols
# --------------------------------------------------------------------------- #


@runtime_checkable
class ModelContextProtocol(Protocol):
    """Context passed to a device model's ``predict``/``estimate``.

    Only ``device_id`` is conventional; every other attribute is optional and
    read through :func:`context_value`.
    """

    device_id: str | None


@runtime_checkable
class WindowReadingProtocol(Protocol):
    """One CT reading inside an aligned window."""

    value: float
    age_s: float
    quality: str
    repeated: bool


@runtime_checkable
class AlignedWindowProtocol(Protocol):
    """An aligned multi-CT snapshot (see ``synchronization.SampleAligner``).

    Implementations in the wild may expose ``values`` (ct_id -> watts),
    ``channels`` (ct_id -> reading), ``readings``, ``ages`` or a ``value()``
    method.  :func:`window_channels` handles all of these.
    """

    at: datetime


# --------------------------------------------------------------------------- #
# Convenience implementation used by tests and by callers without an aligner
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class SimpleReading:
    value: float
    age_s: float = 0.0
    quality: str = "good"
    repeated: bool = False


@dataclass(slots=True)
class SimpleWindow:
    """Minimal standards-compliant aligned window.

    ``values`` maps CT ids to watts.  ``ages``/``qualities``/``repeated`` are
    parallel mappings; missing entries default to age 0 / good / not repeated.
    """

    at: datetime
    values: dict[str, float] = field(default_factory=dict)
    ages: dict[str, float] = field(default_factory=dict)
    qualities: dict[str, str] = field(default_factory=dict)
    repeated: dict[str, bool] = field(default_factory=dict)
    max_age_s: float | None = None

    def channels(self) -> dict[str, SimpleReading]:
        return {
            ct_id: SimpleReading(
                value=float(value),
                age_s=float(self.ages.get(ct_id, 0.0)),
                quality=str(self.qualities.get(ct_id, "good")),
                repeated=bool(self.repeated.get(ct_id, False)),
            )
            for ct_id, value in self.values.items()
        }


# --------------------------------------------------------------------------- #
# Adapters: context
# --------------------------------------------------------------------------- #

_MISSING = object()


def _as_mapping(context: Any) -> Mapping[str, Any]:
    if context is None:
        return {}
    if isinstance(context, Mapping):
        return context
    dump = getattr(context, "model_dump", None)
    if callable(dump):
        try:
            return dump()
        except Exception:  # pragma: no cover - defensive
            pass
    return {}


def context_value(context: Any, *names: str, default: Any = None) -> Any:
    """Read the first present key/attribute from a context-like object."""
    mapping = _as_mapping(context)
    for name in names:
        if name in mapping and mapping[name] is not None:
            return mapping[name]
        value = getattr(context, name, _MISSING)
        if value is not _MISSING and value is not None:
            return value
    return default


def context_attributes(context: Any) -> dict[str, Any]:
    """Return the HA ``attributes`` mapping (or an empty dict)."""
    raw = context_value(context, "attributes", "attrs", "state_attributes", default={})
    return dict(raw) if isinstance(raw, Mapping) else {}


def attribute_value(context: Any, *names: str, default: Any = None) -> Any:
    """Look in ``context.attributes`` first, then at top-level context fields."""
    attrs = context_attributes(context)
    for name in names:
        if name in attrs and attrs[name] is not None:
            return attrs[name]
        value = context_value(context, name, default=_MISSING)
        if value is not _MISSING and value is not None:
            return value
    return default


def context_timestamp(context: Any) -> datetime | None:
    value = context_value(context, "timestamp", "at", "captured_at", "time")
    return _coerce_datetime(value)


def context_state(context: Any) -> str | None:
    value = context_value(context, "state", "ha_state", "new_state")
    if value is None:
        return None
    return str(value).strip().lower()


def context_power_series(context: Any) -> list[tuple[datetime, float]]:
    """Optional ``power_series``/``series`` on the context (for stage tracking)."""
    raw = context_value(context, "power_series", "series", "history")
    return normalise_series(raw)


# --------------------------------------------------------------------------- #
# Adapters: windows
# --------------------------------------------------------------------------- #


def window_time(window: Any) -> datetime:
    value = context_value(window, "at", "timestamp", "time", "captured_at")
    dt = _coerce_datetime(value)
    return dt or datetime.now(UTC)


def _reading_from(value: Any, *, age: float = 0.0, quality: str = "good", repeated: bool = False) -> dict[str, Any]:
    if isinstance(value, Mapping):
        raw = value.get("value", value.get("value_w", value.get("power_w", value.get("watts"))))
        return {
            "value": float(raw) if raw is not None else None,
            "age_s": float(value.get("age_s", value.get("age_seconds", value.get("age", age)))),
            "quality": str(value.get("quality", quality)),
            "repeated": bool(value.get("repeated", repeated)),
        }
    if isinstance(value, (int, float)):
        return {"value": float(value), "age_s": float(age), "quality": str(quality), "repeated": bool(repeated)}
    raw = (
        getattr(value, "value_w", None)
        if getattr(value, "value_w", None) is not None
        else getattr(value, "value", None)
        if getattr(value, "value", None) is not None
        else getattr(value, "power_w", None)
        if getattr(value, "power_w", None) is not None
        else getattr(value, "watts", None)
    )
    return {
        "value": float(raw) if raw is not None else None,
        "age_s": float(
            getattr(value, "age_s", None)
            if getattr(value, "age_s", None) is not None
            else getattr(value, "age_seconds", None)
            if getattr(value, "age_seconds", None) is not None
            else getattr(value, "age", age)
        ),
        "quality": str(getattr(value, "quality", quality)),
        "repeated": bool(getattr(value, "repeated", repeated)),
    }


def window_channels(window: Any) -> dict[str, dict[str, Any]]:
    """Normalise an aligned window to ``{ct_id: {value,age_s,quality,repeated}}``.

    Supported shapes (checked in order):
    ``channels`` mapping, ``readings`` mapping, ``values`` mapping (ages taken
    from parallel ``ages``/``qualities``/``repeated`` mappings), or a
    ``value(ct_id)`` method combined with a ``ct_ids``/``channels`` iterable.
    """
    raw = context_value(window, "channels", "readings")
    if isinstance(raw, Mapping):
        ages = context_value(window, "ages", default={}) or {}
        qualities = context_value(window, "qualities", default={}) or {}
        repeated = context_value(window, "repeated", default={}) or {}
        out: dict[str, dict[str, Any]] = {}
        for ct_id, value in raw.items():
            out[str(ct_id)] = _reading_from(
                value,
                age=float(ages.get(ct_id, 0.0)) if isinstance(ages, Mapping) else 0.0,
                quality=str(qualities.get(ct_id, "good")) if isinstance(qualities, Mapping) else "good",
                repeated=bool(repeated.get(ct_id, False)) if isinstance(repeated, Mapping) else False,
            )
        return out

    values = context_value(window, "values", "power_w")
    if isinstance(values, Mapping):
        ages = context_value(window, "ages", default={}) or {}
        qualities = context_value(window, "qualities", default={}) or {}
        repeated = context_value(window, "repeated", default={}) or {}
        return {
            str(ct_id): _reading_from(
                value,
                age=float(ages.get(ct_id, 0.0)) if isinstance(ages, Mapping) else 0.0,
                quality=str(qualities.get(ct_id, "good")) if isinstance(qualities, Mapping) else "good",
                repeated=bool(repeated.get(ct_id, False)) if isinstance(repeated, Mapping) else False,
            )
            for ct_id, value in values.items()
        }

    getter = getattr(window, "value", None)
    ids = context_value(window, "ct_ids", "channel_ids")
    if callable(getter) and isinstance(ids, Sequence) and not isinstance(ids, (str, bytes)):
        out = {}
        for ct_id in ids:
            try:
                out[str(ct_id)] = _reading_from(getter(ct_id))
            except Exception:  # pragma: no cover - defensive
                continue
        return out

    # Last resort: serialise __dict__ and keep only numeric channel attributes.
    data = getattr(window, "__dict__", {})
    out = {}
    for key, value in data.items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out[str(key)] = _reading_from(value)
    return out


def channel_value(window: Any, ct_id: str) -> dict[str, Any] | None:
    return window_channels(window).get(str(ct_id))


# --------------------------------------------------------------------------- #
# Series helpers
# --------------------------------------------------------------------------- #


def _coerce_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def normalise_series(raw: Any) -> list[tuple[datetime, float]]:
    """Normalise a power series to a time-sorted ``[(datetime, watts)]`` list."""
    if raw is None:
        return []
    items: list[Any]
    if isinstance(raw, Mapping):
        items = list(raw.items())
    else:
        try:
            items = list(raw)
        except TypeError:
            return []
    out: list[tuple[datetime, float]] = []
    for item in items:
        if isinstance(item, Mapping):
            t = item.get("timestamp", item.get("at", item.get("time")))
            p = item.get("power_w", item.get("value", item.get("watts")))
        elif isinstance(item, (tuple, list)) and len(item) >= 2:
            t, p = item[0], item[1]
        else:
            t = getattr(item, "timestamp", None) or getattr(item, "at", None) or getattr(item, "time", None)
            p = (
                getattr(item, "power_w", None)
                if getattr(item, "power_w", None) is not None
                else getattr(item, "value", None)
            )
        dt = _coerce_datetime(t)
        if dt is None or p is None:
            continue
        try:
            value = float(p)
        except (TypeError, ValueError):
            continue
        if value != value or value in (float("inf"), float("-inf")):
            continue
        out.append((dt, value))
    out.sort(key=lambda pair: pair[0])
    # collapse duplicate timestamps (last wins)
    collapsed: list[tuple[datetime, float]] = []
    for dt, value in out:
        if collapsed and collapsed[-1][0] == dt:
            collapsed[-1] = (dt, value)
        else:
            collapsed.append((dt, value))
    return collapsed


def normalise_timestamp(value: datetime | float | str | None) -> datetime | None:
    return _coerce_datetime(value)


__all__ = [
    "AlignedWindowProtocol",
    "ModelContextProtocol",
    "SimpleReading",
    "SimpleWindow",
    "WindowReadingProtocol",
    "attribute_value",
    "channel_value",
    "context_attributes",
    "context_power_series",
    "context_state",
    "context_timestamp",
    "context_value",
    "normalise_series",
    "normalise_timestamp",
    "window_channels",
    "window_time",
]
