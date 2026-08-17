"""Fail-closed load/recovery report generation and validation."""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

from release.load.measurements import evidence_errors, is_number, percentile, recompute
from release.load.profiles import (
    PROFILE_NAMES,
    THRESHOLD_DERIVATION,
    THRESHOLD_RULES,
    ProfileError,
    load_profiles,
)
from release.load.profiles import (
    require as _require,
)
from release.load.profiles import (
    validate_profiles as _validate_profiles,
)

METRIC_NAMES = frozenset(
    {
        "throughput_per_second",
        "latency_p50_ms",
        "latency_p95_ms",
        "latency_p99_ms",
        "rejection_rate",
        "queue_depth_max",
        "tenant_fairness",
        "cpu_percent_max",
        "memory_bytes_max",
        "recovery_seconds_max",
        "lost_accepted_runs",
        "duplicate_accepted_runs",
    }
)
DISTRIBUTION_NAMES = frozenset(
    {
        "cpu_percent",
        "latency_ms",
        "memory_bytes",
        "queue_depth",
        "recovery_seconds",
        "status_code",
        "workload_finished_at_ms",
        "workload_started_at_ms",
    }
)
REPORT_KEYS = frozenset(
    {
        "schema_version",
        "profile_version",
        "candidate_identity",
        "baseline",
        "profiles",
        "raw_requests",
        "measurements",
        "evaluation",
        "errors",
        "passed",
    }
)
BASELINE_DIGEST = "sha256:6fcf36a648300cef5574059beb4f8031e3c6705bf7f26253534c4cbdf55ff267"
COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")


def validate_profiles(value: Any) -> None:
    """Compatibility export for the profile contract validator."""
    _validate_profiles(value)


def baseline_distribution_metrics(samples: dict) -> dict[str, float | int]:
    """Recompute baseline performance values from its retained raw distributions."""
    _require(isinstance(samples, dict) and set(samples) == DISTRIBUTION_NAMES, "bad samples")
    for name, values in samples.items():
        _require(isinstance(values, list), f"baseline {name} distribution is not a list")
        _require(all(is_number(value) and value >= 0 for value in values), f"bad {name}")
    latency = samples["latency_ms"]
    started = samples["workload_started_at_ms"]
    finished = samples["workload_finished_at_ms"]
    _require(latency and started and len(started) == len(finished), "baseline samples are empty")
    elapsed = max((max(finished) - min(started)) / 1000, 1e-9)
    return {
        "throughput_per_second": round(len(started) / elapsed, 6),
        "latency_p50_ms": round(percentile(latency, 0.50), 6),
        "latency_p95_ms": round(percentile(latency, 0.95), 6),
        "latency_p99_ms": round(percentile(latency, 0.99), 6),
        "rejection_rate": round(
            sum(status in {429, 503} for status in samples["status_code"])
            / len(samples["status_code"]),
            6,
        ),
        "queue_depth_max": max(samples["queue_depth"]),
        "cpu_percent_max": max(samples["cpu_percent"]),
        "memory_bytes_max": max(samples["memory_bytes"]),
        "recovery_seconds_max": max(samples["recovery_seconds"], default=0.0),
    }


def _baseline_profile_errors(value: dict) -> list[str]:
    errors = []
    if set(value.get("profiles", {})) != PROFILE_NAMES:
        errors.append("baseline profiles are incomplete")
    for name, metrics in value.get("profiles", {}).items():
        if not isinstance(metrics, dict) or set(metrics) != METRIC_NAMES:
            errors.append(f"baseline profile {name} metrics are incomplete")
        elif not all(is_number(metric) for metric in metrics.values()):
            errors.append(f"baseline profile {name} metrics must be finite numbers")
    return errors


def _baseline_distribution_errors(value: dict) -> list[str]:
    distributions = value.get("sample_distribution", {})
    errors = []
    if set(distributions) != PROFILE_NAMES:
        errors.append("baseline raw distributions are incomplete")
    for name, samples in distributions.items():
        try:
            derived = baseline_distribution_metrics(samples)
        except ProfileError as error:
            errors.append(f"baseline profile {name} distributions are invalid: {error}")
            continue
        recorded = value["profiles"].get(name, {})
        if any(recorded.get(metric) != measured for metric, measured in derived.items()):
            errors.append(f"baseline profile {name} metrics do not match raw distributions")
    sample_count = sum(len(samples.get("status_code", [])) for samples in distributions.values())
    if value.get("source", {}).get("raw_request_count") != sample_count:
        errors.append("baseline raw request count does not match its distributions")
    if value.get("source", {}).get("sample_run_count", 0) < 3:
        errors.append("baseline requires at least three isolated sample runs")
    return errors


def validate_baseline(path: Path) -> list[str]:
    """Return errors for a mutated or structurally incomplete baseline."""
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
    except (OSError, json.JSONDecodeError) as error:
        return [f"baseline unreadable: {error}"]
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    errors = [] if digest == BASELINE_DIGEST else ["baseline does not match the pinned digest"]
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        errors.append("baseline schema_version is unsupported")
        return errors
    errors.extend(_baseline_profile_errors(value))
    errors.extend(_baseline_distribution_errors(value))
    return errors


def load_baseline(path: Path) -> dict:
    errors = validate_baseline(path)
    if errors:
        raise ProfileError("; ".join(errors))
    return json.loads(path.read_text(encoding="utf-8"))


