"""GitHubAppClient against the in-process fake: endpoint mapping + error discipline."""

from __future__ import annotations

import time

import pytest

from tests.github.fake_github import FakeGitHubAPI
from zeroth.integrations.github.app_jwt import AppJwtIssuer
from zeroth.integrations.github.client import GitHubAppClient
from zeroth.integrations.github.config import GitHubAppConfig
from zeroth.integrations.github.models import (
    GitHubApiError,
    InstallationRevokedError,
    InstallationSuspendedError,
    InstallationTokenRejectedError,
    RefNotFoundError,
    RepoOutOfScopeError,
)
from zeroth.platform.secrets.provider import EnvSecretProvider

BASE_URL = "http://api.github.test"
HEAD_SHA = "a" * 40


def _standard_api() -> FakeGitHubAPI:
    api = FakeGitHubAPI()
    api.add_installation(1, "acme")
    api.add_repo(1, "acme/widgets", private=True)
    api.set_ref("acme/widgets", "heads/main", HEAD_SHA)
    api.set_ref("acme/widgets", "tags/v1", HEAD_SHA)
    return api


def _client(api: FakeGitHubAPI) -> GitHubAppClient:
    config = GitHubAppConfig(app_id=api.app_id, api_base_url=BASE_URL)
    issuer = AppJwtIssuer(
        config, EnvSecretProvider({"GITHUB_APP_PRIVATE_KEY": api.private_key_pem})
    )
    return GitHubAppClient(config, issuer, transport=api.transport())


async def test_get_installation_returns_live_state() -> None:
    api = _standard_api()
    async with _client(api) as client:
        data = await client.get_installation(1)
    assert data["id"] == 1
    assert data["suspended_at"] is None


async def test_get_installation_404_maps_to_revoked() -> None:
    api = _standard_api()
    api.revoke_installation(1)
    async with _client(api) as client:
        with pytest.raises(InstallationRevokedError) as excinfo:
            await client.get_installation(1)
    assert excinfo.value.code.value == "installation_revoked"


async def test_mint_installation_token_returns_token_and_expiry() -> None:
    api = _standard_api()
    async with _client(api) as client:
        token, expires_at = await client.mint_installation_token(1, "widgets")
    assert token == "ghs_fake_1"
    assert expires_at > time.time()
    assert api.minted_tokens == [token]


async def test_mint_403_suspended_maps_without_echoing_the_body() -> None:
    api = _standard_api()
    api.suspend(1)
    async with _client(api) as client:
        with pytest.raises(InstallationSuspendedError) as excinfo:
            await client.mint_installation_token(1, "widgets")
    # The fake's body says "This installation has been suspended" -- the typed
    # error must use its own template, never the response text.
    assert "This installation has been suspended" not in str(excinfo.value)


async def test_mint_403_out_of_scope_repo() -> None:
    api = _standard_api()
    async with _client(api) as client:
        with pytest.raises(RepoOutOfScopeError):
            await client.mint_installation_token(1, "not-granted")


async def test_mint_on_revoked_installation() -> None:
    api = _standard_api()
    api.revoke_installation(1)
    async with _client(api) as client:
        with pytest.raises(InstallationRevokedError):
            await client.mint_installation_token(1, "widgets")


async def test_list_installation_repositories() -> None:
    api = _standard_api()
    async with _client(api) as client:
        token, _ = await client.mint_installation_token(1, "widgets")
        grants = await client.list_installation_repositories(token)
    assert [grant.full_name for grant in grants] == ["acme/widgets"]
    assert grants[0].private is True


async def test_bad_installation_token_maps_to_rejection() -> None:
    api = _standard_api()
    async with _client(api) as client:
        with pytest.raises(InstallationTokenRejectedError) as excinfo:
            await client.list_installation_repositories("ghs_wrong")
    assert excinfo.value.status_code == 401
    assert "ghs_wrong" not in str(excinfo.value)


async def test_get_repository_returns_grant() -> None:
    api = _standard_api()
    async with _client(api) as client:
        token, _ = await client.mint_installation_token(1, "widgets")
        grant = await client.get_repository(token, "acme", "widgets")
    assert grant.repo_id == 1000
    assert grant.owner == "acme"
    assert grant.name == "widgets"
    assert grant.default_branch == "main"


async def test_get_repository_outside_grants() -> None:
    api = _standard_api()
    async with _client(api) as client:
        token, _ = await client.mint_installation_token(1, "widgets")
        with pytest.raises(RepoOutOfScopeError):
            await client.get_repository(token, "acme", "other")


async def test_resolve_ref_branch_tag_and_pinned_sha() -> None:
    api = _standard_api()
    async with _client(api) as client:
        token, _ = await client.mint_installation_token(1, "widgets")
        assert await client.resolve_ref(token, "acme", "widgets", "main") == HEAD_SHA
        assert await client.resolve_ref(token, "acme", "widgets", "v1") == HEAD_SHA
        assert await client.resolve_ref(token, "acme", "widgets", HEAD_SHA) == HEAD_SHA


async def test_resolve_ref_unknown_ref_and_unknown_sha() -> None:
    api = _standard_api()
    async with _client(api) as client:
        token, _ = await client.mint_installation_token(1, "widgets")
        with pytest.raises(RefNotFoundError):
            await client.resolve_ref(token, "acme", "widgets", "does-not-exist")
        with pytest.raises(RefNotFoundError):
            await client.resolve_ref(token, "acme", "widgets", "b" * 40)


async def test_revoke_installation_token_is_best_effort() -> None:
    api = _standard_api()
    async with _client(api) as client:
        token, _ = await client.mint_installation_token(1, "widgets")
        await client.revoke_installation_token(token)
        # And revoking garbage swallows the 401 silently.
        await client.revoke_installation_token("ghs_wrong")
    assert api.revoked_tokens == [token]


async def test_api_error_messages_never_embed_response_bodies() -> None:
    api = _standard_api()
    async with _client(api) as client:
        api.fail_next(500)
        with pytest.raises(GitHubApiError) as excinfo:
            await client.get_installation(1)
    message = str(excinfo.value)
    assert "injected failure" not in message  # the canary planted by the fake
    assert "500" in message
