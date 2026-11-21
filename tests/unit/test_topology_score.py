"""Unit tests for the pure topology scorer (spec §3.3).

Hand-built 2-leg ground truth: 2 mains (one per leg), 2 sub-panels and 6
children.  No engine imports, no database, no network.  Covers a perfect match,
ancestor-only placement, cross-leg violations, transitive reduction, lifecycle
levels, unscorable children, reversed edges and alias checks.
"""

from __future__ import annotations

from tools.dev import topology_score

M1, M2 = "ct_main_a", "ct_main_b"
SUB1, SUB2 = "ct_sub_a", "ct_sub_b"
K1, K2, K3 = "ct_kettle", "ct_dryer", "ct_oven"
K4, K5, K6 = "ct_tv", "ct_fridge", "ct_light"

NAMES = {
    M1: "Main A",
    M2: "Main B",
    SUB1: "Sub A",
    SUB2: "Sub B",
    K1: "Kettle",
    K2: "Dryer",
    K3: "Oven",
    K4: "TV",
    K5: "Fridge",
    K6: "Light",
}

GT = {
    "schema": "wattmosaic.topology_ground_truth/1",
    "readers": [
        {"name": name, "reader_id": rid, "power_entity": f"sensor.{rid}_power", "factor_inputs": []}
        for rid, name in NAMES.items()
    ],
    "contains": [
        {"parent": SUB1, "child": K1},
        {"parent": SUB1, "child": K2},
        {"parent": M1, "child": SUB1},
        {"parent": SUB2, "child": K3},
        {"parent": M2, "child": SUB2},
        {"parent": M2, "child": K6},
    ],
    "paired_leg": [[M1, M2]],
    "same_conductor": [{"alias": K4, "canonical": K2}],
    "one_leg_240v": [],
    "legs": {
        "A": [M1, SUB1, K1, K2],
        "B": [M2, SUB2, K3, K6],
    },
}

ALL_CHILDREN = [c["child"] for c in GT["contains"]]
ACTIVITY = {child: 5 for child in ALL_CHILDREN}


def edge(source: str, target: str, relation: str = "contains", lifecycle: str = "confirmed", **kw):
    result = {"source": source, "target": target, "relation": relation, "lifecycle": lifecycle}
    result.update(kw)
    return result


def perfect_edges() -> list[dict]:
    return [edge(item["parent"], item["child"]) for item in GT["contains"]] + [
        edge(M1, M2, "paired_leg"),
        edge(K4, K2, "same_conductor"),
    ]


def test_perfect_match_scores_f1_one() -> None:
    result = topology_score.score(perfect_edges(), GT)
    assert result["contains_direct"]["structural"]["f1"] == 1.0
    assert result["contains_direct"]["structural_or_soft"]["f1"] == 1.0
    assert result["contains_closure"]["structural"]["f1"] == 1.0
    assert result["contains_direct"]["structural"]["fp"] == []
    assert result["contains_direct"]["structural"]["fn"] == []
    assert all(v["found"] for v in result["paired_leg"])
    assert all(v["found"] for v in result["same_conductor"])
    assert all(not items for items in result["violations"].values())


def test_ancestor_only_is_closure_tp_but_not_direct() -> None:
    # Kettle is really under SUB1; placing it directly under M1 is ancestor-only.
    edges = perfect_edges()
    edges = [e for e in edges if not (e["source"] == SUB1 and e["target"] == K1)]
    edges.append(edge(M1, K1))
    result = topology_score.score(edges, GT)

    child = next(c for c in result["per_child"] if c["child"] == K1)
    assert child["status"] == "ancestor_only"
    # Direct M1->K1 is a false positive, but the closure still credits M1 as an
    # ancestor of the true parent (SUB1), so K1 is a closure TP.
    assert [M1, K1] in result["contains_direct"]["structural"]["fp"]
    assert [SUB1, K1] in result["contains_direct"]["structural"]["fn"]
    assert [M1, K1] in result["contains_closure"]["structural"]["tp"]
    # The same placement is also a sub-child directly under its grandparent.
    assert any(
        v["parent"] == M1 and v["child"] == K1
        for v in result["violations"]["sub_child_direct_under_main"]
    )


