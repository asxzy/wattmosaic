# WattMosaic — Home Assistant Integration

The WattMosaic integration is a **thin, read-only client** for the WattMosaic
engine. The engine does all sampling, inference, learning and topology work;
Home Assistant displays the results as sensors, binary sensors and diagnostics,
and forwards explicit user commands (learning/model/topology) to the engine.

- Domain: `wattmosaic`
- Target Home Assistant: **2026.11.0**
- IoT class: `local_polling`
- Contract version: **1.3.0** (`engine/wattmosaic_engine/contracts.py`)
- No device control: the integration never calls HA services that actuate
  devices, and the engine has no code path that can.

---

## 1. Installation

### 1.1 Requirements

| Component | Requirement |
| --- | --- |
| Home Assistant | 2026.11.0 (older 2024.x+ releases should work) |
| WattMosaic engine | reachable over HTTP from the HA host, API v1 |
| Network | HA → engine on the engine port (default `8787`) |
| Token | same `WM_API_TOKEN` as the engine, if the engine has one configured |

The integration has **no Python requirements**: Home Assistant already provides
`aiohttp`, so `manifest.json` declares `"requirements": []` and the client uses
`homeassistant.helpers.aiohttp_client.async_get_clientsession`.

### 1.2 Copy the component

```bash
# From a checkout of this repository:
cp -r custom_components/wattmosaic <HA_CONFIG>/custom_components/
```

Restart Home Assistant, then add the integration:

**Settings → Devices & services → Add integration → WattMosaic**

### 1.3 Engine URL

Use the origin only; the integration appends `/api/v1` itself:

```
http://192.168.1.10:8787      ✅
http://192.168.1.10:8787/     ✅ (normalised)
http://192.168.1.10:8787/api/v1  ✅ (stripped)
https://engine.example.com    ✅
```

The URL is stored as the config entry unique ID, so one engine = one entry.

### 1.4 Token handling

The token is sent only in the `X-API-Token` header. It is never placed in a URL,
never logged, never returned by the config flow, and never included in
diagnostics. In the UI a configured token is shown as **configured**; leaving
the field empty during reconfiguration keeps the stored token.

---

## 2. Configuration flow

The wizard runs in four steps (five including the discovery confirmation an App
install arrives with). **Nothing is written to the engine until the final step**,
and uncertain mappings are never auto-committed.

### Step 0 — Discovery (App installs)

When the engine runs as a Home Assistant App, Home Assistant lists a
**Discovered** card and the flow enters through `async_step_hassio` instead of
`async_step_user`. The announced host and port are normalised through the same
rule as the manual field, so both paths produce the same config entry, and the
engine is probed before the confirmation screen appears — a stale announcement
shows `cannot_connect`, never a broken entry. Discovery is plain HTTP on the
Supervisor network, so an announced `scheme` is ignored. Confirming proceeds to
step 2; a dismissed card is equivalent to choosing manual setup at step 1.

### Step 1 — Connect

| Field | Notes |
| --- | --- |
| Engine URL | Required. Validated locally, then probed with `GET /health` + `GET /snapshot`. |
| API token | Optional. Sent as `X-API-Token`. |

Error keys shown in the dialog:

| Error | Meaning |
| --- | --- |
| `cannot_connect` | DNS/TCP/timeout/5xx — engine not reachable. |
| `invalid_auth` | Engine returned 401/403. |
| `contract_mismatch` | Engine's `X-WattMosaic-Contract-Version` major ≠ `1`. |
| `invalid_response` | Unreadable JSON or wrong payload shape. |
| `invalid_url` | Not a valid `http://`/`https://` URL. |
| `already_configured` | This engine URL is already configured. |

### Step 2 — Polling and freshness

| Option | Default | Range | Meaning |
| --- | --- | --- | --- |
| `scan_interval` | 30 s | 5 – 3600 | Poll period for `/snapshot` + `/health`. |
| `stale_after` | 120 s | 10 – 86400 | Results older than this are **not published as current**. |
| `unavailable_after` | 120 s | 10 – 86400 | No successful poll for this long → entry raises `UpdateFailed`. |

