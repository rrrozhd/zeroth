from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import warnings
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from pydantic import BaseModel

from zeroth.contracts.graph import (
    AgentNode,
    AgentNodeData,
    DisplayMetadata,
    Edge,
    ExecutionSettings,
    Graph,
    GraphRepository,
    HumanApprovalNode,
    HumanApprovalNodeData,
)
from zeroth.contracts.registry import ContractRegistry, contract_scope_context
from zeroth.governance.identity import ServiceRole
from zeroth.runtime.runs import Run
from zeroth.service.api.authentication import ServiceAuthConfig, StaticApiKeyCredential
from zeroth.service.bootstrap import bootstrap_app
from zeroth.service.bootstrap.factory import bootstrap_service
from zeroth.service.deployments import DeploymentService, SQLiteDeploymentRepository


class RunInputPayload(BaseModel):
    value: int


class RunInputPayloadV2(BaseModel):
    value: int
    request_id: str


TEST_API_KEYS = {
    "operator": "test-operator-key",
    "reviewer": "test-reviewer-key",
    "admin": "test-admin-key",
}


def default_service_auth_config():
    from zeroth.governance.identity import ServiceRole
    from zeroth.service.api.authentication import ServiceAuthConfig, StaticApiKeyCredential

    return ServiceAuthConfig(
        api_keys=[
            StaticApiKeyCredential(
                credential_id="operator-key",
                secret=TEST_API_KEYS["operator"],
                subject="operator-1",
                roles=[ServiceRole.OPERATOR],
                tenant_id="default",
                workspace_id=None,
            ),
            StaticApiKeyCredential(
                credential_id="reviewer-key",
                secret=TEST_API_KEYS["reviewer"],
                subject="reviewer-1",
                roles=[ServiceRole.REVIEWER],
                tenant_id="default",
                workspace_id=None,
            ),
            StaticApiKeyCredential(
                credential_id="admin-key",
                secret=TEST_API_KEYS["admin"],
                subject="admin-1",
                roles=[ServiceRole.ADMIN],
                tenant_id="default",
                workspace_id=None,
            ),
        ]
    )


def operator_headers() -> dict[str, str]:
    return {"X-API-Key": TEST_API_KEYS["operator"]}


def reviewer_headers() -> dict[str, str]:
    return {"X-API-Key": TEST_API_KEYS["reviewer"]}


def admin_headers() -> dict[str, str]:
    return {"X-API-Key": TEST_API_KEYS["admin"]}


def scoped_auth_config(
    *credentials: tuple[str, str, ServiceRole, str, str | None],
) -> ServiceAuthConfig:
    return ServiceAuthConfig(
        api_keys=[
            StaticApiKeyCredential(
                credential_id=credential_id,
                secret=secret,
                subject=credential_id,
                roles=[role],
                tenant_id=tenant_id,
                workspace_id=workspace_id,
            )
            for credential_id, secret, role, tenant_id, workspace_id in credentials
        ]
    )


def api_key_headers(secret: str) -> dict[str, str]:
    return {"X-API-Key": secret}


@dataclass(slots=True)
class BlockingAgentRunner:
    started: threading.Event
    release: threading.Event
    output_data: dict[str, Any]

    async def run(
        self,
        input_payload: Any,
        *,
        thread_id: str | None = None,
        runtime_context: dict[str, Any] | None = None,
    ) -> SimpleNamespace:
        self.started.set()
        await asyncio.to_thread(self.release.wait)
        return SimpleNamespace(
            output_data=dict(self.output_data),
            audit_record={
                "thread_id": thread_id,
                "runtime_context": dict(runtime_context or {}),
            },
        )


@dataclass(slots=True)
class FailingAgentRunner:
    started: threading.Event

    async def run(
        self,
        input_payload: Any,
        *,
        thread_id: str | None = None,
        runtime_context: dict[str, Any] | None = None,
    ) -> SimpleNamespace:
        self.started.set()
        raise RuntimeError("boom")


@dataclass(slots=True)
class CountingFinishRunner:
    """Deterministic downstream runner used to prove resume behavior."""

    call_count: int = 0
    last_input: dict[str, Any] | None = None

    async def run(
        self,
        input_payload: Any,
        *,
        thread_id: str | None = None,
        runtime_context: dict[str, Any] | None = None,
    ) -> SimpleNamespace:
        self.call_count += 1
        self.last_input = dict(input_payload)
        return SimpleNamespace(
            output_data={"value": int(input_payload["value"]) + 1},
            audit_record={
                "thread_id": thread_id,
                "runtime_context": dict(runtime_context or {}),
            },
        )


