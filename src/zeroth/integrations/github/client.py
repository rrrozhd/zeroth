"""Typed async client over the GitHub App REST surface.

Every method maps HTTP outcomes onto the checkout failure vocabulary and never
lets a response body, credential, or URL-with-secret reach an exception
message: error text is always a fixed template over the status code and an
operation label. App endpoints authenticate with a ``Bearer <app-jwt>``;
installation endpoints with a ``Bearer <installation-token>``.

The underlying :class:`httpx.AsyncClient` comes from the governed factory
(:func:`zeroth.integrations.http.factory.governed_async_client`), which owns
the mandatory timeout and the shared connection lifecycle -- this module never
constructs a transport client directly.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import httpx

from zeroth.integrations.github.app_jwt import AppJwtIssuer
from zeroth.integrations.github.config import GitHubAppConfig
from zeroth.integrations.github.models import (
    CheckoutError,
    CheckoutFailureCode,
    GitHubApiError,
    InstallationRevokedError,
    InstallationSuspendedError,
    InstallationTokenRejectedError,
    RefNotFoundError,
    RepoOutOfScopeError,
    RepositoryGrant,
)
from zeroth.integrations.http.factory import aclose_all, governed_async_client

_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_DEFAULT_TIMEOUT_SECONDS = 30.0


def _epoch_from_iso(value: str) -> float:
    """Parse GitHub's ISO-8601 ``...Z`` timestamps into epoch seconds."""
    return datetime.fromisoformat(value).timestamp()


