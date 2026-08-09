from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from release.acceptance.config import AcceptanceConfig
from release.acceptance.models import (
    REQUIRED_SCENARIOS,
    AcceptanceContract,
    ScenarioStatus,
)
from release.acceptance.runner import AcceptanceRunner
from release.acceptance.transport import HttpObservation


def _config(tmp_path: Path):
    identity = tmp_path / "identity.json"
    identity.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "commit": "a" * 40,
                "package": {"version": "1", "artifacts": {}},
                "image": {"candidate": "sha256:" + "b" * 64},
            }
        ),
        encoding="utf-8",
    )
    return AcceptanceConfig.model_validate(
        {
            "schema_version": 1,
            "base_url": "https://candidate.example",
            "tenant_id": "acceptance-tenant",
            "deployment_ref": "dep",
            "candidate_identity": str(identity),
            "credentials": {"operator": "OP", "reviewer": "REV", "admin": "ADM"},
            "poll_deadline_seconds": 1,
            "poll_interval_seconds": 0.01,
            "lifecycle": {
                "restart_url": "/restart",
                "shutdown_url": "/shutdown",
                "restart_status": 200,
                "shutdown_status": 200,
            },
        }
    ).resolve({"OP": "op", "REV": "rev", "ADM": "adm"}, run_id="01234567")


def _step(path: str, **overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "protocol": "http",
        "role": "operator",
        "method": "GET",
        "path": path,
        "expected_status": 200,
    }
    value.update(overrides)
    return value


def _contract() -> dict[str, object]:
    scenarios = {name: {"steps": [_step(f"/{name}")]} for name in REQUIRED_SCENARIOS}
    scenarios["readiness"] = {
        "steps": [
            _step("/readiness", expected_json={"status": "ok"}),
            _step("/identity", expected_json={"deployment_ref": "{deployment_ref}"}),
        ]
    }
    scenarios["authentication"] = {
        "steps": [_step("/authentication", role="anonymous", expected_status=401)]
    }
    scenarios["rbac"] = {"steps": [_step("/rbac", role="reviewer", expected_status=403)]}
    scenarios["workflow_lifecycle"] = {
        "steps": [
            _step("/workflow/create", method="POST", expected_json={"status": "draft"}),
            _step("/workflow/read"),
            _step("/workflow/publish", method="POST", expected_json={"status": "published"}),
        ]
    }
    scenarios["deployment"] = {
        "steps": [_step("/deployment", expected_json={"deployment_ref": "{deployment_ref}"})]
    }
    scenarios["runs"] = {
        "steps": [
            _step("/runs", method="POST", expected_status=202),
            _step("/runs/settled", expected_json={"status": "succeeded"}, poll=True),
        ]
    }
    scenarios["retention"] = {"steps": [_step("/retention", expected_json={"enabled": True})]}
    scenarios["gateway_http"] = {
        "steps": [
            _step("/gateway/allow", method="POST", require_correlation=True),
            _step(
                "/gateway/deny",
                method="POST",
                expected_status=403,
                expected_json={"code": "zeroth.policy_denied"},
                require_correlation=True,
            ),
            _step(
                "/gateway/upstream-failure",
                method="POST",
                expected_status=502,
                expected_json={"code": "zeroth.upstream_unavailable"},
                require_correlation=True,
            ),
        ]
    }
    scenarios["gateway_websocket"] = {
        "steps": [
            {
                "protocol": "websocket",
                "role": "operator",
                "path": "/gateway-ws/stream",
                "payload": {},
                "max_events": 2,
                "ordered_events": ["metadata", "values"],
            }
        ]
    }
    scenarios["compatibility"] = {
        "steps": [
            _step(
                "/compatibility",
                expected_json={"status": "supported", "detected_agent_server": "0.11.1"},
            )
        ]
    }
    counted = {"count_path": "entries", "count_where": {"node_id": "finish", "status": "completed"}}
    scenarios["approvals"] = {
        "steps": [
            _step("/approval/before", expected_count=0, **counted),
            _step("/approval/resolve", method="POST"),
            _step("/approval/after", expected_count=1, **counted),
        ]
    }
    scenarios["streaming"] = {
        "steps": [
            {
                "protocol": "websocket",
                "role": "operator",
                "path": "/stream",
                "payload": {},
                "max_events": 3,
                "ordered_events": ["run.started", "node.completed", "run.completed"],
            }
        ]
    }
    anchor = _step(
        "/anchors",
        count_path="audits",
        count_where={"node_id": "finish", "status": "completed"},
        expected_count=1,
    )
    scenarios["restart_recovery"] = {
        "steps": [
            dict(anchor),
            {"protocol": "lifecycle", "role": "admin", "operation": "restart"},
            dict(anchor),
        ]
    }
    scenarios["shutdown"] = {
        "steps": [
            {"protocol": "lifecycle", "role": "admin", "operation": "shutdown"},
            _step("/health/ready", expected_status=503),
        ]
    }
    return {
        "schema_version": 1,
        "supported_agent_server_versions": ["0.11.1"],
        "scenarios": scenarios,
        "cleanup": [
            _step(
                "/fixtures/{namespace}-workflow",
                method="DELETE",
                expected_status=204,
                resource_id="{namespace}-workflow",
            )
        ],
    }


