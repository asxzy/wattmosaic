# Topology drag-and-drop correction + devices under circuits

Status: ready for implementation. Cites DEC-10 (a user declaration is
authoritative), ADR-0043 (topology is dynamic; only explicit user actions
write), ADR-0045 (placement is display), ADR-0028/ADR-0001 (panel never
computes power), HB §16 (soft edges never become structure by themselves).

## Goals

1. **Show devices under each circuit** in the Topology tab hierarchy.
2. **Let the user drag a circuit onto another circuit (or to the top level)**
   to correct the inferred parent. The correction is a user declaration that
   wins over inference and survives later inference runs.

## Background — why devices are missing today (read this first)

`GET /api/v1/topology/tree` is `EngineCore.panel_tree`
(`engine/wattmosaic_engine/core/snapshot.py:541`). It first calls
`build_display_tree_v3(...)` (`engine/wattmosaic_engine/mapping/display_tree.py`),
which **does** attach device nodes (`kind: "device"`) under each circuit using
`devices_by_reader`. But when the live ledger has a placement report, the code
then **replaces** `tree["roots"]` with `self._placement_roots(graph, report, active)`
(snapshot.py ~line 576). `_placement_roots` builds circuit nodes only, so every
device node is thrown away. That is the bug behind goal 1.

The panel (`addon/ui/view-topology.js`) already renders `kind: "device"` rows
(`hierarchyRow`, the `kind === 'device'` branch) and a per-circuit device count
chip (`circuitChildSummary`). It just never receives device nodes.

## Background — how placement and user edges work today

* `compute_placements(...)` in `engine/wattmosaic_engine/electrical/topology_placement.py`
  picks one parent per child by **evidence score**; it does not give a
  user-declared edge absolute priority.
* `declare_edge`, `pin_edge`, `reject_edge` in `engine/wattmosaic_engine/core/topology_api.py`
  write edges event-sourced (never delete rows; close with `valid_until`),
  record a `ConfigurationEvent` and call `_propose_and_promote`. Reuse their
  patterns exactly.
* The live ledger (`self.topology_inference`) is seeded from the active version
  only at startup, so a new user edge does **not** change `report.by_child`
  until restart. We therefore apply user authority as a **read-time override**
  on top of the placement report (Part A2), computed from the persisted
  `topology_edges` rows. Do NOT try to mutate `self.topology_inference`.

---

## Part A — Engine

### A1. Devices in the placement tree

File: `engine/wattmosaic_engine/mapping/display_tree.py`

* Extract the inner `_device(...)` builder into a module-level public function
  `device_node(subject: Mapping[str, Any], reader_id: str, depth: int) -> dict[str, Any]`
  returning exactly the same dict shape as today (`id`, `kind: "device"`,
  `label`, `device_id`, `depth_hint`, `reader_id`, `latest_power_w: None`,
  `measurement: None`, `mapping_lifecycle`, `declared_by`, `subtitle`,
  `children: []`). Keep `build_display_tree_v3` behaviour byte-identical (it
  calls `device_node` and still appends to its local `placed_devices` list).
  Export it in `__all__` if the module has one.

File: `engine/wattmosaic_engine/core/snapshot.py`

* Change the signature to
  `_placement_roots(self, graph, report, structural_links, devices_by_reader=None)`.
  In `panel_tree`, compute `devices_by_reader = self._mapped_devices_by_reader(versions["mapping_version_id"])`
  once, pass it to both `build_display_tree_v3` and `_placement_roots`.
* Every circuit node built by `_placement_roots` (root `node(...)`, `_child(...)`,
  the circuit nodes inside the "Unplaced measurements" group, and the
  `both_legs` copies) gets its devices appended **after** its sub-circuit
  children: `[device_node(s, reader, depth + 1) for s in devices_by_reader.get(reader, ())]`.
