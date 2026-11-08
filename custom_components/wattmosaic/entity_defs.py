"""Entity definition tables (plain data, no Home Assistant imports).

``sensor.py``/``binary_sensor.py`` translate these tables into HA
``SensorEntityDescription`` objects.  Keeping the table here lets tests assert
units/state classes without importing Home Assistant.
"""

from __future__ import annotations

from typing import Any

# --------------------------------------------------------------------------- #
# Per-device sensors
# --------------------------------------------------------------------------- #

DEVICE_SENSORS: dict[str, dict[str, Any]] = {
    "power": {
        "name": "Estimated power",
        "device_class": "power",
        "state_class": "measurement",
        "unit": "W",
        "icon": "mdi:flash",
        "suggested_display_precision": 1,
    },
    "energy": {
        "name": "Estimated energy",
        "device_class": "energy",
        "state_class": "total_increasing",
        "unit": "kWh",
        "icon": "mdi:counter",
        "suggested_display_precision": 3,
    },
    "source": {
        "name": "Measurement source",
        "device_class": "enum",
        "state_class": None,
        "unit": None,
        "icon": "mdi:source-branch",
        "options": ["direct_measured", "inferred", "model_estimated", "unknown"],
    },
    "confidence": {
        "name": "Confidence",
        "device_class": None,
        "state_class": "measurement",
        "unit": "%",
        "icon": "mdi:percent",
        "suggested_display_precision": 1,
    },
    "confidence_tier": {
        "name": "Evidence tier",
        "device_class": "enum",
        "state_class": None,
        "unit": None,
        "icon": "mdi:stairs",
        "options": [
            "tier_0_unknown",
            "tier_1_modelled",
            "tier_2_inferred",
            "tier_3_measured",
            "tier_4_verified",
        ],
    },
    "sample_count": {
        "name": "Sample count",
        "device_class": None,
        "state_class": "total",
        "unit": None,
        "icon": "mdi:counter",
    },
    "model_validity": {
        "name": "Model validity",
        "device_class": "enum",
        "state_class": None,
        "unit": None,
        "icon": "mdi:check-decagram",
        "options": ["valid", "frozen", "invalid", "not_required", "unknown"],
    },
    "operating_stage": {
        "name": "Operating stage",
        "device_class": None,
        "state_class": None,
        "unit": None,
        "icon": "mdi:state-machine",
    },
    "last_inference": {
        "name": "Last inference",
        "device_class": "timestamp",
        "state_class": None,
        "unit": None,
        "icon": "mdi:clock-outline",
    },
    "data_quality": {
        "name": "Data quality",
        "device_class": "enum",
        "state_class": None,
        "unit": None,
        "icon": "mdi:shield-check",
        "options": ["good", "degraded", "stale", "bad", "missing"],
    },
}

# --------------------------------------------------------------------------- #
# Global sensors
# --------------------------------------------------------------------------- #

GLOBAL_SENSORS: dict[str, dict[str, Any]] = {
    "total_power": {
        "name": "Total incoming power",
        "device_class": "power",
        "state_class": "measurement",
        "unit": "W",
        "icon": "mdi:transmission-tower",
        "suggested_display_precision": 1,
    },
    "l1_power": {
        "name": "L1 power",
        "device_class": "power",
        "state_class": "measurement",
        "unit": "W",
        "icon": "mdi:flash",
        "suggested_display_precision": 1,
    },
    "l2_power": {
        "name": "L2 power",
        "device_class": "power",
        "state_class": "measurement",
        "unit": "W",
        "icon": "mdi:flash",
        "suggested_display_precision": 1,
    },
    "direct_measured_power": {
        "name": "Directly measured power",
        "device_class": "power",
        "state_class": "measurement",
        "unit": "W",
        "icon": "mdi:meter-electric",
        "suggested_display_precision": 1,
    },
    "inferred_power": {
        "name": "Inferred power",
        "device_class": "power",
        "state_class": "measurement",
        "unit": "W",
        "icon": "mdi:chart-bell-curve",
        "suggested_display_precision": 1,
    },
    "model_estimated_power": {
        "name": "Model estimated power",
        "device_class": "power",
        "state_class": "measurement",
        "unit": "W",
        "icon": "mdi:sine-wave",
        "suggested_display_precision": 1,
    },
    "unknown_power": {
        "name": "Unknown / unattributed power",
        "device_class": "power",
        "state_class": "measurement",
        "unit": "W",
        "icon": "mdi:help-circle-outline",
        "suggested_display_precision": 1,
    },
    "reconciliation_error": {
        "name": "Power reconciliation error",
        "device_class": "power",
        "state_class": "measurement",
        "unit": "W",
        "icon": "mdi:scale-balance",
        "suggested_display_precision": 1,
    },
    "accounted_fraction": {
        "name": "Accounted fraction",
        "device_class": None,
        "state_class": "measurement",
        "unit": "%",
        "icon": "mdi:gauge",
        "suggested_display_precision": 1,
    },
    "quality_score": {
        "name": "Data quality score",
        "device_class": None,
        "state_class": "measurement",
        "unit": "%",
        "icon": "mdi:shield-check",
        "suggested_display_precision": 1,
    },
    "engine_status": {
        "name": "Engine status",
        "device_class": "enum",
        "state_class": None,
        "unit": None,
        "icon": "mdi:engine",
        # "unavailable" is a reserved HA state string, so offline engines
        # report "offline" instead.
        "options": ["ok", "degraded", "error", "offline"],
    },
    "last_update": {
        "name": "Last snapshot",
        "device_class": "timestamp",
        "state_class": None,
        "unit": None,
        "icon": "mdi:clock-outline",
    },
}

# --------------------------------------------------------------------------- #
# Binary sensors
# --------------------------------------------------------------------------- #

DEVICE_BINARY_SENSORS: dict[str, dict[str, Any]] = {
    "running": {
        "name": "Running",
        "device_class": "running",
        "icon": "mdi:play-circle-outline",
    },
}

GLOBAL_BINARY_SENSORS: dict[str, dict[str, Any]] = {
    "engine_reachable": {
        "name": "Engine reachable",
        "device_class": "connectivity",
        "icon": "mdi:lan-connect",
    },
    "learning_active": {
        "name": "Active learning",
        "device_class": "running",
        "icon": "mdi:school-outline",
    },
}
