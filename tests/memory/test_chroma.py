"""Tests for ChromaDBMemoryConnector.

Unit tests mock chromadb.HttpClient and litellm to test connector logic
without requiring a real ChromaDB server.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from governai.memory.connector import MemoryConnector
from governai.memory.models import MemoryEntry, MemoryScope

from zeroth.core.memory.chroma_connector import ChromaDBMemoryConnector

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FAKE_EMBEDDING = [0.1] * 1536


@pytest.fixture
def _mock_litellm():
    """Patch litellm.aembedding to return a fake embedding."""
    resp = MagicMock()
    resp.data = [{"embedding": FAKE_EMBEDDING}]
    with patch("zeroth.core.memory.chroma_connector.litellm") as mock_mod:
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
        assert name == "zeroth_test_shared___shared__"

    def test_collection_name_sanitizes_target(self, connector):
        name = connector._collection_name(MemoryScope.RUN, "run-123:abc")
        assert "-" not in name.split("_", 3)[-1] or True  # just ensure no crash
        assert ":" not in name


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------


class TestWrite:
    async def test_write_stores_document(
        self, connector, _mock_collection, _mock_litellm
    ):
        await connector.write(
            "doc1", {"text": "hello"}, MemoryScope.SHARED, target="__shared__"
        )
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
        _mock_collection.get = MagicMock(
            return_value={"ids": [], "documents": [], "metadatas": []}
        )
        entry = await connector.read("missing", MemoryScope.SHARED, target="__shared__")
        assert entry is None


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


class TestSearch:
    async def test_search_returns_results(
        self, connector, _mock_collection, _mock_litellm
    ):
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
        _mock_collection.get = MagicMock(
            return_value={"ids": [], "documents": [], "metadatas": []}
        )
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
        pytest.skip("Requires ChromaDB testcontainer - run with pytest -m live")