def _ratio(candidate: float, baseline: float) -> float:
    if baseline == 0:
        return 1.0 if candidate == 0 else math.inf
    return candidate / baseline


def _comparison(name: str, candidate: dict, baseline: dict) -> float:
    metric = THRESHOLD_DERIVATION[name]["metric"]
    if name == "rejection_rate_delta":
        return candidate[metric] - baseline[metric]
    if name == "tenant_fairness_minimum" or name in {
        "lost_accepted_runs",
        "duplicate_accepted_runs",
    }:
        return candidate[metric]
    return _ratio(candidate[metric], baseline[metric])


def evaluate(measurements: dict, baseline: dict, rules: dict) -> dict[str, bool]:
    """Evaluate fixed rules against every measured profile."""
    verdict = {}
    for name, rule in rules.items():
        values = []
        for profile, candidate in measurements.items():
            reference = baseline.get("profiles", {}).get(profile)
            if reference is None:
                values.append(False)
                continue
            observed = _comparison(name, candidate, reference)
            values.append(
                observed <= rule["maximum"] if "maximum" in rule else observed >= rule["minimum"]
            )
        verdict[name] = bool(values) and all(values)
    return verdict


def derive_threshold(baseline: dict, name: str, derivation: dict) -> dict:
    """Re-derive one literal while proving its baseline metric still exists."""
    metric = derivation["metric"]
    _require(
        all(metric in profile for profile in baseline.get("profiles", {}).values()),
        f"baseline has no {metric}",
    )
    return {str(derivation["kind"]): derivation["limit"]}


def build_report(profiles: dict, baseline: dict, identity: dict, rows: list[dict]) -> dict:
    """Build a report that retains failures instead of raising them away."""
    errors, measurements, evaluation = _derived_report_values(rows, profiles, baseline, identity)
    return {
        "schema_version": 1,
        "profile_version": profiles["profile_version"],
        "candidate_identity": identity,
        "baseline": baseline,
        "profiles": profiles["profiles"],
        "raw_requests": rows,
        "measurements": measurements,
        "evaluation": evaluation,
        "errors": errors,
        "passed": not errors and all(evaluation.values()),
    }


def _candidate_identity_errors(identity: Any) -> list[str]:
    package = identity.get("package") if isinstance(identity, dict) else None
    valid = (
        isinstance(identity, dict)
        and identity.get("schema_version") == 1
        and COMMIT_PATTERN.fullmatch(str(identity.get("commit", ""))) is not None
        and isinstance(package, dict)
        and isinstance(package.get("version"), str)
        and bool(package["version"].strip())
        and isinstance(package.get("artifacts"), dict)
    )
    return [] if valid else ["candidate identity is malformed"]


def _derived_report_values(
    rows: Any, profiles: dict, baseline: dict, identity: Any
) -> tuple[list[str], dict, dict]:
    errors = evidence_errors(rows, profiles)
    errors.extend(_candidate_identity_errors(identity))
    try:
        measured = recompute(rows, profiles["profiles"]) if rows else {}
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        measured = {}
        errors.append(f"measurements cannot be recomputed: {error}")
    return errors, measured, evaluate(measured, baseline, THRESHOLD_RULES)


def _report_contract_errors(
    report: dict,
    profiles: dict,
    baseline: dict,
    identity: dict,
    raw_errors: list[str],
    measured: dict,
    evaluation: dict,
) -> list[str]:
    checks = (
        (report["schema_version"] == 1, "report schema version is unsupported"),
        (
            report["profile_version"] == profiles["profile_version"],
            "report profile version does not match the pinned profile",
        ),
        (
            report["profiles"] == profiles["profiles"],
            "report profile settings do not match the pinned profile",
        ),
        (
            report["candidate_identity"] == identity,
            "report candidate identity does not match the measured candidate identity",
        ),
        (report["baseline"] == baseline, "report baseline does not match the pinned baseline"),
        (report["errors"] == raw_errors, "report reported errors do not match raw evidence"),
        (
            report["measurements"] == measured,
            "report measurements do not recompute from raw_requests",
        ),
        (report["evaluation"] == evaluation, "report evaluation does not match fixed thresholds"),
    )
    return [message for valid, message in checks if not valid]


def validate_report(
    report: Any,
    *,
    profiles_path: Path,
    baseline_path: Path,
    expected_identity: dict,
) -> list[str]:
    """Recompute and verify a persisted report without trusting its summaries."""
    if not isinstance(report, dict) or set(report) != REPORT_KEYS:
        return ["report has unexpected keys or is missing raw_requests"]
    try:
        profiles = load_profiles(profiles_path)
        baseline = load_baseline(baseline_path)
    except ProfileError as error:
        return [f"report contract cannot be loaded: {error}"]
    raw_errors, measured, evaluation = _derived_report_values(
        report["raw_requests"], profiles, baseline, report["candidate_identity"]
    )
    errors = list(raw_errors)
    errors.extend(
        _report_contract_errors(
            report, profiles, baseline, expected_identity, raw_errors, measured, evaluation
        )
    )
    expected_passed = not errors and all(evaluation.values())
    if report["passed"] != expected_passed:
        errors.append("report passed flag is inconsistent")
    return errors
