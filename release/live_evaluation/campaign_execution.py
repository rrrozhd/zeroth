"""Declarative workflow campaign specifications with no live execution client.

The builders in this module produce real Zeroth graph contracts and coordinator
actions.  The actions are inert until an explicit ``CampaignExecutionBackend``
is supplied; the default backend fails closed before any network or provider call.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from zeroth.contracts.graph.models import (
    AgentNode,
    AgentNodeData,
    Condition,
    DisplayMetadata,
    Edge,
    EntrypointNode,
    ExecutableUnitNode,
    ExecutableUnitNodeData,
    ExecutionSettings,
    Graph,
    HumanApprovalNode,
    HumanApprovalNodeData,
    IfNode,
    IfNodeData,
    JoinConfig,
    LoopNode,
    LoopNodeData,
    ParallelConfig,
    RetrievalNode,
    RetrievalNodeData,
    SubgraphNode,
    SubgraphNodeData,
)
from zeroth.contracts.graph.validation.control_nodes import canonical_if_route_condition
from zeroth.contracts.mappings.models import (
    ConstantMappingOperation,
    EdgeMapping,
    PassthroughMappingOperation,
)

from .action_sink import ActionSinkFault
from .coordinator import (
    ActionRecorder,
    CampaignPlan,
    CampaignStep,
    Phase,
    StepResult,
)
from .criteria import original_acceptance_criteria

_IDENTITY_TEMPLATE = {
    "campaign_id": "${campaign_id}",
    "operation_id": "${operation_id}",
    "run_id": "${run_id}",
    "fail_closed": True,
}


class Workflow1Query(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=65_536)


class Workflow1Retrieved(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str
    sources: list[dict[str, object]] = Field(max_length=3)


class Workflow1Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str
    answer: str
    source_ids: list[str]
    revision_required: bool = False
    revision_count: int = Field(default=0, ge=0, le=1)


class Workflow1LoopState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    route: Literal["repeat", "done", "limit"]
    attempt: int = Field(ge=1)
    retries_used: int = Field(ge=0, le=1)
    max_retries: Literal[1]
    termination_reason: Literal["condition_met", "max_retries_exhausted"] | None = None


class Workflow1LoopOutcome(Workflow1Answer):
    zeroth_loop: dict[str, Workflow1LoopState]


class Workflow2Item(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index: int = Field(ge=0)
    query: str = Field(min_length=1, max_length=16_384)
    evaluation_behavior: Literal["child_pause", "child_failure"] | None = None


class Workflow2BatchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[Workflow2Item] = Field(min_length=1, max_length=24)

    @model_validator(mode="after")
    def _unique_indices(self) -> Workflow2BatchInput:
        indices = [item.index for item in self.items]
        if len(indices) != len(set(indices)):
            raise ValueError("batch item indices must be unique")
        return self


class Workflow2Retrieved(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index: int
    query: str
    sources: list[dict[str, object]] = Field(max_length=3)
    evaluation_behavior: Literal["child_pause", "child_failure"] | None = None


class Workflow2ChildResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    index: int
    answer: str | None = None
    source_ids: list[str] = Field(default_factory=list)
    error: str | None = None


class Workflow2CollectedResults(BaseModel):
    model_config = ConfigDict(extra="forbid")
    results: list[Workflow2ChildResult]


class Workflow3ActionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticket: str = Field(pattern=r"^synthetic-[a-z0-9][a-z0-9-]{0,79}$")
    status: Literal["remediated"]
    operation_key: str | None = None
    fault: ActionSinkFault | None = None
    evaluation_behavior: Literal["cancel_after_approval"] | None = None


class Workflow3ActionReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_key: str
    payload_hash: str
    receipt: str
    created_at: str


class Workflow3ApprovalResolution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["approve", "reject"]
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ContractSpec:
    ref: str
    model: type[BaseModel]


@dataclass(frozen=True, slots=True)
class CampaignExecutionSettings:
    campaign_id: str
    tenant_id: str
    model: str
    embedding_model: str
    chroma_connector_ref: str
    workspace_id: str | None = None

    def __post_init__(self) -> None:
        required = {
            "campaign_id": self.campaign_id,
            "tenant_id": self.tenant_id,
            "model": self.model,
            "embedding_model": self.embedding_model,
            "chroma_connector_ref": self.chroma_connector_ref,
        }
        missing = [name for name, value in required.items() if not value.strip()]
        if missing:
            raise ValueError(f"campaign execution settings are blank: {', '.join(missing)}")


@dataclass(frozen=True, slots=True)
class DeploymentReferences:
    workflow1: str
    workflow2_child: str
    workflow2_parent: str
    workflow3: str


@dataclass(frozen=True, slots=True)
class WorkflowGraphs:
    workflow1: Graph
    workflow2_child: Graph
    workflow2_parent: Graph
    workflow3: Graph


@dataclass(frozen=True, slots=True)
class ExecutionIdentity:
    campaign_id: str
    operation_id: str
    run_id: str

    def __post_init__(self) -> None:
        if not all((self.campaign_id, self.operation_id, self.run_id)):
            raise ValueError("campaign, operation, and run identities are required")


FaultTarget = Literal[
    "input",
    "provider",
    "connector",
    "runtime",
    "ui",
    "action_sink",
    "action_outcome_lookup",
]


@dataclass(frozen=True, slots=True)
class FaultInjection:
    target: FaultTarget
    mode: str
    deterministic: bool = True
    parameters: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True)
class ActionRequest:
    identity: ExecutionIdentity
    deployment_ref: str
    input_payload: Mapping[str, object]
    fault: FaultInjection | None = None

    @property
    def body(self) -> dict[str, object]:
        """Return the current, callable ``POST /runs`` request body.

        ``operation_id`` is a harness/evidence identity and ``run_id`` is a
        response correlation expectation.  Neither is accepted by today's run
        invocation schema, so emitting either here would make the adapter lie
        about what the service can consume.
        """
        return {
            "campaign_id": self.identity.campaign_id,
            "campaign_strict": True,
            "input_payload": dict(self.input_payload),
        }

    @property
    def correlation_expectations(self) -> dict[str, str]:
        """Identifiers every action must reconcile after the server responds."""
        return {
            "campaign_id": self.identity.campaign_id,
            "operation_id": self.identity.operation_id,
            "run_id": self.identity.run_id,
        }

    @property
    def fault_control(self) -> dict[str, object] | None:
        """Return an out-of-band deterministic test-control instruction."""
        if self.fault is not None:
            return {
                "deterministic": self.fault.deterministic,
                "mode": self.fault.mode,
                "parameters": dict(self.fault.parameters or {}),
                "target": self.fault.target,
            }
        return None


@dataclass(frozen=True, slots=True)
class WorkflowAction:
    workflow: Literal["workflow1", "workflow2", "workflow3"]
    scenario: str
    action_type: Literal["deployment_gate", "run", "negative"]
    criterion_ids: tuple[str, ...]
    request: ActionRequest
    graph_specs: tuple[Graph, ...] = ()
    deployment_refs: tuple[str, ...] = ()


class CampaignExecutionBackend(Protocol):
    """Explicit seam for a future API/browser implementation."""

    def execute(self, action: WorkflowAction, recorder: ActionRecorder) -> StepResult: ...


class UnconfiguredExecutionBackend:
    """Default backend: fail before any external action can occur."""

    def execute(self, action: WorkflowAction, recorder: ActionRecorder) -> StepResult:
        del action, recorder
        raise RuntimeError("campaign execution backend is not configured")


@dataclass(frozen=True, slots=True)
class CampaignExecution:
    settings: CampaignExecutionSettings
    deployments: DeploymentReferences
    graphs: WorkflowGraphs
    actions: tuple[WorkflowAction, ...]
    contracts: tuple[ContractSpec, ...]
    plan: CampaignPlan


def _instrumented_config(*, embedding_model: str | None = None) -> dict[str, object]:
    result: dict[str, object] = {"instrumentation": dict(_IDENTITY_TEMPLATE)}
    if embedding_model is not None:
        result["embedding_model"] = embedding_model
    return result


def _entrypoint(
    node_id: str,
    graph_id: str,
    *,
    input_ref: str,
    output_ref: str,
    **updates: object,
) -> EntrypointNode:
    return EntrypointNode(
        node_id=node_id,
        graph_version_ref=f"{graph_id}@1",
        input_contract_ref=input_ref,
        output_contract_ref=output_ref,
        **updates,
    )


def _workflow1_graph(settings: CampaignExecutionSettings) -> Graph:
    graph_id = f"{settings.campaign_id}-grounded-researcher"
    query_ref = f"{settings.campaign_id}.workflow1.query@1"
    retrieved_ref = f"{settings.campaign_id}.workflow1.retrieved@1"
    answer_ref = f"{settings.campaign_id}.workflow1.answer@1"
    loop_outcome_ref = f"{settings.campaign_id}.workflow1.loop-outcome@1"
    retrieval = RetrievalNode(
        node_id="retrieve",
        graph_version_ref=f"{graph_id}@1",
        input_contract_ref=query_ref,
        output_contract_ref=retrieved_ref,
        capability_bindings=["memory_read"],
        execution_config=_instrumented_config(embedding_model=settings.embedding_model),
        retrieval=RetrievalNodeData(
            connector_ref=settings.chroma_connector_ref,
            query_key="query",
            top_k=3,
            scope="shared",
            as_name="sources",
        ),
    )
    researcher = AgentNode(
        node_id="research",
        graph_version_ref=f"{graph_id}@1",
        input_contract_ref=retrieved_ref,
        output_contract_ref=answer_ref,
        execution_config=_instrumented_config(),
        agent=AgentNodeData(
            instruction=(
                "Answer only from retrieved sources. Return a structured answer, source IDs, "
                "and revision_required. Request at most one revision."
            ),
            model_provider=settings.model,
            max_tool_calls=0,
            timeout_seconds=45,
            model_params={"temperature": 0, "max_tokens": 800},
        ),
    )
    revision_loop = LoopNode(
        node_id="revision-loop",
        graph_version_ref=f"{graph_id}@1",
        input_contract_ref=answer_ref,
        output_contract_ref=loop_outcome_ref,
        loop=LoopNodeData(
            until="payload.revision_required != True",
            max_retries=1,
        ),
    )
    return Graph(
        graph_id=graph_id,
        name="Evaluation grounded researcher",
        tenant_id=settings.tenant_id,
        workspace_id=settings.workspace_id,
        entry_step="request",
        nodes=[
            _entrypoint("request", graph_id, input_ref=query_ref, output_ref=query_ref),
            revision_loop,
            retrieval,
            researcher,
        ],
        edges=[
            Edge(
                edge_id="request-revision-loop",
                source_node_id="request",
                target_node_id="revision-loop",
                mapping=EdgeMapping(
                    operations=[
                        PassthroughMappingOperation(
                            source_path="query",
                            target_path="query",
                        ),
                        ConstantMappingOperation(target_path="answer", value=""),
                        ConstantMappingOperation(target_path="source_ids", value=[]),
                        ConstantMappingOperation(
                            target_path="revision_required",
                            value=False,
                        ),
                        ConstantMappingOperation(target_path="revision_count", value=0),
                    ]
                ),
                metadata={
                    "source_handle": "output-data",
                    "target_handle": "input-data",
                },
            ),
            Edge(
                edge_id="revision-loop-retrieve",
                source_node_id="revision-loop",
                target_node_id="retrieve",
                mapping=EdgeMapping(
                    operations=[
                        PassthroughMappingOperation(
                            source_path="query",
                            target_path="query",
                        )
                    ]
                ),
                condition=Condition(
                    expression=(
                        "payload.zeroth_loop['revision-loop'].route == 'repeat'"
                    ),
                    operand_refs=["payload.zeroth_loop.revision-loop.route"],
                    allow_cycle_traversal=True,
                    metadata={"loop_route": "repeat"},
                ),
                metadata={
                    "source_handle": "repeat",
                    "target_handle": "input-data",
                },
            ),
            Edge(edge_id="retrieve-research", source_node_id="retrieve", target_node_id="research"),
            Edge(
                edge_id="research-revision-loop",
                source_node_id="research",
                target_node_id="revision-loop",
                condition=Condition(
                    expression="True",
                    allow_cycle_traversal=True,
                    metadata={"purpose": "reevaluate_revision"},
                ),
            ),
        ],
        execution_settings=ExecutionSettings(
            max_total_steps=8,
            max_total_runtime_seconds=90,
            max_visits_per_node=3,
            max_visits_per_edge=2,
            default_timeout_seconds=45,
            failure_policy="fail_fast",
            audit_enabled=True,
            sequential_join_enabled=True,
        ),
        metadata={"evaluation_workflow": "workflow1", "loop_bound": 2},
    )


def _workflow2_child_graph(settings: CampaignExecutionSettings) -> Graph:
    graph_id = f"{settings.campaign_id}-batched-investigation-child"
    item_ref = f"{settings.campaign_id}.workflow2.item@1"
    retrieved_ref = f"{settings.campaign_id}.workflow2.retrieved@1"
    result_ref = f"{settings.campaign_id}.workflow2.child-result@1"
    return Graph(
        graph_id=graph_id,
        name="Evaluation investigation child",
        tenant_id=settings.tenant_id,
        workspace_id=settings.workspace_id,
        entry_step="request",
        nodes=[
            _entrypoint("request", graph_id, input_ref=item_ref, output_ref=item_ref),
            RetrievalNode(
                node_id="retrieve",
                graph_version_ref=f"{graph_id}@1",
                input_contract_ref=item_ref,
                output_contract_ref=retrieved_ref,
                capability_bindings=["memory_read"],
                execution_config=_instrumented_config(embedding_model=settings.embedding_model),
                retrieval=RetrievalNodeData(
                    connector_ref=settings.chroma_connector_ref,
                    query_key="query",
                    top_k=3,
                    scope="shared",
                    as_name="sources",
                ),
            ),
            AgentNode(
                node_id="investigate",
                graph_version_ref=f"{graph_id}@1",
                input_contract_ref=retrieved_ref,
                output_contract_ref=result_ref,
                execution_config=_instrumented_config(),
                agent=AgentNodeData(
                    instruction="Investigate one item from retrieved sources and return its index.",
                    model_provider=settings.model,
                    max_tool_calls=0,
                    timeout_seconds=45,
                    model_params={"temperature": 0, "max_tokens": 500},
                ),
            ),
            HumanApprovalNode(
                node_id="evaluation-child-pause",
                graph_version_ref=f"{graph_id}@1",
                input_contract_ref=retrieved_ref,
                output_contract_ref=retrieved_ref,
                execution_config={"instrumentation": dict(_IDENTITY_TEMPLATE)},
                human_approval=HumanApprovalNodeData(
                    approval_payload_schema_ref=retrieved_ref,
                    resolution_schema_ref=result_ref,
                    approval_policy_config={
                        "allow_edits": False,
                        "require_explicit_decision": True,
                    },
                    pause_behavior_config={"persist_before_pause": True},
                    sla_timeout_seconds=300,
                ),
            ),
            ExecutableUnitNode(
                node_id="evaluation-child-failure",
                graph_version_ref=f"{graph_id}@1",
                input_contract_ref=retrieved_ref,
                output_contract_ref=result_ref,
                execution_config={"instrumentation": dict(_IDENTITY_TEMPLATE)},
                executable_unit=ExecutableUnitNodeData(
                    manifest_ref="evaluation://controlled-failure/v1",
                    execution_mode="native",
                    timeout_seconds=5,
                    output_extraction_strategy="json_stdout",
                ),
            ),
        ],
        edges=[
            Edge(edge_id="request-retrieve", source_node_id="request", target_node_id="retrieve"),
            Edge(
                edge_id="retrieve-investigate",
                source_node_id="retrieve",
                target_node_id="investigate",
                condition=Condition(
                    expression="payload.evaluation_behavior is None",
                    operand_refs=["payload.evaluation_behavior"],
                ),
            ),
            Edge(
                edge_id="retrieve-evaluation-pause",
                source_node_id="retrieve",
                target_node_id="evaluation-child-pause",
                condition=Condition(
                    expression="payload.evaluation_behavior == 'child_pause'",
                    operand_refs=["payload.evaluation_behavior"],
                ),
            ),
            Edge(
                edge_id="retrieve-evaluation-failure",
                source_node_id="retrieve",
                target_node_id="evaluation-child-failure",
                condition=Condition(
                    expression="payload.evaluation_behavior == 'child_failure'",
                    operand_refs=["payload.evaluation_behavior"],
                ),
            ),
        ],
        execution_settings=ExecutionSettings(
            max_total_steps=4,
            max_total_runtime_seconds=60,
            max_visits_per_node=1,
            audit_enabled=True,
            sequential_join_enabled=True,
        ),
        metadata={"evaluation_workflow": "workflow2-child"},
    )


def _workflow2_parent_graph(
    settings: CampaignExecutionSettings, *, child_deployment_ref: str
) -> Graph:
    graph_id = f"{settings.campaign_id}-batched-investigation-parent"
    batch_ref = f"{settings.campaign_id}.workflow2.batch@1"
    item_ref = f"{settings.campaign_id}.workflow2.item@1"
    result_ref = f"{settings.campaign_id}.workflow2.child-result@1"
    collected_ref = f"{settings.campaign_id}.workflow2.collected@1"
    return Graph(
        graph_id=graph_id,
        name="Evaluation batched investigation parent",
        tenant_id=settings.tenant_id,
        workspace_id=settings.workspace_id,
        entry_step="request",
        nodes=[
            _entrypoint(
                "request",
                graph_id,
                input_ref=batch_ref,
                output_ref=batch_ref,
                parallel_config=ParallelConfig(
                    split_path="items",
                    merge_strategy="collect",
                    fail_mode="best_effort",
                    max_branches=24,
                    max_concurrency=4,
                    batch_size=4,
                    branch_timeout_seconds=60,
                ),
            ),
            SubgraphNode(
                node_id="investigate-child",
                graph_version_ref=f"{graph_id}@1",
                input_contract_ref=item_ref,
                output_contract_ref=result_ref,
                execution_config={"instrumentation": dict(_IDENTITY_TEMPLATE)},
                subgraph=SubgraphNodeData(
                    graph_ref=child_deployment_ref,
                    # The child repair is deployed as immutable deployment v2.
                    # Pin it explicitly: ``None`` would float to latest, while v1
                    # predates the Entrypoint/capability/condition repairs.
                    version=2,
                    thread_participation="isolated",
                    max_depth=2,
                ),
            ),
            AgentNode(
                node_id="synthesize",
                graph_version_ref=f"{graph_id}@1",
                input_contract_ref=collected_ref,
                output_contract_ref=collected_ref,
                execution_config=_instrumented_config(),
                join_config=JoinConfig(merge_strategy="collect", merge_path="results"),
                agent=AgentNodeData(
                    instruction=(
                        "Synthesize the collected child results in ascending authored index "
                        "order. Preserve failures and pauses as explicit partial results."
                    ),
                    model_provider=settings.model,
                    max_tool_calls=0,
                    timeout_seconds=45,
                    model_params={"temperature": 0, "max_tokens": 1000},
                ),
            ),
        ],
        edges=[
            Edge(
                edge_id="request-investigate",
                source_node_id="request",
                target_node_id="investigate-child",
            ),
            Edge(
                edge_id="investigate-synthesize",
                source_node_id="investigate-child",
                target_node_id="synthesize",
            ),
        ],
        execution_settings=ExecutionSettings(
            max_total_steps=40,
            max_total_runtime_seconds=180,
            max_visits_per_node=24,
            audit_enabled=True,
            sequential_join_enabled=True,
        ),
        metadata={
            "evaluation_workflow": "workflow2-parent",
            "ordered_fan_in": True,
            "preserve_input_index_order": True,
            "batch_validation": "tenant_contract",
            "required_happy_item_count": 8,
        },
    )


def _workflow3_graph(settings: CampaignExecutionSettings) -> Graph:
    graph_id = f"{settings.campaign_id}-governed-remediation"
    action_ref = f"{settings.campaign_id}.workflow3.action@1"
    receipt_ref = f"{settings.campaign_id}.workflow3.receipt@1"
    resolution_ref = f"{settings.campaign_id}.workflow3.approval-resolution@1"
    return Graph(
        graph_id=graph_id,
        name="Evaluation governed remediation",
        tenant_id=settings.tenant_id,
        workspace_id=settings.workspace_id,
        entry_step="request",
        nodes=[
            _entrypoint("request", graph_id, input_ref=action_ref, output_ref=action_ref),
            HumanApprovalNode(
                node_id="approval",
                graph_version_ref=f"{graph_id}@1",
                input_contract_ref=action_ref,
                output_contract_ref=action_ref,
                execution_config={"instrumentation": dict(_IDENTITY_TEMPLATE)},
                human_approval=HumanApprovalNodeData(
                    approval_payload_schema_ref=action_ref,
                    resolution_schema_ref=resolution_ref,
                    approval_policy_config={
                        "allow_edits": False,
                        "require_explicit_decision": True,
                    },
                    pause_behavior_config={"persist_before_pause": True},
                    # Five seconds proved impossible to operate reliably in the
                    # real Safari console: hydration plus keyboard navigation
                    # could let the SLA enforcer win before a reviewer click
                    # reached the service. Keep expiry strict, but human-scale.
                    sla_timeout_seconds=60,
                    escalation_action="auto_reject",
                ),
            ),
            IfNode(
                node_id="evaluation-route",
                graph_version_ref=f"{graph_id}@1",
                display=DisplayMetadata(title="Route remediation"),
                input_contract_ref=action_ref,
                output_contract_ref=action_ref,
                execution_config={"instrumentation": dict(_IDENTITY_TEMPLATE)},
                condition=IfNodeData(
                    expression="payload.evaluation_behavior == 'cancel_after_approval'"
                ),
            ),
            HumanApprovalNode(
                node_id="evaluation-pre-action-barrier",
                graph_version_ref=f"{graph_id}@1",
                input_contract_ref=action_ref,
                output_contract_ref=action_ref,
                execution_config={"instrumentation": dict(_IDENTITY_TEMPLATE)},
                human_approval=HumanApprovalNodeData(
                    approval_payload_schema_ref=action_ref,
                    resolution_schema_ref=resolution_ref,
                    approval_policy_config={
                        "allow_edits": False,
                        "require_explicit_decision": True,
                    },
                    pause_behavior_config={"persist_before_pause": True},
                    sla_timeout_seconds=300,
                ),
            ),
            ExecutableUnitNode(
                node_id="synthetic-action",
                graph_version_ref=f"{graph_id}@1",
                input_contract_ref=action_ref,
                output_contract_ref=receipt_ref,
                execution_config={"instrumentation": dict(_IDENTITY_TEMPLATE)},
                executable_unit=ExecutableUnitNodeData(
                    manifest_ref="evaluation://synthetic-action/v1",
                    execution_mode="native",
                    timeout_seconds=30,
                    output_extraction_strategy="json_stdout",
                ),
            ),
        ],
        edges=[
            Edge(edge_id="request-approval", source_node_id="request", target_node_id="approval"),
            Edge(
                edge_id="approval-evaluation-route",
                source_node_id="approval",
                target_node_id="evaluation-route",
            ),
            Edge(
                edge_id="evaluation-route-action",
                source_node_id="evaluation-route",
                target_node_id="synthetic-action",
                condition=canonical_if_route_condition("evaluation-route", "false"),
                metadata={"source_handle": "false"},
            ),
            Edge(
                edge_id="evaluation-route-barrier",
                source_node_id="evaluation-route",
                target_node_id="evaluation-pre-action-barrier",
                condition=canonical_if_route_condition("evaluation-route", "true"),
                metadata={"source_handle": "true"},
            ),
            Edge(
                edge_id="evaluation-barrier-action",
                source_node_id="evaluation-pre-action-barrier",
                target_node_id="synthetic-action",
            ),
        ],
        execution_settings=ExecutionSettings(
            max_total_steps=6,
            max_total_runtime_seconds=420,
            max_visits_per_node=1,
            audit_enabled=True,
            sequential_join_enabled=True,
        ),
        metadata={
            "action_allowlist": ["evaluation://synthetic-action/v1"],
            "evaluation_workflow": "workflow3",
        },
    )


_SHARED_HAPPY_CRITERIA = {
    "workflow1": (
        "workflow1.semantic-retrieval",
        "workflow1.structured-payload",
        "workflow1.loop-bound-two",
        "workflow1.final-output",
        "workflow1.audit-chain",
        "workflow1.economics",
    ),
    "workflow2": (
        "workflow2.eight-items-concurrency-four",
        "workflow2.child-runs-isolated",
        "workflow2.ordering-and-join",
        "workflow2.aggregate-economics",
    ),
    "workflow3": ("workflow3.exactly-one-marker-each",),
}

_SETUP_CRITERIA = {
    "workflow1": (
        "workflow1.health-exact-graph-version",
        "workflow1.publish-deploy-restart",
        "workflow1.deterministic-provider-fault-injection",
    ),
    "workflow2": (
        "workflow2.health-exact-graph-version",
        "workflow2.child-publish-deploy",
        "workflow2.parent-publish-deploy-restart",
        "workflow2.recursive-runner-inventory",
    ),
    "workflow3": (
        "workflow3.health-exact-graph-version",
        "workflow3.signed-action-sink-registered",
        "workflow3.publish-deploy-restart",
    ),
}

_NEGATIVE_FAULTS: dict[str, FaultInjection] = {
    "no-result": FaultInjection("input", "retrieval_no_result"),
    "conflicting-document": FaultInjection("input", "conflicting_documents"),
    "empty-query": FaultInjection("input", "empty_query"),
    "oversized-query": FaultInjection("input", "oversized_query", parameters={"bytes": 65537}),
    "chroma-unavailable": FaultInjection("connector", "unavailable", parameters={"once": True}),
    "bad-credential": FaultInjection("provider", "invalid_secret_reference"),
    "provider-timeout": FaultInjection("provider", "timeout", parameters={"after_ms": 10}),
    "rate-limit": FaultInjection("provider", "rate_limit", parameters={"status": 429}),
    "malformed-response": FaultInjection("provider", "malformed_response"),
    "excessive-revision": FaultInjection("provider", "revision_required", parameters={"uses": 2}),
    "empty-batch": FaultInjection("input", "empty_batch"),
    "over-24-batch": FaultInjection("input", "branch_limit_exceeded", parameters={"items": 25}),
    "malformed-item": FaultInjection("input", "malformed_batch_item"),
    "retrieval-miss": FaultInjection("connector", "retrieval_miss"),
    "cancellation": FaultInjection("runtime", "cancel_after_child", parameters={"child_index": 1}),
    "refresh-restoration": FaultInjection("ui", "refresh_during_run"),
    "child-pause-partial-collection": FaultInjection(
        "runtime", "child_pause", parameters={"child_index": 3}
    ),
    "child-failure-partial-collection": FaultInjection(
        "runtime", "child_failure", parameters={"child_index": 3}
    ),
    "rejection-zero-marker": FaultInjection("ui", "approval_rejected"),
    "refresh-before-approval": FaultInjection("ui", "refresh_before_approval"),
    "sla-expiry": FaultInjection("runtime", "approval_sla_expired"),
    "duplicate-submission": FaultInjection("ui", "duplicate_approval_submission"),
    "cancellation-after-approval": FaultInjection("runtime", "cancel_after_approval"),
    "restart-around-receipt": FaultInjection("runtime", "restart_around_action_receipt"),
    "sink-unavailable": FaultInjection("action_sink", "unavailable", parameters={"once": True}),
    "timeout-after-commit": FaultInjection("action_sink", "timeout_after_commit"),
    "ambiguous-no-reexecution": FaultInjection("action_outcome_lookup", "unavailable"),
}


def _deployment_references(campaign_id: str) -> DeploymentReferences:
    return DeploymentReferences(
        workflow1=f"{campaign_id}-grounded-researcher-v1",
        workflow2_child=f"{campaign_id}-batched-investigation-child-v1",
        workflow2_parent=f"{campaign_id}-batched-investigation-parent-v1",
        workflow3=f"{campaign_id}-governed-remediation-v1",
    )


def _contract_specs(campaign_id: str) -> tuple[ContractSpec, ...]:
    definitions = (
        ("workflow1.query", Workflow1Query),
        ("workflow1.retrieved", Workflow1Retrieved),
        ("workflow1.answer", Workflow1Answer),
        ("workflow1.loop-outcome", Workflow1LoopOutcome),
        ("workflow2.item", Workflow2Item),
        ("workflow2.batch", Workflow2BatchInput),
        ("workflow2.retrieved", Workflow2Retrieved),
        ("workflow2.child-result", Workflow2ChildResult),
        ("workflow2.collected", Workflow2CollectedResults),
        ("workflow3.action", Workflow3ActionPayload),
        ("workflow3.receipt", Workflow3ActionReceipt),
        ("workflow3.approval-resolution", Workflow3ApprovalResolution),
    )
    return tuple(
        ContractSpec(ref=f"{campaign_id}.{name}@1", model=model) for name, model in definitions
    )


def _happy_input(workflow: str, repetition: int) -> dict[str, object]:
    if workflow == "workflow1":
        return {"query": f"known-answer-{repetition}"}
    if workflow == "workflow2":
        return {
            "items": [
                {"index": index, "query": f"investigation-{repetition}-{index}"}
                for index in range(8)
            ]
        }
    return {"ticket": f"synthetic-ticket-{repetition}", "status": "remediated"}


def _negative_input(workflow: str, suffix: str) -> dict[str, object]:
    """Reach the intended fault boundary except for explicit input-contract cases."""
    if workflow == "workflow1":
        if suffix == "empty-query":
            return {"query": ""}
        if suffix == "oversized-query":
            return {"query": "x" * 65_537}
        query = {
            "no-result": "synthetic-no-result",
            "conflicting-document": "synthetic-conflict",
            "excessive-revision": "synthetic-excessive-revision",
        }.get(suffix, "known-answer-1")
        return {"query": query}
    if workflow == "workflow2":
        if suffix == "empty-batch":
            return {"items": []}
        if suffix == "over-24-batch":
            return {
                "items": [
                    {"index": index, "query": f"investigation-over-limit-{index}"}
                    for index in range(25)
                ]
            }
        if suffix == "malformed-item":
            return {"items": [{"index": 0}]}
        result = {
            "items": [
                {"index": index, "query": f"investigation-negative-{suffix}-{index}"}
                for index in range(8)
            ]
        }
        if suffix in {
            "child-pause-partial-collection",
            "child-failure-partial-collection",
        }:
            result["items"][3]["evaluation_behavior"] = (
                "child_pause" if suffix == "child-pause-partial-collection" else "child_failure"
            )
        return result
    payload: dict[str, object] = {
        "ticket": f"synthetic-negative-{suffix}",
        "status": "remediated",
    }
    if suffix in {
        "sink-unavailable",
        "timeout-after-commit",
        "ambiguous-no-reexecution",
    }:
        payload["fault"] = (
            "unavailable" if suffix == "sink-unavailable" else "timeout_after_commit"
        )
    if suffix == "cancellation-after-approval":
        payload["evaluation_behavior"] = "cancel_after_approval"
    return payload


def _action(
    settings: CampaignExecutionSettings,
    deployments: DeploymentReferences,
    *,
    workflow: Literal["workflow1", "workflow2", "workflow3"],
    scenario: str,
    criterion_ids: tuple[str, ...],
    action_type: Literal["deployment_gate", "run", "negative"],
    input_payload: Mapping[str, object],
    fault: FaultInjection | None = None,
    graph_specs: tuple[Graph, ...] = (),
    deployment_refs: tuple[str, ...] = (),
) -> WorkflowAction:
    deployment_ref = getattr(
        deployments, "workflow2_parent" if workflow == "workflow2" else workflow
    )
    stable = f"{settings.campaign_id}:{workflow}:{scenario}"
    return WorkflowAction(
        workflow=workflow,
        scenario=scenario,
        action_type=action_type,
        criterion_ids=criterion_ids,
        request=ActionRequest(
            identity=ExecutionIdentity(
                campaign_id=settings.campaign_id,
                operation_id=f"{stable}:operation",
                run_id=f"${{response.run_id:{workflow}:{scenario}}}",
            ),
            deployment_ref=deployment_ref,
            input_payload=input_payload,
            fault=fault,
        ),
        graph_specs=graph_specs,
        deployment_refs=deployment_refs,
    )


def _build_actions(
    settings: CampaignExecutionSettings,
    deployments: DeploymentReferences,
    graphs: WorkflowGraphs,
) -> tuple[WorkflowAction, ...]:
    actions: list[WorkflowAction] = []
    catalog = tuple(
        criterion.criterion_id
        for criterion in original_acceptance_criteria()
        if criterion.criterion_id.startswith(("workflow1.", "workflow2.", "workflow3."))
    )
    catalog_ids = set(catalog)
    for workflow in ("workflow1", "workflow2", "workflow3"):
        actions.append(
            _action(
                settings,
                deployments,
                workflow=workflow,
                scenario="health",
                criterion_ids=_SETUP_CRITERIA[workflow],
                action_type="deployment_gate",
                input_payload={"expected_graph_version": 1},
                graph_specs=(
                    (graphs.workflow1,)
                    if workflow == "workflow1"
                    else (
                        (graphs.workflow2_child, graphs.workflow2_parent)
                        if workflow == "workflow2"
                        else (graphs.workflow3,)
                    )
                ),
                deployment_refs=(
                    (deployments.workflow1,)
                    if workflow == "workflow1"
                    else (
                        (deployments.workflow2_child, deployments.workflow2_parent)
                        if workflow == "workflow2"
                        else (deployments.workflow3,)
                    )
                ),
            )
        )
        for repetition in range(1, 4):
            shared = _SHARED_HAPPY_CRITERIA[workflow] if repetition == 1 else ()
            actions.append(
                _action(
                    settings,
                    deployments,
                    workflow=workflow,
                    scenario=f"happy-{repetition}",
                    criterion_ids=(f"{workflow}.happy-{repetition}", *shared),
                    action_type="run",
                    input_payload=_happy_input(workflow, repetition),
                )
            )
        for criterion_id in (item for item in catalog if item.startswith(f"{workflow}.negative-")):
            suffix = criterion_id.removeprefix(f"{workflow}.negative-")
            try:
                fault = _NEGATIVE_FAULTS[suffix]
            except KeyError as exc:
                raise ValueError(
                    f"negative case lacks a deterministic plan: {criterion_id}"
                ) from exc
            actions.append(
                _action(
                    settings,
                    deployments,
                    workflow=workflow,
                    scenario=f"negative-{suffix}",
                    criterion_ids=(criterion_id,),
                    action_type="negative",
                    input_payload=_negative_input(workflow, suffix),
                    fault=fault,
                )
            )
        if workflow == "workflow3" and "workflow3.ambiguous-no-reexecution" in catalog_ids:
            actions.append(
                _action(
                    settings,
                    deployments,
                    workflow="workflow3",
                    scenario="negative-ambiguous-no-reexecution",
                    criterion_ids=("workflow3.ambiguous-no-reexecution",),
                    action_type="negative",
                    input_payload=_negative_input("workflow3", "ambiguous-no-reexecution"),
                    fault=_NEGATIVE_FAULTS["ambiguous-no-reexecution"],
                )
            )
        owned = {criterion for action in actions for criterion in action.criterion_ids}
        missing = [
            item for item in catalog if item.startswith(f"{workflow}.") and item not in owned
        ]
        if missing:
            raise ValueError(f"workflow action plan omits criteria: {', '.join(missing)}")
    return tuple(actions)


def _build_plan(
    actions: tuple[WorkflowAction, ...], backend: CampaignExecutionBackend
) -> CampaignPlan:
    criteria = tuple(
        criterion
        for criterion in original_acceptance_criteria()
        if criterion.criterion_id.startswith(("workflow1.", "workflow2.", "workflow3."))
    )
    steps: list[CampaignStep] = []
    for action in actions:

        def execute(
            recorder: ActionRecorder,
            *,
            action: WorkflowAction = action,
        ) -> StepResult:
            return backend.execute(action, recorder)

        steps.append(
            CampaignStep(
                step_id=f"{action.workflow}.{action.scenario}",
                phase=Phase.for_criterion(action.criterion_ids[0]),
                criterion_ids=action.criterion_ids,
                execute=execute,
            )
        )
    return CampaignPlan(criteria=criteria, steps=tuple(steps))


def build_campaign_execution(
    settings: CampaignExecutionSettings,
    *,
    backend: CampaignExecutionBackend | None = None,
) -> CampaignExecution:
    """Build the three workflow graphs and inert coordinator action plan."""
    deployments = _deployment_references(settings.campaign_id)
    graphs = WorkflowGraphs(
        workflow1=_workflow1_graph(settings),
        workflow2_child=_workflow2_child_graph(settings),
        workflow2_parent=_workflow2_parent_graph(
            settings, child_deployment_ref=deployments.workflow2_child
        ),
        workflow3=_workflow3_graph(settings),
    )
    actions = _build_actions(settings, deployments, graphs)
    effective_backend = backend or UnconfiguredExecutionBackend()
    return CampaignExecution(
        settings=settings,
        deployments=deployments,
        graphs=graphs,
        actions=actions,
        contracts=_contract_specs(settings.campaign_id),
        plan=_build_plan(actions, effective_backend),
    )
