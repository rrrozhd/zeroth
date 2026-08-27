"""Tenant-facing repository-unit REST API (ZER-37).

Provides:
  POST /repos/installations/{installation_id}/claim      -- Claim an installation
  GET  /repos/installations                              -- List installations
  GET  /repos/installations/{installation_id}/repositories -- List repo grants
  POST /repos/{repository_id}/resolve-ref                -- Resolve a ref to a SHA
  POST /repos/{repository_id}/checkouts                  -- Stage a checkout (202)
  GET  /repos/checkouts/{checkout_id}                    -- Read a checkout
  GET  /repos/checkouts/{checkout_id}/attestation        -- Checkout attestation
  POST /repos/checkouts/{checkout_id}/runs               -- Admit a script run (202)
  GET  /repos/runs/{run_id}                              -- Read a repo run
  GET  /repos/runs/{run_id}/evidence                     -- Run evidence bundle

Every route is tenant-level control-plane surface: authorization is
``require_permission(..., enforce_deployment_scope=False)`` plus per-resource
scope checks, and every lookup is scoped to the caller's tenant/workspace so a
foreign-tenant id and an unknown id answer byte-identical 404s (the
``tests/security/test_api_surface_isolation.py`` discipline). Failure details
are fixed platform-authored templates -- caller-chosen names (scripts, refs,
manifest keys) are never echoed into a response.

Registration is conditional: :func:`register_repo_routes` is a no-op unless
the bootstrap carries the GitHub integration and repository-unit components
(``settings.github.enabled``), mirroring the webhook receiver's conditioning
while still riding the standard /v1 + unversioned dual mount.
"""

from __future__ import annotations

import json
import re
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from zeroth.contracts.repo_manifest import (
    RepoManifestValidationReport,
)
from zeroth.governance.audit import (
    NodeAuditRecord,
    build_summary,
    collect_policy_events,
)
from zeroth.governance.identity import AuthenticatedPrincipal
from zeroth.integrations.github.models import (
    CheckoutError,
    CheckoutFailureCode,
    InstallationRevokedError,
    InstallationState,
    RepoOutOfScopeError,
    RepositoryState,
)
from zeroth.service.api.audit_api import EvidenceSummaryResponse, _visible_record
from zeroth.service.api.authorization import (
    Permission,
    require_permission,
    require_resource_scope,
)
from zeroth.service.github.repository import (
    GitHubInstallationRecord,
    GitHubRepositoryRecord,
)
from zeroth.service.repositories.attestation import (
    CheckoutAttestationPayload,
    verify_checkout_attestation,
)
from zeroth.service.repositories.repo_models import (
    INPUT_PAYLOAD_CAP_BYTES,
    RepoCheckout,
    RepoRun,
)
from zeroth.service.repositories.service import (
    CheckoutUnavailableError,
    ScriptNotDeclaredError,
)

# Byte-identical not-found templates: an unknown id and a foreign-tenant id
# must be indistinguishable, so each resource class has exactly one 404 body.
_INSTALLATION_NOT_FOUND = "installation not found"
_REPOSITORY_NOT_FOUND = "repository not found"
_CHECKOUT_NOT_FOUND = "checkout not found"
_RUN_NOT_FOUND = "run not found"
_ATTESTATION_NOT_FOUND = "attestation not found"

# Git refs the API accepts: branch/tag names or a 40-hex commit SHA. The value
# is later interpolated into GitHub API paths and git commands, so the charset
# is pinned here at the trust boundary (no whitespace, no traversal).
_REF_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]{0,254}$")
_COMMIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")

# Checkout failure codes that describe an unresolvable requested revision;
# they map to 422 (the request names something the repository does not have)
# while every other pipeline refusal maps to 409.
_UNRESOLVABLE_REVISION_CODES = frozenset(
    {CheckoutFailureCode.REF_NOT_FOUND, CheckoutFailureCode.COMMIT_UNREACHABLE}
)


class RepoInstallationResponse(BaseModel):
    """One tracked GitHub App installation in the caller's tenant."""

    model_config = ConfigDict(extra="forbid")

    installation_id: int
    account_login: str
    account_type: str
    repository_selection: str
    status: str
    last_verified_at: str | None = None
    suspended_at: str | None = None
    revoked_at: str | None = None
    created_at: str
    updated_at: str


