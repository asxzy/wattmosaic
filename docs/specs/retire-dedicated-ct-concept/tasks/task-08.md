# task-08 — Nesting closure and integration reasons

**Wave:** 3 · **Depends on:** task-06, task-07 · **Owner:** `main.py`, `integration.py`

## Objective

Remove the last engine-internal consumers of the role: `_direct_load_series` (nesting closure) and the `no_dedicated_device_ct` diagnostic reason.

## Context

`_direct_load_series` (`main.py:380-423`) filters channels with a string comparison `str(ch.ct_type) != "dedicated_device"` to build per-container own-load sums for `infer_nesting`. After migration no channel matches, so it silently returns `{}`. Deleting it makes that explicit.

`integration.py:516-524` emits the user-visible reason `no_dedicated_device_ct:attribution_ct_types=...`. That wording names the removed concept and must be reworded neutrally.

## Scope

Delete the helper; pass `direct_loads=None`; reword the reason.

## Files owned

- `engine/wattmosaic_engine/main.py`
- `engine/wattmosaic_engine/integration.py`

## Steps

1. Delete `_direct_load_series` (`main.py:380-423`).
2. At `:1959`, remove the assignment and pass `direct_loads=None` to `infer_nesting`.
3. Confirm `electrical/nesting.py` handles `None` (default at `:579`) and document that closure now relies on the parent's measured series alone.
4. In `integration.py:516-524`, replace the reason with a neutral explanation, e.g.:

```python
missing_direct[device_id] = "no_private_channel"
+ (f":attribution_ct_types={'/'.join(kinds)}" if kinds else ":no_attribution_ct")
```

5. Update the structural guard comment at `:539-549`, which still refers to "every device the graph says has a dedicated CT".
6. Grep for any other `"dedicated"` literal in engine payload-producing code and remove or reword.

## Tests

- `test_infer_nesting_accepts_none_direct_loads`: call the endpoint with a fixture that previously relied on direct loads; assert it returns candidates and reports no error.
- `test_missing_direct_reason_is_neutral`: assert the diagnostic string contains no "dedicated".
- Update `tests/unit/test_nesting.py:35`, `tests/unit/test_nesting_perf.py:311-312`, `tests/unit/test_nesting_wizard.py:83`.

## Acceptance criteria

- AC-1 (engine part): no user-visible engine string names the concept.
- Nesting endpoint behaves sanely without direct loads.
