# WattMosaic — System Assessment and Gap Analysis

**Scope:** `engine/wattmosaic_engine/`, `custom_components/wattmosaic/`, `tools/simulator/`,
`tests/`, `addon/`, deployment manifests. Contract `CONTRACT_VERSION = "1.5.0"`
(`engine/wattmosaic_engine/contracts.py:31`), schema `LATEST_VERSION = 8`
(`storage/migrations.py:473`), package `0.2.1-dev.24` (`pyproject.toml:7`).

**Method:** every statement below is checked against source, not against the prose in `docs/`.
File and line references are to the current worktree. Abbreviated references are paths under
`engine/wattmosaic_engine/` — `main.py` means `engine/wattmosaic_engine/main.py`,
`disaggregation/engine.py` means `engine/wattmosaic_engine/disaggregation/engine.py` — while
`custom_components/…`, `tools/…`, `tests/…` and `addon/…` are relative to the repository root.
Where a behaviour could not be established from code, the claim is marked **unverified** rather
than inferred.

**Verification run:** `python -m pytest tests -p no:cacheprovider` → exit 0,
`2168 passed, 9 skipped` in 127 s; 2177 collected by `pytest --collect-only`, 0 failures. All nine
skips are the gates in `tests/simulation/test_real_topology.py`, which require a live capture:
they skip here because `data/wattmosaic.db` is git-ignored and absent
(`tests/simulation/test_real_topology.py:57-66`), so "all green" excludes the only tests that touch
a real installation.

---

## Executive summary

- The engine is **structurally sound**: one process, one writer, bounded queues, supervised
  loops, read-only HA client, additive versioned contract, 8 forward-only migrations, 2177
  collected tests (2168 pass, 9 real-capture gates skip).
  It is not a prototype (`main.py:578`, `main.py:2907`, `storage/migrations.py:117-471`).
- **Evidence separation is the system's core strength**: `Source` (how a watt value was obtained),
  `ConfidenceTier` 0–4 (what kind of evidence), `MonitoringLevel` (how closely a device is
  observed) are three deliberate, non-collapsed axes (`contracts.py:62`, `:151`, `:226`).
- **The Refoss ×2 factor is a scalar, not a concept.** `CTChannel.measurement_multiplier`
  (`contracts.py:344`) is a user-declared float applied once on read
  (`electrical/topology.py:615-640`); it has no provenance, no validity interval, no record of
  monitor-vs-user application, and never reaches a result. A tree-wide `grep factor` finds only
  unrelated maths (`electrical/models.py:332` upper-bound factor, `ha_source.py:214` reconnect
  jitter, `group_fit.py:153-733` NNLS factorisation) plus `default_factory`.
- **There are no isolation statuses.** No `ISOLATED` / `RESOLVABLE` / `CONTAMINATED` /
  `INSUFFICIENT_EVIDENCE` symbol exists anywhere in the codebase. The nearest thing is a numeric
  component, `event_isolation_score` → 1.0 / 0.60 / 0.45 / 0.15
  (`disaggregation/confidence.py:202-220`), inside a weighted blend that the requirement
  explicitly rules out.
- **The concurrent-event guard in the learning path is dead in production.** `SampleEvidence`
  is never constructed outside tests; `LearningPipeline.consider` calls
  `self.filter.evaluate(sample)` with no evidence (`learning/pipeline.py:72`), so
  `concurrent_events` is always `0` (`learning/sample_filter.py:307`) and
  `SampleRejection.CONCURRENT_EVENTS` can never fire. `concurrency_window_s`
  (`learning/thresholds.py:43`) is referenced **nowhere** outside its definition.
- **Device↔CT association is topology metadata, not a persisted relationship.** It lives in three
  unrelated places — `CTChannel.device_id` (`contracts.py:328`), `DeviceTopology.dedicated_ct_ids`
  / `branch_ct_ids` (`contracts.py:400-401`), `DeviceConfiguration.monitored_by_ct_ids`
  (`contracts.py:587`) — with no precedence rule, no evidence field, and no validity interval.
- **Residual decomposition is honest but shallow.** `unmonitored_w + meter_disagreement_w ==
  unknown_residual_w` holds exactly (`disaggregation/engine.py:2177`), and nothing is
  redistributed (`contracts.py:827`). But there is no baseline-vs-dynamic split, no residual
  time series, no "unresolved since", and `insert_reconciliation` writes one row per 5 s cycle
  that only `latest_reconciliation()` ever reads (`storage/database.py:577`, `:598`).
- **Inference results are append-only rows, not versions.** `inference_results` has no
  algorithm version, no previous/new pair, no delta, no reason code
  (`storage/migrations.py:221-239`); `provenance_json` is a free-form dict that happens to hold
  the confidence breakdown (`disaggregation/engine.py:1578`).
- **Retention deletes the audit trail.** `prune()` deletes `inference_results` and
  `reconciliation_results` rows older than `retention_days_raw` (default 30)
  (`storage/database.py:826-833`, `addon/config.yaml:118`). Any versioned-inference or
  residual-history requirement must either exempt these tables or accept 30-day history.
- **The device history read path is broken.** `EngineCore._device_history` queries
  `since=utcnow()` (`main.py:1789`) against `timestamp >= ?` (`storage/database.py:570`), so
  `/api/v1/devices/{id}` `history` is **always empty**. Confirmed by direct execution against a
  temporary database: one row stored, `since=utcnow()` returns 0.
