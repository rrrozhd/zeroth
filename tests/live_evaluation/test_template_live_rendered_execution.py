from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from release.live_evaluation.template_live_rendered_execution import (
    ARM_ENVIRONMENT_VARIABLE,
    CRITERION_ID,
    LiveTemplateConfig,
    LiveTemplateFixture,
    LiveTemplateHarness,
    LiveTemplateObservation,
    RestartableService,
    collect_live_template_observation,
    main,
    seal_live_template_evidence,
    validate_live_template_observation,
)


class _Response:
    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self) -> object:
        return self._payload


class _RestartableService:
    def __init__(self) -> None:
        self.identity = "instance-before"
        self.restarts = 0

    def instance_id(self) -> str:
        return self.identity

    def restart(self) -> None:
        self.restarts += 1
        self.identity = "instance-after"


def _live_service_request(
    calls: list[tuple[str, str, dict[str, Any] | None]],
    *,
    rendered_prompt: str = (
        "Return only a JSON object with answer LIVE-TEMPLATE-ALPHA for probe alpha."
    ),
    provider_request_id: str | None = "req-live-template-1",
    cost_measurement: str = "measured",
) -> Any:
    terminal = {
        "run_id": "run-live-template-1",
        "status": "succeeded",
        "deployment_ref": "live-render-template-20260826-v1",
        "graph_version_ref": "workflow-template-live@1",
        "thread_id": "thread-live-template-1",
        "tenant_id": "evaluation-studio-v1",
        "workspace_id": None,
        "campaign_id": "evaluation-studio-v1",
        "terminal_output": {"answer": "LIVE-TEMPLATE-ALPHA"},
    }
    audit_metadata: dict[str, object] = {
        "rendered_prompt_sha256": hashlib.sha256(rendered_prompt.encode()).hexdigest(),
        "template_name_sha256": hashlib.sha256(b"live-render-template-20260826").hexdigest(),
        "template_version": 1,
    }
    audit = {
        "audit_id": "audit-live-template-1",
        "run_id": "run-live-template-1",
        "node_id": "rendered-agent",
        "graph_version_ref": "workflow-template-live@1",
        "deployment_ref": "live-render-template-20260826-v1",
        "tenant_id": "evaluation-studio-v1",
        "workspace_id": None,
        "status": "completed",
        "execution_metadata": audit_metadata,
        "token_usage": {
            "input_tokens": 31,
            "output_tokens": 7,
            "total_tokens": 38,
            "model_name": "openai/gpt-4o-mini",
        },
        "cost_usd": 0.00019 if cost_measurement == "measured" else None,
        "estimated_cost_usd": 0.00019 if cost_measurement == "estimated" else None,
        "cost_measurement": cost_measurement,
        "cost_event_id": "cost-live-template-1",
        "record_digest": "a" * 64,
        "record_signature": "hmac-sha256:signed",
        "signing_key_id": "local-campaign",
        "signing_algorithm": "hmac-sha256",
    }

    def request(method: str, path: str, payload: dict[str, Any] | None = None) -> _Response:
        calls.append((method, path, payload))
        if (method, path) == ("GET", "/v1/econ/configuration"):
            return _Response(
                200,
                {
                    "tenant_id": "evaluation-studio-v1",
                    "deployment_ref": "live-render-template-20260826-v1",
                    "per_run_cap_usd": 0.25,
                    "failure_mode": "fail_closed",
                    "source": "service_runtime",
                },
            )
        if (method, path) == (
            "GET",
            "/v1/tenants/evaluation-studio-v1/cost",
        ):
            return _Response(
                200,
                {
                    "tenant_id": "evaluation-studio-v1",
                    "total_cost_usd": 1.20019,
                    "actual_spend_usd": 1.20019,
                    "budget_consumed_usd": 1.20019,
                    "budget_cap_usd": 10.0,
                },
            )
        if (method, path) == ("GET", "/health"):
            return _Response(
                200,
                {
                    "status": "ok",
                    "deployment_ref": "live-render-template-20260826-v1",
                    "deployment_version": 1,
                    "graph_version_ref": "workflow-template-live@1",
                    "campaign_id": "evaluation-studio-v1",
                },
            )
        if (method, path) == (
            "GET",
            "/v1/templates/live-render-template-20260826?version=1",
        ):
            return _Response(
                200,
                {
                    "name": "live-render-template-20260826",
                    "version": 1,
                    "template_str": (
                        "Return only a JSON object with answer LIVE-TEMPLATE-ALPHA "
                        "for probe {{ input.probe }}."
                    ),
                    "variables": [],
                    "description": "fixture",
                },
            )
        if (method, path) == ("POST", "/v1/runs"):
            return _Response(202, {**terminal, "status": "queued", "terminal_output": None})
        if (method, path) == ("GET", "/v1/runs/run-live-template-1"):
            return _Response(200, terminal)
        if (method, path) == ("GET", "/v1/runs/run-live-template-1/evidence"):
            return _Response(
                200,
                {
                    "run": terminal,
                    "audits": [audit],
                    "approvals": [],
                    "summary": {
                        "audit_count": 1,
                        "priced_call_count": 1,
                        "cost_event_count": 1,
                        "total_cost_usd": 0.00019,
                        "cost_identity_state": "correlated",
                        "reconciliation_state": "reconciled",
                    },
                    "policy_events": [],
                },
            )
        if (method, path) == ("POST", "/v1/runs/run-live-template-1/verify-chain"):
            return _Response(
                200,
                {
                    "scope": "run:run-live-template-1",
                    "verified": True,
                    "record_count": 1,
                    "signature_verified": True,
                    "signing_key_id": "local-campaign",
                    "unsigned_record_count": 0,
                },
            )
        raise AssertionError((method, path, payload))

    return request


