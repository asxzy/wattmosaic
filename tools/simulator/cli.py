"""CLI: generate a deterministic synthetic household dataset.

    python -m tools.simulator.cli --seed 7 --minutes 120 --interval 10 --out out/

Writes:
    <out>/household.jsonl   one record per timestamp (default)
    <out>/household.json    full document (with ``--format json``)
    <out>/samples.jsonl     one Measurement-shaped row per CT observation
    <out>/manifest.json     config, hashes, counts (for reproducibility checks)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from datetime import UTC, datetime
from pathlib import Path

from .scenario import HouseholdScenario, SimulatorConfig
from .sensors import QUALITY_PRESETS, QualityLevel
from .topology import REAL_HOME_TOPOLOGY


def _clean(obj):
    """JSON-safe: convert NaN/Inf to null and strip non-finite floats."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    return obj


def dumps(record) -> str:
    return json.dumps(_clean(record), separators=(",", ":"), sort_keys=True)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="wattmosaic-simulator",
        description="Generate realistic synthetic household power data for WattMosaic.",
    )
    p.add_argument("--seed", type=int, default=42, help="deterministic RNG seed")
    p.add_argument("--minutes", type=float, default=60.0, help="simulated duration")
    p.add_argument("--interval", type=float, default=10.0, help="record cadence in seconds")
    p.add_argument("--out", type=str, default=None, help="output directory (default: stdout)")
    p.add_argument("--format", choices=["jsonl", "json"], default="jsonl")
    p.add_argument(
        "--quality",
        choices=[q.value for q in QualityLevel],
        default=QualityLevel.GOOD.value,
        help="measurement impairment preset",
    )
    p.add_argument("--start", default="2026-01-01T00:00:00+00:00", help="simulated start time (ISO)")
    p.add_argument(
        "--invert-ct",
        action="append",
        default=[],
        help="CT id whose physical polarity is reversed (repeatable)",
    )
    p.add_argument(
        "--drift-ct",
        action="append",
        default=[],
        metavar="CT_ID=ppm_per_hour",
        help="slow measurement drift for a channel (repeatable)",
    )
    p.add_argument(
        "--outage",
        action="append",
        default=[],
        metavar="CT_ID=START:END",
        help="communication outage window in simulated seconds (repeatable)",
    )
    p.add_argument("--no-unknown-loads", action="store_true", help="exclude unmetered residual loads")
    p.add_argument(
        "--stratum",
        default=None,
        metavar="NAME",
        help=(
            "build a documented scenario stratum instead of the default household "
            "(e.g. factor2, shared, motor_startup); see "
            "`--list-strata`"
        ),
    )
    p.add_argument(
        "--list-strata",
        action="store_true",
        help="print the buildable stratum names and exit",
    )
    p.add_argument("--summary", action="store_true", help="print a scenario summary to stderr")
    p.add_argument("--print-topology", action="store_true", help="print simulated topology and exit")
    return p


def _parse_drift(items: list[str]) -> dict[str, float]:
    out: dict[str, float] = {}
    for item in items:
        ct, _, val = item.partition("=")
        if not ct or not val:
            raise SystemExit(f"invalid --drift-ct {item!r}, expected CT_ID=PPM")
        out[ct] = float(val)
    return out


