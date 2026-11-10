# WattMosaic Engine

> Multi-CT load disaggregation for Home Assistant.
> Learns what each device actually draws, from your multi-channel CT energy
> monitor plus Home Assistant state changes — and keeps "unknown" as an honest
> answer when the evidence is not there.

**WattMosaic Engine** is the inference and learning backend of WattMosaic. It runs
as a Home Assistant App (formerly "add-on"), reads Home Assistant **read-only**,
and serves a versioned local API (`/api/v1`) for the WattMosaic custom
integration.

| | |
| --- | --- |
| Engine version | 0.1.3 |
| Contract version | 1.1.0 |
| API | `http://<hostname>:8787/api/v1` (internal network; see §4) |
| Ingress panel | yes — sidebar entry "WattMosaic" |
| Architectures | `amd64`, `aarch64` |
| Persistent data | `/data/wattmosaic.db` (SQLite, WAL) |

---

## What it does

1. **Ingests** every power and energy entity you can measure, plus registry and
   state events, over the Home Assistant WebSocket API.
2. **Synchronises** asynchronous CT readings into aligned windows, marking repeats
   and stale values instead of treating them as new measurements.
3. **Reconciles** main, branch and dedicated CTs using an explicit electrical
   topology, so nested circuits are never double-counted.
4. **Infers** per-device power using direct measurement first, then constrained
   inference, then learned models — and reports an explicit unknown residual
   instead of distributing it arbitrarily.
5. **Learns** device models (binary, dimmable, variable-speed, multi-state,
   cycle-based) from natural operation, with validation before promotion and
   versioned rollback.
6. **Serves** results, diagnostics, data quality and exports over `/api/v1`.

The engine contains **no service-call code path**. It cannot switch a device,
change a thermostat or actuate anything, even if it is given broader credentials.

---

## Installation

### 1. Build the app directory

The Supervisor builds a local App with the **app folder as the Docker build
context**, so the engine sources have to be staged next to the Dockerfile. From
the repository root:

```bash
scripts/package_addon.sh
# -> dist/addon/wattmosaic_engine/  (self-contained, ~600 KB)
```

Copy that directory to the local-apps directory as `wattmosaic_engine`. Home
Assistant 2026.11 exposes it as `/local_apps` (previously `/addons`) and stores
it on the host under `apps/local/`:

```bash
# via SSH/Samba, or directly with the helper (which probes both mount names):
scripts/package_addon.sh --install root@homeassistant
```

Then in Home Assistant: **Settings → Apps → App store → ⋮ → Check for updates**.
The local app **WattMosaic Engine** appears under *Local apps*.

> Do not copy the repository's `addon/` folder by itself — it references
> `pyproject.toml`, `engine/` and `constraints.txt`, which
> `scripts/package_addon.sh` places in the staging directory.

### 2. Configure

Open the app → **Configuration**:

| Option | Default | Meaning |
| --- | --- | --- |
| `ha_url` | `http://homeassistant:8123` | Home Assistant base URL. |
| `ha_token` | *(empty)* | Long-lived **user** token (Profile → Security). If empty, the app uses its own `SUPERVISOR_TOKEN` through the Core proxy. |
| `api_token` | *(empty)* | Shared secret the WattMosaic integration sends as `X-API-Token`. **Set this.** |
| `learning_enabled` | `true` | Automatic learning. Inference keeps running when disabled. |
| `stale_after_s` | `120` | Age at which a reading is stale: excluded from learning, confidence reduced. |
| `inference_interval_s` | `5` | Inference loop period. |
| `log_level` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`. |
| `log_json` | `true` | Structured JSON logs. |
| `database_path` | `/data/wattmosaic.db` | SQLite file inside the persistent volume. |
| `retention_days_raw` | `30` | Raw sample/event retention. |
| `retention_days_aggregates` | `730` | Aggregate retention. |
| `queue_max_size` | `5000` | Bounded ingestion queue. |

Generate an API token with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

### 3. Start

Press **Start**, then open the **Log** tab. A healthy start looks like:

```text
level=info component=entrypoint msg="read /data/options.json (12 options)"
level=info component=entrypoint msg="starting engine db=/data/wattmosaic.db ..."
... "WattMosaic API listening on 127.0.0.1:8788"
... "inference backend loaded: DisaggregationEngine"
```

Open the sidebar panel **WattMosaic** (ingress). It is a read-only view of what the
engine currently believes, refreshed every few seconds while the tab is visible:

- **Overview** — engine/HA/database state, contract version, the live power split
  (known loads, residual, and what is still unknown), and the device list.
- **Devices** — every inferred or learned device with its latest estimate, the
  evidence grade behind it (a tier, not a probability) and its measurement age.
  Open one for history, phase/topology placement and the reasons behind its tier.
- **Topology** — the current topology version, candidate parents and aliases, and
  what the engine has suppressed or could not infer. **Confirm topology** records
  the current view as authoritative.
- **Data quality** — per-channel sample cadence, gaps and ages, so a bad feed can be
  told apart from a bad model.
- **Diagnostics** — aligner state, channel health, event log, and JSON download /
  copy for bug reports.

The panel never changes device state; the only write it offers is confirming the
topology the engine already inferred. Unknown values are shown as unknown rather
than as zero.

### 4. Connect the integration

Install `custom_components/wattmosaic` in Home Assistant, then
**Settings → Devices & Services → Add integration → WattMosaic**:

- **Engine URL**: `http://local-wattmosaic-engine:8787` for a local copy, or
  `http://f48de52e-wattmosaic-engine:8787` when installed from the app store.
  The authoritative value is **Hostname** on the app's info tab.
