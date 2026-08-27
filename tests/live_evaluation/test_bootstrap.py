from __future__ import annotations

from zeroth.contracts.graph.repository import GraphRepository
from zeroth.service.demo import seed_demo
from zeroth.service.deployments.repository import SQLiteDeploymentRepository

from release.live_evaluation.bootstrap import seed_campaign_bootstrap


async def test_campaign_bootstrap_is_tenant_scoped_and_idempotent(sqlite_db) -> None:
    await seed_demo(sqlite_db, deployment_ref="default")

    first = await seed_campaign_bootstrap(
        sqlite_db,
        tenant_id="evaluation-studio-v1",
        deployment_ref="evaluation-bootstrap",
    )
    second = await seed_campaign_bootstrap(
        sqlite_db,
        tenant_id="evaluation-studio-v1",
        deployment_ref="evaluation-bootstrap",
    )

    assert first.deployment_id == second.deployment_id
    assert first.tenant_id == "evaluation-studio-v1"
    assert first.deployment_ref == "evaluation-bootstrap"

    graphs = GraphRepository(sqlite_db)
    assert await graphs.get(
        first.graph_id, tenant_id="evaluation-studio-v1", workspace_id=None
    ) is not None
    assert await graphs.get(first.graph_id) is None
    assert await graphs.get("demo-hello") is not None

    deployments = SQLiteDeploymentRepository(sqlite_db)
    assert await deployments.get(
        "evaluation-bootstrap", tenant_id="evaluation-studio-v1", workspace_id=None
    ) is not None
    assert await deployments.get("evaluation-bootstrap") is None
