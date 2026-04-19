"""Tests for declarative template memory binding (Phase TMB).

Covers TemplateMemoryBinding model validation, _resolve_template_memory
resolution logic, and the integration path through _dispatch_node.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest
from governai.memory.models import MemoryScope
from pydantic import BaseModel

from zeroth.core.agent_runtime import AgentConfig, AgentRunner
from zeroth.core.agent_runtime.provider import CallableProviderAdapter, ProviderResponse
from zeroth.core.audit import AuditRepository
from zeroth.core.execution_units import ExecutableUnitRegistry, ExecutableUnitRunner
from zeroth.core.graph import (
    AgentNode,
    AgentNodeData,
    Edge,
    ExecutionSettings,
    Graph,
    TemplateMemoryBinding,
)
from zeroth.core.memory.connectors import KeyValueMemoryConnector
from zeroth.core.memory.models import ConnectorManifest
from zeroth.core.memory.registry import InMemoryConnectorRegistry, MemoryConnectorResolver
from zeroth.core.orchestrator.runtime import MemoryBindingResolutionError, RuntimeOrchestrator
from zeroth.core.runs import Run, RunRepository, RunStatus
from zeroth.core.templates.registry import TemplateRegistry
from zeroth.core.templates.renderer import TemplateRenderer

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_run(run_id: str = "run-test-001") -> Run:
    return Run(
        run_id=run_id,
        graph_version_ref="graph:v1",
        deployment_ref="dep:v1",
        metadata={},
    )


def _connector_ref() -> str:
    return "memory://shared-store"


def _make_registry_and_connector() -> tuple[InMemoryConnectorRegistry, KeyValueMemoryConnector]:
    """Return a registry with a shared-scope KeyValue connector registered."""
    raw = KeyValueMemoryConnector()
    reg = InMemoryConnectorRegistry()
    reg.register(
        _connector_ref(),
        ConnectorManifest(connector_type="key_value", scope=MemoryScope.SHARED),
        raw,
    )
    return reg, raw


def _make_resolver(registry: InMemoryConnectorRegistry) -> MemoryConnectorResolver:
    return MemoryConnectorResolver(registry=registry, workflow_name="test-wf")


def _make_orchestrator_for_unit(
    *,
    resolver: MemoryConnectorResolver | None = None,
    sqlite_db=None,
) -> RuntimeOrchestrator:
    """Minimal orchestrator for calling _resolve_template_memory directly."""
    eu_runner = ExecutableUnitRunner(ExecutableUnitRegistry())
    if sqlite_db is not None:
        run_repo = RunRepository(sqlite_db)
    else:
        run_repo = MagicMock()
    return RuntimeOrchestrator(
        run_repository=run_repo,
        agent_runners={},
        executable_unit_runner=eu_runner,
        memory_resolver=resolver,
    )


def _make_agent_node(
    bindings: list[TemplateMemoryBinding],
    *,
    memory_refs: list[str] | None = None,
    template_ref=None,
) -> AgentNode:
    return AgentNode(
        node_id="test-node",
        graph_version_ref="graph:v1",
        agent=AgentNodeData(
            instruction="default instruction",
            model_provider="provider://test",
            memory_refs=memory_refs or [_connector_ref()],
            template_memory_bindings=bindings,
            template_ref=template_ref,
        ),
    )


async def _prepopulate(
    raw: KeyValueMemoryConnector,
    key: str,
    value: Any,
) -> None:
    """Write to SHARED scope so ScopedMemoryConnector resolves it correctly."""
    await raw.write(key, value, MemoryScope.SHARED, target="__shared__")


# ---------------------------------------------------------------------------
# 1. Model validation
# ---------------------------------------------------------------------------


def test_get_mode_requires_key() -> None:
    """TemplateMemoryBinding in 'get' mode without key raises ValueError."""
    with pytest.raises(ValueError, match="key is required"):
        TemplateMemoryBinding(
            as_name="x",
            connector_instance_id="memory://any",
            access_mode="get",
            key=None,
        )


def test_scan_mode_does_not_require_key() -> None:
    """TemplateMemoryBinding in 'scan' mode with no key or key_prefix is valid."""
    b = TemplateMemoryBinding(
        as_name="items",
        connector_instance_id="memory://any",
        access_mode="scan",
    )
    assert b.access_mode == "scan"
    assert b.key is None
    assert b.key_prefix is None


# ---------------------------------------------------------------------------
# 2. _resolve_template_memory — unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_mode_returns_value_from_connector() -> None:
    """Single 'get' binding resolves the stored value into the memory namespace."""
    reg, raw = _make_registry_and_connector()
    await _prepopulate(raw, "greeting", "Hello!")

    orchestrator = _make_orchestrator_for_unit(resolver=_make_resolver(reg))
    node = _make_agent_node(
        [
            TemplateMemoryBinding(
                as_name="greeting",
                connector_instance_id=_connector_ref(),
                access_mode="get",
                key="greeting",
                scope="shared",
            )
        ]
    )
    run = _make_run()

    memory_ns, audit = await orchestrator._resolve_template_memory(node, run, None, {})

    assert memory_ns == {"greeting": "Hello!"}
    assert len(audit) == 1
    assert audit[0]["as_name"] == "greeting"
    assert audit[0]["found"] is True


@pytest.mark.asyncio
async def test_get_mode_uses_default_when_key_missing() -> None:
    """'get' binding returns the declared default when the key is absent."""
    reg, _ = _make_registry_and_connector()  # store is empty

    orchestrator = _make_orchestrator_for_unit(resolver=_make_resolver(reg))
    node = _make_agent_node(
        [
            TemplateMemoryBinding(
                as_name="user",
                connector_instance_id=_connector_ref(),
                access_mode="get",
                key="nonexistent",
                default="anonymous",
                scope="shared",
            )
        ]
    )
    run = _make_run()

    memory_ns, audit = await orchestrator._resolve_template_memory(node, run, None, {})

    assert memory_ns == {"user": "anonymous"}
    assert audit[0]["found"] is False


@pytest.mark.asyncio
async def test_scan_mode_returns_prefix_filtered_dict() -> None:
    """'scan' binding with key_prefix returns only matching entries, keys stripped of prefix."""
    reg, raw = _make_registry_and_connector()
    await _prepopulate(raw, "item_alpha", 1)
    await _prepopulate(raw, "item_beta", 2)
    await _prepopulate(raw, "other_gamma", 99)

    orchestrator = _make_orchestrator_for_unit(resolver=_make_resolver(reg))
    node = _make_agent_node(
        [
            TemplateMemoryBinding(
                as_name="items",
                connector_instance_id=_connector_ref(),
                access_mode="scan",
                key_prefix="item_",
                scope="shared",
            )
        ]
    )
    run = _make_run()

    memory_ns, audit = await orchestrator._resolve_template_memory(node, run, None, {})

    assert set(memory_ns["items"].keys()) == {"alpha", "beta"}
    assert memory_ns["items"]["alpha"] == 1
    assert memory_ns["items"]["beta"] == 2
    assert audit[0]["access_mode"] == "scan"
    assert audit[0]["item_count"] == 2


@pytest.mark.asyncio
async def test_scan_max_items_truncates_results() -> None:
    """'scan' binding with max_items=2 returns at most 2 items."""
    reg, raw = _make_registry_and_connector()
    for i in range(5):
        await _prepopulate(raw, f"entry_{i}", i)

    orchestrator = _make_orchestrator_for_unit(resolver=_make_resolver(reg))
    node = _make_agent_node(
        [
            TemplateMemoryBinding(
                as_name="entries",
                connector_instance_id=_connector_ref(),
                access_mode="scan",
                key_prefix="entry_",
                max_items=2,
                scope="shared",
            )
        ]
    )
    run = _make_run()

    memory_ns, _ = await orchestrator._resolve_template_memory(node, run, None, {})

    assert len(memory_ns["entries"]) == 2


@pytest.mark.asyncio
async def test_key_substitution_with_input_placeholder() -> None:
    """Key containing {input.user_id} is expanded before the read call."""
    reg, raw = _make_registry_and_connector()
    await _prepopulate(raw, "profile_u42", {"name": "Alice"})

    orchestrator = _make_orchestrator_for_unit(resolver=_make_resolver(reg))
    node = _make_agent_node(
        [
            TemplateMemoryBinding(
                as_name="profile",
                connector_instance_id=_connector_ref(),
                access_mode="get",
                key="profile_{input.user_id}",
                scope="shared",
            )
        ]
    )
    run = _make_run()

    memory_ns, audit = await orchestrator._resolve_template_memory(
        node, run, None, {"user_id": "u42"}
    )

    assert memory_ns["profile"] == {"name": "Alice"}
    assert audit[0]["key"] == "profile_u42"


@pytest.mark.asyncio
async def test_empty_bindings_returns_empty_namespace() -> None:
    """No template_memory_bindings → empty namespace and empty audit list."""
    orchestrator = _make_orchestrator_for_unit(resolver=_make_resolver(InMemoryConnectorRegistry()))
    node = _make_agent_node([])
    run = _make_run()

    memory_ns, audit = await orchestrator._resolve_template_memory(node, run, None, {})

    assert memory_ns == {}
    assert audit == []


@pytest.mark.asyncio
async def test_no_memory_resolver_returns_empty_namespace() -> None:
    """When memory_resolver is None, _resolve_template_memory returns ({},[])."""
    orchestrator = _make_orchestrator_for_unit(resolver=None)
    node = _make_agent_node(
        [
            TemplateMemoryBinding(
                as_name="x",
                connector_instance_id=_connector_ref(),
                access_mode="get",
                key="k",
                scope="shared",
            )
        ]
    )
    run = _make_run()

    memory_ns, audit = await orchestrator._resolve_template_memory(node, run, None, {})

    assert memory_ns == {}
    assert audit == []


@pytest.mark.asyncio
async def test_unknown_connector_raises_memory_binding_resolution_error() -> None:
    """connector_instance_id not in registry raises MemoryBindingResolutionError."""
    reg = InMemoryConnectorRegistry()  # empty registry
    orchestrator = _make_orchestrator_for_unit(resolver=_make_resolver(reg))
    node = _make_agent_node(
        [
            TemplateMemoryBinding(
                as_name="x",
                connector_instance_id="memory://missing",
                access_mode="get",
                key="k",
                scope="shared",
            )
        ],
        memory_refs=["memory://missing"],
    )
    run = _make_run()

    with pytest.raises(MemoryBindingResolutionError, match="unknown memory connector"):
        await orchestrator._resolve_template_memory(node, run, None, {})


@pytest.mark.asyncio
async def test_multiple_bindings_all_resolved() -> None:
    """Two bindings from the same connector produce two keys in the namespace."""
    reg, raw = _make_registry_and_connector()
    await _prepopulate(raw, "first_name", "Bob")
    await _prepopulate(raw, "last_name", "Smith")

    orchestrator = _make_orchestrator_for_unit(resolver=_make_resolver(reg))
    node = _make_agent_node(
        [
            TemplateMemoryBinding(
                as_name="first",
                connector_instance_id=_connector_ref(),
                access_mode="get",
                key="first_name",
                scope="shared",
            ),
            TemplateMemoryBinding(
                as_name="last",
                connector_instance_id=_connector_ref(),
                access_mode="get",
                key="last_name",
                scope="shared",
            ),
        ]
    )
    run = _make_run()

    memory_ns, audit = await orchestrator._resolve_template_memory(node, run, None, {})

    assert memory_ns == {"first": "Bob", "last": "Smith"}
    assert len(audit) == 2


# ---------------------------------------------------------------------------
# 9-10. Integration tests via run_graph
# ---------------------------------------------------------------------------


class _AnyInput(BaseModel):
    """Minimal input model for integration test runners."""


class _AnyOutput(BaseModel):
    """Minimal output model for integration test runners."""

    result: str = ""


@pytest.mark.asyncio
async def test_memory_flows_into_rendered_instruction(sqlite_db) -> None:
    """End-to-end: memory value from template_memory_binding reaches the rendered template."""
    from zeroth.core.templates.models import TemplateReference

    reg, raw = _make_registry_and_connector()
    await _prepopulate(raw, "lang", "Python")
    resolver = _make_resolver(reg)

    tmpl_registry = TemplateRegistry()
    tmpl_registry.register("skill-tmpl", 1, "Write code in {{ memory.language }}.")
    renderer = TemplateRenderer()

    captured_instructions: list[str] = []

    runner = AgentRunner(
        AgentConfig(
            name="coder",
            instruction="placeholder",
            model_name="governai:test",
            input_model=_AnyInput,
            output_model=_AnyOutput,
        ),
        CallableProviderAdapter(
            lambda req: ProviderResponse(content={"result": "ok"})
        ),
    )

    # Patch runner.run to capture the rendered instruction (set on runner.config before call).
    _original_run = runner.run

    async def _capturing_run(input_data, **kwargs):
        captured_instructions.append(runner.config.instruction)
        return await _original_run(input_data, **kwargs)

    runner.run = _capturing_run

    node = AgentNode(
        node_id="coder",
        graph_version_ref="graph:v1",
        agent=AgentNodeData(
            instruction="placeholder",
            model_provider="provider://test",
            memory_refs=[_connector_ref()],
            template_ref=TemplateReference(name="skill-tmpl", version=1),
            template_memory_bindings=[
                TemplateMemoryBinding(
                    as_name="language",
                    connector_instance_id=_connector_ref(),
                    access_mode="get",
                    key="lang",
                    scope="shared",
                )
            ],
        ),
    )
    graph = Graph(
        graph_id="g-tmb-integration",
        name="tmb-test",
        entry_step="coder",
        execution_settings=ExecutionSettings(max_total_steps=5),
        nodes=[node],
        edges=[],
    )

    orchestrator = RuntimeOrchestrator(
        run_repository=RunRepository(sqlite_db),
        agent_runners={"coder": runner},
        executable_unit_runner=ExecutableUnitRunner(ExecutableUnitRegistry()),
        audit_repository=AuditRepository(sqlite_db),
        memory_resolver=resolver,
        template_registry=tmpl_registry,
        template_renderer=renderer,
    )

    run = await orchestrator.run_graph(graph, {})

    assert run.status is RunStatus.COMPLETED
    assert len(captured_instructions) == 1
    assert "Python" in captured_instructions[0]
    assert "{{ memory.language }}" not in captured_instructions[0]


@pytest.mark.asyncio
async def test_tmb_audit_record_present_in_dispatch_audit(sqlite_db) -> None:
    """_dispatch_node populates execution_metadata.template_memory_bindings in audit record."""
    from zeroth.core.templates.models import TemplateReference

    reg, raw = _make_registry_and_connector()
    await _prepopulate(raw, "ctx", "some-context")
    resolver = _make_resolver(reg)

    tmpl_registry = TemplateRegistry()
    tmpl_registry.register("audit-tmpl", 1, "Context: {{ memory.ctx }}")
    renderer = TemplateRenderer()

    runner = AgentRunner(
        AgentConfig(
            name="agent",
            instruction="placeholder",
            model_name="governai:test",
            input_model=_AnyInput,
            output_model=_AnyOutput,
        ),
        CallableProviderAdapter(lambda req: ProviderResponse(content={"result": "done"})),
    )

    node = AgentNode(
        node_id="agent",
        graph_version_ref="graph:v1",
        agent=AgentNodeData(
            instruction="placeholder",
            model_provider="provider://test",
            memory_refs=[_connector_ref()],
            template_ref=TemplateReference(name="audit-tmpl", version=1),
            template_memory_bindings=[
                TemplateMemoryBinding(
                    as_name="ctx",
                    connector_instance_id=_connector_ref(),
                    access_mode="get",
                    key="ctx",
                    scope="shared",
                )
            ],
        ),
    )

    orchestrator = RuntimeOrchestrator(
        run_repository=RunRepository(sqlite_db),
        agent_runners={"agent": runner},
        executable_unit_runner=ExecutableUnitRunner(ExecutableUnitRegistry()),
        audit_repository=AuditRepository(sqlite_db),
        memory_resolver=resolver,
        template_registry=tmpl_registry,
        template_renderer=renderer,
    )

    # Call _dispatch_node directly to inspect the returned audit_record.
    run = _make_run()
    _, audit_record = await orchestrator._dispatch_node(node, run, {})

    tmb_records = audit_record.get("execution_metadata", {}).get("template_memory_bindings")
    assert tmb_records is not None, "template_memory_bindings missing from audit_record"
    assert len(tmb_records) == 1
    assert tmb_records[0]["as_name"] == "ctx"
    assert tmb_records[0]["found"] is True
