"""Versioned load profile schema and fixed threshold controls."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from release.load.measurements import is_number

PROFILE_NAMES = frozenset({"burst", "sustained", "soak", "overload"})
SURFACE_NAMES = frozenset(
    {"langgraph-streams", "slow-script", "failing-script", "approvals", "artifacts", "webhooks"}
)
FAULT_NAMES = frozenset(
    {
        "database-contention",
        "redis-loss",
        "worker-loss",
        "service-restart",
        "network-delay",
        "downstream-throttling",
    }
)
THRESHOLD_DERIVATION = {
    "throughput_ratio": {"kind": "minimum", "metric": "throughput_per_second", "limit": 0.8},
    "latency_p50_ratio": {"kind": "maximum", "metric": "latency_p50_ms", "limit": 1.5},
    "latency_p95_ratio": {"kind": "maximum", "metric": "latency_p95_ms", "limit": 1.5},
    "latency_p99_ratio": {"kind": "maximum", "metric": "latency_p99_ms", "limit": 1.5},
    "rejection_rate_delta": {"kind": "maximum", "metric": "rejection_rate", "limit": 0.1},
    "queue_depth_ratio": {"kind": "maximum", "metric": "queue_depth_max", "limit": 1.5},
    "tenant_fairness_minimum": {"kind": "minimum", "metric": "tenant_fairness", "limit": 0.9},
    "deployment_fairness_minimum": {
        "kind": "minimum",
        "metric": "deployment_fairness",
        "limit": 0.9,
    },
    "replica_fairness_ratio": {
        "kind": "minimum",
        "metric": "replica_fairness",
        "limit": 0.8,
    },
    "worker_fairness_ratio": {
        "kind": "minimum",
        "metric": "worker_fairness",
        "limit": 0.8,
    },
    "cpu_ratio": {"kind": "maximum", "metric": "cpu_percent_max", "limit": 1.5},
    "memory_ratio": {"kind": "maximum", "metric": "memory_bytes_max", "limit": 1.5},
    "recovery_seconds_ratio": {
        "kind": "maximum",
        "metric": "recovery_seconds_max",
        "limit": 1.5,
    },
    "lost_accepted_runs": {"kind": "maximum", "metric": "lost_accepted_runs", "limit": 0},
    "duplicate_accepted_runs": {
        "kind": "maximum",
        "metric": "duplicate_accepted_runs",
        "limit": 0,
    },
}
THRESHOLD_RULES = {
    "throughput_ratio": {"minimum": 0.8},
    "latency_p50_ratio": {"maximum": 1.5},
    "latency_p95_ratio": {"maximum": 1.5},
    "latency_p99_ratio": {"maximum": 1.5},
    "rejection_rate_delta": {"maximum": 0.1},
    "queue_depth_ratio": {"maximum": 1.5},
    "tenant_fairness_minimum": {"minimum": 0.9},
    "deployment_fairness_minimum": {"minimum": 0.9},
    "replica_fairness_ratio": {"minimum": 0.8},
    "worker_fairness_ratio": {"minimum": 0.8},
    "cpu_ratio": {"maximum": 1.5},
    "memory_ratio": {"maximum": 1.5},
    "recovery_seconds_ratio": {"maximum": 1.5},
    "lost_accepted_runs": {"maximum": 0},
    "duplicate_accepted_runs": {"maximum": 0},
}


class ProfileError(ValueError):
    """A profile or baseline cannot safely drive the release gate."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ProfileError(message)


def _validate_profile_shapes(value: dict) -> None:
    require(set(value["profiles"]) == PROFILE_NAMES, "profiles must name all four profiles")
    for name, profile in value["profiles"].items():
        require(
            set(profile) == {"duration_seconds", "requests_per_second", "max_in_flight"},
            f"profile {name} has unexpected keys",
        )
        for field, measured in profile.items():
            require(
                is_number(measured) and measured > 0,
                f"profile {name} {field} must be positive",
            )


def _validate_workload_matrix(value: dict) -> None:
    matrix = value["matrix"]
    expected = {"tenants", "deployments_per_tenant", "replicas", "workers"}
    require(isinstance(matrix, dict) and set(matrix) == expected, "matrix is incomplete")
    require(
        all(
            isinstance(count, int) and not isinstance(count, bool) and count > 0
            for count in matrix.values()
        ),
        "matrix counts must be positive integers",
    )
    surfaces = value["surfaces"]
    require(
        isinstance(surfaces, list)
        and len(surfaces) == len(SURFACE_NAMES)
        and set(surfaces) == SURFACE_NAMES,
        "product surfaces are incomplete or duplicated",
    )
    faults = value["faults"]
    require(isinstance(faults, dict) and set(faults) == FAULT_NAMES, "fault matrix is incomplete")
    require(
        all(isinstance(detail, str) and detail.strip() for detail in faults.values()),
        "fault descriptions must be non-empty",
    )


def _validate_gate_controls(value: dict) -> None:
    thresholds = value["thresholds"]
    require(
        isinstance(thresholds, dict)
        and set(thresholds) == {"derived_from", "rules"}
        and thresholds["derived_from"] == "release/load/baseline-v1.json",
        "threshold baseline path moved",
    )
    require(thresholds["rules"] == THRESHOLD_RULES, "threshold rules moved")
    require(
        value["overload_contract"]
        == {
            "statuses": [429, 503],
            "require_retry_after": True,
            "drain": True,
            "cancellation": True,
        },
        "overload contract moved",
    )
    baseline = value["baseline"]
    require(
        isinstance(baseline, dict)
        and set(baseline) == {"path", "release"}
        and baseline["path"] == "release/load/baseline-v1.json"
        and isinstance(baseline["release"], str)
        and bool(baseline["release"].strip()),
        "baseline reference is invalid",
    )


def validate_profiles(value: Any) -> None:
    """Reject unknown, incomplete, or unbounded profile configuration."""
    keys = {
        "schema_version",
        "profile_version",
        "profiles",
        "matrix",
        "surfaces",
        "faults",
        "thresholds",
        "overload_contract",
        "baseline",
    }
    require(isinstance(value, dict) and set(value) == keys, "profile has unexpected keys")
    require(value["schema_version"] == 1 and value["profile_version"] == "1", "bad version")
    _validate_profile_shapes(value)
    _validate_workload_matrix(value)
    _validate_gate_controls(value)


def load_profiles(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ProfileError(f"profile unreadable: {error}") from error
    validate_profiles(value)
    return value
