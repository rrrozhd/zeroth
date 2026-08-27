"""ZER-33 report contract: raw, candidate-bound, and fail closed."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
PROFILES = ROOT / "release/load/profiles-v1.json"
BASELINE = ROOT / "release/load/baseline-v1.json"


def _identity() -> dict:
    return {
        "schema_version": 1,
        "commit": "a" * 40,
        "package": {"version": "0.23.10.4", "artifacts": {"wheel": "sha256:" + "a" * 64}},
    }


def _candidate_services(environment: dict) -> dict:
    return {
        "postgres": {
            "instance_id": "f" * 64,
            "started_at": "2099-01-01T00:00:00Z",
            "image": environment["postgres"],
        },
        "redis": {
            "instance_id": "e" * 64,
            "started_at": "2099-01-01T00:00:01Z",
            "image": environment["redis"],
        },
    }


SURFACES = (
    "langgraph-streams",
    "slow-script",
    "failing-script",
    "approvals",
    "artifacts",
    "webhooks",
)
FAULTS = (
    "database-contention",
    "redis-loss",
    "worker-loss",
    "service-restart",
    "network-delay",
    "downstream-throttling",
)
FAULT_STATES = {
    "database-contention": ("coordination-timeout", "query-restored"),
    "redis-loss": ("artifact-unavailable", "artifact-restored"),
    "worker-loss": ("worker-withdrawn", "worker-replaced"),
    "service-restart": ("service-stopped", "service-started"),
    "network-delay": ("transport-delayed", "transport-restored"),
    "downstream-throttling": ("downstream-429", "delivery-retried", "delivered"),
}


def _workload_row(profile: str, offset: int, sequence: int, rate: float) -> dict:
    run_id = f"run-{sequence}"
    started = offset * 1000 / rate
    latency = float(10 + offset % 3)
    states = [{"state": "submitted", "at_ms": started}]
    states.append({"state": "accepted", "at_ms": started + 1, "run_id": run_id})
    if profile == "overload" and offset == 0:
        states.append({"state": "draining", "at_ms": started + 2, "run_id": run_id})
    if profile == "overload" and offset == 1:
        states.append({"state": "cancel-requested", "at_ms": started + 2, "run_id": run_id})
    terminal = "cancelled" if profile == "overload" and offset == 1 else "completed"
    states.append({"state": terminal, "at_ms": started + latency, "run_id": run_id})
    tenant = offset % 3
    return {
        "request_id": f"request-{sequence}",
        "profile": profile,
        "tenant_id": f"tenant-{chr(97 + tenant)}",
        "deployment_ref": f"deployment-{chr(97 + (offset // 3) % 2)}",
        "replica": f"replica-{(offset // 6) % 2}",
        "worker": f"worker-{(offset // 12) % 3}",
        "surface": SURFACES[offset % len(SURFACES)],
        "fault": None,
        "status_code": 202,
        "retry_after_seconds": None,
        "started_at_ms": started,
        "finished_at_ms": started + latency,
        "latency_ms": latency,
        "queue_depth": offset % 2,
        "cpu_percent": 20.0 + offset % 3,
        "memory_bytes": 1_000_000 + sequence,
        "lifecycle": states,
    }


def _fault_row(sequence: int, offset: int, status: int, retry_after: int | None) -> dict:
    latency = float(10 + offset % 3)
    fault = FAULTS[offset]
    return {
        "request_id": f"request-{sequence}",
        "profile": "overload",
        "tenant_id": f"tenant-{chr(97 + offset % 3)}",
        "deployment_ref": f"deployment-{chr(97 + offset % 2)}",
        "replica": f"replica-{offset % 2}",
        "worker": f"worker-{offset % 3}",
        "surface": SURFACES[offset % len(SURFACES)],
        "fault": fault,
        "status_code": status,
        "retry_after_seconds": retry_after,
        "started_at_ms": 0.0,
        "finished_at_ms": latency,
        "latency_ms": latency,
        "queue_depth": 1,
        "cpu_percent": 45.0,
        "memory_bytes": 1_100_000,
        "lifecycle": [
            {"state": "fault-injected", "at_ms": 0.0},
            {"state": "rejected", "at_ms": 1.0},
            *[
                {"state": state, "at_ms": 2.0 + index}
                for index, state in enumerate(FAULT_STATES[fault])
            ],
            {"state": "recovered", "at_ms": latency, "repair": "automatic"},
        ],
    }


def _restart_recovery_row(sequence: int) -> dict:
    run_id = f"run-{sequence}"
    row = _fault_row(sequence, FAULTS.index("service-restart"), 202, None)
    row["lifecycle"] = [
        {"state": "fault-injected", "at_ms": 0.0},
        {"state": "service-stopped", "at_ms": 1.0},
        {"state": "service-started", "at_ms": 2.0},
        {"state": "accepted", "at_ms": 3.0, "run_id": run_id},
        {"state": "completed", "at_ms": 4.0, "run_id": run_id},
        {"state": "recovered", "at_ms": 10.0, "repair": "automatic"},
    ]
    return row


def _rows(*, overload_status: int = 429, retry_after: int | None = 2) -> list[dict]:
    profiles = json.loads(PROFILES.read_text(encoding="utf-8"))["profiles"]
    rows = []
    sequence = 0
    for name, settings in profiles.items():
        count = round(settings["duration_seconds"] * settings["requests_per_second"])
        for offset in range(count):
            sequence += 1
            rows.append(_workload_row(name, offset, sequence, settings["requests_per_second"]))
    for offset in range(len(FAULTS)):
        sequence += 1
        rows.append(_fault_row(sequence, offset, overload_status, retry_after))
        if FAULTS[offset] == "service-restart":
            sequence += 1
            rows.append(_restart_recovery_row(sequence))
    return rows


def _report(rows: list[dict] | None = None) -> dict:
    from release.load.report import build_report, load_baseline, load_profiles, observation_digest

    baseline = load_baseline(BASELINE)
    observations = rows or _rows()

    return build_report(
        load_profiles(PROFILES),
        baseline,
        _identity(),
        observations,
        environment=baseline["environment"],
        service_instances=_candidate_services(baseline["environment"]),
        observation_digest=observation_digest(observations),
    )


def test_raw_rows_are_sufficient_to_recompute_every_release_metric() -> None:
    from release.load.report import recompute

    report = _report()
    recomputed = recompute(report["raw_requests"], report["profiles"])

    assert report["schema_version"] == 1
    assert report["candidate_identity"] == _identity()
    assert report["raw_requests"] == _rows()
    assert report["measurements"] == recomputed
    assert set(recomputed) == {"burst", "sustained", "soak", "overload"}
    for metrics in recomputed.values():
        assert {
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
        } == set(metrics)
    assert recomputed["sustained"]["lost_accepted_runs"] == 0
    assert recomputed["sustained"]["duplicate_accepted_runs"] == 0
    assert report["passed"] is True


def test_throughput_is_recomputed_from_the_observed_request_window() -> None:
    from release.load.report import recompute

    fast = _rows()
    slow = copy.deepcopy(fast)
    for sequence, row in enumerate(fast):
        row["started_at_ms"] = float(sequence * 10)
        row["finished_at_ms"] = float(sequence * 10 + row["latency_ms"])
    for sequence, row in enumerate(slow):
        row["started_at_ms"] = float(sequence * 100)
        row["finished_at_ms"] = float(sequence * 100 + row["latency_ms"])

    profile_values = json.loads(PROFILES.read_text(encoding="utf-8"))["profiles"]
    fast_rate = recompute(fast, profile_values)["burst"]["throughput_per_second"]
    slow_rate = recompute(slow, profile_values)["burst"]["throughput_per_second"]

    assert fast_rate > slow_rate


def test_every_profile_must_cover_the_full_matrix_and_scheduled_request_count() -> None:
    from release.load.report import evidence_errors, load_profiles

    profiles = load_profiles(PROFILES)
    rows = [
        row for row in _rows() if not (row["profile"] == "burst" and row["tenant_id"] == "tenant-c")
    ]

    errors = evidence_errors(rows, profiles)

    assert any("burst" in error and "tenant" in error for error in errors)
    assert any("scheduled request count" in error for error in errors)


def test_raw_timestamps_must_prove_the_schedule_window_and_in_flight_bound() -> None:
    from release.load.report import evidence_errors, load_profiles

    profiles = load_profiles(PROFILES)
    rows = _rows()
    for row in rows:
        if row["profile"] == "burst" and row["fault"] is None:
            row["started_at_ms"] = 0.0
            row["finished_at_ms"] = row["latency_ms"]

    errors = evidence_errors(rows, profiles)

    assert any("burst" in error and "schedule window" in error for error in errors)
    assert any("burst" in error and "in-flight" in error for error in errors)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value.pop("raw_requests"), "raw_requests"),
        (
            lambda value: value["raw_requests"][0]["lifecycle"].pop(),
            "lost accepted",
        ),
        (
            lambda value: value["raw_requests"][1]["lifecycle"][1].update(
                run_id=value["raw_requests"][0]["lifecycle"][1]["run_id"]
            ),
            "duplicate accepted",
        ),
        (
            lambda value: value["raw_requests"].__setitem__(
                slice(None),
                [row for row in value["raw_requests"] if row["fault"] != "redis-loss"],
            ),
            "redis-loss",
        ),
        (
            lambda value: next(row for row in value["raw_requests"] if row["fault"]).update(
                retry_after_seconds=None
            ),
            "Retry-After",
        ),
        (
            lambda value: value["candidate_identity"].update(commit="b" * 40),
            "candidate identity",
        ),
        (lambda value: value.update(profile_version="2"), "profile version"),
        (
            lambda value: value["profiles"]["burst"].update(duration_seconds=1),
            "profile settings",
        ),
        (lambda value: value["errors"].append("fabricated"), "reported errors"),
    ],
)
def test_missing_malformed_or_unbound_evidence_fails_closed(mutation, message: str) -> None:
    from release.load.report import validate_report

    report = _report()
    mutation(report)

    errors = validate_report(
        report,
        profiles_path=PROFILES,
        baseline_path=BASELINE,
        expected_identity=_identity(),
    )

    assert errors
    assert message.lower() in "\n".join(errors).lower()


def test_thresholds_are_literal_but_match_the_pinned_baseline_derivation() -> None:
    from release.load.report import (
        BASELINE_DIGEST,
        THRESHOLD_DERIVATION,
        THRESHOLD_RULES,
        derive_threshold,
        validate_baseline,
    )

    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    actual_digest = "sha256:" + hashlib.sha256(BASELINE.read_bytes()).hexdigest()
    assert actual_digest == BASELINE_DIGEST
    assert validate_baseline(BASELINE) == []
    for name, derivation in THRESHOLD_DERIVATION.items():
        assert THRESHOLD_RULES[name] == derive_threshold(baseline, name, derivation)


def test_baseline_raw_distributions_recompute_its_performance_metrics() -> None:
    from release.load.report import baseline_distribution_metrics

    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    profiles = json.loads(PROFILES.read_text(encoding="utf-8"))["profiles"]
    run_count = baseline["source"]["sample_run_count"]
    total = 0
    for name, samples in baseline["sample_distribution"].items():
        total += len(samples["status_code"])
        expected = (
            round(profiles[name]["duration_seconds"] * profiles[name]["requests_per_second"])
            * run_count
        )
        assert len(samples["workload_started_at_ms"]) == expected
        assert len(samples["workload_finished_at_ms"]) == expected
        derived = baseline_distribution_metrics(samples)
        for metric, value in derived.items():
            assert baseline["profiles"][name][metric] == value

    assert total == baseline["source"]["raw_request_count"]
    assert run_count >= 3


def test_report_rejects_a_malformed_candidate_identity() -> None:
    from release.load.report import build_report, load_baseline, load_profiles, observation_digest

    baseline = load_baseline(BASELINE)
    rows = _rows()
    report = build_report(
        load_profiles(PROFILES),
        baseline,
        {},
        rows,
        environment=baseline["environment"],
        service_instances=_candidate_services(baseline["environment"]),
        observation_digest=observation_digest(rows),
    )

    assert report["passed"] is False
    assert any("candidate identity" in error for error in report["errors"])


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("surface", "invented-surface"),
        ("fault", "invented-fault"),
        ("status_code", "202"),
        ("worker", []),
        ("lifecycle", ["not-an-event"]),
        (
            "lifecycle",
            [
                {"state": "accepted", "at_ms": 2.0, "run_id": "run-malformed"},
                {"state": "completed", "at_ms": 1.0, "run_id": "run-malformed"},
            ],
        ),
    ],
)
def test_malformed_raw_row_semantics_fail_closed_without_raising(field: str, value) -> None:
    from release.load.report import validate_report

    report = _report()
    report["raw_requests"][0][field] = value

    errors = validate_report(
        report,
        profiles_path=PROFILES,
        baseline_path=BASELINE,
        expected_identity=_identity(),
    )

    assert errors
    assert any("request" in error or "raw" in error for error in errors)


def test_tampering_with_the_baseline_does_not_move_thresholds(tmp_path: Path) -> None:
    from release.load.report import THRESHOLD_RULES, evaluate, validate_baseline

    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    tampered = copy.deepcopy(baseline)
    for metrics in tampered["profiles"].values():
        for name, value in metrics.items():
            if isinstance(value, (int, float)):
                metrics[name] = value * 10
    path = tmp_path / BASELINE.name
    path.write_text(json.dumps(tampered, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    assert validate_baseline(path) and "digest" in validate_baseline(path)[0]
    assert evaluate(_report()["measurements"], baseline, THRESHOLD_RULES)


def test_a_candidate_regression_blocks_even_if_the_baseline_is_edited() -> None:
    from release.load.report import THRESHOLD_RULES, evaluate, load_baseline

    baseline = load_baseline(BASELINE)
    measurements = copy.deepcopy(_report()["measurements"])
    for metrics in measurements.values():
        metrics["latency_p95_ms"] = baseline["profiles"]["sustained"]["latency_p95_ms"] * 5

    evaluation = evaluate(measurements, baseline, THRESHOLD_RULES)

    assert evaluation["latency_p95_ratio"] is False
    assert not all(evaluation.values())
