from __future__ import annotations

from zeroth.core.graph.models import (
    AgentNode,
    AgentNodeData,
    Condition,
    DisplayMetadata,
    Edge,
    ExecutableUnitNode,
    ExecutableUnitNodeData,
    ExecutionSettings,
    Graph,
    GraphStatus,
    HumanApprovalNode,
    HumanApprovalNodeData,
)
from zeroth.core.graph.validation import GraphValidator
from zeroth.core.graph.validation_errors import (
    GraphValidationError,
    ValidationCode,
)
from zeroth.core.mappings.models import (
    ConstantMappingOperation,
    DefaultMappingOperation,
    EdgeMapping,
    PassthroughMappingOperation,
    RenameMappingOperation,
)


def build_valid_graph(*, cycle_safeguard: bool = True) -> Graph:
    return Graph(
        graph_id="graph-validation-1",
        name="Validated Graph",
        version=1,
        status=GraphStatus.DRAFT,
        entry_step="agent-step",
        policy_bindings=["policy://safety"],
        deployment_settings={"environment": "test"},
        metadata={"owner": "team-a"},
        execution_settings=ExecutionSettings(
            max_total_steps=10,
            max_visits_per_node=2,
            max_visits_per_edge=2 if cycle_safeguard else None,
        ),
        nodes=[
            AgentNode(
                node_id="agent-step",
                graph_version_ref="graph-validation-1@1",
                display=DisplayMetadata(title="Agent"),
                input_contract_ref="contract://agent.input",
                output_contract_ref="contract://agent.output",
                policy_bindings=["policy://agent"],
                capability_bindings=["capability://memory-read"],
                agent=AgentNodeData(
                    instruction="Analyze the request",
                    model_provider="governai:model-router",
                    tool_refs=["tool://summarizer"],
                    memory_refs=["memory://run"],
                    retry_policy={"max_retries": 2},
                    state_persistence={"mode": "thread"},
                    thread_participation="full",
                ),
            ),
            ExecutableUnitNode(
                node_id="tool-step",
                graph_version_ref="graph-validation-1@1",
                display=DisplayMetadata(title="Tool"),
                input_contract_ref="contract://tool.input",
                output_contract_ref="contract://tool.output",
                policy_bindings=["policy://tool"],
                capability_bindings=["capability://filesystem-read"],
                executable_unit=ExecutableUnitNodeData(
                    manifest_ref="eu://summarizer",
                    execution_mode="wrapped_command",
                    runtime_binding="python",
                    sandbox_config={"network": "off"},
                    output_extraction_strategy="json_stdout",
                ),
            ),
            HumanApprovalNode(
                node_id="approval-step",
                graph_version_ref="graph-validation-1@1",
                display=DisplayMetadata(title="Approval"),
                input_contract_ref="contract://approval.input",
                output_contract_ref="contract://approval.output",
                policy_bindings=["policy://approval"],
                human_approval=HumanApprovalNodeData(
                    approval_payload_schema_ref="schema://approval",
                    resolution_schema_ref="schema://resolution",
                    approval_policy_config={"requires_rationale": True},
                    pause_behavior_config={"resume_mode": "async"},
                ),
            ),
        ],
        edges=[
            Edge(
                edge_id="edge-1",
                source_node_id="agent-step",
                target_node_id="tool-step",
                mapping=EdgeMapping(
                    operations=[
                        PassthroughMappingOperation(
                            source_path="payload.user.name",
                            target_path="request.user.name",
                        ),
                        RenameMappingOperation(
                            source_path="payload.user.id",
                            target_path="request.user.identifier",
                        ),
                        ConstantMappingOperation(
                            target_path="request.source",
                            value="zeroth",
                        ),
                        DefaultMappingOperation(
                            source_path="payload.user.locale",
                            target_path="request.user.locale",
                            default_value="en-US",
                        ),
                    ]
                ),
                condition=Condition(
                    expression="payload.user.id is not None",
                    operand_refs=["payload.user.id"],
                    allow_cycle_traversal=False,
                ),
            ),
            Edge(
                edge_id="edge-2",
                source_node_id="tool-step",
                target_node_id="approval-step",
            ),
            Edge(
                edge_id="edge-3",
                source_node_id="approval-step",
                target_node_id="agent-step",
                condition=Condition(
                    expression="continue_cycle",
                    operand_refs=["state.loop"],
                    allow_cycle_traversal=cycle_safeguard,
                ),
            ),
        ],
    )


def validation_codes(report) -> set[ValidationCode]:
    return {issue.code for issue in report.issues}


