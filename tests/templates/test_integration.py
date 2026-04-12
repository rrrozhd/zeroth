"""Integration tests for template resolution in the orchestrator.

Tests that the orchestrator resolves templates from the registry, renders them,
passes the rendered instruction to the agent runner, records template metadata
in audit records, and maintains backward compatibility with nodes that have no
template_ref.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from zeroth.core.graph.models import AgentNode, AgentNodeData, Edge, Graph
from zeroth.core.templates.models import TemplateReference


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class _FakeRunResult:
    output_data: dict[str, Any]
    audit_record: dict[str, Any]


@dataclass
class _RecordingRunner:
    """Test double that records the instruction it receives via config.instruction."""

    instruction_received: str | None = field(default=None, init=False)
    config: Any = None
    provider: Any = None
    memory_resolver: Any = None
    budget_enforcer: Any = None

    async def run(
        self,
        input_data: dict[str, Any],
        *,
        thread_id: str | None = None,
        runtime_context: dict[str, Any] | None = None,
    ) -> _FakeRunResult:
        self.instruction_received = self.config.instruction if self.config else None
        return _FakeRunResult(
            output_data={"result": "ok"},
            audit_record={"status": "completed"},
        )


class _FakeConfig:
    """Minimal config stand-in with instruction and model_copy."""

    def __init__(self, instruction: str) -> None:
        self.instruction = instruction

    def model_copy(self, *, update: dict[str, Any] | None = None) -> _FakeConfig:
        new = _FakeConfig(self.instruction)
        if update:
            for k, v in update.items():
                setattr(new, k, v)
        return new


class _FakeRunRepository:
    """Minimal RunRepository double for orchestrator tests."""

    def __init__(self) -> None:
        self._runs: dict[str, Any] = {}

    async def create(self, run: Any) -> Any:
        self._runs[run.run_id] = run
        return run

    async def put(self, run: Any) -> Any:
        self._runs[run.run_id] = run
        return run

    async def get(self, run_id: str) -> Any:
        return self._runs.get(run_id)

    async def write_checkpoint(self, run: Any) -> None:
        pass


class _FakeAuditRepository:
    """Records audit writes for later assertions."""

    def __init__(self) -> None:
        self.records: list[Any] = []

    async def write(self, record: Any) -> None:
        self.records.append(record)


# ---------------------------------------------------------------------------
# AgentNodeData tests
# ---------------------------------------------------------------------------


class TestAgentNodeDataTemplateRef:
    def test_template_ref_accepted(self):
        data = AgentNodeData(
            instruction="default instruction",
            model_provider="test:model",
            template_ref=TemplateReference(name="greeting", version=1),
        )
        assert data.template_ref is not None
        assert data.template_ref.name == "greeting"
        assert data.template_ref.version == 1

    def test_template_ref_none_by_default(self):
        data = AgentNodeData(
            instruction="default instruction",
            model_provider="test:model",
        )
        assert data.template_ref is None

    def test_backward_compat_without_template_ref(self):
        """Existing graphs without template_ref deserialize correctly."""
        data = AgentNodeData.model_validate(
            {
                "instruction": "do something",
                "model_provider": "test:model",
            }
        )
        assert data.instruction == "do something"
        assert data.template_ref is None


# ---------------------------------------------------------------------------
# Orchestrator template resolution tests
# ---------------------------------------------------------------------------


class TestOrchestratorTemplateResolution:
    @pytest.fixture()
    def registry(self):
        from zeroth.core.templates.registry import TemplateRegistry

        reg = TemplateRegistry()
        reg.register("greeting", 1, "Hello {{ input.name }}! Key={{ input.api_key }}")
        reg.register("greeting", 2, "Hi {{ input.name }}!")
        return reg

    @pytest.fixture()
    def renderer(self):
        from zeroth.core.templates.renderer import TemplateRenderer

        return TemplateRenderer()

    @pytest.fixture()
    def runner(self):
        runner = _RecordingRunner()
        runner.config = _FakeConfig("raw instruction")
        return runner

    def _make_graph(self, *, template_ref: TemplateReference | None = None) -> Graph:
        agent_data = AgentNodeData(
            instruction="raw instruction",
            model_provider="test:model",
            template_ref=template_ref,
        )
        return Graph(
            graph_id="g1",
            name="test-graph",
            entry_step="agent1",
            nodes=[
                AgentNode(
                    node_id="agent1",
                    graph_version_ref="g1:v1",
                    agent=agent_data,
                ),
            ],
            edges=[],
        )

    @pytest.mark.asyncio()
    async def test_template_resolved_and_rendered(self, registry, renderer, runner):
        from zeroth.core.orchestrator import RuntimeOrchestrator

        graph = self._make_graph(template_ref=TemplateReference(name="greeting", version=1))
        orchestrator = RuntimeOrchestrator(
            run_repository=_FakeRunRepository(),
            agent_runners={"agent1": runner},
            executable_unit_runner=None,
            template_registry=registry,
            template_renderer=renderer,
        )
        run = await orchestrator.run_graph(graph, {"name": "Alice", "api_key": "sk-123"})

        # The runner should have received the rendered instruction, not the raw one.
        assert runner.instruction_received is not None
        assert "Hello Alice!" in runner.instruction_received
        assert runner.instruction_received != "raw instruction"

    @pytest.mark.asyncio()
    async def test_backward_compat_no_template_ref(self, registry, renderer, runner):
        from zeroth.core.orchestrator import RuntimeOrchestrator

        graph = self._make_graph(template_ref=None)
        orchestrator = RuntimeOrchestrator(
            run_repository=_FakeRunRepository(),
            agent_runners={"agent1": runner},
            executable_unit_runner=None,
            template_registry=registry,
            template_renderer=renderer,
        )
        run = await orchestrator.run_graph(graph, {"name": "Alice"})

        # Without template_ref, should use raw instruction unchanged.
        assert runner.instruction_received == "raw instruction"

    @pytest.mark.asyncio()
    async def test_no_registry_skips_resolution(self, runner):
        from zeroth.core.orchestrator import RuntimeOrchestrator

        graph = self._make_graph(template_ref=TemplateReference(name="greeting", version=1))
        orchestrator = RuntimeOrchestrator(
            run_repository=_FakeRunRepository(),
            agent_runners={"agent1": runner},
            executable_unit_runner=None,
            # No template_registry / template_renderer
        )
        run = await orchestrator.run_graph(graph, {"name": "Alice"})

        # Should fall through to raw instruction.
        assert runner.instruction_received == "raw instruction"

    @pytest.mark.asyncio()
    async def test_version_none_resolves_latest(self, registry, renderer, runner):
        from zeroth.core.orchestrator import RuntimeOrchestrator

        graph = self._make_graph(template_ref=TemplateReference(name="greeting"))
        orchestrator = RuntimeOrchestrator(
            run_repository=_FakeRunRepository(),
            agent_runners={"agent1": runner},
            executable_unit_runner=None,
            template_registry=registry,
            template_renderer=renderer,
        )
        run = await orchestrator.run_graph(graph, {"name": "Bob"})

        # Latest version (2) template is "Hi {{ input.name }}!"
        assert runner.instruction_received is not None
        assert "Hi Bob!" in runner.instruction_received

    @pytest.mark.asyncio()
    async def test_template_not_found_raises(self, registry, renderer, runner):
        from zeroth.core.orchestrator import RuntimeOrchestrator
        from zeroth.core.templates.errors import TemplateNotFoundError

        graph = self._make_graph(template_ref=TemplateReference(name="nonexistent"))
        orchestrator = RuntimeOrchestrator(
            run_repository=_FakeRunRepository(),
            agent_runners={"agent1": runner},
            executable_unit_runner=None,
            template_registry=registry,
            template_renderer=renderer,
        )
        # The orchestrator should let the TemplateNotFoundError propagate.
        # It will be caught by the outer try/except in _drive and fail the run.
        run = await orchestrator.run_graph(graph, {"name": "Alice"})
        assert run.status.value.lower() == "failed"

    @pytest.mark.asyncio()
    async def test_audit_contains_rendered_prompt(self, registry, renderer, runner):
        from zeroth.core.orchestrator import RuntimeOrchestrator

        graph = self._make_graph(template_ref=TemplateReference(name="greeting", version=2))
        audit_repo = _FakeAuditRepository()
        orchestrator = RuntimeOrchestrator(
            run_repository=_FakeRunRepository(),
            agent_runners={"agent1": runner},
            executable_unit_runner=None,
            audit_repository=audit_repo,
            template_registry=registry,
            template_renderer=renderer,
        )
        run = await orchestrator.run_graph(graph, {"name": "Charlie"})

        assert len(audit_repo.records) == 1
        exec_meta = audit_repo.records[0].execution_metadata
        # execution_metadata on the NodeAuditRecord is the full audit dict;
        # template metadata is nested under the "execution_metadata" key within it.
        inner_meta = exec_meta.get("execution_metadata", exec_meta)
        assert "rendered_prompt" in inner_meta
        assert "Hi Charlie!" in inner_meta["rendered_prompt"]

    @pytest.mark.asyncio()
    async def test_audit_contains_template_ref(self, registry, renderer, runner):
        from zeroth.core.orchestrator import RuntimeOrchestrator

        graph = self._make_graph(template_ref=TemplateReference(name="greeting", version=1))
        audit_repo = _FakeAuditRepository()
        orchestrator = RuntimeOrchestrator(
            run_repository=_FakeRunRepository(),
            agent_runners={"agent1": runner},
            executable_unit_runner=None,
            audit_repository=audit_repo,
            template_registry=registry,
            template_renderer=renderer,
        )
        run = await orchestrator.run_graph(graph, {"name": "Dan", "api_key": "sk-000"})

        assert len(audit_repo.records) == 1
        exec_meta = audit_repo.records[0].execution_metadata
        inner_meta = exec_meta.get("execution_metadata", exec_meta)
        assert "template_ref" in inner_meta
        assert inner_meta["template_ref"]["name"] == "greeting"
        assert inner_meta["template_ref"]["version"] == 1

    @pytest.mark.asyncio()
    async def test_template_variables_include_input_and_state(self, renderer, runner):
        """Template variables should include input payload and run state metadata."""
        from zeroth.core.templates.registry import TemplateRegistry
        from zeroth.core.orchestrator import RuntimeOrchestrator

        reg = TemplateRegistry()
        reg.register("state_test", 1, "Input={{ input.val }} State={{ state }}")
        graph = self._make_graph(template_ref=TemplateReference(name="state_test", version=1))
        orchestrator = RuntimeOrchestrator(
            run_repository=_FakeRunRepository(),
            agent_runners={"agent1": runner},
            executable_unit_runner=None,
            template_registry=reg,
            template_renderer=renderer,
        )
        run = await orchestrator.run_graph(graph, {"val": "hello"})
        assert runner.instruction_received is not None
        assert "Input=hello" in runner.instruction_received

    @pytest.mark.asyncio()
    async def test_original_config_restored_after_execution(self, registry, renderer, runner):
        """The runner's config must be restored to original after template resolution."""
        from zeroth.core.orchestrator import RuntimeOrchestrator

        graph = self._make_graph(template_ref=TemplateReference(name="greeting", version=2))
        orchestrator = RuntimeOrchestrator(
            run_repository=_FakeRunRepository(),
            agent_runners={"agent1": runner},
            executable_unit_runner=None,
            template_registry=registry,
            template_renderer=renderer,
        )
        run = await orchestrator.run_graph(graph, {"name": "Eve"})
        # After execution completes, runner config should be restored.
        assert runner.config.instruction == "raw instruction"
