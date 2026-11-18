# Gap Analysis B — Real-Time Measurement and Event Pipeline

Scope: HB §2.2–2.3, §5, §6.1–6.6, §6.2 late data, §18 null semantics, acceptance tests 17 and 18.
Repo: `<repo>` (branch `spec/realtime-topology-disaggregation`). All paths below are relative to `engine/wattmosaic_engine/` unless stated otherwise.
Method: read-only code reading. Nothing was executed except grep and sed. Items marked **(suspected)** are inferred from the code and still need a failing test to confirm.


---

## 0. Answer to the key question

**Today inference is a periodic full-window recompute, not event-driven.**

- `EngineCore._inference_loop` (`main.py:1184-1197`) calls `run_inference_once()` every `inference_interval_s` = **5.0 s** (`config/settings.py:192`, floor 1 s at `:322`).
- Each cycle builds a point-in-time snapshot of every channel (`main.py:1051-1079` → `SampleAligner.window`, `synchronization/aligner.py:671-694`). It drains *all* HA transitions batched since the last tick (`main.py:1249-1259`). It then calls `backend.process()` (`main.py:1268-1276`).
- `WattMosaicBackend.process` (`integration.py:319-377`) runs, **synchronously on the asyncio event-loop thread**:
  1. a full `DisaggregationEngine.infer` that solves for every device (`disaggregation/engine.py:704-966`);
  2. energy aggregation;
  3. learning sample collection plus **model training** (`integration.py:628-742`);
  4. the six-stage audit pass (`integration.py:791-830`);
  5. "realtime" reanalysis. Reanalysis reads up to **6 h of `ct_samples`** plus 2000 device events from SQLite and re-runs the whole audit (`integration.py:857-905`, `disaggregation/audit.py:931-987`).
- `maybe_await(process(...))` (`main.py:1268`) never offloads the call. `process` is a plain sync function, so the whole chain blocks the loop. HA WebSocket ingestion, `/health` and `/healthz` share that loop. This is the ADR-0033 defect class, still present in the hot path.
- Change detection is not a pipeline stage. It is a **by-product of each 5 s cycle**: `detect_changes` scans each channel's `StepTracker` history (`engine.py:790-800`), and `tracker.observe(readings, coerced.at)` records the snapshot at the **cycle wall-clock time** (`engine.py:828-831`, `disaggregation/events.py:526-552`).
- The only event-driven parts are two watermarks: training (`integration.py:705-715`) and reanalysis (`integration.py:876-893`). Both are still *evaluated* on the 5 s tick.

**Distance to HB §5/§6:**
- **Present in some form:** pieces of stages 1, 3 and 4 (unit normalization, MAD noise, a robust two-window step test).
- **Absent:**
  - the three-loop separation;
  - a streaming per-reader state model with hysteresis/CUSUM/EWMA;
  - event-window evidence capture;
  - cross-reader event groups with sub-events, HA context ids and late-arrival revision.

---

## 1. Current-state map