* A reader that appears in `devices_by_reader` but is not an endpoint in
  `graph.endpoints` gets its devices listed under a final group node
  `{"id": "group:Unplaced devices:1", "kind": "group", "label": "Unplaced devices", "depth_hint": 1, "children": [...device nodes...]}`
  (omit the group when empty).
* Make sure `tree["stats"]["devices"]` stays correct (it is computed by
  `build_display_tree_v3`; do not double count).

### A2. User override on placements (pure, no I/O)

File: `engine/wattmosaic_engine/electrical/topology_placement.py`

Add and export (`__all__`) a pure function:

```python
def apply_user_placements(
    report: PlacementReport,
    edges: Sequence[TopologyEdgeV3],
) -> PlacementReport:
```

`edges` is **every** persisted CONTAINS row (all lifecycles, including closed
and rejected history). Return a **new** `PlacementReport` (do not mutate the
input). Rules, applied per child:

1. **User parent wins.** If the child has an in-force CONTAINS edge
   (`valid_until is None`, lifecycle `structural`) with `declared_by == "user"`
   or `pinned is True`, the placement becomes
   `Placement(child=child, parent=<edge.source_endpoint_id>, band="high", score=1.0, reason="user", alternatives=<old alternatives minus that parent>, both_legs=())`.
   If several qualify, pick the one with the latest `valid_from`, then the
   largest `edge_id` (deterministic).
2. **Rejected pair falls back.** Otherwise, if the current placement's parent
   `p` has a REJECTED CONTAINS row for `(p, child)`, pick the first entry in
   `placement.alternatives` whose pair is not rejected (keep its score; band by
   the same thresholds `SCORE_HIGH`/`SCORE_MEDIUM` as `compute_placements`;
   reason `None`). If none remains, the placement becomes
   `Placement(child=child, parent=None, reason="user_root")`.
3. Any child not covered is copied unchanged.

A "move to top level" (A3 with `parent: null`) rejects every in-force parent
edge of the child, so rule 2 finds no usable alternative and yields
`reason="user_root"`. No extra rule and no contract change is needed.

Check the real `Placement` dataclass fields before writing; if `reason` has a
fixed vocabulary, extend it with `"user"` and `"user_root"` and update
`as_dict()` if needed.

Wire it in:

* `EngineCore.panel_tree` (snapshot.py): after reading `report`, do
  `report = apply_user_placements(report, <all CONTAINS rows from self.db.repos.topology_v3.edges() converted with _edge_row -> TopologyEdgeV3>)`.
  Wrap the DB read in `try/except Exception` → use the report unchanged and
  `log.debug(...)` (a broken read is a degraded tree, never a 500).
  Import `_edge_row` from `.topology_api` (it already exists there).
* `with_placements` in `core/topology_api.py`: apply the same override so
  `/topology/v3`'s `placements` agrees with `/topology/tree`. Refactor the
  "load all contains rows" into one helper `_contains_rows(db)` used by both.

In `_placement_roots`:

* A child whose placement has `reason == "user_root"` is drawn as a **top-level
  circuit** (like a root that has children), **not** inside the
  "Unplaced measurements" group.
* A circuit node whose placement `reason == "user"` gets the chip
  `{"text": "set by you", "tone": "info"}` and `"structural": True`.
* Add `"placement_reason": <reason or None>` to every circuit node dict.

### A3. Move endpoint

Route (file `engine/wattmosaic_engine/api/routes/topology_v3.py`, register it
next to the other topology routes):

```
POST /api/v1/topology/circuits/{reader_id}/parent
body: {"parent": "<endpoint_id>"}   -> move under that circuit
body: {"parent": null}              -> move to the top level (no parent)
200 -> {"version_id", "event_id", "child", "parent", "edge_id" (str|None), "closed_edge_ids": [..]}
```

* Body must be a JSON object containing the key `parent` whose value is a
  non-empty string or `null`, else `InvalidRequest` (400).
