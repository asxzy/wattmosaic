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
| Branch point (`main` HEAD) | — |
| `verify.py` result | — |
| pytest passed / skipped | — |
| `replay_nesting.py --score` (operator DB) | — |

## Decisions

| DEC | Status |
|---|---|
| ★ DEC-01, 04, 05, 10, 19, 25, 31 | PROPOSED |
| all others | PROPOSED (proceed on default) |

## Tasks

| ID | Wave | Status | Owner / PR | Acceptance closed | Notes |
|---|---|---|---|---|---|
| X00 | 0 | NOT_STARTED | | | blocked on ★ decisions |
| X01 | 0 | READY | | | |
| X02 | 0 | READY | | | |
| X03 | 0 | READY | | | |
| X04 | 0 | READY | | | |
| X05 | 0 | READY | | | |
| X06 | 0 | READY | | | |
| X07 | 0 | READY | | | |
| K00 | 1 | NOT_STARTED | | | |
| K01 | 1 | NOT_STARTED | | | |
| K02 | 1 | NOT_STARTED | | | |
| K03 | 1 | NOT_STARTED | | | |
| K04 | 1 | NOT_STARTED | | | |
| K05 | 1 | NOT_STARTED | | | |
| K06 | 1 | NOT_STARTED | | | |
| K07 | 1 | NOT_STARTED | | | |
| K08 | 1 | NOT_STARTED | | | |
| S13 | 2 | NOT_STARTED | | | |
| S14 | 2 | NOT_STARTED | | | |
| S15 | 2 | NOT_STARTED | | | |
| S16 | 2 | NOT_STARTED | | | |
| S17 | 2 | NOT_STARTED | | | |
| S18 | 2 | NOT_STARTED | | | |
| S19 | 2 | NOT_STARTED | | | |
| P1 | 3 | NOT_STARTED | | | DEF-01 test first |
| P2 | 3 | NOT_STARTED | | | |
| P3 | 3 | NOT_STARTED | | | |
| P4 | 3 | NOT_STARTED | | | |
| P5 | 3 | NOT_STARTED | | | |
| T1 | 3 | NOT_STARTED | | | |
| T2 | 3 | NOT_STARTED | | | |
| T3 | 3 | NOT_STARTED | | | |
| T4 | 3 | NOT_STARTED | | | |
| I1 | 3 | NOT_STARTED | | AT-10 | |
| I2 | 3 | NOT_STARTED | | AT-05 | |
| I3 | 3 | NOT_STARTED | | AT-06, 08, 09 | |
| I4 | 3 | NOT_STARTED | | AT-07 | 250 W test first |
| M1 | 3 | NOT_STARTED | | AT-01, 13 | |
| L1 | 3 | NOT_STARTED | | | |
| L2 | 3 | NOT_STARTED | | AT-03 | |
| L3 | 3 | NOT_STARTED | | | |
| D1 | 3 | NOT_STARTED | | | |
| D2 | 3 | NOT_STARTED | | | |
| D3 | 3 | NOT_STARTED | | AT-17 | |
| U1 | 3 | NOT_STARTED | | AT-12 | |
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
