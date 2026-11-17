# 11 — Test and Validation Plan

**SPEC_STATUS: APPROVED_FOR_IMPLEMENTATION** — specification for the load disaggregation workstream, in the approved specification set. Defines the unit, integration, synthetic, replay, regression, performance, migration and UI test layers with measurable acceptance criteria, and fixes the verification vehicle for the canonical T01–T15 scenario catalogue.

---

## 1. Scope, purpose and standing rules

This document specifies how the load disaggregation system is tested and how a change is judged verified. It does not define requirements, architecture or code. Requirements live in `01-requirements.md`; the module-level designs live in `02-`–`10-`; this document describes the tests that prove those documents are implemented correctly.

**Rule.** The verification harness is the only accepted evidence. A change is verified only by a passing orchestrator-run test log (`docs/implementation_plan_v2.md:333`; `docs/agent_assignments.md:26-28`). Prose claims, code review and self-reported results are not evidence.

**Rule.** Missing is never zero. A gap in measurement is recorded as missing, never imputed as a zero-power reading (§7, regression rule R3).

**Rule.** Never fabricate a per-device number. When separation is not identifiable, the correct outcome is a group estimate, a range, or an explicit "not identifiable" status (§11.2).

**Rule.** Negative verification: every guard in §7 is proved by removing the guard and watching the corresponding test fail. The precedent harness is `tools/dev/negverify_nesting.py`, which purges bytecode, runs with `-p no:cacheprovider`, and re-runs on the restored file.

**Rule.** Training evidence and evaluation evidence must come from disjoint event sets. A figure computed on events used for fitting is a training figure and may not be presented as verification (§11.3).

**Rule.** All currently published accuracy figures are simulator-derived. There is no real-house per-device ground truth (`docs/limitations_v2.md` §1). No acceptance threshold in this document may be described as a real-world accuracy claim.

---

## 2. Test layers and their purposes

| # | Layer | Location | What it proves | What it must never do | Gate that runs it |
|---|---|---|---|---|---|
| LAYER-1 | Unit | `tests/unit/` | Pure logic on contract objects: parsers, math, validators, serialisers, state machines. No I/O, no clock, no network, no global random state. | Touch the filesystem, the network, the system clock, or `random` globals. | `unit_tests` in `tools/dev/verify.py:105-124` |
| LAYER-2 | Integration | `tests/integration/` | Boundary contracts: engine↔database, engine↔API, engine↔HA source, engine↔integration, gateway↔engine. | Reach the real Home Assistant instance or the public internet. | `integration_tests` |
| LAYER-3 | Synthetic (simulator) | `tests/simulation/` | Behaviour under a *known* truth: the simulator's measurement pipeline corrupts a truth it computes independently (`tools/simulator/scenario.py:213`), so per-device error is measurable. | Let ground truth leak into the algorithm under test. | `simulation_tests` |
| LAYER-4 | End-to-end | `tests/end_to_end/` | Whole-system flow against a real `Database` and a real topology fixture. | Substitute an in-memory mock for the database. | `end_to_end_tests` |
| LAYER-5 | Replay | `tools/audit/replay_capture.py` + `tests/simulation/` | Deterministic re-execution of stored evidence yields identical topology candidates and model versions. | Rewrite history, mutate the source capture, or re-fit on the replayed window. | `simulation_tests` (plus a manual capture job) |
| LAYER-6 | Regression (pinned invariants) | `tests/unit/test_invariant_*.py` | Each hard rule in §7 has a test that fails when the rule is removed. | Be skipped by default; the invariant files carry no skip markers. | `unit_tests` |
| LAYER-7 | Performance | `tests/unit/test_health_probe_cost.py`, `tests/unit/test_nesting_perf.py`, `tests/perf/` | Measurable latency, throughput, memory and size behaviour under representative workloads. | Measure only toy fixtures and call the result representative. | `unit_tests` (fast subset) + a scheduled perf job |
| LAYER-8 | Storage / migration | `tests/unit/test_migrations_*.py`, `tests/integration/test_storage_*.py` | Schema evolution, retention, roll-ups, crash recovery, integrity, backup/restore. | Build a prior-schema fixture via `Database()` (which runs migrations in `__init__`, `engine/.../storage/database.py:90`). | `unit_tests` + `integration_tests` |
| LAYER-9 | UI | `tools/dev/check_panel.py`, `tests/unit/test_panel_*.py` | The panel's static rules, its API binding, and the render functions' input shapes. | Claim browser coverage that does not exist. | `unit_tests` + the `docs_fresh` gate |

**Rule.** Every layer states its proof obligation and its forbidden behaviour in this table; a test that violates the "never" column is a defect in the test, not an excuse.

**Rule.** The layer identifiers are `LAYER-1`…`LAYER-9`. They are **not** the storage tiers `L1`–`L4` of `08-storage-and-data-lifecycle.md` §2, and the two vocabularies are never mixed in one sentence.

### 2.1 Gate inventory

`tools/dev/verify.py:105-124` defines 12 required gates and 1 optional gate (`config_valid`, requires `HA_TOKEN`). The gates are `contract_schema_sync` (`export_schemas.py`), `secret_scan` (`check_secrets.py`), `lint` (`ruff check engine tools tests`), `unit_tests`, `integration_tests`, `simulation_tests`, `end_to_end_tests`, `manifest_valid` (`check_manifests.py`), `release_valid` (`check_release.py`), `docs_fresh` (`render_validation_docs.py --check`), `deployment_valid` (`bash scripts/validate_addon.sh --no-build`), `hacs_valid` (`check_hacs.py`).

`tools/dev/verify.py:67-78` forces `sys.executable` and points `PYTHONPATH` at this checkout's `engine/`, so a worktree cannot pass against a parent's code. This is the mechanism that makes the harness authoritative.

`pyproject.toml:38-42` sets `testpaths=["tests"]`, `asyncio_mode="auto"` and `addopts="-q"`. `pytest-cov>=5,<7` is declared at `pyproject.toml:26` but `--cov` is used nowhere in CI (`.github/workflows/ci.yml`), so coverage is not currently measurable from the harness. **G-TEST-01: coverage reporting is unavailable; the requirement to prove "which requirements have no test" (§14) must be met by the manual matrix in §14 until `--cov` is wired into CI.**

---

## 3. Existing coverage baseline

### 3.1 Measured suite size

| Metric | Value | Source |
|---|---|---|
| Total collected tests | 2827 | measured run of the current suite |
| Unit | 1863 | idem |
| Integration | 760 | idem |
| Simulation | 130 | idem |
| End-to-end | 74 | idem |
| `test_*.py` modules | 93 (60 unit / 17 integration / 10 simulation / 6 e2e) | idem |

### 3.2 Scenario-matrix tally

`docs/test_scenario_matrix.md` holds 84 rows, IDs `A1`–`J9`, in ten groups: **A** basic loads, **B** Refoss factor, **C** CT relationships, **D** dedicated circuits, **E** shared circuits, **F** residuals, **G** HA events/automation, **H** dynamic, **I** identifiability/ambiguity, **J** learning/regression. The legend is COVERED / PARTIAL / MISSING with a tally of **51 / 23 / 10**.

### 3.3 Known weak spots and the new work's answer

| Weak spot | Evidence | What this plan does |
|---|---|---|
| Group J (learning/regression) has **0 COVERED** | `docs/test_scenario_matrix.md:182-199` | J5/J7/J8/J9 are structural, not test-writing gaps. §11.3 defines the holdout split and the training/evaluation separation; §7 adds the learning-related invariants; the J rows are added to `MATRIX_STRATA` (§4.3). |
| Group B's 3 MISSING rows are entirely about the factor's *timeline* | `docs/test_scenario_matrix.md:182-199` (rows `B4`, `B5`, `B9` — `docs/test_scenario_matrix.md:49-54`) | The canonical T-catalogue contains no factor scenario (§5.1), so this gap is closed by a **named non-T test**, `tests/simulation/test_factor_timeline.py`, backed by a new `factor_timeline` stratum. Adding a T identifier for it would violate the fixed catalogue. |
| PARTIAL rows cluster on entity roles (A3, G4), residual lifecycle (F7, J5) and conflict surfacing (F9, I6) | `docs/test_scenario_matrix.md:182-199` | Entity roles are covered by the mapping tests (§6.1); residual lifecycle by T03/T04 plus R2 and R8; conflict surfacing by T01/T02/T11 plus R4 and R5. Each names a test file. |
| A6 and H4 are MISSING although `ExpectedPower.startup_w` already exists and is never used | `docs/test_scenario_matrix.md:182-199` (rows `A6`, `H4` — `docs/test_scenario_matrix.md:39`, `:130`) | The motor profile now exists in the simulator (`MotorProfile`, `tools/simulator/behavior.py:404-444`; the `motor_startup` stratum, `tools/simulator/labhouse.py:679-722`), so the gap is closed by the named non-T test `tests/simulation/test_startup_surge.py`, which exercises `ExpectedPower.startup_w` as a live contract field. |
| `monitor_published` hot-tub variant is in the simulator's 10 but not in the fixture's 9 | `tools/simulator/hot_tub.py:943` vs `tests/fixtures/labhouse.py:147-157` | **G-TEST-02.** Re-checked against the canonical catalogue: **no** canonical T-scenario needs it (T01–T15 contain no factor scenario). The divergence is recorded and left alone (OD-90). |
| Nine live-capture tests skip without a local `WATTMOSAIC_REAL_DB` capture | `tests/simulation/test_real_topology.py`; `docs/limitations_v2.md` §3 | The skip is *honest* and must remain visible; §6.4 keeps the capture-dependent tests in a named, explicitly-skipped class. |
| `MATRIX_STRATA` has zero J rows although the matrix document has 9 | `tests/fixtures/labhouse.py:59-144`; `docs/test_scenario_matrix.md:182-199` | **G-TEST-03.** The coverage contract cannot currently see the J rows. The J work adds them to `MATRIX_STRATA` with the new learning strata (§4.3). |
| `C9` ("Reversed orientation / direction change") is PARTIAL — only *static* inversion is covered | `docs/test_scenario_matrix.md:68` | T12 covers the *change* case (move, replace, recalibrate, reverse) with a new `reader_relocation` stratum (§5.1). |

**Rule.** The 2827-test suite is the baseline; the new work adds to it. It does not replace or restructure the existing tests.

**Rule.** A matrix gap is closed by a canonical T-scenario when one exists, and by a **named non-T test** when the fixed catalogue does not cover it. Every gap closed in this plan names its test file.