Validation: `stale_after ≥ scan_interval` and `unavailable_after ≥ stale_after`
(otherwise the UI would flap every cycle).

### Step 3 — CT channel assignment

One dialog per channel, with:

| Field | Values |
| --- | --- |
| Role | `main`, `branch`, `dedicated_device`, `unknown` |
| Phase | `l1`, `l2`, `both`, `unknown` |
| Polarity | `normal`, `inverted` |
| Parent CT | the CT (panel feed or branch) that physically feeds this one; empty for a top-level feed |
| Circuit multiplier | scale applied to the reported reading, default `1.0` |
| Paired CT | sibling CT for a double-pole load |
| Duplicate of | another CT clamped on the *same* conductors (an alias) |
| Device | device served by a `dedicated_device` CT |
| Enabled | whether the channel participates |

Channels come from `GET /topology`. Where the engine already suggests a
`ct1`/`ct2` sibling, that pair is **pre-selected as a suggestion** (visible in
the dialog), not committed.

**Demoting a `dedicated_device` CT to `branch` keeps the device on its row.**
The two roles describe the same conductor; they differ only in what the user is
willing to assert about it. A `dedicated_device` CT claims "exactly one
appliance sits here", while `branch` says "this circuit may feed several". When
you change the role, the channel stops carrying a `device_id` and the device's
ownership moves from `dedicated_ct_ids` to `branch_ct_ids`, so the fixture is
still measured on that circuit — as a shared row rather than a private reading.
Turning the CT into a `main` feed, or reassigning it to a different device, does
remove it from this device's attribution, because a feed is a containment
constraint rather than a measurement.

**Nesting is supported to any depth.** A `main` CT may name another `main` CT as
its parent (utility → panel → sub-panel), and a branch may sit under any feed.
Only the outermost feed of each branch of the tree contributes to the whole-home
total, so declaring the real wiring never double-counts. The same applies to a
branch that contains sub-CTs.

**Circuit multiplier.** One CT on one leg of a 240 V load under-reports by 2×.
Set the multiplier to `2` for that circuit. The default is `1.0` and it is never
inferred: most monitors apply the vendor's circuit multiplier themselves and
already publish the full load, so inferring it would double-count. The
multiplier is applied exactly once, on the way out of the reading layer.

**Duplicate feeds.** Two monitors clamped on the same service entrance measure
one point twice. Declaring one as a duplicate of the other makes the engine count
it once; the alias still serves its own device readings.

Parent links are **proposed, not applied**: `GET /topology/nesting` measures
containment (a child cannot exceed its feed) and closure (the parent's own loads
plus the child must not over-explain the parent) over stored history, and returns
candidates. A single unambiguous candidate is pre-selected in the dropdown; the
user confirms it. Two candidates mean the evidence is ambiguous and nothing is
pre-selected.

**Only a panel feed is proposed as a child.** A load's level is not measurable
from power alone: a 9 W circuit is contained by every panel in the house while
idle. The engine therefore reports loads as *not inferable* and the wizard asks
the user for their parent instead of offering a guess. Duplicate feeds are
proposed separately, as *aliases* rather than parents — the same conductor
measured twice is not a nesting — and a declared pair or declared parent
suppresses the alias suggestion rather than competing with it.

Validation rules (enforced on the confirmation step):

- a `main` CT must have a concrete phase (`l1`/`l2`/`both`);
- a `dedicated_device` CT must name a device;
- pairing must be reciprocal and cannot be self-referential;
- phase `both` requires a paired CT;
- every `ct_id` must exist in the engine topology;
- a parent must exist, must be a container (`main`/`branch`), and must not form
  a cycle;
- a duplicate-of target must exist, must not be self, and must not form a cycle;
- a multiplier must be positive and finite.

### Step 4 — Device mapping (candidates)

For each `dedicated_device` CT without a device, the flow shows **ranked
candidates** derived from token overlap between the CT name/ID and
`GET /devices` names. Each candidate label includes the matched words, e.g.
`Panel A Water Heater (dev_panel_a_water_heater) — match: water, heater`.