- **CT nesting is proposed, never invented, and only for feeds.** `infer_nesting` proposes only
  `main` children ("a load's level is not measurable", `electrical/nesting.py:604-607`), marks
  `requires_confirmation=True` (`:185`), and is read-only (`main.py:1877-2026`).
- **Circuit classification exists but is conflated.** `MonitoringLevel` describes a *device's*
  visibility, `CTType` describes a *sensor*, and neither describes a *circuit's* electrical scope;
  there is no `PARTIAL`/`PARTIALLY_UNDERSTOOD` state and no per-circuit confidence
  (`contracts.py:43`, `:151`; `disaggregation/configuration.py:158-198`).
- **The simulator is the right shape but the wrong content** for the new requirements: 36
  channels / 39 devices (measured), 2 panels, nested sub-panel, standby and unknown loads,
  and accuracy is already reported per device *class* (`tests/simulation/metrics.py:288`,
  `tests/simulation/test_accuracy.py:118-128`). But there are zero factor-adjusted channels, no
  factor change over time, and inrush is a hard-coded 2 s, 1.25× ramp
  (`tools/simulator/behavior.py:120-124`) rather than a device property.
- **Operationally the system is production-shaped**: JSON logs by default (`main.py:164`), token-
  protected API with contract header (`api/server.py:58-59`), unauthenticated `/healthz` for the
  Supervisor watchdog (`addon/gateway.py:398`, `addon/config.yaml:95`), read-only container,
  dropped capabilities, cold backups (`docker-compose.yml`, `addon/config.yaml:105`).
- **The ranked gaps are all additive.** Nothing in the list below requires rewriting the solver,
  the topology graph, the tier system or the storage layer; each is a new field, table or
  consumer of data the engine already computes.

---

## 1. Architecture

| Concern | Implementation | Evidence |
| --- | --- | --- |
| Process model | Single asyncio process; one `EngineCore`, one SQLite `Database` writer | `main.py:578`, `main.py:2974-3027` |
| Concurrency | Five supervised tasks (`ha_source`, `writer`, `quality`, `inference`, `maintenance`); `asyncio.Task` per component, restart with 1–60 s backoff | `main.py:3022-3026`, `:2907-2965` |
| Ingest path | `on_sample` → aligner + bounded persist queue; `on_event` → link check → state/deque | `main.py:907-1006` |
| Inference cadence | `_inference_loop` every `max(1.0, inference_interval_s)`, default 5 s | `main.py:1226-1239`, `config/settings.py:86` |
| Persistence | `persist_queue` (`BoundedQueue`, drop-oldest with counters) drained by `_writer_loop` | `main.py:601`, `main.py:1009-1030`, `ingestion/queue.py:18-113` |
| Backend seam | `WattMosaicBackend.process()` → `DisaggregationEngine.infer()` → `BackendBatch` | `integration.py:76`, `:276-331` |
| Topology source | Discovery proposes; user confirms; DB is the source of truth; cache reloaded on write | `ingestion/discovery.py:208`, `main.py:1805-1838`, `storage/database.py:207-300` |
| Read models | `panel_tree` (read-time hierarchy), `nesting_candidates` (read-only inference) | `electrical/panel_tree.py:1148`, `main.py:1840-1875`, `main.py:1877-2026` |
| Failure isolation | Per-device `try/except` inside the batch: one device raises → `_fallback_result`, the rest continue | `disaggregation/engine.py:699-731` |
| Module boundaries | `ingestion` → `electrical` → `disaggregation` → `learning`/`models` → `aggregation`; contracts shared | directory layout, `contracts.py` imports |

Assessment: the layering is correct and worth preserving. The one structural weakness is that
**the read models are computed at request time from primitives that are not persisted**
(`panel_tree`, `nesting_candidates`, `_device_history`), so there is no artifact that can be
compared across time. That is the common root of the "versioned inference", "association
history" and "residual history" gaps in §11.

---

## 2. Home Assistant integration and API access

| Concern | Implementation | Evidence |
| --- | --- | --- |
| Transport | One `websockets` client; `subscribe_events` on the prime path + four registry queries | `ingestion/ha_source.py:337-338`, `:824-830`, `:889-900` |
| Read-only guarantee | `READ_ONLY_COMMANDS` allowlist; anything else raises `PermissionError` before send | `ingestion/ha_source.py:147-165`, `:932-935` |
| Reconnect | Jittered exponential backoff, 1 s → 60 s, unbounded retries | `ingestion/ha_source.py:186-220`, `:739-745` |
| Startup handshake | `wait_connected(timeout=30.0)` used by bootstrap | `ingestion/ha_source.py:501`, `main.py:704` |
| History queries | 12 h slices when window > 24 h; per-slice timeout floor 15 s; merge by key | `ingestion/ha_source.py:103`, `:120`, `:145`, `:231-300` |
| Timeout budget | `max(60, 2.5 × hours)`, discovery adds 1200 s fit overhead, 90-day cap | `api/timeouts.py:28-62` |
| Engine API | 18 routes under `/api/v1` on aiohttp | `api/server.py:619-636` |
| Auth | `X-API-Token` constant-time compare; `X-WattMosaic-Contract-Version` echoed | `api/server.py:58-59`, `:229-252` |
| HA side | `DataUpdateCoordinator`, default 30 s poll, 5–3600 s bounds | `custom_components/wattmosaic/coordinator.py:136`, `const.py:70-78` |
| Writes to HA | None. `ServiceCommand` is engine-internal only (`start_learning`, `rebuild_topology`, …) | `contracts.py:932-950` |

