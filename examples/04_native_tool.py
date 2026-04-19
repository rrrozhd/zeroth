"""04 — Native Python tool attached to an agent and called through the runtime.

What this shows
---------------
This is the canonical pattern for giving an agent a Python-callable
tool. Three layers are involved:

1. **Manifest** — a :class:`NativeUnitManifest` registered in an
   :class:`ExecutableUnitRegistry` tells the runtime how to invoke the
   Python function (via :class:`ExecutableUnitRunner`).
2. **Attachment** — a :class:`ToolAttachmentManifest` declares the tool
   on the agent side, giving it an alias and a permission scope.
3. **Bridge** — the :class:`ToolAttachmentBridge` plus a small
   ``tool_executor`` coroutine wire the two sides together so the agent
   can actually dispatch a call.

Once wired, the agent plans a call, the runtime executes it, and the
result flows back into the agent's output. The orchestrator sees nothing
but a normal :class:`AgentNode` — there's no separate "tool node".

This example uses a :class:`DeterministicProviderAdapter` that forces a
single tool call in the first turn and returns structured JSON in the
second, so the tool-call loop is observable without an LLM key.

Run
---
    uv run python examples/04_native_tool.py
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
    ToolAttachmentBridge,
    ToolAttachmentManifest,
    ToolAttachmentRegistry,
)
from zeroth.core.execution_units import ExecutableUnitRunner
from zeroth.core.graph import (
    AgentNode,
    AgentNodeData,
    DisplayMetadata,
    ExecutionSettings,
    Graph,
)


def build_graph() -> Graph:
    return Graph(
        graph_id="native-tool",
        name="Native tool",
        version=1,
        entry_step="writer",
        execution_settings=ExecutionSettings(max_total_steps=10),
        nodes=[
            AgentNode(
                node_id="writer",
                graph_version_ref="native-tool@1",
                display=DisplayMetadata(title="Writer with format tool"),
                input_contract_ref="contract://topic",
                output_contract_ref="contract://tool-output",
                agent=AgentNodeData(
                    instruction=(
                        "Draft an article, then call the 'format_article' tool "
                        "to wrap it in a heading. Return the tool's output."
                    ),
                    model_provider="openai/gpt-4o-mini",
                    tool_refs=["format_article"],
                ),
            ),
        ],
        edges=[],
    )


async def main() -> int:
    eu_runner = ExecutableUnitRunner(build_demo_tool_registry())

    # Tool attachment: the agent's side of the binding. ``alias`` is
    # what the model calls (like an OpenAI function name), and
    # ``executable_unit_ref`` is the registered manifest ref the runtime
    # dispatches to.
    tool_attachment = ToolAttachmentManifest(
        alias="format_article",
        executable_unit_ref="eu://format_article",
        description="Wrap an article body in a Markdown heading.",
        permission_scope=("fs:none",),
    )

    tool_registry = ToolAttachmentRegistry([tool_attachment])
    tool_bridge = ToolAttachmentBridge(tool_registry)

    async def tool_executor(binding, payload):
        """Dispatch a tool call to the executable-unit runner."""
        result = await eu_runner.run_manifest_ref(binding.executable_unit_ref, payload)
        return result.output_data

    # Deterministic two-turn conversation: first turn plans the tool
    # call, second turn returns the final ToolOutput JSON.
    provider = DeterministicProviderAdapter(
        responses=[
            ProviderResponse(
                content=None,
                tool_calls=[
                    {
                        "id": "call-1",
                        "name": "format_article",
                        "args": {
                            "topic": "native tools",
                            "body": "Native tools run in-process and keep audit scope tight.",
                        },
                    }
                ],
            ),
            ProviderResponse(
                content={
                    "topic": "native tools",
                    "formatted": (
                        "# Native Tools\n\n"
                        "Native tools run in-process and keep audit scope tight.\n"
                    ),
                }
            ),
        ]
    )

    runner = AgentRunner(
        AgentConfig(
            name="writer",
            description="Drafts an article and formats it via a tool.",
            instruction="Draft and format.",
            model_name="openai/gpt-4o-mini",
            input_model=Topic,
            output_model=ToolOutput,
            tool_attachments=[tool_attachment],
        ),
        provider,
        tool_bridge=tool_bridge,
        tool_executor=tool_executor,
        granted_tool_permissions=["fs:none"],
    )

    async with running_service(
        build_graph(),
        contracts={
            "contract://topic": Topic,
            "contract://tool-input": ToolInput,
            "contract://tool-output": ToolOutput,
        },
        agent_runners={"writer": runner},
        executable_unit_runner=eu_runner,
    ) as demo:
        run = await demo.service.orchestrator.run_graph(
            demo.service.graph,
            {"topic": "native tools"},
            deployment_ref=demo.deployment_ref,
        )
        print_run_summary(run, label="native-tool")

        # Pull the per-node audit records — the agent's tool-call history
        # lives in ``execution_metadata`` and is what the audit timeline
        # surfaces through ``GET /v1/runs/{run_id}/timeline``.
        audit = await demo.service.audit_repository.list_by_run(run.run_id)
        for rec in audit:
            meta = rec.execution_metadata or {}
            tool_calls = meta.get("tool_call_records") or meta.get("tool_calls") or []
            if tool_calls:
                print("\n  tool calls captured in audit:")
                for call in tool_calls:
                    print(f"    - {call}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
