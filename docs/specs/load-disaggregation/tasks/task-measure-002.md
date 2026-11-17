# TASK-MEASURE-002 — Leg pairs, reverse flow and the reader declaration surface

**SPEC_STATUS: APPROVED_FOR_IMPLEMENTATION**

**Task ID.** `TASK-MEASURE-002`
**Title.** Leg pairs, reverse flow and the reader declaration surface
**Status.** `NOT_STARTED`
**Requirement IDs.** `REQ-MEASURE-005`, `REQ-MEASURE-006`, `REQ-MEASURE-007`

**Description.**
Make the leg pair an atomic unit, keep reverse and bidirectional flow intact,
and publish the reader's own declarations — resolution, reliability and
expected interval — alongside its readings.

**Expected output.**
- A skewed leg pair excludes **both** rows with a named reason.
- A negative or reverse reading keeps its sign through reconciliation.
- `resolution_w`, `reliability` and `expected_interval_s` published per reader.

**Files / modules affected.**

| Path | Role |
| --- | --- |
| `engine/wattmosaic_engine/disaggregation/engine.py` | the leg-pair exclusion |
| `engine/wattmosaic_engine/electrical/matrix.py` | `MeasurementMatrix` and the reverse-flow handling |
| `engine/wattmosaic_engine/ingestion/correlation.py` | the reader declaration surface |

**Input contracts.**

| Contract | Source | Note |
| --- | --- | --- |
| `Measurement` | `TASK-MEASURE-001` | signed, phase-declared |
| The reader declaration | the configuration | resolution, reliability, expected interval |

**Output contracts.**

| Contract | Consumer | Note |
| --- | --- | --- |
| Excluded-row reason | the API, the panel | a machine reason code, never a silent drop |
| Published reader declaration | `api/server.py` | `resolution_w`, `reliability`, `expected_interval_s` |

**Dependencies.**

| Task | Reason |
| --- | --- |
| `TASK-MEASURE-001` | the measurement contract must be settled |

**Acceptance criteria.**
- [ ] A leg pair whose legs were sampled far apart excludes both rows with a named reason.
- [ ] A negative reading keeps its sign; it is never clamped.
- [ ] `resolution_w`, `reliability` and `expected_interval_s` are published for each reader.
- [ ] Excluding one leg excludes the other; a half-pair is never used.

**Test requirements.**

| Test ID | Vehicle | Asserts |
| --- | --- | --- |
| `T13` · `UT-disaggregation::test_a_pair_whose_legs_were_sampled_far_apart_is_excluded` | scenario `T13` | both legs excluded, with a reason |
| `T15` · `UT-disaggregation::test_negative_export_is_preserved_in_reconciliation` | scenario `T15` | the negative export survives |
| `UT-correlation::test_reader_declaration_is_published` (new) | `pytest tests/unit` | the three declaration fields are published |

**Builder assignment.**
Unassigned. Wave 1, segment 1.2. Write owner of
`disaggregation/engine.py` and `electrical/matrix.py` for wave 1.

**Blockers.**
- None.
