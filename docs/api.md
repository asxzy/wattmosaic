# Engine HTTP API (v1)

Reference for the HTTP surface exposed by `engine/wattmosaic_engine/api/`.
This is the interface the Home Assistant integration and any operator tooling use.

- **Base path:** `/api/v1`
XX (`wattmosaic_engine.CONTRACT_VERSION`)
- **Transport:** HTTP/1.1, JSON (`application/json`) except `/metrics`
  (`text/plain; charset=utf-8`)
- **Bind:** `127.0.0.1:8787` by default (`WM_API_HOST` / `WM_API_PORT`); port `0`
  binds an ephemeral port and the resolved port is readable after `start()`.

The authoritative payload schemas live in `wattmosaic_engine/contracts.py`
(see also `docs/interface_contracts.md` §2.2).

---

## 1. Authentication

Every request is authenticated when a token is configured (`WM_API_TOKEN`).

| Header | Required | Notes |
| --- | --- | --- |
| `X-API-Token` | when `WM_API_TOKEN` is set | Compared with `hmac.compare_digest` (constant time). |
| `X-WattMosaic-Contract-Version` | response only | Always set on responses, including errors. |

If no token is configured the server allows anonymous access (suitable for
loopback-only deployments). A missing or wrong token yields:

```http
HTTP/1.1 401 Unauthorized
X-WattMosaic-Contract-Version: 1.4.0
Content-Type: application/json

{"error": "unauthorized", "detail": "missing or invalid X-API-Token", "retryable": false}
```

---

## 2. Error model

All failures — 4xx and 5xx — return the same shape:

```json
{"error": "bad_request", "detail": "human readable explanation", "retryable": false}
```

| Status | `error` | When |
| --- | --- | --- |
| 400 | `bad_request` | Malformed JSON body, schema validation failure, semantically invalid topology. **HTTP 422 is never returned.** |
| 401 | `unauthorized` | Missing/invalid `X-API-Token`. |
| 404 | `not_found` | Unknown route, or a facade method returned `None` (e.g. unknown `device_id`). |
| 405 | `method_not_allowed` | Wrong HTTP method for a known path. |
| 500 | `internal_error` | Unexpected engine error; the message is not leaked to the client. |
| 503 | `engine_unavailable` | No inference backend / facade not wired yet. |
| 503 | `server_busy` | Bounded-concurrency limit reached; retry shortly. |
| 504 | `timeout` | The request exceeded the engine request timeout. |

Client guidance: treat `retryable: true` as "retry with backoff", everything else
as a permanent failure for that request. Never retry `401` until the token is
fixed.

### Validation errors

Request bodies are parsed and validated with pydantic, then mapped to HTTP 400:

```bash
curl -sS -X POST http://127.0.0.1:8787/api/v1/commands \
  -H 'X-API-Token: secret' -H 'Content-Type: application/json' \
  -d '{"command": "not_a_command"}' -i
```

```http
HTTP/1.1 400 Bad Request
X-WattMosaic-Contract-Version: 1.4.0

{"error": "bad_request", "detail": "[{\"type\":\"literal_error\",\"loc\":[\"command\"],...}]", "retryable": false}
```

---

## 3. Endpoints

### 3.1 `GET /health`

Liveness/readiness without touching the inference path. `status` is one of
`ok`, `degraded`, `error`.

```json
{
  "status": "ok",
  "contract_version": "1.4.0",
  "engine_version": "<version>",
  "database": "ok",
  "ha_connected": true,
  "ha_last_event_at": "2026-11-05T23:40:45.916958+00:00",
  "uptime_seconds": 42.5,
  "queue_depth": 0,
  "inference_last_run_at": "2026-11-05T23:40:43.001122+00:00",
  "learning_active": false,
  "details": {
    "inference_backend": "WattMosaicBackend",
    "inference_error": null,
    "ha_state": "connected",
    "samples_ingested": 443,
    "events_ingested": 7459,
    "topology": {
      "configured": true,
      "status": "candidate",
      "version": 1,
      "channels": 38,
      "devices": 28
    }
  }
}
```

`degraded` means one of: inference backend missing, Home Assistant
disconnected, database unhealthy, the persistence queue is more than half
full, or **no topology configured**. The last one matters: a topology-less
engine answers every probe, ingests every sample and emits no device at all, so
reporting `ok` there would describe a service that is not doing its job. When
`details.topology.configured` is `false`, `details.topology.reason` says why
(e.g. no multi-channel energy monitor was found).
`error` means the database failed the integrity check performed at
startup.