| HB concept | Location (file:line) | What it does today | Verdict |
|---|---|---|---|
| §2.1 ReaderMeasurement record | `contracts.py:1484-1530` `Measurement` | Has `entity_id, ct_id, value, unit, value_w, state_timestamp, received_at, physical_timestamp, quality, repeated, polarization, sequence, measurement_basis, multiplier_applied`. **No** `current_rms_a, voltage_rms_v, reactive_power_var, power_factor, phase, leg, voltage_reference, measurement_type, measurement_window_ms, calibration_version, quality_flags[]`. `physical_timestamp` is never set by HASource. | PARTIAL |
| §6.1 Unit normalization | `ingestion/normalize.py:41-63`, `ha_source.py:1112-1161` | W/kW→W, Wh/kWh→kWh, sign preserved, unsupported units rejected (`normalize.py:49-63`). Non-finite values rejected (`normalize.py:70-91`). | MATCHES (units only) |
| §6.1 Validate measurement type / phase / leg / calibration / plausible range | `ha_source.py:1066-1068` (power vs energy by unit/device_class only) | Phase and leg live on `CTChannel` (`contracts.py:1533+`) and are never attached to the reading. There is no plausible-range check at ingestion; `quality/engine.py` scores plausibility *after the fact*. | PARTIAL |
| §6.1 "Invalid measurements must not silently enter inference" | `main.py:1199-1303` | `run_inference_once` never consults `latest_quality` (`DataQualityReport` from `quality/engine.py:293`). A channel the quality engine scores `bad`/`degraded` still enters `engine.infer` with the HASource quality (`GOOD`/`STALE` only, `ha_source.py:1138`). | CONFLICTS |
| §2.2 Compatibility layer (phase/leg/240 V/paired/polarity/calibration/reverse flow) | `electrical/topology.py:608` `multiplier_for`, `electrical/matrix.py:160-182`, `CTChannel.phase/paired_ct_id/polarity` | Multiplier is applied in step detection (`engine.py:799`). Polarity: the matrix applies `channel_polarity` **only** for raw-float readings (`matrix.py:173-178`). For `ChannelReading` it uses `reading.signed_w()`, whose polarization comes from the aligner. `AlignedChannel` has no polarization field (`aligner.py:132-148`), so `coerce_window` defaults it to NORMAL (`window.py:284-290`). **(suspected)** A declared `CTChannel.polarity=inverted` is not applied to step deltas, and probably not to `y` either, on the production path. There is no explicit compatibility predicate ("which readers may share an equation"). | PARTIAL / (suspected) CONFLICTS |
| §2.3 Signed deltas | `disaggregation/events.py:333` `delta = after - base`; `StepChange.delta_w` | Signed. No `abs()` on primary data. `z_score` is signed (`events.py:105-107`). | MATCHES (modulo polarity bug above) |
| §5.1 Fast measurement loop | `main.py:865-868` `on_sample` | Appends to aligner and persist queue only. No per-sample baseline, noise or detection update. Quality is recomputed for the full window every 5 s (`main.py:1169-1182`). | PARTIAL |
| §5.2 Event inference loop (triggered) | `main.py:1184-1303` | Timer-driven, full recompute of all devices every 5 s. Triggers (reader change, HA change, residual change, topology change) do not schedule work. | CONFLICTS |
| §5.3 Background learning loop, fast path responsive | `integration.py:362-364` `_learn` / `_analyze` / `_maybe_reanalyze` | Training, audit and 6 h reanalysis all run inline in the 5 s cycle, on the event loop. The reanalysis DB read uses a separate reader connection (`storage/database.py:593-603`), but the call is still synchronous from the loop thread. No cycle-duration metric exists. | CONFLICTS (ADR-0033 class) |
| §6.2 observed_at / received_at | `ha_source.py:1130-1136` | `state_timestamp` = HA `last_updated`, falling back to `last_changed`, then `time_fired`, then now. `received_at` = engine clock. Both are persisted (`main.py:994-1007`). | PARTIAL (named `state_timestamp`; observed_at semantics not defined per reader type) |
| §6.2 measurement_window_ms | — | Absent. A window-averaged sensor (most CT monitors report 1–15 s averages) is treated as an instantaneous point. | ABSENT |
| §6.2 Common time domain | `aligner.py:747-752` `_latest_at` (uses **received_at**), `events.py:526-552` tracker (uses **cycle wall time**), `audit.py:527` `history_from_rows` (uses **state_timestamp**), `engine.py:1058-1063` anchor (HA `last_updated`) | Four time bases for the same evidence. Live step times are quantized to the 5 s tick in the receive domain, while reanalysis uses the observed domain. The anchor window (`after_s + cadence`) hides much of the skew. | CONFLICTS |
| §6.2 Configurable alignment windows | `electrical/window.py:164-177` (before = 2×cadence clamped 5–60 s, after = 1×cadence clamped 2.5–30 s), `disaggregation/isolation.py:290-612` (per-device/class/global windows) | Configurable and cadence-derived. | PARTIAL / reusable |
| §6.2 Late-arriving data | `aligner.py:637-658` (out-of-order insert by sequence), `isolation.py:1311` `EventIsolator.reclassify`, `events.py:534-537` (tracker **drops** out-of-order points) | The aligner keeps late samples. The tracker discards them. `reclassify` exists but has **no production caller** (grep: only defined). Pending transitions are consumed by the cycle that drains them (`main.py:1259`). If the CT post-sample arrives one cycle later, the anchored rescan for that transition never re-runs. No group revision. | ABSENT (test 18 not satisfiable) |
| §6.3 Rolling baseline per Reader | `events.py:218-240` `_baseline` | The baseline is the median of a pre-window recomputed at every candidate split, every cycle. There is no persistent rolling baseline, no guard against "absorbing a persistent step", and no new-level vs transient state. | PARTIAL |
| §6.3 MAD noise σ = 1.4826·MAD | `aligner.py:18,102-116,850` (innovation MAD, per channel, over a ≤240 s buffer), `events.py:136-140,327` (pre-window level MAD), `ingestion/correlation.py:527-550` (successive-difference MAD, offline) | Three separate noise definitions. The per-channel innovation MAD is wired as a floor (`main.py:1094-1125` → `engine.py:971`). | PARTIAL |
| §6.3 z-score with σ_min, absolute threshold, no global W threshold | `events.py:328-334` | `threshold = max(min_delta_w=5 W, k_mad=4 × max(noise, floor), resolution)`. Per-channel floor exists, but `min_delta_w` is a **global** 5 W hard floor (`events.py:79`). | PARTIAL |
| §6.3 Expected short-term noise, recent event frequency, measurement quality per reader | `aligner.py:808-854` `ChannelHealth` (cadence, repeat ratio, resolution, noise) | Has cadence, repeats and noise. No event frequency. Quality comes from `quality/engine.py` per window. | PARTIAL |
| §6.4 Hysteresis / CUSUM / EWMA / change-point | — | None. Each cycle runs a fresh best-split scan (`events.py:308-366`). The same physical step can be "detected" on several consecutive cycles until it leaves the window. Nothing debounces it or gives it an identity. | ABSENT |
| §6.4 Positive/negative, sustained vs transient, periodic, missing | `StepChange.direction` (`events.py:101-103`) | Direction only. No transient/sustained classification, no periodicity, and missing/unreliable data is not an event. | PARTIAL |
| §6.4 Event quality score | `StepChange.z_score` | Only `|Δ|/threshold`. No composite quality (sample count, staleness, cadence coverage, timestamp uncertainty). | PARTIAL |
| §6.4 Do not force every fluctuation into an event | `events.py:56-58,323-324` min 4 pre-samples; `previous_snapshot` fallback `events.py:456-475` | The two-point fallback has `noise_w=0`, threshold `min_delta_w`, and `change_time=at`. It is used for every flat channel, because the tracker collapses equal values (`events.py:543-548`). | PARTIAL |
| §6.5 Event window collection (pre/post, circular buffer) | `aligner.py:532-660` (deque 600 / 240 s), `events.py:480-552` `StepTracker` (64 distinct values, cycle-timestamped), `learning/sample_collector.py:139-210` (4096 per-cycle snapshots, repeats included), `disaggregation/audit.py` power history | **Four overlapping per-channel buffers**, each with different time bases and dedup rules. None stores an event window with signed deltas, baseline, noise, quality, start/end, timestamp uncertainty and topology version. | PARTIAL (fragmented) |
| §6.5 Immutable stored evidence | `contracts.py:2791` `EventEvidenceRecord` | The contract exists, keyed to HA state changes (`observed_at` = HA `last_updated`). It has **no production producer or consumer** (grep). It lacks reader deltas, window samples, baseline and noise. | ABSENT (contract stub) |
| §6.6 Event group formation | `disaggregation/isolation.py:909-1076` `group_events` / `EventGroup` | Groups **HA events only** by declared cause (`attributes.automation_id/script_id/cause_id/correlation_group`, `:912-930`) and 30 s proximity. Transient and never persisted (docstring `:937`). Reader steps are not members. Used only for isolation verdicts (`isolation.py:1109,1230`). | PARTIAL |
| §6.6 HA context id / parent context | `ingestion/ha_source.py:1042-1044,1185-1203` | HASource copies only `new_state["attributes"]`. HA puts `context.{id,parent_id,user_id}` at the top level of the state object, so it is **dropped**. `automation_id` is not an HA state attribute, so `group_events`' declared-cause pass **never fires on real HA data**. | ABSENT |
| §6.6 Sub-events with own timestamps; simultaneous changes stay distinct | `engine.py:261-311` `_collect_transitions` | Keyed `out[device_id]`, so two transitions of one device in one 5 s batch collapse to the last one. Unanchored transitions get `at = window.at` (the cycle time). | CONFLICTS |
| §6.6 HA events for not-yet-mapped entities | `main.py:894-899` | Events for entities the topology does not declare are counted and dropped (not buffered, not persisted). The rationale is ~1.5 GB/day of noise. Consequence: an HA command for a not-yet-mapped light can never join a real-time event group or become a candidate. Only offline discovery over recorder history sees it. | CONFLICTS (with §6.6/§7.1) |
| §12.3 lifecycle DETECTED…UNRESOLVED | — | No event status lifecycle. `EventIsolation` verdicts (ISOLATED/CONTAMINATED/INSUFFICIENT_DATA) are a different axis. | ABSENT |
| §18 / test 17 missing ≠ zero | `aligner.py:755-765` (missing → `value_w=None`), ADR-0015 | Good in the aligner and solver. **Leaks:** `main.py:213-219,401-460` `_safe_float(default=0.0)` turns DB-null `power_w`, `confidence`, `measurement_age_seconds`, `main_power_w`, `inferred_w`, … into `0.0` when rehydrating results. `electrical/window.py:293` turns `age_seconds=None` into `0.0` for a missing channel. `ingestion/correlation.py:543-545` returns noise `0.0` for "insufficient data". `audit.py:1171-1172` back-fills `received_at` with `occurred_at`. | PARTIAL |
| Repeated-value semantics | `ha_source.py:1140-1143`, ADR-0003, ADR-0015 | `repeated=True` when the value equals the previous one. HA does not emit `state_changed` for an unchanged value (it emits `state_reported`, which is not subscribed: grep finds no `state_reported`). A steady load is therefore *silent*, ages to stale after 120 s, and is pruned after 240 s (`main.py:549-553`). | PARTIAL (see Traps) |
| Replayability of stages (§6 "inspectable and replayed") | `main.py:1070-1071` `aligner.window(...)` with no `at`, so `utcnow()` | The live pipeline is wall-clock bound. Simulator tests bypass `EngineCore` and call the aligner and `engine.infer` directly (`tests/end_to_end/test_pipeline_e2e.py:69-127`). | PARTIAL |
| Timeouts | `api/timeouts.py:1-67` | Only covers the discovery request/history budget. Inference-cycle work has no budget or deadline. | N/A to pipeline (no change needed except new metrics) |
| Placement correlation (offline) | `ingestion/correlation.py:553`, `:846` `observations_from_history` | A separate step/noise implementation for offline discovery (ADR-0024/0033). | Out of hot path; reuse candidate |

