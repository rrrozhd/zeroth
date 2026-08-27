"""Inbound GitHub App webhook surface, authenticated by HMAC signature.

The route is registered once, directly on the app (never on the ``/v1``
router or the unversioned compatibility block): GitHub cannot carry Zeroth
credentials, so the path is exempted from the credential middleware and
authenticated here instead, fail-closed, by ``X-Hub-Signature-256`` over the
raw body. The body is never parsed before the signature verifies. Payload
fields are attacker-influencable data even after verification, so every read
is type-guarded and nothing from the payload reaches a log or an error
message.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from zeroth.integrations.github.models import (
    InstallationState,
    RepositoryGrant,
    RepositoryState,
)
from zeroth.service.webhooks.signing import verify_signature

if TYPE_CHECKING:
    from zeroth.service.github.service import GitHubIntegrationService

logger = logging.getLogger(__name__)

GITHUB_WEBHOOK_PATH = "/integrations/github/webhook"

type SecretResolver = Callable[[], Awaitable[str | None]]
type RevocationHook = Callable[[int], None]

_UNAUTHORIZED = {"detail": "unauthorized"}


def register_github_webhook_route(
    app: FastAPI,
    service: GitHubIntegrationService,
    secret_resolver: SecretResolver,
    *,
    on_revoked: RevocationHook | None = None,
) -> None:
    """Register ``POST /integrations/github/webhook`` on the app.

    Args:
        app: The FastAPI application (the route goes directly on it).
        service: The tenant-bound integration service backing the dispatch.
        secret_resolver: Async resolver for the webhook secret; resolving to
            ``None`` fails every delivery closed with a generic 401.
        on_revoked: Optional hook invoked with the installation id after an
            ``installation deleted`` cascade (e.g. a broker cache drop).
    """

    @app.post(GITHUB_WEBHOOK_PATH, include_in_schema=False, name="github_webhook")
    async def github_webhook(request: Request) -> Response:
        """Verify, dedup, and dispatch one GitHub webhook delivery."""
        body = await request.body()
        secret = await secret_resolver()
        if not secret:
            # Fail closed with the same generic answer as a bad signature so
            # an unconfigured deployment discloses nothing.
            return JSONResponse(status_code=401, content=_UNAUTHORIZED)
        if not verify_signature(
            body, secret, request.headers.get("X-Hub-Signature-256")
        ):
            return JSONResponse(status_code=401, content=_UNAUTHORIZED)

        delivery_guid = request.headers.get("X-GitHub-Delivery")
        event = request.headers.get("X-GitHub-Event")
        if not delivery_guid or not event:
            return JSONResponse(
                status_code=400, content={"detail": "missing webhook headers"}
            )
        try:
            payload = json.loads(body)
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            return JSONResponse(
                status_code=400, content={"detail": "invalid webhook payload"}
            )

        action = payload.get("action")
        action = action if isinstance(action, str) else None
        installation_id = _installation_id(payload)
        fresh = await service.repository.record_delivery(
            service.tenant_id,
            delivery_guid,
            event=event,
            action=action,
            installation_id=installation_id,
        )
        if not fresh:
            return JSONResponse(status_code=202, content={"status": "duplicate"})
        await _dispatch(service, event, action, payload, on_revoked)
        return JSONResponse(status_code=202, content={"status": "accepted"})


def _installation_id(payload: dict[str, Any]) -> int | None:
    """Extract the installation id from a payload, or ``None`` when malformed."""
    installation = payload.get("installation")
    if not isinstance(installation, dict):
        return None
    installation_id = installation.get("id")
    return installation_id if isinstance(installation_id, int) else None


async def _dispatch(
    service: GitHubIntegrationService,
    event: str,
    action: str | None,
    payload: dict[str, Any],
    on_revoked: RevocationHook | None,
) -> None:
    """Apply one verified, deduplicated delivery to the persisted projection.

    Every handler is idempotent (upserts and status transitions), and an
    event addressing an installation this tenant does not track is recorded
    by the caller and ignored here. ``push`` and unrecognized events fall
    through: recorded, ignored.
    """
    if event == "installation":
        await _handle_installation(service, action, payload, on_revoked)
    elif event == "installation_repositories":
        await _handle_installation_repositories(service, payload)


async def _handle_installation(
    service: GitHubIntegrationService,
    action: str | None,
    payload: dict[str, Any],
    on_revoked: RevocationHook | None,
) -> None:
    """Apply an ``installation`` lifecycle event."""
    installation_id = _installation_id(payload)
    if installation_id is None:
        return
    tenant_id = service.tenant_id
    repository = service.repository
    if action == "created":
        data = payload.get("installation")
        data = data if isinstance(data, dict) else {}
        account = data.get("account")
        account = account if isinstance(account, dict) else {}
        existing = await repository.get_installation(tenant_id, installation_id)
        record = await repository.upsert_installation(
            tenant_id,
            installation_id=installation_id,
            account_login=str(account.get("login") or ""),
            account_type=str(account.get("type") or ""),
            repository_selection=str(data.get("repository_selection") or "all"),
            # A fresh install (or a re-install after revocation) waits for a
            # tenant claim; an already-tracked installation keeps its status.
            status=(
                InstallationState.PENDING_CLAIM
                if existing is None or existing.status is InstallationState.REVOKED
                else None
            ),
        )
        for grant in _grants(payload.get("repositories")):
            await repository.upsert_repository(
                tenant_id, installation_pk=record.id, grant=grant
            )
        return
    existing = await repository.get_installation(tenant_id, installation_id)
    if existing is None:
        # Unclaimed installation: the delivery is recorded, the event ignored.
        return
    if action == "deleted":
        await service.revoke_installation(tenant_id, installation_id)
        if on_revoked is not None:
            on_revoked(installation_id)
    elif action == "suspend":
        await repository.set_installation_status(
            tenant_id, installation_id, InstallationState.SUSPENDED
        )
    elif action == "unsuspend":
        await repository.set_installation_status(
            tenant_id, installation_id, InstallationState.ACTIVE
        )


async def _handle_installation_repositories(
    service: GitHubIntegrationService, payload: dict[str, Any]
) -> None:
    """Apply an ``installation_repositories`` added/removed event."""
    installation_id = _installation_id(payload)
    if installation_id is None:
        return
    tenant_id = service.tenant_id
    repository = service.repository
    existing = await repository.get_installation(tenant_id, installation_id)
    if existing is None:
        return
    for grant in _grants(payload.get("repositories_added")):
        await repository.upsert_repository(
            tenant_id, installation_pk=existing.id, grant=grant
        )
    for grant in _grants(payload.get("repositories_removed")):
        await repository.set_repository_status(
            tenant_id,
            installation_pk=existing.id,
            repo_id=grant.repo_id,
            status=RepositoryState.REMOVED,
        )


def _grants(items: Any) -> list[RepositoryGrant]:
    """Project webhook repository objects onto grants, skipping malformed items.

    Webhook payloads carry only ``id``/``name``/``full_name``/``private``;
    the default branch is unknown until a claim or refresh asks GitHub, so it
    is projected as ``main`` and corrected by the next live sync.
    """
    if not isinstance(items, list):
        return []
    grants: list[RepositoryGrant] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        repo_id = item.get("id")
        full_name = item.get("full_name")
        if not isinstance(repo_id, int) or not isinstance(full_name, str):
            continue
        owner, _, name = full_name.partition("/")
        fallback_name = item.get("name")
        grants.append(
            RepositoryGrant(
                repo_id=repo_id,
                owner=owner,
                name=name or (fallback_name if isinstance(fallback_name, str) else ""),
                full_name=full_name,
                private=bool(item.get("private", False)),
                default_branch="main",
            )
        )
    return grants


__all__ = ["GITHUB_WEBHOOK_PATH", "register_github_webhook_route"]