`database` is a cheap liveness verdict, not a fresh scan: this endpoint is
polled every few seconds by the App watchdog, and `PRAGMA integrity_check` reads
the whole file (~1.2 s on a 392 MB database). The one full scan runs at startup
and its result is reported here; `/api/v1/status` and the diagnostics report
expose the full detail on demand.

### 3.2 `GET /status`

Operational summary: engine, database, ingestion and inference counters.

```json
{
  "status": "ok",
  "contract_version": "1.4.0",
  "engine_version": "<version>",
  "started_at": "2026-11-05T23:39:59.100000+00:00",
  "uptime_seconds": 128.4,
  "ha_connected": true,
  "queue_depth": 0,
  "inference": {
    "backend": "WattMosaicBackend",
    "degraded": false,
    "error": null,
    "last_run_at": "2026-11-05T23:42:07.000000+00:00",
    "cycles": 12,
    "backend_status": {
      "backend": "WattMosaicBackend",
      "disaggregation": "DisaggregationEngine",
      "model_registry": true,
      "learning": false
    }
  },
  "learning": {"active": false},
  "database": {"samples": 18234, "events": 90211},
  "quality": {"overall_score": 0.72, "issues": 3},
  "ingestion": {"samples_ingested": 443, "events_ingested": 7459}
}
```

`inference.backend_status` is the backend's own `status()` dict, redacted:
values under sensitive keys (`token`, `api_key`, `password`, …) are replaced by
`"***"`.

### 3.3 `GET /snapshot`

Latest inference results for every device plus the current reconciliation.

```json
{
  "timestamp": "2026-11-05T23:42:10.000000+00:00",
  "reconciliation": {
    "timestamp": "2026-11-05T23:42:07.000000+00:00",
    "topology_version": 1,
    "main_power_w": 12592.956,
    "direct_measured_w": 3962.919,
    "inferred_w": 22.049,
    "model_estimated_w": 0.0,
    "unknown_residual_w": 8607.988,
    "unmonitored_w": 8443.988,
    "meter_disagreement_w": 164.0,
    "reconciliation_error_w": 8607.988,
    "reconciliation_error_ratio": 0.683,
    "accounted_fraction": 0.317,
    "quality_status": "degraded",
    "explanation": ["main_power_sum_of_parallel_mains", "residual_never_redistributed"]
  },
  "results": [
    {
      "device_id": "dev_fridge",
      "power_w": 91.487,
      "confidence": 0.6062,
      "quality_status": "good",
      "direct_measured": false,
      "explanation": ["model_estimated"]
    }
  ],
  "quality": null
}
```

`unknown_residual_w` is always reported and is **never** redistributed across
devices. It is decomposed (contract 1.4.0, additive) into
`meter_disagreement_w` — the difference between a feed and its declared alias
on the same conductors, i.e. instrument error — and `unmonitored_w`, the
remainder that is genuinely unclamped load. `unmonitored_w +
meter_disagreement_w == unknown_residual_w` for a non-negative residual, and
the published total is unchanged by the split. Both default to `0.0`; older
engines omit them and an older reader ignores them.

### 3.4 `GET /devices`

Array of devices from the active topology, each with its latest result and
model state:

```json
[
  {
    "device_id": "dev_fridge",
    "name": "Fridge",
    "phase": "l1",
    "double_pole": false,
    "topology_status": "candidate",
    "dedicated_ct_ids": [],
    "branch_ct_ids": ["br_fridge"],
    "latest_result": {"power_w": 91.487, "confidence": 0.6062, "quality_status": "good"},
    "model": {"model_type": "binary", "version": 2, "status": "active"}
  }
]
```

### 3.5 `GET /devices/{device_id}`

One device, same object shape as an element of `/devices`.

- **200** — the device exists.
- **404** — unknown `device_id`; the facade returns `None`, which the server
  converts centrally to an `ErrorResponse`.

```json
{"error": "not_found", "detail": "device 'nope' not found", "retryable": false}
```

### 3.6 `GET /topology`

The active `Topology`:

```json
{
  "version": 1,
  "created_at": "2026-11-05T23:09:38.111690+00:00",
  "status": "candidate",
  "channels": [
    {
      "ct_id": "panel_a_l1",
      "entity_id": "sensor.panel_a_ct1_power",
      "name": "Panel A L1",
      "ct_type": "main",
      "phase": "l1",
      "parent_ct_id": null,
      "polarity": "normal",
      "enabled": true,
      "expected_interval_s": 15.7,
      "reliability": 0.9,
      "topology_status": "candidate"
    }
  ],
  "devices": [
    {"device_id": "dev_fridge", "name": "Fridge", "dedicated_ct_ids": [], "branch_ct_ids": ["br_fridge"]}
  ],
  "notes": []
}
```

### 3.7 `GET /topology/tree`

The engine-owned panel → circuits → devices hierarchy. The client paints this
verbatim: it never re-derives structure from entity-id prefixes or friendly
names, which is how the previous view produced a branch under a panel that does
not feed it and repeated devices. The tree is computed at read time from
declared topology facts only (`panel_id`, `parent_ct_id`, `alias_of_ct_id`,
device attribution); the endpoint writes nothing and never modifies
`parent_ct_id`.

Query parameter: `include_nesting` (`bool`, default `false`). `1`, `true`,
`yes` and `on` are true; any other value, including an empty one, is false.
The default is false because nesting runs a multi-second CT sample scan.
When true the scan's duplicate candidates can merge panels whose feeds alias
each other — the merged node lists every feed in `feed_ct_ids` and, when more
than one distinct monitor name contributed, carries a `"<n> monitors"` badge —
and a `suggestions` object is added. A suggestion is advisory: a candidate
parent never becomes a tree edge, and a duplicate is an alias, so circuit and
device counts are unchanged. Declared `alias_of_ct_id` merges apply whether or
not nesting is requested; the scan only contributes what the stored topology
does not already declare. A merge can lower `stats.panels` and raise
`stats.duplicate_feeds`; `stats.circuits` and `stats.devices` cannot move.

Every node carries the same seven fields:

| Field | Meaning |
| --- | --- |
| `id` | Stable `"<kind>:<identifier>"` key, e.g. `circuit:br_fridge`. |
| `kind` | `panel`, `circuit`, `device` or `group`. |
| `label` | Display name; falls back to the underlying id when none is declared. |
| `subtitle` | Short secondary line, e.g. `"branch · l1"` or `"2 node(s)"`. |
| `badges` | `[{"text": ..., "tone": ...}]`; tone is `ok`, `warn`, `bad`, `info` or `muted`. |
| `depth_hint` | 0-based depth, for indentation without measuring the tree. |
| `children` | Always present, `[]` for a leaf. |

Kind-specific fields:

| `kind` | Additional fields |
| --- | --- |
| `panel` | `panel_id`, `monitor`, `monitors`, `feed_ct_ids`, `is_service_entry` |
| `circuit` | `ct_id`, `ct_type`, `phase`, `entity_id`, `enabled`, `parent_ct_id`, `paired_ct_id`, `alias_of_ct_id`, `topology_status`, `polarity`, `measurement_multiplier`, `latest_power_w` |
| `device` | `device_id`, `phase`, `topology_status`, `source`, `latest_power_w`, `confidence`, `via_ct_id`, and `double_pole` only when true |
| `group` | `role`: `circuits`, `devices` or `subpanels`. The trailing fallback group for unplaceable rows is labelled `Unattributed`. |

Empty groups are omitted, not rendered as empty nodes. A device appears
exactly once: under the single circuit it references, or in the panel's
`DEVICES` group when it spans two CTs or references no single circuit of that
board. A device whose CTs resolve to no panel is listed under `Unattributed`
and counted in `stats.unplaced_devices`. A circuit node carries
`latest_power_w` for shape compatibility but does not populate it; the live
reading lives on the device node.

Top-level fields:

| Field | Meaning |
| --- | --- |
| `version` | Payload schema version (currently `2`). |
| `status` | Topology status: `candidate`, `confirmed` or `rejected`. |
| `generated_at` | ISO-8601 UTC, `Z`-suffixed. |
| `topology_version` | Stored topology version; `0` when none is configured. |
| `stats` | Counts: `panels`, `circuits`, `devices`, `unplaced_devices`, `unplaced_circuits`, `max_depth`, `duplicate_feeds`, plus `channels`/`feed_circuits` (present for a built tree). |
| `roots` | Root `panel` nodes; the service entry is first, then name order. |
| `flat` | Every panel, circuit and device as a summary row, in tree order. |
| `notes` | Diagnostics about fallbacks, merges, cycles and dropped ids. |
| `suggestions` | Only when nesting was requested and a topology exists: `candidate_parents`, `duplicate_aliases`, `applied`, `note`. |

