"""GitHub webhook receiver: HMAC gate, dedup, event dispatch, and auth matrix."""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.github.fake_github import FakeGitHubAPI
from tests.service.helpers import default_service_auth_config
from zeroth.integrations.github.app_jwt import AppJwtIssuer
from zeroth.integrations.github.client import GitHubAppClient
from zeroth.integrations.github.config import GitHubAppConfig
from zeroth.integrations.github.models import (
    InstallationRevokedError,
    InstallationState,
    RepositoryState,
)
from zeroth.integrations.github.token_broker import InstallationTokenBroker
from zeroth.platform.secrets.provider import EnvSecretProvider
from zeroth.service.api.authentication import ServiceAuthenticator
from zeroth.service.app import create_app
from zeroth.service.github.repository import SQLiteGitHubRepository
from zeroth.service.github.service import GitHubIntegrationService
from zeroth.service.github.webhook_receiver import (
    GITHUB_WEBHOOK_PATH,
    register_github_webhook_route,
)
from zeroth.service.webhooks.signing import sign_payload, verify_signature

BASE_URL = "http://api.github.test"
SECRET = "hook-secret"


def _rig(sqlite_db) -> tuple[FakeGitHubAPI, GitHubIntegrationService]:
    api = FakeGitHubAPI()
    transport = api.transport()
    config = GitHubAppConfig(app_id=api.app_id, api_base_url=BASE_URL)
    issuer = AppJwtIssuer(
        config, EnvSecretProvider({"GITHUB_APP_PRIVATE_KEY": api.private_key_pem})
    )
    client = GitHubAppClient(config, issuer, transport=transport)
    broker = InstallationTokenBroker(client)
    service = GitHubIntegrationService(
        SQLiteGitHubRepository(sqlite_db),
        client,
        broker,
        config=config,
        jwt_issuer=issuer,
        tenant_id="default",
        transport=transport,
    )
    return api, service


def _receiver_app(service: GitHubIntegrationService, *, secret: str | None = SECRET) -> FastAPI:
    """A bare app with just the receiver route (no middleware in the way)."""
    app = FastAPI()

    async def resolver() -> str | None:
        return secret

    register_github_webhook_route(app, service, resolver)
    return app


def _post(client, payload: dict, *, event: str, secret: str = SECRET, guid: str | None = None,
          signature: str | None = "valid"):
    body = json.dumps(payload).encode()
    headers = {
        "X-GitHub-Delivery": guid or uuid4().hex,
        "X-GitHub-Event": event,
    }
    if signature == "valid":
        headers["X-Hub-Signature-256"] = f"sha256={sign_payload(body, secret)}"
    elif signature is not None:
        headers["X-Hub-Signature-256"] = signature
    return client.post(GITHUB_WEBHOOK_PATH, content=body, headers=headers)


def _created_payload(installation_id: int = 501) -> dict:
    return {
        "action": "created",
        "installation": {
            "id": installation_id,
            "account": {"login": "acme", "type": "Organization"},
            "repository_selection": "selected",
        },
        "repositories": [
            {"id": 9001, "name": "repo-one", "full_name": "acme/repo-one", "private": True}
        ],
    }


# -- signature gate ------------------------------------------------------------


def test_verify_signature_accepts_only_the_correct_digest() -> None:
    body = b'{"zen": "Keep it logically awesome."}'
    good = f"sha256={sign_payload(body, SECRET)}"
    assert verify_signature(body, SECRET, good)
    assert not verify_signature(body, SECRET, None)
    assert not verify_signature(body, SECRET, "")
    assert not verify_signature(body, SECRET, "sha256=")
    assert not verify_signature(body, SECRET, "sha1=deadbeef")
    assert not verify_signature(body, SECRET, good.replace("sha256=", "sha256=00"))
    assert not verify_signature(body, "other-secret", good)
    assert not verify_signature(b"tampered", SECRET, good)


async def test_valid_created_delivery_upserts_pending_claim(sqlite_db) -> None:
    _, service = _rig(sqlite_db)
    client = TestClient(_receiver_app(service))

    response = _post(client, _created_payload(), event="installation")

    assert response.status_code == 202
    record = await service.repository.get_installation("default", 501)
    assert record is not None
    assert record.status is InstallationState.PENDING_CLAIM
    assert record.account_login == "acme"
    grants = await service.repository.list_repositories("default", record.id)
    assert [row.full_name for row in grants] == ["acme/repo-one"]


@pytest.mark.parametrize("signature", [None, "sha256=deadbeef", "garbage"])
async def test_bad_or_missing_signature_is_generic_401_with_no_state_change(
    sqlite_db, signature
) -> None:
    _, service = _rig(sqlite_db)
    client = TestClient(_receiver_app(service))
    guid = uuid4().hex

    response = _post(
        client, _created_payload(), event="installation", guid=guid, signature=signature
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "unauthorized"}
    assert await service.repository.get_installation("default", 501) is None
    # The body was never parsed and the GUID never consumed: the same delivery
    # with a valid signature is fresh, not a duplicate.
    retry = _post(client, _created_payload(), event="installation", guid=guid)
    assert retry.status_code == 202
    assert retry.json() == {"status": "accepted"}


