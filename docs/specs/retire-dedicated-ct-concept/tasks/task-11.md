# task-11 — Documentation and ADRs

**Wave:** 5 · **Depends on:** task-04, task-06 · **Owner:** `docs/**`

## Objective

Make the documentation state the new vocabulary and record the supersession of ADR-0025/0026.

## Context

Several docs assert the removed concept: the three-role table in `docs/electrical_model.md:31-44`, the monitoring axis table in `docs/user_guide.md:328-347`, `docs/configuration.md:116`/`:135`, `docs/api.md:230`, `docs/integration.md` role references, and ADR narrative. `docs/interface_contracts.md:198-200` mandates its own update on a contract change.

AC-1 requires no shipped doc to contain "dedicated".

## Scope

Docs only. No code.

## Files owned

- `docs/decisions/README.md`
- `docs/electrical_model.md`
- `docs/configuration.md`
- `docs/api.md`
- `docs/user_guide.md`
- `docs/integration.md`
- `docs/algorithms.md`
- `docs/interface_contracts.md`
- `PROJECT_STATUS.md`
- `RELEASE_NOTES.md`

## Steps

1. Add **ADR-0027** (text supplied in `decisions.md`) to `docs/decisions/README.md`.
2. Amend ADR-0025 (`:1110-1112`) and ADR-0026 (`:1198-1200`) headers with a supersession note pointing at ADR-0027.
3. Rewrite the three-role table in `electrical_model.md:31-44` as a two-role model (main / branch) plus `unknown`.
4. Update `user_guide.md:328-347`: remove the `dedicated` row from the monitoring axis; resolve the existing contradiction at `:345` (it claims a sole-occupant branch is "also dedicated in fact", which the code never did).
5. Update `configuration.md:116`/`:135` enum lists.
6. Update `api.md:230` (remove `dedicated_ct_ids`).
7. Update `integration.md` role references (`:117`, `:124`, `:131-136`, `:176`, `:187`, `:198`).
8. Update `interface_contracts.md:3` version and `:198-200` protocol note.
9. Append a `PROJECT_STATUS.md` section recording the removal and its accepted consequence. Do **not** state a device count unless the build agent has counted the live rows; `PROJECT_STATUS.md` records 21/0/22/26 across snapshots and a 2026-11-10 reset may have destroyed the data.
10. Add a `RELEASE_NOTES.md` entry for the next dev release describing the contract break and the migration.

## Tests

- Docs gate in `tools/dev/verify.py` (release/docs checks).
- Add a docs scan test asserting no `/dedicat/i` match in `docs/**` and `PROJECT_STATUS.md`/`RELEASE_NOTES.md`, **excluding** this specification directory `docs/specs/retire-dedicated-ct-concept/**` (which intentionally discusses the retired term).

## Acceptance criteria

- AC-1 (docs part): no shipped doc contains "dedicated".
- AC-10: contract doc updated per its own protocol.
- AC-11: ADR-0027 present and cross-referenced.
