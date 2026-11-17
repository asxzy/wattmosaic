# TASK-MEASURE-001 — Measurable quantities, units, phase and polarity

**SPEC_STATUS: APPROVED_FOR_IMPLEMENTATION**

**Task ID.** `TASK-MEASURE-001`
**Title.** Measurable quantities, units, phase and polarity
**Status.** `NOT_STARTED`
**Requirement IDs.** `REQ-MEASURE-001`, `REQ-MEASURE-002`, `REQ-MEASURE-003`, `REQ-MEASURE-004`

**Description.**
Close the measurement contract so that only genuinely measurable quantities
exist, phase is declared rather than inferred, and polarity is applied once
and never clamped.

**Expected output.**
- A unit whitelist of `W`, `kW`, `kWh`, `Wh` and nothing else.
- No `V`, `A`, `PF`, `VAR`, `VA` or `Hz` field anywhere in the measurement contract.
- Phase as a declared reader property with the `L1`/`L2`/`BOTH` coefficients.
- Polarity applied exactly once, with the sign preserved.

**Files / modules affected.**

| Path | Role |
| --- | --- |
| `engine/wattmosaic_engine/contracts.py` | `Measurement`, `MeasurementBasis`, `Phase`, `Polarity` |
| `engine/wattmosaic_engine/ingestion/normalize.py` | the normalisation and polarity application |
| `engine/wattmosaic_engine/electrical/factors.py` | the phase coefficients |

**Input contracts.**

| Contract | Source | Note |
| --- | --- | --- |
| The bumped contract | `TASK-FOUNDATION-002` | additive and optional |
| The HA state payload | `ingestion/ha_source.py` | the raw source of the reading |

**Output contracts.**

| Contract | Consumer | Note |
| --- | --- | --- |
| `Measurement` | `synchronization/`, `electrical/matrix.py` | unit-whitelisted, phase-declared, signed |
| Phase coefficients | `electrical/factors.py` | declared, never inferred |

**Dependencies.**

| Task | Reason |
| --- | --- |
| `TASK-FOUNDATION-002` | the contract bump must land first |

**Acceptance criteria.**
- [ ] Only `W`, `kW`, `kWh`, `Wh` are accepted as units.
- [ ] No unmeasurable electrical quantity field exists in the contract.
- [ ] Phase is declared per reader and never inferred from the data.
- [ ] Polarity is applied once and the sign is preserved, including a negative reading.
- [ ] No negative value is clamped to zero anywhere in this path.

**Test requirements.**

| Test ID | Vehicle | Asserts |
| --- | --- | --- |
| `UT-contracts::test_measurement_unit_whitelist` (new) | `pytest tests/unit` | only the four units are accepted |
| `UT-contracts::test_no_unmeasurable_quantity_field` (new) | `pytest tests/unit` | no voltage/current field exists |
| `UT-electrical::test_phase_coefficients_l1_l2_both` | `pytest tests/unit` | the three declared phase cases |
| `T15` · `UT-disaggregation::test_11_reversed_ct_polarity` | scenario `T15` | a reversed CT keeps its sign |

**Builder assignment.**
Unassigned. Wave 1, segment 1.1. Write owner of `contracts.py` for wave 1.

**Blockers.**
- None.
