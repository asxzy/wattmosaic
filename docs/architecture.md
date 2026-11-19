# WattMosaic Architecture

**Contract version:** `3.0.0` (mirrors
`engine/wattmosaic_engine/contracts/foundation.py` and the integration's
`custom_components/wattmosaic/const.py`) · **Engine version:** reported at runtime
as `wattmosaic_engine.__version__` (development builds carry `X.Y.Z-dev.N`)

This document describes the shipped 3.0 system. The normative algorithm
reference is the [technical handbook](../docs/specs/realtime-topology-disaggregation/handbook.md)
(HB §n), and the concrete parameters and decision rules are in the
[implementation specification](../docs/specs/realtime-topology-disaggregation/design.md).
Contract 3.0 is a **breaking change**: the engine and the integration must be
upgraded together (see [`RELEASE_NOTES.md`](../RELEASE_NOTES.md)).

WattMosaic is three cooperating pieces with one narrow, versioned boundary
between the Home Assistant side and the inference side.

```text
                       HOME ASSISTANT OS
                              │
         ┌────────────────────┴────────────────────┐
         │                                         │
         ▼                                         ▼
  WattMosaic Integration                  Existing HA Integrations
  (thin REST client, entities)            energy monitors / plugs / devices
         │                                         │
         │  HTTP /api/v1  (X-API-Token)             │  WebSocket state_changed + reader samples
         ▼                                         ▼
  ┌──────────────────────────────────────────────────────────────┐
  │                     WattMosaic Engine                         │
  │                                                              │
  │  ┌──────────── three-loop runtime (core/runtime.py) ───────┐  │
  │  │ fast loop (O(1)/sample)   event loop     background loop│  │
  │  │ reader state, detection  event groups   learning, retain│ │
  │  └──────────────────────────────────────────────────────────┘  │
  │                          │                                    │
  │       typed topology / mapping / model version service        │
  │                          │                                    │
  │       joint delta + absolute inference, groups, residual       │
  │                          │                                    │
  │                      SQLite (WAL)                              │
  │                          │                                    │
  │                      HTTP API v3                               │
  └──────────────────────────────────────────────────────────────┘
```

## 1. Why this split

| Concern | Where it lives | Why |
| --- | --- | --- |
| Reading HA | Engine (`ingestion/`) | Inference needs raw cadence and event timing, not a polled summary |
| Inference/learning | Engine | Must be runnable outside HA (Docker, other host) |
| Entity creation | Integration | Only HA can own entities |
| Long-term storage | Engine SQLite | Independent of HA Recorder retention |

The integration never computes power. It renders what the engine already
decided. This is the single most important architectural rule: it prevents two
divergent implementations of the math.

The reverse direction matters too. The engine never consumes the entities the
integration publishes: a `sensor.<device>_power` value is an output, not an
observation. Ingestion and endpoint discovery both exclude entities whose
registry `platform` is `wattmosaic`, so a prediction can never become its own
input.

### 1.1 The four-entity model (HB §1)

3.0 separates four entity kinds; no table carries a single device FK on a reader
or a single reader FK on a device:

- **Measurement Endpoint** — a CT, smart plug or appliance monitor that produces
  reader samples. An endpoint whose *declared scope* is exactly one Physical
  Device or Load Component (`SINGLE_SUBJECT`, confirmed by the user) is the only
  source of `MEASURED` estimates.
- **Physical Device** — the thing that consumes power. Discovery no longer mints
  one device per CT: unexplained load on an endpoint is a reader residual or an
  Unknown Load Profile, not a fake device.
- **Load Component** — a sub-part of a device (HB §1.1).
- **HA Entity / Link** — HA entities are linked to subjects through
  `ha_entity_links`; mapping edges link subjects to endpoints.

Endpoint configuration is an append-only set of revisions with validity
intervals, so a reading is always interpreted with the revision valid at its
`observed_at`. A calibration or factor change is never applied retroactively.

## 2. Module boundaries

```text
engine/wattmosaic_engine/
├── contracts/          # authoritative v3 schemas (foundation, entities, measurement,
│                       #   topology, inference, matrix, power_models, service)
├── config/             # env loading, validation, redaction
├── storage/            # SQLite, per-version migrations, backup, repositories
├── ingestion/          # HA WebSocket read-only client, normalization, quarantine
├── synchronization/    # per-reader state store, held/stale/missing, baseline, detector
├── electrical/         # typed topology graph, observation operator, solver
├── disaggregation/     # event matching, joint inference, groups, residual, audit
├── models/             # discriminated power-model classes + registry
├── learning/           # delta-evidence observation intake, training, promotion
├── mapping/            # device-by-reader matrix projection and store
├── versioning/         # independent topology/mapping/model version lifecycle
├── core/               # three-loop runtime, executors, APIs for the facade
├── runtime/            # Protocols and fakes (K08) for pure, importable modules
├── api/                # aiohttp v3 API (routes/, facade, server)
├── diagnostics/        # health/diagnostics report
└── main.py             # process lifecycle
```

Dependency direction is strictly downward:

