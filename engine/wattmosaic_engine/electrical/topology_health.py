"""Confirmed-edge health monitor for measurement-inferred topology (M3).

The T5 ledger keeps cumulative support/conflict counts with hysteresis, so an
edge confirmed by hundreds of events can stay CONFIRMED long after the wiring
changed.  This module keeps a *rolling* window of the most recent decisive
verdicts for each confirmed edge.  A rolling conflict ratio answers the question
the cumulative ledger cannot: is this edge still healthy *now*?

The module is pure: no I/O, no clock, no scenario knowledge.  Callers inject the
verdict (``"support"`` / ``"conflict"`` / ``None``) and the timestamp.  Everything
is immutable -- each ``record`` returns a new object and leaves the old one
untouched.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from typing import Any

__all__: list[str] = [
    "BROKEN_CONFLICT_RATIO",
    "DEGRADED_CONFLICT_RATIO",
    "HEALTH_MIN_DECISIVE",
    "HEALTH_WINDOW",
    "EdgeHealth",
    "HealthBook",
    "HealthStatus",
    "demotions",
]

HEALTH_WINDOW = 20
HEALTH_MIN_DECISIVE = 5
DEGRADED_CONFLICT_RATIO = 0.20
BROKEN_CONFLICT_RATIO = 0.50


class HealthStatus(str, Enum):
    """Rolling-window health of one confirmed edge."""

    UNKNOWN = "unknown"     # fewer than HEALTH_MIN_DECISIVE verdicts in the window
    HEALTHY = "healthy"
    DEGRADED = "degraded"   # conflict ratio > DEGRADED_CONFLICT_RATIO
    BROKEN = "broken"       # conflict ratio > BROKEN_CONFLICT_RATIO


@dataclass(frozen=True, slots=True)
class EdgeHealth:
    """Rolling verdict window and timestamps for one (parent, child) edge."""

    parent: str
    child: str
    recent: tuple[str, ...] = ()           # "s" or "c", oldest first, len <= HEALTH_WINDOW
    last_support_at: datetime | None = None
    last_conflict_at: datetime | None = None
    checked: int = 0                        # all decisive verdicts ever recorded

    def record(self, verdict: str | None, at: datetime) -> EdgeHealth:
        """Append a decisive verdict, trimming the window; neutral steps are a no-op."""
        if verdict is None:
            return self
        if verdict == "support":
            token = "s"
        elif verdict == "conflict":
            token = "c"
        else:
            raise ValueError(f"unknown health verdict: {verdict!r}")
        recent = (self.recent + (token,))[-HEALTH_WINDOW:]
        return replace(
            self,
            recent=recent,
            last_support_at=at if token == "s" else self.last_support_at,
            last_conflict_at=at if token == "c" else self.last_conflict_at,
            checked=self.checked + 1,
        )

    @property
    def conflict_ratio(self) -> float:
        """Share of conflicts in the rolling window; 0.0 when empty."""
        if not self.recent:
            return 0.0
        return self.recent.count("c") / len(self.recent)

    @property
    def status(self) -> HealthStatus:
        """Health derived from the rolling window and its size."""
        if len(self.recent) < HEALTH_MIN_DECISIVE:
            return HealthStatus.UNKNOWN
        ratio = self.conflict_ratio
        if ratio > BROKEN_CONFLICT_RATIO:
            return HealthStatus.BROKEN
        if ratio > DEGRADED_CONFLICT_RATIO:
            return HealthStatus.DEGRADED
        return HealthStatus.HEALTHY

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe representation (datetimes isoformat, status value)."""
        return {
            "parent": self.parent,
            "child": self.child,
            "recent": list(self.recent),
            "last_support_at": self.last_support_at.isoformat() if self.last_support_at else None,
            "last_conflict_at": self.last_conflict_at.isoformat() if self.last_conflict_at else None,
            "checked": self.checked,
            "status": self.status.value,
        }


