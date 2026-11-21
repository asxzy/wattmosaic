# Decisions — Real-time, topology-aware disaggregation (contract 3.0)

All rows APPROVED by the product owner on 2026-11-18.

Each row is a product or architecture decision the handbook (`handbook.md`, cited as HB §n) does not settle, or settles in a way that conflicts with an accepted ADR. The *Default* column is what the plan implements. Every default is **APPROVED** — the product owner approved all ★ rows and all defaults on 2026-11-18. Rows marked ★ blocked their dependent tasks while PROPOSED; with every row now APPROVED, no ★-dependent task is blocked.

When a row is approved, change its status to `APPROVED` and append the ADR text listed in *Lands as* to `docs/decisions/README.md` (task X00).

Evidence for each row is in `analysis/` (the six gap reports, file:line cited there).

---

## 1. Release and compatibility

| ID | Question | Default (approved) | Why | Lands as | Blocks |
|---|---|---|---|---|---|
| **DEC-01 ★** | One breaking cutover, or a staged dual-shape API? | **One cutover to contract `3.0.0`.** Engine, HA integration and panel ship in the same release. There is no dual-read period. All intermediate work lands on the integration branch `feat/realtime-v3`, never on `main`. | HB §16 forbids indefinite compatibility layers. Every push to `main` publishes a `-dev.N` release (`.github/workflows/release.yaml`), so half-migrated states on `main` would reach dev-channel users. `api_client._check_contract` already refuses a major mismatch (`custom_components/wattmosaic/api_client.py:190-210`). | ADR-0035 | all |
| **DEC-02** | What is "rollback" (HB §16.8)? | Before any migration ≥ 13, automatically back up the DB file next to it. Rollback = reinstall the previous add-on version and restore the backup. There are no down-migrations. Legacy tables stay **read-only** for one release, not dropped. | `Database.__init__` migrates immediately and only `reset()` backs up (`storage/database.py:93,182`). Down-migrations of JSON payload rewrites are error-prone. | ADR-0035 | S-wave |
| **DEC-03** | Old `WM_INFERENCE_INTERVAL_S` config | Accept the variable, ignore it with a startup warning for one release, then delete it. | It is a user-visible add-on option. A hard removal would fail add-on config validation. | ADR-0038 | P6 |

## 2. Canonical domain model

| ID | Question | Default (approved) | Why | Lands as | Blocks |
|---|---|---|---|---|---|
| **DEC-04 ★** | Device identity | Keep every existing `device_id` (`dev_<ct>`, `dev_auto_*`, user ids) **verbatim** as the Physical Device id. New devices get `pd_<ulid>`. **Discovery stops minting one device per CT.** A CT is a Measurement Endpoint. What it sees without an explained device is the Reader Residual row (HB §9.4 E), not a fake device. Devices come from HA devices that own load-evidence entities, from user declaration, or from a promoted Unknown Load Profile. | HB §1.1 / rule 3. `dev_{ct_id}` (`ingestion/discovery.py:391`) welds a device to a reader, so AT-15 is impossible. HA unique ids embed `device_id` (`custom_components/wattmosaic/sensor.py:291`), so preserving the ids keeps entities and statistics. | ADR-0035 | D2, S13, H1 |
| **DEC-05 ★** | MEASURED status and Smart Plug anchors vs ADR-0034 | **Amend ADR-0034 decision 2.** A Measurement Endpoint whose *declared scope* is exactly one Physical Device or Load Component (`EndpointScope.SINGLE_SUBJECT`: smart plug, appliance energy monitor) produces `MEASURED` and acts as an anchor in joint inference. The scope starts as a **candidate** and becomes effective only when the user confirms it. The retired CT role (`dedicated_device`, `MonitoringLevel.DEDICATED`, `dedicated_ct_ids`) stays retired, and the new concept must not reuse those names (`tests/unit/test_docs_vocabulary.py`). A *circuit CT* never gets `SINGLE_SUBJECT` scope automatically. | HB §1.1 D, §8.3, §8.5, AT-8, AT-15. ADR-0034 removed a *claim about a circuit*. This is a *property of an endpoint that physically measures one load*. | ADR-0036 | D2, I3, I5, K01 |
| **DEC-06** | Which HA power entities become endpoints? | Discovery lists every non-CT-monitor HA power entity as a **candidate** endpoint (no ingestion). The engine ingests samples only after the user accepts it (HA setup step or panel). | Keeps ADR storage protections (`main.py:884-893`: ~1.5 GB/day from undeclared entities) while making plugs first-class. | ADR-0036 | D2, H2 |
| **DEC-07** | Load Components | Contract, storage, matrix rows and models support components in this release. They are **user-declared only**. Learning may *suggest* a component (a later task, L8) but never creates one. | HB §1.1 B is mandatory in the data model. Automatic component discovery is research. | ADR-0035 | K01, L3 |
| **DEC-08** | HA entity ↔ subject links | A single `ha_entity_links` table, many-to-many, with `target_kind ∈ {device, component}`. One entity may link to several subjects. Disambiguation is a joint-inference question, not an ingestion refusal. | HB §1.2. There are four representations today (`analysis/A-…` §1.1), and `ambiguous_entities()` refuses one-to-many. | ADR-0035 | K01, S13 |
| **DEC-09** | Version id format | Human-readable, monotonically numbered ids: `topo-<n>`, `map-<n>`, `mdl-<subject_id>-<n>`, `snap-<ulid>`. Each id has a `*_number:int` companion. | They are visible in the UI and HA attributes. Today ints and strings are mixed (`analysis/A-…` C8). | ADR-0035 | K00 |

