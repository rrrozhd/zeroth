from __future__ import annotations

import json
import sqlite3
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest

from release.live_evaluation.native_safari_resilient_http_checkpoint import (
    DEPLOYMENT,
    GRAPH,
    HEALTH,
    RUNS,
    build_checkpoint,
)


def _audit(
    run_id: str,
    sequence: int,
    node_id: str,
    status: str,
    metadata: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "audit_id": f"{run_id}:audit:{sequence}",
        "run_id": run_id,
        "thread_id": run_id,
        "deployment_ref": DEPLOYMENT,
        "graph_version_ref": GRAPH,
        "node_id": node_id,
        "status": status,
        "chain_sequence": sequence,
        "record_digest": f"{sequence:x}" * 64,
        "record_signature": f"{sequence + 5:x}" * 64,
        "signing_key_id": "dev-local",
        "signing_algorithm": "HS256",
        "cost_usd": 0.0,
        "estimated_cost_usd": 0.0,
        "cost_event_id": None,
        "provider_request_id": None,
        "execution_metadata": {"cost_usd": 0.0, **(metadata or {})},
    }


def _responses() -> dict[tuple[str, str], tuple[int, dict[str, object]]]:
    values: dict[tuple[str, str], tuple[int, dict[str, object]]] = {
        ("GET", "/health"): (200, dict(HEALTH))
    }
    cases = {
        RUNS[0]: {
            "status": "succeeded",
            "nodes": (
                ("request", "completed", {}),
                ("route-retry", "completed", {}),
                (
                    "http-retry",
                    "completed",
                    {
                        "node_kind": "http_request",
                        "retry_count": 2,
                        "upstream_status_code": 200,
                        "duration_ms": 12.0,
                        "target_url_sha256": "a" * 64,
                    },
                ),
            ),
            "failure": None,
            "terminal": {
                "scenario": "retry",
                "http_response": {
                    "status_code": 200,
                    "body": {"scenario": "retry-then-success", "attempt": 3},
                },
            },
        },
        RUNS[1]: {
            "status": "failed",
            "nodes": (
                ("request", "completed", {}),
                ("route-retry", "completed", {}),
                ("route-timeout", "completed", {}),
                (
                    "http-timeout",
                    "failed",
                    {
                        "node_kind": "http_request",
                        "reason_code": "http_retry_exhausted_error",
                        "retry_count": 2,
                        "duration_ms": 128.0,
                        "target_url_sha256": "b" * 64,
                    },
                ),
            ),
            "failure": {
                "reason": "node_execution_failed",
                "message": "All 2 retry attempts exhausted. Last error: ReadTimeout",
                "details": {},
            },
            "terminal": None,
        },
        RUNS[2]: {
            "status": "failed",
            "nodes": (
                ("request", "completed", {}),
                ("route-retry", "completed", {}),
                ("route-timeout", "completed", {}),
                (
                    "http-circuit",
                    "failed",
                    {
                        "node_kind": "http_request",
                        "reason_code": "http_retry_exhausted_error",
                        "retry_count": 0,
                        "duration_ms": 14.0,
                        "target_url_sha256": "c" * 64,
                    },
                ),
            ),
            "failure": {
                "reason": "node_execution_failed",
                "message": "All 0 retry attempts exhausted. Last error: HTTP 503",
                "details": {},
            },
            "terminal": None,
        },
        RUNS[3]: {
            "status": "failed",
            "nodes": (
                ("request", "completed", {}),
                ("route-retry", "completed", {}),
                ("route-timeout", "completed", {}),
                (
                    "http-circuit",
                    "failed",
                    {
                        "node_kind": "http_request",
                        "reason_code": "circuit_open_error",
                        "retry_count": 0,
                        "duration_ms": 0.0,
                        "target_url_sha256": "c" * 64,
                    },
                ),
            ),
            "failure": {
                "reason": "node_execution_failed",
                "message": "Circuit breaker open for endpoint 123",
                "details": {},
            },
            "terminal": None,
        },
        RUNS[4]: {
            "status": "succeeded",
            "nodes": (
                ("request", "completed", {}),
                ("route-retry", "completed", {}),
                ("route-timeout", "completed", {}),
                (
                    "http-circuit",
                    "completed",
                    {
                        "node_kind": "http_request",
                        "retry_count": 0,
                        "upstream_status_code": 200,
                        "duration_ms": 11.0,
                        "target_url_sha256": "c" * 64,
                    },
                ),
            ),
            "failure": None,
            "terminal": {
                "scenario": "circuit",
                "http_response": {
                    "status_code": 200,
                    "body": {"scenario": "circuit", "recovered": True},
                },
            },
        },
    }
    for run_id, case in cases.items():
        audits = [
            _audit(run_id, index, node, status, metadata)
            for index, (node, status, metadata) in enumerate(case["nodes"], start=1)
        ]
        run = {
            "run_id": run_id,
            "thread_id": run_id,
            "tenant_id": "evaluation-studio-v1",
            "campaign_id": "evaluation-studio-v1",
            "deployment_ref": DEPLOYMENT,
            "graph_version_ref": GRAPH,
            "status": case["status"],
            "failure_state": case["failure"],
            "terminal_output": case["terminal"],
            "audit_refs": [f"audit:{index}" for index in range(1, len(audits) + 1)],
        }
        summary = {
            "approval_count": 0,
            "audit_count": len(audits),
            "cost_event_count": 0,
            "cost_identity_state": "not_applicable_no_priced_call",
            "memory_interaction_count": 0,
            "priced_call_count": 0,
            "reconciliation_state": "reconciled_zero_activity",
            "tool_call_count": 0,
            "total_cost_usd": 0.0,
        }
        values[("GET", f"/v1/runs/{run_id}")] = (200, run)
        values[("GET", f"/v1/runs/{run_id}/timeline")] = (
            200,
            {"deployment_ref": DEPLOYMENT, "run_id": run_id, "entries": audits},
        )
        values[("GET", f"/v1/runs/{run_id}/evidence")] = (
            200,
            {"run": run, "audits": audits, "approvals": [], "summary": summary},
        )
        values[("POST", f"/v1/runs/{run_id}/verify-chain")] = (
            200,
            {
                "scope": f"run:{run_id}",
                "verified": True,
                "signature_verified": True,
                "record_count": len(audits),
                "unsigned_record_count": 0,
                "signing_key_id": "dev-local",
                "failed_audit_id": None,
                "error": None,
            },
        )
    return values


