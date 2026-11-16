# TASK-POWER-002 — Measured-step attribution, non-HA devices, entity separation and declared bounds

**Task ID.** `TASK-POWER-002`
**Title.** Measured-step attribution, non-HA devices, entity separation and declared bounds
**Status.** `NOT_STARTED`
**Requirement IDs.** `REQ-POWER-003`, `REQ-POWER-005`, `REQ-POWER-006`, `REQ-POWER-007`

**Description.**
Credit a state change with the measured step from the same reader, support a
device that has no Home Assistant entity, keep the five entity identities
separate, and treat a declared wattage as a hard upper bound on its row.

**Expected output.**
- A transition is credited with the measured step from the same reader.
- A device with no HA entity is published with an honest confidence.
- The five entity identities are separate fields.
- A declared wattage is a non-relaxable upper bound on its row.

**Files / modules affected.**

| Path | Role |
| --- | --- |
| `engine/wattmosaic_engine/disaggregation/attachment.py` (new) | the device attachment |
| `engine/wattmosaic_engine/disaggregation/engine.py` | the measured-step credit |
| `engine/wattmosaic_engine/contracts.py` | `HaConnectivity`, `HaEntityRole` |
| `engine/wattmosaic_engine/api/server.py` | the device identity read routes |

**Input contracts.**

| Contract | Source | Note |
| --- | --- | --- |
| The load-shape class and the power levels | `TASK-POWER-001` | the profile vocabulary |
| The joint inference result | `TASK-DISAGG-001` | the row the bound applies to |
| The event grouping | `TASK-EVENT-001` | the transition to credit |

**Output contracts.**

| Contract | Consumer | Note |
| --- | --- | --- |
| The attached device power | `api/server.py`, the panel | source-attributed |
| The five entity identities | `api/server.py` | separate fields, never merged |

**Dependencies.**

| Task | Reason |
| --- | --- |
| `TASK-DISAGG-001` | the joint inference result and the row budget |

**Acceptance criteria.**
- [ ] A transition is credited with the measured step from the same reader.
- [ ] A device with no HA entity is published with an honest confidence.
- [ ] The five entity identities are separate fields.
- [ ] A declared wattage is a non-relaxable upper bound on the row it shares.
- [ ] A device is never invented; no unattributed power is given a device.

**Test requirements.**

| Test ID | Vehicle | Asserts |
| --- | --- | --- |
| `T01` · `UT-disaggregation::test_01_single_device_activation` | scenario `T01` | the measured step is credited |
| `T10` · `UT-device-taxonomy::test_device_without_an_entity_is_first_class` (new) | scenario `T10` | the device is published honestly |
| `UT-device-links::test_the_five_identities_stay_separate` (new) | `pytest tests/unit` | the five fields stay apart |
| `UT-device-attachment::test_configured_priors_cannot_exceed_the_row_they_share` | `pytest tests/unit` | the bound holds |

**Builder assignment.**
Unassigned. Wave 2, segment 2.5. Write owner of the new
`disaggregation/attachment.py` for wave 2.

**Blockers.**
- None. The `contracts.py`, `disaggregation/engine.py` and `api/server.py`
  writes are **write requests** to their wave-2 owners.
