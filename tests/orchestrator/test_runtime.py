from __future__ import annotations

import sys
from pathlib import Path

from pydantic import BaseModel

from zeroth.agent_runtime import (
    AgentConfig,
    AgentRunner,
    RepositoryThreadResolver,
    RepositoryThreadStateStore,
)
from zeroth.agent_runtime.provider import CallableProviderAdapter, ProviderResponse
from zeroth.approvals import ApprovalDecision, ApprovalRepository, ApprovalService
from zeroth.audit import AuditRepository
from zeroth.execution_units import (
    CommandArtifactSource,
    ExecutableUnitRegistry,
    ExecutableUnitRunner,
    ExecutionMode,
    InputMode,
    NativeUnitManifest,
    OutputMode,
    PythonModuleArtifactSource,
    RunConfig,
    WrappedCommandUnitManifest,
)
from zeroth.graph import (
    AgentNode,
    AgentNodeData,
    Condition,
    Edge,
    ExecutableUnitNode,
    ExecutableUnitNodeData,
    ExecutionSettings,
    Graph,
    HumanApprovalNode,
    HumanApprovalNodeData,
)
from zeroth.identity import ActorIdentity, AuthMethod
from zeroth.mappings.models import EdgeMapping, PassthroughMappingOperation
from zeroth.orchestrator import RuntimeOrchestrator
from zeroth.policy import (
    Capability,
    CapabilityRegistry,
    PolicyDefinition,
    PolicyGuard,
    PolicyRegistry,
)
from zeroth.runs import Run, RunRepository, RunStatus, ThreadRepository


class NumberInput(BaseModel):
    value: int


class NumberOutput(BaseModel):
    value: int


class RouteOutput(BaseModel):
    route: str
    value: int


class FinalOutput(BaseModel):
    final: int


class LabelOutput(BaseModel):
    label: str


def _agent_runner(
    *,
    output_model: type[BaseModel],
    provider,
    thread_state_store=None,
) -> AgentRunner:
    return AgentRunner(
        AgentConfig(
            name="agent",
            instruction="respond",
            model_name="governai:test",
            input_model=NumberInput,
            output_model=output_model,
        ),
        provider,
        thread_state_store=thread_state_store,
    )


def _command_manifest(script: Path, *, unit_id: str) -> WrappedCommandUnitManifest:
    return WrappedCommandUnitManifest(
        unit_id=unit_id,
        onboarding_mode=ExecutionMode.WRAPPED_COMMAND,
        runtime="command",
        artifact_source=CommandArtifactSource(ref=str(script)),
        entrypoint_type="command",
        input_mode=InputMode.JSON_STDIN,
        output_mode=OutputMode.JSON_STDOUT,
        input_contract_ref="contract://input",
        output_contract_ref="contract://output",
        run_config=RunConfig(command=[sys.executable, str(script)]),
        cache_identity_fields={"script": script.name},
    )


