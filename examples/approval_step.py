"""Add a human approval step to a node — runnable example for docs/how-to/cookbook/approval-step.md.

In-process demo that builds a tiny graph with an agent, an approval gate,
and a tool, then shows a caller how to drive the orchestrator, inspect a
pending approval via ``ApprovalService.list_pending``, and resolve it via
``ApprovalService.resolve``. Uses deterministic stub runners so the script
runs without any LLM credentials.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from pydantic import BaseModel


class DemoPayload(BaseModel):
    message: str = ""


class _EchoAgentRunner:
    async def run(
        self,
        input_payload: Any,
        *,
        thread_id: str | None = None,
        runtime_context: Any = None,  # noqa: ARG002
    ) -> SimpleNamespace:
        return SimpleNamespace(
            output_data={"message": f"agent-echo: {input_payload.get('message', '')}"},
            audit_record={"thread_id": thread_id},
        )


class _EchoUnitRunner:
    async def run(self, manifest_ref: str, input_payload: Any) -> SimpleNamespace:  # noqa: ARG002
        return SimpleNamespace(
            output_data=dict(input_payload) if isinstance(input_payload, dict) else {},
            audit_record={"manifest_ref": manifest_ref},
        )


async def _run_demo() -> int:
    from zeroth.core.approvals.models import ApprovalDecision
    from zeroth.core.contracts import ContractRegistry
    from zeroth.core.deployments import DeploymentService, SQLiteDeploymentRepository
    from zeroth.core.examples.quickstart import build_demo_graph
    from zeroth.core.graph import GraphRepository
    from zeroth.core.identity import ActorIdentity
    from zeroth.core.identity.models import AuthMethod, ServiceRole
    from zeroth.core.runs import RunStatus
    from zeroth.core.service.bootstrap import bootstrap_service, run_migrations
    from zeroth.core.storage.async_sqlite import AsyncSQLiteDatabase

    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "approval_step.sqlite")
        run_migrations(f"sqlite:///{db_path}")
        database = AsyncSQLiteDatabase(path=db_path)

        contract_registry = ContractRegistry(database)
        await contract_registry.register(DemoPayload, name="contract://demo-input")
        await contract_registry.register(DemoPayload, name="contract://demo-output")

        graph_repository = GraphRepository(database)
        graph = await graph_repository.create(build_demo_graph(include_approval=True))
        await graph_repository.publish(graph.graph_id, graph.version)

        deployment_service = DeploymentService(
            graph_repository=graph_repository,
            deployment_repository=SQLiteDeploymentRepository(database),
            contract_registry=contract_registry,
        )
        deployment = await deployment_service.deploy(
            "cookbook-approval-step", graph.graph_id, graph.version
        )

        service = await bootstrap_service(
            database,
            deployment_ref=deployment.deployment_ref,
            executable_unit_runner=_EchoUnitRunner(),
            enable_durable_worker=False,
        )
        service.orchestrator.agent_runners = {"agent": _EchoAgentRunner()}

        paused = await service.orchestrator.run_graph(
            service.graph,
            {"message": "please approve this action"},
            deployment_ref=deployment.deployment_ref,
        )
        assert paused.status is RunStatus.WAITING_APPROVAL, paused.status
        pending = await service.approval_service.list_pending(
            run_id=paused.run_id, deployment_ref=deployment.deployment_ref
        )
        approval = pending[0]
        print(f"Pending approval {approval.approval_id} on node {approval.node_id}")

        actor = ActorIdentity(
            subject="demo-operator",
            auth_method=AuthMethod.API_KEY,
            roles=[ServiceRole.OPERATOR, ServiceRole.REVIEWER],
        )
        resolved = await service.approval_service.resolve(
            approval.approval_id,
            decision=ApprovalDecision.APPROVE,
            actor=actor,
        )
        print(f"Resolved approval {resolved.approval_id}: {resolved.status.value}")
        return 0


def main() -> int:
    required_env: list[str] = []
    missing = [k for k in required_env if not os.environ.get(k)]
    if missing:
        print(f"SKIP: missing env vars: {', '.join(missing)}", file=sys.stderr)
        return 0
    return asyncio.run(_run_demo())


if __name__ == "__main__":
    sys.exit(main())
