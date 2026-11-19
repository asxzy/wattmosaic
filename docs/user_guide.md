# User Guide (contract 3.0)

This guide is about getting WattMosaic running, and about interpreting what it
tells you — and, just as importantly, what it refuses to tell you. The
algorithm rationale is in the
[technical handbook](../docs/specs/realtime-topology-disaggregation/handbook.md)
and the [design spec](../docs/specs/realtime-topology-disaggregation/design.md).

- **Part 1 — Install and first run** (§0): from a fresh install to your first device value.
- **Part 2 — Reading the output** (§1 onward): what each status, bound and explanation means.

## 0. Install and first run

### 0.1 What has to be running

| Piece | What it does | Where it runs |
| --- | --- | --- |
| **Engine** | Talks to Home Assistant, measures, solves, learns. Owns the database. | A container (HA OS App or Docker) |
| **Integration** | Displays engine results as HA entities. Sends your commands. | Inside Home Assistant |

The integration is deliberately thin: it computes nothing. If the engine is not
running, the integration loads but its entities go `unavailable`. That is by
design, not a bug. Engine and integration speak **contract 3.0.0** and must be
upgraded together.

```text
Home Assistant  ──read-only WebSocket──►  Engine  ◄──HTTP──  Integration
      ▲                                        │
      └──────── sensors / group sensors ◄──────┘
```

### 0.2 Install the engine first

