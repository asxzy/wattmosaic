# Configuration Reference

WattMosaic is configured entirely through environment variables. On Home
Assistant OS, the App options UI maps to these variables (see
`addon/config.yaml`); the integration has its own small options flow for
connection and topology.

> Secrets (`HA_TOKEN`, `WM_API_TOKEN`) are never logged, never written to the
> database and never included in diagnostics or exports.

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

The API is versioned: base path `/api/v1`, and every response carries
`X-WattMosaic-Contract-Version`. The integration refuses to run against an
incompatible contract version rather than silently misinterpreting data.

## Storage

| Variable | Default | Description |
| --- | --- | --- |
| `WM_DATABASE_PATH` | `data/wattmosaic.db` | SQLite file. On HA OS use `/data/wattmosaic.db` (persistent volume). |
| `WM_RETENTION_DAYS_RAW` | `30` | Raw sample/event/result retention. Must exceed your longest model-training horizon. |
| `WM_RETENTION_DAYS_AGGREGATES` | `730` | 5-minute and 1-hour aggregate retention. |

Retention runs periodically and is logged to `system_events`. Pruning never
touches `device_model_versions` or `learning_samples`, so model history survives.

## Behaviour

| Variable | Default | Description |
| --- | --- | --- |
| `WM_LEARNING_ENABLED` | `true` | Master switch for automatic learning. Inference continues when disabled. |
| `WM_STALE_AFTER_S` | `120` | A reading older than this is `stale`: excluded from learning, confidence reduced, entity marked unavailable. |
| `WM_INFERENCE_INTERVAL_S` | `5` | Inference loop period. Must be shorter than your fastest meaningful load cycle. |
| `WM_QUEUE_MAX_SIZE` | `5000` | Bounded ingestion queue. On overflow the oldest items are dropped and a metric incremented; memory never grows unbounded. |
| `WM_TOPOLOGY_BOOTSTRAP` | `true` | Discover a candidate topology from Home Assistant at startup when the database has none. Disabling it leaves a fresh install with nothing to infer; `/health` then reports `degraded` with `details.topology.configured = false`. |
| `WM_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`. |
| `WM_LOG_JSON` | `true` | Structured JSON logs for the App; set `false` for human-readable local logs. |

## Learning thresholds

These are code-level constants in `EngineConfig` and documented here because
they directly determine what the system is willing to claim.

| Constant | Default | Meaning |
| --- | --- | --- |
| `min_samples_for_model` | `12` | Below this, a device keeps `model_status=pending` and may only be `model_estimated` with low confidence. |
| `min_validation_r2` | `0.70` | A candidate must reach this hold-out R² to be promotable. |
| `promotion_margin` | `0.05` | A candidate must also beat the active model by this margin; otherwise the active model stays. |
| `max_sample_rejection_ratio` | `0.60` | If more than 60% of a device's samples are rejected, the trainer reports a data-quality problem rather than training. |

Changing these upward makes the system more conservative. Lowering them is not
recommended: it produces confident-sounding numbers without evidence.

## App-install discovery

When the engine runs as a Home Assistant App, it announces itself so the
integration can be added by confirming one screen instead of typing a container
hostname. This is on by default and needs no configuration.

| Variable | Default | Description |
| --- | --- | --- |
| `WM_DISCOVERY_ENABLED` | `true` | Publish the announcement. `false` disables it; the integration can still be added by hand. |
| `WM_DISCOVERY_HOST` | resolved | Override the announced host. Normally unset: the publisher reads the Supervisor's own record of the app (`GET /addons/self/info`), then the container hostname, then the slug rule (`_` → `-`). A value that is not a bare host name (a scheme, path, userinfo or port), or that is loopback, is skipped in favour of the next candidate rather than announced. |
| `WM_DISCOVERY_PORT` | `8787` | Announced port. Must match `ingress_port` in `addon/config.yaml`. |
| `WM_DISCOVERY_SERVICE` | `wattmosaic` | Announced service name. Must equal the integration domain. |
| `WM_DISCOVERY_WAIT_S` | `120` | How long to wait for the engine's `/healthz` before announcing anyway. A slow engine still gets announced, because the integration probes before creating an entry. |

