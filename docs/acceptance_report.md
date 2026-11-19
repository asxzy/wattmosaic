# Acceptance Report — measured results (contract 3.0, v3 runtime)

> Every number in this report was **measured** by running production v3 code
> against the deterministic simulator. Nothing here is a target or an aggregate
> "accuracy score". The command that produced each table is given beside it,
> and each value is pinned by a regression test so it cannot drift silently.
>
> **Ground truth is synthetic.** This report is evidence that the *pipeline and
> its invariants* hold on controlled households, **not** evidence that
> per-device accuracy has been validated in your home. That remains
> **unverified** — see §5.

This report replaces the 0.1.x report, which graded the removed
`DisaggregationEngine` through `tests/simulation/test_accuracy.py` (both deleted
by the contract-3.0 cutover). Numbers are not comparable with that report:
UNKNOWN is now *no claim* (`None`), never 0 W, and a value the evidence cannot
separate is no longer counted as attributed (implementation-plan §8).

---

## 1. Scope and configuration

| Item | Value |
| --- | --- |
| Harness | `tests/simulation/accuracy_v3.py` (V1, V1b) |
| Reproduce | `.venv/bin/python -m tests.simulation.accuracy_v3` (add `--json` for the raw report) |
| Gate | `.venv/bin/python -m pytest tests/simulation/test_accuracy_v3.py -q` |
| Seed | `17` (every stratum) |
| HB §17 houses | `tools/simulator/hb17.py`, 15 min quiet prelude + 12 min script, 10 s records (162 records) |
| `lab_shared` | lab house, 60 simulated minutes, 15 s records (240), replayed as HA-shaped records |
| Other lab strata | lab houses, 6 simulated minutes, 15 s records (`ambiguous_pair` runs 40 min) |
| Engine path, HB §17 and `lab_shared` | the real `EngineCore` three-loop runtime via `EngineHarness` (HA-shaped messages → fast loop; event loop and steady loop run after every record on an injected clock); the published `InferenceSnapshot` is graded **at every record** |
| Engine path, other lab strata | pure I7 `infer_steady` over the real T2 observation operator |
| Declared configuration | HB §17: one subject per HA light on Circuit A, the TV on its plug, the plug's single-subject scope (DEC-05), the HA entity links. Lab: each HA-visible device's declared branch circuit(s); a branch CT declared for exactly one device is that device's single-subject endpoint. This is what a user declares; simulator watts, conductor truth and unmetered power are read only by the grader |
| Observation seam | every engine input passes `tools.simulator.frames.observation_view` and the leak detector; the replay driver re-checks it |

The HB §17 prelude exists because the P3 detector trusts its noise estimate only
after 64 samples (`sigma_warmup_samples`). A real installation has observed its
readers long before any event: that is HB §17 step 1. `lab_shared` runs for an
hour because its requirement, a group estimate for two loads on one CT
(HB §21.7), needs at least two CONFIRMED co-switches in history
(`MIN_HISTORY_EVENTS`) before I4 publishes the group sum.

---

## 2. Stratified results

`python -m tests.simulation.accuracy_v3`, seed 17:

| Stratum | Records | Published estimates | Attributed % | Group-identified kWh | Unknown-load kWh | Residual preserved | Forced attributions | MEASURED n | MEASURED MAE / RMSE (W) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `hb17_co_switched` | 162 | 238 | 11.76 | 0.000000 | 0.008330 | 1.0000 | 0 | 156 | 0.228 / 0.228 |
| `hb17_solo_switches` | 162 | 238 | 11.76 | 0.000000 | 0.008330 | 1.0000 | 0 | 156 | 0.228 / 0.228 |
| `lab_dedicated` | 24 | 72 | 96.09 | 0.000000 | 0.000000 | 0.9583 | 0 | 71 | 1.556 / 2.286 |
| `lab_shared` (live runtime) | 240 | 550 | 49.23 | 0.104445 | 0.071016 | 0.9708 | 0 | 236 | 0.313 / 0.500 |
| `lab_ambiguous_pair` | 160 | 480 | 0.01 | 0.000000 | 0.000000 | 1.0000 | 0 | 160 | 0.058 / 0.075 |
| `lab_automation_burst` | 24 | 888 | 73.34 | 0.000000 | 0.000000 | 0.9583 | 0 | 574 | 5.923 / 88.202 |