## 3. Topology

| ID | Question | Default (approved) | Why | Lands as | Blocks |
|---|---|---|---|---|---|
| **DEC-10 ★** | Authority over topology edges | A user-declared parent creates a `CONTAINS` edge with `declared_by=user, lifecycle=STRUCTURAL` as a *prior* (contract 4.0 renamed topology `CONFIRMED` to `STRUCTURAL`; mapping confirmation is unchanged). If recent weighted evidence contradicts it it moves to `CONFLICTED` but is never silently deleted, and returns to STRUCTURAL once the evidence recovers. An inferred edge follows `CANDIDATE → PROVISIONAL → STRUCTURAL` from decaying support/conflict weights (≥ 8 recent support with r < 0.05 and ≥ 2 independent causes to promote; hysteresis holds a STRUCTURAL edge at ≥ 6 / r < 0.10), and moves back down as conflicts accumulate. **One event never moves an edge past CANDIDATE.** A pin is the only freeze. ADR-0043 supersedes ADR-0008 and amends ADR-0037: there is no terminal topology confirmation. | HB §4.2–4.4, §4.3 "single event must not make an edge structural". | ADR-0043 (amends ADR-0037) | T4, T5, H2 |
| **DEC-11** | Pointer fields `parent_ct_id` / `paired_ct_id` / `alias_of_ct_id` | Remove them from the 3.0 contract. They become typed edges (`CONTAINS`, `PAIRED_LEG`, `SAME_CONDUCTOR`). A read-only derived "primary tree" view serves UI and HA. | HB §4.2. They are consumed unconditionally today (`electrical/topology.py:101-173`). | ADR-0037 | T1, Z1 |
| **DEC-12** | Missing child reading under a nested parent | Keep the parent row. Treat the missing child's contribution as an unknown column with the child's last-known level as a soft prior and inflated variance. Never set it to 0. | HB §3.3 (parent need not be fully explained), §18. Today the whole parent row is dropped (`electrical/matrix.py:245-248`), which wastes evidence. Expect coverage numbers to move (ADR-0030 baseline 65.25 %). | ADR-0037 | T2 |
| **DEC-13** | `minimal-ha-setup` draft (untracked, `docs/specs/minimal-ha-setup/`) | **Fold it into task H2 and rebase it onto 3.0.** HA writes only what the user picks: endpoints, optional user-declared parents, plug scope. It never writes an engine-inferred parent as a declaration, and there is no silent `POST /topology/confirm` of inferred structure. Engine proposals stay engine-owned PROVISIONAL edges. | HB §4.3–4.4. The draft's D3/D7 (prefill and silent confirm) would turn inference into declared fact. | ADR-0037 | H2 |

