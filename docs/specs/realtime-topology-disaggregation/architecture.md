# Architecture — from timer-driven snapshot solver to event-driven, versioned inference

## 1. Today (verified at `27373aa`, see `analysis/`)

```
HA WebSocket ──► HASource ──► SampleAligner (received_at clock)          ┐
                    │            StepTracker (cycle clock)               │ four buffers,
                    │            SampleCollector snapshots               │ four time bases
                    │            audit histories (state_timestamp)       ┘
                    ▼
         every 5 s: EngineCore.run_inference_once  (on the asyncio loop)
                    │
                    ▼
   WattMosaicBackend.process  ── synchronous, all inline ──────────────────────┐
     DisaggregationEngine.infer: absolute NNLS, lower=0, every device          │
        H from declared pointer tree (parent_ct_id, branch_ct_ids)             │
        ambiguity → member 0.0 placeholder; next cycle → model 250/250 split   │
     LearningPipeline: fit on whole-circuit power, R² gate                     │
     AuditPass: isolation, association, nesting/hierarchy, residual (1 window) │
     reanalysis: up to 6 h of ct_samples                                       │
                    ▼                                                          │
   inference_results rows ──► /snapshot (DB round-trip, lossy) ──► HA sensors (0 W for unknown)
   GET /mapping ──► build_mapping_matrix (full recompute, under writer lock)
```

The structural defects that the handbook cannot tolerate (details in `analysis/`):
- Device identity is the CT (`dev_<ct>`).
- Topology and mapping share one version.
- Provisional structure acts as hard constraints.
- There is no delta model, no unknown competitor on branch rows, and no group estimate.
- Zero is used where a value is unknown.
- Training and audit run on the event loop.
- Nothing about the matrix is persisted.

## 2. Target

```
                     ┌──────────────────── FAST LOOP (asyncio, O(1)/sample) ─────────────────────┐
HA WebSocket ──► P1 normalize (ReaderMeasurement, HAEvent; polarity/multiplier once; quarantine)
                     ──► P2 ReaderStateStore (one ring/reader, observed_at clock, held/stale/missing,
                                              σ_r long-run, frozen-on-suspect baseline)
                     ──► P3 detector (hysteresis + CUSUM → MeasurementEvent with identity)
                     ──► P4 evidence windows (immutable EventEvidence)
                     ──► P5 event groups (context, temporal, co-movement; late → REVISED)
                     └────────────────────────────────────────┬────────────────────────────────┘
                                                              │ ready/revised groups (bounded queue)
                     ┌──────────────────── EVENT LOOP (single executor thread) ──────────────────┐
                     │  D1 VersionService.current() → (topo, map, mset)                          │
                     │  T2 build_observation_operator(T, M, factors@t)    T3 UnknownPrior         │
                     │  I7 infer_group:                                                          │
                     │     I1 candidates (+ unknown per scope) → I2 evidence A–H                 │
                     │     I3 joint robust signed solve (Σ-aware, anchors, ΔU columns)           │
                     │     I4 identifiability on history-stacked experiment matrix, LP bounds     │
                     │     I5 estimates (MEASURED/ESTIMATED/ALLOCATED/UNKNOWN, groups, null)     │
                     │     I6 match lifecycle, EventInferenceRecord (versioned interpretation)   │
                     │  M2 MatrixUpdater.apply (affected cells only, append history)             │
                     │  D4 InferenceSnapshot (persist on change, copy-on-write publish)          │
                     │  steady pass: infer_steady per changed operator component (DEC-14)        │
                     └────────────────────────────────────────┬──────────────────────────────────┘
                                                              │ confirmed evidence / jobs
                     ┌──────────────────── BACKGROUND LOOP (worker thread, ≤2 s chunks) ─────────┐
                     │  L4 observations → L5 incremental fit + promotion → L1 registry swap      │
                     │  T5 topology evidence ledger → edge lifecycle → D1 propose/promote        │
                     │  T7 change detectors → ConfigurationEvent → SUSPECTED_CHANGE workflow     │
                     │  T3 residual priors   T6 unknown profiles   M2 reconcile   L7 replay       │
                     └───────────────────────────────────────────────────────────────────────────┘

HTTP /api/v1 (M3, T4, D2, D4, I6, L1) ──► panel ES modules (U1, U2) ──► HA integration (H1, H2)
```

## 3. Package layout after Wave 0 and the target modules

`engine/wattmosaic_engine/`:

