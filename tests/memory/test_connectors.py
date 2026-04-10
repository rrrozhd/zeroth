"""Tests for GovernAI-protocol in-memory connectors.

Verifies that RunEphemeralMemoryConnector, KeyValueMemoryConnector, and
ThreadMemoryConnector implement the GovernAI MemoryConnector protocol
with correct read/write/delete/search behavior.
"""

from __future__ import annotations

import pytest
from governai.memory.connector import MemoryConnector as GovernAIMemoryConnector
from governai.memory.models import MemoryEntry, MemoryScope

from zeroth.core.memory.connectors import (
    KeyValueMemoryConnector,
    RunEphemeralMemoryConnector,
    ThreadMemoryConnector,
)


# --- Protocol compliance ---


@pytest.mark.asyncio
async def test_ephemeral_connector_is_governai_protocol() -> None:
    connector = RunEphemeralMemoryConnector()
    assert isinstance(connector, GovernAIMemoryConnector)


@pytest.mark.asyncio
async def test_key_value_connector_is_governai_protocol() -> None:
    connector = KeyValueMemoryConnector()
    assert isinstance(connector, GovernAIMemoryConnector)


@pytest.mark.asyncio
async def test_thread_connector_is_governai_protocol() -> None:
    connector = ThreadMemoryConnector()
    assert isinstance(connector, GovernAIMemoryConnector)


# --- RunEphemeralMemoryConnector ---


@pytest.mark.asyncio
async def test_ephemeral_read_returns_none_when_empty() -> None:
    connector = RunEphemeralMemoryConnector()
    result = await connector.read("key1", MemoryScope.RUN, target="run-123")
    assert result is None


@pytest.mark.asyncio
async def test_ephemeral_write_then_read_returns_entry() -> None:
    connector = RunEphemeralMemoryConnector()
    await connector.write("key1", {"data": 1}, MemoryScope.RUN, target="run-123")
    result = await connector.read("key1", MemoryScope.RUN, target="run-123")
    assert result is not None
    assert isinstance(result, MemoryEntry)
    assert result.value == {"data": 1}
    assert result.key == "key1"
    assert result.scope == MemoryScope.RUN
    assert result.scope_target == "run-123"


@pytest.mark.asyncio
async def test_ephemeral_write_overwrites_preserves_created_at() -> None:
    connector = RunEphemeralMemoryConnector()
    await connector.write("key1", {"v": 1}, MemoryScope.RUN, target="run-1")
    first = await connector.read("key1", MemoryScope.RUN, target="run-1")
    await connector.write("key1", {"v": 2}, MemoryScope.RUN, target="run-1")
    second = await connector.read("key1", MemoryScope.RUN, target="run-1")
    assert second is not None
    assert second.value == {"v": 2}
    assert first is not None
    assert second.created_at == first.created_at
    assert second.updated_at >= first.updated_at


# --- KeyValueMemoryConnector ---


@pytest.mark.asyncio
async def test_key_value_delete_raises_key_error_when_missing() -> None:
    connector = KeyValueMemoryConnector()
    with pytest.raises(KeyError):
        await connector.delete("key1", MemoryScope.SHARED, target="__shared__")


@pytest.mark.asyncio
async def test_key_value_write_read_delete_cycle() -> None:
    connector = KeyValueMemoryConnector()
    await connector.write("k", "value", MemoryScope.SHARED, target="__shared__")
    entry = await connector.read("k", MemoryScope.SHARED, target="__shared__")
    assert entry is not None
    assert entry.value == "value"
    await connector.delete("k", MemoryScope.SHARED, target="__shared__")
    assert await connector.read("k", MemoryScope.SHARED, target="__shared__") is None


# --- ThreadMemoryConnector ---


@pytest.mark.asyncio
async def test_thread_search_returns_matching_entries() -> None:
    connector = ThreadMemoryConnector()
    await connector.write("hello-world", {"text": "hello"}, MemoryScope.THREAD, target="t-1")
    await connector.write("goodbye", {"text": "bye"}, MemoryScope.THREAD, target="t-1")
    results = await connector.search({"text": "hello"}, MemoryScope.THREAD, target="t-1")
    assert len(results) == 1
    assert results[0].key == "hello-world"


@pytest.mark.asyncio
async def test_thread_search_empty_text_returns_all() -> None:
    connector = ThreadMemoryConnector()
    await connector.write("a", 1, MemoryScope.THREAD, target="t-1")
    await connector.write("b", 2, MemoryScope.THREAD, target="t-1")
    results = await connector.search({}, MemoryScope.THREAD, target="t-1")
    assert len(results) == 2


# --- Cross-cutting ---


@pytest.mark.asyncio
async def test_different_targets_are_isolated() -> None:
    connector = RunEphemeralMemoryConnector()
    await connector.write("key1", "val-a", MemoryScope.RUN, target="run-a")
    await connector.write("key1", "val-b", MemoryScope.RUN, target="run-b")
    a = await connector.read("key1", MemoryScope.RUN, target="run-a")
    b = await connector.read("key1", MemoryScope.RUN, target="run-b")
    assert a is not None and a.value == "val-a"
    assert b is not None and b.value == "val-b"


@pytest.mark.asyncio
async def test_no_target_uses_empty_string() -> None:
    connector = KeyValueMemoryConnector()
    await connector.write("k", "v", MemoryScope.SHARED)
    entry = await connector.read("k", MemoryScope.SHARED)
    assert entry is not None
    assert entry.scope_target == ""
