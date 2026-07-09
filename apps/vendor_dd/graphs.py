"""The three vendor-dd graphs: main workflow, dimension child, follow-up chat.

Everything here is the declarative authoring surface — the same shapes the
Studio canvas produces. No custom runner wiring is required: agent runners are
built by the stock factory from ``model_provider``, executable units resolve
through the registry, and the child graph is invoked by deployment ref.
"""

from __future__ import annotations

from apps.vendor_dd.units import (
    FINMETRICS_REF,
    NORMALIZE_SOURCE,
    PREPARE_PANEL_REF,
    REPORT_STAMP_REF,
    RISK_SCORE_SOURCE,
    SANCTIONS_REF,
)
from zeroth.core.graph import (
    AgentNode,
    AgentNodeData,
    AgentToolBinding,
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
    RetrievalNode,
    RetrievalNodeData,
    SubgraphNode,
    SubgraphNodeData,
    ToolArgument,
)
from zeroth.core.mappings.models import (
    ConstantMappingOperation,
    DefaultMappingOperation,
    EdgeMapping,
    PassthroughMappingOperation,
    RenameMappingOperation,
)
from zeroth.core.parallel.models import ParallelConfig

# Policy allowing the sandboxed units to spawn their subprocess. Registered in
# the entrypoint's PolicyGuard; binding it on the unit nodes makes the guard
# evaluate (and audit) every dispatch.
UNITS_POLICY_ID = "policy://vendor-dd/sandboxed-units"

MAIN_GRAPH_ID = "vendor-dd"
DIMENSION_GRAPH_ID = "vendor-dd-dimension"
CHAT_GRAPH_ID = "vendor-dd-chat"

MAIN_DEPLOYMENT_REF = "vendor-dd"
DIMENSION_DEPLOYMENT_REF = "vendor-dd-dimension"
CHAT_DEPLOYMENT_REF = "vendor-dd-chat"

# Instruction routing tags: the scripted (hermetic) provider recognizes agents
# by these markers; real LLMs simply read them as part of the role framing.
SCREEN_TAG = "[vendor-dd:screen]"
DIMENSION_TAG = "[vendor-dd:dimension]"
REPORT_TAG = "[vendor-dd:report]"
CHAT_TAG = "[vendor-dd:chat]"

DEFAULT_MODEL = "openai/gpt-4o-mini"

# Tenant stamped onto deployments (and therefore runs, audits, cost events).
TENANT_ID = "tenant-acme"


