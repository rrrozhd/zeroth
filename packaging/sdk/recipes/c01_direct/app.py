"""A representative existing application using the OpenAI and Anthropic SDKs directly.

Nothing in this module knows about Zeroth. The recipe instruments the two
clients it is given and wraps each request in a run; see README.md and check.py.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context

PLANNER = "gpt-4.1-mini"
REVIEWER = "gpt-4.1"
WRITER = "claude-sonnet-4-20250514"
QUICK = "claude-haiku-4-5-20251001"

SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "Search the web.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
    },
}


class Assistant:
    def __init__(self, openai_client, anthropic_client, search: Callable[[str], str]) -> None:
        self.openai, self.anthropic, self.search = openai_client, anthropic_client, search

    def plan(self, question: str, model: str = PLANNER) -> str:
        completion = self.openai.chat.completions.create(
            model=model, messages=[{"role": "user", "content": question}]
        )
        return completion.choices[0].message.content

    def review(self, text: str, model: str = REVIEWER) -> str:
        response = self.openai.responses.create(model=model, input=text)
        return response.output_text

    def answer(self, question: str, model: str = WRITER) -> str:
        message = self.anthropic.messages.create(
            model=model, max_tokens=512, messages=[{"role": "user", "content": question}]
        )
        return message.content[0].text

    def stream_plan(self, question: str, model: str = PLANNER) -> str:
        stream = self.openai.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": question}],
            stream=True,
            stream_options={"include_usage": True},
        )
        return "".join(chunk.choices[0].delta.content or "" for chunk in stream if chunk.choices)

    def stream_review(self, text: str, model: str = REVIEWER) -> str:
        events = self.openai.responses.create(model=model, input=text, stream=True)
        return "".join(e.delta for e in events if e.type == "response.output_text.delta")

    def stream_answer(self, question: str, model: str = WRITER) -> str:
        with self.anthropic.messages.stream(
            model=model, max_tokens=512, messages=[{"role": "user", "content": question}]
        ) as stream:
            return "".join(stream.text_stream)

    def robust_extract(self, text: str) -> dict:
        """Application-level retry: one more attempt when the model returns invalid JSON."""
        for attempt in (1, 2):
            reply = self.plan(f"Return JSON for: {text}")
            try:
                return json.loads(reply)
            except ValueError:
                if attempt == 2:
                    raise
        raise AssertionError("unreachable")

    def answer_with_fallback(self, question: str) -> str:
        import anthropic

        try:
            return self.answer(question)
        except anthropic.APIStatusError:
            return self.review(question)

    def fanout(self, questions: list[str]) -> list[str]:
        """Parallel planning; ``copy_context`` carries the caller's context into workers."""
        with ThreadPoolExecutor(max_workers=len(questions)) as pool:
            futures = [pool.submit(copy_context().run, self.plan, q) for q in questions]
            return [f.result() for f in futures]

    def search_then_review(self, question: str) -> str:
        return self.review(f"{question}\n\n{self.search(question)}")

    def tool_cycle(self, question: str) -> str:
        """One model turn asks for the tool, the application runs it, a second turn answers."""
        first = self.openai.chat.completions.create(
            model=PLANNER, messages=[{"role": "user", "content": question}], tools=[SEARCH_TOOL]
        )
        call = first.choices[0].message.tool_calls[0]
        result = self.search(json.loads(call.function.arguments)["query"])
        second = self.openai.chat.completions.create(
            model=PLANNER,
            messages=[
                {"role": "user", "content": question},
                first.choices[0].message,
                {"role": "tool", "tool_call_id": call.id, "content": result},
            ],
        )
        return second.choices[0].message.content


class AsyncAssistant:
    def __init__(self, openai_client, anthropic_client) -> None:
        self.openai, self.anthropic = openai_client, anthropic_client

    async def plan(self, question: str, model: str = PLANNER) -> str:
        completion = await self.openai.chat.completions.create(
            model=model, messages=[{"role": "user", "content": question}]
        )
        return completion.choices[0].message.content

    async def stream_plan(self, question: str, model: str = PLANNER) -> str:
        stream = await self.openai.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": question}],
            stream=True,
            stream_options={"include_usage": True},
        )
        parts = []
        async for chunk in stream:
            if chunk.choices:
                parts.append(chunk.choices[0].delta.content or "")
        return "".join(parts)

    async def answer(self, question: str, model: str = WRITER) -> str:
        message = await self.anthropic.messages.create(
            model=model, max_tokens=512, messages=[{"role": "user", "content": question}]
        )
        return message.content[0].text

    async def stream_answer(self, question: str, model: str = WRITER) -> str:
        async with self.anthropic.messages.stream(
            model=model, max_tokens=512, messages=[{"role": "user", "content": question}]
        ) as stream:
            parts = [text async for text in stream.text_stream]
        return "".join(parts)
