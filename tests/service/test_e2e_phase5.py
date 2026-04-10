from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient
from governai.memory.models import MemoryScope
from pydantic import BaseModel

from tests.service.helpers import (
    CountingFinishRunner,
    approval_resume_graph,
    deploy_service,
    operator_headers,
    reviewer_headers,
    service_app,
    wait_for,
)
from zeroth.core.agent_runtime import AgentConfig, AgentRunner, RepositoryThreadStateStore
from zeroth.core.agent_runtime.provider import CallableProviderAdapter, ProviderResponse
from zeroth.core.execution_units import (
    CommandArtifactSource,
    ExecutableUnitRegistry,
    ExecutableUnitRunner,
    ExecutionMode,
    InputMode,
    OutputMode,
    RunConfig,
    WrappedCommandUnitManifest,
)
from zeroth.core.graph import (
    AgentNode,
    AgentNodeData,
    Condition,
    Edge,
    ExecutableUnitNode,
    ExecutableUnitNodeData,
    ExecutionSettings,
    Graph,
)
from zeroth.core.mappings.models import EdgeMapping, PassthroughMappingOperation
from zeroth.core.memory import (
    ConnectorManifest,
    InMemoryConnectorRegistry,
    KeyValueMemoryConnector,
    MemoryConnectorResolver,
)
from zeroth.core.policy import (
    Capability,
    CapabilityRegistry,
    PolicyDefinition,
    PolicyGuard,
    PolicyRegistry,
)


class NumberInput(BaseModel):
    value: int


class NumberOutput(BaseModel):
    value: int


class BranchPlanOutput(BaseModel):
    value: int
    go_left: bool
    go_right: bool


class BranchResult(BaseModel):
    branch: str
    value: int


class MemoryOutput(BaseModel):
    value: int
    seen: int = 0


@dataclass(slots=True)
class FunctionalRunner:
    handler: Callable[[dict[str, Any], str | None, dict[str, Any]], dict[str, Any]]
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def run(
        self,
        input_payload: Any,
        *,
        thread_id: str | None = None,
        runtime_context: dict[str, Any] | None = None,
    ) -> SimpleNamespace:
        resolved_input = dict(input_payload)
        resolved_context = dict(runtime_context or {})
        self.calls.append(
            {
                "input_payload": resolved_input,
                "thread_id": thread_id,
                "runtime_context": resolved_context,
            }
        )
        return SimpleNamespace(
            output_data=self.handler(resolved_input, thread_id, resolved_context),
            audit_record={
                "thread_id": thread_id,
                "runtime_context": resolved_context,
            },
        )


def _register_command_unit(
    registry: ExecutableUnitRegistry,
    *,
    manifest_ref: str,
    script_path: Path,
    input_model: type[BaseModel],
    output_model: type[BaseModel],
) -> None:
    registry.register(
        manifest_ref,
        WrappedCommandUnitManifest(
            unit_id=manifest_ref.removeprefix("eu://"),
            onboarding_mode=ExecutionMode.WRAPPED_COMMAND,
            runtime="command",
            artifact_source=CommandArtifactSource(ref=str(script_path)),
            entrypoint_type="command",
            input_mode=InputMode.JSON_STDIN,
            output_mode=OutputMode.JSON_STDOUT,
            input_contract_ref="contract://input",
            output_contract_ref="contract://output",
            run_config=RunConfig(command=[sys.executable, str(script_path)]),
            cache_identity_fields={"script": script_path.name},
        ),
        input_model=input_model,
        output_model=output_model,
    )


