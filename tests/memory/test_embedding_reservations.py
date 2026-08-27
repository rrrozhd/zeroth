from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from zeroth.integrations.memory.chroma_connector import ChromaDBMemoryConnector
from zeroth.integrations.memory.embedding_calls import (
    EmbeddingCallBound,
    EmbeddingCallIdentity,
    EmbeddingCallResult,
    EmbeddingControlPlaneError,
    EmbeddingReservationMemoryConnector,
)
from zeroth.integrations.memory.governed.models import MemoryScope
from zeroth.integrations.memory.models import ConnectorManifest
from zeroth.integrations.memory.pgvector_connector import PgvectorMemoryConnector
from zeroth.integrations.memory.registry import InMemoryConnectorRegistry, MemoryConnectorResolver


class RecordingHooks:
    def __init__(self) -> None:
        self.reserved: list[tuple[EmbeddingCallIdentity, EmbeddingCallBound]] = []
        self.succeeded: list[tuple[str, EmbeddingCallResult]] = []
        self.ambiguous_events: list[tuple[str, str]] = []

    async def reserve(
        self, identity: EmbeddingCallIdentity, bound: EmbeddingCallBound
    ) -> str:
        self.reserved.append((identity, bound))
        await asyncio.sleep(0)
        return f"reservation-{identity.run_id}"

    async def succeed(self, reservation_id: str, result: EmbeddingCallResult) -> None:
        self.succeeded.append((reservation_id, result))

    async def ambiguous(self, reservation_id: str, reason: str) -> None:
        self.ambiguous_events.append((reservation_id, reason))


def _chroma() -> ChromaDBMemoryConnector:
    collection = MagicMock()
    collection.get.return_value = {"ids": [], "documents": [], "metadatas": []}
    collection.upsert.return_value = None
    client = MagicMock()
    client.get_or_create_collection.return_value = collection
    return ChromaDBMemoryConnector(
        client,
        collection_prefix="reservation_test",
        embedding_model="openai/text-embedding-3-small",
    )


async def test_resolver_keeps_concurrent_embedding_identities_isolated() -> None:
    hooks = RecordingHooks()
    raw = _chroma()
    registry = InMemoryConnectorRegistry()
    registry.register(
        "memory://shared-chroma",
        ConnectorManifest(connector_type="chroma", scope=MemoryScope.SHARED),
        raw,
    )
    resolver = MemoryConnectorResolver(registry=registry)
    resolver.set_embedding_call_hooks(hooks)
    first = (
        await resolver.resolve(
            ["memory://shared-chroma"],
            runtime_context={
                "tenant_id": "tenant-a",
                "run_id": "run-a",
                "campaign_id": "campaign-a",
                "campaign_strict": True,
            },
            node_id="node-a",
        )
    )[0].connector
    second = (
        await resolver.resolve(
            ["memory://shared-chroma"],
            runtime_context={
                "tenant_id": "tenant-b",
                "run_id": "run-b",
                "campaign_id": "campaign-b",
                "campaign_strict": True,
            },
            node_id="node-b",
        )
    )[0].connector

    gate = asyncio.Event()
    started = 0

    async def embedding(*, model: str, input: list[str]):  # noqa: A002
        nonlocal started
        started += 1
        if started == 2:
            gate.set()
        await gate.wait()
        return SimpleNamespace(
            data=[{"embedding": [0.1, 0.2]}],
            id=f"provider-{input[0].split(':', 1)[0]}",
            usage={"prompt_tokens": len(input[0])},
        )

    with patch(
        "zeroth.integrations.memory.chroma_connector.litellm.aembedding",
        new=embedding,
    ):
        await asyncio.gather(
            first.write("first", "a", MemoryScope.SHARED),
            second.write("second", "b", MemoryScope.SHARED),
        )

    identities = {identity.run_id: identity for identity, _bound in hooks.reserved}
    assert identities["run-a"] == EmbeddingCallIdentity(
        tenant_id="tenant-a",
        run_id="run-a",
        node_id="node-a",
        campaign_id="campaign-a",
        operation="write",
    )
    assert identities["run-b"].tenant_id == "tenant-b"
    assert identities["run-b"].campaign_id == "campaign-b"
    assert {bound.model for _identity, bound in hooks.reserved} == {
        "openai/text-embedding-3-small"
    }
    assert all(bound.input_count == 1 and bound.input_utf8_bytes > 0 for _, bound in hooks.reserved)
    assert {result.provider_request_id for _, result in hooks.succeeded} == {
        "provider-first",
        "provider-second",
    }
    assert {result.usage["prompt_tokens"] for _, result in hooks.succeeded if result.usage} == {
        len("first: a"),
        len("second: b"),
    }
    assert not hooks.ambiguous_events


async def test_litellm_hidden_provider_request_id_settles_embedding_reservation() -> None:
    hooks = RecordingHooks()
    raw = _chroma()
    raw._client.get_or_create_collection.return_value.query.return_value = {
        "ids": [[]],
        "documents": [[]],
        "metadatas": [[]],
        "distances": [[]],
    }
    connector = EmbeddingReservationMemoryConnector(
        raw,
        hooks=hooks,
        tenant_id="tenant-a",
        run_id="run-hidden-id",
        node_id="node-search",
        campaign_id="campaign-a",
        strict=True,
    )

    response = SimpleNamespace(
        data=[{"embedding": [0.1, 0.2]}],
        usage={"prompt_tokens": 3},
        _hidden_params={
            "additional_headers": {
                "llm_provider-x-request-id": "req-hidden-embedding",
            }
        },
    )
    with patch(
        "zeroth.integrations.memory.chroma_connector.litellm.aembedding",
        new=AsyncMock(return_value=response),
    ):
        await connector.search({"text": "find me"}, MemoryScope.SHARED)

    assert hooks.succeeded[0][1].provider_request_id == "req-hidden-embedding"