`flat` exists so a client can build a filter or a list view from one pass over
the summaries instead of walking `roots`, and without duplicating the tree in
its own state.

```json
{
  "version": 2,
  "status": "confirmed",
  "generated_at": "2026-11-11T22:25:35.431020Z",
  "topology_version": 3,
  "stats": {
    "panels": 1,
    "circuits": 1,
    "devices": 1,
    "unplaced_devices": 0,
    "unplaced_circuits": 0,
    "max_depth": 3,
    "duplicate_feeds": 0,
    "channels": 2,
    "feed_circuits": 1
  },
  "roots": [
    {
      "id": "panel:main_panel",
      "kind": "panel",
      "label": "Main Panel",
      "subtitle": "service entry · 1 circuits",
      "badges": [],
      "depth_hint": 0,
      "children": [
        {
          "id": "group:main_panel:circuits",
          "kind": "group",
          "label": "CIRCUITS",
          "subtitle": "1 node(s)",
          "badges": [],
          "depth_hint": 1,
          "children": [
            {
              "id": "circuit:br_fridge",
              "kind": "circuit",
              "label": "Fridge",
              "subtitle": "branch · l1",
              "badges": [{"text": "confirmed", "tone": "ok"}],
              "depth_hint": 2,
              "children": [
                {
                  "id": "device:dev_fridge",
                  "kind": "device",
                  "label": "Fridge",
                  "subtitle": "l1",
                  "badges": [{"text": "confirmed", "tone": "ok"}],
                  "depth_hint": 3,
                  "children": [],
                  "device_id": "dev_fridge",
                  "phase": "l1",
                  "topology_status": "confirmed",
                  "source": null,
                  "latest_power_w": null,
                  "confidence": null,
                  "via_ct_id": "br_fridge"
                }
              ],
              "ct_id": "br_fridge",
              "ct_type": "branch",
              "phase": "l1",
              "entity_id": "sensor.br_fridge_power",
              "enabled": true,
              "parent_ct_id": "main_l1",
              "paired_ct_id": null,
              "alias_of_ct_id": null,
              "topology_status": "confirmed",
              "polarity": "normal",
              "measurement_multiplier": 1.0,
              "latest_power_w": null
            }
          ],
          "role": "circuits"
        }
      ],
      "panel_id": "main_panel",
      "monitor": "Main Panel",
      "monitors": ["Main Panel"],
      "feed_ct_ids": ["main_l1"],
      "is_service_entry": true
    }
  ],
  "flat": {
    "panels": [
      {"id": "panel:main_panel", "panel_id": "main_panel", "label": "Main Panel", "monitor": "Main Panel", "feed_ct_ids": ["main_l1"], "is_service_entry": true, "depth": 0}
    ],
    "circuits": [
      {"id": "circuit:br_fridge", "ct_id": "br_fridge", "label": "Fridge", "ct_type": "branch", "phase": "l1", "parent_ct_id": "main_l1", "topology_status": "confirmed", "depth": 2}
    ],
    "devices": [
      {"id": "device:dev_fridge", "device_id": "dev_fridge", "label": "Fridge", "phase": "l1", "topology_status": "confirmed", "via_ct_id": "br_fridge", "latest_power_w": null, "confidence": null, "source": null, "depth": 3}
    ]
  },
  "notes": [
    "Read-time view: parent_ct_id is never modified by this endpoint.",
    "A panel's feed is the set of 'main' CTs declaring that panel.",
    "A duplicate feed is an alias, not a child: panels measured by two monitors are merged into one node.",
    "Devices spanning two CTs (double-pole) are listed in the panel's DEVICES group; no single circuit owns them."
  ]
}
```

- **200** — the tree.
- **200** — with empty `roots` when no topology is configured. This is not an
  error: `status` is `candidate`, `topology_version` is `0`, `stats` counts are
  zero (and omit `channels`/`feed_circuits`), and `notes` is
  `["no topology configured"]`.
- **503** — no inference backend/facade is wired (same as every endpoint).

Ordering is deterministic for a given topology and latest-results map: roots,
circuits, devices and flat rows are sorted by stable keys, so two responses
differ only in `generated_at` and can be diffed directly.

### 3.8 `PUT /topology`

Replaces the topology. The body is a complete `Topology` document; unknown
fields are rejected.

Semantic requirements (enforced by the API, beyond schema validation):

