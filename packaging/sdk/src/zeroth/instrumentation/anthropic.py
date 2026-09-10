"""Capture direct Anthropic SDK calls as physical charges on the active run.

``instrument_anthropic(client)`` replaces ``client.messages.create`` and
``client.messages.stream`` (sync or async client). ``create`` goes through the
SDK's raw-response path for retries, request id and usage; ``create(stream=True)``
charges at ``message_stop`` from ``message_start``/``message_delta`` usage;
the ``stream()`` helper charges when its context exits with a finished message
and records an unmeasured attempt otherwise.
"""

from __future__ import annotations

import inspect
from typing import Any

from zeroth.instrumentation._provider import Capture, active, bind, official
from zeroth.instrumentation.capture import Usage

HOST = "api.anthropic.com"


def _usage(usage: Any) -> Usage | None:
    if usage is None:
        return None
    return Usage(
        usage.input_tokens or 0, usage.output_tokens or 0,
        cache_read_tokens=getattr(usage, "cache_read_input_tokens", None) or 0,
        cache_write_tokens=getattr(usage, "cache_creation_input_tokens", None) or 0,
    )


class _CapturedEvents:
    """Raw ``stream=True`` events: usage assembled from message_start and message_delta."""

    def __init__(self, stream: Any, capture: Capture, model: str, request_id: str | None,
                 attempt: int) -> None:
        self._stream, self._capture, self._model = stream, capture, model
        self._request_id, self._attempt = request_id, attempt
        self._start: Usage | None = None
        self._output: int | None = None
        self._complete = False

    def _seen(self, event: Any) -> None:
        kind = getattr(event, "type", None)
        if kind == "message_start":
            self._start = _usage(event.message.usage)
            self._model = event.message.model or self._model
        elif kind == "message_delta" and getattr(event, "usage", None) is not None:
            self._output = event.usage.output_tokens
        elif kind == "message_stop":
            self._complete = True

    def _usage_now(self) -> Usage | None:
        if not self._complete or self._start is None or self._output is None:
            return None
        return Usage(
            self._start.input_tokens, self._output,
            cache_read_tokens=self._start.cache_read_tokens,
            cache_write_tokens=self._start.cache_write_tokens,
        )

    def _finish(self) -> None:
        self._capture.complete(
            model=self._model, usage=self._usage_now(), request_id=self._request_id,
            attempt=self._attempt, stream="complete" if self._complete else "aborted",
        )

    def _broken(self, error: BaseException) -> None:
        if isinstance(error, GeneratorExit):
            self._capture.abort(model=self._model, attempt=self._attempt)
        else:
            self._capture.failed(error, model=self._model, attempt=self._attempt, stream="aborted")

    def __iter__(self):
        try:
            for event in self._stream:
                self._seen(event)
                yield event
        except BaseException as error:
            self._broken(error)
            raise
        self._finish()

    async def __aiter__(self):
        try:
            async for event in self._stream:
                self._seen(event)
                yield event
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


class _CapturedManager:
    """Wraps ``messages.stream()``: the application uses the SDK's MessageStream unchanged."""

    def __init__(self, manager: Any, capture: Capture, model: str) -> None:
        self._manager, self._capture, self._model = manager, capture, model
        self._stream: Any = None

    def _settle(self, error: BaseException | None) -> None:
        stream = self._stream
        snapshot = getattr(stream, "current_message_snapshot", None) if stream else None
        request_id = None
        response = getattr(stream, "response", None)
        if response is not None:
            request_id = response.headers.get("request-id")
        if snapshot is not None and snapshot.stop_reason is not None:
            self._capture.complete(
                model=snapshot.model or self._model, usage=_usage(snapshot.usage),
                request_id=request_id, attempt=1, stream="complete",
            )
        elif error is not None:
            self._capture.failed(error, model=self._model, stream="aborted")
        else:
            self._capture.abort(model=self._model, attempt=1)

    def __enter__(self) -> Any:
        try:
            self._stream = self._manager.__enter__()
        except BaseException as error:
            self._capture.failed(error, model=self._model, stream="aborted")
            raise
        return self._stream

    def __exit__(self, exc_type: Any, error: Any, tb: Any) -> Any:
        result = self._manager.__exit__(exc_type, error, tb)
        self._settle(error)
        return result

    async def __aenter__(self) -> Any:
        try:
            self._stream = await self._manager.__aenter__()
        except BaseException as error:
            self._capture.failed(error, model=self._model, stream="aborted")
            raise
        return self._stream

    async def __aexit__(self, exc_type: Any, error: Any, tb: Any) -> Any:
        result = await self._manager.__aexit__(exc_type, error, tb)
        self._settle(error)
        return result


