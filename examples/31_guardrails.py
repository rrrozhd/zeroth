"""31 — Guardrails: GuardrailConfig wired through bootstrap, DeadLetterManager observed.

What this shows
---------------
:class:`GuardrailConfig` is what you'd pass to ``bootstrap_service`` to
tune the durable dispatcher's knobs: max concurrency, max failure
count before dead-letter, rate limit bucket size, backpressure depth,
and more. This example bootstraps a service with a very aggressive
``max_failure_count=2`` and drives it with an agent that always fails,
then directly asks :class:`DeadLetterManager` whether the run has
crossed the dead-letter threshold.

The point is: the guardrail knobs aren't hypothetical — they're fields
on a Pydantic model that :func:`bootstrap_service` already reads.

Run
---
    uv run python examples/31_guardrails.py
"""

from __future__ import annotations

# Allow python examples/NN_name.py to find the sibling examples/_common.py helper.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import asyncio
import sys

from examples._common import (
    bootstrap_examples_service,
    print_run_summary,
)
from examples._contracts import Answer, Question
from zeroth.core.graph import (
    AgentNode,
    AgentNodeData,
    DisplayMetadata,
    ExecutionSettings,
    Graph,
)
from zeroth.core.guardrails import GuardrailConfig
from zeroth.core.runs import RunStatus


def build_graph() -> Graph:
    return Graph(
        graph_id="guardrails",
        name="Guardrails demo",
        version=1,
        entry_step="qa",
        execution_settings=ExecutionSettings(max_total_steps=5),
        nodes=[
            AgentNode(
                node_id="qa",
                graph_version_ref="guardrails@1",
                display=DisplayMetadata(title="Failing QA"),
                input_contract_ref="contract://question",
                output_contract_ref="contract://answer",
                agent=AgentNodeData(
                    instruction="Always fail.",
                    model_provider="openai/gpt-4o-mini",
                ),
            ),
        ],
        edges=[],
    )


class _AlwaysFailRunner:
    """Async runner whose ``run`` raises — lets us exercise the failure path.

    The orchestrator catches the exception, marks the run as failed,
    and the guardrail config decides what happens next (retry, dead-
    letter, metric emission, etc.).
    """

    async def run(self, *_args, **_kwargs):
        raise RuntimeError("deliberate failure for the guardrail demo")


async def main() -> int:
    config = GuardrailConfig(
        max_concurrency=2,
        max_failure_count=2,  # dead-letter after 2 consecutive failures
        backpressure_queue_depth=8,
        rate_limit_capacity=5.0,
        rate_limit_refill_rate=1.0,
    )
    print(
        f"guardrail config: max_concurrency={config.max_concurrency} "
        f"max_failure_count={config.max_failure_count} "
        f"rate_limit_capacity={config.rate_limit_capacity}"
    )

    demo = await bootstrap_examples_service(
        build_graph(),
        contracts={
            "contract://question": Question,
            "contract://answer": Answer,
        },
        agent_runners={"qa": _AlwaysFailRunner()},
        deployment_ref="guardrails-demo",
    )
    # Apply the custom guardrail knobs to the dead-letter manager.
    # bootstrap_service wires a default GuardrailConfig; we swap in the
    # custom one here because ``bootstrap_examples_service`` doesn't
    # take one (kept minimal on purpose).
    demo.service.dead_letter_manager = type(demo.service.dead_letter_manager)(
        run_repository=demo.service.run_repository,
        max_failure_count=config.max_failure_count,
    )

    # Drive two deliberate failures on the same run. Each failure goes
    # through ``DeadLetterManager.handle_run_failure``, which bumps a
    # persistent counter and transitions the run to
    # ``failure_state.reason == "dead_letter"`` once it crosses the
    # configured ``max_failure_count``.
    run = await demo.service.orchestrator.run_graph(
        demo.service.graph,
        {"question": "failing attempt 1"},
        deployment_ref=demo.deployment_ref,
    )
    print_run_summary(run, label="attempt-1")
    assert run.status is RunStatus.FAILED

    for attempt in (1, 2):
        dead_lettered = await demo.service.dead_letter_manager.handle_run_failure(
            run.run_id
        )
        print(
            f"  handle_run_failure #{attempt} → dead_lettered={dead_lettered}"
        )

    reloaded = await demo.service.run_repository.get(run.run_id)
    assert reloaded is not None and reloaded.failure_state is not None
    print(
        f"\nfinal failure_state.reason = {reloaded.failure_state.reason!r} "
        f"(details={reloaded.failure_state.details})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
