"""WS-B: TenantScopedMemoryConnector isolation, fail-closed, and slug tests.

Proves that on ONE physical backend connector, memory written as tenant A is
invisible to tenant B across every scope — RUN, THREAD and (critically) SHARED,
which governai's ScopedMemoryConnector otherwise resolves to the un-tenanted
literal ``"__shared__"``. The wrapper stack mirrors production exactly:

    ScopedMemoryConnector(TenantScopedMemoryConnector(raw))

so Scoped resolves scope -> target first, then TenantScoped namespaces it.
"""

from __future__ import annotations

import fnmatch

import pytest

from zeroth.integrations.memory.connectors import KeyValueMemoryConnector
from zeroth.integrations.memory.governed.models import MemoryScope
from zeroth.integrations.memory.governed.scoped import ScopedMemoryConnector
from zeroth.integrations.memory.redis_kv import RedisKVMemoryConnector
from zeroth.integrations.memory.redis_thread import RedisThreadMemoryConnector
from zeroth.integrations.memory.tenant_scoped import (
    TenantScopedMemoryConnector,
    TenantScopeError,
    tenant_slug,
)

# ---------------------------------------------------------------------------
# In-process async Redis fake (faithful to the ops the connectors use)
# ---------------------------------------------------------------------------


class _FakeAsyncRedis:
    """Minimal async Redis supporting get/set/delete/scan_iter/zadd/zrevrange."""

    def __init__(self) -> None:
        self._kv: dict[str, str] = {}
        self._z: dict[str, list[tuple[float, str]]] = {}

    async def get(self, key: str) -> str | None:
        return self._kv.get(key)

    async def set(self, key: str, value: str) -> None:
        self._kv[key] = value

    async def delete(self, key: str) -> int:
        removed = 0
        if key in self._kv:
            del self._kv[key]
            removed = 1
        if key in self._z:
            del self._z[key]
            removed = 1
        return removed

    async def scan_iter(self, match: str):
        for key in list(self._kv) + list(self._z):
            if fnmatch.fnmatch(key, match):
                yield key

    async def zadd(self, key: str, mapping: dict[str, float]) -> None:
        bucket = self._z.setdefault(key, [])
        for member, score in mapping.items():
            bucket.append((score, member))

    async def zrevrange(
        self, key: str, start: int, stop: int, withscores: bool = False
    ) -> list[str] | list[tuple[str, float]]:
        bucket = sorted(self._z.get(key, []), key=lambda item: item[0], reverse=True)
        window = bucket[start:] if stop == -1 else bucket[start : stop + 1]
        if withscores:
            return [(member, score) for score, member in window]
        return [member for _, member in window]


# ---------------------------------------------------------------------------
# Helpers: build the production wrapper stack for a given tenant + backend
# ---------------------------------------------------------------------------


def _stack(raw: object, tenant_id: str, *, run_id: str = "run-1", thread_id: str = "thread-1"):
    """Scoped(TenantScoped(raw)) — the exact production nesting."""
    return ScopedMemoryConnector(
        TenantScopedMemoryConnector(raw, tenant_id=tenant_id),
        run_id=run_id,
        thread_id=thread_id,
        workflow_name="wf",
    )


def _backends() -> list[tuple[str, object]]:
    return [
        ("in_memory_kv", KeyValueMemoryConnector()),
        ("redis_kv", RedisKVMemoryConnector(_FakeAsyncRedis())),
        ("redis_thread", RedisThreadMemoryConnector(_FakeAsyncRedis())),
    ]


# ---------------------------------------------------------------------------
# Cross-tenant isolation on a SINGLE physical connector, every scope
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("backend_name,raw", _backends())
@pytest.mark.parametrize("scope", [MemoryScope.SHARED, MemoryScope.RUN, MemoryScope.THREAD])
@pytest.mark.asyncio
async def test_write_as_a_not_readable_as_b(backend_name: str, raw: object, scope: MemoryScope):
    a = _stack(raw, "tenant-a")
    b = _stack(raw, "tenant-b")

    await a.write("secret", {"v": 1}, scope)

    # Same physical backend, same run/thread ids — only the tenant differs.
    assert await a.read("secret", scope) is not None, backend_name
    assert await b.read("secret", scope) is None, backend_name