---

## 2. Conflicts that need a breaking change

| # | Legacy concept | HB rule violated | Proposed replacement | Blast radius |
|---|---|---|---|---|
| C1 | 5 s timer-driven full recompute (`main.py:1184-1303`, `integration.py:319-377`), with `AlignedWindow` as the unit of work | §5 (staged, event-driven), §22.19, §15 ("do not create full snapshot every second") | A fast loop driven by `on_sample`. An event loop driven by a queue of ready Event Groups. A background loop for learning/audit/reanalysis. Keep a low-rate "snapshot heartbeat" (e.g. 30–60 s or on change) only for staleness publishing. | `main.py` (EngineCore loops, `run_inference_once`, `on_event`), `integration.py` (`process` split), `disaggregation/engine.py` `infer` signature (event-scoped input), all tests calling `run_inference_once` (`tests/unit/test_disaggregation.py`, `test_topology_bootstrap.py`, `test_learning.py`, `tests/end_to_end/*`, `tests/integration/test_learning_intake.py`), HA integration polling assumptions on `/snapshot` cadence |
| C2 | Inline training/audit/reanalysis on the event loop (`integration.py:362-364`) | §5.3 "fast path must remain responsive", ADR-0033 | A dedicated background worker (single-thread executor or process) with a bounded job queue. Inputs are immutable event evidence. Results are published back via a version bump. | `integration.py`, `learning/pipeline.py` call sites, `disaggregation/audit.py` (must become thread-safe or own its state), DB write path (single-writer rule ADR-0004: background writes must go through the writer or their own connection) |
| C3 | Four per-channel history buffers with different clocks (aligner received-time; tracker cycle-time; sample collector per-cycle snapshots incl. repeats; DB state_timestamp) | §6.2 common time domain, §6.5 single circular buffer, §15 no hidden state | One `ReaderStateStore`: per-reader ring keyed by `observed_at` that keeps `received_at` and `measurement_window_ms`. Zero-order-hold semantics: "value valid since t, last confirmed at t′". Rolling stats live alongside. Delete `StepTracker` and the `SampleCollector._snapshots` duplication. | `synchronization/aligner.py`, `disaggregation/events.py:480-611`, `learning/sample_collector.py:139-240`, `disaggregation/audit.py` `_histories` (`:1100-1142`), `electrical/window.py` `ChannelReading.history` producers, ~10 test modules |
| C4 | Stateless best-split step scan, re-run each cycle (`events.py:243-367`) | §6.4 hysteresis/CUSUM/EWMA, event identity, quality score | A streaming per-reader detector with a state machine (`STABLE → SUSPECT → CONFIRMED_STEP / TRANSIENT_REJECTED`), CUSUM/EWMA on residual-to-baseline, and per-reader σ from long-run stats. It emits a `MeasurementEvent` with a stable id. Keep `detect_step` as an offline/reanalysis helper. | `disaggregation/engine.py:772-831,1004-1130` (anchoring moves to grouping), `audit.py` isolation coverage, `tests/unit/test_event_anchor.py` (26 tests), `test_disaggregation.py` step tests |
| C5 | `_collect_transitions` keyed per device; `_pending_transitions` drained per cycle | §6.6 sub-events with own timestamps, simultaneous changes distinct | `EventGroup{group_id, status, sub_events[]}`. Each sub-event is a HA change, reader step or smart-plug change with its own observed_at, received_at and uncertainty. | `disaggregation/engine.py:261-311`, `main.py:598-600,945-952,1245-1259`, isolation/learning consumers |
| C6 | `isolation.EventGroup` = transient HA-only grouping by attribute keys that real HA never sets | §6.6 (HA context id, parent context, reader correlation) | A persistent `EventGroup` (domain contract) formed from context id/parent_id + temporal proximity + reader co-movement. Keep isolation verdicts as a consumer of groups. | `disaggregation/isolation.py:909-1076`, `learning/pipeline.py:183` (isolator), `audit.py:1286`; contract owned by domain area |
| C7 | `DeviceEvent` lacks HA context; `Measurement` lacks phase/leg/window/calibration/type | §2.1, §6.2, §18 typed contracts | New `ReaderMeasurement` and `HAEvent` contracts (domain area). HASource populates `context_id`, `context_parent_id`, `context_user_id`, `last_reported`, `measurement_window_ms` (declared per reader). | `contracts.py` (hot, shared), `ha_source.py`, `ct_samples`/`device_events` schema (storage area), `tools/simulator` |
| C8 | Dropping HA events for undeclared entities (`main.py:894-899`) | §6.6 grouping evidence, §7.1 candidates (matching area) | Keep an **in-memory** bounded ring of HA events for `candidate_entities` (`ingestion/candidate_entities.py`). Persist only those that join an Event Group that becomes evidence. This keeps ADR storage-growth protection. | `main.py:870-964`, storage volume, ADR referenced in comment (needs a new ADR) |
| C9 | DB rehydration `_safe_float(default=0.0)` (`main.py:401-460`) | §18 "absent must not become zero", test 17 | Nullable rehydration. Contract fields become `float | None` where semantically optional. | `main.py:401-460`, `InferenceResult`/`Reconciliation` contracts (domain area), API/UI consumers |
| C10 | Polarity not applied on the reading path (suspected) | §2.2 CT polarity, §2.3 signed deltas | Apply polarity and multiplier once, at normalization into `ReaderMeasurement`, stamped with `calibration_version`. Downstream code never re-applies them. | `ha_source.py`/new normalizer, `electrical/matrix.py:160-182`, `engine.py:799` (multiplier currently applied in detection), factor registry (`electrical/factors.py`) |