* Facade method name: `move_circuit(reader_id: str, parent: str | None)`.
  Implement it on the topology mixin in `core/topology_api.py` next to
  `declare_topology_edge`, delegating to a new method on the v3 topology
  service class (the one that has `declared_edge`/`pin`/`reject`), which calls
  a new module-level function `move_circuit(service, child, parent)` via
  `self._call(...)` exactly like `pin`.
* The route requires `X-API-Token` like the other routes (it is mounted the
  same way; verify the existing auth middleware covers it and add the path to
  any route allow-list/inventory test that enumerates write routes).

`move_circuit(service, child, parent)` semantics (mirror `declare_edge`):

1. Validate: `child` is a known endpoint id (`service._endpoints.endpoints()`),
   else `NotFound`; if `parent` is not None it must be a known endpoint and
   `!= child`, else `NotFound` / `InvalidRequest`.
2. `at = _now(service)`; `rows = service._topo.edges()`.
3. For every row with relation `contains`, `target_endpoint_id == child`,
   `valid_until is None`, lifecycle not in (`rejected`, `retired`) and
   `source_endpoint_id != parent`:
   * `declared_by == "user"` → close it: `valid_until = at` (keep lifecycle).
   * otherwise (inferred) → `lifecycle = REJECTED`, `valid_until = at` (this
     is what `reject_edge` does; it also stops inference from re-promoting
     the pair, see `TopologyGraph` override rule).
   `service._topo.upsert_edge(...)` each and collect `closed_edge_ids`.
4. If `parent` is not None and there is no in-force **user** CONTAINS row
   `parent → child`, create one with the same id-family logic as
   `declare_edge` (`_edge_id` + `-rN` suffix), `lifecycle=STRUCTURAL`,
   `declared_by="user"`, `pinned=True`, `beta=1.0`, `beta_basis="unity"`,
   `valid_from=at`, `evidence=EdgeEvidence()`. If an in-force inferred row
   `parent → child` exists, leave it (the user edge outranks it).
5. Build `proposed = [active edges not in closed_edge_ids] + [new edge]`,
   validate with `TopologyGraph.from_edges(proposed, at=at)`; if
   `not graph.is_consistent()` **and** the problems include a cycle involving
   `child`, raise `InvalidRequest("moving <child> under <parent> would create a cycle")`
   **before any write**. (Do the cycle check first, writes second: compute the
   would-be parent chain of `parent` over the *effective* structure — user
   parents, then structural edges — and refuse if it reaches `child`.)
6. One `ConfigurationEvent(event_id=f"evt-move-{child}-{int(at.timestamp())}", kind="circuit_reconfiguration", detection="declared", status="confirmed", affected_endpoint_ids=[child] + ([parent] if parent else []) + old parents, affected_edge_ids=closed + new, created_at=at)`
   then `_propose_and_promote(service, proposed, event, "user move")`.
7. Moving a circuit to the parent it already has (user edge in force, nothing
   else to close) is a no-op that still returns 200 with `edge_id` of the
   existing edge and `closed_edge_ids: []`, without promoting a version.

Never touch power models or mapping edges (topology, mapping and model
versions stay independent). Devices follow their circuit automatically because
the mapping names the reader, not a position in the tree.

### A4. Engine tests (all must fail before the change, pass after)

* `tests/unit/test_topology_placement_user_override.py` — pure tests of
  `apply_user_placements`: user parent beats a higher-scoring inferred parent;
  latest user edge wins among two; rejected pair falls back to the next
  alternative; no alternative → `user_root`; input report not mutated;
  `both_legs` cleared when a user parent exists.
* `tests/unit/test_placement_roots_devices.py` — call
  `EngineCore._placement_roots` with simple stub objects (a `graph` stub with
  `.endpoints` items having `endpoint_id`/`display_name`, a `report` stub with
  `.by_child` of real `Placement` objects) and `devices_by_reader`; assert
  device nodes (`kind == "device"`, correct `device_id`) appear as children of
  their circuit (placed, unplaced-group and root cases), the "Unplaced
  devices" group appears for an unknown reader, a `user_root` child is
  top-level, and a `reason="user"` child has the "set by you" chip. (If calling
  the method unbound is awkward, move the body into a module-level pure
  function `placement_roots(...)` in snapshot.py and have the method delegate.)
