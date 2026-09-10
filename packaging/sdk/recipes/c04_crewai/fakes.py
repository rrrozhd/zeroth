"""Deterministic CrewAI LLMs: queued replies emitted through CrewAI's own event helpers."""

from __future__ import annotations

import inspect
from collections import deque

from crewai.llms.base_llm import BaseLLM, llm_call_context

try:
    from crewai.events.types.llm_events import LLMCallType

    _CALL = LLMCallType.LLM_CALL
except ImportError:  # pragma: no cover - older event modules
    _CALL = "llm_call"


def usage(input_tokens: int, output_tokens: int, cached: int = 0) -> dict:
    """The dict CrewAI attaches to LLMCallCompletedEvent; prompt tokens include cached ones."""
    return {
        "prompt_tokens": input_tokens + cached,
        "completion_tokens": output_tokens,
        "cached_prompt_tokens": cached,
        "total_tokens": input_tokens + cached + output_tokens,
    }


class FakeLLM(BaseLLM):
    """Answers from a shared per-model queue; an Exception item is raised as a failed call."""

    def __init__(self, model: str, queue: deque) -> None:
        super().__init__(model=model)
        self._queue = queue

    def call(
        self,
        messages,
        tools=None,
        callbacks=None,
        available_functions=None,
        from_task=None,
        from_agent=None,
        response_model=None,
    ):
        with llm_call_context():
            self._emit_call_started_event(
                messages=messages,
                tools=tools,
                callbacks=callbacks,
                available_functions=available_functions,
                from_task=from_task,
                from_agent=from_agent,
            )
            item = self._queue.popleft()
            if isinstance(item, Exception):
                self._emit_call_failed_event(
                    error=str(item), from_task=from_task, from_agent=from_agent
                )
                raise item
            text, tokens = item
            self._completed(text, tokens, messages, from_task, from_agent)
            return text

    def _completed(self, text, tokens, messages, from_task, from_agent) -> None:
        """Emit the completed event with usage on every supported CrewAI release."""
        wanted = {
            "response": text,
            "call_type": _CALL,
            "from_task": from_task,
            "from_agent": from_agent,
            "messages": messages,
            "usage": tokens,
            "response_id": "resp-fixture",
        }
        accepted = inspect.signature(self._emit_call_completed_event).parameters
        if "usage" in accepted:
            self._emit_call_completed_event(**{k: v for k, v in wanted.items() if k in accepted})
            return
        from crewai.events import crewai_event_bus
        from crewai.events.types.llm_events import LLMCallCompletedEvent
        from crewai.llms.base_llm import get_current_call_id

        fields = LLMCallCompletedEvent.model_fields
        event = {k: v for k, v in wanted.items() if k in fields}
        event.update(
            {
                k: v
                for k, v in (
                    ("model", self.model),
                    ("call_id", get_current_call_id()),
                    ("agent_role", getattr(from_agent, "role", None)),
                    (
                        "task_name",
                        getattr(from_task, "name", None) or getattr(from_task, "description", None),
                    ),
                )
                if k in fields
            }
        )
        crewai_event_bus.emit(self, event=LLMCallCompletedEvent(**event))

    def supports_function_calling(self) -> bool:
        return False

    def supports_stop_words(self) -> bool:
        return False

    def get_context_window_size(self) -> int:
        return 8192


class Replies:
    """One queue per model name; ``expect`` queues ``(text, usage)`` or an Exception."""

    def __init__(self) -> None:
        self.queues: dict[str, deque] = {}

    def llm(self, model: str) -> FakeLLM:
        return FakeLLM(model, self.queues.setdefault(model, deque()))

    def expect(self, model: str, *items) -> None:
        self.queues.setdefault(model, deque()).extend(items)

    @property
    def pending(self) -> int:
        return sum(len(q) for q in self.queues.values())


def final(text: str = "done") -> str:
    """CrewAI's ReAct executor accepts an answer only in this form."""
    return f"Final Answer: {text}"
