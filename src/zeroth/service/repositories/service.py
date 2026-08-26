"""Orchestration glue for repository units: stage checkouts, admit runs (ZER-37).

:class:`RepositoryUnitService` is the write-side seam the API wave will call.
``create_checkout`` drives one repository through the full staging pipeline --
installation/repository fail-closed checks, :class:`CheckoutService` staging,
manifest parse + policy + staged-path validation, translation into a
:class:`RepositoryUnitManifest`, admission-digest registration, and a signed
checkout attestation -- recording every outcome on the durable
:class:`~zeroth.service.repositories.repo_models.RepoCheckout` row.
``create_run`` admits one script execution against a STAGED checkout and
persists it PENDING for :class:`~zeroth.service.repositories.worker.RepoRunWorker`.

Failure contract: checkout-pipeline refusals raise the pipeline's own typed
:class:`CheckoutError` (after the row is marked FAILED with the same code);
manifest-validation refusals do NOT raise -- the row is marked FAILED and the
validation report is returned so the API can answer 422 with it. The row's
``failure_code`` column is typed to :class:`CheckoutFailureCode`, which has no
member for manifest-validation errors, so those rows carry state FAILED with a
``None`` code and the report is the machine-readable record (see
``docstring: create_checkout``).

Pipeline state recording: ``CheckoutService`` mints its own internal checkout
id and reports lifecycle transitions under it, while the durable row has an id
this service minted first (so pre-staging failures land somewhere).
:class:`RepoCheckoutPipelineRecorder` bridges the two through a task-local
binding: the service binds its row id (and a scope-bound repository view)
around each ``stage`` call, and the recorder forwards the pipeline's
*intermediate* states onto that row. Terminal states ("ready"/"failed") are
dropped by the recorder on purpose -- the service records them itself with
strictly more information (digests, redacted detail), and a fire-and-forget
terminal write racing the service's own would let a stale HARDENING/FAILED
land after STAGED.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from contextvars import ContextVar
from datetime import timedelta
from pathlib import Path
from typing import ClassVar

from zeroth.contracts.repo_manifest import (
    CONFIG_FILENAME,
    RepoManifestDocument,
    RepoManifestIssue,
    RepoManifestSeverity,
    RepoManifestValidationCode,
    RepoManifestValidationReport,
    RepoUnitPolicy,
    evaluate_policy,
    parse_manifest_document,
)
from zeroth.integrations.execution.errors import ManifestValidationError
from zeroth.integrations.execution.integrity import AdmissionController, compute_manifest_digest
from zeroth.integrations.execution.repo_units import (
    build_repository_manifest,
    manifest_config_digest,
    validate_staged_manifest,
)
from zeroth.integrations.execution.validator import ExecutableUnitValidator
from zeroth.integrations.github.checkout import CheckoutService
from zeroth.integrations.github.models import (
    CheckoutError,
    CheckoutFailureCode,
    CheckoutRequest,
    InstallationRevokedError,
    InstallationState,
    InstallationSuspendedError,
    RepoOutOfScopeError,
    RepositoryState,
    StagedCheckout,
)
from zeroth.platform.primitives import utc_now
from zeroth.platform.signing import SigningKeyProvider
from zeroth.service.github.repository import (
    GitHubInstallationRecord,
    GitHubRepositoryRecord,
    SQLiteGitHubRepository,
)
from zeroth.service.repositories.attestation import (
    CheckoutAttestationPayload,
    build_checkout_attestation,
    sign_checkout_attestation,
)
from zeroth.service.repositories.repo_models import (
    RepoCheckout,
    RepoCheckoutState,
    RepoRun,
)
from zeroth.service.repositories.repository import (
    SQLiteRepoCheckoutRepository,
    SQLiteRepoRunRepository,
)

__all__ = [
    "CheckoutUnavailableError",
    "RepoCheckoutPipelineRecorder",
    "RepositoryUnitService",
    "ScriptNotDeclaredError",
]

# The stage call currently bound for pipeline state recording: the durable row
# id plus the scope-bound repository view transitions are recorded through.
# Task-local, so concurrent checkouts in separate tasks never cross-record.
_PIPELINE_BINDING: ContextVar[tuple[str, SQLiteRepoCheckoutRepository] | None] = ContextVar(
    "zeroth_repo_checkout_pipeline_binding", default=None
)

# Pipeline states the recorder forwards. Terminal states are service-owned;
# see the module docstring for why they are dropped here.
_FORWARDED_PIPELINE_STATES = frozenset(
    {"resolving", "fetching", "scanning", "materializing", "verifying"}
)


class RepositoryUnitError(RuntimeError):
    """Base for repository-unit orchestration refusals; ``code`` is stable."""

    code: ClassVar[str] = "repository_unit_error"


class CheckoutUnavailableError(RepositoryUnitError):
    """The named checkout cannot host a run: absent, consumed, expired, failed."""

    code: ClassVar[str] = "checkout_unavailable_error"


class ScriptNotDeclaredError(RepositoryUnitError):
    """The requested script is not the one the staged manifest declares.

    The message never echoes the requested name -- the code plus the declared
    script are all a caller needs, and the requested name is caller-authored
    text this codebase does not repeat into logs.
    """

    code: ClassVar[str] = "script_not_declared"


class RepoCheckoutPipelineRecorder:
    """CheckoutStateStore bridging pipeline transitions onto the durable row.

    Bound to the unscoped checkout repository at construction; each ``stage``
    call is wrapped by :class:`RepositoryUnitService` setting the task-local
    binding to ``(row_id, repository.for_scope(tenant, workspace))``, so the
    recorder writes under the caller's scope and the service's row id rather
    than the pipeline's internal id. With no binding (a stage call outside the
    service) recording is a no-op, matching the protocol's "when anyone cares"
    contract.
    """

    def __init__(self, repository: SQLiteRepoCheckoutRepository) -> None:
        self._repository = repository

    def record_state(self, checkout_id: str, state: str, **fields: object) -> None:
        """Forward one intermediate pipeline state onto the bound durable row."""
        del checkout_id  # the pipeline's internal id; the bound row id is used
        binding = _PIPELINE_BINDING.get()
        if binding is None or state not in _FORWARDED_PIPELINE_STATES:
            return
        row_id, scoped = binding
        scoped.record_state(row_id, state, **fields)


class RepositoryUnitService:
    """Stage repository checkouts and admit script runs against them."""

    def __init__(
        self,
        *,
        checkout_repository: SQLiteRepoCheckoutRepository,
        run_repository: SQLiteRepoRunRepository,
        github_repository: SQLiteGitHubRepository,
        checkout_service: CheckoutService,
        admission_controller: AdmissionController,
        policy: RepoUnitPolicy,
        staging_root: Path,
        signer: SigningKeyProvider | None = None,
        checkout_ttl_seconds: int = 900,
    ) -> None:
        self._checkout_repository = checkout_repository
        self._run_repository = run_repository
        self._github_repository = github_repository
        self._checkout_service = checkout_service
        self._admission_controller = admission_controller
        self._policy = policy
        self._staging_root = Path(staging_root)
        self._signer = signer
        self._checkout_ttl_seconds = checkout_ttl_seconds

    # -- checkout staging --------------------------------------------------------

    async def create_checkout(
        self,
        tenant_id: str | None,
        workspace_id: str | None,
        repository_id: int,
        *,
        ref: str | None = None,
        commit_sha: str | None = None,
    ) -> tuple[RepoCheckout, RepoManifestValidationReport | None]:
        """Stage one repository checkout end-to-end and persist its lifecycle.

        Args:
            tenant_id: Owning tenant (``None`` means the default-compat tenant).
            workspace_id: Owning workspace, or ``None`` for tenant-wide.
            repository_id: The GitHub repository id of a tracked grant.
            ref: Symbolic ref to resolve (branch or tag), when no SHA is pinned.
            commit_sha: Pinned commit SHA; when given it is also enforced as
                ``expected_commit_sha`` through the pipeline.

        Returns:
            The (re-read) checkout row and a validation report. The report is
            ``None`` on a clean staging; when it carries errors the row is
            FAILED and the caller should surface the report (the API's 422).

        Raises:
            RepoOutOfScopeError: When no tracked grant matches
                ``repository_id`` in this tenant (no row is created -- there is
                no repository identity to record one under).
            CheckoutError: When the installation is unusable or the staging
                pipeline refused; the row is FAILED under the same code first.
            ValueError: When neither ``ref`` nor ``commit_sha`` is given.
        """
        if ref is None and commit_sha is None:
            raise ValueError("either ref or commit_sha is required")
        installation, grant = await self._resolve_repository(tenant_id, repository_id)
        checkout = await self._checkout_repository.create(
            RepoCheckout(
                tenant_id=tenant_id or "default",
                workspace_id=workspace_id,
                repository_pk=grant.id,
                installation_id=installation.installation_id,
                repository_id=grant.repo_id,
                repository_full_name=grant.full_name,
                requested_ref=(ref or commit_sha or ""),
            )
        )
        destination = self._staging_root / checkout.tenant_id / checkout.id
        try:
            self._refuse_unusable_rows(installation, grant)
        except CheckoutError as exc:
            await self._fail_checkout(checkout, code=exc.code, detail=exc.detail)
            raise
        staged = await self._stage(checkout, installation, grant, destination, commit_sha)
        report = await self._validate_and_translate(checkout, staged, destination)
        final = await self._reload(checkout)
        return final, report

    def _refuse_unusable_rows(
        self, installation: GitHubInstallationRecord, grant: GitHubRepositoryRecord
    ) -> None:
        """Refuse a checkout whose persisted rows already rule it out (no git runs)."""
        if grant.status is not RepositoryState.ACTIVE:
            raise RepoOutOfScopeError("repository grant is removed")
        if installation.status is InstallationState.SUSPENDED:
            raise InstallationSuspendedError()
        if installation.status is not InstallationState.ACTIVE:
            raise InstallationRevokedError()

    async def _resolve_repository(
        self, tenant_id: str | None, repository_id: int
    ) -> tuple[GitHubInstallationRecord, GitHubRepositoryRecord]:
        """Find the tracked installation+grant pair for one GitHub repository id.

        Raises:
            RepoOutOfScopeError: When no installation in this tenant tracks a
                grant for ``repository_id``.
        """
        installations = await self._github_repository.list_installations(tenant_id)
        for installation in installations:
            grant = await self._github_repository.get_repository(
                tenant_id, installation.id, repository_id
            )
            if grant is not None:
                return installation, grant
        raise RepoOutOfScopeError("repository is not tracked by any installation in this tenant")

    async def _stage(
        self,
        checkout: RepoCheckout,
        installation: GitHubInstallationRecord,
        grant: GitHubRepositoryRecord,
        destination: Path,
        commit_sha: str | None,
    ) -> StagedCheckout:
        """Run the checkout pipeline into ``destination``, recording failures."""
        destination.mkdir(parents=True, exist_ok=True)
        request = CheckoutRequest(
            installation_id=installation.installation_id,
            owner=grant.owner,
            name=grant.name,
            ref=checkout.requested_ref,
            expected_commit_sha=commit_sha,
        )
        scoped = self._checkout_repository.for_scope(checkout.tenant_id, checkout.workspace_id)
        token = _PIPELINE_BINDING.set((checkout.id, scoped))
        try:
            try:
                return await self._checkout_service.stage(
                    request, destination=destination, tenant_id=checkout.tenant_id
                )
            finally:
                _PIPELINE_BINDING.reset(token)
                # Drain the recorder's fire-and-forget transitions before any
                # terminal write, so a late HARDENING cannot overwrite it.
                await scoped.flush_state_records()
        except CheckoutError as exc:
            await self._fail_checkout(checkout, code=exc.code, detail=exc.detail)
            raise
        except asyncio.CancelledError:
            await self._fail_checkout(
                checkout,
                code=CheckoutFailureCode.CANCELLED,
                detail="checkout was cancelled",
            )
            raise

    async def _validate_and_translate(
        self, checkout: RepoCheckout, staged: StagedCheckout, destination: Path
    ) -> RepoManifestValidationReport | None:
        """Validate the staged manifest and, when clean, finish staging the row.

        Returns the merged validation report when it carries any issue (the
        row is FAILED when those are errors), or ``None`` on a clean staging.
        """
        config_path = destination / CONFIG_FILENAME
        try:
            raw = config_path.read_bytes()
        except OSError:
            await self._fail_checkout(
                checkout,
                code=CheckoutFailureCode.CONFIG_MISSING,
                detail="repository has no manifest at the checkout root",
            )
            return RepoManifestValidationReport(
                issues=(
                    RepoManifestIssue(
                        severity=RepoManifestSeverity.ERROR,
                        code=RepoManifestValidationCode.CONFIG_MISSING,
                        path=(),
                        message="repository has no .zeroth.yaml manifest",
                    ),
                )
            )
        document, parse_report = parse_manifest_document(raw)
        issues = list(parse_report.issues)
        if document is not None:
            issues.extend(evaluate_policy(document, self._policy).issues)
            issues.extend(validate_staged_manifest(document, destination).issues)
        report = RepoManifestValidationReport(issues=tuple(issues))
        if document is None or report.has_errors:
            # No CheckoutFailureCode member names manifest-validation errors
            # and the column is typed to that enum, so the row carries FAILED
            # with no code; the returned report is the machine-readable why.
            await self._checkout_repository.transition_state(
                checkout.tenant_id,
                checkout.id,
                RepoCheckoutState.FAILED,
                workspace_id=checkout.workspace_id,
            )
            self._reset_staging_dir(destination)
            return report
        await self._finish_staging(checkout, staged, destination, raw, document)
        return report if report.issues else None

    async def _finish_staging(
        self,
        checkout: RepoCheckout,
        staged: StagedCheckout,
        destination: Path,
        raw: bytes,
        document: RepoManifestDocument,
    ) -> None:
        """Translate, register admission, attest, and transition the row STAGED."""
        config_digest = manifest_config_digest(raw)
        script_name = next(iter(document.scripts))
        manifest = build_repository_manifest(
            document,
            script_name=script_name,
            staged=staged,
            repository_id=checkout.repository_id,
            installation_id=checkout.installation_id,
            config_digest=config_digest,
            policy=self._policy,
        )
        try:
            ExecutableUnitValidator().validate_or_raise(manifest)
        except ManifestValidationError:
            await self._checkout_repository.transition_state(
                checkout.tenant_id,
                checkout.id,
                RepoCheckoutState.FAILED,
                workspace_id=checkout.workspace_id,
            )
            self._reset_staging_dir(destination)
            raise
        manifest_digest = compute_manifest_digest(manifest)
        # admit() resolves trusted digests by the manifest's unit_id.
        self._admission_controller.register_trusted_digest(manifest.unit_id, manifest_digest)
        payload = CheckoutAttestationPayload(
            tenant_id=checkout.tenant_id,
            workspace_id=checkout.workspace_id,
            checkout_id=checkout.id,
            installation_id=checkout.installation_id,
            repository_id=checkout.repository_id,
            repository_full_name=checkout.repository_full_name,
            requested_ref=checkout.requested_ref,
            commit_sha=staged.commit_sha,
            git_tree_id=staged.git_tree_id,
            tree_digest=staged.tree_digest,
            config_digest=config_digest,
            manifest_digest=manifest_digest,
            script_name=script_name,
            issued_at=utc_now(),
        )
        digest = build_checkout_attestation(payload)
        signature, key_id, algorithm = sign_checkout_attestation(digest, self._signer)
        await self._checkout_repository.record_attestation(
            checkout.tenant_id,
            checkout.id,
            digest=digest,
            signature=signature,
            key_id=key_id,
            algorithm=algorithm,
            payload_json=payload.model_dump_json(),
            workspace_id=checkout.workspace_id,
        )
        await self._checkout_repository.transition_state(
            checkout.tenant_id,
            checkout.id,
            RepoCheckoutState.STAGED,
            workspace_id=checkout.workspace_id,
            resolved_commit_sha=staged.commit_sha,
            git_tree_id=staged.git_tree_id,
            tree_digest=staged.tree_digest,
            config_digest=config_digest,
            manifest_digest=manifest_digest,
            script_name=script_name,
            staged_path=str(destination),
            file_count=staged.file_count,
            size_bytes=staged.size_bytes,
            has_lfs_pointers=staged.has_lfs_pointers,
            verified_at=staged.verified_at,
            expires_at=utc_now() + timedelta(seconds=self._checkout_ttl_seconds),
        )

    async def _fail_checkout(
        self, checkout: RepoCheckout, *, code: CheckoutFailureCode, detail: str
    ) -> None:
        """Mark the row FAILED with a typed code and drop its staging directory."""
        await self._checkout_repository.record_failure(
            checkout.tenant_id,
            checkout.id,
            code=code,
            redacted_detail=detail,
            workspace_id=checkout.workspace_id,
        )
        self._reset_staging_dir(self._staging_root / checkout.tenant_id / checkout.id)

    def _reset_staging_dir(self, destination: Path) -> None:
        """Remove one staging directory, confined to the staging root."""
        root = self._staging_root.resolve()
        resolved = Path(destination).resolve()
        if resolved != root and resolved.is_relative_to(root):
            shutil.rmtree(destination, ignore_errors=True)

    async def _reload(self, checkout: RepoCheckout) -> RepoCheckout:
        """Re-read the row so callers see every transition this call recorded."""
        current = await self._checkout_repository.get(
            checkout.tenant_id, checkout.id, workspace_id=checkout.workspace_id
        )
        return current if current is not None else checkout

    # -- run admission -----------------------------------------------------------

    async def create_run(
        self,
        tenant_id: str | None,
        workspace_id: str | None,
        checkout_id: str,
        *,
        script: str,
        input_payload: dict[str, object],
    ) -> RepoRun:
        """Admit one script execution against a STAGED checkout, persisted PENDING.

        Raises:
            CheckoutUnavailableError: When the checkout is absent from this
                scope, not STAGED (consumed, expired, failed, mid-pipeline),
                or already past its expiry horizon.
            ScriptNotDeclaredError: When ``script`` is not the checkout's
                declared script. The message never echoes the requested name.
        """
        checkout = await self._checkout_repository.get(
            tenant_id, checkout_id, workspace_id=workspace_id
        )
        if checkout is None:
            raise CheckoutUnavailableError("checkout does not exist in this scope")
        if checkout.state is not RepoCheckoutState.STAGED:
            raise CheckoutUnavailableError(f"checkout is {checkout.state.value}, not staged")
        if checkout.expires_at is not None and checkout.expires_at <= utc_now():
            raise CheckoutUnavailableError("checkout is past its expiry horizon")
        if checkout.script_name is None or script != checkout.script_name:
            raise ScriptNotDeclaredError(
                "the checkout declares exactly one runnable script: "
                f"{checkout.script_name!r}"
            )
        return await self._run_repository.create(
            RepoRun(
                tenant_id=tenant_id or "default",
                workspace_id=workspace_id,
                checkout_id=checkout.id,
                script_name=checkout.script_name,
                input_payload_json=json.dumps(input_payload),
            )
        )

    # -- query passthroughs (the API wave reads through these) -------------------

    async def get_checkout(
        self, tenant_id: str | None, checkout_id: str, *, workspace_id: str | None = None
    ) -> RepoCheckout | None:
        """Load one checkout in the caller's scope, or ``None``."""
        return await self._checkout_repository.get(
            tenant_id, checkout_id, workspace_id=workspace_id
        )

    async def list_checkouts(
        self,
        tenant_id: str | None,
        *,
        workspace_id: str | None = None,
        state: RepoCheckoutState | None = None,
    ) -> list[RepoCheckout]:
        """List the scope's checkouts, oldest first, optionally by state."""
        return await self._checkout_repository.list_checkouts(
            tenant_id, workspace_id=workspace_id, state=state
        )

    async def get_run(
        self, tenant_id: str | None, run_id: str, *, workspace_id: str | None = None
    ) -> RepoRun | None:
        """Load one repo run in the caller's scope, or ``None``."""
        return await self._run_repository.get(tenant_id, run_id, workspace_id=workspace_id)

    async def list_runs(
        self,
        tenant_id: str | None,
        *,
        workspace_id: str | None = None,
        checkout_id: str | None = None,
    ) -> list[RepoRun]:
        """List the scope's runs, oldest first, optionally for one checkout."""
        return await self._run_repository.list_runs(
            tenant_id, workspace_id=workspace_id, checkout_id=checkout_id
        )
