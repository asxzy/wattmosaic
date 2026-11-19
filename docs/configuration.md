# Configuration Reference

WattMosaic is configured entirely through environment variables. On Home
Assistant OS, the App options UI maps to these variables (see
`addon/config.yaml`); the integration has its own small options flow for
connection, endpoints and topology.

> Secrets (`HA_TOKEN`, `WM_API_TOKEN`) are never logged, never written to the
> database and never included in diagnostics or exports.

The authoritative source is `engine/wattmosaic_engine/config/settings.py`.
Contract 3.0 replaced the old timer-driven solver with a three-loop runtime;
several settings below are new, and `WM_INFERENCE_INTERVAL_S` is deprecated.

## Home Assistant connection

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `HA_URL` | one of | — | Full base URL, e.g. `http://homeassistant.local:8123`. Takes precedence over `HA_HOST`. |
| `HA_HOST` | one of | — | Host or `host:port`. If no port is given, `8123` is used. A host without a scheme is treated as `http://`. |
| `HA_TOKEN` | yes | — | Long-lived access token. A read-only user is sufficient. |
| `WM_HA_TOKEN` | no | — | Overrides `HA_TOKEN` when both are present. |
| `WM_HA_VERIFY_SSL` | no | `true` | Verify TLS certificates. Only disable for a trusted local self-signed setup. |

The engine validates the URL and reports a precise error instead of retrying
forever with a bad URL. `--check-config` prints the redacted effective config.

## Engine API

| Variable | Default | Description |
| --- | --- | --- |
| `WM_API_HOST` | `0.0.0.0` | Bind address. |
| `WM_API_PORT` | `8787` | Bind port. |
| `WM_API_TOKEN` | empty | When set, every request must send `X-API-Token`. Strongly recommended on HA OS. Compared with `hmac.compare_digest`. |

The API speaks contract `3.0.0`: base path `/api/v1`, every response carries
`X-WattMosaic-Contract-Version: 3.0.0`. The integration refuses to run against an
incompatible contract. The `/mapping` endpoint and v2 forms are gone — use
`/matrix` and its drill-down routes.

## Storage and retention tiers

| Variable | Default | Description |
| --- | --- | --- |
| `WM_DATABASE_PATH` | `data/wattmosaic.db` | SQLite file. On HA OS use `/data/wattmosaic.db` (persistent volume). |
| `WM_RETENTION_DAYS_RAW` | `30` | L1 raw reader-sample retention. The validator accepts 1–90 days; must exceed your longest model-training horizon. |
| `WM_RETENTION_DAYS_AGGREGATES` | `730` | L3 aggregate retention (30–36500 days). |
| `WM_RETENTION_DAYS_EVIDENCE` | unset | L2 event-evidence horizon in days. Empty disables it: evidence is long-term and only an explicit operator action bounds it. |
| `WM_RETENTION_DAYS_VERSIONS` | unset | L4 version-history horizon in days. Empty disables it, for the same reason. |
| `WM_EVIDENCE_RETENTION_DAYS` | `400` | Derived-evidence horizon (DEC-30). |
| `WM_EVIDENCE_RAW_RETENTION_DAYS` | `30` | Raw samples cited by evidence; expires sooner than derived evidence. |

An explicit L2/L4 horizon must be at least as long as the raw horizon: evidence
that does not outlive the raw window it explains cannot be audited.
Before any migration numbered ≥ 13 the engine automatically writes a
pre-migration backup next to the database (see [`docs/deployment.md`](deployment.md)).

## Behaviour

| Variable | Default | Description |
| --- | --- | --- |
| `WM_LEARNING_ENABLED` | `true` | Master switch for learning. Inference continues when disabled. |
| `WM_STALE_AFTER_S` | `120` | A reading older than this is `stale`: excluded from learning, quality reduced, entity marked unavailable. |
| `WM_DEFAULT_SAMPLE_INTERVAL_S` | `10` | Last-resort fallback for a CT publish interval when no observed cadence is available. Not a measurement. |
| `WM_QUEUE_MAX_SIZE` | `5000` | Bounded ingestion queue; oldest items dropped on overflow with a counted metric. |
| `WM_AUTO_PLACEMENT` | `false` | Allows discovery to **write device configurations** when a request also sends `apply: true`. Off by default because a write must be requested. |
| `WM_TOPOLOGY_BOOTSTRAP` | `true` | Discover candidate endpoints/topology at startup when the database has none. Disabling leaves a fresh install with nothing to infer; `/health` reports `degraded` with `details.topology.configured = false`. |
| `WM_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`. |
| `WM_LOG_JSON` | `true` | Structured JSON logs for the App; `false` for human-readable local logs. |

