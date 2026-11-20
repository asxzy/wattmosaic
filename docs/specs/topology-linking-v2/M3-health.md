# M3: confirmed-edge health monitor

Create `engine/wattmosaic_engine/electrical/topology_health.py` and
`tests/unit/test_topology_health.py`. Edit no other file.

## Why

The ledger keeps cumulative support/conflict counts with hysteresis, so an
edge confirmed by 200 events stays CONFIRMED long after the wiring changed
(a plug moved, a sub-panel rewired). After an edge is confirmed we keep
checking every step of the child: did the parent still move by it? A rolling
window of the most recent verdicts tells whether the edge is still healthy.

## API (exact names)

```python
HEALTH_WINDOW = 20
HEALTH_MIN_DECISIVE = 5
DEGRADED_CONFLICT_RATIO = 0.20
BROKEN_CONFLICT_RATIO = 0.50

class HealthStatus(str, Enum):
    UNKNOWN = "unknown"     # fewer than HEALTH_MIN_DECISIVE verdicts in the window
    HEALTHY = "healthy"
    DEGRADED = "degraded"   # conflict ratio in the window > DEGRADED_CONFLICT_RATIO
    BROKEN = "broken"       # conflict ratio in the window > BROKEN_CONFLICT_RATIO

@dataclass(frozen=True, slots=True)
class EdgeHealth:
    parent: str
    child: str
    recent: tuple[str, ...] = ()           # "s" or "c", oldest first, len <= HEALTH_WINDOW
    last_support_at: datetime | None = None
    last_conflict_at: datetime | None = None
    checked: int = 0                        # all decisive verdicts ever recorded

    def record(self, verdict: str | None, at: datetime) -> EdgeHealth: ...
    @property
    def conflict_ratio(self) -> float: ...  # share of "c" in recent; 0.0 when empty
    @property
    def status(self) -> HealthStatus: ...
    def as_dict(self) -> dict[str, Any]: ...  # JSON-safe: datetimes isoformat, status value

@dataclass(frozen=True, slots=True)
class HealthBook:
    entries: Mapping[tuple[str, str], EdgeHealth] = field(default_factory=dict)

    def record(self, parent: str, child: str, verdict: str | None, at: datetime) -> HealthBook: ...
    def get(self, parent: str, child: str) -> EdgeHealth: ...    # empty EdgeHealth when absent
    def status(self, parent: str, child: str) -> HealthStatus: ...
    def broken(self) -> list[tuple[str, str]]: ...               # sorted
    def prune(self, keep: Iterable[tuple[str, str]]) -> HealthBook: ...  # drop entries not in keep
    def to_json(self) -> str: ...
    @classmethod
    def from_json(cls, text: str | None) -> HealthBook: ...     # None/""/invalid -> empty book

def demotions(book: HealthBook, edges: Iterable[Any]) -> list[tuple[str, str]]:
    """(edge_id, reason) for each edge that is CONTAINS, lifecycle CONFIRMED,
    declared_by == "inference", not pinned, and whose health is BROKEN.
    reason = "health: <n_conflict>/<n_window> recent steps unmatched".
    Read attributes with getattr: edge_id, relation, lifecycle, declared_by,
    pinned, source_endpoint_id, target_endpoint_id; relation and lifecycle may
    be enums or strings (compare their .value or the string itself to
    "contains" / "confirmed")."""
```

Semantics:
* `record` with verdict `None` returns `self` unchanged (neutral steps are not
  health evidence). `"support"` appends `"s"`, `"conflict"` appends `"c"`,
  any other string raises `ValueError`. Keep only the last `HEALTH_WINDOW`.
  Update the matching `last_*_at` and `checked += 1`.
* `status`: `< HEALTH_MIN_DECISIVE` entries -> UNKNOWN; ratio >
  BROKEN_CONFLICT_RATIO -> BROKEN; ratio > DEGRADED_CONFLICT_RATIO ->
  DEGRADED; else HEALTHY.
* Everything is immutable: `record` returns new objects, the old book is
  unchanged.
* `to_json`/`from_json` round-trip exactly (entries keyed by
  `"<parent>\u0000<child>"` or as a list of objects; your choice, but the
  round trip must be lossless and deterministic: sorted keys).

## Tests (tests/unit/test_topology_health.py), at least

* status transitions: 4 supports -> UNKNOWN; 5 supports -> HEALTHY;
  16 s + 4 c -> HEALTHY (0.20 is not > 0.20); 15 s + 5 c -> DEGRADED;
  window full of 20 s then 11 c -> BROKEN (window rolled: 9 s, 11 c).
* `None` verdict changes nothing; bad verdict raises.
* immutability of the book.
* JSON round trip incl. datetimes with tz; `from_json(None)` and
  `from_json("garbage")` are empty.
* `demotions`: only the confirmed, inferred, unpinned, broken edge is listed
  (use `wattmosaic_engine.contracts.TopologyEdgeV3` objects built like
  `tests/unit/test_topology_history.py::test_a_user_decision_wins_over_the_replay`,
  plus a plain `types.SimpleNamespace` with string values).
* `prune` keeps only the given keys.
