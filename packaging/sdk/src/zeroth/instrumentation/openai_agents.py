"""Capture OpenAI Agents SDK runs as physical charges on the active run.

``ZerothRunHooks`` is a ``RunHooks`` implementation and a context manager::

    with ZerothRunHooks(model_provider=provider) as hooks:
        result = await Runner.run(agent, question, hooks=hooks, run_config=config)

Every model call the SDK reports (``on_llm_start`` / ``on_llm_end``) becomes one
charge from ``ModelResponse.usage``, named after the agent. The SDK runs hook
coroutines in their own tasks, so a scope opened inside a hook would not reach
the runner's model call; instead the ``with`` block installs one shared holder
as the physical scope for the whole run, which every task inherits. A provider
adapter (``instrument_openai`` on the client behind the model provider) marks
that holder when it charges the HTTP call, and the hooks skip a call whose
window saw such a mark, so wrapping both layers cannot double count. The SDK
fires no hook for a failed model call, so leaving the block charges every call
still pending as an unmeasured failed attempt. Agent runs and handoffs are
aggregates, counted in ``hooks.aggregates()`` for the run summary, never
charged. Trace processors are not touched.
"""

from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter
from typing import Any

from agents import RunHooks
from zeroth.instrumentation import _scope
from zeroth.instrumentation.capture import Run, Usage, current_run

PRICED_PROVIDERS = frozenset({"openai", "anthropic"})


@dataclass
class _Pending:
    step: str
    marks_at_start: int
    started: float
    model: str
    provider: str


class _Holder:
    """The physical scope for one Runner call, shared by the runner and hook tasks."""

    def __init__(self) -> None:
        self.pending: list[_Pending] = []
        self.marks = 0

    @property
    def step(self) -> str | None:
        return self.pending[-1].step if self.pending else None

    @property
    def charged(self) -> bool:
        return False  # a provider capture always adopts the current step name

    @charged.setter
    def charged(self, value: bool) -> None:
        self.marks += int(bool(value))


def _model_identity(agent: Any, model_provider: Any) -> tuple[str, str]:
    """Model name and billing provider for an agent.

    A model given by name is resolved through the same ``ModelProvider`` the run
    uses (the SDK's OpenAI provider when none is declared). A model object names
    itself in ``model``; its provider is ``provider`` when declared, the ``provider/``
    prefix of a routed name, ``openai`` for the SDK's own OpenAI models, else unknown.
    Nothing is inferred from response shapes.
    """
    model = agent.model
    if isinstance(model, str) or model is None:
        name = model or "unknown"
        if model_provider is None:
            if "/" in name:
                return name.split("/", 1)[1], name.split("/", 1)[0]
            return name, "openai"
        model = model_provider.get_model(name)
    name = str(getattr(model, "model", None) or "unknown")
    provider = getattr(model, "provider", None)
    if provider is None and "/" in name:
        provider, name = name.split("/", 1)
    if provider is None and type(model).__name__.startswith("OpenAI"):
        provider = "openai"
    return name, provider or "unknown"


def _usage(usage: Any) -> Usage | None:
    if usage is None or not getattr(usage, "requests", 0) or not usage.total_tokens:
        return None
    cached = getattr(getattr(usage, "input_tokens_details", None), "cached_tokens", 0) or 0
    reasoning = getattr(getattr(usage, "output_tokens_details", None), "reasoning_tokens", 0) or 0
    return Usage(
        usage.input_tokens - cached, usage.output_tokens, cache_read_tokens=cached,
        reasoning_tokens=reasoning,
    )


class ZerothRunHooks(RunHooks):
    """One charge per model call the Agents SDK reports; aggregates for the summary."""

    def __init__(
        self,
        run: Run | None = None,
        *,
        model_provider: Any = None,
        priced_providers=PRICED_PROVIDERS,
    ) -> None:
        self._run = run
        self._model_provider = model_provider
        self._priced = priced_providers
        self._holder = _Holder()
        self._token: Any = None
        self._agents: dict[str, dict[str, int]] = {}
        self.handoffs: list[tuple[str, str]] = []

    @property
    def run(self) -> Run | None:
        return self._run or current_run()

    def aggregates(self) -> dict[str, Any]:
        """Per-agent model-call counts and handoffs, for ``Run.summary`` metadata."""
        return {"framework": "openai-agents", "agents": dict(self._agents),
                "handoffs": [list(pair) for pair in self.handoffs]}

    async def on_llm_start(self, context, agent, system_prompt, input_items) -> None:
        run = self.run
        if run is None:
            return
        model, provider = _model_identity(agent, self._model_provider)
        self._holder.pending.append(_Pending(
            run.next_step(agent.name), self._holder.marks, perf_counter(), model, provider,
        ))
        self._agents.setdefault(agent.name, {"model_calls": 0, "turns": 0})["turns"] += 1

    async def on_llm_end(self, context, agent, response) -> None:
        run = self.run
        pending = self._pop(agent.name)
        if run is None or pending is None:
            return
        self._agents[agent.name]["model_calls"] += 1
        if self._holder.marks > pending.marks_at_start:
            return  # the provider adapter charged this call's HTTP request
        run.charge(
            pending.step, model=pending.model, usage=_usage(response.usage),
            provider=pending.provider, priced=pending.provider in self._priced,
            request_id=getattr(response, "request_id", None),
            latency_ms=int((perf_counter() - pending.started) * 1000),
            metadata={"framework": "openai-agents", "agent": agent.name},
        )

    async def on_handoff(self, context, from_agent, to_agent) -> None:
        self.handoffs.append((from_agent.name, to_agent.name))

    def _pop(self, agent_name: str) -> _Pending | None:
        pending = self._holder.pending
        for index in range(len(pending) - 1, -1, -1):
            if pending[index].step.split("#", 1)[0] == agent_name:
                return pending.pop(index)
        return pending.pop() if pending else None

    def settle(self, error: BaseException | None) -> None:
        """Charge calls that never reported an end: the SDK raised out of them."""
        run = self.run
        while self._holder.pending:
            pending = self._holder.pending.pop()
            if run is None or self._holder.marks > pending.marks_at_start:
                continue
            run.charge(
                pending.step, model=pending.model, usage=None, provider=pending.provider,
                priced=pending.provider in self._priced,
                error=type(error).__name__ if error is not None else "aborted",
                latency_ms=int((perf_counter() - pending.started) * 1000),
                metadata={"framework": "openai-agents"},
            )

    def __enter__(self) -> ZerothRunHooks:
        self._token = _scope._scope.set(self._holder)  # shared by every task of this run
        return self

    def __exit__(self, exc_type, error, tb) -> None:
        try:
            self.settle(error)
        finally:
            _scope._scope.reset(self._token)
