"""Raw streaming HTTP transport for the LangGraph Agent Server gateway."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from urllib.parse import quote

import httpx
from starlette.requests import Request
from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from zeroth.core.config.settings import LangGraphGatewaySettings
from zeroth.core.langgraph_gateway.headers import (
    prepare_upstream_request_headers,
    strip_hop_by_hop_headers,
)
from zeroth.core.secrets.provider import SecretProvider


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
    ) -> None:
        if settings.upstream_url is None:
            raise ValueError("LangGraph gateway upstream_url is required")
        self._settings = settings
        self._secret_provider = secret_provider
        self._upstream_url = httpx.URL(settings.upstream_url)
        self._open_responses: set[httpx.Response] = set()
        self._closing_responses: dict[httpx.Response, asyncio.Task[None]] = {}
        self._lifecycle = asyncio.Condition()
        self._in_flight_sends = 0
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
        if self._open_responses:
            await asyncio.gather(
                *(self._close_upstream_response(response) for response in self._open_responses)
            )
        await self._client.aclose()

    async def __aenter__(self) -> HTTPGatewayTransport:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()
