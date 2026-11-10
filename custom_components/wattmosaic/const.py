"""Constants for the WattMosaic Home Assistant integration.

This module is deliberately free of Home Assistant imports so that the pure
logic helpers (``util.py``, ``api_client.py``, ``flow_logic.py``,
``service_logic.py``) can be unit-tested on a machine where
``homeassistant`` is not installed.
"""

from __future__ import annotations

DOMAIN = "wattmosaic"
NAME = "WattMosaic"
MANUFACTURER = "WattMosaic"
VERSION = "0.2.1-dev.10"

#: Platforms forwarded by ``async_setup_entry`` (strings, so no HA import).
PLATFORMS: list[str] = ["sensor", "binary_sensor"]

#: Engine HTTP API base path.  The engine owns the schema; the integration is a
#: read-mostly client and never computes inference (master spec §7.2, D3).
API_BASE_PATH = "/api/v1"

#: Contract version mirrored from ``engine/wattmosaic_engine/contracts.py``.
#: This is a consumer-side expectation, not a competing schema definition.
CONTRACT_VERSION = "1.2.0"

HEADER_API_TOKEN = "X-API-Token"
HEADER_CONTRACT_VERSION = "X-WattMosaic-Contract-Version"

# --------------------------------------------------------------------------- #
# Config / options keys
# --------------------------------------------------------------------------- #

CONF_BASE_URL = "base_url"
CONF_API_TOKEN = "api_token"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_STALE_AFTER = "stale_after"
CONF_UNAVAILABLE_AFTER = "unavailable_after"
CONF_CT_ASSIGNMENTS = "ct_assignments"
CONF_TOPOLOGY_CONFIRMED = "topology_confirmed"
CONF_TOPOLOGY_VERSION = "topology_version"
#: Options-flow checkbox that branches into the per-device configuration step.
CONF_CONFIGURE_DEVICE = "configure_device"
#: Per-device opt-out of the energy entity: ``{device_id: bool}``.
#: Absent or true means "create it" (the behaviour before the option existed).
CONF_ENERGY_ENTITIES = "energy_entities"
#: Per-device link to an existing Home Assistant device:
#: ``{engine_device_id: ha_device_id}``.
#:
#: This is what makes WattMosaic *augment* an appliance that is already in Home
#: Assistant instead of showing its measurements under a parallel synthetic
#: device.  It is a Home Assistant placement decision, so it lives in the config
#: entry and never travels to the engine.
CONF_DEVICE_LINKS = "device_links"
#: Opt-in: let WattMosaic create devices for fixtures whose circuit membership it
#: learned from observed co-movement (ADR-0024).  Stored as an option so the
#: decision is the user's, survives restarts, and can be revoked.
CONF_AUTO_PLACEMENT = "auto_placement"

# --------------------------------------------------------------------------- #
# Defaults
# --------------------------------------------------------------------------- #

DEFAULT_SCAN_INTERVAL = 30
DEFAULT_STALE_AFTER = 120
DEFAULT_UNAVAILABLE_AFTER = 120
DEFAULT_REQUEST_TIMEOUT = 10
DEFAULT_MAX_RETRIES = 2
DEFAULT_BACKOFF_BASE = 0.5

MIN_SCAN_INTERVAL = 5
MAX_SCAN_INTERVAL = 3600
MIN_STALE_AFTER = 10
MAX_STALE_AFTER = 86400
MIN_UNAVAILABLE_AFTER = 10
MAX_UNAVAILABLE_AFTER = 86400

# --------------------------------------------------------------------------- #
# Enumerations mirrored from the contract (kept as plain strings)
# --------------------------------------------------------------------------- #

CT_ROLES: tuple[str, ...] = ("main", "branch", "dedicated_device", "unknown")
CT_ROLE_MAIN = "main"
CT_ROLE_BRANCH = "branch"
CT_ROLE_DEDICATED = "dedicated_device"
CT_ROLE_UNKNOWN = "unknown"

PHASES: tuple[str, ...] = ("l1", "l2", "both", "unknown")
PHASE_UNKNOWN = "unknown"

POLARITIES: tuple[str, ...] = ("normal", "inverted")
POLARITY_NORMAL = "normal"

SOURCES: tuple[str, ...] = ("direct_measured", "inferred", "model_estimated", "unknown")

#: Per-device taxonomy axes from the contract (ADR-0011).  Mirrored as plain
#: string tuples so ``flow_logic`` stays importable without Home Assistant.
DEVICE_TYPES: tuple[str, ...] = (
    "light",
    "light_group",
    "fan",
    "pump",
    "hvac_heat",
    "hvac_cool",
    "hvac_combined",
    "water_heater",
    "appliance",
    "refrigeration",
    "ev_charger",
    "electronics",
    "socket",
    "motor",
    "humidifier",
    "dehumidifier",
    "sauna",
    "hot_tub",
    "other",
    "unknown",
)

CONNECTIVITY_LEVELS: tuple[str, ...] = ("connected", "not_connected", "unknown")

MONITORING_LEVELS: tuple[str, ...] = (
    "dedicated",
    "shared",
    "home_total_only",
    "unmonitored",
    "unknown",
)

POWER_BEHAVIORS: tuple[str, ...] = ("fixed", "variable", "complex", "unknown")

LIGHT_TYPES: tuple[str, ...] = (
    "led",
    "incandescent",
    "halogen",
    "cfl",
    "fluorescent",
    "smart_bulb",
    "unknown",
)

