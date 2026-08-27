from __future__ import annotations

from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock

import pytest

from release.live_evaluation.action_runner import (
    EVALUATION_ACTION_MANIFEST_REF,
    EVALUATION_CONTROLLED_FAILURE_MANIFEST_REF,
    EvaluationActionRunner,
)
from release.live_evaluation.config import CampaignConfig
from release.live_evaluation.fault_control import FaultingProviderAdapter
from release.live_evaluation.receipt_restart_barrier import RestartBarrierAuditRepository
from release.live_evaluation.service import (
    EvaluationSyntheticCaptureClassifier,
    EvaluationStartupError,
    bootstrap_evaluation_action_service,
    build_parser,
)
from zeroth.platform.signing import EnvHmacSigner
from zeroth.governance.audit.capture_policy import CaptureDecision
from zeroth.governance.audit.models import NodeAuditRecord
from zeroth.runtime.orchestration.orchestrator import RuntimeOrchestrator
from zeroth.service.bootstrap.container import ServiceBootstrap
from zeroth.integrations.execution.sandbox import SandboxPolicyViolationError


def test_service_bootstrap_declares_evaluation_runtime_metadata_slots() -> None:
    """The production slotted container must accept evaluation metadata explicitly."""
    names = {item.name for item in fields(ServiceBootstrap)}

    assert {
        "evaluation_campaign",
        "evaluation_fault_state",
        "evaluation_receipt_restart_barriers",
    } <= names


def _settings(*, sla_enabled: bool = True):
    return SimpleNamespace(
        provenance=SimpleNamespace(mode="env"),
        approval_sla=SimpleNamespace(enabled=sla_enabled),
    )


def _campaign(tmp_path: Path) -> CampaignConfig:
    artifact_root = tmp_path / "campaign"
    return CampaignConfig.model_validate(
        {
            "schema_version": 1,
            "campaign_id": "evaluation-studio-v1",
            "tenant_id": "evaluation-studio-v1",
            "provider": "openai",
            "model": "openai/gpt-4o-mini",
            "embedding_model": "openai/text-embedding-3-small",
            "vector_backend": "chroma",
            "campaign_budget_usd": "10.00",
            "per_run_cap_usd": "0.25",
            "provider_secret_ref": "llm.openai",
            "artifact_root": artifact_root,
            "action_sink_root": artifact_root / "action-sink",
        }
    )


def _signed_bootstrap_factory(calls: list[dict]):
    signer = EnvHmacSigner(key_id="evaluation", keys={"evaluation": b"signing-key"})

    async def _factory(database, **kwargs):
        calls.append({"database": database, **kwargs})
        orchestrator = RuntimeOrchestrator(
            run_repository=object(),
            agent_runners=kwargs["agent_runners"] or {},
            executable_unit_runner=kwargs["executable_unit_runner"],
        )
        return SimpleNamespace(
            signer=signer,
            audit_repository=SimpleNamespace(_signer=signer),
            artifact_store=object(),
            delivery_worker=SimpleNamespace(http_client=None),
            webhook_repository=object(),
            orchestrator=orchestrator,
            worker=SimpleNamespace(orchestrator=orchestrator),
        )

    return _factory


def _audit_record(
    *, node_id: str, status: str = "completed", tenant_id: str = "evaluation-studio-v1"
):
    return NodeAuditRecord(
        tenant_id=tenant_id,
        workspace_id=None,
        audit_id=f"audit-{node_id}-{status}",
        run_id="run-1",
        node_id=node_id,
        graph_version_ref="graph@1",
        deployment_ref="deployment-1",
        status=status,
        input_snapshot={"synthetic": "input"},
        output_snapshot={"synthetic": "output"},
    )


def test_evaluation_capture_retains_only_successful_synthetic_agent_nodes() -> None:
    classifier = EvaluationSyntheticCaptureClassifier(tenant_id="evaluation-studio-v1")

    for node_id in ("research", "investigate", "synthesize"):
        assert classifier.classify(_audit_record(node_id=node_id)) == CaptureDecision.CONTENT.value
    assert (
        classifier.classify(_audit_record(node_id="retrieve"))
        == CaptureDecision.METADATA_ONLY.value
    )
    assert (
        classifier.classify(_audit_record(node_id="research", status="failed"))
        == CaptureDecision.METADATA_ONLY.value
    )
    assert (
        classifier.classify(_audit_record(node_id="research", tenant_id="other"))
        == CaptureDecision.METADATA_ONLY.value
    )