---

## 4. Fixture strategy and the coverage contract

### 4.1 Where new fixtures live

| Fixture kind | Home | Rationale |
|---|---|---|
| New simulator strata (builders) | `tools/simulator/labhouse.py` | That module owns the strata: `STRATA` at `:898` (21 strata), `available_strata` at `:923`, `build_scenario` at `:928`, `validate_strata` at `:949`. |
| Stratum→matrix-row contract and required sets | `tests/fixtures/labhouse.py` | That module owns `REQUIRED_STRATA` (`:32-54`, 21 names), `MATRIX_STRATA` (`:59-144`) and `assert_matrix_coverage()` (`:170-207`). |
| Synthetic scenario fixtures (per-test) | `tests/simulation/` | Co-located with the scenario tests that use them. |
| Truth/observation seam | `tools/simulator/frames.py` | `TRUTH_KEYS` (`:40`), `is_truth_key` (`:75`), `find_ground_truth_keys` (`:84`), `assert_no_ground_truth_keys` (`:105`), `observation_view` (`:183`), `truth_view` (`:223`). |

### 4.2 The truth seam is the leak detector

**Rule.** A fixture must never let ground truth reach an algorithm. `tools/simulator/frames.py:105` provides `assert_no_ground_truth_keys`, and `tools/simulator/scenario.py:213` guarantees the truth is computed independently of the measurement pipeline — the pipeline only consumes the truth to corrupt it. Every synthetic test asserts the leak check before it asserts anything else.

### 4.3 Extending `assert_matrix_coverage()`

`tests/fixtures/labhouse.py:170-207` currently fails in two directions: when a required stratum is missing, and when a buildable stratum is referenced by no row ("an unreferenced builder is dead weight that nothing exercises"). The extension required by this plan adds the symmetric third direction:

- a **new stratum** must be referenced by at least one matrix row;
- a **new row** must be backed by at least one stratum in `MATRIX_STRATA`;
- a stratum referenced by a row must still exist in `tools/simulator/labhouse.py:898`.

The current verified state is 21/21 strata referenced and 0 unreferenced. `MATRIX_STRATA` has 75 row keys (A1–A7, B1–B9, C1–C9, D1–D9, E1–E5, F1–F10, G1–G10, H1–H9, I1–I8) and **zero J rows**, while the matrix document defines 9 J rows (A1–J9, 84 rows total) — **G-TEST-03**.

### 4.4 The new strata required by the canonical catalogue

Every stratum below was checked against the simulator source, not assumed. The "Verified shape" column records what the builder actually produces.

| New stratum | Serves | Verified shape (source) | Matrix rows to add |
|---|---|---|---|
| `single_child_containment` | T01 | A parent branch CT and exactly one child branch CT, both fed by one device that is metered on the child only, so the parent's delta equals the child's delta | C-family rows for parent→child containment |
| `multi_child_contributions` | T02 | One parent CT with **two** child CTs, each fed by its own device, so the parent's delta equals the sum of the two children's deltas | C-family rows for two children summing to a parent |
| `unobserved_parent_load` | T03 | A parent CT carrying an unmetered device (`SimDeviceType.UNKNOWN`, `attributable: False` — `tools/simulator/topology.py:534-539`) so the parent rises while every monitored child is flat | F-family residual rows |
| `plug_relocation` | T11 | A monitored device whose `circuits` mapping changes from one branch CT to another partway through the run | C-family change-detection rows |
| `reader_relocation` | T12 | A channel whose `ct_id`, `entity_id` or physical position changes partway through the run, plus a reversed-orientation sub-case | C-family change-detection rows |

**Resulting stratum count: 21 + 5 = 26.**

Two further strata are needed for the non-T matrix gaps in §3.3 and are counted separately: `factor_timeline` (group B) and `factor_misconfigured` (group B). They bring the total to **28**.

**Rule.** `assert_matrix_coverage()` must fail when a new stratum is unreferenced and when a new row is unbacked. This is the enforcement mechanism for "every new fixture is exercised and every new row is real".

**Rule.** A new stratum is added to all three places in the same change: `STRATA` (`tools/simulator/labhouse.py:898`), `REQUIRED_STRATA` (`tests/fixtures/labhouse.py:32-54`) and `MATRIX_STRATA` (`:59-144`). A stratum added to only one or two is a defect.

---

## 5. The T01–T15 scenarios

The T-catalogue is fixed by the master instruction and is canonically held in `01-requirements.md` §4.1 (lines 1372-1386); §4.2 (lines 1402-1416) maps each scenario to the existing matrix rows, its status and the requirements it serves; §4.3 (lines 1426-1472) maps requirements back to scenarios. **The IDs and titles below are taken from that catalogue verbatim. No second catalogue exists and nothing is renumbered.**

**Rule.** The T-catalogue in `01-requirements.md` §4.1-§4.3 is the contract. This document adds setup, expected observable outcome and verification vehicle; it does not redefine a scenario. A scenario identifier that is not in §4.1 is a defect.

### 5.1 T01–T15 table

The canonical definitions in `01-requirements.md` §4.1 are the contract. This table reproduces each one verbatim and adds the setup, the expected observable outcome and the verification vehicle. **The "Stratum" column states whether an existing stratum already produces the required shape (verified against the simulator source) or whether a new stratum must be built.**

