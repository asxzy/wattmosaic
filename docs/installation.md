# Installation

WattMosaic is two installable pieces:

| Piece | Installed how | Required? |
| --- | --- | --- |
| **Engine** (App or Docker) | Below, §A or §B | Yes — does all the work |
| **Integration** (`custom_components/wattmosaic`) | Marketplace / HACS, §D | Yes — displays results in HA |

Install the **engine first**: the integration is a thin client and has nothing to
show without it.

Three supported deployment shapes for the engine. All run the same engine code
and the same database migrations.

- **A. Home Assistant OS App** — primary, recommended for HA OS users.
- **B. Standalone Docker** — for a separate host or a non-HA-OS install.
- **C. Local development** — for contributing and for the simulation harness.
- **D. Integration via Marketplace / HACS** — installs the HA display client.

For a guided first-run walkthrough with expected timings, see
[`user_guide.md` §0](user_guide.md#0-install-and-first-run).

---

## Prerequisites

1. A Home Assistant instance reachable over HTTP(S), with a **long-lived access
   token** (Profile → Security → Long-lived access tokens).
2. At least one power-measuring integration (a multi-channel CT energy monitor
   in a typical setup).
3. For the App: Home Assistant OS with the App store, and access to
   `/addons` (Samba share, SSH, or the File editor).
4. For Docker: Docker Engine 24+ and Docker Compose v2.

The token only needs read access. WattMosaic contains no service-call code path,
so it cannot actuate devices even if granted broader rights.

---

## A. Home Assistant OS App

### 1. Install the App

**From the app store (recommended).** Add this repository:

```text
https://github.com/asxzy/wattmosaic
```

via **Settings → Apps → Install app → ⋮ → Repositories**, then install
**WattMosaic Engine**. The App uses a prebuilt multi-arch image
(`ghcr.io/asxzy/wattmosaic-engine:<version>`, `amd64` + `aarch64`), so nothing is
compiled on your hardware.

> **Image version.** The Supervisor always pulls `<image>:<version>` where
> `version` is the `version:` field in `addon/config.yaml`. Every version a push
> publishes gets a matching multi-arch image in the same workflow, so the App
> announces itself and step 4 is automatic. If a version is ever offered before
> its image is pullable, install it by hand via step 4 and it will announce
> itself on the next start.

**From a local copy.** If you cannot reach GitHub Container Registry, stage a
self-contained folder and copy it into the local-apps directory:

```bash
scripts/package_addon.sh
scp -r dist/addon/wattmosaic_engine root@homeassistant:/local_apps/
```

Home Assistant 2026.11 renamed that mount from `/addons` to `/local_apps`
(exposed as the **local_apps** Samba share); older releases still use
`/addons`. `scripts/package_addon.sh --install` probes for the right one.

The staged folder carries a local copy of the engine package and has the
prebuilt `image:` reference removed, so the Supervisor builds it on your machine
instead of pulling. Then **Settings → Apps → ⋮ → Check for updates**.

Copying `addon/` directly is **not** enough: it is the repository's app
definition, not a build context, and it deliberately has no `engine/` inside.

### 2. Configure

Open the App → **Configuration** and set:

```yaml
ha_url: http://homeassistant:8123
ha_token: <long-lived access token>
api_token: <random token, used by the integration>
learning_enabled: true
stale_after_s: 120
inference_interval_s: 5
database_path: /data/wattmosaic.db
retention_days_raw: 30
log_level: INFO
```

The App runs on the Supervisor network and can reach Home Assistant at
`http://homeassistant:8123` (no port surprises). The database lives in `/data`,
which survives App updates and is included in HA backups.

### 3. Start and verify

Start the App, then open its **Log** tab. You should see:

```text
engine_starting  contract_version=1.4.0
database_ready   schema_version=<latest>
ha_connected
api_listening    host=0.0.0.0 port=8787
```

From the App's ingress panel (or `http://<ha>:8787/api/v1/health`) you should get:

```json
{"status": "ok", "ha_connected": true, "database": "ok"}
```

Click **Open Web UI** (ingress) to see the WattMosaic panel. It opens on an
overview of engine health and the live power split, with tabs for **Devices**
(latest estimate, evidence grade and measurement age per device), **Topology**
(candidate parents, aliases and what could not be inferred), **Data quality**
(per-channel cadence, gaps and ages) and **Diagnostics** (aligner state, event log,
JSON export). It is read-only apart from confirming the topology the engine has
already inferred.

### 4. Install the integration

Use the Marketplace (§D) — that is the supported path. For a manual copy instead:

```bash
scp -r custom_components/wattmosaic/ root@homeassistant:/config/custom_components/
```

Restart Home Assistant, then **Settings → Devices & Services → Add Integration →
WattMosaic**.

#### The App announces itself (no address to type)

When the engine App is running, Home Assistant should already show a
**Discovered** card for WattMosaic: **Settings → Devices & Services** lists
"WattMosaic Engine" under *Discovered*. Click **Configure** and confirm one
screen; the engine URL is filled in for you.

How it works, and what to check if the card does not appear:

- The App lists `wattmosaic` under `discovery:` in `addon/config.yaml` — the
  Supervisor refuses any service name an app has not declared, so this list is
  the permission.
- On start, `addon/discovery.py` waits for the engine's `/healthz`, then POSTs
  `{"service": "wattmosaic", "config": {"host": …, "port": 8787}}` to the
  Supervisor. The announced host is taken from the Supervisor's own record of
  the app (`GET /addons/self/info`), falling back to the container hostname and
  then to the slug rule (`_` → `-`). Locally installed apps get a repository-hash
  prefix, which is exactly the part users cannot guess.
- Home Assistant turns the message into a config flow, which **probes the engine
  before creating anything**. A stale announcement (app removed, engine down)
  therefore produces the normal "cannot connect" error, never a broken entry.
- The card survives dismissal only indirectly: Home Assistant re-processes every
  stored discovery message on restart (`hassio_discovery.py`,
  `_async_discovery_start_handler`), and the Supervisor re-pushes a message
  after the App restarts. So the card comes back after a Home Assistant or App
  restart. It is also re-offered when the config entry is deleted
  (`signal_discovered_config_entry_removed`). If you would rather not use it at
  all, add the integration manually with the values below. Discovering an engine
  that is already configured aborts with "already configured" rather than
  creating a duplicate.

Nothing secret is announced: only a host name and a port, on the Supervisor's
private network.

#### Adding it by hand

If you prefer the manual flow (or the discovery card was dismissed), enter:

- **Engine URL**: the app's **Hostname** from its info tab, e.g.
  `http://f48de52e-wattmosaic-engine:8787` for an app-store install or
  `http://local-wattmosaic-engine:8787` for a local copy — the
  config flow validates the connection and rejects a contract mismatch.
- **API token**: the same `api_token` from step 2.

Both paths produce the same config entry: the discovery step normalises the
announced URL through the same rule as the manual field, so the two cannot
differ by a trailing slash.

### 5. Confirm topology

The engine discovers a candidate topology for you the first time it starts with
an empty database, and again on the first inference cycle if Home Assistant was
not reachable at startup. It writes:

- every power channel published by a **multi-channel energy monitor** as a CT
  channel (a lone smart plug is not CT hardware, so it is never turned into one);
- one device per circuit, with double-pole 240 V loads detected from a matching
  `*_ct1` / `*_ct2` pair;
- the panel's incoming feed pair as `main`;
- polarity from the **observed sign**: a channel reading negative is recorded as
  `inverted` with the evidence in `metadata.polarity_evidence`. Without this a
  reversed CT is clipped to zero and published as a good 0 W measurement.
  Because this is a single snapshot, treat it as a candidate and confirm it.

Discovery is **structural**, not brand-based: a hardware device that exposes
several power entities is treated as the monitor. Roles come from position within
the monitor (the shortest paired `ct1`/`ct2` stem is the panel feed), never from
the word "main" in a name — a load called `main_area_ac` stays a load.

The result is always a **candidate**, never a confirmed topology. Review it in
the integration options:

1. Assign each CT a role (`main` / `branch` / `dedicated_device` / `unknown`).
2. Set the phase (`l1` / `l2` / `both`) and polarity.
3. Pair double-pole legs if they were not detected.
4. Confirm or reject each device→CT candidate.
5. Link each device to the Home Assistant entity that reports whether it is on
   (`switch.*`, `climate.*`, `binary_switch`). Discovery never guesses this link.

Only after confirmation should you treat `direct_measured` values as
measurements rather than candidates.

If `GET /api/v1/health` shows `"topology": {"configured": false}`, discovery
found no monitor and the reason is in `topology.reason`. The engine reports
`degraded` in that state rather than `ok`: it is running, but has nothing to
attribute power to.

### 6. Updating

Replace `addon/` contents, click **Check for updates**, then **Update**. The
database migrates forward automatically. Never downgrade a database file to an
older engine without taking a backup first — migrations are forward-only.

---

## B. Standalone Docker

```bash
git clone <repository> wattmosaic
cd wattmosaic
cp .env.example .env
$EDITOR .env            # set HA_URL, HA_TOKEN, WM_API_TOKEN

docker compose up -d --build
docker compose logs -f wattmosaic
```

The compose file:

- stores the database in a named volume,
- runs as a non-root user,
- exposes `8787`,
- has a `healthcheck` against `/api/v1/health`,
- restarts unless stopped.

Set `WM_DATABASE_PATH=/data/wattmosaic.db` and the volume mounts `/data`.

Verify:

```bash
curl -s -H "X-API-Token: $WM_API_TOKEN" http://localhost:8787/api/v1/health | jq
```

Then install `custom_components/wattmosaic/` into Home Assistant as in step A4,
pointing the integration at the Docker host and port.

### Building the image directly

```bash
docker build -t wattmosaic-engine:<version> .
docker run -d --name wattmosaic \
  --env-file .env \
  -e WM_DATABASE_PATH=/data/wattmosaic.db \
  -v wattmosaic-data:/data \
  -p 8787:8787 \
  wattmosaic-engine:<version>
```

---

## C. Local development

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env        # fill in real values
wattmosaic-engine --check-config   # validates without connecting
wattmosaic-engine                  # runs the engine
```

Run the tests:

```bash
pytest -q
```

Run the synthetic simulator (no Home Assistant required):

```bash
python -m tools.simulator.cli --seed 42 --minutes 120 --out data/sim
```

---

## D. Integration via Marketplace / HACS

The integration is published as a GitHub repository. Home Assistant installs it
from the repository; there is no PyPI package and no Python requirement.

### D.1 Home Assistant 2026.11 and newer — Marketplace

HACS was replaced by the built-in **Marketplace** in 2026.11. It ships with Home
Assistant and needs no installation.

1. **Settings → Marketplace**, accept the community-content warning once.
2. **Add from link** (top-right) → enter
   `https://github.com/asxzy/wattmosaic` → type **Integration** → **Add**.
3. Open **WattMosaic** → **Install**.
4. Restart Home Assistant when prompted.

The Marketplace creates an update entity, so future releases appear under
**Settings → Updates** automatically.

### D.2 Home Assistant before 2026.11 — HACS

HACS still works on older versions:

1. **HACS → Integrations → ⋮ → Custom repositories**.
2. URL `https://github.com/asxzy/wattmosaic`, category **Integration**, **Add**.
3. Find **WattMosaic** → **Download** → restart Home Assistant.

HACS and Marketplace both require the repository to be **public**.

### D.3 Manual install

```bash
# into /config/custom_components/wattmosaic/
scp -r custom_components/wattmosaic/ root@homeassistant:/config/custom_components/
```

Restart Home Assistant. You will not receive automatic updates.

### D.4 After installing: configure

**Settings → Devices & Services → Add Integration → WattMosaic**, then supply the
engine URL (reachable *from* Home Assistant) and the engine API token. When the
engine runs as an App, take the URL from the app's info tab: the hostname is
`f48de52e-wattmosaic-engine` for an app-store install, or
`local-wattmosaic-engine` for a local copy, both on port `8787`.

Then confirm the CT topology — see §A5.

### D.5 Updating and removing

- **Update:** Marketplace shows an update entity; HACS shows an update badge.
  Restart HA afterwards.
- **Remove:** uninstall the repository, then delete the integration entry. This
  removes entities and the config entry only; the engine database is untouched.

### D.6 Publishing notes (maintainers)

`hacs.json` at the repository root declares the Marketplace metadata, and
`custom_components/wattmosaic/brand/` provides the icon and logo. Integration
releases are ordinary git tags; `manifest.json` carries the version.

#### Release channels

There are two channels, and they are separate installs rather than a toggle:

| Channel | Git ref | App version | Integration version | Intended for |
| --- | --- | --- | --- | --- |
| Stable | `stable` | `x.y.z` | `x.y.z` | normal use |
| Development | `main` | `x.y.z-dev.N` | `x.y.z-dev.N` | testing a fix before it is stable |

**The App** distinguishes the channels by repository URL. The Supervisor accepts
a `#branch` suffix, and because the repository id is the SHA-1 of the whole URL,
`https://github.com/asxzy/wattmosaic#main` resolves to a *different* repository
id than `https://github.com/asxzy/wattmosaic#stable`. That gives the development
App its own slug, its own `/data` volume and its own sidebar entry, so installing
a development build cannot overwrite a stable one. Add it as a second app
repository and install it alongside the stable app.

**The integration** cannot pin a branch: the Marketplace extracts `owner/repo`
from the URL and rejects `#`. Install a development build explicitly instead —
in the Marketplace repository page use **Install another version** and pick the
newest prerelease tag. Enable the beta/pre-release toggle to be offered these
updates automatically; without it the Marketplace offers only the newest stable
Release, which is why a development release can never reach a stable install.

Every push to `main` publishes one prerelease, so development installs move
without a manual bump. See *Releasing* in `docs/deployment.md` for the pipeline.

A version already present in GHCR is never rebuilt unless the workflow is
dispatched with `force`, so a published tag is immutable.

---

## Verifying a fresh install

| Check | Command | Expected |
| --- | --- | --- |
| Config valid | `wattmosaic-engine --check-config` | `config ok`, redacted values |
| Database migrations | engine start log | `database_ready schema_version=N` |
| HA reachable | engine start log | `ha_connected` |
| API alive | `GET /api/v1/health` | `status: ok`, `ha_connected: true` |
| Contract match | response header | `X-WattMosaic-Contract-Version: 1.1.0` |
| Integration loaded | HA logs | `Setting up wattmosaic` |
| Entities present | HA developer tools | `sensor.wattmosaic_*` |
| Topology | `GET /api/v1/topology` | channels listed, `status: candidate` |

---

## Uninstall

1. Remove the integration in Home Assistant (Settings → Devices & Services →
   WattMosaic → Delete). This only removes entities and the config entry.
2. Stop and uninstall the App (or `docker compose down`).
3. The database is intentionally preserved in `/data` (or the named volume).
   Delete it manually only if you want to discard history and learned models.

---

## Backup and restore

- **HA OS App**: included in Home Assistant snapshots (the `/data` volume). For
  a manual copy:
  ```bash
  docker exec app_f48de52e_wattmosaic_engine \
    sqlite3 /data/wattmosaic.db ".backup '/data/backup.db'"
  ```
- **Docker**: back up the named volume, or use `.backup` as above.
- **Restore**: stop the engine, replace `wattmosaic.db`, start the engine. It runs
  `PRAGMA integrity_check` and refuses to start on corruption rather than
  silently discarding data.

See also [`deployment.md`](deployment.md) for App internals and security notes,
and [`troubleshooting.md`](troubleshooting.md) if something does not come up.