def _run_status_payload(
    client: TestClient,
    run_id: str,
    *,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    response = client.get(f"/runs/{run_id}", headers=headers)
    assert response.status_code == 200
    return response.json()


def _wait_for_status(
    client: TestClient,
    run_id: str,
    status: str,
    *,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    wait_for(lambda: _run_status_payload(client, run_id, headers=headers)["status"] == status)
    return _run_status_payload(client, run_id, headers=headers)


def _linear_graph(*, graph_id: str) -> Graph:
    return Graph(
        graph_id=graph_id,
        name="Phase 5 Linear",
        version=1,
        entry_step="start",
        execution_settings=ExecutionSettings(max_total_steps=10),
        nodes=[
            AgentNode(
                node_id="start",
                graph_version_ref=f"{graph_id}@1",
                input_contract_ref="contract://input",
                output_contract_ref="contract://number",
                agent=AgentNodeData(instruction="start", model_provider="provider://start"),
            ),
            ExecutableUnitNode(
                node_id="double",
                graph_version_ref=f"{graph_id}@1",
                input_contract_ref="contract://number",
                output_contract_ref="contract://number",
                executable_unit=ExecutableUnitNodeData(
                    manifest_ref="eu://double",
                    execution_mode="wrapped_command",
                ),
            ),
            AgentNode(
                node_id="finish",
                graph_version_ref=f"{graph_id}@1",
                input_contract_ref="contract://number",
                output_contract_ref="contract://output",
                agent=AgentNodeData(instruction="finish", model_provider="provider://finish"),
            ),
        ],
        edges=[
            Edge(
                edge_id="edge-start-double",
                source_node_id="start",
                target_node_id="double",
                mapping=EdgeMapping(
                    operations=[
                        PassthroughMappingOperation(source_path="value", target_path="value")
                    ]
                ),
            ),
            Edge(
                edge_id="edge-double-finish",
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


def _cyclic_graph(*, graph_id: str) -> Graph:
    return Graph(
        graph_id=graph_id,
        name="Phase 5 Cycle",
        version=1,
        entry_step="loop",
        execution_settings=ExecutionSettings(max_total_steps=2),
        nodes=[
            AgentNode(
                node_id="loop",
                graph_version_ref=f"{graph_id}@1",
                input_contract_ref="contract://input",
                output_contract_ref="contract://output",
                agent=AgentNodeData(instruction="loop", model_provider="provider://loop"),
            )
        ],
        edges=[Edge(edge_id="edge-loop", source_node_id="loop", target_node_id="loop")],
    )


def _branching_graph(*, graph_id: str) -> Graph:
    return Graph(
        graph_id=graph_id,
        name="Phase 5 Branching",
        version=1,
        entry_step="decide",
        execution_settings=ExecutionSettings(max_total_steps=10),
        nodes=[
            AgentNode(
                node_id="decide",
                graph_version_ref=f"{graph_id}@1",
                input_contract_ref="contract://input",
                output_contract_ref="contract://branch-plan",
                agent=AgentNodeData(instruction="branch", model_provider="provider://branch"),
            ),
            ExecutableUnitNode(
                node_id="left",
                graph_version_ref=f"{graph_id}@1",
                input_contract_ref="contract://number",
                output_contract_ref="contract://branch-result",
                executable_unit=ExecutableUnitNodeData(
                    manifest_ref="eu://left",
                    execution_mode="wrapped_command",
                ),
            ),
            ExecutableUnitNode(
                node_id="right",
                graph_version_ref=f"{graph_id}@1",
                input_contract_ref="contract://number",
                output_contract_ref="contract://branch-result",
                executable_unit=ExecutableUnitNodeData(
                    manifest_ref="eu://right",
                    execution_mode="wrapped_command",
                ),
            ),
        ],
        edges=[
            Edge(
                edge_id="edge-left",
                source_node_id="decide",
                target_node_id="left",
                condition=Condition(expression="payload.go_left"),
                mapping=EdgeMapping(
                    operations=[
                        PassthroughMappingOperation(source_path="value", target_path="value")
                    ]
                ),
            ),
            Edge(
                edge_id="edge-right",
                source_node_id="decide",
                target_node_id="right",
                condition=Condition(expression="payload.go_right"),
                mapping=EdgeMapping(
                    operations=[
                        PassthroughMappingOperation(source_path="value", target_path="value")
                    ]
                ),
            ),
        ],
    )


def _thread_state_graph(*, graph_id: str) -> Graph:
    return Graph(
        graph_id=graph_id,
        name="Phase 5 Thread",
        version=1,
        entry_step="thread-agent",
        execution_settings=ExecutionSettings(max_total_steps=5),
        nodes=[
            AgentNode(
                node_id="thread-agent",
                graph_version_ref=f"{graph_id}@1",
                input_contract_ref="contract://input",
                output_contract_ref="contract://output",
                agent=AgentNodeData(
                    instruction="thread",
                    model_provider="provider://thread",
                    state_persistence={"mode": "thread"},
                    thread_participation="full",
                ),
            )
        ],
        edges=[],
    )


def _shared_memory_graph(*, graph_id: str) -> Graph:
    return Graph(
        graph_id=graph_id,
        name="Phase 5 Memory",
        version=1,
        entry_step="writer",
        execution_settings=ExecutionSettings(max_total_steps=5),
        nodes=[
            AgentNode(
                node_id="writer",
                graph_version_ref=f"{graph_id}@1",
                input_contract_ref="contract://input",
                output_contract_ref="contract://output",
                agent=AgentNodeData(
                    instruction="write",
                    model_provider="provider://writer",
                    memory_refs=["memory://shared"],
                ),
            ),
            AgentNode(
                node_id="reader",
                graph_version_ref=f"{graph_id}@1",
                input_contract_ref="contract://input",
                output_contract_ref="contract://output",
                agent=AgentNodeData(
                    instruction="read",
                    model_provider="provider://reader",
                    memory_refs=["memory://shared"],
                ),
            ),
        ],
        edges=[
            Edge(
                edge_id="edge-writer-reader",
                source_node_id="writer",
                target_node_id="reader",
                mapping=EdgeMapping(
                    operations=[
                        PassthroughMappingOperation(source_path="value", target_path="value")
                    ]
                ),
            )
        ],
    )


def _service_wrapper_graph(*, graph_id: str) -> Graph:
    return Graph(
        graph_id=graph_id,
        name="Phase 5 Deploy Invoke",
        version=1,
        entry_step="service-agent",
        execution_settings=ExecutionSettings(max_total_steps=5),
        nodes=[
            AgentNode(
                node_id="service-agent",
                graph_version_ref=f"{graph_id}@1",
                input_contract_ref="contract://input",
                output_contract_ref="contract://output",
                agent=AgentNodeData(
                    instruction="service",
                    model_provider="provider://service",
                ),
            )
        ],
        edges=[],
    )


def _policy_graph(*, graph_id: str) -> Graph:
    return Graph(
        graph_id=graph_id,
        name="Phase 5 Policy",
        version=1,
        entry_step="policy-agent",
        execution_settings=ExecutionSettings(max_total_steps=5),
        policy_bindings=["policy://graph"],
        nodes=[
            AgentNode(
                node_id="policy-agent",
                graph_version_ref=f"{graph_id}@1",
                input_contract_ref="contract://input",
                output_contract_ref="contract://output",
                policy_bindings=["policy://node"],
                capability_bindings=["capability://secret-access"],
                agent=AgentNodeData(
                    instruction="policy",
                    model_provider="provider://policy",
                ),
            )
        ],
        edges=[],
    )


async def test_phase5_linear_graph_runs_agent_to_eu_to_agent_via_api(
    sqlite_db, tmp_path: Path
) -> None:
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
    registry = ExecutableUnitRegistry()
    _register_command_unit(
        registry,
        manifest_ref="eu://double",
        script_path=script,
        input_model=NumberInput,
        output_model=NumberOutput,
    )
    service, _ = await deploy_service(
        sqlite_db,
        _linear_graph(graph_id="graph-phase5-linear"),
        extra_contract_models={"contract://number": NumberOutput},
    )
    service.orchestrator.agent_runners["start"] = FunctionalRunner(
        lambda payload, _thread_id, _context: {"value": payload["value"]}
    )
    service.orchestrator.agent_runners["finish"] = FunctionalRunner(
        lambda payload, _thread_id, _context: {"value": payload["value"] + 1}
    )
    service.orchestrator.executable_unit_runner = ExecutableUnitRunner(registry)
    app = await service_app(sqlite_db, service.deployment.deployment_ref, service)

    with TestClient(app) as client:
        create_response = client.post(
            "/runs",
            json={"input_payload": {"value": 3}},
            headers=operator_headers(),
        )
        assert create_response.status_code == 202
        run_id = create_response.json()["run_id"]
        completed = _wait_for_status(client, run_id, "succeeded", headers=operator_headers())

    persisted = await service.run_repository.get(run_id)
    audits = await service.audit_repository.list_by_run(run_id)

    assert completed["terminal_output"] == {"value": 7}
    assert completed["audit_refs"] == ["audit:1", "audit:2", "audit:3"]
    assert persisted is not None
    assert [entry.node_id for entry in persisted.execution_history] == ["start", "double", "finish"]
    assert [record.node_id for record in audits] == ["start", "double", "finish"]
    assert audits[1].execution_metadata["backend"] == "local"


async def test_phase5_cyclic_graph_stops_at_loop_guard_via_api(sqlite_db) -> None:
    service, _ = await deploy_service(sqlite_db, _cyclic_graph(graph_id="graph-phase5-cycle"))
    service.orchestrator.agent_runners["loop"] = FunctionalRunner(
        lambda payload, _thread_id, _context: {"value": payload.get("value", 0) + 1}
    )
    app = await service_app(sqlite_db, service.deployment.deployment_ref, service)

    with TestClient(app) as client:
        create_response = client.post(
            "/runs",
            json={"input_payload": {"value": 1}},
            headers=operator_headers(),
        )
        assert create_response.status_code == 202
        run_id = create_response.json()["run_id"]
        failed = _wait_for_status(
            client,
            run_id,
            "terminated_by_loop_guard",
            headers=operator_headers(),
        )

    persisted = await service.run_repository.get(run_id)

    assert failed["terminal_output"] is None
    assert failed["failure_state"]["reason"] == "max_total_steps"
    assert persisted is not None
    assert len(persisted.execution_history) == 2
    assert persisted.node_visit_counts["loop"] == 2


async def test_phase5_conditional_branching_fans_out_via_api(sqlite_db, tmp_path: Path) -> None:
    left_script = tmp_path / "left.py"
    left_script.write_text(
        """
import json
import sys

payload = json.load(sys.stdin)
print(json.dumps({"branch": "left", "value": payload["value"] + 10}))
""".strip(),
        encoding="utf-8",
    )
    right_script = tmp_path / "right.py"
    right_script.write_text(
        """
import json
import sys

payload = json.load(sys.stdin)
print(json.dumps({"branch": "right", "value": payload["value"] + 20}))
""".strip(),
        encoding="utf-8",
    )
    registry = ExecutableUnitRegistry()
    _register_command_unit(
        registry,
        manifest_ref="eu://left",
        script_path=left_script,
        input_model=NumberInput,
        output_model=BranchResult,
    )
    _register_command_unit(
        registry,
        manifest_ref="eu://right",
        script_path=right_script,
        input_model=NumberInput,
        output_model=BranchResult,
    )
    service, _ = await deploy_service(
        sqlite_db,
        _branching_graph(graph_id="graph-phase5-branching"),
        extra_contract_models={"contract://branch-plan": BranchPlanOutput},
    )
    service.orchestrator.agent_runners["decide"] = FunctionalRunner(
        lambda payload, _thread_id, _context: {
            "value": payload["value"],
            "go_left": True,
            "go_right": True,
        }
    )
    service.orchestrator.executable_unit_runner = ExecutableUnitRunner(registry)
    app = await service_app(sqlite_db, service.deployment.deployment_ref, service)

    with TestClient(app) as client:
        create_response = client.post(
            "/runs",
            json={"input_payload": {"value": 5}},
            headers=operator_headers(),
        )
        assert create_response.status_code == 202
        run_id = create_response.json()["run_id"]
        completed = _wait_for_status(client, run_id, "succeeded", headers=operator_headers())

    persisted = await service.run_repository.get(run_id)
    audits = await service.audit_repository.list_by_run(run_id)

    assert completed["terminal_output"] == {"branch": "right", "value": 25}
    assert persisted is not None
    assert [entry.node_id for entry in persisted.execution_history] == ["decide", "left", "right"]
    assert sorted(
        result.selected_edge_id for result in persisted.condition_results if result.selected_edge_id
    ) == ["edge-left", "edge-right"]
    assert [record.node_id for record in audits] == ["decide", "left", "right"]


async def test_phase5_approval_pause_and_resume_via_api(sqlite_db) -> None:
    service, _ = await deploy_service(
        sqlite_db,
        approval_resume_graph(graph_id="graph-phase5-approval"),
    )
    finish_runner = CountingFinishRunner()
    service.orchestrator.agent_runners["finish-step"] = finish_runner
    app = await service_app(sqlite_db, service.deployment.deployment_ref, service)

    with TestClient(app) as client:
        create_response = client.post(
            "/runs",
            json={"input_payload": {"value": 3}},
            headers=operator_headers(),
        )
        assert create_response.status_code == 202
        run_id = create_response.json()["run_id"]
        paused = _wait_for_status(
            client,
            run_id,
            "paused_for_approval",
            headers=operator_headers(),
        )
        approval_id = paused["approval_paused_state"]["approval_id"]

        resolve_response = client.post(
            f"/deployments/{service.deployment.deployment_ref}/approvals/{approval_id}/resolve",
            json={
                "decision": "edit_and_approve",
                "edited_payload": {"value": 8},
            },
            headers=reviewer_headers(),
        )
        completed = _wait_for_status(client, run_id, "succeeded", headers=operator_headers())

    persisted = await service.run_repository.get(run_id)
    audits = await service.audit_repository.list_by_run(run_id)

    assert resolve_response.status_code == 200
    assert completed["terminal_output"] == {"value": 9}
    assert finish_runner.call_count == 1
    assert persisted is not None
    assert [entry.node_id for entry in persisted.execution_history] == [
        "approval-step",
        "finish-step",
    ]
    assert any(record.status == "approval_api" for record in audits)
    assert any(
        record.node_id == "approval-step" and record.output_snapshot == {"value": 8}
        for record in audits
        if record.status == "completed"
    )


async def test_phase5_thread_continuity_across_runs_via_api(sqlite_db) -> None:
    service, _ = await deploy_service(
        sqlite_db, _thread_state_graph(graph_id="graph-phase5-thread")
    )
    thread_store = RepositoryThreadStateStore(
        run_repository=service.run_repository,
        thread_repository=service.thread_repository,
    )
    service.orchestrator.agent_runners["thread-agent"] = AgentRunner(
        AgentConfig(
            name="thread-agent",
            instruction="thread-state",
            model_name="governai:test",
            input_model=NumberInput,
            output_model=NumberOutput,
        ),
        CallableProviderAdapter(
            lambda request: ProviderResponse(
                content={
                    "value": request.metadata["input_payload"]["value"]
                    + request.metadata["thread_state"].get("output", {}).get("value", 0)
                }
            )
        ),
        thread_state_store=thread_store,
    )
    app = await service_app(sqlite_db, service.deployment.deployment_ref, service)

    with TestClient(app) as client:
        first_create = client.post(
            "/runs",
            json={"input_payload": {"value": 3}},
            headers=operator_headers(),
        )
        assert first_create.status_code == 202
        first_run_id = first_create.json()["run_id"]
        thread_id = first_create.json()["thread_id"]
        first_completed = _wait_for_status(
            client,
            first_run_id,
            "succeeded",
            headers=operator_headers(),
        )

        second_create = client.post(
            "/runs",
            json={"input_payload": {"value": 5}, "thread_id": thread_id},
            headers=operator_headers(),
        )
        assert second_create.status_code == 202
        second_run_id = second_create.json()["run_id"]
        second_completed = _wait_for_status(
            client,
            second_run_id,
            "succeeded",
            headers=operator_headers(),
        )

    thread = await service.thread_repository.get(thread_id)
    assert first_completed["thread_id"] == thread_id
    assert first_completed["terminal_output"] == {"value": 3}
    assert second_completed["thread_id"] == thread_id
    assert second_completed["terminal_output"] == {"value": 8}
    assert thread is not None
    assert thread.run_ids == [first_run_id, second_run_id]
    checkpoint = await service.run_repository.get_checkpoint(thread.state_snapshot_refs[-1])
    assert checkpoint is not None
    assert checkpoint.metadata["thread_state"]["output"] == {"value": 8}


async def test_phase5_shared_memory_connector_between_agents_via_api(sqlite_db) -> None:
    service, _ = await deploy_service(
        sqlite_db, _shared_memory_graph(graph_id="graph-phase5-memory")
    )
    registry = InMemoryConnectorRegistry()
    registry.register(
        "memory://shared",
        ConnectorManifest(
            connector_type="key_value",
            scope=MemoryScope.SHARED,
            instance_id="phase5-shared-memory",
        ),
        KeyValueMemoryConnector(),
    )
    resolver = MemoryConnectorResolver(
        registry=registry,
        thread_repository=service.thread_repository,
    )

    def build_memory_runner(name: str) -> AgentRunner:
        return AgentRunner(
            AgentConfig(
                name=name,
                instruction=name,
                model_name="governai:test",
                input_model=NumberInput,
                output_model=MemoryOutput,
                memory_refs=["memory://shared"],
            ),
            CallableProviderAdapter(
                lambda request: ProviderResponse(
                    content={
                        "value": request.metadata["input_payload"]["value"],
                        "seen": request.metadata["runtime_context"]
                        .get("memory", {})
                        .get("memory://shared", {})
                        .get("latest", {})
                        .get("value", 0),
                    }
                )
            ),
            memory_resolver=resolver,
        )

    service.orchestrator.agent_runners["writer"] = build_memory_runner("writer")
    service.orchestrator.agent_runners["reader"] = build_memory_runner("reader")
    app = await service_app(sqlite_db, service.deployment.deployment_ref, service)

    with TestClient(app) as client:
        create_response = client.post(
            "/runs",
            json={"input_payload": {"value": 4}},
            headers=operator_headers(),
        )
        assert create_response.status_code == 202
        run_id = create_response.json()["run_id"]
        completed = _wait_for_status(client, run_id, "succeeded", headers=operator_headers())

    persisted = await service.run_repository.get(run_id)
    audits = await service.audit_repository.list_by_run(run_id)
    reader_memory = audits[1].execution_metadata["extra"]["memory_interactions"]

    assert completed["terminal_output"] == {"value": 4, "seen": 4}
    assert persisted is not None
    assert [entry.node_id for entry in persisted.execution_history] == ["writer", "reader"]
    assert [interaction["operation"] for interaction in reader_memory] == ["read", "write"]


async def test_phase5_deploy_and_invoke_via_service_wrapper_api(sqlite_db) -> None:
    service, deployment = await deploy_service(
        sqlite_db,
        _service_wrapper_graph(graph_id="graph-phase5-deploy"),
        deployment_ref="phase5-deploy",
    )
    service.orchestrator.agent_runners["service-agent"] = FunctionalRunner(
        lambda payload, _thread_id, _context: {"value": payload["value"] * 3}
    )
    app = await service_app(sqlite_db, service.deployment.deployment_ref, service)

    with TestClient(app) as client:
        health_response = client.get("/health", headers=operator_headers())
        metadata_response = client.get(
            f"/deployments/{deployment.deployment_ref}/metadata",
            headers=operator_headers(),
        )
        create_response = client.post(
            "/runs",
            json={"input_payload": {"value": 4}},
            headers=operator_headers(),
        )
        assert create_response.status_code == 202
        run_id = create_response.json()["run_id"]
        completed = _wait_for_status(client, run_id, "succeeded", headers=operator_headers())

    assert health_response.status_code == 200
    assert health_response.json()["deployment_ref"] == deployment.deployment_ref
    assert metadata_response.status_code == 200
    assert metadata_response.json()["graph_version"] == deployment.graph_version
    assert completed["deployment_ref"] == deployment.deployment_ref
    assert completed["terminal_output"] == {"value": 12}
    assert completed["audit_refs"] == ["audit:1"]


async def test_phase5_policy_violation_fails_execution_and_records_audit(sqlite_db) -> None:
    service, _ = await deploy_service(sqlite_db, _policy_graph(graph_id="graph-phase5-policy"))
    runner = FunctionalRunner(lambda payload, _thread_id, _context: {"value": payload["value"]})
    service.orchestrator.agent_runners["policy-agent"] = runner

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
    service.orchestrator.policy_guard = PolicyGuard(
        policy_registry=policy_registry,
        capability_registry=capability_registry,
    )
    app = await service_app(sqlite_db, service.deployment.deployment_ref, service)

    with TestClient(app) as client:
        create_response = client.post(
            "/runs",
            json={"input_payload": {"value": 2}},
            headers=operator_headers(),
        )
        assert create_response.status_code == 202
        run_id = create_response.json()["run_id"]
        failed = _wait_for_status(
            client,
            run_id,
            "terminated_by_policy",
            headers=operator_headers(),
        )

    audits = await service.audit_repository.list_by_run(run_id)

    assert failed["terminal_output"] is None
    assert failed["failure_state"]["reason"] == "policy_violation"
    assert runner.calls == []
    assert len(audits) == 1
    assert audits[0].status == "rejected"
    assert audits[0].error == "capability denied: secret_access"