You can pick a candidate, type a device ID manually, or skip. **No candidate is
ever applied automatically.**

### Step 5 — Confirm

A summary lists every channel plus the list of *uncertain* entries (unknown
role, unknown phase, or a dedicated CT without a device).

- Submitting PUTs the merged topology to the engine (`PUT /topology`).
- Without the box ticked the entry is created and everything stays `candidate`.
  This is the normal outcome on a real installation, where some phases are
  genuinely unknown.
- Ticking **Confirm uncertain assignments** additionally marks the topology
  confirmed and calls `POST /topology/confirm`. Do this only when you have
  actually verified the uncertain rows. Confirming an assignment you are unsure
  of is worse than leaving it candidate, because results then carry no topology
  uncertainty penalty while still being wrong.

The box only ever *adds* confirmation; it is never required to finish the
wizard.

### Options / reconfigure

- **Configure → Options**: polling settings, then the same channel/mapping/
  confirm wizard. Stored assignments are the starting point.
- **Reconfigure** (entry menu): change the engine URL/token only.

Changing options triggers a clean config-entry reload.

---

## 3. Entities

Entities exist in two groups. All per-device entities belong to a device named
after the engine device; all entities hang off a **WattMosaic Engine** device
(via-device), so the device registry stays tidy.

### 3.1 Per device

| Entity | Unit | Device class | State class | Notes |
| --- | --- | --- | --- | --- |
| Power | W | `power` | `measurement` | `InferenceResult.power_w` |
| Energy | kWh | `energy` | `total_increasing` | Engine-owned total; published when the reading is publishable, restored on restart |
| Measurement source | enum | `enum` | — | `direct_measured` / `inferred` / `model_estimated` / `unknown` |
| Confidence | % | — | `measurement` | Engineering score ×100, **not calibrated** |
| Evidence tier | enum | `enum` | — | `tier_0_unknown` … `tier_4_verified` (ADR-0013) |
| Sample count | — | — | `total` | Samples behind the active model |
| Model validity | enum | `enum` | — | `valid` / `frozen` / `invalid` / `not_required` / `unknown` |
| Operating stage | text | — | — | Engine stage name |
| Last inference | timestamp | `timestamp` | — | Result timestamp (UTC) |
| Data quality | enum | `enum` | — | `good` / `degraded` / `stale` / `bad` / `missing` |
| Running | on/off | `running` binary_sensor | — | Power > 1 W and source ≠ `unknown` |

Attributes on the power entity: `explanation`, `provenance`, `model_id`,
`model_version`, `supporting_ct_channels`, `measurement_age_seconds`,
`confidence_is_calibrated` (always `false`), `confidence_tier`, `engine_device_id`.
The evidence-tier entity additionally exposes `tier_reasons`.

### 3.2 Whole home / engine

| Entity | Unit | Device class | State class | Source |
| --- | --- | --- | --- | --- |
| Total incoming power | W | `power` | `measurement` | `Reconciliation.main_power_w` |
| L1 power | W | `power` | `measurement` | `Reconciliation.l1_power_w` |
| L2 power | W | `power` | `measurement` | `Reconciliation.l2_power_w` |
| Directly measured power | W | `power` | `measurement` | `direct_measured_w` |
| Inferred power | W | `power` | `measurement` | `inferred_w` |
| Model power | W | `power` | `measurement` | `model_estimated_w` |
| Unknown / unattributed power | W | `power` | `measurement` | `unknown_residual_w` |
| Power reconciliation error | W | `power` | `measurement` | `reconciliation_error_w` |
| Accounted fraction | % | — | `measurement` | `accounted_fraction` ×100 |
| Data quality score | % | — | `measurement` | `DataQualityReport.overall_score` ×100 |
| Engine status | enum | `enum` | — | `ok` / `degraded` / `error` / `offline` (never `unavailable`: reserved by HA) |
| Last snapshot | timestamp | `timestamp` | — | `SnapshotResponse.timestamp` |
| Engine reachable | on/off | `connectivity` binary_sensor | — | last poll success |
| Active learning | on/off | `running` binary_sensor | — | `HealthResponse.learning_active` |