async def deploy_service(
    sqlite_db,
    graph: Graph,
    *,
    deployment_ref: str = "service-run-api",
    extra_contract_models: dict[str, type[BaseModel]] | None = None,
    auth_config=None,
    tenant_id: str = "default",
    workspace_id: str | None = None,
):
    graph_repository = GraphRepository(sqlite_db)
    contract_registry = ContractRegistry.scoped(
        sqlite_db,
        contract_scope_context(tenant_id, workspace_id),
    )
    await contract_registry.register(RunInputPayload, name="contract://input")
    await contract_registry.register(RunInputPayload, name="contract://output")
    for contract_ref, model in (extra_contract_models or {}).items():
        await contract_registry.register(model, name=contract_ref)
    graph = graph.model_copy(update={"tenant_id": tenant_id, "workspace_id": workspace_id})
    graph = await graph_repository.create(graph, tenant_id=tenant_id, workspace_id=workspace_id)
    await graph_repository.publish(
        graph.graph_id,
        graph.version,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
    )
    deployment_service = DeploymentService(
        graph_repository=graph_repository,
        deployment_repository=SQLiteDeploymentRepository(sqlite_db),
        contract_registry=contract_registry,
    )
    deployment = await deployment_service.deploy(
        deployment_ref,
        graph.graph_id,
        graph.version,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
    )
    service = await bootstrap_service(
        sqlite_db,
        deployment_ref=deployment.deployment_ref,
        tenant_id=deployment.tenant_id,
        workspace_id=deployment.workspace_id,
        auth_config=auth_config or default_service_auth_config(),
    )
    return service, deployment


async def service_app(sqlite_db, deployment_ref: str, service, *, auth_config=None):
    app = await bootstrap_app(
        sqlite_db,
        deployment_ref=deployment_ref,
        auth_config=auth_config or default_service_auth_config(),
    )
    app.state.bootstrap = service
    return app


async def bootstrap_only_app(sqlite_db, deployment_ref: str, *, auth_config=None):
    return await bootstrap_app(
        sqlite_db,
        deployment_ref=deployment_ref,
        auth_config=auth_config or default_service_auth_config(),
    )


def agent_graph(*, graph_id: str, node_id: str = "agent-step") -> Graph:
    return Graph(
        graph_id=graph_id,
        name="Run API Graph",
        version=1,
        entry_step=node_id,
        execution_settings=ExecutionSettings(max_total_steps=5),
        nodes=[
            AgentNode(
                node_id=node_id,
                graph_version_ref=f"{graph_id}@1",
                display=DisplayMetadata(title="Agent step"),
                input_contract_ref="contract://input",
                output_contract_ref="contract://output",
                agent=AgentNodeData(
                    instruction="echo",
                    model_provider="provider://demo",
                ),
            )
        ],
        edges=[],
    )


def approval_graph(*, graph_id: str, node_id: str = "approval-step") -> Graph:
    return Graph(
        graph_id=graph_id,
        name="Approval API Graph",
        version=1,
        entry_step=node_id,
        execution_settings=ExecutionSettings(max_total_steps=5),
        nodes=[
            HumanApprovalNode(
                node_id=node_id,
                graph_version_ref=f"{graph_id}@1",
                display=DisplayMetadata(title="Approval step"),
                input_contract_ref="contract://input",
                output_contract_ref="contract://output",
                human_approval=HumanApprovalNodeData(
                    resolution_schema_ref="schema://resolution",
                    approval_policy_config={"allow_edits": True},
                ),
            )
        ],
        edges=[],
    )


def approval_resume_graph(*, graph_id: str) -> Graph:
    return Graph(
        graph_id=graph_id,
        name="Approval Resume Graph",
        version=1,
        entry_step="approval-step",
        execution_settings=ExecutionSettings(max_total_steps=5),
        nodes=[
            HumanApprovalNode(
                node_id="approval-step",
                graph_version_ref=f"{graph_id}@1",
                display=DisplayMetadata(title="Approval step"),
                input_contract_ref="contract://input",
                output_contract_ref="contract://output",
                human_approval=HumanApprovalNodeData(
                    resolution_schema_ref="schema://resolution",
                    approval_policy_config={"allow_edits": True},
                ),
            ),
            AgentNode(
                node_id="finish-step",
                graph_version_ref=f"{graph_id}@1",
                display=DisplayMetadata(title="Finish step"),
                input_contract_ref="contract://input",
                output_contract_ref="contract://output",
                agent=AgentNodeData(
                    instruction="finish",
                    model_provider="provider://finish",
                ),
            ),
        ],
        edges=[
            Edge(
                edge_id="edge-1",
                source_node_id="approval-step",
                target_node_id="finish-step",
            )
        ],
    )


