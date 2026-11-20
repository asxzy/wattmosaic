# HA history pull, ground-truth replay and topology scoring

Status: approved for implementation. Two independent deliverables, A and B. They
share only the on-disk history format (section 1). Each implementer touches
**only the files listed for its deliverable**.

Goal: pull real recorder history from the live Home Assistant, replay it
through the **real** v3 runtime with an inferred topology (no declared CONTAINS
edges) and score the inferred topology against a machine-readable ground truth.

Hard rules (from CLAUDE.md):

- Read-only toward HA. Only websocket commands already allowed by
  `tools/audit/ha_client.py` (`READ_ONLY_WS_COMMANDS`) and REST GETs. Never
  print the token.
- No live-install data in tracked files. All pulled data, ground-truth JSON and
  reports live under `data/raw/` (git-ignored). Tests use synthetic data only.
- Tools live in `tools/dev/`. No change under `engine/` in this spec.
- Absent is `None`, never 0: non-numeric states (`unavailable`, `unknown`, ``)
  are dropped, never written as 0.
- `ruff check tools tests` must pass. Python 3.11 compatible.

## 1. On-disk history format (shared contract)

Directory (default `data/raw/ha_history/`):

```
manifest.json
samples-YYYY-MM-DD.jsonl.gz     # numeric sensor rows of that UTC day
events-YYYY-MM-DD.jsonl.gz      # state-entity rows of that UTC day
factors.json                    # derived measurement factors (section 2.3)
```

`samples-*.jsonl.gz`: one JSON object per line,
`{"entity_id": "sensor.x_power", "t": "2026-11-10T10:12:03.123000+00:00", "v": 123.4}`.
Sorted by `t` within the file. `t` is the recorder `last_updated` (`lu`) in UTC
ISO-8601. `v` is a float.

`events-*.jsonl.gz`: `{"entity_id": "climate.x", "t": ISO, "state": "heat", "attributes": {...}}`.
Attributes are kept (climate `hvac_action` lives there). Sorted by `t`.

`manifest.json`:

```json
{"schema": "wattmosaic.ha_history/1",
 "start": ISO, "end": ISO,
 "days": {"2026-11-10": {"complete": true, "samples": 168000, "events": 900}},
 "sample_entities": [...], "event_entities": [...],
 "earliest_row": ISO, "pulled_at": ISO}
```

`factors.json`:

```json
{"schema": "wattmosaic.measurement_factors/1",
 "method": "P/(V*I*PF) snapped",
 "factors": {"sensor.x_power": [{"from": ISO, "to": ISO|null, "factor": 2.0, "samples": 412}]},
 "unknown": ["sensor.y_power"]}
```

## 2. Deliverable A: `tools/dev/ha_history_pull.py`

Files: `tools/dev/ha_history_pull.py`, `tests/unit/test_ha_history_pull.py`.

### 2.1 CLI

```
.venv/bin/python tools/dev/ha_history_pull.py \
    --readers data/raw/ground_truth/topology_gt.json \
    [--days 10 | --start ISO --end ISO] \
    [--events auto | --events-file FILE | --no-events] \
    [--out data/raw/ha_history] [--refresh] [--derive-factors-only]
```

- `--readers`: a JSON file with a `readers` list. Each item has
  `power_entity` and optional `factor_inputs` (voltage, current, power-factor
  entity ids). All power entities and all factor inputs are pulled as samples.
