# task-05a — Engine reference cleanup (direct-path modules)

**Wave:** 2 · **Depends on:** task-04 · **Owner:** the ten direct-path modules

## Objective

Repoint every reference to the removed contract members so the engine imports and runs, and **delete `dedicated_channel_for` outright together with all its callers** in one task.

## Context

Once task-04 removes the enum members, literal references become AttributeError at runtime (not import time — they sit inside function bodies). Wave 3 then deletes the direct-measurement subsystem; this task must not leave a half-deleted state where `dedicated_channel_for` is stubbed but still called.

Deleting it here, rather than in wave 3, keeps wave 3 free to remove whole subsystems without touching `topology.py`.

## Scope

Repoint references and delete `dedicated_channel_for`. Do not redesign the solver; wave 3 does that.

## Files owned

- `engine/wattmosaic_engine/electrical/topology.py`
- `engine/wattmosaic_engine/disaggregation/configuration.py`
- `engine/wattmosaic_engine/disaggregation/engine.py`
- `engine/wattmosaic_engine/main.py`
- `engine/wattmosaic_engine/api/facade.py`
- `engine/wattmosaic_engine/integration.py`
- `engine/wattmosaic_engine/quality/engine.py`
- `engine/wattmosaic_engine/electrical/nesting.py`
- `engine/wattmosaic_engine/electrical/direct.py`
- `engine/wattmosaic_engine/electrical/__init__.py`

## Steps

1. `grep -rn "DEDICATED_DEVICE\|dedicated_ct_ids\|MonitoringLevel.DEDICATED\|\"dedicated\"" engine/` and work through every hit. Note that bare string comparisons (`str(ch.ct_type) != "dedicated_device"` at `main.py:394`, `"dedicated_device"` at `disaggregation/engine.py:317`) do **not** match an enum-name grep.
2. `topology.py`: delete `dedicated_channel_for` (`:333-349`). Update its callers:
   - `attribution_ct_ids` (`:417-426`): drop the fast path, return `most_specific_branch_group(device_id)` directly;
   - `resolve` (`:428-466`): remove the `Discipline.dedicated_ct_id` population at `:442` and `:466` and the field if nothing else sets it.
3. `configuration.py`:
   - `derive_monitoring` (`:158-193`): return `SHARED` for attributable devices; never a dedicated value;
   - `allows_direct_measurement` (`:142-148`): remove the `MonitoringLevel.DEDICATED` member from the tuple (task-07 deletes the property);
   - `is_sole_occupant` (`:208`): drop the `dedicated_channel_for` early return;
   - `cts_claimed_by_dedicated` (`:549-581`): the second detection path (`:579`) compares `channel.ct_type == CTType.DEDICATED_DEVICE`; keep the `device.dedicated_ct_ids` path by reading `branch_ct_ids` instead, preserving the double-count guard.
4. `engine.py`: remove the `"dedicated_device"` step-specificity entry (`:314-318`); drop the `resolve_direct` call site scaffolding only where it does not conflict with task-06 (prefer leaving the call for task-06 if both would touch the same lines).
5. `main.py`: `_devices()` stops emitting `dedicated_ct_ids` (final key set is task-09); `_direct_load_series` returns `{}` (deletion is task-08).
6. `api/facade.py:270`: remove the hard-coded `"dedicated_ct_ids": []`.
7. `quality/engine.py:619-631`: drop `"dedicated_device"` from the tuple, leaving `("branch",)`.
8. `integration.py`: stop emitting `no_dedicated_device_ct` (rewording is task-08); remove the structural guard's `dedicated_channel_for` use at `:545-549` and the availability flag at `:564`.
9. `nesting.py`: verify `direct_loads=None` is handled (default at `:579`).
10. Run `ruff check engine` and `pytest -q tests/unit`; fix fallout.

## Tests (implemented by task-14)

- `test_no_engine_reference_to_dedicated_enum` — source scan asserting no `CTType.DEDICATED_DEVICE`, `MonitoringLevel.DEDICATED`, or bare `"dedicated_device"` literal remains outside comments.
- `test_dedicated_channel_for_is_gone` — assert the attribute does not exist on `TopologyGraph`.
- Unit suite green.

## Acceptance criteria

- `import wattmosaic_engine` succeeds.
- No `dedicated_channel_for` symbol exists and nothing calls it.
- Unit suite green.
