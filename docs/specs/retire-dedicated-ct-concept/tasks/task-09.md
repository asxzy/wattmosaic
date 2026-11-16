# task-09 — API payload cleanup

**Wave:** 4 · **Depends on:** task-05, task-06, task-07 · **Owner:** `main.py`, `api/facade.py`

## Objective

Remove the last API fields that expose the retired concept.

## Context

`devices()` emits `"dedicated_ct_ids": list(dev.dedicated_ct_ids)` (`main.py:1723`). Once `DeviceTopology.dedicated_ct_ids` is gone this key must disappear from the response. `device()` returns `"topology": dev.model_dump(...)` (`main.py:1779`), which inherits the field automatically. The stub facade hard-codes `"dedicated_ct_ids": []` (`api/facade.py:270`).

The monitoring value `"dedicated"` can no longer be produced after task-07, but the payload builder should not rely on that implicitly.

## Scope

Payload keys only. No behaviour change.

## Files owned

- `engine/wattmosaic_engine/main.py`
- `engine/wattmosaic_engine/api/facade.py`

## Steps

1. Remove `"dedicated_ct_ids"` from the dict built at `main.py:1716-1733`.
2. Verify `device()`'s model dump no longer carries the field (inherited).
3. Remove `"dedicated_ct_ids": []` from `api/facade.py:270`.
4. Confirm `configuration.monitoring` can only be `shared | home_total_only | unmonitored | unknown` via task-07.
5. Update `docs/api.md:230`, which documents the field.

## Tests

- `test_devices_payload_has_no_dedicated_key`: assert the key set exactly.
- `test_stub_facade_matches_contract`: assert the stub payload validates against `data/schemas/device_topology.json`.
- Update `tests/end_to_end/test_api_e2e.py:284` (`assert by_id["water_heater"]["dedicated_ct_ids"]`) and `tests/end_to_end/test_storage_e2e.py:127`.

## Acceptance criteria

- `GET /devices` and `GET /devices/{id}` carry no dedicated field.
- Response schemas validate.