def build_main_graph() -> Graph:
    ref = f"{MAIN_GRAPH_ID}@1"
    return Graph(
        graph_id=MAIN_GRAPH_ID,
        name="Vendor due-diligence desk",
        version=1,
        entry_step="intake",
        execution_settings=ExecutionSettings(max_total_steps=60),
        deployment_settings={"tenant_id": TENANT_ID},
        nodes=[
            EntrypointNode(
                node_id="intake",
                graph_version_ref=ref,
                display=DisplayMetadata(
                    title="Dossier intake",
                    description="Public input contract of the workflow.",
                ),
                input_contract_ref="contract://vendor-dd/dossier",
                output_contract_ref="contract://vendor-dd/dossier",
            ),
            ExecutableUnitNode(
                node_id="normalize",
                graph_version_ref=ref,
                display=DisplayMetadata(title="Normalize dossier (inline code)"),
                input_contract_ref="contract://vendor-dd/mapped-dossier",
                output_contract_ref="contract://vendor-dd/normalized",
                policy_bindings=[UNITS_POLICY_ID],
                capability_bindings=["process_spawn"],
                executable_unit=ExecutableUnitNodeData(
                    execution_mode="inline",
                    inline_source=NORMALIZE_SOURCE,
                    timeout_seconds=30,
                ),
            ),
            RetrievalNode(
                node_id="policy-context",
                graph_version_ref=ref,
                display=DisplayMetadata(title="Ground on policy corpus (RAG)"),
                input_contract_ref="contract://vendor-dd/normalized",
                output_contract_ref="contract://vendor-dd/grounded",
                retrieval=RetrievalNodeData(
                    connector_ref="key_value",
                    query_key="query",
                    top_k=3,
                    scope="shared",
                    as_name="policy_context",
                ),
            ),
            AgentNode(
                node_id="screen",
                graph_version_ref=ref,
                display=DisplayMetadata(title="Screening analyst"),
                input_contract_ref="contract://vendor-dd/grounded",
                output_contract_ref="contract://vendor-dd/screening",
                agent=AgentNodeData(
                    instruction=(
                        f"{SCREEN_TAG} You are a senior third-party-risk analyst. "
                        "Screen the vendor described in the input dossier. Use the "
                        "sanctions_screen tool exactly once on the vendor's legal name "
                        "and exactly once per named subprocessor — never screen the "
                        "same name twice. Then conclude. Ground your findings on "
                        "the policy_context excerpts and cite the policy ids you relied "
                        "on in policy_citations. Copy vendor_slug, vendor_name, "
                        "jurisdiction, category, annual_spend_usd, data_access, "
                        "data_risk, spend_band and dimensions from the input unchanged. "
                        "Set sanctions_status to 'hit' if any screened name is listed, "
                        "otherwise 'clear'. Return JSON matching the output schema."
                    ),
                    model_provider=DEFAULT_MODEL,
                    model_params={"temperature": 0.1},
                    # Vendor + up to ~7 subprocessors; past this the runtime
                    # forces the final answer rather than failing the run.
                    max_tool_calls=8,
                    tool_bindings=[
                        AgentToolBinding(
                            target_node_id="sanctions-screen",
                            name="sanctions_screen",
                            description=(
                                "Screen a single legal entity name against the "
                                "consolidated sanctions denylist. Returns whether the "
                                "name is listed and on which list."
                            ),
                            arguments=[
                                ToolArgument(
                                    name="name",
                                    type="string",
                                    description="Exact legal name to screen.",
                                    required=True,
                                )
                            ],
                        )
                    ],
                ),
            ),
            ExecutableUnitNode(
                node_id="sanctions-screen",
                graph_version_ref=ref,
                display=DisplayMetadata(
                    title="Sanctions denylist screen",
                    description="Native unit exposed to the screening agent as a tool.",
                ),
                input_contract_ref="contract://vendor-dd/sanctions-query",
                output_contract_ref="contract://vendor-dd/sanctions-verdict",
                executable_unit=ExecutableUnitNodeData(
                    execution_mode="native",
                    manifest_ref=SANCTIONS_REF,
                ),
            ),
            ExecutableUnitNode(
                node_id="financial-metrics",
                graph_version_ref=ref,
                display=DisplayMetadata(title="Financial metrics (project unit)"),
                input_contract_ref="contract://vendor-dd/screening",
                output_contract_ref="contract://vendor-dd/metrics",
                policy_bindings=[UNITS_POLICY_ID],
                capability_bindings=["process_spawn"],
                executable_unit=ExecutableUnitNodeData(
                    execution_mode="project",
                    manifest_ref=FINMETRICS_REF,
                    timeout_seconds=60,
                ),
            ),
            ExecutableUnitNode(
                node_id="prepare-panel",
                graph_version_ref=ref,
                display=DisplayMetadata(
                    title="Prepare dimension panel",
                    description="Builds one brief per risk dimension and fans out.",
                ),
                input_contract_ref="contract://vendor-dd/metrics",
                output_contract_ref="contract://vendor-dd/panel-prep",
                parallel_config=ParallelConfig(
                    split_path="panel",
                    merge_strategy="collect",
                    fail_mode="fail_fast",
                    max_branches=4,
                ),
                executable_unit=ExecutableUnitNodeData(
                    execution_mode="native",
                    manifest_ref=PREPARE_PANEL_REF,
                ),
            ),
            SubgraphNode(
                node_id="dimension-panel",
                graph_version_ref=ref,
                display=DisplayMetadata(
                    title="Dimension analysis (child workflow)",
                    description="Runs the published vendor-dd-dimension graph per branch.",
                ),
                input_contract_ref="contract://vendor-dd/panel-branch",
                output_contract_ref="contract://vendor-dd/dimension-finding",
                subgraph=SubgraphNodeData(
                    graph_ref=DIMENSION_DEPLOYMENT_REF,
                    thread_participation="isolated",
                ),
            ),
            ExecutableUnitNode(
                node_id="risk-score",
                graph_version_ref=ref,
                display=DisplayMetadata(title="Risk scoring (inline code)"),
                input_contract_ref="contract://vendor-dd/panel-findings",
                output_contract_ref="contract://vendor-dd/risk-decision",
                policy_bindings=[UNITS_POLICY_ID],
                capability_bindings=["process_spawn"],
                executable_unit=ExecutableUnitNodeData(
                    execution_mode="inline",
                    inline_source=RISK_SCORE_SOURCE,
                    timeout_seconds=30,
                ),
            ),
            HumanApprovalNode(
                node_id="risk-review",
                graph_version_ref=ref,
                display=DisplayMetadata(
                    title="Risk reviewer sign-off",
                    description="TPR-005: high/critical tiers require human approval.",
                ),
                input_contract_ref="contract://vendor-dd/risk-decision",
                output_contract_ref="contract://vendor-dd/risk-decision",
                human_approval=HumanApprovalNodeData(
                    approval_payload_schema_ref="contract://vendor-dd/risk-decision",
                    resolution_schema_ref="contract://vendor-dd/risk-decision",
                ),
            ),
            AgentNode(
                node_id="report",
                graph_version_ref=ref,
                display=DisplayMetadata(title="Assessment report writer"),
                input_contract_ref="contract://vendor-dd/risk-decision",
                output_contract_ref="contract://vendor-dd/assessment",
                agent=AgentNodeData(
                    instruction=(
                        f"{REPORT_TAG} You are the third-party-risk report writer. "
                        "Draft the final vendor assessment memo from the risk decision "
                        "in the input. Copy vendor_name, vendor_slug, tier and "
                        "risk_score unchanged. Set decision to 'approved' for low "
                        "tier, 'conditional' for medium and high, 'rejected' for "
                        "critical. Summarize each panel finding in "
                        "dimension_summaries and derive concrete follow-up "
                        "conditions. Return JSON matching the output schema."
                    ),
                    model_provider=DEFAULT_MODEL,
                    model_params={"temperature": 0.2},
                ),
            ),
            ExecutableUnitNode(
                node_id="report-stamp",
                graph_version_ref=ref,
                display=DisplayMetadata(
                    title="Integrity stamp (openssl)",
                    description="Wraps the memo with its sha256 fingerprint.",
                ),
                input_contract_ref="contract://vendor-dd/assessment",
                output_contract_ref="contract://vendor-dd/stamped",
                policy_bindings=[UNITS_POLICY_ID],
                capability_bindings=["process_spawn"],
                executable_unit=ExecutableUnitNodeData(
                    execution_mode="wrapped_command",
                    manifest_ref=REPORT_STAMP_REF,
                    timeout_seconds=30,
                ),
            ),
        ],
        edges=[
            Edge(
                edge_id="intake-normalize",
                source_node_id="intake",
                target_node_id="normalize",
                # All four basic mapping operations, on the one edge whose
                # upstream schema is fully closed (the public contract).
                mapping=EdgeMapping(
                    operations=[
                        PassthroughMappingOperation(
                            source_path="vendor_name", target_path="vendor_name"
                        ),
                        PassthroughMappingOperation(
                            source_path="description", target_path="description"
                        ),
                        PassthroughMappingOperation(
                            source_path="jurisdiction", target_path="jurisdiction"
                        ),
                        PassthroughMappingOperation(source_path="category", target_path="category"),
                        PassthroughMappingOperation(
                            source_path="annual_spend_usd", target_path="annual_spend_usd"
                        ),
                        PassthroughMappingOperation(
                            source_path="data_access", target_path="data_access"
                        ),
                        PassthroughMappingOperation(
                            source_path="subprocessors", target_path="subprocessors"
                        ),
                        RenameMappingOperation(source_path="website", target_path="vendor_website"),
                        ConstantMappingOperation(target_path="pipeline", value="vendor-dd/v1"),
                        DefaultMappingOperation(
                            source_path="priority",
                            target_path="priority",
                            default_value="standard",
                        ),
                    ]
                ),
            ),
            Edge(
                edge_id="normalize-policy",
                source_node_id="normalize",
                target_node_id="policy-context",
            ),
            Edge(
                edge_id="policy-screen",
                source_node_id="policy-context",
                target_node_id="screen",
            ),
            Edge(
                edge_id="screen-uses-sanctions",
                source_node_id="screen",
                target_node_id="sanctions-screen",
                kind="tool",
            ),
            Edge(
                edge_id="screen-metrics",
                source_node_id="screen",
                target_node_id="financial-metrics",
            ),
            Edge(
                edge_id="metrics-panel",
                source_node_id="financial-metrics",
                target_node_id="prepare-panel",
            ),
            Edge(
                edge_id="panel-dimensions",
                source_node_id="prepare-panel",
                target_node_id="dimension-panel",
            ),
            Edge(
                edge_id="dimensions-score",
                source_node_id="dimension-panel",
                target_node_id="risk-score",
            ),
            Edge(
                edge_id="score-review",
                source_node_id="risk-score",
                target_node_id="risk-review",
                condition=Condition(
                    expression="payload.tier == 'high' or payload.tier == 'critical'"
                ),
            ),
            Edge(
                edge_id="score-report",
                source_node_id="risk-score",
                target_node_id="report",
                condition=Condition(expression="payload.tier == 'low' or payload.tier == 'medium'"),
            ),
            Edge(
                edge_id="review-report",
                source_node_id="risk-review",
                target_node_id="report",
            ),
            Edge(
                edge_id="report-stamp-edge",
                source_node_id="report",
                target_node_id="report-stamp",
            ),
        ],
    )