class RepoRepositoryResponse(BaseModel):
    """One repository grant persisted under a tracked installation."""

    model_config = ConfigDict(extra="forbid")

    repository_id: int
    owner: str
    name: str
    full_name: str
    private: bool
    default_branch: str
    status: str
    added_at: str
    removed_at: str | None = None


class ResolveRefRequest(BaseModel):
    """Request body for POST /repos/{repository_id}/resolve-ref."""

    model_config = ConfigDict(extra="forbid")

    ref: str


class ResolveRefResponse(BaseModel):
    """The verified commit SHA a ref resolves to."""

    model_config = ConfigDict(extra="forbid")

    commit_sha: str


class CreateCheckoutRequest(BaseModel):
    """Request body for POST /repos/{repository_id}/checkouts.

    Exactly one of ``ref`` or ``commit_sha`` is required; the handler refuses
    ambiguous requests with a stable 422 rather than guessing precedence.
    """

    model_config = ConfigDict(extra="forbid")

    ref: str | None = None
    commit_sha: str | None = None


class RepoValidationIssueResponse(BaseModel):
    """One manifest-validation issue; every field is platform-authored."""

    model_config = ConfigDict(extra="forbid")

    severity: str
    code: str
    path: list[str] = Field(default_factory=list)
    message: str


class CheckoutResponse(BaseModel):
    """Public serialization of one durable repository checkout."""

    model_config = ConfigDict(extra="forbid")

    checkout_id: str
    installation_id: int
    repository_id: int
    repository_full_name: str
    requested_ref: str
    state: str
    resolved_commit_sha: str | None = None
    git_tree_id: str | None = None
    tree_digest: str | None = None
    config_digest: str | None = None
    manifest_digest: str | None = None
    script_name: str | None = None
    failure_code: str | None = None
    failure_detail: str | None = None
    file_count: int | None = None
    size_bytes: int | None = None
    expires_at: str | None = None
    created_at: str
    updated_at: str
    attestation_present: bool = False
    validation_report: list[RepoValidationIssueResponse] | None = None


class CheckoutAttestationResponse(BaseModel):
    """Persisted checkout attestation plus its dual verification axes.

    Mirrors the deployment attestation semantics: ``digest_verified`` is the
    recompute axis, ``signature_verified`` is the keyed axis (three-state,
    null = unsigned-legacy), and overall ``verified`` requires the digest to
    pass and the signature not to be a definite failure.
    """

    model_config = ConfigDict(extra="forbid")

    payload: CheckoutAttestationPayload
    attestation_digest: str
    attestation_signature: str | None = None
    attestation_key_id: str | None = None
    attestation_algorithm: str | None = None
    digest_verified: bool
    signature_verified: bool | None = None
    verified: bool


class CreateRepoRunRequest(BaseModel):
    """Request body for POST /repos/checkouts/{checkout_id}/runs."""

    model_config = ConfigDict(extra="forbid")

    script: str
    input_payload: dict[str, Any] = Field(default_factory=dict)


class RepoRunResponse(BaseModel):
    """Public serialization of one script execution against a checkout."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    checkout_id: str
    script_name: str
    state: str
    exit_code: int | None = None
    failure_code: str | None = None
    smoke_passed: bool | None = None
    output_payload: Any | None = None
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    updated_at: str


class RepoRunEvidenceResponse(BaseModel):
    """Review-friendly evidence bundle for a single repository run."""

    model_config = ConfigDict(extra="forbid")

    run: RepoRunResponse
    checkout_attestation: CheckoutAttestationResponse | None = None
    audits: list[NodeAuditRecord] = Field(default_factory=list)
    summary: EvidenceSummaryResponse
    policy_events: list[str] = Field(default_factory=list)


def _iso(value: object) -> str | None:
    """Render an optional datetime as its ISO form."""
    isoformat = getattr(value, "isoformat", None)
    return isoformat() if callable(isoformat) else None


def _installation_response(record: GitHubInstallationRecord) -> RepoInstallationResponse:
    """Serialize one persisted installation row."""
    return RepoInstallationResponse(
        installation_id=record.installation_id,
        account_login=record.account_login,
        account_type=record.account_type,
        repository_selection=record.repository_selection,
        status=record.status.value,
        last_verified_at=_iso(record.last_verified_at),
        suspended_at=_iso(record.suspended_at),
        revoked_at=_iso(record.revoked_at),
        created_at=record.created_at.isoformat(),
        updated_at=record.updated_at.isoformat(),
    )


def _repository_response(grant: GitHubRepositoryRecord) -> RepoRepositoryResponse:
    """Serialize one persisted repository grant."""
    return RepoRepositoryResponse(
        repository_id=grant.repo_id,
        owner=grant.owner,
        name=grant.name,
        full_name=grant.full_name,
        private=grant.private,
        default_branch=grant.default_branch,
        status=grant.status.value,
        added_at=grant.added_at.isoformat(),
        removed_at=_iso(grant.removed_at),
    )


def _issue_payloads(report: RepoManifestValidationReport) -> list[dict[str, Any]]:
    """Render a validation report's issues as JSON-safe platform-authored dicts."""
    return [
        RepoValidationIssueResponse(
            severity=issue.severity.value,
            code=issue.code.value,
            path=list(issue.path),
            message=issue.message,
        ).model_dump(mode="json")
        for issue in report.issues
    ]


