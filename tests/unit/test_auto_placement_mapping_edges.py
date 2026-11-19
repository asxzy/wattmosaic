"""ZT: an applied auto placement publishes v3 MappingEdge rows (S15).

Auto placement is an inference, not a user declaration.  The rows it writes
must carry ``declared_by="auto_placement"`` and start at CANDIDATE — one event
never promotes an edge past CANDIDATE (HB §4.3, DEC-10).  A CT id is the v3
endpoint id for a CT reader, so the placement's monitored CT becomes the edge's
``endpoint_id`` verbatim; a fixture without exactly one monitored CT is left
unmapped rather than fabricated onto a feed.
"""

from __future__ import annotations

from datetime import UTC, datetime

from wattmosaic_engine.contracts import (
    DeviceConfiguration,
    EdgeLifecycleV3,
    HaConnectivity,
    MappingEdge,
    MonitoringLevel,
    Observability,
)
from wattmosaic_engine.disaggregation.auto_placement import (
    Placement,
    PlacementPlan,
    mapping_edges_for_plan,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _placement(entity_id: str, ct_id: str) -> Placement:
    return Placement(
        device_id=f"dev_auto_{entity_id}",
        configuration=DeviceConfiguration(
            device_id=f"dev_auto_{entity_id}",
            connectivity=HaConnectivity.CONNECTED,
            monitoring=MonitoringLevel.SHARED,
            state_entity_ids=[entity_id],
            monitored_by_ct_ids=[ct_id],
            configured_by="auto",
        ),
        evidence=("measured 20 W", "corroborated"),
    )


def test_mapping_edges_are_auto_placement_candidates() -> None:
    plan = PlacementPlan(accepted=(_placement("light.one", "ct1"),))
    edges = mapping_edges_for_plan(plan, valid_from=T0)

    assert len(edges) == 1
    edge = edges[0]
    assert isinstance(edge, MappingEdge)
    assert edge.declared_by == "auto_placement"
    assert edge.lifecycle == EdgeLifecycleV3.CANDIDATE.value
    assert edge.observability == Observability.INFERRED.value
    assert edge.subject_kind == "device"
    assert edge.subject_id == "dev_auto_light.one"
    assert edge.endpoint_id == "ct1"
    assert edge.valid_from == T0
    assert edge.evidence.support == 2
    assert edge.evidence.independent_causes == 1


def test_a_fixture_without_one_monitored_ct_is_not_fabricated() -> None:
    def placement(entity_id: str, ct_ids: list[str]) -> Placement:
        return Placement(
            device_id=f"dev_auto_{entity_id}",
            configuration=DeviceConfiguration(
                device_id=f"dev_auto_{entity_id}",
                connectivity=HaConnectivity.CONNECTED,
                monitoring=MonitoringLevel.SHARED,
                state_entity_ids=[entity_id],
                monitored_by_ct_ids=ct_ids,
                configured_by="auto",
            ),
            evidence=("measured 20 W",),
        )

    assert mapping_edges_for_plan(
        PlacementPlan(
            accepted=(placement("light.zero", []), placement("light.two", ["ct1", "ct2"]))
        ),
        valid_from=T0,
    ) == ()


def test_edges_are_deterministically_ordered() -> None:
    plan = PlacementPlan(
        accepted=(
            _placement("light.z", "ct2"),
            _placement("light.a", "ct1"),
        )
    )
    edges = mapping_edges_for_plan(plan, valid_from=T0)
    assert [e.mapping_edge_id for e in edges] == sorted(
        e.mapping_edge_id for e in edges
    )


def test_applied_placement_writes_mapping_edges_via_the_version_service(
    tmp_path: object,
) -> None:
    """The full apply path persists auto_placement edges through S15/D1."""
    import asyncio
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import test_discover_placements as helpers  # noqa: PLC0415

    core, db, _ = helpers._core(Path(str(tmp_path)), auto_placement=True)
    core._wire_v3_services()
    result = asyncio.run(core.discover_placements(lookback_hours=24, apply=True))

    assert result["applied"] is True
    assert result.get("mapping_version_id")
    rows = db.repos.mapping.mapping_edges(core.version_service.mapping_version_id())
    assert rows
    assert all(row["declared_by"] == "auto_placement" for row in rows)
    assert all(row["lifecycle"] == EdgeLifecycleV3.CANDIDATE.value for row in rows)
    assert {row["subject_id"] for row in rows} == {
        helpers.auto_device_id("light.first"),
        helpers.auto_device_id("light.second"),
    }
