"""02 — Multi-agent handoff: researcher → writer, through the orchestrator.

What this shows
---------------
A *real* in-graph handoff between two :class:`AgentNode` instances with
different input/output contracts. The orchestrator drives both — there
is no out-of-band ``runner.run(...)`` call. An :class:`EdgeMapping`
translates the researcher's ``Research`` payload into the writer's
input shape on the way through the edge.

If you've seen the old ``agent_handoff.py``, this file is what it
*wanted* to be.

Requirements
------------
* ``OPENAI_API_KEY`` in the environment.

Run
---
    uv run python examples/02_multi_agent.py
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
from examples._contracts import Article, Research, Topic
from zeroth.core.agent_runtime import (
    AgentConfig,
    AgentRunner,
    LiteLLMProviderAdapter,
)
from zeroth.core.graph import (
    AgentNode,
    AgentNodeData,
    DisplayMetadata,
    Edge,
    ExecutionSettings,
    Graph,
)
from zeroth.core.mappings.models import EdgeMapping, PassthroughMappingOperation


def build_graph(model_name: str) -> Graph:
    """Topic → researcher (agent) → writer (agent) → Article."""
    graph_id = "multi-agent"
    ref = f"{graph_id}@1"
    return Graph(
        graph_id=graph_id,
        name="Multi-agent handoff",
        version=1,
        entry_step="researcher",
        execution_settings=ExecutionSettings(max_total_steps=10),
        nodes=[
            AgentNode(
                node_id="researcher",
                graph_version_ref=ref,
                display=DisplayMetadata(title="Researcher"),
                input_contract_ref="contract://topic",
                output_contract_ref="contract://research",
                agent=AgentNodeData(
                    instruction=(
                        "You research topics. Given a topic, produce JSON with fields: "
                        "'topic' (echo), 'findings' (3 bullet strings), 'sources' (1-3 URLs)."
                    ),
                    model_provider=model_name,
                    model_params={"temperature": 0.3, "max_tokens": 300},
                ),
            ),
            AgentNode(
                node_id="writer",
                graph_version_ref=ref,
                display=DisplayMetadata(title="Writer"),
                input_contract_ref="contract://research",
                output_contract_ref="contract://article",
                agent=AgentNodeData(
                    instruction=(
                        "You turn research notes into a short article. Given 'topic' and "
                        "'findings', return JSON with fields: 'topic', 'title', 'body'."
                    ),
                    model_provider=model_name,
                    model_params={"temperature": 0.5, "max_tokens": 400},
                ),
            ),
        ],
        edges=[
            Edge(
                edge_id="researcher-to-writer",
                source_node_id="researcher",
                target_node_id="writer",
                # Explicit per-field hand-off. Even though both nodes
                # use the Research contract on this edge, pinning the
                # mapping makes the dataflow auditable and lets you
                # insert ``RenameMappingOperation`` / ``ConstantMappingOperation``
                # later without touching node code. See 30_contracts_and_mappings.py
                # for the full set of mapping operations.
                mapping=EdgeMapping(
                    operations=[
                        PassthroughMappingOperation(source_path="topic", target_path="topic"),
                        PassthroughMappingOperation(
                            source_path="findings", target_path="findings"
                        ),
                        PassthroughMappingOperation(source_path="sources", target_path="sources"),
                    ]
                ),
            ),
        ],
    )


async def main() -> int:
    if not require_env("OPENAI_API_KEY"):
        return 0

    model_name = os.environ.get("ZEROTH_EXAMPLE_MODEL", "openai/gpt-4o-mini")
    provider = LiteLLMProviderAdapter()

    researcher = AgentRunner(
        AgentConfig(
            name="researcher",
            description="Researches a topic and returns structured findings.",
            instruction="Research the topic in JSON.",
            model_name=model_name,
            input_model=Topic,
            output_model=Research,
        ),
        provider,
    )
    writer = AgentRunner(
        AgentConfig(
            name="writer",
            description="Writes an article from research findings.",
            instruction="Write an article from the findings.",
            model_name=model_name,
            input_model=Research,
            output_model=Article,
        ),
        provider,
    )

    async with running_service(
        build_graph(model_name),
        contracts={
            "contract://topic": Topic,
            "contract://research": Research,
            "contract://article": Article,
        },
        agent_runners={"researcher": researcher, "writer": writer},
    ) as demo:
        run = await demo.service.orchestrator.run_graph(
            demo.service.graph,
            {"topic": "why graph-based agent workflows matter"},
            deployment_ref=demo.deployment_ref,
        )
        print_run_summary(run, label="multi-agent")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