def _fixture(tmp_path: Path):
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "README.md").write_text("fixture\n")
    subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
    subprocess.run(["git", "add", "README.md"], cwd=repository, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
        cwd=repository,
        check=True,
    )
    state = tmp_path / "state"
    state.mkdir()
    source = state / "staging/native-safari-resilient-http-20260826-1"
    screenshots = source / "screenshots"
    screenshots.mkdir(parents=True)
    for name in (
        "01-configured-workflow.png",
        "02-retry-succeeded.png",
        "03-timeout-failed.png",
        "04-circuit-open.png",
        "05-recovery-succeeded.png",
    ):
        (screenshots / name).write_bytes(b"\xff\xd8\xff" + b"safe-safari-image" * 64)
    secret = state / "runtime-secrets/service-api-key"
    secret.parent.mkdir()
    secret.write_text("service-secret-must-never-be-retained")
    secret.chmod(0o600)
    econ = state / "econ.db"
    with sqlite3.connect(econ) as database:
        database.execute(
            "CREATE TABLE execution_events "
            "(execution_id TEXT, join_key TEXT, metadata TEXT, provider_request_id TEXT, "
            "token_cost_usd NUMERIC, tool_cost_usd NUMERIC, compute_cost_usd NUMERIC)"
        )
        database.execute(
            "CREATE TABLE cost_reservations "
            "(run_id TEXT, cost_event_id TEXT, provider_request_id TEXT, actual_cost_usd NUMERIC)"
        )
    return repository, state, source, secret, econ


