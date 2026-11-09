# WattMosaic Development Lifecycle

Authoritative, continuously updated record of planning, delegation, integration and
verification. If it is not recorded here with evidence, it does not count as done.

**Last updated:** 2026-11-08
**Commit:** `5c766bd` (`dev`, 0.2.0-dev4 deployed to the live installation)

---

## 1. Environment facts (verified, read-only)

> **Deliberately generic.** The audit was run against a real Home Assistant, but
> the instance fingerprint is *not* published: a version, time zone, entity count
> and area count together identify a specific household. The concrete audit
> artifacts live in git-ignored `data/*.json` on the operator's machine. What is
> published is the shape of the problem, in `data/examples/reference_house/`.

| Fact | Value | Evidence |
| --- | --- | --- |
| HA URL | operator's host, `:8123` (not published) | `curl /api/` → 401 without token, 200 with |
| HA version | 2026.11.0 | `GET /api/config` |
| HA state | RUNNING, safe_mode=false | `GET /api/config` |
| Installation | Home Assistant OS, YAML-free, integration-driven | `GET /api/config` |
| Power-measuring integration | multi-channel CT energy monitor (2 panels) | registry audit |
| CT power channels | 36 | `data/ha_environment_audit.json` (local) |
| Median CT state-update interval | **15.7 s** | `data/ct_update_profile.json` (local) |
| `two_second_assumption_supported` | **false** | same |

Hardware topology discovered (as **candidate**, not committed):

- Panel A — CT1/CT2 mains + 16 named branch channels.
- Panel B — CT1/CT2 + 16 named branch channels.
- A separate feed CT pair (CT1/CT2/total) — a third measurement group.
- Double-pole patterns: `*_ct1_power` / `*_ct2_power` for 240 V loads, inferred
  from naming only and therefore never trusted until corroborated.

The published equivalents are `data/examples/reference_house/` (a structurally
identical synthetic household) rendered as `docs/ha_environment_audit.md` and
`docs/real_ha_validation.md`.

---

## 2. Workstreams and status

Status legend: `TODO` / `IN_PROGRESS` / `IMPLEMENTED` / `VERIFIED` (tests run) /
`BLOCKED`.

| # | Workstream | Owner | Status | Evidence |
| --- | --- | --- | --- | --- |
| 0 | HA environment audit | Orchestrator | VERIFIED | `docs/ha_environment_audit.md`, `data/*.json`, 16-min passive run |
| 1 | Contracts + DB schema | Orchestrator | VERIFIED | `contracts.py`, `storage/`, schema-sync gate PASS |
| 2 | Engine core (ingestion/API/quality/diagnostics) | Agent D | VERIFIED | `tests/unit/test_ingestion.py`, `test_api.py`, `test_quality.py`, `test_synchronization.py` |
| 3 | Electrical model + solver | Agent E | VERIFIED | `tests/unit/test_electrical.py` (51), `test_disaggregation.py` (36) |
| 4 | Learning + models + aggregation | Agent F | VERIFIED | `tests/unit/test_learning.py`, `test_models.py`, `test_aggregation.py`; production wiring in `integration.py` exercised end-to-end |
| 5 | HA integration | Agent C | VERIFIED | `tests/integration/test_ha_integration.py` (202), stub-HA smoke |
| 6 | Add-on / packaging | Agent H | VERIFIED | `scripts/validate_addon.sh` 62 passed, real `docker build` OK |
| 7 | Simulator + test suite | Agent I | VERIFIED | `tests/simulation/` (58) incl. stratified accuracy; `tests/end_to_end/` (44) |
| 8 | Documentation | Agents + Orchestrator | VERIFIED | `docs/*` incl. `acceptance_report.md`; link check clean |
| 9 | Integration + E2E verification | Orchestrator | VERIFIED | **7/7** required gates PASS; `pytest tests` 871 passed |
| 10 | Device taxonomy + tiers + per-device config | Orchestrator | VERIFIED | `contracts.py` v1.1.0, `disaggregation/{tiers,configuration}.py`, `entity_evidence.py`; `tests/unit/test_device_taxonomy.py` (51) |