**TOTAL `forced_attribution_count` = 0.**

Definitions (all v3 semantics, HB §8.4/§8.5, HB §18):

- **Published estimates** — `(record, subject)` estimates present in the published
  view, any status. A `None` point is published as UNKNOWN and is *no claim*.
- **Attributed %** — energy of published MEASURED/ESTIMATED/ALLOCATED points plus
  published group sums, over true device energy.
- **Forced attribution** — a published point for a subject the simulator's own
  evidence cannot separate (the HB §17 co-switched lights; lab devices sharing
  exactly one branch CT) or for a member of a published group. The detector is
  proven live: a fabricated split of the lights, or a number for one shared-CT
  device, is counted (`test_forced_attribution_detector_counts_a_fabricated_split`).
- **Group-identified kWh** — energy of published group sums, counted once.
- **Unknown-load kWh** — published unknown-load estimate, counted once.
- **Residual preserved** — fraction of records where the observed main reading
  minus everything claimed stays above the true unmetered load minus
  `max(10 %, 5 W)`.
- **MEASURED** — a single-subject endpoint, graded against the CT conductor truth
  it observes at the sample instant.

Reading the table:

- **HB §17 houses.** The TV is MEASURED from its plug from the first heartbeat on
  (3 W standby, 156 points). Both lights are published as UNKNOWN
  (`point_w=None`, bounds `[0, —]`) from the lights event on: one co-switched
  event is not enough history to separate or group them, and nothing is
  invented. The fridge's +150 W, with no HA entity, is published once as unknown
  load on Circuit B (0.00833 kWh), not three times over Circuit B, Subpanel and
  Main. `solo_switches` grades identically because the simulator's solo cycles of
  light 1 fall while both lights are already on (or after the horizon); this is a
  simulator limitation.
- **`lab_shared` (HB §21.7).** After two CONFIRMED co-switches of utility_1 and
  utility_2 are in history, I4 publishes their GROUP_IDENTIFIABLE sum and the
  steady pass keeps it current from the shared CT: about 2.4 kW while both run
  and the CT's noise floor when off (51 group points, 0.104445 kWh). The members
  are never given a number. utility_3, alone on its CT, is MEASURED
  (MAE 0.313 W). Co-switches before the group is history-backed stay UNKNOWN,
  which is why attributed % is 49.23 rather than higher.
- `lab_automation_burst` RMSE (88 W) and max (1.49 kW) come from a handful of
  transition-instant samples; the MAE is the regression gate.

Thresholds (`tests/simulation/test_accuracy_v3.py`, measured value plus
documented headroom): forced attributions = 0; MAE ceilings dedicated 2.0 W
(RMSE 3.0, max 10.0), shared 0.6 W (RMSE 1.0, max 5.0), ambiguous pair 0.2, burst
7.5, HB §17 0.5; attributed floors dedicated 85 %, burst 65 %, shared 44–55 %,
HB §17 10–13 %; residual preserved ≥ 0.9; HB §17 published estimates ≥ 230 and
MEASURED ≥ 150 per house, unknown-load energy in [0.0075, 0.0125] kWh;
`lab_shared` group points ≥ 45 and group energy ≥ 0.09 kWh, the group tracking
2.4 kW on and ~0 W off.

---

## 3. HB §17 walkthrough end to end

`tests/end_to_end/test_hb17_walkthrough.py` replays the X07 house through
`EngineCore`'s three loops and, after each step, asserts the snapshot, the
`/matrix` payload, the HA entity values (via the integration's pure
`util.coerce_v3_snapshot` and `entity_values`) and the HB §18 provenance answers.
Result: **13 passed, 0 xfail**.