def _checkout_response(
    checkout: RepoCheckout, report: RepoManifestValidationReport | None = None
) -> CheckoutResponse:
    """Serialize one checkout row (``staged_path`` stays server-internal)."""
    validation_report = None
    if report is not None and report.issues:
        validation_report = [
            RepoValidationIssueResponse.model_validate(payload)
            for payload in _issue_payloads(report)
        ]
    return CheckoutResponse(
        checkout_id=checkout.id,
        installation_id=checkout.installation_id,
        repository_id=checkout.repository_id,
        repository_full_name=checkout.repository_full_name,
        requested_ref=checkout.requested_ref,
        state=checkout.state.value,
        resolved_commit_sha=checkout.resolved_commit_sha,
        git_tree_id=checkout.git_tree_id,
        tree_digest=checkout.tree_digest,
        config_digest=checkout.config_digest,
        manifest_digest=checkout.manifest_digest,
        script_name=checkout.script_name,
        failure_code=checkout.failure_code.value if checkout.failure_code else None,
        failure_detail=checkout.failure_detail,
        file_count=checkout.file_count,
        size_bytes=checkout.size_bytes,
        expires_at=_iso(checkout.expires_at),
        created_at=checkout.created_at.isoformat(),
        updated_at=checkout.updated_at.isoformat(),
        attestation_present=checkout.attestation_digest is not None,
        validation_report=validation_report,
    )


def _run_response(run: RepoRun) -> RepoRunResponse:
    """Serialize one repo-run row; the output payload is parsed JSON."""
    output_payload = None
    if run.output_payload_json is not None:
        output_payload = json.loads(run.output_payload_json)
    return RepoRunResponse(
        run_id=run.id,
        checkout_id=run.checkout_id,
        script_name=run.script_name,
        state=run.state.value,
        exit_code=run.exit_code,
        failure_code=run.failure_code,
        smoke_passed=run.smoke_passed,
        output_payload=output_payload,
        created_at=run.created_at.isoformat(),
        started_at=_iso(run.started_at),
        finished_at=_iso(run.finished_at),
        updated_at=run.updated_at.isoformat(),
    )


def _attestation_response(
    checkout: RepoCheckout, signer: object | None
) -> CheckoutAttestationResponse | None:
    """Verify and serialize a checkout's persisted attestation, if it has one."""
    if checkout.attestation_payload_json is None or checkout.attestation_digest is None:
        return None
    payload = CheckoutAttestationPayload.model_validate_json(checkout.attestation_payload_json)
    digest_ok, signature_ok = verify_checkout_attestation(
        payload,
        digest=checkout.attestation_digest,
        signature=checkout.attestation_signature,
        key_id=checkout.attestation_key_id,
        algorithm=checkout.attestation_algorithm,
        signer=signer,  # type: ignore[arg-type]
    )
    return CheckoutAttestationResponse(
        payload=payload,
        attestation_digest=checkout.attestation_digest,
        attestation_signature=checkout.attestation_signature,
        attestation_key_id=checkout.attestation_key_id,
        attestation_algorithm=checkout.attestation_algorithm,
        digest_verified=digest_ok,
        signature_verified=signature_ok,
        verified=digest_ok and signature_ok is not False,
    )


def _service_bootstrap(request: Request) -> Any:
    """Fetch the service bootstrap from app state."""
    bootstrap = getattr(request.app.state, "bootstrap", None)
    if bootstrap is None:
        raise RuntimeError("service bootstrap is not configured")
    return bootstrap


def _component(request: Request, name: str) -> Any:
    """Fetch one GitHub/repository component, 503 when the integration is off."""
    component = getattr(_service_bootstrap(request), name, None)
    if component is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="github integration is not enabled",
        )
    return component