## Three-loop runtime (3.0)

| Variable | Default | Description |
| --- | --- | --- |
| `WM_INFERENCE_INTERVAL_S` | `5` | **Deprecated and ignored.** Accepted (for one release, so App config validation keeps working) with a startup warning. Inference now runs per event group, not on a timer. Its value is parsed but cannot drive a loop. |
| `WM_EVENT_QUEUE_MAX` | `1000` | Maximum ready/revised event groups held for the event loop. Overflow drops the oldest group and increments `groups_dropped`. |
| `WM_STEADY_HEARTBEAT_S` | `60` | Seconds between staleness-only heartbeats. A heartbeat publishes `stale_sources`/aging only; it never runs a full solve. |
| `WM_UNKNOWN_PENALTY_SCALE` | `1.0` | Multiplier on the unknown-load penalty in the joint solve. `1.0` is neutral; never a measurement and never `0` for a missing value. |

The fast loop is O(1) per sample; the event loop runs inference on grouped
measurement events; the background loop runs learning, retention and
reconciliation. See [`docs/data_flow.md`](data_flow.md) and HB §5.

## Learning thresholds

These are code-level constants in `EngineConfig`, documented here because they
determine what the system is willing to claim.

| Constant | Default | Meaning |
| --- | --- | --- |
| `min_samples_for_model` | `12` | Below this, a device keeps `model_status=pending`. |
| `min_validation_r2` | `0.70` | Hold-out R² required for a candidate model to be promotable. |
| `promotion_margin` | `0.05` | A candidate must also beat the active model by this margin. |
| `max_sample_rejection_ratio` | `0.60` | Above this rejection rate the trainer reports a data-quality problem. |

Legacy models fitted on whole-circuit power are read-only (`LEGACY_ABSOLUTE`);
models are retrained from device-attributed delta evidence.

## App-install discovery

| Variable | Default | Description |
| --- | --- | --- |
| `WM_DISCOVERY_ENABLED` | `true` | Publish the announcement so the integration can be added by confirming one screen. |
| `WM_DISCOVERY_HOST` | resolved | Override the announced host; normally unset. A value that is not a bare host name, or that is loopback, is skipped in favour of the next candidate. |
| `WM_DISCOVERY_PORT` | `8787` | Announced port; must match `ingress_port` in `addon/config.yaml`. |
| `WM_DISCOVERY_SERVICE` | `wattmosaic` | Announced service name; must equal the integration domain. |
| `WM_DISCOVERY_WAIT_S` | `120` | How long to wait for `/healthz` before announcing anyway. |

Outside the Supervisor there is no `SUPERVISOR_TOKEN`, so the publisher logs one
line and exits 0 — it never blocks startup.

## Integration options

Configured in **Settings → Devices & Services → WattMosaic → Configure**. An App
install normally arrives pre-filled through discovery.

| Option | Default | Description |
| --- | --- | --- |
| Engine URL | — | The address the *HA host* can reach, e.g. `http://f48de52e-wattmosaic-engine:8787`. |
| API token | — | Must match `WM_API_TOKEN`. Stored in the config entry; redacted in diagnostics. |
| Scan interval | `30` s | Snapshot poll period. Does not affect engine cadence. |
| Stale after | `120` s | Result age at which entities become unavailable. |
| Unavailable after | `120` s | Engine-unreachable duration before entities become unavailable. |
| Endpoints | discovered | Accept HA power entities as measurement endpoints; only accepted endpoints are ingested. |
| Endpoint scope / plug | candidate | Confirm a `SINGLE_SUBJECT` scope so a smart plug or appliance monitor yields `MEASURED`. |
| Topology edges | candidate | Review and pin/reject typed edges; engine proposals stay soft until confirmed. |
| Create energy entity (per device) | `true` | Adds a kWh sensor. Attached to the real HA device after registration, so the entity id never changes (ADR-0021). |
| Automatically create devices | `false` | Stored as `auto_placement`; requires both the integration consent and `WM_AUTO_PLACEMENT` before a write. |

## Per-device configuration

Each device is described on three independent axes (ADR-0011). All fields default
to `unknown`, so a partial configuration is honest.

```jsonc
{
  "device_id": "pd_panel_a_lighting_3",
  "device_type": "light",
  "connectivity": "connected",        // connected | not_connected | unknown
  "monitoring": "shared",             // shared | home_total_only | unmonitored | unknown
  "behavior": "variable",             // fixed | variable | complex | unknown

  "state_entity_ids": ["light.panel_a_lighting_3"],
  "mode_entity_ids": [],
  "controllable_entity_ids": [],
  "expected": {"on_w": 40.0, "idle_w": null, "standby_w": null},
  "light": {"light_type": "led", "bulb_count": 6, "watts_per_bulb_on": 9.0},
  "controller": null,
  "notes": []
}
```

