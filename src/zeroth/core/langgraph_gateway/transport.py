"""Raw streaming HTTP transport for the LangGraph Agent Server gateway."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx
import websockets
from starlette.requests import Request
from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed

from zeroth.core.config.settings import LangGraphGatewaySettings
from zeroth.core.langgraph_gateway.headers import (
    prepare_upstream_request_headers,
    strip_hop_by_hop_headers,
)
from zeroth.core.secrets.provider import SecretProvider

WebSocketMessage = str | bytes

_WEBSOCKET_HANDSHAKE_HEADERS = frozenset(
    {
        b"host",
        b"sec-websocket-accept",
        b"sec-websocket-extensions",
        b"sec-websocket-key",
        b"sec-websocket-protocol",
        b"sec-websocket-version",
    }
)
_WEBSOCKET_SUBPROTOCOL_TOKEN = re.compile(rb"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
_MAX_WEBSOCKET_SUBPROTOCOLS = 16
_MAX_WEBSOCKET_SUBPROTOCOL_BYTES = 128
_MAX_WEBSOCKET_SUBPROTOCOL_HEADER_BYTES = 1024
_MAX_CORRELATION_ID_BYTES = 128


@dataclass(frozen=True, slots=True)
class WebSocketClientError(Exception):
    """Safe client-owned WebSocket failure that the route closes exactly once."""

    code: int
    reason: str
    preserve_downstream_close = True


@dataclass(frozen=True, slots=True)
class _WebSocketFrame:
    value: WebSocketMessage


@dataclass(frozen=True, slots=True)
class _WebSocketClose:
    code: int
    reason: str


class _WebSocketBridgeFinishedError(Exception):
    """Terminate one structured duplex bridge after an orderly close."""


class _WebSocketByteBudget:
    """One shared byte budget across both directional application queues."""

    def __init__(self, limit: int, high_water_callback: Callable[[int], None]) -> None:
        self._limit = limit
        self._used = 0
        self._condition = asyncio.Condition()
        self._high_water_callback = high_water_callback

    async def reserve(self, size: int) -> None:
        async with self._condition:
            await self._condition.wait_for(lambda: self._used + size <= self._limit)
            self._used += size
            self._high_water_callback(self._used)

    async def release(self, size: int) -> None:
        async with self._condition:
            self._used -= size
            self._condition.notify_all()


class _UpstreamStreamingResponse(StreamingResponse):
    """Streaming response whose ASGI lifecycle owns upstream cleanup."""

    def __init__(
        self,
        content: AsyncIterator[bytes],
        *,
        status_code: int,
        close_upstream: Callable[[], Awaitable[None]],
    ) -> None:
        super().__init__(content, status_code=status_code)
        self._close_upstream = close_upstream

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self._close_upstream()


class HTTPGatewayTransport:
    """Forward HTTP requests through one long-lived streaming client."""

    def __init__(
        self,
        settings: LangGraphGatewaySettings,
        secret_provider: SecretProvider,
        *,
        http_transport: httpx.AsyncBaseTransport | None = None,
        websocket_queue_size: int = 16,
        websocket_max_message_bytes: int | None = None,
        websocket_max_queued_bytes: int | None = None,
    ) -> None:
        if settings.upstream_url is None:
            raise ValueError("LangGraph gateway upstream_url is required")
        self._settings = settings
        self._secret_provider = secret_provider
        self._upstream_url = httpx.URL(settings.upstream_url)
        if type(websocket_queue_size) is not int or websocket_queue_size <= 0:
            raise ValueError("websocket_queue_size must be a positive integer")
        self._websocket_queue_size = websocket_queue_size
        max_message_bytes = (
            settings.max_governed_body_bytes
            if websocket_max_message_bytes is None
            else websocket_max_message_bytes
        )
        if type(max_message_bytes) is not int or max_message_bytes <= 0:
            raise ValueError("websocket_max_message_bytes must be a finite positive integer")
        max_queued_bytes = (
            max_message_bytes * websocket_queue_size
            if websocket_max_queued_bytes is None
            else websocket_max_queued_bytes
        )
        if type(max_queued_bytes) is not int or max_queued_bytes <= 0:
            raise ValueError("websocket_max_queued_bytes must be a finite positive integer")
        if max_queued_bytes < max_message_bytes:
            raise ValueError(
                "websocket_max_queued_bytes must be at least websocket_max_message_bytes"
            )
        self._websocket_max_message_bytes = max_message_bytes
        self._websocket_max_queued_bytes = max_queued_bytes
        self.websocket_max_buffered_frames = 0
        self.websocket_max_buffered_bytes = 0
        self._open_responses: set[httpx.Response] = set()
        self._closing_responses: dict[httpx.Response, asyncio.Task[None]] = {}
        self._lifecycle = asyncio.Condition()
        self._in_flight_sends = 0
        self._active_websocket_tasks: set[asyncio.Task[Any]] = set()
        self._active_websockets: set[ClientConnection] = set()
        self._closing = False
        self._shutdown_task: asyncio.Task[None] | None = None
        self._client = httpx.AsyncClient(
            http2=True,
            follow_redirects=False,
            timeout=httpx.Timeout(
                connect=settings.connect_timeout_seconds,
                read=settings.read_timeout_seconds,
                write=settings.write_timeout_seconds,
                pool=settings.pool_timeout_seconds,
            ),
            limits=httpx.Limits(
                max_connections=100,
                max_keepalive_connections=20,
                keepalive_expiry=5.0,
            ),
            transport=http_transport,
        )
        self._client.headers.clear()

    @property
    def client(self) -> httpx.AsyncClient:
        """Expose the owned client for lifecycle diagnostics and conformance tests."""
        return self._client

    @property
    def websocket_active_count(self) -> int:
        """Return active or dialing WebSocket bridge count for lifecycle diagnostics."""
        return len(self._active_websocket_tasks)

    def _request_url(self, request: Request) -> httpx.URL:
        incoming_path = request.scope.get("raw_path")
        if incoming_path is None:
            incoming_path = quote(request.scope["path"], safe="/").encode("ascii")
        base_path = self._upstream_url.raw_path.rstrip(b"/")
        raw_path = base_path + b"/" + incoming_path.lstrip(b"/")
        query = request.scope.get("query_string", b"")
        if query:
            raw_path += b"?" + query
        return self._upstream_url.copy_with(raw_path=raw_path)

    def _websocket_url(self, websocket: Any) -> str:
        incoming_path = websocket.scope.get("raw_path")
        if incoming_path is None:
            incoming_path = quote(websocket.scope["path"], safe="/").encode("ascii")
        base_path = self._upstream_url.raw_path.rstrip(b"/")
        raw_path = base_path + b"/" + incoming_path.lstrip(b"/")
        query = websocket.scope.get("query_string", b"")
        if query:
            raw_path += b"?" + query
        scheme = "wss" if self._upstream_url.scheme == "https" else "ws"
        return str(self._upstream_url.copy_with(scheme=scheme, raw_path=raw_path))

    async def forward_websocket(
        self,
        websocket: Any,
        *,
        tenant_id: str | None = None,
        transform_client_message: Callable[[WebSocketMessage], Awaitable[WebSocketMessage]]
        | None = None,
        correlation_id: str | None = None,
    ) -> None:
        """Forward one WebSocket with bounded, ordered queues in both directions."""
        task = await self._begin_websocket()
        upstream: ClientConnection | None = None
        try:
            transform = transform_client_message or _identity_websocket_message
            raw_headers = list(websocket.headers.raw)
            subprotocols = _validated_subprotocols(raw_headers)
            encoded_correlation_id = _validated_correlation_id(correlation_id)
            prepared = await prepare_upstream_request_headers(
                raw_headers,
                upstream_url=self._upstream_url,
                settings=self._settings,
                secret_provider=self._secret_provider,
                tenant_id=tenant_id,
            )
            additional_headers = [
                (name.decode("ascii"), value.decode("latin-1"))
                for name, value in prepared
                if name.lower() not in _WEBSOCKET_HANDSHAKE_HEADERS
            ]
            async with websockets.connect(
                self._websocket_url(websocket),
                subprotocols=subprotocols or None,
                additional_headers=additional_headers,
                open_timeout=self._settings.connect_timeout_seconds,
                close_timeout=self._settings.connect_timeout_seconds,
                ping_interval=self._settings.heartbeat_interval_seconds,
                ping_timeout=self._settings.heartbeat_interval_seconds,
                max_size=self._websocket_max_message_bytes,
                max_queue=1,
                write_limit=min(32_768, self._websocket_max_message_bytes),
                proxy=None,
            ) as upstream_connection:
                upstream = upstream_connection
                await self._set_active_upstream(upstream)
                response_headers = []
                if encoded_correlation_id is not None:
                    response_headers.append((b"x-correlation-id", encoded_correlation_id))
                await websocket.accept(
                    subprotocol=upstream.subprotocol,
                    headers=response_headers,
                )
                try:
                    await self._bridge_websocket(websocket, upstream, transform)
                except asyncio.CancelledError:
                    await _close_websocket_pair(
                        websocket,
                        upstream,
                        code=1001,
                        reason="gateway stream cancelled",
                    )
                    raise
                except BaseException as exc:
                    if _preserves_downstream_close(exc):
                        with suppress(Exception):
                            await upstream.close(1008, "gateway request rejected")
                    else:
                        await _close_websocket_pair(
                            websocket,
                            upstream,
                            code=1011,
                            reason="gateway stream failed",
                        )
                    raise
        finally:
            await self._finish_websocket(task, upstream)

    async def _begin_websocket(self) -> asyncio.Task[Any]:
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("WebSocket gateway transport has no owning task")
        async with self._lifecycle:
            if self._closing:
                raise RuntimeError("WebSocket gateway transport is closed")
            self._active_websocket_tasks.add(task)
        return task

    async def _set_active_upstream(self, upstream: ClientConnection) -> None:
        async with self._lifecycle:
            self._active_websockets.add(upstream)

    async def _finish_websocket(
        self,
        task: asyncio.Task[Any],
        upstream: ClientConnection | None,
    ) -> None:
        async with self._lifecycle:
            self._active_websocket_tasks.discard(task)
            if upstream is not None:
                self._active_websockets.discard(upstream)
            self._lifecycle.notify_all()

    async def _bridge_websocket(
        self,
        websocket: Any,
        upstream: ClientConnection,
        transform: Callable[[WebSocketMessage], Awaitable[WebSocketMessage]],
    ) -> None:
        queue_item = tuple[_WebSocketFrame | _WebSocketClose, int]
        client_to_upstream: asyncio.Queue[queue_item] = asyncio.Queue(
            maxsize=self._websocket_queue_size
        )
        upstream_to_client: asyncio.Queue[queue_item] = asyncio.Queue(
            maxsize=self._websocket_queue_size
        )
        byte_budget = _WebSocketByteBudget(
            self._websocket_max_queued_bytes,
            self._record_websocket_buffered_bytes,
        )

        async def enqueue(
            queue: asyncio.Queue[queue_item],
            item: _WebSocketFrame | _WebSocketClose,
        ) -> None:
            size = _websocket_item_size(item)
            if size > self._websocket_max_message_bytes:
                raise WebSocketClientError(4400, "zeroth.websocket_message_too_large")
            await byte_budget.reserve(size)
            try:
                await queue.put((item, size))
            except BaseException:
                await byte_budget.release(size)
                raise
            self.websocket_max_buffered_frames = max(
                self.websocket_max_buffered_frames, queue.qsize()
            )

        async def read_client() -> None:
            while True:
                event = await websocket.receive()
                event_type = event.get("type")
                if event_type == "websocket.disconnect":
                    await enqueue(
                        client_to_upstream,
                        _WebSocketClose(
                            int(event.get("code") or 1000),
                            str(event.get("reason") or ""),
                        ),
                    )
                    return
                if event_type != "websocket.receive":
                    continue
                value = event.get("text")
                if value is None:
                    value = event.get("bytes")
                if not isinstance(value, (str, bytes)):
                    continue
                if _websocket_message_size(value) > self._websocket_max_message_bytes:
                    raise WebSocketClientError(4400, "zeroth.websocket_message_too_large")
                transformed = await transform(value)
                await enqueue(client_to_upstream, _WebSocketFrame(transformed))

        async def write_upstream() -> None:
            while True:
                item, size = await client_to_upstream.get()
                try:
                    if isinstance(item, _WebSocketClose):
                        if _sendable_close_code(item.code):
                            await upstream.close(item.code, item.reason)
                        else:
                            upstream.transport.abort()
                        raise _WebSocketBridgeFinishedError
                    await upstream.send(item.value)
                finally:
                    await byte_budget.release(size)

        async def read_upstream() -> None:
            try:
                while True:
                    await enqueue(upstream_to_client, _WebSocketFrame(await upstream.recv()))
            except ConnectionClosed as exc:
                del exc
                code = upstream.close_code or 1006
                reason = upstream.close_reason or ""
                sent_close = getattr(upstream.protocol, "close_sent", None)
                if code == 1006 and getattr(sent_close, "code", None) == 1009:
                    close = _WebSocketClose(1009, "websocket message too large")
                elif code == 1006:
                    close = _WebSocketClose(1011, "upstream disconnected abnormally")
                else:
                    close = _WebSocketClose(code, reason)
                await enqueue(upstream_to_client, close)

        async def write_client() -> None:
            while True:
                item, size = await upstream_to_client.get()
                try:
                    if isinstance(item, _WebSocketClose):
                        await websocket.close(code=item.code, reason=item.reason)
                        raise _WebSocketBridgeFinishedError
                    if isinstance(item.value, str):
                        await websocket.send_text(item.value)
                    else:
                        await websocket.send_bytes(item.value)
                finally:
                    await byte_budget.release(size)

        try:
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(read_client())
                tasks.create_task(write_upstream())
                tasks.create_task(read_upstream())
                tasks.create_task(write_client())
        except* _WebSocketBridgeFinishedError:
            pass

    def _record_websocket_buffered_bytes(self, used: int) -> None:
        self.websocket_max_buffered_bytes = max(
            self.websocket_max_buffered_bytes,
            used,
        )

    async def forward(
        self,
        request: Request,
        *,
        tenant_id: str | None = None,
    ) -> StreamingResponse:
        """Forward a request without buffering either request or response content."""
        async with self._lifecycle:
            if self._closing:
                raise RuntimeError("HTTP gateway transport is closed")
            self._in_flight_sends += 1

        try:
            headers = await prepare_upstream_request_headers(
                request.headers.raw,
                upstream_url=self._upstream_url,
                settings=self._settings,
                secret_provider=self._secret_provider,
                tenant_id=tenant_id,
            )
            upstream_request = self._client.build_request(
                request.method,
                self._request_url(request),
                headers=headers,
                content=request.stream(),
            )
            upstream_response = await self._client.send(upstream_request, stream=True)
        except BaseException:
            await self._finish_send_shielded()
            raise
        self._open_responses.add(upstream_response)
        try:
            closing = await self._finish_send_shielded()
        except BaseException:
            await self._close_upstream_response(upstream_response)
            raise
        if closing:
            await self._close_upstream_response(upstream_response)
            raise RuntimeError("HTTP gateway transport is closed")
        downstream_response = _UpstreamStreamingResponse(
            self._response_body(upstream_response),
            status_code=upstream_response.status_code,
            close_upstream=lambda: self._close_upstream_response(upstream_response),
        )
        downstream_response.raw_headers = strip_hop_by_hop_headers(upstream_response.headers.raw)
        return downstream_response

    async def _finish_send(self) -> bool:
        async with self._lifecycle:
            self._in_flight_sends -= 1
            self._lifecycle.notify_all()
            return self._closing

    async def _finish_send_shielded(self) -> bool:
        finish_task = asyncio.create_task(self._finish_send())
        try:
            return await asyncio.shield(finish_task)
        except asyncio.CancelledError:
            await finish_task
            raise

    async def _response_body(self, response: httpx.Response) -> AsyncIterator[bytes]:
        try:
            async for chunk in response.aiter_raw():
                yield chunk
        finally:
            await self._close_upstream_response(response)

    async def _close_upstream_response(self, response: httpx.Response) -> None:
        close_task = self._closing_responses.get(response)
        if close_task is None:
            if response.is_closed:
                self._open_responses.discard(response)
                return
            close_task = asyncio.create_task(response.aclose())
            self._closing_responses[response] = close_task

            def finished(task: asyncio.Task[None]) -> None:
                self._closing_responses.pop(response, None)
                self._open_responses.discard(response)
                if not task.cancelled():
                    task.exception()

            close_task.add_done_callback(finished)

        current_task = asyncio.current_task()
        if current_task is not None and current_task.cancelling():
            return
        try:
            await asyncio.shield(close_task)
        except asyncio.CancelledError:
            raise

    async def aclose(self) -> None:
        """Close the gateway's owned connection pool."""
        async with self._lifecycle:
            if self._shutdown_task is None:
                self._closing = True
                self._shutdown_task = asyncio.create_task(self._shutdown())
            shutdown_task = self._shutdown_task
        await asyncio.shield(shutdown_task)

    async def _shutdown(self) -> None:
        async with self._lifecycle:
            while self._in_flight_sends:
                await self._lifecycle.wait()
            websocket_tasks = tuple(self._active_websocket_tasks)
        for task in websocket_tasks:
            task.cancel()
        if websocket_tasks:
            await asyncio.gather(*websocket_tasks, return_exceptions=True)
        if self._open_responses:
            await asyncio.gather(
                *(self._close_upstream_response(response) for response in self._open_responses)
            )
        await self._client.aclose()

    async def __aenter__(self) -> HTTPGatewayTransport:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()