def test_contract_requires_every_scenario_and_pins_approval_and_lifecycle_invariants() -> None:
    missing = _contract()
    del missing["scenarios"]["audit"]
    with pytest.raises(ValidationError, match="audit"):
        AcceptanceContract.model_validate(missing)

    weak_approval = _contract()
    weak_approval["scenarios"]["approvals"]["steps"][-1]["expected_count"] = 2
    with pytest.raises(ValidationError, match="zero executions before approval"):
        AcceptanceContract.model_validate(weak_approval)

    drifting_approval = _contract()
    drifting_approval["scenarios"]["approvals"]["steps"][-1]["count_where"] = {"node_id": "other"}
    with pytest.raises(ValidationError, match="same records before and after"):
        AcceptanceContract.model_validate(drifting_approval)

    weak_restart = _contract()
    weak_restart["scenarios"]["restart_recovery"]["steps"][-1]["expected_count"] = 7
    with pytest.raises(ValidationError, match="identical durable fact"):
        AcceptanceContract.model_validate(weak_restart)

    no_restart = _contract()
    no_restart["scenarios"]["restart_recovery"]["steps"][1] = _step("/pretend", method="POST")
    with pytest.raises(ValidationError, match="restart lifecycle operation"):
        AcceptanceContract.model_validate(no_restart)

    no_identity = _contract()
    no_identity["scenarios"]["readiness"]["steps"] = [
        _step("/readiness", expected_json={"status": "ok"})
    ]
    with pytest.raises(ValidationError, match="bind the serving deployment"):
        AcceptanceContract.model_validate(no_identity)

    extra = _contract()
    extra["scenarios"]["optional-looking-skip"] = {"steps": [_step("/ignored")]}
    with pytest.raises(ValidationError, match="unknown scenarios"):
        AcceptanceContract.model_validate(extra)


def test_contract_cannot_mark_a_read_or_non_namespaced_create_as_cleanup_owned() -> None:
    read_capture = _contract()
    read_capture["scenarios"]["artifacts"]["steps"][0]["owned_capture"] = {"foreign_id": "id"}
    with pytest.raises(ValidationError, match="mutating create"):
        AcceptanceContract.model_validate(read_capture)

    unsafe_create = _contract()
    unsafe_create["scenarios"]["artifacts"]["steps"] = [
        _step(
            "/artifacts",
            method="POST",
            payload={"name": "not-namespaced"},
            owned_capture={"foreign_id": "id"},
        )
    ]
    with pytest.raises(ValidationError, match="namespace"):
        AcceptanceContract.model_validate(unsafe_create)


class FakeTransport:
    def __init__(self, responses: dict[str, HttpObservation]) -> None:
        self.responses = responses
        self.requested: list[tuple[str | None, str, str]] = []

    async def request(self, role, method, path, *, json_body=None):
        self.requested.append((role, method, path))
        return self.responses.get(path, HttpObservation(200, {}, "corr"))

    async def websocket_events(self, role, path, payload, *, max_events):
        if path.startswith("/gateway-ws/"):
            return [
                {"event": "metadata", "sequence": 1},
                {"event": "values", "sequence": 2},
            ]
        return [
            {"event": "run.started", "sequence": 1},
            {"event": "node.completed", "sequence": 2},
            {"event": "run.completed", "sequence": 3},
        ]