async def test_validator_accepts_well_formed_cyclic_graph_with_safeguard() -> None:
    report = await GraphValidator().validate(build_valid_graph(cycle_safeguard=True))

    assert report.is_valid
    assert report.summary() == {"errors": 0, "warnings": 0, "total": 0}
    assert report.errors == []
    assert report.warnings == []


async def test_validator_rejects_cyclic_graph_without_safeguard() -> None:
    report = await GraphValidator().validate(build_valid_graph(cycle_safeguard=False))

    assert not report.is_valid
    assert ValidationCode.UNSAFE_CYCLE in validation_codes(report)
    assert report.summary()["errors"] == 1


async def test_validator_reports_entrypoint_and_edge_errors() -> None:
    graph = build_valid_graph().model_copy(
        update={
            "entry_step": "missing-step",
            "edges": [
                Edge(
                    edge_id="edge-1",
                    source_node_id="missing-source",
                    target_node_id="tool-step",
                ),
                Edge(
                    edge_id="edge-1",
                    source_node_id="tool-step",
                    target_node_id="missing-target",
                ),
            ],
        }
    )

    report = await GraphValidator().validate(graph)

    assert ValidationCode.UNKNOWN_ENTRYPOINT in validation_codes(report)
    assert ValidationCode.UNKNOWN_EDGE_SOURCE in validation_codes(report)
    assert ValidationCode.UNKNOWN_EDGE_TARGET in validation_codes(report)
    assert ValidationCode.DUPLICATE_EDGE_ID in validation_codes(report)


async def test_validator_reports_contract_attachment_and_condition_errors() -> None:
    graph = build_valid_graph().model_copy(
        update={
            "nodes": [
                build_valid_graph()
                .nodes[0]
                .model_copy(
                    update={
                        "input_contract_ref": "",
                        "output_contract_ref": "",
                        "policy_bindings": [""],
                        "capability_bindings": [""],
                        "agent": build_valid_graph()
                        .nodes[0]
                        .agent.model_copy(
                            update={
                                "instruction": "",
                                "model_provider": "",
                                "tool_refs": [""],
                                "memory_refs": [""],
                            }
                        ),
                    }
                ),
                build_valid_graph()
                .nodes[1]
                .model_copy(
                    update={
                        "input_contract_ref": "",
                        "output_contract_ref": "",
                        "executable_unit": build_valid_graph()
                        .nodes[1]
                        .executable_unit.model_copy(update={"manifest_ref": ""}),
                    }
                ),
                build_valid_graph()
                .nodes[2]
                .model_copy(
                    update={
                        "input_contract_ref": "",
                        "output_contract_ref": "",
                        "human_approval": build_valid_graph()
                        .nodes[2]
                        .human_approval.model_copy(
                            update={
                                "approval_payload_schema_ref": "",
                                "resolution_schema_ref": "",
                            }
                        ),
                    }
                ),
            ],
            "policy_bindings": [""],
            "edges": [
                Edge(
                    edge_id="edge-1",
                    source_node_id="agent-step",
                    target_node_id="tool-step",
                    condition=Condition(expression="", operand_refs=["", "payload.user.id"]),
                )
            ],
        }
    )

    report = await GraphValidator().validate(graph)

    assert ValidationCode.MISSING_CONTRACT_REF in validation_codes(report)
    assert ValidationCode.INVALID_OUTPUT_CONTRACT in validation_codes(report)
    assert ValidationCode.INVALID_POLICY_REF in validation_codes(report)
    assert ValidationCode.INVALID_CAPABILITY_REF in validation_codes(report)
    assert ValidationCode.INVALID_NODE_ATTACHMENT in validation_codes(report)
    assert ValidationCode.INVALID_CONDITION in validation_codes(report)


async def test_validator_reports_invalid_mapping_and_raise_helper() -> None:
    graph = build_valid_graph().model_copy(
        update={
            "edges": [
                Edge(
                    edge_id="edge-1",
                    source_node_id="agent-step",
                    target_node_id="tool-step",
                    mapping=EdgeMapping(
                        operations=[
                            PassthroughMappingOperation(
                                source_path="payload.user.name",
                                target_path="request.user.name",
                            ),
                            RenameMappingOperation(
                                source_path="payload.user.id",
                                target_path="request.user.name",
                            ),
                        ]
                    ),
                )
            ]
        }
    )

    validator = GraphValidator()
    report = await validator.validate(graph)

    assert ValidationCode.INVALID_MAPPING in validation_codes(report)

    try:
        await validator.validate_or_raise(graph)
    except GraphValidationError as exc:
        assert exc.report == report
    else:  # pragma: no cover - defensive guard
        raise AssertionError("expected validation error")