- **API token**: the same `api_token`

---

## Networking

- No host networking. The app listens on the Supervisor's internal Docker network.
- Ingress proxies the app's port `8787`; Home Assistant authentication gates the
  panel (admin-only by default).
- The engine itself listens on loopback `127.0.0.1:8788` and always requires
  `X-API-Token` when one is set. A tiny gateway (`gateway.py`) on `8787` injects
  the token only for Supervisor-ingress requests and exposes an unauthenticated,
  data-free `/healthz` for the watchdog and Docker health check.
- The host port `8787/tcp` is **not published** by default. Enable it in the app's
  *Network* section only for debugging or for non-Supervisor clients, and keep
  `api_token` set.

### Watchdog and health

- Supervisor watchdog: `http://[HOST]:8787/healthz` → `200 ok`.
- Docker `HEALTHCHECK`: `/app/healthcheck.py` checks `/healthz` **and**
  `/api/v1/health` with the API token, so it detects a live gateway with a dead
  engine. `degraded` (e.g. HA reconnecting) is treated as healthy; `error` is not.

---

## Persistent storage and backups

- `/data` is the app's private volume and survives updates.
- SQLite runs in WAL mode; a hot copy can be inconsistent, so the app declares
  `backup: cold` — Home Assistant stops it before snapshotting.
- Manual backup:
  ```bash
  docker exec app_local_wattmosaic_engine \
    python -c "import sqlite3;s=sqlite3.connect('/data/wattmosaic.db');d=sqlite3.connect('/data/backup.db');s.backup(d)"
  ```
- Restore: stop the app, replace `wattmosaic.db`, start it. Migrations are
  forward-only; never downgrade the database to an older engine without a backup.

---

## Security notes

- Secrets are only read from the app options / environment. They are never
  logged (`EngineConfig.redacted()`), never written to the database, and never
  included in diagnostics or exports.
- `api_token` is compared in constant time. When it is empty the gateway serves
  only loopback and the Supervisor's internal app network
  (`172.30.32.0/23`); anything else is refused, including a host port you
  publish yourself. Set `api_token` whenever the engine must be reachable from
  outside that network.
- `SUPERVISOR_TOKEN` is an administrator-grade credential for the Core proxy.
  The engine only reads; still, prefer a dedicated read-only user token in
  `ha_token` if your policy requires it.
- The ingress panel is static files inside the image (`addon/ui/`), served only
  to a trusted ingress peer. Direct clients get a JSON descriptor at `/` and
  `401` for `/ui/*`, so the panel cannot be enumerated from a published port.
  Its CSP is `script-src 'self'`; there is no inline script, and the gateway
  injects the API token for ingress requests, so the browser never sees it.
- The container drops all capabilities and `no_new_privs`, and the engine runs as
  UID 10001. `run.sh` starts as root only to fix `/data` ownership.

---

## Troubleshooting

| Symptom | Check |
| --- | --- |
| App stops immediately | Log shows `no Home Assistant token` → set `ha_token` or leave empty with `homeassistant_api: true`. |
| `no Home Assistant URL` | Set `ha_url` (or `WM_HA_URL`). |
| Panel loads, data missing | The panel calls `/api/v1` through the gateway, which injects the token for ingress peers. An empty `api_token` is fine for ingress. Check the engine log and `/healthz` instead. |
| Panel is blank | The image has no `ui/` directory (stale or hand-built image), or the CSP blocked an asset. Check the browser console; `/healthz` should still answer. |
| `status: degraded` | HA disconnected or inference backend missing; see `/api/v1/health` → `details`. |
| Watchdog restarts the app | `/healthz` must return 200; inspect engine logs for `error`. |
| Database permission error | Volume owned by another UID → set `WM_DROP_PRIVILEGES=false` (not recommended) or remove the volume. |

Full runbook: [`docs/deployment.md`](../docs/deployment.md) in the repository.

---

## Links

- Repository: <https://github.com/asxzy/wattmosaic>
- Architecture and contracts: `docs/architecture.md`, `docs/interface_contracts.md`
- Configuration reference: `docs/configuration.md`

License: MIT.