1. `channels` must be non-empty.
2. At least one channel must have `ct_type: "main"` — reconciliation needs a
   power reference.
3. `devices` must be non-empty — there must be something to disaggregate.

Because every `Topology` field has a default, `{}` would otherwise be accepted;
it is rejected with `400`:

```json
{"error": "bad_request", "detail": "topology must contain at least one channel; topology must contain at least one device", "retryable": false}
```

Minimal valid request:

```bash
curl -sS -X PUT http://127.0.0.1:8787/api/v1/topology \
  -H 'X-API-Token: secret' -H 'Content-Type: application/json' \
  -d '{
    "channels": [
      {"ct_id": "main_l1", "entity_id": "sensor.m1", "ct_type": "main", "phase": "l1"},
      {"ct_id": "br_fridge", "entity_id": "sensor.br1", "ct_type": "branch", "phase": "l1", "parent_ct_id": "main_l1"}
    ],
    "devices": [{"device_id": "dev_fridge", "branch_ct_ids": ["br_fridge"]}]
  }'
```

Returns the stored topology (with the database-assigned `version`).

### 3.9 `POST /topology/confirm`

Promotes the current candidate topology to `confirmed`. No body required.

```json
{"status": "confirmed"}
```

`404` when no topology is configured.

### 3.10 `GET /quality`

`DataQualityReport` for the latest evaluation:

```json
{
  "timestamp": "2026-11-05T23:42:08.000000+00:00",
  "overall_score": 0.72,
  "channels": {
    "room_a_power": {
      "domain": "power",
      "unit": "W",
      "score": 0.55,
      "status": "degraded",
      "value_w": 6.42,
      "value_kwh": null,
      "reliability_factor": 0.55,
      "repeated": false,
      "issues": ["room_a_power: measurement_overlap (cluster size 10)"]
    },
    "panel_a_energy": {
      "domain": "energy",
      "unit": "kWh",
      "score": 1.0,
      "status": "good",
      "value_w": null,
      "value_kwh": 1426.0,
      "monotonic": true,
      "reset_count": 0,
      "max_drop_kwh": 0.0,
      "issues": []
    }
  },
  "issues": [
    "measurement_overlap (identical value 6.420 W at the same instant across 10 entities (size=10, members=room_a_power,room_b_power,...): identical simultaneous values across independent entities indicate a shared template or duplicate source; they are not independent measurements and must not be used as direct measurements. Reliability contribution reduced.)"
  ]
}
```

Notes:

- **Power and energy are separate domains.** Energy counters are reported as
  `value_kwh` with `"unit": "kWh"`; `value_w` is always `null` for them.
- Identical simultaneous power values are grouped into **one** issue per
  cluster, listing every member and the cluster size; channels are kept but
  their reliability is reduced.
- Repeated identical values are flagged `repeated: true` and are not treated as
  new measurements.

### 3.11 `GET /diagnostics`

Redacted support bundle: engine health, Home Assistant status, per-channel
health, model state, database integrity, recent errors, configuration, and the
inference backend self-description.

```json
{
  "generated_at": "2026-11-05T23:42:12.000000+00:00",
  "ok": true,
  "engine_health": {"status": "ok", "uptime_seconds": 132.6},
  "home_assistant": {"connected": true, "state": "connected", "reconnect_attempt": 0},
  "channels": {"room_a_power": {"channel": "room_a_power", "sample_count": 214}},
  "aligner": {"channels": 60, "samples": 4100},
  "models": {"devices": {}, "count": 0},
  "inference_quality": {"available": true, "report": {}},
  "database": {"available": true, "schema_current": true},
  "errors": {"available": true, "recent_system_events": [], "recent_quality_events": []},
  "configuration": {"ha_url": "http://homeassistant.local:8123", "api_token": "***"},
  "inference_backend": {
    "backend": "WattMosaicBackend",
    "disaggregation": "DisaggregationEngine",
    "model_registry": true,
    "learning": false
  }
}
```

Every sensitive value (tokens, passwords, keys) is replaced by `"***"`
recursively before the response is written.

### 3.12 `GET /models/{device_id}`

Model versions available for a device, with the currently active one.

```json
{
  "device_id": "dev_fridge",
  "active": {
    "device_id": "dev_fridge",
    "model_type": "binary",
    "version": 2,
    "status": "active",
    "sample_count": 148,
    "confidence": 0.91
  },
  "versions": [
    {"device_id": "dev_fridge", "model_type": "binary", "version": 1, "status": "rolled_back"},
    {"device_id": "dev_fridge", "model_type": "binary", "version": 2, "status": "active"}
  ]
}
```

