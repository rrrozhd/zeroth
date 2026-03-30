from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from zeroth.agent_runtime import (
    AgentConfig,
    AgentProviderError,
    AgentRunner,
    DeterministicProviderAdapter,
    ProviderResponse,
    ToolAttachmentManifest,
)
from zeroth.audit import AuditRepository
from zeroth.execution_units import (
    CommandArtifactSource,
    ExecutableUnitBinding,
    ExecutableUnitRegistry,
    ExecutableUnitRunner,
    ExecutionMode,
    InputMode,
    OutputMode,
    RunConfig,
    SandboxExecutionResult,
    SandboxStrictnessMode,
    WrappedCommandUnitManifest,
)
from zeroth.graph import (
    AgentNode,
    AgentNodeData,
    Edge,
    ExecutableUnitNode,
    ExecutableUnitNodeData,
    ExecutionSettings,
    Graph,
)
from zeroth.identity import ActorIdentity, AuthMethod
from zeroth.orchestrator import RuntimeOrchestrator
from zeroth.policy import Capability, EnforcementResult, PolicyDecision
from zeroth.runs import RunRepository, RunStatus


class NumberInput(BaseModel):
    value: int


class NumberOutput(BaseModel):
    value: int


class ToolInput(BaseModel):
    query: str


class ToolOutput(BaseModel):
    answer: str
    score: int


class RecordingPolicyGuard:
    def evaluate(self, graph, node, run, input_payload):  # noqa: ANN001
        del graph, run, input_payload
        return EnforcementResult(
            decision=PolicyDecision.ALLOW,
            effective_capabilities={Capability.SECRET_ACCESS},
            allowed_secrets=["API_KEY"],
            network_mode="disabled",
            timeout_override_seconds=3,
            sandbox_strictness_mode="strict",
            approval_required_for_side_effects=node.node_id == "side-effect",
        )


class RecordingAgentRunner:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def run(  # noqa: ANN201
        self,
        input_payload,
        *,
        thread_id=None,
        runtime_context=None,
        enforcement_context=None,
    ):
        self.calls.append(
            {
                "input_payload": dict(input_payload),
                "thread_id": thread_id,
                "runtime_context": dict(runtime_context or {}),
                "enforcement_context": dict(enforcement_context or {}),
            }
        )
        return SimpleNamespace(
            output_data={"value": input_payload["value"] + 1},
            audit_record={"runner": "agent"},
        )


class RecordingExecutableUnitRunner:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def run(self, manifest_ref, payload, *, enforcement_context=None):  # noqa: ANN201
        self.calls.append(
            {
                "manifest_ref": manifest_ref,
                "payload": dict(payload),
                "enforcement_context": dict(enforcement_context or {}),
            }
        )
        return SimpleNamespace(
            output_data={"value": payload["value"] * 2},
            audit_record={"runner": "eu"},
        )


class RecordingSandboxManager:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def run(self, command, **kwargs):  # noqa: ANN001, ANN201
        self.calls.append({"command": list(command), **kwargs})
        return SandboxExecutionResult(
            command=tuple(command),
            returncode=0,
            stdout='{"value": 6}',
            stderr="",
            workdir="/tmp/zeroth-eu",
            environment=dict(kwargs.get("overlay_env", {})),
            backend="docker",
        )


@pytest.mark.asyncio
async def test_runtime_orchestrator_passes_enforcement_context_to_runners_and_audit(
    sqlite_db,
) -> None:
    agent_runner = RecordingAgentRunner()
    eu_runner = RecordingExecutableUnitRunner()
    graph = Graph(
        graph_id="graph-enforcement",
        name="policy-enforcement",
        entry_step="agent",
        execution_settings=ExecutionSettings(max_total_steps=5),
        nodes=[
            AgentNode(
                node_id="agent",
                graph_version_ref="graph-enforcement:v1",
                input_contract_ref="contract://input",
                output_contract_ref="contract://middle",
                agent=AgentNodeData(instruction="step", model_provider="provider://demo"),
            ),
            ExecutableUnitNode(
                node_id="eu",
                graph_version_ref="graph-enforcement:v1",
                input_contract_ref="contract://middle",
                output_contract_ref="contract://output",
                executable_unit=ExecutableUnitNodeData(
                    manifest_ref="eu://double",
                    execution_mode="wrapped_command",
                ),
            ),
        ],
        edges=[Edge(edge_id="edge-1", source_node_id="agent", target_node_id="eu")],
    )
    orchestrator = RuntimeOrchestrator(
        audit_repository=AuditRepository(sqlite_db),
        run_repository=RunRepository(sqlite_db),
        agent_runners={"agent": agent_runner},
        executable_unit_runner=eu_runner,  # type: ignore[arg-type]
        policy_guard=RecordingPolicyGuard(),  # type: ignore[arg-type]
    )

    run = await orchestrator.run_graph(graph, {"value": 2})

    assert run.status is RunStatus.COMPLETED
    assert run.metadata["enforcement"]["agent"]["network_mode"] == "disabled"
    assert agent_runner.calls[0]["enforcement_context"]["sandbox_strictness_mode"] == "strict"
    assert eu_runner.calls[0]["enforcement_context"]["allowed_secrets"] == ["API_KEY"]

    audits = AuditRepository(sqlite_db).list_by_run(run.run_id)
    assert audits[0].execution_metadata["enforcement"]["network_mode"] == "disabled"
    assert audits[1].execution_metadata["enforcement"]["sandbox_strictness_mode"] == "strict"


