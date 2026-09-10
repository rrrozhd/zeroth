"""A representative OpenAI Agents SDK application. It does not import Zeroth."""

from __future__ import annotations

import asyncio
import json

from agents import Agent, RunConfig, Runner, function_tool

PLANNER, REVIEWER, WRITER, QUICK = (
    "gpt-4.1-mini",
    "gpt-4.1",
    "claude-sonnet-4-20250514",
    "claude-haiku-4-5-20251001",
)


@function_tool
def web_search(query: str) -> str:
    return f"results for {query}"


def writer() -> Agent:
    return Agent(name="writer", instructions="Write the answer.", model=WRITER)


def planner(handoff_to: Agent | None = None, tools: list | None = None) -> Agent:
    return Agent(
        name="planner",
        instructions="Plan, then hand off.",
        model=PLANNER,
        handoffs=[handoff_to] if handoff_to else [],
        tools=tools or [],
    )


def reviewer() -> Agent:
    return Agent(name="reviewer", instructions="Review.", model=REVIEWER)


def quick() -> Agent:
    return Agent(name="quick", instructions="Answer briefly.", model=QUICK)


async def run(agent: Agent, question: str, *, hooks=None, config: RunConfig | None = None) -> str:
    result = await Runner.run(agent, question, hooks=hooks, run_config=config)
    return str(result.final_output)


async def robust_extract(agent: Agent, text: str, *, hooks=None, config=None) -> dict:
    """Application-level retry: one more run when the agent returns invalid JSON."""
    for attempt in (1, 2):
        reply = await run(agent, f"Return JSON for: {text}", hooks=hooks, config=config)
        try:
            return json.loads(reply)
        except ValueError:
            if attempt == 2:
                raise
    raise AssertionError("unreachable")


async def answer_with_fallback(
    primary: Agent, fallback: Agent, question: str, *, hooks=None, config=None
) -> str:
    try:
        return await run(primary, question, hooks=hooks, config=config)
    except Exception:  # noqa: BLE001 - the application's own fallback policy
        return await run(fallback, question, hooks=hooks, config=config)


async def fanout(agent: Agent, questions: list[str], *, hooks=None, config=None) -> list[str]:
    return list(
        await asyncio.gather(*(run(agent, q, hooks=hooks, config=config) for q in questions))
    )
