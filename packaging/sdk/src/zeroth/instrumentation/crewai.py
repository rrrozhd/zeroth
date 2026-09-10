"""Capture CrewAI LLM calls as physical charges on the active run.

``instrument_crewai()`` registers one listener on CrewAI's process-global event
bus (calling it again returns the same listener, so registration cannot double
count). Every ``LLMCallStartedEvent`` opens a pending call on the run that is
active where the call is made; the matching ``LLMCallCompletedEvent`` becomes
one charge from the event's usage, named after the agent role or task, and an
``LLMCallFailedEvent`` becomes an unmeasured attempt. Crews, tasks and agents
are aggregates: counted for the run summary, never charged.

Attribution: CrewAI dispatches events on pooled handler threads with the
emitter's context, so ``current_run()`` names the run for sequential work, flows
and ``kickoff_async``. Tasks with ``async_execution=True`` run on plain threads
without context, so ``instrument_crewai()`` also wraps ``Crew.kickoff`` to bind
the crew to the run active in the application thread before any task thread
exists; those calls are attributed through their agent's crew. Because the pool
may process a call's completion before its start, charges are built from the
completion event's own fields; start events only record timing.

Billing identity comes from the model name only: ``provider/model`` (the
LiteLLM form CrewAI uses) declares the provider; a bare name is recorded with
provider ``unknown`` and stays unmeasured unless ``default_provider`` is given.
"""

from __future__ import annotations

import weakref
from dataclasses import dataclass
from threading import Lock
from time import perf_counter
from typing import Any

from crewai.events import BaseEventListener, crewai_event_bus
from crewai.events.types.agent_events import AgentExecutionStartedEvent
from crewai.events.types.crew_events import CrewKickoffStartedEvent
from crewai.events.types.llm_events import (
    LLMCallCompletedEvent,
    LLMCallFailedEvent,
    LLMCallStartedEvent,
)
from crewai.events.types.task_events import TaskStartedEvent
from zeroth.instrumentation.capture import Run, Usage, current_run

PRICED_PROVIDERS = frozenset({"openai", "anthropic"})


@dataclass
class _Pending:
    started: float


def _identity(model: str | None, default_provider: str | None) -> tuple[str, str]:
    name = model or "unknown"
    if "/" in name:
        provider, bare = name.split("/", 1)
        return bare, provider
    return name, default_provider or "unknown"


def _usage(usage: dict[str, Any] | None) -> Usage | None:
    if not usage:
        return None
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    if prompt == 0 and completion == 0:
        return None
    cached = int(usage.get("cached_prompt_tokens") or 0)
    return Usage(
        prompt - cached, completion, cache_read_tokens=cached,
        cache_write_tokens=int(usage.get("cache_creation_tokens") or 0),
        reasoning_tokens=int(usage.get("reasoning_tokens") or 0),
    )


