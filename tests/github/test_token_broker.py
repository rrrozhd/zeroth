"""InstallationTokenBroker: single-flight, refresh, retry, and lifetime redaction."""

from __future__ import annotations

import asyncio

import pytest

from tests.github.fake_github import FakeGitHubAPI
from zeroth.integrations.github.app_jwt import AppJwtIssuer
from zeroth.integrations.github.client import GitHubAppClient
from zeroth.integrations.github.config import GitHubAppConfig
from zeroth.integrations.github.models import (
    InstallationSuspendedError,
    InstallationTokenRejectedError,
)
from zeroth.integrations.github.token_broker import InstallationTokenBroker
from zeroth.platform.secrets.provider import EnvSecretProvider

BASE_URL = "http://api.github.test"

REDACTION_MARKER = "[REDACTED:github-installation-token]"


def _standard_api(**api_kwargs) -> FakeGitHubAPI:
    api = FakeGitHubAPI(**api_kwargs)
    api.add_installation(1, "acme")
    api.add_repo(1, "acme/widgets", private=True)
    return api


def _rig(api: FakeGitHubAPI, **broker_kwargs) -> tuple[GitHubAppClient, InstallationTokenBroker]:
    config = GitHubAppConfig(app_id=api.app_id, api_base_url=BASE_URL)
    issuer = AppJwtIssuer(
        config, EnvSecretProvider({"GITHUB_APP_PRIVATE_KEY": api.private_key_pem})
    )
    client = GitHubAppClient(config, issuer, transport=api.transport())
    return client, InstallationTokenBroker(client, **broker_kwargs)


async def test_ten_concurrent_leases_share_one_mint() -> None:
    api = _standard_api()
    client, broker = _rig(api)
    async with client:
        leases = await asyncio.gather(*(broker.lease(1, "widgets") for _ in range(10)))
    tokens = {lease.reveal() for lease in leases}
    assert tokens == {"ghs_fake_1"}
    assert api.minted_tokens == ["ghs_fake_1"]


async def test_token_is_refreshed_inside_the_expiry_margin() -> None:
    # TTL 100s < the 120s refresh margin, so every lease re-mints.
    api = _standard_api(token_ttl_seconds=100)
    client, broker = _rig(api)
    async with client:
        first = await broker.lease(1, "widgets")
        second = await broker.lease(1, "widgets")
    assert first.reveal() != second.reveal()
    assert len(api.minted_tokens) == 2


async def test_fresh_token_is_cached_across_leases() -> None:
    api = _standard_api()
    client, broker = _rig(api)
    async with client:
        first = await broker.lease(1, "widgets")
        second = await broker.lease(1, "widgets")
    assert first.reveal() == second.reveal()
    assert len(api.minted_tokens) == 1


async def test_rejected_token_is_cleared_and_retried_exactly_once() -> None:
    api = _standard_api()
    api.expire_next_token()  # the first mint is dead on arrival
    client, broker = _rig(api)
    async with client:
        grants = await broker.run_with_lease(
            1, "widgets", lambda token: client.list_installation_repositories(token)
        )
    assert [grant.full_name for grant in grants] == ["acme/widgets"]
    assert len(api.minted_tokens) == 2  # one failed use, one retry mint
    # The dead token was never cached: the cache now holds the working one.
    fresh = await broker.lease(1, "widgets")
    assert fresh.reveal() == api.minted_tokens[-1]
    assert len(api.minted_tokens) == 2


async def test_persistent_rejection_stops_after_one_retry() -> None:
    api = _standard_api()
    client, broker = _rig(api)
    calls = 0

    async def always_rejected(token: str) -> None:
        nonlocal calls
        calls += 1
        raise InstallationTokenRejectedError("test operation")

    async with client:
        with pytest.raises(InstallationTokenRejectedError):
            await broker.run_with_lease(1, "widgets", always_rejected)
    assert calls == 2  # the original attempt plus exactly one retry


async def test_redact_scrubs_every_historical_token_and_credential_form() -> None:
    api = _standard_api(token_ttl_seconds=100)  # short TTL forces fresh mints
    client, broker = _rig(api)
    async with client:
        first = await broker.lease(1, "widgets")
        second = await broker.lease(1, "widgets")
    assert len(api.minted_tokens) == 2
    text = " ".join(
        [first.reveal(), second.reveal(), first.basic_auth_header(), second.basic_auth_header()]
    )
    scrubbed = broker.redact(text)
    for token in api.minted_tokens:
        assert token not in scrubbed
    assert first.basic_auth_header() not in scrubbed
    assert REDACTION_MARKER in scrubbed
    # lease.redact is the same history, bound through the broker.
    assert first.redact(second.reveal()) == REDACTION_MARKER


async def test_lease_repr_never_shows_the_token() -> None:
    api = _standard_api()
    client, broker = _rig(api)
    async with client:
        lease = await broker.lease(1, "widgets")
    assert lease.reveal() not in repr(lease)
    assert REDACTION_MARKER in repr(lease)


async def test_verify_installation_is_memoized_on_the_monotonic_clock() -> None:
    api = _standard_api()
    client, broker = _rig(api)
    now = [100.0]
    broker._monotonic = lambda: now[0]  # inject a controllable monotonic clock
    calls = 0
    original = client.get_installation

    async def counting(installation_id: int):
        nonlocal calls
        calls += 1
        return await original(installation_id)

    client.get_installation = counting  # type: ignore[method-assign]
    async with client:
        await broker.verify_installation(1)
        await broker.verify_installation(1)
        assert calls == 1  # inside the 30s TTL: memoized
        now[0] += 31.0
        await broker.verify_installation(1)
        assert calls == 2  # TTL elapsed: re-verified


async def test_suspension_raises_and_is_never_memoized() -> None:
    api = _standard_api()
    api.suspend(1)
    client, broker = _rig(api)
    calls = 0
    original = client.get_installation

    async def counting(installation_id: int):
        nonlocal calls
        calls += 1
        return await original(installation_id)

    client.get_installation = counting  # type: ignore[method-assign]
    async with client:
        with pytest.raises(InstallationSuspendedError):
            await broker.verify_installation(1)
        with pytest.raises(InstallationSuspendedError):
            await broker.verify_installation(1)
    assert calls == 2  # failures hit the API every time