Assessment: this is the most complete part of the system; it needs **no rebuild**. Two caveats:

1. The HA client is a *state* subscriber, not an event-stream consumer for device/entity-registry
   changes; entity renaming is handled by `entity_migration.py` on the integration side, so a
   rename during an engine outage is **unverified** as recovered.
2. `HASource.history()` is called from exactly one production site, placement discovery
   (`main.py:2471`), and slices long windows internally (`ingestion/ha_source.py:231`, `:630`).
   There is no path that re-runs a stored window through today's algorithm: "re-analyse an old
   window" would re-query HA rather than read events already persisted.

---

## 3. CT and measurement model

| Concept | Representation | Evidence | Note |
| --- | --- | --- | --- |
| CT observation | `Measurement` (entity, ct, value, unit, 3 timestamps, quality, polarity, sequence) | `contracts.py:277-312` | Immutable reading; correct independent node |
| CT channel | `CTChannel` (ct_id, entity, type, phase, parent, polarity, device, paired, alias, multiplier, panel) | `contracts.py:315-366` | Pre-assigns hierarchy facts onto the observation |
| Sensor role | `CTType`: `MAIN`, `BRANCH`, `DEDICATED_DEVICE`, `UNKNOWN` | `contracts.py:43-47` | Classifies the *sensor* |
| Phase | `L1`, `L2`, `BOTH`, `UNKNOWN` | `contracts.py:50-54` | Double-pole is `BOTH` + `paired_ct_id` |
| Scaling | `measurement_multiplier: float = 1.0`, validated >0 finite | `contracts.py:344`, `:367-383` | Applied once, on read, never written back |
| Scaling application | `TopologyGraph.scale_w()` / `multiplier_for()`; `direct.py` and `matrix.py` consume it | `electrical/topology.py:615-640`, `electrical/direct.py:118`, `electrical/matrix.py:401` | Cannot be applied twice by construction |
| Alias | `alias_of_ct_id`, resolved at read time by `canonical_channel_id` | `contracts.py:335`, `electrical/topology.py:641-660` | Correct: only the canonical channel contributes to totals |
| Panel | `panel_id` / `panel_name`, additive since 1.5.0, migration 8 | `contracts.py:363-365`, `storage/migrations.py:96-114`, `:459-470` | Persisted, round-trips (fixed defect) |
| Device visibility | `MonitoringLevel`: `DEDICATED`/`SHARED`/`HOME_TOTAL_ONLY`/`UNMONITORED`/`UNKNOWN` | `contracts.py:151-168` | Classifies a *device*, not a circuit |
| Measurement basis | `Source`: `DIRECT_MEASURED`/`INFERRED`/`MODEL_ESTIMATED`/`CONFIGURED_PRIOR`/`UNKNOWN` | `contracts.py:62-73` | Answers "how obtained", not "what instrument" |

**Factor semantics — what exists and what does not.**

Exists: a per-channel positive float, user-declared, applied once when a reading is read
(`contracts.py:344`; `electrical/topology.py:629-640`); UI bounds 0.01–100
(`custom_components/wattmosaic/flow_logic.py:66-67`); persisted since migration 6
(`storage/migrations.py:440-452`).

Does not exist:

| Requirement | Status | Evidence |
| --- | --- | --- |
| Distinguish "monitor already applied ×2" from "user must apply ×2" | **MISSING** — one scalar, docstring says the monitor "commonly" applies it (`contracts.py:336-343`) | `grep` finds no such field |
| Factor with provenance (`declared_by`, reason) | **MISSING** — `metadata` is a free dict with no schema | `contracts.py:351` |
| Factor validity interval (changed at t) | **MISSING** — a single mutable float; history unreconstructable | `contracts.py:344` |
| Factor visible in the result | **MISSING** — `InferenceResult` has no basis/multiplier field | `contracts.py:796-823` |
| Detect a *wrong* factor from physics (child > parent) | **PARTIAL** — containment can reject, but nothing names the factor as the cause | `electrical/nesting.py:281-308` |
| Simulator emits factor-adjusted readings | **MISSING** — no `factor`/`multiplier` concept in `tools/simulator/*` | measured: all 36 channels have `measurement_multiplier == 1.0` |

The consequence is precise: the engine can *apply* a factor correctly but cannot *say* it did,
cannot *audit* it, and cannot *reconstruct* a past reading if the factor changed.

---

## 4. Storage and history

| Table | Purpose | Evidence | Verdict |
| --- | --- | --- | --- |
| `ct_channels`, `devices` | Denormalised current topology (deleted and rewritten on each save) | `storage/database.py:207-252` | Fine for "current", useless for "as-of" |
| `topology_versions` | Full JSON payload per `save_topology` + status + note | `storage/migrations.py:165-172`, `storage/database.py:243-246` | **Real versioning** (only place it exists) |
| `ct_samples` | Raw readings with three timestamps | `storage/migrations.py:174-190` | Pruned by raw retention |
| `device_events` | Declared entity events | `storage/migrations.py:192-206` | Pruned by raw retention |
| `device_state_samples` | Runtime state snapshots | `storage/migrations.py:208-219` | Pruned |
| `inference_results` | Append-only per-cycle device results | `storage/migrations.py:221-239` | No version/previous/delta/reason |
| `learning_samples` | Accepted/rejected samples with reason | `storage/migrations.py:244-262` | Good audit of learning |
| `device_models`, `device_model_versions` | Model lifecycle incl. `ROLLED_BACK` | `storage/migrations.py:264-292`, `contracts.py:100-106` | **Real versioning** |
| `reconciliation_results` | One row per cycle | `storage/migrations.py:343-361` | Rows exist; no reader for history |
| `data_quality_events`, `system_events` | Diagnostics trail | `storage/migrations.py:363-384` | Adequate |
| `device_configurations` | User declarations | `storage/migrations.py:418-426` | Good |

