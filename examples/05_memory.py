"""05 — Attach memory to an agent so it remembers across runs on the same thread.

What this shows
---------------
A :class:`ThreadMemoryConnector` registered against
:class:`InMemoryConnectorRegistry` and wrapped in a
:class:`MemoryConnectorResolver`. The agent declares the memory via
``memory_refs`` in its :class:`AgentNodeData` and its
:class:`AgentConfig`. The orchestrator injects the resolver into the
runner automatically at dispatch time, so the agent's ``run`` method
reads thread-scoped memory *before* the prompt is assembled and writes
its output back after the call.

The example drives the same ``thread_id`` twice: the second run sees
what the first one wrote. That's the real loop agents use for
conversation state.

Run
---
    uv run python examples/05_memory.py
"""

from __future__ import annotations

# Allow python examples/NN_name.py to find the sibling examples/_common.py helper.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import asyncio
import sys

from governai.memory.models import MemoryScope

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
from zeroth.core.memory import (
    ConnectorManifest,
    InMemoryConnectorRegistry,
    MemoryConnectorResolver,
    ThreadMemoryConnector,
)

MEMORY_REF = "memory://conversation"


def build_graph() -> Graph:
    return Graph(
        graph_id="memory-demo",
        name="Memory demo",
        version=1,
        entry_step="assistant",
        execution_settings=ExecutionSettings(max_total_steps=5),
        nodes=[
            AgentNode(
                node_id="assistant",
                graph_version_ref="memory-demo@1",
                display=DisplayMetadata(title="Assistant with memory"),
                input_contract_ref="contract://question",
                output_contract_ref="contract://answer",
                agent=AgentNodeData(
                    instruction=(
                        "Answer the user briefly. When you've answered before on this "
                        "thread, acknowledge it."
                    ),
                    model_provider="openai/gpt-4o-mini",
                    # Declaring the memory ref on the node tells the
                    # orchestrator to resolve it per run and hand the
                    # binding to the agent runner.
                    memory_refs=[MEMORY_REF],
                    state_persistence={"mode": "thread"},
                    thread_participation="full",
                ),
            ),
        ],
        edges=[],
    )


def build_memory_resolver(service) -> MemoryConnectorResolver:
    """Register an in-process thread-scoped connector and return a resolver."""
    registry = InMemoryConnectorRegistry()
    registry.register(
        MEMORY_REF,
        ConnectorManifest(
            connector_type="thread",
            scope=MemoryScope.THREAD,
        ),
        ThreadMemoryConnector(),
    )
    return MemoryConnectorResolver(
        registry=registry,
        thread_repository=service.thread_repository,
    )


async def main() -> int:
    # Two turns on the same thread: first answer, then acknowledgement.
    provider = DeterministicProviderAdapter(
        responses=[
            ProviderResponse(content={"answer": "The capital of France is Paris."}),
            ProviderResponse(
                content={"answer": "As I said earlier, the capital of France is Paris."}
            ),
        ]
    )
    runner = AgentRunner(
        AgentConfig(
            name="assistant",
            description="Assistant that reads conversation memory.",
            instruction="Answer with memory.",
            model_name="openai/gpt-4o-mini",
            input_model=Question,
            output_model=Answer,
            memory_refs=[MEMORY_REF],
        ),
        provider,
    )

    async with running_service(
        build_graph(),
        contracts={
            "contract://question": Question,
            "contract://answer": Answer,
        },
        agent_runners={"assistant": runner},
    ) as demo:
        # Wire the memory registry into the orchestrator. The orchestrator
        # injects the resolver into any runner whose ``memory_resolver`` is
        # still None (so tests that pre-wire their own resolvers are
        # respected). See RuntimeOrchestrator._dispatch_agent.
        demo.service.orchestrator.memory_resolver = build_memory_resolver(demo.service)

        thread_id = "conversation-1"
        for turn in ("first", "second"):
            run = await demo.service.orchestrator.run_graph(
                demo.service.graph,
                {"question": "What is the capital of France?"},
                deployment_ref=demo.deployment_ref,
                thread_id=thread_id,
            )
            print_run_summary(run, label=f"{turn}-turn")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
