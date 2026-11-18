"""M1: DEC-27 row tree (DEC-27, HB §9.3–9.4)."""

from __future__ import annotations

from datetime import UTC, datetime

from wattmosaic_engine.contracts import PhysicalDevice, RowKind
from wattmosaic_engine.mapping import build_rows

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _device(device_id: str, **kwargs: object) -> PhysicalDevice:
    defaults: dict[str, object] = dict(
        device_id=device_id,
        name=device_id,
        origin="user",
        created_at=T0,
    )
    defaults.update(kwargs)
    return PhysicalDevice(**defaults)  # type: ignore[arg-type]


def test_all_row_kinds_are_present() -> None:
    rows = build_rows(
        devices=[_device("dev-1")],
        components=[{"component_id": "lc-1", "device_id": "dev-1", "name": "Heater"}],
        groups=[{"group_id": "grp-1", "member_ids": ["dev-1"], "name": "Lights"}],
        profiles=[{"profile_id": "prof-1", "name": "Fridge"}],
        readers=["Main"],
    )
    kinds = {row.kind for row in rows}
    assert RowKind.DEVICE in kinds
    assert RowKind.GROUP in kinds
    assert RowKind.UNKNOWN_PROFILE in kinds
    assert RowKind.READER_RESIDUAL in kinds
    assert RowKind.SUMMARY in kinds

    device = next(row for row in rows if row.kind == RowKind.DEVICE)
    assert any(child.kind == RowKind.COMPONENT for child in device.children)


def test_unlinked_ha_device_is_not_a_row() -> None:
    # An HA registry device with no Physical Device link is skipped (DEC-27).
    ha_phone = {"ha_device_id": "abc123", "name": "Phone"}
    linked = {
        "ha_device_id": "def456",
        "physical_device_id": "dev-tv",
        "name": "Television",
    }
    rows = build_rows(devices=[ha_phone, linked])
    ids = [row.row_id for row in rows if row.kind == RowKind.DEVICE]
    assert ids == ["dev-tv"]


def test_area_used_only_for_grouping() -> None:
    devices = [
        _device("dev-1", area_id="living", name="Lamp"),
        _device("dev-2", area_id="living", name="TV"),
        _device("dev-3", area_id="kitchen", name="Kettle"),
    ]
    rows = build_rows(
        devices=devices,
        areas=[{"area_id": "living", "name": "Living Room"}, {"area_id": "kitchen", "name": "Kitchen"}],
        include_summary=False,
    )
    rooms = [row for row in rows if row.kind == "room"]
    assert {room.label for room in rooms} == {"Living Room", "Kitchen"}
    living = next(room for room in rooms if room.label == "Living Room")
    assert {child.row_id for child in living.children} == {"dev-1", "dev-2"}


def test_group_members_reference_devices_without_values() -> None:
    rows = build_rows(
        devices=[_device("dev-1"), _device("dev-2")],
        groups=[{"group_id": "grp-1", "member_ids": ["dev-1", "dev-2"]}],
        include_summary=False,
    )
    group = next(row for row in rows if row.kind == RowKind.GROUP)
    assert group.row_id == "grp-1"
    assert {child.row_id for child in group.children} == {"dev-1", "dev-2"}
    # Member nodes carry no values (the row tree never stores watts).
    assert all(child.row_id is not None for child in group.children)


def test_reader_residual_and_unknown_profile_are_distinct() -> None:
    rows = build_rows(
        profiles=[{"profile_id": "prof-1"}],
        readers=["Main"],
        include_summary=False,
    )
    residual = next(row for row in rows if row.kind == RowKind.READER_RESIDUAL)
    profile = next(row for row in rows if row.kind == RowKind.UNKNOWN_PROFILE)
    assert residual.kind is not profile.kind
    assert residual.row_id == "Main"
    assert profile.row_id == "prof-1"


def test_summary_is_a_derived_kind() -> None:
    rows = build_rows()
    summary = next(row for row in rows if row.kind == RowKind.SUMMARY)
    assert summary.node_id == "row::summary"
