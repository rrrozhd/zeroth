"""Concurrent dispatches must never mutate a registered runner prototype."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from zeroth.core.governed.memory.models import MemoryScope
import pytest
from pydantic import BaseModel

from zeroth.core.agent_runtime import AgentConfig, AgentRunner
from zeroth.core.agent_runtime.provider import (
    CallableProviderAdapter,
    ProviderRequest,
    ProviderResponse,
)
from zeroth.core.agent_runtime.tools import ToolAttachmentManifest
from zeroth.core.audit.models import TokenUsage
from zeroth.core.context_window import ContextWindowSettings
from zeroth.core.econ.adapter import InstrumentedProviderAdapter
from zeroth.core.execution_units import ExecutableUnitRunResult
from zeroth.contracts.graph import (
    AgentNode,
    AgentNodeData,
    AgentToolBinding,
    ExecutableUnitNode,
    ExecutableUnitNodeData,
    Graph,
    ToolArgument,
)
from zeroth.core.memory.connectors import KeyValueMemoryConnector
from zeroth.core.memory.models import ConnectorManifest
from zeroth.core.memory.registry import InMemoryConnectorRegistry, MemoryConnectorResolver
from zeroth.core.orchestrator.runtime import RuntimeOrchestrator
from zeroth.core.runs import Run, RunRepository
from zeroth.contracts.templates import TemplateReference, TemplateRegistry, TemplateRenderer


class _Input(BaseModel):
    tenant: str


class _Output(BaseModel):
    answer: str


class _TwoPartyBarrier:
    def __init__(self) -> None:
        self._arrivals = 0
        self._lock = asyncio.Lock()
        self._ready = asyncio.Event()

    async def wait(self) -> None:
        async with self._lock:
            self._arrivals += 1
            if self._arrivals == 2:
                self._ready.set()
        await asyncio.wait_for(self._ready.wait(), timeout=30)


@dataclass(frozen=True)
class _DispatchSnapshot:
    runner_id: int
    instruction: str
    provider: InstrumentedProviderAdapter
    memory_resolver: object
    budget_enforcer: object
    context_tracker: object
    tool_executor: object


class _ObservedAgentRunner(AgentRunner):
    """A real runner that records dispatch-local wiring at a fixed interleave."""

    def __init__(
        self,
        *args: Any,
        barrier: _TwoPartyBarrier,
        recorder: list[_DispatchSnapshot],
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.barrier = barrier
        self.recorder = recorder

    async def run(
        self,
        input_payload: BaseModel | Mapping[str, Any],
        *,
        thread_id: str | None = None,
        runtime_context: Mapping[str, Any] | None = None,
        enforcement_context: Mapping[str, Any] | None = None,
    ):
        await self.barrier.wait()
        assert isinstance(self.provider, InstrumentedProviderAdapter)
        self.recorder.append(
            _DispatchSnapshot(
                runner_id=id(self),
                instruction=self.config.instruction,
                provider=self.provider,
                memory_resolver=self.memory_resolver,
                budget_enforcer=self.budget_enforcer,
                context_tracker=self.context_tracker,
                tool_executor=self.tool_executor,
            )
        )
        return await super().run(
            input_payload,
            thread_id=thread_id,
            runtime_context=runtime_context,
            enforcement_context=enforcement_context,
        )


class _TenantProvider:
    def __init__(self) -> None:
        self.requests: list[tuple[str, ProviderRequest]] = []

    async def __call__(self, request: ProviderRequest) -> ProviderResponse:
        rendered = repr(request.messages)
        tenant = "tenant-A" if "tenant-A" in rendered else "tenant-B"
        self.requests.append((tenant, request))
        usage = TokenUsage(input_tokens=3, output_tokens=2, total_tokens=5, model_name="test")
        if not any("tool-" in repr(message) for message in request.messages):
            call_id = f"call-{tenant}"
            return ProviderResponse(
                tool_calls=[{"id": call_id, "name": "tenant_tool", "args": {}}],
                raw={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": "tenant_tool", "arguments": "{}"},
                        }
                    ],
                },
                token_usage=usage,
            )
        return ProviderResponse(
            content={"answer": tenant},
            token_usage=usage,
        )


class _CostEstimator:
    def estimate(self, *args: Any, **kwargs: Any) -> Decimal:
        return Decimal("0.25")


class _RegulusClient:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def track_execution(self, event: Any) -> None:
        self.events.append(event)


class _BudgetEnforcer:
    def __init__(self) -> None:
        self.tenants: list[str] = []

    async def check_budget(self, tenant_id: str) -> tuple[bool, float, float]:
        self.tenants.append(tenant_id)
        return True, 0.0, 10.0


class _ToolRunner:
    def __init__(self) -> None:
        self.manifest_refs: list[str] = []

    async def run(
        self,
        manifest_ref: str,
        input_payload: Any,
        *,
        enforcement_context: Any = None,
    ) -> ExecutableUnitRunResult:
        self.manifest_refs.append(manifest_ref)
        tenant = manifest_ref.removeprefix("eu://")
        return ExecutableUnitRunResult(
            manifest_ref=manifest_ref,
            input_data=dict(input_payload),
            output_data={"value": f"tool-{tenant}"},
        )


def _graph(tenant: str) -> tuple[Graph, AgentNode]:
    node = AgentNode(
        node_id="agent",
        graph_version_ref="graph:v1",
        agent=AgentNodeData(
            instruction="placeholder",
            model_provider="provider://test",
            tool_bindings=[
                AgentToolBinding(
                    target_node_id="tool",
                    name="tenant_tool",
                    description="Return the dispatch tenant",
                    arguments=[ToolArgument(name="unused", description="Unused", required=False)],
                )
            ],
            template_ref=TemplateReference(name="tenant-template", version=1),
            context_window=ContextWindowSettings(max_context_tokens=100_000),
        ),
    )
    graph = Graph(
        graph_id=f"graph-{tenant}",
        name=f"graph-{tenant}",
        entry_step="agent",
        nodes=[
            node,
            ExecutableUnitNode(
                node_id="tool",
                graph_version_ref="graph:v1",
                executable_unit=ExecutableUnitNodeData(
                    manifest_ref=f"eu://{tenant}",
                    execution_mode="wrapped_command",
                ),
            ),
        ],
        edges=[],
    )
    return graph, node


async def _seed_memory(
    resolver: MemoryConnectorResolver,
    *,
    tenant: str,
    run_id: str,
    value: str,
) -> None:
    [binding] = await resolver.resolve(
        ["memory://tenant"],
        runtime_context={"tenant_id": tenant, "run_id": run_id},
    )
    await binding.connector.write("latest", value, MemoryScope.RUN)


async def test_concurrent_dispatches_fork_all_mutable_runner_state(sqlite_db) -> None:
    barrier = _TwoPartyBarrier()
    snapshots: list[_DispatchSnapshot] = []
    provider = _TenantProvider()
    prototype = _ObservedAgentRunner(
        AgentConfig(
            name="agent",
            instruction="prototype instruction",
            model_name="test-model",
            input_model=_Input,
            output_model=_Output,
            memory_refs=["memory://tenant"],
            tool_attachments=[
                ToolAttachmentManifest(
                    alias="tenant_tool",
                    executable_unit_ref="node://tool",
                )
            ],
        ),
        CallableProviderAdapter(provider),
        barrier=barrier,
        recorder=snapshots,
    )
    original_config = prototype.config
    original_provider = prototype.provider
    original_resolver = prototype.memory_resolver
    original_budget = prototype.budget_enforcer
    original_tracker = prototype.context_tracker
    original_tool_executor = prototype.tool_executor

    registry = InMemoryConnectorRegistry()
    registry.register(
        "memory://tenant",
        ConnectorManifest(connector_type="key_value", scope=MemoryScope.RUN),
        KeyValueMemoryConnector(),
    )
    resolver = MemoryConnectorResolver(registry=registry)
    await _seed_memory(resolver, tenant="tenant-A", run_id="run-A", value="memory-A")
    await _seed_memory(resolver, tenant="tenant-B", run_id="run-B", value="memory-B")

    templates = TemplateRegistry()
    templates.register("tenant-template", 1, "instruction for {{ input.tenant }}")
    budget = _BudgetEnforcer()
    regulus = _RegulusClient()
    tool_runner = _ToolRunner()
    orchestrator = RuntimeOrchestrator(
        run_repository=RunRepository(sqlite_db),
        agent_runners={"agent": prototype},
        executable_unit_runner=tool_runner,
        memory_resolver=resolver,
        budget_enforcer=budget,
        cost_estimator=_CostEstimator(),
        regulus_client=regulus,
        deployment_ref="deployment",
        template_registry=templates,
        template_renderer=TemplateRenderer(),
    )
    graph_a, node_a = _graph("tenant-A")
    graph_b, node_b = _graph("tenant-B")
    run_a = Run(
        run_id="run-A",
        graph_version_ref="graph-A:v1",
        tenant_id="tenant-A",
        deployment_ref="deployment",
    )
    run_b = Run(
        run_id="run-B",
        graph_version_ref="graph-B:v1",
        tenant_id="tenant-B",
        deployment_ref="deployment",
    )

    (output_a, audit_a), (output_b, audit_b) = await asyncio.gather(
        orchestrator._dispatch_node(node_a, run_a, {"tenant": "tenant-A"}, graph_a),
        orchestrator._dispatch_node(node_b, run_b, {"tenant": "tenant-B"}, graph_b),
    )

    assert output_a == {"answer": "tenant-A"}
    assert output_b == {"answer": "tenant-B"}
    assert {snapshot.instruction for snapshot in snapshots} == {
        "instruction for tenant-A",
        "instruction for tenant-B",
    }
    assert len({snapshot.runner_id for snapshot in snapshots}) == 2
    assert {snapshot.provider._tenant_id for snapshot in snapshots} == {
        "tenant-A",
        "tenant-B",
    }
    assert all(snapshot.memory_resolver is resolver for snapshot in snapshots)
    assert all(snapshot.budget_enforcer is budget for snapshot in snapshots)
    assert len({id(snapshot.context_tracker) for snapshot in snapshots}) == 2
    assert all(snapshot.context_tracker.state.accumulated_tokens > 0 for snapshot in snapshots)
    assert len({id(snapshot.tool_executor) for snapshot in snapshots}) == 2

    requests_by_tenant = {tenant: repr(request.messages) for tenant, request in provider.requests}
    assert "memory-A" in requests_by_tenant["tenant-A"]
    assert "tool-tenant-A" in requests_by_tenant["tenant-A"]
    assert "memory-B" in requests_by_tenant["tenant-B"]
    assert "tool-tenant-B" in requests_by_tenant["tenant-B"]
    assert sorted(budget.tenants) == ["tenant-A", "tenant-B"]
    assert sorted(tool_runner.manifest_refs) == ["eu://tenant-A", "eu://tenant-B"]
    assert len(regulus.events) == 4
    assert {event.tenant_id for event in regulus.events} == {"tenant-A", "tenant-B"}
    assert {event.token_cost_usd for event in regulus.events} == {Decimal("0.25")}
    assert {event.metadata["run_id"] for event in regulus.events} == {"run-A", "run-B"}
    assert audit_a["cost_usd"] == 0.25
    assert audit_b["cost_usd"] == 0.25

    assert prototype.config is original_config
    assert prototype.provider is original_provider
    assert prototype.memory_resolver is original_resolver is None
    assert prototype.budget_enforcer is original_budget is None
    assert prototype.context_tracker is original_tracker is None
    assert prototype.tool_executor is original_tool_executor is None


async def test_callable_fork_protocol_wins_over_instance_run_override(sqlite_db) -> None:
    class ProtocolRunner:
        def __init__(self) -> None:
            self.fork_calls = 0

            async def instance_run(*args: Any, **kwargs: Any) -> Any:
                return SimpleNamespace(
                    output_data={"answer": "prototype"},
                    audit_record={},
                )

            self.run = instance_run

        def fork_for_dispatch(self) -> Any:
            self.fork_calls += 1

            async def fork_run(*args: Any, **kwargs: Any) -> Any:
                return SimpleNamespace(output_data={"answer": "fork"}, audit_record={})

            return SimpleNamespace(run=fork_run)

    prototype = ProtocolRunner()
    node = AgentNode(
        node_id="agent",
        graph_version_ref="graph:v1",
        agent=AgentNodeData(instruction="test", model_provider="provider://test"),
    )
    orchestrator = RuntimeOrchestrator(
        run_repository=RunRepository(sqlite_db),
        agent_runners={"agent": prototype},
        executable_unit_runner=_ToolRunner(),
    )

    output, _ = await orchestrator._dispatch_node(
        node,
        Run(
            run_id="run-protocol",
            graph_version_ref="graph:v1",
            tenant_id="tenant",
            deployment_ref="deployment",
        ),
        {"tenant": "tenant"},
    )

    assert prototype.fork_calls == 1
    assert output == {"answer": "fork"}


async def test_fallback_runner_restores_state_when_tool_setup_fails(sqlite_db) -> None:
    class FallbackRunner:
        def __init__(self) -> None:
            self.config = AgentConfig(
                name="fallback",
                instruction="prototype instruction",
                model_name="test-model",
                input_model=_Input,
                output_model=_Output,
            )
            self.provider = CallableProviderAdapter(
                lambda request: ProviderResponse(content={"answer": "unused"})
            )
            self.memory_resolver = None
            self.budget_enforcer = None
            self.context_tracker = None
            self._tool_executor = None

        @property
        def tool_executor(self) -> Any:
            return self._tool_executor

        @tool_executor.setter
        def tool_executor(self, value: Any) -> None:
            self._tool_executor = value
            if value is not None:
                raise RuntimeError("tool setup failed")

        async def run(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("run must not be reached")

    runner = FallbackRunner()
    original_config = runner.config
    original_provider = runner.provider
    original_resolver = runner.memory_resolver
    original_budget = runner.budget_enforcer
    original_tracker = runner.context_tracker
    original_tool_executor = runner.tool_executor
    templates = TemplateRegistry()
    templates.register("tenant-template", 1, "instruction for {{ input.tenant }}")
    graph, node = _graph("tenant-fallback")
    orchestrator = RuntimeOrchestrator(
        run_repository=RunRepository(sqlite_db),
        agent_runners={"agent": runner},
        executable_unit_runner=_ToolRunner(),
        memory_resolver=object(),
        budget_enforcer=object(),
        cost_estimator=_CostEstimator(),
        template_registry=templates,
        template_renderer=TemplateRenderer(),
    )

    with pytest.raises(RuntimeError, match="tool setup failed"):
        await orchestrator._dispatch_node(
            node,
            Run(
                run_id="run-fallback",
                graph_version_ref="graph:v1",
                tenant_id="tenant-fallback",
                deployment_ref="deployment",
            ),
            {"tenant": "tenant-fallback"},
            graph,
        )

    assert runner.config is original_config
    assert runner.provider is original_provider
    assert runner.memory_resolver is original_resolver is None
    assert runner.budget_enforcer is original_budget is None
    assert runner.context_tracker is original_tracker is None
    assert runner.tool_executor is original_tool_executor is None