| ID | Canonical scenario (canonical assertion, `01-requirements.md` §4.1) | Stratum | Input | Expected observable outcome | Verification vehicle (file) | Existing row(s) / status (§4.2) | Requirements (§4.2) |
|---|---|---|---|---|---|---|---|
| **T01** | **Single Child Containment** — parent +100 W when child +100 W; repeated compatible events support a candidate containment relationship | **new** `single_child_containment` (one parent branch CT, one child branch CT, one device metered on the child) | Child load steps +100 W; parent steps +100 W in the same window; the pair repeats over several compatible events | A **candidate** containment edge (child ⊂ parent) is proposed and its confidence rises with each compatible repeat; the candidate is **not** auto-confirmed (R4); the parent is not double-counted with its child (R6) | `tests/simulation/test_t01_single_child_containment.py` | `C1`, `C5` — **new coverage** (a single child whose delta matches its parent is not asserted) | REQ-TOPO-003, REQ-TOPO-004, REQ-POWER-003, REQ-EVENT-005 |
| **T02** | **Multiple Child Contributions** — parent +100 W, child A +30 W, child B +70 W | **new** `multi_child_contributions` (one parent CT, two child CTs, one device per child) | Parent +100 W; child A +30 W; child B +70 W | Two candidate containment edges (A ⊂ parent, B ⊂ parent) are proposed with **separate** confidences (R5); the parent's attributed power equals the sum of the children's (30 + 70 = 100 W) within tolerance (R8); **no** residual event is emitted for the parent | `tests/simulation/test_t02_multiple_child_contributions.py` | `C5` — **new coverage** (two children summing to the parent is not asserted) | REQ-TOPO-003, REQ-TOPO-007, REQ-EVENT-005 |
| **T03** | **Unobserved Parent Load** — parent +150 W, all monitored children unchanged; a dynamic residual event, not rejection of the topology | **new** `unobserved_parent_load` (a parent CT carrying an unmetered `SimDeviceType.UNKNOWN` device, `attributable: False` — `tools/simulator/topology.py:534-539`) | Parent +150 W; every monitored child unchanged | A **dynamic residual event** is raised on the parent; the topology is **not** rejected and the change is **not** forced onto a child; the residual is recorded as a first-class residual with its own operating pattern and is never redistributed (R2) | `tests/simulation/test_t03_unobserved_parent_load.py` | `F3`, `F4` — **new coverage** (the parent change must produce a residual event, not a topology rejection) | REQ-TOPO-005, REQ-TOPO-006, REQ-RESIDUAL-002, REQ-RESIDUAL-004 |
| **T04** | **Baseline Standby Consumption** — persistent non-zero background on an unmonitored or partially monitored circuit; a baseline residual model, not repeated unknown events | **existing** `standby` (`tools/simulator/labhouse.py:386-401`: `hot_water_tank`, `network_cabinet`; `SimDeviceType.STANDBY` → `StandbyProfile`, standby 1.5–6.0 W with rare active windows — `tools/simulator/behavior.py:141-152`), plus a purpose-built **partially monitored** variant | Persistent non-zero background on a circuit whose devices are partly unmonitored | A **baseline residual model** is fitted and the background is reported as baseline standby; the window yields **one** baseline-establishment event, not a repeated stream of unknown events; the baseline is never pushed into a device (R9) | `tests/simulation/test_t04_baseline_standby.py` | `F1`, `F2`, `A7` — **new coverage** (a baseline model rather than repeated unknown events) | REQ-POWER-002, REQ-POWER-004, REQ-RESIDUAL-007, REQ-MAPPING-004 |
| **T05** | **Noise** — readings fluctuate within configured measurement uncertainty; the noise model absorbs it without excessive false events | **existing** `noisy_legs` (`tools/simulator/labhouse.py:473-499`: both 240 V legs metered with `QualityLevel.POOR`, noise σ 0.010 at GOOD scaled by the preset) and `spikes` (`tools/simulator/labhouse.py:800-824`: measurement-only impulses, ground truth untouched) | Readings fluctuate within the configured measurement uncertainty (`SimulatorConfig.quality`, `channel_quality`, `spike_windows` — `tools/simulator/scenario.py:44-92`) | The noise model absorbs the fluctuation: **no** spurious events are raised; a step inside the noise floor is refused rather than flagged; the false-event count over the window stays inside the configured uncertainty band | `tests/simulation/test_t05_noise.py` | `H8` — fixture only | REQ-MEASURE-007, REQ-CONFIDENCE-006 |
| **T06** | **Multiple Simultaneous Devices** — several devices change at nearly the same time; joint inference and confidence-aware attribution | **existing** `automation_burst` (`tools/simulator/labhouse.py:527-545`: `SimulatorConfig.simultaneous_event_batches=12`, dense simultaneous batches; `inject_simultaneous_events` at `tools/simulator/scenario.py:159-164`) | Several devices change at nearly the same time | **Joint inference** attributes the combined change across the devices instead of emitting one independent event per device; per-device confidences are reported **separately**, never blended (R5); the sum of attributed deltas equals the measured delta within tolerance | `tests/simulation/test_t06_multiple_simultaneous.py` | `G2`, `E5`, `G5` — fixture only | REQ-DISAGG-002, REQ-DISAGG-006, REQ-EVENT-002, REQ-TOPO-007 |
| **T07** | **Non-identifiable Devices** — two devices always operate together and cannot be separated; a group estimate or a non-identifiable status | **existing** `ambiguous_pair` (`tools/simulator/labhouse.py:548-598`: two `SimDeviceType.STEP` devices, `amb_a` / `amb_b`, 900 W each, on the **same** `ct_main_utility_plug_ct1`, identical step schedules at 300 s and 1800 s, `simultaneous_event_batches=0`) | Two devices always operate together and cannot be separated | The output is a **group estimate** or an explicit **non-identifiable** status, with a range where a range is meaningful; **no** per-device numbers are fabricated; the two devices collapse to one group rather than two independent estimates | `tests/simulation/test_t07_non_identifiable_devices.py` | `I1`, `I4`, `F8` — fixture only | REQ-DISAGG-002, REQ-DISAGG-003, REQ-DISAGG-004, REQ-DISAGG-005 |
| **T08** | **Variable Power Device** — a dimmer or variable-speed device with a non-linear power curve; an adjustable model, not forced linear behaviour | **existing** `dynamic` (`tools/simulator/labhouse.py:422-438`: `lighting_1` is `SimDeviceType.DIMMABLE` → `DimmableProfile`, a **convex** brightness→power curve `max_power * level**1.6` — `tools/simulator/behavior.py:155-190`) and `hvac_modulation` (thermostat cycling) | A dimmer or variable-speed device with a non-linear power curve | An **adjustable (non-linear)** model is fitted and its residual is within tolerance; a forced-linear fit is rejected or reported as insufficient; the device power tracks the measurement rather than a learned constant | `tests/simulation/test_t08_variable_power_device.py` | `A5`, `D5` — fixture only | REQ-POWER-001 |
| **T09** | **Delayed Device Response** — HVAC, blinds or similar equipment responds after a delay; correlation respects the response model | **existing** `delayed_ct` (`tools/simulator/labhouse.py:725-746`: `channel_delay_s={"ct_sub_office_outlet": 20.0}` — the CT sample arrives late while the device state flips on time) and `hvac_modulation`; `SimulatorConfig.state_lag_s` (`tools/simulator/scenario.py:62-70`, applied at `:428-430`) delays the HA state stream independently of the CT path | HVAC, blinds or similar equipment responds after a delay | Correlation **respects the response model**: the change is attributed at the configured lag, not at zero lag; a zero-lag attribution would place the event on the wrong channel, and the test distinguishes the two; the late value is flagged, not used as fresh | `tests/simulation/test_t09_delayed_device_response.py` | `H6`, `G10` — fixture only | REQ-EVENT-003 |
| **T10** | **Unknown Device Discovery** — an unknown load repeatedly creates a recognisable power pattern with no HA state change; an unknown load profile is discovered | **existing** `unknown_load` (`tools/simulator/labhouse.py:404-419`: `unknown_load_l1`, `unknown_load_l2`, `SimDeviceType.UNKNOWN` with `attributable: False`) — **but the current `UnknownLoadProfile` is a bounded random walk** (`tools/simulator/behavior.py:513-533`), which is *not* a recognisable repeating pattern. A **recurring-pattern profile** is required. | An unknown load repeatedly creates a recognisable power pattern with **no HA state change** | An **unknown-load profile is discovered** and persisted with a stable signature; the repeat is not re-reported as a new event each cycle; the unknown-load slack is never written to a known device (R9); the device never becomes a confirmed topology row (R4) | `tests/simulation/test_t10_unknown_device_discovery.py` | none — **new coverage** | REQ-POWER-005, REQ-EVENT-001, REQ-RESIDUAL-004, REQ-RESIDUAL-006, REQ-LEARNING-004, REQ-MAPPING-004, REQ-STORAGE-005 |
| **T11** | **Smart Plug Relocation** — a monitored plug moves from circuit A to circuit B; a candidate topology change, validation, a new topology version, preserved history | **new** `plug_relocation` (a monitored device whose `circuits` mapping changes from one branch CT to another partway through the run) | A monitored plug moves from circuit A to circuit B; the power pattern follows the plug | A **candidate topology change** is proposed (R4); validation runs and the candidate is confirmed or rejected on evidence, not by fiat; a **new topology version** is created; the **previous version's history is preserved**, never rewritten (R16); no duplicate attribution results | `tests/simulation/test_t11_smart_plug_relocation.py` | none — **new coverage** | REQ-CHANGE-001, REQ-CHANGE-002, REQ-CHANGE-004, REQ-TOPO-008, REQ-TOPO-009 |
| **T12** | **Reader Relocation or Replacement** — a reader is moved, replaced, recalibrated or reversed; a configuration change is detected or registered, affected assumptions invalidated, reconciliation safe | **new** `reader_relocation` (a channel whose `ct_id` / `entity_id` / physical position changes partway through the run), with the existing `reversed_orientation` (`tools/simulator/labhouse.py:845-866`: `polarity_flips` at 1200 s, declared polarity stays normal) and `negative_ct` (`tools/simulator/labhouse.py:869-892`: a physically inverted CT for the whole run) as the orientation and calibration sub-cases | A reader is moved, replaced, recalibrated or reversed | A **configuration change is detected or registered**; affected assumptions (scale, orientation, leg pairing) are **invalidated**; reconciliation is safe — no duplicate attribution, no history rewrite (R16); the mid-run re-clamp is flagged as an orientation change, not clipped to 0 W or read as export | `tests/simulation/test_t12_reader_relocation.py` | none (the nearest row, `C9` "Reversed orientation / direction change", is **PARTIAL** and only covers *static* inversion — `docs/test_scenario_matrix.md:68`) — **new coverage** | REQ-CHANGE-001, REQ-CHANGE-002, REQ-CHANGE-004, REQ-CHANGE-007, REQ-TOPO-002, REQ-TOPO-008, REQ-TOPO-009, REQ-EVENT-007, REQ-STORAGE-012 |
| **T13** | **240 V Split-Phase Load** — the same physical load represented through paired leg measurements; no duplicate attribution | **existing** `unbalanced` (`tools/simulator/labhouse.py:502-524`: a 240 V load on two leg CTs at a physical 70/30 split, `double_pole=True`) and `noisy_legs` (both legs metered, degraded quality) | The same physical load represented through paired leg measurements | The load is attributed **once**, not once per leg; the leg pair is atomic (R7); the two leg readings sum to the device power within tolerance; a declared phase split is never used to "correct" two real readings | `tests/simulation/test_t13_split_phase_load.py` | `A2`, `B8`, `C2` — fixture only | REQ-MEASURE-005, REQ-TOPO-001, REQ-MAPPING-002 |
| **T14** | **Missing and Delayed Data** — readers become unavailable or deliver data late; an unknown/stale measurement state, not zero-power assumptions | **existing** `missing_intervals` (`tools/simulator/labhouse.py:776-797`: `missing_windows` on `ct_main_lighting_ct1` — **no sample produced at all**) and `irregular_sampling` (`tools/simulator/labhouse.py:749-773`: per-channel cadences 5 s / 15.7 s / 30 s / 60 s); `outage_windows` delivers a **stale value** instead, which is a different state (`tools/simulator/scenario.py:75-79`) | Readers become unavailable or deliver data late | An **unknown / stale measurement state** is recorded — never a zero-power assumption (R3); the two states are distinguishable (a gap produces no sample; an outage delivers a stale repeat flagged `stale=True`); a gap accrues **no** energy; a late sample is attributed to its correct sample time, not its arrival time | `tests/simulation/test_t14_missing_delayed_data.py` | `C8`, `H9`, `G6` — fixture only | REQ-MEASURE-010, REQ-EVENT-006, REQ-EVENT-008 |
| **T15** | **Bidirectional Power** — solar or another source creates reverse power flow; signed measurement handling without invalid non-negative-load assumptions | **existing** `negative_ct` (`tools/simulator/labhouse.py:869-892`: `ct_sub_l1` physically inverted for the whole run, `Polarity.INVERTED`, sign corrected exactly once) and `reversed_orientation` (mid-run sign flip with normal declared polarity). **Note:** the reference topology has **no** solar or generation device — a true export source does not exist in the simulator today (§5.3). | Solar or another source creates reverse power flow | Reverse power is recorded **signed** (negative) and is never clamped to zero; the sign is corrected exactly once (R10, R1); no invalid non-negative-load assumption is applied; true export stays negative after reconciliation; the residual algebra stays signed (R1) | `tests/simulation/test_t15_bidirectional_power.py` | `C7`, `F5` — **new coverage** (the reverse-flow *direction* is not asserted) | REQ-MEASURE-004, REQ-MEASURE-006, REQ-RESIDUAL-001 |

### 5.2 Stratum reuse summary

| Reuses an existing stratum | Needs a new stratum |
|---|---|
| T04 `standby`, T05 `noisy_legs` / `spikes`, T06 `automation_burst`, T07 `ambiguous_pair`, T08 `dynamic` / `hvac_modulation`, T09 `delayed_ct` / `hvac_modulation`, T10 `unknown_load` (+ a new recurring-pattern profile), T13 `unbalanced` / `noisy_legs`, T14 `missing_intervals` / `irregular_sampling`, T15 `negative_ct` / `reversed_orientation` | T01 `single_child_containment`, T02 `multi_child_contributions`, T03 `unobserved_parent_load`, T11 `plug_relocation`, T12 `reader_relocation` |

**Ten scenarios reuse an existing stratum; five need a new one.** T10 and T15 reuse a stratum but need an extended *profile* (see §5.3).

### 5.3 The honest limitation: three fixtures are implementation work, not configuration

`SimulatorConfig` (`tools/simulator/scenario.py:44-92`) carries `seed`, `minutes`, `interval`, `quality`, `channel_intervals`, `inverted_channels`, `drifting_channels`, `channel_quality`, `outage_windows`, `missing_windows`, `spike_windows`, `polarity_flips`, `state_lag_s`, `channel_delay_s`, `include_unknown_loads`, `simultaneous_event_batches` and `factor_channels`. **It has no relocation and no replacement capability.** The following therefore require *new simulator code*, not a configuration change:

| Scenario | Why configuration is not enough | Work needed |
|---|---|---|
| **T10** Unknown Device Discovery | `UnknownLoadProfile` (`tools/simulator/behavior.py:513-533`) is a bounded random walk with knots every 300–1800 s. A random walk is not a *recognisable repeating pattern*, so nothing can be "discovered" as a stable profile. A recurring-pattern profile (e.g. a periodic duty cycle at a fixed period) must be added to `tools/simulator/behavior.py` and selected by `SimDeviceType` or by fixture metadata. | New profile class + a stratum that uses it |
| **T11** Smart Plug Relocation | No knob moves a device between circuits. `HouseholdTopology.true_ct_powers` (`tools/simulator/topology.py:139-157`) aggregates a device's **static** `circuits` map, so a mid-run move cannot be expressed today. | A time-varying circuit assignment in the simulator, or a two-phase scenario stitched at the tick boundary |
| **T12** Reader Relocation or Replacement | `polarity_flips` (`tools/simulator/scenario.py:88`) flips the *sign* of a channel mid-run — it does not move, replace or recalibrate the reader, and it keeps the same `ct_id` and `entity_id`. `inverted_channels` is install-time. | A channel-identity change (new `ct_id` / `entity_id`, or a calibration factor change) within a run |

**Rule.** T10, T11 and T12 are **new coverage** with **no existing matrix row** (`01-requirements.md` §4.2). Their fixtures do not exist today and are implementation work. A plan that claims otherwise is wrong.

**Rule.** T15's canonical scenario says "solar or another source creates reverse power flow". The reference topology contains **no generation device** (`tools/simulator/topology.py` defines only loads; there is no solar or PV entry). `negative_ct` and `reversed_orientation` produce a *negative reading* but not a *generation source*. The test therefore proves the signed-handling contract against a negative reading, and the "solar" framing is recorded as **OD-96** — a generation device in the reference topology is a scope question for the simulator owner, not something the test plan may invent.

### 5.4 Notes on the set

- **T10, T11 and T12 have no existing matrix row** (`01-requirements.md` §4.2) and therefore need new rows *and* new fixtures. The nearest existing row, `C9` ("Reversed orientation / direction change"), is **PARTIAL** and covers only *static* inversion (`docs/test_scenario_matrix.md:68`).
- **T01, T02, T03, T04 and T15 are "new coverage" over existing rows** — the rows exist but do not assert the canonical scenario, so the new strata (`single_child_containment`, `multi_child_contributions`, `unobserved_parent_load`) carry the exact canonical setup.
- **T05, T06, T07, T08, T09, T13 and T14 are "fixture only"** — the behaviour is already covered; what they need is a named, deterministic builder, per the definition in `01-requirements.md` §4.2 (the Status column, lines 1402-1416).
- Each new stratum must be added to `REQUIRED_STRATA` (`tests/fixtures/labhouse.py:32-54`) *and* referenced from `MATRIX_STRATA` (`:59-144`), otherwise `assert_matrix_coverage()` fails by design (`:170-207`).
- **REQ-TEST-003** (`01-requirements.md` §4.3, line 1472) applies to all of `T01`–`T15`; §12's A36 row is its acceptance vehicle.

**Rule.** The "status" column distinguishes *new coverage* (a behaviour not previously asserted) from *fixture only* (a behaviour already asserted, re-run under a named deterministic builder for traceability). Both are required; only the former closes a matrix gap.

---

## 6. Unit, integration and synthetic test specifications

### 6.1 Unit tests (LAYER-1)

**In scope — pure functions over contract objects.** Parsers and serialisers, schema validation, unit conversion, power and energy arithmetic, the signed-residual subtraction (`engine/.../models/residual_power.py:298` `_signed_residual`), candidate ranking and confidence computation, topology graph algorithms (cycle prevention, hierarchy depth, edge selection), factor application, timebase and windowing, device-role and alias handling, entity mapping (the mapping-layer unit tests close the A3/G4 role cluster, §3.3), and the truth-key classifier (`tools/simulator/frames.py:75`).

