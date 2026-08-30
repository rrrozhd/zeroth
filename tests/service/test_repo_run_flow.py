"""ZER-37 orchestration glue: checkout staging -> repo run -> audit provenance.

Full-stack over the fake substrate: the fake GitHub REST surface behind an
ASGI transport, a real git smart-HTTP loopback server, the real checkout
pipeline, SQLite persistence, the repository-unit service, and the repo-run
worker driven one cycle at a time (no sleeping poll loops). The LOCAL sandbox
backend runs the author scripts; the enforcement context carries the explicit
permissive strictness override exactly as the repository-units tests do,
because the inherited policy ceilings demand hard isolation which LOCAL
refuses under STANDARD strictness.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.github.fake_github import FakeGitHubAPI
from tests.github.git_http_server import GitSmartHTTPServer
from tests.github.repo_fixtures import (
    RepoFixture,
    git_env,
    make_benign_repo,
    make_multi_script_manifest_repo,
    make_no_config_repo,
)
from zeroth.contracts.repo_manifest import RepoManifestValidationCode, RepoUnitPolicy
from zeroth.governance.audit import AuditRepository
from zeroth.integrations.execution.integrity import AdmissionController
from zeroth.integrations.execution.sandbox import SandboxConfig, SandboxManager
from zeroth.integrations.github.app_jwt import AppJwtIssuer
from zeroth.integrations.github.checkout import CheckoutService
from zeroth.integrations.github.client import GitHubAppClient
from zeroth.integrations.github.config import GitHubAppConfig
from zeroth.integrations.github.git_cli import GitInvocation
from zeroth.integrations.github.models import (
    CheckoutError,
    CheckoutFailureCode,
    InstallationState,
)
from zeroth.integrations.github.token_broker import InstallationTokenBroker
from zeroth.platform.secrets.provider import EnvSecretProvider
from zeroth.service.github.repository import SQLiteGitHubRepository
from zeroth.service.github.service import GitHubIntegrationService
from zeroth.service.repositories.attestation import (
    CheckoutAttestationPayload,
    verify_checkout_attestation,
)
from zeroth.service.repositories.repo_models import RepoCheckoutState, RepoRunState
from zeroth.service.repositories.repository import (
    SQLiteRepoCheckoutRepository,
    SQLiteRepoRunRepository,
)
from zeroth.service.repositories.service import (
    CheckoutUnavailableError,
    RepoCheckoutPipelineRecorder,
    RepositoryUnitService,
    ScriptNotDeclaredError,
)
from zeroth.service.repositories.worker import RepoRunWorker

BASE_URL = "http://api.github.test"
PERMISSIVE = {"sandbox_strictness_mode": "permissive"}

_SMOKE_FAILING_MANIFEST = """\
schema_version: 1
scripts:
  train:
    entry: "scripts/train.py"
    runtime: "python3"
    working_directory: "."
    input: {mode: "json_stdin"}
    output: {mode: "json_stdout"}
    resources: {timeout_seconds: 60}
    network: {access: "none"}
    smoke:
      exit_code: 0
      stdout_contains: "READY"
"""

_SMOKE_FAILING_SCRIPT = """\
#!/usr/bin/env python3
import json
import sys