What is announced is exactly `{"service": "wattmosaic", "config": {"host": …,
"port": …}}`: a host name and a port, no token and no household data. Outside
the Supervisor (plain Docker, CI, a laptop) there is no `SUPERVISOR_TOKEN`, so
the publisher logs one line and exits 0 — it never blocks or fails startup.

The App must also declare the domain, which is the permission the Supervisor
checks (`supervisor/api/discovery.py` returns 403 for an undeclared service):

```yaml
discovery:
  - wattmosaic
```

## Integration options

Configured in the Home Assistant UI (Settings → Devices & Services → WattMosaic
→ Configure). An App install normally arrives pre-filled through discovery
(above); the manual values are listed for a standalone engine or a dismissed
discovery card.

| Option | Default | Description |
| --- | --- | --- |
| Engine URL | — | Copy **Hostname** from the app's info tab, e.g. `http://f48de52e-wattmosaic-engine:8787` for an app-store install or `http://local-wattmosaic-engine:8787` for a local copy. Discovery normalises the announced value through the same rule, so both paths produce the same config entry. |
| API token | — | Must match `WM_API_TOKEN`. Stored in the HA config entry; redacted in diagnostics. |
| Scan interval | `30` s | Snapshot poll period. Does not affect engine inference cadence. |
| Stale after | `120` s | Result age at which entities become unavailable. |
| Unavailable after | `120` s | Engine unreachable duration before entities become unavailable. |
| CT role | from topology | `main`, `branch`, `dedicated_device`, `unknown`. |
| CT phase | from topology | `l1`, `l2`, `both`, `unknown`. |
| CT polarity | `normal` | Set `inverted` if a CT is installed backwards. |
| Paired CT | from topology | Second leg of a double-pole load. |
| Device → CT mapping | candidates | Must be explicitly confirmed; unconfirmed mappings stay `candidate`. |
| Create energy entity (per device) | `true` | Adds a `total_increasing` kWh sensor for that load. Stored in the config entry, never sent to the engine. When the device's state entity belongs to a real Home Assistant device, the energy sensor is attached there — after registration, so the move never changes its entity ID (ADR-0021). |

## Per-device configuration

Each device can be described on three independent axes (ADR-0011). All fields
default to `unknown`, so a partially filled configuration is honest rather than
silently defaulted.

```jsonc
{
  "device_id": "dev_panel_a_lighting_3",   // set from the command, not the payload
  "device_type": "light",
  "connectivity": "connected",        // connected | not_connected | unknown
  "monitoring": "shared",             // dedicated | shared | home_total_only | unmonitored | unknown
  "behavior": "variable",             // fixed | variable | complex | unknown

  "state_entity_ids": ["light.panel_a_lighting_3"],
  "mode_entity_ids": [],
  "controllable_entity_ids": [],

  "monitored_by_ct_ids": [],
  "expected": {
    "on_w": 40.0, "idle_w": null, "standby_w": null,
    "startup_w": null, "shutdown_w": null,
    "tolerance_w": null, "tolerance_fraction": null
  },
  "light": {
    "light_type": "led", "bulb_count": 6, "watts_per_bulb_on": 9.0,
    "standby_w": null, "min_output_fraction": null, "dimming_curve": []
  },
  "controller": null,
  "notes": []
}
```

### Validation rules (gateway-side, enforced by the contract)

