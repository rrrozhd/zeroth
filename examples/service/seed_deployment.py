"""Seed the example deployment into a persistent SQLite database.

Run this once before starting the service so the entrypoint has a
deployment to load. In a Postgres production flow you'd run the same
logic as a one-shot migration or through an admin endpoint.

Run
---
    ZEROTH_DATABASE__SQLITE_PATH=examples_service.sqlite \\
        uv run python examples/service/seed_deployment.py
"""

from __future__ import annotations

# Allow python examples/NN_name.py to find the sibling examples/_common.py helper.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

import asyncio
import sys
from pathlib import Path

from examples._contracts import Answer, Question
from zeroth.core.config.settings import get_settings
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
from zeroth.core.service.bootstrap import run_migrations
from zeroth.core.storage.factory import create_database

DEPLOYMENT_REF = "examples-api"


def build_graph() -> Graph:
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


async def main() -> int:
    settings = get_settings()

    if settings.database.backend == "sqlite":
        db_path = Path(settings.database.sqlite_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        run_migrations(f"sqlite:///{db_path}")
    elif settings.database.backend == "postgres" and settings.database.postgres_dsn:
        run_migrations(settings.database.postgres_dsn.get_secret_value())

    database = await create_database(settings)

    contract_registry = ContractRegistry(database)
    await contract_registry.register(Question, name="contract://question")
    await contract_registry.register(Answer, name="contract://answer")

    graph_repository = GraphRepository(database)
    saved = await graph_repository.create(build_graph())
    published = await graph_repository.publish(saved.graph_id, saved.version)

    deployment_service = DeploymentService(
        graph_repository=graph_repository,
        deployment_repository=SQLiteDeploymentRepository(database),
        contract_registry=contract_registry,
    )
    deployment = await deployment_service.deploy(
        DEPLOYMENT_REF, published.graph_id, published.version
    )
    print(f"seeded deployment {deployment.deployment_ref} @ v{deployment.version}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