def _cost_identity(*, provider_request_id: str | None = "req-live-template-1") -> Any:
    def read(cost_event_id: str, run_id: str) -> dict[str, object]:
        row: dict[str, object] = {
            "cost_event_id": cost_event_id,
            "run_id": run_id,
            "provider": "openai",
        }
        if provider_request_id is not None:
            row["provider_request_id"] = provider_request_id
        return row

    return read


def _config() -> LiveTemplateConfig:
    return LiveTemplateConfig(
        fixture_id="template-render-20260826",
        tenant_id="evaluation-studio-v1",
        template_name="live-render-template-20260826",
        deployment_ref="live-render-template-20260826-v1",
    )


def _observation() -> LiveTemplateObservation:
    rendered = "Return only a JSON object with answer LIVE-TEMPLATE-ALPHA for probe alpha."
    return LiveTemplateObservation(
        criterion_id=CRITERION_ID,
        template_name="live-render-template-20260826",
        template_version=1,
        graph_version_ref="workflow-template-live@1",
        deployment_ref="live-render-template-20260826-v1",
        deployment_version=1,
        rendered_prompt_sha256=hashlib.sha256(rendered.encode()).hexdigest(),
        provider="openai",
        provider_request_id="req-live-template-1",
        model="openai/gpt-4o-mini",
        input_tokens=31,
        output_tokens=7,
        total_tokens=38,
        cost_usd="0.00019",
        campaign_cost_after_usd="1.20019",
        run_id="run-live-template-1",
        audit_id="audit-live-template-1",
        cost_event_id="cost-live-template-1",
        terminal_output_sha256=hashlib.sha256(b'{"answer":"LIVE-TEMPLATE-ALPHA"}').hexdigest(),
        refresh_run_id="run-live-template-1",
        restart_run_id="run-live-template-1",
        refresh_template_version=1,
        restart_template_version=1,
        refresh_graph_version_ref="workflow-template-live@1",
        restart_graph_version_ref="workflow-template-live@1",
        refresh_deployment_version=1,
        restart_deployment_version=1,
        pre_restart_instance_id="instance-before",
        post_restart_instance_id="instance-after",
        audit_chain_signed=True,
        credential_reference="llm.openai",
        credential_value_retained=False,
    )


