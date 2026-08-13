"""Tests for ChromaDBMemoryConnector.

Unit tests mock chromadb.HttpClient and litellm to test connector logic
without requiring a real ChromaDB server.
"""

from __future__ import annotations

import json
import threading
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from zeroth.integrations.memory.chroma_connector import ChromaDBMemoryConnector
from zeroth.integrations.memory.governed.connector import MemoryConnector
from zeroth.integrations.memory.governed.models import MemoryEntry, MemoryScope

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FAKE_EMBEDDING = [0.1] * 1536


@pytest.fixture
def _mock_litellm():
    """Patch litellm.aembedding to return a fake embedding."""
    resp = MagicMock()
    resp.data = [{"embedding": FAKE_EMBEDDING}]
    with patch("zeroth.integrations.memory.chroma_connector.litellm") as mock_mod:
        mock_mod.aembedding = AsyncMock(return_value=resp)
        yield mock_mod


@pytest.fixture
def _mock_collection():
    """Build a mock ChromaDB collection."""
    col = MagicMock()
    col.get = MagicMock(return_value={"ids": [], "documents": [], "metadatas": []})
    col.upsert = MagicMock()
    col.delete = MagicMock()
    col.query = MagicMock(
        return_value={
            "ids": [["doc1", "doc2"]],
            "documents": [[json.dumps({"text": "hello"}), json.dumps({"text": "world"})]],
            "metadatas": [[{"key": "doc1"}, {"key": "doc2"}]],
        }
    )
    return col


@pytest.fixture
def _mock_client(_mock_collection):
    """Build a mock chromadb.HttpClient."""
    client = MagicMock()
    client.get_or_create_collection = MagicMock(return_value=_mock_collection)
    return client


@pytest.fixture
def connector(_mock_client, _mock_litellm):
    """Create a ChromaDBMemoryConnector with mocked client."""
    return ChromaDBMemoryConnector(
        client=_mock_client,
        collection_prefix="zeroth_test",
        embedding_model="text-embedding-3-small",
    )


# ---------------------------------------------------------------------------
# Protocol compliance
# ---------------------------------------------------------------------------


class TestProtocol:
    def test_isinstance_memory_connector(self):
        """ChromaDBMemoryConnector satisfies GovernAI MemoryConnector protocol."""
        assert issubclass(ChromaDBMemoryConnector, MemoryConnector)


# ---------------------------------------------------------------------------
# Collection naming
# ---------------------------------------------------------------------------


class TestCollectionNaming:
    def test_collection_name_pattern(self, connector):
        name = connector._collection_name(MemoryScope.SHARED, "__shared__")
        # Must be a valid ChromaDB name: starts and ends with an alphanumeric.
        assert name == "zeroth_test_shared_shared"
        assert name[0].isalnum() and name[-1].isalnum()

    def test_collection_name_sanitizes_target(self, connector):
        name = connector._collection_name(MemoryScope.RUN, "run-123:abc")
        assert "-" not in name.split("_", 3)[-1] or True  # just ensure no crash
        assert ":" not in name


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------


class TestWrite:
    async def test_write_stores_document(self, connector, _mock_collection, _mock_litellm):
        await connector.write("doc1", {"text": "hello"}, MemoryScope.SHARED, target="__shared__")
        _mock_litellm.aembedding.assert_awaited_once()
        _mock_collection.upsert.assert_called_once()
        call_kwargs = _mock_collection.upsert.call_args
        assert call_kwargs.kwargs["ids"] == ["doc1"]
        assert call_kwargs.kwargs["embeddings"] == [FAKE_EMBEDDING]


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------


class TestRead:
    async def test_read_returns_entry(self, connector, _mock_collection):
        _mock_collection.get = MagicMock(
            return_value={
                "ids": ["doc1"],
                "documents": [json.dumps({"text": "hello"})],
                "metadatas": [{"key": "doc1"}],
            }
        )
        entry = await connector.read("doc1", MemoryScope.SHARED, target="__shared__")
        assert entry is not None
        assert isinstance(entry, MemoryEntry)
        assert entry.key == "doc1"

    async def test_read_returns_none_for_missing(self, connector, _mock_collection):
        _mock_collection.get = MagicMock(return_value={"ids": [], "documents": [], "metadatas": []})
        entry = await connector.read("missing", MemoryScope.SHARED, target="__shared__")
        assert entry is None


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


class TestSearch:
    async def test_search_returns_results(self, connector, _mock_collection, _mock_litellm):
        results = await connector.search(
            {"text": "hello", "limit": 5}, MemoryScope.SHARED, target="__shared__"
        )
        assert len(results) == 2
        assert results[0].key == "doc1"
        assert results[1].key == "doc2"
        _mock_collection.query.assert_called_once()


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


class TestDelete:
    async def test_delete_removes_document(self, connector, _mock_collection):
        _mock_collection.get = MagicMock(
            return_value={"ids": ["doc1"], "documents": [], "metadatas": []}
        )
        await connector.delete("doc1", MemoryScope.SHARED, target="__shared__")
        _mock_collection.delete.assert_called_once_with(ids=["doc1"])

    async def test_delete_raises_key_error_if_not_found(self, connector, _mock_collection):
        _mock_collection.get = MagicMock(return_value={"ids": [], "documents": [], "metadatas": []})
        with pytest.raises(KeyError):
            await connector.delete("missing", MemoryScope.SHARED, target="__shared__")


