"""Agent tool-edge dispatch: attached executable units run as tool calls.

Covers the full wiring: graph tool edges + bindings -> factory-built
tool attachments -> orchestrator-injected tool executor -> the attached
unit executing mid-agent-turn instead of as a graph step.
"""

from __future__ import annotations

import sys
from pathlib import Path

from pydantic import BaseModel

from zeroth.runtime.agents import DeterministicProviderAdapter, ProviderResponse
from zeroth.runtime.agents.factory import build_agent_runners
from zeroth.governance.audit import AuditRepository
from zeroth.contracts.registry import ContractRegistry
from zeroth.integrations.execution import (
    CommandArtifactSource,
    ExecutableUnitRegistry,
    ExecutableUnitRunner,
    ExecutionMode,
    InputMode,
    OutputMode,
    RunConfig,
    WrappedCommandUnitManifest,
)
from zeroth.contracts.graph import (
    AgentNodeData,
    AgentToolBinding,
    Edge,
    ExecutableUnitNodeData,
    Graph,
    ToolArgument,
)
from zeroth.contracts.graph.models import AgentNode, ExecutableUnitNode
from zeroth.core.orchestrator import RuntimeOrchestrator
from zeroth.integrations.persistence.runs import RunRepository
from zeroth.runtime.runs import RunStatus
from tests.conftest import content_capture


class NumberInput(BaseModel):
    value: int


class NumberOutput(BaseModel):
    value: int


def _tool_graph() -> Graph:
    return Graph(
        graph_id="graph-tools",
        name="tools",
        entry_step="agent",
        nodes=[
            AgentNode(
                node_id="agent",
                graph_version_ref="graph-tools:v1",
                input_contract_ref="contract://number-in",
                output_contract_ref="contract://number-out",
                agent=AgentNodeData(
                    instruction="double the value using your tool",
                    model_provider="provider://test",
                    tool_bindings=[
                        AgentToolBinding(
                            target_node_id="doubler",
                            name="double_value",
                            description="Doubles the given value",
                            arguments=[
                                ToolArgument(
                                    name="value",
                                    type="integer",
                                    description="The number to double",
                                )
                            ],
                        )
                    ],
                ),
            ),
            ExecutableUnitNode(
                node_id="doubler",
                graph_version_ref="graph-tools:v1",
                input_contract_ref="contract://number-in",
                output_contract_ref="contract://number-out",
                executable_unit=ExecutableUnitNodeData(
                    manifest_ref="eu://double",
                    execution_mode="wrapped_command",
                ),
            ),
        ],
        edges=[
            Edge(
                edge_id="tool-1",
                source_node_id="agent",
                target_node_id="doubler",
                kind="tool",
            )
        ],
    )


def _double_manifest(script: Path) -> WrappedCommandUnitManifest:
    return WrappedCommandUnitManifest(
        unit_id="double",
        onboarding_mode=ExecutionMode.WRAPPED_COMMAND,
        runtime="command",
        artifact_source=CommandArtifactSource(ref=str(script)),
        entrypoint_type="command",
        input_mode=InputMode.JSON_STDIN,
        output_mode=OutputMode.JSON_STDOUT,
        input_contract_ref="contract://number-in",
        output_contract_ref="contract://number-out",
        run_config=RunConfig(command=[sys.executable, str(script)]),
        cache_identity_fields={"script": script.name},
    )


async def test_agent_runs_attached_unit_as_tool_call(sqlite_db, tmp_path: Path) -> None:
    script = tmp_path / "double.py"
    script.write_text(
        "import json, sys\npayload = json.load(sys.stdin)\n"
        'print(json.dumps({"value": payload["value"] * 2}))\n',
        encoding="utf-8",
    )
    eu_registry = ExecutableUnitRegistry()
    eu_registry.register(
        "eu://double",
        _double_manifest(script),
        input_model=NumberInput,
        output_model=NumberOutput,
    )
    contract_registry = ContractRegistry(sqlite_db)
    await contract_registry.register(NumberInput, name="contract://number-in")
    await contract_registry.register(NumberOutput, name="contract://number-out")

    graph = _tool_graph()
    provider = DeterministicProviderAdapter(
        [
            ProviderResponse(
                content=None,
                tool_calls=[{"id": "call-1", "name": "double_value", "args": {"value": 21}}],
            ),
            ProviderResponse(content='{"value": 42}'),
        ]
    )
    runners = await build_agent_runners(graph, contract_registry, provider=provider)

    # The factory compiled the canvas bindings into declared tool manifests.
    attachments = runners["agent"].config.tool_attachments
    assert [a.alias for a in attachments] == ["double_value"]
    assert attachments[0].executable_unit_ref == "node://doubler"
    assert attachments[0].description == "Doubles the given value"
    assert attachments[0].parameters_schema["properties"]["value"]["type"] == "integer"

    orchestrator = RuntimeOrchestrator(
        audit_repository=content_capture(AuditRepository(sqlite_db)),
        run_repository=RunRepository(sqlite_db),
        agent_runners=runners,
        executable_unit_runner=ExecutableUnitRunner(eu_registry),
    )

    run = await orchestrator.run_graph(graph, {"value": 21})

    assert run.status is RunStatus.COMPLETED
    assert run.final_output == {"value": 42}
    # The attached unit ran inside the agent turn, not as a graph step.
    assert [entry.node_id for entry in run.execution_history] == ["agent"]

    audits = await AuditRepository(sqlite_db).list_by_run(run.run_id)
    assert len(audits) == 1
    tool_calls = audits[0].tool_calls
    assert len(tool_calls) == 1
    assert tool_calls[0].tool_ref == "node://doubler"
    assert tool_calls[0].outcome == {"value": 42}
