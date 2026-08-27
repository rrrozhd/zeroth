"""Regressions for ZER-33 AUDIT-6 fresh-service provenance."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]
BASELINE = ROOT / "release/load/baseline-v1.json"
DOCS = ROOT / "docs/how-to/deployment/release-gates.md"
WORKFLOW = ROOT / ".github/workflows/release-gates.yml"


def _service_instances(seed: int, environment: dict) -> dict:
    return {
        "postgres": {
            "instance_id": f"{seed:064x}",
            "started_at": f"2026-08-18T0{seed}:00:00Z",
            "image": environment["postgres"],
        },
        "redis": {
            "instance_id": f"{seed + 100:064x}",
            "started_at": f"2026-08-18T0{seed}:00:01Z",
            "image": environment["redis"],
        },
    }


def _pin_baseline(path: Path, baseline: dict, monkeypatch) -> None:
    from release.load import report

    raw = json.dumps(baseline, indent=2, sort_keys=True).encode() + b"\n"
    path.write_bytes(raw)
    monkeypatch.setattr(report, "BASELINE_DIGEST", "sha256:" + hashlib.sha256(raw).hexdigest())


def test_baseline_receipts_bind_three_distinct_fresh_service_pairs() -> None:
    from release.load.report import validate_baseline

    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    receipts = baseline["source"]["run_receipts"]
    pairs = [receipt["service_instances"] for receipt in receipts]

    assert validate_baseline(BASELINE) == []
    assert (
        len({pair[service]["instance_id"] for pair in pairs for service in ("postgres", "redis")})
        == 6
    )
    assert len({pair["postgres"]["instance_id"] for pair in pairs}) == 3
    assert len({pair["redis"]["instance_id"] for pair in pairs}) == 3
    assert all(pair[service]["started_at"].endswith("Z") for pair in pairs for service in pair)
    assert all(
        pair[service]["image"] == baseline["environment"][service]
        for pair in pairs
        for service in pair
    )


def test_baseline_validation_rejects_a_reused_service_pair(tmp_path: Path, monkeypatch) -> None:
    from release.load.report import validate_baseline

    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    receipts = baseline["source"]["run_receipts"]
    for seed, receipt in enumerate(receipts, 1):
        receipt["service_instances"] = _service_instances(seed, baseline["environment"])
    receipts[1]["service_instances"]["redis"]["instance_id"] = receipts[0]["service_instances"][
        "postgres"
    ]["instance_id"]
    candidate = tmp_path / BASELINE.name
    _pin_baseline(candidate, baseline, monkeypatch)

    errors = validate_baseline(candidate)

    assert any("distinct fresh service instances" in error for error in errors)


def test_candidate_service_pair_must_be_distinct_from_every_baseline_pair() -> None:
    from release.load.environment import observation_digest
    from release.load.report import build_report, load_profiles
    from tests.load_release.test_report import PROFILES, _identity, _rows

    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    for seed, receipt in enumerate(baseline["source"]["run_receipts"], 1):
        receipt["service_instances"] = _service_instances(seed, baseline["environment"])
    rows = _rows()
    candidate_services = _service_instances(4, baseline["environment"])
    candidate_services["postgres"]["instance_id"] = baseline["source"]["run_receipts"][0][
        "service_instances"
    ]["redis"]["instance_id"]
    report = build_report(
        load_profiles(PROFILES),
        baseline,
        _identity(),
        rows,
        environment=baseline["environment"],
        service_instances=candidate_services,
        observation_digest=observation_digest(rows),
    )

    assert report["passed"] is False
    assert any("candidate service instances overlap" in error for error in report["errors"])


def test_runner_contract_creates_inspects_and_removes_each_fresh_pair() -> None:
    docs = DOCS.read_text(encoding="utf-8")
    workflow = WORKFLOW.read_text(encoding="utf-8")

    for required in (
        "one fresh service pair per sample",
        "SAMPLE_ID",
        "{{.Id}}",
        "{{.State.StartedAt}}",
        "trap cleanup EXIT",
        "docker rm -f",
    ):
        assert required in docs
    for required in (
        "ZEROTH_LOAD_POSTGRES_INSTANCE_ID",
        "ZEROTH_LOAD_POSTGRES_STARTED_AT",
        "ZEROTH_LOAD_REDIS_INSTANCE_ID",
        "ZEROTH_LOAD_REDIS_STARTED_AT",
        "job.services.postgres.id",
        "job.services.redis.id",
        "/var/run/docker.sock",
        '["State"]["StartedAt"]',
    ):
        assert required in workflow


def test_probe_server_teardown_outlives_the_worker_shutdown_bound() -> None:
    from tests.load_release.workload_probe import _server_shutdown_timeout

    app = SimpleNamespace(
        state=SimpleNamespace(
            bootstrap=SimpleNamespace(worker=SimpleNamespace(shutdown_timeout=30.0))
        )
    )

    assert _server_shutdown_timeout(app) > app.state.bootstrap.worker.shutdown_timeout
