"""Bootstrap wiring for the GitHub App integration (construct-when-enabled)."""

from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from tests.service.helpers import agent_graph, deploy_service
from zeroth.service.app import create_app
from zeroth.service.bootstrap.container import DeploymentBootstrapError
from zeroth.service.bootstrap.factory import bootstrap_scoped_service
from zeroth.service.github.janitor import GitHubMaintenanceWorker
from zeroth.service.github.repository import SQLiteGitHubRepository
from zeroth.service.github.service import GitHubIntegrationService
from zeroth.service.github.webhook_receiver import GITHUB_WEBHOOK_PATH
from zeroth.service.repositories.repository import (
    SQLiteRepoCheckoutRepository,
    SQLiteRepoRunRepository,
)
from zeroth.service.repositories.service import RepositoryUnitService
from zeroth.service.repositories.worker import RepoRunWorker
from zeroth.service.webhooks.signing import sign_payload

GITHUB_FIELDS = (
    "github_repository",
    "github_client",
    "github_token_broker",
    "github_integration_service",
    "github_maintenance_worker",
    "github_webhook_secret_resolver",
    # ZER-37 orchestration glue rides the same enabled flag.
    "repo_checkout_repository",
    "repo_run_repository",
    "repository_unit_service",
    "repo_run_worker",
)


def _reset_settings(monkeypatch) -> None:
    import zeroth.platform.config.settings as settings_module

    # This module deliberately discards the singleton after the suite-level
    # explicit test secret is installed. Keep the reconstructed app equally
    # explicit instead of falling back to development-only ephemeral signing.
    monkeypatch.setenv(
        "ZEROTH_AUTH__BROWSER_SESSION_SECRET",
        "test-github-browser-session-secret-32-bytes",
    )
    monkeypatch.setattr(settings_module, "_settings_singleton", None)


def _enable_github(monkeypatch) -> None:
    monkeypatch.setenv("ZEROTH_GITHUB__ENABLED", "true")
    monkeypatch.setenv("ZEROTH_GITHUB__APP_ID", "424242")
    # The env-backed secret provider resolves the logical webhook secret name
    # through its normalized token form.
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "hook-secret")
    _reset_settings(monkeypatch)


async def _bootstrap(sqlite_db, *, graph_id: str, tenant_id: str = "default"):
    service, _ = await deploy_service(
        sqlite_db, agent_graph(graph_id=graph_id), tenant_id=tenant_id
    )
    return await bootstrap_scoped_service(
        sqlite_db,
        deployment_ref=service.deployment.deployment_ref,
        tenant_id=service.deployment.tenant_id,
        auth_config=service.auth_config,
    )


async def test_disabled_by_default_builds_nothing_and_registers_no_route(
    sqlite_db, monkeypatch
) -> None:
    _reset_settings(monkeypatch)
    bootstrap = await _bootstrap(sqlite_db, graph_id="graph-gh-disabled")

    for field in GITHUB_FIELDS:
        assert getattr(bootstrap, field) is None, field

    app = create_app(bootstrap)
    with TestClient(app) as client:
        # The path bypasses credential auth but no route exists: plain 404.
        response = client.post(GITHUB_WEBHOOK_PATH, content=b"{}")
        assert response.status_code == 404

    async with app.router.lifespan_context(app):
        names = {task.get_name() for task in asyncio.all_tasks() if not task.done()}
        assert "github-maintenance" not in names
        assert "repo-run-worker" not in names


async def test_enabled_constructs_components_route_and_janitor(
    sqlite_db, monkeypatch
) -> None:
    _enable_github(monkeypatch)
    bootstrap = await _bootstrap(sqlite_db, graph_id="graph-gh-enabled")

    assert isinstance(bootstrap.github_repository, SQLiteGitHubRepository)
    assert isinstance(bootstrap.github_integration_service, GitHubIntegrationService)
    assert isinstance(bootstrap.github_maintenance_worker, GitHubMaintenanceWorker)
    assert bootstrap.github_client is not None
    assert bootstrap.github_token_broker is not None
    assert bootstrap.github_integration_service.tenant_id == "default"
    assert await bootstrap.github_webhook_secret_resolver() == "hook-secret"

    # ZER-37 orchestration glue is constructed alongside the integration.
    assert isinstance(bootstrap.repo_checkout_repository, SQLiteRepoCheckoutRepository)
    assert isinstance(bootstrap.repo_run_repository, SQLiteRepoRunRepository)
    assert isinstance(bootstrap.repository_unit_service, RepositoryUnitService)
    assert isinstance(bootstrap.repo_run_worker, RepoRunWorker)

    app = create_app(bootstrap)

    # The janitor and repo-run worker start with the lifespan and stop with it.
    async with app.router.lifespan_context(app):
        tasks = [
            task
            for task in asyncio.all_tasks()
            if task.get_name() in {"github-maintenance", "repo-run-worker"}
            and not task.done()
        ]
        assert {task.get_name() for task in tasks} == {
            "github-maintenance",
            "repo-run-worker",
        }
    assert all(task.done() for task in tasks)

    # The route is live: a signed delivery lands without Zeroth credentials.
    with TestClient(app) as client:
        body = json.dumps(
            {
                "action": "created",
                "installation": {
                    "id": 88,
                    "account": {"login": "acme", "type": "Organization"},
                    "repository_selection": "all",
                },
            }
        ).encode()
        response = client.post(
            GITHUB_WEBHOOK_PATH,
            content=body,
            headers={
                "X-Hub-Signature-256": f"sha256={sign_payload(body, 'hook-secret')}",
                "X-GitHub-Delivery": uuid4().hex,
                "X-GitHub-Event": "installation",
            },
        )
        assert response.status_code == 202
        unsigned = client.post(GITHUB_WEBHOOK_PATH, content=body)
        assert unsigned.status_code == 401
    record = await bootstrap.github_repository.get_installation("default", 88)
    assert record is not None