Parallelisation rule: subagents received **disjoint file scopes** plus the
authoritative `contracts.py`. No subagent redefined a shared schema. Every
subagent report was independently re-verified by the orchestrator against the
real engine; three of them contained defects that "tests pass" did not reveal
(see §5, #19–#22).

---

## 3. Locked architectural decisions

See `docs/decisions/README.md` (ADR-0001…ADR-0014). Summary:

| ID | Decision |
| --- | --- |
| D1 | Engine owns all inference and learning |
| D2 | Engine subscribes to HA over WebSocket, read-only |
| D3 | Integration is a thin REST client + entity publisher |
| D4 | SQLite WAL + versioned migrations |
| D5 | Constrained NNLS / interpretable models, no deep learning |
| D6 | `last_updated` ≠ new measurement; repeated values deduped |
| D7 | Unknown residual is a first-class output, never redistributed |
| D8 | Confidence is an engineering score, not calibrated probability |
| D9 | Unconfirmed topology stays `candidate` until user confirms |
| D10 | Evening window timings derived from measured 15.7 s cadence |
| D11 | Devices have three orthogonal axes: HA connectivity, monitoring level, power behaviour |
| D12 | Mode entities are context only; a declared load signal proves runtime |
| D13 | Confidence tiers grade evidence kind; the 0–1 score stays, neither is calibrated |
| D14 | A power entity is measurable, not state-bearing; only runtime entities write device state |

---

## 3a. Production wiring (was the largest functional gap)

Unit tests passed while the engine had **no** energy aggregator and **no**
learning pipeline constructed, so `energy_kwh` was always `None` and
`learning_active` advertised a loop that did not exist. This section records
what was wired and how it was verified.

| Capability | Where | Verified by |
| --- | --- | --- |
| Energy aggregation (per-bucket + lifetime total) | `WattMosaicBackend._update_energy`, `EnergyAggregator` | `tests/unit/test_aggregation.py`, `test_results_carry_a_running_cumulative_energy_total` |
| Crash recovery of half-written buckets | `EnergyAggregator.recompute_incomplete_buckets(24 h)` | restart tests |
| Learning pipeline (collect → filter → persist → train → promote) | `WattMosaicBackend._learn` / `_maybe_train` | `test_production_loop_trains_and_promotes_a_model` |
| Event maturation relative to event time | `_learn` (`window_s + settle_s`) | `test_learning_waits_for_the_post_event_window` |
| Declarative entity ↔ device linking | `engine/wattmosaic_engine/device_links.py` | `tests/unit/test_device_links.py` (13) |
| `device_states` keyed by `device_id` | `EngineCore.on_event` | `test_device_states_are_keyed_by_device_id` |
| Learning pause/resume reaches the backend | `EngineCore.command` | `test_pause_learning_command_reaches_the_backend` |
| Evidence-derived `state_entity_ids` + polarity | `tools/audit/derive_state_entities.py` → `data/topology_candidates.json` | `tests/unit/test_state_entity_mapping.py` (33) |
| Event intake restricted to declared links | `EngineCore.on_event` (`device_for_entity`) | `tests/end_to_end/test_event_intake.py` (10) |
| Declared transitions reach inference | `AlignedWindow.state_transitions` / `previous_states` | `test_transitions_survive_coercion_and_reach_the_engine` |
| No fabricated attribution without a declared CT | `SampleCollector.build` | `test_unlinked_event_is_not_attributed_to_a_random_circuit` |

Design point that made the failure invisible for so long: the topology's
channels name **power** entities only, while HA events arrive on **state**
entities (`switch.*`, `climate.*`). A device therefore declares its state
entities in `DeviceTopology.metadata["state_entity_ids"]`. The link is
**declarative only** — names, HA areas and the HA device registry are never used
to guess it, because a guessed association would fabricate attribution
(`ambiguous_entities()` surfaces conflicts instead of resolving them).

Confirmed topology vs. candidate topology is the learning gate: a `candidate`
topology still *collects* samples (so the rejection reason is visible) but every
sample is rejected as `ambiguous_topology` and nothing is trained. That is
correct behaviour, not a bug.

---

## 3b. Evidence-derived state-entity mapping (real HA, read-only)

The audited topology named only **power** entities, so no device could be linked
to the `switch.*`/`climate.*` entity that says whether it is on. Guessing that
link by name similarity is forbidden (a guessed association fabricates
attribution), so it was derived from evidence instead.

Method (`tools/audit/derive_state_entities.py`, 7 days of read-only HA recorder
history via `history/history_during_period`):

1. **State-gated level test.** For each (CT channel, state entity) pair, compare
   CT power while the entity is *on* against while it is *off*.
2. **Balanced accuracy** `(sensitivity + specificity) / 2` — raw agreement is
   dominated by the base rate (almost every appliance is off almost always), so
   a constant "off" predictor would score ~0.95 and mean nothing.
3. **Lag sweep** 0–600 s at 60 s grid: some appliances report state only after a
   cloud round trip, so the aligned lag is measured, not assumed.
4. **Coverage** — the shift must account for a real share of that CT's power, not
   a wart on a busy circuit.
5. **Relocation null** — the same test on a shifted window must *not* reproduce
   the effect.
6. **Circularity check** — power sensors that are templates of the CT itself
   are excluded, otherwise the test correlates a signal with itself.
7. **Marginal-effect cross-tab** for sole-carrier resolution and **confound
   detection** (additive multi-device circuits score well on every member and
   cannot be separated).
8. **Duplicate-channel detection** per CT channel, evidence only, never merged.

Gates: `MIN_COVERAGE=0.50`, `MIN_CIRCUIT_RECALL=0.85` (supported 0.90),
`MIN_SEPARATION_W=20`, `MIN_NULL_MARGIN=0.10`, supported balanced accuracy 0.90,
weak 0.85.

Result on the operator's installation — **8 supported, 0 weak, 3 confounded,
0 ambiguous, 17 unsupported** out of 28 devices. The identities are not
published; what matters is the *shape* of the outcome, because it is the honest
capability limit of a name-blind association:

| Outcome | Count | What it means |
| --- | --- | --- |
| supported | 8 | one entity explains ≥90 % of the circuit's high periods and ≥50 % of its level |
| confounded (additive multi-device) | 3 | the circuit carries two independently switchable loads; unmappable by design |
| rejected: `insufficient_circuit_recall` | 1 | the candidate is specific but covers only ~56 % of the circuit |
| rejected: `insufficient_coverage` | 1 | candidate tracks the circuit but adds only ~11 % of its level |
| rejected: `duplicate_of` | 1 | two CT channels measure the same conductor |
| rejected: `channel_has_no_events` | 2 | the circuit never steps; no behavioural evidence exists |
| rejected: `channel_polarity_suspect` | 2 | the clamp reads negative without any generation source |

Duplicate-channel findings (recorded as evidence, nothing merged):

| Class | Count | Evidence pattern |
| --- | --- | --- |
| `same_current` | 1 pair | r 0.845, relative IQR 0.0093, 0.277 W residual — one conductor, two clamps |
| `double_pole_legs` | 2 pairs | r 1.000 and 0.998 — genuine 240 V leg pairs |
| `double_pole_legs_uncorrelated` | 1 pair | r 0.282 / −0.002 — the naming pattern lied; these are separate loads |
| `correlated_activity` | 1 pair | r 0.838 but relative IQR 0.456 — co-varying distinct circuits |

Polarity: two channels read negative 98.8 % and 92.2 % of the time and the
installation has no PV/solar entity, so `Polarity.INVERTED` is applied from that
evidence rather than from a name.

The mapping is loaded by `tools/audit/build_topology.py`, which attaches a
`state_entity_ids` entry **only** for `supported`/`weak` devices with exactly one
entity and no `duplicate_of`, and writes per-device `state_overrides` for
controllers whose "on" vocabulary differs from the global `ON_STATES` (a
water heater that reports `Heating`/`Cooling`/`Idle` is the canonical case: the
global set contains `cooling` but the *low-power* state is the one labelled
cooling). `topology_status` stays `candidate` — only the user can confirm it.

The tool reads `data/entity_registry_slim.json` (`entity_id` → `platform` /
`device_id`), not the full HA registry dump: the raw dump is ~5.6 MB of MAC
addresses, serial numbers and internal IPs, and only those three fields are ever
used. Both are gitignored; the slim derivative is reproducible from a live
read-only run.

Installed into `data/wattmosaic.db` as topology **version 2** (candidate): 38
channels, 28 devices, 8 state links, 3 inverted channels, `entity_links()`
resolving 40 entities, `ambiguous_entities()` empty.

---

## 3c. Passive real-HA run (read-only, left running)

A read-only engine run records against the live installation so the user can
inspect real behaviour later. It sends no service calls and writes nothing to HA.

| Item | Value |
| --- | --- |
| Start | `.venv/bin/python -m wattmosaic_engine.main --no-json-logs --log-level INFO` |
| Log | `data/passive/engine.log` |
| Database | `data/wattmosaic.db` |
| API | `http://127.0.0.1:8787/api/v1/status` |
| Stop | `kill <pid>` (SIGTERM; the engine shuts down gracefully) |

What to check in `/api/v1/status`:

- `ingestion.unlinked_events_ignored` grows at ~30/s — normal; those entities are
  not declared by any device. They are counted, **not** persisted: writing them
  grew `device_events` at ~1.5 GB/day (99.98 % of all rows) for data nothing
  reads, which would fill the disk on an unattended run.
- `ingestion.events_evicted_before_maturity` must stay ~0. A non-zero value means
  declared events are being dropped before they can produce a sample.
- `inference.backend_status.samples_collected` grows as declared appliances
  switch. Each new sample carries a real `supporting_ct_ids` and a real
  `baseline/event/delta` power triple.
- `rejection_reason` is expected to be `ambiguous_topology`: learning cannot
  accept a sample until the user confirms the topology (`POST
  /api/v1/topology/confirm`). That is the gate working, not a failure.

The engine runs from the repository virtualenv; it does **not** survive a reboot
and holds no system service. Restarting it loses in-memory CT history (bounded
per-process by design), not stored data.

Disk growth while recording is ~400 MB/day, almost entirely `inference_results`
(one row per device per cycle) and `ct_samples`. This is the product's actual
output, not noise, and it is bounded by `WM_RETENTION_DAYS_RAW` (default 30 days)
which the maintenance loop enforces every 60 s. To cap a long unattended run
lower, start it with e.g. `WM_RETENTION_DAYS_RAW=7`. Aggregates are kept far
longer (`WM_RETENTION_DAYS_AGGREGATES`, default 730 days) because they are small
and are what a model would train from.

---

## 3d. Device taxonomy, confidence tiers and per-device configuration

The three axes the user asked for (ADR-0011) are implemented end to end. A
device is described simultaneously by HA connectivity, monitoring level and power
behaviour; none is inferred from a name.

| Capability | Where | Verified by |
| --- | --- | --- |
| Taxonomy enums + configuration contracts | `engine/wattmosaic_engine/contracts.py` (v1.1.0) | `tests/unit/test_contracts.py`, `test_device_taxonomy.py` |
| Cross-axis validation (`not_connected` forbids state entities, `complex` needs a controller, `light` only for lights) | `DeviceConfiguration._consistent_axes` | `test_device_taxonomy.py` (8 contract tests) |
| Monitoring derived from CT **co-occupancy** | `disaggregation/configuration.derive_monitoring` | `test_monitoring_is_derived_from_cooccupancy_not_ct_type`, `test_branch_shared_by_two_devices_is_shared` |
| Configuration projected into topology metadata (single source of truth) | `configuration.project_configuration` | `test_removing_configuration_restores_original_metadata` |
| Evidence-owned `state_entity_ids` never rewritten | `config_load_entity_ids` key | `test_projection_does_not_rewrite_evidence_derived_state_entities` |
| Mode-vs-load entity classification | `engine/wattmosaic_engine/entity_evidence.py` | `test_mode_domains_are_never_load_evidence`, `test_runtime_domains_are_load_evidence` |
| Deterministic reclassification of a `climate` entity listed as a state entity | `configuration._split_entities` | `test_mode_entity_listed_as_state_entity_is_reclassified` |
| Shared circuit with unmodelled siblings = **upper bound**, slack column, level-2 blocked | `engine._infer_device`, `solver.augment_with_unknown_slack(shared_rows=…)` | `test_shared_circuit_does_not_credit_one_lamp_with_the_whole_row` |
| Configured prior path (`Source.CONFIGURED_PRIOR`, Level 3b) | `engine._infer_device` | `test_shared_circuit_…`, `test_off_device_prior_is_not_applied`, `test_mode_only_device_never_reports_its_expected_watts` |
| Measurable vs state-bearing entities (a CT power entity never becomes device state) | `device_links.runtime_state_entities`, `state_entity_for_device`, `EngineCore.on_event` | `test_a_power_entity_cannot_overwrite_the_device_runtime_state`, `test_state_key_never_maps_a_power_entity_to_device_state` |
| User-declared load entities actually reach ingestion | `device_links.entity_links` reads both `state_entity_ids` and `config_load_entity_ids` | `test_user_declared_load_entities_are_linked_and_are_state_entities` |
| Evidence tiers (0–4) independent of the score | `disaggregation/tiers.py` | 18 tier tests incl. `test_grade_never_rewrites_the_score` |
| Tier persistence | migration 5 on `inference_results` | verified on a copy of the real 75 MB DB |
| Tier vocabulary endpoint | `GET /api/v1/confidence_tiers` | `tests/unit/test_device_config_api.py` |
| Per-device configuration persistence + command | migration 4, `EngineCore._set_device_configuration` | `tests/unit/test_storage_config.py` (6) |
| HA options flow (`device_config` → `device_config_details`, opt-in) | `custom_components/wattmosaic/{config_flow,flow_logic,const}.py` | `tests/unit/test_config_flow_logic.py` (25) |

Two properties are load-bearing and pinned by tests:

1. **A declaration is never evidence.** `Source.CONFIGURED_PRIOR` is separate from
   every measurement source, caps at Tier 1, and never outranks a CT. Removing a
   configuration restores metadata exactly.
2. **A mode never creates watts.** A thermostat that only reports a setpoint mode
   (a `heat_cool`-style state present in the overwhelming majority of recorded
   rows) yields `unknown` / Tier 0 for the device's power and leaves it in the
   residual — a mode is context, never a wattage.

---


Append-only. Each entry: date, what was run, result.

- 2026-11-05 — HA audit against a live system (read-only); 58 power entities, 36
  CT channels, registry via WebSocket (REST registry endpoints 404 on this build).
- 2026-11-05 — CT profile: median 15.7 s, 5 channels > 30 s, 1 channel < 1 s.
- 2026-11-05 — Passive collector, 45 min, read-only `subscribe_events`.
- 2026-11-05 — Full suite: **530 passed** (`pytest tests`).
- 2026-11-05 — `ruff check engine tools tests custom_components addon`: clean.
- 2026-11-05 — Live read-only engine run: `inference_backend=WattMosaicBackend`,
  `ha_connected=true`, 341 samples / 7049 events, 57 channels, 28 inference
  results, clean shutdown, no ERROR lines.
- 2026-11-05 — Live reconciliation self-check: `main_power =
  direct + inferred + model + unknown` exactly; residual preserved (9299.7 W of
  13256.6 W unattributed), no double counting.
- 2026-11-05 — `scripts/validate_addon.sh`: **62 passed, 0 failed**, including a
  real `docker build` of the add-on context.
- 2026-11-05 — Passive validation, 16 min read-only: 37 channels observed,
  report at `docs/real_ha_validation.md`.
- 2026-11-05 — `tools/dev/verify.py`: **6/7 required gates PASS**; only
  `end_to_end_tests` outstanding (`tests/end_to_end/` not yet created).
- 2026-11-06 — Production wiring verified through the real `EngineCore` +
  simulator: 37 devices publish energy, every series monotonic,
  `energy_updates` 2960. Learning chain then verified: 175 accepted samples,
  1 training attempt, 1 model promoted.
- 2026-11-06 — Two API defects fixed (see §5, #10–#11). Both strict `xfail`
  markers removed; they are plain regression tests now.
- 2026-11-06 — Three production-only defects fixed (see §5, #12–#14), each with
  a regression test that fails if the behaviour is lost.
- 2026-11-06 — `tools/dev/verify.py`: **7/7 required gates PASS**.
- 2026-11-06 — `pytest tests`: **737 passed**, 0 failed, 0 xfailed.
- 2026-11-06 — `ruff check engine tools tests custom_components addon`: clean.
- 2026-11-06 — `check_secrets.py`: clean. `check_manifests.py`: ok.
- 2026-11-06 — Stratified accuracy measured (seed 202 / 60 min / 15 s / good)
  and written to `docs/acceptance_report.md`: coverage 0.7854, false
  attribution 0.0089, residual preserved 0.9792, direct MAE 22.55 W, inferred
  MAE 26.06 W, peak-concurrency MAE 122.86 W, nested double counts 0/213.
- 2026-11-06 — Evidence mapping derived from 7 days of read-only HA recorder
  history: **8 supported, 0 weak, 3 confounded, 0 ambiguous, 17 unsupported**;
  6 duplicate-channel findings; 3 polarity candidates. Written to
  `data/state_entity_candidates.json`; topology rebuilt as version 2 (candidate,
  8 state links, 3 inverted channels).
- 2026-11-06 — `pytest tests`: **782 passed**, 0 failed.
- 2026-11-06 — `ruff check .` clean; `check_secrets.py` clean.
- 2026-11-06 — Four event-intake defects found by the passive real-HA run and
  fixed (§5, #16–#20), each pinned by `tests/end_to_end/test_event_intake.py`.
- 2026-11-06 — Passive read-only run restarted with the intake fixes; first
  mature sample observed on a dedicated branch CT (78.16 W → 7.07 W, −71.09 W),
  rejected `ambiguous_topology` as designed. `unlinked_events_ignored` climbing,
  `events_evicted_before_maturity` 0.
- 2026-11-06 — **Scope expansion: device taxonomy, confidence tiers and
  per-device configuration** (ADR-0011/0012/0013). Contracts bumped to
  **v1.1.0** (19 schemas). New modules: `disaggregation/tiers.py`,
  `disaggregation/configuration.py`, `entity_evidence.py`. Migrations **4**
  (`device_configurations`) and **5** (tier columns on `inference_results`).
  New read endpoint `GET /api/v1/confidence_tiers`; `/api/v1/devices` carries
  `configuration` + `confidence_tier`. HA options flow gained the opt-in
  `device_config` → `device_config_details` branch (default off).
- 2026-11-06 — Nine defects found while wiring the new scope (§5, #22–#27 plus
  the three subagent defects re-verified below). Every one was found by
  hand-checking real engine behaviour, **not** by the subagent's "tests pass"
  report.
- 2026-11-06 — `pytest tests`: **871 passed**, 0 failed. `ruff check .` clean.
  `check_secrets.py` clean.
- 2026-11-06 — Migrations 4 and 5 verified on a **copy of the real 75 MB
  `data/wattmosaic.db`**: `user_version=5`, all 28 device results preserved,
  `device_configurations` present and empty.
- 2026-11-06 — Four more defects found by probing the real runtime, not by the
  suite (§5, #29–#31): a CT power entity overwriting every device's runtime
  state, the per-device configuration feature being inert in the ingestion path,
  and a mode-only device reporting its full `on_w` for *any* state including
  `off`. `pytest tests`: **878 passed**, 0 failed. `ruff check .` clean, secret
  scan clean, `tools/dev/verify.py` **7/7**.

---

## 5. Defects found by integration (not by unit tests)

These were only visible when components were wired together, and each was
fixed and re-verified.

| # | Defect | Found by | Fix |
| --- | --- | --- | --- |
| 1 | Graceful shutdown logged `ConnectionClosedOK` as ERROR | live run | treat clean WS close as normal (`ha_source`) |
| 2 | Energy (kWh) entities classified as power (W) | live run | quality engine unit/kind separation |
| 3 | Duplicate sources at identical 6.420 W not grouped | live run | grouped duplicate-source detection |
| 4 | `PUT /topology {}` returned 200 instead of 400 | API contract test | payload validation |
| 5 | Event detection dead in production (no history) | Agent E report + review | `StepTracker` wired in `integration.py` + regression tests |
| 6 | `argparse` used but not imported (`--main`) | ruff `F821` | import added |
| 7 | `Sequence` used but not imported in promotion | ruff `F821` | import added |
| 8 | Add-on staging failed (`cp -R` no parent dirs) | `validate_addon.sh` | `mkdir -p` in `copy()` |
| 9 | Add-on `list(...)` schema check parsed wrong | `validate_addon.sh` | read allowed values from full decl |
| 10 | `GET /api/v1/quality` returned 500: `self.quality` shadowed `EngineCore.quality()` | end-to-end API test | renamed to `self.quality_engine`, call sites updated |
| 11 | `GET /api/v1/status` returned 500: `_jsonable()` returned raw `datetime`/`Enum` | end-to-end API test | recursive serialiser with ISO/`Enum` handling |
| 12 | `flush(now=wall-clock)` popped a live bucket, later re-opened it empty → energy silently lost (total fell) | production run trace | only drop a bucket when complete **and** `device.last_at >= end` |
| 13 | `load_topology()` discarded `topology.status` → a user-confirmed topology reloaded as `candidate`, permanently rejecting every learning sample after a restart | production learning run | read status/created_at from `topology_versions` |
| 14 | `_maybe_train` stamped its hourly throttle on the first cycle, before any samples existed → training suppressed for an hour *after* data arrived | production learning run | only stamp when at least one device is actually ready |
| 15 | `direct_exceeds_main_records` counted the same record twice (two overlapping conditions), inflating a reported acceptance metric | metrics review + ruff `F841` | removed the duplicate count; the retired variable was dead code |
| 16 | Undeclared HA entities flooded the 512-entry learning buffer. At 31.7 events/s the 68 s maturity horizon needs ~2 150 buffered events; the buffer spanned ~16 s, so **every** event was evicted ~4× too early and **no** declared event could ever mature (1 439 samples, all `missing_measurement`) | passive real-HA run | `on_event` filters through `device_for_entity`; undeclared entities are counted (`unlinked_events_ignored`) and kept out of inference/learning |
| 17 | `SampleCollector.build()` fell back to "whatever CTs appear in the window", crediting an unrelated lamp event with a 5 W delta against `ct_water_heater` — fabricated attribution | reproduction before/after | fallback deleted; an unlinked event yields `supporting_ct_ids=[]` + `no_measurement` |
| 18 | `AlignedWindow` carried neither `previous_states` nor `state_transitions`, so `coerce_window` produced empty lists and `_collect_transitions` returned `{}` — state-transition inference was **dead code in production** despite passing its own unit tests | production-shape probe | both fields added; `run_inference_once` attaches `core._pending_transitions` via `dataclasses.replace` |
| 19 | Buffer eviction was silent — a full buffer dropped events with no counter and no log, so data loss was invisible | intake review | `events_evicted_before_maturity` counter exposed under `/status` → `ingestion` |
| 20 | Startup snapshot replay fabricated transitions: `get_states` re-emits each entity with its *old* `last_updated`, so every restart queued "unknown → current" transitions with hours-old timestamps and no measurable window | passive run after #16–#19 | first observation of a device establishes state but is **not** a transition; repeated identical state is not a transition either |
| 21 | Persisting every undeclared event grew `device_events` at **~1.5 GB/day** (99.98 % of rows: 143 876 of 143 902) for entities no device owns and nothing reads — unbounded growth on an unattended run, violating the spec's storage requirement | DB-size measurement during the passive run | undeclared events are counted but **not** persisted; existing bloat purged and `VACUUM`ed (82 MB → 23 MB) |
| 22 | **Main-only topology crashed reconciliation.** A device visible only through the home energy monitor produces an empty measurement matrix (`shape=(0, n)`). `prepared.mask & np.array([...])` built a `float64` empty array and `&` with a bool mask raised `ufunc 'bitwise_and' not supported`. This is precisely the user's "device is only under the home energy monitor" case — the one configuration the taxonomy must support | first test of a `home_total_only` device | row-support mask built with explicit `dtype=bool`; regression test asserts the whole main power stays in `unknown_residual` (`accounted_fraction == 0`) |
| 23 | **Configuration projection destroyed evidence provenance.** Projecting `DeviceConfiguration.state_entity_ids` into the topology's `state_entity_ids` overwrote the mapping tool's record, and *removing* the configuration then left the user's typed entities behind, indistinguishable from measured evidence | hand-check of set-then-remove on the real candidate topology | user declarations moved to a separate `config_load_entity_ids` key; `state_entity_ids` is evidence-owned and never rewritten; removal restores metadata byte-for-byte |
| 24 | **Undeclared state text promoted a device to "verified".** Tier 4 requires corroboration, but any value present in `device_states` was counted as corroboration — including a stale key with no topology-declared load entity, giving a dedicated CT a "verified" grade on no evidence | hand-check of an unconfigured device carrying `state="on"` | `TierInputs.has_declared_load_signal` gate: without a declared load entity, Tier 4 is unreachable |
| 25 | **Tiers were computed but never persisted**, and enum objects leaked on direct attribute assignment (`use_enum_values` coerces only on validation). History would silently reload every result as `tier_0_unknown` after a restart | `InferenceResult` dump probe + DB schema inspection | migration 5 adds `confidence_tier`/`monitoring_level`/`power_behavior`/`tier_reasons_json`; `finish()` assigns `.value` explicitly; `row_to_inference_result` restores them. Verified on a copy of the real 75 MB DB with all 28 device results preserved |
| 26 | **Clamping the score into a per-tier band destroyed score resolution** — a repeated-update sample became indistinguishable from a fresh one within the same tier (both forced to 0.60) | full-suite regression (`confidence` comparison test) | tier and score are independent outputs; divergence is reported as `tier_score_divergence:<score>_outside_typical_<low>_<high>` instead of rewriting the score |
| 27 | **A shared circuit with modelled siblings was wrongly unsolvable.** Blocking level-2 attribution (and adding a slack column) for *every* `monitoring=shared` device discarded legitimate multi-device separation | `test_state_pinning_makes_shared_branch_identifiable` failed after the guard was added | both the attribution block and the slack column key off `shared_unmodelled` (shared **and** no modelled sibling), not merely `shared` |
| 28 | **Two lighting devices had zero attribution rows.** `build_topology` filed every single-channel device's CT under `dedicated_ct_ids`, including two lighting devices whose channels are `CTType.BRANCH`. `dedicated_channel_for` only accepts `DEDICATED_DEVICE`, so both devices fell through to `most_specific_branch_group`, which reads `branch_ct_ids` — empty. They were measured by a real CT and yet invisible to inference, and were mis-classified `home_total_only` by the new monitoring derivation | first `effective_configs()` run over the real candidate topology (`home_total_only: 2` looked wrong) | builder files the CT according to its role (`dedicated_device` → `dedicated_ct_ids`, else `branch_ct_ids`); two regression tests assert every device owns a role-legal CT and every device has at least one attribution row. **Applied to `data/wattmosaic.db` as topology v3** (see §7b) |
| 29 | **A CT *power* entity overwrote the device's runtime state on availability blips.** `entity_links` links a device's CT power entities as well as its state entities — necessary, so a learning sample knows which CTs measure it. But `EngineCore.on_event` wrote `event.new_state` into `device_states[device_id]` for *any* linked entity. Numeric readings do not reach this path (`HASource` consumes them as measurements and returns early), but `unavailable` / `unknown` / `restored` do: the live `device_events` table holds 16 such rows across 16 CT power entities. Each blip replaced the real state with `"unavailable"`, and the next genuine reading of the runtime entity then looked like a transition *back*, so one blip manufactured **two** phantom transitions and the runtime signal was unreliable | tracing `device_states` writers after the mode-only prior probe; confirmed against the live DB and against `HASource._process_state` (numeric → sample, non-numeric → event) | `state_entity_for_device` + `runtime_state_entities` separate *measurable* from *state-bearing*; `on_event` persists a power entity's event but refuses to write its value as device state, counting it in `power_events_not_state` (status + Prometheus). `state_key` likewise never maps a power entity to a device key. Regression test replays `unavailable`/`unknown`/`restored` and asserts both the runtime state and the absence of phantom transitions |
| 30 | **The entire per-device configuration feature was inert in production.** `project_configuration` correctly writes user declarations to `config_load_entity_ids` (so `state_entity_ids` stays evidence-owned), but `entity_links` only read `state_entity_ids`. A user who configured an appliance switch would have had the engine hold the configuration while ingestion never linked the entity: no state, no transition, no sample. Unit tests passed because they built `DeviceTopology` objects directly rather than going through projection → linking | projection probe: `state_entity_ids()==[]`, `entity_links()["switch.appliance"] is None` after configuring that switch | `entity_links`/`ambiguous_entities` read both keys; `runtime_state_entities` is their union; regression test projects a configuration and asserts the declared entity is linked |
| 31 | **A mode-only device reported its full `on_w` for every state, including `off`.** `configured_power` applied a generic `expected.on_w` whenever `state is not None`. A thermostat emits its setpoint mode (`heat_cool`) in the overwhelming majority of rows, so a configured 4.5 kW burner would have been reported continuously — and `off` also yielded 4.5 kW. This directly defeated ADR-0012's "a mode never creates watts" | hand-run of a configured mode-only thermostat: `heat_cool`/`cooling`/`off` all returned 4500 W `configured_prior` | `configured_power` now requires a **positively on declared load entity** before a generic `on_w` applies, taking the caller's post-override verdict (`state_is_on`) so integration-specific labels are honoured. A confirmed `controller` load label may still supply watts on its own (the opt-in assertion). Mode-only states now return `unknown` / tier 0 and leave the power in the residual |
| 32 | **The engine ingested its own output.** The integration publishes `sensor.<device>_estimated_power` (W) and `_estimated_energy` (kWh). `HASource._process_state` accepted any power/energy entity, so those 64 entities became measurements: 6 283 rows across 29 `*_estimated_power` ct_ids in `ct_samples` (367 distinct ct_ids against 38 topology channels). `discover_candidate_topology` then proposed **36** of them back to the user as CT channels — the engine offering its own answer as an independent measurement of the same circuit. Inference was not corrupted (quality and the solver are topology-scoped, so 0 of 120 quality channels were self-outputs), but the storage and discovery contamination was real | `ct_samples` grouped by `ct_id` during the idle-channel investigation; then a live `discover_candidate_topology` run over real states+registry: 87 candidates, `platform` histogram `{'wattmosaic': 36, ...}` | `is_own_entity(entity_id, platform)` in `ingestion/normalize.py`: registry `platform == "wattmosaic"` is authoritative, with a `sensor.wattmosaic_` id-prefix fallback for when the registry is unavailable (device-level estimates are named after the device, so they carry no marker in the id). Wired into `_process_state` (`own_entities_skipped` stat) and `discover_candidate_topology`. 6 tests; removing either guard fails the corresponding tests |
| 33 | **Staleness was measured from arrival, not from the state timestamp.** ADR-0003 and `docs/data_flow.md` both state `age = engine_now - state_timestamp`, but `SampleAligner._make_channel` and `_health_for` used `received_at`. `_prime()` replays every state on each reconnect with a current `received_at` while HA's `last_updated` can be hours old, so a replayed 2-hour-old reading reported `age_seconds=0`, `quality=good`, and `ChannelReading.is_usable()` returned **True** — the solver would accept stale data as a current measurement. The live DB already holds 801 such rows across 232 ct_ids | reproducing `_prime` on the real aligner: `age_seconds=0.0` for a `state_timestamp` 7 200 s old, `is_usable(120 s) = True` | `_age_seconds(entry, at)` derives age from `state_timestamp` (clamped at zero for future skew) and is used by both `_make_channel` and `_health_for`. Two tests assert a replayed old state reports `age≈7200`, `quality=stale`, and `is_usable()==False`; both fail on the old code. This also makes the resync question decidable (see §7d) |

---

## 6. Open risks

| ID | Risk | Mitigation | Status |
| --- | --- | --- | --- |
| R1 | Main vs sub-panel nesting unconfirmed | Topology configurable, candidate-only | OPEN |
| R2 | Room `6.42 W` sensors are template duplicates | Flagged; excluded from direct measurement | OPEN |
| R3 | No physical measurement timestamp | Separate receipt/state timestamps + staleness scoring | MITIGATED |
| R4 | Hidden entities (`hidden_by`) still live | Included in audit, not silently dropped | MITIGATED |
| R5 | Double-pole pairing must not be double counted | Explicit pairing model + solver constraint | MITIGATED |
| R6 | Production writes forbidden without authorization | All work read-only; install deferred | MITIGATED |
| R7 | No household ground truth | Accuracy remains **unverified**; never claimed | OPEN |
| R8 | `homeassistant` not installed locally | Integration tested pure-logic + stub HA only | ACCEPTED |
| R9 | Tier labels could be misread as probabilities | Every tier payload carries `calibrated: false`; docs state grade vs score explicitly | MITIGATED |
| R10 | A configured prior shown as a watt value could be mistaken for a measurement | Separate `Source.CONFIGURED_PRIOR`, Tier 1 only, explanation states `configured_running_power` | MITIGATED |
| R11 | No configuration has been applied to the live engine | Projection path unit-tested and hand-run on the real candidate topology; apply needs authorization | OPEN |
| R12 | ~~The live DB topology (v2) mis-files two lighting CTs under `dedicated_ct_ids`~~ | **Resolved** — topology v3 written to `data/wattmosaic.db`; all 28 devices now have attribution rows and report `dedicated` (§7b) | CLOSED |
| R13 | The passive run (PID 38375) started before defects #29–#31 were fixed, so its `device_states` were still being clobbered by CT power entities and any configuration would have been inert | A restart exercises the fixes; the run is deliberately not disturbed mid-flight | OPEN |

---

## 7. What is *not* done (explicit)

- No production install or configuration change has been made.
- No per-device accuracy claim is made against the real home;
  `docs/real_ha_validation.md` and `docs/acceptance_report.md` §7 say
  **unverified** for absolute accuracy. All accuracy figures come from the
  deterministic simulator.
- The integration has not run inside a real Home Assistant instance; it was
  verified against a stub HA surface with the real HA module absent.
- No model has been trained on real household data. Learning is proven
  end-to-end only on synthetic samples.
- `state_entity_ids` is populated for **8 of 28** devices from measured evidence
  (§3b). The other 20 cannot be linked honestly: 3 are confounded additive
  circuits, 2 have reversed polarity, 2 have no state events at all, 2 are
  duplicate channels, and 11 have no HA state entity describing them (a mix of
  large appliances, small plug loads, and the feed CT pair). Assigning them
  would require guessing, which is forbidden.
- With the topology still `candidate`, every real sample is rejected
  `ambiguous_topology`; no real model can exist until the user confirms.
- Shared-branch devices (13 of 37) remain unresolved by this topology. This is a
  physical metering limitation, reported as residual, not a code gap.
- **Per-device reporting is stratified only.** No aggregate accuracy figure is
  produced for the taxonomy or for tiers, because the household ground truth does
  not exist. Tier semantics are verified by unit and engine tests; their
  *accuracy* is unverified.
- No `DeviceConfiguration` has been written to the live engine. The default is
  all-`unknown` for every device, which is the pre-existing behaviour.

---

## 7a. Publishing and installation

Goal: publish the project and install it on the live HA so the user can use it
and report back.

### Done (verified this session)

| Item | State |
| --- | --- |
| `hacs.json` at repo root | Created (name, `homeassistant: 2024.11.0`, `content_in_root: false`) |
| `custom_components/wattmosaic/brand/` | Created: 256 px icon, 512 px `icon@2x`, logo |
| `manifest.json` repo URLs | Fixed: placeholder `wattmosaic/wattmosaic` → `asxzy/wattmosaic`, `codeowners: ["@asxzy"]` |
| `addon/config.yaml` `url`, both Dockerfiles `image.source`, `addon/README.md` | Repoint to `asxzy/wattmosaic` (0 placeholder refs remain) |
| `scripts/validate_addon.sh --no-build` gate | `deployment_valid`: 58 checks that the Dockerfile, add-on staging, `run.sh` config mapping and token redaction stay correct. A broken install path fails CI (10/10) |
| Evidence tier surfaced in HA | New `sensor.<device>_evidence_tier` enum + `tier_reasons`; `confidence_tier` on power attributes. Previously the whole ADR-0013 feature was **invisible** in Home Assistant. |
| Integration contract pin | `1.0.0` → `1.1.0` to match the engine |
| `tools/dev/check_hacs.py` + CI job + verify gate | Publication rules enforced; a required gate (8/10) |
| `tools/dev/render_validation_docs.py --check` gate | `docs_fresh`: published validation docs must be regenerated from a real simulator run, never hand-edited. A stale doc fails CI (9/10) |
| Real HA IP removed from tracked files | `PROJECT_STATUS.md`, `tools/audit/ha_client.py`, `data/verification.json`; also removed from the secrets allowlist so it cannot return |
| Verification artifacts scrub host/IP | `tools/dev/verify.py` now redacts URLs and IPs before writing tracked `data/verification.json` / `docs/verification_report.md` |
| User-guide install documentation | `docs/user_guide.md` §0 (install, first-run expectations, troubleshooting); `docs/installation.md` §D; `README.md` install rewritten |
| Stale contract `1.0.0` references | Corrected across `docs/` (api, integration, architecture, interface_contracts, deployment, installation, integration_status) |

### Findings that change the plan

1. **HACS no longer exists in HA 2026.11.** It was replaced by the built-in
   **Marketplace**, which this HA instance already runs (`marketplace` config
   entry confirmed). "Install via HACS" therefore means "install via
   Marketplace"; the repo requirements are the same (`hacs.json` at root).
2. **The Marketplace only accepts public GitHub repositories.** Confirmed in
   upstream docs. A private repo cannot be installed.
3. **`asxzy/wattmosaic` does not exist on GitHub, and the agent cannot create
   it.** The available credential is a fine-grained PAT scoped to existing
   repositories: it reports `push/admin` on them but the REST
   `POST /user/repos` and GraphQL `createRepository` both return
   `Resource not accessible by personal access token`. Repository creation needs
   `Administration: write` on the account (fine-grained) or the classic `repo`
   scope. No SSH key for GitHub is configured either. Everything is staged, so
   publishing is one `git push` once the repository exists.
4. **The repository used to track ~3.2 MB of real home data** in `data/*.json`
   (8054 entities, 4093 distinct sensors, real room/appliance names, real
   topology, device hashes, MAC addresses, serial numbers, manufacturer/model
   strings, config-entry identifiers). Publishing it as-is would have been a
   privacy leak. **Resolved**: those files are now git-ignored and untracked, and
   the committed audit fixtures are synthetic
   (`data/examples/reference_house/`, generated by
   `tools/dev/make_reference_house.py`).
5. **Historical note:** five integration tests originally hard-depended on
   `data/ha_environment_audit.json`. They now read the synthetic fixture, and the
   suite passes with the real files moved aside entirely (verified).
6. **No shell or Supervisor access to the HA host**: SSH publickey/password
   denied, Samba 445 closed, `/api/hassio/*` returns 401. The agent therefore
   cannot install anything into `/config` or the add-on store directly.

### Decision taken

A sanitizer was prototyped and **deliberately discarded**: chef-style field
scrubbing of 12 000+ identifiers (MACs, serials, non-ASCII names, token lists,
manufacturer strings) cannot be made provably complete, and a partial scrub is
worse than none because it looks safe.

**Chosen: public repo with synthetic fixtures.** Real audit data stays local and
git-ignored; every committed document is either methodology or a clearly-labelled
synthetic worked example. A private repo was rejected because the Marketplace
cannot install from one.

### Published

- **https://github.com/asxzy/wattmosaic** — public, single commit `5d323b7`.
- 219 tracked files; 10/10 required verification gates pass **in a fresh public
  clone** (`git clone` → `pip install -e .[dev]` → `tools/dev/verify.py`), which
  is the only run that proves the tree is self-contained. This caught a real
  defect: `deployment_valid` read the operator's git-ignored `.env` and so failed
  for everyone else; it now stages the committed compose file with the committed
  placeholder env into a throwaway directory.
- Topics set; description set; issues enabled.

### Why an orphan root, not a history rewrite

The development history still contains the pre-sanitisation `data/*.json` blobs,
so publishing it would defeat the exercise. `git-filter-repo` was available but
rewriting was rejected in favour of a clean root, which is simpler to reason
about and impossible to get subtly wrong. The published tree therefore has
exactly one commit and no household data anywhere in it:

- `git ls-files data/` → `data/schemas/` (19 contracts), `data/examples/`
  (5 synthetic reference-household files + 2 config examples), and
  `data/verification.json` (IP-scrubbed gate output). Nothing else.
- The local `local-only-history` branch keeps the development history and is
  never pushed.

### Installation

Installation is performed by the user, following `docs/user_guide.md` §0:
engine first (add-on, Docker, or `python -m wattmosaic_engine.main`), then the
integration via **Settings → Marketplace → Add repository**.

The engine URL is `http://<engine-host>:8787`, the integration talks to it over
HTTP, and the engine holds the HA token. Never the reverse.

---

## 7b. Live database repair (defect #28), authorised

Defect #28 was fixed in the builder but the operator's live database still held
the defective topology v2. Applied on 2026-11-06 with authorization.

| Step | Result |
| --- | --- |
| Backup before any write | `data/backups/wattmosaic-<timestamp>-pre-topology-v3.db` (git-ignored: same household data) |
| Stop the passive engine (PID 38375) | Clean SIGTERM shutdown |
| Schema migration on open | `user_version` 3 → 5. Migrations 4/5 are additive only (`CREATE TABLE IF NOT EXISTS`, `ALTER TABLE ADD COLUMN … DEFAULT`), so no row was rewritten |
| Rehearse on a copy first | Confirmed exactly 2 devices had zero attribution rows (two lighting devices), and that the fix moved exactly 2 CTs |
| Surgical write | Only `branch`-typed CTs moved from `dedicated_ct_ids` to `branch_ct_ids`. Saved as topology **v3**, still `candidate` |
| Verified by re-reading | 28/28 devices have ≥1 attribution row (was 26/28); both lighting devices now `dedicated`, not `home_total_only`; a third lighting CT untouched; 40 state links, 3 inverted channels, `ambiguous_entities()` empty |

Preserved through the migration: 81 130 `ct_samples`, 102 732
`inference_results`, 365 `device_events`, 44 `learning_samples`, 2 100 power and
2 100 energy aggregates.

**Deliberately not done:** the topology was *not* rebuilt from the current audit
input. A naive `build_topology.py` run produces 36 channels (vs 38) and renames
13 device ids, because the audit file predates the device-id scheme — applying
that would be a far larger change than the defect warrants. The write touched
device ownership only.

Topology remains `candidate`; confirming it is still the operator's decision.


---

## 7c. Installation on the live system, API auth, and three shipped defects

Everything below was done against the operator's production Home Assistant with
explicit authorization. HA is `2026.11.0` on HA OS; the engine stays on the
existing host, reachable at its LAN address on port `8787` (operator decision,
not migrated to the App).

### Engine API hardening

| Step | Result |
| --- | --- |
| `WM_API_TOKEN` generated (`secrets.token_urlsafe(32)`) into the git-ignored `.env` | Never printed, never committed; `.env.example` already documents the key |
| Engine restarted | Unauthenticated `GET /api/v1/devices` → **401** on loopback *and* LAN; wrong token → 401; correct token → 200 |

The token is a second layer: the engine already binds a host port, so without it
anyone on the LAN could read the household's power data and topology.

### Defect: the confirm step made the setup wizard impossible to finish

`async_step_confirm` treated an unticked **Confirm uncertain assignments** box as
`invalid_mapping`. Any installation with a single assumption therefore could not
complete setup at all — and the only escape was to tick the box, which marks the
**whole** topology confirmed. 22 of this household's 34 CTs have an unknown
phase, so the escape hatch was the only exit, and it would have asserted 22
unverified facts. `docs/integration.md` already promised the opposite.

- Decision extracted to `flow_logic.resolve_confirmation(errors, uncertain,
  confirm_uncertain) -> (confirmed | None, error_key | None)`; pure Python, no HA
  import.
- Semantics now: submitting the form **is** the explicit user action, so it
  stores the assignments and leaves the topology `candidate`. The box only ever
  *adds* confirmation.
- Four tests (uncertain / clean / ticked / invalid). Verified to fail against the
  previous gate before being accepted.
- Docs, `strings.json` and `translations/en.json` updated to match.

### Defect: every entity published no value (322 entities)

`_WattMosaicBase` in `sensor.py` and `_Base` in `binary_sensor.py` inherited
`CoordinatorEntity` alone. `SensorEntity`/`BinarySensorEntity` were imported but
appeared only in type annotations.

Home Assistant's plain `Entity.state` returns `unknown`; only `SensorEntity`
consults `native_value` and only `BinarySensorEntity` consults `is_on`. The
observable symptom was 322 registered entities that polled on schedule, carried
fresh attributes (`last_success`, `topology_version`), and never published a
value — while the coordinator itself reported `snapshot_age_stale: false`,
`last_error: null` and 28 devices. It looked like a data problem; it was an
inheritance problem.

- Both bases now inherit their platform entity.
- New static AST regression test `test_entity_bases_inherit_their_platform_entity`
  fails on the previous bases and passes on the fix. `homeassistant` is not
  installed here, so the check is static, like the rest of that suite.
- Verified live after restart: 286 of 322 entities publish values. Global
  reconciliation is real (`total incoming 6518 W`, L1 2588 W, L2 3931 W,
  unknown residual 4406 W, `engine_status: ok`).

The 30 that remain `unknown` are honest, not broken:

- 28 × `operating_stage` — the engine sends `operating_stage: null` and
  `model_id: null` for every device, because no model has been trained while the
  topology is `candidate`.
- 1 × `measurement_source` and 1 × `model_validity` (one device) — its source
  is genuinely `unknown` (`tier_0_unknown`), so there is no source to report.

### App repository was not installable

`POST /api/hassio/*` returns 401 from the Core REST API (it needs
`SUPERVISOR_TOKEN`), SSH on the HA host rejects every key, and Samba is closed,
so the App cannot be installed *by this agent*. The operator reported the store
rejecting the repository outright, which was a real packaging defect:

1. **No `repository.yaml` at the repository root.** The Supervisor refuses the
   URL entirely without it ("is not a valid app repository"). Added.
2. **The app folder could not build.** The Supervisor uses the app folder
   (`addon/`) as the Docker build context, and `addon/Dockerfile` does
   `COPY engine ./engine` — but `addon/engine/` does not exist, so even a
   recognised repository failed at build. Fixed by publishing a prebuilt
   multi-arch image instead: `image: ghcr.io/asxzy/wattmosaic-engine` in
   `addon/config.yaml`, built and pushed by `.github/workflows/app-image.yaml`
   (`linux/amd64` + `linux/aarch64`, tag = the app `version`). Verified locally
   that both architectures build.
3. **Staging must not pull.** `scripts/package_addon.sh` produces the *other*
   install shape — a self-contained folder built locally. It now strips the
   `image:` line from the staged copy, so a copy-install builds on the box
   rather than silently pulling. Verified: staged folder builds, source keeps
   `image:`.

`scripts/validate_addon.sh` now enforces both contracts: a valid root
`repository.yaml` with exactly one app folder, and a tagless `image:` whose tag
will exist.

### The published image (verified)

| Check | Result |
| --- | --- |
| Workflow | `App image` (`.github/workflows/app-image.yaml`), run `42351845993`, conclusion `success` |
| Manifest list | `ghcr.io/asxzy/wattmosaic-engine:0.1.0` → `linux/amd64`, `linux/arm64` + attestations |
| Anonymous pull | Succeeds with an empty `DOCKER_CONFIG` — the package is public, so the store can pull it |
| Entrypoint | `/app/run.sh`, `EXPOSE 8787/tcp`, healthcheck `python3 /app/healthcheck.py` |
| Fails closed | With no HA token: logs `no Home Assistant token` and exits non-zero (as designed) |
| Boots | With dummy settings: `/healthz` → `200 ok` in ~2 s |
| API auth in the image | `/api/v1/health` → **401** without a token, **200** with the right one |
| Container healthcheck | Passes inside the running container |

The image was published by a `workflow_dispatch` run. The `push` trigger did not
fire for `37cb106` because no push added or changed files under the workflow's
path filter; the run that exists was dispatched manually with the keyring
credential (the fine-grained `GITHUB_TOKEN` lacks the `workflow` scope for both
dispatch and the Actions-permissions API). Future pushes that touch `addon/**`,
`engine/**` or `pyproject.toml` will fire it normally.

### Documentation defects found while verifying the install

The install could not be completed by this agent (`/api/hassio/*` returns 401
without `SUPERVISOR_TOKEN`), so the names a user must type were derived from the
Supervisor 2026.11 source instead of guessed. Four were wrong in the published
docs:

| Defect | Was | Is |
| --- | --- | --- |
| Store hostname | `a0d7b954-wattmosaic-engine` | `f48de52e-wattmosaic-engine` |
| Copy-install hostname | claimed to be the store one | `local-wattmosaic-engine` |
| Container name | `addon_<slug>` | `app_<slug>` (changed in Supervisor 2026.08) |
| Local-apps path | `/addons` | `/local_apps` on 2026.11, host `apps/data/` not `addons/data/` |

`a0d7b954` is the *Community Apps* repository id, pasted in as if it were ours.
The repository id is `sha1(lowercased URL)[:8]`, so it depends on the exact URL
typed: `.git` or a trailing slash yields a different id and a different hostname.
The app's own **Hostname** field is the authoritative value, and the docs now say
so instead of presenting a hand-computed string as fact.

`tools/dev/check_manifests.py` gained `check_documented_app_names()`, which
derives all four values from `repository.yaml` + `addon/config.yaml` and fails on
drift. Verified negatively: restoring the old hash and the old `addon_` prefix
makes it fail with four specific messages.

### Integration install (verified)

| Step | Result |
| --- | --- |
| Installed via Marketplace from `asxzy/wattmosaic` | Repository ID withheld |
| HA Core restarted (authorized) | Down ~30 s, `RUNNING` after ~65 s, `safe_mode: false` |
| Config entry | one config entry, state `loaded` (identifier withheld) |
| Wizard driven through all 38 channel steps | Topology written as **v4**, still `candidate` |
| Pre-write backup | `data/backups/wattmosaic-<timestamp>-pre-integration-topology.db` (git-ignored) |

The only topology change from v3 → v4 is `device_id` populated on the 22
dedicated channels — a mirror of ownership the device rows already declared. No
ownership change, 0 new graph problems, 22 "no device_id" problems removed,
28/28 attribution preserved.

### Also fixed: two genuinely failing CI jobs

- `shellcheck addon/run.sh` (SC2163). The fix (`export "${env_name?}"`) also had
  to work on macOS bash 3.2, which rules out `declare -g`.
- The `secrets` job grepped for `ghp_|github_pat_` across the tree, so it matched
  **its own pattern text** and the existing scanner's patterns — it could never
  pass. It now runs `tools/dev/check_secrets.py`, the same check as the
  `secret_scan` gate.

---

## 7d. Idle circuits, self-ingestion, and staleness from the wrong clock

Three defects were found by investigating why 6 devices showed `unavailable`
while 22 neighbours published normally. All three are fixed, tested, and
negatively verified (each test fails when its guard is removed).

### 7d.1 The "frozen" CT channels are idle, not broken

Eight CT channels had stopped updating. The first hypothesis — a dead bridge or
a failed integration — was **wrong**, and the evidence rules it out:

| Observation | Reading |
| --- | --- |
| Frozen channels: `min == max == 0.0` over 47 samples | The value never varied |
| Changing channels: ~2 570 samples at ~15.7 s cadence | Normal polling cadence |
| `last_updated`, `last_changed` and `last_reported` all equally stale | Upstream state, not an engine artefact |
| Two channels recovered when the circuit later drew power | The sensor was alive throughout |
| One sensor reported a **fresh** `0.0` | A fresh zero is possible, so staleness is not intrinsic to zero |

The integration is **push-on-change**: a circuit that is genuinely idle stops
publishing and its last state sits at `0.0 W`. That is correct upstream
behaviour, and the engine must not "fix" it by inventing a reading. The decision
is recorded as **ADR-0015**; the user-facing explanation is in
`docs/troubleshooting.md` §3.

### 7d.2 The engine ingested its own output (defect #32)

`HASource` accepted any power/energy entity, including the 64 entities the
integration itself publishes. Measured on the live database: **6 283 rows**
across 29 `*_estimated_power` ct_ids, against 38 topology channels (367 distinct
ct_ids total). Worse, `discover_candidate_topology` proposed **36** of them back
to the user as CT channels — the engine offering its own answer as independent
evidence of the same circuit.

Inference was not corrupted, because quality and the solver are topology-scoped
(0 of 120 quality channels were self-outputs). The harm was wasted storage and a
contaminated discovery wizard.

Fixed by `is_own_entity(entity_id, platform)` in `ingestion/normalize.py`,
wired into both `_process_state` and `discover_candidate_topology`. The registry
`platform == "wattmosaic"` is authoritative; the `sensor.wattmosaic_` id prefix
is a fallback for the engine's global sensors, because a device-level estimate
is named after the device and carries no marker in its id.

### 7d.3 Staleness was measured from the wrong clock (defect #33)

ADR-0003 and `docs/data_flow.md` both state `age = engine_now - state_timestamp`.
`SampleAligner` used `received_at` instead. `_prime()` replays every state on
each reconnect with a current `received_at`, so a reading Home Assistant recorded
hours ago reported `age_seconds=0`, `quality=good`, and
`ChannelReading.is_usable()` returned **True** — the solver would accept stale
data as a current measurement. The live database already held **801 such rows
across 232 ct_ids**.

Fixed with `_age_seconds(entry, at)`, which derives age from `state_timestamp`
and clamps future skew to zero.

**This fix is what makes the resync question decidable.** With age from
`state_timestamp`, a periodic `get_states` resync would re-observe the held
`0.0` and report `age ≈ 7 200 s`, `quality=stale`, `is_usable() == False` — the
device stays `unavailable`. A resync is therefore **provably useless**, and it is
not implemented. Without this fix a resync would have stamped a two-hour-old
reading as fresh, which is exactly the hazard the constraint "`last_updated` ≠
new measurement" exists to prevent.

### 7d.4 A published identifier leak, and a guard against the class

`PROJECT_STATUS.md` is published, and it carried a **real Home Assistant config
entry id**, a **real marketplace repository id**, and several real appliance and
device names. Those are exactly the identifiers the project's privacy rule
excludes from published docs. They are now genericised.

The `secrets` scanner could not have caught them: it detected private IPs and
tokens, but not HA config-entry ids or MAC addresses. Both patterns are now in
`tools/dev/check_secrets.py`, and both were verified negatively (a deliberately
injected id and MAC each fail the scan; removing them restores `clean`).

---

## 7e. The App "failed to start" — four defects on the startup path

The user installed the App and the Supervisor reported it failed to start. The
Supervisor log showed the container starting and then:

```text
Starting Docker app ghcr.io/asxzy/wattmosaic-engine with version 0.1.0
No API token provided for /core:8123/api/websocket      (repeated)
Timeout while waiting for app WattMosaic Engine to start, took more than 120 seconds
```

The App log showed the matching engine-side failure:

```text
no ha_token option: using SUPERVISOR_TOKEN via the Core proxy
WM_API_TOKEN is empty; only Supervisor ingress may reach the gateway
HA connection error: InvalidStatus: server rejected WebSocket connection: HTTP 401
reconnecting to HA in 0.94s (attempt 1) ... up to attempt 10
```

Two independent failures were visible, and reproducing the published image
locally exposed two more.

### 7e.1 The Core-proxy URL was mangled (defect #34)

`EngineConfig.from_env` appended `:8123` to any URL that had no port:

```python
if host.startswith(("http://", "https://")) and ":" not in host.split("//", 1)[1]:
    host = f"{host}:8123"
```

`run.sh` deliberately switches to the Supervisor Core proxy when `ha_token` is
empty, setting `WM_HA_URL=http://supervisor/core`. The heuristic turned that into
`http://supervisor/core:8123`, and `urlsplit` reads that as host `supervisor`
with path `/core:8123`. That is exactly the path in the Supervisor log line
(`/core:8123/api/websocket`), and it is why every connection was rejected with
401 before authentication was even attempted.

Verified directly against the published image: the old code resolves
`ws://supervisor/core:8123/api/websocket`; the fixed code resolves
`ws://supervisor/core/api/websocket`, which the Supervisor serves.

The fix applies the default port only to a bare host with no path, which keeps
the convenience (`homeassistant` → `http://homeassistant:8123`) and leaves
`http://supervisor/core` and reverse-proxy prefixes untouched.

### 7e.2 The health probe could never authenticate (defect #35)

`healthcheck.py` called `/api/v1/health` directly with `WM_API_TOKEN`. That
variable is set by `run.sh` *inside PID 1*; a Docker `HEALTHCHECK` is a separate
exec built from the container configuration and does not inherit it. Verified:
`docker inspect .Config.Env` on the running container has no `WM_API_TOKEN`, and
`printenv WM_API_TOKEN` exits 1.

So every probe was unauthenticated and the gateway returned 401. Docker stayed
`starting` for the 90 s start-period and only reported `unhealthy` at ~150 s —
already past the Supervisor's 120 s startup timeout. The watchdog then restarts
the app on every `UNHEALTHY` event, so a stock install would have looped
indefinitely.

Reproduced with the published image: `docker_health=starting` for 140 s, then
`unhealthy`, with the probe output `unhealthy: engine rejected WM_API_TOKEN
(401)`.

The probe now uses the gateway's `/healthz`, which performs the same upstream
check (engine `ok|degraded`, database integrity, HA state) using the token the
gateway holds. Timing was tightened to `--interval=5s --start-period=60s`, so a
terminal state is reached in under 100 s. With the fixed image and an empty
`ha_token`/`api_token` (exactly the App's defaults), the container reported
**healthy in 10 s**.

### 7e.3 An empty `api_token` denied every peer (defect #36)

`run.sh` logged "the engine API is unauthenticated", but `gateway._authorised`
returned `(False, False)` for every peer when `API_TOKEN` was empty. Only the
Supervisor ingress proxy could reach the API — not the integration in Core, and
not the health probe. The log line described intent the code did not implement.

The gateway now serves loopback and the Supervisor's internal network
(`172.30.32.0/23`) when no token is configured, and refuses everything else. A
host-published port is outside those ranges, so exposing the engine without
setting `api_token` still fails closed. Verified: with a token set, an
unauthenticated loopback request still returns 401.

### 7e.4 The storage write test ran as root

`run.sh` tested `/data` writability as root, then dropped to UID 10001. A volume
owned by the wrong user passed the test and the engine died opening the database
as the service user. The test now runs through `run_as_service`.

### Verification

- 916 tests pass (898 before; 18 new in `tests/unit/test_app_startup.py`).
- `ruff check .` clean; `scripts/validate_addon.sh --no-build` 59/59.
- Every new guard was verified negatively: reverting the URL heuristic fails the
  proxy-URL tests; restoring `return False, False` fails the loopback and
  Supervisor-network tests; reintroducing a direct API call fails the
  healthcheck guard; restoring `90s/30s` timing fails the timing bound. Each
  passes again once the fix is restored.
- End-to-end: the fixed image, run with the App's default empty tokens,
  reported healthy in 10 s and resolved the Core proxy path correctly.
- The secret scanner's new allowlist entries (`127.0.0.0/8`, `172.30.32.0/23`)
  were checked against a real public address, which it still flags.

### Also corrected

- Version bumped to 0.1.1 across the App, engine, integration and docs so the
  image can be republished (the Supervisor pulls `<image>:<version>`, and
  0.1.0 already exists).
- ADR-0016 (health is judged by the credential holder) and ADR-0017 (an empty
  token narrows the audience) record the two design decisions.
- `docs/troubleshooting.md` §9.1 maps each log line to its cause and fix.

### 7f. Defect #38 — the fixed probe re-scanned the whole database

The 0.1.1 probe fix exposed a second defect that had been masked by the first.
`/api/v1/health` ran `PRAGMA integrity_check` on every call, synchronously on
the event loop. While the probe could never authenticate (0.1.0) that path was
effectively dead. Once `/healthz` worked, the container `HEALTHCHECK` began
polling it every 5 s, and each poll read the entire database file.

Measured on a 392 MB copy of the live database:

| Call | Cost |
| --- | --- |
| `PRAGMA integrity_check` (cold) | 6.8 s |
| `PRAGMA integrity_check` (warm) | ~1.2 s |
| `SELECT 1` (new `ping()`) | 0.000 ms |
| `health()` before | ~1200 ms |
| `health()` after | 0.006 ms |

At a 5 s interval the old path spent ~24 % of a core inside the event loop, and
on slow App storage a single poll could exceed the 8 s probe timeout, flipping a
healthy engine to `unhealthy` and letting the Supervisor watchdog restart it.

The fix splits verification from liveness: `Database.ping()` is `SELECT 1` and
is what `health()` calls; the full scan runs once at startup via
`EngineCore.verify_integrity()` and its verdict is cached and reported, so a
corrupt file still shows `error`. Diagnostics keeps the on-demand full scan.

Verified:

- 5 new tests in `tests/unit/test_health_probe_cost.py`.
- Negatively verified: restoring the `integrity_check()` call in `health()`
  fails `test_health_does_not_run_integrity_check`,
  `test_health_issues_a_cheap_probe` and
  `test_verify_integrity_caches_a_failure`; all pass once the fix is restored.
- 921 tests pass; `ruff check .` clean.
- Version bumped 0.1.1 → 0.1.2 so a new image tag can be published.

---

### 7g. Defect #39 — the integration was installed, and the engine had nothing to infer

Two separate failures produced one symptom: the integration and its 322 entities
existed, but no per-device power or energy was ever published.

**7g.1 The config entry pointed at the wrong engine.** The entry was `loaded`,
but its URL named the developer machine's host engine, not the App: a process
that was hung (385+ minutes of CPU, bound to `*:8787`, refusing connections, no
database writes since 13:20Z). Every `wattmosaic` entity was therefore
`unavailable`. The correct target for an App-store install is the app hostname
`http://<app-slug>-wattmosaic-engine:8787` (ADR-0017: an empty `api_token` still
admits Home Assistant Core).

**7g.2 The engine never bootstrapped a topology.** `discover_candidate_topology`
existed and `docs/installation.md` §5 promised discovery, but its only importer
was its own unit test. With no topology stored, every inference cycle returned
`{"skipped": "no topology configured"}`; that return value was never logged and
`/health` reported `ok`. The integration creates per-device entities only from
`snapshot.results`, so it correctly published nothing — there was nothing to
publish.

Discovery was also unusable as written. Its classifier treated any channel whose
id contained `"main"` as an incoming feed, so on the real 36-channel installation
it produced **21 mains out of 36** (every `main_area_*` load). It emitted
`devices: []`, which `_validate_topology` rejects, so even a wired-up call would
have returned HTTP 400.

**The fix.** Discovery was rewritten around a structural anchor and wired into
startup:

| Rule | Why |
| --- | --- |
| A CT monitor is a hardware device exposing several power entities (≥2 shaped `..._ctN_power`) | The only honest anchor; a bare smart plug is not CT hardware |
| Roles come from position within the monitor: the shortest paired `ct1`/`ct2` stem is the panel feed | Substring `"main"` classified 21/36 channels as feeds |
| Exactly two legs = one 240 V device; three or more legs = separate circuits | Defect #28: `circuit_ct1/ct2/ct3` are three loads, not one |
| Monitor aggregates (`_total_power`, `_total`, `_sum_power`) are never circuits | They would double-count every other CT |
| Every non-main channel gets an owning device, with `device_id` set on the channel | Unclaimed CTs are measured but never attributed |
| Polarity is derived from the observed sign and the evidence stored in `metadata.polarity_evidence` | Forcing `normal` clipped a reversed CT to 0 W and reported it `direct_measured` / `quality: good` (verified with a real engine run) |
| Startup waits up to 30 s for the first subscription; an empty state list is *deferred*, not "no monitor" | `HASource.start()` only schedules the connection, so discovery raced `_prime()` |
| Discovery retries at most once per 60 s while none is configured | A slow/restarting Home Assistant must not leave the engine inert until restart |
| Health reports `details.topology` and degrades when `configured` is false | The engine must not say `ok` while it has nothing to infer |

**Verified against the live installation** (saved HA snapshot: 7 649 states,
8 489 entity-registry rows, 490 devices) and against the stored topology as
ground truth:

| Measure | Result |
| --- | --- |
| Channels matched to ground truth | **38 / 38** |
| Devices matched to ground truth | **28 / 28** |
| Channel-role disagreements | **0** |
| Duplicate `ct_id` / `device_id` | **0 / 0** |
| Measurement matrix | **32 × 28**, `unobserved_devices = []` |
| Graph problems | 22, all `unknown phase` — the true state, not fabricated |
| Real inference run | **28 results**, per-device power summing to 5 274.7 W, residual 9 770 W preserved, reconciliation error 0.23 % |

The remaining 22 `unknown phase` problems are correct: the phases are genuinely
unknown in the installation, and guessing them would fabricate attribution.

**Verified negatively.** Each guard was removed and the corresponding test
confirmed to fail, then restored:

1. `start()` does not call bootstrap.
2. `start()` waits zero seconds (the shipped race).
3. Health does not degrade on a missing topology.
4. Substring-`main` classification restored.
5. Single-channel devices are not emitted.
6. Three-way lighting family is paired (defect #28).
7. Panel total treated as a circuit.
8. Isolated smart plug becomes a channel.
9. Discovery emits `confirmed`.
10. The silent skip is restored.
11. Bootstrap overwrites an existing topology.
12. No retry after the connection appears.
13. Discovery retry is not rate limited.
14. Polarity is forced to `normal` again.
15. A double-pole leg's reading is attached to the wrong leg.

Two weak tests found by this process were strengthened:
`test_health_reports_a_missing_topology_as_degraded` now makes Home Assistant,
the database and the inference backend all healthy so the missing topology is the
only variable; `test_every_non_main_channel_is_owned_by_a_device` fails when a
whole class of devices disappears (28 → 4 devices) instead of only checking that
*some* device exists. A third gap was found by negative verification itself: the
first polarity and startup tests called `bootstrap_topology`/`discovery` directly,
so nothing pinned `start()` to wait or pinned a leg's reading to its own channel;
both are now driven through the real path.

A separate test-isolation defect was found in `scripts/validate_addon.sh`: the
`run.sh` smoke tests read the operator's ambient `.env`, so
`SUPERVISOR_TOKEN` fallback could never fire on a machine with a real
`HA_TOKEN`. The gate now runs `run.sh` in a scrubbed environment; removing the
scrub fails 2 checks.

**Test and gate state:** 953 tests pass, `ruff check .` clean, all 10
required gates pass (including `docs_fresh`), `validate_addon.sh --no-build`
59/59 both with and without `.env` sourced.

**Still not fixed (needs the user, not code):** devices are 1:1 with CTs. The
engine supports multiple devices on one branch CT (proven by
`test_state_pinning_makes_shared_branch_identifiable`), but nothing in the wizard
can express it. So `circuit_ct1`/`ct2` remain circuit-level entities rather than
named appliances, and the office-light case is not derivable from CT data alone:
screening every circuit surfaced it only on one unrelated circuit, rejected as
`inverted_on_off_labels` (`off` → 56.0 W, `on` → 17.4 W).

---

### 7h. Two vendor-name leaks found while auditing the publish

The pre-publish identifier sweep caught two places where a **vendor hardware
name** was hard-coded into the published tree. Both were pre-existing, not
introduced by 0.1.3, and both were replaced by the structural rule the engine
already uses rather than by a longer list.

| Location | Leak | Replacement |
| --- | --- | --- |
| `custom_components/wattmosaic/flow_logic.py` | a household's monitor brand in the candidate `_STOPWORDS` | `panel_shared_tokens()`: the token set common to *every* channel, derived from the channels themselves |
| `tools/audit/audit_environment.py` | `MONITOR_HINTS` as a dated list of energy-monitor brand names | `identify_monitors()`: one device publishing ≥3 power entities |

Neither fix is cosmetic. On the saved live snapshot the dated brand list matched
**33 devices**, four of which were real monitors — one hint was an ordinary
English word, which matched device names across unrelated integrations.
The structural rule matched exactly the **4** multi-channel devices.

The wizard's candidate filter had the same shape of bug: matching on the monitor
prefix offered the monitor itself as a candidate for every one of its own
circuits. `extract shared tokens` is brand-agnostic and returns an empty set for a
single channel, so a one-circuit topology keeps its candidates.

Both are negatively verified: reverting the filter to plain token overlap fails
`test_monitor_prefix_is_filtered_without_a_brand_list`; removing the ≥3 channel
threshold fails both new audit tests.

**Test and gate state after 7h:** 953 tests pass, `ruff check .` clean, 10/10
required gates. The engine image is unaffected by 7h (the integration, audit tool
and tests are not in the Docker build context), so `0.1.3` stays the published
tag.

### 7i. Energy entities: the meter froze after a reset, and energy never reached the device

Two independent defects in how the integration published per-device energy,
found by comparing against powercalc's design (ADR-0020).

**Defect #40 — the energy meter froze permanently after any engine-side reset.**
`util.monotonic_energy()` returned the previous value whenever the engine
reported anything lower. The docstring justified this as avoiding Home
Assistant "adding the whole total again". That claim is false: HA's
`_compile_statistics()` (`TOTAL_INCREASING`) calls `reset_detected()`, treats a
drop below 90 % as a meter reset, and continues the sum from the new value — it
does not re-add the old total.

The consequence was measured, not reasoned: publishing `12.6, 0.0, 0.1, 0.2,
0.5` produced `12.6` at every step. The sensor is frozen for the life of the
entity, so **every future kilowatt-hour is lost**. Replaced by
`util.published_energy()`, which publishes the engine total verbatim when the
reading is publishable and republishes the previous total when it is not (stale,
missing, bad quality). A genuine reset is now visible and statistics resume.

The trade-off is documented rather than hidden: a *transient* dip that recovers
is counted as a reset plus new energy and is therefore double-counted. The
quality gate is the mitigation.

**Defect #41 — energy entities never appeared on the physical device.** Every
entity hung off a synthetic `model="Inferred load"` device, so a plug's energy
was invisible on the plug's own page and could not be assigned to it in the
Energy Dashboard. Energy now binds to the user's real HA device when one of the
device's declared `state_entity_ids` / `load_entity_ids` resolves to one.
Power, source and diagnostic entities stay on the synthetic device so inference
provenance never masquerades as hardware telemetry. Entities owned by this
integration are never binding targets — that would make an inferred load the
host of its own sensor.

**Measured on the live installation** (`/tmp/ha_reg_live.json`, read-only
WebSocket registry read; 8 489 entities, 490 devices):

| Measure | Result |
| --- | --- |
| Engine devices | 28 |
| `/devices` payload carrying `configuration` | 28 / 28 |
| Devices declaring runtime entities | 8 |
| Energy entities bindable to a real HA device | 6 |
| Declarations that resolve to no device | 2 |

One binding resolves to a television because that is the entity the user
declared for that circuit; the rule follows the declaration and does not
second-guess it.

**Also added.** A per-device energy opt-out (`create_energy_sensor`, default
`true`, matching powercalc), stored in the config entry under `energy_entities`
and never sent to the engine.

**Defect #42 — editing a device silently re-enabled its energy entity.** The
wizard checkbox carried a static default of `True`, and the form always submits
its checkbox. A user who turned energy off for a load would turn it back on by
editing any *other* field on that same device — a silent reversal of an explicit
decision. The default now comes from storage
(`flow.stored_energy_choice()`).

**Defect #43 — saving any setting discarded every energy choice.** Home
Assistant replaces a config entry's `options` wholesale
(`OptionsFlowManager.async_finish_flow` → `async_update_entry(options=result["data"])`).
The settings step is a different branch from the per-device form and did not
repeat `energy_entities`, so changing the scan interval wiped the whole map and
re-enabled every opted-out entity. Both completion paths now persist it.

**Defect #44 — a stored choice could be read as enabled by `bool()`.** The
coordinator layered the stored opt-in onto the engine configuration with
`bool(stored[device_id])`. `bool("false")` is `True`, so a config entry whose
options held a string would silently keep creating the entity. The same
hand-written coercion had also been copied into `flow_logic`, so the three
copies could drift apart. There is now one rule (`util.as_bool()`), and the
merge itself is pure (`entity_values.merge_energy_choice()`). Splitting it out
also fixed a quieter bug: the merge returned `{}` rather than `None` when
nothing was known, erasing the "engine said nothing" signal every default
depends on.

**Negative verification.** Each guard was confirmed to fail when removed:
removing the publishability gate fails `test_degraded_reading_cannot_move_the_total`
and `test_unpublishable_first_reading_stays_none`; restoring the old pinning
fails `test_reset_is_forwarded_so_the_meter_resumes` and
`test_rollback_is_forwarded`; forcing `energy_enabled()` to `True` fails six
opt-in tests; removing the self-output rejection fails
`test_never_binds_to_its_own_output` and
`test_skips_own_output_and_uses_a_later_real_entity`; dropping the opt-in filter
from `async_setup_entry`, deleting the `_bind_real_device()` call, or moving an
energy mutation back into `native_value` each fails a `TestEnergyWiring` static
guard (`native_value` must have no side effects: HA may read it repeatedly);
making `stored_energy_choice()` ignore storage fails five
`TestStoredEnergyChoice` tests; removing the map from the settings completion
path fails `test_saving_settings_does_not_discard_energy_choices`; returning
`{}` instead of `None` from the merge fails two `TestMergeEnergyChoice` tests;
coercing the stored value with `bool()` instead of the shared `util.as_bool()`
rule fails the four `test_string_false_from_options_is_an_opt_out` cases and the
five `TestStoredEnergyChoice::test_string_false_is_an_opt_out` cases; treating an
uninterpretable string as true fails two `TestAsBool::test_uninterpretable_uses_the_default`
cases. The same coercion was duplicated in three modules before this round; it is
now one function, so all three cannot drift apart.

**Test and gate state after 7i:** 1036 tests pass (953 before), `ruff check .`
clean, 10/10 required gates. Integration-only change: the engine image and
`addon/config.yaml` are unaffected, so the App tag stays at `0.1.3`. The
integration version is bumped to `0.1.4` because Marketplace updates are gated
on `manifest.json`; without a bump the fix could not reach an installed
instance.

**Not verified end-to-end on the live installation.** The engine is only
reachable inside Home Assistant's Docker network, the App's port is not
published, and the HA host rejects SSH; deploying this code and observing a real
entity migration was therefore impossible. The binding decision and the
`/devices` payload were verified against the live registry and the real engine
database; the Home Assistant entity-add path was verified against HA core source
(`entity_platform._async_get_entity_device`, `entity_registry._async_update_entity`)
but not observed running.

---

### 7j. Defect #45 — binding energy before registration orphaned its statistics

Found by reading the add path in Home Assistant 2026.11.0 after 7i shipped the
binding. v0.1.4 attached the energy entity to the user's real device by setting
`entity.device_entry` in `__init__`, which is what powercalc does.

**Why that is wrong here.** Home Assistant derives a `has_entity_name` entity's
`entity_id` from the device the entity is *created on*
(`entity_platform._async_get_entity_device()` resolves the device before
`entity_registry._async_generate_entity_id()` runs, and
`_async_get_full_entity_name()` prefixes the device name). The same engine
device therefore produced two different IDs:

| Binding outcome | Derived `entity_id` |
| --- | --- |
| resolves to a real device | `sensor.<real device>_estimated_energy` |
| resolves to nothing | `sensor.<engine device>_estimated_energy` |

A device that became bindable *after* its first start changed its entity ID. The
recorder migrates statistics only on an explicit rename
(`recorder/entity_registry.py`, `_async_entity_id_changed` →
`async_update_statistics_metadata`), never when an ID is re-derived at create
time, and statistics are keyed by `statistic_id == entity_id`. Every accumulated
kilowatt-hour for that device was left behind under the old ID.

**Measured, not reasoned.** The scenario was reproduced against a faithful
transcription of the HA functions that decide the ID: a water heater created on the
synthetic device records 0.47 kWh as `sensor.water_heater_estimated_energy`; after the
user declares a state entity, the old code creates it on the declared device and
the new ID is `sensor.water_heater_thermostat_estimated_energy`. The 0.47 kWh stays
under the old key, and the live statistics resume from zero under the new one.
The same measurement for the six bindable devices on the live installation gives
a worst case of 0.47 kWh (the television), then 0.23 (the bedroom lighting
circuit), 0.18 (the water heater), 0.06 (the outdoor socket), 0.03 (the vehicle
charger), 0.00 (the hot tub) — the amount each would have orphaned had it been
created unbindable first. Names are described by class rather than quoted: this
file is published, and the real appliance labels are household detail.

**Fix.** Identity is now independent of placement (ADR-0021):

- `_attr_device_info` is always the synthetic "Inferred load" device, so the ID
  is derived from engine device names, which are stable.
- `_bind_real_device()` runs in `async_added_to_hass` — after the registry entry
  exists — and calls `entity_reg.async_update_entity(entity_id, device_id=…)`.
  A placement change does not touch the ID, so the recorder has nothing to
  migrate.
- `entity_values.binding_update()` is the single writer. It returns
  `{"device_id": …}` or `None`, never `entity_id`/`new_entity_id`, and returns
  `None` when the entity is already on the target, so a reload does not rewrite
  the registry.
- `_adopt_registered_device()` restores an existing bound device on reload. It
  only acts when a registry entry already exists, so a brand-new entity is still
  created on the synthetic device and its ID still cannot depend on binding.

**Negative verification.** `tests/integration/test_entity_id_stability.py` (11
tests) transcribes `_async_derive_object_ids`, `_async_get_entity_device`,
`_async_get_full_entity_name`, `_async_generate_entity_id`,
`async_get_available_entity_id` and HA's `slugify`, and models the registry plus
statistics keys. `TestTheModelMatchesTheShippedSource` also parses `sensor.py`,
so the model cannot drift away from the code it describes: reinstating the
`__init__`-time binding fails
`test_init_creates_on_the_synthetic_device_only` and
`test_binding_happens_after_registration_not_in_init`; deleting the
`_bind_real_device()` call from `async_added_to_hass` fails
`test_the_move_is_inside_async_added_to_hass`; unguarding adoption fails
`test_adoption_is_guarded_by_an_existing_registry_entry`; adding `new_entity_id`
to `binding_update()` fails `test_binding_never_renames_the_entity` and
`test_binding_is_idempotent`; dropping the `current == target` early return fails
`test_binding_is_idempotent`; removing the registry-entry precondition from
adoption fails `test_reload_adoption_requires_a_registry_entry`.

**Not verified on the live installation.** The engine is only reachable inside
Home Assistant's Docker network, and the live instance has no WattMosaic config
entry at all (0 wattmosaic entities; the user deleted it while trying to move to
the App). The fix is proven against transcribed HA code and will be observed on
the real instance by the reconfiguration described in §7l.

---

### 7k. App-install discovery — removing the hostname the user had to guess

Installing the App left the user with a connection step that required a string
nothing in the App's UI shows: the Supervisor's DNS name for the container,
which for a locally installed App is prefixed with a repository hash
(`f48de52e-wattmosaic-engine` in the live installation), and a port.

**Implemented** (ADR-0022):

- `addon/config.yaml` declares `discovery:\n  - wattmosaic`. This list *is* the
  permission: `supervisor/api/discovery.py` compares the posted service against
  it and answers 403 otherwise. `POST /discovery` is in the Supervisor's
  `api_bypass` pattern, so `hassio_api: true` is not needed.
- `addon/discovery.py` waits for the gateway's `/healthz`, then POSTs
  `{"service": "wattmosaic", "config": {"host": …, "port": "8787"}}` with the
  Supervisor token. The host comes from `WM_DISCOVERY_HOST`, then the
  Supervisor's own record (`GET /addons/self/info` `hostname`), then
  `socket.gethostname()`, then the slug rule. Loopback names are never
  announced. Retries are bounded and every failure path exits 0: outside the
  Supervisor (plain Docker, CI, a laptop) there is simply nothing to announce,
  and the App must still boot.
- `addon/run.sh` starts the publisher in the background and includes the new
  variables in `--print-config`; the Dockerfile and packaging/validation scripts
  ship and require the file.
- `config_flow.async_step_hassio()` normalises the announced URL through the
  same `normalise_base_url` as the manual step, sets the unique id from it,
  aborts on a duplicate, and probes the engine before showing
  `async_step_hassio_confirm()`. A stale announcement therefore yields the
  ordinary `cannot_connect` error; it cannot create a broken entry. No scheme is
  honoured: discovery is plain HTTP on the Supervisor network, so a message
  cannot downgrade a URL.
- Translations for the confirmation step, and version bumps to `0.1.5`
  (integration manifest + `const.VERSION`, App `config.yaml`).

**Negative verification.** Removing `discovery:` from `addon/config.yaml` fails
`test_app_manifest_declares_the_discovery_domain`; renaming the flow step fails
`test_config_flow_has_the_hassio_discovery_step`; honouring the announced
`scheme` fails `test_announced_scheme_cannot_downgrade_an_https_engine`;
accepting URL syntax in the announced host (the `user@evil.example` →
`http://evil.example` and `host/path` → `host/path:8787` reparses) fails the
`test_unusable_hosts_are_rejected` cases; and the publisher's
`SUPERVISOR_TOKEN` test fails when it raises instead of skipping outside the
Supervisor. The publisher and the config flow are pinned to the same host rule
by parametrised tests that fail if either side changes alone.

**Docker-less verification.** `addon/discovery.py` parses as Python 3.11 (the
container runtime), `python3 addon/discovery.py --dry-run` prints the message and
exits 0, and without `SUPERVISOR_TOKEN` it logs "not running under the Supervisor,
skipping" and exits 0.

**Blocker.** Discovery cannot work on the live App until
`ghcr.io/asxzy/wattmosaic-engine:0.1.5` exists. The image is built only by
`.github/workflows/app-image.yaml` on a push to `main`; `0.1.3` is published and
`0.1.4`/`0.1.5` are not, and the available GitHub token has no `write:packages`
scope, so it cannot be pushed from here. The live App therefore still runs
`0.1.3` and will not announce itself.

**Test and gate state after 7j/7k:** 1148 tests pass (1036 before), `ruff check
.` clean, `ruff check addon/ --no-cache` clean (the root `pyproject.toml`
excludes `addon/`, so the app scripts are linted separately), 11/11 verification
gates pass with 10/10 required.

**Also corrected while here (defect #46).** `__init__._resolve_entry()` used
`device_registry.async_get_device()` and `device.config_entries`. Both are
deprecated in Home Assistant 2026.11 (removed in 2027.8 and 2027.10) and both log
a deprecation warning for custom integrations. Their replacements
(`async_get_devices`, `async_get_device_by_identifier`) do not exist in the
2024.11 minimum this integration declares in `hacs.json`, so the lookup is an
iteration of `registry.devices` instead — iterating is the supported form on
both versions, while using it as a mapping (`.values()`, `.get()`, subscription)
is itself deprecated for 2027.9. `test_no_deprecated_device_registry_lookup`
walks the AST of every integration module and fails on any of the three
deprecated forms.

### 7l. Privacy rename of the reference household, and the label coupling it exposed

The published tree carried a synthetic reference household whose labels were
derived from the real installation. The values themselves were invented, but the
names were not: room and appliance slugs, a lighting stem, and prose such as
"two-panel" described the operator's home closely enough to identify it. The
requirement is that the *published* household contain no real household
identifiers, so the reference household was renamed to neutral vocabulary
(`vehicle_charger`, `hot_water_tank`, `central_hvac`, `ventilation_unit`,
`network_cabinet`, `underfloor_loop`, ...). The new vocabulary was checked to be
token-disjoint from every real slug before use, so no rename could accidentally
reproduce a real name.

**The rename was not cosmetic, and that is the finding.** Running the suite after
the rename moved published accuracy numbers. Investigating that (a rename should
change nothing measurable) proved the simulator seeded each load's waveform from
`device_id`, each channel's noise from `ct_id`, and each channel's sample times
from `hash_stable(ct_id)`. A privacy relabel therefore re-realised the entire
scenario: renaming *only* `furnace` moved `humidifier_2` MAE from 10.45 W to
20.44 W although `humidifier_2`'s own ids were untouched. A rename probe confirmed
it — prefixing the id (`aaa_probe`) and infixing (`mmm_probe`) left the metric at
baseline, suffixing (`zzz_probe`) moved it.

**Fix.** `SimDevice.seed_key` / `SimChannel.seed_key` are assigned from
declaration order (`HouseholdTopology.assign_seed_keys`) and every RNG is keyed on
it, so the physics is pinned to structure rather than labels. Forced-simultaneous
event selection is sorted by seed key too. `panel_totals()` now derives root and
nested feeds from topology structure (no parent; parent in another panel) instead
of hardcoded channel names, and the unused `main_total()` — which had drifted to a
second, inconsistent definition of "root" — was deleted.

**Negative verification.** `test_a_rename_cannot_change_any_measurement` relabels
every device and channel and requires byte-identical measurements. It fails when
any one of the three couplings is restored (device behaviour, sensor noise, sample
times) and passes when decoupled. Re-running the full measurement after restoring
the original `lighting_ct` structure produced identical metrics, which is the
property the fix buys: the published numbers can no longer drift because a name
changed.

**Structural shape was preserved deliberately.** The three lighting circuits were
first renamed to a `lighting_bank_N` form, which *silently changed CT type*: the
audit derives branch-vs-dedicated from the `_ct1`/`_ct2` suffix, so two channels
flipped from `branch` to `dedicated_device` and the branch-owned fixture emptied.
The rename instead keeps a `_ctN` suffix (the real stem is replaced, not the leg
marker), which preserves the three-leg shape and defect #28's subject intact.
Regenerating the fixtures caught this; a rename that had been accepted on
inspection would have weakened a test without failing one.

**Two fragile tests were fixed honestly, not relaxed.** Detection was floored at a
0.80 recall *rate* per device, but `lighting_1` is ON for 10 of 240 samples, so one
missed sample moves recall 10 points — the floor was a coin flip on scenario
realisation. Devices with ≥20 ON samples keep the rate floor; those below it are
bounded by an absolute miss count (`fn <= max(1, round(0.20*on))`), the same
guarantee in units the sample supports. The nested-panel test chose an instant by
sample freshness alone, but a 15 s-old sample can still carry the *previous* load
level across a step, which made root-truth vs root+sub depend on the step rather
than on double counting; it now requires ground truth to be flat across the whole
sample-vintage span.

**Measured again, not adjusted.** Every published number was re-measured from a
real engine run (seed 202 / 60 min / 15 s / good) and rewritten: coverage 0.7031,
false attribution 0.0098, unknown load 0.0247, direct MAE 12.73 W over 5 641
samples, inferred MAE 1.61 W, residual preserved 0.9958, forced/over-attribution
0, nested double-count 0 of 229. Baselines still separate cleanly (flat 0.0000,
main-only 0.7034, naive 37.94 coverage / 0.6265 false attribution, oracle 1.0000).
Every ceiling is measured × 1.8 with the measurement cited beside it.

**Verification.** 1148 tests pass, `ruff check .` clean, and
`ruff check custom_components/ addon/ tests/ engine/ --no-cache` clean, 11/11 gates
with 10/10 required. The tracked tree contains no real device slug, no MAC, no
host IP and no time zone; the only appliance words left are public `DeviceType`
enum values (`ev_charger`, `hot_tub`, `sauna`, ...), which are schema vocabulary
and identify nothing.

### 7m. Defect #47 — the wizard could not accept its own defaults

Installing 0.1.5 on the live installation produced a config flow that walked all
38 channels and then stopped dead on the confirm step with:

```
pair_not_reciprocal:<ct3 channel>:<its ct1 sibling>
```

**Why it blocked the install.** `suggested_pair()` supplies the default
`paired_ct_id` for every channel, and `validate_assignments()` correctly rejects
a one-sided pair. For a channel ending in `ct3` the trailing-integer fallback
rewrote the leg to `1`; for the channel ending in `ct1` it rewrote it to `2`. So
the wizard proposed a `ct3 -> ct1` pair while proposing `ct1 -> ct2`. The confirm
form therefore could not accept its own defaults, and there is no way for a user
to recover: the error names one channel, the real mistake is the default, and 38
channels all look alike.

It is not specific to three-leg lighting. Any circuit with `ctN`, `N >= 3`,
triggers it.

**Fix, in the suggestion layer.** Only legs 1 and 2 are ever paired — a third leg
is a separate circuit, which is the rule the engine's discovery already applies
(`if leg in (1, 2)`) and the same distinction as defect #28. A declared
`paired_ct_id` is honoured only when the other side repeats it, and every
suggestion is checked for reciprocity before it is offered. Validation was
*not* relaxed.

**Measured on the real topology.** Replaying the live engine's 38 channels
through the fixed logic: accept-all-defaults gives 0 validation errors (was 1
blocking), 18 reciprocal pairs, and 22 phase unknowns that stay honestly
uncertain. `resolve_confirmation` returns `(False, None)` — a finishable
*candidate*, which is the documented outcome.

**Live result.** The config entry was created and is `loaded`; 322 entities were
published (292 sensors, 30 binary sensors), 28 `*_estimated_energy` entities are
all bound to 28 distinct real devices (none left on the synthetic device), and
per-device power and energy are flowing. The topology was stored as a
**candidate**, not confirmed — 22 channels have an unknown phase, and confirming
upwards from that would be the false confirmation this project treats as worse
than none.

**Negative verification.** `test_a_third_leg_is_never_paired` and
`test_every_suggestion_is_reciprocal` fail when the leg restriction and the
reciprocity check are removed and pass with them. 1151 tests pass, `ruff check .`
clean, 10/10 required gates.

**App update: blocked, then resolved.** The Supervisor refused
`AppManager.update` with *"not enough free space (1.8GB) left on the device"*
(host disk 44.4 GB used of 48.5 GB, zero backups to reclaim). The free space then
rose to 4.0 GB and the update completed; the App now runs **0.1.5**, so the
`discovery:` announcement is live. Discovery was exercised against an existing
entry and correctly aborted rather than duplicating it; the entry itself was
created through the manual `async_step_user` path. This was an environment
limit, not a defect.

### 7n. Live finding: three monitor feeds are summed as if they were parallel

The installation has three multi-channel monitors, each publishing its own panel
feed. Discovery types every one of those feeds as `ct_type: main` with no
`parent_ct_id`, and `main_power_w()` sums all enabled mains. Six feed legs are
therefore added together.

**The feeds are not parallel.** Measured over 12 h of recorder history (2627
instants with more than 200 W somewhere, 30 W tolerance), the containment
asymmetry is unambiguous:

| row ≥ column | load-manager | main-panel | sub-panel |
| --- | --- | --- | --- |
| load-manager | 100.0% | 52.8% | 99.8% |
| main-panel | 52.0% | 100.0% | 99.9% |
| sub-panel | 0.2% | 0.2% | 100.0% |

Parallel feeds would each sit near 50%. One feed contains another gives ~100% one
way and ~0% the other. The sub-panel is inside both others at essentially every
instant; the load-manager and main-panel pair is symmetrically ~52/52, so those
two measure the same service (102% of each other's median, correlated 0.984).

**A lag sweep rules out sampling skew.** If two CTs sat on one conductor but were
sampled at different times, sweeping the comparison lag would collapse the gap to
meter noise. It does not: for load-manager vs main-panel the median absolute
difference bottoms out at 126 W at zero lag and rises monotonically either side,
and for main-panel vs sub-panel it never falls below 604 W at any lag in
−45…+45 s. These are different conductors, not one wire seen twice.

**Physical closure confirms the sub-panel hangs off the main panel.** Summing
each panel's own branch loads over 6 h (1437 grid instants, forward-filled):
`main_feed − (main_loads + sub_feed)` is negative in only 11/1437 instants
(0.8%), median remainder +230 W. Without the sub-feed the main feed is
unexplained by 1110 W median against an 870 W median sub-feed — i.e. the sub-feed
*is* the missing outlet. Each feed is also ≥ its own measured loads in ≥99.3% of
instants, so the channels are individually sound.

**The engine total is literally the sum of the six legs.** Across 57 paired
instants the engine's `total_incoming_power` equals the arithmetic sum of all
six feed legs with median relative error **1.35%** and ratio **1.007**. Against
any single panel it is 2.89–3.24× out. There is no inference step in between.

**Consequence, measured on the live install (48 paired instants):**

| Quantity | Reported | Against the outermost feed |
| --- | --- | --- |
| `total_incoming_power` | 11 620 W median | 4 053 W |
| `accounted_fraction` | 31.6% | 92.1% (**2.91× understated**) |
| `unknown_unattributed_power` | 7 900 W | 334 W (**23.7× overstated**) |

So the reconciliation denominator is roughly 2.9× too large, and almost all
"unattributed" power is the double-counted feeds rather than a real unknown load.
Per-device power and energy are unaffected — they come from the CT rows, not the
total — which is why the 28 per-device energy entities look sane while the
household roll-up does not.

**Replaying the live channel set through the engine's own `main_power_w()`:**

| Topology | mains | `total_w` |
| --- | --- | --- |
| as discovered (all six legs `main`) | 6 | 12 613 W |
| main-panel feed retyped `branch` | 4 | 8 155 W |
| main + sub feeds retyped `branch` | 2 | 4 356 W |

A/B = 1.55×, A/C = 2.90×. So the excess is entirely the double count.

**Why this is not simply a user error, and not yet called a defect either.**

* The wizard's channel form exposes `role`, `phase`, `polarity`, `paired_ct_id`,
  `device_id`, `enabled`. There is **no `parent_ct_id` field anywhere** in
  `custom_components/`, so the correct nesting cannot be expressed through the
  UI even by a user who knows the wiring.
* The engine's nested-mains guard (`main_power_w()` refuses to sum mains where
  one has a `parent_ct_id`) is therefore unreachable from the app: only an
  explicit `parent_ct_id` triggers it, and discovery never sets one — it records
  the panel name in `metadata["evidence"]` and discards the relationship.
* `docs/electrical_model.md` documents nesting only as main-under-main. It says
  nothing about a second monitor's feed feeding a sub-panel, which is what this
  install has.
* The simulator models the correct shape — `tools/simulator/topology.py` states
  *"A sub-panel feed is a `branch` CT whose parent is the main-panel line. Nested
  `main` CTs are illegal because mains are summed."* So the intended model exists
  and the tests pass against it; production discovery just never produces it.

**Workaround, and why it is not offered as a fix.** Retyping the two nested feeds
to `branch` in the wizard does produce the correct total (row C above). But it
silently discards the nested relationship: those channels stop being mains, so
nothing downstream knows the sub-panel sits inside the main panel, and the
`nested_child_missing` / subtraction logic never engages. It converts a visible
double count into an invisible topology error. It is recorded here as evidence,
not recommended.

**What the fix requires.** Recorded here, then implemented in §7o.

1. Discovery must carry the panel relationship. And it must be a *candidate*,
   since which feed contains which is a wiring fact.
2. The wizard must let the user see and confirm that nesting, and must offer a
   circuit multiplier and a duplicate-feed declaration.
3. Until a user confirms, the engine must not present a summed multi-panel total
   as authoritative.

**Is it a real defect?** The wiring is a household fact only the user can settle.
The defect that *is* demonstrated is narrower and does not depend on the answer:
discovery accepts several independent monitors, types all their feeds `main`,
and the engine sums them with no containment check, no warning, and no UI path to
correct it. `accounted_fraction` and `unknown_unattributed_power` are then
reported as if they were meaningful. That is a reporting defect regardless of how
the panels are wired.

**Blast radius: per-device output is unaffected.** For every device measured by a
dedicated CT, the published `*_estimated_power` is *exactly* its own CT reading —
median absolute error 0.0 W over 68–70 aligned samples for six devices across two
panels. Energy accumulation is also correct: aligning 5-minute energy deltas with
5-minute mean power bucket-by-bucket gives a ratio of 0.99–1.01 for every
segment after the first. (An earlier 0.78 figure was a window-misalignment
artefact of comparing a closed energy window to a different power window; the
aligned comparison is the valid one.) So the 28 per-device power and energy
entities are trustworthy; only the household roll-up, the accounted fraction and
the unknown residual are wrong.

---

## 7o. Nested panels, circuit multipliers and duplicate feeds (contract 1.2.0)

§7n established the defect: several monitors' feeds were all typed `main` and
summed as if parallel, so the household roll-up was 2.9× too high and the
residual 23.7× too high. It also established that the fix needs a *wiring fact*,
which only the user can supply. This section is the implementation.

### 7o.1 Two user confirmations that frame the design

The user confirmed two facts that the code must not re-derive:

1. **The sub-panel is fed from the main panel — the structure is nested.** So a
   `main` CT may legitimately have a `parent_ct_id`, to any depth.
2. **The CT monitor already applies its circuit multiplier** and
   publishes the full load. So `measurement_multiplier` must default to `1.0` and
   must never be inferred from `phase` or `ct_type`; inferring it would double a
   circuit the device already doubled.

Additionally: **all CT measurements are treated as ground truth.** CT sensor
accuracy is out of scope. A CT serving exactly one load therefore carries very
high confidence, and the UI must not hedge with the word "estimated" — the user
reads confidence to decide what is inferred.

### 7o.2 Contract (`1.2.0`)

`CTChannel` gains:

| Field | Meaning |
| --- | --- |
| `alias_of_ct_id` | this CT watches the *same conductors* as another; only the canonical one contributes to a total |
| `measurement_multiplier` | scale applied to this channel's reading, default `1.0` |

Validators: the multiplier must be positive and finite (zero would silently hide
the circuit, negative would read as export), and a CT cannot be an alias of
itself. `parent_ct_id` is now documented as arbitrary depth.

### 7o.3 Resolution replaces refusal

`main_power_w()` no longer returns an error for nested mains. It sums
`TopologyGraph.effective_feeds()` — the mains with no *main* ancestor — so:

| topology | as-discovered sum (old) | resolved sum (new) |
| --- | --- | --- |
| three monitors, all `main` | 12 613 W | depends on declared nesting |
| main-panel → branch (4 mains) | — | 8 155 W |
| main + sub → branch (2 mains) | — | 4 356 W |

Every exclusion is named in `MainPower.notes`
(`nested_feeds_not_summed: …`, `duplicate_feeds_counted_once: …`) and listed in
`excluded_channels`. A refusal would have been safer than a wrong number, but a
*resolution* is better than both: the nesting is a real wiring fact, so the total
stays available and correct.

The destructive branch-subtraction logic in `MeasurementMatrix` was already
depth-agnostic (`ancestors`/`descendants` are recursive), so nested branch CTs
needed no change.

### 7o.4 Multiplier applied exactly once

The multiplier is applied only on the way out of a raw reading, never written
back into a window or reading object:

| site | change |
| --- | --- |
| `electrical/direct.py` | `topology.scale_w(cid, reading.signed_w())` |
| `electrical/main_power.py` | `graph.scale_w(...)` before summation |
| `electrical/matrix.py` | `channel_multiplier` map; `_reading_w()` scales, `_raw_reading_w()` does not |
| `disaggregation/events.py` | `detect_changes(..., multipliers=…)` scales the series, baseline *and* threshold |

Scaling the step-detection threshold too is deliberate: a doubled leg has
doubled noise, so the same significance rule applies to the *load* step.

### 7o.5 Candidate nesting, never inferred fact

New module `electrical/nesting.py`. It returns `NestingCandidate` objects, all
`requires_confirmation=True`. The inference unit is a **measurement node**, not a
CT: a CT plus its reciprocal `paired_ct_id` partner, or the two legs of a
split-phase panel feed (`<stem>_l1`/`<stem>_l2`, grouped by id stem, flagged
`is_assumed`). Comparing legs compares different conductors, which is why the
first implementation failed.

**The shipped detector did not reproduce §7n.** The originally published version
used *mutual containment* (each inside the other at ±10 %) as the duplicate test.
Replayed against the stored 48 h of real measurements, it found **zero**
duplicates: the real alias pair balances only 0.7475 / 0.5347, because the two
monitors disagree by a few percent at each instant and a strict mutual bound is
the wrong statistic. §7n's conclusion was right; the test that was supposed to
reproduce it was not. That test was deleted.

The detector was rebuilt around **joint activity**, and then around nodes:

* **activity** — two channels are only comparable while *both* carry ≥200 W.
  Two circuits at 8 W and 9 W agree to the watt on everything.
* **relative difference** — median |a−b| / max(a,b) ≤ 0.15. Measured separation
  on the reference installation: the real alias pair 0.065, the closest pair of
  genuinely different circuits 0.166. The threshold sits inside that margin.
* **magnitude ratio** — ≤ 1.25. This is *not* subsumed by the relative
  difference: rel = 0.125 with ratio = 1.29 is reachable, and it is what keeps a
  circuit-multiplier channel from being collapsed into its partner.
* **declared facts are excluded** — a declared double-pole pair and any declared
  ancestor/descendant pair are never tested for duplication.

**A load's level is not inferable — measured, not assumed.** Four independent
rules were tried against the 32 declared parents in the stored topology:

| Rule | Result |
| --- | --- |
| tightest peak among containers with containment ≥ 0.995 | **0/32 correct** |
| coverage (parent live whenever child is live) | 0/32 — every container ties |
| unique-fit elimination (a load exceeding a panel cannot be on it) | **32/32 ambiguous** |
| windowed step-response (parent follows the child's steps) | 1/25 |

Containment prunes but never selects: a 9 W lighting circuit is contained by
every panel in the house at 1.000 while idle. So **only a `main` CT may be
proposed as a nested child** (`_NESTABLE_CHILD_TYPES = {"main"}`); loads are
reported in `report["not_inferable"]` as `load_level_not_measurable` and the
wizard asks the user. `phase_compatible()` was deleted for the same reason it
could not help: the measurements contradict the phase labels
(one sub-panel leg is 0.9950 contained by the *other* panel leg), so phase is now used
only to make the written-back leg readable.

**Ambiguity is reported, not resolved.** When several parents fit and closure
cannot separate them, the child goes into `report["ambiguous"]` with the
candidate list and is never proposed. A channel with a declared parent is skipped
and recorded under `report["suppressed"]` as `parent_already_declared`.

**Acceptance on real data (48 h, 38 channels, 32 declared parents):**

| Metric | Result |
| --- | --- |
| duplicate candidates | 2 (panel leg 1 == load-manager leg 1, same for leg 2), act 0.993, rel 0.065, ratio 1.07 |
| nested candidates | 2 (sub-panel leg 1 → load-manager leg 1, same for leg 2), containment 0.9972 |
| false nestings | 0 |
| loads reported not inferable | 28 |
| whole-home feeds | **6 → 2** |
| median whole-home power | **2 100 W** (was ~12.6 kW with 6 mains summed) |

**Known limitation.** sub-panel → load-manager is the unique measured answer,
but the load manager and the panel are the same wire (the alias above), so the
proposal names the load-manager leg as the parent rather than the panel leg.
Functionally correct — the feeds drop from 6 to 2 either way — but which of two
aliases becomes the canonical parent is arbitrary. Duplicates are offered to the
wizard as `candidate_aliases` (a separate proposal kind from
`candidate_parents`, because "same conductor" and "fed by" are different
questions), and `suggested_alias` pre-selects one only when it is unambiguous and
does not contradict a declared pair or parent.

**`GET /topology/nesting`** exposes all of it: `candidates`,
`candidate_parents`, `candidate_aliases`, `ambiguous`, `suppressed`,
`not_inferable`, `nodes`, and `notes`.

### 7o.6 UI wording

Display names no longer use "estimated": `Estimated power` → `Power`,
`Estimated energy` → `Energy`, `Model estimated power` → `Model power`. The
source enum values (`model_estimated`) are protocol, not UI, and are unchanged.

Renaming a translated name changes the `entity_id` Home Assistant derives, which
would orphan long-term statistics. `entity_migration.py` therefore performs an
**explicit registry rename** at setup (`async_update_entity(new_entity_id=…)`),
which is the only path that makes the recorder migrate `statistics` /
`statistics_short_term` rows. The decision (which entities to rename) is pure and
unit-tested; the guard was negative-verified by deleting the "user-renamed"
check, which made the test fail.

### 7o.7 Verification

| Check | Result |
| --- | --- |
| Full suite | 1 249 passed |
| `ruff check engine/ tools/dev/ --no-cache` | clean |
| `ruff check custom_components/ tests/ --no-cache` | clean |
| Contract version | `1.2.0`, `data/schemas/` regenerated (19) |
| Negative verification (nesting + alias) | **17/17** guards fail when removed |
| Real-data replay (48 h, 38 channels) | 6 → 2 feeds, 2 100 W, 0 false nestings |

The negative verification is the part worth spelling out. On the node-based
rewrite it first reported **6/14**, and that number was itself wrong twice over:

1. **The harness was flaky.** It did not clear `__pycache__` or disable the
   pytest cache, so a removed guard could be masked by stale bytecode, and it
   gave different answers for the same case on consecutive runs. It now purges
   bytecode, runs with `-p no:cacheprovider`, and re-runs each test on the
   restored file to prove the failure was caused by the removal. It lives at
   `tools/dev/negverify_nesting.py` so the check is reproducible.
2. **Eight tests were too weak.** They called `duplicate_evidence` directly
   while the guards they were supposed to prove live in `infer_nesting`'s
   duplicate loop, so removing the guard could not possibly fail them. Each was
   rewritten to exercise the guard at the level it actually operates, using the
   measured separating cases.

Two guards were nearly deleted as redundant, and measuring saved both:
`ratio_ceiling` is *not* subsumed by `rel_diff` (rel = 0.125 with ratio = 1.29
is reachable), and `leg_count_exactly_two` could not be tested with three legs
(the regex only matches `_l1`/`_l2`) but *is* load-bearing for a **lone** leg,
which must not be grouped.

| # | Guard | Where |
| --- | --- | --- |
| 1 | joint-activity floor rejects rare co-activity | `nesting.infer_nesting` |
| 2 | relative-difference ceiling rejects tracking feeds | `nesting.infer_nesting` |
| 3 | magnitude ratio rejects a consistent multiple | `nesting.infer_nesting` |
| 4 | a one-sided declared pair is never collapsed | `nesting.infer_nesting` |
| 5 | a declared parent is not re-proposed | `nesting.infer_nesting` |
| 6 | only a `main` may be a nested child | `nesting.infer_nesting` |
| 7 | excitation rejects a standby feed | `nesting.infer_nesting` |
| 8 | a child that exceeds its parent is not nested | `nesting.infer_nesting` |
| 9 | closure rejects an over-explained parent | `nesting.infer_nesting` |
| 10 | a non-reciprocal pair is not grouped | `nesting.build_nodes` |
| 11 | a lone leg is not grouped | `nesting.build_nodes` |
| 12 | a node reading is the sum of its members | `nesting._node_series` |
| 13 | closure skips an unknown own-load instant | `nesting.closure_fraction` |
| 14 | containment skips instants below the parent floor | `nesting.containment` |
| 15 | a declared fact suppresses the alias suggestion | `flow_logic.suggested_alias` |
| 16 | an alias never suggests itself or a descendant | `flow_logic.suggested_alias` |
| 17 | a parent proposal is not read as an alias | `flow_logic.alias_candidates` |

Earlier guards, still verified in the same pass: `effective_feeds` excludes a
main with a main ancestor and resolves alias chains; `scale_w` and the
direct/matrix/event paths apply the multiplier; a parent cycle, an alias cycle, a
single-load parent and an out-of-range multiplier are all rejected by
`validate_assignments`; two candidates are never narrowed to one; discovery
proposals never write `parent_ct_id`; a descendant is never suggested as a
parent.

**Defect found by the earlier negative pass (fixed).** `_as_multiplier` clamped an
out-of-range value to `1.0` during *normalisation*, which meant
`validate_assignments` could never see the bad number: a user typing `200`
would have had the circuit silently reported at 1/200th of its load, with no
error shown. Range is a validation rule, so normalisation now preserves a finite
number and only falls back to `1.0` when the input cannot be parsed at all.

**Integration gap found and fixed.** The duplicate candidates — the single
biggest correction on real data, the ones that take the total from 6 feeds to
2 — had no path to the wizard at all: `candidate_parents` carried only `nested`
candidates. `candidate_aliases` now carries them, `apply_nesting_candidates`
records them in a separate `metadata.alias_candidates` key, and
`suggested_alias` pre-selects one without ever writing `alias_of_ct_id`.

**Still unverified:** real per-device accuracy. There is no household ground
truth, so it stays "unverified" rather than being claimed.

---

## 7p. Release channels — `main` stable, `dev` development

### The constraint that shaped it

Both artifacts install from a git ref, and neither had a development path:

- The **Marketplace** tracks the default branch's commit SHA for a repository
  without releases (the live install reports `available_version: eec273d`, a
  commit, not a version).
- The **App** reads `addon/config.yaml` from the default branch and pulls
  `ghcr.io/asxzy/wattmosaic-engine:<version>`, where the tag *is* that field.

So a dev build placed on `main` would be what every stable user receives. Two
alternatives were rejected: a separate repository (the credential cannot create
one), and publishing dev versions on `main` (it defeats the point of a stable
channel).

### What the two installers actually support

Read from the upstream source rather than assumed, because the two differ:

| Mechanism | Supervisor (App) | Marketplace (integration) |
| --- | --- | --- |
| `#branch` in the repository URL | **accepted** (`GitRepo` parses it) | **rejected** — `is_valid_ref` forbids `#`, and the URL regex keeps only `owner/repo` |
| Install an explicit version/ref | via `addon/config.yaml` | **yes** — `marketplace/repository/install` takes an optional `version` |
| Prerelease versions | any `AwesomeVersion` | **yes** — releases are read with `prerelease=True`; `show_beta` controls what is offered |

The `#branch` suffix also changes the repository id, because the id is
`sha1(lowercased URL)[:8]`: the stable URL hashes to `f48de52e`, the `#dev` URL
to `7acd0210`. A different id means a different app slug, a different `/data`
volume and a different sidebar entry — so a dev App is a *second* app rather
than an upgrade of the stable one.

### Decision

- **`dev` branch** carries development versions. `main` is untouched by a dev
  release.
- **App version** is a SemVer prerelease (`0.2.0-dev2`), which is what the
  Supervisor pulls as an image tag. The dev App is installed from
  `https://github.com/asxzy/wattmosaic#dev` as a separate repository entry.
- **Integration version** is the same prerelease string. The Marketplace cannot
  pin the branch, so a dev build is installed with **Install another version**
  and an explicit ref; the stable entry keeps tracking `main`.
- **One workflow, two branches.** `.github/workflows/app-image.yaml` now runs on
  `push` to `main` *and* `dev`. The tag is the `version:` field, so the branch
  only decides which version was committed; a tag already in GHCR is still never
  rebuilt without `force`.

### Tooling defect found while doing it

`tools/dev/check_hacs.py` rejected any manifest version that was not exactly
`MAJOR.MINOR.PATCH`, which contradicts SemVer 2.0.0 (a prerelease suffix is part
of the spec, not a violation of it). The gate would have failed on a correctly
versioned dev build, so it was fixed to accept an optional prerelease suffix
rather than the dev version being renamed to a fake stable one. `hacs.json`'s
minimum-Home-Assistant check keeps the strict form, because that value is a Home
Assistant version, not a project version.

### Verification

1249 tests pass; `tools/dev/verify.py` reports **11/11 gates** (10/10 required).
The dev tree is byte-identical to the verified local tree except for the version
strings, the release notes, the docs section above and the workflow trigger.

---

## 7q. Defect #48 — the nesting fix could not reach the engine

The dev build was installed on the live installation to prove the 7o fix with
real CT data. It reported **~13.7 kW** while the canonical feed alone measured
**~4.9 kW**, and the alias declaration was demonstrably absent from what the
engine read back. Three independent wiring defects were found. All three are
invisible to a unit suite that builds topologies in memory — the shipped code
was correct, the path the product actually uses was not.

### #48a. `alias_of_ct_id` and `measurement_multiplier` had no column

`CTChannel` has carried both fields since contract 1.2.0, and every consumer
(`canonical_channel_id`, `alias_channels`, `scale_w`, `multiplier_for`) reads
them from the contract object. But `ct_channels` had columns only for the fields
that existed earlier, and `save_topology()` writes an explicit column list:

```sql
INSERT INTO ct_channels(ct_id, entity_id, name, ct_type, phase, parent_ct_id,
    polarity, device_id, paired_ct_id, enabled, ...)   -- no alias, no multiplier
```

`load_topology()` then rebuilt each `CTChannel` without them, so both silently
took their defaults. `parent_ct_id` survived **only because it had a column**,
which is exactly the asymmetry the live measurement showed: the sub-panel
disappeared from the total while the duplicate feed stayed in it.

Migration 6 adds both columns; the write and read paths now carry them.

### #48b. A nested topology could not be written at all

`ct_channels.parent_ct_id` is `TEXT REFERENCES ct_channels(ct_id)` and the
connection sets `PRAGMA foreign_keys=ON`. Channels were inserted in payload
order. The wizard sends channels in discovery order, which puts the sub-panel
feed **before** the service feed it hangs from, so the statement
aborted with `FOREIGN KEY constraint failed` and the whole transaction rolled
back. On the live 38-channel map, declaring nesting made the write fail rather
than persist.

`save_topology()` now orders channels parents-first (`_insertion_order`, tolerant
of unknown parents and of cycles). A genuine cycle remains unstorable — a
self-referencing FK cannot represent one — but it was verified to fail
**atomically**, leaving the previous topology intact rather than emptying the
installation.

### #48c. The options flow was unreachable

`async_get_options_flow` was a module-level function in `config_flow.py`. Home
Assistant resolves it on the handler *class*:

- `OptionsFlowManager.async_create_flow` → `handler.async_get_options_flow(entry)`
- `ConfigFlow.async_supports_options_flow` → compares
  `cls.async_get_options_flow` with `ConfigFlow.async_get_options_flow`

Neither can see a module-level function, so the entry reported
`supports_options: false` and `POST /api/config/config_entries/options/flow`
answered `{"message": "Invalid handler specified"}`. A user could install the
integration and then never edit CT assignments or energy toggles again. It is
now a `@staticmethod` on `WattMosaicConfigFlow`.

### Measured through the real storage layer

The 48-hour replay was re-run so the topology went through
`save_topology()`/`load_topology()` rather than staying in memory — the path
that was broken:

| | feeds summed | median whole-home | notes |
| --- | --- | --- | --- |
| before | 6 | 4975 W | — |
| after | 2 | 2100 W | `duplicate_feeds_counted_once`, `nested_feeds_not_summed` |

`effective_feeds()` returned the two service-feed legs,
`alias_channels()` returned the two duplicate panel legs, and `nested_mains()`
returned both sub-panel→service-feed links.

### Measured on the live installation

The dev App (`0.2.0-dev2`) and the dev integration entry were installed
alongside the untouched stable pair, and the four proven declarations were
applied **through the options flow** — the surface that had been unreachable
before #48c.

At one sampled instant, reading the raw CT sensors and the integration's own
entities in the same response:

| Reading | Value |
| --- | --- |
| service feed L1 (raw CT) | 1128.0 W |
| dev entry `l1_power` | 1128.0 W (exact) |
| service feed L2 (raw CT) | 1536.0 W |
| dev entry `l2_power` | 1536.0 W (exact) |
| **dev entry total** | **2664.0 W** = L1 + L2 exactly |
| sub-panel legs (nested) | 1172.1 W — excluded |
| main-panel legs (alias) | 2584.7 W — excluded |
| naive six-feed sum | 6420.9 W |
| stable entry (0.1.5, no declarations) | 6434.8 W |

The dev total equals the canonical service feed *to the watt* while the other
four feeds are present and excluded. The stable entry, running 0.1.5 against the
same conductors, still reports the naive six-feed sum. Both entries now report
`supports_options: true`.

### Operational consequence of installing a dev integration

The Marketplace installs an integration **per domain, not per entry**: selecting
an explicit ref replaces the files for `custom_components/wattmosaic` that *both*
config entries load. During this validation the stable App also had to be
reinstalled, because removing the stable repository entry had taken its container
with it. Practical rule for this project: while a dev ref is installed, every
WattMosaic entry — including the "stable" one — runs dev code. The A/B
separation that holds for the App (`#dev` → its own slug and `/data`) does
**not** extend to the integration.

### Verification

1256 tests pass; `tools/dev/verify.py` reports **11/11 gates** (10/10 required).
Five guards were negatively verified — the alias write path, the alias read
path, migration 6, the insertion ordering, and the options-flow registration.
Removing each makes its test fail; restoring it makes it pass.

---

## 7r. Measurements on the user's own devices (ADR-0023)

Two ordinary installations showed that a measurement is useless if it lands on a
synthetic device next to the appliance instead of on the appliance. A 240 V
double-pole load has two hot legs its monitor reports twice; a bank of dimmers
shares one lighting circuit and only the switches say which lamp is lit. Both
cases now attach power **and** energy to the existing Home Assistant device, and
the second one lets a positively-OFF declaration settle a shared row.

### 7r.1 Four engine defects — the declaration never reached attribution

| # | Defect | Effect on the product |
| --- | --- | --- |
| 49 | `project_configuration` wrote the declared CTs only to `metadata["monitored_by_ct_ids"]` | The matrix graph never reads metadata, so declaring "this CT measures that device" changed nothing: no direct measurement, no attribution row. |
| 50 | The OFF pin required a learnt model | A switch-only shared circuit (the normal dimmer case) stayed permanently unidentifiable even though the state said exactly which lamps were dark. |
| 51 | The OFF pin used `max(off_tolerance, 2 × standby)` | With no stated standby this is still a positive cap, so the solver's least-norm tie-break invented a share: two lamps on one row split 190/10 and credited 10 W to a lamp the user said was off. |
| 52 | The relaxation pass could widen a *state* pin, and reported `at_upper_bound` rather than what it relaxed | Relaxing "this lamp is off" reintroduces the fabrication the pin removes; the diagnostics also named a device whose bound was untouched. |

The fixes:

1. **Declaration is projected into attribution.** Declared CTs are mirrored onto
   `DeviceTopology.dedicated_ct_ids` (for a `dedicated_device` role) or
   `branch_ct_ids` (branch or unclassified), the added ids are recorded under
   `PROJECTED_CTS_KEY`, and only those are retracted when the declaration is
   removed. A CT assigned through the channel wizard is untouched, and a
   disabled or unknown channel is never mirrored.
2. **A declared load signal makes OFF a constraint.** A `load_entity_id` is
   evidence in its own right (ADR-0012), so the pin fires on it as well as on a
   model. An *undeclared* state key still proves nothing.
3. **An OFF device with no stated standby is pinned at zero** (`_PINNED_ZERO_W`).
   A stated standby is honoured and still measured (196/4 for a 3 W standby), not
   assumed.
4. **A state pin is never relaxed,** and `relaxed_bounds` reports only the bounds
   that were actually widened.

### 7r.2 Integration — power and energy both attach

`BINDABLE_KEYS` is exactly `{power, energy}`. Every provenance entity — source,
confidence, tier, sample count, model validity, operating stage, last inference,
data quality — stays on the synthetic inferred-load device: a model's
self-assessment must not appear as telemetry from the appliance.

Precedence is an explicit `CONF_DEVICE_LINKS` declaration first, entity-derived
resolution second, so an installation that never opens the new step is
unchanged. The link lives in the config entry and never travels to the engine —
the engine has no concept of a Home Assistant device id, and sending one would
make a placement decision look like electrical knowledge.

Three integration-side defects were found while wiring it:

- **Unlink was a no-op.** `_binding_target()` returning `None` simply returned,
  leaving the entity on the old real device forever, because the move is the only
  thing that writes `device_id`. Clearing a link now moves the entity back to its
  own WattMosaic device (registry write of `device_id` only, idempotent).
- **The picker was a dead end.** Candidates were restricted to devices owning a
  declared state entity, but the case the feature exists for — a hot tub on its
  own CT — usually has no switch at all. Candidates are now the declared-entity
  hosts first, then the rest of the registry, with WattMosaic-owned devices
  excluded.
- **The detail step had no candidates on first visit.** Submitting the device
  picker jumped to the detail step before `devices()` was fetched, so the list
  was empty. The fetch now precedes the branch.

A stored link is re-validated on every start (`_linkable`): the device may have
been deleted, and a link must never name a WattMosaic-owned device.

### 7r.3 A display lie found by testing

The engine's `TIER_3_MEASURED` description already covers "a declared load
signal corroborates a shared-channel estimate", but the Home Assistant string
said *"Measured — dedicated CT"*. A corroborated shared row is not a dedicated
measurement, so the user would have been told something false. It now reads
*"Measured — CT row or corroborated share"*.

The requirement holds independently: `source` stays `inferred` for a shared row,
and the UI's "Measurement source" field shows that. Tier is an ordering of
evidence strength, not a claim that a CT measured the device individually.

### 7r.4 Live environment facts (read-only)

| Fact | Value |
| --- | --- |
| devices in the HA registry | 518 |
| WattMosaic-owned (excluded from the picker) | 58 |
| real devices offered | 460 |
| `light.*` entities | 81 |
| hot tub topology | two paired branch CTs (`l1` / `l2`) |

The projection was exercised against that exact shape: 600 W + 600 W → 1 200 W,
and a `2.0` circuit multiplier applied once → 2 400 W. Declaring the pair as
`dedicated_device` yields a direct measurement; declaring it as `branch` yields
an attribution row and no direct measurement, which is the intended difference.

### 7r.5 Verification

`tests/unit/test_device_attachment.py` (15 tests) and
`tests/integration/test_device_link.py` (43 tests) cover every guard above.
Each was **negatively verified** by removing the guard and watching the
corresponding test fail:

| Guard removed | Tests that fail |
| --- | --- |
| the CT mirror onto the device list | 8 |
| retraction limited to projected CTs | 1 (wizard-assigned CT is destroyed) |
| disabled-channel exclusion | 1 |
| OFF pin on a declared load signal | 3 |
| zero pin (`_PINNED_ZERO_W`) | 5 (190/10 split returns) |
| state pin excluded from relaxation | 1 |
| `relaxed_bounds` reports `relaxable` | 1 |
| `configured_prior` in the source enum | 1 |
| `BINDABLE_KEYS` = `{power, energy}` | 1 |
| explicit-link precedence | 2 |
| `CONF_DEVICE_LINKS` on a finish path | 1 |
| unlink returns the entity | 1 |
| stale-link validation | 1 |

Full gate suite **11/11** (10 required), `ruff check custom_components engine
tests` clean.

### 7r.6 Live end-to-end confirmation (Example 1, double-pole load)

`0.2.0-dev3` was installed through the Marketplace (repository `1448895470`,
`version: "dev"`), the manifest on disk read back `0.2.0-dev3`, and Home
Assistant was restarted. Both entries returned to `state=loaded` with
`supports_options: True`.

The whole path was then driven through the **product surface**, not a scripted
shortcut:

1. `POST /api/config/config_entries/options/flow` reached `init`; the options
   form is reachable at dev3.
2. `device_config` listed all 28 engine devices.
3. `device_config_details` offered **461** link options — 460 real devices plus
   "keep on the WattMosaic device" — with `link_count: 460`.
4. Submitting only a link (no engine parameters) returned `create_entry`, and
   the entry persisted
   `device_links: {'<engine hot tub device>': '<real hot tub device id>'}`.
5. The channel wizard walked all 38 channels; the hot-tub pair was set to
   `role=dedicated_device` on its engine device with phases `l1`/`l2` and
   reciprocal pairing, committed with `confirm_uncertain: false`. Topology
   advanced to `version: 5`, `status: candidate`.

Placement after the change — the exact split the design calls for:

| entity | device |
| --- | --- |
| `sensor.hot_tub_power_2` | the real **Hot tub** device |
| `sensor.hot_tub_energy_2` | the real **Hot tub** device |
| `binary_sensor.hot_tub_running_2` | the synthetic WattMosaic device |
| `confidence`, `data_quality`, `evidence_tier`, `last_inference`, `measurement_source`, `model_validity`, `operating_stage`, `sample_count` | the synthetic WattMosaic device |

Entity IDs were unchanged, and long-term statistics survived the move:
`sensor.hot_tub_energy_2` still returned a daily `sum` of 0.083978 through the
recorder, under the same statistic id.

**Direct measurement confirmed.** `measurement_source = direct_measured`,
`evidence_tier = tier_3_measured`, `confidence = 67.9`. The value is the sum of
both legs, not one of them, verified over six consecutive samples:

| CT leg 1 | CT leg 2 | leg sum | WattMosaic | delta |
| --- | --- | --- | --- | --- |
| 9.025 W | 12.573 W | 21.598 W | 21.598 W | +0.000 |
| 9.019 W | 12.522 W | 21.541 W | 21.537 W | −0.004 |
| 9.031 W | 12.573 W | 21.604 W | 21.546 W | −0.058 |
| 9.020 W | 12.576 W | 21.596 W | 21.555 W | −0.041 |
| 9.012 W | 12.515 W | 21.527 W | 21.582 W | +0.055 |

The first sample taken after the commit showed +2.161 W. That was **sample
vintage skew, not a defect**: the two CT legs had been sampled 15 s *after* the
integration value it was compared against. Once the timestamps aligned the
deltas fell to ±0.06 W.

### 7r.6a Example 2 was unreachable through the product (defect #53)

Attempting Example 2 live turned up a **fifth wiring defect of the same class as
#48**: the engine handled shared circuits correctly and its tests proved it, but
no integration path could declare one.

The evidence, in order:

1. The form offers `monitoring: shared` but `normalise_device_configuration`
   never emitted `monitored_by_ct_ids`. Confirmed by calling it directly: a full
   "shared circuit" submission produced
   `{device_type, connectivity, monitoring, behavior, state_entity_ids, light}`
   and nothing else.
2. The channel wizard's only device slot is `CTChannel.device_id`, which holds
   **one** id. Assigning a second lamp to the same branch CT overwrites the
   first.
3. `channel.device_id` on a **branch** CT is inert even for that one device.
   `build_topology_update` mirrors only `dedicated_device` roles onto
   `dedicated_ct_ids`; attribution for a branch row comes from
   `DeviceTopology.branch_ct_ids`. Measured on the real functions:

   | shape | attribution for `lamp_a` | for `lamp_b` |
   | --- | --- | --- |
   | `channel.device_id = lamp_a`, no declaration | `()` | `()` |
   | declaration `monitored_by_ct_ids = [ct_lights]` | `(ct_lights,)` | `(ct_lights,)` |

4. The live entry confirmed it: all three lighting CTs are `dedicated_device`
   on three separate synthetic devices, and the engine reported **no device
   configurations at all**.

The fix adds the missing field to the product:

- `monitored_by_ct_ids` is emitted by `normalise_device_configuration` and
  validated as a CT id (a plain token, **not** an entity id — accepting
  `sensor.foo` would declare a channel that does not exist, which the engine
  drops silently).
- The detail step offers the engine's CT channels as a multi-select
  (`_ct_options`, main feeds excluded because a main is not a load row) and
  pre-selects what is already stored (`_stored_monitored_cts`).
- The device branch loads the topology when it was not already loaded, so
  coming back to edit one device still offers CTs.

Verified end to end through the real functions: a form submission now projects
`branch_ct_ids = ['ct_lights']` onto each declaring device, and inference on that
projection gives the ON lamp `200.0 W` / `inferred` / `tier_3_measured` with the
OFF lamps at `0.0 W`, while two ON claimants both stay `unknown`.

**Negative verification.** Each guard was removed and its test failed:

| Guard removed | Test that fails |
| --- | --- |
| the CT list is emitted | 2 |
| CT-id shape validation | 1 |
| the direct visit loads channels | 1 |
| projection retraction | 2 |
| an unchanged selection is not resent | 1 |
| clearing the selection clears it | 3 |
| a stored CT is only pre-filled when offered | 1 |

### 7r.6a-1 The new field, confirmed live on the instrumented install

`0.2.0-dev4` was installed through the Marketplace (`repository: "1448895470"`,
`version: "dev"`), the manifest on disk read back `0.2.0-dev4`, and Home
Assistant was restarted. Both entries returned to `state=loaded` with
`supports_options: True`.

The device step was then driven through its real HTTP flow, and the field the
fix adds was present in the rendered schema:

| property | value |
| --- | --- |
| `description_placeholders.ct_count` | `32` |
| selector | `select`, `multiple: true` |
| options | 32 engine channels |
| main feeds among the options | none |
| the double-pole load's two CTs | both offered |

So the picker offers exactly the rows a load can sit on, and a main feed cannot
be declared as one. What the field *does* with a submission was verified through
the real functions (§7r.6a); this run verified that the product surface reaches
them on the live install.

**Post-restart settling, observed and not a defect.** For the first samples after
the restart the engine reported `status: degraded` / `ha_connected: false`, and
the double-pole device read `0.0` / `unknown` / `degraded` while its CT legs were
already fresh. Two polls later it returned `21.563` / `direct_measured` / `good`
and held (`21.679`, `direct_measured`, `good`) with legs summing to the reported
value. The first inference after a restart can precede the measurement window;
the next poll resolves it.

### 7r.6b Example 2 still needs the user's knowledge
The product can now express the case, but it still has **no live confirmation**,
and the remaining blocker is knowledge, not code:

- 15 dimmer-controlled lights are on, across three lighting CTs reading 8.3 W,
  928.0 W and 23.7 W.
- The Home Assistant registry carries **no circuit membership**: the 157 dimmer
  devices expose no panel, breaker or CT identifier. Area and name do not
  determine which CT feeds which fixture.
- Attributing a light to a CT from its name would be fabricated attribution,
  which the project forbids.

So Example 2 needs one of two things from the user: a statement of which
fixtures each lighting CT feeds, or a switching experiment (toggle one light,
observe which CT moves). Until then it stays **unverified** rather than
plausible-looking.

Per-device accuracy also remains **unverified** in both cases: it needs a
household ground-truth set that only the user can supply.

One deliberate gap is recorded rather than fixed: a device pinned OFF reports
`unknown` / `0.0` rather than a measured `0 W`, and the row's power stays in the
residual as `unexplained_load_on_device_circuit`. Reporting a hard zero would
claim a measurement the engine did not make.

### 7r.7 Known limitation found while probing the dimming case

The dimmer case is only fully "CT as ground truth" while **one** lamp is on. With
two or more lamps on the same row the linear system genuinely cannot separate
them, and the engine falls back to Level 3 (`model_estimated`) when a model
exists. That value is not clamped by the CT row:

| case | CT row | reported | source |
| --- | --- | --- | --- |
| three dimmers on, models 100/60/40 | 120 W | 100 + 60 + 40 = 200 W | `model_estimated` |
| three dimmers on, models 100/60/40 | 200 W | 100 + 60 + 40 = 200 W | `model_estimated` |
| two lamps on, models 300/200 | 260 W | 300 + 200 = 500 W | `model_estimated` |

This is **pre-existing** (identical on `fa26fe3`) and is what the master spec's
Level 3 prescribes ("use its learned model … mark the result
`model_estimated`"). It is not fabrication in the ADR-0001 sense: the value is
labelled `model_estimated`, never `inferred` or `direct_measured`, and
`Reconciliation.reconciliation_error_w` exposes the mismatch (340 W in the second
case). But it does mean a model can report more than the circuit is drawing, so
it must not be described as a measurement.

Deliberately not changed here: clamping Level 3 by the row would alter every
model-estimated device in the product, not just this feature, and it needs its
own negative verification. It is recorded as the next engine task.

---

## 8. Needs user authorization

| Action | Why it needs authorization |
| --- | --- |
| Installing the integration / add-on on the live HA | Runtime change to a production system |
| Confirming the topology (`POST /topology/confirm`) | Converts candidate→confirmed, enabling learning |
| Declaring `state_entity_ids` for the remaining devices | 8 are derived from measured evidence; the other 20 need the user's knowledge (or acceptance that they stay unattributed) |
| Enabling any service call or device actuation | WattMosaic is structurally read-only |
| Long real-data run for accuracy validation | Occupies the live system; needs consent plus ground truth |
| Applying device configurations in the live engine | Writes the topology (version bump); needs authorization |
| Writing a household ground-truth set for per-device accuracy | Only the user knows the true wattages; without it, accuracy stays "unverified" |

---

## 9. Verification of subagent reports

Per the working agreement, no subagent "tests pass" claim was accepted as
evidence. Each deliverable was re-run and inspected by the orchestrator against
the real engine. Defects this caught:

| Subagent area | Claimed | Found on review |
| --- | --- | --- |
| Persistence: migration 4, `save/load_device_configurations`, `set_device_configuration` command handler | complete, tests pass | Implementation correct (SQL, atomic upsert, corrupt-row skip, forced `device_id`, empty-payload removal). The defect was in the projection it was told to call, which is orchestrator code — see #23. |
| API + HA config flow: `/devices` enrichment, `/confidence_tiers`, options flow steps, `flow_logic` helpers | complete, tests pass | Endpoint shape, route wiring, enum mirrors and flow branch all correct as reported; 38 new tests pass under independent re-run. |
| Tier grading and engine wiring (orchestrator's own code, listed for completeness) | — | #24 undeclared-state promotion, #25 tier non-persistence + enum leak, #26 score clamping, #27 shared-with-modelled-siblings regression. Found only by running the real engine. |

The lesson recorded in §2 stands: a green suite proves the tests agree with the
code, not that the code agrees with reality. Every defect above was found by
running the engine against realistic topologies and reading the numbers, not by
reading a test report.
