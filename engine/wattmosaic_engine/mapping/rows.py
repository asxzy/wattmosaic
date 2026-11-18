"""DEC-27 row tree builder (M1, design §8 / HB §9.3–9.4).

Rows come from the canonical registry: Physical Devices and Load Components,
plus Device Group, Unknown Load Profile, Reader Residual and derived Summary
rows (``RowKind``).  Two rules from DEC-27 are enforced here:

* an HA-registry device that is **not** linked to a Physical Device is not a
  row (this avoids the duplicate empty HA row of ``analysis/E-…`` C4);
* HA ``area_id`` is used for **display grouping only** (room/folder nodes),
  never for membership.

The function is pure: no clock, no database.
"""

from __future__ import annotations

from collections.abc import Mapping as MappingABC
from collections.abc import Sequence
from typing import Any

from wattmosaic_engine.contracts import RowKind, RowNode

__all__: list[str] = ["build_rows"]


def _get(item: Any, *names: str) -> Any:
    """Read an attribute from a mapping or an object."""
    if isinstance(item, MappingABC):
        for name in names:
            if name in item:
                return item[name]
        return None
    for name in names:
        if hasattr(item, name):
            return getattr(item, name)
    return None


def _canonical_device_id(device: Any) -> str | None:
    """The Physical Device id a device entry resolves to, or ``None``.

    A canonical ``PhysicalDevice`` exposes ``device_id`` and is always a row.
    An HA-registry device exposes ``ha_device_id`` and is a row **only** when it
    carries a non-null link to a Physical Device (``physical_device_id`` or
    ``linked_device_id``); otherwise it is skipped (DEC-27).
    """
    device_id = _get(device, "device_id", "physical_device_id", "linked_device_id")
    if isinstance(device_id, str) and device_id:
        return device_id
    return None


def _is_ha_only(device: Any) -> bool:
    """Whether the entry is an unlinked HA-registry device (not a row)."""
    has_ha = _get(device, "ha_device_id", "ha_entity_id") is not None
    canonical = _canonical_device_id(device)
    return has_ha and canonical is None


def _label(item: Any, *fallbacks: str) -> str:
    for name in ("name", "label", "display_name", "group_id", "profile_id", "reader_id"):
        value = _get(item, name)
        if isinstance(value, str) and value:
            return value
    for fallback in fallbacks:
        if fallback:
            return str(fallback)
    return str(item)


def _area_name(area: Any) -> str:
    name = _get(area, "name", "label", "area_name")
    if isinstance(name, str) and name:
        return name
    return str(_get(area, "area_id", "id", area))