def _parse_datetime(value: Any) -> datetime | None:
    """Parse an isoformat timestamp, treating a missing or bad value as absent."""
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class HealthBook:
    """Immutable collection of per-edge health records."""

    entries: Mapping[tuple[str, str], EdgeHealth] = field(default_factory=dict)

    def record(self, parent: str, child: str, verdict: str | None, at: datetime) -> HealthBook:
        """Return a new book with one more verdict for the given edge."""
        if verdict is None:
            return self
        key = (parent, child)
        current = self.entries.get(key, EdgeHealth(parent=parent, child=child))
        updated = current.record(verdict, at)
        entries = dict(self.entries)
        entries[key] = updated
        return HealthBook(entries=entries)

    def get(self, parent: str, child: str) -> EdgeHealth:
        """Return the edge health, or an empty record when absent."""
        return self.entries.get((parent, child), EdgeHealth(parent=parent, child=child))

    def status(self, parent: str, child: str) -> HealthStatus:
        """Rolling health status of one edge."""
        return self.get(parent, child).status

    def broken(self) -> list[tuple[str, str]]:
        """Sorted keys of every edge whose rolling health is BROKEN."""
        return sorted(key for key, entry in self.entries.items() if entry.status is HealthStatus.BROKEN)

    def prune(self, keep: Iterable[tuple[str, str]]) -> HealthBook:
        """Return a new book holding only the given (parent, child) keys."""
        keep_set = set(keep)
        return HealthBook(entries={key: entry for key, entry in self.entries.items() if key in keep_set})

    def to_json(self) -> str:
        """Deterministic JSON: entries keyed by ``parent\\x00child``, sorted keys."""
        payload = {
            f"{parent}\x00{child}": entry.as_dict()
            for (parent, child), entry in self.entries.items()
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, text: str | None) -> HealthBook:
        """Load a book; ``None``/empty/invalid input yields an empty book."""
        if not text:
            return cls()
        try:
            payload = json.loads(text)
            if not isinstance(payload, dict):
                return cls()
            entries: dict[tuple[str, str], EdgeHealth] = {}
            for key, value in payload.items():
                parent, child = key.split("\x00", 1)
                entries[(parent, child)] = EdgeHealth(
                    parent=parent,
                    child=child,
                    recent=tuple(value["recent"]),
                    last_support_at=_parse_datetime(value.get("last_support_at")),
                    last_conflict_at=_parse_datetime(value.get("last_conflict_at")),
                    checked=int(value.get("checked", 0)),
                )
            return cls(entries=entries)
        except (AttributeError, KeyError, TypeError, ValueError):
            return cls()


def _attr(edge: Any, name: str) -> str | None:
    """Read an attribute as a plain string, unwrapping enums via ``.value``."""
    value = getattr(edge, name, None)
    raw = getattr(value, "value", value)
    return str(raw) if raw is not None else None


def demotions(book: HealthBook, edges: Iterable[Any]) -> list[tuple[str, str]]:
    """Return ``(edge_id, reason)`` for each inferred, confirmed, unpinned, broken edge.

    ``relation`` and ``lifecycle`` may be enums or strings; both are normalised
    with :func:`_attr`.  Attributes are read with ``getattr`` so callers can pass
    contract objects or plain namespaces.
    """
    out: list[tuple[str, str]] = []
    for edge in edges:
        if _attr(edge, "relation") != "contains":
            continue
        if _attr(edge, "lifecycle") != "confirmed":
            continue
        if _attr(edge, "declared_by") != "inference":
            continue
        if bool(getattr(edge, "pinned", False)):
            continue
        parent = getattr(edge, "source_endpoint_id", None)
        child = getattr(edge, "target_endpoint_id", None)
        if parent is None or child is None:
            continue
        health = book.get(parent, child)
        if health.status is not HealthStatus.BROKEN:
            continue
        n_conflict = health.recent.count("c")
        n_window = len(health.recent)
        out.append((
            str(edge.edge_id),
            f"health: {n_conflict}/{n_window} recent steps unmatched",
        ))
    return out