def build_dimension_graph() -> Graph:
    ref = f"{DIMENSION_GRAPH_ID}@1"
    return Graph(
        graph_id=DIMENSION_GRAPH_ID,
        name="Vendor DD — dimension analysis",
        version=1,
        entry_step="dim-intake",
        execution_settings=ExecutionSettings(max_total_steps=10),
        deployment_settings={"tenant_id": TENANT_ID},
        nodes=[
            EntrypointNode(
                node_id="dim-intake",
                graph_version_ref=ref,
                display=DisplayMetadata(title="Dimension brief intake"),
                input_contract_ref="contract://vendor-dd/panel-branch",
                output_contract_ref="contract://vendor-dd/panel-branch",
            ),
            AgentNode(
                node_id="dim-analyst",
                graph_version_ref=ref,
                display=DisplayMetadata(title="Dimension analyst"),
                input_contract_ref="contract://vendor-dd/panel-branch",
                output_contract_ref="contract://vendor-dd/dimension-finding",
                agent=AgentNodeData(
                    instruction=(
                        f"{DIMENSION_TAG} You are a specialist third-party-risk "
                        "analyst for one risk dimension. Assess the vendor strictly "
                        "for the dimension named in the input brief, considering the "
                        "flags, sanctions status, and financial metrics provided. "
                        "Copy dimension and vendor_slug from the input unchanged. "
                        "Rate 1 (negligible) to 5 (severe), justify in rationale, "
                        "and list concrete red_flags. Return JSON matching the "
                        "output schema."
                    ),
                    model_provider=DEFAULT_MODEL,
                    model_params={"temperature": 0.1},
                ),
            ),
        ],
        edges=[
            Edge(
                edge_id="dim-intake-analyst",
                source_node_id="dim-intake",
                target_node_id="dim-analyst",
            ),
        ],
    )