def test_sub_child_direct_under_grandparent_is_a_violation() -> None:
    # SUB1 is a child of M1 and K1 is a child of SUB1: M1->K1 is the violation.
    edges = perfect_edges() + [edge(M1, K1)]
    result = topology_score.score(edges, GT)
    assert any(
        v["parent"] == M1 and v["child"] == K1
        for v in result["violations"]["sub_child_direct_under_main"]
    )


def test_cross_leg_edge_raises_violation_and_status() -> None:
    # Dryer is on leg A; putting it under sub B (leg B) is cross-leg.
    edges = perfect_edges()
    edges = [e for e in edges if not (e["source"] == SUB1 and e["target"] == K2)]
    edges.append(edge(SUB2, K2))
    result = topology_score.score(edges, GT)
    child = next(c for c in result["per_child"] if c["child"] == K2)
    assert child["status"] == "cross_leg"
    assert any(
        v["parent"] == SUB2 and v["child"] == K2
        for v in result["violations"]["cross_leg_contains"]
    )


def test_transitive_reduction_prevents_double_fp() -> None:
    # Add a spurious grandparent edge M1 -> K2 alongside the correct SUB1 -> K2.
    # Without transitive reduction both would be counted as direct FPs; with it
    # only the redundant M1 -> K2 is dropped.
    edges = perfect_edges() + [edge(M1, K2)]
    result = topology_score.score(edges, GT)
    assert result["contains_direct"]["structural"]["fp"] == []
    # The relevant edges (before reduction) include the redundant one.
    raw = topology_score._transitive_reduction({(M1, SUB1), (SUB1, K2), (M1, K2)})
    assert raw == {(M1, SUB1), (SUB1, K2)}


def test_lifecycle_levels_separate_structural_from_soft() -> None:
    # Kettle's correct edge is soft only: absent from structural, present at the
    # structural_or_soft level.
    edges = perfect_edges()
    edges = [e for e in edges if not (e["source"] == SUB1 and e["target"] == K1)]
    edges.append(edge(SUB1, K1, lifecycle="provisional"))
    result = topology_score.score(edges, GT)
    assert [SUB1, K1] in result["contains_direct"]["structural"]["fn"]
    assert [SUB1, K1] in result["contains_direct"]["structural_or_soft"]["tp"]
    assert result["contains_direct"]["structural"]["f1"] < 1.0
    assert result["contains_direct"]["structural_or_soft"]["f1"] == 1.0
    # Lifecycle names are parameters, not hard-coded.
    custom = topology_score.score(
        [edge(SUB1, K1, lifecycle="rigid")],
        GT,
        structural=("rigid",),
        soft=(),
    )
    assert [SUB1, K1] in custom["contains_direct"]["structural"]["tp"]


def test_unscorable_children_excluded_from_recall_scorable() -> None:
    # Drop three correct edges; each child had < 3 changes, so those misses are
    # excluded from recall_scorable.
    missed = [SUB1, SUB2, K6]
    edges = perfect_edges()
    edges = [e for e in edges if e.get("target") not in missed]
    activity = {child: 5 for child in ALL_CHILDREN}
    for child in missed:
        activity[child] = 0
    result = topology_score.score(edges, GT, activity_by_reader=activity, min_events=3)
    assert set(result["unscorable"]) == set(missed)
    assert result["recall_scorable"]["recall"] == 1.0
    assert result["contains_direct"]["structural"]["recall"] < 1.0