def test_readiness_fails_closed_without_arm_and_opaque_secret_reference() -> None:
    calls: list[str] = []
    harness = LiveTemplateHarness(
        config=_config(),
        request=lambda *args: calls.append("request"),
        secret_reference_available=lambda ref, tenant: (
            calls.append(f"secret:{ref}:{tenant}") or False
        ),
        environment={},
    )

    readiness = harness.readiness(armed=False)

    assert readiness.ready is False
    assert readiness.provider_calls_performed == 0
    assert set(readiness.blockers) == {
        "explicit --arm-live-provider flag is absent",
        f"{ARM_ENVIRONMENT_VARIABLE} does not equal {CRITERION_ID}",
        "external logical credential reference llm.openai is unavailable",
    }
    assert calls == ["secret:llm.openai:evaluation-studio-v1"]


def test_execute_requires_two_interlocks_and_never_receives_secret_value() -> None:
    executed: list[dict[str, object]] = []
    harness = LiveTemplateHarness(
        config=_config(),
        request=lambda *_: pytest.fail("request should be supplied by execute_live callback"),
        secret_reference_available=lambda ref, tenant: ref == "llm.openai",
        environment={ARM_ENVIRONMENT_VARIABLE: CRITERION_ID},
    )

    with pytest.raises(RuntimeError, match="not armed"):
        harness.execute(armed=False, execute_live=lambda body: executed.append(body))
    assert executed == []

    observation = harness.execute(
        armed=True,
        execute_live=lambda body: executed.append(body) or _observation().to_dict(),
    )

    assert observation.provider_request_id == "req-live-template-1"
    assert executed == [
        {
            "input_payload": {"probe": "alpha", "expected": "LIVE-TEMPLATE-ALPHA"},
            "campaign_id": "evaluation-studio-v1",
            "campaign_strict": True,
            "max_cost_usd": "0.25",
        }
    ]
    assert "credential" not in json.dumps(executed).lower()


def test_service_collector_submits_real_run_and_derives_observation_from_readbacks() -> None:
    calls: list[tuple[str, str, dict[str, Any] | None]] = []
    service: RestartableService = _RestartableService()

    observation = collect_live_template_observation(
        config=_config(),
        fixture=LiveTemplateFixture(
            fixture_id="template-render-20260826",
            template_name="live-render-template-20260826",
            template_version=1,
            workflow_id="workflow-template-live",
            graph_version_ref="workflow-template-live@1",
            deployment_ref="live-render-template-20260826-v1",
            deployment_version=1,
        ),
        request=_live_service_request(calls),
        cost_identity=_cost_identity(),
        lifecycle=service,
        wait=lambda _seconds: pytest.fail("terminal fixture must not sleep"),
    )

    assert observation == _observation()
    submission = next(payload for method, path, payload in calls if path == "/v1/runs")
    assert submission == {
        "input_payload": {"probe": "alpha", "expected": "LIVE-TEMPLATE-ALPHA"},
        "campaign_id": "evaluation-studio-v1",
        "campaign_strict": True,
    }
    assert "max_cost_usd" not in submission
    assert service.restarts == 1
    assert sum(path == "/v1/runs/run-live-template-1" for _, path, _ in calls) == 3
    assert (
        sum(path == "/v1/templates/live-render-template-20260826?version=1" for _, path, _ in calls)
        == 3
    )
    assert not any(
        any(word in json.dumps(payload or {}).lower() for word in ("credential", "api_key"))
        for _, _, payload in calls
    )


def test_harness_service_execution_preserves_both_paid_call_interlocks() -> None:
    calls: list[tuple[str, str, dict[str, Any] | None]] = []
    harness = LiveTemplateHarness(
        config=_config(),
        request=_live_service_request(calls),
        cost_identity=_cost_identity(),
        secret_reference_available=lambda ref, tenant: (
            ref == "llm.openai" and tenant == "evaluation-studio-v1"
        ),
        environment={ARM_ENVIRONMENT_VARIABLE: CRITERION_ID},
    )
    fixture = LiveTemplateFixture(
        fixture_id="template-render-20260826",
        template_name="live-render-template-20260826",
        template_version=1,
        workflow_id="workflow-template-live",
        graph_version_ref="workflow-template-live@1",
        deployment_ref="live-render-template-20260826-v1",
        deployment_version=1,
    )

    with pytest.raises(RuntimeError, match="not armed"):
        harness.execute_service(
            armed=False,
            fixture=fixture,
            lifecycle=_RestartableService(),
        )
    assert calls == []

    observed = harness.execute_service(
        armed=True,
        fixture=fixture,
        lifecycle=_RestartableService(),
        wait=lambda _seconds: pytest.fail("terminal fixture must not sleep"),
    )
    assert observed.run_id == "run-live-template-1"


