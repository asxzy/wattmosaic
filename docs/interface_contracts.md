# Interface Contracts (authoritative)

**Contract version: `4.1.0`** — mirrors `engine/wattmosaic_engine/contracts/foundation.py` (re-exported by `engine/wattmosaic_engine/contracts/__init__.py`).

The `contracts/` package is the single source of truth. JSON schemas are exported
from it to `data/schemas/` by `tools/dev/export_schemas.py`. No component may
define a competing schema. The complete, normative shapes and handoffs are in the
[specification's `interfaces.md`](specs/realtime-topology-disaggregation/interfaces.md);
algorithm semantics are in the
[technical handbook](specs/realtime-topology-disaggregation/handbook.md) (HB §n).

Contract 3.0 is a **breaking change**: engine and integration update together.
Legacy contract models, the `/mapping` endpoint, v2 forms and the legacy HA
confirm endpoint were removed at the cutover. Legacy tables were archived by
migration v025 and are read-only for one release; no down-migration exists.

## 1. Ownership map

| Module | Owner | Files |
| --- | --- | --- |
| Contracts | Orchestrator | `engine/wattmosaic_engine/contracts/` |
| Config | Orchestrator | `engine/wattmosaic_engine/config/` |
| Storage | Orchestrator | `engine/wattmosaic_engine/storage/` |
| Ingestion / sync / API / quality / diagnostics / main | Agent D | `ingestion/`, `synchronization/`, `api/`, `diagnostics/`, `main.py` |
| Detection / events / topology / inference | Agents B–E | `core/`, `topology/`, `inference/`, `events/` |
| Mapping / matrix | Agent E | `mapping/` |
| Models / learning | Agent F | `models/`, `learning/` |
| Versioning / residual | Agents A, C | `versioning/`, `residual/` |
| HA integration | Agent C | `custom_components/wattmosaic/` |
| Add-on | Agent H | `addon/`, `docker-compose.yml`, `Dockerfile*` |
| Simulator + tests | Agent I | `tools/simulator/`, `tests/` |
| Docs | Agent J | `README.md`, `docs/*.md` (except contracts/architecture) |

## 2. Engine ↔ Home Assistant

### 2.1 Ingestion (engine → HA, read-only)

- WebSocket `ws(s)://<ha>/api/websocket`, `auth` with a long-lived token.
- Commands used: `get_states`, `config/*_registry/list`, `subscribe_events`
  (`state_changed`), non-destructive reads only.
- **No service calls.** The engine has no code path that can actuate a device.
- Reconnect: exponential backoff 1 s → 60 s, jittered, unlimited retries.
- Each observation carries `observed_at`, `received_at` and the endpoint
  calibration revision used to interpret it. Non-finite/out-of-range readings are
  quarantined and counted, never passed to reader state.

### 2.2 Integration → engine (HTTP, `X-API-Token` when configured)

Base path `/api/v1`. Every response carries `X-WattMosaic-Contract-Version: 4.1.0`.
Error shape: `ErrorResponse` with HTTP 4xx/5xx.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/health`, `/status` | health + engine/DB/learning summary |
| GET | `/snapshot` | `InferenceSnapshot` — latest persisted estimates + groups + residual |
| GET | `/devices`, `/devices/{id}` | `DeviceEstimate` + `PhysicalDevice` + components |
| GET | `/devices/{id}/records` | paged event inference records |
| GET | `/groups`, `/groups/{id}` | `DeviceGroupEstimate` |
| GET | `/events`, `/events/{group_id}` | event groups + record revisions |
| GET | `/endpoints`, `PUT /endpoints/{id}`, `POST /endpoints/{id}/scope` | endpoints; confirm/reject single-subject scope |
| GET | `/topology/v3`, `/topology/tree`, `/topology/versions` | active topology, primary tree, history |
| POST | `/topology/edges`, `.../{id}/pin`, `.../{id}/reject` | typed edges + per-edge user authority |
| PUT | `/topology/endpoints/{id}/config` | append-only endpoint configuration revision |
| GET | `/configuration-events` | typed configuration-change events |
| GET | `/matrix`, `/matrix/cells/{row}/{reader}`, `/matrix/readers/{id}`, `/matrix/rows/{id}` | mapping matrix + drill-down |
| GET | `/residuals`, `/unknown-profiles` | per-reader residual points, unknown-load profiles |
| GET | `/models/{subject_id}` | model versions + model events |
| GET | `/provenance/{kind}/{id}` | resolved evidence (HB §18) |
| GET | `/events`, `/runtime-metrics`, `/metrics` | event groups, loop metrics, Prometheus text |

**Removed at contract 3.0:** `/mapping`, `PUT /topology`, `POST /topology/confirm`,
`/topology/nesting`, `/confidence_tiers`, `/relations`. Clients must use the
matrix and the v3 topology/endpoint routes above.

### 2.3 Integration behaviour

- Polls `/snapshot` on `scan_interval` (default 30 s); never computes inference.
- Publishes a device power sensor from `DeviceEstimate.point_w`, applying the
  single HA publication rule: the point is published only for `MEASURED`, or
  `ESTIMATED` + `IDENTIFIABLE`; every other combination publishes `None`
  (`unknown`). `ALLOCATED` shares and non-identifiable members are never
  published as device watts.
- Attributes carry `status`, `identifiability`, `lower_w`, `upper_w`,
  `bounds_basis`, `group_id`, the version triple and `provisional`.
- One group power sensor per stable `DeviceGroupEstimate.group_id`, on a
  synthetic "WattMosaic group" device (DEC-25). Existing entity unique ids are
  unchanged; past 0 W history becomes gaps going forward, not rewritten.
- Marks entities `unavailable` when the engine is unreachable beyond
  `unavailable_after` or the result is older than `stale_after`.

## 3. Engine internal module interfaces

These are the Python call boundaries; they are stable and type-annotated. Full
type signatures and fakes are in the spec's
[`interfaces.md`](specs/realtime-topology-disaggregation/interfaces.md) §3–§8 and
`engine/wattmosaic_engine/runtime/`.

### 3.1 Ingestion (`ingestion/`)

Normalizes HA states and events into `ReaderMeasurement` and `HAEvent`, applying
endpoint revisions exactly once. Events of unlinked entities go to a bounded ring
and are persisted only if grouped into evidence.

### 3.2 Synchronization (`synchronization/`)

`ReaderStateStore` keeps one bounded ring per endpoint with zero-order hold;
noise σ_r and baseline b_r are maintained per reader and persisted so a restart
has no hidden state. A reader transitions held → stale → missing; missing yields
`None`, never 0.

### 3.3 Topology (`topology/`)

A typed graph built from mapping edges filtered by lifecycle. `LifecycleStatus`
places edges at CANDIDATE / PROVISIONAL / CONFIRMED / REJECTED; one event never
promotes an edge past CANDIDATE. The observation operator
`H(T, M, t)` applies time-scoped factors. PROVISIONAL edges are soft.

### 3.4 Inference (`inference/`)

Candidate generation, temporal/response matching (evidence A–H), a joint robust
signed solver with unknown-load columns, identifiability, and estimate assembly.
Output: `DeviceEstimate` (status + identifiability + justified `Bounds` +
`ConfidenceVectorV3` + evidence refs + version triple), `DeviceGroupEstimate`,
and reader residuals. A non-identifiable member has `point_w = None`.

### 3.5 Mapping (`mapping/`)

`MappingCell` v3 with column/row trees, drill-down payloads and an event-driven
updater/deterministic reconciler. The ETag of `/matrix` is the
`mapping_version_id`; topology, mapping and model versions are independent.

### 3.6 Models and learning (`models/`, `learning/`)

A discriminated spec union (`FIXED`, `ADJUSTABLE`, `DYNAMIC`, `RESPONSE_KERNEL`,
`COMPONENT_SUM`) with append-only versions and observable predictions. Learning
observations are delta-basis; an `ABSOLUTE_ANCHOR` basis is used only for a
confirmed single-subject endpoint. Legacy whole-circuit models are flagged
`LEGACY_ABSOLUTE`, read-only, and retrained from delta evidence (DEC-31).

## 4. Error and null semantics

- Never raise from one device's inference into the loop; isolate per device.
- Every estimate's point value is `float | None`. Missing is `None`, never 0
  (HB §18); a null-policy reflective test enforces this across all contracts.
- Bounds are justified (`BoundsBasis`); model priors are never bounds.
- Confidence components are engineering scores in `[0, 1]` or `None`, explicitly
  **not** probabilities; there is no blended scalar.

## 5. Change protocol

1. Bump `CONTRACT_VERSION` in `contracts/foundation.py` and the integration's
   mirror `custom_components/wattmosaic/const.py` together (Z1).
2. Regenerate `data/schemas/`.
3. Update `docs/interface_contracts.md` and the spec it mirrors.
4. Run the contract tests and `tools/dev/check_release.py`.
5. Run `tools/dev/verify.py` (orchestrator-owned) — it gates spec status, schema
   sync and the Z1 no-legacy-symbol check.

Steps 1–3 are the contract change; steps 4–5 prove that both halves of the
system and the specification stay in lockstep.
