"""The runtime's graph driver.

``GraphDriver`` owns the state-machine progression: the loop that pops the next
pending node, dispatches it, records history, plans and queues successors, and
persists a checkpoint — plus the terminal transitions (completion, failure) and
the pause points that return a run mid-flight.

``RuntimeOrchestrator`` keeps ``_drive`` and ``_entry_step`` as delegating
methods because ``zeroth.runtime.orchestration.run_worker`` and
``zeroth.runtime.subgraphs.executor`` call them on the orchestrator by name.

End-to-end behavior is guarded by ``tests/runtime/orchestration/
test_characterization.py``, which pins the exact side-effect order this loop
produces, plus ``tests/orchestrator``, ``tests/subgraph`` and ``tests/parallel``.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any

import pytest

from zeroth.contracts.graph import (
    AgentNode,
    AgentNodeData,
    Edge,
    ExecutionSettings,
    Graph,
)
from zeroth.core.runs import Run, RunStatus
from zeroth.runtime.orchestration import GraphDriver


class _EchoRunRepository:
    def __init__(self) -> None:
        self.puts: list[Run] = []
        self.checkpoints: list[Run] = []

    async def put(self, run: Run) -> Run:
        self.puts.append(run)
        return run

    async def write_checkpoint(self, run: Run) -> str:
        self.checkpoints.append(run)
        return "cp"


class _RecordingWebhookService:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def emit_event(self, *, event_type: str, deployment_ref: str, tenant_id: str, data: Any):
        self.events.append(event_type)


class _ExplodingWebhookService:
    async def emit_event(self, **kwargs: Any) -> None:
        raise RuntimeError("webhook backend down")


def _run(**kwargs: Any) -> Run:
    defaults: dict[str, Any] = {
        "graph_version_ref": "g:v1",
        "deployment_ref": "d",
        "thread_id": "t",
        "current_node_ids": [],
        "pending_node_ids": [],
        "metadata": {},
    }
    defaults.update(kwargs)
    return Run(**defaults)


def _node(node_id: str) -> AgentNode:
    return AgentNode(
        node_id=node_id,
        graph_version_ref="g:v1",
        agent=AgentNodeData(instruction="i", model_provider="provider://p"),
    )


def _graph() -> Graph:
    return Graph(
        graph_id="g",
        name="g",
        entry_step="a",
        execution_settings=ExecutionSettings(max_total_steps=10),
        nodes=[_node("a"), _node("b")],
        edges=[Edge(edge_id="e1", source_node_id="a", target_node_id="b")],
    )


def _driver(**overrides: Any) -> GraphDriver:
    kwargs: dict[str, Any] = {"run_repository": _EchoRunRepository()}
    kwargs.update(overrides)
    return GraphDriver(**kwargs)


def test_the_driver_takes_its_dependencies_by_injection() -> None:
    """Every collaborator arrives explicitly; the optional ones default off."""
    repository = _EchoRunRepository()
    driver = _driver(run_repository=repository)

    assert driver.run_repository is repository
    assert driver.webhook_service is None
    assert driver.artifact_store is None
    assert driver.subgraph_executor is None
    assert driver.per_run_cap_usd is None


def test_the_entry_step_is_the_declared_one_or_the_first_node() -> None:
    driver = _driver()
    graph = _graph()

    assert driver.entry_step(graph) == "a"
    graph.entry_step = None
    assert driver.entry_step(graph) == "a"


def test_an_empty_graph_has_no_entry_step() -> None:
    from zeroth.runtime.orchestration import OrchestratorError

    driver = _driver()
    graph = _graph()
    graph.entry_step = None
    graph.nodes = []

    with pytest.raises(OrchestratorError, match="graph has no nodes"):
        driver.entry_step(graph)


def test_initial_metadata_seeds_the_entry_payload_and_traversal_counters() -> None:
    driver = _driver()

    metadata = driver.initial_metadata(_graph(), {"value": 1})

    assert metadata == {
        "graph_id": "g",
        "graph_name": "g",
        "node_payloads": {"a": {"value": 1}},
        "edge_visit_counts": {},
        "path": [],
        "audits": {},
    }


def test_a_node_payload_is_consumed_exactly_once() -> None:
    """Reading a queued payload removes it, so a re-visit starts clean."""
    driver = _driver()
    run = _run(metadata={"node_payloads": {"a": {"value": 1}}})

    assert driver.payload_for(run, "a") == {"value": 1}
    assert driver.payload_for(run, "a") == {}


def test_graph_version_refs_are_built_from_id_and_version() -> None:
    assert _driver().graph_version_ref(_graph()) == "g:v1"


def test_tool_edges_are_never_used_for_payload_routing() -> None:
    """Tool edges connect the same pair but carry no mapping and route nothing."""
    driver = _driver()
    graph = _graph()
    graph.edges.append(
        Edge(edge_id="tool", source_node_id="a", target_node_id="b", kind="tool")
    )

    edge = driver.edge_for(graph, "a", "b")

    assert edge is not None
    assert edge.edge_id == "e1"


async def test_failing_a_run_persists_checkpoints_and_emits_the_webhook() -> None:
    repository = _EchoRunRepository()
    webhooks = _RecordingWebhookService()
    driver = _driver(run_repository=repository, webhook_service=webhooks)
    run = _run()

    failed = await driver.fail_run(run, "node_execution_failed", "boom")

    assert failed.status is RunStatus.FAILED
    assert failed.failure_state is not None
    assert failed.failure_state.reason == "node_execution_failed"
    assert failed.metadata["termination_reason"] == "node_execution_failed"
    assert repository.puts and repository.checkpoints
    assert webhooks.events == ["run.failed"]


async def test_a_broken_webhook_service_never_breaks_the_run() -> None:
    """Webhook emission is best-effort; a failing sink must not fail the run."""
    driver = _driver(webhook_service=_ExplodingWebhookService())

    failed = await driver.fail_run(_run(), "reason", "message")

    assert failed.status is RunStatus.FAILED


async def test_artifact_ttl_refresh_never_raises() -> None:
    """A broken artifact store is logged, not propagated."""

    class _BrokenStore:
        def __getattr__(self, name: str) -> Any:
            raise RuntimeError("store down")

    driver = _driver(artifact_store=_BrokenStore())

    assert await driver.refresh_artifact_ttls(_run()) is None


@pytest.mark.parametrize(
    "statement",
    [
        "from zeroth.runtime.orchestration import GraphDriver",
        "import zeroth.core.orchestrator.runtime",
        "from zeroth.core.orchestrator import RuntimeOrchestrator",
    ],
)
def test_the_package_imports_in_a_cold_interpreter(statement: str) -> None:
    """Both import directions must work from a cold interpreter."""
    result = subprocess.run(
        [sys.executable, "-c", statement],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
