# Integration Status

Live record of how the independently developed workstreams fit together.
Updated by the orchestrator after each verification run.

**Last updated:** 2026-11-06 — all workstreams merged; **7/7** verification gates
PASS; `pytest tests` **737 passed**.

## Wave 1 — parallel implementation

| Workstream | Owner | Delivered files | Status |
| --- | --- | --- | --- |
| Contracts + storage + config | Orchestrator | `contracts.py`, `config/`, `storage/` | VERIFIED |
| Environment audit | Orchestrator | `tools/audit/*`, `data/*.json`, `docs/ha_environment_audit.md` | VERIFIED |
| Electrical + disaggregation | Agent E | `electrical/`, `disaggregation/` | VERIFIED — 87 tests |
| Engine core (ingestion/API/quality) | Agent D | `ingestion/`, `synchronization/`, `api/`, `quality/`, `diagnostics/`, `main.py` | VERIFIED — live run OK |
| Learning + models + aggregation | Agent F | `learning/`, `models/`, `aggregation/` | VERIFIED — 3 suites + production wiring |
| HA integration | Agent C | `custom_components/wattmosaic/**` | VERIFIED — 202 tests + stub-HA smoke |
| Simulator + QA | Agent I | `tools/simulator/`, `tests/simulation`, `tests/end_to_end` | VERIFIED — 58 + 44 tests |
| App/add-on | Agent H | `addon/`, `Dockerfile`, `docker-compose.yml`, `scripts/` | VERIFIED — real `docker build` |

## Live-environment integration checks (orchestrator-run)

| Check | Result | Evidence |
| --- | --- | --- |
| Config loads from real `.env` | PASS | `--check-config` → `ok: true`, token redacted |
| Engine connects to production HA | PASS | `connected: true`, `ha_connected: true` |
| Ingestion is read-only | PASS | only `get_states`, registry list, `subscribe_events` |
| Samples ingested | PASS | 341 samples, 57 power channels tracked |
| Events ingested | PASS | 7049 state events |
| Database opens + migrates | PASS | `database: ok`, schema at latest version |
| Inference backend attached | PASS | `inference_backend: WattMosaicBackend` |
| Inference produces results | PASS | 28 devices, contract-valid sources |
| Residual never redistributed | PASS | `main = direct + inferred + model + unknown` exactly |
| Graceful shutdown | PASS | `engine stopped`, no ERROR lines |
| Event detection in production | PASS | `StepTracker` wired; regression tests added |
| Passive real-HA validation | PASS | `docs/real_ha_validation.md`, 37 channels / 16 min |

### Live numbers from the read-only run

- Main measured power: **13 256.6 W**
- Directly measured: **3 909.0 W**
- Inferred: **47.9 W**
- Model estimated: **0.0 W** (no trained models yet)
- Unknown residual: **9 299.7 W** (70.1 % unattributed, preserved as unknown)
- Accounted fraction: **0.298**
- Devices reported: **28**

The high unknown residual is the honest result: most load is on CTs whose
devices have no model yet. It is *not* redistributed to devices.

## Production wiring (added after the live run)

The live run above exercised ingestion → inference → API. It did **not** exercise
energy aggregation or learning, because neither was constructed. Both are now
wired in `WattMosaicBackend` and proven end-to-end against the real `EngineCore`.

| Capability | Verified by |
| --- | --- |
| Energy buckets + monotonic lifetime total (`energy_kwh`) | `tests/unit/test_aggregation.py`, `test_results_carry_a_running_cumulative_energy_total` |
| Crash recovery of incomplete buckets (`recompute_incomplete_buckets`) | restart tests in `test_aggregation.py` |
| Sample collect → filter → persist → train → promote | `test_production_loop_trains_and_promotes_a_model` |
| Event maturation relative to **event** time, not wall clock | `test_learning_waits_for_the_post_event_window` |
| Declarative entity↔device linking, ambiguity surfaced | `tests/unit/test_device_links.py` |
| `device_states` keyed by `device_id` (was HA entity id) | `test_device_states_are_keyed_by_device_id` |
| Learning pause/resume reaches the backend | `test_pause_learning_command_reaches_the_backend` |
| Candidate topology collects but never trains | `test_candidate_topology_collects_but_never_trains` |