payload = json.load(sys.stdin)
json.dump({"ok": True, "echo": payload}, sys.stdout)
"""


def make_smoke_failing_repo(base_dir: Path) -> RepoFixture:
    """A manifest demanding READY on stdout from a script that never prints it."""
    workdir = base_dir / "_work" / "smoke-failing"
    workdir.mkdir(parents=True)

    def run(args: list[str]) -> str:
        completed = subprocess.run(
            ["git", *args],
            cwd=workdir,
            env=git_env(),
            capture_output=True,
            check=True,
            timeout=60,
        )
        return completed.stdout.decode("utf-8", "replace").strip()

    run(["init", "-q", "-b", "main"])
    (workdir / ".zeroth.yaml").write_text(_SMOKE_FAILING_MANIFEST)
    scripts = workdir / "scripts"
    scripts.mkdir()
    train = scripts / "train.py"
    train.write_text(_SMOKE_FAILING_SCRIPT)
    train.chmod(0o755)
    run(["add", "-A"])
    run(
        [
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-q",
            "-m",
            "smoke-failing fixture",
        ]
    )
    head_sha = run(["rev-parse", "HEAD"])
    bare = base_dir / "smoke-failing.git"
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(workdir), str(bare)],
        cwd=base_dir,
        env=git_env(),
        capture_output=True,
        check=True,
        timeout=60,
    )
    subprocess.run(
        ["git", "config", "uploadpack.allowReachableSHA1InWant", "true"],
        cwd=bare,
        env=git_env(),
        capture_output=True,
        check=True,
        timeout=60,
    )
    return RepoFixture(bare, head_sha)


@dataclass
class RepoRig:
    """One fully wired repository-unit stack over a served fixture repository."""

    api: FakeGitHubAPI
    server: GitSmartHTTPServer
    client: GitHubAppClient
    github_repository: SQLiteGitHubRepository
    checkout_repository: SQLiteRepoCheckoutRepository
    run_repository: SQLiteRepoRunRepository
    audit_repository: AuditRepository
    service: RepositoryUnitService
    worker: RepoRunWorker
    repo_id: int
    head_sha: str
    staging_root: Path

    async def aclose(self) -> None:
        await self.client.aclose()
        self.server.stop()

    def git_requests(self) -> list[tuple[str, str | None]]:
        return list(self.server.requests)


async def make_rig(
    sqlite_db,
    tmp_path: Path,
    builder=make_benign_repo,
    *,
    checkout_ttl_seconds: int = 900,
) -> RepoRig:
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
        tenant_id="default",
        transport=api.transport(),
    )
    await integration.claim_installation("default", 1)
    installation_row = await github_repository.get_installation("default", 1)
    assert installation_row is not None
    grants = await github_repository.list_repositories("default", installation_row.id)
    assert len(grants) == 1

    checkout_repository = SQLiteRepoCheckoutRepository(sqlite_db)
    run_repository = SQLiteRepoRunRepository(sqlite_db)
    audit_repository = AuditRepository.for_default_compatibility(sqlite_db)
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
    service = RepositoryUnitService(
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
        deployment_ref="repo-flow-tests",
        tenant_id="default",
        workspace_id=None,
        enforcement_overrides=dict(PERMISSIVE),
    )
    return RepoRig(
        api=api,
        server=server,
        client=client,
        github_repository=github_repository,
        checkout_repository=checkout_repository,
        run_repository=run_repository,
        audit_repository=audit_repository,
        service=service,
        worker=worker,
        repo_id=grants[0].repo_id,
        head_sha=fixture.head_sha,
        staging_root=staging_root,
    )


async def _staged_checkout(rig: RepoRig):
    checkout, report = await rig.service.create_checkout(
        "default", None, rig.repo_id, ref="main"
    )
    assert report is None, report
    assert checkout.state is RepoCheckoutState.STAGED
    return checkout


PROVENANCE_KEYS = (
    "checkout_id",
    "repo_installation_id",
    "repo_repository_id",
    "repo_commit_sha",
    "repo_config_digest",
    "repo_tree_digest",
    "repo_manifest_digest",
    "smoke_passed",
)


async def test_full_repo_run_flow_records_provenance(sqlite_db, tmp_path) -> None:
    rig = await make_rig(sqlite_db, tmp_path)
    try:
        checkout = await _staged_checkout(rig)
        assert checkout.script_name == "train"
        assert checkout.resolved_commit_sha == rig.head_sha
        assert checkout.tree_digest is not None and checkout.tree_digest.startswith("sha256:")
        assert checkout.config_digest is not None and checkout.config_digest.startswith("sha256:")
        assert checkout.manifest_digest is not None and len(checkout.manifest_digest) == 64
        assert checkout.staged_path is not None and checkout.expires_at is not None

        # The attestation on the row recomputes cleanly; unsigned-legacy
        # (no signer in this rig) reports the signature axis as None.
        assert checkout.attestation_payload_json is not None
        payload = CheckoutAttestationPayload.model_validate_json(
            checkout.attestation_payload_json
        )
        assert payload.checkout_id == checkout.id
        digest_ok, signature_ok = verify_checkout_attestation(
            payload,
            digest=str(checkout.attestation_digest),
            signature=checkout.attestation_signature,
            key_id=checkout.attestation_key_id,
            algorithm=checkout.attestation_algorithm,
            signer=None,
        )
        assert (digest_ok, signature_ok) == (True, None)

        run = await rig.service.create_run(
            "default", None, checkout.id, script="train", input_payload={"word": "hello"}
        )
        assert run.state is RepoRunState.PENDING

        assert await rig.worker.run_once() is True
        finished = await rig.service.get_run("default", run.id)
        assert finished is not None
        assert finished.state is RepoRunState.SUCCEEDED, finished.failure_code
        assert finished.smoke_passed is True
        assert finished.exit_code == 0
        output = json.loads(str(finished.output_payload_json))
        assert output["echo"] == {"word": "hello"}

        consumed = await rig.service.get_checkout("default", checkout.id)
        assert consumed is not None and consumed.state is RepoCheckoutState.CONSUMED

        # The ONE terminal audit record, asserted on the PERSISTED row --
        # i.e. after the capture boundary's metadata projection.
        records = await rig.audit_repository.list_by_run(run.id)
        assert len(records) == 1
        record = records[0]
        assert record.node_id == "repo:train"
        assert record.graph_version_ref == f"repo-checkout:{checkout.id}"
        assert record.deployment_ref == "repo-flow-tests"
        assert record.status == "completed"
        metadata = record.execution_metadata
        for key in PROVENANCE_KEYS:
            assert key in metadata, f"{key} did not survive capture: {sorted(metadata)}"
        assert metadata["checkout_id"] == checkout.id
        assert metadata["repo_installation_id"] == "1"
        assert metadata["repo_repository_id"] == str(rig.repo_id)
        assert metadata["repo_commit_sha"] == rig.head_sha
        assert metadata["repo_config_digest"] == checkout.config_digest
        assert metadata["repo_tree_digest"] == checkout.tree_digest
        assert metadata["repo_manifest_digest"] == checkout.manifest_digest
        assert metadata["smoke_passed"] is True
    finally:
        await rig.aclose()


async def test_create_run_refuses_undeclared_script_without_echoing_it(
    sqlite_db, tmp_path
) -> None:
    rig = await make_rig(sqlite_db, tmp_path)
    try:
        checkout = await _staged_checkout(rig)
        with pytest.raises(ScriptNotDeclaredError) as excinfo:
            await rig.service.create_run(
                "default", None, checkout.id, script="exfiltrate", input_payload={}
            )
        assert excinfo.value.code == "script_not_declared"
        assert "exfiltrate" not in str(excinfo.value)
        assert "train" in str(excinfo.value)
    finally:
        await rig.aclose()


async def test_checkout_unavailable_after_consumption(sqlite_db, tmp_path) -> None:
    rig = await make_rig(sqlite_db, tmp_path)
    try:
        checkout = await _staged_checkout(rig)
        await rig.service.create_run(
            "default", None, checkout.id, script="train", input_payload={}
        )
        assert await rig.worker.run_once() is True
        with pytest.raises(CheckoutUnavailableError):
            await rig.service.create_run(
                "default", None, checkout.id, script="train", input_payload={}
            )
    finally:
        await rig.aclose()


async def test_checkout_unavailable_after_expiry(sqlite_db, tmp_path) -> None:
    rig = await make_rig(sqlite_db, tmp_path, checkout_ttl_seconds=0)
    try:
        checkout = await _staged_checkout(rig)
        with pytest.raises(CheckoutUnavailableError):
            await rig.service.create_run(
                "default", None, checkout.id, script="train", input_payload={}
            )
    finally:
        await rig.aclose()


async def test_multi_script_manifest_fails_the_checkout_with_a_report(
    sqlite_db, tmp_path
) -> None:
    rig = await make_rig(sqlite_db, tmp_path, builder=make_multi_script_manifest_repo)
    try:
        checkout, report = await rig.service.create_checkout(
            "default", None, rig.repo_id, ref="main"
        )
        assert checkout.state is RepoCheckoutState.FAILED
        assert report is not None and report.has_errors
        codes = {issue.code for issue in report.issues}
        assert RepoManifestValidationCode.MULTIPLE_SCRIPTS_UNSUPPORTED in codes
    finally:
        await rig.aclose()


async def test_missing_config_fails_the_checkout_config_missing(sqlite_db, tmp_path) -> None:
    rig = await make_rig(sqlite_db, tmp_path, builder=make_no_config_repo)
    try:
        checkout, report = await rig.service.create_checkout(
            "default", None, rig.repo_id, ref="main"
        )
        assert checkout.state is RepoCheckoutState.FAILED
        assert checkout.failure_code is CheckoutFailureCode.CONFIG_MISSING
        assert report is not None
        codes = {issue.code for issue in report.issues}
        assert RepoManifestValidationCode.CONFIG_MISSING in codes
    finally:
        await rig.aclose()


async def test_revocation_between_staging_and_run_fails_closed(sqlite_db, tmp_path) -> None:
    rig = await make_rig(sqlite_db, tmp_path)
    try:
        checkout = await _staged_checkout(rig)
        run = await rig.service.create_run(
            "default", None, checkout.id, script="train", input_payload={}
        )
        rig.api.revoke_installation(1)
        await rig.github_repository.set_installation_status(
            "default", 1, InstallationState.REVOKED
        )
        assert await rig.worker.run_once() is True
        failed = await rig.service.get_run("default", run.id)
        assert failed is not None
        assert failed.state is RepoRunState.FAILED
        assert failed.failure_code == "installation_revoked"
        records = await rig.audit_repository.list_by_run(run.id)
        assert len(records) == 1
        assert records[0].status == "failed"
        assert records[0].execution_metadata["reason_code"] == "installation_revoked"
    finally:
        await rig.aclose()


async def test_revocation_before_checkout_fails_before_any_git(sqlite_db, tmp_path) -> None:
    rig = await make_rig(sqlite_db, tmp_path)
    try:
        await rig.github_repository.set_installation_status(
            "default", 1, InstallationState.REVOKED
        )
        with pytest.raises(CheckoutError) as excinfo:
            await rig.service.create_checkout("default", None, rig.repo_id, ref="main")
        assert excinfo.value.code is CheckoutFailureCode.INSTALLATION_REVOKED
        checkouts = await rig.service.list_checkouts("default")
        assert len(checkouts) == 1
        assert checkouts[0].state is RepoCheckoutState.FAILED
        assert checkouts[0].failure_code is CheckoutFailureCode.INSTALLATION_REVOKED
        assert rig.git_requests() == []
    finally:
        await rig.aclose()


async def test_smoke_assertion_failure_fails_the_run(sqlite_db, tmp_path) -> None:
    rig = await make_rig(sqlite_db, tmp_path, builder=make_smoke_failing_repo)
    try:
        checkout = await _staged_checkout(rig)
        run = await rig.service.create_run(
            "default", None, checkout.id, script="train", input_payload={"word": "x"}
        )
        assert await rig.worker.run_once() is True
        failed = await rig.service.get_run("default", run.id)
        assert failed is not None
        assert failed.state is RepoRunState.FAILED
        assert failed.failure_code == "smoke_assertion_failed"
        assert failed.smoke_passed is False
        records = await rig.audit_repository.list_by_run(run.id)
        assert len(records) == 1
        metadata = records[0].execution_metadata
        assert metadata["smoke_passed"] is False
        assert metadata["reason_code"] == "smoke_assertion_failed"
    finally:
        await rig.aclose()


async def test_manifest_digest_drift_is_refused_by_admission(sqlite_db, tmp_path) -> None:
    rig = await make_rig(sqlite_db, tmp_path)
    try:
        checkout = await _staged_checkout(rig)
        run = await rig.service.create_run(
            "default", None, checkout.id, script="train", input_payload={}
        )
        # Corrupt the staged manifest AFTER staging registered the trusted
        # digest: still parseable, same script, different bytes -- so the
        # worker's rebuilt manifest digest no longer matches the registered
        # one and admission refuses the run.
        staged_manifest = Path(str(checkout.staged_path)) / ".zeroth.yaml"
        staged_manifest.write_bytes(staged_manifest.read_bytes() + b"\n# tampered\n")
        assert await rig.worker.run_once() is True
        failed = await rig.service.get_run("default", run.id)
        assert failed is not None
        assert failed.state is RepoRunState.FAILED
        assert failed.failure_code == "trusted_digest_mismatch"
        records = await rig.audit_repository.list_by_run(run.id)
        assert len(records) == 1
        assert records[0].execution_metadata["reason_code"] == "trusted_digest_mismatch"
    finally:
        await rig.aclose()