---

## 3. Reusable assets to keep

- `ingestion/normalize.py` in full: unit canon, `parse_float`, `parse_state_timestamp`, own-entity guard, allow/deny.
- `ingestion/ha_source.py`:
  - connection, backoff, history slicing and `_level_changed` (brightness as a measured level);
  - availability events (`:1048-1059`);
  - the `repeated` flag;
  - the own-entity guard.
  It needs extension, not rewrite.
- `ingestion/queue.py` `BoundedQueue` (drop-oldest with counters). Reuse it for the event-group queue and the background job queue.
- `synchronization/aligner.py`:
  - `robust_noise_mad`, `median`/`percentile`;
  - `ChannelHealth` (cadence, repeat ratio, resolution);
  - `resolve_channel_cadence` / `resolve_topology_cadence` with source tagging;
  - out-of-order insert (`:637-658`);
  - the power vs energy domain split.
- `disaggregation/events.py`:
  - `detect_step` math: baseline widening (`:218-240`), anchor bounds and validation (`:170-198`), multiplier scaling, signed delta;
  - keep it as the reanalysis/offline detector and as the confirmation test inside the streaming detector.
- `electrical/window.py`: `clamp_cadence`, `observed_cadence_s`, the documented 15.7 s fallback, `before_after_seconds` (as defaults for per-class window config).
- `disaggregation/isolation.py`:
  - three-layer window config (`IsolationConfig`, `:457-612`);
  - `physical_device_events` (many entities → one appliance);
  - contamination decision table;
  - `reclassify` (it already implements "late event retroactively contaminates");
  - deterministic `grp_` ids (`:1047-1076`).
