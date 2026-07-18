"""The runtime's node dispatch and tool invocation collaborators.

``NodeDispatcher`` resolves a node's type and runs it: agent nodes through their
runner (with template, provider-instrumentation, memory, budget and context
window wiring applied and then restored), executable units and retrieval nodes
through their own paths. ``RuntimeToolExecutor`` owns every governed invocation
of an executable unit — as a graph step, as an inline code node, and as a tool
an agent calls mid-loop.

The behavioral guard for the dispatch paths themselves is the existing suite
(``tests/orchestrator``, ``tests/agent_runtime``, ``tests/rag``,
``tests/execution_units``), which exercises them end to end. These tests pin the
collaborator boundary: that each is constructible from explicit dependencies,
and that the public error types survive the move to a canonical module.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any

import pytest

from zeroth.core.graph import ExecutableUnitNode, ExecutableUnitNodeData, Graph
from zeroth.runtime.orchestration import (
    MemoryBindingResolutionError,
    NodeDispatcher,
    NodeDispatcherError,
    OrchestratorError,
    RuntimeToolExecutor,
)


class _StubUnitRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def run(self, manifest_ref: str, payload: Any, **kwargs: Any) -> Any:
        self.calls.append(("run", manifest_ref))
        return _Result()

    async def run_binding(self, binding: Any, payload: Any, **kwargs: Any) -> Any:
        self.calls.append(("run_binding", binding.manifest_ref))
        return _Result()


class _Result:
    output_data: dict[str, Any] = {"ok": True}
    audit_record: dict[str, Any] = {}


def _unit_node(node_id: str, *, inline_source: str | None = None) -> ExecutableUnitNode:
    # manifest_ref and inline_source are mutually exclusive on the node data:
    # a registered unit is looked up, an inline one travels in the graph.
    data = (
        ExecutableUnitNodeData(inline_source=inline_source, execution_mode="inline")
        if inline_source is not None
        else ExecutableUnitNodeData(manifest_ref="eu://x", execution_mode="native")
    )
    return ExecutableUnitNode(node_id=node_id, graph_version_ref="g:v1", executable_unit=data)


def _graph(nodes: list[Any]) -> Graph:
    return Graph(
        graph_id="g",
        name="g",
        entry_step=nodes[0].node_id,
        nodes=nodes,
        edges=[],
    )


def test_the_error_hierarchy_is_preserved_across_the_move() -> None:
    """The public exceptions keep their identities and inheritance.

    They are protected legacy capabilities importable from both
    ``zeroth.core.orchestrator`` and ``zeroth.core.orchestrator.runtime``. The
    definitions now live in the canonical package; the legacy modules
    re-export the same class objects.
    """
    from zeroth.core.orchestrator import (
        NodeDispatcherError as LegacyPackageNodeDispatcherError,
    )
    from zeroth.core.orchestrator import (
        OrchestratorError as LegacyPackageOrchestratorError,
    )
    from zeroth.core.orchestrator.runtime import (
        MemoryBindingResolutionError as LegacyMemoryBindingResolutionError,
    )
    from zeroth.core.orchestrator.runtime import (
        NodeDispatcherError as LegacyNodeDispatcherError,
    )
    from zeroth.core.orchestrator.runtime import (
        OrchestratorError as LegacyOrchestratorError,
    )

    assert LegacyOrchestratorError is OrchestratorError
    assert LegacyPackageOrchestratorError is OrchestratorError
    assert LegacyNodeDispatcherError is NodeDispatcherError
    assert LegacyPackageNodeDispatcherError is NodeDispatcherError
    assert LegacyMemoryBindingResolutionError is MemoryBindingResolutionError
    assert issubclass(NodeDispatcherError, OrchestratorError)
    assert issubclass(MemoryBindingResolutionError, OrchestratorError)
    assert issubclass(OrchestratorError, RuntimeError)


def test_the_tool_executor_takes_its_dependencies_by_injection() -> None:
    runner = _StubUnitRunner()
    executor = RuntimeToolExecutor(executable_unit_runner=runner)

    assert executor.executable_unit_runner is runner


async def test_the_tool_executor_runs_a_manifest_backed_unit() -> None:
    runner = _StubUnitRunner()
    executor = RuntimeToolExecutor(executable_unit_runner=runner)

    result = await executor.run_unit("eu://x", {}, enforcement_context={})

    assert result.output_data == {"ok": True}
    assert runner.calls == [("run", "eu://x")]


async def test_the_tool_executor_synthesizes_a_binding_for_inline_source() -> None:
    """A Studio code node's source is bound on demand, not looked up."""
    runner = _StubUnitRunner()
    executor = RuntimeToolExecutor(executable_unit_runner=runner)
    node = _unit_node("code", inline_source="print('hi')")

    await executor.run_inline(node, {}, enforcement_context={})

    (call,) = runner.calls
    assert call[0] == "run_binding"


async def test_a_tool_call_targeting_a_non_unit_node_is_rejected() -> None:
    """Tool edges may only target executable unit nodes."""
    runner = _StubUnitRunner()
    executor = RuntimeToolExecutor(executable_unit_runner=runner)
    graph = _graph([_unit_node("unit")])
    execute = executor.build(graph, {})

    class _Binding:
        executable_unit_ref = "node://missing"
        alias = "t"

    with pytest.raises(KeyError):
        await execute(_Binding(), {})


async def test_a_tool_call_dispatches_the_target_unit_node() -> None:
    runner = _StubUnitRunner()
    executor = RuntimeToolExecutor(executable_unit_runner=runner)
    graph = _graph([_unit_node("unit")])
    execute = executor.build(graph, {})

    class _Binding:
        executable_unit_ref = "node://unit"
        alias = "t"

    assert await execute(_Binding(), {"a": 1}) == {"ok": True}
    assert runner.calls == [("run", "eu://x")]


def test_the_dispatcher_takes_its_dependencies_by_injection() -> None:
    """The dispatcher is constructible from explicit dependencies alone."""
    runners: dict[str, Any] = {}
    unit_runner = _StubUnitRunner()
    tool_executor = RuntimeToolExecutor(executable_unit_runner=unit_runner)
    dispatcher = NodeDispatcher(
        agent_runners=runners,
        executable_unit_runner=unit_runner,
        tool_executor=tool_executor,
    )

    assert dispatcher.agent_runners is runners
    assert dispatcher.tool_executor is tool_executor
    # Every optional integration is off by default, which is the unwired runtime.
    assert dispatcher.memory_resolver is None
    assert dispatcher.cost_estimator is None
    assert dispatcher.template_registry is None
    assert dispatcher.context_window_enabled is True


async def test_dispatching_an_unsupported_node_type_raises_node_dispatcher_error() -> None:
    dispatcher = NodeDispatcher(
        agent_runners={},
        executable_unit_runner=_StubUnitRunner(),
        tool_executor=RuntimeToolExecutor(executable_unit_runner=_StubUnitRunner()),
    )

    class _Weird:
        node_id = "w"
        node_version = 1

    with pytest.raises(NodeDispatcherError, match="unsupported node type"):
        await dispatcher.dispatch(_Weird(), _AnyRun(), {})


class _AnyRun:
    run_id = "r"
    tenant_id = "t"
    thread_id = "th"
    workspace_id = "w"
    metadata: dict[str, Any] = {}


@pytest.mark.parametrize(
    "statement",
    [
        "from zeroth.runtime.orchestration import NodeDispatcher, RuntimeToolExecutor",
        "from zeroth.runtime.orchestration.errors import OrchestratorError",
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