**Home Assistant OS App** — see [`installation.md` §A](installation.md#a-home-assistant-os-app).

**Standalone Docker** — this is the easier option if the engine lives on a
different machine than HA:

```bash
git clone https://github.com/asxzy/wattmosaic.git
cd wattmosaic
cp .env.example .env
$EDITOR .env      # set HA_HOST, HA_TOKEN, WM_API_TOKEN
docker compose up -d --build
```

Confirm the engine can see Home Assistant:

```bash
curl -s -H "X-API-Token: $WM_API_TOKEN" http://<engine-host>:8787/api/v1/health
```

You want `"status": "ok"` and `"ha_connected": true`. **Do not continue until
you have both.** The token only needs read access; WattMosaic contains no
service-call path and cannot actuate devices.

### 0.3 Install the integration from the Marketplace

Since Home Assistant **2026.11**, HACS has been replaced by the built-in
**Marketplace**:

1. **Settings → Marketplace → Add from link** → `https://github.com/asxzy/wattmosaic`
2. Choose type **Integration**, then **Add** and **Install**.
3. Restart Home Assistant when prompted.

<details>
<summary>On Home Assistant versions before 2026.11 (HACS)</summary>

Add `https://github.com/asxzy/wattmosaic` as a custom repository of type
*Integration* in HACS, then install **WattMosaic** and restart. HACS requires the
repository to be public.

</details>

### 0.4 Point the integration at the engine

1. **Settings → Devices & services → Add integration** → **WattMosaic**.
2. Enter the **engine URL** the *HA host* can reach (not `localhost` unless the
   engine runs on the same machine).
3. Enter the **API token** if you set `WM_API_TOKEN`.
4. Choose a poll interval; the defaults are fine to start.

A failed connection almost always means wrong host/port, engine not running, or a
token mismatch.

### 0.5 Your first five minutes

The engine is event-driven: it needs to observe real state changes before it has
anything honest to say. Expect this progression — **it is normal, not a fault**:

| When | What you will see | Why |
| --- | --- | --- |
| Immediately | Global entities populate; per-device power mostly `unknown` | No measurements/events yet |
| Minutes | `MEASURED` values appear for confirmed single-subject endpoints | A smart plug / appliance monitor reads its subject directly |
| Hours | `ESTIMATED` values appear for identifiable devices | Joint inference needs isolated state events |
| Days | Group estimates and model-backed estimates appear | Learning and matching need repetitions |
| Ongoing | Some devices stay `unknown` forever | Nothing can attribute them — see §5 |

- **Whole-home total** should match your incoming mains. If not, fix endpoint
  roles/phases before looking at any device value.
- **A device power sensor reading `unknown`** means the engine has no
  identifiable value — not that the device is off. See §2.
- **Per-device `unavailable`** usually means the engine is unreachable or was
  just restarted.

Open the **WattMosaic** sidebar panel (the App's ingress page) to see the same
values with the evidence behind each one: status, identifiability, bounds,
version ids and the reasons a device was placed where it was. The panel is
read-only — it never changes a device.

### 0.6 The two things to configure first: endpoints and topology

The engine cannot guess which endpoint measures what.

1. Accept the HA power entities that are genuine measurement endpoints. Only
   accepted endpoints are ingested.
2. For a smart plug or appliance monitor that physically measures **one** device,
   confirm its `SINGLE_SUBJECT` scope. Only then can it produce `MEASURED`.
3. Review the typed topology edges: a single event never promotes an edge past
   `candidate`, and `provisional` edges are soft. Pin the ones you are certain
   about; reject the ones that are wrong.

### 0.7 If something looks wrong

| Symptom | First thing to check |
| --- | --- |
| All entities `unavailable` | Engine reachable from HA? `GET /api/v1/health` from the HA host |
| Total power is wrong | Endpoint roles/phases — mains must be incoming feeds, not branches |
| Everything is `unknown` | No accepted endpoints, no events, or no confirmed scope/edges |
| A device is always `unknown` | Probably genuinely unattributable — read §5 |
| Integration will not install | Repository must be public; check the Marketplace error text |

Full troubleshooting: [`troubleshooting.md`](troubleshooting.md).

### 0.8 Uninstalling

Removing the integration deletes its entities and config entry only. The engine
and its database are separate — see [`installation.md` §Uninstall](installation.md#uninstall).
Your learned history lives in the engine database, not in HA.

## 1. The core idea

Your home has one honest total: the incoming mains. It also has many unknowns:
which appliance contributed which watts. WattMosaic narrows that gap using
everything measurable, and is explicit about what remains unexplained.

The governing rule:

> **A plausible wrong number is worse than an honest gap.**

## 2. Reading a device's power value

Each device estimate carries several independent signals, exposed as separate
entities or attributes. The power sensor's state follows one rule: it shows the
point value only when the estimate is `MEASURED`, or `ESTIMATED` and
`IDENTIFIABLE`.

| Signal | Shows | Purpose |
| --- | --- | --- |
| `sensor.<device>_power` | watts or `unknown` | The estimate when publication is licensed |
| `estimate_status` | `MEASURED` / `ESTIMATED` / `ALLOCATED` / `UNKNOWN` | What kind of value this is (HB §8.5) |
| `identifiability` | `identifiable` / `partially_identifiable` / `group_identifiable` / `non_identifiable` / `insufficient_evidence` | Whether the math separated a value (HB §8.4) |
| `lower_w`, `upper_w`, `bounds_basis` | justified interval | What the value could be, and why |
| `group_id` | stable id | Which group sum this device belongs to, if any |
| version ids | topology / mapping / model | Which versions produced the value |
| confidence components | seven `[0,1]` or `None` scores | How well supported — **not** a probability |

### Estimate status — *what kind of value is it*

| Status | Meaning | Trust |
| --- | --- | --- |
| `MEASURED` | A confirmed single-subject endpoint read exactly one device or component. | Direct reading; one source reader. |
| `ESTIMATED` | Solved from endpoint measurements, state events and a validated model. | Depends on identifiability and isolation. |
| `ALLOCATED` | A share of an identified group sum, with a stated allocation rule. | A group result, never invented per-member watts when inseparable. |
| `UNKNOWN` | Insufficient evidence. | No device value is invented; bounds and a reason are given. |

### Identifiability — *did the measurements actually separate this device?*

Identifiability is independent of status. A subject that shares a measurement
row with another, with no distinguishing evidence, is `non_identifiable`:
its power sensor reads `unknown` and only justified bounds are published. A
non-separable set is reported as a **group** (`group_identifiable`), and its
identified sum gets its own group sensor. Model priors are never used as bounds
and never turn an unidentifiable subject into a number.

### Confidence — *how well supported*

A seven-component vector (measurement quality, event association, model fit,
topology consistency, identifiability, sample coverage, freshness), each in
`[0,1]` or `None`. There is no single blended score. Do **not** read a component
of `0.8` as "80% chance this is exactly right" — it is an engineering signal, not
a calibrated probability. See [`docs/confidence_model.md`](confidence_model.md).

## 3. Whole-home entities

| Entity | What it means |
| --- | --- |
| Total incoming power | Sum of the confirmed incoming feed endpoints. Never the sum of branches. |
| Per-phase power | Per-phase incoming power, sign preserved. |
| Unknown / unattributed | Measured total minus everything attributed. **This is a feature.** |
| Residual per reader | Unexplained load on each endpoint (a Reader Residual row). |
| Data quality score | Aggregate endpoint health. |
| Engine status | `ok` / `degraded` / `error`. |

If "unknown" is large, that is the system being honest, not broken. Check for a
genuinely unmonitored load or a topology error.

## 4. Endpoints and topology

An endpoint is a CT, smart plug or appliance monitor that produces samples. It
has a declared scope, phase, polarity and a time-scoped configuration revision.

- **Scope**: a plug or appliance monitor whose scope is exactly one device or
  component can be set to `SINGLE_SUBJECT` and, once confirmed, yields
  `MEASURED`. A circuit CT never gets this scope automatically. A circuit feeding
  one appliance is still an ordinary branch row.
- **Phase / polarity / paired legs**: set `l1`, `l2`, `both` for a 240 V circuit;
  mark an inverted CT as `inverted`; pair the two legs of a double-pole load so
  it is modelled once.
- **Nesting**: typed parent/child edges are inferred from containment evidence
  and versioned. Physically trace the panel; never infer nesting from names.
  A single event never promotes an edge past `candidate`.
- **Independent versions**: moving a plug never touches a power model.

## 5. Describing your devices

WattMosaic asks you to describe each device on three independent axes. None is
guessed from a name.

### Axis 1 — Is it connected to Home Assistant?

`connected` (HA has an entity), `not_connected` (non-smart; only its endpoint sees
it), or `unknown`. A non-smart device on a circuit of its own is still solvable;
it simply has no control signal to corroborate it.

### Axis 2 — How closely is it monitored?

`shared` (a sub-meter carries this device plus others), `home_total_only`
(no sub-meter; only the incoming monitor sees it), `unmonitored`, or `unknown`
(derived from the endpoint graph). There is no private-measurement level.
Declaring `shared` makes the row an upper bound, so a 40 W lamp on a 300 W
circuit is reported as 40 W (a configured prior), not 300 W.

### Axis 3 — How does power relate to its control?

`fixed`, `variable`, `complex` (a multi-state controller whose signal may not
prove load), or `unknown`. For `complex`, the mode entity is context only:
runtime must come from a declared load entity or the endpoint. A mode such as
`heat_cool` never unlocks a watt value.

### What counts as "running"

Only an entity describing **runtime** can tell WattMosaic a device is on. A power
sensor is a measurement, never a state entity — a bridge blip would otherwise
manufacture two false transitions. A confirmed controller label mapped to `load`
is the one assertion that may supply watts on its own.

## 6. Mapping and the matrix

WattMosaic proposes device↔endpoint mappings from measured evidence, never from
name similarity. The **mapping matrix** (`/api/v1/matrix` and the panel's Matrix
view) is a device-by-reader grid with drill-down: each cell carries structured
mapping and contribution evidence. A proposal stays a candidate until confirmed;
a wrong confirmation is worse than none, so leave uncertain ones unconfirmed.

## 7. Learning

Learning happens automatically from normal operation. A good learning sample
has a device state change, a matching endpoint delta, no other device changing
at the same time, and fresh measurements. Samples that fail these tests are
recorded with a reason (`concurrent_events`, `stale_measurement`,
`ambiguous_topology`, `insufficient_magnitude`, and so on).

Legacy models fitted on whole-circuit power are retired to read-only; new models
are retrained from device-attributed **delta** evidence, and a model is promoted
only when it beats the active one on held-out data. A device may stay
`model_status: pending` for a long time — that is correct if its usage is always
concurrent with other loads.

## 8. Groups

Where several devices toggle together, their individual watts are not
identifiable. They are reported as one **device group** with an identified sum
and justified bounds; members keep `None`. Each group gets its own power sensor
on a synthetic "WattMosaic group" device. Two true numbers beat six invented
ones.

## 9. Understanding the residual

```text
unknown_residual = main_power − Σ estimated/measured − Σ group sums − Σ unknown
```

It is never divided among devices, and a large residual usually means a load has
no endpoint and no model (expected), or topology is wrong. Use
`GET /api/v1/snapshot`, `/residuals` and the per-estimate evidence to see what
supported each value.

## 10. Using the services

| Service | Effect | Confirmation |
| --- | --- | --- |
| `start_learning` / `pause_learning` / `resume_learning` | Control learning; inference continues. | No |
| `accept_sample` / `reject_sample` | Curate a learning sample. | Reject requires `confirm: true` |
| `rebuild_model` / `reset_device_model` | Retrain or discard a model. | Reset requires `confirm: true` |
| `freeze_model` / `unfreeze_model` | Protect a model from promotion. | No |
| `run_diagnostics` | Produce a health report. | No |
| `export_device_data` | Export results, samples, model versions. | No |

`reset_device_model` is destructive to learned state; export first if you care
about the history.

## 11. Common questions

**Why is a device's power `unknown` when it is clearly on?**
Either it has no confirmed single-subject endpoint, or its evidence is
confounded / non-identifiable. Check `estimate_status`, `identifiability` and the
bounds in `/api/v1/devices/{id}`.

**Why did my HA history get gaps where it used to show 0 W?**
Unknown now publishes as `unknown` rather than a fabricated 0 W (DEC-25). HA
records a gap; past history is not rewritten. Energy integration never integrates
a null.

**Can I trust the numbers for billing?**
No. Use a confirmed measurement endpoint's energy for accounting. Estimated
values are estimates with stated limitations.

**Why does the total not match the sum of devices?**
It should not: the difference is the residual and the unknown load, by design.
