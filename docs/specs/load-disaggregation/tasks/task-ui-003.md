# TASK-UI-003 — Panel evidence, version history, operator correction and storage health

**Task ID.** `TASK-UI-003`
**Title.** Panel evidence, version history, operator correction and storage health
**Status.** `NOT_STARTED`
**Requirement IDs.** `REQ-UI-009`, `REQ-UI-010`, `REQ-UI-011`, `REQ-UI-012`

**Description.**
Show the operator the evidence behind a decision, the version history and the
suspected changes that are visibly not applied, the two operator actions, and
the storage health.

**Expected output.**
- The event list shows the verdict, the window bounds, the competitors and the reason code.
- Two versions can be compared, and a suspected change is visibly not applied.
- A correction is stored with `configured_by="user"`, and a relation can be withdrawn with a reason.
- The health view names the retention horizons, the row counts and the last prune.

**Files / modules affected.**

| Path | Role |
| --- | --- |
| `addon/ui/app.js` | the diagnostics view, the version history, the operator actions |
| `engine/wattmosaic_engine/api/server.py` | the correction and withdrawal routes |

**Input contracts.**

| Contract | Source | Note |
| --- | --- | --- |
| The views | `TASK-UI-002` | the view set |
| The configuration event | `TASK-CHANGE-001` | the suspected change and its state |
| The four version identifiers | `TASK-CHANGE-002` | the comparison |
| The tier registry | `TASK-STORAGE-001` | the retention horizons |

**Output contracts.**

| Contract | Consumer | Note |
| --- | --- | --- |
| The operator correction | `storage/database.py` | `configured_by="user"` |
| The relation withdrawal | `disaggregation/association.py` | with a reason |

**Dependencies.**

| Task | Reason |
| --- | --- |
| `TASK-UI-002` | the views this task extends |

**Acceptance criteria.**
- [ ] The event list shows the verdict, the window bounds, the competitors and the reason code.
- [ ] Two versions can be compared.
- [ ] A suspected change is visibly not applied.
- [ ] A correction is stored with `configured_by="user"`.
- [ ] A relation can be withdrawn with a reason.
- [ ] The health view names the retention horizons, the row counts and the last prune.
- [ ] The panel remains read-only apart from the two operator actions.

**Test requirements.**

| Test ID | Vehicle | Asserts |
| --- | --- | --- |
| `UT-panel::test_event_list_shows_verdict_and_evidence` (new) | `pytest tests/unit` | the evidence is shown |
| `UT-panel::test_version_history_and_suspected_changes_are_visible` (new) | `pytest tests/unit` | the history is visible |
| `IT-confirm-topology::test_operator_correction_is_recorded_as_a_user_declaration` (new) | `pytest tests/integration` | the correction is attributed |
| `UT-panel::test_storage_health_view` (new) | `pytest tests/unit` | the health view is complete |

**Builder assignment.**
Unassigned. Wave 4, segment 4.3. Write owner of `api/server.py` for wave 4.

**Blockers.**
- None. The `addon/ui/app.js` write is a **write request** to `TASK-UI-001`,
  the wave-4 owner of that file.
