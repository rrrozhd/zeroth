"""Evaluation-only action-enabled service composition and runnable entry point."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Mapping
from copy import copy
from dataclasses import fields, replace
from pathlib import Path
from typing import Any

from zeroth.governance.audit.capture_policy import CaptureDecision
from zeroth.governance.audit.models import NodeAuditRecord
from zeroth.integrations.execution import (
    ExecutionMode,
    InputMode,
    NativeUnitManifest,
    OutputMode,
    PythonModuleArtifactSource,
)
from zeroth.integrations.execution.runner import ExecutableUnitRunner
from zeroth.platform.secrets import SecretResolver
from zeroth.platform.signing import NullSigner
from zeroth.runtime.orchestration.orchestrator import RuntimeOrchestrator
from zeroth.service.bootstrap.factory import (
    bootstrap_scoped_service,
    build_runners_for_deployment,
)

from .action_runner import (
    EVALUATION_ACTION_MANIFEST_REF,
    EVALUATION_ARTIFACT_MANIFEST_REF,
    EVALUATION_CONTROLLED_FAILURE_MANIFEST_REF,
    EvaluationActionOutput,
    EvaluationActionPayload,
    EvaluationActionRunner,
    EvaluationArtifactOutput,
    EvaluationArtifactPayload,
)
from .action_sink import EvaluationActionSink
from .config import CampaignConfig
from .fault_control import (
    EvaluationFaultingMemoryResolver,
    EvaluationFaultState,
    FaultingProviderAdapter,
    register_fault_control_routes,
)
from .local_code import register_local_code_manifests
from .receipt_restart_barrier import (
    ReceiptRestartBarrierStore,
    RestartBarrierAuditRepository,
)
from .webhook_sink import EvaluationWebhookSink, EvaluationWebhookTransport


class EvaluationStartupError(RuntimeError):
    """The action-enabled evaluation service failed a startup safety gate."""


class EvaluationSyntheticCaptureClassifier:
    """Retain only successful synthetic agent I/O needed by the campaign evals."""

    _AGENT_NODE_IDS = frozenset({"research", "investigate", "synthesize"})
    _SUCCESS_STATUSES = frozenset({"completed", "success", "succeeded"})

    def __init__(self, *, tenant_id: str) -> None:
        self._tenant_id = tenant_id

    def classify(self, record: NodeAuditRecord) -> str:
        if (
            record.tenant_id == self._tenant_id
            and record.node_id in self._AGENT_NODE_IDS
            and record.status in self._SUCCESS_STATUSES
        ):
            return CaptureDecision.CONTENT.value
        return CaptureDecision.METADATA_ONLY.value


class EvaluationRuntimeOrchestrator(RuntimeOrchestrator):
    """Runtime facade whose dispatchers know how to query the local action sink."""

    @property
    def _node_dispatcher(self):
        dispatcher = super()._node_dispatcher
        lookup = self.executable_unit_runner.outcome_lookup
        return replace(dispatcher, operation_outcome_lookup=lookup)


def _with_evaluation_lookup(orchestrator: RuntimeOrchestrator) -> EvaluationRuntimeOrchestrator:
    values = {
        field.name: getattr(orchestrator, field.name)
        for field in fields(RuntimeOrchestrator)
        if field.init
    }
    enhanced = EvaluationRuntimeOrchestrator(**values)
    token_store = getattr(orchestrator, "_token_snapshot_store", None)
    if token_store is not None:
        enhanced.use_token_snapshot_store(token_store)
    return enhanced


def _require_configured_sink_root(
    campaign: CampaignConfig,
    requested: Path | None,
) -> Path:
    configured = campaign.action_sink_root.expanduser().resolve(strict=False)
    if requested is None:
        return configured
    resolved = requested.expanduser().resolve(strict=False)
    if resolved != configured:
        raise EvaluationStartupError(
            "action_sink_root must exactly match CampaignConfig.action_sink_root"
        )
    return configured


def _require_signed_bootstrap(bootstrap: Any) -> None:
    signer = getattr(bootstrap, "signer", None)
    audit_signer = getattr(getattr(bootstrap, "audit_repository", None), "_signer", None)
    if (
        signer is None
        or isinstance(signer, NullSigner)
        or audit_signer is None
        or isinstance(audit_signer, NullSigner)
    ):
        raise EvaluationStartupError(
            "action-enabled service requires signed audit readiness with an active signer"
        )


def _register_evaluation_action(runner: ExecutableUnitRunner, *, campaign_id: str) -> None:
    """Expose the intercepted local action to manifest/preflight discovery."""
    runner.registry.register(
        EVALUATION_ACTION_MANIFEST_REF,
        NativeUnitManifest(
            unit_id="evaluation-synthetic-action",
            onboarding_mode=ExecutionMode.NATIVE,
            runtime="python",
            artifact_source=PythonModuleArtifactSource(
                ref="release.live_evaluation.action_runner:EvaluationActionRunner.run"
            ),
            callable_ref="release.live_evaluation.action_runner:EvaluationActionRunner.run",
            entrypoint_type="python_callable",
            input_mode=InputMode.JSON_STDIN,
            output_mode=OutputMode.JSON_STDOUT,
            input_contract_ref="evaluation://synthetic-action/input/v1",
            output_contract_ref="evaluation://synthetic-action/output/v1",
            side_effect=True,
            metadata={"evaluation_only": True, "external_calls": False},
        ),
        input_model=EvaluationActionPayload,
        output_model=EvaluationActionOutput,
    )
    from .campaign_execution import Workflow2ChildResult, Workflow2Retrieved

    runner.registry.register(
        EVALUATION_CONTROLLED_FAILURE_MANIFEST_REF,
        NativeUnitManifest(
            unit_id="evaluation-controlled-failure",
            onboarding_mode=ExecutionMode.NATIVE,
            runtime="python",
            artifact_source=PythonModuleArtifactSource(
                ref=("release.live_evaluation.action_runner:EvaluationActionRunner.run")
            ),
            callable_ref="release.live_evaluation.action_runner:EvaluationActionRunner.run",
            entrypoint_type="python_callable",
            input_mode=InputMode.JSON_STDIN,
            output_mode=OutputMode.JSON_STDOUT,
            input_contract_ref=f"{campaign_id}.workflow2.retrieved@1",
            output_contract_ref=f"{campaign_id}.workflow2.child-result@1",
            side_effect=False,
            metadata={"evaluation_only": True, "external_calls": False},
        ),
        input_model=Workflow2Retrieved,
        output_model=Workflow2ChildResult,
    )
    runner.registry.register(
        EVALUATION_ARTIFACT_MANIFEST_REF,
        NativeUnitManifest(
            unit_id="evaluation-artifact-emitter",
            onboarding_mode=ExecutionMode.NATIVE,
            runtime="python",
            artifact_source=PythonModuleArtifactSource(
                ref="release.live_evaluation.action_runner:EvaluationActionRunner.run"
            ),
            callable_ref="release.live_evaluation.action_runner:EvaluationActionRunner.run",
            entrypoint_type="python_callable",
            input_mode=InputMode.JSON_STDIN,
            output_mode=OutputMode.JSON_STDOUT,
            input_contract_ref="evaluation://artifact-emitter/input/v1",
            output_contract_ref="evaluation://artifact-emitter/output/v1",
            side_effect=True,
            metadata={"evaluation_only": True, "external_calls": False},
        ),
        input_model=EvaluationArtifactPayload,
        output_model=EvaluationArtifactOutput,
    )
    register_local_code_manifests(runner)


def _with_faulting_providers(
    agent_runners: Mapping[str, Any] | None, state: EvaluationFaultState
) -> dict[str, Any]:
    """Fork evaluation runner prototypes and wrap only their provider boundary."""
    result: dict[str, Any] = {}
    for key, prototype in (agent_runners or {}).items():
        fork = getattr(prototype, "fork_for_dispatch", None)
        runner = fork() if callable(fork) else copy(prototype)
        provider = getattr(runner, "provider", None)
        if provider is not None:
            runner.provider = FaultingProviderAdapter(inner=provider, state=state)
        result[key] = runner
    return result


async def _wire_evaluation_webhook_sink(bootstrap: Any, *, campaign: CampaignConfig) -> None:
    """Replace outbound webhook networking with the campaign-local signed sink."""
    worker = getattr(bootstrap, "delivery_worker", None)
    repository = getattr(bootstrap, "webhook_repository", None)
    if worker is None or repository is None:
        raise EvaluationStartupError("evaluation webhook delivery must be enabled")

    import httpx

    sink = EvaluationWebhookSink(campaign.artifact_root / "webhook-sink.sqlite3")
    replacement = httpx.AsyncClient(
        transport=EvaluationWebhookTransport(repository=repository, sink=sink),
        timeout=httpx.Timeout(1.0),
    )
    previous = getattr(worker, "http_client", None)
    worker.http_client = replacement
    bootstrap.webhook_http_client = replacement
    bootstrap.evaluation_webhook_sink = sink
    close = getattr(previous, "aclose", None)
    if callable(close):
        await close()


async def bootstrap_evaluation_action_service(
    database: Any,
    *,
    campaign: CampaignConfig,
    deployment_ref: str,
    workspace_id: str | None = None,
    action_sink_root: Path | None = None,
    settings: Any | None = None,
    secret_provider: Any | None = None,
    agent_runners: Mapping[str, Any] | None = None,
    enable_durable_worker: bool = True,
    bootstrap_factory: Any = bootstrap_scoped_service,
) -> Any:
    """Compose production bootstrap with the evaluation-only action adapter."""
    if settings is None:
        from zeroth.platform.config.settings import get_settings

        settings = get_settings()
    if getattr(settings.provenance, "mode", None) == "off":
        raise EvaluationStartupError(
            "provenance signing must be enabled for the action-enabled service"
        )
    if getattr(getattr(settings, "approval_sla", None), "enabled", None) is not True:
        raise EvaluationStartupError(
            "evaluation approval expiry requires the SLA checker to be enabled"
        )
    sink_root = _require_configured_sink_root(campaign, action_sink_root)

    if secret_provider is None:
        from zeroth.platform.secrets import build_secret_provider

        secret_provider = build_secret_provider(settings.secrets)
        warm = getattr(secret_provider, "warm", None)
        if callable(warm):
            await warm()

    resolved_agent_runners = agent_runners
    if resolved_agent_runners is None:
        resolved_agent_runners = await build_runners_for_deployment(
            database,
            deployment_ref,
            tenant_id=campaign.tenant_id,
            workspace_id=workspace_id,
            secret_provider=secret_provider,
            allow_env_fallback=settings.secrets.allow_env_fallback,
            llm_key_map=settings.secrets.llm_key_map,
            llm_base_url_map=settings.secrets.llm_base_url_map,
        )
    fault_state = EvaluationFaultState(campaign.artifact_root / "fault-control.sqlite3")
    resolved_agent_runners = _with_faulting_providers(resolved_agent_runners, fault_state)

    delegate = ExecutableUnitRunner(secret_resolver=SecretResolver(secret_provider))
    _register_evaluation_action(delegate, campaign_id=campaign.campaign_id)
    action_sink = EvaluationActionSink(sink_root)
    action_runner = EvaluationActionRunner(
        delegate=delegate,
        sink=action_sink,
        fault_state=fault_state,
        campaign_id=campaign.campaign_id,
    )
    bootstrap = await bootstrap_factory(
        database,
        deployment_ref=deployment_ref,
        tenant_id=campaign.tenant_id,
        workspace_id=workspace_id,
        agent_runners=resolved_agent_runners,
        executable_unit_runner=action_runner,
        enable_durable_worker=enable_durable_worker,
        secret_provider=secret_provider,
    )
    _require_signed_bootstrap(bootstrap)
    artifact_store = getattr(bootstrap, "artifact_store", None)
    if artifact_store is None:
        raise EvaluationStartupError(
            "evaluation artifact store is required for the action-enabled service"
        )
    action_runner.artifact_store = artifact_store
    configure_capture = getattr(bootstrap.audit_repository, "configure_capture", None)
    if callable(configure_capture):
        configure_capture(EvaluationSyntheticCaptureClassifier(tenant_id=campaign.tenant_id))
    barrier_store = ReceiptRestartBarrierStore(
        campaign.artifact_root / "receipt-restart-barriers.sqlite3"
    )
    barrier_repository = RestartBarrierAuditRepository(
        delegate=bootstrap.audit_repository,
        barrier_store=barrier_store,
        fault_state=fault_state,
        operation_store=bootstrap.orchestrator.operation_store,
        run_repository=bootstrap.orchestrator.run_repository,
        action_sink=action_sink,
        campaign_id=campaign.campaign_id,
    )
    bootstrap.orchestrator.audit_repository = barrier_repository
    bootstrap.evaluation_campaign_id = campaign.campaign_id
    bootstrap.evaluation_campaign = campaign
    bootstrap.evaluation_fault_state = fault_state
    bootstrap.evaluation_receipt_restart_barriers = barrier_store
    await _wire_evaluation_webhook_sink(bootstrap, campaign=campaign)

    memory_resolver = getattr(bootstrap, "memory_resolver", None)
    if memory_resolver is None:
        memory_resolver = getattr(bootstrap.orchestrator, "memory_resolver", None)
    if memory_resolver is not None:
        faulting_memory_resolver = EvaluationFaultingMemoryResolver(
            inner=memory_resolver,
            state=fault_state,
        )
        bootstrap.memory_resolver = faulting_memory_resolver
        bootstrap.orchestrator.memory_resolver = faulting_memory_resolver

    enhanced = _with_evaluation_lookup(bootstrap.orchestrator)
    bootstrap.orchestrator = enhanced
    worker = getattr(bootstrap, "worker", None)
    if worker is not None:
        worker.orchestrator = enhanced
    return bootstrap


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m release.live_evaluation.service",
        description="Serve one signed, action-enabled evaluation deployment.",
    )
    parser.add_argument("--campaign-config", type=Path, required=True)
    parser.add_argument("--deployment-ref", required=True)
    parser.add_argument("--workspace-id", default=None)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--seed-bootstrap",
        action="store_true",
        help="Idempotently seed the tenant-owned bootstrap deployment before serving.",
    )
    parser.add_argument(
        "--seed-artifact-demo",
        action="store_true",
        help="Idempotently seed the provider-free artifact deployment before serving.",
    )
    return parser


def _load_campaign(path: Path) -> CampaignConfig:
    return CampaignConfig.model_validate(json.loads(path.read_text()))


async def _serve(args: argparse.Namespace) -> None:
    import uvicorn

    from zeroth.platform.config.settings import get_settings
    from zeroth.platform.storage.factory import create_database
    from zeroth.service.app import create_app

    campaign = _load_campaign(args.campaign_config)
    settings = get_settings()
    database = await create_database(settings)
    if args.seed_bootstrap:
        from .bootstrap import seed_campaign_bootstrap

        await seed_campaign_bootstrap(
            database,
            tenant_id=campaign.tenant_id,
            deployment_ref=args.deployment_ref,
            model=campaign.model,
        )
    if args.seed_artifact_demo:
        from .artifact_demo import ARTIFACT_DEMO_DEPLOYMENT_REF, seed_artifact_demo

        if args.deployment_ref != ARTIFACT_DEMO_DEPLOYMENT_REF:
            raise EvaluationStartupError(
                f"--seed-artifact-demo requires deployment_ref {ARTIFACT_DEMO_DEPLOYMENT_REF!r}"
            )
        await seed_artifact_demo(database, tenant_id=campaign.tenant_id)
    bootstrap = await bootstrap_evaluation_action_service(
        database,
        campaign=campaign,
        deployment_ref=args.deployment_ref,
        workspace_id=args.workspace_id,
        settings=settings,
    )
    from .control_service import register_control_corpus_routes

    app = create_app(
        bootstrap,
        extra_v1_route_registrars=(register_control_corpus_routes,),
    )
    register_fault_control_routes(
        app,
        state=bootstrap.evaluation_fault_state,
        campaign_id=campaign.campaign_id,
    )
    config = uvicorn.Config(app, host=args.host, port=args.port, proxy_headers=True)
    await uvicorn.Server(config).serve()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    from zeroth.service.cli import ensure_schema

    ensure_schema()
    asyncio.run(_serve(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
