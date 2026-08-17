"""ZER-33 release-gate contract: profiles, evidence, and workflow wiring."""

from __future__ import annotations

import json
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "release/gates/release-gates.json"
PROFILES = ROOT / "release/load/profiles-v1.json"
WORKFLOW = ROOT / ".github/workflows/release-gates.yml"
DOCS = ROOT / "docs/how-to/deployment/release-gates.md"


def _manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _workflow() -> dict:
    document = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    document["on"] = document.pop(True, document.get("on"))
    return document


def test_manifest_requires_one_candidate_bound_load_recovery_gate() -> None:
    gates = {gate["id"]: gate for gate in _manifest()["gates"]}

    gate = gates["load-recovery"]
    assert gate == {
        "id": "load-recovery",
        "order": 6,
        "phase": "candidate",
        "title": "Load and recovery",
        "description": (
            "Versioned burst, sustained, soak, overload, and fault profiles hold "
            "their candidate-versus-baseline thresholds without losing or duplicating "
            "accepted runs."
        ),
        "binds": ["commit", "package"],
        "record": "release/evidence/load-recovery.json",
        "requires": ["profiles", "thresholds", "recovery", "accepted-run-integrity"],
        "kinds": ["benchmark", "junit"],
        "triggers": ["nightly", "release-candidate"],
    }


def test_profiles_are_versioned_complete_and_bounded() -> None:
    payload = json.loads(PROFILES.read_text(encoding="utf-8"))

    assert payload["schema_version"] == 1
    assert payload["profile_version"] == "1"
    assert set(payload["profiles"]) == {"burst", "sustained", "soak", "overload"}
    assert set(payload["faults"]) == {
        "database-contention",
        "redis-loss",
        "worker-loss",
        "service-restart",
        "network-delay",
        "downstream-throttling",
    }
    assert payload["matrix"] == {
        "tenants": 3,
        "deployments_per_tenant": 2,
        "replicas": 2,
        "workers": 3,
    }
    assert payload["surfaces"] == [
        "langgraph-streams",
        "slow-script",
        "failing-script",
        "approvals",
        "artifacts",
        "webhooks",
    ]
    for profile in payload["profiles"].values():
        assert profile["duration_seconds"] > 0
        assert profile["requests_per_second"] > 0
        assert profile["max_in_flight"] > 0


def test_thresholds_cover_every_required_measurement_and_failure_semantic() -> None:
    payload = json.loads(PROFILES.read_text(encoding="utf-8"))
    rules = payload["thresholds"]["rules"]

    assert set(rules) == {
        "throughput_ratio",
        "latency_p50_ratio",
        "latency_p95_ratio",
        "latency_p99_ratio",
        "rejection_rate_delta",
        "queue_depth_ratio",
        "tenant_fairness_minimum",
        "cpu_ratio",
        "memory_ratio",
        "recovery_seconds_ratio",
        "lost_accepted_runs",
        "duplicate_accepted_runs",
    }
    assert rules["lost_accepted_runs"] == {"maximum": 0}
    assert rules["duplicate_accepted_runs"] == {"maximum": 0}
    assert payload["overload_contract"] == {
        "statuses": [429, 503],
        "require_retry_after": True,
        "drain": True,
        "cancellation": True,
    }


def test_workflow_runs_the_gate_in_an_isolated_service_matrix() -> None:
    job = _workflow()["jobs"]["load-recovery"]
    script = "\n".join(step.get("run", "") for step in job["steps"] if isinstance(step, dict))

    assert job["needs"] == ["candidate"]
    assert job["services"]["redis"]["image"].startswith("redis:7.4-alpine@sha256:")
    assert job["services"]["postgres"]["image"].startswith("postgres:")
    assert "pg_isready" in job["services"]["postgres"]["options"]
    assert job["env"]["ZEROTH_LOAD_REDIS_URL"] == "redis://redis:6379/14"
    assert job["env"]["ZEROTH_TEST_REDIS_URL"] == "redis://redis:6379/15"
    assert job["env"]["ZEROTH_LOAD_POSTGRES_DSN"].startswith("postgresql://")
    assert "release/load/harness.py run" in script
    assert "--profiles release/load/profiles-v1.json" in script
    assert "--output release/evidence/load-recovery-benchmark.json" in script
    assert "--identity release/evidence/candidate-identity.json" in script
    assert "tests/load_release/test_product_profiles.py" in script
    assert "--junitxml=release/evidence/load-recovery-junit.xml" in script
    assert "--gate load-recovery" in script
    assert "--kind benchmark=release/evidence/load-recovery-benchmark.json" in script
    assert "--kind junit=release/evidence/load-recovery-junit.xml" in script


def test_workflow_retains_raw_measurements_with_the_gate_record() -> None:
    job = _workflow()["jobs"]["load-recovery"]
    upload = next(
        step
        for step in job["steps"]
        if str(step.get("uses", "")).startswith("actions/upload-artifact")
    )

    assert upload["if"] == "always()"
    assert upload["with"]["name"] == "gate-load-recovery"
    assert upload["with"]["path"] == "release/evidence/load-recovery*"
    assert upload["with"]["retention-days"] >= 30


def test_operator_docs_pin_commands_capacity_envelope_thresholds_and_retention() -> None:
    page = DOCS.read_text(encoding="utf-8")

    for required in (
        "release/load/profiles-v1.json",
        "release/load/harness.py run",
        "3 tenants",
        "2 deployments per tenant",
        "2 replicas",
        "3 workers",
        "Candidate safe envelope",
        "Baseline and fixed thresholds",
        "three isolated times",
        "load-recovery*",
        "30 days",
    ):
        assert required in page
