"""Governed clients share one TLS context instead of parsing the CA bundle each time."""

from __future__ import annotations

import pytest

from zeroth.integrations.http import factory


@pytest.mark.asyncio
async def test_governed_clients_share_one_ssl_context(monkeypatch: pytest.MonkeyPatch) -> None:
    factory._default_ssl_context.cache_clear()
    calls = 0
    real = factory.httpx.create_ssl_context

    def counting_create_ssl_context(*args, **kwargs):
        nonlocal calls
        calls += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(factory.httpx, "create_ssl_context", counting_create_ssl_context)
    owner = type("Owner", (), {"state": type("State", (), {})()})()
    try:
        clients = [
            await factory.governed_async_client(
                purpose=f"ssl-share-{index}", timeout=1.0, base_url="http://127.0.0.1:9", app=owner
            )
            for index in range(3)
        ]
        assert len({id(client) for client in clients}) == 3
        assert calls == 1
    finally:
        await factory.aclose_all(owner)
        factory._default_ssl_context.cache_clear()
