# Acceptance Report — measured results (contract 3.0, v3 runtime)

> Every number in this report was **measured** by running production v3 code
> against the deterministic simulator. Nothing here is a target or an aggregate
> "accuracy score". The command that produced each table is given beside it,
> and each value is pinned by a regression test so it cannot drift silently.
>
> **Ground truth is synthetic.** This report is evidence that the *pipeline and
> its invariants* hold on controlled households, **not** evidence that
> per-device accuracy has been validated in your home. That remains
> **unverified** — see §6.

This report replaces the 0.1.x report, which graded the removed
`DisaggregationEngine` through `tests/simulation/test_accuracy.py` (both deleted
by the contract-3.0 cutover). Numbers are not comparable with that report:
UNKNOWN is now *no claim* (`None`), never 0 W, and a value the evidence cannot
separate is no longer counted as attributed (implementation-plan §8).

---

## 1. Scope and configuration

| Item | Value |
| --- | --- |
| Harness | `tests/simulation/accuracy_v3.py` (V1) |
| Reproduce | `.venv/bin/python -m tests.simulation.accuracy_v3` (add `--json` for the raw report) |
| Gate | `.venv/bin/python -m pytest tests/simulation/test_accuracy_v3.py -q` |
| Seed | `17` (every stratum) |
| HB §17 houses | `tools/simulator/hb17.py`, 15 min quiet prelude + 12 min script, 10 s records (162 records) |
| Lab strata | `tools/simulator` lab houses, 6 simulated minutes, 15 s records (`ambiguous_pair` runs 40 min) |
| Engine path, HB §17 | the real `EngineCore` three-loop runtime via `EngineHarness` (HA-shaped messages → fast loop; event loop and steady loop run after every record on an injected clock); the published `InferenceSnapshot` is graded **at every record** |
| Engine path, lab | pure I7 `infer_steady` over the real T2 observation operator |
| Declared configuration | HB §17: one subject per HA light on Circuit A, the TV on its plug, the HA entity links. Lab: each HA-visible device's declared branch circuit(s). This is the wiring a user declares; simulator watts, conductor truth and unmetered power are read only by the grader |
| Observation seam | every engine input passes `tools.simulator.frames.observation_view` and the leak detector; the replay driver re-checks it |

The HB §17 prelude exists because the P3 detector trusts its noise estimate only
after 64 samples (`sigma_warmup_samples`); the original 12-minute house put the
lights event at sample 30 and the fridge at sample 60, so the runtime detected
nothing (V1 root-cause analysis). A real installation has been observing its
readers long before any event: that is HB §17 step 1.

---

## 2. Stratified results

`python -m tests.simulation.accuracy_v3`, seed 17:

| Stratum | Records | Published estimates | Attributed % | Group-identified kWh | Unknown-load kWh | Residual preserved | Forced attributions | MEASURED n | MEASURED MAE / RMSE (W) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| `hb17_co_switched` | 162 | 82 | 0.00 | 0.000000 | 0.008330 | 1.0000 | 0 | 0 | — |
| `hb17_solo_switches` | 162 | 82 | 0.00 | 0.000000 | 0.008330 | 1.0000 | 0 | 0 | — |
| `lab_dedicated` | 24 | 72 | 96.09 | 0.000000 | 0.000000 | 0.9583 | 0 | 71 | 1.556 / 2.286 |
| `lab_shared` | 24 | 72 | 22.65 | 0.000000 | 0.000000 | 1.0000 | 0 | 23 | 0.444 / 0.600 |
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
  proven live: a fabricated 22.9 W split of the lights, or a number for one
  shared-CT device, is counted (`test_forced_attribution_detector_counts_a_fabricated_split`).
- **Unknown-load kWh** — published unknown-load estimate, counted once.
- **Residual preserved** — fraction of records where the observed main reading
  minus everything claimed stays above the true unmetered load minus
  `max(10 %, 5 W)`: claims never eat the residual.
- **MEASURED** — a single-subject endpoint, graded against the CT conductor truth
  it observes at the sample instant.

Reading the table:

- **HB §17 houses publish real estimates, and no device claim.** From the lights
  event on, both lights are published as UNKNOWN (`point_w=None`, identifiability
  `insufficient_evidence`, bounds `[0, —]`): one co-switched event can neither
  separate nor group them, so nothing is invented. The fridge's +150 W with no HA
  entity is published once as unknown load on Circuit B (0.00833 kWh), not three
  times over Circuit B, Subpanel and Main. The TV never changes state, so no event
  solves it; its plug reading (3 W) is in the snapshot and it is never shown as 0 W.
  `attributed % = 0` here is the honest outcome, not vacuity.
- `solo_switches` grades identically to `co_switched`: in the simulator the solo
  cycles of light 1 fall while both lights are already on (or after the horizon),
  so they add HA messages but no physics. This is a simulator limitation.
- **Group-identified energy is 0 everywhere.** `lab_shared` is where HB §21.7
  requires a group estimate (two utility loads that only ever appear together on
  `ct_main_utility_plug_ct1`); the engine leaves them UNKNOWN and attributes only
  `utility_3`, alone on its CT (22.65 %). See §4 — this is a pinned engine defect,
  not an accepted outcome.
