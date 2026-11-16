# TASK-POWER-001 — Load-shape class and the model-version surface

**Task ID.** `TASK-POWER-001`
**Title.** Load-shape class and the model-version surface
**Status.** `NOT_STARTED`
**Requirement IDs.** `REQ-POWER-001`, `REQ-POWER-002`, `REQ-POWER-004`, `REQ-POWER-008`

**Description.**
Add the load-shape class as a separate axis beside `ModelType`, give a device
a profile that distinguishes standby, idle, active and transient power, and
make model versions listable, promotable and reversible.

**Expected output.**
- The six load-shape classes exist as an axis separate from `ModelType` and are never inferred from a name.
- A published power names its level; an inrush sample enters neither a steady-state fit nor the energy integral.
- An off device's standby draw is published and counted once.
- Model versions are listed, promoted and rolled back without deletion.

**Files / modules affected.**

| Path | Role |
| --- | --- |
| `engine/wattmosaic_engine/contracts.py` | the load-shape axis, the power levels |
| `engine/wattmosaic_engine/models/registry.py` | `DeviceModelVersion`, the registry |
| `engine/wattmosaic_engine/models/device_profile.py` (new) | the device power profile |

**Input contracts.**

| Contract | Source | Note |
| --- | --- | --- |
| `ModelType`, `ModelStatus` | `contracts.py` | reused; not extended with load shapes |
| The inference result | `TASK-DISAGG-001` | the published power |

**Output contracts.**

| Contract | Consumer | Note |
| --- | --- | --- |
| The load-shape class | `api/server.py`, the panel `#/device/:id` | a separate axis |
| The model version | `models/registry.py`, `storage/database.py` | listed, promoted, reversible |

**Dependencies.**

| Task | Reason |
| --- | --- |
| `TASK-FOUNDATION-002` | the contract bump must land first |

**Acceptance criteria.**
- [ ] The six load-shape classes are a separate axis from `ModelType`.
- [ ] The load-shape class is never inferred from a device name.
- [ ] A published power names its level.
- [ ] An inrush sample enters neither a steady-state fit nor the energy integral.
- [ ] An off device's standby draw is published and counted once.
- [ ] Model versions are listed, promoted and rolled back without deletion.

**Test requirements.**

| Test ID | Vehicle | Asserts |
| --- | --- | --- |
| `T08` · `UT-device-taxonomy::test_load_shape_class_is_published` (new) | scenario `T08` | the class is published |
| `T04` · `UT-models::test_binary_model_median_ignores_inrush_outlier` | scenario `T04` | the inrush is excluded |
| `T04` · `UT-disaggregation::test_17_standby_power_is_preserved` | scenario `T04` | the standby draw survives |
| `UT-models::test_registry_round_trips_persisted_model_and_estimates` | `pytest tests/unit` | the round trip holds |

**Builder assignment.**
Unassigned. Wave 2, segment 2.2. Write owner of `models/registry.py` and
the new `models/device_profile.py` for wave 2.

**Blockers.**
- The `contracts.py` write is a **write request** to `TASK-TOPOLOGY-001`, the
  wave-2 owner of that file. Not a blocker.
