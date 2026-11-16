# task-06 — Delete direct measurement path

**Wave:** 3 · **Depends on:** task-05a (run before task-07/task-08) · **Owner:** `electrical/direct.py`, `electrical/__init__.py`, `disaggregation/engine.py`

## Objective

Remove Level-1 direct measurement coherently now that the role is gone.

## Context

`resolve_direct` (`electrical/direct.py:78-100`) iterates devices and calls `topology.dedicated_channel_for(device_id)`, which task-05a deleted. The Level-1 branch at `engine.py:1510-1588` is dead weight that claims a capability no longer present.

The block range is `1510-1588`, not `1510-1580`: the `return finish(InferenceResult(...), model_corroborates=…)` expression spans `1556-1588`. Truncating at 1580 breaks the module mid-expression.

A device alone on its row still gets a whole-row reading through `_is_solo_row` (`engine.py:2348-2358`), so inference does not collapse — it grades differently.

## Scope

Delete the machinery; keep the solver's solo-row behaviour intact.

## Files owned

- `engine/wattmosaic_engine/electrical/direct.py`
- `engine/wattmosaic_engine/electrical/__init__.py`
- `engine/wattmosaic_engine/disaggregation/engine.py`

## Steps

1. In `direct.py`, delete `DirectReading` and `resolve_direct`.
2. In `electrical/__init__.py`, remove the re-export: the import block at `:19-21`, the wrapper method at `:120-127`, and `__all__` entry at `:166`.
3. In `engine.py`:
   - remove the import at `:62-64`;
   - remove `direct = resolve_direct(...)` at `:652-658` and `diagnostics["direct_measurements"]` at `:659`;
   - remove the `direct=` argument at `:705-712`;
   - remove the `direct` parameter of `_infer_device` (`:1429`);
   - delete local `direct_is_ours` (`:1461`);
   - delete the Level-1 block `:1510-1588` in full;
   - remove `direct` from `_reconcile`'s signature (`:2078`) and drop `direct_w` (`:2081`);
   - remove `direct` from `_solve_system` and its `y_adj` / `direct_usable` logic (`:1036-1054`).
4. Confirm `_is_solo_row` is untouched and still consulted at `:1161`.
5. Confirm `Source.DIRECT_MEASURED` remains defined in `contracts.py` for historical rows but is produced nowhere new.

## Tests (implemented by task-14)

- `test_no_result_source_is_direct_measured`: inference over a fixture containing a former dedicated row yields no `direct_measured` results.
- `test_solo_row_still_measured`: a single-device branch row yields a positive `inferred` reading.
- `test_shared_row_step_rule_intact`: two devices on one row still use the ADR-0026 step rule.
- `test_resolver_gone`: assert `resolve_direct` and `DirectReading` are absent from their modules, and that `dedicated_channel_for` was already removed by task-05a.

## Acceptance criteria

- Inference never produces `Source.DIRECT_MEASURED`.
- Solo-row devices keep a reading.
- Integration/unit suites green.