def _not_found(detail: str) -> HTTPException:
    """One canonical 404; callers pass a per-resource fixed template."""
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)


def _checkout_error_exception(exc: CheckoutError) -> HTTPException:
    """Map a typed checkout refusal onto a 4xx with template details.

    ``RepoOutOfScopeError`` collapses into the repository 404 (out-of-scope
    reads as absent); unresolvable-revision codes answer 422; everything else
    (revoked/suspended installations, caps, git/API failures) answers 409.
    ``exc.detail`` is safe by the :class:`CheckoutError` contract: fixed
    templates, pre-redacted, never echoing caller-authored text.
    """
    if isinstance(exc, RepoOutOfScopeError):
        return _not_found(_REPOSITORY_NOT_FOUND)
    status_code = (
        status.HTTP_422_UNPROCESSABLE_ENTITY
        if exc.code in _UNRESOLVABLE_REVISION_CODES
        else status.HTTP_409_CONFLICT
    )
    return HTTPException(
        status_code=status_code,
        detail={"code": exc.code.value, "message": exc.detail},
    )


def _require_valid_ref(ref: str) -> None:
    """Refuse a ref outside the pinned charset with a fixed template."""
    if not _REF_PATTERN.match(ref) or ".." in ref:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "invalid_ref",
                "message": "ref must be a branch, tag, or 40-hex commit identifier",
            },
        )


async def _resolve_grant(
    request: Request, tenant_id: str, repository_id: int
) -> tuple[GitHubInstallationRecord, GitHubRepositoryRecord]:
    """Find the tenant's tracked installation+grant pair for one repository id.

    Raises the byte-identical repository 404 when nothing in the caller's
    tenant tracks the id -- unknown and foreign-tenant ids are the same case.
    """
    github_repository = _component(request, "github_repository")
    for installation in await github_repository.list_installations(tenant_id):
        grant = await github_repository.get_repository(
            tenant_id, installation.id, repository_id
        )
        if grant is not None:
            return installation, grant
    raise _not_found(_REPOSITORY_NOT_FOUND)


def _refuse_unusable_rows(
    installation: GitHubInstallationRecord, grant: GitHubRepositoryRecord
) -> None:
    """Fail closed on rows that already rule the repository out (no network)."""
    if grant.status is not RepositoryState.ACTIVE:
        raise _not_found(_REPOSITORY_NOT_FOUND)
    if installation.status is InstallationState.SUSPENDED:
        raise _checkout_error_exception(
            CheckoutError(CheckoutFailureCode.INSTALLATION_SUSPENDED, "installation is suspended")
        )
    if installation.status is not InstallationState.ACTIVE:
        raise _checkout_error_exception(
            CheckoutError(
                CheckoutFailureCode.INSTALLATION_REVOKED,
                "installation is revoked or the app is uninstalled",
            )
        )


