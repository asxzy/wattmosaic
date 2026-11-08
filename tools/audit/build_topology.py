"""Build a topology candidate from the real HA audit.

Reads ``data/ha_environment_audit.json`` and produces
``data/topology_candidates.json`` plus a loadable ``Topology``.

Nothing here is committed as fact: every channel and device is emitted with
``topology_status="candidate"``.  The user must confirm.

If ``data/state_entity_candidates.json`` exists (written by
``derive_state_entities.py`` from measured recorder history), each device also
receives the evidence-backed state link, the per-device state-label overrides,
and the polarity correction.  Only ``supported``/``weak`` verdicts are attached:
a ``confounded``/``ambiguous``/``unsupported`` device is left without a state
entity so the engine cannot attribute power to it, and a ``duplicate_of``
channel gets no link at all because two CTs on one conductor would double-count.

Run: python tools/audit/derive_state_entities.py --from-cache ...
     python tools/audit/build_topology.py
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from wattmosaic_engine.contracts import (
    CTChannel,
    CTType,
    DeviceTopology,
    Phase,
    Polarity,
    Topology,
    TopologyStatus,
)

ROOT = Path(__file__).resolve().parents[2]
#: Source directory for the audit inputs.  Overridable so the published
#: synthetic fixture can be built and tested without a real installation
#: (``python tools/audit/build_topology.py --data data/examples/reference_house``)
#: and so tests do not depend on a real household inventory.
DATA = Path(os.environ.get("WM_AUDIT_DATA") or (ROOT / "data"))

#: Optional operator-specific knowledge.  Kept in a git-ignored file so the
#: published repository contains only generic logic — never a household's
#: entity ids.  Shape::
#:
#:     {
#:       "mains": {"sensor.<panel>_ct1_power": ["<ct_id>", "l1", "<Panel>"], ...},
#:       "double_pole_prefixes": ["water_heater", "dishwasher"]
#:     }
#:
#: Everything is optional; each field falls back to auditing the CT channels.
INSTALL_PROFILE = "install_profile.json"


def _load_install_profile() -> dict:
    path = DATA / INSTALL_PROFILE
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _derive_mains(channels: list[dict]) -> dict[str, tuple[str, Phase, str]]:
    """Infer panel mains from labelled CT1/CT2 channels.

    A multi-channel energy-meter panel exposes its two incoming phases as channels labelled
    ``ct1`` and ``ct2``.  Appliance double-pole legs use the same labels, so the
    mains are the *shortest* such slugs within each panel — ``panel_a_ct1`` is a
    main, ``panel_a_water_heater_ct1`` is a load.  Deriving them from the audit
    keeps this module free of installation-specific entity ids.
    """
    by_panel: dict[str, list[tuple[str, str, str]]] = {}
    for channel in channels:
        label = str(channel.get("label") or "").lower()
        if label not in ("ct1", "ct2"):
            continue
        panel = str(channel.get("device_name") or "")
        entity_id = str(channel.get("entity_id") or "")
        if not entity_id:
            continue
        object_id = entity_id.removeprefix("sensor.").removesuffix("_power")
        by_panel.setdefault(panel, []).append((entity_id, label, object_id))

    mains: dict[str, tuple[str, Phase, str]] = {}
    for panel, entries in by_panel.items():
        # The panel's own CTs have the least specific object id.
        shortest = min(len(object_id) for _, _, object_id in entries)
        prefix = _panel_prefix(panel)
        for entity_id, label, object_id in entries:
            if len(object_id) != shortest:
                continue
            phase = Phase.L1 if label == "ct1" else Phase.L2
            mains[entity_id] = (f"{prefix}_{phase.value}", phase, panel)
    return mains


def _panel_prefix(panel: str) -> str:
    """Stable, generic ct-id prefix for a panel name (``Main Panel`` -> ``main_panel``)."""
    cleaned = re.sub(r"[^a-z0-9]+", "_", panel.lower()).strip("_")
    return cleaned or "panel"


def _derive_double_pole_prefixes(channels: list[dict]) -> tuple[str, ...]:
    """Find ``<load>_ct1`` / ``<load>_ct2`` pairs — two legs of one 240 V load.

    The prefix is everything before ``_ct1``/``_ct2``, minus the panel segment,
    so ``panel_a_water_heater_ct1_power`` yields ``water_heater``.
    """
    found: set[str] = set()
    for channel in channels:
        object_id = str(channel.get("entity_id") or "").removeprefix("sensor.")
        match = re.match(r"^(?P<panel>.+?)_(?P<load>.+?)_ct[12]_power$", object_id)
        if match:
            found.add(match.group("load"))
    # Only keep a prefix if both legs exist under the same panel.
    result: set[str] = set()
    for prefix in found:
        for panel in {str(c.get("device_name") or "") for c in channels}:
            object_ids = {
                str(c.get("entity_id") or "").removeprefix("sensor.")
                for c in channels
                if str(c.get("device_name") or "") == panel
            }
            if any(o.endswith(f"_{prefix}_ct1_power") for o in object_ids) and any(
                o.endswith(f"_{prefix}_ct2_power") for o in object_ids
            ):
                result.add(prefix)
    return tuple(sorted(result))


def slug(entity_id: str) -> str:
    s = entity_id.removeprefix("sensor.").removesuffix("_power")
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def _load_state_mapping() -> dict[str, dict]:
    """Evidence-backed state links from ``derive_state_entities.py``, if present."""
    path = DATA / "state_entity_candidates.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    devices = payload.get("devices")
    return devices if isinstance(devices, dict) else {}


def _load_polarity() -> dict[str, str]:
    """``ct_id -> "inverted"`` from measured polarity evidence, if present."""
    path = DATA / "state_entity_candidates.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    channels = payload.get("channels")
    if not isinstance(channels, dict):
        return {}
    out: dict[str, str] = {}
    for entity_id, report in channels.items():
        if not isinstance(report, dict):
            continue
        evidence = report.get("polarity_evidence")
        if isinstance(evidence, dict) and evidence.get("candidate_polarity") == "inverted":
            out[str(entity_id)] = "inverted"
    return out


def _mapping_for(device_id: str, mapping: dict[str, dict]) -> dict:
    """The parts of a mapping entry a topology may safely consume.

    Refuses anything that is not a positive, single-entity verdict.  A
    ``confounded`` device has two loads on one channel, an ``ambiguous`` one has
    two equally plausible entities, and a ``duplicate_of`` one is the same
    conductor as another channel — in all three cases attaching a state entity
    would let the engine attribute power it cannot justify.
    """
    entry = mapping.get(device_id)
    if not isinstance(entry, dict):
        return {}
    if entry.get("status") not in {"supported", "weak"}:
        return {}
    entity_ids = [str(item) for item in entry.get("state_entity_ids") or [] if item]
    if len(entity_ids) != 1:
        return {}
    if entry.get("duplicate_of"):
        return {}
    return {
        "state_entity_ids": entity_ids,
        "state_overrides": dict(entry.get("state_overrides") or {}),
        "status": entry["status"],
        "reason": entry.get("reason"),
        "evidence": dict(entry.get("evidence") or {}),
    }


def build() -> Topology:
    audit = json.loads((DATA / "ha_environment_audit.json").read_text())
    mapping = _load_state_mapping()
    inverted = _load_polarity()
    profile = _load_install_profile()
    channels_raw = audit["ct_channels"]

    # Mains: explicit install profile if present, else derived from labels.
    mains: dict[str, tuple[str, Phase, str]] = {}
    for entity_id, spec in (profile.get("mains") or {}).items():
        if isinstance(spec, (list, tuple)) and len(spec) >= 2:
            mains[entity_id] = (spec[0], Phase(spec[1]), spec[2] if len(spec) > 2 else "")
    if not mains:
        mains = _derive_mains(channels_raw)

    # A third CT pair that is a separate measurement group (never summed).
    load_manager: dict[str, tuple[str, Phase]] = {}
    for entity_id, spec in (profile.get("load_manager") or {}).items():
        if isinstance(spec, (list, tuple)) and len(spec) >= 2:
            load_manager[entity_id] = (spec[0], Phase(spec[1]))

    double_pole = (
        tuple(profile["double_pole_prefixes"])
        if profile.get("double_pole_prefixes")
        else _derive_double_pole_prefixes(channels_raw)
    )

    # A prefix is only a genuine 240 V load if it has *exactly* two legs.
    # Separate circuits named `circuit_ct1`/`circuit_ct2`/`circuit_ct3`
    # are three distinct loads that share a name stem; pairing the first two
    # would fuse two unrelated circuits into one device (defect #28).  The same
    # rule keeps a lone `_ct1` with no mate from being paired.
    def _legs(prefix: str) -> int:
        return sum(
            1
            for c in channels_raw
            if re.search(rf"_{re.escape(prefix)}_ct[0-9]+_power$", str(c.get("entity_id") or ""))
        )

    double_pole = tuple(p for p in double_pole if _legs(p) == 2)

    channels: list[CTChannel] = []
    seen: set[str] = set()

    def add(ct_id: str, entity_id: str, name: str, ct_type: CTType, phase: Phase,
            parent: str | None, paired: str | None = None, device_id: str | None = None) -> None:
        if ct_id in seen:
            return
        seen.add(ct_id)
        channels.append(
            CTChannel(
                ct_id=ct_id,
                entity_id=entity_id,
                name=name,
                ct_type=ct_type,
                phase=phase,
                parent_ct_id=parent,
                paired_ct_id=paired,
                device_id=device_id,
                polarity=Polarity.NORMAL,
                topology_status=TopologyStatus.CANDIDATE,
                expected_interval_s=15.7,  # measured median, see ct_update_profile.json
                reliability=0.9,
                metadata={"source": "audit", "panel": name.split(" ")[0] if name else ""},
            )
        )

    # Mains
    for entity_id, (ct_id, phase, panel) in mains.items():
        add(ct_id, entity_id, f"{panel} {phase.value.upper()}".strip(), CTType.MAIN, phase, None)
    for entity_id, (ct_id, phase) in load_manager.items():
        add(ct_id, entity_id, f"Feed {phase.value.upper()}", CTType.MAIN, phase, None)

    # Branch + dedicated channels
    for ch in channels_raw:
        eid = ch["entity_id"]
        if eid in mains or eid in load_manager:
            continue
        ct_id = slug(eid)
        panel = ch.get("device_name") or "unknown"
        # Parent: the panel's own L1 main, when this channel belongs to a panel
        # we recognised.  A channel from an unknown panel gets no parent rather
        # than a guessed one.
        panel_prefix = _panel_prefix(panel)
        parent = f"{panel_prefix}_l1" if any(
            cid == f"{panel_prefix}_l1" for cid, _, _ in mains.values()
        ) else None
        # Heuristic phase: channels named `*_ct1` / `*_ct2` are double-pole legs.
        m = re.search(r"_(ct)([12])_power$", eid)
        phase = Phase.L1 if m and m.group(2) == "1" else Phase.L2 if m else Phase.UNKNOWN
        # A named appliance channel is a *dedicated* CT for that appliance if a
        # single channel exists; if it is one leg of a paired set it is a branch.
        is_leg = bool(m)
        add(
            ct_id,
            eid,
            ch.get("name") or eid,
            CTType.BRANCH if is_leg else CTType.DEDICATED_DEVICE,
            phase,
            parent,
        )

    # Polarity: a load CT that read negative for the entire observation window on
    # a house with no generation is a reversed clamp, not export.  Correcting it
    # here keeps the sign honest; it does not change what the channel is.
    for channel in channels:
        if inverted.get(channel.entity_id):
            channel.polarity = Polarity.INVERTED
            channel.metadata["polarity_evidence"] = (
                "negative for the whole observation window with no generation entity present"
            )

    # Pair the double-pole legs.  ct_ids are panel-prefixed, so match on suffix.
    for prefix in double_pole:
        l1 = next((c for c in channels if c.ct_id.endswith(f"{prefix}_ct1")), None)
        l2 = next((c for c in channels if c.ct_id.endswith(f"{prefix}_ct2")), None)
        if l1 and l2:
            l1.paired_ct_id = l2.ct_id
            l2.paired_ct_id = l1.ct_id
            l1.phase = Phase.L1
            l2.phase = Phase.L2
            l1.metadata["pair_confidence"] = "name_derived"
            l2.metadata["pair_confidence"] = "name_derived"

    # Device topology: a logical device per channel, EXCEPT genuine double-pole
    # pairs (both legs have paired_ct_id set by explicit evidence above).
    devices: list[DeviceTopology] = []
    paired_handled: set[str] = set()
    for c in channels:
        if c.ct_type == CTType.MAIN or c.ct_id in paired_handled:
            continue
        if c.paired_ct_id:
            mate = next((x for x in channels if x.ct_id == c.paired_ct_id), None)
            if mate is None:
                # Dangling pair reference: treat as a single-pole branch.
                device_id = f"dev_{c.ct_id}"
                metadata = {"note": "paired_ct_id unresolved; requires confirmation"}
                metadata.update(_mapping_for(device_id, mapping))
                devices.append(
                    DeviceTopology(
                        device_id=device_id,
                        name=c.name,
                        branch_ct_ids=[c.ct_id],
                        phase=c.phase,
                        topology_status=TopologyStatus.CANDIDATE,
                        metadata=metadata,
                    )
                )
                continue
            paired_handled.update({c.ct_id, mate.ct_id})
            base = c.ct_id[: -len("_ct1")] if c.ct_id.endswith("_ct1") else c.ct_id[: -len("_ct2")]
            device_id = f"dev_{base}"
            metadata = {
                "ct_group": [c.ct_id, mate.ct_id],
                "pair_confidence": "name_derived",
                "note": "two-pole load; combined power is the sum of both legs",
            }
            metadata.update(_mapping_for(device_id, mapping))
            devices.append(
                DeviceTopology(
                    device_id=device_id,
                    name=(c.name or base).rsplit(" CT", 1)[0],
                    branch_ct_ids=[c.ct_id, mate.ct_id],
                    phase=Phase.BOTH,
                    double_pole=True,
                    topology_status=TopologyStatus.CANDIDATE,
                    metadata=metadata,
                )
            )
            continue
        device_id = f"dev_{c.ct_id}"
        metadata: dict = {"note": "single channel; requires confirmation"}
        metadata.update(_mapping_for(device_id, mapping))
        # A channel's role decides *how* the device owns it.  Filing a `branch`
        # CT under `dedicated_ct_ids` makes `dedicated_channel_for` reject it
        # (that resolver requires `CTType.DEDICATED_DEVICE`), and the device then
        # has no attribution rows at all — it silently becomes invisible while
        # still being measured by a real CT.  Two lighting circuits were affected.
        # Explicit role check with a loud fallback: any *new* CTType must be
        # classified deliberately here rather than silently landing in a bucket
        # that a resolver will reject.
        if c.ct_type == CTType.DEDICATED_DEVICE:
            ownership: dict[str, list[str]] = {"dedicated_ct_ids": [c.ct_id]}
        elif c.ct_type == CTType.BRANCH:
            ownership = {"branch_ct_ids": [c.ct_id]}
        else:  # pragma: no cover - defensive
            raise ValueError(
                f"unhandled CTType {c.ct_type!r} for single-channel device {c.ct_id}; "
                "decide whether it is dedicated- or branch-owned"
            )
        devices.append(
            DeviceTopology(
                device_id=device_id,
                name=c.name,
                phase=c.phase,
                topology_status=TopologyStatus.CANDIDATE,
                metadata=metadata,
                **ownership,
            )
        )

    notes = [
        "All entries are candidates. Confirm before relying on inference.",
        "Main Panel and Sub Panel nesting is NOT assumed: they are separate measurement groups.",
        "Extra feed CTs are a separate main pair; relationship to panels is unknown.",
        "Double-pole pairs derived from *_ct1/*_ct2 naming only.",
        "Lighting channels CT1/CT2/CT3 are treated as separate circuits, not one load.",
    ]
    return Topology(
        version=1,
        status=TopologyStatus.CANDIDATE,
        channels=channels,
        devices=devices,
        notes=notes,
    )


def main() -> None:
    import argparse

    global DATA

    ap = argparse.ArgumentParser(description="Build a topology candidate from an HA audit.")
    ap.add_argument(
        "--data",
        type=Path,
        default=DATA,
        help="directory holding ha_environment_audit.json (default: data/)",
    )
    args = ap.parse_args()
    DATA = args.data

    topology = build()
    payload = topology.model_dump(mode="json")
    (DATA / "topology_candidates.json").write_text(json.dumps(payload, indent=2) + "\n")
    seen_entity = {c.entity_id for c in topology.channels}
    orphan = [
        e["entity_id"]
        for e in json.loads((DATA / "ha_environment_audit.json").read_text())["power_entities"]
        if e["entity_id"] not in seen_entity and e["device_id"]
    ]
    print(json.dumps({
        "channels": len(topology.channels),
        "devices": len(topology.devices),
        "double_pole": sum(1 for d in topology.devices if d.double_pole),
        "power_entities_not_in_topology": len(orphan),
        "mains": [c.ct_id for c in topology.channels if c.ct_type == CTType.MAIN],
    }, indent=2))


if __name__ == "__main__":
    main()
