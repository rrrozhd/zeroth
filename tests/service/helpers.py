from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from pydantic import BaseModel

from zeroth.contracts import ContractRegistry
from zeroth.deployments import DeploymentService, SQLiteDeploymentRepository
from zeroth.graph import (
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
from zeroth.runs import Run
from zeroth.service.bootstrap import bootstrap_app, bootstrap_service


class RunInputPayload(BaseModel):
    value: int


class RunInputPayloadV2(BaseModel):
    value: int
    request_id: str


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


def deploy_service(
    sqlite_db,
    graph: Graph,
    *,
    deployment_ref: str = "service-run-api",
    extra_contract_models: dict[str, type[BaseModel]] | None = None,
):
    graph_repository = GraphRepository(sqlite_db)
    contract_registry = ContractRegistry(sqlite_db)
    contract_registry.register(RunInputPayload, name="contract://input")
    contract_registry.register(RunInputPayload, name="contract://output")
    for contract_ref, model in (extra_contract_models or {}).items():
        contract_registry.register(model, name=contract_ref)
    graph = graph_repository.create(graph)
    graph_repository.publish(graph.graph_id, graph.version)
    deployment_service = DeploymentService(
        graph_repository=graph_repository,
        deployment_repository=SQLiteDeploymentRepository(sqlite_db),
        contract_registry=contract_registry,
    )
    deployment = deployment_service.deploy(deployment_ref, graph.graph_id, graph.version)
    service = bootstrap_service(sqlite_db, deployment_ref=deployment.deployment_ref)
    return service, deployment


def service_app(sqlite_db, deployment_ref: str, service):
    app = bootstrap_app(sqlite_db, deployment_ref=deployment_ref)
    app.state.bootstrap = service
    return app


def bootstrap_only_app(sqlite_db, deployment_ref: str):
    return bootstrap_app(sqlite_db, deployment_ref=deployment_ref)


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
    )


def wait_for(predicate, *, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("timed out waiting for condition")
