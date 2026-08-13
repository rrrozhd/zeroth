"""``max_tool_calls`` is a budget for the agent run, not for one attempt.

ZER-49 A06-24. The retry loop reassigns the outer ``messages`` from
``_resolve_tool_calls``, so attempt 2 starts from attempt 1's history --
tool-call messages included -- and re-seeds the tool loop from it. The
per-attempt tool counter did not follow: it reset to zero every attempt, so
``max_attempts`` attempts could execute ``max_attempts x max_tool_calls`` tools.
For side-effecting tools that is real, repeated, unbudgeted work.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from zeroth.runtime.agents.models import AgentConfig
from zeroth.runtime.agents.provider import ProviderResponse
from zeroth.runtime.agents.runner import AgentRunner
from zeroth.runtime.agents.tools import ToolAttachmentManifest


class _In(BaseModel):
    text: str


class _Out(BaseModel):
    result: str


class _ScriptedProvider:
    """Replays a fixed sequence of provider turns and records tool_choice."""

    def __init__(self, *turns: ProviderResponse) -> None:
        self._turns = list(turns)
        self.invocations = 0

    async def ainvoke(self, request: Any) -> ProviderResponse:
        self.invocations += 1
        if self._turns:
            return self._turns.pop(0)
        return ProviderResponse(content='{"result": "ok"}')


def _tool_turn(call_id: str) -> ProviderResponse:
    response = ProviderResponse(content=None)
    response.tool_calls = [{"id": call_id, "name": "charge", "args": {"amount": 10}}]
    return response


def _runner(
    provider: _ScriptedProvider,
    executions: list[Any],
    *,
    max_tool_calls: int = 1,
) -> AgentRunner:
    config = AgentConfig(
        name="budget-agent",
        instruction="charge it",
        model_name="test-model",
        input_model=_In,
        output_model=_Out,
        max_tool_calls=max_tool_calls,
        retry_policy={"max_retries": 1, "retry_on_validation_error": True},
        tool_attachments=[
            ToolAttachmentManifest(
                alias="charge",
                executable_unit_ref="node://charge",
                description="Charge the card",
            )
        ],
    )
    runner = AgentRunner(config, provider)

    def _execute(binding: Any, arguments: Any, tool_call_id: str | None = None) -> dict[str, Any]:
        executions.append((binding.alias, dict(arguments), tool_call_id))
        return {"charged": True}

    runner.tool_executor = _execute
    return runner


async def test_a_retry_does_not_refill_the_tool_budget() -> None:
    """The whole run gets ``max_tool_calls`` tools, however many attempts it takes.

    Attempt 1 spends the budget and then fails output validation. Attempt 2
    inherits its tool results in the message history, so re-running the tool
    would repeat an effect the model already has the answer to -- and would do it
    outside any cap, since the counter had restarted.
    """
    executions: list[Any] = []
    provider = _ScriptedProvider(
        _tool_turn("call-1"),  # attempt 1 asks for a tool
        ProviderResponse(content="not valid json"),  # attempt 1 fails validation
        _tool_turn("call-2"),  # attempt 2 asks again
        ProviderResponse(content='{"result": "ok"}'),  # forced tool_choice="none"
    )

    result = await _runner(provider, executions).run({"text": "hi"})

    assert len(executions) == 1, f"the tool budget was refilled by the retry: {executions}"
    assert result.attempts == 2, "the test must actually exercise a second attempt"


async def test_the_run_record_keeps_every_tool_call_the_run_executed() -> None:
    """A retry must not erase the earlier attempt's tool calls from the record.

    The audits were rebuilt per attempt, so a run whose first attempt charged a
    card and then failed validation reported, durably, that it made the tool
    calls of the *final* attempt only. The effect happened; the record has to
    say so. The budget is two here precisely so both attempts execute a tool --
    with a one-call budget the record is length one either way and proves
    nothing.
    """
    executions: list[Any] = []
    provider = _ScriptedProvider(
        _tool_turn("call-1"),  # attempt 1 charges
        ProviderResponse(content="not valid json"),  # attempt 1 fails validation
        _tool_turn("call-2"),  # attempt 2 charges again, within budget
        ProviderResponse(content='{"result": "ok"}'),
    )

    result = await _runner(provider, executions, max_tool_calls=2).run({"text": "hi"})

    assert len(executions) == 2, "both attempts must actually execute a tool"
    assert len(result.tool_call_records) == 2, (
        f"the record dropped an executed tool call: {result.tool_call_records}"
    )
    assert [record["tool"]["alias"] for record in result.tool_call_records] == [
        "charge",
        "charge",
    ]
