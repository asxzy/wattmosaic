# TASK-MAPPING-001 — The derived mapping projection

**Task ID.** `TASK-MAPPING-001`
**Title.** The derived mapping projection
**Status.** `NOT_STARTED`
**Requirement IDs.** `REQ-MAPPING-001`, `REQ-MAPPING-002`, `REQ-MAPPING-003`, `REQ-MAPPING-006`, `REQ-MAPPING-007`

**Description.**
Build the device↔reader mapping matrix as a derived projection — never
written, never a second source of truth — with every cardinality expressible,
every cell self-describing and a coverage block that says what the matrix
cannot see.

**Expected output.**
- The matrix is derived from relations, residual history, the model registry and the topology; it is never written.
- 1:1, 1:N, N:1 and N:N are all expressible.
- Every cell publishes its nine fields.
- The reader count never multiplies into the confidence.
- The response carries a coverage block.

**Files / modules affected.**

| Path | Role |
| --- | --- |
| `engine/wattmosaic_engine/disaggregation/mapping.py` (new) | the projection |
| `engine/wattmosaic_engine/api/server.py` | the `GET /mapping` route |

**Input contracts.**

| Contract | Source | Note |
| --- | --- | --- |
| The typed relations | `TASK-TOPOLOGY-002` | the containment and shared-load edges |
| The residual history | `TASK-RESIDUAL-001` | the unexplained scope |
| The model registry | `TASK-POWER-001` | the known device profiles |

**Output contracts.**

| Contract | Consumer | Note |
| --- | --- | --- |
| `GET /mapping` | the panel `#/topology` | additive route, contract header, no table behind it |
| The mapping cell | the panel | its nine fields and its own state |

**Dependencies.**

| Task | Reason |
| --- | --- |
| `TASK-TOPOLOGY-002` | the typed relations the projection reads |

**Acceptance criteria.**
- [ ] The matrix is derived and deterministic; it is never written to storage.
- [ ] 1:1, 1:N, N:1 and N:N are all expressible.
- [ ] Every cell publishes its nine fields.
- [ ] The reader count never multiplies into the confidence.
- [ ] The response carries a coverage block naming what the matrix cannot see.
- [ ] A missing relation is reported as missing, never guessed.

**Test requirements.**

| Test ID | Vehicle | Asserts |
| --- | --- | --- |
| `UT-mapping::test_mapping_is_derived_and_deterministic` (new) | `pytest tests/unit` | the same inputs give the same output |
| `T13` · `UT-mapping::test_mapping_supports_every_cardinality` (new) | scenario `T13` | all four cardinalities work |
| `UT-api::test_mapping_cell_response_shape` (new) | `pytest tests/unit` | the nine fields are present |
| `UT-mapping::test_reader_count_does_not_raise_confidence` (new) | `pytest tests/unit` | the confidence is unaffected |
| `UT-mapping::test_mapping_reports_its_coverage` (new) | `pytest tests/unit` | the coverage block is present |

**Builder assignment.**
Unassigned. Wave 3, segment 3.1. Write owner of the new
`disaggregation/mapping.py` and of `api/server.py` for wave 3.

**Blockers.**
- `ASM-06` — **RESOLVED**. The project owner decided that the Device↔Reader
  mapping module **is** built in V1 ("Build it in V1.", 2026-11-16). The
  assumption is therefore no longer a blocker: this task proceeds, and it
  proceeds on the requirement text in `01-requirements.md` unchanged. The
  decision is recorded in `13-open-decisions-and-assumptions.md` §4 (`ASM-06`,
  `RESOLVED`); the entry is kept visible so the history of the decision stays
  traceable.