async def test_enabled_without_git_binary_fails_bootstrap_closed(
    sqlite_db, monkeypatch
) -> None:
    _enable_github(monkeypatch)
    service, _ = await deploy_service(
        sqlite_db, agent_graph(graph_id="graph-gh-nogit"), tenant_id="default"
    )
    import zeroth.service.bootstrap.factory as factory_module

    monkeypatch.setattr(factory_module.shutil, "which", lambda _command: None)

    with pytest.raises(DeploymentBootstrapError, match="git"):
        await bootstrap_scoped_service(
            sqlite_db,
            deployment_ref=service.deployment.deployment_ref,
            tenant_id=service.deployment.tenant_id,
            auth_config=service.auth_config,
        )


async def test_janitor_sweep_prunes_only_expired_deliveries(sqlite_db) -> None:
    from datetime import timedelta

    repository = SQLiteGitHubRepository(sqlite_db)
    worker = GitHubMaintenanceWorker(
        repository, tenant_id="default", delivery_retention=timedelta(days=7)
    )
    await repository.record_delivery(
        "default", "fresh-guid", event="push", action=None, installation_id=None
    )
    assert await worker.sweep_once() == 0
    # The fresh delivery still dedups after the sweep.
    assert not await repository.record_delivery(
        "default", "fresh-guid", event="push", action=None, installation_id=None
    )


async def test_janitor_expires_stale_checkouts_and_removes_their_staging_dirs(
    sqlite_db, tmp_path
) -> None:
    from datetime import timedelta

    from zeroth.integrations.github.models import InstallationState, RepositoryGrant
    from zeroth.platform.primitives import utc_now
    from zeroth.service.repositories.repo_models import RepoCheckout, RepoCheckoutState

    repository = SQLiteGitHubRepository(sqlite_db)
    installation = await repository.upsert_installation(
        "default",
        installation_id=1,
        account_login="acme",
        account_type="Organization",
        repository_selection="all",
        status=InstallationState.ACTIVE,
    )
    grant = await repository.upsert_repository(
        "default",
        installation_pk=installation.id,
        grant=RepositoryGrant(
            repo_id=1000,
            owner="acme",
            name="widgets",
            full_name="acme/widgets",
            private=False,
            default_branch="main",
        ),
    )
    checkout_repository = SQLiteRepoCheckoutRepository(sqlite_db)
    staging_root = tmp_path / "stages"
    stale_dir = staging_root / "default" / "stale-checkout"
    stale_dir.mkdir(parents=True)
    (stale_dir / "scripts").mkdir()
    (stale_dir / ".zeroth.yaml").write_text("schema_version: 1\n")
    outside_dir = tmp_path / "not-a-stage"
    outside_dir.mkdir()

    stale = await checkout_repository.create(
        RepoCheckout(
            tenant_id="default",
            repository_pk=grant.id,
            installation_id=1,
            repository_id=1000,
            repository_full_name="acme/widgets",
            requested_ref="main",
            state=RepoCheckoutState.STAGED,
            staged_path=str(stale_dir),
            expires_at=utc_now() - timedelta(hours=1),
        )
    )
    # A FAILED row whose recorded path escapes the staging root is left alone.
    escaping = await checkout_repository.create(
        RepoCheckout(
            tenant_id="default",
            repository_pk=grant.id,
            installation_id=1,
            repository_id=1000,
            repository_full_name="acme/widgets",
            requested_ref="main",
            state=RepoCheckoutState.FAILED,
            staged_path=str(outside_dir),
        )
    )

    worker = GitHubMaintenanceWorker(
        repository,
        tenant_id="default",
        checkout_repository=checkout_repository,
        staging_root=staging_root,
    )
    await worker.sweep_once()

    swept = await checkout_repository.get("default", stale.id)
    assert swept is not None and swept.state is RepoCheckoutState.EXPIRED
    assert not stale_dir.exists()
    untouched = await checkout_repository.get("default", escaping.id)
    assert untouched is not None and untouched.state is RepoCheckoutState.FAILED
    assert outside_dir.exists()
