"""Safety-gated composition root for the live evaluation campaign.

Importing and dry-running this module performs no network, database, filesystem
mutation, subprocess, or provider operation.  The live boundary requires exact
campaign-specific acknowledgements before it creates the evidence bundle.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import shlex
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

from zeroth.platform.storage import AsyncDatabase

from .action_sink import EvaluationActionSink
from .browser_refresh import BoundedRefreshEvidenceProducer
from .campaign_execution import (
    CampaignExecutionSettings,
    WorkflowAction,
    build_campaign_execution,
)
from .campaign_finalizer import EvidenceFirstCampaignFinalizer
from .campaign_http import (
    AcceptanceEvaluator,
    BackendObservation,
    HttpBackendConfig,
    HttpCampaignExecutionBackend,
    StrictAcceptanceEvaluator,
    Workflow1NegativeEvaluator,
    Workflow1ScenarioController,
    provider_acknowledgement,
)
from .campaign_runtime import (
    LiveExecutionOptions,
    LocalDeploymentSupervisor,
    RepositoryTenantGraphPublisher,
    load_live_execution_options,
    mutation_acknowledgement,
)
from .config import CampaignConfig
from .coordinator import (
    ActionRecorder,
    CampaignCoordinator,
    CampaignPlan,
    CampaignStep,
    CampaignSummary,
    CriterionResult,
    Phase,
    StepResult,
)
from .criteria import original_acceptance_criteria
from .cross_cutting_gates import (
    CrossCuttingSources,
    EvidenceFirstCrossCuttingGateExecutor,
)
from .evidence import AcceptanceCriterion, EvidenceStore
from .fault_control import EvaluationFaultState
from .ledger import CampaignLedger
from .live_sources import (
    BoundedPlaywrightProducer,
    BoundedSnapshotProducer,
    BoundedZerothCheckRunner,
    CampaignSnapshotCollector,
)
from .receipt_restart_barrier import ReceiptRestartBarrierStore
from .scenario_controller import (
    LoopbackDeployment,
    LoopbackHttpScenarioRuntimeGateway,
    create_scenario_controller_app,
)
from .scenario_controller_server import OwnedScenarioControllerServer
from .workflow1_scenarios import (
    ChromaCollectionFixtureBackend,
    LocalWorkflow1ScenarioController,
)
from .workflow2_scenarios import (
    LocalWorkflow2ScenarioController,
    Workflow2NegativeEvaluator,
    Workflow2ScenarioController,
)
from .workflow3_scenarios import (
    RemoteWorkflow3ScenarioController,
    Workflow3NegativeEvaluator,
    Workflow3ScenarioController,
)

_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_FINAL_DERIVED_CRITERIA = {
    "evidence.acceptance",
    "evidence.report",
    "evidence.sha256-checksums",
}
_ENVIRONMENT_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{1,127}$")
WORKFLOW1_FALLBACK_REASON = (
    "authorized workflow1 fallback: the shared provider-project usage window is an "
    "upper-bound with unrelated traffic; workflow2, workflow3, action, cross-cutting, "
    "check, and final handoff gates were not executed"
)
_WORKFLOW1_FALLBACK_HALTED_BY = "authorized.workflow1-fallback"


def _loopback_origin(value: str, *, label: str) -> str:
    parsed = urlparse(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.port is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"{label} must be an explicit loopback HTTP origin")
    return value.rstrip("/")


@dataclass(frozen=True, slots=True)
class CampaignEndpoints:
    """Explicit console, deployment, and deterministic-fault origins."""

    console_base_url: str
    deployment_base_urls: Mapping[str, str]
    fault_control_url: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "console_base_url",
            _loopback_origin(self.console_base_url, label="console URL"),
        )
        if not self.deployment_base_urls:
            raise ValueError("deployment URLs are required")
        normalized = {
            reference: _loopback_origin(url, label=f"deployment URL for {reference}")
            for reference, url in self.deployment_base_urls.items()
        }
        if any(
            not reference or any(character.isspace() for character in reference)
            for reference in normalized
        ):
            raise ValueError("deployment references must be non-empty tokens")
        if len(set(normalized.values())) != len(normalized):
            raise ValueError("deployment services require distinct loopback origins")
        object.__setattr__(self, "deployment_base_urls", normalized)
        object.__setattr__(
            self,
            "fault_control_url",
            _loopback_origin(self.fault_control_url, label="fault-control URL"),
        )


@dataclass(frozen=True, slots=True)
class CampaignEntrypointResult:
    mode: str
    campaign_id: str
    evidence_bundle: Path
    summary: CampaignSummary | None = None


@dataclass(frozen=True, slots=True)
class LocalCampaignControllerComposition:
    """Concrete local controller graph; credential values are never represented."""

    api_key: str = field(repr=False)
    controller_key: str = field(repr=False)
    scenario_controller_url: str
    runtime_gateway: LoopbackHttpScenarioRuntimeGateway
    workflow1_controller: LocalWorkflow1ScenarioController
    workflow2_controller: LocalWorkflow2ScenarioController
    workflow3_controller: Workflow3ScenarioController
    scenario_app: object

    def require_scenario_controller_ready(self, client: httpx.Client) -> None:
        response = client.request(
            "GET",
            f"{self.scenario_controller_url}/health",
            headers={"X-Controller-Key": self.controller_key},
            timeout=3.0,
        )
        if response.status_code < 200 or response.status_code >= 300:
            raise RuntimeError("prestarted scenario controller is not ready")


def _required_environment_secret(
    environment: Mapping[str, str], *, variable: str, label: str
) -> str:
    if not _ENVIRONMENT_NAME.fullmatch(variable):
        raise ValueError(f"{label} environment variable name is invalid")
    value = environment.get(variable)
    if value is None or not value.strip():
        raise ValueError(f"{label} environment variable is required")
    return value


def _open_loopback_chroma_collection(base_url: str, collection_name: str) -> object:
    """Open one explicit pre-seeded collection; never creates a collection."""
    normalized = _loopback_origin(base_url, label="Chroma URL")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{2,511}", collection_name):
        raise ValueError("Chroma collection must be an explicit valid collection name")
    parsed = urlparse(normalized)
    import chromadb

    client = chromadb.HttpClient(
        host=str(parsed.hostname),
        port=parsed.port,
        ssl=False,
    )
    return client.get_collection(name=collection_name)


def compose_local_campaign_controllers(
    *,
    campaign: CampaignConfig,
    endpoints: CampaignEndpoints,
    evidence_store: EvidenceStore,
    environment: Mapping[str, str],
    api_key_env: str,
    controller_key_env: str,
    scenario_controller_url: str,
    chroma_collection: object,
    refresh_producer: BoundedRefreshEvidenceProducer | None = None,
    chroma_connector_ref: str = "chroma",
    client: httpx.Client | None = None,
    supervisor: object | None = None,
    receipt_barriers: object | None = None,
) -> LocalCampaignControllerComposition:
    """Build local-only campaign controllers without issuing any HTTP request.

    The caller supplies environment-variable *names*. Secret values never enter
    argv, campaign configuration, evidence, or a dataclass representation.
    """
    api_key = _required_environment_secret(
        environment, variable=api_key_env, label="Zeroth API key"
    )
    controller_key = _required_environment_secret(
        environment, variable=controller_key_env, label="scenario controller key"
    )
    artifact_root = campaign.artifact_root.expanduser().resolve(strict=False)
    controller_url = _loopback_origin(scenario_controller_url, label="scenario-controller URL")
    topology = build_campaign_execution(
        CampaignExecutionSettings(
            campaign_id=campaign.campaign_id,
            tenant_id=campaign.tenant_id,
            model=campaign.model,
            embedding_model=campaign.embedding_model,
            chroma_connector_ref=chroma_connector_ref,
        )
    ).deployments
    expected_refs = {
        topology.workflow1,
        topology.workflow2_child,
        topology.workflow2_parent,
        topology.workflow3,
    }
    if set(endpoints.deployment_base_urls) != expected_refs:
        raise ValueError("local controller deployment map must exactly cover four workflows")
    deployments = {
        reference: LoopbackDeployment(
            base_url=base_url,
            deployment_ref=reference,
        )
        for reference, base_url in endpoints.deployment_base_urls.items()
    }
    barriers = receipt_barriers or ReceiptRestartBarrierStore(
        artifact_root / "receipt-restart-barriers.sqlite3"
    )
    gateway = LoopbackHttpScenarioRuntimeGateway(
        campaign_id=campaign.campaign_id,
        deployments=deployments,
        client=client,
        headers={"X-API-Key": api_key, "X-Tenant-ID": campaign.tenant_id},
        supervisor=supervisor,
        receipt_barriers=barriers,
    )
    sink = EvaluationActionSink(campaign.action_sink_root)
    workflow1 = LocalWorkflow1ScenarioController(
        campaign_id=campaign.campaign_id,
        tenant_id=campaign.tenant_id,
        state_root=artifact_root / "scenario-controller",
        corpus=ChromaCollectionFixtureBackend(
            chroma_collection,
            tenant_id=campaign.tenant_id,
        ),
    )
    workflow2 = LocalWorkflow2ScenarioController(
        controller_url=controller_url,
        controller_key=controller_key,
        workflow_id=topology.workflow2_parent,
        client=client or gateway.client,
        refresh_producer=refresh_producer,
    )
    workflow3 = RemoteWorkflow3ScenarioController(
        sink=sink,
        controller_url=controller_url,
        controller_key=controller_key,
        workflow_id=topology.workflow3,
        client=client or gateway.client,
        refresh_producer=refresh_producer,
    )
    if evidence_store.root != artifact_root and not evidence_store.root.is_relative_to(
        artifact_root
    ):
        raise ValueError("evidence store must be campaign-scoped under artifact root")
    scenario_app = create_scenario_controller_app(
        campaign_id=campaign.campaign_id,
        artifact_root=artifact_root,
        evidence_store=evidence_store,
        fault_state=EvaluationFaultState(artifact_root / "fault-control.sqlite3"),
        action_sink=sink,
        controller_key=controller_key,
        runtime_gateway=gateway,
    )
    return LocalCampaignControllerComposition(
        api_key=api_key,
        controller_key=controller_key,
        scenario_controller_url=controller_url,
        runtime_gateway=gateway,
        workflow1_controller=workflow1,
        workflow2_controller=workflow2,
        workflow3_controller=workflow3,
        scenario_app=scenario_app,
    )


class _Supervisor(Protocol):
    def restart(self, *, deployment_ref: str, service_url: str) -> None: ...
    def stop_all(self) -> None: ...


class CrossCuttingGateExecutor(Protocol):
    """Injected evidence collector/evaluator for non-workflow campaign gates."""

    def execute(
        self,
        *,
        phase: Phase,
        criterion_ids: tuple[str, ...],
        recorder: ActionRecorder,
    ) -> StepResult: ...


class CampaignEvidenceFinalizer(Protocol):
    """Write final acceptance/report/checksums after all prerequisites pass."""

    def finalize(self, *, store: EvidenceStore, ledger: CampaignLedger) -> None: ...


class _BlockedCrossCuttingGateExecutor:
    """Default: never convert missing UI/audit/economics evidence into success."""

    def execute(
        self,
        *,
        phase: Phase,
        criterion_ids: tuple[str, ...],
        recorder: ActionRecorder,
    ) -> StepResult:
        if phase is Phase.CHECK:
            evidence = recorder.record_ui_action(
                action="zeroth-check",
                outcome="blocked",
                metadata={
                    "reason": "cross-cutting evidence gates are not configured in this entrypoint"
                },
            )
            return StepResult(
                tuple(
                    CriterionResult(
                        criterion_id,
                        "fail",
                        (evidence,),
                        "full campaign completion requires a cross-cutting gate executor",
                    )
                    for criterion_id in criterion_ids
                )
            )
        return StepResult(
            tuple(
                CriterionResult(
                    criterion_id,
                    "blocked",
                    (),
                    "requires its dedicated evidence collector and acceptance evaluator",
                )
                for criterion_id in criterion_ids
            )
        )


class _AcceptanceRouter:
    """Route implemented evaluators; W2/W3 negatives intentionally fail closed."""

    def __init__(self) -> None:
        self.strict = StrictAcceptanceEvaluator()
        self.workflow1_negative = Workflow1NegativeEvaluator()
        self.workflow2_negative = Workflow2NegativeEvaluator()
        self.workflow3_negative = Workflow3NegativeEvaluator()

    def evaluate(self, action: WorkflowAction, observation: BackendObservation) -> StepResult:
        if action.workflow == "workflow1" and action.action_type == "negative":
            return self.workflow1_negative.evaluate(action, observation)
        if action.workflow == "workflow2" and action.action_type == "negative":
            return self.workflow2_negative.evaluate(action, observation)
        if action.workflow == "workflow3" and action.action_type == "negative":
            return self.workflow3_negative.evaluate(action, observation)
        return self.strict.evaluate(action, observation)


def _default_database_factory() -> AsyncDatabase:
    from zeroth.platform.config.settings import get_settings
    from zeroth.platform.storage.factory import create_database

    return asyncio.run(create_database(get_settings()))


class CampaignEntrypoint:
    """Compose and run the resumable three-workflow campaign after safety gates."""

    def __init__(
        self,
        *,
        repository_root: Path,
        campaign_config_path: Path,
        evidence_bundle: Path,
        endpoints: CampaignEndpoints,
        options: LiveExecutionOptions,
        database_factory: Callable[[], Any] = _default_database_factory,
        client_factory: Callable[[], httpx.Client] = httpx.Client,
        publisher_factory: Callable[[Any], Any] = RepositoryTenantGraphPublisher,
        supervisor_factory: Callable[[EvidenceStore], _Supervisor] | None = None,
        evaluator: AcceptanceEvaluator | None = None,
        workspace_id: str | None = None,
        api_key: str | None = None,
        service_environment: Mapping[str, str] | None = None,
        chroma_connector_ref: str = "chroma",
        workflow1_scenario_controller: Workflow1ScenarioController | None = None,
        workflow2_scenario_controller: Workflow2ScenarioController | None = None,
        workflow3_scenario_controller: Workflow3ScenarioController | None = None,
        cross_cutting_gate_executor: CrossCuttingGateExecutor | None = None,
        evidence_finalizer: CampaignEvidenceFinalizer | None = None,
        compose_local_controllers: bool = False,
        controller_environment: Mapping[str, str] | None = None,
        api_key_env: str = "ZEROTH_EVALUATION_API_KEY",
        controller_key_env: str = "ZEROTH_EVALUATION_FAULT_CONTROLLER_KEY",
        scenario_controller_url: str | None = None,
        chroma_url: str = "http://127.0.0.1:8121",
        chroma_collection_name: str | None = None,
        chroma_collection_factory: Callable[[str, str], object] = (
            _open_loopback_chroma_collection
        ),
        stop_after_workflow1: bool = False,
        frontend_root: Path | None = None,
        browser_environment: Mapping[str, str] | None = None,
    ) -> None:
        self.repository_root = repository_root.resolve(strict=True)
        self.campaign_config_path = campaign_config_path.resolve(strict=True)
        self.evidence_bundle = evidence_bundle.expanduser().resolve(strict=False)
        self.endpoints = endpoints
        self.options = options
        self.database_factory = database_factory
        self.client_factory = client_factory
        self.publisher_factory = publisher_factory
        self.supervisor_factory = supervisor_factory
        self.evaluator = evaluator or _AcceptanceRouter()
        self.workspace_id = workspace_id
        self.api_key = api_key
        self.service_environment = dict(service_environment or {})
        self.chroma_connector_ref = chroma_connector_ref
        self.workflow1_scenario_controller = workflow1_scenario_controller
        self.workflow2_scenario_controller = workflow2_scenario_controller
        self.workflow3_scenario_controller = workflow3_scenario_controller
        self.cross_cutting_gate_executor = (
            cross_cutting_gate_executor or _BlockedCrossCuttingGateExecutor()
        )
        self.evidence_finalizer = evidence_finalizer
        self.compose_local_controllers = compose_local_controllers
        self.controller_environment = dict(controller_environment or {})
        self.api_key_env = api_key_env
        self.controller_key_env = controller_key_env
        self.scenario_controller_url = scenario_controller_url
        self.chroma_url = chroma_url
        self.chroma_collection_name = chroma_collection_name
        self.chroma_collection_factory = chroma_collection_factory
        self.stop_after_workflow1 = stop_after_workflow1
        self.frontend_root = frontend_root
        self.browser_environment = dict(browser_environment or {})
        self.local_controller_composition: LocalCampaignControllerComposition | None = None

    def _load_and_validate(self) -> tuple[CampaignConfig, Any]:
        campaign = CampaignConfig.model_validate(
            json.loads(self.campaign_config_path.read_text(encoding="utf-8"))
        )
        if campaign.campaign_id != self.options.campaign_id:
            raise ValueError("campaign config and execution options disagree")
        artifact_matches = campaign.artifact_root.resolve(
            strict=False
        ) == self.options.artifact_root.resolve(strict=False)
        sink_matches = campaign.action_sink_root.resolve(
            strict=False
        ) == self.options.action_sink_root.resolve(strict=False)
        if not artifact_matches or not sink_matches:
            raise ValueError("campaign config and execution roots disagree")
        artifact_root = campaign.artifact_root.resolve(strict=False)
        if not self.evidence_bundle.is_relative_to(artifact_root):
            raise ValueError("evidence bundle must be campaign-scoped under artifact root")
        sink_root = campaign.action_sink_root.resolve(strict=False)
        if self.evidence_bundle == sink_root or self.evidence_bundle.is_relative_to(sink_root):
            raise ValueError("evidence bundle must remain separate from action-sink state")
        execution = build_campaign_execution(
            CampaignExecutionSettings(
                campaign_id=campaign.campaign_id,
                tenant_id=campaign.tenant_id,
                model=campaign.model,
                embedding_model=campaign.embedding_model,
                chroma_connector_ref=self.chroma_connector_ref,
                workspace_id=self.workspace_id,
            )
        )
        expected_refs = {
            execution.deployments.workflow1,
            execution.deployments.workflow2_child,
            execution.deployments.workflow2_parent,
            execution.deployments.workflow3,
        }
        if set(self.endpoints.deployment_base_urls) != expected_refs:
            raise ValueError("deployment URL map must exactly cover the campaign topology")
        return campaign, execution

    def _require_live_authority(self, campaign: CampaignConfig) -> None:
        if not self.options.allow_mutation or self.options.mutation_acknowledgement != (
            mutation_acknowledgement(campaign.campaign_id)
        ):
            raise ValueError("live campaign requires the exact mutation acknowledgement")
        if not self.options.allow_provider or self.options.paid_acknowledgement != (
            provider_acknowledgement(campaign.campaign_id)
        ):
            raise ValueError("live campaign requires the exact provider acknowledgement")

    def _require_control_acceptance(
        self, campaign: CampaignConfig, store: EvidenceStore
    ) -> CampaignLedger:
        manifest_path = store.root / "manifest.json"
        if not manifest_path.is_file():
            raise RuntimeError("live campaign requires a pre-existing control manifest")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise RuntimeError("control manifest is unreadable") from exc
        if not isinstance(manifest, dict):
            raise RuntimeError("control manifest must be a JSON object")
        campaign_config = manifest.get("campaign_config")
        snapshots = manifest.get("pretest_sqlite_snapshots")
        if (
            not isinstance(campaign_config, dict)
            or campaign_config.get("campaign_id") != campaign.campaign_id
            or not isinstance(manifest.get("revision"), str)
            or not manifest["revision"]
            or not isinstance(manifest.get("dirty_tree_hash"), str)
            or not _SHA256.fullmatch(manifest["dirty_tree_hash"])
            or not isinstance(snapshots, list)
            or not snapshots
            or not all(isinstance(item, str) for item in snapshots)
            or not all(
                Path(item).parts[:1] == ("database-snapshots",)
                and Path(item).suffix in {".db", ".sqlite", ".sqlite3"}
                for item in snapshots
            )
        ):
            raise RuntimeError("control manifest lacks revision, diff, or snapshot identity")
        store.validate_evidence_references(
            (AcceptanceCriterion("control-snapshots", "pass", tuple(snapshots)),)
        )
        for reference in snapshots:
            snapshot_path = store.root / reference
            try:
                with sqlite3.connect(f"file:{snapshot_path}?mode=ro", uri=True) as connection:
                    integrity = connection.execute("PRAGMA integrity_check").fetchone()
            except sqlite3.DatabaseError as exc:
                raise RuntimeError("control database snapshot is not valid SQLite") from exc
            if integrity != ("ok",):
                raise RuntimeError("control database snapshot failed integrity_check")

        ledger = CampaignLedger(store, original_acceptance_criteria())
        statuses = {item.criterion_id: item.status for item in ledger.criteria}
        required = {
            criterion_id for criterion_id in statuses if criterion_id.startswith("control.")
        } | {"audit.probe-events-instrumented"}
        missing = sorted(item for item in required if statuses[item] != "pass")
        if missing or ledger.halted:
            raise RuntimeError(
                "control acceptance is incomplete: " + ", ".join(missing or ["stop condition"])
            )
        store.validate_evidence_references(
            (
                AcceptanceCriterion(
                    "control-readiness",
                    "pass",
                    tuple(
                        reference
                        for item in ledger.criteria
                        if item.criterion_id in required
                        for reference in item.evidence
                    ),
                ),
            )
        )

        events = store.read_events()
        signed = [
            item for item in events if item.get("type") == "control.signed-readiness.recorded"
        ]
        signed_data = signed[0].get("data") if len(signed) == 1 else None
        if (
            not isinstance(signed_data, dict)
            or signed_data.get("state") != "signed"
            or signed_data.get("algorithm") != "hmac-sha256"
        ):
            raise RuntimeError("control journal lacks unique signed audit readiness")
        for kind in ("provider", "chroma"):
            authorized = [
                item
                for item in events
                if item.get("type") == "control.probe.authorized"
                and isinstance(item.get("data"), dict)
                and item["data"].get("kind") == kind
            ]
            reconciled = [
                item
                for item in events
                if item.get("type") == "control.probe.reconciled"
                and isinstance(item.get("data"), dict)
                and item["data"].get("kind") == kind
            ]
            if len(authorized) != 1 or len(reconciled) != 1:
                raise RuntimeError(f"control journal lacks exactly one {kind} probe")
            data = reconciled[0].get("data")
            authorized_ids = authorized[0].get("correlation")
            reconciled_ids = reconciled[0].get("correlation")
            if (
                not isinstance(data, dict)
                or not isinstance(authorized_ids, dict)
                or not isinstance(reconciled_ids, dict)
                or any(
                    not isinstance(reconciled_ids.get(field), str)
                    for field in (
                        "operation_id",
                        "run_id",
                        "audit_event_id",
                        "cost_event_id",
                        "provider_request_id",
                    )
                )
                or authorized_ids.get("operation_id") != reconciled_ids.get("operation_id")
                or authorized_ids.get("run_id") != reconciled_ids.get("run_id")
                or data.get("request_count") != 1
                or data.get("cache_hit") is not False
                or data.get("audit_chain_signed") is not True
                or data.get("cleanup_state") != "committed"
                or (kind == "chroma" and not data.get("connector_request_identity"))
            ):
                raise RuntimeError(f"{kind} probe instrumentation is incomplete")
        return ledger

    @staticmethod
    def _plan_with_durable_control(
        workflow_plan: CampaignPlan,
        control_ledger: CampaignLedger,
        cross_cutting_gate_executor: CrossCuttingGateExecutor,
    ) -> CampaignPlan:
        required_ids = tuple(
            item.criterion_id
            for item in control_ledger.criteria
            if item.criterion_id.startswith("control.")
            or item.criterion_id == "audit.probe-events-instrumented"
        )
        durable = {
            item.criterion_id: item
            for item in control_ledger.criteria
            if item.criterion_id in required_ids
        }

        def resume_result(criterion_ids: tuple[str, ...]):
            def execute(_recorder: object) -> StepResult:
                return StepResult(
                    tuple(
                        CriterionResult(
                            criterion_id,
                            "pass",
                            durable[criterion_id].evidence,
                            durable[criterion_id].note,
                        )
                        for criterion_id in criterion_ids
                    )
                )

            return execute

        control_ids = tuple(item for item in required_ids if item.startswith("control."))
        audit_ids = tuple(item for item in required_ids if item.startswith("audit."))
        catalog_items = original_acceptance_criteria()
        owned = set(required_ids) | {item.criterion_id for item in workflow_plan.criteria}
        remaining_ids = tuple(
            item.criterion_id
            for item in catalog_items
            if item.criterion_id not in owned
            and item.criterion_id != "check.after-workflow-gates"
            and item.criterion_id not in _FINAL_DERIVED_CRITERIA
        )

        def execute_remaining(recorder: ActionRecorder) -> StepResult:
            return cross_cutting_gate_executor.execute(
                phase=Phase.CROSS_CUTTING,
                criterion_ids=remaining_ids,
                recorder=recorder,
            )

        def refuse_check(recorder: ActionRecorder) -> StepResult:
            return cross_cutting_gate_executor.execute(
                phase=Phase.CHECK,
                criterion_ids=("check.after-workflow-gates",),
                recorder=recorder,
            )

        return CampaignPlan(
            criteria=tuple(
                item for item in catalog_items if item.criterion_id not in _FINAL_DERIVED_CRITERIA
            ),
            steps=(
                CampaignStep(
                    "control.durable-readiness",
                    Phase.CONTROL,
                    control_ids,
                    resume_result(control_ids),
                ),
                *workflow_plan.steps,
                CampaignStep(
                    "audit.durable-probe-instrumentation",
                    Phase.CROSS_CUTTING,
                    audit_ids,
                    resume_result(audit_ids),
                ),
                CampaignStep(
                    "campaign.unconfigured-cross-cutting-gates",
                    Phase.CROSS_CUTTING,
                    remaining_ids,
                    execute_remaining,
                ),
                CampaignStep(
                    "check.refuse-incomplete-campaign",
                    Phase.CHECK,
                    ("check.after-workflow-gates",),
                    refuse_check,
                ),
            ),
        )

    @staticmethod
    def _workflow1_fallback_reference(store: EvidenceStore) -> str:
        existing = [
            event
            for event in store.read_events()
            if event.get("type") == "campaign.workflow1-fallback.authorized"
        ]
        if existing:
            if len(existing) != 1 or not isinstance(existing[0].get("data"), dict):
                raise RuntimeError("workflow1 fallback authorization is not unique")
            data = existing[0]["data"]
            if (
                data.get("reason") != WORKFLOW1_FALLBACK_REASON
                or data.get("provider_window") != "shared-project-upper-bound"
            ):
                raise RuntimeError("workflow1 fallback authorization changed")
            return f"events.ndjson#{existing[0]['event_id']}"
        event_id = store.append_event(
            "campaign.workflow1-fallback.authorized",
            {
                "provider_window": "shared-project-upper-bound",
                "reason": WORKFLOW1_FALLBACK_REASON,
                "scope": "workflow1-only",
            },
        )
        return f"events.ndjson#{event_id}"

    @staticmethod
    def _workflow1_fallback_plan(
        workflow_plan: CampaignPlan,
        control_ledger: CampaignLedger,
        evidence_reference: str,
    ) -> CampaignPlan:
        augmented = CampaignEntrypoint._plan_with_durable_control(
            workflow_plan,
            control_ledger,
            _BlockedCrossCuttingGateExecutor(),
        )
        kept = tuple(
            step
            for step in augmented.steps
            if step.phase in {Phase.CONTROL, Phase.WORKFLOW_1}
            or step.step_id == "audit.durable-probe-instrumentation"
        )
        owned = {criterion for step in kept for criterion in step.criterion_ids}
        catalog = original_acceptance_criteria()
        remaining = tuple(
            criterion.criterion_id for criterion in catalog if criterion.criterion_id not in owned
        )

        def blocked_result(criterion_ids: tuple[str, ...]):
            def execute(_recorder: ActionRecorder) -> StepResult:
                return StepResult(
                    tuple(
                        CriterionResult(
                            criterion_id,
                            "blocked",
                            (evidence_reference,),
                            WORKFLOW1_FALLBACK_REASON,
                        )
                        for criterion_id in criterion_ids
                    )
                )

            return execute

        blocking_steps: list[CampaignStep] = []
        for phase, slug in (
            (Phase.WORKFLOW_2, "workflow2"),
            (Phase.WORKFLOW_3, "workflow3"),
            (Phase.CROSS_CUTTING, "later-gates"),
            (Phase.CHECK, "check"),
        ):
            criterion_ids = tuple(
                criterion_id
                for criterion_id in remaining
                if Phase.for_criterion(criterion_id) is phase
            )
            if criterion_ids:
                blocking_steps.append(
                    CampaignStep(
                        f"fallback.block-{slug}",
                        phase,
                        criterion_ids,
                        blocked_result(criterion_ids),
                    )
                )
        return CampaignPlan(criteria=catalog, steps=(*kept, *blocking_steps))

    @staticmethod
    def _completed_workflow1_fallback(
        store: EvidenceStore,
    ) -> CampaignSummary | None:
        completed = [
            event
            for event in store.read_events()
            if event.get("type") == "campaign.workflow1-fallback.completed"
        ]
        if not completed:
            return None
        if len(completed) != 1 or not isinstance(completed[0].get("data"), dict):
            raise RuntimeError("workflow1 fallback completion is not unique")
        ledger = CampaignLedger(store, original_acceptance_criteria())
        statuses = {item.criterion_id: item.status for item in ledger.criteria}
        if any(
            status != "pass"
            for criterion_id, status in statuses.items()
            if criterion_id.startswith("workflow1.")
        ):
            raise RuntimeError("workflow1 fallback completion lacks every workflow1 pass")
        allowed_pass = {
            criterion_id
            for criterion_id in statuses
            if criterion_id.startswith("control.")
            or criterion_id.startswith("workflow1.")
            or criterion_id == "audit.probe-events-instrumented"
        }
        if any(
            status != "blocked"
            for criterion_id, status in statuses.items()
            if criterion_id not in allowed_pass
        ):
            raise RuntimeError("workflow1 fallback did not durably block every later criterion")
        data = completed[0]["data"]
        if data.get("reason") != WORKFLOW1_FALLBACK_REASON:
            raise RuntimeError("workflow1 fallback completion reason changed")
        return CampaignSummary(
            completed=False,
            halted_by=_WORKFLOW1_FALLBACK_HALTED_BY,
            check_ran=False,
            completed_steps=tuple(data.get("completed_steps", ())),
        )

    @staticmethod
    def _verify_final_bundle(store: EvidenceStore) -> None:
        if not store.is_sealed:
            raise RuntimeError("campaign finalizer did not seal the evidence bundle")
        acceptance_path = store.root / "acceptance.json"
        report_path = store.root / "report.md"
        if not acceptance_path.is_file() or not report_path.is_file():
            raise RuntimeError("campaign finalizer omitted acceptance or report output")
        acceptance = json.loads(acceptance_path.read_text(encoding="utf-8"))
        rows = acceptance.get("criteria") if isinstance(acceptance, dict) else None
        expected = [item.criterion_id for item in original_acceptance_criteria()]
        if (
            not isinstance(rows, list)
            or [row.get("criterion_id") for row in rows if isinstance(row, dict)] != expected
            or any(not isinstance(row, dict) or row.get("status") != "pass" for row in rows)
        ):
            raise RuntimeError("sealed acceptance does not prove the complete original catalog")
        checksum_path = store.root / "SHA256SUMS"
        for line in checksum_path.read_text(encoding="utf-8").splitlines():
            digest, separator, relative = line.partition("  ")
            if not separator or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise RuntimeError("sealed checksum manifest is malformed")
            target = store.root / relative
            if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise RuntimeError("sealed evidence checksum verification failed")
        store.scan_recursive()

    def run(self, *, dry_run: bool = False) -> CampaignEntrypointResult:
        campaign, inert_execution = self._load_and_validate()
        if dry_run:
            return CampaignEntrypointResult(
                mode="dry-run",
                campaign_id=campaign.campaign_id,
                evidence_bundle=self.evidence_bundle,
            )
        self._require_live_authority(campaign)

        if not self.evidence_bundle.is_dir():
            raise RuntimeError("live campaign requires a pre-existing evidence bundle")
        event_log = self.evidence_bundle / "events.ndjson"
        resuming = event_log.is_file() and event_log.stat().st_size > 0
        store = EvidenceStore(self.evidence_bundle)
        store.scan_recursive()
        control_ledger = self._require_control_acceptance(campaign, store)
        if self.stop_after_workflow1:
            resumed_fallback = self._completed_workflow1_fallback(store)
            if resumed_fallback is not None:
                return CampaignEntrypointResult(
                    mode="workflow1-fallback",
                    campaign_id=campaign.campaign_id,
                    evidence_bundle=self.evidence_bundle,
                    summary=resumed_fallback,
                )
        if store.is_sealed:
            self._verify_final_bundle(store)
            stage_events = [
                event
                for event in store.read_events()
                if event.get("type") == "campaign.stage.completed"
            ]
            data = stage_events[-1].get("data") if stage_events else {}
            data = data if isinstance(data, dict) else {}
            return CampaignEntrypointResult(
                mode="live",
                campaign_id=campaign.campaign_id,
                evidence_bundle=self.evidence_bundle,
                summary=CampaignSummary(
                    completed=True,
                    halted_by=None,
                    check_ran=bool(data.get("check_ran", True)),
                    completed_steps=tuple(data.get("completed_steps", ())),
                ),
            )
        fallback_reference = (
            self._workflow1_fallback_reference(store) if self.stop_after_workflow1 else None
        )
        campaign_plan = (
            self._workflow1_fallback_plan(
                inert_execution.plan,
                control_ledger,
                fallback_reference,
            )
            if fallback_reference is not None
            else self._plan_with_durable_control(
                inert_execution.plan,
                control_ledger,
                self.cross_cutting_gate_executor,
            )
        )
        store.append_event(
            "campaign.entrypoint.started",
            {
                "campaign_id": campaign.campaign_id,
                "deployment_count": len(self.endpoints.deployment_base_urls),
                "negative_evaluator_coverage": {
                    "workflow1": "implemented",
                    "workflow2": "implemented_controller_required",
                    "workflow3": "implemented_transport_controller_required",
                },
                "resuming": resuming,
            },
        )
        database: Any = None
        client: httpx.Client | None = None
        supervisor: _Supervisor | None = None
        scenario_server: OwnedScenarioControllerServer | None = None
        summary: CampaignSummary | None = None
        cleanup_failures: list[str] = []
        try:
            events = store.read_events() if resuming else ()
            if resuming and any(event.get("type") == "campaign.terminated" for event in events):
                summary = CampaignCoordinator(store, campaign_plan).run()
                return CampaignEntrypointResult(
                    mode="live",
                    campaign_id=campaign.campaign_id,
                    evidence_bundle=self.evidence_bundle,
                    summary=summary,
                )
            stage_events = [
                event for event in events if event.get("type") == "campaign.stage.completed"
            ]
            if stage_events:
                stage_data = stage_events[-1].get("data")
                stage_data = stage_data if isinstance(stage_data, dict) else {}
                summary = CampaignSummary(
                    completed=True,
                    halted_by=None,
                    check_ran=bool(stage_data.get("check_ran")),
                    completed_steps=tuple(stage_data.get("completed_steps", ())),
                )
            else:
                database = self.database_factory()
                client = self.client_factory()
                publisher = self.publisher_factory(database)
                supervisor = (
                    self.supervisor_factory(store)
                    if self.supervisor_factory is not None
                    else LocalDeploymentSupervisor(
                        campaign_config=self.campaign_config_path,
                        evidence_store=store,
                        environment=self.service_environment,
                        working_directory=self.repository_root,
                        workspace_id=self.workspace_id,
                    )
                )
                api_key = self.api_key
                workflow1_controller = self.workflow1_scenario_controller
                workflow2_controller = self.workflow2_scenario_controller
                workflow3_controller = self.workflow3_scenario_controller
                if self.compose_local_controllers:
                    if any(
                        controller is not None
                        for controller in (
                            workflow1_controller,
                            workflow2_controller,
                            workflow3_controller,
                        )
                    ):
                        raise ValueError(
                            "local controller composition cannot replace injected controllers"
                        )
                    if self.scenario_controller_url is None:
                        raise ValueError("live local controllers require --scenario-controller-url")
                    if self.chroma_collection_name is None:
                        raise ValueError("live local controllers require --chroma-collection")
                    chroma_collection = self.chroma_collection_factory(
                        self.chroma_url,
                        self.chroma_collection_name,
                    )
                    refresh_producer = (
                        BoundedRefreshEvidenceProducer(
                            frontend_root=self.frontend_root,
                            environment=self.browser_environment,
                        )
                        if self.frontend_root is not None
                        else None
                    )
                    self.local_controller_composition = compose_local_campaign_controllers(
                        campaign=campaign,
                        endpoints=self.endpoints,
                        evidence_store=store,
                        environment=self.controller_environment,
                        api_key_env=self.api_key_env,
                        controller_key_env=self.controller_key_env,
                        scenario_controller_url=self.scenario_controller_url,
                        chroma_collection=chroma_collection,
                        refresh_producer=refresh_producer,
                        chroma_connector_ref=self.chroma_connector_ref,
                        client=client,
                        supervisor=supervisor,
                    )
                    scenario_server = OwnedScenarioControllerServer(
                        app=self.local_controller_composition.scenario_app,
                        base_url=self.scenario_controller_url,
                    )
                    scenario_server.start()
                    self.local_controller_composition.require_scenario_controller_ready(client)
                    api_key = self.local_controller_composition.api_key
                    workflow1_controller = self.local_controller_composition.workflow1_controller
                    workflow2_controller = self.local_controller_composition.workflow2_controller
                    workflow3_controller = self.local_controller_composition.workflow3_controller
                backend = HttpCampaignExecutionBackend(
                    config=HttpBackendConfig(
                        console_base_url=self.endpoints.console_base_url,
                        deployment_base_urls=dict(self.endpoints.deployment_base_urls),
                        campaign_id=campaign.campaign_id,
                        local_fault_control_url=self.endpoints.fault_control_url,
                        provider_execution_enabled=True,
                        provider_acknowledgement=self.options.paid_acknowledgement,
                    ),
                    client=client,
                    publisher=publisher,
                    evaluator=self.evaluator,
                    contracts=inert_execution.contracts,
                    tenant_id=campaign.tenant_id,
                    workspace_id=self.workspace_id,
                    api_key=api_key,
                    supervisor=supervisor,
                    workflow1_scenario_controller=workflow1_controller,
                    workflow2_scenario_controller=workflow2_controller,
                    workflow3_scenario_controller=workflow3_controller,
                )
                execution = build_campaign_execution(inert_execution.settings, backend=backend)
                execution_plan = (
                    self._workflow1_fallback_plan(
                        execution.plan,
                        control_ledger,
                        fallback_reference,
                    )
                    if fallback_reference is not None
                    else self._plan_with_durable_control(
                        execution.plan,
                        control_ledger,
                        self.cross_cutting_gate_executor,
                    )
                )
                summary = CampaignCoordinator(
                    store,
                    execution_plan,
                    campaign_terminal=False,
                    enforce_check_gate=fallback_reference is None,
                    emit_completion_event=fallback_reference is None,
                ).run()
                if fallback_reference is not None and summary.completed:
                    event_id = store.append_event(
                        "campaign.workflow1-fallback.completed",
                        {
                            "completed_steps": list(summary.completed_steps),
                            "provider_window": "shared-project-upper-bound",
                            "reason": WORKFLOW1_FALLBACK_REASON,
                            "workflow1_all_gates_passed": True,
                        },
                    )
                    _ = event_id
                    summary = CampaignSummary(
                        completed=False,
                        halted_by=_WORKFLOW1_FALLBACK_HALTED_BY,
                        check_ran=False,
                        completed_steps=summary.completed_steps,
                    )
        except BaseException as exc:
            store.append_event(
                "campaign.entrypoint.exception",
                {"exception_type": type(exc).__name__},
            )
            raise
        finally:
            if scenario_server is not None:
                try:
                    scenario_server.stop()
                except BaseException:
                    cleanup_failures.append("scenario_controller")
            if supervisor is not None:
                try:
                    supervisor.stop_all()
                except BaseException:
                    cleanup_failures.append("deployment_processes")
            if client is not None:
                try:
                    client.close()
                except BaseException:
                    cleanup_failures.append("http_client")
            close = getattr(database, "close", None)
            if callable(close):
                try:
                    result = close()
                    if asyncio.iscoroutine(result):
                        asyncio.run(result)
                except BaseException:
                    cleanup_failures.append("database")
            store.append_event(
                "campaign.entrypoint.finished",
                {
                    "cleanup_failures": cleanup_failures,
                    "workflow_and_gate_stage_completed": bool(summary and summary.completed),
                },
            )
        if summary is not None and summary.completed:
            if self.evidence_finalizer is None:
                store.append_event(
                    "campaign.finalization.blocked",
                    {"reason": "evidence_finalizer_not_configured"},
                )
                summary = CampaignSummary(
                    completed=False,
                    halted_by="finalization.required",
                    check_ran=summary.check_ran,
                    completed_steps=summary.completed_steps,
                )
            else:
                full_ledger = CampaignLedger(store, original_acceptance_criteria())
                self.evidence_finalizer.finalize(store=store, ledger=full_ledger)
                self._verify_final_bundle(store)
        return CampaignEntrypointResult(
            mode="workflow1-fallback" if self.stop_after_workflow1 else "live",
            campaign_id=campaign.campaign_id,
            evidence_bundle=self.evidence_bundle,
            summary=summary,
        )


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line composition root; execution is opt-in."""
    parser = argparse.ArgumentParser(prog="python -m release.live_evaluation.campaign_entrypoint")
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--campaign-config", type=Path, required=True)
    parser.add_argument("--evidence-bundle", type=Path, required=True)
    parser.add_argument("--console-url", required=True)
    parser.add_argument(
        "--console-deployment-ref",
        default="evaluation-bootstrap",
        help="deployment reference served by the console/control API",
    )
    parser.add_argument("--deployment-url", action="append", default=[], metavar="REF=URL")
    parser.add_argument("--fault-control-url", required=True)
    parser.add_argument("--workspace-id")
    parser.add_argument("--chroma-connector-ref", default="chroma")
    parser.add_argument("--api-key-env", default="ZEROTH_EVALUATION_API_KEY")
    parser.add_argument(
        "--controller-key-env",
        default="ZEROTH_EVALUATION_FAULT_CONTROLLER_KEY",
    )
    parser.add_argument("--scenario-controller-url")
    parser.add_argument("--chroma-url", default="http://127.0.0.1:8121")
    parser.add_argument("--chroma-collection")
    parser.add_argument("--frontend-root", type=Path)
    parser.add_argument("--playwright-root", type=Path)
    parser.add_argument(
        "--produce-playwright",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    parser.add_argument("--browser-base-url", default="http://127.0.0.1:3000")
    parser.add_argument("--playwright-timeout-seconds", type=int, default=1800)
    parser.add_argument("--reconciliation-snapshot", type=Path)
    parser.add_argument("--reconciliation-command")
    parser.add_argument("--reconciliation-timeout-seconds", type=int, default=300)
    parser.add_argument("--econ-db", type=Path)
    parser.add_argument("--action-sink-db", type=Path)
    parser.add_argument("--provider-window", type=Path)
    parser.add_argument(
        "--audit-deployment",
        action="append",
        default=[],
        help="additional REF=URL audit sources, including control/bootstrap deployments",
    )
    parser.add_argument("--handoff-discrepancies", type=Path)
    parser.add_argument("--handoff-rollback", type=Path)
    parser.add_argument("--handoff-project-model", type=Path)
    parser.add_argument("--check-config", type=Path)
    parser.add_argument("--check-report-dir", type=Path)
    parser.add_argument("--check-timeout-seconds", type=int, default=1800)
    parser.add_argument(
        "--local-controllers",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="compose campaign-local runtime/scenario controllers in live mode",
    )
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--allow-mutation", action="store_true")
    parser.add_argument("--mutation-ack")
    parser.add_argument("--allow-provider", action="store_true")
    parser.add_argument("--paid-ack")
    parser.add_argument(
        "--stop-after-workflow1",
        action="store_true",
        help=(
            "execute control and Workflow 1 only, then durably block every later "
            "criterion under the authorized provider-window fallback"
        ),
    )
    return parser


def _deployment_urls(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        reference, separator, url = value.partition("=")
        if not separator or not reference or not url or reference in result:
            raise ValueError("deployment URLs must be unique REF=URL pairs")
        result[reference] = url
    return result


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    raw_campaign = CampaignConfig.model_validate(
        json.loads(args.campaign_config.read_text(encoding="utf-8"))
    )
    environment = dict(os.environ)
    environment.update(
        {
            "ZEROTH_EVALUATION_ALLOW_MUTATION": "true" if args.allow_mutation else "false",
            "ZEROTH_EVALUATION_ALLOW_PROVIDER": "true" if args.allow_provider else "false",
            "ZEROTH_EVALUATION_CAMPAIGN_ID": raw_campaign.campaign_id,
            "ZEROTH_EVALUATION_ARTIFACT_ROOT": str(raw_campaign.artifact_root),
            "ZEROTH_EVALUATION_ACTION_SINK_ROOT": str(raw_campaign.action_sink_root),
        }
    )
    if args.mutation_ack is not None:
        environment["ZEROTH_EVALUATION_MUTATION_ACK"] = args.mutation_ack
    if args.paid_ack is not None:
        environment["ZEROTH_EVALUATION_PAID_ACK"] = args.paid_ack
    options = load_live_execution_options(environment, repository_root=args.repository_root)
    browser_names = {
        "ZEROTH_EVALUATION_API_BASE",
        "ZEROTH_EVALUATION_TENANT",
        "ZEROTH_EVALUATION_FAULT_CONTROLLER_URL",
        "ZEROTH_EVALUATION_WORKFLOW2_ID",
        "ZEROTH_EVALUATION_WORKFLOW2_GRAPH_VERSION",
        "ZEROTH_EVALUATION_WORKFLOW2_DEPLOYMENT_REF",
        "ZEROTH_EVALUATION_WORKFLOW3_ID",
        "ZEROTH_EVALUATION_WORKFLOW3_GRAPH_VERSION",
        "ZEROTH_EVALUATION_WORKFLOW3_DEPLOYMENT_REF",
    }
    browser_environment = {name: environment[name] for name in browser_names if name in environment}
    if args.execute and args.api_key_env in environment:
        browser_environment["ZEROTH_EVALUATION_API_KEY"] = environment[args.api_key_env]
    if args.execute and args.controller_key_env in environment:
        browser_environment["ZEROTH_EVALUATION_FAULT_CONTROLLER_KEY"] = environment[
            args.controller_key_env
        ]
    cross_cutting = None
    finalizer = None
    if args.execute:
        store = EvidenceStore(args.evidence_bundle)
        playwright_root = (
            args.playwright_root
            or raw_campaign.artifact_root / "browser" / raw_campaign.campaign_id
        ).resolve(strict=False)
        playwright_producer = (
            BoundedPlaywrightProducer(
                artifact_root=playwright_root,
                command=("npm", "exec", "playwright", "test"),
                working_directory=args.repository_root / "frontend",
                timeout_seconds=args.playwright_timeout_seconds,
                environment={
                    "PLAYWRIGHT_NO_SERVER": "1",
                    "ZEROTH_EVALUATION_LIVE": "1",
                    "ZEROTH_EVALUATION_BASE_URL": args.browser_base_url,
                    "ZEROTH_EVALUATION_API_BASE": args.console_url,
                    "ZEROTH_EVALUATION_TENANT": raw_campaign.tenant_id,
                },
            )
            if args.produce_playwright
            else None
        )
        reconciliation_source = (
            args.reconciliation_snapshot
            or raw_campaign.artifact_root / "reconciliation" / f"{raw_campaign.campaign_id}.json"
        )
        reconciliation_command = (
            tuple(shlex.split(args.reconciliation_command))
            if args.reconciliation_command
            else (
                "uv",
                "run",
                "python",
                "-m",
                "release.live_evaluation.reconciliation_export",
                "--econ-db",
                str(args.econ_db),
                "--action-sink-db",
                str(args.action_sink_db or raw_campaign.action_sink_root / "actions.sqlite3"),
                "--provider-window",
                str(
                    args.provider_window
                    or raw_campaign.artifact_root
                    / "reconciliation"
                    / f"{raw_campaign.campaign_id}.provider-window.json"
                ),
                "--events",
                str(args.evidence_bundle / "events.ndjson"),
                "--output",
                str(reconciliation_source),
                "--campaign",
                raw_campaign.campaign_id,
                "--tenant",
                raw_campaign.tenant_id,
                "--api-key-env",
                args.api_key_env,
                *(
                    item
                    for deployment in (
                        *args.deployment_url,
                        f"{args.console_deployment_ref}={args.console_url}",
                        *args.audit_deployment,
                    )
                    for item in ("--deployment", deployment)
                ),
            )
            if args.econ_db is not None
            else None
        )
        reconciliation_producer = (
            BoundedSnapshotProducer(
                output_path=reconciliation_source,
                command=reconciliation_command,
                working_directory=args.repository_root,
                timeout_seconds=args.reconciliation_timeout_seconds,
                environment={
                    "ZEROTH_EVALUATION_CAMPAIGN_ID": raw_campaign.campaign_id,
                    "ZEROTH_EVALUATION_TENANT": raw_campaign.tenant_id,
                },
            )
            if reconciliation_command is not None
            else None
        )
        check_config = args.check_config or args.repository_root / "zeroth-check.yaml"
        check_report_dir = (
            args.check_report_dir or raw_campaign.artifact_root / "check" / raw_campaign.campaign_id
        )
        sources = CrossCuttingSources(
            playwright_root=playwright_root if playwright_producer is None else None,
            playwright_producer=playwright_producer,
            reconciliation_collector=CampaignSnapshotCollector(
                source=reconciliation_source,
                campaign_id=raw_campaign.campaign_id,
                tenant_id=raw_campaign.tenant_id,
                producer=reconciliation_producer,
            ),
            handoff_documents={
                "handoff.discrepancy-register": (
                    args.handoff_discrepancies
                    or raw_campaign.artifact_root / "handoff" / "discrepancies.md"
                ),
                "handoff.execution-and-rollback-instructions": (
                    args.handoff_rollback
                    or raw_campaign.artifact_root / "handoff" / "execution-and-rollback.md"
                ),
                "handoff.project-model-updated": (
                    args.handoff_project_model or args.repository_root / "PROJECT_MODEL.md"
                ),
            },
        )
        cross_cutting = EvidenceFirstCrossCuttingGateExecutor(
            store,
            sources,
            check_runner=BoundedZerothCheckRunner(
                command=(
                    "uv",
                    "run",
                    "zeroth-core",
                    "check",
                    "run",
                    "--config",
                    str(check_config),
                    "--report-dir",
                    str(check_report_dir),
                ),
                working_directory=args.repository_root,
                timeout_seconds=args.check_timeout_seconds,
            ),
        )
        finalizer = EvidenceFirstCampaignFinalizer()
    result = CampaignEntrypoint(
        repository_root=args.repository_root,
        campaign_config_path=args.campaign_config,
        evidence_bundle=args.evidence_bundle,
        endpoints=CampaignEndpoints(
            console_base_url=args.console_url,
            deployment_base_urls=_deployment_urls(args.deployment_url),
            fault_control_url=args.fault_control_url,
        ),
        options=options,
        workspace_id=args.workspace_id,
        chroma_connector_ref=args.chroma_connector_ref,
        compose_local_controllers=args.local_controllers and args.execute,
        controller_environment=(
            {
                name: environment[name]
                for name in (args.api_key_env, args.controller_key_env)
                if name in environment
            }
            if args.execute
            else None
        ),
        api_key_env=args.api_key_env,
        controller_key_env=args.controller_key_env,
        scenario_controller_url=args.scenario_controller_url,
        chroma_url=args.chroma_url,
        chroma_collection_name=args.chroma_collection,
        frontend_root=(
            args.frontend_root or args.repository_root / "frontend" if args.execute else None
        ),
        browser_environment=browser_environment if args.execute else None,
        cross_cutting_gate_executor=cross_cutting,
        evidence_finalizer=finalizer,
        stop_after_workflow1=args.stop_after_workflow1,
    ).run(dry_run=not args.execute)
    print(
        json.dumps(
            {
                "campaign_id": result.campaign_id,
                "completed": result.summary.completed if result.summary else None,
                "evidence_bundle": str(result.evidence_bundle),
                "mode": result.mode,
            },
            sort_keys=True,
        )
    )
    return 0 if result.summary is None or result.summary.completed else 2


if __name__ == "__main__":
    raise SystemExit(main())