| HB §17 step | What is asserted |
| --- | --- |
| 1 Measurement state | every reader's held value is its last delivered sample; the only thing published is the TV MEASURED from its declared single-subject plug; no light, no house total (HA shows `None`, never 0 W) |
| 2 Topology | the active topology version holds the 4 declared CONTAINS edges; mapping declares the lights on Circuit A and the TV on its plug; matrix columns are multi-level with no duplicate reader column |
| 3 Load event | one event group holds both light HA changes and the three nested +45 W reader steps (Circuit A, Subpanel, Main), each with its own timestamp |
| 4 Dynamic matching | both lights are HA-sourced candidates, the three unknown-scope alternatives are present, and the +45 W is not counted three times |
| 5 Joint inference | no individual value is invented: both lights UNKNOWN, `point_w=None`, bounds `[0, —]`; HA shows no state plus the status axes |
| 5 History | the lights event is persisted CONFIRMED with `closed_at`, its subjects are recorded, and I4's history returns it |
| 6 Matrix update | exactly the six `light × {Main, Subpanel, Circuit A}` cells changed (DIRECT on Circuit A, EXPECTED on the parents), none shows a watt value; provenance names the three readers, versions, status, bounds, `updated_at`, and resolves the group's HA events (no unresolved ref) |
| 7 Unknown fridge | the no-HA step is one group on Circuit B/Subpanel/Main, no device candidate, one 150 W unknown on Circuit B (≈0 on the parents); HA unknown power 150 W; no matrix cell changed |
| 8 TV standby | the TV is MEASURED at 3 W from its plug (point bounds, standby level, provenance names `ct_plug`); HA shows 3 W |
| 9 Plug moved | a CONFIRMED, INFERRED `smart_plug_relocation` with the detector's `association:ct_a->ct_c` evidence; the promoted mapping moves the TV off Circuit A to a new PROVISIONAL, inference-declared Circuit C candidate |
| 10 Reconciliation | new topology and mapping versions, model set unchanged, prior versions readable, unrelated mapping edges kept, fridge never declared, pre-move evidence preserved, L7 ran for the new mapping version, `/matrix` reads it |

---

## 4. HB §21 acceptance tests

`.venv/bin/python -m pytest tests/acceptance -q` → **46 passed, 0 xfail**. No
acceptance file carries an xfail marker; each names its closing task
(`tests/acceptance/test_closing_task_ids.py` enforces it).

| AT | Requirement (HB §21) | Closed by | Status |
| --- | --- | --- | --- |
| 01 | single load through nested readers, no double count | M1 | pass |
| 02 | parent residual does not invalidate child containment | T5 | pass |
| 03 | stable non-zero standby modelled | L2 | pass |
| 04 | unknown devices detected without HA | T6 | pass |
| 05 | response delays matched | I2 | pass |
| 06 | simultaneous devices jointly inferred | I3 | pass |
| 07 | non-identifiable devices → group estimate, no fabrication | I4 | pass |
| 08 | smart plug is a stronger anchor | I3 | pass |
| 09 | covariance of nested readers | I3 | pass |
| 10 | unknown-load alternatives kept in matching | I1 | pass |
| 11 | mapping cells change with evidence | M2 | pass |
| 12 | matrix columns multi-level | U1 | pass |
| 13 | no duplicate secondary columns | M1 | pass |
| 14 | one physical load counted once | I5 | pass |
| 15 | plug relocation changes mapping, not power model | T7 | pass |
| 16 | reader relocation/calibration → reconciliation | T7 | pass |
| 17 | missing data never becomes zero | P6 | pass |
| 18 | late events revise provisional results | I6 | pass |
| 19 | historical evidence replays under a newer model | L7 | pass |
| 20 | UI explains source and uncertainty | U2 | pass |

The acceptance tests drive the pure stage seams; §2 and §3 exercise the same
requirements through the live runtime, including AT-07's group estimate
(`lab_shared`) and AT-15's new mapping candidate (walkthrough step 9).

---

## 5. What this report does **not** establish

- Per-device accuracy against a real household: **unverified**, there is no
  ground truth outside the simulator.
- A group estimate for the HB §17 lights: the 12-minute script switches them
  together once, which is not enough history; the live mechanism is shown on
  `lab_shared`.
- Learned power models: no stratum trains a model; the learning pipeline is
  covered by its own tests, not by this report.

Full gate output: `docs/verification_report.md` and `data/verification.json`
(`.venv/bin/python tools/dev/verify.py`).