## 4. Real-time runtime

| ID | Question | Default (approved) | Why | Lands as | Blocks |
|---|---|---|---|---|---|
| **DEC-14** | Is there still a periodic snapshot? | No periodic full solve. Inference runs per closed or revised Event Group (event loop). A **steady-state absolute pass** runs per reader when that reader's level changes by more than its noise band, and at most every 60 s as a staleness heartbeat that only publishes `stale_sources` and aging. | HB §5, §15 (no full snapshot every second). The HA integration keeps polling `/snapshot`, which serves the latest persisted snapshot. | ADR-0038 | P6 |
| **DEC-15** | Provisional latency | Publish `PROVISIONAL` at the first post-event sample of the earliest reader (~0–16 s at the measured 15.7 s CT cadence). HA-only "state changed but no reader yet" stays `DETECTED` with no watt value. | HB §12.3. Real cadence is 15.7 s (`analysis/B-…` trap 2). | ADR-0038 | P5, I6 |
| **DEC-16** | Subscribe to HA `state_reported`? | No, out of scope for 3.0. Keep ADR-0015 behaviour: silent means held until stale. The ReaderStateStore models "held" explicitly. | Event volume. It can be added later without contract change. | — | — |
| **DEC-17** | HA events from entities not yet linked | Keep them in a bounded **in-memory** ring for grouping and candidate generation. Persist them only if they join an Event Group that becomes evidence. | HB §6.6 / §7.1 vs storage growth (`main.py:884-899`). | ADR-0038 | P1, P5 |
| **DEC-18** | Event windows | Defaults are 30 s pre and 120 s post, per load class, configured like `IsolationConfig`. Pre-window noise comes from long-run per-reader statistics, never from the ~2 samples inside a 30 s pre-window. | HB §6.5, cadence reality. | ADR-0038 | P2, P4 |

## 5. Inference output semantics

