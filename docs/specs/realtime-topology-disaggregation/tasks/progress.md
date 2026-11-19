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
| P5 | 3 | VERIFIED |  |  | unlinked HA event persistence via callback, wired in P6 |
| T1 | 3 | VERIFIED |  |  | primary_display_tree matches M1 build_columns |
| T2 | 3 | VERIFIED |  |  |  |
| T3 | 3 | VERIFIED |  |  | live loop wiring deferred to P6 |
| T4 | 3 | VERIFIED |  |  | orchestrator removed deps guard so v3 routes mount in production |
| I1 | 3 | VERIFIED |  | AT-10 | AT-10 closed via ATX |
| I2 | 3 | VERIFIED |  | AT-05 | AT-05 closed via ATX |
| I3 | 3 | VERIFIED |  | AT-06, 08, 09 | AT-06/08/09 closed; dropped verify.py side-effect files from merge |
| I4 | 3 | VERIFIED |  | AT-07 | AT-07 closed; 250 W test proves legacy defect + mutation guards |
| M1 | 3 | VERIFIED |  | AT-01, 13 | AT-01/13 closed via ATX |
| L1 | 3 | VERIFIED |  |  | /models/{id} now serves v3 payload via facade; U2 must adapt device view |
| L2 | 3 | VERIFIED |  | AT-03 | AT-03 closed |
| L3 | 3 | VERIFIED |  |  | timed out without summary; verified by tests; registry expected_window wired via spec |
| D1 | 3 | VERIFIED |  |  | uses repo private _conn/_lock for mapping writes (tidy later) |
| D2 | 3 | VERIFIED |  |  |  |
| D3 | 3 | VERIFIED |  | AT-17 | None-guards in legacy solver for nullable age; AT-17 closes with D4/H1 |
| U1 | 3 | VERIFIED |  | AT-12 |  |
| I5 | 4 | VERIFIED |  | AT-14 | AT-14 closed (snapshot single-count); V1 must also check matrix physical total |
| I6 | 4 | VERIFIED |  | AT-18 | AT-18 closed; record read index in-memory (persisted via S16) |
| I7 | 4 | VERIFIED |  |  |  |
| T5 | 4 | VERIFIED |  | AT-02 | AT-02 closed; user CONFIRMED edges never auto-CONFLICTED -> fixed in T7; replay_nesting score needs operator DB (V2) |
| T6 | 4 | VERIFIED |  | AT-04 | AT-04 closed |
| T7 | 4 | VERIFIED |  | AT-15, 16 | AT-15/16 closed; DEC-10 CONFLICTED implemented; config-events facade wiring -> P6 |
| M2 | 4 | VERIFIED |  | AT-11 | AT-11 closed |
| M3 | 4 | VERIFIED |  |  | M3b follow-up: real column/row trees in /matrix |
| L4 | 4 | VERIFIED |  |  |  |
| L5 | 4 | VERIFIED |  |  | DEF-03 fixed |
| L6 | 4 | VERIFIED |  |  | executor wiring in P6 |
| D4 | 4 | VERIFIED |  |  |  |
| H1 | 4 | VERIFIED |  |  | AT-17 HA-side helper done; AT-17 e2e closes with P6 |
| H2 | 4 | VERIFIED |  |  | partial: async_migrate_entry hook -> H1; v2 forms removal -> Z2 |
| U2 | 4 | VERIFIED |  | AT-20 | AT-20 closed |
| P6 | 4 | VERIFIED |  |  | AT-17 closed; P6b live wiring; P6c real T7 detection (harness scaffolding removed) |
| Z2 | 5 | VERIFIED |  |  | Z2b/Z2c/Z2d merged; topology_confirm button -> Z1 |
| Z1 | 5 | VERIFIED |  |  | complete: ZC deleted legacy contracts/paths; gate green at c6a48f2 |
| L7 | 5 | VERIFIED |  | AT-19 | AT-19 closed |
| D5 | 5 | VERIFIED |  |  | v021 supersedes link; P6 must pass supersedes_evidence_id |
| X09 | 5 | VERIFIED |  |  | docs+release notes 3.0; routes/settings cross-checked |
| V1 | 6 | VERIFIED |  | all 20 (0 xfail in tests/acceptance) | V1b: live groups, relocation, measured endpoints; 0 xfail; gate green at 50c7fd9 |
| V2 | 6 | NOT_STARTED | | | |
| L8 | 6 | NOT_STARTED | | | optional |
| S00 | ? | VERIFIED | | | seam added by orchestrator: migration numbering, fresh-DB backup |
| ZF | ? | VERIFIED | | | quality from ReaderStateStore, v022 nullable confidence; ratio fixed to |balance_error|/main |
| V1a | ? | VERIFIED | | | v3 accuracy harness merged; HB17 runtime path publishes nothing -> V1 investigates |
| V2a | ? | IN_REVIEW | | | tools merged; rehearsal PASS 11->25 and back; perf tool uses stub health/matrix + replay relabels versions -> V2b |
| V1c | ? | VERIFIED | | | MEASURED only from declared single-subject endpoint; gate green |