* `tests/integration/test_topology_move_circuit.py` — follow the fixture style
  of `tests/integration/test_engine_core_panel_routes.py` (`core` fixture,
  `_seed_endpoints`, `_promote`, `_with_server`). Cases: move `a` under `b`
  → 200, `/topology/v3` shows the old inferred edge `rejected` and a new
  pinned user edge `b→a`; move again under `c` closes the user edge `b→a`;
  `parent: null` → no in-force CONTAINS row targets `a`; moving `feed` under
  its own descendant → 400 and no new version; unknown reader → 404; missing
  `parent` key → 400.

---

## Part B — Panel (`addon/ui/`)

Constraints (enforced by `tools/dev/check_panel.py` and CSP): vanilla ES
modules, no build step, **no inline `<script>`/`style=` attributes added**, no
external origins, no retired vocabulary. All styling goes in `addon/ui/app.css`
using classes / data attributes. Use the existing helpers from `core.js`
(`el`, `post`, `notify`, `render`, `describeError`, `badge`).

### B1. Devices under circuits

Device rows already render once the engine sends them. Changes:

* `defaultOpenSet` already opens the first panel to depth 2; leave it.
* Circuit rows whose only children are devices must still show the expand
  toggle (they do via `entry.kids.length`); verify with a test.
* Device rows are **never draggable** and are never drop targets.
* Update the hierarchy card foot text to: "Computed by the engine … Each device
  appears under the circuit it is mapped to. Drag a circuit (or use Move…) to
  correct its parent; your choice overrides inference."

### B2. Drag-and-drop + keyboard fallback

Add these **pure, exported** helpers to `view-topology.js` (node-testable):

```js
// Every reader_id below the given reader, across all occurrences (both_legs
// readers appear twice).  Device nodes are ignored.
function descendantReaders(nodes, readerId) -> Set<string>
// Circuits the reader may move under: every circuit reader_id in the tree,
// deduped, excluding itself, its descendants and its current parent.
// Returns [{reader_id, label}] sorted by label.
function validMoveTargets(nodes, readerId) -> Array
// true iff target is null (top level) or in validMoveTargets.
function canDropOn(nodes, readerId, targetReaderId) -> boolean
// POST /topology/circuits/{readerId}/parent with {parent: parentId|null}.
async function moveCircuit(readerId, parentId, http = post)
```

`hierarchyNodes(topology, tree)` is the node list to pass (the same `nodes`
the card already computes once).

Rendering (in `topologyHierarchyCard` / `hierarchyRow`), only when the tree
came from the engine (`tree` non-null) — the legacy fallback stays read-only:

* Every `kind === 'circuit'` row that has a `reader_id` gets
  `draggable="true"` and `data-reader-id`. Use HTML5 DnD:
  * `dragstart`: remember the dragged reader id in a module-level variable,
    `event.dataTransfer.setData('text/plain', readerId)`,
    `effectAllowed = 'move'`, add class `is-dragging` to the row.
  * `dragover` on circuit rows and on the top-level drop zone: if
    `canDropOn(nodes, dragged, target)` then `event.preventDefault()` and add
    class `drop-ok`; else do nothing.
  * `dragleave` / `dragend`: remove `drop-ok` / `is-dragging` everywhere in
    the card body.
  * `drop`: `preventDefault()`, then call `confirmAndMove(dragged, target)`.
* A **top-level drop zone** element above the tree, shown only while a drag is
  in progress (toggle a class on the card body, e.g. `hier.is-dragging`):
  `el('div', { class: 'hier-dropzone', dataset: { target: 'root' } }, 'Drop here to make it a top-level circuit')`.
