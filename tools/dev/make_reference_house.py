"""Generate the synthetic reference-household audit used by tests and docs.

The repository must never publish the operator's real Home Assistant inventory:
``data/ha_environment_audit.json`` from a live install contains room names,
appliance names (including non-ASCII ones), device-registry hashes, MAC
addresses, serial numbers and config-entry IDs.

This module builds a *structurally equivalent* stand-in so that:

* the topology builder, the simulator and the tests still exercise the same
  shapes — two panels, 18 channels each, dedicated CTs, branch CTs and CT1/CT2
  double-pole pairs;
* no byte of it describes a real home.

The output is deterministic: regenerating produces an identical file.

Run::

    python tools/dev/make_reference_house.py            # writes data/examples/reference_house/
    python tools/dev/make_reference_house.py --stdout   # print instead of writing
    python tools/dev/make_reference_house.py --passive-validation

``--passive-validation`` additionally writes
``data/examples/reference_house/passive_validation.json``: the *real* passive
analysis code (``tools/audit/passive_validation.py``) run over a deterministic
simulator capture.  That file is the input to the documented offline
reproduction command, so the whole validation report can be rebuilt without a
Home Assistant instance:

    python tools/dev/make_reference_house.py --passive-validation
    python tools/audit/passive_validation.py \\
        --from data/examples/reference_house/passive_validation.json

The payload is byte-identical to the one ``tools/dev/render_validation_docs.py``
writes, so the two tools cannot drift apart.

``ct_update_profile.json`` is *not* produced here: its cadence statistics are
measured from a real simulator run by ``tools/dev/render_validation_docs.py``,
so that every published number is measured rather than invented.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "data" / "examples" / "reference_house"

#: Home Assistant version the audit was shaped against.
HA_VERSION = "2026.11.0"

#: Reference time so the fixture is byte-stable.
GENERATED_AT = "2026-01-01T00:00:00+00:00"

#: The reference house has two panels plus a third CT pair.  Channel names are
#: deliberately generic: they describe a *class* of load, never a household.
_MAIN_PANEL = "Panel A"
_SUB_PANEL = "Panel B"

#: (slug, display name, ct_type, phase, pair_prefix)
#: ``ct_type`` here is the *role the audit observed*, not a confirmed fact.
_MAIN_CHANNELS: tuple[tuple[str, str, str, str], ...] = (
    ("heat_pump", "Heat Pump", "dedicated_device", "both"),
    ("vehicle_charger", "Vehicle Charger", "dedicated_device", "both"),
    ("water_heater", "Water Heater", "dedicated_device", "l1"),  # double-pole, below
    ("dishwasher", "Dishwasher", "branch", "l1"),  # double-pole, below
    ("lighting_ct1", "Lighting Circuit 1", "dedicated_device", "l1"),
    ("lighting_ct2", "Lighting Circuit 2", "dedicated_device", "l2"),
    ("lighting_ct3", "Lighting Circuit 3", "dedicated_device", "l1"),
    ("air_conditioner", "Air Conditioner", "dedicated_device", "both"),
    ("central_hvac", "Central HVAC", "dedicated_device", "l1"),
    ("space_heater", "Space Heater", "dedicated_device", "both"),
    ("patio_heater", "Patio Heater", "dedicated_device", "l1"),  # double-pole, below
    ("upper_ac", "Upper Floor AC", "dedicated_device", "both"),
    ("upper_central_hvac", "Upper Floor HVAC", "dedicated_device", "both"),
)

_SUB_CHANNELS: tuple[tuple[str, str, str, str], ...] = (
    ("refrigerator_1", "Refrigerator 1", "dedicated_device", "l1"),
    ("refrigerator_2", "Refrigerator 2", "dedicated_device", "l1"),
    ("hot_water_tank", "Hot Water Tank", "dedicated_device", "both"),
    ("utility_sink", "Utility Sink", "dedicated_device", "l1"),
    ("workshop_outlet", "Workshop Outlet", "branch", "l1"),
    ("guest_bath_unit", "Guest Bath Unit", "dedicated_device", "l1"),
    ("humidifier_1", "Humidifier 1", "dedicated_device", "l1"),
    ("office_outlet", "Office Outlet", "branch", "l1"),
    ("garden_outlet", "Garden Outlet", "branch", "l1"),
    ("display_panel", "Wall Display", "dedicated_device", "l1"),
    ("network_cabinet", "Network Cabinet", "dedicated_device", "both"),
    ("eave_outlet", "Eave Outlet", "branch", "l1"),
    ("ventilation_unit", "Ventilation Unit", "dedicated_device", "l1"),
    ("humidifier_2", "Humidifier 2", "dedicated_device", "l1"),
    ("upper_plug", "Upper Floor Plug", "branch", "l1"),  # double-pole, below
)

#: Loads measured by two CTs (one leg per phase).  Each contributes two channels.
#: Deliberately excludes ``lights``: two lighting circuits that merely share a
#: name stem are separate loads, not the two legs of one 240 V device.
_DOUBLE_POLE = ("water_heater", "dishwasher", "patio_heater", "upper_plug")



def _entity_id(panel: str, slug: str) -> str:
    prefix = "sensor.panel_a" if panel == _MAIN_PANEL else "sensor.panel_b"
    return f"{prefix}_{slug}_power"


def _channel(
    panel: str,
    slug: str,
    name: str,
    ct_type: str,
    phase: str,
    label: str,
    *,
    state: str,
    area: str,
) -> dict[str, Any]:
    entity_id = _entity_id(panel, slug)
    return {
        "device_id": _device_hash(panel),
        "device_name": panel,
        "entity_id": entity_id,
        "name": f"{panel} {name} power",
        "label": label,
        "area_id": area,
        "unit": "W",
        "state": state,
        "state_class": "measurement",
        # Not part of the real audit schema; kept out of the emitted payload.
        "_ct_type": ct_type,
        "_phase": phase,
    }


def _device_hash(panel: str) -> str:
    # 32 hex chars, shaped like a real registry id but derived from the label.
    seed = 0x5EED
    for ch in panel:
        seed = (seed * 131 + ord(ch)) & 0xFFFFFFFFFFFFFFFF
        seed = (seed * 131 + 7) & 0xFFFFFFFFFFFFFFFF
    return f"{seed:016x}{seed ^ 0xA5A5A5A5A5A5A5A5:016x}"


def _states() -> list[str]:
    # Deterministic plausible wattages so no random state is needed.
    return ["12.480", "0.000", "2.848", "48.120", "1.204", "0.000"]


def build_channels() -> list[dict[str, Any]]:
    channels: list[dict[str, Any]] = []

    # --- Main panel ------------------------------------------------------- #
    channels.append(
        _channel(_MAIN_PANEL, "ct1", "CT1", "main", "l1", "ct1", state="1840.216", area="utility")
    )
    channels.append(
        _channel(_MAIN_PANEL, "ct2", "CT2", "main", "l2", "ct2", state="1210.404", area="utility")
    )
    for slug, name, ct_type, phase in _MAIN_CHANNELS:
        if slug in _DOUBLE_POLE:
            channels.append(
                _channel(
                    _MAIN_PANEL, f"{slug}_ct1", f"{name} CT1", "branch", "l1", "ct1",
                    state=_states()[3], area="main_floor",
                )
            )
            channels.append(
                _channel(
                    _MAIN_PANEL, f"{slug}_ct2", f"{name} CT2", "branch", "l2", "ct2",
                    state=_states()[2], area="main_floor",
                )
            )
        else:
            label = "ct1" if slug.endswith("_ct1") else "ct2" if slug.endswith("_ct2") else "channel"
            channels.append(
                _channel(
                    _MAIN_PANEL, slug, name, ct_type, phase, label,
                    state=_states()[3], area="main_floor",
                )
            )

    # --- Sub panel -------------------------------------------------------- #
    channels.append(
        _channel(_SUB_PANEL, "ct1", "CT1", "main", "l1", "ct1", state="860.112", area="utility")
    )
    channels.append(
        _channel(_SUB_PANEL, "ct2", "CT2", "main", "l2", "ct2", state="744.900", area="utility")
    )
    for slug, name, ct_type, phase in _SUB_CHANNELS:
        if slug in _DOUBLE_POLE:
            channels.append(
                _channel(
                    _SUB_PANEL, f"{slug}_ct1", f"{name} CT1", "branch", "l1", "ct1",
                    state=_states()[4], area="upper_floor",
                )
            )
            channels.append(
                _channel(
                    _SUB_PANEL, f"{slug}_ct2", f"{name} CT2", "branch", "l2", "ct2",
                    state=_states()[2], area="upper_floor",
                )
            )
        else:
            channels.append(
                _channel(
                    _SUB_PANEL, slug, name, ct_type, phase, "channel",
                    state=_states()[0], area="basement",
                )
            )
    return channels


def build_audit() -> dict[str, Any]:
    channels = build_channels()

    mapping_candidates = [
        {
            "ct": {k: v for k, v in ch.items() if not k.startswith("_")},
            "candidates": [
                {
                    "device_id": _device_hash(_MAIN_PANEL),
                    "device_name": _MAIN_PANEL,
                    "overlap_tokens": ["panel"],
                    "has_power": True,
                },
                {
                    "device_id": _device_hash(_SUB_PANEL),
                    "device_name": _SUB_PANEL,
                    "overlap_tokens": ["panel"],
                    "has_power": True,
                },
            ],
            "status": "pending_confirmation",
            "note": "Name-token evidence only. Not committed topology.",
        }
        for ch in channels
    ]

    power_entities = [
        {
            "entity_id": ch["entity_id"],
            "device_id": ch["device_id"],
            "device_name": ch["device_name"],
            "friendly_name": ch["name"],
            "original_name": ch["name"].removeprefix(f"{ch['device_name']} "),
            "platform": "energy_monitor",
            "device_class": "power",
            "unit_of_measurement": "W",
            "state": ch["state"],
        }
        for ch in channels
    ]

    energy_entities = [
        {
            "entity_id": ch["entity_id"].replace("_power", "_energy"),
            "device_id": ch["device_id"],
            "device_name": ch["device_name"],
            "friendly_name": f"{ch['device_name']} energy",
            "platform": "energy_monitor",
            "device_class": "energy",
            "unit_of_measurement": "kWh",
        }
        for ch in channels
    ]

    areas = [
        {"id": "utility", "name": "Utility"},
        {"id": "main_floor", "name": "Main Floor"},
        {"id": "upper_floor", "name": "Upper Floor"},
        {"id": "basement", "name": "Basement"},
    ]

    return {
        "generated_at": GENERATED_AT,
        "ha": {
            "version": HA_VERSION,
            "time_zone": "UTC",
            "unit_system": {
                "length": "km",
                "accumulated_precipitation": "mm",
                "area": "m²",
                "mass": "g",
                "pressure": "Pa",
                "temperature": "°C",
                "volume": "L",
                "wind_speed": "m/s",
            },
            "state": "RUNNING",
            "safe_mode": False,
            "location_name": "Home",
        },
        "counts": {
            "states": len(power_entities) + len(energy_entities),
            "entities_registry": len(power_entities) + len(energy_entities),
            "devices_registry": 2,
            "areas_registry": len(areas),
            "floors_registry": 1,
            "entities": len(power_entities) + len(energy_entities),
            "devices": 2,
            "power_entities": len(power_entities),
            "energy_entities": len(energy_entities),
            "energy_monitors": 2,
            "monitor_power_entities": len(power_entities),
            "monitor_energy_entities": len(energy_entities),
            "state_rich_devices": 0,
            "power_measured_devices": 2,
        },
        "energy_monitors": [
            {
                "id": _device_hash(_MAIN_PANEL),
                "device_id": _device_hash(_MAIN_PANEL),
                "name": _MAIN_PANEL,
                "model": "multi-channel CT monitor",
                "manufacturer": None,
                "area_id": "utility",
                "hw_version": "3.0.0",
            },
            {
                "id": _device_hash(_SUB_PANEL),
                "device_id": _device_hash(_SUB_PANEL),
                "name": _SUB_PANEL,
                "model": "multi-channel CT monitor",
                "manufacturer": None,
                "area_id": "utility",
                "hw_version": "3.0.0",
            },
        ],
        "ct_channels": [{k: v for k, v in ch.items() if not k.startswith("_")} for ch in channels],
        "duplicate_power_names": [
            {"duplicate_ct_id": "panel_b_display_panel", "duplicate_of": "dev_panel_b_display_panel"}
        ],
        "mapping_candidates": mapping_candidates,
        "power_entities": power_entities,
        "energy_entities": energy_entities,
        "devices": [
            {
                "device_id": _device_hash(_MAIN_PANEL),
                "name": _MAIN_PANEL,
                "entity_count": 18,
                "has_power": True,
                "state_richness": "power_measured",
            },
            {
                "device_id": _device_hash(_SUB_PANEL),
                "name": _SUB_PANEL,
                "entity_count": 18,
                "has_power": True,
                "state_richness": "power_measured",
            },
        ],
        "areas": areas,
        "limitations": [
            "Mapping candidates are name-token evidence only and MUST be confirmed.",
            "Physical measurement timestamps are not exposed by HA; last_updated is a state update time.",
            "This file is a synthetic reference household, not a real installation.",
        ],
    }


def build_state_entities() -> dict[str, Any]:
    """A small, fully-synthetic state-link report.

    Only ``supported``/``weak`` verdicts carry a state entity, matching
    ``derive_state_entities.py``.  One confounded and one duplicate channel are
    included so the builder's refusal paths stay exercised.
    """
    return {
        "generated_at": GENERATED_AT,
        "devices": {
            "dev_panel_a_central_hvac": {
                "status": "supported",
                "state_entity_ids": ["binary_sensor.panel_a_central_hvac_running"],
                "state_overrides": {"on": ["on"], "off": ["off"]},
                "reason": "balanced_accuracy_above_strong",
                "evidence": {"balanced_accuracy": 0.94},
            },
            "dev_panel_b_hot_water_tank": {
                "status": "weak",
                "state_entity_ids": ["binary_sensor.panel_b_hot_water_tank_running"],
                "state_overrides": {},
                "reason": "balanced_accuracy_below_strong:0.71",
                "evidence": {"balanced_accuracy": 0.71},
            },
            "dev_panel_b_utility_sink": {
                "status": "confounded",
                "state_entity_ids": [],
                "reason": "additive_multi_device",
                "evidence": {},
            },
            "dev_panel_b_display_panel": {
                "status": "supported",
                "state_entity_ids": ["binary_sensor.panel_b_display_panel_on"],
                "state_overrides": {},
                "reason": "balanced_accuracy_above_strong",
                "duplicate_of": "dev_panel_b_display_panel",
                "evidence": {"balanced_accuracy": 0.9},
            },
        },
        "channels": {
            "sensor.panel_a_patio_heater_ct1_power": {
                "polarity_evidence": {"candidate_polarity": "inverted"}
            },
            "sensor.panel_b_garden_outlet_power": {
                "polarity_evidence": {"candidate_polarity": "normal"}
            },
        },
    }


def build_passive_validation() -> dict[str, Any]:
    """Run the *real* passive analysis over the synthetic reference household.

    Deliberately delegates to ``tools/dev/render_validation_docs.py`` instead of
    re-implementing the pipeline: that module is the single source of the
    committed worked example, so this flag and the docs renderer cannot produce
    different files.  Imported lazily so ``--stdout`` stays cheap.
    """
    here = Path(__file__).resolve().parent
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "tools" / "audit"))
    sys.path.insert(0, str(here))
    import render_validation_docs

    committed_raw, *_ = render_validation_docs.build()
    return committed_raw


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stdout", action="store_true", help="print instead of writing")
    ap.add_argument(
        "--passive-validation",
        action="store_true",
        help=(
            "also write data/examples/reference_house/passive_validation.json "
            "by running the real passive-analysis code over a simulator capture"
        ),
    )
    args = ap.parse_args()

    payloads = {
        "ha_environment_audit.json": build_audit(),
        "state_entity_candidates.json": build_state_entities(),
    }
    if args.passive_validation:
        payloads["passive_validation.json"] = build_passive_validation()

    if args.stdout:
        for name, payload in payloads.items():
            print(f"### {name}")
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, payload in payloads.items():
        path = OUT_DIR / name
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        print(f"wrote {path.relative_to(ROOT)}")

    channels = len(payloads["ha_environment_audit.json"]["ct_channels"])
    print(f"channels: {channels}")
    if channels != 36:
        print(f"ERROR: expected 36 channels (18 per panel), got {channels}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