**Two defects worth fixing before adding schema:**

1. **Retention destroys the audit substrate.** `prune()` includes `inference_results` and
   `reconciliation_results` in the *raw* window (`storage/database.py:826-833`), default 30 days
   (`addon/config.yaml:118`). A "versioned inference audit" cannot live in a table deleted after
   30 days.
2. **`inference_history` is unreachable in practice.** `EngineCore._device_history` passes
   `since=utcnow()` (`main.py:1789`), which `timestamp >= ?` (`storage/database.py:570`) makes
   always false. The `history` field of `GET /devices/{id}` is returned empty
   (`main.py:1784`); no test asserts its content (`grep '"history"' tests/` → no API test).

Everything needed for a versioned audit is *nearly* present: `provenance` already carries the
confidence components as a dict (`disaggregation/engine.py:1578`), and `model_version` /
`operating_stage` are already columns (`storage/migrations.py:231-232`). The missing pieces are
`algorithm_version`, `previous_*`, `confidence_delta`, `reason`, `evidence_refs`, plus a table
that retention does not prune.

---

## 5. Event processing and isolation

| Stage | Implementation | Evidence | Assessment |
| --- | --- | --- | --- |
| Entity→device link | Declarative only; never name/area similarity | `device_links.py:1-40`, `:223-229` | Correct and load-bearing |
| Role discrimination | `classify_entity`: mode domains are never runtime | `entity_evidence.py:119-157` | Strong (`docs/decisions/README.md:229`, ADR-0012) |
| Link ambiguity | `ambiguous_entities()` detects one entity claimed by two devices, never resolves | `device_links.py:204-220` | Detects, does not prevent |
| State write guard | A CT power entity may link for measurement but not overwrite runtime state | `main.py:956-963`, `device_links.py:261-276` | Fixed defect, tested |
| Transition detection | `previous == new` → attribute churn; first observation → no transition | `main.py:975-986` | Correct |
| Pending transitions | Bounded `deque(maxlen=4096)`, attached to the next window then cleared | `main.py:642`, `:987-994`, `:1291-1301` | Lossy on overflow, not counted |
| Event buffer | `deque(maxlen=512)`; evictions counted | `main.py:998-1006` | Counted, surfaced in status |
| CT step anchoring | Anchored rescan restricted to `[at ± (after_s + cadence)]` per device | `disaggregation/engine.py:784-880` | The strongest part of event handling |
| Concurrency (inference) | `concurrent_events = len(window.state_transitions)` — a whole-window count, not per device | `disaggregation/engine.py:1449` | Coarse |
| Ambiguity | Null-space groups + unidentifiable changed devices | `disaggregation/engine.py:1375-1421` | Correct "unknown over fabricated" |
| Concurrency (learning) | `SampleEvidence.concurrent_events` compared to `max_concurrent_events = 1` | `learning/sample_filter.py:306-312`, `thresholds.py:45` | **Dead**: evidence never supplied (`learning/pipeline.py:72`) |
| Concurrency window | `concurrency_window_s = 10.0` | `learning/thresholds.py:43` | **Unused** anywhere in the tree |
| Multi-entity → one device dedup | none | — | See below |

**There is no isolation status.** The only per-device signal is a float:
`event_isolation_score` returns 1.0 (own event, ≤1 concurrent), 0.45 (own event plus others),
0.60 (no event but identified), 0.15 (unresolvable group)
(`disaggregation/confidence.py:202-220`). It is then multiplied into a single blended score
(`:167`). A consumer cannot ask "was this reading isolated?" and get a durable, named answer.

**Multi-entity devices are not deduplicated.** A physical appliance with a `switch` and a
`binary_sensor` produces two transitions; `on_event` handles them independently and appends two
`_pending_transitions` entries (`main.py:987`), so `len(window.state_transitions)` counts both and
the device looks twice as concurrent as it is (`disaggregation/engine.py:1449`). The only
de-duplication is `DUPLICATE_STATE` at the *sample* level, keyed by `(device, state)` within 30 s
(`learning/sample_filter.py:231-246`, `thresholds.py:67`) — a different problem.

---

## 6. Disaggregation and learning

| Aspect | Implementation | Evidence |
| --- | --- | --- |
| Solver | Weighted least squares over a topology matrix, bounded, with preconditioning | `disaggregation/engine.py:992-1370`, `electrical/matrix.py`, `electrical/solver.py:136` |
| Level 1 | Dedicated CT direct read (multiplier applied here) | `disaggregation/engine.py:1510-1589` |
| Level 2a | Measured step on own row with siblings steady (`docs/decisions/README.md:1198`) | `:1681-1795` |
| Level 2 | Inferred from the fit | `:1795-1910` |
| Level 3 | Model estimate | `:1911-1968` |
| Level 3b | Configured prior (declared watts) | `:1969-2031` |
| Level 4 | Unknown; value 0 W, source `UNKNOWN` | `:2032-2070`, `:2314-2345` |
| Confidence | Six weighted components, then penalties | `disaggregation/confidence.py:62-69`, `:135-200` |
| Tier | `evidence_tier` + `grade`, monotone with confidence, coarser | `disaggregation/tiers.py:126-217` |
| Learning | Collect → filter (11 reasons) → persist → train → validate → promote | `learning/pipeline.py:61-176`, `learning/sample_filter.py:27-40` |
| Learning observability | `seen/accepted/rejected`, per-reason tally, `learning`/`blocked`/`off` | `learning/reporting.py:39-48`, `:167-230` |
| Shared-circuit groups | Co-toggle group NNLS with rotated null, reports group totals | `disaggregation/group_fit.py:1037`, `main.py:2549-2600` |
| Placement | Branch rows only; user declarations win; positive delta required | `disaggregation/auto_placement.py:159-260` |

