"""Deterministic provider replay for the C01 checks: no network, no keys.

Each provider serves a FIFO queue of ``Reply`` specs, rendered in the exact
response and event shapes the real SDK parses, through the SDK's own httpx
module (``anthropic`` >= 1.0 and ``openai`` >= 3.0 ship on ``httpx2``).
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass


def httpx_for(sdk_module) -> object:
    """The httpx package a provider SDK is built on (``httpx`` or ``httpx2``)."""
    base = __import__(f"{sdk_module.__name__}._base_client", fromlist=["_"])
    return getattr(base, "httpx2", None) or base.httpx


@dataclass
class Reply:
    """One physical response: usage is (input, output, cache_read[, cache_write])."""

    usage: tuple | None = (10, 5, 0)
    status: int = 200
    text: str = "ok"
    model: str | None = None
    tool_call: dict | None = None
    request_id: str = "req-fixture"


class Replay:
    def __init__(self, provider: str, httpx_module) -> None:
        self.provider, self.httpx = provider, httpx_module
        self.queue: deque[Reply] = deque()
        self.requests: list = []

    def expect(self, *replies: Reply) -> None:
        self.queue.extend(replies)

    def transport(self):
        return self.httpx.MockTransport(self.handler)

    def handler(self, request):
        self.requests.append(request)
        body = json.loads(request.content) if request.content else {}
        reply = self.queue.popleft()
        model = reply.model or body.get("model", "unknown")
        headers = {
            ("request-id" if self.provider == "anthropic" else "x-request-id"): reply.request_id
        }
        if reply.status != 200:
            headers["retry-after-ms"] = "1"
            return self.httpx.Response(reply.status, headers=headers, json=_error(self.provider))
        if self.provider == "anthropic":
            payload, stream = _anthropic(reply, model), body.get("stream")
        elif request.url.path.endswith("/responses"):
            payload, stream = _responses(reply, model), body.get("stream")
        else:
            payload, stream = _chat(reply, model), body.get("stream")
        if stream:
            include = (
                self.provider == "anthropic"
                or request.url.path.endswith("/responses")
                or ((body.get("stream_options") or {}).get("include_usage"))
            )
            headers["content-type"] = "text/event-stream"
            return self.httpx.Response(
                200, headers=headers, content=payload.stream(include).encode()
            )
        return self.httpx.Response(200, headers=headers, json=payload.body())


def _error(provider: str) -> dict:
    if provider == "anthropic":
        return {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}
    return {"error": {"message": "Try again", "type": "server_error", "code": None}}


def _usage(reply: Reply) -> tuple[int, int, int, int]:
    inp, out, cache_read, *rest = reply.usage
    return inp, out, cache_read, (rest[0] if rest else 0)


class _Chat:
    def __init__(self, reply: Reply, model: str) -> None:
        self.reply, self.model = reply, model

    def _usage(self) -> dict | None:
        if self.reply.usage is None:
            return None
        inp, out, cache, _ = _usage(self.reply)
        return {
            "prompt_tokens": inp + cache,
            "completion_tokens": out,
            "total_tokens": inp + cache + out,
            "prompt_tokens_details": {"cached_tokens": cache},
        }

    def _message(self) -> dict:
        if self.reply.tool_call:
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": self.reply.tool_call["name"],
                            "arguments": json.dumps(self.reply.tool_call["arguments"]),
                        },
                    }
                ],
            }
        return {"role": "assistant", "content": self.reply.text}

    def body(self) -> dict:
        body = {
            "id": "chatcmpl-1",
            "object": "chat.completion",
            "created": 1,
            "model": self.model,
            "choices": [
                {
                    "index": 0,
                    "message": self._message(),
                    "finish_reason": "tool_calls" if self.reply.tool_call else "stop",
                }
            ],
        }
        if (usage := self._usage()) is not None:
            body["usage"] = usage
        return body

    def stream(self, include_usage: bool) -> str:
        base = {
            "id": "chatcmpl-1",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": self.model,
        }
        chunks = [
            {
                **base,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": self.reply.text[:1]},
                        "finish_reason": None,
                    }
                ],
            },
            {
                **base,
                "choices": [
                    {"index": 0, "delta": {"content": self.reply.text[1:]}, "finish_reason": "stop"}
                ],
            },
        ]
        if include_usage and (usage := self._usage()) is not None:
            chunks.append({**base, "choices": [], "usage": usage})
        return "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"


class _Responses:
    def __init__(self, reply: Reply, model: str) -> None:
        self.reply, self.model = reply, model

    def body(self, status: str = "completed") -> dict:
        body = {
            "id": "resp-1",
            "object": "response",
            "created_at": 1,
            "model": self.model,
            "status": status,
            "output": []
            if status != "completed"
            else [
                {
                    "type": "message",
                    "id": "msg-1",
                    "status": "completed",
                    "role": "assistant",
                    "content": [
                        {"type": "output_text", "text": self.reply.text, "annotations": []}
                    ],
                }
            ],
            "parallel_tool_calls": True,
            "tool_choice": "auto",
            "tools": [],
            "usage": None,
        }
        if status == "completed" and self.reply.usage is not None:
            inp, out, cache, _ = _usage(self.reply)
            body["usage"] = {
                "input_tokens": inp + cache,
                "output_tokens": out,
                "total_tokens": inp + cache + out,
                "input_tokens_details": {"cached_tokens": cache},
                "output_tokens_details": {"reasoning_tokens": 0},
            }
        return body

    def stream(self, include_usage: bool) -> str:
        events = [
            (
                "response.created",
                {
                    "type": "response.created",
                    "sequence_number": 0,
                    "response": self.body("in_progress"),
                },
            ),
            (
                "response.output_text.delta",
                {
                    "type": "response.output_text.delta",
                    "sequence_number": 1,
                    "item_id": "msg-1",
                    "output_index": 0,
                    "content_index": 0,
                    "delta": self.reply.text,
                    "logprobs": [],
                },
            ),
            (
                "response.completed",
                {"type": "response.completed", "sequence_number": 2, "response": self.body()},
            ),
        ]
        return "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in events)


class _Messages:
    def __init__(self, reply: Reply, model: str) -> None:
        self.reply, self.model = reply, model

    def _usage(self, output: int | None = None) -> dict:
        inp, out, cache, write = _usage(self.reply)
        return {
            "input_tokens": inp,
            "output_tokens": out if output is None else output,
            "cache_read_input_tokens": cache,
            "cache_creation_input_tokens": write,
        }

    def body(self) -> dict:
        return {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": self.model,
            "content": [{"type": "text", "text": self.reply.text}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": self._usage(),
        }

    def stream(self, include_usage: bool) -> str:
        start = {**self.body(), "content": [], "stop_reason": None, "usage": self._usage(output=1)}
        events = [
            ("message_start", {"type": "message_start", "message": start}),
            (
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
            ),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": self.reply.text},
                },
            ),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            (
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": _usage(self.reply)[1]},
                },
            ),
            ("message_stop", {"type": "message_stop"}),
        ]
        return "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in events)


def _chat(reply, model):
    return _Chat(reply, model)


def _responses(reply, model):
    return _Responses(reply, model)


def _anthropic(reply, model):
    return _Messages(reply, model)


def openai_replay():
    import openai

    return Replay("openai", httpx_for(openai))


def anthropic_replay():
    import anthropic

    return Replay("anthropic", httpx_for(anthropic))


def openai_client(
    replay: Replay,
    *,
    asynchronous: bool = False,
    base_url: str = "https://api.openai.com/v1",
    max_retries: int = 0,
    **kwargs,
):
    import openai

    cls = openai.AsyncOpenAI if asynchronous else openai.OpenAI
    http = replay.httpx.AsyncClient if asynchronous else replay.httpx.Client
    return cls(
        api_key="fixture",
        base_url=base_url,
        max_retries=max_retries,
        http_client=http(transport=replay.transport(), **kwargs),
    )


def anthropic_client(
    replay: Replay,
    *,
    asynchronous: bool = False,
    base_url: str = "https://api.anthropic.com",
    max_retries: int = 0,
    **kwargs,
):
    import anthropic

    cls = anthropic.AsyncAnthropic if asynchronous else anthropic.Anthropic
    http = replay.httpx.AsyncClient if asynchronous else replay.httpx.Client
    return cls(
        api_key="fixture",
        base_url=base_url,
        max_retries=max_retries,
        http_client=http(transport=replay.transport(), **kwargs),
    )
