from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from release.live_evaluation.batch_provider_economics import ReadinessAttestation
from release.live_evaluation.live_provider_gate import ProviderFreeWiring
from release.live_evaluation.template_live_execution_driver import (
    ARM_PHRASE,
    DockerBackendLifecycle,
    DriverBlockedError,
    execute_approved_template,
    main,
    write_observation_exclusive,
)
from release.live_evaluation.template_live_rendered_execution import (
    ARM_ENVIRONMENT_VARIABLE,
    CRITERION_ID,
    LiveTemplateConfig,
    LiveTemplateFixture,
    LiveTemplateObservation,
)
from release.live_evaluation.rightsizing_service_adapter import ExperimentRequest


def _observation() -> LiveTemplateObservation:
    return LiveTemplateObservation(
        criterion_id=CRITERION_ID,
        template_name="live-template-render-20260826",
        template_version=1,
        graph_version_ref="workflow-template-live@1",
        deployment_ref="live-template-render-20260826-v1",
        deployment_version=1,
        rendered_prompt_sha256="72aac78577c3b31311d2d7716aeaad2369508211f464f242942767312d55597b",
        provider="openai",
        provider_request_id="provider-request-1",
        model="openai/gpt-4o-mini",
        input_tokens=10,
        output_tokens=5,
        total_tokens=15,
        cost_usd="0.00001",
        campaign_cost_after_usd="0.10001",
        run_id="run-1",
        audit_id="audit-1",
        cost_event_id="cost-1",
        terminal_output_sha256="2b81e02ad8d0e28fbd2a6de907cf9b804349e7e7bdfc74a2f096ecf3cd4b06f8",
        refresh_run_id="run-1",
        restart_run_id="run-1",
        refresh_template_version=1,
        restart_template_version=1,
        refresh_graph_version_ref="workflow-template-live@1",
        restart_graph_version_ref="workflow-template-live@1",
        refresh_deployment_version=1,
        restart_deployment_version=1,
        pre_restart_instance_id="started-before",
        post_restart_instance_id="started-after",
        audit_chain_signed=True,
        credential_reference="llm.openai",
        credential_value_retained=False,
    )


def _wiring(tmp_path: Path) -> ProviderFreeWiring:
    files = []
    for name in ("service.sqlite3", "econ.sqlite3", "actions.sqlite3", "window.json"):
        path = tmp_path / name
        path.write_text("fixture", encoding="utf-8")
        files.append(path)
    config = LiveTemplateConfig(
        fixture_id="live-template-render-20260826",
        tenant_id="evaluation-studio-v1",
        template_name="live-template-render-20260826",
        deployment_ref="live-template-render-20260826-v1",
    )
    fixture = LiveTemplateFixture(
        fixture_id=config.fixture_id,
        template_name=config.template_name,
        template_version=1,
        workflow_id="workflow-template-live",
        graph_version_ref="workflow-template-live@1",
        deployment_ref=config.deployment_ref,
        deployment_version=1,
    )
    return ProviderFreeWiring(
        service_base_url="http://127.0.0.1:8122",
        service_database=files[0],
        econ_database=files[1],
        action_sink_database=files[2],
        provider_window=files[3],
        batch_items=tuple({"index": index, "query": f"item-{index}"} for index in range(8)),
        template_config=config,
        template_fixture=fixture,
        rightsizing_request=ExperimentRequest(
            node_id="research-agent",
            incumbent="openai/gpt-4o-mini",
            instruction="Answer only from supplied context.",
            needs_tools=False,
            needs_vision=False,
            judge_model="openai/gpt-4o-mini",
            max_candidates=1,
            max_cases=1,
            min_cases=1,
            tolerance_pct=5.0,
            mode="equivalence",
        ),
        rightsizing_cases_sha256="a" * 64,
    )


def _attestation() -> ReadinessAttestation:
    return ReadinessAttestation.from_mapping(
        {
            "campaign_id": "evaluation-studio-v1",
            "tenant_id": "evaluation-studio-v1",
            "logical_secret_ref": "llm.openai",
            "installed": True,
            "provider_probe_reconciled": True,
            "provider_request_id": "readiness-provider",
            "operation_id": "readiness-operation",
            "run_id": "readiness-run",
            "audit_event_id": "readiness-audit",
            "cost_event_id": "readiness-cost",
            "measured_cost_usd": "0.000001",
            "campaign_spend_before_usd": "0.000001",
            "audit_signed": True,
        }
    )