| ID | Question | Default (approved) | Why | Lands as | Blocks |
|---|---|---|---|---|---|
| **DEC-19 ★** | May a model licence an individual value for a non-identifiable member? | **No.** A member of a non-identifiable group has `point_w = null`, mathematically justified bounds only, and `NON_IDENTIFIABLE`. A model learned from isolated events *does* count as identifiability evidence through the history-stacked experiment matrix (I4). If history separates the devices, they are `IDENTIFIABLE` and get values. Model priors are never bounds and never floors. | HB §8.4 mandatory output. Today Level-3 publishes 250/250 (`disaggregation/engine.py:2142-2198`; probe in `analysis/D-…` §0). | ADR-0039 | I4, I5 |
| **DEC-20** | User-declared wattage priors sharing one row | Remove proportional clipping (`engine.py:2393-2401`). Declared priors are model priors (λ_m), not allocations. If the user explicitly asks for an allocation (a later feature), it must be published as `ALLOCATED` with `allocation_rule`. | HB §8.4, §8.5. This supersedes ADR-0030's proportional sharing. The "accounted ≤ measured per row" invariant stays. | ADR-0039 | I5 |
| **DEC-21** | Greedy measured-step credit (ADR-0026) | Supersede it. The measured step becomes one hypothesis in the joint solve, scored against the unknown-load alternative. | HB §7.5, §7.7. Today a non-HA compressor in the same window is absorbed (`engine.py:1930-2010`). | ADR-0039 | I3, I7 |
| **DEC-22** | Absolute vs delta disagreement | The published current snapshot comes from the steady-state absolute solve, **constrained** by the identities and groups established by event-delta inference. A disagreement beyond 3σ marks the device `CONFLICTED` in mapping status and surfaces it in the snapshot's `unresolved_events`. | HB §3.2 (both models), §9.7. | ADR-0039 | I7 |
| **DEC-23** | Unknown-load penalty λ_u | Learned per reader scope from the residual engine (T3). The initial operating point favours **residual over attribution**: λ_u starts low, so unknown is cheap. It is exposed as config `WM_UNKNOWN_PENALTY_SCALE` (default 1.0) and monitored by a diagnostic: the share of |ΔY| explained by ΔU, per reader, per day. | HB §8.2 and the closing rule ("a preserved 200 W residual beats false attribution"). | ADR-0039 | T3, I3 |
| **DEC-24** | Confidence | Keep an uncalibrated engineering score, labelled "not a probability" (ADR-0007), with inputs re-scoped to HB §9.6's seven components. **Retire evidence tiers** in favour of `EstimateStatus × IdentifiabilityStatus`. | It removes a fourth "what kind of value" axis (`analysis/A-…` C9). | ADR-0039 | I5 |
| **DEC-25 ★** | HA presentation of non-identifiable and unknown values | The device power sensor state is `unknown` (None) unless the status is `MEASURED`, or `ESTIMATED` and `IDENTIFIABLE`. Bounds, status, identifiability, group id and versions are attributes. **New** group power sensors live on a synthetic "WattMosaic group" device per stable `group_id`. Existing unique ids are unchanged. Past 0 W history becomes gaps going forward; it is not rewritten. Energy integration never integrates a null. | HB §8.4, §18, AT-17, AT-20. Changing the state to None is safe for HA statistics (gaps). Changing unit or state_class would not be. | ADR-0039 | H1 |
| **DEC-26** | Legacy inference history | Rows with `provenance.power_w_is_placeholder=true` are migrated to `power_w = NULL` and flagged `legacy_semantics=placeholder_zero`. Old `source=direct_measured` rows are shown as `legacy`, not `MEASURED`. | HB §16.6–7. | ADR-0035 | S17 |

## 6. Mapping matrix

| ID | Question | Default (approved) | Why | Lands as | Blocks |
|---|---|---|---|---|---|
| **DEC-27** | Which rows exist? | Physical Devices and Load Components from the canonical registry, plus Device Group, Unknown Load Profile, Reader Residual and derived Summary rows. An HA registry device is **not** a row unless it is linked to a Physical Device. HA `area_id` is read **for display grouping only**. | HB §1, §9.3–9.4. Today every HA registry device is a row, so the same appliance can appear twice (`analysis/E-…` C4). | ADR-0040 | M1 |
| **DEC-28** | `/mapping` endpoint | Delete it. Replace it with `/api/v1/matrix` plus drill-down routes (`interfaces.md` §6). There is no known external consumer. | HB §16. | ADR-0040 | M3, Z1 |
| **DEC-29** | Default cell content | The compact cell shows the status glyph + text, the value or interval, and a confidence glyph. Everything else is in the drill-down. The current debug cards move behind a "Diagnostic" toggle on the matrix page. | HB §19.3, §19 intro ("not an internal debugging table"). | ADR-0040 | U1 |
| **DEC-30** | Retention | `mapping_cell_versions`, topology, mapping and model versions are kept forever (small). `event_evidence` (deltas, baseline, σ and summary samples) is kept 400 days. Raw high-resolution samples inside evidence windows are kept 30 days. All three are configurable. Amended by ADR-0042: canonical CT readings (`ct_samples`) are not persisted by default; `raw_capture_days` enables a debug capture. | HB §15 "replay after raw samples expire". `device_events` is today's growth driver (`storage/tiers.py:211-215`). | ADR-0035 | S16, D5 |

## 7. Power models and learning