def _parse_outage(items: list[str]) -> dict[str, list[tuple[float, float]]]:
    out: dict[str, list[tuple[float, float]]] = {}
    for item in items:
        ct, _, window = item.partition("=")
        start, _, end = window.partition(":")
        if not ct or not start or not end:
            raise SystemExit(f"invalid --outage {item!r}, expected CT_ID=START:END")
        out.setdefault(ct, []).append((float(start), float(end)))
    return out


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.list_strata:
        from .labhouse import available_strata

        for name in available_strata():
            print(name)
        return 0

    if args.stratum:
        return _run_stratum(args)

    if args.print_topology:
        print(json.dumps(_clean({
            "channels": [
                {
                    "ct_id": c.ct_id, "entity_id": c.entity_id, "ct_type": c.ct_type.value,
                    "phase": c.phase.value, "parent_ct_id": c.parent_ct_id,
                    "paired_ct_id": c.paired_ct_id, "panel": c.panel,
                }
                for c in REAL_HOME_TOPOLOGY.channels
            ],
            "devices": [
                {
                    "device_id": d.device_id, "name": d.name, "type": d.sim_type.value,
                    "phase": d.phase.value, "circuits": d.circuits,
                    "nominal_power_w": d.nominal_power_w,
                }
                for d in REAL_HOME_TOPOLOGY.devices
            ],
            "notes": REAL_HOME_TOPOLOGY.notes,
        }), indent=2))
        return 0

    start = datetime.fromisoformat(args.start)
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)

    cfg = SimulatorConfig(
        seed=args.seed,
        minutes=args.minutes,
        interval=args.interval,
        start_time=start,
        quality=QualityLevel(args.quality),
        inverted_channels=tuple(args.invert_ct),
        drifting_channels=_parse_drift(args.drift_ct),
        outage_windows=_parse_outage(args.outage),
        include_unknown_loads=not args.no_unknown_loads,
    )
    scenario = HouseholdScenario(cfg)

    out_dir = Path(args.out) if args.out else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)

    records = list(scenario.records()) if args.format == "jsonl" else list(scenario.records())

    payload = records if args.format == "json" else None

    sample_rows: list[dict] = []
    for rec in records:
        for ct_id, m in rec["measurements"].items():
            sample_rows.append({
                "ct_id": ct_id,
                "entity_id": m["entity_id"],
                "value": m["value"],
                "unit": "W",
                "state_timestamp": m["state_timestamp"],
                "received_at": m["received_at"],
                "physical_timestamp": m["physical_timestamp"],
                "quality": m["quality"],
                "repeated": m["repeated"],
                "polarization": m["polarization"],
                "sequence": m["sequence"],
            })

    digest = hashlib.sha256()
    if out_dir:
        if args.format == "jsonl":
            path = out_dir / "household.jsonl"
            with path.open("w", encoding="utf-8") as fh:
                for rec in records:
                    line = dumps(rec)
                    fh.write(line + "\n")
                    digest.update(line.encode())
        else:
            path = out_dir / "household.json"
            doc = _clean({"scenario": records[0]["scenario"] if records else {}, "records": payload})
            text = json.dumps(doc, indent=1, sort_keys=True)
            path.write_text(text, encoding="utf-8")
            digest.update(text.encode())

        with (out_dir / "samples.jsonl").open("w", encoding="utf-8") as fh:
            for row in sample_rows:
                fh.write(dumps(row) + "\n")

        manifest = {
            "generated_at": datetime.now(UTC).isoformat(),
            "config": cfg.as_metadata(),
            "record_count": len(records),
            "sample_count": len(sample_rows),
            "sha256": digest.hexdigest(),
            "quality_presets": {k.value: v for k, v in QUALITY_PRESETS.items()},
        }
        (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        print(str(path))
    else:
        if args.format == "jsonl":
            for rec in records:
                print(dumps(rec))
        else:
            print(json.dumps(_clean({"records": payload}), indent=1, sort_keys=True))

    if args.summary:
        print(json.dumps(scenario.summary(), indent=2), file=sys.stderr)
    return 0


def _run_stratum(args) -> int:
    """Emit a documented scenario stratum using the pre-WP2 CLI output shape.

    The record stream is unchanged — the stratum only decides which household is
    built — so a consumer of this CLI needs no new code.  Impairment flags are
    rejected rather than silently applied: a stratum already fixes its own
    quality and wiring, and honouring a conflicting flag would make the output
    not the stratum that was asked for.
    """
    from .labhouse import build_scenario

    conflicting = [
        flag for flag, given in (
            ("--invert-ct", bool(args.invert_ct)),
            ("--drift-ct", bool(args.drift_ct)),
            ("--outage", bool(args.outage)),
            ("--quality", args.quality != QualityLevel.GOOD.value),
            ("--no-unknown-loads", bool(args.no_unknown_loads)),
        ) if given
    ]
    if conflicting:
        raise SystemExit(
            f"--stratum {args.stratum} fixes its own impairments; "
            f"drop {', '.join(conflicting)}"
        )
    try:
        house = build_scenario(
            args.stratum, args.seed, minutes=args.minutes, interval=args.interval
        )
    except KeyError as exc:
        raise SystemExit(str(exc)) from None
    records = house.to_list()
    if args.out:
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / ("household.json" if args.format == "json" else "household.jsonl")
        if args.format == "json":
            doc = _clean({"scenario": records[0]["scenario"] if records else {},
                          "records": records})
            path.write_text(json.dumps(doc, indent=1, sort_keys=True), encoding="utf-8")
        else:
            with path.open("w", encoding="utf-8") as fh:
                for record in records:
                    fh.write(dumps(record) + "\n")
        (out_dir / "README.txt").write_text(
            f"stratum: {house.plan.name}\n{house.plan.description}\n"
            f"seed: {house.seed}\nflags: {sorted(house.flags())}\n",
            encoding="utf-8",
        )
        print(str(path))
    elif args.format == "json":
        print(json.dumps(_clean({"records": records}), indent=1, sort_keys=True))
    else:
        for record in records:
            print(dumps(record))
    if args.summary:
        print(json.dumps(house.summary(), indent=2), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
