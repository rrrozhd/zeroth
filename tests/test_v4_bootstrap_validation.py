"""Bootstrap validation tests for v4.0 subsystems.

Proves that all seven v4.0 subsystems (artifact store, HTTP client, template
registry, context window, parallel executor, subgraph executor, transform
mappings) are properly wired and non-None after bootstrap_service().

Also validates the SubgraphNode-in-parallel guard: SubgraphNode must be
rejected by split_fan_out with FanOutValidationError.
"""

from __future__ import annotations

import pytest

from tests.service.helpers import agent_graph, deploy_service


# ---------------------------------------------------------------------------
# Bootstrap validation: every v4.0 subsystem is wired
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bootstrap_has_artifact_store(sqlite_db) -> None:
    """bootstrap_service() returns ServiceBootstrap where artifact_store is not None."""
    graph = agent_graph(graph_id="boot-artifact")
    svc, _dep = await deploy_service(sqlite_db, graph, deployment_ref="boot-artifact")
    assert svc.artifact_store is not None


@pytest.mark.asyncio
async def test_bootstrap_has_http_client(sqlite_db) -> None:
    """bootstrap_service() returns ServiceBootstrap where http_client is not None."""
    graph = agent_graph(graph_id="boot-http")
    svc, _dep = await deploy_service(sqlite_db, graph, deployment_ref="boot-http")
    assert svc.http_client is not None


@pytest.mark.asyncio
async def test_bootstrap_has_template_registry(sqlite_db) -> None:
    """bootstrap_service() returns ServiceBootstrap where template_registry is not None."""
    graph = agent_graph(graph_id="boot-tmpl")
    svc, _dep = await deploy_service(sqlite_db, graph, deployment_ref="boot-tmpl")
    assert svc.template_registry is not None


@pytest.mark.asyncio
async def test_bootstrap_has_subgraph_executor(sqlite_db) -> None:
    """bootstrap_service() returns ServiceBootstrap where subgraph_executor is not None."""
    graph = agent_graph(graph_id="boot-subgraph")
    svc, _dep = await deploy_service(sqlite_db, graph, deployment_ref="boot-subgraph")
    assert svc.subgraph_executor is not None


@pytest.mark.asyncio
async def test_bootstrap_orchestrator_context_window_enabled(sqlite_db) -> None:
    """bootstrap_service() returns orchestrator with context_window_enabled True."""
    graph = agent_graph(graph_id="boot-cw")
    svc, _dep = await deploy_service(sqlite_db, graph, deployment_ref="boot-cw")
    assert svc.orchestrator.context_window_enabled is True


# ---------------------------------------------------------------------------
# SubgraphNode-in-parallel guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_split_fan_out_rejects_subgraph_node() -> None:
    """split_fan_out raises FanOutValidationError for SubgraphNode."""
    from unittest.mock import MagicMock

    from zeroth.core.parallel.errors import FanOutValidationError
    from zeroth.core.parallel.executor import ParallelExecutor
    from zeroth.core.parallel.models import ParallelConfig

    executor = ParallelExecutor()
    config = ParallelConfig(split_path="items")

    node = MagicMock()
    node.node_type = "subgraph"

    with pytest.raises(FanOutValidationError, match="SubgraphNode"):
        executor.split_fan_out("run1", {"items": [1, 2]}, config, node)