**Confidence is a blend, by design.** `WEIGHTS = {measurement_quality 0.30, ct_support 0.15,
model_quality 0.20, event_isolation 0.15, topology_certainty 0.10, residual_quality 0.10}`
(`disaggregation/confidence.py:62-69`), combined by `sum(WEIGHTS[k] * components[k])`
(`:167`), then multiplied by penalties. The components *are* computed and are written into
`provenance["confidence"]["components"]` (`disaggregation/engine.py:1578`), but they are never
exposed as independent dimensions, and the aggregate is what every consumer reads
(`custom_components/wattmosaic/entity_values.py:65-66`).

Assessment: the *inputs* to independent dimensions already exist. `ConfidenceBreakdown`
(`disaggregation/confidence.py:109-122`) has `score`, `components`, `penalties`, `notes`, and
`as_dict()` already marks `calibrated: False`. Exposing `components` as the dimension vector is a
serialisation change, not a solver change.

---

## 7. Residuals

| Property | Implementation | Evidence |
| --- | --- | --- |
| Definition | `unknown_residual = main.total_w - accounted`; signed, never clamped | `disaggregation/engine.py:2096`, `contracts.py:838` |
| Never redistributed | Contract docstring; enforced by construction | `contracts.py:827` |
| Meter spread (unbounded diagnostic) | `\|canonical total − alias total\|` | `electrical/main_power.py:417-440`, `disaggregation/engine.py:2149` |
| Meter disagreement (clipped contribution) | `clip(spread, 0, residual)` | `disaggregation/engine.py:2155-2162` |
| Unmonitored | `residual − disagreement` | `:2177` |
| Invariant | `unmonitored + disagreement == residual` exactly | `:2177`; tests in `tests/unit/test_reconciliation_split.py` |
| Spread > residual flag | `meter_spread_exceeds_residual` + explanation string | `contracts.py:871`, `:2160` |
| Unexplained residual w/o transition | Diagnostic with reason, no claimant invented | `:2103-2118` |
| Negative residual | Reported as-is with `negative_residual_sample_vintage_skew` | `:2258-2260` |
| Persistence | One row per cycle | `storage/database.py:577-596` |
| Read path | Latest row only | `storage/database.py:598-604`, `main.py:1598-1602` |

Missing for the new requirements:

- **No baseline-vs-dynamic split.** A 90 W always-on load and a 90 W load that cycled at 03:00
  are the same `unmonitored_w`.
- **No residual time series API.** Rows are written every 5 s but nothing reads them
  (`grep reconciliation_results` → insert, latest, prune, stats only).
- **No age or resolution state.** Nothing records "this residual has been unexplained for 30
  days"; the field docstring itself says `unmonitored_w` means "not attributable to a device",
  not "not metered" (`contracts.py:853-860`).
- **30-day horizon.** `reconciliation_results` is pruned with raw retention
  (`storage/database.py:832`).

---

## 8. Configuration and scheduling

| Knob | Default | Where | Effect |
| --- | --- | --- | --- |
| `inference_interval_s` | 5.0 | `config/settings.py:86`, `addon/config.yaml:114` | Both quality and inference loop cadence (`main.py:1211-1239`) |
| `stale_after_s` | 120 | `settings.py:85` | Window usability + learning staleness |
| `default_sample_interval_s` | 10.0 | `settings.py:84` | Fallback cadence |
| `learning_enabled` | true | `settings.py:83` | Constructs pipeline; pause is runtime |
| `auto_placement` | false | `settings.py:95`, `addon/config.yaml:126` | Opt-in device creation; needs `apply=true` too |
| `min_samples_for_model` | 12 | `settings.py:100` | Training threshold |
| `promotion_margin` | 0.05 | `settings.py:102` | Must beat active model by this |
| `max_sample_rejection_ratio` | 0.60 | `settings.py:103` | Guard against garbage training |
| `queue_max_size` | 5000 | `settings.py:87` | Drop-oldest ingest queue |
| `retention_days_raw` / `_aggregates` | 30 / 730 | `settings.py:79-80` | Prune horizon (see §4) |
| Maintenance interval | 60 s | `main.py:95`, `:1351` | Aligner prune + DB prune |
| HA poll interval | 30 s | `custom_components/wattmosaic/const.py:70` | Integration-side, bounds 5–3600 |
| Config validation | `EngineConfig.validate()` + `--check-config` (exit 2 on problems) | `settings.py:150`, `main.py:3096-3108` | Good operator ergonomics |

Assessment: scheduling is coherent — one interval drives both loops, and the maintenance prune is
independent. The gap relevant to the brief is that **no threshold is per-device**: isolation
windows, concurrency tolerance and duplicate windows are global constants
(`learning/thresholds.py:17-83`), so a 2-minute HVAC cycle and a 200 ms dimmer transition share
one `concurrency_window_s`.

---

## 9. Testing framework

