"""Fail-closed load/recovery report generation and validation."""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

from release.load.environment import observation_digest
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
from release.load.receipt import load_source_identity

METRIC_NAMES = frozenset(
    {
        "throughput_per_second",
        "latency_p50_ms",
        "latency_p95_ms",
        "latency_p99_ms",
        "rejection_rate",
        "queue_depth_max",
        "tenant_fairness",
        "deployment_fairness",
        "replica_fairness",
        "worker_fairness",
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
        "environment",
        "observation_digest",
        "baseline",
        "profiles",
        "raw_requests",
        "measurements",
        "evaluation",
        "errors",
        "passed",
    }
)
BASELINE_DIGEST = "sha256:ba876c8b178af7b20a87f5e753fa64f075254a10a05c2ee088e0ff23ee95b5d3"
BASELINE_SOURCE_IDENTITY = Path(__file__).with_name("baseline-source-v1.json")
COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
ENVIRONMENT_KEYS = frozenset(
    {
        "system",
        "machine",
        "python",
        "cpu_limit",
        "memory_limit_bytes",
        "runtime_image",
        "postgres",
        "redis",
    }
)


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


def _environment_errors(value: Any) -> list[str]:
    if not isinstance(value, dict) or set(value) != ENVIRONMENT_KEYS:
        return ["load environment identity is malformed"]
    text = ("system", "machine", "python", "runtime_image", "postgres", "redis")
    if any(not isinstance(value[name], str) or not value[name].strip() for name in text):
        return ["load environment identity is incomplete"]
    if not isinstance(value["cpu_limit"], int) or value["cpu_limit"] < 1:
        return ["load environment CPU limit is invalid"]
    if not isinstance(value["memory_limit_bytes"], int) or value["memory_limit_bytes"] < 1:
        return ["load environment memory limit is invalid"]
    return []


def _baseline_source_errors(value: dict) -> list[str]:
    source = value.get("source", {})
    digests = source.get("run_digests")
    if (
        not isinstance(digests, list)
        or len(digests) != source.get("sample_run_count")
        or len(digests) < 3
        or len(set(digests)) != len(digests)
        or any(DIGEST_PATTERN.fullmatch(str(digest)) is None for digest in digests)
    ):
        return ["baseline requires distinct source run digests"]
    receipts = source.get("run_receipts")
    source_digests = (
        {receipt.get("source_digest") for receipt in receipts}
        if isinstance(receipts, list)
        else set()
    )
    if (
        not isinstance(receipts, list)
        or len(receipts) != len(digests)
        or [receipt.get("observation_digest") for receipt in receipts] != digests
        or any(receipt.get("commit") != source.get("commit") for receipt in receipts)
        or any(receipt.get("tree") != source.get("tree") for receipt in receipts)
        or any(
            receipt.get("package_version") != source.get("package_version") for receipt in receipts
        )
        or any(receipt.get("environment") != value.get("environment") for receipt in receipts)
        or any(
            receipt.get("source_identity_digest") != source.get("source_identity_digest")
            for receipt in receipts
        )
        or len(source_digests) != 1
        or source.get("source_digest") not in source_digests
        or any(DIGEST_PATTERN.fullmatch(str(digest)) is None for digest in source_digests)
        or any(
            not str(receipt.get("generated_at", "")).endswith("Z")
            or not str(receipt.get("product_import_origin", "")).endswith(
                "/src/zeroth/service/app.py"
            )
            for receipt in receipts
        )
        or COMMIT_PATTERN.fullmatch(str(source.get("commit", ""))) is None
        or COMMIT_PATTERN.fullmatch(str(source.get("tree", ""))) is None
    ):
        return ["baseline requires measured source receipts"]
    return []


def _baseline_identity_errors(source: dict) -> list[str]:
    try:
        identity, identity_digest = load_source_identity(BASELINE_SOURCE_IDENTITY)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        return [f"baseline source identity is unreadable: {error}"]
    bound = source.get("source_identity_digest") == identity_digest and all(
        source.get(name) == identity[name]
        for name in ("commit", "tree", "package_version", "source_digest")
    )
    return [] if bound else ["baseline source does not match its retained identity"]


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
    errors.extend(_environment_errors(value.get("environment")))
    errors.extend(_baseline_source_errors(value))
    errors.extend(_baseline_identity_errors(value.get("source", {})))
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
    if name.endswith("_fairness_minimum") or name in {
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


def build_report(
    profiles: dict,
    baseline: dict,
    identity: dict,
    rows: list[dict],
    *,
    environment: dict,
    observation_digest: str,
) -> dict:
    """Build a report that retains failures instead of raising them away."""
    errors, measurements, evaluation = _derived_report_values(
        rows, profiles, baseline, identity, environment, observation_digest
    )
    return {
        "schema_version": 1,
        "profile_version": profiles["profile_version"],
        "candidate_identity": identity,
        "environment": environment,
        "observation_digest": observation_digest,
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
        and bool(package["artifacts"])
    )
    return [] if valid else ["candidate identity is malformed"]


def _derived_report_values(
    rows: Any,
    profiles: dict,
    baseline: dict,
    identity: Any,
    environment: Any,
    raw_digest: Any,
) -> tuple[list[str], dict, dict]:
    errors = evidence_errors(rows, profiles)
    errors.extend(_candidate_identity_errors(identity))
    errors.extend(_environment_errors(environment))
    if environment != baseline.get("environment"):
        errors.append("candidate environment does not match the pinned baseline environment")
    if isinstance(identity, dict) and identity.get("commit") == baseline.get("source", {}).get(
        "commit"
    ):
        errors.append("candidate commit overlaps the pinned baseline source")
    if DIGEST_PATTERN.fullmatch(str(raw_digest)) is None:
        errors.append("candidate observation digest is malformed")
    elif raw_digest in baseline.get("source", {}).get("run_digests", []):
        errors.append("candidate observations overlap a pinned baseline run")
    if raw_digest != observation_digest(rows):
        errors.append("candidate observation digest does not match raw_requests")
    try:
        measured = recompute(rows, profiles["profiles"], profiles["matrix"]) if rows else {}
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
        (
            report["environment"] == baseline["environment"],
            "report environment does not match the pinned baseline environment",
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
        report["raw_requests"],
        profiles,
        baseline,
        report["candidate_identity"],
        report["environment"],
        report["observation_digest"],
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
