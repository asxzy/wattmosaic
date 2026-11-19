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
| P2 | 3 | NOT_STARTED | | | |
| P3 | 3 | NOT_STARTED | | | |
| P4 | 3 | NOT_STARTED | | | |
| P5 | 3 | NOT_STARTED | | | |
| T1 | 3 | IN_PROGRESS |  |  |  |
| T2 | 3 | NOT_STARTED | | | |
| T3 | 3 | NOT_STARTED | | | |
| T4 | 3 | NOT_STARTED | | | |
| I1 | 3 | VERIFIED |  | AT-10 | AT-10 property test in unit tests; marker flip pending |
| I2 | 3 | VERIFIED |  | AT-05 | AT-05 scenario in unit tests; marker flip pending K07 |
| I3 | 3 | VERIFIED |  | AT-06, 08, 09 | AT-06/08/09 closed; dropped verify.py side-effect files from merge |
| I4 | 3 | VERIFIED |  | AT-07 | AT-07 closed; 250 W test proves legacy defect + mutation guards |
| M1 | 3 | VERIFIED |  | AT-01, 13 | AT-01/13 scenarios in unit tests; marker flip pending; reconcile primary_display_tree with T1 |
| L1 | 3 | IN_PROGRESS |  |  |  |
| L2 | 3 | NOT_STARTED | | AT-03 | |
| L3 | 3 | NOT_STARTED | | | |
| D1 | 3 | IN_PROGRESS |  |  |  |
| D2 | 3 | IN_PROGRESS |  |  |  |
| D3 | 3 | NOT_STARTED | | AT-17 | |
| U1 | 3 | VERIFIED |  | AT-12 |  |
| I5 | 4 | NOT_STARTED | | AT-14 | |
| I6 | 4 | NOT_STARTED | | AT-18 | |
| I7 | 4 | NOT_STARTED | | | |
| T5 | 4 | NOT_STARTED | | AT-02 | |
| T6 | 4 | NOT_STARTED | | AT-04 | |
| T7 | 4 | NOT_STARTED | | AT-15, 16 | |
| M2 | 4 | NOT_STARTED | | AT-11 | |
| M3 | 4 | NOT_STARTED | | | |
| L4 | 4 | NOT_STARTED | | | |
| L5 | 4 | NOT_STARTED | | | |
| L6 | 4 | NOT_STARTED | | | |
| D4 | 4 | NOT_STARTED | | | |
| H1 | 4 | NOT_STARTED | | | |
| H2 | 4 | NOT_STARTED | | | |
| U2 | 4 | NOT_STARTED | | AT-20 | |
| P6 | 4 | NOT_STARTED | | | |
| Z2 | 5 | NOT_STARTED | | | |
| Z1 | 5 | NOT_STARTED | | | |
| L7 | 5 | NOT_STARTED | | AT-19 | |
| D5 | 5 | NOT_STARTED | | | |
| X09 | 5 | NOT_STARTED | | | |
| V1 | 6 | NOT_STARTED | | all 20 | |
| V2 | 6 | NOT_STARTED | | | |
| L8 | 6 | NOT_STARTED | | | optional |
| S00 | ? | VERIFIED | | | seam added by orchestrator: migration numbering, fresh-DB backup |