**Unit test conventions** (the repository's existing convention, enforced by review):

- No I/O. No filesystem, no network, no database handle.
- No dependence on the system clock. Time is injected.
- No dependence on global random state. The seeded fixture is `rng` (`tests/conftest.py:49`), a `random.Random(20261105)` instance — "tests must not depend on global random state".
- Fixtures from `tests/unit/conftest.py`: `BASE_TIME` (`:15`), `make_sample` (`:18`), `binary_samples` (`:60`), `series` (`:93`, at the audited 15.7 s cadence), `FakeWindow` (`:102`), `windows_over` (`:113`).
- Contract objects in, contract objects out. No dict-of-any-shape assertions.

**Deliberately not unit-tested.** Anything that needs a database, an API server, a Home Assistant stub, the simulator's corruption pipeline, or a timer. Those move to LAYER-2/LAYER-3/LAYER-4. A behaviour that can only be observed with a database present is not a unit test; a behaviour that can only be observed with a truth present is not a unit test either — it belongs to LAYER-3.

**Rule.** If a unit test needs a `tmp_path`, a socket, or `asyncio.sleep`, it is not a unit test and belongs to another layer.

### 6.2 Integration tests (LAYER-2)

| Boundary | What is asserted | Vehicle |
|---|---|---|
| engine↔database | Rows written by the engine read back with identical values; a rejected write leaves no partial row | `tests/integration/test_engine_database.py` |
| engine↔API | The API response shape matches the contract schema; the schema-sync gate (`export_schemas.py`) stays green | `tests/integration/test_api_contract.py` |
| engine↔HA source | Entity metadata is parsed and mapped without a live HA | `tests/integration/test_ha_source_mapping.py` |
| engine↔integration | The HA integration's setup, options flow and coordinator update behave deterministically | `tests/integration/test_integration_flow.py` |
| gateway↔engine | The gateway's payload is accepted, de-duplicated and acknowledged | `tests/integration/test_gateway_engine.py` |

**Stub-HA constraint.** `homeassistant` is **not installed locally**, so integration tests are static/AST or stub-HA only. Two acceptable forms:

1. **Static/AST**: parse the integration module's source and assert the structural contract (decorators registered, entity list present, `async_setup_entry` signature). No runtime import of `homeassistant`.
2. **Stub-HA**: install a minimal in-process stub module providing exactly the symbols the integration imports, then run the setup path against it.

**Rule.** No integration test may import `homeassistant` for real. A test that needs the real package is marked `skipif` with the reason "homeassistant not installed locally" and is counted as *not run* in the acceptance evidence, never as passed.

**Rule.** The end-to-end layer uses a real `Database` (`tests/end_to_end/conftest.py:22`, built on `tmp_db_path` from `tests/conftest.py:55`) with a topology saved via `db.save_topology(...)` from `sample_topology.to_contract_topology()` — not an in-memory mock.

### 6.3 Synthetic (simulator) tests (LAYER-3)

The synthetic layer is the only layer where per-device ground truth exists. The truth is computed independently of the measurement pipeline (`tools/simulator/scenario.py:213`) and delivered to the algorithm only through `observation_view` (`tools/simulator/frames.py:183`).

**Rule.** Every synthetic test calls `assert_no_ground_truth_keys` (`tools/simulator/frames.py:105`) on the observation payload before it asserts anything else. A leak fails the test at the first line, not at the assertion.

**Existing strata** (21, `tools/simulator/labhouse.py:898`): `dedicated`, `shared`, `standby`, `unknown_load`, `dynamic`, `factor2`, `noisy_legs`, `unbalanced`, `automation_burst`, `ambiguous_pair`, `hvac_modulation`, `ev_rate_change`, `fridge_cycling`, `motor_startup`, `delayed_ct`, `irregular_sampling`, `missing_intervals`, `spikes`, `main_subpanel_overlap`, `reversed_orientation`, `negative_ct`.

**New strata required by this plan** (7): `single_child_containment` (T01), `multi_child_contributions` (T02), `unobserved_parent_load` (T03), `plug_relocation` (T11), `reader_relocation` (T12), `factor_timeline` and `factor_misconfigured` (the non-T group B gaps, §3.3). Their verified shapes and the three-place registration contract are in §4.3-§4.4.

**Determinism.** `LabHouse.signature()` (`tools/simulator/labhouse.py:214`) is a SHA-256 over the observation stream — equal seeds give equal digests. Every synthetic test asserts the signature it expects, so a silent change in the simulated household fails loudly. `LabHouse` also exposes `observations()` (`:122`) and `ground_truth()` (`:126`) separately, plus `flags()` (`:141`), `channel_counts()` (`:182`) and `summary()` (`:220`).

**Simulator configuration surface** available to the synthetic tests (`tools/simulator/scenario.py:44-92`): `seed`, `minutes`, `interval`, `quality`, `channel_intervals`, `inverted_channels`, `drifting_channels`, `channel_quality`, `outage_windows`, `missing_windows`, `spike_windows`, `polarity_flips`, `state_lag_s`, `channel_delay_s`, `include_unknown_loads`, `simultaneous_event_batches`, `factor_channels`. `factor_channels` is absent by default on purpose, so a factor fixture must enable it explicitly. **There is no relocation or replacement knob** — see §5.3.

**Note.** `missing_windows` produces *no sample at all*, which is distinct from `outage_windows`, which delivers a *stale value*. T14 depends on this distinction.

**Household scenario fixtures** available: `fast_scenario` (`tests/conftest.py:83`, `HouseholdScenario(seed=1234, minutes=10, interval=15)`), `household_scenario` (`:90`), `lab_house` (`:105`), `lab_houses` (`:132`), `hot_tub_fixture` (`:151`).

### 6.4 Capture-dependent tests (explicitly skipped)

Nine live-capture tests skip without a local `WATTMOSAIC_REAL_DB` capture (`tests/simulation/test_real_topology.py`; the nine gate names are listed in `docs/limitations_v2.md` §3).

**Rule.** These skips stay visible in the test log. A skipped capture test is reported as *not run*, and the acceptance evidence in §12 states which figures are therefore simulator-only.

The capture path lives in `tools/audit/`: `collect_samples.py` (`--minutes` default 45, `--out data/raw/ct_events.jsonl`), `profile_samples.py`, `passive_validation.py`, `build_topology.py`, `derive_state_entities.py`, `replay_capture.py`, `audit_environment.py`, `ha_client.py`.

### 6.5 Replay tests (LAYER-5)

**Rule.** Replaying stored evidence reproduces the same topology candidates and model versions **without rewriting history**.

| Assertion | Vehicle |
|---|---|
| Two replays of the same capture produce identical topology candidate sets (same edges, same confidences, same ordering) | `tests/simulation/test_replay_determinism.py` |
| The model version referenced by a historical inference is unchanged after a replay | `tests/integration/test_replay_no_rewrite.py` |
| Replaying a capture does not append new inference rows for the replayed window | `tests/integration/test_replay_no_duplicate_rows.py` |
| The replayed capture's derived state entities match the originally derived entities | `tests/simulation/test_replay_entity_equivalence.py` |

**Replay entry point.** `tools/audit/replay_capture.py` — it reads a read-only engine SQLite database via `--db`, or a JSONL capture via `--jsonl`, with `--minutes`, `--out` and `--max-age-s` (`:719-756`). Its `evaluation_shape` (`:643`) currently reports **empty per-device strata** with an honest note; §11.1 names the metrics that must fill those strata.

**Rule.** The replay path must never write to the source capture, never mutate the historical database it reads, and never re-fit a model on the replayed window. Replay is a *read-and-compare* operation.

---

## 7. Regression tests: the pinned invariants

Every rule in this section has a test that **fails when the rule is removed**. Each is proved by the negative-verification harness pattern of `tools/dev/negverify_nesting.py`: purge bytecode, run with `-p no:cacheprovider`, re-run on the restored file, and confirm the test fails in the mutated state and passes in the restored state.

| Rule | Statement | Test file | Mutation that must make it fail | Scenario link |
|---|---|---|---|---|
| **R1 Signed residual only** | The only permitted subtraction of load power from an aggregate is the single signed subtraction in `engine/.../models/residual_power.py:298` (`_signed_residual`). | `tests/unit/test_invariant_signed_residual.py` | Replace `_signed_residual` with an absolute-value subtraction; the test must fail. | T15 |
| **R2 Residual never redistributed** | A residual is never allocated back across the loads it was derived from. | `tests/unit/test_invariant_residual_no_redistribution.py` | Add a redistribution step; the test must fail. | T03, T04 |
| **R3 Missing ≠ 0** | An absent sample is recorded as missing. It is never imputed as zero. | `tests/unit/test_invariant_missing_not_zero.py` | Impute missing samples as zero; the test must fail. | T14 |
| **R4 Candidate ≠ confirmed** | A topology candidate is never persisted as a confirmed topology without confirmation. | `tests/unit/test_invariant_candidate_not_confirmed.py` | Promote candidates on creation; the test must fail. | T01, T02, T11 |
| **R5 No blended confidence** | No output exposes a single blended confidence across competing explanations; competing candidates carry distinct, separate confidences. | `tests/unit/test_invariant_no_blended_confidence.py` | Average two candidates' confidences into one field; the test must fail. | T06, T07 |
| **R6 One-deepest-CT attribution** | Where a device is reachable through more than one CT path, attribution uses exactly the one deepest CT. | `tests/unit/test_invariant_deepest_ct.py` | Attribute to the shallower or the aggregate CT; the test must fail. | T01, T02 |
| **R7 Leg-pair atomicity** | A split-phase device's two legs are attributed as one atomic pair; a half-present pair is reported as incomplete, not as one leg. | `tests/unit/test_invariant_leg_pair_atomic.py` | Attribute per leg; the test must fail. | T13 |
| **R8 Residual-split identity** | When a residual is split into components, the parts sum exactly to the original residual. | `tests/unit/test_invariant_residual_split_identity.py` | Introduce rounding drift in the split; the test must fail. | T02, T04 |
| **R9 Unknown-load slack never written to a device** | The unknown-load slack is never written to, or attributed to, a known device. | `tests/unit/test_invariant_unknown_slack.py` | Assign slack to the largest device; the test must fail. | T10 |
| **R10 Measurement multiplier applied once** | A measurement multiplier is applied exactly once per sample. | `tests/unit/test_invariant_multiplier_once.py` | Apply the multiplier twice; the test must fail. | T15 (measurement semantics) |
| **R11 The alias gets no row** | An alias is a naming convenience; it never produces a device row of its own. | `tests/unit/test_invariant_alias_no_row.py` | Materialise aliases as rows; the test must fail. | T04, T10, T13 (mapping layer) |
| **R12 `two_second_assumption_supported = false`** | The published finding stands: the two-second assumption is *not* supported by the collected evidence (`PROJECT_STATUS.md:28`). | `tests/unit/test_invariant_two_second_assumption.py` | Flip the flag to `true`; the test must fail. | — |
| **R13 No ground-truth leak** | No observation handed to an algorithm contains a ground-truth key (`tools/simulator/frames.py:105`). | `tests/unit/test_invariant_no_truth_leak.py` | Inject a truth key into an observation; the test must fail. | all synthetic |
| **R14 Stratum coverage is enforced** | Every required stratum is referenced by a row and every row is backed by a stratum (`tests/fixtures/labhouse.py:170-207`). | `tests/unit/test_invariant_matrix_coverage.py` | Add an unreferenced stratum or an unbacked row; the test must fail. | all synthetic |
| **R15 No cycle in the topology graph** | The asserted topology graph is acyclic; a proposed edge that would create a cycle is rejected. | `tests/unit/test_invariant_no_cycle.py` | Allow cycle-forming edges; the test must fail. | T01, T02, T11 |
| **R16 History is never rewritten** | A superseded topology version is immutable; a new version is appended. | `tests/unit/test_invariant_no_history_rewrite.py` | Overwrite the previous version's rows on change; the test must fail. | T11, T12 |

**Rule.** The invariant tests carry no skip markers and no conditional execution. They run in the `unit_tests` gate every time.

### 7.1 Negative-verification record

**Rule.** For each rule above, the mutation test is executed at least once and the failing output is captured in the validation record. A rule whose test cannot be made to fail by mutation is not a proven invariant; it is deleted or rewritten.

---

## 8. Performance tests

### 8.1 What "representative" means here

**Rule.** A performance figure from a toy fixture is reported as a micro-benchmark, never as a capacity figure. A representative workload is one of:

- **R1 — simulated household at the audited cadence:** a `LabHouse` household (`tools/simulator/labhouse.py:107`) at the audited 15.7 s cadence (`tests/unit/conftest.py:93` `series`), a realistic household topology, and at least 24 h of simulated time.
- **R2 — capture replay:** a real `WATTMOSAIC_REAL_DB` capture replayed through `tools/audit/replay_capture.py`.

### 8.2 Targets

| # | Metric | Target | Workload | Source of measurement | Status |
|---|---|---|---|---|---|
| P1 | Samples ingested per second | ≥ 500 samples/s sustained, no unbounded queue growth | R1 | `tests/perf/test_ingest_throughput.py` | new |
| P2 | Event processing latency (sample → inferred state) | p95 ≤ 2 s end-to-end in-process | R1 | `tests/perf/test_event_latency.py` | new |
| P3 | Database write latency | p95 ≤ 50 ms per batched write | R1 | `tests/perf/test_write_latency.py` | new |
| P4 | Database size growth | ≤ X bytes per channel per day; X is fixed at the start of the implementation and recorded in the validation record | R1 | `tests/perf/test_db_size_growth.py` | new (threshold is **OD-89**) |
| P5 | Query latency — `latest_inference_results()` | ≤ 250 ms warm at 343k rows | real-scale DB | `tests/perf/test_query_latency.py` | **measured today**: ~24 ms warm / ~590 ms cold at 343k rows (`docs/limitations_v2.md:221`) |
| P6 | Memory usage | no unbounded growth over a 24 h simulated run; steady-state RSS reported | R1 | `tests/perf/test_memory_profile.py` | new |
| P7 | CPU usage | reported as a percentage of one core, steady-state over 24 h | R1 | `tests/perf/test_cpu_profile.py` | new |
| P8 | Model reconciliation duration | p95 ≤ 30 s for a full household re-fit | R1 | `tests/perf/test_reconcile_duration.py` | new |
| P9 | Recovery after process restart | all state restored within 60 s of restart; no duplicate rows | R1 with a restart injected | `tests/perf/test_restart_recovery.py` | new |
| P10 | Behaviour during a temporary storage outage | writes are queued durably and flushed on recovery; no sample is silently dropped | R1 with the DB made unavailable | `tests/perf/test_storage_outage.py` | new |
| P11 | Behaviour during a reader outage | a failing reader does not block the writer; errors are surfaced | R1 with a reader failing | `tests/perf/test_reader_outage.py` | new |

**Rule.** P5 is the only performance figure that currently has a published measurement. Every other target is new work; the acceptance evidence must state which targets were measured and which were not.

### 8.3 Existing performance-test precedents

Two precedents exist in the repository and the new perf tests follow their shape:

- `tests/unit/test_health_probe_cost.py`
- `tests/unit/test_nesting_perf.py`

These run inside the `unit_tests` gate. The new perf tests that exceed the unit gate's time budget live in `tests/perf/` and are run by a scheduled job, with the fast subset (P2, P3, P5) kept inside `unit_tests` so a regression is caught on every change.

---

## 9. Storage, migration and data-lifecycle tests

### 9.1 Migration tests

`engine/.../storage/migrations.py` holds 11 migrations and `LATEST_VERSION` at `:792`. `Database.__init__` runs `apply_migrations` (`engine/.../storage/database.py:90`).

**Rule.** A prior-schema fixture must be built with **raw `sqlite3`**, never by constructing a `Database` — constructing it applies the migrations and destroys the prior state the test needs to exercise.

| Test | Assertion | File |
|---|---|---|
| Fresh install | A new database reaches `LATEST_VERSION` with the expected tables and indexes | `tests/unit/test_migrations_fresh.py` |
| Upgrade path | A raw-`sqlite3`-built database at version *n* upgrades to `LATEST_VERSION` with data intact | `tests/unit/test_migrations_upgrade.py` |
| Idempotence | Applying migrations twice is a no-op the second time | `tests/unit/test_migrations_idempotent.py` |
| Forward-only | Migration *n* → *n+1* never requires a downgrade path | `tests/unit/test_migrations_forward_only.py` |
| Data preservation | Existing rows survive the upgrade with values unchanged | `tests/unit/test_migrations_data_preserved.py` |

### 9.2 Retention, roll-ups and integrity

| Area | Assertion | File |
|---|---|---|
| Retention / prune | Rows older than the retention window are pruned; rows inside it are not; pruning is idempotent | `tests/integration/test_storage_retention.py` |
| Aggregate roll-up | Hourly/daily roll-ups equal the sum of their source rows within floating tolerance | `tests/integration/test_storage_rollup.py` |
| Roll-up re-run | Re-running a roll-up over an already-rolled-up window does not double-count | `tests/integration/test_storage_rollup_idempotent.py` |
| Integrity check | The integrity check reports OK on a healthy DB and names the specific corruption on a deliberately damaged DB | `tests/integration/test_storage_integrity.py` |
| Backup / restore round-trip | A backup of a populated DB restores logically identical content, including roll-ups and device rows | `tests/integration/test_storage_backup_restore.py` |

### 9.3 Crash recovery

**Rule.** The crash-recovery test writes a partial energy bucket, kills the writer without a clean close, restarts, and asserts that the bucket is either completed or rolled back — never left half-written and silently accepted.

- Test: `tests/integration/test_storage_crash_recovery.py`.
- Mechanism: the writer is interrupted between the bucket's header write and its row write; on restart, the recovery path reconciles the bucket.
- Assertion: no device row is attributed a partial bucket; the recovered bucket total is either the full pre-crash amount or absent.

---

## 10. UI tests

### 10.1 Static gate

`tools/dev/check_panel.py` implements 8 rules, including the `STRINGS` replay check at `:83-140`. This is the baseline UI gate.

### 10.2 API-binding check

**Rule.** The panel must render against the *real* API response shapes. The test takes a recorded API response body (contract-shaped) and asserts the panel's field accesses resolve to fields that actually exist in the shape.

- File: `tests/unit/test_panel_api_binding.py`.
- Mechanism: the response fixtures are generated from the contract schema (`export_schemas.py` is the schema-sync gate's source of truth), so a schema change the panel does not follow fails the test.

### 10.3 DOM-level check — the honest constraint

**There is no browser test harness in this repository today.** No Playwright, no Selenium, no headless browser dependency exists, and this plan does not add one.

**Rule.** No document, release note or validation record may claim browser-level UI coverage.

The proposal, adding **no node dependency**:

1. **Pure-JS assertion harness.** A small vanilla-JS module under `tools/panel/test/` that imports the panel's render functions and asserts on the produced DOM tree using the platform's own DOM implementation — either the panel's existing DOM builder or a minimal in-repo DOM shim written in plain JavaScript. Executed by the repository's existing JS check in the `unit_tests` gate path, not by a new runtime.
2. **Python-side fixture replay against the render functions' inputs.** The Python test captures the exact argument object each render function receives, serialises it to a fixture, and the JS harness renders the fixture and asserts on the output tree. This keeps the assertion logic testable without a browser while still exercising the real render path.

**Rule.** If the pure-JS harness cannot be implemented without a node dependency, the DOM-level check is reported as *not implemented*, and §10.4's manual checklist is the only UI evidence. It is not silently skipped.

### 10.4 Manual UI checklist

The manual checklist is run by a human against a live panel and recorded in the validation record. Each state must be reachable and legible:

| State | What must be visible |
|---|---|
| Loading | A loading indicator; no stale numbers presented as current |
| Empty | An explicit empty state, not a blank table and not zeros |
| Error | The error text, plus the fact that the displayed values are stale |
| Stale | Values marked as stale with their age |
| Offline | Offline indication; the last known values are marked as such |
| Degraded | Partial-degradation indication (e.g. one source unavailable) |

**Rule.** The empty state must never be visually identical to an all-zero state. This is the UI expression of "missing is never zero" (R3).

### 10.5 UI gate summary

| Check | Vehicle | Status |
|---|---|---|
| Static rules (8) | `tools/dev/check_panel.py` | exists |
| API binding to real response shapes | `tests/unit/test_panel_api_binding.py` | new |
| DOM-level render assertions | pure-JS harness, no node dependency | new, may be reported as not implemented |
| State checklist (loading/empty/error/stale/offline/degraded) | manual record | process, not automated |

---

## 11. Evaluation method

### 11.1 Topology evaluation

| Metric | Definition | Source of truth | Vehicle |
|---|---|---|---|
| Edge precision | Of the edges asserted, the fraction that exist in the reference topology | reference topology from `build_topology.py` / `derive_state_entities.py` (`tools/audit/`) | `tests/simulation/test_topology_metrics.py` |
| Edge recall | Of the reference edges, the fraction that are asserted | idem | idem |
| False containment rate | Asserted edges that assert a device belongs to a channel it does not belong to | idem | idem |
| Correct hierarchy | The asserted parent chain matches the reference for every asserted node | idem | idem |
| Cycle prevention | The asserted graph is acyclic | invariant R15 | `tests/unit/test_invariant_no_cycle.py` |
| Residual stability | The residual for a channel does not oscillate between device and channel attribution across consecutive windows | synthetic truth | `tests/simulation/test_residual_stability.py` |
| Change-detection accuracy | Detected topology changes vs actual changes across a re-configured run | synthetic truth | `tests/simulation/test_topology_change_detection.py` |
| Historical version integrity | Superseded topology versions are never rewritten | invariant R16 | `tests/unit/test_invariant_no_history_rewrite.py` |
| Recovery after a topology change | After a change is applied, attribution returns to the correct baseline within a bounded number of windows | synthetic truth | `tests/simulation/test_topology_change_recovery.py` |

**Rule.** These metrics fill the per-device strata that `tools/audit/replay_capture.py`'s `evaluation_shape` (`:643`) currently leaves empty, with its honest note. Until then, topology metrics are simulator-only.

### 11.2 Disaggregation evaluation

| Metric | Definition | Notes |
|---|---|---|
| Per-device power MAE | Mean absolute error per device, over windows where ground truth exists | Direct MAE baseline 12.73 W over 5 641 samples (`PROJECT_STATUS.md:1457-1463`) |
| Per-device power RMSE | Root-mean-square error per device | Reported alongside MAE; RMSE exposes outliers MAE hides |
| Energy estimation error | |∑ estimated energy − ∑ true energy| over a window | Energy, not power, is what a user bills against |
| Event detection precision | Of detected events, the fraction that are real events | |
| Event detection recall | Of real events, the fraction detected | |
| Unknown-load discovery | Real unknown loads found / all real unknown loads present | Baseline unknown load 0.0247 (`PROJECT_STATUS.md:1457-1463`) |
| Standby estimation | Error on the standby draw of devices in the `standby` stratum | T04 |
| Identifiability classification | Correctness of the "identifiable / not identifiable" decision | Reported as a confusion table, not a single score (T07) |
| Double-counting errors | Sum over devices of attributed power exceeding the measured channel power | Nested case baseline: nested double-count 0/229 (`PROJECT_STATUS.md:1457-1463`) |

**Rule.** Where separation is impossible, the reported outcome is a group estimate, a range, or an explicit "not identifiable" status. A per-device number is never invented. The identifiability classification metric exists precisely to make "not identifiable" a first-class, measurable outcome.

### 11.3 The training/evaluation separation rule

**Rule.** Training evidence and evaluation evidence come from disjoint event sets. Any figure computed on events that were used to fit a model is a **training figure** and may not be presented as verification.

**Mechanism.**

1. **Split at the event level, before fitting.** The split function is `split_holdout`; the validation logic is the R²-based check in `engine/.../learning/validation.py`.
2. The holdout window is fixed by seed and recorded in the validation record: window boundaries, seed, and the number of events on each side.
3. A fitted model reports its training figure and its holdout figure **side by side**, and the holdout figure is the one that may be quoted as an evaluation result.
4. Any publication that omits the split is invalid.

**Rule.** When the same events were used for both fitting and the claimed verification, that claim must be stated explicitly and labelled as in-sample. It is a defect to publish an in-sample figure without that label.

**Limitation, stated plainly.** Every published accuracy figure is simulator-measured. There is no real-house per-device ground truth (`docs/limitations_v2.md` §1). The published figures (`PROJECT_STATUS.md:1457-1463`) are:

| Figure | Value |
|---|---|
| Coverage | 0.7031 |
| False attribution | 0.0098 |
| Unknown load | 0.0247 |
| Direct MAE | 12.73 W (over 5 641 samples) |
| Inferred MAE | 1.61 W |
| Residual preserved | 0.9958 |
| Forced / over-attribution | 0 |
| Nested double-count | 0/229 |
| Baseline: flat | 0.0000 coverage |
| Baseline: main-only | 0.7034 coverage |
| Baseline: naive | 37.94 coverage / 0.6265 false attribution |
| Oracle | 1.0000 coverage |

These are the baseline the new work moves from, and they are simulator figures.

---

## 12. Acceptance criteria

Each row is a measurable threshold with a named source of measurement and a named test that produces it. "Baseline" cites the current published figure and the required direction of change.

| # | Metric | Threshold | Baseline | Direction | Source of measurement | Test |
|---|---|---|---|---|---|---|
| A1 | Topology edge precision | ≥ 0.95 | not published | — | simulation vs reference topology | `tests/simulation/test_topology_metrics.py` |
| A2 | Topology edge recall | ≥ 0.90 | not published | — | idem | idem |
| A3 | False containment rate | ≤ 0.01 | not published | — | idem | idem |
| A4 | Double-counting errors | 0 occurrences | nested double-count 0/229 | hold | simulation truth | `tests/simulation/test_no_double_count.py` |
| A5 | Residual preserved | ≥ 0.995 | 0.9958 | hold | simulation truth | `tests/simulation/test_residual_preservation.py` |
| A6 | Direct per-device MAE | ≤ 12.73 W | 12.73 W | hold or improve | simulation truth, 5 641 samples | `tests/simulation/test_mae.py` |
| A7 | Inferred per-device MAE | ≤ 1.61 W | 1.61 W | hold or improve | idem | `tests/simulation/test_inferred_mae.py` |
| A8 | False attribution rate | ≤ 0.0098 | 0.0098 | hold or improve | idem | `tests/simulation/test_false_attribution.py` |
| A9 | Unknown-load rate | ≤ 0.0247 | 0.0247 | hold or improve | idem | `tests/simulation/test_unknown_load.py` |
| A10 | Forced / over-attribution | 0 | 0 | hold | idem | `tests/simulation/test_forced_attribution.py` |
| A11 | Coverage | ≥ 0.7031 and > main-only baseline 0.7034 | 0.7031 | improve past the main-only baseline | idem | `tests/simulation/test_coverage.py` |
| A12 | Event detection precision / recall | both ≥ 0.90 | not published | — | simulation truth | `tests/simulation/test_event_detection.py` |
| A13 | Identifiability classification (T07) | ≥ 0.95 correct on the `ambiguous_pair` stratum | not published | — | simulation truth | `tests/simulation/test_t07_non_identifiable_devices.py` |
| A14 | Missing-vs-zero correctness (T14) | 0 misclassifications | — | — | simulation truth | `tests/simulation/test_t14_missing_delayed_data.py` |
| A15 | Single child containment (T01) | every compatible child→parent delta produces a candidate; 0 auto-confirmations; candidate confidence rises with each compatible repeat | — | — | simulation truth | `tests/simulation/test_t01_single_child_containment.py` |
| A16 | Multiple child contributions (T02) | 2 candidate edges proposed with separate confidences; parent attribution = child A + child B within tolerance; 0 residual events on the parent | — | — | simulation truth | `tests/simulation/test_t02_multiple_child_contributions.py` |
| A17 | Unobserved-parent residual (T03) | 100 % of unexplained parent deltas become residual events; 0 topology rejections; 0 changes forced onto a child | — | — | simulation truth | `tests/simulation/test_t03_unobserved_parent_load.py` |
| A18 | Baseline standby (T04) | ≤ 1 baseline event per baseline window; 0 repeated unknown events; 0 background pushed into a device | — | — | simulation truth | `tests/simulation/test_t04_baseline_standby.py` |
| A19 | Noise absorption (T05) | 0 spurious events above the configured noise floor; 0 spikes learned as device power | — | — | simulation truth | `tests/simulation/test_t05_noise.py` |
| A20 | Joint inference (T06) | sum of attributed deltas = measured delta within tolerance; 1 joint attribution, not N independent events; 0 blended confidences | — | — | simulation truth | `tests/simulation/test_t06_multiple_simultaneous.py` |
| A21 | Variable-power model (T08) | residual within tolerance under a non-linear curve; 0 forced-linear fits | — | — | simulation truth | `tests/simulation/test_t08_variable_power_device.py` |
| A22 | Delayed-response correlation (T09) | the change is attributed at the configured lag; a zero-lag attribution fails the test | — | — | simulation truth | `tests/simulation/test_t09_delayed_device_response.py` |
| A23 | Unknown-device discovery (T10) | the repeated pattern is discovered once and persisted with a stable signature; 0 slack written to a known device | — | — | simulation truth | `tests/simulation/test_t10_unknown_device_discovery.py` |
| A24 | Change detection and versioning (T11, T12) | 100 % of relocations and reader changes detected or registered; 0 history rewrites; 0 duplicate attributions | — | — | simulation truth + DB | `tests/simulation/test_t11_smart_plug_relocation.py`, `test_t12_reader_relocation.py` |
| A25 | Split-phase attribution (T13) | 0 duplicate attributions of the same physical load; leg sum = device power within tolerance | — | — | simulation truth | `tests/simulation/test_t13_split_phase_load.py` |
| A26 | Signed handling (T15) | reverse flow recorded negative; 0 clamps to zero; sign corrected exactly once | — | — | simulation truth | `tests/simulation/test_t15_bidirectional_power.py` |
| A27 | `latest_inference_results()` query latency | ≤ 250 ms warm at 343k rows | ~24 ms warm / ~590 ms cold | hold warm; **improve cold** | real-scale DB | `tests/perf/test_query_latency.py` |
| A28 | Ingest throughput | ≥ 500 samples/s | not published | — | R1 workload | `tests/perf/test_ingest_throughput.py` |
| A29 | Event processing latency | p95 ≤ 2 s | not published | — | R1 workload | `tests/perf/test_event_latency.py` |
| A30 | Migration upgrade correctness | 11 migrations upgrade a raw-`sqlite3`-built prior DB with data intact | — | — | `LATEST_VERSION` (`:792`) | `tests/unit/test_migrations_upgrade.py` |
| A31 | Roll-up identity | roll-up sum equals source sum, 0 drift | — | — | DB | `tests/integration/test_storage_rollup.py` |
| A32 | Crash recovery | 0 partially-attributed buckets after an injected crash | — | — | DB | `tests/integration/test_storage_crash_recovery.py` |
| A33 | Backup/restore round-trip | logical content identical | — | — | DB | `tests/integration/test_storage_backup_restore.py` |
| A34 | Invariant suite | all 16 rules (R1–R16) pass, and each fails under its mutation | — | — | negative-verification record | `tests/unit/test_invariant_*.py` |
| A35 | Stratum coverage | 26 strata (21 existing + 5 canonical) referenced; 0 unreferenced; 0 unbacked rows | 21/21, 0 unreferenced | extend | `assert_matrix_coverage()` | `tests/unit/test_invariant_matrix_coverage.py` |
| A36 | T01–T15 | all 15 canonical scenarios from `01-requirements.md` §4.1 have an executable, passing test with a stated expected outcome | 0 of 15 have a dedicated named test | +15 | scenario catalogue `01-requirements.md` §4.1-§4.2 | `tests/simulation/test_t*.py` |
| A37 | Two-second assumption | remains `false` | `false` (`PROJECT_STATUS.md:28`) | hold | flag | `tests/unit/test_invariant_two_second_assumption.py` |
| A38 | Harness green | all 12 required gates pass | — | — | `tools/dev/verify.py` | orchestrator-run |

**Rule.** A threshold whose baseline is "not published" is a **new** measurement. The first run establishes the baseline; the acceptance threshold stands from that point on. A threshold is never relaxed after the fact without an open decision.

**Rule.** Every acceptance row states whether it is simulator-measured or capture-measured. Rows whose capture measurement is skipped are reported as *not run*, not as passed.

---

## 13. The release gate

**Rule.** A change is verified only when the orchestrator runs the harness independently. The developer's own run is necessary but not sufficient (`docs/implementation_plan_v2.md:333`; `docs/agent_assignments.md:26-28`).

### 13.1 Command sequence

```bash
# 0. Clean state — no stale bytecode can mask a failure
find . -name '__pycache__' -type d -prune -exec rm -rf {} +

# 1. The full harness: 12 required gates (+ optional config_valid with HA_TOKEN)
python tools/dev/verify.py

# 2. Explicit layer runs, for an auditable per-layer log
python -m pytest tests/unit -q
python -m pytest tests/integration -q
python -m pytest tests/simulation -q
python -m pytest tests/end_to_end -q

# 3. The invariant suite on its own
python -m pytest tests/unit -q -k invariant

# 4. The synthetic scenario suite
python -m pytest tests/simulation -q -k 't01 or t02 or t03 or t04 or t05 or t06 or t07 or t08 or t09 or t10 or t11 or t12 or t13 or t14 or t15'

# 5. Negative verification: each guard removed, its test watched to fail
python tools/dev/negverify_nesting.py

# 6. Lint (also a gate, run standalone for a clean error log)
python -m ruff check engine tools tests

# 7. Schema sync, docs freshness, manifests, release metadata
python tools/dev/export_schemas.py
python tools/dev/render_validation_docs.py --check
python tools/dev/check_manifests.py
python tools/dev/check_release.py
```

### 13.2 Rules for the gate

- **Rule.** The orchestrator runs `python tools/dev/verify.py` from a clean checkout of the branch under test. The developer's own green run does not substitute.
- **Rule.** The harness pins `PYTHONPATH` to this checkout's `engine/` (`tools/dev/verify.py:67-78`), so a worktree cannot pass against a parent's code. This behaviour is preserved.
- **Rule.** Skipped tests are listed in the verification record by name. Nine capture tests skip without `WATTMOSAIC_REAL_DB` (`docs/limitations_v2.md` §3) and the HA-dependent tests skip without `homeassistant` installed; both are reported as *not run*.
- **Rule.** A gate that is skipped is a failed gate unless the skip is recorded as a limitation in the validation record.

---

## 14. Requirement-area coverage matrix

The requirement areas are those of `01-requirements.md` §5 (coverage check against the target brief). The "Synthetic" column is derived from `01-requirements.md` §4.3, which maps every scenario-dependent requirement to its canonical `T##` identifier. A reviewer reads this table to see which requirement areas have no test.

| Requirement area | Requirements | Unit | Integration | Synthetic (per §4.3) | Replay | Regression | Perf | Storage | UI |
|---|---|---|---|---|---|---|---|---|---|
| Measurement semantics | REQ-MEASURE-001…010 | ✅ | ✅ | ✅ T05, T13, T14, T15 | ✅ | ✅ R1, R3, R7, R10 | — | — | — |
| Measurement topology | REQ-TOPO-001…011 | ✅ | ✅ | ✅ T01, T02, T03, T06, T11, T12, T13 | ✅ | ✅ R4, R5, R6, R15, R16 | — | ✅ | — |
| Residuals | REQ-RESIDUAL-001…008 | ✅ | ✅ | ✅ T03, T04, T10, T15 | ✅ | ✅ R1, R2, R8, R9 | — | ✅ | — |
| Device and load models | REQ-POWER-001…008 | ✅ | ✅ | ✅ T01, T04, T08, T10 | ✅ | ✅ R2 | — | — | — |
| Disaggregation | REQ-DISAGG-001…007 | ✅ | ✅ | ✅ T01, T06, T07 | ✅ | ✅ R5, R6, R9 | — | — | — |
| Event model | REQ-EVENT-001…008 | ✅ | ✅ | ✅ T01, T02, T06, T09, T10, T12, T14 | ✅ | ✅ R3 | ✅ P2 | — | — |
| Storage and lifecycle | REQ-STORAGE-001…012 | ✅ | ✅ | ✅ T10, T12 | ✅ | ✅ R16 | ✅ P4 | ✅ | — |
| Change detection and reconciliation | REQ-CHANGE-001…007 | ✅ | ✅ | ✅ T11, T12 | ✅ | ✅ R4, R15, R16 | ✅ P8 | ✅ | — |
| Learning | REQ-LEARNING-001…004 | ✅ | ✅ | ✅ T10 | ✅ (holdout) | — | ✅ P8 | ✅ | — |
| Entity mapping and roles | REQ-MAPPING-001…004 | ✅ | ✅ | ✅ T04, T10, T13 | ✅ | ✅ R11 | — | — | — |
| Confidence semantics | REQ-CONFIDENCE-001…006 | ✅ | ✅ | ✅ T05 (T06, T07 exercise it too) | ✅ | ✅ R5 | — | — | — |
| Test obligations | REQ-TEST-001…004 | — | — | ✅ T01–T15 | ✅ | ✅ R13, R14 | — | — | — |
| HA integration and gateway | — | ✅ | ✅ (stub) | ✅ `automation_burst` | ✅ | — | — | — | — |
| Panel rendering and states | — | — | — | — | — | — | — | — | ✅ (static + binding) |
| Browser-level UI rendering | — | — | — | — | — | — | — | — | ❌ **no harness** |
| Real-capture per-device accuracy | — | — | — | — | ✅ (capture, skipped) | — | — | — | — |

**Legend.** ✅ = the area has a named test vehicle in this plan. ❌ = a stated limitation with no vehicle.

**Rule.** A requirement area with no ✅ in any column is a coverage hole. The two known holes are (a) real-capture per-device accuracy, which has no ground truth at all (`docs/limitations_v2.md` §1), and (b) browser-level UI rendering, for which no harness exists (§10.3). Both are stated limitations, not unclaimed coverage.

**Rule.** `REQ-TEST-003` applies to all of T01–T15 (`01-requirements.md` §4.3, line 1472); A36 is its acceptance vehicle.

---

## 15. Open decisions

This document's own decisions occupy the **`OD-88`–`OD-98`** band allocated to it by the canonical register in `13-open-decisions-and-assumptions.md` §5.2 (the canonical register lives in `00-project-overview.md`, which holds OD-01–OD-08; `10-…` holds OD-81–OD-87; `12-…` holds OD-99–OD-103).

| ID | Decision needed | Assumption proceeded on | Impact if the assumption is wrong |
|---|---|---|---|
| **OD-88** | Whether the seven new strata (`single_child_containment`, `multi_child_contributions`, `unobserved_parent_load`, `plug_relocation`, `reader_relocation`, `factor_timeline`, `factor_misconfigured`) are added to the canonical `STRATA` set or held in a separate testing-only registry | Added to `STRATA` in `tools/simulator/labhouse.py`, to `REQUIRED_STRATA` in `tests/fixtures/labhouse.py` and to `MATRIX_STRATA`, because `assert_matrix_coverage()` requires every stratum to be referenced by a row and every row to be backed by a stratum | If held separately, the coverage contract needs a second registry and `assert_matrix_coverage()` needs a parallel branch |
| **OD-89** | The database size-growth threshold (P4) in bytes per channel per day | No threshold is asserted; the first measurement establishes it and the value is recorded in the validation record | A threshold must be chosen before P4 can be an acceptance row rather than a measurement |
| **OD-90** | Whether the `monitor_published` hot-tub variant (present in `tools/simulator/hot_tub.py:943`, absent from `tests/fixtures/labhouse.py:147-157`) is added to the required set | **Re-checked against the canonical catalogue:** no canonical T-scenario references a factor, so the variant is not needed by T01–T15. Left unchanged. | If a non-T factor test needs it, `REQUIRED_HOT_TUB_VARIANTS` must gain the entry |
| **OD-91** | Whether the nine J-group matrix rows become `MATRIX_STRATA` entries backed by new learning strata, or the J group is re-scoped | J rows are added to `MATRIX_STRATA` with the learning strata, because `MATRIX_STRATA` currently has zero J rows while the matrix document has 9 | If J is re-scoped, the matrix document and the contract must be edited together |
| **OD-92** | Whether the pure-JS UI harness is acceptable without a node dependency, or the DOM-level check is formally deferred | The pure-JS harness is attempted first; if it requires a node dependency the DOM-level check is reported as *not implemented* and §10.4's manual checklist is the sole UI evidence | If a node dependency is later approved, §10.3 changes and a browser-level tier may be added |
| **OD-93** | Whether `--cov` is wired into CI so coverage becomes machine-measured | Not wired; §14's matrix is maintained manually and `G-TEST-01` is recorded | If wired, §14 can be generated rather than hand-maintained |
| **OD-94** | The reconciliation between locally-scoped `G-TEST-nn` IDs and the canonical gap register | Locally scoped; the IDs are **registered** in the canonical gap register at `00-project-overview.md` §13.2, which is the single source of truth for gap identifiers | If the orchestrator requires canonical `G21`–`G32` IDs instead, this file's IDs change |
| **OD-95** | Whether the two non-T gap closures (factor timeline, startup surge) are accepted as coverage for matrix groups B and A6/H4, given that the canonical T-catalogue is fixed and contains no such scenario | Accepted as named non-T tests (§3.3), because the master instruction fixes T01–T15 and a new T identifier would be a contract violation | If the orchestrator requires a T identifier, the catalogue itself must be reopened by the master instruction |
| **OD-96** | Whether the reference topology gains a **generation** device (solar / PV) so T15 "Bidirectional Power" can be exercised against a true export source | The reference topology is **not** changed by this plan. T15 is proved against a *negative reading* (`negative_ct`, `reversed_orientation`), which exercises the signed-handling contract but is not literally "solar creates reverse power flow". `tools/simulator/topology.py` defines loads only; there is no generation device | If a generation device is added to the reference topology, T15's fixture changes to use it; the signed-handling assertions are unchanged |
| **OD-97** | Whether `SimulatorConfig` gains an explicit relocation / replacement capability for T11 and T12, or the simulator grows a second-phase scenario model | Not decided here — this is simulator implementation work outside the test plan's scope (§5.3). The plan names the required capability rather than choosing the mechanism | If a different mechanism is chosen, only the fixture implementation in §5.1 changes; the expected outcomes and acceptance rows A24 do not |
| **OD-98** | Whether `UnknownLoadProfile` gains a recurring-pattern mode for T10, or a new profile class is added | A new profile class is the plan's assumption (§5.3), because the current bounded random walk (`tools/simulator/behavior.py:513-533`) produces no recognisable repeating pattern to discover | If a recurring-pattern mode is added to the existing class instead, only the fixture changes; the expected outcome in §5.1 is unchanged |

### 15.1 Locally-scoped gaps

| ID | Gap | Evidence |
|---|---|---|
| **G-TEST-01** | Coverage reporting unavailable: `pytest-cov` is declared but `--cov` is used nowhere in CI | `pyproject.toml:26`; `.github/workflows/ci.yml` |
| **G-TEST-02** | Hot-tub variant sets diverge: `monitor_published` is in the simulator's 10, not in the fixture's 9 | `tools/simulator/hot_tub.py:943`; `tests/fixtures/labhouse.py:147-157` |
| **G-TEST-03** | `MATRIX_STRATA` has zero J rows while the matrix document defines 9 J rows | `tests/fixtures/labhouse.py:59-144`; `docs/test_scenario_matrix.md:182-199` |
| **G-TEST-04** | No browser/UI test harness exists | repository-wide absence; §10.3 |
| **G-TEST-05** | Nine capture tests skip without a local `WATTMOSAIC_REAL_DB` capture | `tests/simulation/test_real_topology.py`; `docs/limitations_v2.md` §3 |
| **G-TEST-06** | No per-device real-world ground truth exists, so every accuracy figure is simulator-derived | `docs/limitations_v2.md` §1 |
| **G-TEST-07** | `homeassistant` is not installed locally; HA-bound integration tests are static/AST or stub-HA | local environment; §6.2 |
| **G-TEST-08** | `ExpectedPower.startup_w` exists in the contract; the simulator's motor profile now produces one (`tools/simulator/behavior.py:404-444`, `tools/simulator/labhouse.py:679-722`), but no test asserts that one 5× spike does not inflate the steady-state estimate | `docs/test_scenario_matrix.md:39`, `:130` |
| **G-TEST-09** | Group J (learning/regression) has 0 COVERED rows | `docs/test_scenario_matrix.md:182-199` |
| **G-TEST-10** | The fixed T-catalogue contains no factor or startup-surge scenario, so the group B and A6/H4 matrix gaps cannot be closed by a T-scenario | `01-requirements.md` §4.1; §3.3 |
| **G-TEST-11** | `replay_capture.py`'s `evaluation_shape` reports empty per-device strata, so capture-derived topology metrics are unavailable today | `tools/audit/replay_capture.py:643` |
| **G-TEST-12** | `SimulatorConfig` has **no** relocation or replacement capability, so T11 and T12 cannot be expressed as configuration today | `tools/simulator/scenario.py:44-92`; §5.3 |
| **G-TEST-13** | `UnknownLoadProfile` is a bounded random walk, not a recurring pattern, so T10's "discovered profile" has no producible fixture today | `tools/simulator/behavior.py:513-533`; §5.3 |
| **G-TEST-14** | The reference topology contains **no** generation device, so T15's "solar or another source" framing cannot be exercised literally | `tools/simulator/topology.py`; §5.3, OD-96 |
| **G-TEST-15** | `HouseholdTopology.true_ct_powers` aggregates a device's **static** `circuits` map, so a mid-run circuit move is not expressible | `tools/simulator/topology.py:139-157`; §5.3 |

**Rule.** `G-TEST-nn` IDs are locally scoped to this document. They are **registered** in the canonical gap register at `00-project-overview.md` §13.2, which is the register of record and the single source of truth for gap identifiers; a locally-scoped ID is valid only together with its entry there. The table above is the upstream source for the gaps it lists.

---

## 16. Related documents

- [00 — Project Overview](00-project-overview.md) — canonical requirement and gap registers, OD-01–OD-08
- [01 — Requirements](01-requirements.md) — §4.1 the canonical T01–T15 catalogue, §4.2 the scenario-to-matrix mapping, §4.3 the requirement-to-scenario mapping, §5 the requirement-area breakdown
- [02 — Architecture](02-architecture.md)
- [03 — Measurement and Electrical Domain](03-measurement-and-electrical-domain.md)
- [05 — Residual Model Spec](05-residual-model-spec.md)
- [06 — Device and Load Model Spec](06-device-and-load-model-spec.md)
- [07 — Event and Inference Spec](07-event-and-inference-spec.md)
- [08 — Storage and Data Lifecycle](08-storage-and-data-lifecycle.md)
- [09 — API and Data Contracts](09-api-and-data-contracts.md)
- [10 — UI and User Experience Spec](10-ui-and-user-experience-spec.md)
- [12 — Deployment and Rollback](12-deployment-and-rollback.md)
- [13 — Open Decisions and Assumptions](13-open-decisions-and-assumptions.md)
- [Test Scenario Matrix](../../test_scenario_matrix.md) — the 84-row coverage matrix
- [Implementation Plan v2](../../implementation_plan_v2.md) — the no-completion-without-orchestrator-test-log rule (`:333`)
- [Agent Assignments](../../agent_assignments.md) — the VERIFIED-after-orchestrator-run rule (`:26-28`)
- [Limitations v2](../../limitations_v2.md) — the simulator-only accuracy limitation (§1), the nine skipped capture gates (§3), the `latest_inference_results()` latency figures (`:221`)
- [Project Status](../../../PROJECT_STATUS.md) — the published simulator figures (`:1457-1463`) and the two-second-assumption finding (`:28`)