async def test_factory_wraps_normal_runner_and_wires_dispatcher_lookup(tmp_path: Path) -> None:
    campaign = _campaign(tmp_path)
    calls: list[dict] = []
    secret_provider = object()
    original_provider = object()
    agent_runners = {"agent": SimpleNamespace(provider=original_provider)}

    bootstrap = await bootstrap_evaluation_action_service(
        object(),
        campaign=campaign,
        deployment_ref="workflow-3",
        workspace_id="evaluation-workspace",
        settings=_settings(),
        secret_provider=secret_provider,
        agent_runners=agent_runners,
        bootstrap_factory=_signed_bootstrap_factory(calls),
    )

    assert len(calls) == 1
    call = calls[0]
    assert call["deployment_ref"] == "workflow-3"
    assert call["tenant_id"] == campaign.tenant_id
    assert call["workspace_id"] == "evaluation-workspace"
    assert call["secret_provider"] is secret_provider
    assert call["agent_runners"] is not agent_runners
    wrapped_provider = call["agent_runners"]["agent"].provider
    assert isinstance(wrapped_provider, FaultingProviderAdapter)
    assert wrapped_provider.inner is original_provider
    adapter = call["executable_unit_runner"]
    assert isinstance(adapter, EvaluationActionRunner)
    assert adapter.sink.root == campaign.action_sink_root.resolve()
    assert adapter.fault_state is bootstrap.evaluation_fault_state
    assert adapter.campaign_id == campaign.campaign_id
    assert adapter.delegate.__class__.__name__ == "ExecutableUnitRunner"
    assert adapter.delegate.secret_resolver.provider is secret_provider
    assert adapter.registry.has(EVALUATION_ACTION_MANIFEST_REF)
    assert adapter.registry.has(EVALUATION_CONTROLLED_FAILURE_MANIFEST_REF)
    assert adapter.declares_side_effect(EVALUATION_CONTROLLED_FAILURE_MANIFEST_REF) is False
    binding = adapter.registry.get(EVALUATION_ACTION_MANIFEST_REF)
    assert binding.manifest.side_effect is True
    assert binding.input_model.__name__ == "EvaluationActionPayload"

    dispatcher = bootstrap.orchestrator._node_dispatcher
    assert dispatcher.operation_outcome_lookup is not None
    assert dispatcher.operation_outcome_lookup.__self__ is adapter
    assert bootstrap.worker.orchestrator is bootstrap.orchestrator
    assert bootstrap.evaluation_campaign_id == campaign.campaign_id
    assert (
        bootstrap.evaluation_fault_state.database_path
        == (campaign.artifact_root / "fault-control.sqlite3").resolve()
    )
    assert not isinstance(bootstrap.audit_repository, RestartBarrierAuditRepository)
    assert isinstance(bootstrap.orchestrator.audit_repository, RestartBarrierAuditRepository)
    assert bootstrap.orchestrator.audit_repository.delegate is bootstrap.audit_repository
    assert (
        bootstrap.evaluation_receipt_restart_barriers.database_path
        == (campaign.artifact_root / "receipt-restart-barriers.sqlite3").resolve()
    )


async def test_factory_forwards_trusted_provider_base_urls_to_deployment_runners(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict] = []
    runner_builder = AsyncMock(return_value={"answer": SimpleNamespace(provider=object())})
    monkeypatch.setattr(
        "release.live_evaluation.service.build_runners_for_deployment",
        runner_builder,
    )
    settings = SimpleNamespace(
        provenance=SimpleNamespace(mode="env"),
        approval_sla=SimpleNamespace(enabled=True),
        secrets=SimpleNamespace(
            allow_env_fallback=False,
            llm_key_map={},
            llm_base_url_map={"openai": "http://127.0.0.1:8124/v1"},
        ),
    )

    await bootstrap_evaluation_action_service(
        object(),
        campaign=_campaign(tmp_path),
        deployment_ref="bad-credential-workflow",
        settings=settings,
        secret_provider=object(),
        agent_runners=None,
        bootstrap_factory=_signed_bootstrap_factory(calls),
    )

    runner_builder.assert_awaited_once_with(
        ANY,
        "bad-credential-workflow",
        tenant_id="evaluation-studio-v1",
        workspace_id=None,
        secret_provider=ANY,
        allow_env_fallback=False,
        llm_key_map={},
        llm_base_url_map={"openai": "http://127.0.0.1:8124/v1"},
    )