- `--days` defaults to 10 (HA's default recorder retention). The window ends now.
- `--events auto`: pull state entities that look like load drivers for the
  installation. Use `engine/wattmosaic_engine/ingestion/ha_source.py::is_event_domain`
  over `GET /api/states`. Keep only the domains `switch`, `climate`, `humidifier`,
  `fan`, `water_heater`, `binary_sensor`, `light`, `input_boolean` and
  `media_player`. Exclude `sensor.wattmosaic_*` and the `wattmosaic` domain.
  Cap the list at 400 entities, ordered by change count in the last 24 h
  (descending). `--events-file` is a newline-separated list of entity ids.
- Refuse an `--out` outside `<repo>/data/raw/` unless `--force-out` is given,
  so pulled data cannot land in a tracked directory.

### 2.2 Fetching

- Use `tools/audit/ha_client.py` (`load_env`, `connection_settings`,
  `HAClient`). Credentials come from `.env` at the repo root, falling back to the
  main checkout. Use `git rev-parse --git-common-dir` to find its parent when run
  from a linked worktree.
- Use websocket `history/history_during_period`. For samples, set
  `minimal_response=True`, `no_attributes=True` and
  `significant_changes_only=False`. For events, set `minimal_response=False` and
  `no_attributes=False`. Add a helper for the event variant; do not change
  `fetch_history`'s defaults.
- Chunk by UTC day. Pass `include_start_time_state=True` only for the first day,
  and pull entities in batches of at most 50 per request.
- Resume: a day already in the manifest with `complete: true` is skipped unless
  `--refresh` is given. The current (partial) day is always re-fetched and
  marked `complete: false`.
- Record `earliest_row` as the earliest row seen in all pulled data.
- Print progress per day (rows, seconds), never values or tokens.

### 2.3 Factor derivation (pure function, unit tested)

`derive_factors(rows_by_entity, readers) -> dict` produces the `factors.json`
payload. For each reader that has all three factor inputs:

1. Align P, V, I and PF by holding the last value. Use only instants where all
   four exist and are at most 30 s old.
2. Keep instants with `|I| >= 0.5`, `|PF| >= 0.3` and `|P| >= 50`.
3. Compute `f = |P| / (V * |I| * |PF|)`. Snap to the nearest of `{1.0, 2.0}`
   when within 15 %, otherwise discard the instant.
4. Take the mode of snapped factors per 1 h bucket. Buckets with fewer than 5
   qualifying instants have no value.
5. Merge consecutive buckets with the same factor into intervals. A change
   needs two consecutive buckets with the new value (hysteresis). Bucket gaps
   without a value do not split an interval.
6. A reader with no qualifying bucket goes to `unknown`. Never guess its factor.

`--derive-factors-only` recomputes `factors.json` from the existing files without
touching HA. A normal pull always ends by deriving factors.

### 2.4 Tests (`tests/unit/test_ha_history_pull.py`, synthetic data only)

- Non-numeric rows are dropped, and numeric strings become floats.
- Day chunking and batching produce the expected request windows. Use a fake
  client that records calls, with no network.
- `include_start_time_state` is True only on the first chunk.
- Resume skips complete days and re-fetches the partial day.
- `derive_factors`:
  - ×1 and ×2 readers are detected;
  - a ×1→×2 change mid-series produces two intervals at the right hour;
  - one stray bucket does not produce a change;
  - low current or low PF instants are ignored;
  - a reader with no qualifying data lands in `unknown`.
- `--out` outside `data/raw` is refused.

## 3. Deliverable B: `tools/dev/replay_ground_truth.py` + `tools/dev/topology_score.py`

Files:

- `tools/dev/replay_ground_truth.py`
- `tools/dev/topology_score.py`
- `tests/unit/test_topology_score.py`
- `tests/integration/test_replay_ground_truth_smoke.py`
- Optional: a small refactor of `tools/dev/replay_pipeline.py` that moves shared
  helpers into importable functions. It must not change that tool's behaviour,
  and its existing tests must still pass.

### 3.1 Ground-truth file (input; already exists for the live install)

```json
{"schema": "wattmosaic.topology_ground_truth/1",
 "readers": [{"name": "...", "reader_id": "...", "power_entity": "sensor....", "factor_inputs": [...]}],
 "contains": [{"parent": reader_id, "child": reader_id}],
 "paired_leg": [[reader_id, reader_id]],
 "same_conductor": [{"alias": reader_id, "canonical": reader_id}],
 "one_leg_240v": [{"reader": reader_id, "unmonitored_leg_parent": reader_id}],
 "legs": {"A": [reader_id...], "B": [reader_id...]}}
```

`contains` lists **direct** parent→child only. The ground truth was observed by
hand and may be wrong, so the scorer reports disagreements; it never "fixes"
the engine output.

### 3.2 Replay runner CLI

```
.venv/bin/python tools/dev/replay_ground_truth.py \
    --history data/raw/ha_history --gt data/raw/ground_truth/topology_gt.json \
    [--since ISO --until ISO] [--declare none|pairs|pairs+aliases] \
    [--factors auto|none] [--checkpoint-hours 12] \
    [--json-out data/raw/replay/report.json] [--db-out PATH] [--max-records N]
```

Behaviour:

1. Create a fresh temporary SQLite DB (or `--db-out`). Wire a real
   `EngineCore` with an injected clock, exactly as
   `tools/dev/replay_pipeline.py::replay` does (`EngineCore(EngineConfig(...),
   Database(path), clock=...)`, `core._wire_v3_services()`, `EngineHarness`).
   Reuse its code by importing it; do not duplicate it.
2. Seed one CT endpoint per GT reader via `db.repos.entities.upsert_endpoint(...)`
   (`kind="ct"`, `endpoint_id=reader_id`, `entity_id=power_entity`,
   `scope="circuit"`, `scope_status="candidate"`, `enabled=True`) and one
   endpoint config revision with `multiplier=1.0`. Look at how
   `replay_pipeline._prepare_work_db` and `tests/support/hb17_seed.py` create
   endpoints and revisions, and follow the same repositories.
3. Declarations (installer knowledge) depend on `--declare`, which defaults to
   `pairs+aliases`. Pairs declare `PAIRED_LEG` for every `paired_leg` entry and
   aliases declare `SAME_CONDUCTOR` alias→canonical. Both are user declarations
   through the public core API (`core.declare_topology_edge(...)` or
   `VersionService.propose_topology/promote_topology` with
   `declared_by="user"`). **Never declare CONTAINS.**
4. Factors (`--factors auto`): if `factors.json` exists, store each interval in
   the engine's factor store (`db.repos.factors.insert_measurement_factor`
   with a `MeasurementFactor`; read its contract for the fields, and use
   applied-by monitor / basis `MONITOR_REPORTED` where the contract has such
   fields). Keep the endpoint multiplier at 1.0. Readers in `unknown` get no
   factor row. Record in the report what was stored.