async def test_missing_secret_fails_closed_with_401(sqlite_db) -> None:
    _, service = _rig(sqlite_db)
    client = TestClient(_receiver_app(service, secret=None))

    response = _post(client, _created_payload(), event="installation")

    assert response.status_code == 401
    assert response.json() == {"detail": "unauthorized"}
    assert await service.repository.get_installation("default", 501) is None


# -- dedup ---------------------------------------------------------------------


async def test_duplicate_delivery_guid_is_a_202_no_op(sqlite_db) -> None:
    _, service = _rig(sqlite_db)
    client = TestClient(_receiver_app(service))
    guid = uuid4().hex

    assert _post(client, _created_payload(), event="installation", guid=guid).status_code == 202
    # Replaying the SAME guid with a state-changing payload must not apply it.
    replay = _post(
        client,
        {"action": "suspend", "installation": {"id": 501}},
        event="installation",
        guid=guid,
    )

    assert replay.status_code == 202
    assert replay.json() == {"status": "duplicate"}
    record = await service.repository.get_installation("default", 501)
    assert record is not None
    assert record.status is InstallationState.PENDING_CLAIM


# -- event dispatch ------------------------------------------------------------


async def test_suspend_and_unsuspend_transition_status(sqlite_db) -> None:
    _, service = _rig(sqlite_db)
    client = TestClient(_receiver_app(service))
    _post(client, _created_payload(), event="installation")

    _post(client, {"action": "suspend", "installation": {"id": 501}}, event="installation")
    record = await service.repository.get_installation("default", 501)
    assert record is not None and record.status is InstallationState.SUSPENDED

    _post(client, {"action": "unsuspend", "installation": {"id": 501}}, event="installation")
    record = await service.repository.get_installation("default", 501)
    assert record is not None and record.status is InstallationState.ACTIVE


async def test_deleted_cascades_revocation_and_calls_on_revoked(sqlite_db) -> None:
    _, service = _rig(sqlite_db)
    revoked: list[int] = []
    app = FastAPI()

    async def resolver() -> str | None:
        return SECRET

    register_github_webhook_route(app, service, resolver, on_revoked=revoked.append)
    client = TestClient(app)
    _post(client, _created_payload(), event="installation")
    installation = await service.repository.get_installation("default", 501)
    assert installation is not None

    response = _post(
        client, {"action": "deleted", "installation": {"id": 501}}, event="installation"
    )

    assert response.status_code == 202
    record = await service.repository.get_installation("default", 501)
    assert record is not None and record.status is InstallationState.REVOKED
    grants = await service.repository.list_repositories("default", installation.id)
    assert {row.status for row in grants} == {RepositoryState.REMOVED}
    assert revoked == [501]


async def test_installation_repositories_added_and_removed(sqlite_db) -> None:
    _, service = _rig(sqlite_db)
    client = TestClient(_receiver_app(service))
    _post(client, _created_payload(), event="installation")
    installation = await service.repository.get_installation("default", 501)
    assert installation is not None

    _post(
        client,
        {
            "action": "added",
            "installation": {"id": 501},
            "repositories_added": [
                {"id": 9002, "name": "repo-two", "full_name": "acme/repo-two", "private": False}
            ],
            "repositories_removed": [],
        },
        event="installation_repositories",
    )
    names = [
        row.full_name
        for row in await service.repository.list_repositories("default", installation.id)
        if row.status is RepositoryState.ACTIVE
    ]
    assert names == ["acme/repo-one", "acme/repo-two"]

    _post(
        client,
        {
            "action": "removed",
            "installation": {"id": 501},
            "repositories_added": [],
            "repositories_removed": [
                {"id": 9001, "name": "repo-one", "full_name": "acme/repo-one", "private": True}
            ],
        },
        event="installation_repositories",
    )
    removed = await service.repository.get_repository("default", installation.id, 9001)
    assert removed is not None and removed.status is RepositoryState.REMOVED


async def test_unclaimed_installation_event_is_recorded_but_ignored(sqlite_db) -> None:
    _, service = _rig(sqlite_db)
    client = TestClient(_receiver_app(service))
    guid = uuid4().hex

    response = _post(
        client,
        {"action": "suspend", "installation": {"id": 777}},
        event="installation",
        guid=guid,
    )

    assert response.status_code == 202
    assert response.json() == {"status": "accepted"}
    assert await service.repository.get_installation("default", 777) is None
    # The delivery itself was recorded: a replay is recognized as a duplicate.
    replay = _post(
        client,
        {"action": "suspend", "installation": {"id": 777}},
        event="installation",
        guid=guid,
    )
    assert replay.json() == {"status": "duplicate"}


