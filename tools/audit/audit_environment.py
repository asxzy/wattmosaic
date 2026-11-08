"""Full read-only Home Assistant environment audit for WattMosaic.

Produces:
  docs/ha_environment_audit.md
  data/ha_environment_audit.json
  data/entity_inventory.json
  data/device_inventory.json
  data/initial_mapping_candidates.json
  data/ct_update_profile.json  (from collected samples; see profile_samples.py)

Run:  python tools/audit/audit_environment.py
"""

from __future__ import annotations

import asyncio
import json
import re
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ha_client import HAClient

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
DOCS = ROOT / "docs"

POWER_CLASSES = {"power"}

#: A CT monitor exposes many power channels from one hardware device; a smart
#: plug exposes one.  Three is the smallest count that cannot be a plug with a
#: couple of extra diagnostics, and it is deliberately *structural*: matching on
#: brand or model names only recognises the hardware whose name was hard-coded,
#: and it publishes those vendor names.
MIN_MONITOR_CHANNELS = 3


def identify_monitors(power: list[dict[str, Any]]) -> set[str]:
    """Hardware device ids that expose several power entities.

    Used only to decide which power entities are CT channels.  Nothing here
    consults a name: a device is a monitor because of what it measures.
    """
    counts: dict[str, int] = Counter(
        item["device_id"] for item in power if item.get("device_id")
    )
    return {device_id for device_id, count in counts.items() if count >= MIN_MONITOR_CHANNELS}
ENERGY_CLASSES = {"energy"}


def now() -> str:
    return datetime.now(UTC).isoformat()


def _slug_tail(entity_id: str) -> str:
    """``sensor.panel_a_hot_tub_ct1_power`` → ``panel_a_hot_tub_ct1``.

    Used only to group channel names for the human-readable report; never to
    identify a device.
    """
    slug = entity_id.split(".", 1)[-1]
    return slug.removesuffix("_power")


def pick_meta(state: dict[str, Any]) -> dict[str, Any]:
    a = state.get("attributes", {})
    return {
        "state": state.get("state"),
        "unit_of_measurement": a.get("unit_of_measurement"),
        "device_class": a.get("device_class"),
        "state_class": a.get("state_class"),
        "friendly_name": a.get("friendly_name"),
        "last_changed": state.get("last_changed"),
        "last_updated": state.get("last_updated"),
        "available": state.get("state") not in ("unavailable", "unknown", None),
        "attributes": {
            k: v
            for k, v in a.items()
            if k
            in (
                "friendly_name",
                "unit_of_measurement",
                "device_class",
                "state_class",
                "suggested_display_precision",
                "supported_features",
                "voltage",
                "current",
                "power",
                "energy",
                "restored",
            )
        },
    }