5. Stream samples of GT reader power entities, in time order across days, into
   `core.on_reading` via the harness consumer, exactly like replay_pipeline
   does. Events go to `on_ha_event`. Factor-input entities (V/I/PF) are **not**
   fed as readings. Drive pump, heartbeat and settle the same way.
6. Every `--checkpoint-hours` of replayed time, and at the end, read the
   inferred topology (`core.topology_inference.edges`: each `TopologyEdgeV3` has
   `source_endpoint_id`, `target_endpoint_id`, `relation`, `lifecycle`, `evidence`
   and `declared_by`; if the attribute differs, find the accessor used by
   `core/topology_api.py`). Score it with `topology_score.score(...)`.
7. Write the JSON report: run metadata, record counts, factors stored, the
   checkpoint score series and the final full score with all edges. Print a
   human-readable summary using GT reader **names**. The report contains no raw
   samples.
8. Print progress every 100k records (rate, replayed time). Long runs must be
   resumable only by re-running; no daemon.

### 3.3 Scorer (`tools/dev/topology_score.py`, pure, no engine imports)

`score(edges: list[dict], gt: dict, *, structural=("confirmed", "structural"), soft=("provisional",)) -> dict`

Each edge dict has `source`, `target`, `relation` (lower-case value), `lifecycle`
(lower-case value), and optionally `support`, `conflict`, `parent_only` and
`declared_by`. The runner converts `TopologyEdgeV3` to this shape. The
lifecycle names are parameters because the engine's lifecycle vocabulary is
about to change; never hard-code them anywhere else.

Output keys:

- `contains_direct`: computed for two levels, `structural` (edges in
  `structural`) and `structural_or_soft`. Each level has `tp`, `fp` and `fn`
  lists, `precision`, `recall` and `f1`. Inferred CONTAINS edges at a level are
  transitively reduced before comparison: if A⊃B, B⊃C and A⊃C are all present,
  A⊃C is not counted as a direct FP.
- `contains_closure`: the same metrics on transitive closures. This credits a
  child placed under the right ancestor.
- `per_child`: for every GT child, the GT parent, the inferred direct parent(s)
  with lifecycle and evidence, and a status:
  - `correct`;
  - `ancestor_only` (placed under an ancestor of the true parent, e.g. a
    sub-panel load directly under a main CT);
  - `wrong_parent`;
  - `cross_leg` (the parent is on the other leg);
  - `missing`.
- `violations` (must-not list):
  - `cross_leg_contains`;
  - `sub_child_direct_under_main` (GT grandchild placed as a direct child of
    its GT grandparent);
  - `alias_not_same_conductor` (a GT alias pair without SAME_CONDUCTOR at any
    lifecycle);
  - `alias_with_children` (an alias reader is the parent of any CONTAINS edge
    at structural level);
  - `pair_missing`;
  - `child_contains_parent` (a reversed GT edge).
- `paired_leg` and `same_conductor`: found/missing per GT entry, with the
  inferred lifecycle.
- `extra_readers`: endpoints in edges that are not in GT.
- `lifecycle_counts`: counts of all edges by relation × lifecycle.
- `unscorable`: GT children whose reader had fewer than `min_events` (default
  3) P3 steps or meaningful power changes in the replay. The runner passes
  `activity_by_reader`, a count of sample-to-sample changes ≥ 30 W per reader.
  Their misses are listed separately and excluded from recall under
  `recall_scorable`.

`format_markdown(result, names) -> str` renders a compact table per parent
plus the violations.

### 3.4 Tests

- `tests/unit/test_topology_score.py` uses a hand-built 2-leg ground truth, 2
  mains, 2 subs and 6 children. It covers:
  - a perfect match scores F1 = 1;
  - an ancestor-only placement is counted as `ancestor_only` and is still a
    closure TP;
  - a cross-leg edge raises a violation;
  - transitive reduction prevents a double FP;
  - lifecycle levels separate structural from soft;
  - unscorable children are excluded from `recall_scorable`;
  - reversed edges are flagged;
  - alias checks work.
- `tests/integration/test_replay_ground_truth_smoke.py` writes a tiny
  synthetic history in the section 1 format to `tmp_path`: 1 main, 2 children,
  step loads over 2 h, a few events and a GT file. It runs the runner's
  `main([...])` with `--json-out` and asserts that the report has the expected
  keys, a non-zero records count and a `final.score` block. It does not assert
  that inference got it right; it is a plumbing test. Keep it under about 20 s.
