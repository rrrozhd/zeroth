"""32 — Observability: MetricsCollector and correlation IDs across a real run.

What this shows
---------------
Two observability primitives working through the real service:

* :class:`MetricsCollector` — thread-safe in-process metrics store the
  bootstrap wires into :class:`RunWorker` and :class:`QueueDepthGauge`.
  You can render it straight to Prometheus text format.
* ``get_correlation_id`` / ``new_correlation_id`` — per-request
  correlation IDs that the service middleware propagates through the
  ``X-Correlation-ID`` header and logging context.

We drive a run through the orchestrator, manually emit a few metrics
around the call, and then render the Prometheus snapshot so a reader
can see the exact shape of what ``/v1/metrics`` returns in production.

Run
---
    uv run python examples/32_observability.py
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
from zeroth.core.observability import (
    MetricsCollector,
    new_correlation_id,
    set_correlation_id,
)


def build_graph() -> Graph:
    return Graph(
        graph_id="observability",
        name="Observability demo",
        version=1,
        entry_step="qa",
        execution_settings=ExecutionSettings(max_total_steps=5),
        nodes=[
            AgentNode(
                node_id="qa",
                graph_version_ref="observability@1",
                display=DisplayMetadata(title="Q&A"),
                input_contract_ref="contract://question",
                output_contract_ref="contract://answer",
                agent=AgentNodeData(
                    instruction="Answer.",
                    model_provider="openai/gpt-4o-mini",
                ),
            ),
        ],
        edges=[],
    )


def build_runner() -> AgentRunner:
    return AgentRunner(
        AgentConfig(
            name="qa",
            instruction="Answer.",
            model_name="openai/gpt-4o-mini",
            input_model=Question,
            output_model=Answer,
        ),
        DeterministicProviderAdapter(
            responses=[
                ProviderResponse(content={"answer": "Zeroth tracks runs end-to-end."})
            ]
        ),
    )


async def main() -> int:
    metrics = MetricsCollector()

    correlation_id = new_correlation_id()
    set_correlation_id(correlation_id)
    print(f"correlation_id for this request: {correlation_id}")

    async with running_service(
        build_graph(),
        contracts={
            "contract://question": Question,
            "contract://answer": Answer,
        },
        agent_runners={"qa": build_runner()},
    ) as demo:
        # bootstrap_service wires its own MetricsCollector onto the
        # worker; replace it with ours so the example can read back
        # whatever the orchestrator records during this run.
        demo.service.metrics_collector = metrics

        metrics.increment(
            "zeroth_examples_requests_total",
            labels={"example": "32_observability"},
        )

        start = time.monotonic()
        run = await demo.service.orchestrator.run_graph(
            demo.service.graph,
            {"question": "What does Zeroth track?"},
            deployment_ref=demo.deployment_ref,
        )
        elapsed = time.monotonic() - start

        metrics.observe(
            "zeroth_examples_run_duration_seconds",
            elapsed,
            labels={"example": "32_observability", "status": run.status.value},
        )
        metrics.gauge_set(
            "zeroth_examples_last_run_status",
            1.0 if run.status.value == "COMPLETED" else 0.0,
        )

        print_run_summary(run, label="observability")

        print("\nmetrics snapshot (Prometheus exposition format):")
        print(metrics.render_prometheus_text())
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