def _area_index(areas: Sequence[Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for area in areas:
        area_id = _get(area, "area_id", "id")
        if area_id is not None:
            result[str(area_id)] = _area_name(area)
    return result


def _components_by_device(components: Sequence[Any]) -> dict[str, list[Any]]:
    result: dict[str, list[Any]] = {}
    for component in components:
        device_id = _get(component, "device_id")
        if device_id is not None:
            result.setdefault(str(device_id), []).append(component)
    return result


def _device_nodes(
    devices: Sequence[Any],
    comps: dict[str, list[Any]],
    area_names: dict[str, str],
) -> tuple[list[RowNode], dict[str, RowNode]]:
    """Build DEVICE rows, grouping by area; return (top nodes, by-device map)."""
    grouped: dict[str, list[RowNode]] = {}
    ungrouped: list[RowNode] = []
    by_device: dict[str, RowNode] = {}

    for device in devices:
        if _is_ha_only(device):
            continue
        device_id = _canonical_device_id(device)
        if device_id is None:
            continue
        children: list[RowNode] = []
        for component in comps.get(device_id, ()):
            component_id = _get(component, "component_id", "id")
            children.append(
                RowNode(
                    node_id=f"row::component::{component_id}",
                    kind=RowKind.COMPONENT,
                    label=_label(component, str(component_id)),
                    row_id=str(component_id) if component_id is not None else None,
                )
            )
        node = RowNode(
            node_id=f"row::device::{device_id}",
            kind=RowKind.DEVICE,
            label=_label(device, device_id),
            row_id=device_id,
            children=children,
        )
        by_device[device_id] = node
        area_id = _get(device, "area_id")
        if area_id is not None and str(area_id) in area_names:
            grouped.setdefault(str(area_id), []).append(node)
        else:
            ungrouped.append(node)

    result: list[RowNode] = []
    for area_id, nodes in grouped.items():
        result.append(
            RowNode(
                node_id=f"row::room::{area_id}",
                kind="room",
                label=area_names[area_id],
                children=nodes,
            )
        )
    result.extend(ungrouped)
    return result, by_device


def _group_nodes(
    groups: Sequence[Any],
    by_device: dict[str, RowNode],
    by_component: dict[str, RowNode],
) -> list[RowNode]:
    nodes: list[RowNode] = []
    for group in groups:
        group_id = _get(group, "group_id", "id")
        members = list(_get(group, "member_ids", "members", "members_ids") or ())
        children: list[RowNode] = []
        for member in members:
            member_id = str(member)
            target = by_device.get(member_id) or by_component.get(member_id)
            # A member reference carries no values (HB §8.4): just its id/kind.
            kind = RowKind.DEVICE if member_id in by_device else RowKind.COMPONENT
            children.append(
                RowNode(
                    node_id=f"row::group-member::{group_id}::{member_id}",
                    kind=kind,
                    label=target.label if target is not None else member_id,
                    row_id=member_id,
                )
            )
        nodes.append(
            RowNode(
                node_id=f"row::group::{group_id}",
                kind=RowKind.GROUP,
                label=_label(group, str(group_id)),
                row_id=str(group_id) if group_id is not None else None,
                children=children,
            )
        )
    return nodes


def build_rows(
    devices: Sequence[Any] = (),
    components: Sequence[Any] = (),
    groups: Sequence[Any] = (),
    profiles: Sequence[Any] = (),
    readers: Sequence[Any] = (),
    areas: Sequence[Any] = (),
    *,
    include_summary: bool = True,
) -> list[RowNode]:
    """Build the DEC-27 row tree.

    ``devices`` are Physical Device (or linked HA device) entries;
    ``components`` are Load Components; ``groups`` are Device Groups;
    ``profiles`` are Unknown Load Profiles; ``readers`` are reader ids/objects
    that get a Reader Residual row each; ``areas`` are display grouping labels.

    Returns the top-level rows in a deterministic order: devices (grouped by
    area), groups, unknown profiles, reader residuals and finally the derived
    summary row.
    """
    area_names = _area_index(areas)
    comps = _components_by_device(components)
    device_nodes, by_device = _device_nodes(devices, comps, area_names)

    by_component: dict[str, RowNode] = {}
    for comp_list in comps.values():
        for component in comp_list:
            component_id = str(_get(component, "component_id", "id"))
            by_component[component_id] = RowNode(
                node_id=f"row::component::{component_id}",
                kind=RowKind.COMPONENT,
                label=_label(component, component_id),
                row_id=component_id,
            )

    rows: list[RowNode] = list(device_nodes)
    rows.extend(_group_nodes(groups, by_device, by_component))

    for profile in profiles:
        profile_id = _get(profile, "profile_id", "id")
        rows.append(
            RowNode(
                node_id=f"row::unknown_profile::{profile_id}",
                kind=RowKind.UNKNOWN_PROFILE,
                label=_label(profile, str(profile_id)),
                row_id=str(profile_id) if profile_id is not None else None,
            )
        )

    for reader in readers:
        reader_id = _get(reader, "reader_id", "endpoint_id") if not isinstance(reader, str) else reader
        reader_id = reader_id if isinstance(reader_id, str) else str(reader_id)
        rows.append(
            RowNode(
                node_id=f"row::reader_residual::{reader_id}",
                kind=RowKind.READER_RESIDUAL,
                label=f"{reader_id} residual",
                row_id=reader_id,
            )
        )

    if include_summary:
        rows.append(
            RowNode(
                node_id="row::summary",
                kind=RowKind.SUMMARY,
                label="Summary",
                row_id="summary",
            )
        )
    return rows