| Rule | Reasoning |
| --- | --- |
| `connectivity=not_connected` forbids `state_entity_ids` | A non-smart device has no HA entity to name. |
| `behavior=complex` requires a `controller` | Complex behaviour with no declared mode/loads is meaningless. |
| `light` is only valid for `device_type` in (`light`, `light_group`, `unknown`) | A refrigerator cannot have a bulb count. |
| `ControllerStateMapping.label` must be non-empty | Labels are matched case-insensitively against HA state text. |
| `dimming_curve` x values must ascend | A dimming curve is a function of brightness. |
| `DeviceConfigurationSet.devices` must be unique by `device_id` | Two configurations for one device are ambiguous. |
| A `climate`/`water_heater` entity in `state_entity_ids` is reclassified to mode | The natural mistake; corrected deterministically with a `mode_entity_reclassified:<entity>` reason (ADR-0012). |

### What a state entity may and may not do

`state_entity_ids` and `controller.load_entity_ids` are **runtime** signals: they
say whether the device is drawing power. They are the only entities that can
write into the device's state, and only a positively-on value unlocks a
configured `expected.on_w`.

A CT **power** entity (`sensor.water_heater_power`) is never a state entity. It is
already the device's measurement, and its value is watts. Numeric readings are
consumed as measurements and never become events, but availability changes
(`unavailable`, `unknown`, `restored`) do — and those must not reach the state
slot either: a single blip would replace `off` with `unavailable`, and the next
real `off` would then read as a transition. Power-entity events are still
recorded, but they are counted under `power_events_not_state` and cannot become
device state.

A `controller` state label mapped with `role=load` **and** `confirmed=true` is the
one declaration that may supply watts on its own, without a separate load entity.
That is deliberate: it is an explicit assertion about one specific label, not a
claim that a mode proves load.

### Setting a configuration

Configurations are written through the engine command channel:

```bash
curl -X POST http://localhost:8787/api/v1/commands \
  -H "X-API-Token: $WM_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
        "command": "set_device_configuration",
        "device_id": "dev_panel_b_water_heater",
        "parameters": {
          "device_type": "hvac_heat",
          "connectivity": "connected",
          "monitoring": "dedicated",
          "behavior": "complex",
          "expected": {"on_w": 4500.0},
          "controller": {
            "mode_entity_id": "water_heater.panel_b_water_heater",
            "load_entity_ids": ["binary_sensor.panel_b_water_heater_heating"],
            "states": [
              {"label": "comfort", "role": "mode"},
              {"label": "heating", "role": "load", "power_w": 4500.0, "confirmed": true}
            ]
          }
        }
      }'
```

`device_id` is taken from the command and forced onto the payload, so it cannot
be overridden inside `parameters`. Sending an **empty** `parameters` object
removes the configuration, restoring the device metadata exactly.

Each write projects the configuration into the topology's device metadata and
bumps the topology version, so ingestion, inference and learning always read the
same answer.

### Read endpoints

| Endpoint | Returns |
| --- | --- |
| `GET /api/v1/devices` | Bare JSON list; each device now carries `configuration`, `confidence_tier` and the tier fields inside `latest_result`. |
| `GET /api/v1/confidence_tiers` | The ordered tier vocabulary, each entry with `tier`, `rank`, `label`, `description` and `calibrated: false`. |

`confidence_is_calibrated` remains `false` everywhere. Neither the tier nor the
score is a probability.

## Topology confirmation

Auto-derived topology is written with `topology_status=candidate`. You confirm it
either in the integration options flow or via:

```bash
curl -X POST http://localhost:8787/api/v1/topology/confirm \
  -H "X-API-Token: $WM_API_TOKEN"
```

Confirmation creates a new topology version. Previous versions remain in
`topology_versions` for audit and rollback.

## Recommended starting configuration

```bash
HA_URL=http://192.168.1.10:8123
HA_TOKEN=<long-lived token>
WM_API_TOKEN=<random 32+ chars>
WM_DATABASE_PATH=/data/wattmosaic.db
WM_LEARNING_ENABLED=true
WM_STALE_AFTER_S=120
WM_INFERENCE_INTERVAL_S=5
WM_RETENTION_DAYS_RAW=30
WM_LOG_LEVEL=INFO
```

Generate the API token with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```
