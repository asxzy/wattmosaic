# User Guide

This guide is about getting WattMosaic running, and about interpreting what it
tells you — and, just as importantly, what it refuses to tell you.

- **Part 1 — Install and first run** (§0): a step-by-step walkthrough from a
  fresh repository to your first device value.
- **Part 2 — Reading the output** (§1 onward): what every number, grade and
  explanation actually means.

## 0. Install and first run

This section is the shortest path from nothing to a working WattMosaic. If you
only read one section of this guide, read this one.

### 0.1 What has to be running

WattMosaic is two pieces, and they have different jobs:

| Piece | What it does | Where it runs |
| --- | --- | --- |
| **Engine** | Talks to Home Assistant, measures, solves, learns. Owns the database. | A container (HA OS App or Docker) |
| **Integration** | Displays engine results as HA entities. Sends your commands to the engine. | Inside Home Assistant |

The integration is deliberately thin: it computes nothing. If the engine is not
running, the integration loads but its entities go `unavailable`. That is by
design, not a bug.

```text
Home Assistant  ──read-only WebSocket──►  Engine  ◄──HTTP──  Integration
     ▲                                        │
     └──────── sensors / services ◄───────────┘
```

### 0.2 Install the engine first

The engine must exist before the integration is worth adding. Install it with
one of the two supported shapes — both run identical code.