async def _identity_websocket_message(message: WebSocketMessage) -> WebSocketMessage:
    return message


def _validated_subprotocols(headers: list[tuple[bytes, bytes]]) -> list[str]:
    protocols: list[str] = []
    total_bytes = 0
    for name, value in headers:
        if name.lower() != b"sec-websocket-protocol":
            continue
        total_bytes += len(value)
        candidates = [part.strip() for part in value.split(b",")]
        if any(not protocol for protocol in candidates):
            raise WebSocketClientError(4400, "zeroth.invalid_websocket_handshake")
        for protocol in candidates:
            if (
                len(protocol) > _MAX_WEBSOCKET_SUBPROTOCOL_BYTES
                or _WEBSOCKET_SUBPROTOCOL_TOKEN.fullmatch(protocol) is None
            ):
                raise WebSocketClientError(4400, "zeroth.invalid_websocket_handshake")
            protocols.append(protocol.decode("ascii"))
    if (
        len(protocols) > _MAX_WEBSOCKET_SUBPROTOCOLS
        or len(set(protocols)) != len(protocols)
        or total_bytes > _MAX_WEBSOCKET_SUBPROTOCOL_HEADER_BYTES
    ):
        raise WebSocketClientError(4400, "zeroth.invalid_websocket_handshake")
    return protocols


