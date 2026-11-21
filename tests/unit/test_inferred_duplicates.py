"""E6 §4: inferred SAME_CONDUCTOR from an affine copy, not co-sampled.

A co-sampled pair of readers that passes the fit is a balanced 240 V pair, not
a duplicate.  The fitted slope and offset are exposed as diagnostics.
"""

from __future__ import annotations

import numpy as np
from wattmosaic_engine.contracts import RelationType, TopologyEdgeState
from wattmosaic_engine.electrical.topology_inference import (
    co_sampled,
    detect_inferred_duplicates,
    inferred_duplicate_details,
)


def _days(n: int = 1500) -> list[float]:
    """n 2-minute buckets of a lively synthetic load."""
    rng = np.random.default_rng(3)
    return (300 + 100 * np.sin(np.arange(n) / 30.0) + rng.normal(0, 2, n)).tolist()


def test_co_sampled_timestamp_check() -> None:
    stamps = [float(i * 60) for i in range(100)]
    assert co_sampled(stamps, [t + 1 for t in stamps]) is True
    assert co_sampled(stamps, [t + 7 for t in stamps]) is False


def test_affine_copy_becomes_inferred_same_conductor() -> None:
    a = _days()
    # An affine copy: slope 0.95, offset +100 W (VEC-style), not co-sampled.
    b = [0.95 * x + 100 for x in a]
    series = {"main": a, "copy": b}
    edges = detect_inferred_duplicates(series)
    assert len(edges) == 1
    edge = edges[0]
    assert edge.relation == RelationType.SAME_CONDUCTOR
    assert edge.declared_by == "inference"
    assert edge.lifecycle == TopologyEdgeState.CANDIDATE
    detail = inferred_duplicate_details(series)[("copy", "main")]
    assert 0.9 <= detail["slope"] <= 1.1
    assert abs(detail["offset"]) <= 150
    assert detail["correlation"] >= 0.985


def test_co_sampled_pair_is_not_a_duplicate() -> None:
    a = _days()
    b = [0.98 * x + 20 for x in a]
    stamps_a = [float(i * 120) for i in range(len(a))]
    stamps_b = [t + 1 for t in stamps_a]
    edges = detect_inferred_duplicates(
        {"main": a, "leg": b},
        timestamps={"main": stamps_a, "leg": stamps_b},
    )
    assert edges == []


def test_each_reader_is_others_best_match() -> None:
    """A third reader that matches one copy better breaks the mutual pair."""
    a = _days()
    copy = [0.97 * x + 10 for x in a]
    closer = [0.995 * x + 2 for x in a]
    edges = detect_inferred_duplicates(
        {"main": a, "copy": copy, "closer": closer},
    )
    pair_ids = {frozenset((e.source_endpoint_id, e.target_endpoint_id)) for e in edges}
    # `main` is not `copy`'s best match (`closer` is), so the main/copy pair is
    # rejected; only mutual best matches become inferred duplicates.
    assert frozenset(("main", "copy")) not in pair_ids