#: Command name accepted by the engine for a per-device configuration write.
COMMAND_SET_DEVICE_CONFIGURATION = "set_device_configuration"

QUALITY_STATES: tuple[str, ...] = ("good", "degraded", "stale", "bad", "missing")

#: Ordered evidence grades from the engine (ADR-0013).  A *grade*, not a
#: probability: it answers "what kind of evidence produced this number?".
#: ``confidence`` remains an independent, finer-grained engineering score.
CONFIDENCE_TIERS: tuple[str, ...] = (
    "tier_0_unknown",
    "tier_1_modelled",
    "tier_2_inferred",
    "tier_3_measured",
    "tier_4_verified",
)

MODEL_STATES: tuple[str, ...] = (
    "valid",
    "frozen",
    "invalid",
    "not_required",
    "unknown",
)

#: Quality states that must never be published as a *current* measurement.
UNPUBLISHABLE_QUALITY: frozenset[str] = frozenset({"stale", "missing", "bad"})

#: A device is shown as "running" above this threshold (watts).
RUNNING_POWER_THRESHOLD_W = 1.0

# --------------------------------------------------------------------------- #
# Entity keys (used by sensor/binary_sensor and translations)
# --------------------------------------------------------------------------- #

DEVICE_SENSOR_KEYS: tuple[str, ...] = (
    "power",
    "energy",
    "source",
    "confidence",
    "confidence_tier",
    "sample_count",
    "model_validity",
    "operating_stage",
    "last_inference",
    "data_quality",
)

DEVICE_BINARY_KEYS: tuple[str, ...] = ("running",)

GLOBAL_SENSOR_KEYS: tuple[str, ...] = (
    "total_power",
    "l1_power",
    "l2_power",
    "direct_measured_power",
    "inferred_power",
    "model_estimated_power",
    "unknown_power",
    "reconciliation_error",
    "accounted_fraction",
    "quality_score",
    "engine_status",
    "last_update",
)

GLOBAL_BINARY_KEYS: tuple[str, ...] = ("engine_reachable", "learning_active")

# --------------------------------------------------------------------------- #
# Services
# --------------------------------------------------------------------------- #

SERVICE_START_LEARNING = "start_learning"
SERVICE_PAUSE_LEARNING = "pause_learning"
SERVICE_RESUME_LEARNING = "resume_learning"
SERVICE_ACCEPT_SAMPLE = "accept_sample"
SERVICE_REJECT_SAMPLE = "reject_sample"
SERVICE_REBUILD_MODEL = "rebuild_model"
SERVICE_FREEZE_MODEL = "freeze_model"
SERVICE_UNFREEZE_MODEL = "unfreeze_model"
SERVICE_RESET_DEVICE_MODEL = "reset_device_model"
SERVICE_REBUILD_TOPOLOGY = "rebuild_topology"
SERVICE_RUN_DIAGNOSTICS = "run_diagnostics"
SERVICE_EXPORT_DEVICE_DATA = "export_device_data"
#: Learn which fixtures sit on which CT row from recorded history and, if the
#: user consents, create the devices (ADR-0024).  Analysis is read-only; only
#: ``apply: true`` writes, and that is what the confirmation gate protects.
SERVICE_DISCOVER_PLACEMENTS = "discover_placements"

SERVICES: tuple[str, ...] = (
    SERVICE_START_LEARNING,
    SERVICE_PAUSE_LEARNING,
    SERVICE_RESUME_LEARNING,
    SERVICE_ACCEPT_SAMPLE,
    SERVICE_REJECT_SAMPLE,
    SERVICE_REBUILD_MODEL,
    SERVICE_FREEZE_MODEL,
    SERVICE_UNFREEZE_MODEL,
    SERVICE_RESET_DEVICE_MODEL,
    SERVICE_REBUILD_TOPOLOGY,
    SERVICE_RUN_DIAGNOSTICS,
    SERVICE_EXPORT_DEVICE_DATA,
    SERVICE_DISCOVER_PLACEMENTS,
)

#: Destructive commands require an explicit ``confirm: true`` service field.
DESTRUCTIVE_SERVICES: frozenset[str] = frozenset(
    {
        SERVICE_RESET_DEVICE_MODEL,
        SERVICE_REBUILD_TOPOLOGY,
        SERVICE_REJECT_SAMPLE,
    }
)

#: Services that return a response payload to the caller.
RESPONSE_SERVICES: frozenset[str] = frozenset(
    {
        SERVICE_RUN_DIAGNOSTICS,
        SERVICE_EXPORT_DEVICE_DATA,
        SERVICE_DISCOVER_PLACEMENTS,
    }
)

#: Services that are read-only *unless* a parameter asks them to write.
#:
#: ``discover_placements`` analyses history without changing anything; passing
#: ``apply: true`` creates devices.  Gating the whole service would block the
#: harmless analysis, and gating nothing would let a write through unconfirmed,
#: so the confirmation is required exactly when the write is requested.
APPLY_CONFIRMATION_SERVICES: frozenset[str] = frozenset({SERVICE_DISCOVER_PLACEMENTS})

# --------------------------------------------------------------------------- #
# Diagnostics / redaction
# --------------------------------------------------------------------------- #

REDACTED = "**REDACTED**"
TOKEN_CONFIGURED_LABEL = "configured"
TOKEN_PLACEHOLDERS: tuple[str, ...] = (    "replace-with-a-random-local-token",
    "changeme",
    "change-me",
)
