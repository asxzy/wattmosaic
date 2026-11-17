# TASK-CONFIDENCE-002 — Live confidence dimensions and per-edge evidence

**SPEC_STATUS: APPROVED_FOR_IMPLEMENTATION**

**Task ID.** `TASK-CONFIDENCE-002`
**Title.** Live confidence dimensions and per-edge evidence
**Status.** `NOT_STARTED`
**Requirement IDs.** `REQ-CONFIDENCE-005`, `REQ-CONFIDENCE-006`, `REQ-CONFIDENCE-007`

**Description.**
Populate the two dead confidence dimensions where the evidence exists, publish
the measurement quality, the evidence count, the freshness and the residual
uncertainty separately, and give each edge its own confidence.

**Expected output.**
- The `topology` and `residual` dimensions carry a value where derivable and `null` otherwise.
- Measurement quality, evidence count, freshness and residual uncertainty are separately observable.
- An edge confidence is capped at 0.99 while a candidate and never becomes a device's `power` dimension.

**Files / modules affected.**

| Path | Role |
| --- | --- |
| `engine/wattmosaic_engine/disaggregation/audit.py` | the analysis-pass derivations |
| `engine/wattmosaic_engine/disaggregation/association.py` | the per-edge evidence |
| `engine/wattmosaic_engine/disaggregation/confidence.py` | the dimension derivations |

**Input contracts.**

| Contract | Source | Note |
| --- | --- | --- |
| The confidence member names | `TASK-CONFIDENCE-001` | the five names, unchanged |
| The hierarchy evidence | `TASK-TOPOLOGY-002` | the topology dimension |
| The signed residual | `TASK-RESIDUAL-001` | the residual dimension |

**Output contracts.**

| Contract | Consumer | Note |
| --- | --- | --- |
| The populated vector | `storage/database.py`, `GET /devices/{id}/records` | `null` where not derivable |
| The edge confidence | `GET /relations` | capped at 0.99 while a candidate |

**Dependencies.**

| Task | Reason |
| --- | --- |
| `TASK-CONFIDENCE-001` | the member names and the calibration flag |

**Acceptance criteria.**
- [ ] The `topology` and `residual` dimensions carry a value where derivable and `null` otherwise.
- [ ] Measurement quality, evidence count, freshness and residual uncertainty are separately observable.
- [ ] An edge confidence is capped at 0.99 while a candidate.
- [ ] An edge confidence never becomes a device's `power` dimension.
- [ ] A `null` dimension stays distinct from `0.0`.

**Test requirements.**

| Test ID | Vehicle | Asserts |
| --- | --- | --- |
| `UT-inference-records::test_topology_and_residual_dimensions_are_populated` (new) | `pytest tests/unit` | the two dimensions carry values |
| `UT-association::test_evidence_publishes_counts_freshness_and_lag` (new) | `pytest tests/unit` | the four are separable |
| `UT-association::test_candidate_edge_confidence_is_capped` (new) | `pytest tests/unit` | the 0.99 cap holds |

**Builder assignment.**
Unassigned. Wave 3, segment 3.3. Write owner of `disaggregation/audit.py`,
`disaggregation/association.py` and `disaggregation/confidence.py` for wave 3.

**Blockers.**
- `OD-34` — `OD-34` assumes the `topology` and `residual` dimensions stay
  declared-but-unpopulated in V1; `REQ-CONFIDENCE-005` requires them to carry
  a value wherever the evidence exists. **Assumption:** the requirement wins —
  the dimensions carry a value where derivable and `null` otherwise. The
  orchestrator resolves `OD-34` in favour of the requirement, or rewords
  `REQ-CONFIDENCE-005`, before this task is verified.