async def test_runtime_orchestrator_executes_linear_graph(sqlite_db, tmp_path: Path) -> None:
    script = tmp_path / "double.py"
    script.write_text(
        """
import json
import sys

payload = json.load(sys.stdin)
print(json.dumps({"value": payload["value"] * 2}))
""".strip(),
        encoding="utf-8",
    )
    eu_registry = ExecutableUnitRegistry()
    eu_registry.register(
        "eu://double",
        _command_manifest(script, unit_id="double"),
        input_model=NumberInput,
        output_model=NumberOutput,
    )
    eu_runner = ExecutableUnitRunner(eu_registry)
    start_runner = _agent_runner(
        output_model=NumberOutput,
        provider=CallableProviderAdapter(
            lambda request: ProviderResponse(
                content={"value": request.metadata["input_payload"]["value"]}
            )
        ),
    )
    finish_runner = _agent_runner(
        output_model=FinalOutput,
        provider=CallableProviderAdapter(
            lambda request: ProviderResponse(
                content={"final": request.metadata["input_payload"]["value"]}
            )
        ),
    )
    graph = Graph(
        graph_id="graph-linear",
        name="linear",
        entry_step="start",
        execution_settings=ExecutionSettings(max_total_steps=10),
        nodes=[
            AgentNode(
                node_id="start",
                graph_version_ref="graph-linear:v1",
                input_contract_ref="contract://input",
                output_contract_ref="contract://number",
                agent=AgentNodeData(instruction="start", model_provider="provider://start"),
            ),
            ExecutableUnitNode(
                node_id="double",
                graph_version_ref="graph-linear:v1",
                input_contract_ref="contract://number",
                output_contract_ref="contract://number",
                executable_unit=ExecutableUnitNodeData(
                    manifest_ref="eu://double",
                    execution_mode="wrapped_command",
                ),
            ),
            AgentNode(
                node_id="finish",
                graph_version_ref="graph-linear:v1",
                input_contract_ref="contract://number",
                output_contract_ref="contract://final",
                agent=AgentNodeData(instruction="finish", model_provider="provider://finish"),
            ),
        ],
        edges=[
            Edge(edge_id="edge-1", source_node_id="start", target_node_id="double"),
            Edge(
                edge_id="edge-2",
                source_node_id="double",
                target_node_id="finish",
                mapping=EdgeMapping(
                    operations=[
                        PassthroughMappingOperation(source_path="value", target_path="value")
                    ]
                ),
            ),
        ],
    )
    orchestrator = RuntimeOrchestrator(
        audit_repository=AuditRepository(sqlite_db),
        run_repository=RunRepository(sqlite_db),
        agent_runners={"start": start_runner, "finish": finish_runner},
        executable_unit_runner=eu_runner,
    )

    run = await orchestrator.run_graph(graph, {"value": 2})

    assert run.status is RunStatus.COMPLETED
    assert run.final_output == {"final": 4}
    assert [entry.node_id for entry in run.execution_history] == ["start", "double", "finish"]
    assert run.audit_refs == ["audit:1", "audit:2", "audit:3"]

    audits = await AuditRepository(sqlite_db).list_by_run(run.run_id)
    assert [audit.node_id for audit in audits] == ["start", "double", "finish"]
    assert audits[0].status == "completed"
    assert audits[1].execution_metadata["backend"] == "local"


