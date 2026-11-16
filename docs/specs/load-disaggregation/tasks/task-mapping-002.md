# TASK-MAPPING-002 — Unknown-load entries and the evidence-triggered refresh

**Task ID.** `TASK-MAPPING-002`
**Title.** Unknown-load entries and the evidence-triggered refresh
**Status.** `NOT_STARTED`
**Requirement IDs.** `REQ-MAPPING-004`, `REQ-MAPPING-005`

**Description.**
Let an unexplained scope appear in the matrix as an unknown-load entry that
names no device, and refresh the projection only when the evidence or the
models change — never per sample.

**Expected output.**
- An unexplained scope shows an unknown-load entry that names no device.
- A steady stream of identical samples produces no matrix write.
- An association withdrawal produces exactly one matrix write.

**Files / modules affected.**

| Path | Role |
| --- | --- |
| `engine/wattmosaic_engine/disaggregation/mapping.py` (new) | the unknown-load entry, the refresh trigger |
| `engine/wattmosaic_engine/models/residual_power.py` | the unknown-load profile |
| `engine/wattmosaic_engine/storage/database.py` | the refresh marker |

**Input contracts.**

| Contract | Source | Note |
| --- | --- | --- |
| The derived projection | `TASK-MAPPING-001` | the cells and the coverage block |
| The signed residual | `TASK-RESIDUAL-001` | the unexplained scope |
| The typed relations | `TASK-TOPOLOGY-002` | the withdrawal signal |

**Output contracts.**

| Contract | Consumer | Note |
| --- | --- | --- |
| The unknown-load entry | `GET /mapping`, the panel | names no device |
| The refresh marker | `storage/database.py` | one write per evidence change |

**Dependencies.**

| Task | Reason |
| --- | --- |
| `TASK-MAPPING-001` | the projection this task extends |

**Acceptance criteria.**
- [ ] An unexplained scope shows an unknown-load entry that names no device.
- [ ] The unknown-load entry is a first-class identity with a lifecycle.
- [ ] A steady stream of identical samples produces no matrix write.
- [ ] An association withdrawal produces exactly one matrix write.
- [ ] The refresh is triggered by an evidence or model change, never by a sample.

**Test requirements.**

| Test ID | Vehicle | Asserts |
| --- | --- | --- |
| `T04`, `T10` · `UT-mapping::test_unknown_load_entry_names_no_device` (new) | scenarios | no device is named |
| `UT-inference-records::test_mapping_refreshes_only_on_an_evidence_change` (new) | `pytest tests/unit` | the trigger is the evidence |

**Builder assignment.**
Unassigned. Wave 3, segment 3.5. Write owner of the unknown-load entry in
`disaggregation/mapping.py` for wave 3.

**Blockers.**
- `ASM-06` — **RESOLVED**. The project owner decided that the Device↔Reader
  mapping module **is** built in V1 ("Build it in V1.", 2026-11-16), so the
  assumption is no longer a blocker: this task proceeds alongside
  `TASK-MAPPING-001` on the requirement text in `01-requirements.md`
  unchanged. The decision is recorded in `13-open-decisions-and-assumptions.md`
  §4 (`ASM-06`, `RESOLVED`); the entry is kept visible so the history of the
  decision stays traceable.
- The `models/residual_power.py` and `storage/database.py` writes are **write
  requests** to their wave-3 owners.
