"""E5 §3: live factor inference writes a monitor factor that T5 then honours.

The pull tool and the engine share one ``derive_factors`` implementation (the
pull-tool tests in ``test_ha_history_pull.py`` stay green through the import).
This test drives the engine job directly: a synthetic ×2 reader with companion
rings produces a factor row, and the runtime fold then gives the child support.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from wattmosaic_engine.contracts import TopologyEdgeState
from wattmosaic_engine.electrical.factor_inference import infer_factors_job
from wattmosaic_engine.electrical.factors import FactorRegistry
from wattmosaic_engine.electrical.topology_inference import DeltaObservation, TopologyInferenceView
from wattmosaic_engine.ingestion.companions import CompanionStore

T0 = datetime(2026, 11, 20, 0, 0, tzinfo=UTC)


class FakeFactorsRepo:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def factors_for_ct(self, ct_id: str) -> list[dict]:
        return [dict(r) for r in self.rows if r["ct_id"] == ct_id]

    def insert_measurement_factor(self, factor) -> None:
        self.rows.append(
            {
                "ct_id": factor.ct_id,
                "multiplier": factor.multiplier,
                "effective_from": factor.effective_from,
                "effective_to": factor.effective_to,
                "applied_by": factor.applied_by,
                "declared_by": factor.declared_by,
            }
        )


def _synthetic_x2() -> tuple[CompanionStore, list[dict]]:
    """One reader reporting r with V/I/PF such that f = 2."""
    store = CompanionStore()
    power_rows: list[dict] = []
    for hour in range(3):
        for offset in range(10):
            at = T0 + timedelta(hours=hour, seconds=offset * 10)
            # P = 400 W, V = 120 V, I = 5 A, PF = 0.333... -> P/(V*I*PF) ~ 2.
            v, i, pf, p = 120.0, 5.0, 1.0 / 3.0, 400.0
            store.observe("child", voltage_v=v, current_a=i, power_factor=pf, observed_at=at)
            power_rows.append({"t": at.isoformat(), "v": p})
    return store, power_rows


def test_job_appends_a_monitor_factor_and_t5_support_follows() -> None:
    store, power_rows = _synthetic_x2()
    repo = FakeFactorsRepo()
    report = infer_factors_job(
        store,
        [{"endpoint_id": "child", "power_rows": power_rows}],
        factors_repo=repo,
        now=T0 + timedelta(hours=3),
    )
    assert report["appended"] >= 1
    rows = repo.factors_for_ct("child")
    assert rows
    assert rows[0]["multiplier"] == 2.0
    assert rows[0]["applied_by"] == "monitor"
    assert rows[0]["declared_by"] == "inferred"

    # A second run sees no change (the first interval is open and identical).
    again = infer_factors_job(
        store,
        [{"endpoint_id": "child", "power_rows": power_rows}],
        factors_repo=repo,
        now=T0 + timedelta(hours=4),
    )
    assert again["appended"] == 0

    # The factor is picked up by the registry and the T5 fold scales the child.
    registry = FactorRegistry(
        [
            __import__("wattmosaic_engine.contracts", fromlist=["MeasurementFactor"]).MeasurementFactor(
                ct_id=r["ct_id"],
                multiplier=r["multiplier"],
                effective_from=r["effective_from"],
                effective_to=r["effective_to"],
                applied_by=r["applied_by"],
                declared_by=r["declared_by"],
            )
            for r in rows
        ]
    )
    step = T0 + timedelta(hours=1)
    resolution = registry.resolve("child", step)
    assert resolution.reported_multiplier == 2.0

    view = TopologyInferenceView(
        at=step,
        absolute={
            "parent_a": [200.0] * 40,
            "parent_b": [200.0] * 40,
            "child": [400.0] * 40,
        },
    )
    for i in range(10):
        at = step + timedelta(minutes=i)
        view = view.observe_group(
            DeltaObservation(
                group_id=f"g-{i}",
                deltas={"parent_a": (50.0, 1.0), "parent_b": (50.0, 1.0), "child": (100.0, 1.0)},
                at=at,
                cause_id=f"c-{i}",
            ),
            scales={"child": 0.5},
            observed={
                "parent_a": (1.0, 50.0),
                "parent_b": (1.0, 50.0),
                "child": (1.0, 100.0),
            },
        )
    edge = view.edge("parent_a", "child")
    assert edge is not None
    assert edge.evidence.support >= 8
    assert edge.lifecycle == TopologyEdgeState.STRUCTURAL


def test_user_declared_factor_is_never_overwritten() -> None:
    store, power_rows = _synthetic_x2()
    repo = FakeFactorsRepo()
    repo.rows.append(
        {
            "ct_id": "child",
            "multiplier": 1.0,
            "effective_from": datetime(2000, 1, 1, tzinfo=UTC),
            "effective_to": None,
            "applied_by": "user",
            "declared_by": "user",
        }
    )
    report = infer_factors_job(
        store,
        [{"endpoint_id": "child", "power_rows": power_rows}],
        factors_repo=repo,
        now=T0 + timedelta(hours=3),
    )
    assert report["appended"] == 0
    assert all(r["applied_by"] != "monitor" for r in repo.factors_for_ct("child"))