| Layer | Files | Tests | What it proves |
| --- | --- | --- | --- |
| Unit | `tests/unit/` | 1302 collected | Solvers, contracts, storage, learning, API, panel tree, persistence |
| Integration | `tests/integration/` | 709 collected | HA-side entities/services, backend wiring, safety invariants, release gates |
| End-to-end | `tests/end_to_end/` | 57 collected | API, event intake, pipeline, storage against a real DB file |
| Simulation | `tests/simulation/` | 109 collected | Stratified accuracy, energy integral, leg split, replay, real-capture gates |

(Collected counts from `pytest --collect-only`, summing to 2177.)

Strengths:

- The simulator harness is genuinely good: per-stratum reporting, never one aggregate score
  (`tests/simulation/metrics.py:5`), an oracle self-check (`metrics.py:948`), and negative
  controls — "flat"/"naive" predictors must score worse (`test_accuracy.py:98`).
- Safety invariants are tested as properties, not examples: no field name containing
  "redistribut" (`tests/integration/test_safety_invariants.py:265`), rejection codes present
  (`:268-277`).
- CI runs many gates beyond tests: ruff, mypy, contract-schema freshness, validation-doc
  freshness, deployment install path, no-tracked-live-audit, synthetic-fixture check, secrets
  scan, manifest/add-on JSON, HACS validation, version agreement
  (`.github/workflows/ci.yml:20-131`).

Gaps:

| Gap | Evidence |
| --- | --- |
| The only real-installation tests skip without a local capture | `tests/simulation/test_real_topology.py:57-66`; 9 skipped here |
| No test asserts `/devices/{id}` `history` is non-empty | `grep '"history"' tests/` finds no API test |
| `CONCURRENT_EVENTS` is tested only with hand-built `SampleEvidence` | `tests/unit/test_learning.py:163-167` |
| No scenario exercises a factor-adjusted CT or a factor change | no `measurement_multiplier != 1.0` anywhere in `tools/simulator` |
| Per-class error is reported, but only asserted as *present*, not bounded | `test_accuracy.py:118-128` asserts stratum membership; ceilings are asserted per device id (`:179`) |
| No coverage gate in CI | no `--cov` in `.github/workflows/ci.yml` |

---

## 10. Logging, diagnostics, deployment

| Concern | Implementation | Evidence |
| --- | --- | --- |
| Log format | JSON formatter by default; plain via `WM_LOG_JSON=false` | `main.py:145-161`, `:164-177` |
| Levels | `WM_LOG_LEVEL`; aiohttp access logs forced to WARNING | `main.py:177` |
| Diagnostics payload | One JSON-safe, recursively redacted dict: generated_at, contract version, health, HA, channels, aligner, models, quality, DB, last success, errors, config, runtime, ok | `diagnostics/report.py:162-224` |
| Secret redaction | Key-based recursion + explicit `config.redacted()` | `diagnostics/report.py:42-69`, `config/settings.py:164-177` |
| Metrics | Prometheus text from `EngineCore.metrics()`: up, status, HA, backend present, queue depth, dropped, ingested, persisted, events, `power_events_not_state_total`, contract/engine info; the API has a static fallback if the facade fails | `main.py:2793-2845`, `api/server.py:536-582` |
| Health semantics | `degraded` when no backend, HA down, no topology, or queue > 50 %; `error` when the DB probe or integrity check fails | `main.py:1398-1424` |
| Watchdog | Unauthenticated `/healthz` proxying engine health | `addon/gateway.py:398`, `addon/config.yaml:95` |
| Container | `read_only`, `cap_drop: ALL`, `no-new-privileges`, `/tmp` tmpfs, `/data` the only writable volume; entrypoint fixes `/data` ownership then drops to UID 10001 | `docker-compose.yml:45-58`, `Dockerfile:67-70`, `addon/run.sh:1-20` |
| HA App | Ingress on 8787, no host networking, `homeassistant_api: true`, discovery service | `addon/config.yaml:53-69` |
| Backup | `backup: cold` so the WAL DB is copied consistently | `addon/config.yaml:105` |
| Startup | `init: false`, `boot: auto`, `stage: experimental` | `addon/config.yaml:40-43` |

Assessment: operator-facing observability is better than typical for this class of project —
notably `learning.effective_state` exists precisely because "learning ON with 0 accepted samples"
looked healthy (`learning/reporting.py:1-28`). The gaps are that **diagnostics are snapshot-only**
(no ring buffer of cycle-level decisions) and that **`provenance` is the only place per-result
evidence lives**, so a support engineer must parse a free-form dict.

---

## 11. Gap analysis

Legend: **EXISTS** = a first-class, persisted, consumer-visible mechanism. **PARTIAL** = the
information is computed or reachable but conflated, ephemeral, or not exposed. **MISSING** = no
representation exists.

No requirement in the brief is a clean **EXISTS**: 8 are **PARTIAL**, 12 are **MISSING** (one of
which, G17, is a plain defect rather than a new capability), and one of the PARTIALs (G7) is a
*dead* code path. Every proposed change below is additive.