def _settle_create(capture: Capture, raw: Any, parsed: Any, model: str, streaming: bool) -> Any:
    retries = getattr(raw, "retries_taken", 0) or 0
    capture.retried(retries, model)
    request_id = raw.headers.get("request-id")
    if streaming:
        return _CapturedEvents(parsed, capture, model, request_id, retries + 1)
    capture.complete(
        model=getattr(parsed, "model", None) or model, usage=_usage(parsed.usage),
        request_id=request_id, attempt=retries + 1,
    )
    return parsed


def _captured_create(raw_create: Any, provider: str, priced: bool, asynchronous: bool):
    def factory(original):
        if asynchronous:
            async def create(*args: Any, **kwargs: Any) -> Any:
                capture = active("messages", provider, priced)
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
                return _settle_create(capture, raw, parsed, model, bool(kwargs.get("stream")))

            return create

        def create(*args: Any, **kwargs: Any) -> Any:
            capture = active("messages", provider, priced)
            if capture is None:
                return original(*args, **kwargs)
            model = str(kwargs.get("model", "unknown"))
            try:
                raw = raw_create(*args, **kwargs)
                parsed = raw.parse()
            except BaseException as error:
                capture.failed(error, model=model)
                raise
            return _settle_create(capture, raw, parsed, model, bool(kwargs.get("stream")))

        return create

    return factory


def _captured_raw(raw_create: Any, provider: str, priced: bool, asynchronous: bool):
    """Capture ``messages.with_raw_response.create``; raw streams are left to the observer."""

    def factory(original):
        if asynchronous:
            async def create(*args: Any, **kwargs: Any) -> Any:
                capture = active("messages", provider, priced)
                if capture is None:
                    return await original(*args, **kwargs)
                model = str(kwargs.get("model", "unknown"))
                try:
                    raw = await raw_create(*args, **kwargs)
                    parsed = None if kwargs.get("stream") else raw.parse()
                    if inspect.isawaitable(parsed):
                        parsed = await parsed
                except BaseException as error:
                    capture.failed(error, model=model)
                    raise
                if parsed is not None:
                    _settle_create(capture, raw, parsed, model, False)
                return raw

            return create

        def create(*args: Any, **kwargs: Any) -> Any:
            capture = active("messages", provider, priced)
            if capture is None:
                return original(*args, **kwargs)
            model = str(kwargs.get("model", "unknown"))
            try:
                raw = raw_create(*args, **kwargs)
                parsed = None if kwargs.get("stream") else raw.parse()
            except BaseException as error:
                capture.failed(error, model=model)
                raise
            if parsed is not None:
                _settle_create(capture, raw, parsed, model, False)
            return raw

        return create

    return factory


def _captured_stream(provider: str, priced: bool):
    def factory(original):
        def stream(*args: Any, **kwargs: Any) -> Any:
            capture = active("messages.stream", provider, priced)
            manager = original(*args, **kwargs)
            if capture is None:
                return manager
            return _CapturedManager(manager, capture, str(kwargs.get("model", "unknown")))

        return stream

    return factory


def instrument_anthropic(client: Any, *, declared_provider: str | None = None) -> Any:
    """Capture messages on an ``Anthropic``/``AsyncAnthropic`` client.

    A client whose ``base_url`` is not ``https://api.anthropic.com`` is a proxied
    endpoint: provider ``unknown`` and unmeasured unless ``declared_provider`` names
    who actually bills it.
    """
    import anthropic

    provider, priced = official(client, HOST, declared_provider)
    asynchronous = isinstance(client, anthropic.AsyncAnthropic)
    resource = client.messages
    raw_resource = resource.with_raw_response  # cached by the SDK; built from the original
    raw_create = raw_resource.create  # bound before either create is replaced
    bind(resource, "create", _captured_create(raw_create, provider, priced, asynchronous))
    bind(raw_resource, "create", _captured_raw(raw_create, provider, priced, asynchronous))
    bind(resource, "stream", _captured_stream(provider, priced))
    return client