### 3.3 Availability semantics

A value is published **only** when all of the following hold:

1. the engine is reachable (`last_success` within `unavailable_after`);
2. the device has a result in the latest snapshot;
3. `quality_status` is not `stale`, `missing` or `bad`;
4. the result timestamp is within `stale_after`.

Otherwise the entity becomes **`unavailable`** — stale values are never
published as current measurements. Two exceptions are deliberate:

- `Energy` stays readable while the engine is reachable (a cumulative total is
  not a current measurement). Its value only advances on publishable readings;
  an untrustworthy sample republishes the previous total unchanged.
- `Engine status`, `Last snapshot`, `Engine reachable`, `Last inference`,
  `Data quality` and `Model validity` stay readable so a user can see *why*
  everything else is unavailable.

### 3.4 Energy restoration

`Energy` uses `RestoreEntity`: the last published total is read on
startup so a Home Assistant restart never resets the meter.

The engine owns the lifetime total, and the integration publishes it
**unmodified** when the reading is publishable (see `util.published_energy()`).
Two consequences are deliberate:

* **A decrease is forwarded.** If the engine's counter restarts (database
  reset, restore from an older backup), the sensor reports the new value. Home
  Assistant treats a drop on `total_increasing` as a meter reset and continues
  the statistics sum from there, so the meter resumes instead of freezing.
  Holding the old value would pin the sensor forever: the engine's new, lower
  total could never catch up, and no future energy would ever be recorded.
* **An untrustworthy reading is held.** A stale, missing, or bad-quality
  reading never reaches Home Assistant's statistics engine; the previous total
  is republished unchanged.

Because a decrease is forwarded, a *transient* dip that recovers is counted by
Home Assistant as a reset followed by new energy. The quality gate is what
prevents that; a dip the engine marks trustworthy will be double-counted. This
is Home Assistant's own `total_increasing` contract, not a WattMosaic choice.

Energy is never integrated locally: `util.integrate_energy()` exists but is
unused by the entities.

### 3.5 Energy entity opt-in and device binding

An energy entity is created for every engine device by default. In the options
flow (**Configure one device → Create an energy (kWh) entity**) each device can
opt out; the choice is stored per config entry under `energy_entities` and is
**not** sent to the engine, which has no concept of a Home Assistant entity.

Disabling the option stops the entity being created on the next reload. Home
Assistant does not delete registry entries that an integration stops providing,
so the existing entity remains in the registry and becomes unavailable; remove
it in the entity registry if you want it gone. Its long-term statistics are
retained by Home Assistant either way.

When a device declares runtime entities (`state_entity_ids` /
`load_entity_ids`) and one of them belongs to a real Home Assistant device, the
energy entity is attached to **that** device instead of the synthetic
"WattMosaic Inferred load" device. Power, source and diagnostic entities always
stay on the synthetic device, so inference provenance is never mistaken for
hardware telemetry. Entities this integration owns are never binding targets:
that would make an inferred load the host of its own sensor.

The attach happens **after** registration, in `async_added_to_hass`, through
`entity_registry.async_update_entity(entity_id, device_id=…)`. That ordering is
deliberate (ADR-0021): Home Assistant derives a `has_entity_name` entity's
`entity_id` from the device it is created on, so binding before registration
would make the ID depend on whether a declaration happens to resolve. A device
declared later would then change its ID, and the recorder migrates statistics
only on an explicit rename — the already-recorded energy would be orphaned under
the old ID. Creating on the synthetic device and moving afterwards keeps one ID
and one statistics series for the life of the installation. A move carries no
`entity_id`/`new_entity_id`, and is skipped entirely when the entity already
sits on the target, so a reload does not churn the registry.

When nothing resolves, the energy entity keeps its synthetic device, so it
still exists and remains attributable.

---

