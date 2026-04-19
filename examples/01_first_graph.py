"""01 — First graph: an AgentNode → ExecutableUnitNode pipeline.

What this shows
---------------
A two-node graph: an :class:`AgentNode` produces a draft article, then an
:class:`ExecutableUnitNode` runs a real :class:`NativeUnitManifest` tool
(a Python callable in ``examples/_tools.py``) to format it. The data
flows through the orchestrator's normal edge path — no out-of-band
function calls, no stub runners. This is the canonical shape of almost
every graph you'll write.

Requirements
------------
* ``OPENAI_API_KEY`` in the environment.

Run
---
    uv run python examples/01_first_graph.py
"""

from __future__ import annotations

# Allow python examples/NN_name.py to find the sibling examples/_common.py helper.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import asyncio
import os
import sys

from examples._common import (
    print_run_summary,
    require_env,
    running_service,
)
from examples._contracts import ToolInput, ToolOutput, Topic
from examples._tools import build_demo_tool_registry
from zeroth.core.agent_runtime import (
    AgentConfig,
    AgentRunner,
    LiteLLMProviderAdapter,
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


def build_graph(model_name: str) -> Graph:
    """Agent drafts content, formatter tool wraps it in a Markdown heading."""
    graph_id = "first-graph"
    version_ref = f"{graph_id}@1"
    return Graph(
        graph_id=graph_id,
        name="First Graph",
        version=1,
        entry_step="drafter",
        execution_settings=ExecutionSettings(max_total_steps=10),
        nodes=[
            AgentNode(
                node_id="drafter",
                graph_version_ref=version_ref,
                display=DisplayMetadata(title="Article drafter"),
                input_contract_ref="contract://topic",
                output_contract_ref="contract://tool-input",
                agent=AgentNodeData(
                    instruction=(
                        "You draft short articles. Return JSON with two fields: "
                        "'topic' (echo the user's topic) and 'body' (a two-sentence draft)."
                    ),
                    model_provider=model_name,
                    model_params={"temperature": 0.4, "max_tokens": 200},
                ),
            ),
            ExecutableUnitNode(
                node_id="formatter",
                graph_version_ref=version_ref,
                display=DisplayMetadata(title="Markdown formatter"),
                input_contract_ref="contract://tool-input",
                output_contract_ref="contract://tool-output",
                executable_unit=ExecutableUnitNodeData(
                    manifest_ref="eu://format_article",
                    execution_mode="native",
                ),
            ),
        ],
        edges=[
            # The drafter's output already matches the formatter's input
            # contract, but a passthrough mapping makes that contract
            # explicit at the edge — which is how you'd wire any real
            # hand-off in production.
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
    if not require_env("OPENAI_API_KEY"):
        return 0

    model_name = os.environ.get("ZEROTH_EXAMPLE_MODEL", "openai/gpt-4o-mini")

    runner = AgentRunner(
        AgentConfig(
            name="drafter",
            description="Drafts a two-sentence article from a topic.",
            instruction="Draft two sentences.",
            model_name=model_name,
            input_model=Topic,
            output_model=ToolInput,
        ),
        LiteLLMProviderAdapter(),
    )

    async with running_service(
        build_graph(model_name),
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
            {"topic": "structured agent workflows"},
            deployment_ref=demo.deployment_ref,
        )
        print_run_summary(run, label="first-graph")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
