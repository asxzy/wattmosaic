# Interfaces: the shared contract between tasks

Every name below is fixed. Tasks code against these names in parallel. If a task finds a
name unworkable, it stops and reports. It does not rename.

## 1. Engine setting (T01)

`engine/wattmosaic_engine/config/settings.py`

```python
#: Accepted range for raw CT capture, in days. 0 disables persistence entirely.
RAW_CAPTURE_DAYS_RANGE: tuple[int, int] = (0, 90)

class EngineConfig:
    #: Days of full-rate canonical CT readings kept in ``ct_samples``.
    #: 0 (default) = do not persist readings; the HA recorder is the production
    #: history (ADR-0042). >0 = debug/dev capture with its own horizon.
    raw_capture_days: int = 0
```

- Env var: `WM_RAW_CAPTURE_DAYS` (empty string → default).
- `validate()` adds `"raw_capture_days must be in 0-90"` when out of range.
- `to_dict()` includes `"raw_capture_days"`.
- `retention_days_raw` keeps its field, env var, range and default (7). Its comment now
  says it no longer governs `ct_samples`.

## 2. Storage retention tier (T02)

`engine/wattmosaic_engine/storage/tiers.py`

```python
CAPTURE_HORIZON = "capture"          # new, exported in __all__
RAW_CAPTURE_ENV = "WM_RAW_CAPTURE_DAYS"  # new, exported in __all__
# prunable_tables(): ("ct_samples", "received_at", CAPTURE_HORIZON)
```

`engine/wattmosaic_engine/storage/repositories/aggregates.py` and the
`Database.prune` wrapper in `storage/database.py`:

```python
def prune(self, raw_days: int = ..., aggregate_days: int = ...,
          *, capture_days: int | None = None) -> dict[str, int]: ...
```

| `capture_days` | Effect on `CAPTURE_HORIZON` tables |
|---|---|
| `None` | Pruned on the **raw** cutoff (legacy behaviour; existing callers unchanged) |
| any `int` (incl. 0) | **Skipped**; reported as `removed[table] == 0` (the background purge owns them) |

Keep existing defaults and positional order.

## 3. Batched purge (T03)

New module `engine/wattmosaic_engine/storage/capture.py`:

```python
@dataclass(frozen=True, slots=True)
class PurgeResult:
    deleted: int      # rows deleted in this call
    batches: int      # transactions committed
    done: bool        # True when no eligible row remains

def purge_ct_samples(
    conn: sqlite3.Connection,
    lock: threading.RLock | threading.Lock,
    *,
    cutoff: datetime | None,          # None = delete every row
    batch_rows: int = 5000,
    max_batches: int | None = 20,
    time_budget_s: float | None = 2.0,
    monotonic: Callable[[], float] = time.monotonic,
) -> PurgeResult: ...
```

- Each batch: acquire `lock`, `BEGIN IMMEDIATE`, delete ≤ `batch_rows` rows with the
  lowest `id` whose `received_at < cutoff` (all rows when `cutoff is None`), `COMMIT`,
  release `lock`. Roll back and re-raise on error.
- Stops when a batch deletes fewer than `batch_rows` (then `done=True`), or when
  `max_batches` or `time_budget_s` is reached (`done=False`).
- `cutoff` is compared as an ISO-8601 UTC string, the same encoding the tier prune uses
  (`storage` `iso()` helper).
- Missing table → `PurgeResult(0, 0, True)`; no exception.
- Exported in `__all__`: `PurgeResult`, `purge_ct_samples`.

## 4. Runtime (T08)

`engine/wattmosaic_engine/core/runtime.py`

- Enqueue at `:682` happens only when `self.config.raw_capture_days > 0`.
- `_maintenance_loop` calls
  `self.db.prune(cfg.retention_days_raw, cfg.retention_days_aggregates, capture_days=cfg.raw_capture_days)`
  and `self.submit_background(self._purge_raw_capture)`.
- New method `_purge_raw_capture(self, *, budget: Any | None = None) -> None`:
  `cutoff = None if days == 0 else now − days`, calls `purge_ct_samples(self.db._conn,
  self.db._lock, cutoff=cutoff)`, adds `result.deleted` to the new counter
  `self.raw_capture_purged: int` (initialised to 0 next to `self.samples_persisted = 0`
  at `main.py:277`), logs at INFO when `deleted > 0`, swallows and logs exceptions (degraded,
  never crash).
- `_prune_evidence` drops its `DELETE FROM ct_samples` block.

## 5. Diagnostics (T09)

`core/diagnostics_api.py` `health()` (served at `GET /api/v1/health`): in the
`HealthResponse.details` mapping (additive, schema-free; `diagnostics_api.py:95`) add

```python
"raw_capture": {"days": int(self.config.raw_capture_days),
                "enabled": self.config.raw_capture_days > 0},
```

`diagnostics/report.py` `engine_health` (the fixed key list at `:266`) gains
`"raw_capture": <health details raw_capture or None>`, so the panel receives it as
`report.engine_health.raw_capture`. `addon/ui/view-diagnostics.js` renders one `kv` row
"Raw CT capture" in the Engine card.

T09 must not reference `raw_capture_purged` (T08-owned; avoids a cross-task dependency).
`raw_capture_days` also appears in the redacted Configuration panel automatically once
T01 adds it to `to_dict()`/`redacted()`.

## 6. App option (T06)

`addon/config.yaml`: `options.raw_capture_days: 0`, `schema.raw_capture_days: "int(0,90)"`,
header comment line `#   raw_capture_days       -> WM_RAW_CAPTURE_DAYS`.
`addon/run.sh` mapping line: `raw_capture_days|WM_RAW_CAPTURE_DAYS|0`.

## 7. Tools (T04, T05)

`tools/audit/replay_capture.py`:

```python
def load_capture_ha_history(
    payload: Mapping[str, Sequence[Mapping[str, Any]]],
    *, entity_to_ct: Mapping[str, str], start: datetime, end: datetime,
) -> Capture: ...           # pure, no network

async def fetch_ha_history_payload(
    client: HAClient, entity_ids: Sequence[str], *, start: datetime, end: datetime,
    chunk: timedelta = timedelta(hours=1),
) -> dict[str, list[dict[str, Any]]]: ...   # merges chunks per entity
```

CLI: `--ha-history` (flag), `--end ISO8601` (default now; only with `--ha-history`).
`--ha-history` is mutually exclusive with `--jsonl`. Wiring/versions still come from
`--db` or `DEFAULT_DB`.

`tools/audit/check_recorder_coverage.py`:

```python
@dataclass(frozen=True, slots=True)
class EntityCoverage:
    entity_id: str
    rows: int
    first: datetime | None
    last: datetime | None
    max_gap_s: float | None
    status: Literal["ok", "short", "missing"]

def coverage_report(
    payload: Mapping[str, Sequence[Mapping[str, Any]]],
    entity_ids: Sequence[str], *, start: datetime, end: datetime,
    short_tolerance: timedelta = timedelta(hours=1),
) -> list[EntityCoverage]: ...
```

- `missing`: zero rows. `short`: first row later than `start + short_tolerance`.
  `ok` otherwise.
- CLI: `--db PATH` (CT entity ids from the latest `topology_versions` payload) or
  `--entities ID ...`; `--days` (default 10); `--json`. Exit 1 if any `missing`, else 0.

HA compressed history rows: the state is `"s"`; the timestamp is `"lu"` if present, else
`"lc"`, as epoch seconds (float). Follow `tools/audit/derive_state_entities.py:275`.
Non-numeric states (`unavailable`, `unknown`) are skipped and counted in a note.