- **200** — at least one version exists.
- **404** — no model has been learned for the device yet (facade returns
  `None`/raises `NotFound`).

### 3.13 `POST /commands`

Sends a `ServiceCommand`. The body is validated; unknown commands or fields are
`400`, never `422`.

```json
{
  "command": "start_learning",
  "device_id": "dev_fridge",
  "parameters": {}
}
```

Allowed values: `start_learning`, `pause_learning`, `resume_learning`,
`accept_sample`, `reject_sample`, `rebuild_model`, `reset_device_model`,
`freeze_model`, `unfreeze_model`, `rebuild_topology`, `run_diagnostics`,
`export_device_data`.

Response is a `ServiceResult`:

```json
{"ok": true, "command": "start_learning", "detail": "learning active", "data": null}
```

A recognised but unsupported command returns **200** with `"ok": false` and a
reason — it is a valid request that the engine declined, not a malformed one:

```json
{"ok": false, "command": "rebuild_model", "detail": "rebuild_model requires the learning backend, which is not installed"}
```

### 3.14 `GET /export/{device_id}`

JSON export of everything the engine knows about a device: samples, inferred
results, learning samples, model versions and metadata.

```json
{
  "device_id": "dev_fridge",
  "exported_at": "2026-11-05T23:42:15.000000+00:00",
  "inference_results": [],
  "learning_samples": [],
  "model_versions": [],
  "topology": {}
}
```

`404` when the device is unknown to both the database and the topology.

### 3.15 `GET /metrics`

Prometheus exposition format (`text/plain`). Example:

```text
# HELP wattmosaic_up Engine process is up (1) or unavailable (0)
# TYPE wattmosaic_up gauge
wattmosaic_up 1
# HELP wattmosaic_engine_status Engine status as a label
# TYPE wattmosaic_engine_status gauge
wattmosaic_engine_status{status="ok"} 1
# HELP wattmosaic_ha_connected Home Assistant WebSocket connected
# TYPE wattmosaic_ha_connected gauge
wattmosaic_ha_connected 1
# HELP wattmosaic_queue_depth Ingestion queue depth
# TYPE wattmosaic_queue_depth gauge
wattmosaic_queue_depth 0
# HELP wattmosaic_uptime_seconds Engine uptime
# TYPE wattmosaic_uptime_seconds counter
wattmosaic_uptime_seconds 132.6
# HELP wattmosaic_contract_info Contract version
# TYPE wattmosaic_contract_info gauge
wattmosaic_contract_info{version="1.4.0",engine="<version>"} 1
```

Metrics must never fail a scrape: if the backend cannot produce them, a
fallback exposition built from `/health` is returned with status `200`.

---

## 4. Versioning

`CONTRACT_VERSION` (`1.4.0`) follows semantic versioning:

| Change | Version bump |
| --- | --- |
| Removing a field, renaming a field, changing a type, removing an endpoint | major |
| Adding an optional field or a new endpoint | minor |
| Behavioural fix with no schema change | patch |

Every response carries `X-WattMosaic-Contract-Version`. Clients should reject
responses whose **major** version differs from what they were built for, and
tolerate a higher minor/patch (extra fields are additive).

---

## 5. Operational notes

- **Bounded concurrency:** in-flight requests are limited (constructor kwarg
  `max_concurrency`, default 32). Beyond that the server answers
  `503 server_busy` with `retryable: true` rather than queueing without bound.
- **Request timeout:** constructor kwarg `request_timeout_s` (default 30 s) →
  `504 timeout`.
- **Idempotent shutdown:** `APIServer.stop()` may be called repeatedly; the
  second call is a no-op.
- **Ephemeral ports:** construct with `port=0` for tests; `server.port` is
  updated to the actually bound port after `start()`.
- **Read-only:** the HTTP API exposes no endpoint that can actuate a Home
  Assistant device. Commands control the *engine's* learning lifecycle only.

## 6. Minimal client example

```python
import aiohttp

async def fetch_health(base_url: str, token: str) -> dict:
    headers = {"X-API-Token": token}
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{base_url}/api/v1/health", headers=headers) as resp:
            if resp.status == 401:
                raise RuntimeError("engine rejected the token")
            if resp.status >= 400:
                raise RuntimeError((await resp.json())["error"])
            return await resp.json()
```
