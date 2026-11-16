# Architecture — Retire the "dedicated device" CT concept

## 1. Component map

```
HA integration (custom_components/wattmosaic)
  config_flow.py / flow_logic.py / const.py / strings.json
        │  declares roles + monitoring via config entry
        ▼
Engine API (engine/wattmosaic_engine/api/server.py, main.py)
        │  validates Topology, serves /topology, /devices, /topology/tree
        ▼
Contracts (contracts.py) ──► Schemas (data/schemas/*.json, generated)
        │
        ├── Topology model (electrical/topology.py)
        │     dedicated_channel_for()  ◄── DELETED
        │     attribution_ct_ids()
        │
        ├── Direct measurement (electrical/direct.py) ◄── DELETED
        │     resolve_direct() → DirectReading
        │
        ├── Configuration (disaggregation/configuration.py)
        │     derive_monitoring() / allows_direct_measurement ◄── SIMPLIFIED
        │
        ├── Solver (disaggregation/engine.py)
        │     Level-1 branch (:1510-1588) ◄── DELETED
        │     _is_solo_row() — survives, now the only whole-row path
        │
        └── Storage (storage/database.py, storage/migrations.py)
              v11 migration ◄── NEW
```

## 2. What is deleted

| Component | Location | Replacement |
| --- | --- | --- |
| `CTType.DEDICATED_DEVICE` | `contracts.py:46` | removed |
| `MonitoringLevel.DEDICATED` | `contracts.py:154` | removed; declaration maps to `shared` |
| `DeviceTopology.dedicated_ct_ids` | `contracts.py:400` | removed; ids live in `branch_ct_ids` |
| `TopologyGraph.dedicated_channel_for` | `electrical/topology.py:333-349` | removed; callers fall through to `most_specific_branch_group` |
| `resolve_direct` + `DirectReading` | `electrical/direct.py`, `electrical/__init__.py:120-127` | removed |
| Level-1 direct branch | `disaggregation/engine.py:1510-1588` | removed; `_infer_device` starts at the shared/solo logic |
| `direct_is_ours` gate | `disaggregation/engine.py:1461` | removed |
| `allows_direct_measurement` | `configuration.py:142-148` | removed (no longer meaningful) |
| `_direct_load_series` | `main.py:380-423` | removed; `infer_nesting` called with `direct_loads=None` |
| `diagnostics["direct_measurements"]` | `engine.py:659` | removed |
| `no_dedicated_device_ct` reason | `integration.py:516-524` | reworded to a neutral "no private channel" explanation |
| `Source.DIRECT_MEASURED` *production* | `tiers.py:158`, `confidence.py:184-190` | enum member retained for history; no new results use it |

Everything else keeps its name. In particular `Source.DIRECT_MEASURED` stays in the contract enum so historical rows remain readable (`main.py:449`).

## 3. What survives unchanged

- `Topology.attribution_ct_ids()` (`topology.py:417-426`) — falls through to `most_specific_branch_group` once the dedicated fast-path is gone.
- `_is_solo_row()` (`engine.py:2348-2358`) — the whole-row measurement path for a device alone on its row; role-independent.
- `_row_siblings` / ADR-0026 measured-step rule (`engine.py:1700-1729`) — unchanged.
- `project_configuration` projection and retraction (`configuration.py:674-807`) — unchanged except that the dedicated branch disappears.
- Panel tree rendering (`panel_tree.py`) — **not** role-agnostic. `_device_ct_ids` unions `branch_ct_ids` *and* `dedicated_ct_ids` (`panel_tree.py:659`), so every tree render breaks until that reference is dropped (task-05b). Once fixed it is role-agnostic.
- Quality checks (`quality/engine.py:619-631`) — `"branch"` still matches.
- Step specificity ranking (`engine.py:314-318`) — dedicated entries removed; all load rows tie at specificity 1.

### 3.1 Bare-string comparisons an enum-name grep misses

Several references compare `ct_type` against the literal `"dedicated_device"` without naming `CTType.DEDICATED_DEVICE`, so a grep for `DEDICATED_DEVICE` alone is insufficient:

| Location | Expression |
| --- | --- |
| `engine/wattmosaic_engine/main.py:394` | `str(ch.ct_type) != "dedicated_device"` |
| `engine/wattmosaic_engine/disaggregation/engine.py:317` | `str(row.ct_type) == "dedicated_device"` (step-specificity ranking) |

Task-05a must catch both; the AC-1 static gate in task-12/13/14 uses `/dedicat/i` over source, not an enum grep, for exactly this reason.

### 3.2 Function-body references

References inside function bodies do not break import but do break at call time. Known ones: `storage/database.py:236` and `:317` (save/load), `panel_tree.py:659`, `device_links.py:166-169`, `auto_placement.py:210-223`. The build agent must grep for the literal, not rely on an import failure to surface them.

## 4. Data flow after the change

```
User opens HA options flow
  → role dropdown: main / branch / unknown          (no dedicated option)
  → stored ct_assignments migrated by async_migrate_entry if legacy
  → plan_topology_push compares stored role to engine ct_type
  → build_topology_update mirrors onto branch_ct_ids only

Engine startup
  → Database.__init__ runs migrations (v11 demotes rows + moves ids)
  → load_topology validates every channel against the narrowed CTType
  → effective_config derives monitoring via derive_monitoring (declaration first, then the graph; never DEDICATED)

Inference per window
  → resolve_direct removed; solver builds matrix from attribution rows
  → solo row ⇒ whole-row fit wins; shared row ⇒ ADR-0026 step rule
  → result source = inferred / model_estimated / configured_prior

App render
  → /topology returns branch/main/unknown only
  → /devices returns branch_ct_ids; Monitoring row hidden
  → badges read STRINGS['branch'] = 'Branch CT'
```

## 5. Migration sequence (critical ordering)

```
1. v11 SQL migration:  ct_channels.ct_type   dedicated_device → branch
2. v11 apply callback: devices.dedicated_ct_ids_json → branch_ct_ids_json
                       strip projected_ct_ids entries
                       device_configurations.payload_json monitoring → shared
                       inference_results.monitoring_level → shared
3. HA async_migrate_entry: config entry ct_assignments role → branch
4. Enum narrowing:      CTType / MonitoringLevel drop the members
5. Contract bump:       CONTRACT_VERSION 1.5.0 → 2.0.0 (engine + integration mirror)
6. Schema regeneration + docs
```

Steps 1–3 are behaviour-preserving data moves. Step 4 is the breaking change and is only safe once 1–3 exist. Step 5 signals the break.

## 6. Technology choices

- **Keep `Source.DIRECT_MEASURED` in the enum.** Historical DB rows store it; removing the member would make `row_to_inference_result` return an invalid value for existing data.
- **Do not add a replacement boolean** such as `single_device`. The user chose full removal; a silent equivalent would keep the concept under another name and re-open the ADR-0025 debate.
- **Migration in SQL + `apply=` callback.** Existing idiom (`migrations.py:16-27`, `:480-497`) supports a Python callback inside the same transaction; JSON manipulation is easier in Python than SQLite `json_*`.
- **Config-entry migration, not a permanent alias.** Keeping `dedicated_device` in `CT_ROLES` as a hidden alias would leave the vocabulary in the product and contradict AC-1.
