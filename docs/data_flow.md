# Data Flow

## 1. End-to-end sequence

```text
multi-channel CT energy monitor (panel A, panel B)
        │  integration poll
        ▼
Home Assistant state machine ──── state_changed ────┐
        │                                            │
        │ (integration also polls engine)            ▼
        │                                    HASource (engine)
        │                                            │
        │                              Measurement / DeviceEvent
        │                                            │
        │                                            ▼
        │                                   SampleAligner
        │                              (bounded ring buffers)
        │                                            │
        │                                     AlignedWindow
        │                                            │
        │                              ┌─────────────┴─────────────┐
        │                              ▼                           ▼
        │                     QualityEngine              DisaggregationEngine
        │                              │                           │
        │                              │                   ┌───────┴────────┐
        │                              │                   ▼                ▼
        │                              │             ElectricalModel   ModelRegistry
        │                              │                   │                │
        │                              │                   └───────┬────────┘
        │                              │                           ▼
        │                              │                    InferenceBatch
        │                              │                    (results + reconciliation)
        │                              │                           │
        │                              │              ┌────────────┴───────────┐
        │                              │              ▼                        ▼
        │                              │        LearningPipeline         EnergyAggregator
        │                              │              │                        │
        │                              │              ▼                        ▼
        │                              │        model versions          energy buckets
        │                              │              │                        │
        │                              └──────────────┴────────────┬───────────┘
        │                                                          ▼
        │                                                    SQLite (WAL)
        │                                                          │
        └────────────────────────── HTTP GET /api/v1/snapshot ◄────┘
                                            │
                                            ▼
                              WattMosaic HA Integration
                                            │
                                            ▼
                                    HA entities (sensors)
```

## 2. Timestamp semantics

Three distinct clocks per observation. Conflating them is a correctness bug.

| Field | Meaning | Source |
| --- | --- | --- |
| `state_timestamp` | HA `last_updated` for the state object | HA state machine |
| `received_at` | When the engine actually observed it | Engine monotonic + wall clock |
| `physical_timestamp` | Hardware sample time, if the integration exposes one | Usually `null` for polling CT integrations |

Consequences:

- `age = engine_now - state_timestamp` is the honest staleness measure.
- A burst of many entities within milliseconds is one poll cycle, not many
  independent measurements.
- Repeated identical values (common at 15.7 s cadence) are **not** new samples.
  They update "current value" but not "evidence count".

## 3. Ordering and idempotency

- Every measurement gets a monotonically increasing `sequence` per process.
- Device events carry `event_id` derived from
  `sha1(entity_id + last_updated + new_state)` so an HA reconnect that re-sends
  an event does not duplicate it (`INSERT OR IGNORE`).
- Inference results are append-only with a timestamp index.

## 4. Retention flow

```text
raw samples (30 d default)
   └─► 5-minute power/energy aggregates
          └─► 1-hour aggregates
                 └─► retained 730 d default
```

Raw data needed for model retraining is preserved for the raw-retention window;
aggregates preserve long-term energy accounting beyond it.

## 5. Failure/backpressure

```text
HA disconnected ──► reconnect loop (1s→60s jittered)
                 └─► current values age; quality → STALE
                     └─► integration marks entities unavailable after threshold

Queue full ──────► drop-oldest, increment dropped metric
                 └─► system_events row (rate-limited)
```

No path blocks the event loop indefinitely; every network call has a timeout.