* A **"Move…" button** (`class: 'btn btn-sm hier-move'`, `aria-label: 'Move <label> to a different parent'`)
  on each draggable circuit row — the keyboard / accessibility path. Clicking
  it replaces the button with an inline form: a `<select>` whose first option
  is "Top level (no parent)" (value `""`) followed by `validMoveTargets(...)`,
  plus "Move" and "Cancel" buttons. "Move" calls `confirmAndMove(reader, value || null)`.
* `confirmAndMove(child, parent)`: `window.confirm(\`Move ${childLabel} under ${parentLabel || 'the top level'}? This overrides what the engine inferred.\`)`;
  on OK → `moveCircuit(...)` then `notify('Circuit moved', 'The topology was updated; inference will not undo this.')`
  and `render()`; on error → `notify('Could not move circuit', describeError(err), 'bad')`.
  Never write without the confirm.
* While a filter query is active, drag-and-drop still works (targets are
  computed from the full `nodes`, not the filtered entries).

CSS in `addon/ui/app.css` (classes only):

```css
.hier .hier-row[draggable="true"] { cursor: grab; }
.hier .hier-row.is-dragging { opacity: .5; }
.hier .hier-row.drop-ok { outline: 2px dashed var(--accent); outline-offset: 2px; }
.hier .hier-dropzone { display: none; border: 2px dashed var(--border); padding: 8px; margin-bottom: 8px; text-align: center; color: var(--text-mute); }
.hier.is-dragging .hier-dropzone { display: block; }
.hier .hier-dropzone.drop-ok { border-color: var(--accent); color: var(--text); }
.hier .hier-move-form { display: inline-flex; gap: 6px; align-items: center; }
```

(Use whatever CSS variables already exist in `app.css`; grep for `--accent`,
`--border`, `--text-mute` and substitute the real names if they differ.)

### B3. Panel tests — add to `tests/unit/test_panel_topology_v3.py`

Use the existing harness (`run_esm`, `lit`, `_render`, `_api`, `EVENT_DOM_SHIM`;
skip without node via `_require_node()`):

* `descendantReaders` / `validMoveTargets` / `canDropOn` on a small tree with
  a nested chain, a `both_legs` duplicate and device leaves: self, descendants
  and the current parent are excluded; devices never appear; `null` is always
  droppable.
* `moveCircuit('ct_oven', 'ct_sub', fakePost)` calls
  `/topology/circuits/ct_oven/parent` with `{parent: 'ct_sub'}`; with `null`
  sends `{parent: null}`.
* Rendering a tree whose circuit has device children shows the device labels
  and the "N devices" chip, circuit rows carry `draggable="true"`, device rows
  do not, and a "Move…" button exists per circuit.
* Rendering performs no POST (only the existing 3.0 GET reads —
  `_assert_only_3_0_reads`). If that helper whitelists paths, do not add the
  POST path to the read whitelist.

---

## Docs

* `docs/specs/realtime-topology-disaggregation/interfaces.md`: document
  `POST /topology/circuits/{reader_id}/parent` next to the other topology
  routes (body, responses, auth, semantics: closes/rejects other parent edges,
  declares a pinned user CONTAINS edge, one version promotion).
* `docs/decisions/README.md`: append **ADR-0046 — Circuit moves are user
  declarations applied over placements** (context: users must correct inferred
  parents; decision: atomic move endpoint + read-time `apply_user_placements`
  override; consequences: the live ledger is not mutated, user authority wins
  in display and in H via the existing TopologyGraph user-override rule).
  Follow the format of the ADR-0045 entry.
* Do **not** edit `RELEASE_NOTES.md`, version fields, `data/schemas/` or any
  contract (no contract change is needed: the new payload is a plain dict).

## Out of scope

* Dragging devices between circuits (that is a mapping change, not topology).
* Mutating the live inference ledger.
* Any change to inference scoring, thresholds or CONTRACT_VERSION.

## Definition of done

All of these pass from the repo root:

```bash
.venv/bin/pytest -q tests/unit tests/integration
.venv/bin/ruff check engine tools tests
python tools/dev/check_panel.py
```