- `quality/engine.py` scoring model and thresholds. Reuse it as the stage-1 quality-flag producer.
- `disaggregation/audit.py` `history_from_rows`, `aligned_series` (state_timestamp-based, NaN-for-absent). They are the right primitives for replay.
- `tools/simulator`:
  - `sensors.py` already models three timestamps, delay, jitter, drops, repeats and outages;
  - `scenario.py` has `state_lag_s`, `channel_delay_s`, polarity flips, late delivery via `_pending`;
  - LabHouse strata T06 automation_burst, T09 delayed_ct, T14 missing_intervals;
  - the truth/observation leak detector (`frames.py`).
- Tests worth porting: `test_event_anchor.py` (26), `test_event_isolation.py` (46), `test_ingestion.py` (28), `test_correlation.py` (61, incl. the linear-cost pin), `test_isolation_wiring.py` (19).

---

## 4. Proposed work packages

Notation: **OWNS** = files this WP may edit exclusively. **SHARED** = hot files that need coordination.

### WP-B1 — Canonical ReaderMeasurement + HAEvent ingestion (stage 1, §2.1–2.3)
- **Objective:** HASource emits a normalized `ReaderMeasurement` and an `HAEvent`.
  - `ReaderMeasurement` carries `observed_at`, `received_at`, `measurement_window_ms`, `measurement_type`, `phase`/`leg` from the channel, `calibration_version`, and `quality_flags[]` (unit_invalid, implausible_range, repeated, stale_on_arrival, clock_skew_future).
  - Polarity and multiplier are applied once here (fixes C10).
  - `HAEvent` carries `context_id`, `parent_id`, `user_id`, `last_reported`.
  - Invalid readings are flagged and routed to a quarantine counter, never into inference.
- **OWNS:** `ingestion/ha_source.py`, `ingestion/normalize.py`, new `ingestion/reader_measurement.py` (normalizer + compatibility predicate `compatible(reader_a, reader_b) -> reason`).
- **SHARED:** `contracts.py` (types come from the domain area), `storage` schema for `ct_samples`/`device_events` (storage area).
- **Depends on:** the domain area for the `ReaderMeasurement`, `HAEvent` and `MeasurementEndpoint` (phase/leg/voltage_reference/window) contracts, and calibration versioning from the factor registry.
- **Tests:**
  - unit: context captured from the `state_changed` payload; inverted CT yields a negative delta on increase; range check flags; unknown phase stays null (not defaulted);
  - simulator: inverted-channel scenario (T11) asserts signed deltas.
- **Size:** M

### WP-B2 — ReaderStateStore: single circular buffer + rolling baseline/noise (stages 2–3)
- **Objective:** One per-reader store keyed by `observed_at`.
  - Zero-order-hold semantics with explicit "last confirmed" for push-on-change silence (ADR-0015).
  - Out-of-order insert, with a late-arrival callback (`on_late(reader, t)`).
  - Rolling stats: baseline (robust median with slow EWMA plus freeze-on-suspect, so a persistent step is not absorbed), σ via innovation-MAD and level-MAD, σ_min per reader (resolution-derived), event frequency, cadence, quality.
  - O(1) amortized per sample.
  - Snapshot and serialize, for restart without hidden state (§15).
  - Replaces `SampleAligner` power buffers, `StepTracker` and `SampleCollector._snapshots`.