async def audit() -> dict[str, Any]:
    async with HAClient.from_env() as client:
        cfg = await client.config()
        states = await client.states()
        regs = await client.registries()

    entities = {e["entity_id"]: e for e in regs["entities"] if "entity_id" in e}
    devices = {d["id"]: d for d in regs["devices"]}
    areas = {a["area_id"]: a for a in regs["areas"]}
    floors = {f["floor_id"]: f for f in regs.get("floors", [])}

    # ---- classify entities -------------------------------------------------
    inventory: list[dict[str, Any]] = []
    for st in states:
        eid = st["entity_id"]
        reg = entities.get(eid)
        if not reg:
            continue
        if reg.get("disabled_by"):
            continue
        attrs = st.get("attributes", {})
        dc = attrs.get("device_class")
        unit = attrs.get("unit_of_measurement")
        if dc not in ("power", "energy") and unit not in ("W", "kW", "kWh", "Wh"):
            continue
        dev = devices.get(reg.get("device_id"), {})
        inventory.append(
            {
                "entity_id": eid,
                "name": attrs.get("friendly_name") or reg.get("original_name"),
                "original_name": reg.get("original_name"),
                "unique_id": reg.get("unique_id"),
                "device_id": reg.get("device_id"),
                "device_name": dev.get("name"),
                "device_model": dev.get("model"),
                "manufacturer": dev.get("manufacturer"),
                "area_id": reg.get("area_id") or dev.get("area_id"),
                "area_name": areas.get(reg.get("area_id") or dev.get("area_id"), {}).get("name"),
                "platform": reg.get("platform"),
                "config_entry_id": reg.get("config_entry_id"),
                "unit": unit,
                "device_class": dc,
                "state_class": attrs.get("state_class"),
                "category": "power" if dc == "power" or unit in ("W", "kW") else "energy",
                "hidden_by": reg.get("hidden_by"),
                **pick_meta(st),
            }
        )

    power = [i for i in inventory if i["category"] == "power"]
    energy = [i for i in inventory if i["category"] == "energy"]

    # ---- Energy-monitor discovery -------------------------------------------
    #: A multi-channel CT monitor is identified structurally: one hardware device
    #: publishing several power entities.  Brand and model names are deliberately
    #: not consulted, because a hint list only recognises the hardware someone
    #: thought of and every entry in it leaks a vendor name into the repository.
    #: Any power entity that is not part of such a device is still inventoried,
    #: just not treated as a CT channel.
    monitor_ids = identify_monitors(power)
    monitor_devices = [d for d in devices.values() if d["id"] in monitor_ids]
    monitor_power = [i for i in power if i["device_id"] in monitor_ids]
    monitor_energy = [i for i in energy if i["device_id"] in monitor_ids]

    # Group monitor channels by device and infer CT role from name.
    def ct_label(name: str | None) -> str:
        n = (name or "").lower()
        m = re.search(r"ct\s*([0-9]+)", n)
        if m:
            return f"ct{m.group(1)}"
        if "total" in n:
            return "total"
        if "voltage" in n:
            return "voltage"
        if "current" in n:
            return "current"
        return "channel"

    monitor_by_device: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for i in monitor_power:
        monitor_by_device[i["device_id"]].append(i)

    ct_channels: list[dict[str, Any]] = []
    for dev_id, items in monitor_by_device.items():
        dev = devices.get(dev_id, {})
        # entries named CTn that duplicate an appliance name are the raw channels
        for i in sorted(items, key=lambda x: x["entity_id"]):
            label = ct_label(i["name"])
            ct_channels.append(
                {
                    "device_id": dev_id,
                    "device_name": dev.get("name"),
                    "entity_id": i["entity_id"],
                    "name": i["name"],
                    "label": label,
                    "area_id": i["area_id"],
                    "unit": i["unit"],
                    "state": i["state"],
                    "state_class": i["state_class"],
                }
            )

    # ---- device state richness --------------------------------------------
    device_entities: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in regs["entities"]:
        if e.get("device_id"):
            device_entities[e["device_id"]].append(e)
    states_by_id = {s["entity_id"]: s for s in states}

    device_inventory: list[dict[str, Any]] = []
    for dev_id, dev in devices.items():
        ents = device_entities.get(dev_id, [])
        present = [e for e in ents if e["entity_id"] in states_by_id]
        avail = [e for e in present if states_by_id[e["entity_id"]].get("state") not in ("unavailable", "unknown")]
        domains = Counter(e["entity_id"].split(".")[0] for e in present)
        has_power = any(
            states_by_id[e["entity_id"]].get("attributes", {}).get("device_class") == "power" for e in present
        )
        has_energy = any(
            states_by_id[e["entity_id"]].get("attributes", {}).get("device_class") == "energy" for e in present
        )
        controllable = [e["entity_id"] for e in present if e["entity_id"].split(".")[0] in ("light", "switch", "fan", "climate", "cover", "media_player", "vacuum", "humidifier", "water_heater", "number", "input_boolean")]
        if has_power:
            richness = "power_measured"
        elif controllable and len(present) >= 3:
            richness = "state_rich_controllable"
        elif controllable:
            richness = "state_controllable"
        elif present:
            richness = "state_only"
        else:
            richness = "registry_only"
        device_inventory.append(
            {
                "device_id": dev_id,
                "name": dev.get("name"),
                "manufacturer": dev.get("manufacturer"),
                "model": dev.get("model"),
                "model_id": dev.get("model_id"),
                "area_id": dev.get("area_id"),
                "area_name": areas.get(dev.get("area_id"), {}).get("name"),
                "integration": (dev.get("identifiers") or [[None]])[0][0],
                "entry_type": dev.get("entry_type"),
                "entity_count": len(present),
                "available_entity_count": len(avail),
                "domains": dict(domains),
                "has_power": has_power,
                "has_energy": has_energy,
                "controllable_entities": controllable[:20],
                "state_richness": richness,
            }
        )

    device_inventory.sort(key=lambda d: (-d["entity_count"], d["name"] or ""))

    # ---- initial mapping candidates ---------------------------------------
    # Evidence-based, never auto-committed. Naming similarity is a *candidate*.
    mappings: list[dict[str, Any]] = []
    for chan in ct_channels:
        tokens = {t for t in re.split(r"[^a-z0-9]+", (chan["name"] or "").lower()) if len(t) > 2}
        matched: list[dict[str, Any]] = []
        for dev in device_inventory:
            if dev["device_id"] == chan["device_id"]:
                continue
            dtokens = {
                t for t in re.split(r"[^a-z0-9]+", (dev["name"] or "").lower()) if len(t) > 2
            }
            overlap = tokens & dtokens
            if overlap:
                matched.append(
                    {
                        "device_id": dev["device_id"],
                        "device_name": dev["name"],
                        "overlap_tokens": sorted(overlap),
                        "has_power": dev["has_power"],
                    }
                )
        mappings.append(
            {
                "ct": chan,
                "candidates": sorted(matched, key=lambda m: -len(m["overlap_tokens"]))[:5],
                "status": "pending_confirmation",
            }
        )

    # ---- duplicate / overlapping detection --------------------------------
    dupes: dict[str, list[str]] = defaultdict(list)
    for i in power:
        key = (i["name"] or "").strip().lower()
        dupes[key].append(i["entity_id"])
    duplicate_names = {k: v for k, v in dupes.items() if len(v) > 1 and k}

    result = {
        "generated_at": now(),
        "ha": {
            "version": cfg.get("version"),
            "time_zone": cfg.get("time_zone"),
            "unit_system": cfg.get("unit_system"),
            "state": cfg.get("state"),
            "safe_mode": cfg.get("safe_mode"),
            "location_name": cfg.get("location_name"),
        },
        "counts": {
            "states": len(states),
            "entities_registry": len(regs["entities"]),
            "devices_registry": len(devices),
            "areas_registry": len(areas),
            "floors_registry": len(floors),
            "power_entities": len(power),
            "energy_entities": len(energy),
            "energy_monitors": len(monitor_devices),
            "monitor_power_entities": len(monitor_power),
            "monitor_energy_entities": len(monitor_energy),
            "state_rich_devices": sum(
                1 for d in device_inventory if d["state_richness"] in ("state_rich_device", "state_rich_controllable")
            ),
            "power_measured_devices": sum(1 for d in device_inventory if d["has_power"]),
        },
        "energy_monitors": [
            {
                "id": d.get("id"),
                "name": d.get("name"),
                "model": d.get("model"),
                "manufacturer": d.get("manufacturer"),
                "area_id": d.get("area_id"),
                "sw_version": d.get("sw_version"),
                "hw_version": d.get("hw_version"),
                "identifiers": d.get("identifiers"),
                "connections": d.get("connections"),
                "via_device_id": d.get("via_device_id"),
            }
            for d in monitor_devices
        ],
        "ct_channels": ct_channels,
        "duplicate_power_names": duplicate_names,
        "mapping_candidates": mappings,
        "power_entities": sorted(power, key=lambda i: i["entity_id"]),
        "energy_entities": sorted(energy, key=lambda i: i["entity_id"]),
        "devices": device_inventory,
        "areas": [{"id": a["area_id"], "name": a.get("name")} for a in regs["areas"]],
        "limitations": [
            "Registry listing is read via WebSocket; REST config endpoints are unavailable on this HA build.",
            "Physical measurement timestamps are not exposed by HA; last_updated is a state update time.",
            "Mapping candidates are name-token evidence only and MUST be confirmed.",
        ],
    }
    return result


