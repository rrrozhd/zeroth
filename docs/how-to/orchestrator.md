# Orchestrator: usage guide

## Overview

This guide shows how to drive a published [graph](../concepts/graph.md) with the `RuntimeOrchestrator` — the engine covered in the [orchestrator concept page](../concepts/orchestrator.md). In practice you rarely construct the orchestrator by hand; `zeroth.core.service.bootstrap.bootstrap_service()` wires one up with every dependency already injected (run repository, audit, policy, approvals, memory, conditions, mappings). You then register your `AgentRunner` and `ExecutableUnitRunner` and call `orchestrator.run_graph()`.

## Minimal example

```python
import asyncio
import tempfile
from pathlib import Path

from zeroth.core.examples.quickstart import build_demo_graph
from zeroth.core.graph import GraphRepository
from zeroth.core.service.bootstrap import bootstrap_service, run_migrations
from zeroth.core.storage.async_sqlite import AsyncSQLiteDatabase
from zeroth.core.deployments import DeploymentService, SQLiteDeploymentRepository
from zeroth.core.contracts import ContractRegistry


async def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "orch.sqlite")
        run_migrations(f"sqlite:///{db_path}")
        database = AsyncSQLiteDatabase(path=db_path)

        graph_repo = GraphRepository(database)
        graph = await graph_repo.create(build_demo_graph())
        await graph_repo.publish(graph.graph_id, graph.version)

        deployments = DeploymentService(
            graph_repository=graph_repo,
            deployment_repository=SQLiteDeploymentRepository(database),
            contract_registry=ContractRegistry(database),
        )
        deployment = await deployments.deploy("demo", graph.graph_id, graph.version)

        service = await bootstrap_service(database, deployment_ref=deployment.deployment_ref)
        final = await service.orchestrator.run_graph(
            service.graph,
            {"message": "hello"},
            deployment_ref=deployment.deployment_ref,
        )
        print(final.status.value, final.final_output)


asyncio.run(main())
```

## Common patterns

- **Bootstrap, don't hand-wire** — call `bootstrap_service()` for a ready-to-run orchestrator; only construct `RuntimeOrchestrator` manually if you need to override a collaborator for testing.
- **Inject runners per deployment** — assign `service.orchestrator.agent_runners = {"agent": MyRunner()}` after bootstrap to plug in real LLM runners.
- **Resume interrupted runs** — because the orchestrator persists state after each step via `RunRepository`, you can reload a `Run` and continue it instead of restarting from the entry node.
- **Catch `OrchestratorError`** — wrap calls to `run_graph()` in a `try/except OrchestratorError` to distinguish orchestration failures from underlying `NodeDispatcherError`.

## Pitfalls

1. **No runner registered for a node type** — the orchestrator raises `NodeDispatcherError("no runner for agent 'foo'")`. Always populate `agent_runners` before `run_graph()`.
2. **Running an unpublished graph** — only `PUBLISHED` graphs should be executed; draft graphs may reference unresolved contracts or tools.
3. **Skipping the deployment layer** — pass `deployment_ref` so policy, audit, and cost tracking know which deployment the run belongs to.
4. **Mutating runtime collaborators mid-run** — swap `agent_runners` only between `run_graph()` calls, never during one.
5. **Ignoring `RunFailureState`** — on failure, inspect `run.failure_state` rather than relying solely on the status enum to understand what went wrong.

## Reference cross-link

See the [Python API reference for `zeroth.core.orchestrator`](../reference/python-api/orchestrator.md).