def test_reversed_edges_are_flagged() -> None:
    edges = perfect_edges()
    edges = [e for e in edges if not (e["source"] == M1 and e["target"] == SUB1)]
    edges.append(edge(SUB1, M1))  # child contains parent
    result = topology_score.score(edges, GT)
    assert any(
        v["parent"] == M1 and v["child"] == SUB1
        for v in result["violations"]["child_contains_parent"]
    )


def test_alias_checks() -> None:
    # Missing SAME_CONDUCTOR and an alias used as a structural parent.
    edges = perfect_edges()
    edges = [e for e in edges if e["relation"] != "same_conductor"]
    edges.append(edge(K4, K6))  # alias is parent of a structural CONTAINS edge
    result = topology_score.score(edges, GT)
    assert any(
        v["alias"] == K4 for v in result["violations"]["alias_not_same_conductor"]
    )
    assert any(v["alias"] == K4 for v in result["violations"]["alias_with_children"])
    # The direction of SAME_CONDUCTOR is not significant.
    reversed_alias = perfect_edges()
    reversed_alias = [e for e in reversed_alias if e["relation"] != "same_conductor"]
    reversed_alias.append(edge(K2, K4, "same_conductor"))
    result2 = topology_score.score(reversed_alias, GT)
    assert result2["same_conductor"][0]["found"]


def test_extra_readers_and_markdown() -> None:
    edges = perfect_edges() + [edge("ct_ghost", M1)]
    result = topology_score.score(edges, GT)
    assert result["extra_readers"] == ["ct_ghost"]
    text = topology_score.format_markdown(result, NAMES)
    assert "# Topology score" in text
    assert "Main A" in text
    assert "Kettle" in text
    assert "alias_with_children" in text


def _child_status(result, child):
    return next(item for item in result["per_child"] if item["child"] == child)


def test_wrong_structural_parent_next_to_right_one_is_not_correct():
    """A cross-leg structural parent beside the true one must not score correct.

    Regression: the first replay of real history labelled a child "correct"
    whenever the true parent appeared at any lifecycle, even with a confirmed
    parent on the other leg.
    """
    edges = [
        edge(M1, SUB1),
        edge(M2, SUB2),
        edge(SUB1, K1),
        edge(M2, K1),  # confirmed, wrong leg
    ]
    result = topology_score.score(edges, GT, activity_by_reader=ACTIVITY)
    item = _child_status(result, K1)
    assert item["level"] == "structural"
    assert item["status"] == "correct_plus_extra"


def test_soft_grandparent_next_to_structural_parent_is_correct():
    edges = [edge(SUB1, K1), edge(M1, K1, lifecycle="provisional")]
    result = topology_score.score(edges, GT, activity_by_reader=ACTIVITY)
    item = _child_status(result, K1)
    assert item["level"] == "structural"
    assert item["status"] == "correct"


def test_soft_only_parent_is_judged_at_soft_level():
    edges = [edge(M2, K1, lifecycle="provisional")]
    result = topology_score.score(edges, GT, activity_by_reader=ACTIVITY)
    item = _child_status(result, K1)
    assert item["level"] == "soft"
    assert item["status"] == "cross_leg"


def test_score_placements_statuses():
    placements = {
        K1: {"parent": SUB1, "band": "high"},
        K2: {"parent": M1, "band": "low"},  # ancestor of the GT parent SUB1
        SUB1: {"parent": M2, "band": "medium"},  # wrong (cross-leg)
        K3: {"parent": None, "both_legs": [SUB2, SUB1], "band": "medium"},
        K6: {},
    }
    result = topology_score.score_placements(placements, GT)
    status = {row["child"]: row["status"] for row in result["children"]}
    assert status[K1] == "correct"
    assert status[K2] == "ancestor"
    assert status[SUB1] == "wrong"
    assert status[K3] == "both_legs_ok"
    assert status[K6] == "unplaced"
    assert status[SUB2] == "unplaced"
    assert result["counts"]["wrong"] == 1
    assert result["precision"] == 3 / 4