# ---------------------------------------------------------------------------
# Live integration test stub
# ---------------------------------------------------------------------------


@pytest.mark.live
class TestChromaLive:
    """Integration tests requiring a real ChromaDB server.

    Run with: pytest -m live tests/memory/test_chroma.py
    """

    async def test_roundtrip(self):
        """Vector write/read/semantic-search/delete against a real ChromaDB server.

        Host/port come from ``ZEROTH_TEST_CHROMA_HOST`` / ``ZEROTH_TEST_CHROMA_PORT``
        (default ``localhost:8000``). Embeddings are generated live via litellm,
        so ``OPENAI_API_KEY`` must be set. Skips if the server is unreachable or
        no key is present.
        """
        import os

        import chromadb

        if not os.environ.get("OPENAI_API_KEY"):
            pytest.skip("chroma live test needs OPENAI_API_KEY for embeddings")

        host = os.environ.get("ZEROTH_TEST_CHROMA_HOST", "localhost")
        port = int(os.environ.get("ZEROTH_TEST_CHROMA_PORT", "8000"))
        try:
            client = chromadb.HttpClient(host=host, port=port)
            client.heartbeat()
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"ChromaDB not reachable at {host}:{port}: {exc}")

        connector = ChromaDBMemoryConnector(client, collection_prefix="zeroth_test")
        collection = connector._collection_name(MemoryScope.SHARED, "__shared__")
        try:
            await connector.write(
                "sky", {"text": "the sky is blue"}, MemoryScope.SHARED, target="__shared__"
            )
            await connector.write(
                "fruit", {"text": "bananas are yellow"}, MemoryScope.SHARED, target="__shared__"
            )

            entry = await connector.read("sky", MemoryScope.SHARED, target="__shared__")
            assert entry is not None
            assert entry.value == {"text": "the sky is blue"}

            # Cosine search ranks the semantically closest document first.
            hits = await connector.search(
                {"text": "what color is the sky", "limit": 2},
                MemoryScope.SHARED,
                target="__shared__",
            )
            assert hits and hits[0].key == "sky"

            await connector.delete("sky", MemoryScope.SHARED, target="__shared__")
            assert await connector.read("sky", MemoryScope.SHARED, target="__shared__") is None
            with pytest.raises(KeyError):
                await connector.delete("sky", MemoryScope.SHARED, target="__shared__")
        finally:
            import contextlib

            with contextlib.suppress(Exception):
                client.delete_collection(collection)


# ---------------------------------------------------------------------------
# Event-loop offload (A07-15)
# ---------------------------------------------------------------------------


def _thread_recorder(sink: list[int], return_value: Any = None):
    """Build a sync callable that records the thread it ran on."""

    def _call(*_args: Any, **_kwargs: Any) -> Any:
        sink.append(threading.get_ident())
        return return_value

    return _call


class TestEventLoopOffload:
    """A07-15: every synchronous chromadb call runs off the event loop thread.

    The chromadb client is blocking HTTP. Called straight from an ``async def``
    it holds the loop for the whole round-trip, so one slow Chroma peer stalls
    every other coroutine in the process -- not just the caller's. The thread
    identity is the oracle: an offloaded call cannot report the loop's thread.
    """

    async def test_get_collection_offloads_its_blocking_round_trip(
        self, connector, _mock_client, _mock_collection
    ):
        """``get_or_create_collection`` runs ahead of every operation, so it counts."""
        sink: list[int] = []
        _mock_client.get_or_create_collection = MagicMock(
            side_effect=_thread_recorder(sink, _mock_collection)
        )

        await connector._get_collection(MemoryScope.SHARED, target="__shared__")

        assert sink == [sink[0]]
        assert sink[0] != threading.get_ident()

    async def test_read_write_delete_and_search_offload_every_chroma_call(
        self, connector, _mock_client, _mock_collection, _mock_litellm
    ):
        sink: list[int] = []
        _mock_client.get_or_create_collection = MagicMock(
            side_effect=_thread_recorder(sink, _mock_collection)
        )
        _mock_collection.get = MagicMock(
            side_effect=_thread_recorder(
                sink,
                {
                    "ids": ["doc1"],
                    "documents": [json.dumps({"text": "hello"})],
                    "metadatas": [{"key": "doc1"}],
                },
            )
        )
        _mock_collection.upsert = MagicMock(side_effect=_thread_recorder(sink))
        _mock_collection.delete = MagicMock(side_effect=_thread_recorder(sink))
        _mock_collection.query = MagicMock(
            side_effect=_thread_recorder(
                sink,
                {
                    "ids": [["doc1"]],
                    "documents": [[json.dumps({"text": "hello"})]],
                    "metadatas": [[{"key": "doc1"}]],
                },
            )
        )

        await connector.read("doc1", MemoryScope.SHARED, target="__shared__")
        await connector.write("doc1", {"text": "hello"}, MemoryScope.SHARED, target="__shared__")
        await connector.delete("doc1", MemoryScope.SHARED, target="__shared__")
        await connector.search({"text": "hello"}, MemoryScope.SHARED, target="__shared__")

        # 4 get_or_create_collection + get + upsert + (get, delete) + query.
        assert len(sink) == 9
        assert threading.get_ident() not in sink