class ZerothCrewListener(BaseEventListener):
    """One charge per CrewAI LLM call; crews, tasks and agents are aggregates."""

    def __init__(self, *, default_provider: str | None = None,
                 priced_providers=PRICED_PROVIDERS) -> None:
        self._default_provider = default_provider
        self._priced = priced_providers
        self._lock = Lock()
        self._pending: dict[str, _Pending] = {}
        self._crew_runs: dict[int, tuple[Any, Run]] = {}
        self._aggregates: dict[int, dict[str, Any]] = {}
        super().__init__()

    def bind_crew(self, crew: Any, run: Run) -> None:
        """Remember which run a crew kicked off in; entries die with the crew object."""
        key = id(crew)
        try:
            ref = weakref.ref(crew, lambda _ref, key=key: self._crew_runs.pop(key, None))
        except TypeError:  # pragma: no cover - crews are weak-referenceable in 1.14+
            ref = None
        with self._lock:
            self._crew_runs[key] = (ref, run)

    def _run_for(self, event: Any) -> Run | None:
        run = current_run()
        if run is not None:
            return run
        agent = getattr(event, "from_agent", None)
        crew = getattr(agent, "crew", None)
        if crew is None:
            return None
        with self._lock:
            bound = self._crew_runs.get(id(crew))
        return bound[1] if bound is not None else None

    def _aggregate(self, run: Run) -> dict[str, Any]:
        return self._aggregates.setdefault(
            id(run), {"framework": "crewai", "crews": 0, "tasks": 0, "agents": {}}
        )

    def aggregates(self, run: Run) -> dict[str, Any]:
        """Crew, task and agent counts recorded for ``run``, for ``Run.summary`` metadata."""
        with self._lock:
            return self._aggregates.pop(id(run), {"framework": "crewai", "crews": 0, "tasks": 0,
                                                  "agents": {}})

    def setup_listeners(self, bus) -> None:  # noqa: D102 - CrewAI hook
        @bus.on(CrewKickoffStartedEvent)
        def kickoff_started(source, event):
            run = current_run()
            if run is None:
                return
            self.bind_crew(source, run)  # fallback when Crew.kickoff was not wrapped
            with self._lock:
                self._aggregate(run)["crews"] += 1

        @bus.on(TaskStartedEvent)
        def task_started(source, event):
            run = self._run_for(event)
            if run is not None:
                with self._lock:
                    self._aggregate(run)["tasks"] += 1

        @bus.on(AgentExecutionStartedEvent)
        def agent_started(source, event):
            run = self._run_for(event)
            if run is not None:
                role = getattr(getattr(event, "agent", None), "role", None) or "agent"
                with self._lock:
                    agents = self._aggregate(run)["agents"]
                    agents[role] = agents.get(role, 0) + 1

        @bus.on(LLMCallStartedEvent)
        def call_started(source, event):
            with self._lock:
                self._pending[event.call_id] = _Pending(perf_counter())

        @bus.on(LLMCallCompletedEvent)
        def call_completed(source, event):
            run = self._run_for(event)
            if run is None:
                return
            model, provider = _identity(event.model, self._default_provider)
            run.charge(
                run.next_step(event.agent_role or event.task_name or "llm"), model=model,
                usage=_usage(event.usage), provider=provider, priced=provider in self._priced,
                request_id=getattr(event, "response_id", None), latency_ms=self._latency(event),
                metadata={"framework": "crewai", "agent": event.agent_role,
                          "task": event.task_name},
            )

        @bus.on(LLMCallFailedEvent)
        def call_failed(source, event):
            run = self._run_for(event)
            if run is None:
                return
            model, provider = _identity(event.model, self._default_provider)
            run.charge(
                run.next_step(event.agent_role or event.task_name or "llm"), model=model,
                usage=None, provider=provider, priced=provider in self._priced,
                error=str(event.error)[:120], latency_ms=self._latency(event),
                metadata={"framework": "crewai", "agent": event.agent_role,
                          "task": event.task_name},
            )

    def _latency(self, event: Any) -> int:
        with self._lock:
            pending = self._pending.pop(getattr(event, "call_id", None), None)
        return 0 if pending is None else int((perf_counter() - pending.started) * 1000)


_listener: ZerothCrewListener | None = None


def _wrap_kickoff(listener: ZerothCrewListener) -> None:
    """Bind crew->run in the application thread, before any async task thread exists."""
    from crewai import Crew

    original = Crew.kickoff
    if getattr(original, "_zeroth_captured", False):
        return

    def kickoff(self, *args: Any, **kwargs: Any):
        run = current_run()
        if run is not None:
            listener.bind_crew(self, run)
        return original(self, *args, **kwargs)

    kickoff._zeroth_captured = True  # type: ignore[attr-defined]
    Crew.kickoff = kickoff


def instrument_crewai(**options: Any) -> ZerothCrewListener:
    """Register the Zeroth listener once on ``crewai_event_bus`` and return it."""
    global _listener
    if _listener is None:
        _listener = ZerothCrewListener(**options)
        _wrap_kickoff(_listener)
    return _listener


__all__ = ["ZerothCrewListener", "crewai_event_bus", "instrument_crewai"]
