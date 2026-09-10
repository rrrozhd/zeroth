"""A representative CrewAI application: crews and a flow. It does not import Zeroth."""

from __future__ import annotations

import json

from crewai import Agent, Crew, Process, Task
from crewai.flow.flow import Flow, listen, start

PLANNER, REVIEWER, WRITER, QUICK = (
    "openai/gpt-4.1-mini",
    "openai/gpt-4.1",
    "anthropic/claude-sonnet-4-20250514",
    "anthropic/claude-haiku-4-5-20251001",
)


def agent(role: str, llm, *, retries: int = 2) -> Agent:
    return Agent(
        role=role,
        goal=f"{role} well",
        backstory="An experienced specialist.",
        llm=llm,
        verbose=False,
        max_iter=2,
        max_retry_limit=retries,
    )


def crew(tasks: list[Task]) -> Crew:
    return Crew(
        agents=[t.agent for t in tasks], tasks=tasks, process=Process.sequential, verbose=False
    )


def task(description: str, agent_: Agent, *, async_execution: bool = False) -> Task:
    return Task(
        description=description,
        expected_output="A short answer.",
        agent=agent_,
        async_execution=async_execution,
    )


def plan_then_write(planner_llm, writer_llm, question: str) -> str:
    plan = task(f"Plan: {question}", agent("planner", planner_llm))
    write = task(
        "Write the answer from the plan.",
        agent("writer", writer_llm),
    )
    return str(crew([plan, write]).kickoff())


def single(role: str, llm, question: str, *, retries: int = 2) -> str:
    return str(crew([task(question, agent(role, llm, retries=retries))]).kickoff())


def parallel_then_merge(planner_llm, merger_llm, question: str) -> str:
    # One agent object per concurrent task: CrewAI forbids running one executor concurrently.
    a = task(f"{question} (a)", agent("planner", planner_llm), async_execution=True)
    b = task(f"{question} (b)", agent("planner", planner_llm), async_execution=True)
    merge = task("Merge the branches.", agent("quick", merger_llm))
    merge.context = [a, b]
    return str(crew([a, b, merge]).kickoff())


def robust_extract(planner_llm, text: str) -> dict:
    """Application-level retry: one more crew run when the output is not JSON."""
    for attempt in (1, 2):
        reply = single("planner", planner_llm, f"Return JSON for: {text}")
        try:
            return json.loads(reply)
        except ValueError:
            if attempt == 2:
                raise
    raise AssertionError("unreachable")


def answer_with_fallback(writer_llm, reviewer_llm, question: str) -> str:
    try:
        return single("writer", writer_llm, question, retries=0)
    except Exception:  # noqa: BLE001 - the application's own fallback policy
        return single("reviewer", reviewer_llm, question)


class SummaryFlow(Flow):
    """A two-step flow that calls the LLM directly from its methods."""

    def __init__(self, llm, question: str) -> None:
        super().__init__()
        self.llm, self.question = llm, question

    @start()
    def draft(self):
        return self.llm.call(f"Draft: {self.question}")

    @listen(draft)
    def polish(self, draft):
        return self.llm.call(f"Polish: {draft}")
