"""Pure scorer for the topology inferred by ``replay_ground_truth``.

This module has **no engine imports**: it consumes plain edge dictionaries and a
machine-readable ground truth, so the scoring rules stay independent of (and
easy to test without) the running engine.  The lifecycle vocabulary is a
parameter because the engine's lifecycle names are about to change; it must not
be hard-coded anywhere else.

The ground truth is treated as a possibly-wrong human observation: this module
reports disagreements, never "fixes" the inferred topology.  Absent stays
absent (``None``/empty list), never a guessed 0 or a guessed parent.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from typing import Any

#: Default relation value used for containment edges.
CONTAINS = "contains"
PAIRED_LEG = "paired_leg"
SAME_CONDUCTOR = "same_conductor"

__all__ = ["score", "format_markdown"]


def _value(item: Any) -> str:
    """Lower-case value of a possible enum/str object."""
    return str(getattr(item, "value", item)).lower()


def _pair(a: Any, b: Any) -> frozenset[str]:
    """Undirected pair key, order-independent."""
    return frozenset((str(a), str(b)))


def _gt_contains(gt: Mapping[str, Any]) -> dict[str, str]:
    """``child -> parent`` map from the GT's *direct* CONTAINS entries."""
    result: dict[str, str] = {}
    for entry in gt.get("contains") or ():
        if isinstance(entry, Mapping):
            parent, child = entry.get("parent"), entry.get("child")
        elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
            parent, child = entry[0], entry[1]
        else:
            continue
        if parent is not None and child is not None and parent != child:
            result[str(child)] = str(parent)
    return result


def _gt_legs(gt: Mapping[str, Any]) -> dict[str, str]:
    """``reader -> leg`` map from the GT's ``legs`` object."""
    result: dict[str, str] = {}
    for leg, readers in (gt.get("legs") or {}).items():
        for reader in readers or ():
            result[str(reader)] = str(leg)
    return result


def _gt_reader_names(gt: Mapping[str, Any]) -> dict[str, str]:
    names: dict[str, str] = {}
    for reader in gt.get("readers") or ():
        if isinstance(reader, Mapping):
            rid = reader.get("reader_id")
            if rid is not None:
                names[str(rid)] = str(reader.get("name") or rid)
    return names


def _ancestors(parents: Mapping[str, str]) -> dict[str, set[str]]:
    """For every node, the set of all its ancestors (following parent links)."""
    out: dict[str, set[str]] = {}
    for child in parents:
        seen: set[str] = set()
        cur = parents.get(child)
        while cur is not None and cur not in seen:
            seen.add(cur)
            cur = parents.get(cur)
        out[child] = seen
    return out


def _transitive_reduction(edges: set[tuple[str, str]]) -> set[tuple[str, str]]:
    """Drop an edge (a, c) when a path a -> b -> ... -> c also exists."""
    children: dict[str, set[str]] = defaultdict(set)
    for parent, child in edges:
        children[parent].add(child)

    def reachable(start: str, target: str) -> bool:
        stack = list(children.get(start, ()))
        seen: set[str] = set()
        while stack:
            node = stack.pop()
            if node == target:
                return True
            if node not in seen:
                seen.add(node)
                stack.extend(children.get(node, ()))
        return False

    reduced: set[tuple[str, str]] = set()
    for parent, child in edges:
        # Is there another first hop (not the direct child) reaching c?
        via_other = any(
            other != child and reachable(other, child) for other in children.get(parent, ())
        )
        if not via_other:
            reduced.add((parent, child))
    return reduced


def _closure(edges: Iterable[tuple[str, str]]) -> set[tuple[str, str]]:
    """Transitive closure of a parent -> child edge set."""
    children: dict[str, set[str]] = defaultdict(set)
    for parent, child in edges:
        children[parent].add(child)
    result = set(edges)
    for parent in list(children):
        stack = list(children[parent])
        seen: set[str] = set()
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            result.add((parent, node))
            stack.extend(children.get(node, ()))
    return result