| # | Requirement | Status | Current state (evidence) | Smallest sensible change |
| --- | --- | --- | --- | --- |
| G1 | Refoss ×2 factor as a *semantic* | PARTIAL | `measurement_multiplier` float, applied once on read (`contracts.py:344`, `electrical/topology.py:629-640`); no provenance, no basis | Add `FactorProvenance` enum (`monitor_applied`/`user_applied`/`unknown`) + `factor_declared_by` to `CTChannel.metadata` schema; no new table |
| G2 | Factor validity interval / history | MISSING | Single mutable float (`contracts.py:344`); change is unreconstructable | Add `ct_measurement_factors(ct_id, multiplier, effective_from, effective_to)` (migration 9); resolve in `TopologyGraph.multiplier_for` by reading timestamp; `CTChannel.measurement_multiplier` stays as "current" for 1.5.0 readers |
| G3 | Factor-adjusted readings distinguishable | MISSING | `Source` cannot express it (`contracts.py:62-73`); `InferenceResult` has no basis field (`:796-823`) | Add `measurement_basis` enum + field to `Measurement` and `InferenceResult` (additive, default `UNKNOWN`); set it in `direct.py`/`matrix.py` where `scale_w` is applied |
| G4 | Event isolation status per device | MISSING | Only `event_isolation_score` numbers 1.0/0.6/0.45/0.15 (`disaggregation/confidence.py:202-220`) inside a blend | Add `IsolationStatus` enum (`ISOLATED`/`RESOLVABLE`/`CONTAMINATED`/`INSUFFICIENT_EVIDENCE`) on `InferenceResult`; derive from the same inputs where `isolation` is computed (`disaggregation/engine.py:1635-1639`, `:1541-1545`) |
| G5 | Per-device isolation window | MISSING | Global `concurrency_window_s = 10.0`, **unused** (`learning/thresholds.py:43`) | Keep the global as default; add `concurrency_window_s` to `EffectiveConfig` (`disaggregation/configuration.py:107-140`), resolved in `effective_config` (`:317`) |
| G6 | Multi-entity → one device dedup | MISSING | Two transitions from one appliance are two entries (`main.py:987`, `disaggregation/engine.py:1449`) | Collapse `_pending_transitions` by `device_id` when building `state_transitions` (`main.py:1291-1300`), keeping a per-device count so concurrency is still visible |
| G7 | Learning concurrency check actually runs | PARTIAL (dead) | `SampleEvidence` never built in production (`learning/pipeline.py:72`); `concurrent_events` always 0 (`learning/sample_filter.py:307`) | Pass `SampleEvidence(concurrent_events=len(window.state_transitions))` from `WattMosaicBackend._learn` (`integration.py:585-635`). No schema change |
| G8 | Device↔CT association as an evidence-bearing relation | PARTIAL | Three unrelated storages, no precedence, no evidence (`contracts.py:328`, `:400-401`, `:587`) | Add `TopologyRelation(kind, subject, object, status, confidence, evidence, created_at)`; persist in one `topology_relations` table; leave existing fields as projections |
| G9 | Association history / validity | MISSING | Association is overwritten with the topology (`storage/database.py:207-252` deletes and re-inserts) | Reuse `topology_versions` (it already stores full payloads, `migrations.py:165-172`) and add a "read as-of" query; no new table needed for v1 |
| G10 | Circuit classification (dedicated/shared/partial/unknown) with confidence | PARTIAL | `CTType` classifies sensors (`contracts.py:43`), `MonitoringLevel` classifies devices (`:151`); no `PARTIAL`, no per-circuit confidence | Add `CircuitClass` + `classification_confidence` to the panel-tree circuit node (`electrical/panel_tree.py:949-960`); derive once per circuit |
| G11 | CT parent/child discovery | PARTIAL | `infer_nesting` proposes only `main` children and never writes (`electrical/nesting.py:604-607`, `main.py:1877`) | None needed for correctness; if required, extend `infer_nesting` to branch→branch with closure evidence. **Do not** auto-write |
| G12 | Residual decomposition (baseline vs dynamic) | MISSING | `unmonitored_w` is one number (`disaggregation/engine.py:2177`) | Split `unmonitored_w` into `baseline_w` (per-device minimum in window) + `dynamic_w`; both derived in `_reconcile`, additive |
| G13 | Residual time series / age | MISSING | Rows written per cycle (`storage/database.py:577`) but only `latest_reconciliation()` reads (`:598`) | Add `reconciliation_history(since, limit)` + expose under `/api/v1/status` or a new route; exempt the table from raw prune |
| G14 | Independent confidence dimensions | PARTIAL | Components computed and stored in `provenance` (`disaggregation/engine.py:1578`); consumers read the blend (`custom_components/wattmosaic/entity_values.py:65`) | Add `confidence_dimensions: dict[str, float]` to `InferenceResult`, populated from `ConfidenceBreakdown.components`; keep `confidence` for 1.5.0 clients |
| G15 | Versioned inference result | PARTIAL | Append-only rows with `model_version`/`operating_stage` (`migrations.py:221-239`); no algorithm version, previous value, delta, reason | Add `algorithm_version`, `previous_power_w`, `confidence_delta_json`, `reason` to `InferenceResult`; compute against `latest_results[device_id]` where results are persisted (`main.py:1326-1328`) |
| G16 | Versioned inference survives retention | MISSING | `prune()` deletes `inference_results` at 30 days (`storage/database.py:826-833`) | Add an `inference_audit` table (its own migration, after G2's) pruned on aggregate retention (730 d) or exempt from prune |
| G17 | Device history read path works | MISSING (defect) | `since=utcnow()` (`main.py:1789`) always excludes every row | Change to `since=utcnow() - timedelta(days=N)`; add one API test |
| G18 | Simulator: factor-adjusted channels + factor change | MISSING | 36 channels, all `multiplier == 1.0` (measured: `SimChannel` has no such field, `tools/simulator/topology.py:51-68`); the engine channel is built with no multiplier (`topology.py:214-230`) | Add `factor: float` + `factor_changes: list[(t, float)]` to `SimChannel`; apply in `SensorPipeline.observe` (`tools/simulator/sensors.py:156-165`) and pass it into `CTChannel.measurement_multiplier`; one scenario with a mid-run change |
| G19 | Simulator: richer household coverage | PARTIAL | 36 ch / 39 devices, standby + 2 unknown loads (`tools/simulator/topology.py:294-538`); per-class error reported (`tests/simulation/metrics.py:288`) but class ceilings unbounded; inrush is a fixed 2 s ×1.25 ramp (`behavior.py:120-124`) | Add class-level tolerance assertions beside the per-device ones (`tests/simulation/test_accuracy.py:179`); make inrush profile-driven via `ExpectedPower.startup_w` (already in `contracts.py:444`, and `DeviceModelParameters.startup_power_w` at `:742`; neither is used by the simulator) |
| G20 | Per-device thresholds | MISSING | All constants global (`learning/thresholds.py:17-83`) | Resolve per-device overrides through `EffectiveConfig`; add fields only where a measured need exists |

---

## What must NOT be rebuilt

These are load-bearing and correct; changing them would destroy value.

| Component | Why it must stay | Evidence |
| --- | --- | --- |
| Three orthogonal taxonomy axes (`HaConnectivity`, `MonitoringLevel`, `PowerBehavior`) | Prevents the "one enum to rule them all" collapse that loses information | `contracts.py:139-196`, `docs/decisions/README.md:179` (ADR-0011) |
| `Source` + `ConfidenceTier` separation | One answers *how obtained*, the other *what kind of evidence*; merging them is the failure mode the brief warns about | `contracts.py:62`, `:226`, `disaggregation/tiers.py:11-36`, `:126-217` |
| Read-only HA client | Structural safety: no service path exists, so no accidental actuation | `ingestion/ha_source.py:147-165`, `:932-935` |
| Declarative entity→device linking | Name/area similarity would fabricate attribution | `device_links.py:1-40` |
| Anchored step rescan | The single biggest accuracy mechanism; replaces a generic step detector with a time-confined one | `disaggregation/engine.py:784-880` |
| Null-space identifiability + explicit ambiguity | "Unknown over fabricated" is the product's core promise | `disaggregation/engine.py:1375-1421`, `electrical/solver.py:136` |
| Residual never redistributed | Enforced in contract and in tests | `contracts.py:827`, `tests/integration/test_safety_invariants.py:265` |
| Canonical-alias and multiplier applied once, on read | Prevents double counting structurally | `electrical/topology.py:615-660` |
| Nested-CT refusal to auto-write parent links | R1/R11 risk register; confirmation is the mitigation | `electrical/nesting.py:574-607`, `main.py:1877` |
| `learning.effective_state` (`learning`/`blocked`/`off`) | Fixes the "ON but accepting nothing" invisibility | `learning/reporting.py:1-48` |
| Migration discipline (forward-only, additive, idempotent helpers) | 8 migrations, `user_version`, no edits to released ones | `storage/migrations.py:1-12`, `:30-40`, `:455-470` |
| Stratified simulation metrics + oracle self-check | Only defence against a single misleading aggregate | `tests/simulation/metrics.py:5`, `:948`, `test_accuracy.py:98` |
| Panel tree as a read-time projection | Avoids a second source of truth for hierarchy | `electrical/panel_tree.py:1148`, `main.py:1840-1875` |

---

## Highest-leverage gaps, ranked

1. **G15 + G16 — versioned, retention-safe inference records.** Without an algorithm version,
   previous value and delta, no inference can be audited or compared after an upgrade; without
   exempting the table from raw prune, any audit evaporates in 30 days. Highest leverage because
   it is the substrate for G1–G4 evidence.
2. **G1–G3 — factor as a semantic with provenance, interval and basis.** The engine applies the
   factor correctly and can say nothing about it. This is the brief's first requirement and the
   only one with a demonstrated data model absent in both engine and simulator.
3. **G4 + G5 + G6 + G7 — isolation status that is real.** Statuses, per-device windows, multi-entity
   dedup, and the currently-dead learning concurrency guard are one coherent workstream; doing
   only the enum without G7 would surface a number nothing verifies.
4. **G14 — independent confidence dimensions.** Nearly free: the components are already computed
   and serialised into `provenance`; the blend simply needs a sibling field and a consumer.
5. **G8 + G9 — association as a relation with history.** Replacing three parallel storages with
   one typed edge (and reusing `topology_versions` for as-of reads) unblocks both "why is this
   device on this CT?" and "when did that change?".
6. **G12 + G13 — residual baseline/dynamic split and time series.** The arithmetic is already
   exact; what is missing is a second split and a reader that is not `LIMIT 1`.
7. **G17 — the empty device history.** A one-line defect with a customer-visible symptom; fix it
   before building history features on top of the same read path.
8. **G10 — circuit classification with its own confidence.** `MonitoringLevel` is per device;
   circuits have no object. Needed before the UI can honestly label a circuit "partial".
9. **G18 + G19 — simulator fidelity for the above.** Every gap above is untestable end-to-end
   until the simulator can emit factor-adjusted readings, a factor change, and per-class strata.
10. **G20 + G11 — per-device thresholds and branch-level nesting.** Deliberately last: both are
    refinements with real risk (wrong threshold, invented nesting) and no current evidence that
    the global defaults are wrong.
