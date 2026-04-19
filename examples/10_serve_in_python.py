"""10 — Ship it: turn a graph into a real HTTP API service with Uvicorn.

What this shows
---------------
Goes from "I have a graph" to "I have a production-shaped HTTP API on
:8000" in one Python file. Uses the real bits:

* :func:`bootstrap_service` — wires the orchestrator, run repository,
  approval service, durable dispatcher, memory, guardrails, auth, and
  audit trail for one deployment.
* :func:`create_app` — returns a fully-configured :class:`fastapi.FastAPI`
  instance with ``/v1/runs``, ``/v1/approvals``, ``/v1/audits``, ``/health``
  and authentication middleware already bolted on.
* :mod:`uvicorn` — the ASGI server used for both local and production
  deployments (see ``11_serve_via_entrypoint.md``).

After launch you can hit it with curl — the commands are printed at
startup so you can copy-paste them into another terminal.

Requirements
------------
* ``OPENAI_API_KEY`` in the environment (the Q&A agent uses a real LLM).

Run
---
    uv run python examples/10_serve_in_python.py

Stop with ``Ctrl-C``.
"""

from __future__ import annotations

# Allow python examples/NN_name.py to find the sibling examples/_common.py helper.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import asyncio
import os
import sys
from pathlib import Path

import uvicorn

from examples._common import DEMO_API_KEY, demo_auth_config, require_env
from examples._contracts import Answer, Question
from zeroth.core.agent_runtime import (
    AgentConfig,
    AgentRunner,
    LiteLLMProviderAdapter,
)
from zeroth.core.contracts import ContractRegistry
from zeroth.core.deployments import DeploymentService, SQLiteDeploymentRepository
from zeroth.core.graph import (
    AgentNode,
    AgentNodeData,
    DisplayMetadata,
    ExecutionSettings,
    Graph,
    GraphRepository,
)
from zeroth.core.service.app import create_app
from zeroth.core.service.bootstrap import bootstrap_service, run_migrations
from zeroth.core.storage import AsyncSQLiteDatabase

DEPLOYMENT_REF = "examples-api"
DB_PATH = Path("examples_serve.sqlite")


def build_graph() -> Graph:
    """Single-node Q&A graph — the minimum viable HTTP API."""
    return Graph(
        graph_id="examples-api",
        name="Examples API",
        version=1,
        entry_step="qa",
        execution_settings=ExecutionSettings(max_total_steps=5),
        nodes=[
            AgentNode(
                node_id="qa",
                graph_version_ref="examples-api@1",
                display=DisplayMetadata(title="Q&A"),
                input_contract_ref="contract://question",
                output_contract_ref="contract://answer",
                agent=AgentNodeData(
                    instruction=(
                        "Answer the user in one short sentence. "
                        "Return JSON matching the output schema."
                    ),
                    model_provider="openai/gpt-4o-mini",
                    model_params={"temperature": 0.3, "max_tokens": 150},
                ),
            ),
        ],
        edges=[],
    )


async def seed_deployment() -> AsyncSQLiteDatabase:
    """Migrate the SQLite database and publish + deploy the graph.

    In production you'd do this once at deploy time (or keep it in
    version control and hand Alembic a Postgres URL). The entrypoint
    would then load the already-persisted deployment by ref.
    """
    if DB_PATH.exists():
        DB_PATH.unlink()
    run_migrations(f"sqlite:///{DB_PATH}")
    database = AsyncSQLiteDatabase(path=str(DB_PATH))

    contract_registry = ContractRegistry(database)
    await contract_registry.register(Question, name="contract://question")
    await contract_registry.register(Answer, name="contract://answer")

    graph_repository = GraphRepository(database)
    saved = await graph_repository.create(build_graph())
    await graph_repository.publish(saved.graph_id, saved.version)

    deployment_service = DeploymentService(
        graph_repository=graph_repository,
        deployment_repository=SQLiteDeploymentRepository(database),
        contract_registry=contract_registry,
    )
    await deployment_service.deploy(DEPLOYMENT_REF, saved.graph_id, saved.version)
    return database


async def build_app():
    """Wire the service end-to-end and return the FastAPI app."""
    database = await seed_deployment()

    runner = AgentRunner(
        AgentConfig(
            name="qa",
            description="Answers questions via LiteLLM.",
            instruction="Answer briefly.",
            model_name="openai/gpt-4o-mini",
            input_model=Question,
            output_model=Answer,
        ),
        LiteLLMProviderAdapter(),
    )

    bootstrap = await bootstrap_service(
        database,
        deployment_ref=DEPLOYMENT_REF,
        agent_runners={"qa": runner},
        auth_config=demo_auth_config(),
        # The durable worker polls the database for PENDING runs and
        # dispatches them through the orchestrator. POST /v1/runs creates
        # a PENDING run and returns immediately, so the worker must be
        # enabled for the HTTP flow to be useful.
        enable_durable_worker=True,
    )
    return create_app(bootstrap)


def print_curl_hints(port: int) -> None:
    base = f"http://127.0.0.1:{port}"
    print("\n" + "─" * 64)
    print("Zeroth example API ready. Try these in another terminal:")
    print("─" * 64)
    print("# 1. health check (no auth)")
    print(f"curl {base}/health")
    print()
    print("# 2. submit a run")
    print(
        f'curl -X POST {base}/v1/runs \\\n'
        f'     -H "X-API-Key: {DEMO_API_KEY}" \\\n'
        f'     -H "Content-Type: application/json" \\\n'
        f'     -d \'{{"input_payload": {{"question": "What is Zeroth?"}}}}\''
    )
    print()
    print("# 3. poll the run (replace <run_id> with the id from step 2)")
    print(
        f'curl -H "X-API-Key: {DEMO_API_KEY}" '
        f"{base}/v1/runs/<run_id>"
    )
    print("─" * 64 + "\n")


def main() -> int:
    if not require_env("OPENAI_API_KEY"):
        return 0

    port = int(os.environ.get("ZEROTH_EXAMPLE_PORT", "8000"))
    app = asyncio.run(build_app())
    print_curl_hints(port)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