def test_execute_requires_exact_interlocks_before_constructing_runtime(tmp_path: Path) -> None:
    constructed: list[str] = []

    with pytest.raises(DriverBlockedError, match="live-template-not-armed"):
        execute_approved_template(
            wiring=_wiring(tmp_path),
            readiness=_attestation(),
            arm_phrase="wrong",
            environment={ARM_ENVIRONMENT_VARIABLE: CRITERION_ID},
            request_factory=lambda *_args, **_kwargs: constructed.append("request"),
            cost_reader_factory=lambda *_args, **_kwargs: constructed.append("cost"),
            lifecycle_factory=lambda: constructed.append("lifecycle"),
        )

    assert constructed == []


def test_execute_uses_approved_template_once_and_never_passes_provider_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wiring = _wiring(tmp_path)
    service_key = tmp_path.parent / "private-service-key"
    calls: list[tuple[str, object]] = []

    class Request:
        def __call__(self, method: str, path: str, payload: object) -> object:
            calls.append(("http", (method, path, payload)))
            return object()

        def close(self) -> None:
            calls.append(("close", None))

    class Harness:
        def __init__(self, **kwargs: object) -> None:
            calls.append(("harness", kwargs))
            self.request = kwargs["request"]

        def execute_service(self, **kwargs: object) -> LiveTemplateObservation:
            calls.append(("execute", kwargs))
            self.request("POST", "/v1/runs", {"campaign_strict": True})
            return _observation()

    monkeypatch.setattr(
        "release.live_evaluation.template_live_execution_driver.LiveTemplateHarness", Harness
    )

    observed = execute_approved_template(
        wiring=wiring,
        readiness=_attestation(),
        arm_phrase=ARM_PHRASE,
        environment={ARM_ENVIRONMENT_VARIABLE: CRITERION_ID},
        service_api_key_file=service_key,
        request_factory=lambda **kwargs: calls.append(("request", kwargs)) or Request(),
        cost_reader_factory=lambda **kwargs: calls.append(("cost", kwargs)) or object(),
        lifecycle_factory=lambda: calls.append(("lifecycle", None)) or object(),
    )

    assert observed == _observation()
    assert [name for name, _ in calls].count("execute") == 1
    request_kwargs = next(value for name, value in calls if name == "request")
    assert request_kwargs == {
        "base_url": "http://127.0.0.1:8122",
        "service_api_key_file": service_key,
    }
    harness_kwargs = next(value for name, value in calls if name == "harness")
    assert harness_kwargs["config"] == wiring.template_config
    assert harness_kwargs["environment"] == {ARM_ENVIRONMENT_VARIABLE: CRITERION_ID}
    assert harness_kwargs["secret_reference_available"]("llm.openai", "evaluation-studio-v1")
    serialized = json.dumps(calls, default=str).lower()
    assert "provider-api-key" not in serialized
    assert "provider_api_key" not in serialized
    assert "sk-" not in serialized


def test_readiness_attestation_must_be_reconciled_and_match_wiring(tmp_path: Path) -> None:
    raw = asdict(_attestation())
    raw["audit_signed"] = False
    readiness = ReadinessAttestation.from_mapping(raw)

    with pytest.raises(DriverBlockedError, match="provider-readiness-invalid"):
        execute_approved_template(
            wiring=_wiring(tmp_path),
            readiness=readiness,
            arm_phrase=ARM_PHRASE,
            environment={ARM_ENVIRONMENT_VARIABLE: CRITERION_ID},
        )


