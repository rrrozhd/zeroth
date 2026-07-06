"""30 — Contracts and edge mappings, all four operation types in one graph.

What this shows
---------------
Two nodes with different input/output :class:`ContractRegistry`-backed
contracts, wired together by an :class:`EdgeMapping` that exercises
every operation the library supports:

* :class:`PassthroughMappingOperation` — copy a field verbatim.
* :class:`RenameMappingOperation` — copy a field to a new name.
* :class:`ConstantMappingOperation` — pin a field to a literal.
* :class:`DefaultMappingOperation` — fall back to a value when the
  source path is missing.

The orchestrator evaluates the mapping when the edge fires, so the
downstream node sees the remapped payload and its own contract
validates it without either agent knowing the wiring details.

Run
---
    uv run python examples/30_contracts_and_mappings.py
"""

from __future__ import annotations

# Allow python examples/NN_name.py to find the sibling examples/_common.py helper.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import asyncio
import sys

from pydantic import BaseModel, Field

from examples._common import print_run_summary, running_service
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
    Edge,
    ExecutionSettings,
    Graph,
)
from zeroth.core.mappings.models import (
    ConstantMappingOperation,
    DefaultMappingOperation,
    EdgeMapping,
    PassthroughMappingOperation,
    RenameMappingOperation,
)


class UpstreamInput(BaseModel):
    """What the first agent receives."""

    user_query: str


class UpstreamOutput(BaseModel):
    """What the first agent emits — note the unusual field names."""

    original_query: str
    raw_score: float = 0.0
    notes: str = ""


class DownstreamInput(BaseModel):
    """What the second agent expects — different field names on purpose."""

    query: str
    confidence: float = 0.0
    tag: str = ""
    priority: str = Field(default="normal")


class DownstreamOutput(BaseModel):
    """Final output shape."""

    query: str
    confidence: float
    tag: str
    priority: str
    verdict: str


def build_graph() -> Graph:
    return Graph(
        graph_id="contracts-mappings",
        name="Contracts and mappings",
        version=1,
        entry_step="classifier",
        execution_settings=ExecutionSettings(max_total_steps=5),
        nodes=[
            AgentNode(
                node_id="classifier",
                graph_version_ref="contracts-mappings@1",
                display=DisplayMetadata(title="Classifier"),
                input_contract_ref="contract://upstream-input",
                output_contract_ref="contract://upstream-output",
                agent=AgentNodeData(
                    instruction="Classify.",
                    model_provider="openai/gpt-4o-mini",
                ),
            ),
            AgentNode(
                node_id="reviewer",
                graph_version_ref="contracts-mappings@1",
                display=DisplayMetadata(title="Reviewer"),
                input_contract_ref="contract://downstream-input",
                output_contract_ref="contract://downstream-output",
                agent=AgentNodeData(
                    instruction="Review.",
                    model_provider="openai/gpt-4o-mini",
                ),
            ),
        ],
        edges=[
            Edge(
                edge_id="classifier-to-reviewer",
                source_node_id="classifier",
                target_node_id="reviewer",
                mapping=EdgeMapping(
                    operations=[
                        # Copy a field whose name differs on each side.
                        RenameMappingOperation(
                            source_path="original_query",
                            target_path="query",
                        ),
                        # Keep the same name across the boundary.
                        PassthroughMappingOperation(
                            source_path="raw_score",
                            target_path="confidence",
                        ),
                        # Pin a literal — useful for provenance markers.
                        ConstantMappingOperation(
                            target_path="tag",
                            value="examples-30",
                        ),
                        # Fall back to ``normal`` if the upstream didn't set it.
                        DefaultMappingOperation(
                            source_path="priority",
                            target_path="priority",
                            default_value="normal",
                        ),
                    ]
                ),
            ),
        ],
    )


async def main() -> int:
    classifier = AgentRunner(
        AgentConfig(
            name="classifier",
            instruction="Classify.",
            model_name="openai/gpt-4o-mini",
            input_model=UpstreamInput,
            output_model=UpstreamOutput,
        ),
        DeterministicProviderAdapter(
            responses=[
                ProviderResponse(
                    content={
                        "original_query": "is this urgent?",
                        "raw_score": 0.82,
                        "notes": "looks urgent",
                    }
                )
            ]
        ),
    )
    reviewer = AgentRunner(
        AgentConfig(
            name="reviewer",
            instruction="Review.",
            model_name="openai/gpt-4o-mini",
            input_model=DownstreamInput,
            output_model=DownstreamOutput,
        ),
        DeterministicProviderAdapter(
            responses=[
                ProviderResponse(
                    content={
                        "query": "is this urgent?",
                        "confidence": 0.82,
                        "tag": "examples-30",
                        "priority": "normal",
                        "verdict": "escalate",
                    }
                )
            ]
        ),
    )

    async with running_service(
        build_graph(),
        contracts={
            "contract://upstream-input": UpstreamInput,
            "contract://upstream-output": UpstreamOutput,
            "contract://downstream-input": DownstreamInput,
            "contract://downstream-output": DownstreamOutput,
        },
        agent_runners={"classifier": classifier, "reviewer": reviewer},
    ) as demo:
        run = await demo.service.orchestrator.run_graph(
            demo.service.graph,
            {"user_query": "is this urgent?"},
            deployment_ref=demo.deployment_ref,
        )
        print_run_summary(run, label="contracts-mappings")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
