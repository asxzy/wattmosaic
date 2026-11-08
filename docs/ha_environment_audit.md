# Home Assistant Environment Audit

Generated: `2026-01-01T00:00:00+00:00` (passive, read-only)

> **This report describes a synthetic reference household.** It is the
> committed example used by the test suite and documentation. It is not
> the operator's installation. Run the audit against your own Home
> Assistant to produce a real one (see *Reproducibility* below); that
> output is local-only and git-ignored, because it reveals room names,
> device hashes and hardware identifiers.

## Access method

- REST `GET /api/states`, `GET /api/config`.
- WebSocket read-only commands: `config/entity_registry/list`,
  `config/device_registry/list`, `config/area_registry/list`,
  `config/floor_registry/list`.
- No service calls, no device control, no configuration changes.

## Confirmed facts

- Home Assistant version: **2026.11.0**
- Installation state: `RUNNING` (safe_mode=False)
- Time zone: `UTC`
- States observed: 72
- Registry entities: 72, devices: 2, areas: 4
- Power entities: **36**, energy entities: **36**
- Energy monitors: **2**, monitor power entities: **36**
- Devices with any power entity: 2

## Energy-monitor hardware

| Device | Model | Manufacturer | Area | ID |
| --- | --- | --- | --- | --- |
| Panel A | multi-channel CT monitor | None | utility | `9816956bf67206a13db330ce53d7a304` |
| Panel B | multi-channel CT monitor | None | utility | `9816956bf67207243db330ce53d7a281` |

## CT channels observed

### Panel A

| Entity | Label | Name | Current state | Unit | Category |
| --- | --- | --- | --- | --- | --- |
| `sensor.panel_a_air_conditioner_power` | channel | Panel A Air Conditioner power | 48.120 | W | measurement |
| `sensor.panel_a_central_hvac_power` | channel | Panel A Central HVAC power | 48.120 | W | measurement |
| `sensor.panel_a_ct1_power` | ct1 | Panel A CT1 power | 1840.216 | W | measurement |
| `sensor.panel_a_ct2_power` | ct2 | Panel A CT2 power | 1210.404 | W | measurement |
| `sensor.panel_a_dishwasher_ct1_power` | ct1 | Panel A Dishwasher CT1 power | 48.120 | W | measurement |
| `sensor.panel_a_dishwasher_ct2_power` | ct2 | Panel A Dishwasher CT2 power | 2.848 | W | measurement |
| `sensor.panel_a_heat_pump_power` | channel | Panel A Heat Pump power | 48.120 | W | measurement |
| `sensor.panel_a_lighting_ct1_power` | ct1 | Panel A Lighting Circuit 1 power | 48.120 | W | measurement |
| `sensor.panel_a_lighting_ct2_power` | ct2 | Panel A Lighting Circuit 2 power | 48.120 | W | measurement |
| `sensor.panel_a_lighting_ct3_power` | channel | Panel A Lighting Circuit 3 power | 48.120 | W | measurement |
| `sensor.panel_a_patio_heater_ct1_power` | ct1 | Panel A Patio Heater CT1 power | 48.120 | W | measurement |
| `sensor.panel_a_patio_heater_ct2_power` | ct2 | Panel A Patio Heater CT2 power | 2.848 | W | measurement |
| `sensor.panel_a_space_heater_power` | channel | Panel A Space Heater power | 48.120 | W | measurement |
| `sensor.panel_a_upper_ac_power` | channel | Panel A Upper Floor AC power | 48.120 | W | measurement |
| `sensor.panel_a_upper_central_hvac_power` | channel | Panel A Upper Floor HVAC power | 48.120 | W | measurement |
| `sensor.panel_a_vehicle_charger_power` | channel | Panel A Vehicle Charger power | 48.120 | W | measurement |
| `sensor.panel_a_water_heater_ct1_power` | ct1 | Panel A Water Heater CT1 power | 48.120 | W | measurement |
| `sensor.panel_a_water_heater_ct2_power` | ct2 | Panel A Water Heater CT2 power | 2.848 | W | measurement |

