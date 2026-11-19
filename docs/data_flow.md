# Data Flow

This document describes the shipped 3.0 event-driven flow. The authoritative
algorithm reference is the
[technical handbook](../docs/specs/realtime-topology-disaggregation/handbook.md)
(HB §5–§8, §12), with concrete parameters in the
[design spec](../docs/specs/realtime-topology-disaggregation/design.md).

## 1. End-to-end sequence

```text
multi-channel CT monitor / smart plug / appliance monitor
        │  HA state_changed + power/energy sensor states
        ▼
Home Assistant state machine ───────────────► HASource (engine, read-only)
                                                  │
                                    normalize: unit, polarity,
                                    multiplier, time-scoped revision
                                                  │
                                                  ▼
                                   ReaderStateStore (one ring per endpoint)
                                   held / stale / missing, baseline, noise
                                                  │
                                                  ▼
                                   Change Detector (hysteresis + CUSUM)
                                                  │
                                     ReaderDelta / MeasurementEvent
                                                  │
                                                  ▼
                                   Event Windows → Event Groups
                                   (late arrivals may revise a group)
                                                  │
                              ┌───────────────────┴────────────────────┐
                              ▼                                        ▼
                    topology-aware candidates             unknown-load competitor
                              │                                        │
                              └──────────────► joint solver ◄───────────┘
                                              (delta + absolute,
                                               covariance-aware, robust)
                                                  │
                              ┌───────────────────┴────────────────────┐
                              ▼                                        ▼
                      DeviceEstimate(s)                    DeviceGroupEstimate(s)
                      status + identifiability             identified group sums
                      + justified bounds                    (no fabricated members)
                              │                                        │
                              └───────────────────┬────────────────────┘
                                                  ▼
                                   per-reader residual + unknown load
                                                  ▼
                                   versioned SQLite (topology/mapping/models)
                                                  ▼
                          HTTP GET /api/v1/snapshot, /matrix, /groups
                                                  ▼
                               WattMosaic HA Integration → entities & groups
```

## 2. Timestamp semantics

Three distinct clocks per observation. Conflating them is a correctness bug
(HB §2, §6.2).

| Field | Meaning | Source |
| --- | --- | --- |
| `observed_at` | Hardware/HA sample time | HA `last_updated` or the monitor's own stamp |
| `received_at` | When the engine actually observed it | Engine monotonic + wall clock |
| `calibration_revision` | The endpoint revision used to interpret the reading | Append-only endpoint config |

Consequences:

- `age = engine_now - observed_at` is the honest staleness measure; a reader is
  *held* until `stale_after_s`, *stale* until *missing_after_s*, then *missing*
  (`None` everywhere — never 0 W).
- A burst of many entities within milliseconds is one poll cycle, not many
  independent measurements.
- A calibration or factor change applies only to readings observed after it; it
  is never applied retroactively.

## 3. Ordering, identity and idempotency

- Each reader sample is keyed in the ring by `observed_at`; out-of-order samples
  are inserted in order and fire a late-arrival callback.
- An event identity is derived from `(reader_id, onset bounds, detector version)`,
  so a physical step yields one event, not one per cycle.
- Event groups can be revised by late arrivals; revisions are append-only and
  carry a strictly increasing revision number.
- Health/heartbeat loops publish staleness only; they never run a full solve and
  so cannot reorder inference history.

## 4. Retention flow (DEC-30)

```text
L1 raw reader samples (30 d default)
   └─► event evidence / groups (long-term, explicit operator bound)
          └─► derived estimates, group estimates, residuals, profiles
                 └─► version history (independent topology/mapping/model lifecycles)
```

Raw data needed for model retraining is preserved for the raw window; derived
evidence outlives it so any historical estimate can still be explained. Retention
never prunes the versioned evidence families.

## 5. Failure/backpressure

```text
HA disconnected ──► reconnect loop (jittered backoff)
                 └─► readers age to stale/missing; quality degrades
                     └─► integration marks entities unavailable after threshold

Event-group queue full ──► drop-oldest, increment groups_dropped
                          └─► system_event row (rate-limited)

Migration fails mid-apply ──► user_version unchanged
                             └─► restore the automatic pre-migration backup
```

No path blocks the event loop indefinitely: every network call has a timeout and
the fast loop is O(1) per sample so bursts cannot starve `/healthz`.
