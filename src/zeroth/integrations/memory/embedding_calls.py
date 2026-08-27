"""Per-operation context for reserving billable workflow embedding calls."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Any, Literal, Protocol

from zeroth.contracts.governed.models.common import JSONValue
from zeroth.integrations.memory.governed.connector import MemoryConnector
from zeroth.integrations.memory.governed.models import MemoryEntry, MemoryScope
from zeroth.platform.secrets import SecretResolutionError, resolve_secret_async
from zeroth.platform.secrets.provider import SecretProvider

EmbeddingOperation = Literal["search", "write"]


@dataclass(frozen=True, slots=True)
class EmbeddingCallIdentity:
    tenant_id: str
    run_id: str
    node_id: str
    campaign_id: str
    operation: EmbeddingOperation


@dataclass(frozen=True, slots=True)
class EmbeddingCallBound:
    model: str
    input_count: int
    input_utf8_bytes: int


@dataclass(frozen=True, slots=True)
class EmbeddingCallResult:
    provider_request_id: str | None
    usage: Mapping[str, Any] | None


class EmbeddingCallHooks(Protocol):
    async def reserve(
        self, identity: EmbeddingCallIdentity, bound: EmbeddingCallBound
    ) -> str: ...

    async def succeed(self, reservation_id: str, result: EmbeddingCallResult) -> None: ...

    async def ambiguous(self, reservation_id: str, reason: str) -> None: ...


class EmbeddingControlPlaneError(RuntimeError):
    """The strict campaign could not establish its embedding reservation."""


@dataclass(frozen=True, slots=True)
class _EmbeddingCallContext:
    identity: EmbeddingCallIdentity
    hooks: EmbeddingCallHooks | None
    strict: bool


_CURRENT_EMBEDDING_CALL: ContextVar[_EmbeddingCallContext | None] = ContextVar(
    "workflow_embedding_call",
    default=None,
)


def _usage(response: Any) -> Mapping[str, Any] | None:
    usage = (
        response.get("usage")
        if isinstance(response, Mapping)
        else getattr(response, "usage", None)
    )
    if isinstance(usage, Mapping):
        return dict(usage)
    model_dump = getattr(usage, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(mode="json")
        return dict(dumped) if isinstance(dumped, Mapping) else None
    return None


def _request_id(response: Any) -> str | None:
    if isinstance(response, Mapping):
        value = response.get("id") or response.get("request_id")
    else:
        value = getattr(response, "id", None) or getattr(response, "request_id", None)
    if value:
        return str(value)
    hidden = getattr(response, "_hidden_params", None)
    if not isinstance(hidden, Mapping):
        return None
    headers = hidden.get("additional_headers")
    if not isinstance(headers, Mapping):
        return None
    for name in (
        "llm_provider-x-request-id",
        "x-request-id",
        "request-id",
        "openai-request-id",
    ):
        if request_id := headers.get(name):
            return str(request_id)
    return None


def _embedding_provider(model: str) -> str:
    if "/" in model:
        return model.split("/", 1)[0].lower()
    lowered = model.lower()
    if lowered.startswith("text-embedding-"):
        return "openai"
    return lowered


async def resolve_embedding_provider_kwargs(
    *,
    model: str,
    secret_provider: SecretProvider | None,
    tenant_id: str | None,
    allow_env_fallback: bool,
) -> dict[str, str]:
    """Resolve an embedding credential at call time without global env state."""
    if secret_provider is None:
        if allow_env_fallback:
            return {}
        raise SecretResolutionError("embedding secret provider is unavailable")
    logical_name = f"llm.{_embedding_provider(model)}"
    key = await resolve_secret_async(secret_provider, logical_name, tenant_id=tenant_id)
    if key is None:
        if allow_env_fallback:
            return {}
        raise SecretResolutionError(
            f"no secret for {logical_name!r} (tenant={tenant_id!r}) and env fallback is disabled"
        )
    return {"api_key": key}


def _ambiguous_reason(error: BaseException) -> str:
    if isinstance(error, asyncio.CancelledError):
        return "cancelled"
    if isinstance(error, TimeoutError):
        return "timeout"
    return "unknown"


async def invoke_embedding_call[ResponseT](
    *,
    model: str,
    inputs: Sequence[str],
    provider_call: Callable[[], Awaitable[ResponseT]],
) -> ResponseT:
    """Reserve, invoke, and settle one provider embedding call when governed."""
    context = _CURRENT_EMBEDDING_CALL.get()
    if context is None:
        return await provider_call()
    hooks = context.hooks
    if hooks is None:
        if context.strict:
            raise EmbeddingControlPlaneError(
                "strict campaign embedding call has no reservation control plane"
            )
        return await provider_call()

    bound = EmbeddingCallBound(
        model=model,
        input_count=len(inputs),
        input_utf8_bytes=sum(len(value.encode("utf-8")) for value in inputs),
    )
    try:
        reservation_id = await hooks.reserve(context.identity, bound)
    except Exception as exc:
        if context.strict:
            raise EmbeddingControlPlaneError(
                "strict campaign embedding reservation failed"
            ) from exc
        return await provider_call()

    try:
        response = await provider_call()
    except BaseException as exc:
        try:
            await hooks.ambiguous(reservation_id, _ambiguous_reason(exc))
        except Exception as hook_exc:
            if context.strict:
                raise EmbeddingControlPlaneError(
                    "strict campaign could not record an ambiguous embedding outcome"
                ) from hook_exc
        raise
    try:
        await hooks.succeed(
            reservation_id,
            EmbeddingCallResult(
                provider_request_id=_request_id(response),
                usage=_usage(response),
            ),
        )
    except Exception as exc:
        if context.strict:
            raise EmbeddingControlPlaneError(
                "strict campaign could not settle an embedding reservation"
            ) from exc
    return response


class EmbeddingReservationMemoryConnector:
    def __init__(
        self,
        inner: MemoryConnector,
        *,
        hooks: EmbeddingCallHooks | None,
        tenant_id: str,
        run_id: str,
        node_id: str,
        campaign_id: str,
        strict: bool,
    ) -> None:
        self._inner = inner
        self._context = _EmbeddingCallContext(
            identity=EmbeddingCallIdentity(
                tenant_id=tenant_id,
                run_id=run_id,
                node_id=node_id,
                campaign_id=campaign_id,
                operation="write",
            ),
            hooks=hooks,
            strict=strict,
        )

    async def _with_operation[ResponseT](
        self,
        operation: EmbeddingOperation,
        call: Callable[[], Awaitable[ResponseT]],
    ) -> ResponseT:
        context = replace(
            self._context,
            identity=replace(self._context.identity, operation=operation),
        )
        token = _CURRENT_EMBEDDING_CALL.set(context)
        try:
            return await call()
        finally:
            _CURRENT_EMBEDDING_CALL.reset(token)

    async def read(
        self, key: str, scope: MemoryScope, *, target: str | None = None
    ) -> MemoryEntry | None:
        return await self._inner.read(key, scope, target=target)

    async def write(
        self, key: str, value: JSONValue, scope: MemoryScope, *, target: str | None = None
    ) -> None:
        await self._with_operation(
            "write",
            lambda: self._inner.write(key, value, scope, target=target),
        )

    async def delete(self, key: str, scope: MemoryScope, *, target: str | None = None) -> None:
        await self._inner.delete(key, scope, target=target)

    async def search(
        self, query: dict[str, Any], scope: MemoryScope, *, target: str | None = None
    ) -> list[MemoryEntry]:
        return await self._with_operation(
            "search",
            lambda: self._inner.search(query, scope, target=target),
        )
