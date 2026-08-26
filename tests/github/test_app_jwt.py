"""AppJwtIssuer: real RS256 acceptance, caching, fail-closed, single-flight."""

from __future__ import annotations

import asyncio

import httpx

from tests.github.fake_github import FakeGitHubAPI
from zeroth.integrations.github.app_jwt import AppJwtIssuer
from zeroth.integrations.github.config import GitHubAppConfig
from zeroth.integrations.github.models import CheckoutError, CheckoutFailureCode
from zeroth.platform.secrets.provider import EnvSecretProvider

import pytest

BASE_URL = "http://api.github.test"


def _config(api: FakeGitHubAPI) -> GitHubAppConfig:
    return GitHubAppConfig(app_id=api.app_id, api_base_url=BASE_URL)


def _provider(api: FakeGitHubAPI) -> EnvSecretProvider:
    return EnvSecretProvider({"GITHUB_APP_PRIVATE_KEY": api.private_key_pem})


class _CountingProvider:
    """Sync-only provider that counts resolve_secret calls."""

    def __init__(self, pem: str) -> None:
        self.pem = pem
        self.calls = 0

    def resolve(self, secret_ref, *, tenant_id=None):
        return None

    def resolve_many(self, refs, *, tenant_id=None):
        return {}

    def resolve_secret(self, logical_name, *, tenant_id=None, deployment_ref=None):
        self.calls += 1
        return self.pem


async def test_issued_jwt_is_accepted_by_the_fake_github_api() -> None:
    api = FakeGitHubAPI()
    api.add_installation(1, "acme")
    issuer = AppJwtIssuer(_config(api), _provider(api))
    token = await issuer.issue()
    async with httpx.AsyncClient(transport=api.transport(), base_url=BASE_URL) as client:
        response = await client.get(
            "/app/installations/1", headers={"Authorization": f"Bearer {token}"}
        )
    assert response.status_code == 200
    assert response.json()["account"]["login"] == "acme"


async def test_jwt_is_reused_until_near_expiry_then_rotated() -> None:
    api = FakeGitHubAPI()
    now = [1_000_000.0]
    issuer = AppJwtIssuer(_config(api), _provider(api), clock=lambda: now[0])
    first = await issuer.issue()
    assert await issuer.issue() == first

    # expiry = issue_time + 540; refresh only when <= 60s of validity remain.
    now[0] += 470.0
    assert await issuer.issue() == first

    now[0] += 15.0  # 485s in -- inside the 60s margin now
    rotated = await issuer.issue()
    assert rotated != first


async def test_missing_private_key_secret_fails_closed() -> None:
    api = FakeGitHubAPI()
    issuer = AppJwtIssuer(_config(api), EnvSecretProvider({}))
    with pytest.raises(CheckoutError) as excinfo:
        await issuer.issue()
    assert excinfo.value.code is CheckoutFailureCode.CONFIG_MISSING
    # The failure is not cached either -- a later fixed provider is a new call,
    # and a second attempt against the same empty provider fails again.
    with pytest.raises(CheckoutError):
        await issuer.issue()


async def test_concurrent_issue_calls_share_one_resolve_and_sign() -> None:
    api = FakeGitHubAPI()
    provider = _CountingProvider(api.private_key_pem)
    issuer = AppJwtIssuer(_config(api), provider)
    tokens = await asyncio.gather(*(issuer.issue() for _ in range(10)))
    assert len(set(tokens)) == 1
    assert provider.calls == 1