def write_outputs(result: dict[str, Any]) -> None:
    DATA.mkdir(exist_ok=True)
    DOCS.mkdir(exist_ok=True)
    (DATA / "ha_environment_audit.json").write_text(json.dumps(result, indent=2, default=str))
    (DATA / "entity_inventory.json").write_text(
        json.dumps(
            {
                "generated_at": result["generated_at"],
                "power": result["power_entities"],
                "energy": result["energy_entities"],
            },
            indent=2,
            default=str,
        )
    )
    (DATA / "device_inventory.json").write_text(
        json.dumps({"generated_at": result["generated_at"], "devices": result["devices"]}, indent=2, default=str)
    )
    (DATA / "initial_mapping_candidates.json").write_text(
        json.dumps(
            {
                "generated_at": result["generated_at"],
                "status": "pending_confirmation",
                "candidates": result["mapping_candidates"],
                "note": "Name-token evidence only. Not committed topology.",
            },
            indent=2,
            default=str,
        )
    )
    write_doc(result)


def _render(result: dict[str, Any]) -> str:
    """Build the markdown report for an audit payload."""
    c = result["counts"]
    channels = result["ct_channels"]
    by_dev: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for ch in channels:
        by_dev[ch["device_name"] or ch["device_id"]].append(ch)

    limitations = result.get("limitations") or []
    synthetic = any("synthetic" in str(item).lower() for item in limitations)

    lines = [
        "# Home Assistant Environment Audit",
        "",
        f"Generated: `{result['generated_at']}` (passive, read-only)",
        "",
    ]
    if synthetic:
        lines += [
            "> **This report describes a synthetic reference household.** It is the",
            "> committed example used by the test suite and documentation. It is not",
            "> the operator's installation. Run the audit against your own Home",
            "> Assistant to produce a real one (see *Reproducibility* below); that",
            "> output is local-only and git-ignored, because it reveals room names,",
            "> device hashes and hardware identifiers.",
            "",
        ]
    lines += [
        "## Access method",
        "",
        "- REST `GET /api/states`, `GET /api/config`.",
        "- WebSocket read-only commands: `config/entity_registry/list`,",
        "  `config/device_registry/list`, `config/area_registry/list`,",
        "  `config/floor_registry/list`.",
        "- No service calls, no device control, no configuration changes.",
        "",
        "## Confirmed facts",
        "",
        f"- Home Assistant version: **{result['ha']['version']}**",
        f"- Installation state: `{result['ha']['state']}` (safe_mode={result['ha']['safe_mode']})",
        f"- Time zone: `{result['ha']['time_zone']}`",
        f"- States observed: {c.get('states', 'n/a')}",
        f"- Registry entities: {c.get('entities_registry', 'n/a')}, "
        f"devices: {c.get('devices_registry', 'n/a')}, "
        f"areas: {c.get('areas_registry', 'n/a')}",
        f"- Power entities: **{c['power_entities']}**, energy entities: **{c['energy_entities']}**",
        f"- Energy monitors: **{c.get('energy_monitors', 0)}**, "
        f"monitor power entities: **{c.get('monitor_power_entities', 0)}**",
        f"- Devices with any power entity: {c.get('power_measured_devices', 'n/a')}",
        "",
        "## Energy-monitor hardware",
        "",
        "| Device | Model | Manufacturer | Area | ID |",
        "| --- | --- | --- | --- | --- |",
    ]
    for d in result["energy_monitors"]:
        lines.append(
            f"| {d.get('name')} | {d.get('model')} | {d.get('manufacturer')} | "
            f"{d.get('area_id')} | `{d.get('id')}` |"
        )

    lines += ["", "## CT channels observed", ""]
    for dev_name, chans in sorted(by_dev.items()):
        lines.append(f"### {dev_name}")
        lines.append("")
        lines.append("| Entity | Label | Name | Current state | Unit | Category |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for ch in sorted(chans, key=lambda x: x["entity_id"]):
            lines.append(
                f"| `{ch['entity_id']}` | {ch['label']} | {ch['name']} | {ch['state']} | {ch['unit']} | {ch['state_class']} |"
            )
        lines.append("")

    # Everything below is derived from the audited payload.  Never hardcode a
    # household's entity ids here: this file generates a tracked document, and a
    # literal would be republished verbatim.
    monitor_names = [d["name"] for d in result["energy_monitors"] if d.get("name")]
    channel_total = len(result.get("ct_channels") or [])

    lines += [
        "## Inferred (not confirmed)",
        "",
        f"- {len(monitor_names)} energy-monitor device(s) expose "
        f"{channel_total} power channels between them.",
    ]
    for name in monitor_names:
        lines.append(f"- `{name}` exposes a set of CT1/CTn plus named branch channels.")
    lines += [
        "- Channel names describe the circuit they are *wired to*, not the device",
        "  currently drawing on it. A shared branch carries several loads.",
        "- Double-pole 240 V loads appear as a `<Name> CT1` / `<Name> CT2` pair",
        "  (each leg on one phase, half the load per leg).",
        "",
        "## Ambiguities requiring confirmation",
        "",
        "1. Which channel pair is the utility service entrance vs. a sub-feed.",
        "   Nesting must be confirmed by the operator.",
        "2. Sign convention / polarity of each channel.",
        "3. Which CTs are double-pole halves of one load. Pairs detected from",
        "   naming alone are candidates: two circuits that merely share a name",
        "   stem are separate loads.",
        "4. Whether repeated identical values are real or template duplicates.",
        "",
        "## Sampling behaviour",
        "",
        "See `data/ct_update_profile.json` and `docs/ct_update_profile.md`. HA does not",
        "expose a physical measurement timestamp; `last_updated` is a state update time.",
        "Repeated identical values are **not** treated as new measurements.",
        "",
        "## Duplicate power entity names",
        "",
    ]
    if result["duplicate_power_names"]:
        dups = result["duplicate_power_names"]
        if isinstance(dups, dict):
            for name, ids in dups.items():
                lines.append(f"- **{name}**: {', '.join(f'`{i}`' for i in ids)}")
        else:
            for entry in dups:
                if isinstance(entry, dict):
                    label = entry.get("duplicate_ct_id") or entry.get("ct_id") or "duplicate"
                    other = entry.get("duplicate_of") or entry.get("of")
                    suffix = f" → `{other}`" if other else ""
                    lines.append(f"- `{label}`{suffix}")
                else:
                    lines.append(f"- {entry}")
    else:
        lines.append("- None detected.")
    lines.append("")
    lines.append("## Reproducibility")
    lines.append("")
    lines.append("```bash")
    lines.append(".venv/bin/python tools/audit/audit_environment.py")
    lines.append(".venv/bin/python tools/audit/collect_samples.py --minutes 45")
    lines.append(".venv/bin/python tools/audit/profile_samples.py")
    lines.append("```")
    lines.append("")

    return "\n".join(lines)
def write_doc(result: dict[str, Any], path: Path | None = None) -> None:
    """Render the markdown report for an audit payload."""
    target = path or (DOCS / "ha_environment_audit.md")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_render(result))


def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--from",
        dest="source",
        type=Path,
        default=None,
        help="Render the markdown report from an existing audit JSON instead of "
        "querying Home Assistant (used to publish the synthetic example).",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Markdown output path (default: docs/ha_environment_audit.md).",
    )
    args = ap.parse_args()

    if args.source is not None:
        payload = json.loads(args.source.read_text())
        write_doc(payload, args.out)
        print(
            json.dumps(
                {
                    "rendered_from": str(args.source),
                    "out": str(args.out or "docs/ha_environment_audit.md"),
                }
            )
        )
        return

    result = asyncio.run(audit())
    write_outputs(result)
    print(
        json.dumps(
            {
                "power_entities": result["counts"]["power_entities"],
                "ct_channels": len(result["ct_channels"]),
                "devices": len(result["devices"]),
                "energy_monitors": result["counts"]["energy_monitors"],
            }
        )
    )


if __name__ == "__main__":
    main()