def build_run_for_service(service) -> Run:
    return Run(
        graph_version_ref=service.deployment.graph_version_ref,
        deployment_ref=service.deployment.deployment_ref,
        tenant_id=service.deployment.tenant_id,
        workspace_id=service.deployment.workspace_id,
    )


#: Where a slow wait is recorded so it survives a noisy run. The gate already emits ~177
#: warnings, so a warning alone is easy to miss -- and this record is the only thing that
#: will describe ZER-21's excursion when it next happens. Override with
#: ``ZEROTH_SLOW_WAIT_LOG``; ``.autopilot/`` is already git-ignored.
SLOW_WAIT_LOG_ENV = "ZEROTH_SLOW_WAIT_LOG"
_DEFAULT_SLOW_WAIT_LOG = ".autopilot/slow-waits.jsonl"


def slow_wait_log_path() -> Path:
    """Resolve the durable slow-wait record's location."""
    return Path(os.environ.get(SLOW_WAIT_LOG_ENV, _DEFAULT_SLOW_WAIT_LOG))


def _report_slow_wait(elapsed: float, polls: int, slow_after: float, describe) -> None:
    """Surface a slow-but-successful wait loudly and durably.

    Measured under load these conditions hold in 0.632-0.917 s, so anything past
    ``slow_after`` is far outside the working distribution. A generous hang deadline must
    not absorb such an excursion silently -- identifying one is the open question in
    ZER21-AUD-001, and the excursion has never been captured with state attached.
    """
    observed = _safe_describe(describe) if describe is not None else None
    message = (
        f"wait_for was satisfied only after {elapsed:.3f}s ({polls} polls), "
        f"well beyond the {slow_after:.1f}s expected range"
        + (f"; observed {observed}" if observed else "")
    )
    warnings.warn(message, stacklevel=3)

    record = {
        "elapsed": round(elapsed, 3),
        "polls": polls,
        "slow_after": slow_after,
        "observed": observed,
    }
    try:
        path = slow_wait_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except Exception:  # noqa: BLE001 - recording must never fail the wait it describes
        pass


def _safe_describe(describe) -> str:
    """Render caller state for a diagnostic; never let it mask what it is describing."""
    try:
        return str(describe())
    except Exception as exc:  # noqa: BLE001 - diagnostics must not mask the real failure
        return f"state unavailable ({exc!r})"


def wait_for(predicate, *, timeout: float = 30.0, describe=None, slow_after: float = 3.0) -> None:
    """Poll ``predicate`` until it holds, or fail once ``timeout`` elapses.

    The timeout is a **hang deadline**, not an expected latency. It used to be 3 s.
    Measured under load this condition holds in 0.632-0.917 s, so 3 s was *outside* the
    working distribution -- but only about six worker poll ticks (~0.51 s each), close
    enough that an excursion crossed it roughly 1 run in 50 under sustained CPU load,
    always with "timed out waiting for condition" rather than a wrong value.

    What produces that excursion is **not identified**. Probes eliminated event-loop
    blocking, poll starvation and storage latency, but every probe sampled a
    *non-crossing* run, so they explain normal latency and not the outlier (ZER21-AUD-001).

    Raising it costs nothing when things are healthy: the loop returns the moment the
    condition holds. It removes the same race from every caller -- 8 modules, 25 call
    sites, none of which pass an explicit timeout.

    The cost is real and worth stating: a genuinely stuck condition now takes 30 s rather
    than 3 s to fail. The suite runs with ``-x``, so that is paid once per run, which is
    the trade being made. Pass ``timeout=`` at a call site that wants a tighter bound.
    """
    started = time.monotonic()
    deadline = started + timeout
    polls = 0
    while time.monotonic() < deadline:
        polls += 1
        if predicate():
            elapsed = time.monotonic() - started
            if elapsed > slow_after:
                _report_slow_wait(elapsed, polls, slow_after, describe)
            return
        time.sleep(0.01)

    # Report what was actually observed. A bare "timed out" says only that a deadline
    # elapsed, which is exactly what left ZER-21's one captured failure undiagnosable:
    # it could not distinguish a slow arrival from a stalled or already-failed run.
    elapsed = time.monotonic() - started
    detail = f"; observed {_safe_describe(describe)}" if describe is not None else ""
    raise AssertionError(
        f"timed out waiting for condition after {elapsed:.3f}s ({polls} polls){detail}"
    )
