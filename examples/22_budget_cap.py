"""22 — Budget enforcement: BudgetEnforcer wired into the AgentRunner.

What this shows
---------------
The real :class:`BudgetEnforcer` wired onto an :class:`AgentRunner` via
``runner.budget_enforcer``. When the enforcer's cached view says the
tenant is over budget, the runner raises ``BudgetExceededError`` *before*
any provider call is made, and the orchestrator terminates the run with
``status == FAILED``.

``BudgetEnforcer.check_budget`` hits a Regulus backend HTTP endpoint in
production. For this example we point it at an :class:`httpx.MockTransport`
so the enforcer can be exercised end-to-end without spinning up Regulus.
(The ``_transport`` kwarg is a real API — see
``src/zeroth/core/econ/budget.py``.)

Run
---
    uv run python examples/22_budget_cap.py
"""

from __future__ import annotations

# Allow python examples/NN_name.py to find the sibling examples/_common.py helper.
import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import asyncio
import sys

import httpx

from examples._common import print_run_summary, running_service
from examples._contracts import Answer, Question
from zeroth.core.agent_runtime import (
    AgentConfig,
    AgentRunner,
    DeterministicProviderAdapter,
    ProviderResponse,
)
from zeroth.core.econ import BudgetEnforcer, CostEstimator
from zeroth.core.graph import (
    AgentNode,
    AgentNodeData,
    DisplayMetadata,
    ExecutionSettings,
    Graph,
)


def build_graph() -> Graph:
    return Graph(
        graph_id="budget-cap",
        name="Budget cap",
        version=1,
        entry_step="qa",
        execution_settings=ExecutionSettings(max_total_steps=5),
        nodes=[
            AgentNode(
                node_id="qa",
                graph_version_ref="budget-cap@1",
                display=DisplayMetadata(title="Budgeted Q&A"),
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


def mock_regulus_handler(*, spend_usd: float, cap_usd: float):
    """Return an httpx handler that mimics ``/dashboard/kpis`` responses."""

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "total_cost_usd": spend_usd,
                "budget_cap_usd": cap_usd,
            },
        )

    return _handler


def build_runner() -> AgentRunner:
    return AgentRunner(
        AgentConfig(
            name="qa",
            description="Deterministic runner used to prove budget blocking.",
            instruction="Answer.",
            model_name="openai/gpt-4o-mini",
            input_model=Question,
            output_model=Answer,
        ),
        DeterministicProviderAdapter(
            responses=[
                ProviderResponse(content={"answer": "This should never run."})
            ]
        ),
    )


async def run_once(*, spend_usd: float, cap_usd: float, label: str) -> None:
    runner = build_runner()
    runner.budget_enforcer = BudgetEnforcer(
        regulus_base_url="http://regulus.example",
        cache_ttl=0,  # Don't cache between scenarios in the demo.
        timeout=1.0,
        _transport=mock_regulus_handler(spend_usd=spend_usd, cap_usd=cap_usd),
    )

    async with running_service(
        build_graph(),
        contracts={
            "contract://question": Question,
            "contract://answer": Answer,
        },
        agent_runners={"qa": runner},
        deployment_ref=f"budget-cap-{label}",
    ) as demo:
        run = await demo.service.orchestrator.run_graph(
            demo.service.graph,
            {"question": "Does this fit the budget?"},
            deployment_ref=demo.deployment_ref,
        )
        print_run_summary(run, label=label)


async def main() -> int:
    # Scenario 1: tenant has headroom — the run succeeds.
    await run_once(spend_usd=0.0001, cap_usd=0.10, label="within-cap")
    # Scenario 2: tenant is over cap — the runner raises before the
    # provider is called and the orchestrator terminates the run.
    await run_once(spend_usd=0.12, cap_usd=0.10, label="over-cap")

    # Bonus: show the offline CostEstimator converting token counts to
    # USD via litellm's pricing table. This is the same helper
    # ``bootstrap_service`` plumbs into the orchestrator when Regulus
    # is enabled.
    estimator = CostEstimator()
    cost = estimator.estimate(
        "openai/gpt-4o-mini",
        input_tokens=500,
        output_tokens=500,
    )
    print(f"\noffline cost estimate (500 in + 500 out): ${cost}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
