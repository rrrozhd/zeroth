"""A representative AutoGen AgentChat application. It does not import Zeroth."""

from __future__ import annotations

import asyncio
import json

from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.conditions import MaxMessageTermination, TextMentionTermination
from autogen_agentchat.teams import RoundRobinGroupChat

PLANNER, REVIEWER, WRITER, QUICK = (
    "gpt-4.1-mini",
    "gpt-4.1",
    "claude-sonnet-4-20250514",
    "claude-haiku-4-5-20251001",
)


def agent(name: str, client, *, stream: bool = False) -> AssistantAgent:
    return AssistantAgent(
        name, model_client=client, system_message=f"You are the {name}.", model_client_stream=stream
    )


async def run_single(name: str, client, task: str) -> str:
    result = await agent(name, client).run(task=task)
    return str(result.messages[-1].content)


async def plan_then_write(planner_client, writer_client, task: str) -> str:
    """Two agents take turns until the writer says TERMINATE."""
    team = RoundRobinGroupChat(
        [agent("planner", planner_client), agent("writer", writer_client)],
        termination_condition=TextMentionTermination("TERMINATE") | MaxMessageTermination(3),
    )
    result = await team.run(task=task)
    return str(result.messages[-1].content)


async def stream_single(name: str, client, task: str) -> tuple[str, int]:
    """Streamed run: returns the final text and the number of chunk events seen."""
    chunks, final = 0, ""
    async for event in agent(name, client, stream=True).run_stream(task=task):
        kind = type(event).__name__
        if kind == "ModelClientStreamingChunkEvent":
            chunks += 1
        elif kind == "TaskResult":
            final = str(event.messages[-1].content)
    return final, chunks


async def robust_extract(client, text: str) -> dict:
    """Application-level retry: one more run when the agent returns invalid JSON."""
    for attempt in (1, 2):
        reply = await run_single("planner", client, f"Return JSON for: {text}")
        try:
            return json.loads(reply)
        except ValueError:
            if attempt == 2:
                raise
    raise AssertionError("unreachable")


async def answer_with_fallback(primary_client, fallback_client, task: str) -> str:
    try:
        return await run_single("writer", primary_client, task)
    except Exception:  # noqa: BLE001 - the application's own fallback policy
        return await run_single("reviewer", fallback_client, task)


async def fanout(client, tasks: list[str]) -> list[str]:
    return list(await asyncio.gather(*(run_single("planner", client, t) for t in tasks)))