- **OWNS:** `synchronization/aligner.py` (rewrite or replace with `synchronization/reader_state.py`), `disaggregation/events.py:480-611` (remove `StepTracker`).
- **SHARED:** `learning/sample_collector.py` (consumer switch; coordinate with the learning area), `disaggregation/audit.py` `_histories`.
- **Depends on:** WP-B1 types.
- **Tests:**
  - unit: baseline does not absorb a sustained step for N cadences; σ estimate on a 15.7 s cadence with ≥20 min history; late sample inserted in order and triggers the callback; flat push-on-change channel keeps last value as "held", not missing, until stale policy; missing stays `None`;
  - perf: 40 readers × 1 Hz burst sustained under a target (e.g. <0.2 ms/sample);
  - simulator: T14 missing_intervals, T09 delayed_ct.
- **Size:** L

### WP-B3 — Streaming change detector (stage 4)
- **Objective:** A per-reader state machine with the following parts.
  - `z = (Y − baseline)/max(σ, σ_min)` with hysteresis (enter / exit thresholds).
  - Two-sided CUSUM for small sustained shifts, and EWMA for drift (a drift is not an event; it feeds the baseline).
  - A confirmation rule: a step is CONFIRMED after k post-samples or a post-window. A transient is a short excursion that returns to baseline.
  - Periodic tagging (event-frequency autocorrelation, cheap).
  - "Missing/unreliable" emitted as a quality event.
  - Each emitted `MeasurementEvent` carries a stable id, signed Δ, detection time, onset time bounds (`[last_pre_sample.observed_at − window_ms, first_post_sample.observed_at]`), and a quality score (z, sample counts, cadence coverage, staleness, timestamp uncertainty).
  - No global watt threshold. `min_delta_w` becomes per-reader, derived from resolution.
- **OWNS:** new `realtime/detection.py`. `disaggregation/events.py` `detect_step` is kept and called for confirmation/offline only.
- **Depends on:** WP-B2.
- **Tests:**
  - unit: positive/negative; sustained vs transient; no event on noise (false-alarm rate bound on a synthetic Gaussian + heavy-tail stream); one physical step → exactly one event across many ticks (fixes the re-detect-each-cycle behaviour); per-reader threshold (10 W event on quiet reader, none on noisy one);
  - simulator: T05 spikes, T08 dynamic, T04 standby (a steady 3 W standby produces no events).
- **Size:** M

### WP-B4 — Event window collector + immutable event evidence (stage 5)
- **Objective:** On DETECTED, capture the pre-window from the ring and keep the window open until post-window expiry (per load-class config, default 30 s pre / 120 s post, resolved like `IsolationConfig`).
  - Stores the samples, signed deltas, baseline, σ, quality, start/end, timestamp uncertainty and topology version as an immutable `EventEvidence` record.
  - The retention policy keeps raw samples only for evidence-bearing events (§15).
  - Emits PROVISIONAL at first post-sample and FINAL at post-window close.
- **OWNS:** new `realtime/windows.py`.
- **SHARED:** storage writer (batch insert via the existing persist queue, `main.py:967-1027`).
- **Depends on:** WP-B2 and WP-B3; the domain area for the `EventEvidence` contract (extend/replace `contracts.py:2791` `EventEvidenceRecord`, which is HA-only today); the storage area for the table and retention.
- **Tests:**
  - unit: pre-window available at detection (circular buffer); 15.7 s cadence → pre-window holds ≥2 samples, otherwise flagged `insufficient_pre`; evidence is immutable (a second write is rejected, a revision creates a new record); replay from stored evidence yields an identical Δ after raw samples expire.
  - simulator: T09 delayed_ct.
- **Size:** M

### WP-B5 — Event group formation + late-arrival revision (stage 6, §6.2 late data, test 18)
- **Objective:** Assemble `EventGroup{group_id, status, sub_events[]}`.
  - Sub-events come from reader `MeasurementEvent`s, `HAEvent`s (including undeclared candidate entities from an in-memory ring, fix C8) and smart-plug changes. Each keeps its own timestamps.
  - Grouping evidence: shared `context_id` or parent chain; temporal overlap of onset bounds (configurable tolerance, cadence-aware); reader co-movement consistent with topology (topology area supplies `observes(reader)`).
  - Group lifecycle DETECTED → MATCHING → (hand-off) PROVISIONAL / CONFIRMED / REVISED / UNRESOLVED.
  - A group closes on a quiet period plus the max post-window.
  - Late-arriving sub-events (late CT sample, late HA event, backfilled history) attach to an open group, or create a REVISED version of a closed group without touching others.
  - Reuse `isolation.reclassify` and `physical_device_events`.
  - Replace `_collect_transitions` (C5) and `isolation.group_events` (C6).
- **OWNS:** new `realtime/grouping.py`; `disaggregation/isolation.py:909-1076` (deprecate `group_events`, adapt to consume groups).
- **SHARED:** `disaggregation/engine.py:261-311` (transition intake; coordinate with the matching/joint-inference area that owns `engine.py`).
- **Depends on:** WP-B1 (context ids), B3, B4; the domain area (`EventGroup` contract and status enum); the topology area (reader observability for co-movement); the matching area as consumer.
- **Tests:**
  - unit: two devices switched by one automation (same parent context) → one group, two HA sub-events, distinct timestamps; one device toggled twice within 5 s → two sub-events, not one; context id present but readers move 60 s apart → not grouped on context alone (§6.6 "not absolute proof"); late CT sample arriving after group close → REVISED version and unrelated groups untouched (**acceptance test 18**);
  - simulator: T06 automation_burst (with context ids, see B8), `state_lag_s` scenario.
