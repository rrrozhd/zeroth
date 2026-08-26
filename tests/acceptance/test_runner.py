from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from release.acceptance.config import AcceptanceConfig
from release.acceptance.models import (
    REQUIRED_SCENARIOS,
    AcceptanceContract,
    AcceptanceStep,
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
    scenarios["artifacts"] = {"steps": [_step("/artifacts"), _step("/v1/artifacts/{artifact_key}")]}
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
    scenarios["migrations"] = {
        "steps": [
            _step(
                "/health/ready",
                role="anonymous",
                expected_json={
                    "schema_revision": {
                        "applied": "029",
                        "head": "029",
                        "state": "current",
                    }
                },
            ),
            _step(
                "/regulus/health",
                role="admin",
                expected_json={
                    "schema_revision": {
                        "applied": "20260812_07",
                        "head": "20260812_07",
                        "state": "current",
                    }
                },
            ),
        ]
    }
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
            _step(
                "/runs",
                method="POST",
                expected_status=202,
                expected_json={"tenant_id": "{tenant_id}"},
            ),
            _step(
                "/runs/settled",
                expected_json={"status": "succeeded"},
                poll=True,
                capture={"artifact_key": "terminal_output.artifact.key"},
            ),
        ]
    }
    scenarios["retention"] = {
        "steps": [
            _step("/retention", expected_json={"enabled": True}),
            _step("/retention/erase", method="POST", expected_status=409),
        ]
    }
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
                expected_json={"checks": {"agent_server": {"status": "supported"}}},
            ),
            _step(
                "/health",
                expected_json={
                    "langgraph_gateway": {
                        "compatibility": {
                            "status": "supported",
                            "detected_agent_server": "0.11.1",
                        }
                    }
                },
            ),
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

    weak_migrations = _contract()
    weak_migrations["scenarios"]["migrations"] = {
        "steps": [_step("/__acceptance/migrations", expected_json={"current": True})]
    }
    with pytest.raises(ValidationError, match="migrations must pin current schema revisions"):
        AcceptanceContract.model_validate(weak_migrations)

    stale_migrations = _contract()
    stale_migrations["scenarios"]["migrations"]["steps"][0]["expected_json"]["schema_revision"][
        "applied"
    ] = "025"
    with pytest.raises(ValidationError, match="migrations must pin current schema revisions"):
        AcceptanceContract.model_validate(stale_migrations)

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
        self.draining = False

    async def request(self, role, method, path, *, json_body=None):
        self.requested.append((role, method, path))
        if path == "/shutdown":
            self.draining = True
        if path == "/health/ready" and self.draining:
            return HttpObservation(503, {}, "corr-shutdown")
        return self.responses.get(path, HttpObservation(200, {}, "corr"))

    async def websocket_events(self, role, path, payload, *, max_events, frames=None):
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
        "/health": HttpObservation(
            200,
            {
                "langgraph_gateway": {
                    "compatibility": {"status": "supported", "detected_agent_server": "0.11.1"}
                }
            },
            "corr-health",
        ),
        "/readiness": HttpObservation(200, {"status": "ok"}, "corr-ready"),
        "/runs": HttpObservation(202, {"tenant_id": config.tenant_id}, "corr-run"),
        "/runs/settled": HttpObservation(
            200,
            {"status": "succeeded", "terminal_output": {"artifact": {"key": "blob-1"}}},
            "corr-run-done",
        ),
        "/retention": HttpObservation(200, {"enabled": True}, "corr-retention"),
        "/retention/erase": HttpObservation(409, {"detail": "held"}, "corr-erase"),
        "/gateway/allow": HttpObservation(200, {"forwarded": True}, "corr-gateway"),
        "/gateway/deny": HttpObservation(403, {"code": "zeroth.policy_denied"}, "corr-gateway"),
        "/gateway/upstream-failure": HttpObservation(
            502, {"code": "zeroth.upstream_unavailable"}, "corr-gateway"
        ),
        "/health/ready": HttpObservation(
            200,
            {
                "schema_revision": {
                    "applied": "029",
                    "head": "029",
                    "state": "current",
                }
            },
            "corr-migrations",
        ),
        "/regulus/health": HttpObservation(
            200,
            {
                "schema_revision": {
                    "applied": "20260812_07",
                    "head": "20260812_07",
                    "state": "current",
                }
            },
            "corr-econ-migrations",
        ),
        "/compatibility": HttpObservation(
            200, {"checks": {"agent_server": {"status": "supported"}}}, "corr-compat"
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
    # The gate reads observed_compatibility.status, so the Agent Server verdict has to
    # survive out of whatever response carried it and into the report.
    assert report.observed_compatibility == {
        "status": "supported",
        "detected_agent_server": "0.11.1",
    }

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
            200, {"checks": {"agent_server": {"status": "unsupported"}}}, "corr-compat"
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
        ),
        # The scenario still has to satisfy the retrieval invariant; this probe is
        # about ownership, not about weakening what artifacts must prove.
        _step("/v1/artifacts/{artifact_id}"),
    ]
    transport = FakeTransport({"/create": HttpObservation(200, {"id": "production"}, "corr")})

    report = await AcceptanceRunner(
        config, AcceptanceContract.model_validate(contract), transport
    ).run()

    artifact = next(item for item in report.scenarios if item.name == "artifacts")
    assert artifact.status is ScenarioStatus.FAILED
    assert "body.name is missing" in artifact.detail