async def test_pgvector_timeout_marks_reserved_embedding_ambiguous() -> None:
    hooks = RecordingHooks()
    raw = PgvectorMemoryConnector(
        AsyncMock(),
        embedding_model="openai/text-embedding-3-small",
        embedding_dimensions=2,
    )
    connector = EmbeddingReservationMemoryConnector(
        raw,
        hooks=hooks,
        tenant_id="tenant-a",
        run_id="run-timeout",
        node_id="node-search",
        campaign_id="campaign-a",
        strict=True,
    )

    with patch(
        "zeroth.integrations.memory.pgvector_connector.litellm.aembedding",
        new=AsyncMock(side_effect=TimeoutError("provider timed out")),
    ):
        with pytest.raises(TimeoutError, match="provider timed out"):
            await connector.search({"text": "find me"}, MemoryScope.SHARED)

    assert hooks.reserved[0][0].operation == "search"
    assert hooks.ambiguous_events == [("reservation-run-timeout", "timeout")]
    assert not hooks.succeeded


async def test_chroma_cancellation_marks_reserved_embedding_ambiguous() -> None:
    hooks = RecordingHooks()
    connector = EmbeddingReservationMemoryConnector(
        _chroma(),
        hooks=hooks,
        tenant_id="tenant-a",
        run_id="run-cancelled",
        node_id="node-write",
        campaign_id="campaign-a",
        strict=True,
    )

    with patch(
        "zeroth.integrations.memory.chroma_connector.litellm.aembedding",
        new=AsyncMock(side_effect=asyncio.CancelledError()),
    ):
        with pytest.raises(asyncio.CancelledError):
            await connector.write("cancelled", "value", MemoryScope.SHARED)

    assert hooks.ambiguous_events == [("reservation-run-cancelled", "cancelled")]
    assert not hooks.succeeded


async def test_reservation_cancellation_never_starts_provider_call() -> None:
    hooks = AsyncMock()
    hooks.reserve.side_effect = asyncio.CancelledError()
    connector = EmbeddingReservationMemoryConnector(
        _chroma(),
        hooks=hooks,
        tenant_id="tenant-a",
        run_id="run-cancelled",
        node_id="node-write",
        campaign_id="campaign-a",
        strict=False,
    )
    provider = AsyncMock()

    with patch(
        "zeroth.integrations.memory.chroma_connector.litellm.aembedding",
        new=provider,
    ):
        with pytest.raises(asyncio.CancelledError):
            await connector.write("cancelled", "value", MemoryScope.SHARED)

    provider.assert_not_awaited()


@pytest.mark.parametrize("hooks", [None, AsyncMock()])
async def test_strict_campaign_fails_before_provider_when_control_plane_unavailable(hooks) -> None:
    if hooks is not None:
        hooks.reserve.side_effect = RuntimeError("control plane unavailable")
    raw = _chroma()
    connector = EmbeddingReservationMemoryConnector(
        raw,
        hooks=hooks,
        tenant_id="tenant-a",
        run_id="run-a",
        node_id="node-a",
        campaign_id="campaign-a",
        strict=True,
    )
    provider = AsyncMock()

    with patch(
        "zeroth.integrations.memory.chroma_connector.litellm.aembedding",
        new=provider,
    ):
        with pytest.raises(EmbeddingControlPlaneError):
            await connector.write("blocked", "value", MemoryScope.SHARED)

    provider.assert_not_awaited()


async def test_unwrapped_non_campaign_connector_keeps_legacy_embedding_behavior() -> None:
    raw = _chroma()
    response = SimpleNamespace(data=[{"embedding": [0.1, 0.2]}])
    provider = AsyncMock(return_value=response)

    with patch(
        "zeroth.integrations.memory.chroma_connector.litellm.aembedding",
        new=provider,
    ):
        await raw.write("legacy", "value", MemoryScope.SHARED)

    provider.assert_awaited_once()


async def test_resolver_does_not_govern_non_campaign_calls_when_hooks_are_configured() -> None:
    hooks = RecordingHooks()
    raw = _chroma()
    registry = InMemoryConnectorRegistry()
    registry.register(
        "memory://legacy",
        ConnectorManifest(connector_type="chroma", scope=MemoryScope.SHARED),
        raw,
    )
    resolver = MemoryConnectorResolver(registry=registry)
    resolver.set_embedding_call_hooks(hooks)
    connector = (
        await resolver.resolve(
            ["memory://legacy"],
            runtime_context={"tenant_id": "default", "run_id": "legacy-run"},
        )
    )[0].connector
    response = SimpleNamespace(data=[{"embedding": [0.1, 0.2]}])

    with patch(
        "zeroth.integrations.memory.chroma_connector.litellm.aembedding",
        new=AsyncMock(return_value=response),
    ):
        await connector.write("legacy", "value", MemoryScope.SHARED)

    assert hooks.reserved == []
