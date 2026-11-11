# WattMosaic Architecture

**Contract version:** 1.4.0 · **Engine version:** reported at runtime as
`wattmosaic_engine.__version__` (development builds carry `X.Y.Z-dev.N`)

WattMosaic is three cooperating pieces with one narrow, versioned boundary
between the Home Assistant side and the inference side.

```text
                      HOME ASSISTANT OS
                             │
        ┌────────────────────┴────────────────────┐
        │                                         │
        ▼                                         ▼
 WattMosaic Integration                  Existing HA Integrations
 (thin REST client, entities)            energy monitors ×2 / devices
        │                                         │
        │  HTTP /api/v1  (X-API-Token)             │  WebSocket state_changed
        ▼                                         ▼
 ┌──────────────────────────────────────────────────────────────┐
 │                     WattMosaic Engine                         │
 │                                                              │
 │  Ingestion ──► Synchronization ──► Quality ──► Disaggregation│
 │      │              │                              │         │
 │      │              └──────► Learning ◄───────────┤         │
 │      │                          │                 │         │
 │      │                     Model Registry          │         │
 │      │                          │                 │         │
 │      └──────────────► Aggregation (energy) ◄──────┘         │
 │                              │                               │
 │                        SQLite (WAL)                          │
 │                              │                               │
 │                          HTTP API                            │
 └──────────────────────────────────────────────────────────────┘
```

## 1. Why this split

| Concern | Where it lives | Why |
| --- | --- | --- |
| Reading HA | Engine (`ingestion/`) | Inference needs raw cadence, not a polled summary |
| Inference/learning | Engine | Must be runnable outside HA (Docker, other host) |
| Entity creation | Integration | Only HA can own entities |
| Long-term storage | Engine SQLite | Independent of HA Recorder retention |

The integration never computes power. It renders what the engine already
decided. This is the single most important architectural rule: it prevents two
divergent implementations of the math.