## 4. Services

All services go to `POST /api/v1/commands` with a `ServiceCommand` body. They
are read-only with respect to physical devices.

| Service | `device_id` | `sample_id` | `confirm` | Purpose |
| --- | --- | --- | --- | --- |
| `wattmosaic.start_learning` | — | — | — | Enable learning |
| `wattmosaic.pause_learning` | — | — | — | Pause learning |
| `wattmosaic.resume_learning` | — | — | — | Resume learning |
| `wattmosaic.accept_sample` | required | required | — | Accept one sample |
| `wattmosaic.reject_sample` | required | required | **required** | Reject one sample (destructive) |
| `wattmosaic.rebuild_model` | required | — | — | Retrain from stored samples |
| `wattmosaic.freeze_model` | required | — | — | Freeze the active model |
| `wattmosaic.unfreeze_model` | required | — | — | Allow replacement again |
| `wattmosaic.reset_device_model` | required | — | **required** | Delete all models (destructive) |
| `wattmosaic.rebuild_topology` | optional | — | **required** | Recompute candidates (destructive) |
| `wattmosaic.run_diagnostics` | — | — | — | Returns the diagnostics report |
| `wattmosaic.export_device_data` | required | — | — | Returns the device export |

### 4.1 Confirmation

`reset_device_model`, `rebuild_topology` and `reject_sample` raise a translated
error unless `confirm: true` is passed explicitly. The check is `is True`, so
`"yes"`, `1` and `false` are all rejected. `confirm` is **not** forwarded to
the engine — it is a UI safety gate on an already-authenticated request.

```yaml
service: wattmosaic.reset_device_model
data:
  device_id: panel_b_network_gear
  confirm: true
```

### 4.2 Responses

`run_diagnostics` and `export_device_data` are registered with
`SupportsResponse.OPTIONAL`; the engine's payload is returned as the service
response.

```yaml
service: wattmosaic.run_diagnostics
response_variable: wm_diagnostics
```

### 4.3 `device_id`

`device_id` is the **engine** device identifier (see the `engine_device_id`
attribute on any per-device entity, or diagnostics). With multiple engines
configured it is resolved through the HA device registry.

---

## 5. Diagnostics

**Settings → Devices & services → WattMosaic → ⋮ → Download diagnostics**
returns:

| Section | Contents |
| --- | --- |
| `integration` | domain, version, entry id/state, URL, token *configured flag*, intervals |
| `config` | full config with token fields replaced by `**REDACTED**` |
| `coordinator` | availability, last success, snapshot age/staleness, device count, last error |
| `engine` | `HealthResponse` (whitelisted keys), reconciliation, quality score + issues |
| `topology` | version, status, channel/device counts, normalised channels, stored assignments, and `device_link_state_entity_ids` (the ids the engine actually carries) |
| `state_links` | per engine device, the state entity ids its `device_links` entry resolved to |
| `devices_without_state_entity` | every link that resolved to **nothing**, with the reason (`only_measurement_entities`, `device_not_in_registry`, `not_a_state_domain`, `no_state_entities`, `engine_device_not_in_topology`) |
| `models` | active model status per device (versions truncated to 10) |
| `last_errors` | accumulated error strings (secrets scrubbed) |

No token, password or secret appears anywhere in the output.

---