- **Size:** L

### WP-B6 — Three-loop runtime in EngineCore (§5)
- **Objective:** Restructure `EngineCore` into three loops.
  - **Fast loop:** `on_sample`/`on_event` → B1 → B2 → B3 → B4, inline, O(1), no DB reads.
  - **Event loop:** an `asyncio` task that awaits a ready-group queue (`BoundedQueue`). It calls the event-scoped inference API (provided by the matching/joint-inference area) via `asyncio.to_thread` on a dedicated single-worker executor, with an immutable input snapshot. It publishes results and the snapshot.
  - **Background loop:** a separate executor and job queue for training, audit, reanalysis, topology refit and residual updates. It triggers on evidence watermarks with a batching window, and never runs on the loop thread.
  - The periodic 5 s full recompute is replaced by a slow heartbeat that publishes staleness / missing (§12.1 `stale_sources`), not a full solve.
  - Add metrics: loop lag, per-stage latency, queue depth, dropped groups.
  - Remove the stale comment at `main.py:844-850`, which contradicts `storage/database.py:82-89` (`check_same_thread=False`).
  - Inject a clock so the pipeline can be replayed from simulator time.
- **OWNS:** `main.py` (EngineCore lifecycle/loops/ingestion callbacks, lines ~536-1320), `integration.py` (split `process` into `infer_group` / `learn_batch` / `audit_batch`), `config/settings.py` (new knobs; deprecate `inference_interval_s`, `WM_INFERENCE_INTERVAL_S`).
- **SHARED / HOT:** `main.py` (also touched by the mapping-matrix, UI and storage areas for facade methods: coordinate by region), `integration.py`.
- **Thread safety:** `api/server.py:622,628,875,889,901,914` already call facade methods in `to_thread` while the loop mutates `latest_results`. Publishing must use copy-on-write snapshots.
- **Depends on:** B2–B5; the matching/joint-inference area for `infer_group(group, state_snapshot) -> EventInferenceRecord`; the learning area for an off-loop-safe `retrain_device`.
- **Tests:**
  - unit: `/healthz` latency stays below 100 ms while a synthetic 10 s background job runs (extend `test_discovery_analysis_runs_off_the_event_loop` pattern); no inference runs when nothing changed (zero `infer_group` calls on a flat stream); an HA event triggers a group within one fast-loop tick;
  - simulator end-to-end replay with an injected clock across T01–T15;
  - restart recovery: kill mid-group, restart, the group is reconstructed from persisted evidence.
- **Size:** L

### WP-B7 — Missing-data, quality-gate and null-semantics sweep (§6.1, §18, test 17)
- **Objective:**
  - Quality report flags gate B2/B3 input: a `bad` channel is excluded and the exclusion is recorded.
  - Remove zero defaults:
    - `main.py:213-219` and `:401-460` (nullable rehydration);
    - `electrical/window.py:267,293` (`age_s` `None`, not 0);
    - `ingestion/correlation.py:543-545` (noise `None`, not 0);
    - `audit.py:1171-1172` (do not fabricate `received_at`).
  - Define the push-on-change "held vs stale vs missing" policy in one place (with B2).
- **OWNS:** `quality/engine.py`, `electrical/window.py`, `ingestion/correlation.py:527-550`.
- **SHARED:** `main.py:213-460` (rehydration helpers; coordinate with B6 and the storage area).
- **Depends on:** the domain area (nullable contract fields in `InferenceResult`/`Reconciliation`).
- **Tests:**
  - **acceptance test 17**: a reader silent past stale → its estimate and the Reader column are `null`/`stale` with a reason, never `0`, through API JSON;
  - a DB row with a null `power_w` round-trips as null;
  - a `bad`-quality channel never reaches the solver;
  - simulator: T14 missing_intervals, outage_windows.
- **Size:** S–M

### WP-B8 — Simulator + replay harness for the realtime pipeline
- **Objective:** Extend the simulator. Add:
  - HA `context` (`id` / `parent_id` / `user_id`) on state changes, including automation bursts sharing a parent context and a decoy context with non-simultaneous physics;
  - `last_reported` and push-on-change silence (no sample when the value is unchanged);
  - a per-reader `measurement_window_ms` with window-averaged values (a step smeared across one window);
  - late backfill: a sample delivered after the group's post-window;
  - out-of-order delivery;
  - clock-skewed readers;
  - an inverted CT with declared polarity.
  Then add a deterministic replay driver that feeds `EngineCore` through the injected clock (B6) and checks the stage outputs (§6 "inspectable and replayed").
- **OWNS:** `tools/simulator/*` (`sensors.py`, `scenario.py`, `labhouse.py`, `frames.py` truth keys), `tests/simulation/`, new `tests/realtime/`.
- **Depends on:** B6 clock injection, for the replay driver only. The simulator extensions can start immediately.
- **Tests:** these are the tests. The leak detector must cover the new truth keys (`true_onset_at`).
- **Size:** M