**Home Assistant OS App.** This requires filesystem access to your HA host
(Samba share, SSH, or the File editor). See
[`installation.md` §A](installation.md#a-home-assistant-os-app).

**Standalone Docker.** This is the easier option if the engine will live on a
different machine than HA:

```bash
git clone https://github.com/asxzy/wattmosaic.git
cd wattmosaic
cp .env.example .env
$EDITOR .env      # set HA_HOST, HA_TOKEN, WM_API_TOKEN
docker compose up -d --build
```

Then confirm the engine is alive and can see Home Assistant:

```bash
curl -s -H "X-API-Token: $WM_API_TOKEN" \
  http://<engine-host>:8787/api/v1/health
```

You want `"status": "ok"` and `"ha_connected": true`. **Do not continue until
you have both.** The most common first-run failure is an engine that is up but
cannot reach HA, which produces a working-but-empty integration.

> The token only needs read access. WattMosaic contains no service-call path,
> so it cannot actuate devices even if you grant broader rights.

### 0.3 Install the integration from the Marketplace

Since Home Assistant **2026.11**, HACS has been replaced by the built-in
**Marketplace**. It installs the same kind of GitHub repositories, and it is
already present on your system — there is nothing to add first.

1. Go to **Settings → Marketplace**.
2. Accept the community-content warning the first time you open it.
3. Select **Add from link** (top right) and enter:
   `https://github.com/asxzy/wattmosaic`
4. Choose type **Integration** if asked, then **Add**.
5. Open **WattMosaic** in the list and select **Install**.
6. Restart Home Assistant when prompted.

The Marketplace keeps the integration updated and shows an update entity under
**Settings → Updates**.

<details>
<summary>On Home Assistant versions before 2026.11 (HACS)</summary>

Add `https://github.com/asxzy/wattmosaic` as a **custom repository** of type
*Integration* in HACS (**HACS → Integrations → ⋮ → Custom repositories**), then
install **WattMosaic** and restart. Note that HACS requires the repository to be
public.

</details>

<details>
<summary>If you cannot or will not use the Marketplace</summary>

Copy `custom_components/wattmosaic/` into your HA configuration directory as
`/config/custom_components/wattmosaic/`, then restart Home Assistant. This is
the manual path; you will not receive updates automatically.

</details>

### 0.4 Point the integration at the engine

1. Go to **Settings → Devices & services → Add integration**.
2. Search for **WattMosaic**.
3. Enter the **engine URL**, e.g. `http://192.168.1.10:8787`. Use the address
   the *HA host* can reach — not `localhost`, unless the engine runs on the same
   machine.
4. Enter the **API token**, if you set `WM_API_TOKEN` on the engine.
5. Choose a poll interval. The defaults are fine to start.

If the URL or token is wrong, the setup form tells you which. A failed
connection at this step almost always means one of: wrong host/port, engine not
running, or the API token does not match.

### 0.5 Your first five minutes

After the integration loads, the engine has to observe your home before it has
anything honest to say. Expect this progression — **it is normal, and it is not
a fault**:

| When | What you will see | Why |
| --- | --- | --- |
| Immediately | Global entities populate; per-device power mostly `0` or `unknown` | No measurements yet |
| Minutes | Per-device values appear for devices with a dedicated CT | A CT measures them directly |
| Hours | `inferred` values appear for devices with a state entity | The engine needs to correlate state with power |
| Days | `model_estimated` values for repeatable devices | Learning needs repetitions |
| Ongoing | Some devices stay `unknown` forever | Nothing can attribute them — see §5 |

Concretely, at first:

- **Whole-home total** should already match your mains CT. If it does not,
  your CT topology is wrong — fix that before looking at any device value.
- **Directly measured** appears first and is the most trustworthy subset.
- **Per-device `unavailable`** usually means the engine is unreachable or was
  just restarted, not that the device is gone.

To watch this happen, open the **WattMosaic** sidebar panel (the App's ingress
page; see [`installation.md`](installation.md) §A). It shows the same values the
entities report, but with the evidence behind each one: the tier, the measurement
age, and the reasons the engine placed a device where it did. **Devices** and
**Data quality** are the useful ones in the first hour; **Diagnostics** is for
when something looks wrong. The panel is read-only — it never changes a device.

### 0.6 The one thing to configure first: your CT topology

The engine cannot guess which CT measures what. On a fresh install the topology
is a **candidate**, and candidate topology is treated as uncertain on purpose.

1. Open the WattMosaic integration → **Configure**.
2. Review each CT: its role (main / branch / dedicated), its phase, and the
   device it points at.
3. Confirm the topology explicitly. Until you do, results carry a topology
   uncertainty penalty.

See §4 for what the roles mean, and §6 for mapping devices.

### 0.7 If something looks wrong

| Symptom | First thing to check |
| --- | --- |
| All entities `unavailable` | Engine reachable from HA? `GET /api/v1/health` from the HA host |
| Entities present but `0` | Is `ha_connected: true`? Is at least one CT producing values? |
| Total power is wrong | CT roles/phases — mains must be `main`, not `branch` |
| Everything is `tier_0_unknown` | No topology confirmed, or no state entities mapped |
| A device is always `unknown` | Probably genuinely unattributable — read §5 |
| Integration will not install | Repository must be public; check the Marketplace error text |

For deeper diagnosis use the **`run_diagnostics`** service (§10) and the logs.
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

Every device value carries three independent pieces of information. In Home
Assistant each one is a separate entity, so you can put the power value on a
dashboard and keep the two honesty signals next to it:

| Entity | Shows | Purpose |
| --- | --- | --- |
| `sensor.<device>_estimated_power` | Watts | The estimate itself |
| `sensor.<device>_measurement_source` | `direct_measured` / `inferred` / … | How the number was obtained |
| `sensor.<device>_confidence` | 0–100 | Engineering score, **not** a probability |
| `sensor.<device>_evidence_tier` | `tier_0_unknown` … `tier_4_verified` | What kind of evidence backs it |

The power sensor's attributes also carry `confidence_tier`, so a single gauge
can show the grade without adding another entity.

These entities are outputs. The engine never reads them back as measurements,
and topology discovery never offers them as candidate CT channels. Otherwise it
would be treating its own answer as independent evidence of the same circuit.
The exclusion is keyed on the entity registry's `platform` field, so a real CT
from another integration is unaffected.

### Source — *how* the number was obtained

| Source | Meaning | Trust |
| --- | --- | --- |
| `direct_measured` | A dedicated CT measures this device alone. | Highest. It is a measurement. |
| `inferred` | Solved from mains/branch CTs plus state events and topology. | Good when isolated; lower under concurrency. |
| `model_estimated` | The device's learned model, conditioned on its HA state. | Depends on model validation quality. |
| `configured_prior` | Only a value you stated (e.g. "this lamp is 40 W") supports it. | An assertion, not a measurement. |
| `unknown` | Not enough evidence. | No device value is invented. |

### Confidence tier — *what kind of evidence* supports it

An ordered grade, from weakest to strongest. This is the quickest way to judge a
number, and it never claims to be a probability.

| Tier | Meaning |
| --- | --- |
| `tier_0_unknown` | Nothing supports a device-level value; the power stays in the residual. |
| `tier_1_modelled` | A configured prior or a validated model only. No live confirmation. |
| `tier_2_inferred` | Separated from shared or bulk CT rows, but uncorroborated. |
| `tier_3_measured` | A CT measured it, or a declared load signal corroborates a shared estimate. |
| `tier_4_verified` | Measured **and** independently corroborated, on a fresh sample. |

Tier 4 needs three things at once: a CT measurement, a *declared* load signal (or
a model) agreeing with it, and a fresh sample. If your CT says 4.5 kW while your
declared "heating" sensor says off, the value stays at tier 3 and the result says
`dedicated_ct_disagrees_with_state:not_verifiable` — the measurement is real, the
attribution is what is in doubt.

### Confidence score — *how well supported* it is

A score from 0 to 1. It is an **engineering score, not a probability**. It
combines measurement freshness, number of supporting CTs, model validation,
event isolation, topology certainty and residual size.

The tier and the score answer different questions. The tier says *how the value
was obtained*; the score says *how much to trust the number right now*. They can
legitimately disagree — an old dedicated-CT reading keeps its tier 3 grade while
its score collapses — and when they do, the result carries a
`tier_score_divergence` reason rather than hiding it.

Do not read `confidence: 0.8` as "80% chance this is exactly right". Read it as
"the evidence for this estimate is strong in several independent ways".

## 3. Whole-home entities

| Entity | What it means |
| --- | --- |
| Total incoming power | Sum of the confirmed main CTs. Never the sum of branches. |
| L1 / L2 power | Per-phase mains, sign preserved. |
| Total directly measured | Sum of devices with their own CT. |
| Total inferred | Sum of solved device contributions. |
| Unknown / unattributed | Measured total minus what we could attribute. **This is a feature.** |
| Reconciliation error | Numerical imbalance between the model and measurement. Small is good; large means topology or stale data. |
| Data quality score | Aggregate channel health. |
| Engine status | `ok` / `degraded` / `error`. |

The *Unknown / unattributed* and *Total incoming power* sensors also carry two
attributes that say what the unknown part is made of — `unmonitored_w`
(genuinely unclamped load) and `meter_disagreement_w` (two meters on the same
wires disagreeing). See section 9a.

If "unknown" is large, that is the system being honest, not broken. Check
whether it is a genuinely unmonitored load (a branch with no CT) or a topology
error.

## 4. CT assignment

Each CT channel has four attributes that must reflect reality:

1. **Role** — `main` (service entrance), `branch` (a circuit), `dedicated_device`
   (one appliance), `unknown`.
2. **Phase** — `l1`, `l2`, `both` for a 240 V circuit, `unknown`.
3. **Polarity** — `normal`, or `inverted` if the CT is installed backwards.
4. **Parent** — which CT physically contains this one, if any.

Getting **nesting** right is the single most important thing you can do. If a
branch CT is also treated as a main, its load is counted twice.

### How to determine nesting

Physically trace the panel. A branch breaker feeds downstream of a main breaker.
Sub-panels are downstream of the main panel. Never infer nesting from name
similarity alone.

### Double-pole loads

A 240 V appliance (hot tub, EV charger, dryer, range) uses two breaker poles.
If you have one CT on each leg, they are two halves of *one* load:

- Pair them (`paired_ct_id`).
- Set `phase: both`.
- The device's power is the sum of both legs, modelled once.

If you model them as two devices, you will see two half-power devices and get
confusing cycle behaviour.

## 5. Describing your devices

WattMosaic can only claim what your configuration justifies, so it asks you to
describe each device on **three independent axes**. None of them is guessed from
a device name.

### Axis 1 — Is it connected to Home Assistant?

| Value | Meaning |
| --- | --- |
| `connected` | HA has an entity for it, and may be able to start it. |
| `not_connected` | Non-smart. No HA entity exists; only its CT sees it. |
| `unknown` | Not stated. |

A non-smart device is not a second-class device. If it sits on a dedicated CT it
is still `tier_3_measured` — it simply has no control signal to corroborate it,
so it cannot reach tier 4 until a model validates.

### Axis 2 — How closely is it monitored?

| Value | Meaning |
| --- | --- |
| `dedicated` | A CT measures this device and nothing else. |
| `shared` | A sub-meter carries this device plus others (e.g. one light on a `lights` circuit). |
| `home_total_only` | No sub-meter; only the home energy monitor sees it. |
| `unmonitored` | Nothing observes it; only a configured prior can supply a value. |
| `unknown` | Derived from the CT graph. |

`shared` is the important declaration. If you say a circuit is shared, WattMosaic
stops treating the whole row as this device's power: the row becomes an upper
bound, the unexplained remainder stays out of your device, and the device can
only report a **configured prior** (tier 1) — a 40 W lamp on a 300 W circuit is
reported as 40 W, not 300 W.

Leave it `unknown` and the level is derived from the CT graph: a dedicated CT is
`dedicated`, a branch CT with no sibling device is also `dedicated` in fact
(often several such devices exist), and a device with no attribution CT is
`home_total_only`.

### Axis 3 — How does power relate to its control?

| Value | Meaning |
| --- | --- |
| `fixed` | Constant watts while running or idle (always-on lamp, garage opener). |
| `variable` | Power follows a control value (dimmable light, variable-speed fan). |
| `complex` | A multi-state controller whose signal may not prove load (e.g. a thermostat fan, ventilation or humidifier stage). |
| `unknown` | Not stated. |

For `complex`, the mode entity is **context only**. It never contributes load and
never proves the appliance is running. Runtime must come from a declared load
entity (e.g. `binary_sensor.water_heater_zone_1_heating`) or from the CT. If you have
neither, the device honestly reports tier 0 rather than inventing watts.

### Advanced per-device settings

These are what actually raise confidence:

| Setting | Why it matters |
| --- | --- |
| Device type | Sets the expected behaviour and the prior watt envelope. |
| Light type (`led`, `halogen`, …) | Different technologies dim differently. |
| Bulb count | A five-bulb fixture at 9 W/bulb is 45 W, not 9 W. |
| Watts per bulb at full output | Makes a light's `on_w` computable without a model. |
| Expected on / idle / standby watts | A bounded prior for `fixed` and `shared` devices. |
| Controller state map | Per-label meaning; only a label marked `load` **and** confirmed may supply watts. |

Dimming is assumed **linear in output** unless you supply a measured
`dimming_curve`. That simplification is reported as `linear_dimming_assumed`, so
you always know when it is in play.

### A declaration is never evidence

Configuration can *explain* power; it cannot *observe* it. Nothing you type is
ever recorded as a measurement, and removing a configuration restores the device
to exactly its previous state — your typed entities do not linger as if they had
been measured evidence.

### What counts as "running"

Only an entity that describes **runtime** can tell WattMosaic a device is on. A
power sensor (`sensor.water_heater_power`) is a *measurement*, not a state: it reports
watts, and it reports `unavailable` whenever a bridge blinks. It is never written
into the device's state slot, because a blip would replace the real `on`/`off`
value and the next genuine reading would look like a transition — one dropout
would manufacture two transitions that never happened. The two kinds of link are
kept separate, and the status page reports how many power-entity events were held
out (`power_events_not_state`).

A confirmed controller label is the one assertion that may supply watts by
itself. Everything else needs a declared **load** entity that is positively on —
a mode such as `heat_cool` never unlocks a watt value, so a configured 4.5 kW
burner cannot report 4.5 kW for a setpoint that never changes.

### Where to enter it

Settings → Devices & Services → WattMosaic → **Configure**. The options flow
shows its usual polling settings plus a **"Configure one device"** checkbox
(off by default, so nothing changes unless you ask). Ticking it opens two extra
steps:

1. `device_config` — pick the engine device id from a list (or type one).
2. `device_config_details` — the three axes plus the advanced fields.

Submitting sends a single `set_device_configuration` command to the engine. To
remove a configuration, open the same device and submit it empty.

## 6. Device mapping

WattMosaic proposes device↔CT mappings from **measured evidence**, never from
name similarity. The mapping tool tests each candidate state entity against the
CT level it should gate: a state-gated level test scored by balanced accuracy,
with a lag sweep over 0–600 s, a coverage gate, a relocation null (the same test
run against a shuffled device), and a confound check that the entity is not
simply tracking the whole circuit rather than this appliance.

A proposal is a **candidate**, not a fact. Confirm the ones you are certain
about; leave uncertain ones unconfirmed. Unconfirmed topology reduces confidence
and is visible in diagnostics.

A wrong confirmation is worse than no confirmation, because the system will
solve confidently in the wrong direction. If the tool finds no honest state
signal for a device, the device stays unlinked — that is the correct outcome, not
a gap to fill.

## 7. Learning

Learning happens automatically from normal operation. You do not calibrate each
device by hand.

What makes a good learning sample:

- The device changed state (on/off, brightness, mode, speed).
- The nearest CT readings changed accordingly.
- No other device changed at the same moment.
- The measurements were fresh, not stale.

What gets rejected, and why you may see it:

| Rejection | Meaning |
| --- | --- |
| `concurrent_events` | Another device changed at the same time; the CT change cannot be attributed safely. |
| `stale_measurement` | The reading was older than the staleness threshold. |
| `unstable_reading` | The CT value was still moving; not a settled baseline. |
| `ambiguous_topology` | The device's CT relationship is unconfirmed. |
| `insufficient_magnitude` | The change was within measurement noise. |
| `excessive_noise` | Channel noise too high for the observed change. |

A device may stay at `model_status: pending` for a long time. That is correct if
its usage is always concurrent with other loads.

### 7a. Confirming the topology (this is what unblocks learning)

Learning samples are only accepted against a topology you have **confirmed**.
WattMosaic never confirms one itself: the panel tree it derives is a
*hypothesis*, and training a model on a guessed association would teach it
something false. Until you confirm, every sample is rejected as
`ambiguous_topology` — correctly — and no model can exist.

The **Active learning** binary sensor tells you which state you are in:

| State | Meaning |
| --- | --- |
| `on` | Learning is running **and accepting** samples. |
| `off` | Learning is not accepting samples — because it is paused/disabled, or because it is blocked. |
| `blocked` (diagnostics) | Running, but rejecting (nearly) all samples. The diagnostics download names the dominant reason and the remedy. |

If it reports `blocked` with `ambiguous_topology`, there is one action:

> Press **Confirm topology** — `button.wattmosaic_confirm_topology`, on the
> WattMosaic Engine device.

It confirms the CT mapping **already stored in your configuration**. It never
invents one, and it only marks the entry as confirmed when the engine itself
reports `confirmed` afterwards. A second press writes nothing, and the button
is unavailable once the engine is confirmed (there is nothing left to do).

What you should see afterwards, on the next poll:

1. The engine's topology status changes from `candidate` to `confirmed`.
2. `Active learning` becomes `on` once samples start being accepted (the first
   one may take an inference window or two).
3. `Model power` starts moving off 0 W as models are promoted.

If the button is unavailable, it says why in its attributes: no stored
declaration (run the setup wizard and confirm the mapping there), or the engine
is unreachable. Confirming a topology that does not match reality is worse than
leaving it unconfirmed — check the CT assignment first (section 4).

#### What "blocked" looks like, and how to tell blocked from dead

These are different failures and they need different actions:

| Engine `/health` → `details.learning_status` | What it means | Action |
| --- | --- | --- |
| `samples_seen: 0`, `samples_rejected: 0` | **Dead intake.** No state change ever reached the engine, so there is nothing to reject. | Check diagnostics → `state_links` and `devices_without_state_entity`. A link must resolve to a `binary_sensor` / `switch` / `climate` / `light`, never to a power or energy sensor. |
| `samples_seen: N > 0`, `samples_rejected: N`, dominant `ambiguous_topology` | **Alive and blocked.** Samples reach the filter and are refused because the topology is still a candidate — by design. | Press **Confirm topology** (above). |
| `samples_accepted > 0` | Learning works. | Nothing. |

Before `0.2.1-dev.13` a `device_links` entry could store a Home Assistant
*device-registry id* and resolve to no state entity at all, which produced the
first row above for days. The integration now expands it through the entity
registry and lists every link that resolved to nothing, with a reason
(`only_measurement_entities`, `device_not_in_registry`, `not_a_state_domain`,
`no_state_entities`, `engine_device_not_in_topology`), in
`diagnostics.devices_without_state_entity`. If that list is empty and
`samples_seen` is still 0, the device genuinely has not changed state — turn
something on and watch for a sample.

## 8. Operating stages

For multi-stage appliances (washer, dishwasher, dryer), WattMosaic tracks stages:
fill, heat, wash, rinse, spin, dry. Each stage carries its own power
characteristic and duration statistics.

Stages are reported as `identified`, `probable`, or `unknown`. If two stages have
overlapping power signatures and your CT cadence cannot separate them, the stage
is `unknown`. This is deliberate.

## 9. Understanding the residual

```
unknown_residual = main_power − direct_measured − inferred − model_estimated
```

It is never divided among devices. A large residual usually means one of:

- A load has no CT and no model (expected — that is the point of the residual).
- Topology is wrong (a CT is mis-roled or mis-nested).
- A device's model is stale and under-predicting.

Use `GET /api/v1/snapshot` and the per-device `explanation` field to see which
measurements supported each estimate.

### 9a. Why part of the residual may not be real load

If your home is metered twice on the same conductors (for example a main panel
CT and a second meter upstream of it), the two instruments never agree exactly.
Part of the residual is then **instrument disagreement**, not missing load.
Since contract 1.4.0 the snapshot splits it for you:

| Field | Meaning |
| --- | --- |
| `unknown_residual_w` | Unchanged: the whole gap. |
| `meter_disagreement_w` | Two CTs on the *same* wires disagree by this much. Nothing is missing. |
| `unmonitored_w` | The rest — genuinely unclamped load. |

`unmonitored_w + meter_disagreement_w == unknown_residual_w`, and the balance
identity `accounted + unknown_residual == main_power_w` still holds exactly.
Nothing is re-estimated: the published total is the same number as before, only
explained. Both values are attributes on the *Unknown / unattributed* and *Total
incoming power* sensors.

The disagreement is `0` — and no claim is made — unless both meters are fresh,
on the same phase, and actually aliased to each other. On the reference
installation the residual is ~600 W, of which the median disagreement is 164 W;
~470 W is genuine unclamped load.

## 10. Using the services

| Service | Effect | Confirmation |
| --- | --- | --- |
| `start_learning` / `pause_learning` / `resume_learning` | Control the learning loop; inference continues. | No |
| `accept_sample` / `reject_sample` | Curate a specific learning sample. | Reject requires `confirm: true` |
| `rebuild_model` | Retrain one device from stored samples. | No |
| `freeze_model` / `unfreeze_model` | Protect a good model from automatic promotion. | No |
| `reset_device_model` | Discard learned models for a device. | Requires `confirm: true` |
| `rebuild_topology` | Re-derive topology candidates (does not confirm them). | Requires `confirm: true` |
| `run_diagnostics` | Produce a full health report. | No |
| `export_device_data` | Export raw results, samples, model versions. | No |

`reset_device_model` and `rebuild_topology` are destructive to learned state.
Export first if you care about the history.

## 11. Common questions

**Why is a device's power zero when it is clearly on?**
Either it has no CT and no model yet, or its evidence is confounded. Check
`/api/v1/devices/{id}` for `source` and `explanation`.

**Why did confidence drop suddenly?**
Usually channel staleness (a CT communication hiccup) or a concurrent event.
Check the data quality report.

**Can I trust the numbers for billing?**
No. Use the directly measured CT energy for accounting. Inferred values are
estimates with stated limitations.

**Why does the total not match the sum of devices?**
It should not, unless everything is directly measured. The difference is the
residual, by design.

**Can it control loads to balance phases?**
No. WattMosaic is read-only and never actuates devices.
