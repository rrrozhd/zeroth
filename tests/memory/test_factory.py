"""Tests for memory connector factory registration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from zeroth.memory.connectors import (
    KeyValueMemoryConnector,
    RunEphemeralMemoryConnector,
    ThreadMemoryConnector,
)
from zeroth.memory.models import ConnectorManifest, ConnectorScope
from zeroth.memory.registry import InMemoryConnectorRegistry


# ---------------------------------------------------------------------------
# Settings stubs -- mirrors the shape the factory expects
# ---------------------------------------------------------------------------


@dataclass
class _MemorySettings:
    default_connector: str = "ephemeral"
    redis_kv_prefix: str = "zeroth:mem:kv"
    redis_thread_prefix: str = "zeroth:mem:thread"


@dataclass
class _PgvectorSettings:
    enabled: bool = False
    table_name: str = "zeroth_memory_vectors"
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536


@dataclass
class _ChromaSettings:
    enabled: bool = False
    host: str = "localhost"
    port: int = 8000
    collection_prefix: str = "zeroth_memory"


@dataclass
class _ElasticsearchSettings:
    enabled: bool = False
    hosts: list[str] = field(default_factory=lambda: ["http://localhost:9200"])
    index_prefix: str = "zeroth_memory"


@dataclass
class _FakeSettings:
    """Minimal settings shape matching what register_memory_connectors expects."""

    memory: _MemorySettings = field(default_factory=_MemorySettings)
    pgvector: _PgvectorSettings = field(default_factory=_PgvectorSettings)
    chroma: _ChromaSettings = field(default_factory=_ChromaSettings)
    elasticsearch: _ElasticsearchSettings = field(default_factory=_ElasticsearchSettings)


def _make_settings(**overrides: Any) -> _FakeSettings:
    """Build fake settings with optional section overrides."""
    settings = _FakeSettings()
    for key, value in overrides.items():
        setattr(settings, key, value)
    return settings


# ---------------------------------------------------------------------------
# Tests: default (in-memory only)
# ---------------------------------------------------------------------------


class TestDefaultRegistration:
    """With all external backends disabled, only in-memory connectors register."""

    def test_registers_ephemeral_key_value_thread(self) -> None:
        from zeroth.memory.factory import register_memory_connectors

        registry = InMemoryConnectorRegistry()
        settings = _make_settings()

        register_memory_connectors(registry, settings)

        for name in ("ephemeral", "key_value", "thread"):
            manifest, connector = registry.resolve(name)
            assert manifest.connector_type == name

    def test_ephemeral_has_run_scope(self) -> None:
        from zeroth.memory.factory import register_memory_connectors

        registry = InMemoryConnectorRegistry()
        register_memory_connectors(registry, _make_settings())

        manifest, _ = registry.resolve("ephemeral")
        assert manifest.scope == ConnectorScope.RUN

    def test_key_value_has_shared_scope(self) -> None:
        from zeroth.memory.factory import register_memory_connectors

        registry = InMemoryConnectorRegistry()
        register_memory_connectors(registry, _make_settings())

        manifest, _ = registry.resolve("key_value")
        assert manifest.scope == ConnectorScope.SHARED

    def test_thread_has_thread_scope(self) -> None:
        from zeroth.memory.factory import register_memory_connectors

        registry = InMemoryConnectorRegistry()
        register_memory_connectors(registry, _make_settings())

        manifest, _ = registry.resolve("thread")
        assert manifest.scope == ConnectorScope.THREAD

    def test_external_connectors_not_registered(self) -> None:
        from zeroth.memory.factory import register_memory_connectors

        registry = InMemoryConnectorRegistry()
        register_memory_connectors(registry, _make_settings())

        for name in ("redis_kv", "redis_thread", "pgvector", "chroma", "elasticsearch"):
            with pytest.raises(KeyError):
                registry.resolve(name)

    def test_connector_instances_are_correct_types(self) -> None:
        from zeroth.memory.factory import register_memory_connectors

        registry = InMemoryConnectorRegistry()
        register_memory_connectors(registry, _make_settings())

        _, eph = registry.resolve("ephemeral")
        _, kv = registry.resolve("key_value")
        _, th = registry.resolve("thread")

        assert isinstance(eph, RunEphemeralMemoryConnector)
        assert isinstance(kv, KeyValueMemoryConnector)
        assert isinstance(th, ThreadMemoryConnector)


# ---------------------------------------------------------------------------
# Tests: Redis connectors
# ---------------------------------------------------------------------------


class TestRedisRegistration:
    """Redis connectors register when a redis_client is provided."""

    def test_registers_redis_kv_and_thread(self) -> None:
        from zeroth.memory.factory import register_memory_connectors

        registry = InMemoryConnectorRegistry()
        settings = _make_settings()
        fake_redis = MagicMock()

        with patch("zeroth.memory.factory.RedisKVMemoryConnector") as kv_cls, \
             patch("zeroth.memory.factory.RedisThreadMemoryConnector") as th_cls:
            kv_cls.return_value = MagicMock(connector_type="redis_kv")
            th_cls.return_value = MagicMock(connector_type="redis_thread")

            register_memory_connectors(registry, settings, redis_client=fake_redis)

        manifest_kv, conn_kv = registry.resolve("redis_kv")
        assert manifest_kv.connector_type == "redis_kv"
        assert manifest_kv.scope == ConnectorScope.SHARED

        manifest_th, conn_th = registry.resolve("redis_thread")
        assert manifest_th.connector_type == "redis_thread"
        assert manifest_th.scope == ConnectorScope.THREAD

    def test_redis_connectors_receive_correct_config(self) -> None:
        from zeroth.memory.factory import register_memory_connectors

        registry = InMemoryConnectorRegistry()
        settings = _make_settings(
            memory=_MemorySettings(
                redis_kv_prefix="custom:kv",
                redis_thread_prefix="custom:thread",
            )
        )
        fake_redis = MagicMock()

        with patch("zeroth.memory.factory.RedisKVMemoryConnector") as kv_cls, \
             patch("zeroth.memory.factory.RedisThreadMemoryConnector") as th_cls:
            kv_cls.return_value = MagicMock(connector_type="redis_kv")
            th_cls.return_value = MagicMock(connector_type="redis_thread")

            register_memory_connectors(registry, settings, redis_client=fake_redis)

            kv_cls.assert_called_once_with(fake_redis, key_prefix="custom:kv")
            th_cls.assert_called_once_with(fake_redis, key_prefix="custom:thread")

    def test_in_memory_connectors_still_registered_with_redis(self) -> None:
        from zeroth.memory.factory import register_memory_connectors

        registry = InMemoryConnectorRegistry()
        fake_redis = MagicMock()

        with patch("zeroth.memory.factory.RedisKVMemoryConnector") as kv_cls, \
             patch("zeroth.memory.factory.RedisThreadMemoryConnector") as th_cls:
            kv_cls.return_value = MagicMock(connector_type="redis_kv")
            th_cls.return_value = MagicMock(connector_type="redis_thread")

            register_memory_connectors(registry, _make_settings(), redis_client=fake_redis)

        for name in ("ephemeral", "key_value", "thread"):
            manifest, _ = registry.resolve(name)
            assert manifest.connector_type == name


# ---------------------------------------------------------------------------
# Tests: pgvector
# ---------------------------------------------------------------------------


class TestPgvectorRegistration:
    """pgvector connector registers when enabled and pg_conninfo provided."""

    def test_registers_pgvector(self) -> None:
        from zeroth.memory.factory import register_memory_connectors

        registry = InMemoryConnectorRegistry()
        settings = _make_settings(pgvector=_PgvectorSettings(enabled=True))

        with patch("zeroth.memory.factory.PgvectorMemoryConnector") as pgv_cls:
            pgv_cls.return_value = MagicMock(connector_type="pgvector")

            register_memory_connectors(
                registry, settings, pg_conninfo="postgresql://localhost/test"
            )

        manifest, _ = registry.resolve("pgvector")
        assert manifest.connector_type == "pgvector"
        assert manifest.scope == ConnectorScope.SHARED

    def test_pgvector_not_registered_without_conninfo(self) -> None:
        from zeroth.memory.factory import register_memory_connectors

        registry = InMemoryConnectorRegistry()
        settings = _make_settings(pgvector=_PgvectorSettings(enabled=True))

        register_memory_connectors(registry, settings)

        with pytest.raises(KeyError):
            registry.resolve("pgvector")

    def test_pgvector_not_registered_when_disabled(self) -> None:
        from zeroth.memory.factory import register_memory_connectors

        registry = InMemoryConnectorRegistry()
        settings = _make_settings(pgvector=_PgvectorSettings(enabled=False))

        register_memory_connectors(
            registry, settings, pg_conninfo="postgresql://localhost/test"
        )

        with pytest.raises(KeyError):
            registry.resolve("pgvector")

    def test_pgvector_receives_correct_config(self) -> None:
        from zeroth.memory.factory import register_memory_connectors

        registry = InMemoryConnectorRegistry()
        pgv_settings = _PgvectorSettings(
            enabled=True,
            table_name="custom_vectors",
            embedding_model="ada-002",
            embedding_dimensions=768,
        )
        settings = _make_settings(pgvector=pgv_settings)

        with patch("zeroth.memory.factory.PgvectorMemoryConnector") as pgv_cls:
            pgv_cls.return_value = MagicMock(connector_type="pgvector")

            register_memory_connectors(
                registry, settings, pg_conninfo="postgresql://localhost/test"
            )

            pgv_cls.assert_called_once_with(
                "postgresql://localhost/test",
                table_name="custom_vectors",
                embedding_model="ada-002",
                embedding_dimensions=768,
            )


# ---------------------------------------------------------------------------
# Tests: ChromaDB
# ---------------------------------------------------------------------------


class TestChromaRegistration:
    """ChromaDB connector registers when enabled."""

    def test_registers_chroma(self) -> None:
        from zeroth.memory.factory import register_memory_connectors

        registry = InMemoryConnectorRegistry()
        settings = _make_settings(chroma=_ChromaSettings(enabled=True))

        with patch("zeroth.memory.factory.chromadb") as mock_chromadb, \
             patch("zeroth.memory.factory.ChromaDBMemoryConnector") as chroma_cls:
            mock_chromadb.HttpClient.return_value = MagicMock()
            chroma_cls.return_value = MagicMock(connector_type="chroma")

            register_memory_connectors(registry, settings)

        manifest, _ = registry.resolve("chroma")
        assert manifest.connector_type == "chroma"
        assert manifest.scope == ConnectorScope.SHARED

    def test_chroma_not_registered_when_disabled(self) -> None:
        from zeroth.memory.factory import register_memory_connectors

        registry = InMemoryConnectorRegistry()
        settings = _make_settings(chroma=_ChromaSettings(enabled=False))

        register_memory_connectors(registry, settings)

        with pytest.raises(KeyError):
            registry.resolve("chroma")

    def test_chroma_receives_correct_config(self) -> None:
        from zeroth.memory.factory import register_memory_connectors

        registry = InMemoryConnectorRegistry()
        settings = _make_settings(
            chroma=_ChromaSettings(enabled=True, host="chroma-host", port=9000, collection_prefix="my_prefix")
        )

        with patch("zeroth.memory.factory.chromadb") as mock_chromadb, \
             patch("zeroth.memory.factory.ChromaDBMemoryConnector") as chroma_cls:
            mock_chromadb.HttpClient.return_value = MagicMock()
            chroma_cls.return_value = MagicMock(connector_type="chroma")

            register_memory_connectors(registry, settings)

            mock_chromadb.HttpClient.assert_called_once_with(host="chroma-host", port=9000)
            chroma_cls.assert_called_once()
            call_kwargs = chroma_cls.call_args[1]
            assert call_kwargs["collection_prefix"] == "my_prefix"


# ---------------------------------------------------------------------------
# Tests: Elasticsearch
# ---------------------------------------------------------------------------


class TestElasticsearchRegistration:
    """Elasticsearch connector registers when enabled."""

    def test_registers_elasticsearch(self) -> None:
        from zeroth.memory.factory import register_memory_connectors

        registry = InMemoryConnectorRegistry()
        settings = _make_settings(elasticsearch=_ElasticsearchSettings(enabled=True))

        with patch("zeroth.memory.factory.AsyncElasticsearch") as mock_es_cls, \
             patch("zeroth.memory.factory.ElasticsearchMemoryConnector") as es_conn_cls:
            mock_es_cls.return_value = MagicMock()
            es_conn_cls.return_value = MagicMock(connector_type="elasticsearch")

            register_memory_connectors(registry, settings)

        manifest, _ = registry.resolve("elasticsearch")
        assert manifest.connector_type == "elasticsearch"
        assert manifest.scope == ConnectorScope.SHARED

    def test_elasticsearch_not_registered_when_disabled(self) -> None:
        from zeroth.memory.factory import register_memory_connectors

        registry = InMemoryConnectorRegistry()
        settings = _make_settings(elasticsearch=_ElasticsearchSettings(enabled=False))

        register_memory_connectors(registry, settings)

        with pytest.raises(KeyError):
            registry.resolve("elasticsearch")

    def test_elasticsearch_receives_correct_config(self) -> None:
        from zeroth.memory.factory import register_memory_connectors

        registry = InMemoryConnectorRegistry()
        settings = _make_settings(
            elasticsearch=_ElasticsearchSettings(
                enabled=True,
                hosts=["http://es1:9200", "http://es2:9200"],
                index_prefix="custom_idx",
            )
        )

        with patch("zeroth.memory.factory.AsyncElasticsearch") as mock_es_cls, \
             patch("zeroth.memory.factory.ElasticsearchMemoryConnector") as es_conn_cls:
            mock_es_cls.return_value = MagicMock()
            es_conn_cls.return_value = MagicMock(connector_type="elasticsearch")

            register_memory_connectors(registry, settings)

            mock_es_cls.assert_called_once_with(hosts=["http://es1:9200", "http://es2:9200"])
            call_kwargs = es_conn_cls.call_args[1]
            assert call_kwargs["index_prefix"] == "custom_idx"


# ---------------------------------------------------------------------------
# Tests: singleton behavior
# ---------------------------------------------------------------------------


class TestSingletonBehavior:
    """Connector instances are singletons -- resolving the same ref twice
    returns the same object."""

    def test_same_connector_object_on_multiple_resolves(self) -> None:
        from zeroth.memory.factory import register_memory_connectors

        registry = InMemoryConnectorRegistry()
        register_memory_connectors(registry, _make_settings())

        _, conn_first = registry.resolve("ephemeral")
        _, conn_second = registry.resolve("ephemeral")

        assert conn_first is conn_second

    def test_all_in_memory_connectors_are_singletons(self) -> None:
        from zeroth.memory.factory import register_memory_connectors

        registry = InMemoryConnectorRegistry()
        register_memory_connectors(registry, _make_settings())

        for name in ("ephemeral", "key_value", "thread"):
            _, first = registry.resolve(name)
            _, second = registry.resolve(name)
            assert first is second, f"{name} connector is not a singleton"
