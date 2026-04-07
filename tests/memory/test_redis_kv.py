"""Unit tests for RedisKVMemoryConnector."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from governai.memory.connector import MemoryConnector
from governai.memory.models import MemoryEntry, MemoryScope

from zeroth.memory.redis_kv import RedisKVMemoryConnector


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_connector(mock_redis: AsyncMock | None = None) -> tuple[RedisKVMemoryConnector, AsyncMock]:
    """Create a connector with a mocked Redis client."""
    if mock_redis is None:
        mock_redis = AsyncMock()
    connector = RedisKVMemoryConnector(mock_redis, key_prefix="zeroth:mem:kv")
    return connector, mock_redis


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    def test_isinstance_check(self) -> None:
        connector, _ = _make_connector()
        assert isinstance(connector, MemoryConnector)

    def test_connector_type(self) -> None:
        connector, _ = _make_connector()
        assert connector.connector_type == "redis_kv"


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------


class TestRead:
    @pytest.mark.asyncio
    async def test_read_existing_key(self) -> None:
        connector, mock_redis = _make_connector()
        entry = MemoryEntry(
            key="user_prefs",
            value={"theme": "dark"},
            scope=MemoryScope.RUN,
            scope_target="run-1",
        )
        mock_redis.get.return_value = entry.model_dump_json().encode()

        result = await connector.read("user_prefs", MemoryScope.RUN, target="run-1")

        assert result is not None
        assert result.key == "user_prefs"
        assert result.value == {"theme": "dark"}
        mock_redis.get.assert_awaited_once_with("zeroth:mem:kv:run:run-1:user_prefs")

    @pytest.mark.asyncio
    async def test_read_nonexistent_returns_none(self) -> None:
        connector, mock_redis = _make_connector()
        mock_redis.get.return_value = None

        result = await connector.read("nonexistent", MemoryScope.RUN, target="run-1")

        assert result is None

    @pytest.mark.asyncio
    async def test_read_no_target(self) -> None:
        connector, mock_redis = _make_connector()
        mock_redis.get.return_value = None

        await connector.read("key", MemoryScope.SHARED)

        mock_redis.get.assert_awaited_once_with("zeroth:mem:kv:shared::key")


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------


class TestWrite:
    @pytest.mark.asyncio
    async def test_write_stores_entry(self) -> None:
        connector, mock_redis = _make_connector()
        mock_redis.get.return_value = None  # no existing entry

        await connector.write("user_prefs", {"theme": "dark"}, MemoryScope.RUN, target="run-1")

        mock_redis.set.assert_awaited_once()
        call_args = mock_redis.set.call_args
        redis_key = call_args[0][0]
        stored_json = call_args[0][1]

        assert redis_key == "zeroth:mem:kv:run:run-1:user_prefs"
        parsed = json.loads(stored_json)
        assert parsed["key"] == "user_prefs"
        assert parsed["value"] == {"theme": "dark"}
        assert parsed["scope"] == "run"

    @pytest.mark.asyncio
    async def test_write_then_read_roundtrip(self) -> None:
        connector, mock_redis = _make_connector()

        # Capture what write stores, then return it on read
        stored: dict[str, bytes] = {}

        async def fake_set(key: str, value: str) -> None:
            stored[key] = value.encode() if isinstance(value, str) else value

        async def fake_get(key: str) -> bytes | None:
            return stored.get(key)

        mock_redis.set.side_effect = fake_set
        mock_redis.get.side_effect = fake_get

        await connector.write("user_prefs", {"theme": "dark"}, MemoryScope.RUN, target="run-1")
        result = await connector.read("user_prefs", MemoryScope.RUN, target="run-1")

        assert result is not None
        assert result.value == {"theme": "dark"}


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


class TestDelete:
    @pytest.mark.asyncio
    async def test_delete_existing_key(self) -> None:
        connector, mock_redis = _make_connector()
        mock_redis.delete.return_value = 1  # 1 key deleted

        await connector.delete("user_prefs", MemoryScope.RUN, target="run-1")

        mock_redis.delete.assert_awaited_once_with("zeroth:mem:kv:run:run-1:user_prefs")

    @pytest.mark.asyncio
    async def test_delete_nonexistent_raises_key_error(self) -> None:
        connector, mock_redis = _make_connector()
        mock_redis.delete.return_value = 0  # nothing deleted

        with pytest.raises(KeyError):
            await connector.delete("nonexistent", MemoryScope.RUN, target="run-1")


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


class TestSearch:
    @pytest.mark.asyncio
    async def test_search_by_text(self) -> None:
        connector, mock_redis = _make_connector()

        entry1 = MemoryEntry(
            key="user_prefs", value={"theme": "dark"}, scope=MemoryScope.RUN, scope_target="run-1"
        )
        entry2 = MemoryEntry(
            key="system_config", value={"debug": True}, scope=MemoryScope.RUN, scope_target="run-1"
        )

        # Mock scan_iter to return keys
        async def fake_scan_iter(match: str = "*"):  # noqa: ANN001
            yield b"zeroth:mem:kv:run:run-1:user_prefs"
            yield b"zeroth:mem:kv:run:run-1:system_config"

        mock_redis.scan_iter = fake_scan_iter

        async def fake_get(key: bytes | str) -> bytes:
            k = key.decode() if isinstance(key, bytes) else key
            if "user_prefs" in k:
                return entry1.model_dump_json().encode()
            return entry2.model_dump_json().encode()

        mock_redis.get = AsyncMock(side_effect=fake_get)

        results = await connector.search({"text": "pref"}, MemoryScope.RUN, target="run-1")

        assert len(results) == 1
        assert results[0].key == "user_prefs"

    @pytest.mark.asyncio
    async def test_search_empty_text_returns_all(self) -> None:
        connector, mock_redis = _make_connector()

        entry = MemoryEntry(
            key="anything", value="val", scope=MemoryScope.RUN, scope_target="run-1"
        )

        async def fake_scan_iter(match: str = "*"):  # noqa: ANN001
            yield b"zeroth:mem:kv:run:run-1:anything"

        mock_redis.scan_iter = fake_scan_iter
        mock_redis.get = AsyncMock(return_value=entry.model_dump_json().encode())

        results = await connector.search({}, MemoryScope.RUN, target="run-1")

        assert len(results) == 1


# ---------------------------------------------------------------------------
# Scope isolation
# ---------------------------------------------------------------------------


class TestScopeIsolation:
    @pytest.mark.asyncio
    async def test_different_targets_produce_different_keys(self) -> None:
        connector, mock_redis = _make_connector()
        mock_redis.get.return_value = None

        await connector.read("key", MemoryScope.RUN, target="run-1")
        await connector.read("key", MemoryScope.RUN, target="run-2")

        calls = mock_redis.get.call_args_list
        assert calls[0][0][0] == "zeroth:mem:kv:run:run-1:key"
        assert calls[1][0][0] == "zeroth:mem:kv:run:run-2:key"

    @pytest.mark.asyncio
    async def test_different_scopes_produce_different_keys(self) -> None:
        connector, mock_redis = _make_connector()
        mock_redis.get.return_value = None

        await connector.read("key", MemoryScope.RUN, target="t-1")
        await connector.read("key", MemoryScope.THREAD, target="t-1")

        calls = mock_redis.get.call_args_list
        assert calls[0][0][0] == "zeroth:mem:kv:run:t-1:key"
        assert calls[1][0][0] == "zeroth:mem:kv:thread:t-1:key"


# ---------------------------------------------------------------------------
# Key format
# ---------------------------------------------------------------------------


class TestKeyFormat:
    def test_key_format_structure(self) -> None:
        connector, _ = _make_connector()
        key = connector._key("user_prefs", MemoryScope.RUN, "run-1")
        assert key == "zeroth:mem:kv:run:run-1:user_prefs"

    def test_key_format_no_target(self) -> None:
        connector, _ = _make_connector()
        key = connector._key("user_prefs", MemoryScope.SHARED, None)
        assert key == "zeroth:mem:kv:shared::user_prefs"


# ---------------------------------------------------------------------------
# Integration test stub (skipped without live marker)
# ---------------------------------------------------------------------------


@pytest.mark.live
class TestRedisKVIntegration:
    """Integration tests that require a real Redis instance.

    Run with: uv run pytest -m live tests/memory/test_redis_kv.py
    """

    @pytest.mark.asyncio
    async def test_write_read_roundtrip_live(self) -> None:
        """Requires a running Redis. Skipped by default."""
        pytest.skip("Requires live Redis -- run with -m live")
