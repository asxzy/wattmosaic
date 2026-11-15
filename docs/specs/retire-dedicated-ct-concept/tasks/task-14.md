# task-14 — Test suite alignment sweep

**Wave:** 8 (continuous) · **Depends on:** all preceding tasks · **Owner:** `tests/**`

## Objective

Leave no test asserting a removed value, and add the acceptance-criteria tests that map 1:1 to `design.md`.

## Context

Investigation counted ~142 matches in `tests/unit` across 23 files and ~47 in `tests/integration` across 5 files, plus simulation/e2e. Most are fixture constructions that compile against `dedicated_ct_ids=[...]`; those need edits only because the field itself is removed. A smaller set pins assertions that must change semantically.

## Scope

Test files only, plus any fixture builders under `tools/` that tests import.

## Files owned

- `tests/unit/**`
- `tests/integration/**`
- `tests/end_to_end/**`
- `tests/simulation/**`

`tools/simulator/topology.py` and `tools/dev/make_reference_house.py` are **not** owned here by default, but the "survives unchanged" claim was wrong for the first of them. `tools/simulator/topology.py` builds contract `CTChannel`s at module scope using the removed `CTType.DEDICATED_DEVICE` member, so it failed at import and caused all 12 test-collection errors. It was therefore repointed to `CTType.BRANCH` in Wave 2 (its simulator metadata `d.dedicated_ct` is untouched). Further edits to `tools/simulator/**` are permitted if a test requires them — raise the change rather than working around it, but do not treat the file as frozen.

## Steps

1. Work file by file through the pinning list:
   - `tests/unit/test_energy_quality.py:395-421`, `:378`, `:547`
   - `tests/unit/test_device_config_api.py:192`, `:224`, `:283`, `:128-140`
   - `tests/unit/test_device_taxonomy.py:241-269`, `:659`, `:675`, `:770`
   - `tests/unit/test_config_flow_logic.py:227`
   - `tests/unit/test_device_attachment.py:146`, `:153-236`
   - `tests/unit/test_auto_placement.py:144`
   - `tests/unit/test_quality.py:226`
   - `tests/unit/test_nesting.py:35`
   - `tests/unit/test_nesting_perf.py:311-312`, `:364`
   - `tests/unit/test_nesting_wizard.py:83`, `:173-175`
   - `tests/unit/test_audit_tools.py:185`, `:199-201`, `:223-229`, `:245-247`
   - `tests/unit/test_discover_placements.py:569`
   - `tests/unit/test_correlation.py:885`, `:1022`
   - `tests/integration/test_ha_integration.py:457-462`, `:1520-1550`, `:1580-1598`, `:1619-1643`, `:1691-1693`, `:1731`, `:1740-1744`, `:1753-1759`, `:1777-1833`
   - `tests/integration/test_learning_intake.py:619-624`
   - `tests/integration/test_backend_wiring.py:47-51`
   - `tests/integration/test_engine_api_contract.py:43`
   - `tests/end_to_end/test_api_e2e.py:284`
   - `tests/end_to_end/test_storage_e2e.py:127`
   - `tests/end_to_end/test_event_intake.py:456`, `:565`
   - `tests/simulation/test_real_topology.py:383-392` (asserts the real topology *must* contain a dedicated CT — invert or remove)
2. Add the AC-mapped tests from each task's "Tests" section.
3. Run the full suite; then run `python tools/dev/verify.py`.
4. Grep the tree for `/dedicat/i` outside `docs/specs/` and confirm every remaining hit is intentional: historical notes, the spec set itself, and the allowlisted UI alias.

## Tests

This task *is* tests. Its own verification is:

- `pytest -q` green.
- `python tools/dev/verify.py` green.
- `grep -rin dedicated --include='*.py' tests/` returns nothing except comments explaining the removal.

## Acceptance criteria

- AC-8: full verification gate passes.
- No test pins a removed enum value, field or label.
- Every AC-1…AC-11 has at least one test that fails if the behaviour regresses.
- The docs scan added in task-11 must exclude `docs/specs/retire-dedicated-ct-concept/**` (the specification necessarily names the concept) and the allowlisted UI alias line.