async def test_runtime_orchestrator_resolves_conditional_branch(sqlite_db) -> None:
    eu_registry = ExecutableUnitRegistry()
    eu_registry.register(
        "eu://left",
        NativeUnitManifest(
            unit_id="left",
            onboarding_mode=ExecutionMode.NATIVE,
            runtime="python",
            artifact_source=PythonModuleArtifactSource(ref="demo.left:handler"),
            callable_ref="demo.left:handler",
            entrypoint_type="python_callable",
            input_mode=InputMode.JSON_STDIN,
            output_mode=OutputMode.JSON_STDOUT,
            input_contract_ref="contract://route",
            output_contract_ref="contract://label",
            cache_identity_fields={"python": "3.12"},
        ),
        input_model=RouteOutput,
        output_model=LabelOutput,
        handler=lambda _ctx, data: {"label": f"{data.route}:{data.value}"},
    )
    eu_registry.register(
        "eu://right",
        NativeUnitManifest(
            unit_id="right",
            onboarding_mode=ExecutionMode.NATIVE,
            runtime="python",
            artifact_source=PythonModuleArtifactSource(ref="demo.right:handler"),
            callable_ref="demo.right:handler",
            entrypoint_type="python_callable",
            input_mode=InputMode.JSON_STDIN,
            output_mode=OutputMode.JSON_STDOUT,
            input_contract_ref="contract://route",
            output_contract_ref="contract://label",
            cache_identity_fields={"python": "3.12"},
        ),
        input_model=RouteOutput,
        output_model=LabelOutput,
        handler=lambda _ctx, data: {"label": f"{data.route}:{data.value}"},
    )
    decide_runner = AgentRunner(
        AgentConfig(
            name="decide",
            instruction="route",
            model_name="governai:test",
            input_model=NumberInput,
            output_model=RouteOutput,
        ),
        CallableProviderAdapter(
            lambda _request: ProviderResponse(content={"route": "left", "value": 5})
        ),
    )
    graph = Graph(
        graph_id="graph-branch",
        name="branch",
        entry_step="decide",
        execution_settings=ExecutionSettings(max_total_steps=10),
        nodes=[
            AgentNode(
                node_id="decide",
                graph_version_ref="graph-branch:v1",
                input_contract_ref="contract://input",
                output_contract_ref="contract://route",
                agent=AgentNodeData(instruction="decide", model_provider="provider://route"),
            ),
            ExecutableUnitNode(
                node_id="left",
                graph_version_ref="graph-branch:v1",
                input_contract_ref="contract://route",
                output_contract_ref="contract://label",
                executable_unit=ExecutableUnitNodeData(
                    manifest_ref="eu://left",
                    execution_mode="native",
                ),
            ),
            ExecutableUnitNode(
                node_id="right",
                graph_version_ref="graph-branch:v1",
                input_contract_ref="contract://route",
                output_contract_ref="contract://label",
                executable_unit=ExecutableUnitNodeData(
                    manifest_ref="eu://right",
                    execution_mode="native",
                ),
            ),
        ],
        edges=[
            Edge(
                edge_id="edge-left",
                source_node_id="decide",
                target_node_id="left",
                condition=Condition(expression="payload.route == 'left'"),
            ),
            Edge(
                edge_id="edge-right",
                source_node_id="decide",
                target_node_id="right",
                condition=Condition(expression="payload.route == 'right'"),
            ),
        ],
    )
    orchestrator = RuntimeOrchestrator(
        run_repository=RunRepository(sqlite_db),
        agent_runners={"decide": decide_runner},
        executable_unit_runner=ExecutableUnitRunner(eu_registry),
    )

    run = await orchestrator.run_graph(graph, {"value": 1})

    assert run.status is RunStatus.COMPLETED
    assert run.final_output == {"label": "left:5"}
    assert [entry.node_id for entry in run.execution_history] == ["decide", "left"]
    assert any(result.selected_edge_id == "edge-left" for result in run.condition_results)


async def test_runtime_orchestrator_stops_cycle_with_max_total_steps(sqlite_db) -> None:
    loop_runner = _agent_runner(
        output_model=NumberOutput,
        provider=CallableProviderAdapter(lambda _request: ProviderResponse(content={"value": 1})),
    )
    graph = Graph(
        graph_id="graph-cycle",
        name="cycle",
        entry_step="loop",
        execution_settings=ExecutionSettings(max_total_steps=2),
        nodes=[
            AgentNode(
                node_id="loop",
                graph_version_ref="graph-cycle:v1",
                input_contract_ref="contract://input",
                output_contract_ref="contract://number",
                agent=AgentNodeData(instruction="loop", model_provider="provider://loop"),
            )
        ],
        edges=[Edge(edge_id="edge-loop", source_node_id="loop", target_node_id="loop")],
    )
    orchestrator = RuntimeOrchestrator(
        run_repository=RunRepository(sqlite_db),
        agent_runners={"loop": loop_runner},
        executable_unit_runner=ExecutableUnitRunner(ExecutableUnitRegistry()),
    )

    run = await orchestrator.run_graph(graph, {"value": 1})

    assert run.status is RunStatus.FAILED
    assert run.failure_state is not None
    assert run.failure_state.reason == "max_total_steps"
    assert len(run.execution_history) == 2