Measured production-loop run (real `EngineCore` + simulator, 20 min):
37 devices publish energy, all series monotonic, `energy_updates` 2960.
Learning run (180 min): 175 accepted samples, 1 training attempt, 1 model
promoted.

### Why entity↔device linking is declarative

The audited topology's channels name **power** entities (`sensor.*_power`), but
HA emits state changes on **state** entities (`switch.*`, `climate.*`). A device
declares those in `DeviceTopology.metadata["state_entity_ids"]` (evidence-derived,
ADR-0011), `config_load_entity_ids` (configuration) or
`link_state_entity_ids` (resolved from a `device_links` entry, ADR-0028). Name
similarity and HA areas are deliberately **not** used: a guessed association
would fabricate attribution. The HA device *registry* is consulted, but only by
the integration, and only to expand a device id the user themselves declared into
that device's state entities — never to guess one. When two devices claim one
entity, `ambiguous_entities()` reports it and the sample is rejected rather than
resolved.

Consequence, and it is intended: a topology with **no** runtime state entity of
any of those three kinds cannot link real HA events, so no real learning sample
can mature. An unresolvable `device_links` entry is reported in diagnostics under
`devices_without_state_entity` with a reason code.

### Evidence-derived mapping (now populated for 8 of 28 devices)

Instead of guessing, the mapping was measured. `tools/audit/derive_state_entities.py`
replays 7 days of read-only HA recorder history (`history/history_during_period`)
and scores every (CT channel, state entity) pair with a **state-gated level test
graded by balanced accuracy** — not raw agreement, because the base rate would let
a constant "off" predictor score ~0.95. The sweep adds a 0–600 s lag grid (cloud
round-trip), a relocation null, a coverage floor, a circuit-recall floor, a
circularity exclusion (templated power sensors), a marginal-effect cross-tab for
sole-carrier resolution and confound detection, and per-CT duplicate-channel
classification.

Result: **8 supported, 0 weak, 3 confounded, 0 ambiguous, 17 unsupported**, plus 6
duplicate-channel findings and 3 polarity candidates. The full table, gates and
per-device evidence are in `PROJECT_STATUS.md` §3b; `tools/audit/build_topology.py`
consumes the JSON and attaches `state_entity_ids` only for `supported`/`weak`
devices with exactly one entity and no `duplicate_of`. `topology_status` stays
`candidate`.

### Event intake (defects 16–20)