async def test_push_event_is_recorded_and_ignored(sqlite_db) -> None:
    _, service = _rig(sqlite_db)
    client = TestClient(_receiver_app(service))
    guid = uuid4().hex

    response = _post(
        client,
        {"ref": "refs/heads/main", "installation": {"id": 501}},
        event="push",
        guid=guid,
    )

    assert response.status_code == 202
    assert await service.repository.list_installations("default") == []
    replay = _post(
        client,
        {"ref": "refs/heads/main", "installation": {"id": 501}},
        event="push",
        guid=guid,
    )
    assert replay.json() == {"status": "duplicate"}


async def test_malformed_payload_and_missing_headers_are_400(sqlite_db) -> None:
    _, service = _rig(sqlite_db)
    client = TestClient(_receiver_app(service))

    body = b"not-json"
    response = client.post(
        GITHUB_WEBHOOK_PATH,
        content=body,
        headers={
            "X-Hub-Signature-256": f"sha256={sign_payload(body, SECRET)}",
            "X-GitHub-Delivery": uuid4().hex,
            "X-GitHub-Event": "installation",
        },
    )
    assert response.status_code == 400

    body = b"{}"
    response = client.post(
        GITHUB_WEBHOOK_PATH,
        content=body,
        headers={"X-Hub-Signature-256": f"sha256={sign_payload(body, SECRET)}"},
    )
    assert response.status_code == 400


# -- integration service lifecycle over the fake GitHub API --------------------


async def test_claim_installation_activates_and_syncs_repositories(sqlite_db) -> None:
    api, service = _rig(sqlite_db)
    api.add_installation(501, "acme")
    api.add_repo(501, "acme/repo-one", private=True)
    api.add_repo(501, "acme/repo-two")

    async with service._client:  # noqa: SLF001 - closes the governed test client
        record = await service.claim_installation("default", 501)
        assert record.status is InstallationState.ACTIVE
        assert record.last_verified_at is not None
        grants = await service.list_repositories("default", 501)
        assert [row.full_name for row in grants] == ["acme/repo-one", "acme/repo-two"]
        # The enumeration token was minted installation-wide and then revoked.
        assert api.token_records[-1].repositories is None
        assert api.revoked_tokens == [api.token_records[-1].token]

        # A repo dropped on GitHub's side is REMOVED by the next refresh.
        api.remove_repo(501, "acme/repo-two")
        await service.refresh_installation("default", 501)
        by_name = {
            row.full_name: row.status
            for row in await service.list_repositories("default", 501)
        }
        assert by_name["acme/repo-one"] is RepositoryState.ACTIVE
        assert by_name["acme/repo-two"] is RepositoryState.REMOVED


async def test_claim_of_revoked_installation_cascades_and_raises(sqlite_db) -> None:
    api, service = _rig(sqlite_db)
    api.add_installation(501, "acme")
    api.add_repo(501, "acme/repo-one")

    async with service._client:  # noqa: SLF001 - closes the governed test client
        record = await service.claim_installation("default", 501)
        api.revoke_installation(501)
        with pytest.raises(InstallationRevokedError):
            await service.refresh_installation("default", 501)

    revoked = await service.repository.get_installation("default", 501)
    assert revoked is not None and revoked.status is InstallationState.REVOKED
    grants = await service.repository.list_repositories("default", record.id)
    assert {row.status for row in grants} == {RepositoryState.REMOVED}


async def test_claim_of_suspended_installation_marks_suspended(sqlite_db) -> None:
    api, service = _rig(sqlite_db)
    api.add_installation(501, "acme")
    api.add_repo(501, "acme/repo-one")
    api.suspend(501)

    async with service._client:  # noqa: SLF001 - closes the governed test client
        record = await service.claim_installation("default", 501)

    assert record.status is InstallationState.SUSPENDED
    # No token mint was attempted against the suspended installation.
    assert api.minted_tokens == []


# -- full app: auth matrix stays intact ----------------------------------------


async def test_webhook_bypasses_credentials_while_the_api_stays_locked(sqlite_db) -> None:
    _, service = _rig(sqlite_db)

    async def resolver() -> str | None:
        return SECRET

    bootstrap = SimpleNamespace(
        authenticator=ServiceAuthenticator(default_service_auth_config()),
        audit_repository=None,
        deployment=None,
        regulus_client=None,
        langgraph_gateway_proxy=None,
        langgraph_gateway_websocket_handler=None,
        github_integration_service=service,
        github_webhook_secret_resolver=resolver,
    )
    app = create_app(bootstrap)
    client = TestClient(app)

    # The webhook path never asks for Zeroth credentials: a signed delivery
    # lands, an unsigned one is refused by the HMAC gate (401), never by the
    # credential middleware.
    assert _post(client, _created_payload(), event="installation").status_code == 202
    unsigned = _post(client, _created_payload(), event="installation", signature=None)
    assert unsigned.status_code == 401
    assert unsigned.json() == {"detail": "unauthorized"}

    # Every other route still authenticates: uncredentialed requests are 401.
    assert client.get("/v1/audits").status_code == 401
    assert client.get("/v1/deployments").status_code == 401
    assert client.post("/v1/runs").status_code == 401
