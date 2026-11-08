# Passive Validation — synthetic reference household

Generated: `2026-01-01T00:00:15+00:00`
Observation window: 20.0 minutes, read-only subscription.
No service calls were issued. No device was controlled.

> **This report is synthetic.** Every figure below was computed by the
> real analysis code over a simulator run, not over a live Home
> Assistant. It is the committed worked example. Run
> `tools/audit/passive_validation.py` against your own installation to
> get a real one; that output is local-only and git-ignored, because a
> per-channel table identifies a household.

## Status classification

### Verified

- 36 power entities subscribe successfully.
- 36 channels produced observations in the window.
- 33/33 parsed power readings are non-negative (no reverse-flow observed in this window).
- Migration, contract and storage tests pass (see test report).

### Plausible (not yet proven)

- Main-panel CT1/CT2 behave as L1/L2 legs; both are consistently non-negative.
- Named branch channels correspond to the circuits in their names.
- `*_ct1`/`*_ct2` pairs are two legs of one 240 V load.

### Unverified

- Absolute accuracy of per-device inference (no household ground truth).
- Nesting depth between the panel mains and any intermediate sub-feed.
- Whether repeated-value suppression affects short-cycle appliances.

## Observed channel behaviour

| Entity | Obs | Median interval (s) | Min | Max | Noise MAD (W) | Resolution (W) |
| --- | --- | --- | --- | --- | --- | --- |
| `sensor.panel_a_patio_heater_ct1_power` | 77 | 15.715 | 6.977 | 1503.598 | 0.24 | 0.001 |
| `sensor.panel_b_humidifier_1_power` | 77 | 15.811 | 8.927 | 129.84 | 9.188 | 0.022 |
| `sensor.panel_b_office_outlet_power` | 77 | 15.619 | 6.688 | 135.577 | 0.173 | 0.012 |
| `sensor.panel_b_garden_outlet_power` | 77 | 15.587 | 1.224 | 501.812 | 0.628 | 0.031 |
| `sensor.panel_b_display_panel_power` | 77 | 15.75 | 1.805 | 35.371 | 0.324 | 0.001 |
| `sensor.panel_a_underfloor_loop_power` | 76 | 15.746 | 1.234 | 1502.894 | 1.06 | 0.002 |
| `sensor.panel_a_water_heater_ct2_power` | 76 | 15.78 | 2.634 | 3003.001 | 1496.8435 | 0.005 |
| `sensor.panel_a_utility_plug_ct1_power` | 76 | 15.632 | 1.622 | 2404.077 | 901.0835 | 0.005 |
| `sensor.panel_a_utility_plug_ct2_power` | 76 | 15.714 | 2.964 | 2003.335 | 200.7465 | 0.009 |
| `sensor.panel_a_lighting_ct1_power` | 76 | 15.53 | 0.17 | 241.27 | 0.0785 | 0.002 |
| `sensor.panel_a_central_hvac_power` | 76 | 15.722 | 5.594 | 902.011 | 1.065 | 0.01 |
| `sensor.panel_b_refrigerator_1_power` | 76 | 15.657 | 0.596 | 120.711 | 0.061 | 0.002 |
| `sensor.panel_b_refrigerator_2_power` | 76 | 15.776 | 0.783 | 120.604 | 0.1015 | 0.016 |
| `sensor.panel_b_guest_bath_unit_power` | 76 | 15.69 | 2.235 | 802.193 | 0.0805 | 0.003 |
| `sensor.panel_b_humidifier_2_power` | 76 | 15.682 | 0.339 | 1201.067 | 0.0605 | 0.002 |
| `sensor.panel_b_workshop_outlet_power` | 76 | 15.754 | 2.376 | 2.849 | 0.045 | 0.001 |
| `sensor.panel_a_ct1_power` | 75 | 15.703 | 4366.017 | 19464.944 | 3170.137 | 0.202 |
| `sensor.panel_a_vehicle_charger_power` | 75 | 15.756 | 9.157 | 4966.825 | 6.416 | 0.018 |
| `sensor.panel_a_upper_ac_power` | 75 | 15.865 | 3.127 | 3204.868 | 0.165 | 0.01 |
| `sensor.panel_b_hot_water_tank_power` | 75 | 15.79 | 5.379 | 250.634 | 0.114 | 0.002 |
| `sensor.panel_b_utility_sink_power` | 75 | 15.842 | 1.804 | 902.583 | 0.229 | 0.008 |
| `sensor.panel_b_network_cabinet_power` | 75 | 15.721 | 8.361 | 309.547 | 0.833 | 0.007 |
| `sensor.panel_a_patio_heater_ct2_power` | 75 | 15.791 | 6.902 | 1502.968 | 0.238 | 0.003 |
| `sensor.panel_b_ct1_power` | 75 | 15.781 | 33.769 | 2673.961 | 503.571 | 0.011 |
| `sensor.panel_a_water_heater_ct1_power` | 74 | 15.679 | 2.652 | 3005.312 | 7.604 | 0.01 |
| `sensor.panel_a_upper_central_hvac_power` | 74 | 15.669 | 4.576 | 802.496 | 3.7735 | 0.006 |
| `sensor.panel_b_upper_plug_ct2_power` | 74 | 15.756 | 3.554 | 68.578 | 0.8845 | 0.002 |
| `sensor.panel_a_space_heater_power` | 74 | 15.697 | 2.104 | 4504.144 | 2.1145 | 0.005 |
| `sensor.panel_a_ct2_power` | 73 | 15.792 | 2162.701 | 10982.934 | 801.749 | 0.22 |
| `sensor.panel_a_air_conditioner_power` | 73 | 15.775 | 7.524 | 3504.837 | 2.311 | 0.012 |
| `sensor.panel_b_eave_outlet_power` | 73 | 15.941 | 1.713 | 60.55 | 0.179 | 0.013 |
| `sensor.panel_b_upper_plug_ct1_power` | 73 | 15.861 | 3.59 | 266.105 | 0.218 | 0.003 |
| `sensor.panel_a_lighting_ct2_power` | 72 | 15.833 | -0.048 | 481.637 | 0.075 | 0.004 |
| `sensor.panel_a_lighting_ct3_power` | 72 | 15.948 | 0.272 | 180.452 | 0.07 | 0.005 |
| `sensor.panel_b_ct2_power` | 72 | 15.957 | 22.562 | 1867.871 | 209.3875 | 0.01 |
| `sensor.panel_b_ventilation_unit_power` | 71 | 15.621 | 4.069 | 160.784 | 64.212 | 0.003 |

## Interpretation

- Median intervals cluster around the integration poll period, not a
  hardware sample period. This confirms ADR-0003 and the rejection of the
  original ~2 s assumption.
- Noise MAD values set the floor for event detection thresholds. A change
  smaller than a few MAD is not evidence of a device transition.
- Resolution (`min_nonzero_delta_w`) bounds the smallest measurable change
  per channel; inference below this is not supported.

## What this does NOT establish

Passive observation cannot establish per-device accuracy without an
independent ground truth. No accuracy figure is claimed here.

## Reproduction

```bash
.venv/bin/python tools/dev/make_reference_house.py --passive-validation
.venv/bin/python tools/audit/passive_validation.py \
    --from data/examples/reference_house/passive_validation.json
```