async def test_factory_registers_and_executes_the_local_record_profiler_manifest(
    tmp_path: Path,
) -> None:
    campaign = _campaign(tmp_path)
    calls: list[dict] = []
    bootstrap = await bootstrap_evaluation_action_service(
        object(),
        campaign=campaign,
        deployment_ref="local-manifest-workflow",
        settings=_settings(),
        secret_provider=object(),
        agent_runners={},
        bootstrap_factory=_signed_bootstrap_factory(calls),
    )
    runner = bootstrap.orchestrator.executable_unit_runner
    manifest_ref = "evaluation://local-code/record-profiler/v1"

    binding = runner.registry.get(manifest_ref)
    assert binding.manifest.onboarding_mode.value == "wrapped_command"
    assert binding.manifest.side_effect is False
    assert binding.manifest.resource_limits.network_access is None
    assert Path(binding.manifest.artifact_source.ref).is_file()

    result = await runner.run(
        manifest_ref,
        {
            "records": [
                {"customer_id": "c-1", "email": "a@example.test"},
                {"customer_id": "c-2", "email": ""},
            ],
            "required_fields": ["customer_id", "email"],
        },
    )

    assert result.output_data == {
        "total_records": 2,
        "missing_counts": {"customer_id": 0, "email": 1},
        "complete_records": 1,
        "completeness_pct": 50.0,
        "ready": False,
    }
    assert result.audit_record["manifest_ref"] == manifest_ref
    assert result.audit_record["sandboxed"] is True
    assert result.audit_record["backend"] == "local"
    assert result.audit_record["cost_usd"] == 0.0
    assert result.audit_record["estimated_cost_usd"] == 0.0
    assert result.audit_record["cost_measurement"] == "measured"

    with pytest.raises(SandboxPolicyViolationError, match="local backend"):
        await runner.run(
            manifest_ref,
            {
                "records": [{"customer_id": "c-1", "email": "a@example.test"}],
                "required_fields": ["customer_id", "email"],
            },
            enforcement_context={"sandbox_strictness_mode": "strict"},
        )


@pytest.mark.parametrize(
    ("manifest_ref", "payload", "expected"),
    [
        (
            "evaluation://local-code/incident-assess/v1",
            {"service": "checkout-api", "severity": "SEV-2"},
            {"ready": False, "readiness_score": 0.4, "planning_round": 0},
        ),
        (
            "evaluation://local-code/incident-prepare/v1",
            {"service": "checkout-api"},
            {"owner": "incident-commander", "planning_round": 1},
        ),
        (
            "evaluation://local-code/incident-finalize/v1",
            {"ready": True, "readiness_score": 1.0, "planning_round": 1},
            {"result": {"status": "ready", "score": 1.0, "planning_rounds": 1, "missing": []}},
        ),
        (
            "evaluation://local-code/incident-escalate/v1",
            {"planning_round": 2, "missing_readiness_fields": ["owner"]},
            {
                "result": {
                    "status": "escalate",
                    "reason": "max_retries_exhausted",
                    "planning_rounds": 2,
                    "missing": ["owner"],
                }
            },
        ),
        (
            "evaluation://local-code/quality-inspect/v1",
            {"records": [{"name": " Ada ", "email": "ADA@EXAMPLE.TEST", "status": "new"}]},
            {"needs_repair": True, "quality_score": 0.0, "repair_pass": 0},
        ),
        (
            "evaluation://local-code/quality-repair/v1",
            {"records": [{"name": " Ada ", "email": "ADA@EXAMPLE.TEST", "status": "new"}]},
            {
                "records": [{"name": "Ada", "email": "ada@example.test", "status": "pending"}],
                "repair_pass": 1,
            },
        ),
        (
            "evaluation://local-code/quality-finalize/v1",
            {"needs_repair": False, "quality_score": 1.0, "repair_pass": 1},
            {
                "result": {
                    "status": "ready",
                    "quality_score": 1.0,
                    "repair_passes": 1,
                    "remaining_issues": 0,
                }
            },
        ),
        (
            "evaluation://local-code/quality-manual-review/v1",
            {"repair_pass": 2, "quality_issues": [{"row": 0, "field": "email"}]},
            {
                "result": {
                    "status": "manual_review",
                    "reason": "max_retries_exhausted",
                    "repair_passes": 2,
                    "remaining_issues": 1,
                }
            },
        ),
    ],
)
async def test_factory_registers_local_manifest_backed_loop_units(
    tmp_path: Path,
    manifest_ref: str,
    payload: dict,
    expected: dict,
) -> None:
    bootstrap = await bootstrap_evaluation_action_service(
        object(),
        campaign=_campaign(tmp_path),
        deployment_ref="manifest-loop-workflow",
        settings=_settings(),
        secret_provider=object(),
        agent_runners={},
        bootstrap_factory=_signed_bootstrap_factory([]),
    )
    runner = bootstrap.orchestrator.executable_unit_runner

    binding = runner.registry.get(manifest_ref)
    assert binding.manifest.onboarding_mode.value == "wrapped_command"
    assert binding.manifest.metadata["external_calls"] is False
    result = await runner.run(manifest_ref, payload)

    for key, value in expected.items():
        assert result.output_data[key] == value
    assert result.audit_record["sandboxed"] is True
    assert result.audit_record["cost_usd"] == 0.0
    assert result.audit_record["cost_measurement"] == "measured"