A passive real-HA run then showed that collecting samples still failed for a
different reason: undeclared entities flooded the bounded learning buffer, so
every declared event was evicted before it could mature. Four further defects
followed (§5, #16–#20). The intake rule is now explicit:

- only entities a device declares reach inference or learning;
- undeclared entities are counted (`unlinked_events_ignored`) and **not**
  persisted — writing them grew `device_events` at ~1.5 GB/day for data nothing
  reads;
- a first observation establishes state but is **not** a transition (the startup
  snapshot replays old `last_updated` timestamps);
- repeated identical state is not a transition;
- buffer eviction is counted (`events_evicted_before_maturity`), never silent;
- a sample with no declared CT carries no CT and is rejected `missing_measurement`
  rather than being credited to an arbitrary circuit.

## Defects found by integration (all fixed)

| # | Defect | Fix |
| --- | --- | --- |
| 1 | Graceful shutdown logged `ConnectionClosedOK` as ERROR | clean close treated as normal |
| 2 | Energy (kWh) entities classified as power (W) | quality engine kind separation |
| 3 | Duplicate sources at identical 6.420 W not grouped | grouped duplicate detection |
| 4 | `PUT /topology {}` returned 200 not 400 | payload validation |
| 5 | Event detection dead in production (no history) | `StepTracker` in `integration.py` |
| 6 | `argparse` used but not imported | import added |
| 7 | `Sequence` used but not imported | import added |
| 8 | Add-on staging failed (missing parent dirs) | `mkdir -p` in `copy()` |
| 9 | Add-on `list(...)` schema check parsed wrong | fixed parser |
| 10 | `GET /api/v1/quality` → 500: `self.quality` shadowed `EngineCore.quality()` | renamed to `self.quality_engine` |
| 11 | `GET /api/v1/status` → 500: `_jsonable()` returned raw `datetime`/`Enum` | recursive serialiser |
| 12 | Wall-clock `flush()` dropped a live bucket → energy total could fall | only drop when `complete` **and** `last_at >= end` |
| 13 | `load_topology()` lost `topology.status` → confirmed topology reloaded as `candidate`, silently disabling learning after restart | read status/created_at from `topology_versions` |
| 14 | `_maybe_train` armed its throttle before any samples existed → training suppressed for an hour after data arrived | stamp only when a device is ready |
| 15 | `direct_exceeds_main_records` counted one record twice | removed duplicate count |
| 16 | Undeclared entities flooded the 512-event learning buffer → every declared event evicted 4× before maturity (1 439 samples, all `missing_measurement`) | intake filtered to declared links; unlinked counted, not buffered |
| 17 | `SampleCollector.build()` credited an unlinked event to an arbitrary CT (fabricated attribution) | fallback removed; no declared CT ⇒ `supporting_ct_ids=[]` |
| 18 | `AlignedWindow` had no `previous_states`/`state_transitions` → `_collect_transitions` always returned `{}`; state-transition inference was dead in production | fields added and populated each inference cycle |
| 19 | Buffer eviction was silent data loss | `events_evicted_before_maturity` counter in `/status` |
| 20 | Startup snapshot replay fabricated transitions from hours-old `last_updated` values | first observation is not a transition; repeated state is not a transition |
| 21 | Persisting every undeclared event grew `device_events` ~1.5 GB/day (99.98 % of rows) — unbounded growth on an unattended run | undeclared events counted, not persisted; bloat purged (82 MB → 23 MB) |

Defects 10–21 were all invisible to unit tests and only appeared when the real
components ran together. Each now has a regression test that fails if it returns.

## Interface conformance

Every component imports `wattmosaic_engine.contracts`. Verified by grep: no
module defines a parallel `InferenceResult` / `Measurement` / `Topology` class.
Contract version `1.4.0`
(`Reconciliation.topology_confirmed`, additive, plus the 1.2.0 device taxonomy +
evidence tiers); schemas regenerate byte-identically (`contract_schema_sync` gate
PASS).

## Known integration gaps

1. `model_estimated` is 0 against real data — no model has been trained on the
   real household (and cannot be until the topology is confirmed).
2. Per-device accuracy is **unverified**: no household ground truth exists.
3. Topology remains `candidate`; nothing auto-confirms it (ADR-0008).
4. `state_entity_ids` is populated for 8 of 28 devices from measured evidence; the
   remaining 20 cannot be linked honestly (3 confounded, 2 reversed polarity, 2 no
   state events, 2 duplicate channels, 11 with no describing state entity).
5. 13 of 37 devices share branch CTs and cannot be separated by this topology;
   their power is preserved as unknown residual.
6. **No `DeviceConfiguration` has been written to the live engine.** Every device
   is `unknown` on all three taxonomy axes, so behaviour is identical to
   pre-ADR-0011. The configuration surface (persistence, projection, command,
   options flow, tiers) is verified by unit and engine tests only.
7. **Tier accuracy is unverified, not tier derivation.** Tiers 0–4 are proven to
   be assigned correctly from evidence (18 unit tests plus hand-run engine
   checks). No claim is made that a higher tier is empirically more accurate in
   this household, and no aggregate accuracy score is produced.
8. A device declared `shared` with unmodelled siblings is reported as a
   **configured prior** (tier 1), never as a measurement. Without a configured
   `on_w` such a device stays `unknown`: the CT row is an upper bound, and
   splitting it by guesswork is forbidden.
