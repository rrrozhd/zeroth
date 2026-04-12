"""03 — Conditional branches resolved at runtime by the orchestrator.

What this shows
---------------
A real conditional graph: a classifier :class:`AgentNode` scores an input
message, then the orchestrator evaluates two :class:`Condition` edges and
picks the matching branch. Each branch lands on its own
:class:`ExecutableUnitNode` that records which lane fired.

The key point is that the branching happens *inside* the runtime —
there's no hand-written ``BranchResolver.resolve(...)`` call. The
orchestrator walks the edges the same way it would at production scale.

The classifier uses the :class:`DeterministicProviderAdapter` so the
example is hermetic. Swap it for :class:`LiteLLMProviderAdapter` with a
real ``OPENAI_API_KEY`` to drive it with a real LLM — the graph shape
doesn't change.

Run
---
    uv run python examples/03_conditional_branches.py
"""

from __future__ import annotations

# Allow python examples/NN_name.py to find the sibling examples/_common.py helper.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import asyncio
import sys

from examples._common import print_run_summary, running_service
from examples._contracts import ScoredPayload, ToolInput, ToolOutput
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
    Condition,
    DisplayMetadata,
    Edge,
    ExecutableUnitNode,
    ExecutableUnitNodeData,
    ExecutionSettings,
    Graph,
)
from zeroth.core.mappings.models import (
    ConstantMappingOperation,
    EdgeMapping,
    PassthroughMappingOperation,
)


def build_graph() -> Graph:
    graph_id = "conditional-branches"
    ref = f"{graph_id}@1"
    return Graph(
        graph_id=graph_id,
        name="Conditional branches",
        version=1,
        entry_step="classifier",
        execution_settings=ExecutionSettings(max_total_steps=10),
        nodes=[
            AgentNode(
                node_id="classifier",
                graph_version_ref=ref,
                display=DisplayMetadata(title="Score classifier"),
                input_contract_ref="contract://scored",
                output_contract_ref="contract://scored",
                agent=AgentNodeData(
                    instruction=(
                        "Classify the message. Return JSON with the original "
                        "message and a 'score' between 0 and 1."
                    ),
                    model_provider="openai/gpt-4o-mini",
                ),
            ),
            ExecutableUnitNode(
                node_id="approve",
                graph_version_ref=ref,
                display=DisplayMetadata(title="Approve lane"),
                input_contract_ref="contract://tool-input",
                output_contract_ref="contract://tool-output",
                executable_unit=ExecutableUnitNodeData(
                    manifest_ref="eu://echo",
                    execution_mode="native",
                ),
            ),
            ExecutableUnitNode(
                node_id="reject",
                graph_version_ref=ref,
                display=DisplayMetadata(title="Reject lane"),
                input_contract_ref="contract://tool-input",
                output_contract_ref="contract://tool-output",
                executable_unit=ExecutableUnitNodeData(
                    manifest_ref="eu://echo",
                    execution_mode="native",
                ),
            ),
        ],
        edges=[
            Edge(
                edge_id="edge-high",
                source_node_id="classifier",
                target_node_id="approve",
                # Conditions are plain Python-style expressions evaluated
                # against ``payload`` at runtime. The orchestrator walks
                # every outgoing edge and takes any whose condition is True.
                condition=Condition(expression="payload.score >= 0.5"),
                mapping=EdgeMapping(
                    operations=[
                        PassthroughMappingOperation(
                            source_path="message", target_path="topic"
                        ),
                        ConstantMappingOperation(
                            target_path="body", value="approved by classifier"
                        ),
                    ]
                ),
            ),
            Edge(
                edge_id="edge-low",
                source_node_id="classifier",
                target_node_id="reject",
                condition=Condition(expression="payload.score < 0.5"),
                mapping=EdgeMapping(
                    operations=[
                        PassthroughMappingOperation(
                            source_path="message", target_path="topic"
                        ),
                        ConstantMappingOperation(
                            target_path="body", value="rejected by classifier"
                        ),
                    ]
                ),
            ),
        ],
    )


def _make_runner(score: float) -> AgentRunner:
    """Deterministic classifier that always returns ``score``."""
    return AgentRunner(
        AgentConfig(
            name="classifier",
            description="Scores an incoming message.",
            instruction="Return a deterministic score.",
            model_name="openai/gpt-4o-mini",
            input_model=ScoredPayload,
            output_model=ScoredPayload,
        ),
        DeterministicProviderAdapter(
            responses=[
                ProviderResponse(
                    content={"message": "scored", "score": score},
                )
            ]
        ),
    )


async def main() -> int:
    contracts = {
        "contract://scored": ScoredPayload,
        "contract://tool-input": ToolInput,
        "contract://tool-output": ToolOutput,
    }

    # Two full runs: a high score lands on "approve", a low score on "reject".
    for label, score in (("high-score", 0.9), ("low-score", 0.2)):
        async with running_service(
            build_graph(),
            contracts=contracts,
            agent_runners={"classifier": _make_runner(score)},
            executable_unit_runner=ExecutableUnitRunner(build_demo_tool_registry()),
            deployment_ref=f"conditional-{label}",
        ) as demo:
            run = await demo.service.orchestrator.run_graph(
                demo.service.graph,
                {"message": "hello world", "score": score},
                deployment_ref=demo.deployment_ref,
            )
            print_run_summary(run, label=label)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
