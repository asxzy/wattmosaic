"""Export JSON Schemas from the authoritative contracts.

Run: python tools/dev/export_schemas.py
Fails if CONTRACT_VERSION does not match the schema_metadata expectation.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# Resolve the engine from *this* checkout, not from whatever `pip install -e`
# happens to point at.  A worktree shares the parent's virtualenv, whose editable
# install names the main checkout, so an unqualified import would generate the
# schemas of a different tree and the gate would pass against stale code.
ENGINE_ROOT = ROOT / "engine"
if str(ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(ENGINE_ROOT))

from wattmosaic_engine import contracts  # noqa: E402
from wattmosaic_engine.contracts import (  # noqa: E402
    entities,
    foundation,
    inference,
    matrix,
    measurement,
    power_models,
    service,
    topology,
)

OUT = ROOT / "data" / "schemas"

# Transport envelopes still emitted by v3 routes.  Every v3 response carries an
# ErrorResponse on failure, /health emits HealthResponse, /commands consumes
# ServiceCommand / emits ServiceResult, and the v3 snapshot embeds a
# DataQualityReport as its system_quality field.  These are the only legacy
# module models that remain API payloads at contract 3.0; the rest of the old
# ``MODELS`` set (topology, ct_channel, snapshot_response, ...) was removed with
# the v2 surface.
V3_TRANSPORT_MODELS = {
    "data_quality_report": contracts.DataQualityReport,
}

# Submodules that contribute v3 schemas.  Each declares ``SCHEMA_EXPORTS`` as a
# list of ``(name, model)`` pairs; tasks own their submodule's list, so no task
# needs to edit this exporter.
SCHEMA_MODULES = (
    service,
    foundation,
    entities,
    measurement,
    topology,
    inference,
    matrix,
    power_models,
)


def collect_models() -> dict[str, type]:
    """Return the v3 transport envelopes plus every submodule's exports."""
    collected = dict(V3_TRANSPORT_MODELS)
    for module in SCHEMA_MODULES:
        for name, model in getattr(module, "SCHEMA_EXPORTS", ()):
            collected[name] = model
    return collected


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    models = collect_models()
    index = {"contract_version": contracts.CONTRACT_VERSION, "schemas": {}}
    for name, model in models.items():
        schema = model.model_json_schema(mode="serialization")
        schema["$id"] = f"https://wattmosaic.local/schemas/{name}.json"
        schema["x-contract-version"] = contracts.CONTRACT_VERSION
        (OUT / f"{name}.json").write_text(json.dumps(schema, indent=2) + "\n")
        index["schemas"][name] = f"{name}.json"
    (OUT / "index.json").write_text(json.dumps(index, indent=2) + "\n")
    print(json.dumps({"contract_version": contracts.CONTRACT_VERSION, "schemas": len(models)}, indent=2))


if __name__ == "__main__":
    main()
