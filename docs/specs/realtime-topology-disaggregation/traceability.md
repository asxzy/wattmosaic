# Traceability — handbook → tasks → tests

## 1. HB §21 mandatory acceptance tests

Each acceptance test is one file, `tests/acceptance/test_atNN_<slug>.py`. K07 creates all 20 as `xfail(strict=True)` against the HB §17 reference house (X07). The **closing task** removes the marker. A test may only be un-xfailed when it passes unchanged, and V1 verifies that no marker remains.

| AT | Requirement (HB §21) | Closing task | Contributing tasks | Asserts (semantics, not HTTP 200) |
|---|---|---|---|---|
| 01 | One load through nested readers without double counting | M1 | T2, I3, I5 | The same 45 W appears in the Circuit A, Subpanel and Main cells. Physical total adds 45 W once. Main reconciliation counts it once. |
| 02 | Parent residual does not invalidate child containment | T5 | T3 | 20 parent-only steps leave the CONTAINS edge lifecycle and support counters unchanged, and the conflict count stays 0. |
| 03 | Stable non-zero standby is modeled | L2 | L4, I5 | TV plug 3 W standby → MEASURED 3 W. A shared-circuit device learns 7 W standby from ΔP and reports level STANDBY, not 0. |
| 04 | Unknown devices detected without HA | T6 | P3, P5, I1 | A fridge compressor with no HA entity produces an event group, an unknown-scope candidate, and an Unknown Load Profile that reaches CHARACTERISED after 5 cycles. |
| 05 | Matching handles different response delays | I2 | L3, I7 | Light (τ≈0), blind (transient → standby) and HVAC (compressor at +90 s) are each matched. The HVAC step is not dropped. |
| 06 | Multiple simultaneous devices jointly inferred | I3 | I1, I7 | HB §7.7 example: A +45, B +60, Main +110 → lights and TV separated, 5 W in ΔU on Main, no greedy assignment. |
| 07 | Non-identifiable → group estimate, no fabricated individual values | I4 | I5, H1 | The mandatory 250 W test (`tasks/I4.md`), including the steady-state and model-prior variants. |
| 08 | Smart plug measurements act as stronger anchors | I3 | D2, I5 | With a noisy circuit, TV via plug is MEASURED. The joint solve keeps TV within plug σ and the error moves to the other candidates or ΔU. |
| 09 | Measurement covariance accounted for when combining nested readers | I3 | T2 | The posterior variance of a load seen by 3 nested readers is ≥ 0.9× its single-reader variance (no independence gain). The load is counted once. |
| 10 | Unknown-load alternatives remain available during matching | I1 | I6 | 100 % of candidate sets contain an unknown_scope candidate (property test). A weak known candidate loses to unknown. |
| 11 | Mapping cells change as evidence accumulates | M2 | I6 | Cell independent-event counts and confidence increase only with distinct event groups. 100 samples from one event change the count by 1. |
| 12 | Matrix columns reflect multi-level reader organization | U1 | M1 | `MatrixPayload.columns` depth = 5 levels for the reference house. The DOM header has one row per level, and Σ colSpan per row = leaf count = reader count. |
| 13 | Reader columns not duplicated for secondary relationships | M1 | U1 | SHARED_LOAD, PAIRED_LEG and SAME_CONDUCTOR edges produce annotations. Leaf count = endpoint count. |
| 14 | One physical load visible to multiple readers not counted multiple times | I5 | M1, D4 | Snapshot reconciliation and the matrix physical total each count the TV once, even with plug + circuit + subpanel + main observing it. |
| 15 | Smart plug relocation changes mappings without relearning power | T7 | D1, M2, L1 | After the move: mapping version +1, old mapping edge RETIRED with valid_until, new edge PROVISIONAL→CONFIRMED. The TV power-model version id is **unchanged**. |
| 16 | Reader relocation or calibration change triggers reconciliation | T7 | T2, L7 | A CT polarity flip → SUSPECTED ConfigurationEvent → affected cells SUSPECTED_CHANGE → new topology version after N events → prior version readable. A calibration change is not retroactive. |
| 17 | Missing data does not become zero | D3 | D4, H1, P2 | A reader silent past `missing_after_s` → reader column None/stale with reason. Dependent estimates are UNKNOWN with null power through DB, API JSON and the HA state. |
| 18 | Late-arriving events revise provisional results | I6 | P5 | Revision 1 is PROVISIONAL from Main only. A Circuit A sample 20 s late → revision 2 REVISED with a changed attribution. Revision 1 and the evidence stay byte-identical. |
| 19 | Historical evidence replayable with a newer model version | L7 | I7, L1 | Replay one week with model set v1 and v2. Derived outputs differ as expected. Raw samples and evidence are byte-identical. Both result sets are retrievable by `derived_run_id`. |
| 20 | UI explains source and uncertainty of every displayed estimate | U2 | U1, H1 | For every rendered number in the panel model: status text, identifiability and a provenance drill exist. HA attributes carry status, bounds and versions. |

## 2. Handbook sections → tasks

| HB | Topic | Tasks |
|---|---|---|
| §0.1 | Ten cooperating engines | architecture.md §2 (P*, T*, I*, M*, L*, D1) |
| §1 | Four object types, many-to-many | K01, S13, D2, H2 |
| §2.1 | ReaderMeasurement | K02, P1, S16 |
| §2.2 | Compatibility layer | P1 (predicate), T2 (operator) |
| §2.3 | Signed deltas | P1, P3, I3 |
| §3.1 | Observation operator, non-binary H | T2 |
| §3.2 | Absolute and delta models | I3 (both modes), I7 (DEC-22) |
| §3.3 | Residual-aware parent equation | T2, T3 |
| §4.1–4.4 | Typed graph, asymmetric evidence, versioned prior | K03, S14, T1, T4, T5, D1 |
| §5 | Three loops | P6, L6 |
| §6.1–6.6 | Pipeline stages 1–6 | P1–P5 |
| §7.1–7.7 | Dynamic matching | I1, I2, I6 |
| §8.1–8.3 | Joint solve, objective, covariance | I3, T3 |
| §8.4 | Identifiability, group output | I4, I5 |
| §8.5 | Estimate status | K00, I5 |
| §9 | Matrix structure | K05, M1, U1 |
| §10 | Dynamic matrix updating | M2 |
| §11 | Power models | K06, L1, L2, L3 |
| §12 | Snapshot, event records, lifecycle | K04, D4, I6, P5 |
| §13 | Learning, reconciliation, active learning | L4, L5, L6, L7, L8 |
| §14 | Topology change, self-healing | T7, D1 |
| §15 | Storage and performance | X05, S13–S19, D5, P6, V2 |
| §16 | Breaking changes, migration | DEC-01/02, X05, S*, Z1, Z2 |
| §17 | End-to-end example | X07 (fixture), K07, V1 |
| §18 | Typed contracts, provenance, null | K00–K06, D3, D4 |
| §19 | Matrix UI | U1, U2, M3 |
| §20 | Stage order | Waves 0–6 (implementation-plan.md §2) |
| §22 | Final rules | each task's *Rules* section and the Z1 static gate |