@pytest.mark.asyncio
async def test_runtime_orchestrator_gates_side_effecting_nodes_behind_approval(sqlite_db) -> None:
    eu_runner = RecordingExecutableUnitRunner()
    graph = Graph(
        graph_id="graph-side-effect",
        name="side-effect",
        entry_step="side-effect",
        nodes=[
            ExecutableUnitNode(
                node_id="side-effect",
                graph_version_ref="graph-side-effect:v1",
                input_contract_ref="contract://input",
                output_contract_ref="contract://output",
                executable_unit=ExecutableUnitNodeData(
                    manifest_ref="eu://write",
                    execution_mode="wrapped_command",
                ),
                execution_config={"side_effect": True},
            )
        ],
        edges=[],
    )
    from zeroth.approvals import ApprovalDecision, ApprovalRepository, ApprovalService

    approval_service = ApprovalService(
        repository=ApprovalRepository(sqlite_db),
        run_repository=RunRepository(sqlite_db),
        audit_repository=AuditRepository(sqlite_db),
    )
    orchestrator = RuntimeOrchestrator(
        approval_service=approval_service,
        audit_repository=AuditRepository(sqlite_db),
        run_repository=RunRepository(sqlite_db),
        agent_runners={},
        executable_unit_runner=eu_runner,  # type: ignore[arg-type]
        policy_guard=RecordingPolicyGuard(),  # type: ignore[arg-type]
    )

    paused = await orchestrator.run_graph(graph, {"value": 2})

    assert paused.status is RunStatus.WAITING_APPROVAL
    approval_id = paused.metadata["pending_approval"]["approval_id"]
    assert approval_id is not None
    approval_service.resolve(
        approval_id,
        decision=ApprovalDecision.APPROVE,
        actor=ActorIdentity(subject="reviewer-1", auth_method=AuthMethod.API_KEY),
    )

    resumed = await orchestrator.resume_graph(graph, paused.run_id)

    assert resumed.status is RunStatus.COMPLETED
    assert eu_runner.calls and eu_runner.calls[0]["manifest_ref"] == "eu://write"


@pytest.mark.asyncio
async def test_executable_unit_runner_applies_policy_overrides_before_sandbox() -> None:
    manifest = WrappedCommandUnitManifest(
        unit_id="policy-unit",
        onboarding_mode=ExecutionMode.WRAPPED_COMMAND,
        runtime="command",
        artifact_source=CommandArtifactSource(ref="/bin/echo"),
        entrypoint_type="command",
        input_mode=InputMode.JSON_STDIN,
        output_mode=OutputMode.JSON_STDOUT,
        input_contract_ref="contract://input",
        output_contract_ref="contract://output",
        run_config=RunConfig(
            command=["echo", '{"value": 6}'],
            environment={"API_KEY": "keep", "OTHER": "drop"},
        ),
        timeout_seconds=10,
        cache_identity_fields={"name": "policy-unit"},
    )
    registry = ExecutableUnitRegistry()
    registry.register(
        ExecutableUnitBinding(
            manifest_ref="eu://policy-unit",
            manifest=manifest,
            input_model=NumberInput,
            output_model=NumberOutput,
        )
    )
    sandbox_manager = RecordingSandboxManager()
    runner = ExecutableUnitRunner(registry, sandbox_manager=sandbox_manager)  # type: ignore[arg-type]

    result = await runner.run_manifest_ref(
        "eu://policy-unit",
        {"value": 3},
        enforcement_context={
            "effective_capabilities": {Capability.SECRET_ACCESS},
            "allowed_secrets": ["API_KEY"],
            "network_mode": "disabled",
            "timeout_override_seconds": 3,
            "sandbox_strictness_mode": SandboxStrictnessMode.STRICT.value,
        },
    )

    assert result.output_data == {"value": 6}
    assert sandbox_manager.calls[0]["timeout_seconds"] == 3
    assert sandbox_manager.calls[0]["overlay_env"]["API_KEY"] == "keep"
    assert sandbox_manager.calls[0]["overlay_env"]["OTHER"] == "drop"
    assert sandbox_manager.calls[0]["resource_constraints"].network_access is False


@pytest.mark.asyncio
async def test_agent_runner_applies_timeout_override_and_blocks_side_effecting_tool_calls(
    monkeypatch,
) -> None:
    provider = DeterministicProviderAdapter(
        [
            ProviderResponse(
                content=None,
                tool_calls=[{"id": "tool-1", "name": "notes", "args": {"query": "hello"}}],
            )
        ]
    )
    captured_timeouts: list[float | None] = []

    async def fake_run_provider_with_timeout(provider, request, timeout_seconds):  # noqa: ANN001
        del provider, request
        captured_timeouts.append(timeout_seconds)
        return ProviderResponse(
            content=None,
            tool_calls=[{"id": "tool-1", "name": "notes", "args": {"query": "hello"}}],
        )

    monkeypatch.setattr(
        "zeroth.agent_runtime.runner.run_provider_with_timeout",
        fake_run_provider_with_timeout,
    )
    runner = AgentRunner(
        AgentConfig(
            name="demo",
            instruction="Use tools when needed.",
            model_name="governai:test",
            input_model=ToolInput,
            output_model=ToolOutput,
            timeout_seconds=10,
            tool_attachments=[
                ToolAttachmentManifest(
                    alias="notes",
                    executable_unit_ref="eu://notes",
                    side_effect_allowed=True,
                )
            ],
        ),
        provider,
        tool_executor=lambda *_args, **_kwargs: {"status": "ok"},
    )

    with pytest.raises(AgentProviderError, match="approval"):
        await runner.run(
            {"query": "hello"},
            enforcement_context={
                "timeout_override_seconds": 2,
                "approval_required_for_side_effects": True,
            },
        )

    assert captured_timeouts == [2]