def register_repo_routes(app: FastAPI | APIRouter, bootstrap: object) -> None:
    """Register the repository-unit routes when the integration is constructed.

    A bootstrap without the GitHub integration components (``settings.github``
    disabled, or the bare inventory bootstrap) registers nothing, so the
    routes are absent from the route inventory and OpenAPI contracts exactly
    like the webhook receiver.
    """
    if (
        getattr(bootstrap, "github_integration_service", None) is None
        or getattr(bootstrap, "repository_unit_service", None) is None
    ):
        return

    @app.post(
        "/repos/installations/{installation_id}/claim",
        response_model=RepoInstallationResponse,
    )
    async def claim_repo_installation(
        request: Request, installation_id: int
    ) -> RepoInstallationResponse:
        """Attach one GitHub App installation to the caller's tenant."""
        principal = await require_permission(
            request, Permission.REPOSITORY_ADMIN, enforce_deployment_scope=False
        )
        integration = _component(request, "github_integration_service")
        try:
            record = await integration.claim_installation(principal.tenant_id, installation_id)
        except InstallationRevokedError as exc:
            raise _not_found(_INSTALLATION_NOT_FOUND) from exc
        except CheckoutError as exc:
            raise _checkout_error_exception(exc) from exc
        return _installation_response(record)

    @app.get("/repos/installations", response_model=list[RepoInstallationResponse])
    async def list_repo_installations(request: Request) -> list[RepoInstallationResponse]:
        """List the caller's tenant's installations (ACTIVE ones live-verified)."""
        principal = await require_permission(
            request, Permission.REPOSITORY_READ, enforce_deployment_scope=False
        )
        integration = _component(request, "github_integration_service")
        records = await integration.list_installations(principal.tenant_id)
        return [_installation_response(record) for record in records]

    @app.get(
        "/repos/installations/{installation_id}/repositories",
        response_model=list[RepoRepositoryResponse],
    )
    async def list_installation_repositories(
        request: Request, installation_id: int
    ) -> list[RepoRepositoryResponse]:
        """List the persisted repository grants for one tenant installation."""
        principal = await require_permission(
            request, Permission.REPOSITORY_READ, enforce_deployment_scope=False
        )
        integration = _component(request, "github_integration_service")
        grants = await integration.list_repositories(principal.tenant_id, installation_id)
        return [_repository_response(grant) for grant in grants]

    @app.post("/repos/{repository_id}/resolve-ref", response_model=ResolveRefResponse)
    async def resolve_repository_ref(
        request: Request, repository_id: int, body: ResolveRefRequest
    ) -> ResolveRefResponse:
        """Resolve a branch, tag, or commit SHA against one tracked repository."""
        principal = await require_permission(
            request, Permission.REPOSITORY_READ, enforce_deployment_scope=False
        )
        _require_valid_ref(body.ref)
        installation, grant = await _resolve_grant(
            request, principal.tenant_id, repository_id
        )
        _refuse_unusable_rows(installation, grant)
        broker = _component(request, "github_token_broker")
        client = _component(request, "github_client")
        try:
            commit_sha = await broker.run_with_lease(
                installation.installation_id,
                grant.name,
                lambda token: client.resolve_ref(token, grant.owner, grant.name, body.ref),
            )
        except CheckoutError as exc:
            raise _checkout_error_exception(exc) from exc
        return ResolveRefResponse(commit_sha=commit_sha)

    @app.post(
        "/repos/{repository_id}/checkouts",
        response_model=CheckoutResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def create_repository_checkout(
        request: Request, repository_id: int, body: CreateCheckoutRequest
    ) -> CheckoutResponse:
        """Stage one governed repository checkout; 422 carries validation issues."""
        principal = await require_permission(
            request, Permission.REPOSITORY_RUN, enforce_deployment_scope=False
        )
        if (body.ref is None) == (body.commit_sha is None):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "code": "invalid_reference_selection",
                    "message": "exactly one of ref or commit_sha is required",
                },
            )
        if body.ref is not None:
            _require_valid_ref(body.ref)
        if body.commit_sha is not None and not _COMMIT_SHA_PATTERN.fullmatch(body.commit_sha):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "code": "invalid_commit_sha",
                    "message": "commit_sha must be a 40-hex commit identifier",
                },
            )
        unit_service = _component(request, "repository_unit_service")
        try:
            checkout, report = await unit_service.create_checkout(
                principal.tenant_id,
                principal.workspace_id,
                repository_id,
                ref=body.ref,
                commit_sha=body.commit_sha,
            )
        except RepoOutOfScopeError as exc:
            raise _not_found(_REPOSITORY_NOT_FOUND) from exc
        except CheckoutError as exc:
            raise _checkout_error_exception(exc) from exc
        if report is not None and report.has_errors:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "code": "manifest_validation_failed",
                    "checkout_id": checkout.id,
                    "issues": _issue_payloads(report),
                },
            )
        return _checkout_response(checkout, report)

    @app.get("/repos/checkouts/{checkout_id}", response_model=CheckoutResponse)
    async def get_repository_checkout(request: Request, checkout_id: str) -> CheckoutResponse:
        """Read one checkout in the caller's scope."""
        principal = await require_permission(
            request, Permission.REPOSITORY_READ, enforce_deployment_scope=False
        )
        checkout = await _get_scoped_checkout(request, principal, checkout_id)
        return _checkout_response(checkout)

    @app.get(
        "/repos/checkouts/{checkout_id}/attestation",
        response_model=CheckoutAttestationResponse,
    )
    async def get_checkout_attestation(
        request: Request, checkout_id: str
    ) -> CheckoutAttestationResponse:
        """Read and dual-verify one checkout's persisted attestation."""
        principal = await require_permission(
            request, Permission.REPOSITORY_READ, enforce_deployment_scope=False
        )
        checkout = await _get_scoped_checkout(request, principal, checkout_id)
        signer = getattr(_service_bootstrap(request), "signer", None)
        attestation = _attestation_response(checkout, signer)
        if attestation is None:
            raise _not_found(_ATTESTATION_NOT_FOUND)
        return attestation

    @app.post(
        "/repos/checkouts/{checkout_id}/runs",
        response_model=RepoRunResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def create_repository_run(
        request: Request, checkout_id: str, body: CreateRepoRunRequest
    ) -> RepoRunResponse:
        """Admit one declared-script execution against a STAGED checkout."""
        principal = await require_permission(
            request, Permission.REPOSITORY_RUN, enforce_deployment_scope=False
        )
        encoded = json.dumps(body.input_payload)
        if len(encoded.encode("utf-8")) > INPUT_PAYLOAD_CAP_BYTES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "code": "input_payload_too_large",
                    "message": (
                        f"input_payload exceeds the {INPUT_PAYLOAD_CAP_BYTES}-byte cap"
                    ),
                },
            )
        unit_service = _component(request, "repository_unit_service")
        try:
            run = await unit_service.create_run(
                principal.tenant_id,
                principal.workspace_id,
                checkout_id,
                script=body.script,
                input_payload=body.input_payload,
            )
        except CheckoutUnavailableError as exc:
            # Absent-in-scope and unusable states share one refusal, so an
            # unknown id and a foreign-tenant id stay byte-identical here too.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc
        except ScriptNotDeclaredError as exc:
            # The message names only the declared script -- never the request's.
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc
        return _run_response(run)

    @app.get("/repos/runs/{run_id}", response_model=RepoRunResponse)
    async def get_repository_run(request: Request, run_id: str) -> RepoRunResponse:
        """Read one repo run in the caller's scope."""
        principal = await require_permission(
            request, Permission.REPOSITORY_READ, enforce_deployment_scope=False
        )
        run = await _get_scoped_run(request, principal, run_id)
        return _run_response(run)

    @app.get("/repos/runs/{run_id}/evidence", response_model=RepoRunEvidenceResponse)
    async def get_repository_run_evidence(
        request: Request, run_id: str
    ) -> RepoRunEvidenceResponse:
        """Return the evidence bundle for a repo run; requires ``AUDIT_READ``."""
        principal = await require_permission(
            request, Permission.AUDIT_READ, enforce_deployment_scope=False
        )
        run = await _get_scoped_run(request, principal, run_id)
        await require_resource_scope(
            request,
            tenant_id=run.tenant_id,
            workspace_id=run.workspace_id,
            not_found_detail=_RUN_NOT_FOUND,
        )
        bootstrap = _service_bootstrap(request)
        audits = [
            await _visible_record(request, record, not_found_detail=_RUN_NOT_FOUND)
            for record in await bootstrap.audit_repository.list_by_run(
                run.id,
                tenant_id=principal.tenant_id,
                workspace_id=run.workspace_id,
                workspace_scoped=True,
            )
        ]
        unit_service = _component(request, "repository_unit_service")
        checkout = await unit_service.get_checkout(
            principal.tenant_id, run.checkout_id, workspace_id=principal.workspace_id
        )
        signer = getattr(bootstrap, "signer", None)
        attestation = _attestation_response(checkout, signer) if checkout is not None else None
        return RepoRunEvidenceResponse(
            run=_run_response(run),
            checkout_attestation=attestation,
            audits=audits,
            summary=EvidenceSummaryResponse.model_validate(build_summary(audits, [])),
            policy_events=collect_policy_events(audits),
        )


async def _get_scoped_checkout(
    request: Request, principal: AuthenticatedPrincipal, checkout_id: str
) -> RepoCheckout:
    """Load one checkout in the principal's scope or answer the canonical 404."""
    unit_service = _component(request, "repository_unit_service")
    checkout = await unit_service.get_checkout(
        principal.tenant_id, checkout_id, workspace_id=principal.workspace_id
    )
    if checkout is None:
        raise _not_found(_CHECKOUT_NOT_FOUND)
    return checkout


async def _get_scoped_run(
    request: Request, principal: AuthenticatedPrincipal, run_id: str
) -> RepoRun:
    """Load one repo run in the principal's scope or answer the canonical 404."""
    unit_service = _component(request, "repository_unit_service")
    run = await unit_service.get_run(
        principal.tenant_id, run_id, workspace_id=principal.workspace_id
    )
    if run is None:
        raise _not_found(_RUN_NOT_FOUND)
    return run