def test_service_collector_fails_before_submission_when_runtime_caps_are_not_exact() -> None:
    calls: list[tuple[str, str, dict[str, Any] | None]] = []
    base = _live_service_request(calls)

    def request(method: str, path: str, payload: dict[str, Any] | None = None) -> _Response:
        if (method, path) == ("GET", "/v1/econ/configuration"):
            calls.append((method, path, payload))
            return _Response(
                200,
                {
                    "tenant_id": "evaluation-studio-v1",
                    "deployment_ref": "live-render-template-20260826-v1",
                    "per_run_cap_usd": 2.0,
                    "failure_mode": "fail_open",
                    "source": "service_runtime",
                },
            )
        return base(method, path, payload)

    with pytest.raises(RuntimeError, match="strict runtime economics"):
        collect_live_template_observation(
            config=_config(),
            fixture=LiveTemplateFixture(
                fixture_id="template-render-20260826",
                template_name="live-render-template-20260826",
                template_version=1,
                workflow_id="workflow-template-live",
                graph_version_ref="workflow-template-live@1",
                deployment_ref="live-render-template-20260826-v1",
                deployment_version=1,
            ),
            request=request,
            cost_identity=_cost_identity(),
            lifecycle=_RestartableService(),
        )

    assert not any(path == "/v1/runs" for _, path, _ in calls)


@pytest.mark.parametrize(
    ("rendered_prompt", "provider_request_id", "message"),
    [
        ("drifted prompt", "req-live-template-1", "rendered prompt"),
        (
            "Return only a JSON object with answer LIVE-TEMPLATE-ALPHA for probe alpha.",
            None,
            "provider request",
        ),
    ],
)
def test_service_collector_fails_closed_on_unproven_runtime_identity(
    rendered_prompt: str,
    provider_request_id: str | None,
    message: str,
) -> None:
    calls: list[tuple[str, str, dict[str, Any] | None]] = []
    with pytest.raises(RuntimeError, match=message):
        collect_live_template_observation(
            config=_config(),
            fixture=LiveTemplateFixture(
                fixture_id="template-render-20260826",
                template_name="live-render-template-20260826",
                template_version=1,
                workflow_id="workflow-template-live",
                graph_version_ref="workflow-template-live@1",
                deployment_ref="live-render-template-20260826-v1",
                deployment_version=1,
            ),
            request=_live_service_request(
                calls,
                rendered_prompt=rendered_prompt,
            ),
            cost_identity=_cost_identity(provider_request_id=provider_request_id),
            lifecycle=_RestartableService(),
        )


def test_service_collector_requires_distinct_supervisor_restart_identity() -> None:
    calls: list[tuple[str, str, dict[str, Any] | None]] = []

    class SameInstance:
        def instance_id(self) -> str:
            return "same-instance"

        def restart(self) -> None:
            return None

    with pytest.raises(RuntimeError, match="distinct post-restart"):
        collect_live_template_observation(
            config=_config(),
            fixture=LiveTemplateFixture(
                fixture_id="template-render-20260826",
                template_name="live-render-template-20260826",
                template_version=1,
                workflow_id="workflow-template-live",
                graph_version_ref="workflow-template-live@1",
                deployment_ref="live-render-template-20260826-v1",
                deployment_version=1,
            ),
            request=_live_service_request(calls),
            cost_identity=_cost_identity(),
            lifecycle=SameInstance(),
        )


