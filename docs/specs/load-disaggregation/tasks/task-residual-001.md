# TASK-RESIDUAL-001 — Signed residual, decomposition, history and the unresolved state

**Task ID.** `TASK-RESIDUAL-001`
**Title.** Signed residual, decomposition, history and the unresolved state
**Status.** `NOT_STARTED`
**Requirement IDs.** `REQ-RESIDUAL-001`, `REQ-RESIDUAL-002`, `REQ-RESIDUAL-003`, `REQ-RESIDUAL-004`, `REQ-RESIDUAL-008`

**Description.**
Make the signed residual the only subtraction in the system, decompose it into
baseline, dynamic and noise, give it history, and keep the unresolved case
honest.

**Expected output.**
- `residual_w` computed as the single signed subtraction.
- `baseline_w + dynamic_w == residual_w` for every published point.
- A residual history route and a residual trend with its operating pattern.
- An unresolved residual is `null` with an age — never `0 W`.

**Files / modules affected.**

| Path | Role |
| --- | --- |
| `engine/wattmosaic_engine/models/residual_power.py` | `ResidualAnalyzer`, `ResidualPower`, `ResidualTrend` |
| `engine/wattmosaic_engine/storage/database.py` | the residual read and write path |
| `engine/wattmosaic_engine/api/server.py` | the residual route |
| `engine/wattmosaic_engine/api/facade.py` | the facade method behind it |

**Input contracts.**

| Contract | Source | Note |
| --- | --- | --- |
| The reconciliation result | `disaggregation/engine.py` | the inputs of the subtraction |
| The residual payload | `models/residual_power.py` | the persisted shape |

**Output contracts.**

| Contract | Consumer | Note |
| --- | --- | --- |
| `ResidualPower` | `api/server.py`, `addon/ui/app.js` | signed, decomposed, possibly `null` |
| The residual history route | the panel `#/quality` | additive route |

**Dependencies.**

| Task | Reason |
| --- | --- |
| `TASK-FOUNDATION-002` | the contract bump must land first |

**Acceptance criteria.**
- [ ] The signed residual is the only subtraction; no other path subtracts.
- [ ] `baseline_w + dynamic_w == residual_w` holds for every published point.
- [ ] The trend publishes its pattern with a `None` confidence when there is no history.
- [ ] An unresolved residual is `null` with an age; it is never `0 W`.
- [ ] A negative residual is never clamped.

**Test requirements.**

| Test ID | Vehicle | Asserts |
| --- | --- | --- |
| `T15` · `UT-residual-power::test_signed_residual_is_never_clamped` | scenario `T15` | the negative residual survives |
| `T03` · `UT-residual-power::test_baseline_plus_dynamic_equals_residual` | scenario `T03` | the decomposition identity |
| `UT-api::test_residual_history_route` (new) | `pytest tests/unit` | the history route answers |
| `T03`, `T10` · `UT-residual-power::test_residual_trend_publishes_its_pattern` | scenarios | the pattern and its `None` confidence |
| `UT-residual-power::test_unresolved_residual_is_none_not_zero` (new) | `pytest tests/unit` | `null`, not `0 W` |

**Builder assignment.**
Unassigned. Wave 1, segment 1.4. Write owner of `storage/database.py`,
`api/server.py` and `api/facade.py` for wave 1.

**Blockers.**
- The `storage/database.py` write is a **write request** to `TASK-STORAGE-001`,
  the wave-1 owner of that file. Not a blocker; the request is filed.