class _EventTransport:
    """Serve one fixed WebSocket frame list, so ordering rules can be probed."""

    def __init__(self, events: list[dict]) -> None:
        self.events = events

    async def request(self, role, method, path, *, json_body=None):
        return HttpObservation(200, {}, "corr")

    async def websocket_events(self, role, path, payload, *, max_events, frames=None):
        return self.events


def _stream_step(**overrides) -> dict[str, object]:
    step = {
        "protocol": "websocket",
        "role": "operator",
        "path": "/stream",
        "payload": {},
        "max_events": 2,
        "ordered_events": ["metadata", "values"],
    }
    step.update(overrides)
    return step


@pytest.mark.parametrize(
    ("events", "expected"),
    [
        # A stream that numbers nothing is the normal case for a proxied gateway.
        ([{"event": "metadata"}, {"event": "values"}], None),
        (
            [{"event": "metadata", "sequence": 1}, {"event": "values", "sequence": 2}],
            None,
        ),
        (
            [{"event": "metadata", "sequence": 2}, {"event": "values", "sequence": 1}],
            "not uniquely causally ordered",
        ),
        (
            [{"event": "metadata", "sequence": 1}, {"event": "values", "sequence": 1}],
            "not uniquely causally ordered",
        ),
        # Numbering some frames and not others hides a gap.
        ([{"event": "metadata", "sequence": 1}, {"event": "values"}], "only some"),
        ([{"event": "values"}, {"event": "metadata"}], "expected ordered events"),
    ],
)
@pytest.mark.asyncio
async def test_stream_ordering_holds_numbered_streams_to_their_numbers(
    tmp_path: Path, events: list[dict], expected: str | None
) -> None:
    config = _config(tmp_path)
    contract = _contract()
    contract["scenarios"]["gateway_websocket"] = {"steps": [_stream_step()]}
    runner = AcceptanceRunner(
        config, AcceptanceContract.model_validate(contract), _EventTransport(events)
    )
    result = await runner._scenario(
        "gateway_websocket", [AcceptanceStep.model_validate(_stream_step())]
    )

    if expected is None:
        assert result.status is ScenarioStatus.PASSED, result.detail
    else:
        assert result.status is ScenarioStatus.FAILED
        assert expected in result.detail


@pytest.mark.asyncio
async def test_correlation_must_be_propagated_not_merely_present(tmp_path: Path) -> None:
    """A relabelling target is not a correlated one."""
    config = _config(tmp_path)

    class Relabelling:
        async def request(self, role, method, path, *, json_body=None):
            return HttpObservation(200, {}, "an-id-of-its-own", sent_correlation_id="ours-1")

        async def websocket_events(self, *args, **kwargs):
            return []

    runner = AcceptanceRunner(config, AcceptanceContract.model_validate(_contract()), Relabelling())
    step = AcceptanceStep.model_validate(
        _step("/gateway/allow", method="POST", require_correlation=True)
    )
    result = await runner._scenario("gateway_http", [step])

    assert result.status is ScenarioStatus.FAILED
    assert "not propagated" in result.detail


@pytest.mark.parametrize(
    ("records", "expected_count", "should_pass"),
    [
        ([{"tool_calls": [{"name": "send"}]}], 1, True),
        ([{"tool_calls": []}, {"tool_calls": [{"name": "send"}]}], 1, True),
        # Two entries each holding one call is two executions, not two entries.
        ([{"tool_calls": [{"name": "send"}]}] * 2, 1, False),
        ([{"tool_calls": [{"name": "other"}]}], 1, False),
        # A record without the nested field contributes nothing rather than erroring.
        ([{"status": "completed"}], 0, True),
    ],
)
@pytest.mark.asyncio
async def test_counting_descends_into_a_nested_collection(
    tmp_path: Path, records: list[dict], expected_count: int, should_pass: bool
) -> None:
    """Tool executions live inside audit records, so the count has to reach them."""
    config = _config(tmp_path)

    class Fixed:
        async def request(self, role, method, path, *, json_body=None):
            return HttpObservation(200, {"entries": records}, "corr")

        async def websocket_events(self, *args, **kwargs):
            return []

    runner = AcceptanceRunner(config, AcceptanceContract.model_validate(_contract()), Fixed())
    step = AcceptanceStep.model_validate(
        _step(
            "/timeline",
            count_path="entries",
            count_flatten="tool_calls",
            count_where={"name": "send"},
            expected_count=expected_count,
        )
    )
    result = await runner._scenario("audit", [step])

    if should_pass:
        assert result.status is ScenarioStatus.PASSED, result.detail
    else:
        assert result.status is ScenarioStatus.FAILED
        assert "entries[].tool_calls" in result.detail