| ID | Question | Default (approved) | Why | Lands as | Blocks |
|---|---|---|---|---|---|
| **DEC-31 ★** | Legacy learned models (absolute circuit basis) | Keep them **read-only**, flagged `legacy_absolute`. They are not served as validated power and are not used as priors, except on a confirmed single-subject endpoint. Models are retrained from delta evidence. | HB §16.6–7. Every current model is fitted on whole-circuit power (`learning/sample_collector.py:327-367`). Composite and dimmable reloads are broken (`analysis/F-…` T1). | ADR-0041 | S18, L1 |
| **DEC-32** | Auto-promotion | Allowed. Promotion uses per-class criteria on a frozen evidence-id set and needs ≥ 5 independent confirmed events. The first version for a subject and any class change are flagged "review suggested" but not blocked. | Matches the realtime-learning direction in `e27f5c4`. It fixes the R² trap that blocks fixed loads (`analysis/F-…` T2). | ADR-0041 | L5 |
| **DEC-33** | Declared vs learned power | A user declaration wins for **priors**. Learned evidence never overwrites it. A discrepancy beyond 3σ is surfaced as a mapping-status/diagnostic item. | User intent is explicit. HB §10.1 says configuration alone is not high confidence, so the discrepancy must be visible. | ADR-0041 | L2 |
| **DEC-34** | Model class | It is the declared `LoadShape`. If undeclared, start with `Fixed` and let learning *propose* a class change (shown in the panel), never apply it silently. | `LoadShape` contract ("never inferred from naming", `contracts.py:209-212`). | ADR-0041 | L2, L3 |
| **DEC-35** | Reconciliation reach | Replay writes into the engine's **versioned derived namespace** only. HA long-term statistics are not rewritten. | HA statistics are append-only. HB §13.3 is satisfied inside the engine. | ADR-0041 | L7 |
| **DEC-36** | Active learning (HB §13.4) | Deferred to the optional task L8 (after cutover). Suggestions only, never actuation. Safety-relevant loads (HVAC, garage door, water heater) are suppressed. | HB marks it "eventually". | — | L8 |
| **DEC-37** | Background compute budget | One background worker thread. Each job is capped at 2 s of CPU (chunked). `/healthz` p99 < 100 ms under load is a CI gate. | ADR-0033 class outage (`analysis/B-…` C2). | ADR-0038 | P6, L6 |
| **DEC-38** | Smart Plug relocation (HB §17 step 9) and the TV's mapping | Product owner ruling (2026-11-19): after a plug relocation the TV **may or may not** be remapped. The current behaviour (a declared relocation retires the old mapping edge and promotes a new one, topology version unchanged) is accepted as AT-15 asserts. Keeping the TV mapped to its plug and moving the plug in topology would also be acceptable; neither reading is required. | HB step 9/10 admit both readings; AT-15 pins the current one. | — | Z7 |

## 8. Suspected defects to confirm, not decide

| ID | Item | Handling |
|---|---|---|
| **DEF-01** | A declared `polarity=inverted` may not be applied on the production reading path (`analysis/B-…` C10). | P1 writes the failing test first. If it is confirmed, the fix lands in P1, and L1 marks models trained on those readers `legacy_absolute` per DEC-31. |
| **DEF-02** | Composite model rehydrates to 0 W; dimmable loses `control_variable` (confirmed by probe). | Fixed by construction in L1 (lossless codec). The regression tests are mandatory. |
| **DEF-03** | Fixed loads can never pass R² ≥ 0.70 (confirmed by probe). | L5 per-class criteria. |
| **DEF-04** | Group range excludes the measured value; `max_w = min_w` collapse; steady-state 250/250 split (confirmed by probe). | I4 + I5. The 250 W test (I4) is written **first**. |
| **DEF-05** | `residual_power` baselines use one install-wide window for every scope (`disaggregation/audit.py:1896-1901`). | T3. |
| **DEF-06** | Calibration changes apply retroactively in the live solve (`disaggregation/engine.py:739` builds the graph without the factor registry). | T2. |
| **DEF-07** | Rollback can reactivate a rejected model candidate (`storage/database.py:1374-1392`). | L1. |
| **DEF-08** | Sticky body-row header bug (`addon/ui/app.css:1011`). | U1. |
