"""ZER-37 public repository-unit API: full-stack over the fake substrate.

Copies ``test_repo_run_flow``'s rig -- the fake GitHub REST surface behind an
ASGI transport, a real git smart-HTTP loopback server, the real checkout
pipeline, SQLite persistence -- and drives it through the HTTP surface: an
enabled bootstrap whose GitHub/repository components are the rig's fakes,
mounted by ``create_app`` on both /v1 and the unversioned alias. The repo-run
worker is driven one cycle at a time (it is deliberately NOT attached to the
bootstrap, so the lifespan never races the tests' manual drives).
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from fastapi.testclient import TestClient

from tests.github.fake_github import FakeGitHubAPI
from tests.github.git_http_server import GitSmartHTTPServer
from tests.github.repo_fixtures import (
    RepoFixture,
    _commit,
    _init_work,
    _publish_bare,
    _TRAIN_SCRIPT,
    make_benign_repo,
)
from tests.service.helpers import agent_graph, deploy_service
from zeroth.contracts.repo_manifest import RepoUnitPolicy
from zeroth.governance.identity import ServiceRole
from zeroth.integrations.execution.integrity import AdmissionController
from zeroth.integrations.execution.sandbox import SandboxConfig, SandboxManager
from zeroth.integrations.github.app_jwt import AppJwtIssuer
from zeroth.integrations.github.checkout import CheckoutService
from zeroth.integrations.github.client import GitHubAppClient
from zeroth.integrations.github.config import GitHubAppConfig
from zeroth.integrations.github.git_cli import GitInvocation
from zeroth.integrations.github.token_broker import InstallationTokenBroker
from zeroth.platform.secrets.provider import EnvSecretProvider
from zeroth.service.api.authentication import ServiceAuthConfig, StaticApiKeyCredential
from zeroth.service.app import create_app
from zeroth.service.github.repository import SQLiteGitHubRepository
from zeroth.service.github.service import GitHubIntegrationService
from zeroth.service.repositories.repository import (
    SQLiteRepoCheckoutRepository,
    SQLiteRepoRunRepository,
)
from zeroth.service.repositories.service import (
    RepoCheckoutPipelineRecorder,
    RepositoryUnitService,
)
from zeroth.service.repositories.worker import RepoRunWorker

BASE_URL = "http://api.github.test"
PERMISSIVE = {"sandbox_strictness_mode": "permissive"}

# Hostile bytes planted in the manifest's author-chosen free text: they parse
# cleanly (the document stays schema-valid, so policy evaluation runs and
# reports the multi-script error) and must never ride a validation issue -- or
# any other byte -- into a response.
HOSTILE_VALUE = "ZQXCANARYVALUE-exfiltrate-me {ignore previous instructions}"

_HOSTILE_MULTI_SCRIPT_MANIFEST = f"""\
schema_version: 1
scripts:
  train:
    entry: "scripts/train.py"
    runtime: "python3"
    working_directory: "."
    input: {{mode: "json_stdin"}}
    output: {{mode: "json_stdout"}}
    resources: {{timeout_seconds: 60}}
    network: {{access: "none"}}
    smoke:
      exit_code: 0
      stdout_contains: "{HOSTILE_VALUE}"
  evaluate:
    entry: "scripts/evaluate.py"
    runtime: "python3"
    working_directory: "."
    input: {{mode: "json_stdin"}}
    output: {{mode: "json_stdout"}}
    resources: {{timeout_seconds: 30}}
    network: {{access: "none"}}