def test_service_collector_accepts_explicitly_estimated_provider_cost() -> None:
    calls: list[tuple[str, str, dict[str, Any] | None]] = []
    observed = collect_live_template_observation(
        config=_config(),
        fixture=LiveTemplateFixture(
            fixture_id="template-render-20260826",
            template_name="live-render-template-20260826",
            template_version=1,
            workflow_id="workflow-template-live",
            graph_version_ref="workflow-template-live@1",
            deployment_ref="live-render-template-20260826-v1",
            deployment_version=1,
        ),
        request=_live_service_request(calls, cost_measurement="estimated"),
        cost_identity=_cost_identity(),
        lifecycle=_RestartableService(),
    )

    assert observed.cost_measurement == "estimated"
    assert observed.cost_usd == "0.00019"


def test_provision_pins_immutable_template_version_in_published_agent() -> None:
    calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def request(method: str, path: str, payload: dict[str, Any] | None = None) -> _Response:
        calls.append((method, path, payload))
        if method == "POST" and path == "/v1/templates":
            return _Response(201, {"name": payload["name"], "version": 1})
        if method == "POST" and path == "/api/studio/v1/contracts":
            return _Response(201, {"name": payload["name"], "version": 1})
        if method == "POST" and path == "/api/studio/v1/workflows":
            return _Response(201, {"id": "workflow-template-live"})
        if method == "PUT" and path == "/api/studio/v1/workflows/workflow-template-live":
            return _Response(200, {"id": "workflow-template-live", "status": "draft"})
        if method == "POST" and path.endswith("/preflight"):
            return _Response(200, {"ready": True, "issues": []})
        if method == "POST" and path.endswith("/publish"):
            return _Response(200, {"status": "published", "version": 1})
        if method == "POST" and path == "/v1/deployments":
            return _Response(
                201,
                {
                    "deployment_ref": payload["deployment_ref"],
                    "version": 1,
                    "graph_version_ref": "workflow-template-live@1",
                },
            )
        raise AssertionError((method, path, payload))

    harness = LiveTemplateHarness(
        config=_config(),
        request=request,
        secret_reference_available=lambda *_: False,
        environment={},
    )
    fixture = harness.provision()

    assert fixture.template_version == 1
    assert fixture.graph_version_ref == "workflow-template-live@1"
    save = next(payload for method, path, payload in calls if method == "PUT")
    assert [node["type"] for node in save["nodes"]] == ["entrypoint", "agent"]
    assert save["entry_step"] == "probe-entry"
    assert save["edges"] == [
        {
            "id": "probe-entry-rendered-agent",
            "source": "probe-entry",
            "target": "rendered-agent",
            "kind": "data",
        }
    ]
    agent = save["nodes"][1]
    assert agent["type"] == "agent"
    assert agent["data"]["config"]["template_ref"] == {
        "name": "live-render-template-20260826",
        "version": 1,
    }
    assert agent["data"]["config"]["model_provider"] == "openai/gpt-4o-mini"
    assert "{{ input.probe }}" not in json.dumps(save)


def test_provider_free_provisioning_passes_real_studio_publish_and_deploy_apis() -> None:
    from tests.test_studio_publish_deploy import _make_env

    app, _ = _make_env()
    with TestClient(app) as client:

        def request(method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
            if method == "POST" and path == "/v1/templates":
                return _Response(201, {"name": payload["name"], "version": 1})
            route = "/deployments" if path == "/v1/deployments" else path
            return client.request(method, route, json=payload)

        fixture = LiveTemplateHarness(
            config=_config(),
            request=request,
            secret_reference_available=lambda *_: pytest.fail(
                "provisioning must not inspect credential availability"
            ),
            environment={},
        ).provision()

    assert fixture.graph_version_ref == f"{fixture.workflow_id}@1"
    assert fixture.deployment_version == 1
    assert fixture.provider_calls_performed == 0


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda row: row.update(template_version=2), "template identity"),
        (lambda row: row.update(rendered_prompt_sha256="0" * 64), "render digest"),
        (lambda row: row.update(provider_request_id=""), "provider request"),
        (lambda row: row.update(cost_usd="0.25001"), "run ceiling"),
        (lambda row: row.update(campaign_cost_after_usd="10.00001"), "campaign ceiling"),
        (lambda row: row.update(refresh_run_id="other"), "refresh persistence"),
        (lambda row: row.update(restart_template_version=2), "restart persistence"),
        (lambda row: row.update(post_restart_instance_id="instance-before"), "restart"),
        (lambda row: row.update(cost_usd="NaN"), "cost accounting"),
        (lambda row: row.update(total_tokens=39), "usage accounting"),
        (lambda row: row.update(audit_id="run-live-template-1"), "distinct"),
        (lambda row: row.update(credential_value_retained=True), "credential retention"),
    ],
)
def test_observation_validation_fails_closed_on_evidence_drift(change, message: str) -> None:
    row = _observation().to_dict()
    change(row)
    with pytest.raises((ValueError, RuntimeError), match=message):
        validate_live_template_observation(row, expected=_config())