async def test_runtime_orchestrator_pauses_on_human_approval(sqlite_db) -> None:
    graph = Graph(
        graph_id="graph-approval",
        name="approval",
        entry_step="approval",
        nodes=[
            HumanApprovalNode(
                node_id="approval",
                graph_version_ref="graph-approval:v1",
                human_approval=HumanApprovalNodeData(),
            )
        ],
        edges=[],
    )
    approval_service = ApprovalService(
        repository=ApprovalRepository(sqlite_db),
        run_repository=RunRepository(sqlite_db),
        audit_repository=AuditRepository(sqlite_db),
    )
    orchestrator = RuntimeOrchestrator(
        approval_service=approval_service,
        run_repository=RunRepository(sqlite_db),
        agent_runners={},
        executable_unit_runner=ExecutableUnitRunner(ExecutableUnitRegistry()),
    )

    run = await orchestrator.run_graph(graph, {"value": 1})

    assert run.status is RunStatus.WAITING_APPROVAL
    assert run.current_node_ids == ["approval"]
    assert run.metadata["pending_approval"]["node_id"] == "approval"
    approval_id = run.metadata["pending_approval"]["approval_id"]
    record = await approval_service.get(approval_id)
    assert record is not None
    assert record.context_excerpt["value"] == 1


async def test_runtime_orchestrator_continues_after_approval_resolution(sqlite_db) -> None:
    approval_service = ApprovalService(
        repository=ApprovalRepository(sqlite_db),
        run_repository=RunRepository(sqlite_db),
        audit_repository=AuditRepository(sqlite_db),
    )
    graph = Graph(
        graph_id="graph-approval",
        name="approval",
        entry_step="approval",
        nodes=[
            HumanApprovalNode(
                node_id="approval",
                graph_version_ref="graph-approval:v1",
                output_contract_ref="contract://number",
                human_approval=HumanApprovalNodeData(
                    approval_policy_config={"allow_edits": True},
                ),
            ),
            AgentNode(
                node_id="finish",
                graph_version_ref="graph-approval:v1",
                input_contract_ref="contract://number",
                output_contract_ref="contract://number",
                agent=AgentNodeData(instruction="finish", model_provider="provider://finish"),
            ),
        ],
        edges=[Edge(edge_id="edge-1", source_node_id="approval", target_node_id="finish")],
    )
    orchestrator = RuntimeOrchestrator(
        approval_service=approval_service,
        audit_repository=AuditRepository(sqlite_db),
        run_repository=RunRepository(sqlite_db),
        agent_runners={
            "finish": _agent_runner(
                output_model=NumberOutput,
                provider=CallableProviderAdapter(
                    lambda request: ProviderResponse(
                        content={"value": request.metadata["input_payload"]["value"] + 1}
                    )
                ),
            )
        },
        executable_unit_runner=ExecutableUnitRunner(ExecutableUnitRegistry()),
    )

    paused = await orchestrator.run_graph(graph, {"value": 2})
    approval_id = paused.metadata["pending_approval"]["approval_id"]
    await approval_service.resolve(
        approval_id,
        decision=ApprovalDecision.EDIT_AND_APPROVE,
        actor=ActorIdentity(subject="user-1", auth_method=AuthMethod.API_KEY),
        edited_payload={"value": 4},
    )

    resumed = await approval_service.continue_run(
        approval_id, graph=graph, orchestrator=orchestrator
    )

    assert resumed.status is RunStatus.COMPLETED
    assert resumed.final_output == {"value": 5}