def build_chat_graph() -> Graph:
    ref = f"{CHAT_GRAPH_ID}@1"
    return Graph(
        graph_id=CHAT_GRAPH_ID,
        name="Vendor DD — follow-up chat",
        version=1,
        entry_step="chat-intake",
        execution_settings=ExecutionSettings(max_total_steps=10),
        deployment_settings={"tenant_id": TENANT_ID},
        nodes=[
            EntrypointNode(
                node_id="chat-intake",
                graph_version_ref=ref,
                display=DisplayMetadata(title="Chat turn intake"),
                input_contract_ref="contract://vendor-dd/chat-turn",
                output_contract_ref="contract://vendor-dd/chat-turn",
            ),
            AgentNode(
                node_id="chat-analyst",
                graph_version_ref=ref,
                display=DisplayMetadata(title="Assessment Q&A analyst"),
                input_contract_ref="contract://vendor-dd/chat-turn",
                output_contract_ref="contract://vendor-dd/chat-reply",
                agent=AgentNodeData(
                    instruction=(
                        f"{CHAT_TAG} You are the third-party-risk desk assistant. "
                        "Answer follow-up questions about completed vendor "
                        "assessments. Be precise about tiers, drivers, and policy "
                        "references. When the conversation history contains earlier "
                        "turns, use them. Return JSON matching the output schema."
                    ),
                    model_provider=DEFAULT_MODEL,
                    model_params={"temperature": 0.2},
                    input_messages_key="messages",
                    persist_conversation=True,
                    conversation_max_turns=20,
                ),
            ),
        ],
        edges=[
            Edge(
                edge_id="chat-intake-analyst",
                source_node_id="chat-intake",
                target_node_id="chat-analyst",
            ),
        ],
    )