def _pair_metrics(tp: list[Any], fp: list[Any], fn: list[Any]) -> dict[str, Any]:
    precision = len(tp) / (len(tp) + len(fp)) if tp or fp else 1.0
    recall = len(tp) / (len(tp) + len(fn)) if tp or fn else 1.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "tp": [list(pair) for pair in sorted(tp)],
        "fp": [list(pair) for pair in sorted(fp)],
        "fn": [list(pair) for pair in sorted(fn)],
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }


def score(
    edges: list[dict[str, Any]],
    gt: dict[str, Any],
    *,
    structural: Iterable[str] = ("confirmed", "structural"),
    soft: Iterable[str] = ("provisional",),
    activity_by_reader: Mapping[str, int] | None = None,
    min_events: int = 3,
) -> dict[str, Any]:
    """Score inferred topology edges against ``gt``.

    ``edges`` items carry ``source``, ``target``, ``relation`` (lower-case value)
    and ``lifecycle`` (lower-case value), optionally ``support``, ``conflict``,
    ``parent_only`` and ``declared_by``.  ``structural`` and ``soft`` name the
    lifecycle values treated as hard and soft CONTAINS evidence.  The lifecycle
    names are parameters, never hard-coded here, and their contents are lowered
    before comparison.

    ``activity_by_reader`` counts sample-to-sample changes >= 30 W per reader;
    a GT child with fewer than ``min_events`` is *unscorable* and its miss is
    excluded from ``recall_scorable``.
    """
    structural_set = {_value(name) for name in structural}
    soft_set = {_value(name) for name in soft}
    relevance = structural_set | soft_set

    gt_parents = _gt_contains(gt)  # child -> parent
    gt_direct = {(parent, child) for child, parent in gt_parents.items()}  # parent -> child
    gt_legs = _gt_legs(gt)
    names = _gt_reader_names(gt)
    gt_ancestors = _ancestors(gt_parents)
    activity = activity_by_reader or {}

    # Index inferred edges by relation/lifecycle.
    structural_contains: set[tuple[str, str]] = set()
    relevant_contains: set[tuple[str, str]] = set()
    contains_edge: dict[tuple[str, str], dict[str, Any]] = {}
    lifecycle_counts: Counter[tuple[str, str]] = Counter()
    edge_readers: set[str] = set()

    paired_seen: dict[frozenset[str], list[str]] = defaultdict(list)
    alias_seen: dict[tuple[str, str], list[str]] = defaultdict(list)

    for raw in edges:
        source = str(raw.get("source", ""))
        target = str(raw.get("target", ""))
        relation = _value(raw.get("relation", ""))
        lifecycle = _value(raw.get("lifecycle", ""))
        lifecycle_counts[(relation, lifecycle)] += 1
        edge_readers.update((source, target))
        detail = dict(raw)
        detail.setdefault("support", None)
        detail.setdefault("conflict", None)
        detail.setdefault("parent_only", None)

        if relation == CONTAINS:
            contains_edge[(source, target)] = detail
            if lifecycle in structural_set:
                structural_contains.add((source, target))
            if lifecycle in relevance:
                relevant_contains.add((source, target))
        elif relation == PAIRED_LEG:
            paired_seen[_pair(source, target)].append(lifecycle)
        elif relation == SAME_CONDUCTOR:
            alias_seen[(source, target)].append(lifecycle)

    # -- direct CONTAINS: transitive reduction before comparison ------------- #
    direct_levels: dict[str, Any] = {}
    for level, candidate in (
        ("structural", structural_contains),
        ("structural_or_soft", relevant_contains),
    ):
        reduced = _transitive_reduction(candidate)
        direct_levels[level] = _pair_metrics(
            tp=sorted(reduced & gt_direct),
            fp=sorted(reduced - gt_direct),
            fn=sorted(gt_direct - reduced),
        )

    # -- closure CONTAINS: credit a child under the right ancestor ----------- #
    gt_closure = _closure(gt_direct)
    closure_metrics: dict[str, Any] = {}
    for level, candidate in (
        ("structural", structural_contains),
        ("structural_or_soft", relevant_contains),
    ):
        closure_metrics[level] = _pair_metrics(
            tp=sorted(_closure(candidate) & gt_closure),
            fp=sorted(_closure(candidate) - gt_closure),
            fn=sorted(gt_closure - _closure(candidate)),
        )

    # -- per child ---------------------------------------------------------- #
    # Direct inferred parents per child, from relevant contains edges only.
    inferred_by_child: dict[str, list[tuple[str, str, dict[str, Any]]]] = defaultdict(list)
    for (parent, child), detail in contains_edge.items():
        lifecycle = _value(detail.get("lifecycle", ""))
        if lifecycle in relevance:
            inferred_by_child[child].append((parent, lifecycle, detail))

    per_child: list[dict[str, Any]] = []
    scorable_misses: list[str] = []
    unscorable: list[str] = []
    for child, true_parent in sorted(gt_parents.items()):
        parents = sorted(inferred_by_child.get(child, []), key=lambda item: item[0])
        gt_child_leg = gt_legs.get(child)
        gt_parent_leg = gt_legs.get(true_parent)
        details = [
            {
                "parent": parent,
                "lifecycle": lifecycle,
                "support": detail.get("support"),
                "conflict": detail.get("conflict"),
                "parent_only": detail.get("parent_only"),
                "declared_by": detail.get("declared_by"),
            }
            for parent, lifecycle, detail in parents
        ]

        # Judge at the strongest level present: structural parents if any, else
        # soft ones.  A soft alternative next to a structural parent is not an
        # error (a grandparent is kept as a soft transitive edge), but a wrong
        # parent at the same level as the right one is.
        strong = [item for item in parents if item[1] in structural_set]
        level: str | None = None
        if strong:
            level, judged = "structural", strong
        elif parents:
            level, judged = "soft", parents
        else:
            judged = []
        if not judged:
            status = "missing"
        else:
            statuses: list[str] = []
            for parent, _lifecycle, _detail in judged:
                if parent == true_parent:
                    statuses.append("correct")
                elif gt_child_leg is not None and gt_parent_leg is not None and gt_child_leg != gt_parent_leg:
                    # Inferred parent on the wrong leg relative to the child.
                    statuses.append("cross_leg")
                elif gt_child_leg is not None and gt_legs.get(parent) != gt_child_leg:
                    statuses.append("cross_leg")
                elif parent in gt_ancestors.get(child, set()):
                    statuses.append("ancestor_only")
                else:
                    statuses.append("wrong_parent")
            # One clear status: most-specific first, then any disagreement.
            if set(statuses) == {"correct"}:
                status = "correct"
            elif "correct" in statuses:
                status = "correct_plus_extra"
            elif "cross_leg" in statuses:
                status = "cross_leg"
            elif "ancestor_only" in statuses:
                status = "ancestor_only"
            else:
                status = "wrong_parent"

        unscorable_child = int(activity.get(child, 0) or 0) < min_events
        if unscorable_child:
            unscorable.append(child)
            if status in ("missing", "wrong_parent", "ancestor_only", "cross_leg"):
                scorable_misses.append(child)
        per_child.append(
            {
                "child": child,
                "child_name": names.get(child, child),
                "gt_parent": true_parent,
                "gt_parent_name": names.get(true_parent, true_parent),
                "inferred_parents": details,
                "status": status,
                "level": level,
                "unscorable": unscorable_child,
                "activity": int(activity.get(child, 0) or 0),
            }
        )

    # recall_scorable: same as structural recall but with unscorable misses
    # removed from fn (they were not observable in the replay).
    structural_metrics = direct_levels["structural"]
    scorable_fn = [pair for pair in structural_metrics["fn"] if pair[1] not in unscorable]
    scorable_tp = list(structural_metrics["tp"])
    scorable_fp = list(structural_metrics["fp"])
    sp = len(scorable_tp) / (len(scorable_tp) + len(scorable_fp)) if scorable_tp or scorable_fp else 1.0
    sr = len(scorable_tp) / (len(scorable_tp) + len(scorable_fn)) if scorable_tp or scorable_fn else 1.0
    sf1 = 2 * sp * sr / (sp + sr) if sp + sr else 0.0

    # -- violations --------------------------------------------------------- #
    violations: dict[str, list[Any]] = {
        "cross_leg_contains": [],
        "sub_child_direct_under_main": [],
        "alias_not_same_conductor": [],
        "alias_with_children": [],
        "pair_missing": [],
        "child_contains_parent": [],
    }

    for (parent, child), detail in sorted(contains_edge.items()):
        lifecycle = _value(detail.get("lifecycle", ""))
        if lifecycle not in relevance:
            continue
        child_leg, parent_leg = gt_legs.get(child), gt_legs.get(parent)
        if child_leg is not None and parent_leg is not None and child_leg != parent_leg:
            violations["cross_leg_contains"].append(
                {"parent": parent, "child": child, "lifecycle": lifecycle}
            )

    # GT grandchild placed directly under its GT grandparent.
    for child, true_parent in gt_parents.items():
        grandparent = gt_parents.get(true_parent)
        if grandparent is None:
            continue
        if (grandparent, child) in structural_contains:
            violations["sub_child_direct_under_main"].append(
                {"parent": grandparent, "child": child, "true_parent": true_parent}
            )

    # Reversed GT edges: a child CONTAINS its declared parent.
    for child, parent in gt_parents.items():
        reversed_edge = contains_edge.get((child, parent))
        if reversed_edge is not None and _value(reversed_edge.get("lifecycle", "")) in relevance:
            violations["child_contains_parent"].append({"parent": parent, "child": child})

    # Alias pairs: SAME_CONDUCTOR required at any lifecycle, alias cannot parent
    # a structural CONTAINS edge.
    for entry in gt.get("same_conductor") or ():
        alias = str(entry.get("alias")) if isinstance(entry, Mapping) else str(entry[0])
        canonical = str(entry.get("canonical")) if isinstance(entry, Mapping) else str(entry[1])
        key = (alias, canonical)
        lifecycles = alias_seen.get(key) or alias_seen.get((canonical, alias)) or []
        if not lifecycles:
            violations["alias_not_same_conductor"].append({"alias": alias, "canonical": canonical})
        for parent, child in structural_contains:
            if parent == alias:
                violations["alias_with_children"].append(
                    {"alias": alias, "parent_of": child}
                )

    for pair in gt.get("paired_leg") or ():
        if isinstance(pair, Mapping):
            left, right = pair.get("left") or pair.get("a"), pair.get("right") or pair.get("b")
        elif isinstance(pair, (list, tuple)) and len(pair) >= 2:
            left, right = pair[0], pair[1]
        else:
            continue
        if left is None or right is None:
            continue
        if _pair(left, right) not in paired_seen:
            violations["pair_missing"].append({"a": str(left), "b": str(right)})

    # -- paired_leg / same_conductor checks --------------------------------- #
    paired_report: list[dict[str, Any]] = []
    for pair in gt.get("paired_leg") or ():
        if isinstance(pair, Mapping):
            left, right = pair.get("left") or pair.get("a"), pair.get("right") or pair.get("b")
        elif isinstance(pair, (list, tuple)) and len(pair) >= 2:
            left, right = pair[0], pair[1]
        else:
            continue
        if left is None or right is None:
            continue
        found = paired_seen.get(_pair(left, right), [])
        paired_report.append(
            {
                "a": str(left),
                "a_name": names.get(str(left), str(left)),
                "b": str(right),
                "b_name": names.get(str(right), str(right)),
                "found": bool(found),
                "lifecycles": sorted(found),
            }
        )

    alias_report: list[dict[str, Any]] = []
    for entry in gt.get("same_conductor") or ():
        alias = str(entry.get("alias")) if isinstance(entry, Mapping) else str(entry[0])
        canonical = str(entry.get("canonical")) if isinstance(entry, Mapping) else str(entry[1])
        found = alias_seen.get((alias, canonical)) or alias_seen.get((canonical, alias)) or []
        alias_report.append(
            {
                "alias": alias,
                "alias_name": names.get(alias, alias),
                "canonical": canonical,
                "canonical_name": names.get(canonical, canonical),
                "found": bool(found),
                "lifecycles": sorted(found),
            }
        )

    # -- extra readers ------------------------------------------------------ #
    gt_readers = set(names)
    extra_readers = sorted(edge_readers - gt_readers)

    return {
        "contains_direct": direct_levels,
        "contains_closure": closure_metrics,
        "per_child": per_child,
        "violations": violations,
        "paired_leg": paired_report,
        "same_conductor": alias_report,
        "extra_readers": extra_readers,
        "lifecycle_counts": {
            f"{relation}×{lifecycle}": count
            for (relation, lifecycle), count in sorted(lifecycle_counts.items())
        },
        "unscorable": sorted(unscorable),
        "unscorable_misses": sorted(set(scorable_misses) & set(unscorable)),
        "recall_scorable": {
            "precision": sp,
            "recall": sr,
            "f1": sf1,
        },
        "gt_reader_names": names,
    }


