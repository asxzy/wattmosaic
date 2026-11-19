# Progress — realtime-topology-disaggregation

**Rules**
- Status vocabulary (closed set): `NOT_STARTED`, `READY`, `IN_PROGRESS`, `BLOCKED`, `IN_REVIEW`, `VERIFIED`, `FAILED`.
- A builder edits **only its own row**, and only to `IN_PROGRESS`, `BLOCKED` (with the blocker named) or `IN_REVIEW` (with the PR link).
- Only the orchestrator sets `READY`, `VERIFIED` and `FAILED`, after running the task's named tests and the wave gate itself (`../implementation-plan.md` §6). A builder's self-report never sets `VERIFIED`.
- The acceptance column counts HB §21 tests whose xfail marker the task removed.

## Baseline (X00 fills this in)

| Item | Value |
|---|---|
| Integration branch | `feat/realtime-v3` |
| Branch point (`main` HEAD) | 27373aa |
| `verify.py` result | — (run by orchestrator) |
| pytest passed / skipped | all pass except the known stale failure tests/end_to_end/test_event_intake.py::test_linked_event_produces_an_acceptable_sample_shape (pre-existing on main, expects the removed ambiguous_topology gate) |
| `replay_nesting.py --score` (operator DB) | — |

## Decisions

| DEC | Status |
|---|---|
| ★ DEC-01, 04, 05, 10, 19, 25, 31 | APPROVED |
| all others | APPROVED |

## Tasks

| ID | Wave | Status | Owner / PR | Acceptance closed | Notes |
|---|---|---|---|---|---|
| X00 | 0 | VERIFIED | | | ADRs 0035–0041, supersession note, tracker baseline |
| X01 | 0 | VERIFIED | | | |
| X02 | 0 | VERIFIED | | | |
| X03 | 0 | VERIFIED | | | |
| X04 | 0 | VERIFIED | | | |
| X05 | 0 | VERIFIED | | | |
| X06 | 0 | VERIFIED | | | |
| X07 | 0 | VERIFIED | | | |
| K00 | 1 | VERIFIED | | | |
| K01 | 1 | VERIFIED |  |  |  |
| K02 | 1 | VERIFIED |  |  |  |
| K03 | 1 | VERIFIED |  |  |  |
| K04 | 1 | VERIFIED |  |  | K04b follow-up wired real types |
| K05 | 1 | VERIFIED |  |  | K05b follow-up reuses topology enums |
| K06 | 1 | VERIFIED |  |  |  |
| K07 | 1 | VERIFIED |  |  | K07b follow-up: closed-test rule (CLOSED_BY), AT-12 closed by U1 |
| K08 | 1 | VERIFIED |  |  |  |
| S13 | 2 | VERIFIED |  |  |  |
| S14 | 2 | VERIFIED |  |  |  |
| S15 | 2 | VERIFIED |  |  |  |
| S16 | 2 | VERIFIED |  |  |  |
| S17 | 2 | VERIFIED |  |  | insert_reconciliation bug did not exist; round-trip test added |
| S18 | 2 | VERIFIED |  |  |  |
| S19 | 2 | VERIFIED |  |  |  |
| P1 | 3 | VERIFIED |  |  | new path applies polarity once; legacy-path DEF-01 unproven (removed in Z2) |
| P2 | 3 | VERIFIED |  |  |  |
| P3 | 3 | VERIFIED |  |  | P3b follow-up: DATA_GAP delta None |
| P4 | 3 | VERIFIED |  |  | supersedes link not persisted (contract has no metadata) -> D5 |
| P5 | 3 | IN_PROGRESS |  |  |  |
| T1 | 3 | VERIFIED |  |  | primary_display_tree matches M1 build_columns |
| T2 | 3 | VERIFIED |  |  |  |
| T3 | 3 | VERIFIED |  |  | live loop wiring deferred to P6 |
| T4 | 3 | VERIFIED |  |  | orchestrator removed deps guard so v3 routes mount in production |
| I1 | 3 | VERIFIED |  | AT-10 | AT-10 property test in unit tests; marker flip pending |
| I2 | 3 | VERIFIED |  | AT-05 | AT-05 scenario in unit tests; marker flip pending K07 |
| I3 | 3 | VERIFIED |  | AT-06, 08, 09 | AT-06/08/09 closed; dropped verify.py side-effect files from merge |
| I4 | 3 | VERIFIED |  | AT-07 | AT-07 closed; 250 W test proves legacy defect + mutation guards |
| M1 | 3 | VERIFIED |  | AT-01, 13 | AT-01/13 scenarios in unit tests; marker flip pending; reconcile primary_display_tree with T1 |
| L1 | 3 | VERIFIED |  |  | /models/{id} now serves v3 payload via facade; U2 must adapt device view |
| L2 | 3 | VERIFIED |  | AT-03 | AT-03 closed |
| L3 | 3 | VERIFIED |  |  | timed out without summary; verified by tests; registry expected_window wired via spec |
| D1 | 3 | VERIFIED |  |  | uses repo private _conn/_lock for mapping writes (tidy later) |
| D2 | 3 | VERIFIED |  |  |  |
| D3 | 3 | VERIFIED |  | AT-17 | None-guards in legacy solver for nullable age; AT-17 closes with D4/H1 |
| U1 | 3 | VERIFIED |  | AT-12 |  |
| I5 | 4 | VERIFIED |  | AT-14 | AT-14 closed (snapshot single-count); V1 must also check matrix physical total |
| I6 | 4 | VERIFIED |  | AT-18 | AT-18 closed; record read index in-memory (persisted via S16) |
| I7 | 4 | IN_PROGRESS |  |  |  |
| T5 | 4 | VERIFIED |  | AT-02 | AT-02 closed; user CONFIRMED edges never auto-CONFLICTED -> fixed in T7; replay_nesting score needs operator DB (V2) |
| T6 | 4 | IN_PROGRESS |  | AT-04 |  |
| T7 | 4 | IN_PROGRESS |  | AT-15, 16 |  |
| M2 | 4 | VERIFIED |  | AT-11 | AT-11 closed |
| M3 | 4 | VERIFIED |  |  | M3b follow-up: real column/row trees in /matrix |
| L4 | 4 | VERIFIED |  |  |  |
| L5 | 4 | VERIFIED |  |  | DEF-03 fixed |
| L6 | 4 | NOT_STARTED | | | |
| D4 | 4 | VERIFIED |  |  |  |
| H1 | 4 | NOT_STARTED | | | |
| H2 | 4 | VERIFIED |  |  | partial: async_migrate_entry hook -> H1; v2 forms removal -> Z2 |
| U2 | 4 | VERIFIED |  | AT-20 | AT-20 closed |
| P6 | 4 | NOT_STARTED | | | |
| Z2 | 5 | NOT_STARTED | | | |
| Z1 | 5 | NOT_STARTED | | | |
| L7 | 5 | NOT_STARTED | | AT-19 | |
| D5 | 5 | IN_PROGRESS |  |  |  |
| X09 | 5 | NOT_STARTED | | | |
| V1 | 6 | NOT_STARTED | | all 20 | |
| V2 | 6 | NOT_STARTED | | | |
| L8 | 6 | NOT_STARTED | | | optional |
| S00 | ? | VERIFIED | | | seam added by orchestrator: migration numbering, fresh-DB backup |
