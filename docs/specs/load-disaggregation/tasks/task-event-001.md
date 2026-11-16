# TASK-EVENT-001 — Event kinds, grouping, lag and window sizing

**Task ID.** `TASK-EVENT-001`
**Title.** Event kinds, grouping, lag and window sizing
**Status.** `NOT_STARTED`
**Requirement IDs.** `REQ-EVENT-001`, `REQ-EVENT-002`, `REQ-EVENT-003`, `REQ-EVENT-004`

**Description.**
Give every event exactly one of the three kinds, treat one automation that
changes several devices as one group, measure the response delay instead of
assuming it, and size the event window from the observed cadence.

**Expected output.**
- Every event names one of the three kinds: HA-driven, measurement-driven or unknown-residual.
- One automation produces one group.
- A response beyond `max_lag_s` is withheld as `lag_exceeded`.
- The resolved window and its source are published.

**Files / modules affected.**

| Path | Role |
| --- | --- |
| `engine/wattmosaic_engine/contracts.py` | the event-kind member |
| `engine/wattmosaic_engine/ingestion/ha_source.py` | the event production |
| `engine/wattmosaic_engine/disaggregation/isolation.py` | `EventIsolator`, the grouping |
| `engine/wattmosaic_engine/disaggregation/configuration.py` | `IsolationConfig`, the window |
| `engine/wattmosaic_engine/disaggregation/association.py` | the lag-aware observation |

**Input contracts.**

| Contract | Source | Note |
| --- | --- | --- |
| The observed cadence | `TASK-MEASURE-003` | the window source |
| `DeviceEvent` | `contracts.py` | the existing event shape |

**Output contracts.**

| Contract | Consumer | Note |
| --- | --- | --- |
| The event kind | `disaggregation/events.py`, the API | one of three |
| The resolved window | `disaggregation/isolation.py`, `GET /isolation` | published with its source |

**Dependencies.**

| Task | Reason |
| --- | --- |
| `TASK-MEASURE-003` | the window is sized from the observed cadence |

**Acceptance criteria.**
- [ ] Every event names one of the three kinds.
- [ ] One automation that changes several devices produces one group.
- [ ] A response beyond `max_lag_s` is withheld with the reason `lag_exceeded`.
- [ ] The resolved window and its source are published.
- [ ] The window size is derived, never hard-coded.

**Test requirements.**

| Test ID | Vehicle | Asserts |
| --- | --- | --- |
| `T10` · `E2E-event-intake::test_every_event_names_its_kind` | scenario `T10` | every event names a kind |
| `T06` · `UT-correlation::test_fixtures_switched_by_one_scene_are_withheld_not_counted_twice` | scenario `T06` | one group, one credit |
| `T09` · `UT-state-lag::test_response_beyond_max_lag_is_withheld` (new) | scenario `T09` | the lag rule fires |
| `UT-event-isolation::test_resolved_window_publishes_its_source` (new) | `pytest tests/unit` | the source is published |

**Builder assignment.**
Unassigned. Wave 2, segment 2.3. Write owner of
`disaggregation/isolation.py`, `disaggregation/configuration.py` and
`ingestion/ha_source.py` for wave 2.

**Blockers.**
- The `contracts.py` write is a **write request** to `TASK-TOPOLOGY-001`, the
  wave-2 owner of that file. Not a blocker.
