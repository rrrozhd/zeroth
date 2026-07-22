"""Tests for ElasticsearchMemoryConnector.

Unit tests mock AsyncElasticsearch to test connector logic
without requiring a real Elasticsearch cluster.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from zeroth.integrations.memory.governed.connector import MemoryConnector
from zeroth.integrations.memory.governed.models import MemoryEntry, MemoryScope

from zeroth.integrations.memory.elastic_connector import ElasticsearchMemoryConnector

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def _mock_es_client():
    """Build a mock AsyncElasticsearch client."""
    client = AsyncMock()
    return client


@pytest.fixture
def connector(_mock_es_client):
    """Create an ElasticsearchMemoryConnector with mocked client."""
    return ElasticsearchMemoryConnector(
        client=_mock_es_client,
        index_prefix="zeroth_test",
    )


# ---------------------------------------------------------------------------
# Protocol compliance
# ---------------------------------------------------------------------------


class TestProtocol:
    def test_isinstance_memory_connector(self):
        """ElasticsearchMemoryConnector satisfies GovernAI MemoryConnector protocol."""
        assert issubclass(ElasticsearchMemoryConnector, MemoryConnector)


# ---------------------------------------------------------------------------
# Index naming
# ---------------------------------------------------------------------------


class TestIndexNaming:
    def test_index_name_pattern(self, connector):
        name = connector._index_name(MemoryScope.SHARED, "__shared__")
        assert name == "zeroth_test_shared___shared__"

    def test_index_name_sanitizes_target(self, connector):
        name = connector._index_name(MemoryScope.RUN, "run-123:abc")
        assert ":" not in name
        assert name.islower() or "_" in name  # lowercase


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------


class TestWrite:
    async def test_write_indexes_document(self, connector, _mock_es_client):
        await connector.write(
            "doc1", {"text": "hello"}, MemoryScope.SHARED, target="__shared__"
        )
        _mock_es_client.index.assert_awaited_once()
        call_kwargs = _mock_es_client.index.call_args.kwargs
        assert call_kwargs["id"] == "doc1"
        assert call_kwargs["index"] == "zeroth_test_shared___shared__"
        assert call_kwargs["document"]["key"] == "doc1"
        assert call_kwargs["document"]["value"] == {"text": "hello"}


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------


class TestRead:
    async def test_read_returns_entry(self, connector, _mock_es_client):
        _mock_es_client.get = AsyncMock(
            return_value={
                "_source": {
                    "key": "doc1",
                    "value": {"text": "hello"},
                    "scope": "shared",
                    "scope_target": "__shared__",
                    "metadata": {},
                }
            }
        )
        entry = await connector.read("doc1", MemoryScope.SHARED, target="__shared__")
        assert entry is not None
        assert isinstance(entry, MemoryEntry)
        assert entry.key == "doc1"
        assert entry.value == {"text": "hello"}

    async def test_read_returns_none_for_missing(self, connector, _mock_es_client):
        from elasticsearch import NotFoundError

        _mock_es_client.get = AsyncMock(
            side_effect=NotFoundError(404, "not found", {})
        )
        entry = await connector.read("missing", MemoryScope.SHARED, target="__shared__")
        assert entry is None


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


class TestSearch:
    async def test_search_returns_matching_documents(self, connector, _mock_es_client):
        _mock_es_client.search = AsyncMock(
            return_value={
                "hits": {
                    "hits": [
                        {
                            "_source": {
                                "key": "doc1",
                                "value": {"text": "hello"},
                                "metadata": {},
                            }
                        },
                        {
                            "_source": {
                                "key": "doc2",
                                "value": {"text": "world"},
                                "metadata": {},
                            }
                        },
                    ]
                }
            }
        )
        results = await connector.search(
            {"text": "hello", "limit": 5}, MemoryScope.SHARED, target="__shared__"
        )
        assert len(results) == 2
        assert results[0].key == "doc1"
        assert results[1].key == "doc2"
        _mock_es_client.search.assert_awaited_once()

    async def test_search_empty_text_uses_match_all(self, connector, _mock_es_client):
        _mock_es_client.search = AsyncMock(
            return_value={"hits": {"hits": []}}
        )
        await connector.search(
            {"text": "", "limit": 10}, MemoryScope.SHARED, target="__shared__"
        )
        call_kwargs = _mock_es_client.search.call_args.kwargs
        assert "match_all" in str(call_kwargs["body"]["query"])


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


class TestDelete:
    async def test_delete_removes_document(self, connector, _mock_es_client):
        _mock_es_client.delete = AsyncMock()
        await connector.delete("doc1", MemoryScope.SHARED, target="__shared__")
        _mock_es_client.delete.assert_awaited_once()

    async def test_delete_raises_key_error_if_not_found(self, connector, _mock_es_client):
        from elasticsearch import NotFoundError

        _mock_es_client.delete = AsyncMock(
            side_effect=NotFoundError(404, "not found", {})
        )
        with pytest.raises(KeyError):
            await connector.delete("missing", MemoryScope.SHARED, target="__shared__")


# ---------------------------------------------------------------------------
# Live integration test stub
# ---------------------------------------------------------------------------


@pytest.mark.live
class TestElasticsearchLive:
    """Integration tests requiring a real Elasticsearch cluster.

    Run with: pytest -m live tests/memory/test_elastic.py
    """

    async def test_roundtrip(self):
        """Index/get/search/delete roundtrip against a real Elasticsearch cluster.

        Endpoint comes from ``ZEROTH_TEST_ELASTICSEARCH_URL`` (default
        ``http://localhost:9200``); skips if the cluster is unreachable.
        """
        import os

        from elasticsearch import AsyncElasticsearch

        url = os.environ.get("ZEROTH_TEST_ELASTICSEARCH_URL", "http://localhost:9200")
        client = AsyncElasticsearch(url, request_timeout=10)
        try:
            if not await client.ping():
                await client.close()
                pytest.skip(f"Elasticsearch not reachable at {url}")
        except Exception as exc:  # noqa: BLE001
            await client.close()
            pytest.skip(f"Elasticsearch not reachable at {url}: {exc}")

        connector = ElasticsearchMemoryConnector(client, index_prefix="zeroth_test_mem")
        index = connector._index_name(MemoryScope.SHARED, "__shared__")
        try:
            await connector.write(
                "doc1", {"text": "hello world"}, MemoryScope.SHARED, target="__shared__"
            )
            # GET-by-id is real-time in Elasticsearch.
            entry = await connector.read("doc1", MemoryScope.SHARED, target="__shared__")
            assert entry is not None
            assert entry.value == {"text": "hello world"}

            # _search is near-real-time: make the new doc visible before querying.
            await client.indices.refresh(index=index)
            hits = await connector.search(
                {"text": "hello"}, MemoryScope.SHARED, target="__shared__"
            )
            assert any(h.key == "doc1" for h in hits)

            # Delete removes it; a second delete raises KeyError.
            await connector.delete("doc1", MemoryScope.SHARED, target="__shared__")
            assert await connector.read("doc1", MemoryScope.SHARED, target="__shared__") is None
            with pytest.raises(KeyError):
                await connector.delete("doc1", MemoryScope.SHARED, target="__shared__")
        finally:
            try:
                await client.indices.delete(index=index, ignore_unavailable=True)
            finally:
                await client.close()