def _validated_correlation_id(correlation_id: str | None) -> bytes | None:
    if correlation_id is None:
        return None
    try:
        encoded = correlation_id.encode("ascii")
    except (AttributeError, UnicodeEncodeError):
        raise WebSocketClientError(4400, "zeroth.invalid_websocket_handshake") from None
    if (
        not encoded
        or len(encoded) > _MAX_CORRELATION_ID_BYTES
        or any(byte < 0x21 or byte > 0x7E for byte in encoded)
    ):
        raise WebSocketClientError(4400, "zeroth.invalid_websocket_handshake")
    return encoded


def _sendable_close_code(code: int) -> bool:
    return 1000 <= code < 5000 and code not in {1004, 1005, 1006, 1015}


async def _close_websocket_pair(
    websocket: Any,
    upstream: ClientConnection,
    *,
    code: int,
    reason: str,
) -> None:
    """Best-effort structured cleanup without replacing the triggering failure."""
    with suppress(Exception):
        await websocket.close(code=code, reason=reason)
    with suppress(Exception):
        await upstream.close(code=code, reason=reason)


def _websocket_message_size(message: WebSocketMessage) -> int:
    return len(message.encode("utf-8")) if isinstance(message, str) else len(message)


def _websocket_item_size(item: _WebSocketFrame | _WebSocketClose) -> int:
    return _websocket_message_size(item.value) if isinstance(item, _WebSocketFrame) else 0


def _preserves_downstream_close(exc: BaseException) -> bool:
    if getattr(exc, "preserve_downstream_close", False) is True:
        return True
    if isinstance(exc, BaseExceptionGroup):
        return any(_preserves_downstream_close(nested) for nested in exc.exceptions)
    return False