class GitHubAppClient:
    """GitHub App REST client with typed, redaction-safe error mapping."""

    def __init__(
        self,
        config: GitHubAppConfig,
        jwt_issuer: AppJwtIssuer,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._config = config
        self._jwt_issuer = jwt_issuer
        self._transport = transport
        self._timeout_seconds = timeout_seconds

    async def _http(self) -> httpx.AsyncClient:
        """Return the governed, shared client for the GitHub API purpose."""
        return await governed_async_client(
            purpose="github-app",
            timeout=self._timeout_seconds,
            base_url=self._config.api_base_url,
            transport=self._transport,
        )

    async def aclose(self) -> None:
        """Close this loop's governed client cache (test/teardown convenience).

        The shared client's lifecycle belongs to the governed cache; a service
        deployment closes it through the factory's ``aclose_all(app)`` instead.
        """
        await aclose_all()

    async def __aenter__(self) -> GitHubAppClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    # -- request plumbing ------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        bearer: str,
        operation: str,
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Perform one request; transport failures map to a template ``api_error``."""
        try:
            client = await self._http()
            return await client.request(
                method,
                path,
                json=json_body,
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {bearer}",
                },
            )
        except httpx.HTTPError as exc:
            raise CheckoutError(
                CheckoutFailureCode.API_ERROR,
                f"github api request failed during {operation}",
            ) from exc

    async def _app_request(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Perform one App-JWT-authenticated request."""
        app_jwt = await self._jwt_issuer.issue()
        return await self._request(
            method, path, bearer=app_jwt, operation=operation, json_body=json_body
        )

    # -- App endpoints ---------------------------------------------------------

    async def get_installation(self, installation_id: int) -> dict[str, Any]:
        """Fetch one installation's live state (App JWT auth).

        Returns:
            The installation JSON object as GitHub shapes it (``id``,
            ``account``, ``suspended_at``, ...).

        Raises:
            InstallationRevokedError: The installation no longer exists (404).
            GitHubApiError: Any other non-200 answer, with only the status code.
        """
        operation = "installation lookup"
        response = await self._app_request(
            "GET", f"/app/installations/{installation_id}", operation=operation
        )
        if response.status_code == 404:
            raise InstallationRevokedError()
        if response.status_code != 200:
            raise GitHubApiError(response.status_code, operation)
        return response.json()

    async def mint_installation_token(
        self, installation_id: int, repository_name: str
    ) -> tuple[str, float]:
        """Mint an installation token scoped to one repository (App JWT auth).

        Returns:
            ``(token, expires_at_epoch_seconds)``.

        Raises:
            InstallationRevokedError: The installation no longer exists (404).
            InstallationSuspendedError: GitHub refused because the installation
                is suspended (403 with a suspension reason).
            RepoOutOfScopeError: GitHub refused the repository scope (other 403).
            GitHubApiError: Any other non-201 answer.
        """
        operation = "installation token mint"
        response = await self._app_request(
            "POST",
            f"/app/installations/{installation_id}/access_tokens",
            operation=operation,
            json_body={"repositories": [repository_name]},
        )
        if response.status_code == 404:
            raise InstallationRevokedError()
        if response.status_code == 403:
            if self._mentions_suspension(response):
                raise InstallationSuspendedError()
            raise RepoOutOfScopeError()
        if response.status_code != 201:
            raise GitHubApiError(response.status_code, operation)
        payload = response.json()
        return payload["token"], _epoch_from_iso(payload["expires_at"])

    @staticmethod
    def _mentions_suspension(response: httpx.Response) -> bool:
        """Classify a 403 body without letting its text escape into errors."""
        try:
            message = response.json().get("message", "")
        except ValueError:
            return False
        return isinstance(message, str) and "suspend" in message.lower()

    # -- installation-token endpoints ------------------------------------------

    async def list_installation_repositories(
        self, installation_token: str
    ) -> list[RepositoryGrant]:
        """List the repositories the installation token can reach."""
        operation = "installation repository listing"
        response = await self._request(
            "GET", "/installation/repositories", bearer=installation_token, operation=operation
        )
        if response.status_code == 401:
            raise InstallationTokenRejectedError(operation)
        if response.status_code != 200:
            raise GitHubApiError(response.status_code, operation)
        return [
            self._grant_from_json(item) for item in response.json().get("repositories", [])
        ]

    async def get_repository(
        self, installation_token: str, owner: str, name: str
    ) -> RepositoryGrant:
        """Fetch one repository's metadata through the installation token.

        Raises:
            RepoOutOfScopeError: The token cannot see the repository (404).
            InstallationTokenRejectedError: The token itself was rejected (401).
        """
        operation = "repository lookup"
        response = await self._request(
            "GET", f"/repos/{owner}/{name}", bearer=installation_token, operation=operation
        )
        if response.status_code == 401:
            raise InstallationTokenRejectedError(operation)
        if response.status_code == 404:
            raise RepoOutOfScopeError()
        if response.status_code != 200:
            raise GitHubApiError(response.status_code, operation)
        return self._grant_from_json(response.json())

    async def resolve_ref(
        self, installation_token: str, owner: str, name: str, ref: str
    ) -> str:
        """Resolve a branch, tag, or full commit SHA to a verified commit SHA.

        A 40-hex ``ref`` is verified via the commits endpoint; anything else is
        tried as ``heads/{ref}`` then ``tags/{ref}``.

        Raises:
            RefNotFoundError: Nothing matched.
            InstallationTokenRejectedError: The token was rejected (401).
        """
        operation = "ref resolution"
        if _COMMIT_SHA.fullmatch(ref):
            response = await self._request(
                "GET",
                f"/repos/{owner}/{name}/git/commits/{ref}",
                bearer=installation_token,
                operation=operation,
            )
            if response.status_code == 401:
                raise InstallationTokenRejectedError(operation)
            if response.status_code == 404:
                raise RefNotFoundError()
            if response.status_code != 200:
                raise GitHubApiError(response.status_code, operation)
            return response.json()["sha"]
        for kind in ("heads", "tags"):
            response = await self._request(
                "GET",
                f"/repos/{owner}/{name}/git/ref/{kind}/{ref}",
                bearer=installation_token,
                operation=operation,
            )
            if response.status_code == 401:
                raise InstallationTokenRejectedError(operation)
            if response.status_code == 200:
                return response.json()["object"]["sha"]
            if response.status_code != 404:
                raise GitHubApiError(response.status_code, operation)
        raise RefNotFoundError()

    async def revoke_installation_token(self, installation_token: str) -> None:
        """Best-effort revocation of an installation token; failures are swallowed."""
        try:
            client = await self._http()
            await client.request(
                "DELETE",
                "/installation/token",
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {installation_token}",
                },
            )
        except httpx.HTTPError:
            return

    @staticmethod
    def _grant_from_json(item: dict[str, Any]) -> RepositoryGrant:
        """Project a GitHub repository object onto :class:`RepositoryGrant`."""
        full_name = item["full_name"]
        owner, _, name = full_name.partition("/")
        return RepositoryGrant(
            repo_id=item["id"],
            owner=owner,
            name=name,
            full_name=full_name,
            private=bool(item.get("private", False)),
            default_branch=item.get("default_branch", "main"),
        )


__all__ = ["GitHubAppClient"]
