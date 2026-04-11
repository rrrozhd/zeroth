"""Getting Started Section 2 — first graph with an agent, a tool, and an LLM call.

Runs the Phase 30 quickstart demo graph end-to-end against an in-memory
SQLite database. This is the **library-embedded** path: Zeroth is booted
in-process, the run is driven directly through the
:class:`~zeroth.core.orchestrator.RuntimeOrchestrator`, and there is no
HTTP hop. The HTTP/curl path lives in ``examples/approval_demo.py``.

SKIPs (exit 0) when ``OPENAI_API_KEY`` is unset so CI on forked PRs and
clean-venv smoke tests never fail. Otherwise makes exactly one real LLM
call via litellm — the same "lightweight provider" pattern used by
``examples/hello.py``.
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
    """Trivial contract model used for both the agent input and the tool output."""

    message: str = ""


class _LiteLLMAgentRunner:
    """Minimal :class:`AgentRunner`-shaped object that calls litellm directly.

    The orchestrator only requires an object exposing
    ``async def run(input_payload, *, thread_id, runtime_context)`` that
    returns something with ``output_data`` and ``audit_record`` attributes.
    Tutorial helper — not a stable runner shape. Production code should
    use :mod:`zeroth.core.agent_runtime` instead.
    """

    def __init__(self, model: str, instruction: str) -> None:
        self._model = model
        self._instruction = instruction

    async def run(
        self,
        input_payload: Any,
        *,
        thread_id: str | None = None,
        runtime_context: Any = None,
    ) -> SimpleNamespace:
        from litellm import completion  # local import keeps top-level cheap

        user_text = input_payload.get("message", "") if isinstance(input_payload, dict) else ""
        response = completion(
            model=self._model,
            messages=[
                {"role": "system", "content": self._instruction},
                {"role": "user", "content": user_text or "Say hello from zeroth-core."},
            ],
        )
        content = response["choices"][0]["message"]["content"]
        return SimpleNamespace(
            output_data={"message": content},
            audit_record={"provider": self._model, "thread_id": thread_id},
        )


class _EchoExecutableUnitRunner:
    """Stub :class:`ExecutableUnitRunner`-shaped object that echoes the payload."""

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
        db_path = str(Path(tmp) / "first_graph.sqlite")
        run_migrations(f"sqlite:///{db_path}")
        database = AsyncSQLiteDatabase(path=db_path)

        # Register the demo contracts, graph, and deployment using the test-helper pattern.
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
            "demo-first-graph", graph.graph_id, graph.version
        )

        service = await bootstrap_service(
            database,
            deployment_ref=deployment.deployment_ref,
            executable_unit_runner=_EchoExecutableUnitRunner(),
            enable_durable_worker=False,
        )
        service.orchestrator.agent_runners = {
            "agent": _LiteLLMAgentRunner(
                model="openai/gpt-4o-mini",
                instruction="You are a friendly assistant. Reply in one short sentence.",
            )
        }

        final = await service.orchestrator.run_graph(
            service.graph,
            {"message": "Say hello from zeroth-core."},
            deployment_ref=deployment.deployment_ref,
        )

        print(f"Run {final.run_id} finished with status: {final.status.value}")
        if final.final_output is not None:
            print(f"Final output: {final.final_output}")
        return 0


def main() -> int:
    if not os.environ.get("OPENAI_API_KEY"):
        print(
            "SKIP: set OPENAI_API_KEY to run examples/first_graph.py against a real LLM",
            file=sys.stderr,
        )
        return 0
    return asyncio.run(_run_demo())


if __name__ == "__main__":
    raise SystemExit(main())
