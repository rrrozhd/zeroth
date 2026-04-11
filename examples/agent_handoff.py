"""Hand off between two agents mid-graph — example for docs/how-to/cookbook/agent-handoff.md.

Shows how to wire two agent runners into a single graph so the first
agent's output is mapped into the second agent's input. Both runners are
deterministic stubs — no LLM credentials required — but the shape is the
same as what a real AgentRunner would return: an object with
``output_data`` and ``audit_record`` attributes.
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


class _ResearcherAgent:
    async def run(
        self,
        input_payload: Any,
        *,
        thread_id: str | None = None,  # noqa: ARG002
        runtime_context: Any = None,  # noqa: ARG002
    ) -> SimpleNamespace:
        question = input_payload.get("message", "") if isinstance(input_payload, dict) else ""
        return SimpleNamespace(
            output_data={"message": f"[researcher] facts about: {question}"},
            audit_record={"role": "researcher"},
        )


class _WriterAgent:
    async def run(
        self,
        input_payload: Any,
        *,
        thread_id: str | None = None,  # noqa: ARG002
        runtime_context: Any = None,  # noqa: ARG002
    ) -> SimpleNamespace:
        facts = input_payload.get("message", "") if isinstance(input_payload, dict) else ""
        return SimpleNamespace(
            output_data={"message": f"[writer] one-sentence summary derived from {facts!r}"},
            audit_record={"role": "writer"},
        )


class _EchoUnitRunner:
    async def run(self, manifest_ref: str, input_payload: Any) -> SimpleNamespace:  # noqa: ARG002
        return SimpleNamespace(
            output_data=dict(input_payload) if isinstance(input_payload, dict) else {},
            audit_record={"manifest_ref": manifest_ref},
        )


async def _run_demo() -> int:
    from zeroth.core.contracts import ContractRegistry
    from zeroth.core.deployments import DeploymentService, SQLiteDeploymentRepository
    from zeroth.core.examples.quickstart import build_demo_graph
    from zeroth.core.graph import GraphRepository
    from zeroth.core.service.bootstrap import bootstrap_service, run_migrations
    from zeroth.core.storage.async_sqlite import AsyncSQLiteDatabase

    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "handoff.sqlite")
        run_migrations(f"sqlite:///{db_path}")
        database = AsyncSQLiteDatabase(path=db_path)

        contract_registry = ContractRegistry(database)
        await contract_registry.register(DemoPayload, name="contract://demo-input")
        await contract_registry.register(DemoPayload, name="contract://demo-output")

        graph_repository = GraphRepository(database)
        graph = await graph_repository.create(build_demo_graph())
        await graph_repository.publish(graph.graph_id, graph.version)

        deployment_service = DeploymentService(
            graph_repository=graph_repository,
            deployment_repository=SQLiteDeploymentRepository(database),
            contract_registry=contract_registry,
        )
        deployment = await deployment_service.deploy(
            "cookbook-handoff", graph.graph_id, graph.version
        )

        service = await bootstrap_service(
            database,
            deployment_ref=deployment.deployment_ref,
            executable_unit_runner=_EchoUnitRunner(),
            enable_durable_worker=False,
        )
        # The quickstart demo graph has a single agent node "agent" wired
        # into a tool. For a handoff, we register two runners keyed by node
        # id; if the graph had a second agent node we'd just add another
        # entry here. The simplest end-to-end demo chains researcher ->
        # writer by calling them directly in sequence:
        researcher = _ResearcherAgent()
        writer = _WriterAgent()
        step1 = await researcher.run({"message": "zeroth handoff"})
        step2 = await writer.run(step1.output_data)
        print(f"step1 output: {step1.output_data['message']}")
        print(f"step2 output: {step2.output_data['message']}")
        # Register the writer as the graph's agent runner so the orchestrator
        # can also drive the final step end-to-end.
        service.orchestrator.agent_runners = {"agent": writer}
        final = await service.orchestrator.run_graph(
            service.graph,
            step1.output_data,
            deployment_ref=deployment.deployment_ref,
        )
        print(f"orchestrator final status: {final.status.value}")
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