The reverse direction matters too. The engine never consumes the entities the
integration publishes: a `sensor.<device>_estimated_power` value is an output,
not an observation. Ingestion and topology discovery both exclude entities whose
registry `platform` is `wattmosaic` (with a `sensor.wattmosaic_` id fallback for
the engine's global sensors), so a prediction can never become its own input.

## 2. Module boundaries

```text
engine/wattmosaic_engine/
├── contracts.py        # authoritative schemas (depends only on __version__)
├── config/             # env loading, validation, redaction
├── storage/            # SQLite, migrations, repositories
├── ingestion/          # HA WebSocket read-only client
├── synchronization/    # per-channel buffers, alignment, staleness
├── quality/            # data-quality scoring
├── electrical/         # topology graph, measurement matrix, NNLS
├── disaggregation/     # inference orchestration, residual, confidence
├── models/             # device model classes + registry
├── learning/           # sample collection, filtering, training, promotion
├── aggregation/        # energy integration, buckets, runs
├── api/                # aiohttp v1 API
├── diagnostics/        # health/diagnostics report
└── main.py             # process lifecycle
```

Dependency direction is strictly downward:

```text
main → api → disaggregation → electrical → contracts
     → learning → models → storage → contracts
     → ingestion → synchronization → quality → contracts
```

`contracts.py` imports nothing from the package. `storage` imports only
`contracts`. This keeps the schema stable and testable in isolation.

### 2.1 Where the Home Assistant registry boundary sits

The engine must run outside Home Assistant (§1), so nothing under
`engine/` may read HA's entity or device registry. A `device_links` entry
stores an HA **device-registry id**, which is not an entity id and means nothing
to the engine. The integration expands it — `custom_components/wattmosaic/state_links.py`,
a pure module that takes the registry as plain rows — and crosses the boundary
with resolved *entity ids* only, written into `DeviceTopology.metadata["link_state_entity_ids"]`.

The engine side only reads that declaration:
`engine/wattmosaic_engine/device_links.py` (`runtime_state_entities`,
`state_entity_for_device`). A link that resolves to nothing is never guessed at;
it is reported as `devices_without_state_entity` in the integration diagnostics.
See **ADR-0028** in `docs/decisions/README.md` (and ADR-0011 for
`config_load_entity_ids`, which is the user-declared sibling of this list).

## 3. Data flow (one inference cycle)

1. **Ingest.** `HASource` receives `state_changed`. Power entities become
   `Measurement`; state-bearing entities become `DeviceEvent`.
2. **Deduplicate.** If the numeric value equals the previous value, the
   measurement is stored with `repeated=True`. It is *still* the current value
   but is *not* evidence of a new physical sample.
3. **Buffer.** `SampleAligner` keeps a bounded ring buffer per CT.
4. **Align.** At inference time, produce a window: for each channel the freshest
   reading within `max_age_s`, with `age_seconds` and quality.
5. **Build matrix.** `ElectricalModel` produces `y = A x` respecting nesting,
   phases and double-pole pairing.
6. **Solve.** Constrained NNLS gives device powers for identifiable devices;
   unidentifiable ones are marked ambiguous rather than guessed.
7. **Reconcile.** Compute direct/inferred/model/residual. Residual is reported,
   never distributed.
8. **Learn.** Events inside the window become learning-sample candidates;
   rejected ones retain a reason code.
9. **Persist.** Results, samples, reconciliation, aggregates.
10. **Serve.** Integration polls `/snapshot`; entities update.

## 4. Electrical model summary

For measurement vector `y` (CT powers) and device vector `x`:

```text
y(t) = A(t) x(t) + e(t)
```

`A` encodes topology. A device under branch CT *B* contributes to row *B*. A
device under main *M* contributes to row *M*. **A device is never represented
twice for the same physical quantity.** When a branch CT is nested under a main
CT, the branch is modelled as a sub-quantity, so the main row carries the branch
aggregate plus the non-branch remainder rather than double counting.

Details and worked examples: `docs/electrical_model.md`.

## 5. Reliability model

| Failure | Behaviour |
| --- | --- |
| HA disconnects | Reconnect with jittered backoff; DB writes continue; entities go stale→unavailable |
| One device raises | Per-device isolation; batch continues; error logged to `system_events` |
| Malformed sample | Skipped with a quality event; pipeline unaffected |
| Engine restarts | Migrations idempotent; topology/models reloaded; last energy bucket recomputed |
| DB corrupt | `PRAGMA integrity_check` at startup; refuses to run on corruption rather than silently losing data |
| Queue full | Drop-oldest with a counted metric; never unbounded memory |

## 6. Security boundary

- Engine → HA: a long-lived token from env; **read-only commands only**. There is
  no service-call code path.
- Integration → engine: optional shared `X-API-Token`, constant-time compare.
- Panel → engine: the App's ingress panel is static files (`addon/ui/`) served by
  the gateway, and it calls the same `/api/v1` through the gateway. The gateway
  injects the token for ingress peers, so the browser never receives it. The
  panel is gated on the same trusted-ingress check as the proxied API, and it is
  read-only apart from confirming an already-inferred topology.
- Secrets never logged; `EngineConfig.redacted()` is used everywhere.
- SQLite never stores tokens.

## 7. Deployment shapes

1. **HA OS App** (primary): `addon/` build, ingress on 8787, `/data` volume, and
   a read-only panel served from `addon/ui/` at `/`.
2. **Standalone Docker**: root `Dockerfile`, volume for DB, env from `.env`.
3. **Local dev**: `.venv` + `python -m wattmosaic_engine.main`.

All three share the same image entrypoint and migration path.

## 8. Explicit non-goals (v0.1)

- No deep learning. No black-box attribution.
- No control of household devices.
- No automatic commitment of unconfirmed topology.
- No claiming calibrated probabilities from the confidence score.
