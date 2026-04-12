"""24 — Audit trail: run a real graph, then query its audit records.

What this shows
---------------
The audit trail is not something you manually write records to — the
orchestrator produces one every time it runs a node. This example
drives a real multi-node graph with a real agent runner, then queries
the audit repository two ways:

1. ``AuditRepository.list_by_run(run_id)`` — every record for a run.
2. ``AuditRepository.list(AuditQuery(run_id=..., node_id=...))`` — filtered slice.

This is a replacement for the old ``audit_query.py`` example that
manually seeded fake records, which taught readers nothing about where
audits come from.

Run
---
    uv run python examples/24_audit_query.py
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
from zeroth.core.audit import AuditQuery
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


def build_graph() -> Graph:
    return Graph(
        graph_id="audit-query",
        name="Audit query",
        version=1,
        entry_step="drafter",
        execution_settings=ExecutionSettings(max_total_steps=5),
        nodes=[
            AgentNode(
                node_id="drafter",
                graph_version_ref="audit-query@1",
                display=DisplayMetadata(title="Drafter"),
                input_contract_ref="contract://topic",
                output_contract_ref="contract://tool-input",
                agent=AgentNodeData(
                    instruction="Draft.",
                    model_provider="openai/gpt-4o-mini",
                ),
            ),
            ExecutableUnitNode(
                node_id="formatter",
                graph_version_ref="audit-query@1",
                display=DisplayMetadata(title="Formatter"),
                input_contract_ref="contract://tool-input",
                output_contract_ref="contract://tool-output",
                executable_unit=ExecutableUnitNodeData(
                    manifest_ref="eu://format_article",
                    execution_mode="native",
                ),
            ),
        ],
        edges=[
            Edge(
                edge_id="drafter-to-formatter",
                source_node_id="drafter",
                target_node_id="formatter",
                mapping=EdgeMapping(
                    operations=[
                        PassthroughMappingOperation(source_path="topic", target_path="topic"),
                        PassthroughMappingOperation(source_path="body", target_path="body"),
                    ]
                ),
            ),
        ],
    )


async def main() -> int:
    runner = AgentRunner(
        AgentConfig(
            name="drafter",
            description="Deterministic drafter for the audit demo.",
            instruction="Draft.",
            model_name="openai/gpt-4o-mini",
            input_model=Topic,
            output_model=ToolInput,
        ),
        DeterministicProviderAdapter(
            responses=[
                ProviderResponse(
                    content={"topic": "audit trails", "body": "Every node produces a record."}
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
        agent_runners={"drafter": runner},
        executable_unit_runner=ExecutableUnitRunner(build_demo_tool_registry()),
    ) as demo:
        run = await demo.service.orchestrator.run_graph(
            demo.service.graph,
            {"topic": "audit trails"},
            deployment_ref=demo.deployment_ref,
        )
        print_run_summary(run, label="audit-query")

        repo = demo.service.audit_repository

        # 1. Full trail for the run.
        trail = await repo.list_by_run(run.run_id)
        print(f"\nfull trail for run {run.run_id}: {len(trail)} records")
        for rec in trail:
            print(f"  [{rec.node_id}] status={rec.status}")

        # 2. Filtered: just the drafter's record.
        filtered = await repo.list(AuditQuery(run_id=run.run_id, node_id="drafter"))
        print(f"\ndrafter-only slice: {len(filtered)} record(s)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