```text
main → api → disaggregation → electrical → contracts
     → learning → models → storage → contracts
     → ingestion → synchronization → quality → contracts
```

`contracts/` imports nothing from the package. `storage` imports only
`contracts`. This keeps the schema stable and testable in isolation.

### 2.1 Where the Home Assistant registry boundary sits

The engine must run outside Home Assistant (§1), so nothing under
`engine/` may read HA's entity or device registry. The integration expands HA
registry ids into resolved *entity ids* at the boundary
(`custom_components/wattmosaic/state_links.py`) and crosses with those ids only.
A link that resolves to nothing is never guessed at; it is reported in the
integration diagnostics. See **ADR-0028** in `docs/decisions/README.md`.

## 3. The three-loop runtime (HB §5)

The old 5-second full-recompute timer is gone. `WM_INFERENCE_INTERVAL_S` is
accepted and ignored (with a startup warning) for one release.

1. **Fast measurement loop.** Per-sample, O(1): normalize the reading, apply the
   endpoint revision in force, update the reader's ring, noise and baseline, and
   run the change detector. Must never starve `/healthz`.
2. **Event inference loop.** Reader steps group into `EventGroup`s; the joint
   delta + absolute solver produces `DeviceEstimate`s with identifiability and
   bounds, group estimates and reader residuals. Late arrivals can revise a
   group.
3. **Background loop.** Learning on matured delta observations, retention,
   matrix reconciliation, and the staleness-only heartbeat. No full solve.

A heartbeat publishes only `stale_sources`/aging; it is distinct from the retired
inference interval and never recomputes device power on a timer.

## 4. Estimates, status and identifiability (HB §8.4–8.5)

Every estimate carries one of four statuses and an independent identifiability
verdict:

- `MEASURED` — one confirmed single-subject endpoint read the subject directly;
  requires method `DIRECT_READING` and exactly one source reader.
- `ESTIMATED` — a joint solve or validated model; published with a point value
  only when `IDENTIFIABLE`.
- `ALLOCATED` — a share of an identified group sum, with an `allocation_rule`.
- `UNKNOWN` — no point value; justified bounds and a reason instead.

A non-identifiable member never gets a number — it gets `None`, justified bounds
and a group (HB §8.4). Model priors are never bounds. The point value is
published to HA only for `MEASURED`, or `ESTIMATED` + `IDENTIFIABLE` (DEC-25);
everything else is `unknown` in HA, leaving honest gaps in history rather than
fabricated 0 W.

Topology, mapping and model versions are independent. A plug move never touches
a power model (AT-15).

## 5. Reconciliation and the mapping matrix (HB §9, §12)

The balance is kept exact and signed: main power equals the sum of attributed
estimates, group sums, the unknown load and the residual. The residual is never
distributed among devices. Each reader also carries its own residual, so
unexplained load on an endpoint is visible without inventing a device.

The device-by-reader **mapping matrix** (`/api/v1/matrix` and drill-down routes)
replaces the removed mapping endpoint. It is sparse, time-dependent, and its
cells carry structured mapping/contribution objects with evidence (HB §9.6).

## 6. Reliability model

| Failure | Behaviour |
| --- | --- |
| HA disconnects | Reconnect with jittered backoff; DB writes continue; entities go stale→unavailable |
| Bad reader sample | Quarantined and counted; never reaches reader state |
| Malformed event | Skipped with a reason; pipeline unaffected |
| One device raises | Per-device isolation; batch continues; error logged |
| Engine restarts | Migrations idempotent; versions/models reloaded; WAL checkpointed |
| DB corrupt | `PRAGMA integrity_check` at startup; refuses to run rather than lose data |
| Event queue full | Drop-oldest with a counted metric; never unbounded memory |

## 7. Security boundary

- Engine → HA: a long-lived token from env; **read-only commands only**. There is
  no service-call code path.
- Integration → engine: optional shared `X-API-Token`, constant-time compare.
- Panel → engine: the App's ingress panel is static files (`addon/ui/`) served by
  the gateway, calling the same `/api/v1` through the gateway; the gateway injects
  the token for ingress peers, and the panel is gated on the trusted-ingress
  check. It is read-only.
- Secrets never logged; `EngineConfig.redacted()` is used everywhere.
- SQLite never stores tokens.

## 8. Deployment shapes

1. **HA OS App** (primary): `addon/` build, ingress on 8787, `/data` volume, and
   a read-only panel served from `addon/ui/` at `/`.
2. **Standalone Docker**: root `Dockerfile`, volume for DB, env from `.env`.
3. **Local dev**: `.venv` + `python -m wattmosaic_engine.main`.

All three share the same image entrypoint and migration path. Backups and
rollback are described in [`docs/deployment.md`](deployment.md).

## 9. Explicit non-goals

- No deep learning. No black-box attribution.
- No control of household devices.
- No automatic commitment of unconfirmed topology or endpoint scope.
- No calibrated probabilities from the confidence vector.
- Read-only toward HA; active learning only *suggests* (CONTRIBUTING.md).