def test_readiness_accepts_truthful_unavailable_upstream_request_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = asdict(_attestation())
    raw["provider_request_id"] = None
    readiness = ReadinessAttestation.from_mapping(raw)

    class Request:
        def __call__(self, method: str, path: str, payload: object) -> object:
            return object()

        def close(self) -> None:
            return None

    class Harness:
        def __init__(self, **kwargs: object) -> None:
            self.request = kwargs["request"]

        def execute_service(self, **_kwargs: object) -> LiveTemplateObservation:
            self.request("POST", "/v1/runs", {"campaign_strict": True})
            return _observation()

    monkeypatch.setattr(
        "release.live_evaluation.template_live_execution_driver.LiveTemplateHarness", Harness
    )
    observed = execute_approved_template(
        wiring=_wiring(tmp_path),
        readiness=readiness,
        arm_phrase=ARM_PHRASE,
        environment={ARM_ENVIRONMENT_VARIABLE: CRITERION_ID},
        service_api_key_file=tmp_path.parent / "private-service-key",
        request_factory=lambda **_kwargs: Request(),
        cost_reader_factory=lambda **_kwargs: object(),
        lifecycle_factory=lambda: object(),
    )

    assert observed == _observation()


def test_lifecycle_uses_fixed_backend_restart_and_process_start_identity(tmp_path: Path) -> None:
    compose = tmp_path / "compose.dev.yml"
    compose.write_text("services: {}\n", encoding="utf-8")
    calls: list[tuple[str, ...]] = []
    starts = iter(("2026-08-26T10:00:00Z\n", "2026-08-26T10:01:00Z\n"))

    def command(argv: tuple[str, ...]) -> tuple[int, str, str]:
        calls.append(argv)
        if argv[-2:] == ("{{.State.Health.Status}}", "zeroth-dev-backend-1"):
            return 0, "healthy\n", ""
        if argv[:2] == ("docker", "inspect"):
            return 0, next(starts), ""
        if argv[-2:] == ("restart", "backend"):
            return 0, "", ""
        raise AssertionError(argv)

    lifecycle = DockerBackendLifecycle(compose_file=compose, command_runner=command)

    assert lifecycle.instance_id() == "2026-08-26T10:00:00Z"
    lifecycle.restart()
    assert lifecycle.instance_id() == "2026-08-26T10:01:00Z"
    assert calls == [
        ("docker", "inspect", "--format", "{{.State.StartedAt}}", "zeroth-dev-backend-1"),
        ("docker", "compose", "-f", str(compose.resolve()), "restart", "backend"),
        (
            "docker",
            "inspect",
            "--format",
            "{{.State.Health.Status}}",
            "zeroth-dev-backend-1",
        ),
        ("docker", "inspect", "--format", "{{.State.StartedAt}}", "zeroth-dev-backend-1"),
    ]


def test_output_is_private_exclusive_and_contains_only_sanitized_observation(
    tmp_path: Path,
) -> None:
    output = tmp_path / "observation.json"
    write_observation_exclusive(output, _observation())

    assert json.loads(output.read_text(encoding="utf-8")) == _observation().to_dict()
    assert output.stat().st_mode & 0o077 == 0
    with pytest.raises(FileExistsError):
        write_observation_exclusive(output, _observation())


def test_cli_blocks_existing_output_before_constructing_live_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    wiring_file = tmp_path / "wiring.json"
    readiness_file = tmp_path / "readiness.json"
    service_key = tmp_path / "service-key"
    output = tmp_path / "observation.json"
    for path in (wiring_file, readiness_file, service_key, output):
        path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        "release.live_evaluation.template_live_execution_driver._parse_wiring",
        lambda _raw: _wiring(tmp_path),
    )
    monkeypatch.setattr(
        "release.live_evaluation.template_live_execution_driver._read_readiness",
        lambda _path: _attestation(),
    )
    monkeypatch.setattr(
        "release.live_evaluation.template_live_execution_driver.execute_approved_template",
        lambda **_kwargs: pytest.fail("existing output must block before a paid call"),
    )

    result = main(
        [
            "--wiring-config",
            str(wiring_file),
            "--readiness-attestation",
            str(readiness_file),
            "--service-api-key-file",
            str(service_key),
            "--output-json",
            str(output),
            "--arm",
            ARM_PHRASE,
        ]
    )

    assert result == 2
    assert json.loads(capsys.readouterr().out) == {
        "reason": "output-already-exists",
        "status": "blocked",
    }


def test_cli_rejects_provider_key_inputs_without_echoing_values(capsys: Any) -> None:
    sentinel = "sk-provider-secret-must-not-escape"
    with pytest.raises(SystemExit):
        main(["--provider-api-key", sentinel])
    captured = capsys.readouterr()
    assert sentinel not in captured.out + captured.err