**Suggested order:** B1 ∥ B8 → B2 → B3 → B4 → B5 → B6. B7 can run any time after B1. B6 is the only WP that must land atomically with the matching area's `infer_group`.

---

## 5. Traps and constraints

1. **Event-loop blocking.**
   - Today the whole of `process()` (infer + train + audit + 6 h reanalysis) runs on the loop every 5 s. ADR-0033 measured this class of problem as 503s on `/healthz`, with Supervisor restarts.
   - `asyncio.to_thread` is safe for SQLite reads (`check_same_thread=False`, separate reader connection `storage/database.py:110-134`). Writes must stay single-writer (ADR-0004).
   - The `main.py:844-850` comment claiming `to_thread` raises `ProgrammingError` is stale. Verify it before relying on either claim.
2. **Cadence ≈ 15.7 s (ADR-0003); HB examples assume denser data.**
   - A 30 s pre-window holds about 2 samples, so per-event MAD over a pre-window is statistically meaningless.
   - σ must come from long-run per-reader stats (B2), not per-window.
   - Onset time is uncertain to ±1 cadence plus `measurement_window_ms`. Grouping tolerance must come from these bounds, not a fixed proximity (`GROUP_PROXIMITY_S=30`, `isolation.py:909`).
   - A provisional estimate arrives at least one cadence (up to ~30 s on slow channels) after the HA event. The §12.3 lifecycle must make that visible.
3. **Repeated-value semantics.**
   - HA fires `state_changed` only on change. Steady loads are silent: 8 of 38 channels in ADR-0015.
   - `repeated=True` is mostly set on reconnect `_prime` replays, not on genuine re-reads.
   - `StepTracker` collapses equal values (`events.py:543-548`), so flat channels never reach the 4-sample baseline. They fall back to the `previous_snapshot` two-point delta with σ = 0.
   - Subscribing to `state_reported` (HA ≥ 2024.3) would provide liveness without value change. This is a scope decision: event volume rises considerably. See Q3.
4. **Four time bases** (received, cycle-wall, state_timestamp, HA last_updated) coexist today.
   - The anchored rescan (`engine.py:1058-1089`) only works because of generous grace.
   - Changing to observed_at will move step times by up to about 5 s + cadence. Anchor tests (`test_event_anchor.py`) will shift.
5. **Polarity (suspected defect).** If confirmed, today's signed deltas on inverted CTs are wrong in production. Fixing it changes learned models trained on those channels; it may need a model invalidation migration (HB §16.6–7).
6. **Storage growth.** Undeclared HA events were 99.98 % of rows at ~1.5 GB/day (`main.py:884-893`). C8 must keep them in memory only, and persist only group members.
7. **Thread safety of published state.** API handlers already read `EngineCore` state in worker threads (`api/server.py:622-628`). Moving inference to a worker makes torn reads of `latest_results` more likely. Use immutable snapshot swaps.
8. **Perf budget.**
   - ~500 HA entities at ~30 events/s (`main.py:874-886`). The fast loop must stay O(1) per message.
   - Topology-observability lookups must be precomputed per topology version, not rebuilt per sample. Today `build_graph` runs every cycle (`engine.py:739`, `audit.py:1072`).
9. **Startup replay.** `_prime` replays the current snapshot with old `last_updated` (`main.py:933-941`). The new detector must treat primed values as baseline seeds, never as steps, and a group must not open on them.
10. **Learning maturation coupling.** `integration.py:654-659` matures events on `window_s + settle_s`. When B4 provides FINAL evidence, learning should consume FINAL events instead of re-deriving windows, so the two maturation clocks do not disagree.

---

## 6. Open questions for the product owner

1. **Should the periodic snapshot stay?** HB §15 forbids a full snapshot every second. Should a low-rate (e.g. 60 s) "current snapshot" recompute remain for HA entity freshness, or must the snapshot update strictly on events plus a staleness heartbeat? The answer affects how the HA integration's sensors age.
2. **Latency target for PROVISIONAL.** With a 15.7 s CT cadence the physics limits this to about one cadence. Is "first post-sample" acceptable (≈ 0–30 s), or is an HA-only provisional (state-based, before any CT confirmation) wanted?
3. **Subscribe to `state_reported`?** It gives true liveness for push-on-change sensors, at a higher event rate. The alternative keeps ADR-0015 behaviour (silent = stale after 120 s).
4. **Default pre/post windows per load class.** Are HB's 30 s / 120 s defaults to be kept given 15.7 s cadence? Who owns per-class tuning: config file, or UI?
5. **Unmapped HA entities as group members.** May the engine hold (in memory) HA events from candidate entities that the user has not linked, and use them as grouping and candidate evidence? This is a privacy and storage choice, separate from persistence.
6. **Raw event-window retention.** How long should raw high-resolution samples inside evidence windows be kept (§15 "defined retention policy"), versus aggregated-only?
7. **Polarity fix migration.** If the suspected polarity defect is confirmed, should existing learned models for inverted channels be invalidated and relearned, or marked "legacy semantics unknown" (HB §16.6)?
8. **Remove `WM_INFERENCE_INTERVAL_S`?** Removing it is a breaking config change for add-on users. Should it be deprecated with a warning, or removed outright?
