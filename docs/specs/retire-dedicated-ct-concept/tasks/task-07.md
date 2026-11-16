# task-07 — Simplify monitoring derivation

**Wave:** 3 · **Depends on:** task-05 · **Owner:** `disaggregation/configuration.py`

## Objective

Remove `allows_direct_measurement` and reduce `derive_monitoring` to graph-only logic.

## Context

`derive_monitoring` (`configuration.py:158-193`) currently returns `MonitoringLevel.DEDICATED` when `dedicated_channel_for` matches. With the role gone there is exactly one producer of that value and it is already unreachable. `allows_direct_measurement` (`:142-148`) exists solely to gate the deleted Level-1 path.

Stored declarations carrying `"dedicated"` already fall through a caught `ValueError` to `UNKNOWN` (`:345-350`). That path must stay tolerant after the enum member disappears.

## Scope

Simplify the two functions; keep the tolerant stored-value handling.

## Files owned

- `engine/wattmosaic_engine/disaggregation/configuration.py`

## Steps

1. Rewrite `derive_monitoring`:

```python
def derive_monitoring(device, graph, *, topology=None, declared=MonitoringLevel.UNKNOWN):
    if declared != MonitoringLevel.UNKNOWN:
        return declared
    if device is None or graph is None:
        return MonitoringLevel.UNKNOWN
    if not set(graph.attribution_ct_ids(device.device_id)):
        return MonitoringLevel.HOME_TOTAL_ONLY
    return MonitoringLevel.SHARED
```

2. Delete `allows_direct_measurement` (`:142-148`) and update its call sites (already handled in task-06 for `engine.py`).
3. Delete `sole_occupant`'s early `dedicated_channel_for` return (`:208`) if it is now redundant; keep the function, since it still explains why a device labelled `shared` has no siblings.
4. Keep the `try/except ValueError` around `MonitoringLevel(str(metadata.get(MONITORING_KEY, ...)))` at `:345-350`.
5. Delete the module docstring sections that describe the dedicated gate.

## Tests

- `test_stored_dedicated_declaration_resolves_from_graph`: a device whose metadata says `monitoring_level = "dedicated"` derives `SHARED` (rows present) or `HOME_TOTAL_ONLY` (none); never raises.
- `test_sole_occupant_branch_is_shared`: preserve the existing test at `tests/unit/test_energy_quality.py:378` semantics.
- Update `tests/unit/test_device_taxonomy.py:241-269` and `:770` which assert `monitoring == "dedicated"`.

## Acceptance criteria

- No code path returns `MonitoringLevel.DEDICATED`.
- Stored legacy declarations load without error.
- Unit suite green.