`monitoring: shared` stops the whole row being read as this device's power — the
row becomes an upper bound and the device can only report a configured prior.
There is no private-measurement level. A power sensor is never a state entity:
availability blips would manufacture false transitions. A confirmed controller
label with `role=load` is the one declaration that may supply watts on its own.

## Read endpoints and confidence

| Endpoint | Returns |
| --- | --- |
| `GET /api/v1/devices` | Devices with their latest `DeviceEstimate` (status, identifiability, bounds, version triple). |
| `GET /api/v1/snapshot` | Latest estimates, group estimates and residual. |
| `GET /api/v1/groups` | One group power estimate per `group_id`. |

Confidence is a seven-component `ConfidenceVectorV3` (HB §9.6), each component in
`[0, 1]` or `None`. There is no blended scalar and it is **not** a probability.
A non-identifiable device publishes `None` with justified bounds, never `0 W`.

## Versioned audit and analysis pass

| Variable | Default | Description |
| --- | --- | --- |
| `WM_AUDIT_ENABLED` | `true` | Master switch for verdict/evidence writing. |
| `WM_AUDIT_ISOLATION_ENABLED` | `true` | Persist isolation verdicts. |
| `WM_AUDIT_ASSOCIATION_ENABLED` | `true` | Persist device→endpoint association relations. |
| `WM_AUDIT_CIRCUITS_ENABLED` | `true` | Persist classified circuits. |
| `WM_AUDIT_RESIDUAL_ENABLED` | `true` | Persist residual observations. |
| `WM_AUDIT_HIERARCHY_ENABLED` | `true` | Persist hierarchy/overlap edges. |
| `WM_AUDIT_RECORDS_ENABLED` | `true` | Persist the append-only inference record trail. |
| `WM_ANALYSIS_OFFSET_S` | `0` | Seconds between an event and the start of its analysis window. |
| `WM_ISOLATION_WINDOW_S` | `30` | Width of the "was anything else changing?" window. |
| `WM_ISOLATION_POWER_WINDOW_S` | `60` | Window whose readings are attributed to the event; must be ≥ `WM_ISOLATION_WINDOW_S`. |
| `WM_ISOLATION_BY_CLASS`, `WM_ISOLATION_BY_DEVICE` | empty | JSON timing overrides (keys `analysis_offset_s`, `isolation_window_s`, `power_window_s`). |
| `WM_ISOLATION_DEVICE_CLASSES` | empty | JSON `device_id → [class labels]` for the class layer. |
| `WM_AUDIT_ALGORITHM_VERSION` | `disagg@2.0` | Version stamped on every audit artifact; a replay is comparable only under the same value. |
| `WM_REANALYSIS_INTERVAL_S` | `0` | Event-driven reanalysis: `0` means immediate; a positive value is only a minimum-gap debounce, never a periodic trigger. |
| `WM_REANALYSIS_LOOKBACK_S` | `21600` | How much stored raw history one reanalysis reads. |
| `WM_TRAIN_INTERVAL_S` | `0` | Event-driven training: `0` immediate; positive is an optional debounce. |
| `WM_HISTORY_LOOKBACK_S` | `86400` | How far back history reads default to (a lower bound is required; `since=now` matched nothing). |

Each `WM_AUDIT_*_ENABLED` flag gates **writing that family only**; the algorithms
still run, because a family not written may still feed one that is. The master
switch is `AND`-ed with each per-artifact flag.

## Validation

`EngineConfig.validate` reports rather than clamps:

- `stale_after_s`, `event_queue_max`, `steady_heartbeat_s` must be positive;
- `unknown_penalty_scale` must be finite and non-negative;
- `retention_days_raw` must be in 1–90, aggregates in 30–36500;
- an explicit L2/L4 horizon must be ≥ the raw horizon;
- isolation windows must be finite, non-negative, and the power window ≥ the
  isolation window; isolation override maps may only use known timing keys.

`--check-config` prints the redacted effective config and these validation errors.

## Recommended starting configuration

```bash
HA_URL=http://192.168.1.10:8123
HA_TOKEN=<long-lived token>
WM_API_TOKEN=<random 32+ chars>
WM_DATABASE_PATH=/data/wattmosaic.db
WM_LEARNING_ENABLED=true
WM_STALE_AFTER_S=120
WM_RETENTION_DAYS_RAW=30
WM_EVENT_QUEUE_MAX=1000
WM_STEADY_HEARTBEAT_S=60
WM_UNKNOWN_PENALTY_SCALE=1.0
WM_LOG_LEVEL=INFO
```

Generate the API token with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```
