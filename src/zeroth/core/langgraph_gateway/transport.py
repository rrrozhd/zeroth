"""Raw streaming HTTP transport for the LangGraph Agent Server gateway."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
from starlette.requests import Request
from starlette.responses import StreamingResponse

from zeroth.core.config.settings import LangGraphGatewaySettings
from zeroth.core.langgraph_gateway.headers import (
    prepare_upstream_request_headers,
    strip_hop_by_hop_headers,
)
from zeroth.core.secrets.provider import SecretProvider


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

    @property
    def client(self) -> httpx.AsyncClient:
        """Expose the owned client for lifecycle diagnostics and conformance tests."""
        return self._client

    def _request_url(self, request: Request) -> httpx.URL:
        incoming_path = request.scope.get("raw_path")
        if incoming_path is None:
            incoming_path = request.url.path.encode("ascii")
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
        downstream_response = StreamingResponse(
            self._response_body(upstream_response),
            status_code=upstream_response.status_code,
        )
        downstream_response.raw_headers = strip_hop_by_hop_headers(upstream_response.headers.raw)
        return downstream_response

    async def _response_body(self, response: httpx.Response) -> AsyncIterator[bytes]:
        try:
            async for chunk in response.aiter_raw():
                yield chunk
        finally:
            await response.aclose()

    async def aclose(self) -> None:
        """Close the gateway's owned connection pool."""
        await self._client.aclose()

    async def __aenter__(self) -> HTTPGatewayTransport:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()
