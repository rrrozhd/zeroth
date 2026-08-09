from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from release.acceptance.config import AcceptanceConfig
from release.acceptance.models import REQUIRED_SCENARIOS, AcceptanceContract, ScenarioStatus
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
            "lifecycle": {"restart_url": "/restart", "shutdown_url": "/shutdown"},
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
    scenarios["authentication"] = {
        "steps": [_step("/authentication", role="anonymous", expected_status=401)]
    }
    scenarios["compatibility"] = {
        "steps": [
            _step(
                "/compatibility",
                expected_json={"status": "supported", "detected_agent_server": "0.11.1"},
            )
        ]
    }
    scenarios["approvals"] = {
        "steps": [
            _step("/approval/before", expected_json={"tool_execution_count": 0}),
            _step("/approval/resolve", method="POST"),
            _step("/approval/after", expected_json={"tool_execution_count": 1}),
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
    scenarios["restart_recovery"] = {
        "steps": [
            _step(
                "/anchors/before", expected_json={"run": True, "approval": True, "artifact": True}
            ),
            _step("{restart_url}", method="POST"),
            _step(
                "/anchors/after", expected_json={"run": True, "approval": True, "artifact": True}
            ),
        ]
    }
    scenarios["shutdown"] = {"steps": [_step("{shutdown_url}", method="POST")]}
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
    weak_approval["scenarios"]["approvals"]["steps"][-1]["expected_json"] = {
        "tool_execution_count": 2
    }
    with pytest.raises(ValidationError, match="zero times.*exactly once"):
        AcceptanceContract.model_validate(weak_approval)

    no_restart = _contract()
    no_restart["scenarios"]["restart_recovery"]["steps"][1]["path"] = "/pretend"
    with pytest.raises(ValidationError, match="restart_url"):
        AcceptanceContract.model_validate(no_restart)


class FakeTransport:
    def __init__(self, responses: dict[str, HttpObservation]) -> None:
        self.responses = responses
        self.requested: list[tuple[str | None, str, str]] = []

    async def request(self, role, method, path, *, json_body=None):
        self.requested.append((role, method, path))
        return self.responses.get(path, HttpObservation(200, {}, "corr"))

    async def websocket_events(self, role, path, payload, *, max_events):
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
        "/authentication": HttpObservation(401, {"detail": "not authenticated"}, "corr-auth"),
        "/compatibility": HttpObservation(
            200, {"status": "supported", "detected_agent_server": "0.11.1"}, "corr-compat"
        ),
        "/approval/before": HttpObservation(200, {"tool_execution_count": 0}, "corr-before"),
        "/approval/after": HttpObservation(200, {"tool_execution_count": 1}, "corr-after"),
        "/anchors/before": HttpObservation(
            200, {"run": True, "approval": True, "artifact": True}, "corr-anchor-1"
        ),
        "/anchors/after": HttpObservation(
            200, {"run": True, "approval": True, "artifact": True}, "corr-anchor-2"
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
    assert ("operator", "POST", "/restart") in transport.requested
    assert ("operator", "POST", "/shutdown") in transport.requested


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