def test_sealer_requires_screenshots_and_writes_checksummed_exact_acceptance(
    tmp_path: Path,
) -> None:
    screenshots = tmp_path / "captures"
    screenshots.mkdir()
    png = b"\x89PNG\r\n\x1a\n" + b"safe-live-template-evidence"
    for name in ("rendered-run.png", "refresh-restored.png", "restart-restored.png"):
        (screenshots / name).write_bytes(png)
    root = tmp_path / "sealed"

    seal_live_template_evidence(
        root=root,
        observation=_observation(),
        config=_config(),
        screenshots=tuple(
            screenshots / name
            for name in (
                "rendered-run.png",
                "refresh-restored.png",
                "restart-restored.png",
            )
        ),
    )

    acceptance = json.loads((root / "acceptance.json").read_text())
    assert acceptance["criteria"] == [
        {
            "criterion_id": CRITERION_ID,
            "evidence": [
                "runtime/live-template-observation.json",
                "screenshots/00-rendered-run.png",
                "screenshots/01-refresh-restored.png",
                "screenshots/02-restart-restored.png",
                "events.ndjson#1",
            ],
            "note": None,
            "status": "pass",
        }
    ]
    checksums = (root / "SHA256SUMS").read_text()
    assert "runtime/live-template-observation.json" in checksums
    assert "acceptance.json" in checksums
    assert "SHA256SUMS" not in checksums
    serialized = b"".join(path.read_bytes() for path in root.rglob("*") if path.is_file())
    assert b"sk-" not in serialized
    assert b"Authorization:" not in serialized


def test_sealer_rejects_missing_screenshot_without_partial_seal(tmp_path: Path) -> None:
    missing = tmp_path / "missing.png"
    with pytest.raises(FileNotFoundError):
        seal_live_template_evidence(
            root=tmp_path / "sealed",
            observation=_observation(),
            config=_config(),
            screenshots=(missing, missing, missing),
        )
    assert not (tmp_path / "sealed" / "SHA256SUMS").exists()


def test_readiness_cli_is_non_executing_and_fails_closed_without_attestation(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ARM_ENVIRONMENT_VARIABLE, raising=False)
    result = main(
        [
            "readiness",
            "--fixture-id",
            _config().fixture_id,
            "--tenant-id",
            _config().tenant_id,
            "--template-name",
            _config().template_name,
            "--deployment-ref",
            _config().deployment_ref,
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 2
    assert payload["ready"] is False
    assert payload["provider_calls_performed"] == 0
    assert "unavailable" in " ".join(payload["blockers"])


def test_validate_cli_checks_future_sealer_inputs_without_writing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    observation = tmp_path / "observation.json"
    observation.write_text(json.dumps(_observation().to_dict()))
    captures = []
    for index in range(3):
        capture = tmp_path / f"capture-{index}.png"
        capture.write_bytes(b"\x89PNG\r\n\x1a\n" + b"safe")
        captures.append(capture)

    result = main(
        [
            "validate",
            "--fixture-id",
            _config().fixture_id,
            "--tenant-id",
            _config().tenant_id,
            "--template-name",
            _config().template_name,
            "--deployment-ref",
            _config().deployment_ref,
            "--observation",
            str(observation),
            "--screenshots",
            *(str(path) for path in captures),
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload == {
        "criterion_id": CRITERION_ID,
        "provider_calls_performed": 0,
        "seal_inputs_valid": True,
    }
    assert {path.name for path in tmp_path.iterdir()} == {
        observation.name,
        *(path.name for path in captures),
    }