async def test_factory_rejects_unsigned_config_before_bootstrap(tmp_path: Path) -> None:
    factory = AsyncMock()
    with pytest.raises(EvaluationStartupError, match="signing.*enabled"):
        await bootstrap_evaluation_action_service(
            object(),
            campaign=_campaign(tmp_path),
            deployment_ref="workflow-3",
            settings=SimpleNamespace(
                provenance=SimpleNamespace(mode="off"),
                approval_sla=SimpleNamespace(enabled=True),
            ),
            secret_provider=object(),
            agent_runners={},
            bootstrap_factory=factory,
        )
    factory.assert_not_awaited()


async def test_factory_rejects_unresolved_signer_and_never_exposes_service(tmp_path: Path) -> None:
    async def _unsigned_factory(_database, **kwargs):
        orchestrator = RuntimeOrchestrator(
            run_repository=object(),
            agent_runners={},
            executable_unit_runner=kwargs["executable_unit_runner"],
        )
        return SimpleNamespace(
            signer=None,
            audit_repository=SimpleNamespace(_signer=None),
            orchestrator=orchestrator,
            worker=None,
        )

    with pytest.raises(EvaluationStartupError, match="signed audit readiness"):
        await bootstrap_evaluation_action_service(
            object(),
            campaign=_campaign(tmp_path),
            deployment_ref="workflow-3",
            settings=_settings(),
            secret_provider=object(),
            agent_runners={},
            bootstrap_factory=_unsigned_factory,
        )


async def test_factory_requires_artifact_store_after_signed_readiness(tmp_path: Path) -> None:
    signer = EnvHmacSigner(key_id="evaluation", keys={"evaluation": b"signing-key"})

    async def _missing_artifact_factory(_database, **kwargs):
        orchestrator = RuntimeOrchestrator(
            run_repository=object(),
            agent_runners={},
            executable_unit_runner=kwargs["executable_unit_runner"],
        )
        return SimpleNamespace(
            signer=signer,
            audit_repository=SimpleNamespace(_signer=signer),
            orchestrator=orchestrator,
            worker=None,
        )

    with pytest.raises(EvaluationStartupError, match="artifact store"):
        await bootstrap_evaluation_action_service(
            object(),
            campaign=_campaign(tmp_path),
            deployment_ref="workflow-3",
            settings=_settings(),
            secret_provider=object(),
            agent_runners={},
            bootstrap_factory=_missing_artifact_factory,
        )


async def test_factory_rejects_any_sink_root_override_outside_campaign(tmp_path: Path) -> None:
    factory = AsyncMock()
    with pytest.raises(EvaluationStartupError, match="action_sink_root"):
        await bootstrap_evaluation_action_service(
            object(),
            campaign=_campaign(tmp_path),
            deployment_ref="workflow-3",
            action_sink_root=tmp_path / "other-sink",
            settings=_settings(),
            secret_provider=object(),
            agent_runners={},
            bootstrap_factory=factory,
        )
    factory.assert_not_awaited()


async def test_factory_requires_sla_checker_for_evaluation_approval_expiry(
    tmp_path: Path,
) -> None:
    factory = AsyncMock()

    with pytest.raises(EvaluationStartupError, match="SLA checker"):
        await bootstrap_evaluation_action_service(
            object(),
            campaign=_campaign(tmp_path),
            deployment_ref="workflow-3",
            settings=_settings(sla_enabled=False),
            secret_provider=object(),
            agent_runners={},
            bootstrap_factory=factory,
        )

    factory.assert_not_awaited()


def test_module_cli_requires_campaign_and_scoped_deployment() -> None:
    args = build_parser().parse_args(
        [
            "--campaign-config",
            "release/live_evaluation/campaign-v1.json",
            "--deployment-ref",
            "workflow-3",
            "--workspace-id",
            "evaluation-workspace",
            "--port",
            "8123",
            "--seed-bootstrap",
            "--seed-artifact-demo",
        ]
    )

    assert args.deployment_ref == "workflow-3"
    assert args.workspace_id == "evaluation-workspace"
    assert args.host == "127.0.0.1"
    assert args.port == 8123
    assert args.seed_bootstrap is True
    assert args.seed_artifact_demo is True
