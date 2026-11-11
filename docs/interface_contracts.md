# Interface Contracts (authoritative)

**Contract version: `1.3.0`** — mirrors `engine/wattmosaic_engine/contracts.py`.

`contracts.py` is the single source of truth. JSON schemas are exported from it to
`data/schemas/` by `tools/dev/export_schemas.py`. No component may define a
competing schema.

## 1. Ownership map

| Module | Owner | Files |
| --- | --- | --- |
| Contracts | Orchestrator | `engine/wattmosaic_engine/contracts.py` |
| Config | Orchestrator | `engine/wattmosaic_engine/config/` |
| Storage | Orchestrator | `engine/wattmosaic_engine/storage/` |
| Ingestion / sync / API / quality / diagnostics / main | Agent D | `ingestion/`, `synchronization/`, `api/`, `quality/`, `diagnostics/`, `main.py` |
| Electrical model + inference | Agent E | `electrical/`, `disaggregation/` |
| Learning + models + aggregation | Agent F | `learning/`, `models/`, `aggregation/` |
| HA integration | Agent C | `custom_components/wattmosaic/` |
| Add-on | Agent H | `addon/`, `docker-compose.yml`, `Dockerfile*` |
| Simulator + tests | Agent I | `tools/simulator/`, `tests/` |
| Docs | Agent J | `README.md`, `docs/*.md` (except contracts/architecture) |

## 2. Engine ↔ Home Assistant

### 2.1 Ingestion (engine → HA, read-only)

- WebSocket `ws(s)://<ha>/api/websocket`, `auth` with a long-lived token.
- Commands used: `get_states`, `config/*_registry/list`, `subscribe_events`
  (`state_changed`), `render_template` (only for non-destructive reads).
- **No service calls.** The engine has no code path that can actuate a device.
- Reconnect: exponential backoff 1 s → 60 s, jittered, unlimited retries.
- Each observed state carries `state_timestamp`, `received_at`, and a
  monotonically increasing `sequence`.

### 2.2 Integration → engine (HTTP, `X-API-Token` when configured)

Base path `/api/v1`.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/health` | `HealthResponse` |
| GET | `/status` | engine + DB + learning summary |
| GET | `/snapshot` | `SnapshotResponse` (all latest results + reconciliation) |
| GET | `/devices` | device list with latest result + model state |
| GET | `/devices/{device_id}` | one device detail |
| GET | `/topology` | current `Topology` |
| PUT | `/topology` | replace topology (candidate by default) |
| POST | `/topology/confirm` | confirm candidate topology |
| GET | `/quality` | `DataQualityReport` |
| GET | `/diagnostics` | `run_diagnostics` payload |
| GET | `/models/{device_id}` | versions + active model |
| POST | `/commands` | `ServiceCommand` → `ServiceResult` |
| GET | `/export/{device_id}` | device data export (JSON) |
| GET | `/metrics` | Prometheus-style text metrics |

Error shape: `ErrorResponse` with HTTP 4xx/5xx. All responses carry
`X-WattMosaic-Contract-Version`.

### 2.3 Integration behaviour

- Polls `/snapshot` every `scan_interval` (default 30 s) and `/health` every 30 s.
- Publishes HA entities from the snapshot; never computes inference.
- Marks entities `unavailable` when the engine is unreachable beyond
  `unavailable_after` (default 120 s) **or** when the result is older than
  `stale_after`.
- Restores last known energy totals from HA state restoration.

## 3. Engine internal module interfaces

These are the Python call boundaries; they are stable and type-annotated.

### 3.1 Ingestion (`ingestion/`)

```python
class HASource:
    async def start(self, on_sample: Callable[[Measurement], Awaitable[None]] | None = None) -> None: ...
    async def stop(self) -> None: ...
    async def state_snapshot(self) -> list[dict]: ...
    async def registries(self) -> dict: ...
    @property
    def connected(self) -> bool: ...
    @property
    def last_event_at(self) -> datetime | None: ...
```

Emitted events are normalized into `Measurement` and `DeviceEvent`.

### 3.2 Synchronization (`synchronization/`)

```python
class SampleAligner:
    def add(self, measurement: Measurement) -> None: ...
    def window(self, at: datetime, channels: list[str], max_age_s: float) -> AlignedWindow: ...
    def channel_health(self) -> dict[str, ChannelHealth]: ...
```

`AlignedWindow` exposes per-channel value, age, quality, and whether the value is
a *repeat* (must not be counted as a fresh measurement).

### 3.3 Electrical model (`electrical/`)

```python
class ElectricalModel:
    def __init__(self, topology: Topology) -> None: ...
    def build_matrix(self, device_ids: list[str]) -> MeasurementMatrix: ...
    def constraints(self) -> list[LinearConstraint]: ...
    def main_power_w(self, readings: Mapping[str, float]) -> MainPower: ...
    def validate(self) -> list[str]: ...
```

`MeasurementMatrix` carries `A` (m × n), row channel ids, column device ids, and
per-row uncertainty. Nested CTs must not both appear as independent rows for the
same physical quantity unless explicitly modelled.

### 3.4 Disaggregation (`disaggregation/`)

```python
class DisaggregationEngine:
    def infer(
        self,
        window: AlignedWindow,
        *,
        device_states: Mapping[str, DeviceState],
        models: ModelRegistry,
        topology: Topology,
    ) -> InferenceBatch: ...

@dataclass
class InferenceBatch:
    results: list[InferenceResult]
    reconciliation: Reconciliation
    diagnostics: dict[str, Any]
```

Guarantees:
- `direct_measured` devices use their dedicated CT.
- `unknown` sources carry `power_w` for residual accounting only in
  `Reconciliation.unknown_residual_w`, never as a fabricated device value.
- Sum of attributed power is never forced to equal main power.
- `Reconciliation.topology_confirmed` (contract 1.3.0) states whether the
  topology behind the balance is user-confirmed or still a `candidate`
  hypothesis. It is additive; older readers ignore it.

### 3.5 Learning (`learning/`)

```python
class LearningPipeline:
    def consider(self, events: Sequence[DeviceEvent], window: AlignedWindow) -> list[LearningSample]: ...
    def train(self, device_id: str, samples: Sequence[LearningSample]) -> DeviceModelVersion: ...
    def validate_and_promote(self, candidate: DeviceModelVersion) -> PromotionDecision: ...

class ModelRegistry:
    def active(self, device_id: str) -> DeviceModelVersion | None: ...
    def estimate(self, device_id: str, context: ModelContext) -> ModelEstimate | None: ...
```

### 3.6 Aggregation (`aggregation/`)

```python
class EnergyAggregator:
    def add_result(self, result: InferenceResult, at: datetime) -> None: ...
    def flush(self, now: datetime) -> list[EnergyBucket]: ...
```

Energy is integrated with trapezoidal rule on actual elapsed time; gaps larger
than `stale_after` do not accrue energy.

## 4. Error and quality semantics

- Never raise from one device's inference into the loop; isolate per device.
- Every sample has a `QualityStatus`. `STALE`/`MISSING` samples are excluded from
  learning and reduce confidence, but are still persisted with provenance.
- Confidence is an engineering score in `[0, 1]`; `confidence_is_calibrated`
  must remain `false` unless a calibration study is documented.

## 5. Change protocol

1. Bump `CONTRACT_VERSION` in `contracts.py`.
2. Regenerate `data/schemas/`.
3. Update this document.
4. Run `pytest tests/` — contract tests fail on mismatch.
