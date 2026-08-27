"""Tests for memory connector factory registration."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from zeroth.integrations.memory.connectors import (
    KeyValueMemoryConnector,
    RunEphemeralMemoryConnector,
    ThreadMemoryConnector,
)
from zeroth.integrations.memory.governed.models import MemoryScope
from zeroth.integrations.memory.registry import InMemoryConnectorRegistry

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
        from zeroth.integrations.memory.factory import register_memory_connectors

        registry = InMemoryConnectorRegistry()
        settings = _make_settings()

        register_memory_connectors(registry, settings)

        for name in ("ephemeral", "key_value", "thread"):
            manifest, connector = registry.resolve(name)
            assert manifest.connector_type == name

    def test_ephemeral_has_run_scope(self) -> None:
        from zeroth.integrations.memory.factory import register_memory_connectors

        registry = InMemoryConnectorRegistry()
        register_memory_connectors(registry, _make_settings())

        manifest, _ = registry.resolve("ephemeral")
        assert manifest.scope == MemoryScope.RUN

    def test_key_value_has_shared_scope(self) -> None:
        from zeroth.integrations.memory.factory import register_memory_connectors

        registry = InMemoryConnectorRegistry()
        register_memory_connectors(registry, _make_settings())

        manifest, _ = registry.resolve("key_value")
        assert manifest.scope == MemoryScope.SHARED

    def test_thread_has_thread_scope(self) -> None:
        from zeroth.integrations.memory.factory import register_memory_connectors

        registry = InMemoryConnectorRegistry()
        register_memory_connectors(registry, _make_settings())

        manifest, _ = registry.resolve("thread")
        assert manifest.scope == MemoryScope.THREAD

    def test_external_connectors_not_registered(self) -> None:
        from zeroth.integrations.memory.factory import register_memory_connectors

        registry = InMemoryConnectorRegistry()
        register_memory_connectors(registry, _make_settings())

        for name in ("redis_kv", "redis_thread", "pgvector", "chroma", "elasticsearch"):
            with pytest.raises(KeyError):
                registry.resolve(name)

    def test_connector_instances_are_correct_types(self) -> None:
        from zeroth.integrations.memory.factory import register_memory_connectors

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
        from zeroth.integrations.memory.factory import register_memory_connectors

        registry = InMemoryConnectorRegistry()
        settings = _make_settings()
        fake_redis = MagicMock()

        with (
            patch("zeroth.integrations.memory.factory.RedisKVMemoryConnector") as kv_cls,
            patch("zeroth.integrations.memory.factory.RedisThreadMemoryConnector") as th_cls,
        ):
            kv_cls.return_value = MagicMock(connector_type="redis_kv")
            th_cls.return_value = MagicMock(connector_type="redis_thread")

            register_memory_connectors(registry, settings, redis_client=fake_redis)

        manifest_kv, conn_kv = registry.resolve("redis_kv")
        assert manifest_kv.connector_type == "redis_kv"
        assert manifest_kv.scope == MemoryScope.SHARED

        manifest_th, conn_th = registry.resolve("redis_thread")
        assert manifest_th.connector_type == "redis_thread"
        assert manifest_th.scope == MemoryScope.THREAD

    def test_redis_connectors_receive_correct_config(self) -> None:
        from zeroth.integrations.memory.factory import register_memory_connectors

        registry = InMemoryConnectorRegistry()
        settings = _make_settings(
            memory=_MemorySettings(
                redis_kv_prefix="custom:kv",
                redis_thread_prefix="custom:thread",
            )
        )
        fake_redis = MagicMock()

        with (
            patch("zeroth.integrations.memory.factory.RedisKVMemoryConnector") as kv_cls,
            patch("zeroth.integrations.memory.factory.RedisThreadMemoryConnector") as th_cls,
        ):
            kv_cls.return_value = MagicMock(connector_type="redis_kv")
            th_cls.return_value = MagicMock(connector_type="redis_thread")

            register_memory_connectors(registry, settings, redis_client=fake_redis)

            kv_cls.assert_called_once_with(fake_redis, key_prefix="custom:kv")
            th_cls.assert_called_once_with(fake_redis, key_prefix="custom:thread")

    def test_in_memory_connectors_still_registered_with_redis(self) -> None:
        from zeroth.integrations.memory.factory import register_memory_connectors

        registry = InMemoryConnectorRegistry()
        fake_redis = MagicMock()

        with (
            patch("zeroth.integrations.memory.factory.RedisKVMemoryConnector") as kv_cls,
            patch("zeroth.integrations.memory.factory.RedisThreadMemoryConnector") as th_cls,
        ):
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
        from zeroth.integrations.memory.factory import register_memory_connectors

        registry = InMemoryConnectorRegistry()
        settings = _make_settings(pgvector=_PgvectorSettings(enabled=True))

        with patch("zeroth.integrations.memory.factory.PgvectorMemoryConnector") as pgv_cls:
            pgv_cls.return_value = MagicMock(connector_type="pgvector")

            register_memory_connectors(
                registry, settings, pg_conninfo="postgresql://localhost/test"
            )

        manifest, _ = registry.resolve("pgvector")
        assert manifest.connector_type == "pgvector"
        assert manifest.scope == MemoryScope.SHARED

    def test_pgvector_not_registered_without_conninfo(self) -> None:
        from zeroth.integrations.memory.factory import register_memory_connectors

        registry = InMemoryConnectorRegistry()
        settings = _make_settings(pgvector=_PgvectorSettings(enabled=True))

        register_memory_connectors(registry, settings)

        with pytest.raises(KeyError):
            registry.resolve("pgvector")

    def test_pgvector_not_registered_when_disabled(self) -> None:
        from zeroth.integrations.memory.factory import register_memory_connectors

        registry = InMemoryConnectorRegistry()
        settings = _make_settings(pgvector=_PgvectorSettings(enabled=False))

        register_memory_connectors(registry, settings, pg_conninfo="postgresql://localhost/test")

        with pytest.raises(KeyError):
            registry.resolve("pgvector")

    def test_pgvector_receives_correct_config(self) -> None:
        from zeroth.integrations.memory.factory import register_memory_connectors

        registry = InMemoryConnectorRegistry()
        pgv_settings = _PgvectorSettings(
            enabled=True,
            table_name="custom_vectors",
            embedding_model="ada-002",
            embedding_dimensions=768,
        )
        settings = _make_settings(pgvector=pgv_settings)

        with patch("zeroth.integrations.memory.factory.PgvectorMemoryConnector") as pgv_cls:
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
        from zeroth.integrations.memory.factory import register_memory_connectors

        registry = InMemoryConnectorRegistry()
        settings = _make_settings(chroma=_ChromaSettings(enabled=True))

        with (
            patch("zeroth.integrations.memory.factory.chromadb") as mock_chromadb,
            patch("zeroth.integrations.memory.factory.ChromaDBMemoryConnector") as chroma_cls,
        ):
            mock_chromadb.HttpClient.return_value = MagicMock()
            chroma_cls.return_value = MagicMock(connector_type="chroma")

            register_memory_connectors(registry, settings)

        manifest, _ = registry.resolve("chroma")
        assert manifest.connector_type == "chroma"
        assert manifest.scope == MemoryScope.SHARED

    def test_chroma_not_registered_when_disabled(self) -> None:
        from zeroth.integrations.memory.factory import register_memory_connectors

        registry = InMemoryConnectorRegistry()
        settings = _make_settings(chroma=_ChromaSettings(enabled=False))

        register_memory_connectors(registry, settings)

        with pytest.raises(KeyError):
            registry.resolve("chroma")

    def test_chroma_receives_correct_config(self) -> None:
        from zeroth.integrations.memory.factory import register_memory_connectors

        registry = InMemoryConnectorRegistry()
        settings = _make_settings(
            chroma=_ChromaSettings(
                enabled=True, host="chroma-host", port=9000, collection_prefix="my_prefix"
            )
        )

        with (
            patch("zeroth.integrations.memory.factory.chromadb") as mock_chromadb,
            patch("zeroth.integrations.memory.factory.ChromaDBMemoryConnector") as chroma_cls,
        ):
            mock_chromadb.HttpClient.return_value = MagicMock()
            chroma_cls.return_value = MagicMock(connector_type="chroma")

            register_memory_connectors(registry, settings)

            mock_chromadb.HttpClient.assert_called_once_with(host="chroma-host", port=9000)
            chroma_cls.assert_called_once()
            call_kwargs = chroma_cls.call_args[1]
            assert call_kwargs["collection_prefix"] == "my_prefix"

    def test_dynamic_chroma_passes_local_embedding_model_and_marks_provider_free(self) -> None:
        from zeroth.integrations.memory.chroma_connector import LOCAL_HASH_EMBEDDING_MODEL
        from zeroth.integrations.memory.factory import build_connector

        with (
            patch("zeroth.integrations.memory.factory.chromadb") as mock_chromadb,
            patch("zeroth.integrations.memory.factory.ChromaDBMemoryConnector") as chroma_cls,
        ):
            mock_chromadb.HttpClient.return_value = MagicMock()
            chroma_cls.return_value = MagicMock(connector_type="chroma")

            manifest, _ = build_connector(
                "chroma",
                {
                    "host": "127.0.0.1",
                    "port": 8121,
                    "collection_prefix": "tenant_fixture",
                    "embedding_model": LOCAL_HASH_EMBEDDING_MODEL,
                },
            )

        assert chroma_cls.call_args.kwargs["embedding_model"] == LOCAL_HASH_EMBEDDING_MODEL
        assert manifest.config == {"provider_call_mode": "none"}


# ---------------------------------------------------------------------------
# Tests: Elasticsearch
# ---------------------------------------------------------------------------


class TestElasticsearchRegistration:
    """Elasticsearch connector registers when enabled."""

    def test_registers_elasticsearch(self) -> None:
        from zeroth.integrations.memory.factory import register_memory_connectors

        registry = InMemoryConnectorRegistry()
        settings = _make_settings(elasticsearch=_ElasticsearchSettings(enabled=True))

        with (
            patch("zeroth.integrations.memory.factory.AsyncElasticsearch") as mock_es_cls,
            patch("zeroth.integrations.memory.factory.ElasticsearchMemoryConnector") as es_conn_cls,
        ):
            mock_es_cls.return_value = MagicMock()
            es_conn_cls.return_value = MagicMock(connector_type="elasticsearch")

            register_memory_connectors(registry, settings)

        manifest, _ = registry.resolve("elasticsearch")
        assert manifest.connector_type == "elasticsearch"
        assert manifest.scope == MemoryScope.SHARED

    def test_elasticsearch_not_registered_when_disabled(self) -> None:
        from zeroth.integrations.memory.factory import register_memory_connectors

        registry = InMemoryConnectorRegistry()
        settings = _make_settings(elasticsearch=_ElasticsearchSettings(enabled=False))

        register_memory_connectors(registry, settings)

        with pytest.raises(KeyError):
            registry.resolve("elasticsearch")

    def test_elasticsearch_receives_correct_config(self) -> None:
        from zeroth.integrations.memory.factory import register_memory_connectors

        registry = InMemoryConnectorRegistry()
        settings = _make_settings(
            elasticsearch=_ElasticsearchSettings(
                enabled=True,
                hosts=["http://es1:9200", "http://es2:9200"],
                index_prefix="custom_idx",
            )
        )

        with (
            patch("zeroth.integrations.memory.factory.AsyncElasticsearch") as mock_es_cls,
            patch("zeroth.integrations.memory.factory.ElasticsearchMemoryConnector") as es_conn_cls,
        ):
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
    """Connector instances are singletons.

    Resolving the same ref twice returns the same object.
    """

    def test_same_connector_object_on_multiple_resolves(self) -> None:
        from zeroth.integrations.memory.factory import register_memory_connectors

        registry = InMemoryConnectorRegistry()
        register_memory_connectors(registry, _make_settings())

        _, conn_first = registry.resolve("ephemeral")
        _, conn_second = registry.resolve("ephemeral")

        assert conn_first is conn_second

    def test_all_in_memory_connectors_are_singletons(self) -> None:
        from zeroth.integrations.memory.factory import register_memory_connectors

        registry = InMemoryConnectorRegistry()
        register_memory_connectors(registry, _make_settings())

        for name in ("ephemeral", "key_value", "thread"):
            _, first = registry.resolve(name)
            _, second = registry.resolve(name)
            assert first is second, f"{name} connector is not a singleton"


# ---------------------------------------------------------------------------
# Tests: mandatory client timeouts (A07-14)
# ---------------------------------------------------------------------------


class _SessionlessClient:
    """A chromadb-shaped client that exposes no HTTP session to bound."""


class TestConnectorTimeouts:
    """A07-14: clients this factory builds get a positive, non-``None`` timeout.

    Measured on the pinned versions: redis-py 5.3.1 leaves ``socket_timeout``
    and ``socket_connect_timeout`` at ``None``, and chromadb 1.5.6 builds its
    transport as ``httpx.Client(timeout=None)``. Neither driver bounds itself,
    so an unresponsive peer blocks the caller for as long as it holds the
    socket. Elasticsearch is deliberately absent: elasticsearch-py stamps
    ``request_timeout=10.0`` onto its node config already.
    """

    def test_redis_client_is_built_with_both_socket_timeouts(self) -> None:
        from zeroth.integrations.memory import factory

        _, connector = factory.build_connector("redis_thread", {"url": "redis://127.0.0.1:6399"})

        kwargs = connector._redis.connection_pool.connection_kwargs
        assert kwargs["socket_timeout"] == factory.REDIS_TIMEOUT_SECONDS
        assert kwargs["socket_connect_timeout"] == factory.REDIS_TIMEOUT_SECONDS
        assert factory.REDIS_TIMEOUT_SECONDS > 0

    def test_redis_kv_client_is_built_with_both_socket_timeouts(self) -> None:
        from zeroth.integrations.memory import factory

        _, connector = factory.build_connector("redis_kv", {"url": "redis://127.0.0.1:6399"})

        kwargs = connector._redis.connection_pool.connection_kwargs
        assert kwargs["socket_timeout"] == factory.REDIS_TIMEOUT_SECONDS
        assert kwargs["socket_connect_timeout"] == factory.REDIS_TIMEOUT_SECONDS

    def test_a_supplied_redis_client_is_left_alone(self) -> None:
        """A shared client the caller already built is not this factory's to rebuild."""
        from zeroth.integrations.memory import factory

        supplied = MagicMock()
        _, connector = factory.build_connector("redis_kv", {}, redis_client=supplied)

        assert connector._redis is supplied

    def test_chroma_timeout_binds_the_session_a_real_client_would_use(self) -> None:
        """Pin the attribute path against the real library, not against a stub.

        ``_bind_chroma_timeout`` reaches into ``client._server._session``. Every
        other test here supplies that shape itself, so all of them would keep
        passing if chromadb moved its transport -- and the binder would then
        raise at bootstrap for every chroma-enabled deployment. A real
        ``chromadb.HttpClient`` cannot be built without a live server (it does a
        tenant handshake in ``__init__``), but the object it assigns to
        ``_server`` can: ``Client.__init__`` sets
        ``self._server = self._system.instance(ServerAPI)``, and that
        construction is local.
        """
        import httpx
        from chromadb.api import ServerAPI
        from chromadb.config import Settings, System

        from zeroth.integrations.memory import factory

        server = System(
            Settings(
                chroma_api_impl="chromadb.api.fastapi.FastAPI",
                chroma_server_host="localhost",
                chroma_server_http_port=8000,
            )
        ).instance(ServerAPI)

        assert isinstance(server._session, httpx.Client)
        assert server._session.timeout == httpx.Timeout(None), (
            "premise: chromadb builds its transport with timeouts disabled"
        )

        factory._bind_chroma_timeout(SimpleNamespace(_server=server), 1.5)

        assert server._session.timeout == httpx.Timeout(1.5)

    def test_chroma_request_timeout_reaches_the_http_session(self) -> None:
        """Chromadb has no timeout kwarg, so the ceiling lands on its httpx client."""
        import httpx

        from zeroth.integrations.memory import factory

        session = httpx.Client(timeout=None)
        assert session.timeout == httpx.Timeout(None), "premise: chromadb disables timeouts"
        stub = SimpleNamespace(_server=SimpleNamespace(_session=session))

        with patch("zeroth.integrations.memory.factory.chromadb") as mock_chromadb:
            mock_chromadb.HttpClient.return_value = stub
            factory.build_connector("chroma", {"host": "chroma-host"})

        assert session.timeout == httpx.Timeout(factory.CHROMA_TIMEOUT_SECONDS)
        assert factory.CHROMA_TIMEOUT_SECONDS > 0

    def test_chroma_client_without_a_session_is_refused_not_left_unbounded(self) -> None:
        """A silent ``getattr`` miss would hand back an unbounded client."""
        from zeroth.integrations.memory import factory

        with patch("zeroth.integrations.memory.factory.chromadb") as mock_chromadb:
            mock_chromadb.HttpClient.return_value = _SessionlessClient()
            with pytest.raises(ValueError, match="no HTTP session"):
                factory.build_connector("chroma", {"host": "chroma-host"})

    @pytest.mark.parametrize("bad", [None, 0, 0.0, -1, -0.5, "10", True, [10]])
    def test_a_non_positive_timeout_is_refused(self, bad: Any) -> None:
        """``None`` is the driver default this guard exists to reject, not a sentinel."""
        from zeroth.integrations.memory.factory import _mandatory_timeout

        with pytest.raises(ValueError, match="positive number of seconds"):
            _mandatory_timeout(bad, "chroma")

    def test_the_module_timeout_constants_are_positive_numbers(self) -> None:
        from zeroth.integrations.memory import factory

        for backend, seconds in (
            ("chroma", factory.CHROMA_TIMEOUT_SECONDS),
            ("redis_kv", factory.REDIS_TIMEOUT_SECONDS),
        ):
            assert backend in factory.TIMEOUT_GOVERNED_BACKENDS
            assert isinstance(seconds, float)
            assert seconds > 0
