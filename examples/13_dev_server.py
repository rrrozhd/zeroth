"""13 — Dev server: rapid graph iteration in one terminal.

What this shows
---------------
The smallest possible "iterate on a graph" loop: rebuild the graph,
bootstrap an in-process service with a durable worker disabled, and
drive one run synchronously. There is no HTTP hop, no uvicorn, no
lifespan dance. If an example or test fails, you can change the graph
and rerun immediately.

Use this shape when you want fast feedback while authoring nodes. Use
:mod:`examples.10_serve_in_python` or ``examples/service/entrypoint.py``
when you want the real HTTP surface.

Run
---
    uv run python examples/13_dev_server.py
"""

from __future__ import annotations

# Allow python examples/NN_name.py to find the sibling examples/_common.py helper.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import asyncio
import sys
import time

from examples._common import print_run_summary, running_service
from examples._contracts import Answer, Question
from zeroth.core.agent_runtime import (
    AgentConfig,
    AgentRunner,
    DeterministicProviderAdapter,
    ProviderResponse,
)
from zeroth.core.graph import (
    AgentNode,
    AgentNodeData,
    DisplayMetadata,
    ExecutionSettings,
    Graph,
)


def build_graph() -> Graph:
    return Graph(
        graph_id="dev-server",
        name="Dev server",
        version=1,
        entry_step="qa",
        execution_settings=ExecutionSettings(max_total_steps=5),
        nodes=[
            AgentNode(
                node_id="qa",
                graph_version_ref="dev-server@1",
                display=DisplayMetadata(title="Dev Q&A"),
                input_contract_ref="contract://question",
                output_contract_ref="contract://answer",
                agent=AgentNodeData(
                    instruction="Answer briefly.",
                    model_provider="openai/gpt-4o-mini",
                ),
            ),
        ],
        edges=[],
    )


def build_runner() -> AgentRunner:
    """Deterministic runner so the dev loop has zero external deps."""
    return AgentRunner(
        AgentConfig(
            name="qa",
            description="Deterministic stub for the dev server loop.",
            instruction="Stub answer.",
            model_name="openai/gpt-4o-mini",
            input_model=Question,
            output_model=Answer,
        ),
        DeterministicProviderAdapter(
            responses=[
                ProviderResponse(
                    content={"answer": "dev-server reply — wire a real provider for production."}
                )
            ]
        ),
    )


async def main() -> int:
    started_at = time.monotonic()
    async with running_service(
        build_graph(),
        contracts={
            "contract://question": Question,
            "contract://answer": Answer,
        },
        agent_runners={"qa": build_runner()},
        deployment_ref="dev-server",
    ) as demo:
        elapsed_boot = time.monotonic() - started_at
        print(f"boot time: {elapsed_boot * 1000:.0f} ms")

        run = await demo.service.orchestrator.run_graph(
            demo.service.graph,
            {"question": "What is the fastest feedback loop in Zeroth?"},
            deployment_ref=demo.deployment_ref,
        )
        print_run_summary(run, label="dev-server")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