- `lab_automation_burst` RMSE (88 W) and max (1.49 kW) come from a handful of
  transition-instant samples whose held value matches a level the CT truth also
  took within one sample interval; the MAE is the regression gate.
- Residual preserved 0.9583 = one of 24 records, where a fresh branch reading
  meets a stale main reading.

Thresholds (`tests/simulation/test_accuracy_v3.py`): forced attributions = 0;
MAE ceilings dedicated 2.0 W (RMSE 3.0, max 10.0), shared 1.0, ambiguous pair
0.2, burst 7.5; attributed floors dedicated 85 %, burst 65 %, shared 20–25 %;
residual preserved ≥ 0.9; HB §17 published estimates ≥ 80 per house and
unknown-load energy in [0.0075, 0.0125] kWh. Every bound is the measured value
plus documented headroom.

---

## 3. HB §17 walkthrough end to end (V1)

`tests/end_to_end/test_hb17_walkthrough.py` replays the X07 house through
`EngineCore`'s three loops and, after each step, asserts the snapshot, the
`/matrix` payload, the HA entity values (via the integration's pure
`util.coerce_v3_snapshot` and `entity_values`) and the HB §18 provenance answers.
Result: **11 passed, 2 xfailed (strict)**.

| HB §17 step | Status | What is asserted |
| --- | --- | --- |
| 1 Measurement state | pass | every reader's held value is its last delivered sample (no fabrication); nothing published, `/snapshot` is 404, HA total power is `None` |
| 2 Topology | pass | the active topology version holds the 4 declared CONTAINS edges; mapping declares the lights on Circuit A and the TV on its plug; matrix columns are multi-level with no duplicate reader column |
| 3 Load event | pass | one event group holds both light HA changes and the three nested +45 W reader steps (Circuit A, Subpanel, Main), each with its own timestamp |
| 4 Dynamic matching | pass | both lights are HA-sourced candidates, the three unknown-scope alternatives are present, and the +45 W is not counted three times |
| 5 Joint inference | pass | no individual value is invented: both lights UNKNOWN, `point_w=None`, bounds `[0, —]`; HA shows no state plus the status axes |
| 5 Group estimate | **xfail** | engine defect, §4 (1) |
| 6 Matrix update | pass | exactly the six `light × {Main, Subpanel, Circuit A}` cells changed (DIRECT on Circuit A, EXPECTED on the parents), none shows a watt value; provenance names the three readers, versions, status, bounds, `updated_at`, and the group's HA events |
| 7 Unknown fridge | pass | the no-HA step is one group on Circuit B/Subpanel/Main, no device candidate, one 150 W unknown on Circuit B (≈0 on the parents); HA unknown power 150 W; no matrix cell changed |
| 8 TV standby | pass | the plug's 3 W is in the snapshot; the TV is never published as 0 W |
| 9 Plug moved | pass | a CONFIRMED, INFERRED `smart_plug_relocation` carrying the detector's `association:ct_a->ct_c` evidence |
| 9 New mapping candidate | **xfail** | engine defect, §4 (2) |
| 10 Reconciliation | pass | new topology and mapping versions, model set unchanged, prior versions readable, unrelated mapping edges kept, pre-move evidence preserved, L7 ran for the new mapping version, `/matrix` reads it |

---

## 4. Known engine defects (strict xfails)

1. **No group estimate is reachable through the live runtime.** `GroupingEngine._persist`
   writes a group revision once, so the CONFIRMED transition is never persisted
   and `tick()` sets no `closed_at`; `EvidenceRepository.put_group_revision` records
   reader evidence ids, not subjects, in `event_group_members`. I4 therefore never
   sees the ≥ 2 historical events (`MIN_HISTORY_EVENTS`) it requires before it
   publishes a group sum. Pinned by
   `test_hb17_walkthrough.py::test_step5_confirmed_lights_event_becomes_identifiability_history`
   and `test_accuracy_v3.py::test_shared_branch_publishes_group_identified_energy`.
2. **A confirmed plug relocation creates no new mapping candidate.** `_self_heal_cycle`
   calls `observe_confirming_event` without `candidate_mapping_edges`, so the
   promoted mapping equals the old one (it no longer wipes every edge — fixed in
   V1). Pinned by `test_hb17_walkthrough.py::test_step9_relocation_creates_a_new_mapping_candidate`.

---

## 5. HB §21 acceptance tests

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

The acceptance tests drive the pure stage seams. §3 and §4 are where the same
requirements were exercised through the live runtime: AT-07's group estimate and
AT-15's new mapping candidate are not yet reachable there.

---

## 6. What this report does **not** establish

- Per-device accuracy against a real household: **unverified**, there is no
  ground truth outside the simulator.
- Group-identified energy: no stratum publishes a group sum yet (§4 (1)).
- Absolute-mode (steady) device estimates in the live runtime: the steady pass
  builds its operator with no subjects, so a device that never changes state
  (the HB §17 TV on its plug) is not published from its plug reading.
- Provenance resolution of HA events: an estimate names its HA events, but the
  snapshot-scoped resolver lists them as unresolved refs.
- Learned power models: no stratum trains a model; the learning pipeline is
  covered by its own tests, not by this report.

Full gate output: `docs/verification_report.md` and `data/verification.json`
(`.venv/bin/python tools/dev/verify.py`).
