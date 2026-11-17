# TASK-DISAGG-002 — No fabrication, single counting and the balance identity

**SPEC_STATUS: APPROVED_FOR_IMPLEMENTATION**

**Task ID.** `TASK-DISAGG-002`
**Title.** No fabrication, single counting and the balance identity
**Status.** `NOT_STARTED`
**Requirement IDs.** `REQ-DISAGG-005`, `REQ-DISAGG-006`, `REQ-DISAGG-007`

**Description.**
Attach provenance to every published value, count a load visible to several
readers exactly once, and make the home-level balance identity hold for every
row that is written.

**Expected output.**
- Every published value carries its source, its quality status and its evidence tier.
- A device with no evidence is `TIER_0_UNKNOWN` with no watt figure.
- A load visible to several nested readers is attributed once.
- Both reconciliation identities hold for every written row.

**Files / modules affected.**

| Path | Role |
| --- | --- |
| `engine/wattmosaic_engine/disaggregation/engine.py` | the provenance fields, `_reconcile` |
| `engine/wattmosaic_engine/electrical/matrix.py` | the outermost-feed-only counting |
| `engine/wattmosaic_engine/contracts.py` | the published-source fields |

**Input contracts.**

| Contract | Source | Note |
| --- | --- | --- |
| The joint inference result | `TASK-DISAGG-001` | the numbers being published |
| The nesting rules | `electrical/matrix.py` | the double-counting rule |

**Output contracts.**

| Contract | Consumer | Note |
| --- | --- | --- |
| The published result | `api/server.py`, the panel | source, quality status, tier |
| The reconciliation row | `storage/database.py` | both identities hold |

**Dependencies.**

| Task | Reason |
| --- | --- |
| `TASK-DISAGG-001` | the joint inference result |

**Acceptance criteria.**
- [ ] Every published value carries its source, quality status and evidence tier.
- [ ] A device with no evidence is `TIER_0_UNKNOWN` and carries no watt figure.
- [ ] A load visible to several readers is attributed once.
- [ ] Both reconciliation identities hold for every written row.
- [ ] No per-device number is fabricated anywhere in this path.
- [ ] A missing measurement never aborts the cycle and never fabricates a value.

**Test requirements.**

| Test ID | Vehicle | Asserts |
| --- | --- | --- |
| `T07` · `UT-disaggregation::test_12_missing_sensor_samples_do_not_abort_or_fabricate` | scenario `T07` | the cycle continues |
| `T06` · `UT-electrical::test_nested_main_sums_only_the_outermost_feed` | scenario `T06` | counted once |
| `UT-reconciliation-split::test_balance_identities_hold` (new) | `pytest tests/unit` | both identities hold |

**Builder assignment.**
Unassigned. Wave 2, segment 2.4. Write owner of `contracts.py` for wave 2.

**Blockers.**
- The `contracts.py` and `disaggregation/engine.py` writes are **write
  requests** to `TASK-TOPOLOGY-001` and `TASK-DISAGG-001` respectively, the
  wave-2 owners. Not blockers.
