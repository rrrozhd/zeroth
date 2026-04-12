"""00 — Hello, Zeroth: a single agent node, run through the real runtime.

What this shows
---------------
The smallest possible end-to-end invocation. One :class:`AgentNode`, one
real :class:`AgentRunner` backed by the real :class:`LiteLLMProviderAdapter`,
run through the real :class:`RuntimeOrchestrator`. No stubs, no hacks, no
``litellm.completion`` calls in user code — this file is what the library
wants you to write.

Requirements
------------
* ``OPENAI_API_KEY`` in the environment (uses ``openai/gpt-4o-mini``).
  Set ``ZEROTH_EXAMPLE_MODEL`` to override the model name.

Run
---
    uv run python examples/00_hello.py
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
    DEMO_GRAPH_ID,
    print_run_summary,
    require_env,
    running_service,
)
from examples._contracts import Answer, Question
from zeroth.core.agent_runtime import (
    AgentConfig,
    AgentRunner,
    LiteLLMProviderAdapter,
)
from zeroth.core.graph import (
    AgentNode,
    AgentNodeData,
    DisplayMetadata,
    ExecutionSettings,
    Graph,
)


def build_graph(model_name: str) -> Graph:
    """A one-node graph whose only step is a Q&A :class:`AgentNode`."""
    graph_version_ref = f"{DEMO_GRAPH_ID}@1"
    return Graph(
        graph_id=DEMO_GRAPH_ID,
        name="Hello, Zeroth",
        version=1,
        entry_step="qa",
        execution_settings=ExecutionSettings(max_total_steps=5),
        nodes=[
            AgentNode(
                node_id="qa",
                graph_version_ref=graph_version_ref,
                display=DisplayMetadata(title="Q&A"),
                input_contract_ref="contract://question",
                output_contract_ref="contract://answer",
                agent=AgentNodeData(
                    instruction=(
                        "You are a helpful assistant. Answer the user's question in one "
                        "short sentence. Return JSON matching the output schema."
                    ),
                    model_provider=model_name,
                    model_params={"temperature": 0.2, "max_tokens": 120},
                ),
            ),
        ],
        edges=[],
    )


async def main() -> int:
    if not require_env("OPENAI_API_KEY"):
        return 0

    model_name = os.environ.get("ZEROTH_EXAMPLE_MODEL", "openai/gpt-4o-mini")

    # Build one real AgentRunner for the one node in the graph. The
    # LiteLLMProviderAdapter reads OPENAI_API_KEY from the environment.
    runner = AgentRunner(
        AgentConfig(
            name="qa",
            description="Answers a user question in one sentence.",
            instruction="Answer the user in one short sentence.",
            model_name=model_name,
            input_model=Question,
            output_model=Answer,
        ),
        LiteLLMProviderAdapter(),
    )

    async with running_service(
        build_graph(model_name),
        contracts={
            "contract://question": Question,
            "contract://answer": Answer,
        },
        agent_runners={"qa": runner},
    ) as demo:
        run = await demo.service.orchestrator.run_graph(
            demo.service.graph,
            {"question": "What is Zeroth in one sentence?"},
            deployment_ref=demo.deployment_ref,
        )
        print_run_summary(run, label="hello")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