### Panel B

| Entity | Label | Name | Current state | Unit | Category |
| --- | --- | --- | --- | --- | --- |
| `sensor.panel_b_ct1_power` | ct1 | Panel B CT1 power | 860.112 | W | measurement |
| `sensor.panel_b_ct2_power` | ct2 | Panel B CT2 power | 744.900 | W | measurement |
| `sensor.panel_b_display_panel_power` | channel | Panel B Wall Display power | 12.480 | W | measurement |
| `sensor.panel_b_eave_outlet_power` | channel | Panel B Eave Outlet power | 12.480 | W | measurement |
| `sensor.panel_b_garden_outlet_power` | channel | Panel B Garden Outlet power | 12.480 | W | measurement |
| `sensor.panel_b_guest_bath_unit_power` | channel | Panel B Guest Bath Unit power | 12.480 | W | measurement |
| `sensor.panel_b_hot_water_tank_power` | channel | Panel B Hot Water Tank power | 12.480 | W | measurement |
| `sensor.panel_b_humidifier_1_power` | channel | Panel B Humidifier 1 power | 12.480 | W | measurement |
| `sensor.panel_b_humidifier_2_power` | channel | Panel B Humidifier 2 power | 12.480 | W | measurement |
| `sensor.panel_b_network_cabinet_power` | channel | Panel B Network Cabinet power | 12.480 | W | measurement |
| `sensor.panel_b_office_outlet_power` | channel | Panel B Office Outlet power | 12.480 | W | measurement |
| `sensor.panel_b_refrigerator_1_power` | channel | Panel B Refrigerator 1 power | 12.480 | W | measurement |
| `sensor.panel_b_refrigerator_2_power` | channel | Panel B Refrigerator 2 power | 12.480 | W | measurement |
| `sensor.panel_b_upper_plug_ct1_power` | ct1 | Panel B Upper Floor Plug CT1 power | 1.204 | W | measurement |
| `sensor.panel_b_upper_plug_ct2_power` | ct2 | Panel B Upper Floor Plug CT2 power | 2.848 | W | measurement |
| `sensor.panel_b_utility_sink_power` | channel | Panel B Utility Sink power | 12.480 | W | measurement |
| `sensor.panel_b_ventilation_unit_power` | channel | Panel B Ventilation Unit power | 12.480 | W | measurement |
| `sensor.panel_b_workshop_outlet_power` | channel | Panel B Workshop Outlet power | 12.480 | W | measurement |

## Inferred (not confirmed)

- 2 energy-monitor device(s) expose 36 power channels between them.
- `Panel A` exposes a set of CT1/CTn plus named branch channels.
- `Panel B` exposes a set of CT1/CTn plus named branch channels.
- Channel names describe the circuit they are *wired to*, not the device
  currently drawing on it. A shared branch carries several loads.
- Double-pole 240 V loads appear as a `<Name> CT1` / `<Name> CT2` pair
  (each leg on one phase, half the load per leg).

## Ambiguities requiring confirmation

1. Which channel pair is the utility service entrance vs. a sub-feed.
   Nesting must be confirmed by the operator.
2. Sign convention / polarity of each channel.
3. Which CTs are double-pole halves of one load. Pairs detected from
   naming alone are candidates: two circuits that merely share a name
   stem are separate loads.
4. Whether repeated identical values are real or template duplicates.

## Sampling behaviour

See `data/ct_update_profile.json` and `docs/ct_update_profile.md`. HA does not
expose a physical measurement timestamp; `last_updated` is a state update time.
Repeated identical values are **not** treated as new measurements.

## Duplicate power entity names

- `panel_b_display_panel` → `dev_panel_b_display_panel`

## Reproducibility

```bash
.venv/bin/python tools/audit/audit_environment.py
.venv/bin/python tools/audit/collect_samples.py --minutes 45
.venv/bin/python tools/audit/profile_samples.py
```