## 6. Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `cannot_connect` on setup | wrong URL/port, engine down, firewall | `curl http://<engine>:8787/api/v1/health` from the HA host |
| `invalid_auth` | token mismatch | compare with the engine's `WM_API_TOKEN`; reconfigure |
| `contract_mismatch` | engine newer/older than integration | update the integration or the engine so both are contract `1.x` |
| `invalid_response` | proxy returning HTML, engine partially started | check the engine log; verify the URL has no `/docs` or `/api` suffix |
| All entities unavailable | engine unreachable longer than `unavailable_after` | check **Engine reachable** and its `last_error` attribute |
| Power entities unknown but energy fine | result older than `stale_after` | raise `stale_after` or reduce engine latency |
| Total power unknown but devices fine | `Reconciliation` missing or `quality_status` stale | run `wattmosaic.run_diagnostics` |
| Energy resets after restart | HA recorder/restore disabled | ensure restore state is enabled; check the entity's restored value |
| New device never appears | device not in `/snapshot` results | it has no inference yet; run diagnostics, confirm topology |
| `samples_seen: 0` **and** `samples_rejected: 0` | no state entity reached the engine: the offer stage is empty, not the filter | check diagnostics `state_links` / `devices_without_state_entity`; a link must resolve to a `binary_sensor`/`switch`/`climate`/… entity, not to a power or energy sensor |
| `samples_seen: 0`, `samples_rejected: 0`, topology `candidate` | normal for a candidate topology **only if** a state change happened; otherwise see the row above | confirm the topology once the rows are verified |
| Options changes ignored | reload did not complete | check logs; options change triggers entry reload |
| `confirmation_required` error | destructive service without `confirm: true` | add `confirm: true` |

### Useful checks

```bash
# From the Home Assistant host
curl -sH "X-API-Token: $TOKEN" http://ENGINE:8787/api/v1/health | jq
curl -sH "X-API-Token: $TOKEN" http://ENGINE:8787/api/v1/snapshot | jq '.reconciliation'
```

Enable debug logging:

```yaml
logger:
  default: info
  logs:
    custom_components.wattmosaic: debug
```

---

## 7. Development notes

### 7.1 Module layout

Pure-Python modules (**no Home Assistant import**, unit-tested standalone):

| Module | Responsibility |
| --- | --- |
| `const.py` | Domain, keys, defaults, enums, service names |
| `util.py` | URL validation, JSON coercion, staleness, energy publication gate, redaction |
| `api_client.py` | Async HTTP client with retries + typed errors |
| `flow_logic.py` | Channel parsing, assignment validation, candidate ranking |
| `service_logic.py` | Service validation incl. destructive-confirmation gate |
| `entity_values.py` | Snapshot → entity value/availability mapping |
| `entity_defs.py` | Entity metadata tables (units, classes, options) |

HA-facing modules (thin wiring):

`__init__.py`, `coordinator.py`, `config_flow.py`, `sensor.py`,
`binary_sensor.py`, `diagnostics.py`.

### 7.2 Tests without Home Assistant

`homeassistant` is not installed in this repository's dev environment. The test
suite therefore imports the pure modules directly through a synthetic package
handle and never executes `custom_components/wattmosaic/__init__.py`:

```bash
.venv/bin/python -m pytest tests/integration/test_ha_integration.py -q
```

The suite covers: URL/secret handling, every API endpoint and error path
(against a stub session, no network), snapshot coercion, entity-value mapping,
staleness → unavailable, energy publication (reset forwarding and the
quality gate), energy opt-in and device binding, config-flow validation and
topology merging, candidate ranking, and the destructive-service confirmation
requirement.

### 7.3 Contract alignment

The integration consumes contract `1.3.0` and sends
`X-WattMosaic-Contract-Version` on every request. It does **not** define a
competing schema; response parsing is defensive and degrades to `unknown`
instead of raising.

Two values are *presentation* derivations, not contract fields:

- **Model validity** is derived from `source` + `model_id` + `model_version`
  (`direct_measured` → `not_required`; inferred/model-estimated with no model →
  `invalid`).
- **Running** is `power_w > 1 W` and `source != unknown`.

### 7.4 Known contract gaps

See the report in the project status; the integration works around them without
changing `contracts.py`:

1. `InferenceResult` has no explicit model-validity or running flag, hence the
   derivations above.
2. `HealthResponse.learning_active` is a bool; there is no paused/running enum.
3. `ServiceResult` carries `data` as an untyped dict, so `export_device_data`
   and `run_diagnostics` responses are passed through unvalidated.
4. `/devices` has no declared response model (`Device list`), so both a plain
   list and `{"devices": [...]}` are accepted.
5. `Topology.status` is the only confirmation signal; there is no per-device
   confirmation timestamp.