@pytest.mark.asyncio
async def test_runner_produces_identity_bound_report_and_cleans_owned_resources(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    responses = {
        "/identity": HttpObservation(
            200,
            {
                "candidate_digest": config.candidate_digest,
                "deployment_ref": config.deployment_ref,
            },
            "corr-identity",
        ),
        "/authentication": HttpObservation(401, {"detail": "not authenticated"}, "corr-auth"),
        "/rbac": HttpObservation(403, {}, "corr-rbac"),
        "/workflow/create": HttpObservation(200, {"status": "draft"}, "corr-workflow"),
        "/workflow/publish": HttpObservation(200, {"status": "published"}, "corr-publish"),
        "/deployment": HttpObservation(200, {"deployment_ref": "dep"}, "corr-deployment"),
        "/readiness": HttpObservation(200, {"status": "ok"}, "corr-ready"),
        "/runs": HttpObservation(202, {}, "corr-run"),
        "/runs/settled": HttpObservation(200, {"status": "succeeded"}, "corr-run-done"),
        "/retention": HttpObservation(200, {"enabled": True}, "corr-retention"),
        "/gateway/allow": HttpObservation(200, {"forwarded": True}, "corr-gateway"),
        "/gateway/deny": HttpObservation(403, {"code": "zeroth.policy_denied"}, "corr-gateway"),
        "/gateway/upstream-failure": HttpObservation(
            502, {"code": "zeroth.upstream_unavailable"}, "corr-gateway"
        ),
        "/health/ready": HttpObservation(503, {}, "corr-shutdown"),
        "/compatibility": HttpObservation(
            200, {"status": "supported", "detected_agent_server": "0.11.1"}, "corr-compat"
        ),
        "/approval/before": HttpObservation(200, {"entries": []}, "corr-before"),
        "/approval/after": HttpObservation(
            200, {"entries": [{"node_id": "finish", "status": "completed"}]}, "corr-after"
        ),
        "/anchors": HttpObservation(
            200, {"audits": [{"node_id": "finish", "status": "completed"}]}, "corr-anchor"
        ),
        f"/fixtures/{config.namespace}-workflow": HttpObservation(204, None, "corr-clean"),
    }
    transport = FakeTransport(responses)

    report = await AcceptanceRunner(
        config, AcceptanceContract.model_validate(_contract()), transport
    ).run()

    assert report.status is ScenarioStatus.PASSED
    assert report.candidate_digest == config.candidate_digest
    assert report.image_identity == config.candidate_identity["image"]
    assert report.tenant_id == "acceptance-tenant"
    assert report.namespace == config.namespace
    assert all(result.status is ScenarioStatus.PASSED for result in report.scenarios)
    assert report.cleanup[0].status is ScenarioStatus.PASSED
    # Lifecycle is a platform operation, so it authenticates as admin regardless of
    # which role the surrounding scenario's probes use.
    assert ("admin", "POST", "/restart") in transport.requested
    assert ("admin", "POST", "/shutdown") in transport.requested


@pytest.mark.asyncio
async def test_runner_fails_visibly_on_unsupported_compatibility_but_still_cleans(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    responses = {
        "/authentication": HttpObservation(401, {}, "corr-auth"),
        "/compatibility": HttpObservation(
            200, {"status": "unsupported", "detected_agent_server": "0.12.0"}, "corr-compat"
        ),
        f"/fixtures/{config.namespace}-workflow": HttpObservation(204, None, "corr-clean"),
    }
    transport = FakeTransport(responses)

    report = await AcceptanceRunner(
        config, AcceptanceContract.model_validate(_contract()), transport
    ).run()

    compatibility = next(item for item in report.scenarios if item.name == "compatibility")
    assert compatibility.status is ScenarioStatus.FAILED
    assert "unsupported" in compatibility.detail
    assert report.status is ScenarioStatus.FAILED
    assert report.cleanup[0].status is ScenarioStatus.PASSED


@pytest.mark.asyncio
async def test_cleanup_refuses_a_contract_resource_outside_the_invocation_namespace(
    tmp_path: Path,
) -> None:
    contract = _contract()
    contract["cleanup"][0]["resource_id"] = "production-workflow"
    runner = AcceptanceRunner(
        _config(tmp_path), AcceptanceContract.model_validate(contract), FakeTransport({})
    )

    report = await runner.run()

    assert report.cleanup[0].status is ScenarioStatus.FAILED
    assert "outside acceptance namespace" in report.cleanup[0].detail
    assert report.status is ScenarioStatus.FAILED


@pytest.mark.asyncio
async def test_owned_capture_refuses_server_identifier_outside_namespace(tmp_path: Path) -> None:
    config = _config(tmp_path)
    contract = _contract()
    contract["scenarios"]["artifacts"]["steps"] = [
        _step(
            "/create",
            method="POST",
            payload={"name": "{namespace}-artifact"},
            expected_json={"name": "{namespace}-artifact"},
            owned_capture={"artifact_id": "id"},
        )
    ]
    transport = FakeTransport({"/create": HttpObservation(200, {"id": "production"}, "corr")})

    report = await AcceptanceRunner(
        config, AcceptanceContract.model_validate(contract), transport
    ).run()

    artifact = next(item for item in report.scenarios if item.name == "artifacts")
    assert artifact.status is ScenarioStatus.FAILED
    assert "body.name is missing" in artifact.detail