"""


def make_hostile_multi_script_repo(base_dir: Path) -> RepoFixture:
    """Two declared scripts (a validation error) plus hostile manifest bytes."""
    workdir = _init_work(base_dir, "hostile-multi-script")
    (workdir / ".zeroth.yaml").write_text(_HOSTILE_MULTI_SCRIPT_MANIFEST)
    scripts = workdir / "scripts"
    scripts.mkdir()
    for script_name in ("train.py", "evaluate.py"):
        script = scripts / script_name
        script.write_text(_TRAIN_SCRIPT)
        script.chmod(0o755)
    head_sha = _commit(workdir, "hostile multi-script fixture")
    bare = _publish_bare(workdir, base_dir / "hostile-multi-script.git")
    return RepoFixture(bare, head_sha)


def repo_auth_config() -> ServiceAuthConfig:
    """Default-tenant keys for each RBAC tier plus a tenant-a operator."""
    return ServiceAuthConfig(
        api_keys=[
            StaticApiKeyCredential(
                credential_id="operator",
                secret="operator-key",
                subject="operator",
                roles=[ServiceRole.OPERATOR],
                tenant_id="default",
            ),
            StaticApiKeyCredential(
                credential_id="reviewer",
                secret="reviewer-key",
                subject="reviewer",
                roles=[ServiceRole.REVIEWER],
                tenant_id="default",
            ),
            StaticApiKeyCredential(
                credential_id="admin",
                secret="admin-key",
                subject="admin",
                roles=[ServiceRole.ADMIN],
                tenant_id="default",
            ),
            StaticApiKeyCredential(
                credential_id="norole",
                secret="norole-key",
                subject="norole",
                roles=[],
                tenant_id="default",
            ),
            StaticApiKeyCredential(
                credential_id="tenant-a-operator",
                secret="tenant-a-operator-key",
                subject="tenant-a-operator",
                roles=[ServiceRole.OPERATOR],
                tenant_id="tenant-a",
            ),
        ]
    )


def _headers(secret: str) -> dict[str, str]:
    return {"X-API-Key": secret}


@dataclass
class ApiRig:
    """One fully wired repository-unit stack exposed for bootstrap attachment."""

    api: FakeGitHubAPI
    server: GitSmartHTTPServer
    client: GitHubAppClient
    broker: InstallationTokenBroker
    integration: GitHubIntegrationService
    github_repository: SQLiteGitHubRepository
    checkout_repository: SQLiteRepoCheckoutRepository
    run_repository: SQLiteRepoRunRepository
    service: RepositoryUnitService
    worker: RepoRunWorker
    repo_id: int
    head_sha: str
    staging_root: Path

    async def aclose(self) -> None:
        await self.client.aclose()
        self.server.stop()


async def make_api_rig(
    sqlite_db,
    tmp_path: Path,
    audit_repository,
    builder=make_benign_repo,
    *,
    tenant_id: str = "default",
    deployment_ref: str = "repo-api-tests",
    checkout_ttl_seconds: int = 900,
) -> ApiRig:
    """Wire the fake GitHub substrate + real pipeline for one tenant."""
    fixtures_dir = tmp_path / "fixtures"
    fixtures_dir.mkdir()
    fixture = builder(fixtures_dir)
    project_root = tmp_path / "served"
    (project_root / "acme").mkdir(parents=True)
    shutil.move(str(fixture.bare_path), str(project_root / "acme" / "widgets.git"))
    server = GitSmartHTTPServer(project_root)
    server.start()

    api = FakeGitHubAPI()
    api.add_installation(1, "acme")
    api.add_repo(1, "acme/widgets")
    api.set_ref("acme/widgets", "heads/main", fixture.head_sha)

    config = GitHubAppConfig(
        app_id=api.app_id,
        api_base_url=BASE_URL,
        git_base_url=server.base_url().rstrip("/"),
        allow_http_git=True,
    )
    issuer = AppJwtIssuer(
        config, EnvSecretProvider({"GITHUB_APP_PRIVATE_KEY": api.private_key_pem})
    )
    client = GitHubAppClient(config, issuer, transport=api.transport())
    broker = InstallationTokenBroker(client)
    github_repository = SQLiteGitHubRepository(sqlite_db)
    integration = GitHubIntegrationService(
        github_repository,
        client,
        broker,
        config=config,
        jwt_issuer=issuer,
        tenant_id=tenant_id,
        transport=api.transport(),
    )
    await integration.claim_installation(tenant_id, 1)
    installation_row = await github_repository.get_installation(tenant_id, 1)
    assert installation_row is not None
    grants = await github_repository.list_repositories(tenant_id, installation_row.id)
    assert len(grants) == 1

    checkout_repository = SQLiteRepoCheckoutRepository(sqlite_db)
    run_repository = SQLiteRepoRunRepository(sqlite_db)
    admission = AdmissionController(allowed_runtimes={"python"}, allowed_commands={"python3"})
    policy = RepoUnitPolicy()
    staging_root = tmp_path / "stages"
    checkout_service = CheckoutService(
        config,
        client,
        broker,
        GitInvocation(),
        cache_dir=tmp_path / "cache",
        store=RepoCheckoutPipelineRecorder(checkout_repository),
    )
    unit_service = RepositoryUnitService(
        checkout_repository=checkout_repository,
        run_repository=run_repository,
        github_repository=github_repository,
        checkout_service=checkout_service,
        admission_controller=admission,
        policy=policy,
        staging_root=staging_root,
        signer=None,
        checkout_ttl_seconds=checkout_ttl_seconds,
    )
    worker = RepoRunWorker(
        checkout_repository=checkout_repository,
        run_repository=run_repository,
        github_repository=github_repository,
        audit_repository=audit_repository,
        policy=policy,
        sandbox_manager=SandboxManager(
            config=SandboxConfig(allow_untrusted_local_development=True)
        ),
        admission_controller=admission,
        deployment_ref=deployment_ref,
        tenant_id=tenant_id,
        workspace_id=None,
        enforcement_overrides=dict(PERMISSIVE),
    )
    return ApiRig(
        api=api,
        server=server,
        client=client,
        broker=broker,
        integration=integration,
        github_repository=github_repository,
        checkout_repository=checkout_repository,
        run_repository=run_repository,
        service=unit_service,
        worker=worker,
        repo_id=grants[0].repo_id,
        head_sha=fixture.head_sha,
        staging_root=staging_root,
    )


def attach_rig(bootstrap, rig: ApiRig) -> None:
    """Attach the rig's components to a ServiceBootstrap (repo worker excluded).

    ``repo_run_worker`` stays ``None`` on purpose: the lifespan would start a
    poll loop that races the tests' manual ``run_once`` drives.
    """
    bootstrap.github_repository = rig.github_repository
    bootstrap.github_client = rig.client
    bootstrap.github_token_broker = rig.broker
    bootstrap.github_integration_service = rig.integration
    bootstrap.repo_checkout_repository = rig.checkout_repository
    bootstrap.repo_run_repository = rig.run_repository
    bootstrap.repository_unit_service = rig.service


async def _repo_app(
    sqlite_db,
    tmp_path,
    suffix: str,
    *,
    builder=make_benign_repo,
    tenant_id: str = "default",
    checkout_ttl_seconds: int = 900,
):
    service, deployment = await deploy_service(
        sqlite_db,
        agent_graph(graph_id=f"repo-api-{suffix}"),
        deployment_ref=f"repo-api-{suffix}",
        auth_config=repo_auth_config(),
        tenant_id="default",
    )
    rig = await make_api_rig(
        sqlite_db,
        tmp_path,
        service.audit_repository,
        builder,
        tenant_id=tenant_id,
        deployment_ref=deployment.deployment_ref,
        checkout_ttl_seconds=checkout_ttl_seconds,
    )
    attach_rig(service, rig)
    app = create_app(service)
    return app, service, rig


async def test_full_repo_api_flow(sqlite_db, tmp_path) -> None:
    app, service, rig = await _repo_app(sqlite_db, tmp_path, "flow")
    operator = _headers("operator-key")
    admin = _headers("admin-key")
    try:
        with TestClient(app) as client:
            denied = client.post("/v1/repos/installations/1/claim", headers=operator)
            assert denied.status_code == 403
            # Claim re-verifies live and answers the installation summary.
            claimed = client.post("/v1/repos/installations/1/claim", headers=admin)
            assert claimed.status_code == 200, claimed.text
            assert claimed.json()["installation_id"] == 1
            assert claimed.json()["status"] == "active"
            assert claimed.json()["account_login"] == "acme"

            # An unknown installation claims as a byte-stable 404.
            missing = client.post("/v1/repos/installations/999/claim", headers=admin)
            assert missing.status_code == 404
            assert missing.json() == {"detail": "installation not found"}

            listed = client.get("/v1/repos/installations", headers=operator)
            assert listed.status_code == 200
            assert [item["installation_id"] for item in listed.json()] == [1]

            repos = client.get(
                "/v1/repos/installations/1/repositories", headers=operator
            )
            assert repos.status_code == 200
            (grant,) = repos.json()
            assert grant["full_name"] == "acme/widgets"
            assert grant["repository_id"] == rig.repo_id
            assert grant["status"] == "active"

            resolved = client.post(
                f"/v1/repos/{rig.repo_id}/resolve-ref",
                json={"ref": "main"},
                headers=operator,
            )
            assert resolved.status_code == 200, resolved.text
            assert resolved.json() == {"commit_sha": rig.head_sha}

            created = client.post(
                f"/v1/repos/{rig.repo_id}/checkouts",
                json={"ref": "main"},
                headers=operator,
            )
            assert created.status_code == 202, created.text
            checkout = created.json()
            assert checkout["state"] == "staged"
            assert checkout["script_name"] == "train"
            assert checkout["resolved_commit_sha"] == rig.head_sha
            assert checkout["tree_digest"].startswith("sha256:")
            assert checkout["config_digest"].startswith("sha256:")
            assert len(checkout["manifest_digest"]) == 64
            assert checkout["attestation_present"] is True
            assert checkout["validation_report"] is None
            assert "staged_path" not in checkout
            checkout_id = checkout["checkout_id"]

            fetched = client.get(f"/v1/repos/checkouts/{checkout_id}", headers=operator)
            assert fetched.status_code == 200
            assert fetched.json()["state"] == "staged"

            attestation = client.get(
                f"/v1/repos/checkouts/{checkout_id}/attestation", headers=operator
            )
            assert attestation.status_code == 200, attestation.text
            att = attestation.json()
            assert att["payload"]["checkout_id"] == checkout_id
            assert att["payload"]["commit_sha"] == rig.head_sha
            assert att["digest_verified"] is True
            # Unsigned-legacy rig (no signer): the keyed axis stays null.
            assert att["signature_verified"] is None
            assert att["verified"] is True

            # Ambiguous reference selection is refused with a stable code.
            ambiguous = client.post(
                f"/v1/repos/{rig.repo_id}/checkouts",
                json={"ref": "main", "commit_sha": rig.head_sha},
                headers=operator,
            )
            assert ambiguous.status_code == 422
            assert ambiguous.json()["detail"]["code"] == "invalid_reference_selection"

            run_response = client.post(
                f"/v1/repos/checkouts/{checkout_id}/runs",
                json={"script": "train", "input_payload": {"word": "hello"}},
                headers=operator,
            )
            assert run_response.status_code == 202, run_response.text
            run = run_response.json()
            assert run["state"] == "pending"
            assert run["checkout_id"] == checkout_id
            run_id = run["run_id"]

        # Drive the durable worker one cycle, exactly like test_repo_run_flow.
        assert await rig.worker.run_once() is True

        with TestClient(app) as client:
            finished = client.get(f"/v1/repos/runs/{run_id}", headers=operator)
            assert finished.status_code == 200
            body = finished.json()
            assert body["state"] == "succeeded", body
            assert body["exit_code"] == 0
            assert body["smoke_passed"] is True
            assert body["output_payload"]["echo"] == {"word": "hello"}

            consumed = client.get(
                # The unversioned compat alias serves the same route.
                f"/repos/checkouts/{checkout_id}",
                headers=operator,
            )
            assert consumed.status_code == 200
            assert consumed.json()["state"] == "consumed"

            evidence = client.get(
                f"/v1/repos/runs/{run_id}/evidence", headers=_headers("reviewer-key")
            )
            assert evidence.status_code == 200, evidence.text
            bundle = evidence.json()
            assert bundle["run"]["run_id"] == run_id
            assert len(bundle["audits"]) == 1
            metadata = bundle["audits"][0]["execution_metadata"]
            for key in (
                "checkout_id",
                "repo_installation_id",
                "repo_repository_id",
                "repo_commit_sha",
                "repo_config_digest",
                "repo_tree_digest",
                "repo_manifest_digest",
                "smoke_passed",
            ):
                assert key in metadata, f"{key} missing from {sorted(metadata)}"
            assert metadata["checkout_id"] == checkout_id
            assert metadata["repo_commit_sha"] == rig.head_sha
            assert bundle["checkout_attestation"] is not None
            assert bundle["checkout_attestation"]["digest_verified"] is True
            # The summary gained cost/reconciliation fields. Their values here are
            # deterministic -- this run makes no priced call -- so they are pinned
            # rather than skipped, keeping the assertion exact.
            assert bundle["summary"] == {
                "audit_count": 1,
                "approval_count": 0,
                "tool_call_count": 0,
                "memory_interaction_count": 0,
                "priced_call_count": 0,
                "cost_event_count": 0,
                "total_cost_usd": 0.0,
                "cost_identity_state": "not_applicable_no_priced_call",
                "reconciliation_state": "reconciled_zero_activity",
            }
            assert bundle["policy_events"] == []
    finally:
        await rig.aclose()


async def test_manifest_validation_failure_answers_422_without_hostile_bytes(
    sqlite_db, tmp_path
) -> None:
    app, service, rig = await _repo_app(
        sqlite_db, tmp_path, "hostile", builder=make_hostile_multi_script_repo
    )
    operator = _headers("operator-key")
    try:
        with TestClient(app) as client:
            response = client.post(
                f"/v1/repos/{rig.repo_id}/checkouts",
                json={"ref": "main"},
                headers=operator,
            )
            assert response.status_code == 422, response.text
            detail = response.json()["detail"]
            assert detail["code"] == "manifest_validation_failed"
            issues = detail["issues"]
            assert issues, "the 422 must carry the validation issues array"
            for issue in issues:
                assert set(issue) == {"severity", "code", "path", "message"}
            assert "multiple_scripts_unsupported" in {issue["code"] for issue in issues}
            # The hostile manifest bytes never reach any response byte.
            assert b"ZQXCANARYVALUE" not in response.content
            assert b"ignore previous instructions" not in response.content

            # The FAILED row is still readable -- with no hostile bytes either.
            checkout_id = detail["checkout_id"]
            failed = client.get(f"/v1/repos/checkouts/{checkout_id}", headers=operator)
            assert failed.status_code == 200
            assert failed.json()["state"] == "failed"
            assert b"ZQXCANARYVALUE" not in failed.content
    finally:
        await rig.aclose()


async def test_create_run_refusals(sqlite_db, tmp_path) -> None:
    app, service, rig = await _repo_app(sqlite_db, tmp_path, "refusals")
    operator = _headers("operator-key")
    try:
        checkout, report = await rig.service.create_checkout(
            "default", None, rig.repo_id, ref="main"
        )
        assert report is None
        with TestClient(app) as client:
            # An undeclared script answers 422 and never echoes the name.
            bogus = client.post(
                f"/v1/repos/checkouts/{checkout.id}/runs",
                json={"script": "exfiltrate", "input_payload": {}},
                headers=operator,
            )
            assert bogus.status_code == 422
            assert bogus.json()["detail"]["code"] == "script_not_declared"
            assert b"exfiltrate" not in bogus.content
            assert "train" in bogus.json()["detail"]["message"]

            admitted = client.post(
                f"/v1/repos/checkouts/{checkout.id}/runs",
                json={"script": "train", "input_payload": {}},
                headers=operator,
            )
            assert admitted.status_code == 202

        assert await rig.worker.run_once() is True

        with TestClient(app) as client:
            # A consumed checkout refuses further runs with 409.
            consumed = client.post(
                f"/v1/repos/checkouts/{checkout.id}/runs",
                json={"script": "train", "input_payload": {}},
                headers=operator,
            )
            assert consumed.status_code == 409
            assert consumed.json()["detail"]["code"] == "checkout_unavailable_error"
    finally:
        await rig.aclose()


async def test_expired_checkout_refuses_runs_with_409(sqlite_db, tmp_path) -> None:
    app, service, rig = await _repo_app(
        sqlite_db, tmp_path, "expired", checkout_ttl_seconds=0
    )
    try:
        checkout, report = await rig.service.create_checkout(
            "default", None, rig.repo_id, ref="main"
        )
        assert report is None
        with TestClient(app) as client:
            response = client.post(
                f"/v1/repos/checkouts/{checkout.id}/runs",
                json={"script": "train", "input_payload": {}},
                headers=_headers("operator-key"),
            )
            assert response.status_code == 409
            assert response.json()["detail"]["code"] == "checkout_unavailable_error"
    finally:
        await rig.aclose()


async def test_rbac_denies_unpermitted_roles_on_every_route(sqlite_db, tmp_path) -> None:
    app, service, rig = await _repo_app(sqlite_db, tmp_path, "rbac")
    norole = _headers("norole-key")
    reviewer = _headers("reviewer-key")
    routes = [
        ("post", "/v1/repos/installations/1/claim"),
        ("get", "/v1/repos/installations"),
        ("get", "/v1/repos/installations/1/repositories"),
        ("post", f"/v1/repos/{rig.repo_id}/resolve-ref"),
        ("post", f"/v1/repos/{rig.repo_id}/checkouts"),
        ("get", "/v1/repos/checkouts/absent-checkout"),
        ("get", "/v1/repos/checkouts/absent-checkout/attestation"),
        ("post", "/v1/repos/checkouts/absent-checkout/runs"),
        ("get", "/v1/repos/runs/absent-run"),
        ("get", "/v1/repos/runs/absent-run/evidence"),
    ]
    try:
        with TestClient(app) as client:
            # A principal with no roles is denied on all ten routes.
            for method, path in routes:
                response = client.request(method, path, headers=norole)
                assert response.status_code == 403, (method, path, response.text)
                assert response.json() == {"detail": "forbidden"}

            # Reviewers read but never claim, stage, or run.
            assert (
                client.get("/v1/repos/installations", headers=reviewer).status_code == 200
            )
            for method, path in [
                ("post", "/v1/repos/installations/1/claim"),
                ("post", f"/v1/repos/{rig.repo_id}/checkouts"),
                ("post", "/v1/repos/checkouts/absent-checkout/runs"),
            ]:
                response = client.request(method, path, headers=reviewer)
                assert response.status_code == 403, (method, path, response.text)
    finally:
        await rig.aclose()


async def test_repo_routes_are_exempt_from_deployment_scope(sqlite_db, tmp_path) -> None:
    """A tenant with no deployment-scoped credential still reaches its repos.

    The served deployment belongs to the ``default`` tenant; the ``tenant-a``
    operator would answer 404 on every deployment-scoped route, yet the
    repository control-plane routes serve its own tenant's data.
    """
    app, service, rig = await _repo_app(sqlite_db, tmp_path, "scope", tenant_id="tenant-a")
    tenant_a = _headers("tenant-a-operator-key")
    try:
        with TestClient(app) as client:
            listed = client.get("/v1/repos/installations", headers=tenant_a)
            assert listed.status_code == 200, listed.text
            assert [item["installation_id"] for item in listed.json()] == [1]

            created = client.post(
                f"/v1/repos/{rig.repo_id}/checkouts",
                json={"ref": "main"},
                headers=tenant_a,
            )
            assert created.status_code == 202, created.text
            assert created.json()["state"] == "staged"
    finally:
        await rig.aclose()
