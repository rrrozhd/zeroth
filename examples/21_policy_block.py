"""21 — Policy enforcement: orchestrator denies a node that requests a forbidden capability.

What this shows
---------------
A two-node graph whose tool node declares a ``network_write`` capability
binding. A :class:`PolicyGuard` is wired into the orchestrator with a
policy that denies ``NETWORK_WRITE``. When the run reaches the tool node,
the orchestrator evaluates the guard, denies the node, and terminates
the run with ``failure_state.reason == "policy_violation"``.

Crucially, this happens *through the orchestrator* — we don't call
``guard.evaluate(...)`` by hand. That's the whole point: in production,
the orchestrator is the enforcement surface, and the audit trail shows
the denial.

Run
---
    uv run python examples/21_policy_block.py
"""

from __future__ import annotations

# Allow python examples/NN_name.py to find the sibling examples/_common.py helper.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import asyncio
import sys

from examples._common import print_run_summary, running_service
from examples._contracts import ToolInput, ToolOutput, Topic
from examples._tools import build_demo_tool_registry
from zeroth.core.agent_runtime import (
    AgentConfig,
    AgentRunner,
    DeterministicProviderAdapter,
    ProviderResponse,
)
from zeroth.core.execution_units import ExecutableUnitRunner
from zeroth.core.graph import (
    AgentNode,
    AgentNodeData,
    DisplayMetadata,
    Edge,
    ExecutableUnitNode,
    ExecutableUnitNodeData,
    ExecutionSettings,
    Graph,
)
from zeroth.core.mappings.models import EdgeMapping, PassthroughMappingOperation
from zeroth.core.policy import (
    Capability,
    CapabilityRegistry,
    PolicyDefinition,
    PolicyGuard,
    PolicyRegistry,
)
from zeroth.core.runs import RunStatus

POLICY_ID = "policy://no-network-writes"


def build_graph() -> Graph:
    return Graph(
        graph_id="policy-block",
        name="Policy block",
        version=1,
        entry_step="agent",
        execution_settings=ExecutionSettings(max_total_steps=5),
        nodes=[
            AgentNode(
                node_id="agent",
                graph_version_ref="policy-block@1",
                display=DisplayMetadata(title="Draft agent"),
                input_contract_ref="contract://topic",
                output_contract_ref="contract://tool-input",
                agent=AgentNodeData(
                    instruction="Draft a body.",
                    model_provider="openai/gpt-4o-mini",
                ),
            ),
            ExecutableUnitNode(
                node_id="publisher",
                graph_version_ref="policy-block@1",
                display=DisplayMetadata(title="Network publisher"),
                input_contract_ref="contract://tool-input",
                output_contract_ref="contract://tool-output",
                # Declare the capability the node *wants*. The policy
                # guard compares this against the capability's effective
                # policy and blocks the node before it runs.
                policy_bindings=[POLICY_ID],
                capability_bindings=[Capability.NETWORK_WRITE.value],
                executable_unit=ExecutableUnitNodeData(
                    manifest_ref="eu://format_article",
                    execution_mode="native",
                ),
            ),
        ],
        edges=[
            Edge(
                edge_id="agent-to-publisher",
                source_node_id="agent",
                target_node_id="publisher",
                mapping=EdgeMapping(
                    operations=[
                        PassthroughMappingOperation(source_path="topic", target_path="topic"),
                        PassthroughMappingOperation(source_path="body", target_path="body"),
                    ]
                ),
            ),
        ],
    )


def build_policy_guard() -> PolicyGuard:
    """Register a policy that denies NETWORK_WRITE for the publisher."""
    capability_registry = CapabilityRegistry()
    for cap in Capability:
        capability_registry.register(cap.value, cap)

    policy_registry = PolicyRegistry()
    policy_registry.register(
        PolicyDefinition(
            policy_id=POLICY_ID,
            denied_capabilities=[Capability.NETWORK_WRITE],
        )
    )
    return PolicyGuard(
        policy_registry=policy_registry,
        capability_registry=capability_registry,
    )


async def main() -> int:
    runner = AgentRunner(
        AgentConfig(
            name="agent",
            description="Deterministic drafter.",
            instruction="Draft a body.",
            model_name="openai/gpt-4o-mini",
            input_model=Topic,
            output_model=ToolInput,
        ),
        DeterministicProviderAdapter(
            responses=[
                ProviderResponse(
                    content={
                        "topic": "policy",
                        "body": "This publisher should never run — it needs network_write.",
                    }
                )
            ]
        ),
    )

    async with running_service(
        build_graph(),
        contracts={
            "contract://topic": Topic,
            "contract://tool-input": ToolInput,
            "contract://tool-output": ToolOutput,
        },
        agent_runners={"agent": runner},
        executable_unit_runner=ExecutableUnitRunner(build_demo_tool_registry()),
    ) as demo:
        # Wire the policy guard onto the orchestrator. In production
        # this happens inside ``bootstrap_service`` via the guardrail
        # wiring; here we set it directly so the example is self-contained.
        demo.service.orchestrator.policy_guard = build_policy_guard()

        run = await demo.service.orchestrator.run_graph(
            demo.service.graph,
            {"topic": "policy"},
            deployment_ref=demo.deployment_ref,
        )
        print_run_summary(run, label="policy-block")

        assert run.status is RunStatus.FAILED, f"expected FAILED, got {run.status}"
        assert run.failure_state and run.failure_state.reason == "policy_violation"

        # The audit trail surfaces the denial as an enforcement record.
        audit = await demo.service.audit_repository.list_by_run(run.run_id)
        denials = [
            rec
            for rec in audit
            if (rec.execution_metadata or {}).get("enforcement", {}).get("decision") == "deny"
        ]
        print(f"\n  audit denials: {len(denials)}")
        for rec in denials:
            enforcement = rec.execution_metadata.get("enforcement", {})
            print(f"    [{rec.node_id}] reason={enforcement.get('reason')!r}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
