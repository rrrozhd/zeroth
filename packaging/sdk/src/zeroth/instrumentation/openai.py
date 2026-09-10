"""Capture direct OpenAI SDK calls as physical charges on the active run.

``instrument_openai(client)`` replaces ``client.chat.completions.create`` and
``client.responses.create`` (sync or async client) with wrappers that route the
same call through the SDK's raw-response path, so the application receives the
identical parsed object or stream while the adapter sees retries, the request
id and the usage. Streams are charged when their final usage chunk arrives
(``stream_options={"include_usage": True}`` for chat completions); a stream
closed, abandoned or broken before that is a charged, unmeasured attempt.
"""

from __future__ import annotations

import inspect
from typing import Any

from zeroth.instrumentation._provider import Capture, active, bind, official
from zeroth.instrumentation.capture import Usage

HOST = "api.openai.com"


def _details(usage: Any, group: str, name: str) -> int:
    return getattr(getattr(usage, group, None), name, None) or 0


def _usage(kind: str, usage: Any) -> Usage | None:
    if usage is None:
        return None
    if kind == "chat":
        cached = _details(usage, "prompt_tokens_details", "cached_tokens")
        return Usage(
            usage.prompt_tokens - cached, usage.completion_tokens, cache_read_tokens=cached,
            reasoning_tokens=_details(usage, "completion_tokens_details", "reasoning_tokens"),
        )
    cached = _details(usage, "input_tokens_details", "cached_tokens")
    return Usage(
        usage.input_tokens - cached, usage.output_tokens, cache_read_tokens=cached,
        reasoning_tokens=_details(usage, "output_tokens_details", "reasoning_tokens"),
    )


def _stream_usage(kind: str, item: Any) -> tuple[Usage | None, str | None]:
    """The final usage carried by a stream item, if this item carries it."""
    if kind == "chat":
        usage = getattr(item, "usage", None)
        return (_usage(kind, usage), getattr(item, "model", None)) if usage else (None, None)
    if getattr(item, "type", None) == "response.completed":
        response = item.response
        return _usage(kind, response.usage), getattr(response, "model", None)
    return None, None


class _CapturedStream:
    """Iterates the SDK stream unchanged and charges once at its end or abort."""

    def __init__(self, stream: Any, capture: Capture, kind: str, model: str,
                 request_id: str | None, attempt: int) -> None:
        self._stream, self._capture, self._kind = stream, capture, kind
        self._model, self._request_id, self._attempt = model, request_id, attempt
        self._usage: Usage | None = None

    def _seen(self, item: Any) -> None:
        usage, model = _stream_usage(self._kind, item)
        if usage is not None:
            self._usage, self._model = usage, model or self._model

    def _finish(self) -> None:
        self._capture.complete(
            model=self._model, usage=self._usage, request_id=self._request_id,
            attempt=self._attempt, stream="complete",
        )

    def _broken(self, error: BaseException) -> None:
        if isinstance(error, GeneratorExit):
            self._capture.abort(model=self._model, attempt=self._attempt)
        else:
            self._capture.failed(error, model=self._model, attempt=self._attempt, stream="aborted")

    def __iter__(self):
        try:
            for item in self._stream:
                self._seen(item)
                yield item
        except BaseException as error:
            self._broken(error)
            raise
        self._finish()

    async def __aiter__(self):
        try:
            async for item in self._stream:
                self._seen(item)
                yield item
        except BaseException as error:
            self._broken(error)
            raise
        self._finish()

    def close(self) -> None:
        self._stream.close()
        self._capture.abort(model=self._model, attempt=self._attempt)

    async def aclose(self) -> None:
        await self._stream.close()
        self._capture.abort(model=self._model, attempt=self._attempt)

    def __enter__(self):
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


def _request_id(raw: Any) -> str | None:
    return getattr(raw, "request_id", None) or raw.headers.get("x-request-id")


def _captured(kind: str, raw_create: Any, provider: str, priced: bool, asynchronous: bool):
    def factory(original):
        if asynchronous:
            async def create(*args: Any, **kwargs: Any) -> Any:
                capture = active(kind, provider, priced)
                if capture is None:
                    return await original(*args, **kwargs)
                model = str(kwargs.get("model", "unknown"))
                try:
                    raw = await raw_create(*args, **kwargs)
                    parsed = raw.parse()
                    if inspect.isawaitable(parsed):
                        parsed = await parsed
                except BaseException as error:
                    capture.failed(error, model=model)
                    raise
                return _settle(capture, kind, raw, parsed, model, bool(kwargs.get("stream")))

            return create

        def create(*args: Any, **kwargs: Any) -> Any:
            capture = active(kind, provider, priced)
            if capture is None:
                return original(*args, **kwargs)
            model = str(kwargs.get("model", "unknown"))
            try:
                raw = raw_create(*args, **kwargs)
                parsed = raw.parse()
            except BaseException as error:
                capture.failed(error, model=model)
                raise
            return _settle(capture, kind, raw, parsed, model, bool(kwargs.get("stream")))

        return create

    return factory


def _settle(capture: Capture, kind: str, raw: Any, parsed: Any, model: str, streaming: bool) -> Any:
    retries = getattr(raw, "retries_taken", 0) or 0
    capture.retried(retries, model)
    request_id = _request_id(raw)
    if streaming:
        return _CapturedStream(parsed, capture, kind, model, request_id, retries + 1)
    capture.complete(
        model=getattr(parsed, "model", None) or model, usage=_usage(kind, parsed.usage),
        request_id=request_id, attempt=retries + 1,
    )
    return parsed


def instrument_openai(client: Any, *, declared_provider: str | None = None) -> Any:
    """Capture chat completions and responses on an ``OpenAI``/``AsyncOpenAI`` client.

    A client whose ``base_url`` is not ``https://api.openai.com`` is an
    OpenAI-compatible endpoint: its calls are recorded with provider ``unknown``
    and stay unmeasured unless ``declared_provider`` names who actually bills them.
    """
    import openai

    provider, priced = official(client, HOST, declared_provider)
    asynchronous = isinstance(client, openai.AsyncOpenAI)
    for kind, resource in (("chat", client.chat.completions), ("responses", client.responses)):
        raw_create = resource.with_raw_response.create  # bound before create is replaced
        bind(resource, "create", _captured(kind, raw_create, provider, priced, asynchronous))
    return client