def test_seals_exact_native_safari_run_audit_and_zero_economics_join(tmp_path: Path) -> None:
    repository, state, source, secret, econ = _fixture(tmp_path)
    destination = state / "evidence/native-safari-resilient-http-accepted-20260826-1"
    responses = _responses()
    calls: list[tuple[str, str]] = []

    def request_json(method: str, path: str, api_key: str):
        assert api_key == "service-secret-must-never-be-retained"
        calls.append((method, path))
        return deepcopy(responses[(method, path)])

    result = build_checkpoint(
        source_root=source,
        destination=destination,
        repository_root=repository,
        state_root=state,
        service_api_key_path=secret,
        econ_database=econ,
        request_json=request_json,
    )

    assert result == destination
    assert len(calls) == 21
    assert len(set(calls)) == 21
    assert set(calls) == set(responses)
    assert {
        "manifest.json",
        "events.ndjson",
        "acceptance.json",
        "report.md",
        "SHA256SUMS",
        "runtime/health.json",
        "runtime/runs.json",
        "runtime/evidence.json",
        "runtime/verify-chain.json",
        "runtime/economics.json",
        "screenshots/01-configured-workflow.jpg",
        "screenshots/02-retry-succeeded.jpg",
        "screenshots/03-timeout-failed.jpg",
        "screenshots/04-circuit-open.jpg",
        "screenshots/05-recovery-succeeded.jpg",
    } == {
        path.relative_to(destination).as_posix()
        for path in destination.rglob("*")
        if path.is_file()
    }
    acceptance = json.loads((destination / "acceptance.json").read_text())
    assert acceptance["criteria"]
    assert {row["status"] for row in acceptance["criteria"]} == {"pass"}
    assert [
        row["run_id"] for row in json.loads((destination / "runtime/runs.json").read_text())
    ] == list(RUNS)
    checksums = (destination / "SHA256SUMS").read_text().splitlines()
    assert len(checksums) == 14
    serialized = b"".join(path.read_bytes() for path in destination.rglob("*") if path.is_file())
    assert b"service-secret-must-never-be-retained" not in serialized


@pytest.mark.parametrize(
    "mutation",
    ("d012", "unsigned", "provider_id", "cost", "circuit_reason", "chain", "econ"),
)
def test_fails_closed_before_creating_bundle(tmp_path: Path, mutation: str) -> None:
    repository, state, source, secret, econ = _fixture(tmp_path)
    destination = state / "evidence/native-safari-resilient-http-accepted-20260826-1"
    responses = _responses()
    if mutation == "d012":
        responses[("GET", "/health")][1]["deployment_ref"] = "wrong"
    elif mutation == "unsigned":
        responses[("GET", f"/v1/runs/{RUNS[0]}/evidence")][1]["audits"][0]["record_signature"] = (
            None
        )
    elif mutation == "provider_id":
        responses[("GET", f"/v1/runs/{RUNS[0]}/evidence")][1]["audits"][0][
            "provider_request_id"
        ] = "provider-call-1"
    elif mutation == "cost":
        responses[("GET", f"/v1/runs/{RUNS[0]}/evidence")][1]["summary"]["total_cost_usd"] = 0.01
    elif mutation == "circuit_reason":
        responses[("GET", f"/v1/runs/{RUNS[3]}/evidence")][1]["audits"][-1]["execution_metadata"][
            "reason_code"
        ] = "wrong"
    elif mutation == "chain":
        responses[("POST", f"/v1/runs/{RUNS[4]}/verify-chain")][1]["verified"] = False
    else:
        with sqlite3.connect(econ) as database:
            database.execute(
                "INSERT INTO cost_reservations VALUES (?,?,?,?)",
                (RUNS[0], "cost-1", None, 0),
            )

    def request_json(method: str, path: str, _api_key: str):
        return deepcopy(responses[(method, path)])

    with pytest.raises(RuntimeError):
        build_checkpoint(
            source_root=source,
            destination=destination,
            repository_root=repository,
            state_root=state,
            service_api_key_path=secret,
            econ_database=econ,
            request_json=request_json,
        )
    assert not destination.exists()


def test_rejects_non_loopback_service_and_legacy_econ_database(tmp_path: Path) -> None:
    repository, state, source, secret, econ = _fixture(tmp_path)
    (state / "econ_plane.db").touch()
    destination = state / "evidence/native-safari-resilient-http-accepted-20260826-1"

    with pytest.raises(ValueError, match="loopback"):
        build_checkpoint(
            source_root=source,
            destination=destination,
            repository_root=repository,
            state_root=state,
            service_api_key_path=secret,
            econ_database=econ,
            base_url="https://example.com",
            request_json=lambda *_: (500, {}),
        )
    with pytest.raises(RuntimeError, match="econ_plane.db"):
        build_checkpoint(
            source_root=source,
            destination=destination,
            repository_root=repository,
            state_root=state,
            service_api_key_path=secret,
            econ_database=econ,
            request_json=lambda method, path, _key: deepcopy(_responses()[(method, path)]),
        )