async def test_runtime_orchestrator_blocks_policy_violation_and_records_audit(sqlite_db) -> None:
    graph = Graph(
        graph_id="graph-policy",
        name="policy",
        entry_step="agent",
        policy_bindings=["policy://graph"],
        nodes=[
            AgentNode(
                node_id="agent",
                graph_version_ref="graph-policy:v1",
                capability_bindings=["capability://secret-access"],
                policy_bindings=["policy://node"],
                agent=AgentNodeData(instruction="policy", model_provider="provider://policy"),
            )
        ],
        edges=[],
    )
    capability_registry = CapabilityRegistry()
    capability_registry.register("capability://secret-access", Capability.SECRET_ACCESS)
    policy_registry = PolicyRegistry()
    policy_registry.register(
        PolicyDefinition(
            policy_id="policy://graph",
            allowed_capabilities=[Capability.SECRET_ACCESS],
        )
    )
    policy_registry.register(
        PolicyDefinition(
            policy_id="policy://node",
            denied_capabilities=[Capability.SECRET_ACCESS],
        )
    )
    orchestrator = RuntimeOrchestrator(
        audit_repository=AuditRepository(sqlite_db),
        run_repository=RunRepository(sqlite_db),
        agent_runners={
            "agent": _agent_runner(
                output_model=NumberOutput,
                provider=CallableProviderAdapter(
                    lambda _request: ProviderResponse(content={"value": 1})
                ),
            )
        },
        executable_unit_runner=ExecutableUnitRunner(ExecutableUnitRegistry()),
        policy_guard=PolicyGuard(
            policy_registry=policy_registry,
            capability_registry=capability_registry,
        ),
    )

    run = await orchestrator.run_graph(graph, {"value": 1})

    assert run.status is RunStatus.FAILED
    assert run.failure_state is not None
    assert run.failure_state.reason == "policy_violation"
    audits = await AuditRepository(sqlite_db).list_by_run(run.run_id)
    assert len(audits) == 1
    assert audits[0].status == "rejected"
    assert audits[0].error is not None


async def test_runtime_orchestrator_resumes_persisted_run(sqlite_db) -> None:
    store = RepositoryThreadStateStore(sqlite_db)
    thread_resolver = RepositoryThreadResolver(ThreadRepository(sqlite_db))
    runner = AgentRunner(
        AgentConfig(
            name="stateful",
            instruction="stateful",
            model_name="governai:test",
            input_model=NumberInput,
            output_model=NumberOutput,
        ),
        CallableProviderAdapter(
            lambda request: ProviderResponse(
                content={"value": request.metadata["input_payload"]["value"] + 1}
            )
        ),
        thread_state_store=store,
    )
    graph = Graph(
        graph_id="graph-resume",
        name="resume",
        entry_step="agent",
        nodes=[
            AgentNode(
                node_id="agent",
                graph_version_ref="graph-resume:v1",
                input_contract_ref="contract://input",
                output_contract_ref="contract://number",
                agent=AgentNodeData(
                    instruction="resume",
                    model_provider="provider://resume",
                    state_persistence={"mode": "thread"},
                    thread_participation="full",
                ),
            )
        ],
        edges=[],
    )
    repository = RunRepository(sqlite_db)
    seeded = await repository.create(
        Run(
            graph_version_ref="graph-resume:v1",
            deployment_ref="graph-resume",
            status=RunStatus.RUNNING,
            pending_node_ids=["agent"],
            metadata={
                "node_payloads": {"agent": {"value": 9}},
                "edge_visit_counts": {},
                "path": [],
                "audits": {},
            },
        )
    )
    await repository.write_checkpoint(seeded)
    orchestrator = RuntimeOrchestrator(
        run_repository=repository,
        agent_runners={"agent": runner},
        executable_unit_runner=ExecutableUnitRunner(ExecutableUnitRegistry()),
        thread_resolver=thread_resolver,
    )

    resumed = await orchestrator.resume_graph(graph, seeded.run_id)

    latest_checkpoint = await repository.get_latest_checkpoint(resumed.thread_id)
    assert resumed.status is RunStatus.COMPLETED
    assert resumed.final_output == {"value": 10}
    assert latest_checkpoint is not None
    assert latest_checkpoint.metadata["checkpoint_kind"] == "thread_state"
    assert latest_checkpoint.metadata["thread_state"]["output"] == {"value": 10}