@pytest.mark.asyncio
async def test_memory_write_same_key_collision_is_tenant_scoped() -> None:
    raw = KeyValueMemoryConnector()
    tenant_a = _stack(raw, "tenant-a")
    tenant_b = _stack(raw, "tenant-b")
    await tenant_a.write("same-key", {"owner": "a"}, MemoryScope.SHARED)
    await tenant_b.write("same-key", {"owner": "b"}, MemoryScope.SHARED)
    assert (await tenant_a.read("same-key", MemoryScope.SHARED)).value == {"owner": "a"}
    assert (await tenant_b.read("same-key", MemoryScope.SHARED)).value == {"owner": "b"}


@pytest.mark.asyncio
async def test_memory_read_foreign_matches_unknown_tenant() -> None:
    raw = KeyValueMemoryConnector()
    tenant_a = _stack(raw, "tenant-a")
    tenant_b = _stack(raw, "tenant-b")
    unknown = _stack(raw, "tenant-unknown")
    await tenant_a.write("owner-key", {"secret": True}, MemoryScope.SHARED)
    assert await tenant_b.read("owner-key", MemoryScope.SHARED) is None
    assert await unknown.read("owner-key", MemoryScope.SHARED) is None


@pytest.mark.parametrize("backend_name,raw", _backends())
@pytest.mark.asyncio
async def test_search_is_tenant_isolated(backend_name: str, raw: object):
    a = _stack(raw, "tenant-a")
    b = _stack(raw, "tenant-b")

    await a.write("doc", {"text": "alpha"}, MemoryScope.SHARED)

    a_hits = await a.search({"text": "alpha"}, MemoryScope.SHARED)
    b_hits = await b.search({"text": "alpha"}, MemoryScope.SHARED)
    assert len(a_hits) >= 1, backend_name
    assert b_hits == [], backend_name


# ---------------------------------------------------------------------------
# Fail-closed guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [None, "", "   "])
def test_empty_tenant_is_fail_closed(bad):
    with pytest.raises(TenantScopeError):
        TenantScopedMemoryConnector(KeyValueMemoryConnector(), tenant_id=bad)


def test_default_sentinel_is_permitted():
    # 'default' is the reserved single-tenant sentinel and must NOT raise.
    conn = TenantScopedMemoryConnector(KeyValueMemoryConnector(), tenant_id="default")
    assert conn is not None


@pytest.mark.asyncio
async def test_never_coerces_missing_into_default():
    # A real 'default' tenant must not be able to read what a missing tenant
    # would have written — because a missing tenant cannot write at all.
    raw = KeyValueMemoryConnector()
    with pytest.raises(TenantScopeError):
        _stack(raw, "")  # constructing the wrapper already fails closed


# ---------------------------------------------------------------------------
# Slug collision-freedom (Chroma-style non-alnum collapse can't merge tenants)
# ---------------------------------------------------------------------------


def test_slug_distinguishes_dash_vs_underscore():
    # Both sanitize to the same readable prefix; the raw-id hash keeps them
    # distinct, so Chroma's [^a-zA-Z0-9]+ -> "_" collapse cannot merge them.
    assert tenant_slug("tenant-a") != tenant_slug("tenant_a")


def test_slug_is_backend_safe_charset():
    import re

    slug = tenant_slug("Tenant A!#")
    # Only [a-z0-9-]; the hex hash survives any non-alnum collapse intact.
    assert re.fullmatch(r"[a-z0-9-]+", slug), slug


@pytest.mark.asyncio
async def test_slug_collision_end_to_end():
    raw = KeyValueMemoryConnector()
    a = _stack(raw, "tenant-a")
    a2 = _stack(raw, "tenant_a")  # different raw id, same sanitized prefix
    await a.write("k", "from-dash", MemoryScope.SHARED)
    assert await a2.read("k", MemoryScope.SHARED) is None