| Package / module | Created by | Owned afterwards by | Purpose |
|---|---|---|---|
| `contracts/__init__.py` (facade) | X01 | orchestrator only | re-exports, so `from wattmosaic_engine.contracts import X` keeps working |
| `contracts/legacy.py` | X01 | Z1 (deletes legacy) | today's models, moved verbatim |
| `contracts/foundation.py` | X01 (empty) | K00 | versions, evidence refs, status enums, bounds, `CONTRACT_VERSION` |
| `contracts/{entities,measurement,topology,inference,matrix,power_models}.py` | X01 (empty) | K01–K06 | §2–§8 of `interfaces.md` |
| `runtime/interfaces.py`, `runtime/fakes.py` | K08 | K08 | cross-area dataclasses and Protocols, plus deterministic fakes |
| `realtime/{normalize,reader_state,detection,windows,grouping}.py` | P1–P5 | P1–P5 | pipeline stages |
| `core/{runtime,rows,snapshot,topology_api,mapping_api,models_api,discovery_api,diagnostics_api}.py` | X03 | P6, D3, D4, T4, M3, L1, D2 | EngineCore split (`main.py` keeps `main()` and composition) |
| `backend/{inference,learning,audit,reanalysis}.py` | X03 | I7/Z2, L6, T5/T6, L7 | `integration.py` split; `integration.py` becomes a facade until Z2 |
| `api/routes/*.py`, `api/protocols.py` | X04 | M3, T4, D2, D4, I6, L1 | per-area route modules |
| `storage/repositories/*.py` | X05 | S13–S19 per aggregate | DAO per aggregate |
| `storage/migrations/{legacy.py, v013_*.py … v025_*.py}` | X05 / S13–S19 / Z1 | per number | auto-discovered and ordered |
| `versioning/service.py` | D1 | D1 | VersionService |
| `electrical/topology_graph.py` (edges) | T1 | T1 | lifecycle-filtered graph |
| `electrical/operator.py` | T2 | T2 | H(T, M, t) |
| `electrical/topology_inference.py` | T5 | T5 | merges `nesting.py` + `hierarchy.py` |
| `electrical/topology_change.py` | T7 | T7 | moves `detect_configuration_changes` out of `topology.py` |
| `residual/{reader_residual,unknown_profiles}.py` | T3, T6 | T3, T6 | |
| `inference/{candidates,topology_filter,temporal,evidence,joint,covariance,identifiability,bounds,estimates,matching,records,orchestrator}.py` | I1–I7 | I1–I7 | |
| `mapping/{cells,columns,rows,projection,store,updater,reconcile,service}.py` | M1–M3 | M1–M3 | replaces `disaggregation/mapping.py` |
| `models/{spec,prediction,fixed,adjustable,dynamic,kernel,components}.py` | L1–L3 | L1–L3 | replaces the binary/dimmable/variable_speed/multi_state/time_varying/composite modules |
| `learning/{observations,incremental,worker,reconciliation,suggestions}.py` | L4–L8 | L4–L8 | |

`addon/ui/` (X06): `index.html` loads `main.js` (`type="module"`). The modules are `core.js`, `view-overview.js`, `view-devices.js`, `view-topology.js`, `view-discover.js`, `view-quality.js`, `view-diagnostics.js`, `view-matrix.js`, `matrix-model.js`, `matrix-cell.js`, `matrix-drill.js`, plus `app.css` and `matrix.css`. Names stay flat (`/ui/{asset}`, one path segment, `addon/gateway.py:575`).

## 4. Dependency direction (extends `docs/architecture.md` §2)

```
main → core → {realtime, inference, mapping, learning, versioning, api}
inference → {electrical(operator), residual, models(registry views), runtime.interfaces, contracts}
mapping   → {electrical(operator), runtime.interfaces, contracts, storage.repositories}
realtime  → {synchronization utils, runtime.interfaces, contracts}
learning  → {models, runtime.interfaces, contracts, storage.repositories}
electrical, residual, models → contracts, runtime.interfaces
storage.repositories → contracts
contracts, runtime.interfaces → (nothing in package except foundation)
```

`inference` never imports `realtime` or `mapping`. It consumes `EventGroup` and produces `JointInferenceResult`, and the runtime (P6) wires them. This is what keeps `infer_group` pure and replayable.

## 5. What is deleted (Z2, Z1)

The following are deleted:
- `DisaggregationEngine.infer` and its 5 s caller.
- `_detect_ambiguity`, `_group_bounds`, the Level-3 per-member model path, proportional prior clipping and the ADR-0026 measured-step greedy credit.
- `StepTracker`, the `SampleCollector._snapshots` buffer and the `SampleAligner` power buffers (replaced by P2).
- `isolation.group_events`.
- `nesting.py` and `hierarchy.py` (merged into T5).
- `disaggregation/mapping.py`, `derive_mapping_matrix`, and the `/mapping`, `/topology/confirm`, `/topology/nesting`, `/relations` and `/confidence_tiers` routes.
- The `CTChannel` pointer fields, `CTChannel.device_id`, `DeviceTopology.branch_ct_ids`, `Source`, `PowerState`, `ConfidenceTier`, `TopologyStatus` (aggregate), `RelationKind` v1, `MappingObservability` v1, `MappingCellStatus` v1, `InferenceResult.power_w: float` and the `power_w_is_placeholder` convention.
- The `electrical/models.py` parameter interpreter.
- The legacy model classes.

Legacy **tables** are renamed `*_legacy` and kept read-only for one release (DEC-02).