def format_markdown(result: Mapping[str, Any], names: Mapping[str, str] | None = None) -> str:
    """Render a compact per-parent table plus the violations from ``result``."""
    names = dict(names or result.get("gt_reader_names") or {})

    def label(reader: str) -> str:
        return names.get(reader, reader)

    lines: list[str] = ["# Topology score", ""]
    lines.append("## Direct CONTAINS")
    lines.append("")
    lines.append("| level | precision | recall | f1 | tp | fp | fn |")
    lines.append("|---|---|---|---|---|---|---|")
    for level, metrics in result["contains_direct"].items():
        lines.append(
            f"| {level} | {metrics['precision']:.3f} | {metrics['recall']:.3f} | "
            f"{metrics['f1']:.3f} | {len(metrics['tp'])} | {len(metrics['fp'])} | {len(metrics['fn'])} |"
        )
    lines.append("")
    lines.append("## Closure CONTAINS")
    lines.append("")
    lines.append("| level | precision | recall | f1 |")
    lines.append("|---|---|---|---|")
    for level, metrics in result["contains_closure"].items():
        lines.append(
            f"| {level} | {metrics['precision']:.3f} | {metrics['recall']:.3f} | {metrics['f1']:.3f} |"
        )

    by_parent: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for child in result["per_child"]:
        by_parent[child["gt_parent"]].append(child)
    lines.append("")
    lines.append("## Per child")
    lines.append("")
    lines.append("| parent | child | status | inferred parents |")
    lines.append("|---|---|---|---|")
    for parent, children in sorted(by_parent.items()):
        for child in children:
            parents = ", ".join(
                f"{label(item['parent'])} ({item['lifecycle']})"
                for item in child["inferred_parents"]
            ) or "-"
            lines.append(
                f"| {label(parent)} | {label(child['child'])} | {child['status']} | {parents} |"
            )

    lines.append("")
    lines.append("## Violations")
    lines.append("")
    for kind, items in result["violations"].items():
        lines.append(f"- **{kind}**: {len(items)}")
        for item in items:
            lines.append(f"  - {item}")

    if result.get("unscorable"):
        lines.append("")
        lines.append("## Unscorable children")
        lines.append("")
        lines.append(", ".join(label(reader) for reader in result["unscorable"]))
    return "\n".join(lines) + "\n"
