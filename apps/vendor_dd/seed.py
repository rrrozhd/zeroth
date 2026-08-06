"""Seed the vendor-dd database: contracts, graphs, publish, deploy — one shot.

Creates (or recreates) the app-local SQLite database with all three graphs
published and deployed:

* ``vendor-dd``            — the main due-diligence workflow
* ``vendor-dd-dimension``  — the child graph the subgraph node invokes
* ``vendor-dd-chat``       — the follow-up conversation deployment

Run
---
    uv run python -m apps.vendor_dd.seed
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
DEFAULT_DB = APP_DIR / "vendor_dd.sqlite"

os.environ.setdefault("ZEROTH_DATABASE__SQLITE_PATH", str(DEFAULT_DB))
os.environ.setdefault("ZEROTH_REGULUS__ENABLED", "false")
os.environ.setdefault("ZEROTH_WEBHOOK__ENABLED", "false")
os.environ.setdefault("ZEROTH_APPROVAL_SLA__ENABLED", "false")
os.environ.setdefault("ZEROTH_REDIS__MODE", "disabled")

from apps.vendor_dd.contracts import CONTRACTS  # noqa: E402
from apps.vendor_dd.graphs import (  # noqa: E402
    CHAT_DEPLOYMENT_REF,
    DIMENSION_DEPLOYMENT_REF,
    MAIN_DEPLOYMENT_REF,
    build_chat_graph,
    build_dimension_graph,
    build_main_graph,
)
from zeroth.contracts.graph import GraphRepository  # noqa: E402
from zeroth.contracts.registry import ContractRegistry  # noqa: E402
from zeroth.platform.config import get_settings  # noqa: E402
from zeroth.platform.storage import create_database  # noqa: E402
from zeroth.runtime.graph_validation import GraphValidator  # noqa: E402
from zeroth.service.bootstrap import run_migrations  # noqa: E402
from zeroth.service.deployments import DeploymentService, SQLiteDeploymentRepository  # noqa: E402


async def main() -> int:
    settings = get_settings()
    db_path = Path(settings.database.sqlite_path)
    if db_path.exists():
        db_path.unlink()
        print(f"removed existing {db_path}")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    run_migrations(f"sqlite:///{db_path}")

    database = await create_database(settings)

    contract_registry = ContractRegistry(database)
    for name, model in CONTRACTS.items():
        await contract_registry.register(model, name=name)
    print(f"registered {len(CONTRACTS)} contracts")

    graph_repository = GraphRepository(
        database, validator=GraphValidator(contract_registry=contract_registry)
    )
    deployment_service = DeploymentService(
        graph_repository=graph_repository,
        deployment_repository=SQLiteDeploymentRepository(database),
        contract_registry=contract_registry,
    )

    for deployment_ref, graph in (
        (DIMENSION_DEPLOYMENT_REF, build_dimension_graph()),
        (CHAT_DEPLOYMENT_REF, build_chat_graph()),
        (MAIN_DEPLOYMENT_REF, build_main_graph()),
    ):
        saved = await graph_repository.create(graph)
        published = await graph_repository.publish(saved.graph_id, saved.version)
        deployment = await deployment_service.deploy(
            deployment_ref, published.graph_id, published.version
        )
        print(
            f"deployed {deployment.deployment_ref} @ v{deployment.version} "
            f"({len(graph.nodes)} nodes, {len(graph.edges)} edges)"
        )

    print(f"\nseeded {db_path}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
