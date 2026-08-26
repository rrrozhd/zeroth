"""Durable execution worker for repo runs (ZER-37).

:class:`RepoRunWorker` copies the poll-loop + lease shape of the webhook
delivery worker (:class:`zeroth.service.webhooks.delivery.WebhookDeliveryWorker`):
claim one due row inside a fenced transaction, act on it, complete or fail only
the claimed generation, and let a lapsed lease hand the row to the next
generation when a worker dies mid-run. Unlike the webhook worker it executes
claims *inline* rather than fanning out tasks: a repo run executes
author-supplied code under resource ceilings, so v1 deliberately bounds the
worker to one execution at a time; horizontal scale comes from running more
workers, which the generation fencing already supports. Graceful shutdown is
the same as the copied worker's: the lifespan cancels ``poll_loop``, an
execution interrupted mid-run stays RUNNING, and its lease lapse re-claims it.

Per-run runner instances: the runner's ``checkout_materializer`` is a plain
attribute, so a shared runner would race concurrent runs' materializers. The
worker instead constructs a fresh :class:`ExecutableUnitRunner` per run --
construction is a plain object wrapping the SHARED ``SandboxManager`` and the
SHARED :class:`AdmissionController` (where staging registered the trusted
digest), so nothing heavy is rebuilt -- and pins that run's
:class:`StagedPathMaterializer` on it.

Fail-closed re-validation: before executing, the worker re-reads the manifest
from the staged path and rebuilds the manifest from those bytes. A tampered
``.zeroth.yaml`` therefore changes the rebuilt manifest's config digest, the
manifest digest no longer matches the digest staging registered, and admission
refuses the run with ``trusted_digest_mismatch`` -- the honest code, because
the mismatch is what was actually observed. Every terminal state writes ONE
:class:`NodeAuditRecord` whose flat ``execution_metadata`` carries the full
repository provenance vocabulary registered in
``zeroth.governance.audit.capture_vocabulary``.

Checkout consumption: a checkout is single-use and is marked CONSUMED at first
run start -- after every fail-closed check passes and immediately before the
sandboxed execution begins -- so a second pending run (or a later create_run)
observes CONSUMED and fails ``checkout_unavailable_error``. A worker that dies
between consuming and finishing leaves the run re-claimable but the checkout
CONSUMED; the re-claimed generation then fails closed rather than re-running
author code against a checkout whose first execution may have side-effected.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any
from uuid import uuid4

from zeroth.contracts.repo_manifest import (
    CONFIG_FILENAME,
    RepoUnitPolicy,
    parse_manifest_document,
)
from zeroth.governance.audit import AuditRepository, NodeAuditRecord
from zeroth.governance.audit.capture_vocabulary import normalize_reason_code
from zeroth.integrations.execution.integrity import AdmissionController
from zeroth.integrations.execution.models import RepositoryCheckoutArtifactSource
from zeroth.integrations.execution.repo_units import (
    build_repository_binding,
    build_repository_manifest,
    evaluate_smoke_assertions,
    manifest_config_digest,
)
from zeroth.integrations.execution.runner import (
    ExecutableUnitAdmissionError,
    ExecutableUnitRunner,
    ExecutableUnitRunResult,
)
from zeroth.integrations.execution.sandbox import SandboxManager
from zeroth.integrations.github.materializer import LocalCheckoutMaterializer
from zeroth.integrations.github.models import InstallationState, StagedCheckout
from zeroth.platform.primitives import utc_now
from zeroth.service.github.repository import SQLiteGitHubRepository
from zeroth.service.repositories.repo_models import (
    OUTPUT_PAYLOAD_CAP_BYTES,
    RepoCheckout,
    RepoCheckoutState,
    RepoRun,
)
from zeroth.service.repositories.repository import (
    ClaimedRepoRun,
    SQLiteRepoCheckoutRepository,
    SQLiteRepoRunRepository,
)
from zeroth.service.repositories.service import CheckoutUnavailableError

logger = logging.getLogger(__name__)

_DEFAULT_POLL_INTERVAL_SECONDS = 1.0

# Slack added to the policy's timeout ceiling when leasing a claim, so an
# execution that legitimately runs to its timeout is not re-claimed mid-run.
_LEASE_GRACE_SECONDS = 120.0


class StagedPathMaterializer:
    """Materialize one persisted checkout's staged tree into a sandbox.

    One instance per run, pinned on that run's private runner: the artifact
    source's identities are checked against the checkout row (defense in
    depth -- admission already pinned the manifest), then the staged tree is
    copied with the symlink-refusing local materializer.
    """

    def __init__(self, staged_root: Path, *, checkout: RepoCheckout) -> None:
        self._staged_root = Path(staged_root)
        self._checkout = checkout

    async def materialize(
        self, source: RepositoryCheckoutArtifactSource, destination: Path
    ) -> None:
        """Copy the staged tree the manifest names into ``destination``."""
        checkout = self._checkout
        if (
            source.repository_id != checkout.repository_id
            or source.installation_id != checkout.installation_id
            or source.commit_sha != checkout.resolved_commit_sha
        ):
            raise CheckoutUnavailableError(
                "manifest artifact identity does not match the staged checkout"
            )
        await asyncio.to_thread(
            LocalCheckoutMaterializer().materialize, self._staged_root, destination
        )


class RepoRunWorker:
    """Claim pending repo runs, execute them sandboxed, and record the audit trail."""

    def __init__(
        self,
        *,
        checkout_repository: SQLiteRepoCheckoutRepository,
        run_repository: SQLiteRepoRunRepository,
        github_repository: SQLiteGitHubRepository,
        audit_repository: AuditRepository,
        policy: RepoUnitPolicy,
        sandbox_manager: SandboxManager,
        admission_controller: AdmissionController,
        deployment_ref: str,
        tenant_id: str = "default",
        workspace_id: str | None = None,
        poll_interval: float = _DEFAULT_POLL_INTERVAL_SECONDS,
        worker_id: str | None = None,
        enforcement_overrides: dict[str, Any] | None = None,
    ) -> None:
        self._checkout_repository = checkout_repository
        self._run_repository = run_repository
        self._github_repository = github_repository
        self._audit_repository = audit_repository
        self._policy = policy
        self._sandbox_manager = sandbox_manager
        self._admission_controller = admission_controller
        self._deployment_ref = deployment_ref
        self._tenant_id = tenant_id
        self._workspace_id = workspace_id
        self._poll_interval = poll_interval
        self._worker_id = worker_id or f"repo-run-{uuid4().hex[:12]}"
        self._enforcement_overrides = dict(enforcement_overrides or {})
        self._lease_seconds = float(policy.max_timeout_seconds) + _LEASE_GRACE_SECONDS

    @property
    def poll_interval(self) -> float:
        """Seconds slept when no run was claimable."""
        return self._poll_interval

    async def poll_loop(self) -> None:
        """Claim and execute runs until cancelled by the service lifespan."""
        while True:
            try:
                executed = await self.run_once()
                if not executed:
                    await asyncio.sleep(self._poll_interval)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Only the exception type could carry foreign text; the
                # traceback is safe for the operator, per the janitor's rule.
                logger.exception("repo run worker poll error")
                await asyncio.sleep(self._poll_interval)

    async def run_once(self) -> bool:
        """Claim and execute at most one due run; returns whether one ran."""
        claim = await self._run_repository.claim_pending(
            self._tenant_id,
            worker_id=self._worker_id,
            workspace_id=self._workspace_id,
            lease_seconds=self._lease_seconds,
        )
        if claim is None:
            return False
        await self._execute_claim(claim)
        return True

    # -- one claimed run ---------------------------------------------------------

    async def _execute_claim(self, claim: ClaimedRepoRun) -> None:
        """Execute one claimed run end-to-end, fail-closed at every gate."""
        run = claim.run
        checkout = await self._checkout_repository.get(
            run.tenant_id, run.checkout_id, workspace_id=run.workspace_id
        )
        failure = self._preflight_failure(checkout)
        if failure is not None:
            await self._fail_run(claim, checkout, failure)
            return
        assert checkout is not None  # _preflight_failure covered None
        installation = await self._github_repository.get_installation(
            run.tenant_id, checkout.installation_id
        )
        if installation is None or installation.status is not InstallationState.ACTIVE:
            # Revocation (or suspension, or a vanished row) between staging
            # and run start fails closed under the registered denial code.
            await self._fail_run(claim, checkout, "installation_revoked")
            return
        staged_root = Path(str(checkout.staged_path))
        rebuilt = self._rebuild_binding(run, checkout, staged_root)
        if rebuilt is None:
            await self._fail_run(claim, checkout, "checkout_unavailable_error")
            return
        binding, spec = rebuilt
        runner = ExecutableUnitRunner(
            sandbox_manager=self._sandbox_manager,
            admission_controller=self._admission_controller,
        )
        runner.checkout_materializer = StagedPathMaterializer(staged_root, checkout=checkout)
        # Consume-on-first-run-start: after every gate above, before execution.
        await self._checkout_repository.transition_state(
            run.tenant_id,
            checkout.id,
            RepoCheckoutState.CONSUMED,
            workspace_id=run.workspace_id,
        )
        context: dict[str, Any] = {
            "tenant_id": run.tenant_id,
            "workspace_id": run.workspace_id,
            **self._enforcement_overrides,
        }
        payload = json.loads(run.input_payload_json) if run.input_payload_json else {}
        try:
            result = await runner.run_binding(binding, payload, enforcement_context=context)
        except ExecutableUnitAdmissionError as exc:
            reason = exc.audit_record.get("reason_code")
            code = reason if type(reason) is str and reason else "executable_unit_admission_error"
            await self._fail_run(claim, checkout, code)
            return
        except CheckoutUnavailableError:
            await self._fail_run(claim, checkout, "checkout_unavailable_error")
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 -- author code failing must not kill the loop
            code = normalize_reason_code(type(exc).__name__) or "unknown_error"
            await self._fail_run(claim, checkout, code)
            return
        await self._settle_result(claim, checkout, spec, result)

    def _preflight_failure(self, checkout: RepoCheckout | None) -> str | None:
        """Name the fail-closed refusal for this checkout, or ``None`` to proceed."""
        if (
            checkout is None
            or checkout.state is not RepoCheckoutState.STAGED
            or not checkout.staged_path
            or not checkout.resolved_commit_sha
        ):
            return "checkout_unavailable_error"
        if checkout.expires_at is not None and checkout.expires_at <= utc_now():
            return "checkout_unavailable_error"
        return None

    async def _fail_run(
        self, claim: ClaimedRepoRun, checkout: RepoCheckout | None, failure_code: str
    ) -> None:
        """Fail the claimed generation with a stable code and write its audit record."""
        run = claim.run
        fenced = await self._run_repository.fail(
            run.tenant_id,
            run.id,
            claim.generation,
            failure_code=failure_code,
            workspace_id=run.workspace_id,
        )
        if not fenced:
            return
        await self._write_audit(
            claim, checkout, status="failed", reason_code=failure_code, smoke_passed=None
        )

    def _rebuild_binding(
        self, run: RepoRun, checkout: RepoCheckout, staged_root: Path
    ) -> tuple[Any, Any] | None:
        """Rebuild the run's binding from the staged tree, or ``None`` when unusable.

        The manifest document is re-read from the staged path and the manifest
        rebuilt from those bytes, so the admission digest check downstream
        judges what is on disk NOW, not what staging saw.
        """
        try:
            raw = (staged_root / CONFIG_FILENAME).read_bytes()
        except OSError:
            return None
        document, _report = parse_manifest_document(raw)
        if document is None or run.script_name not in document.scripts:
            return None
        staged = StagedCheckout(
            checkout_id=checkout.id,
            commit_sha=str(checkout.resolved_commit_sha),
            git_tree_id=str(checkout.git_tree_id or ""),
            tree_digest=str(checkout.tree_digest or ""),
            file_count=checkout.file_count or 0,
            size_bytes=checkout.size_bytes or 0,
            has_lfs_pointers=bool(checkout.has_lfs_pointers),
            verified_at=checkout.verified_at or utc_now(),
        )
        manifest = build_repository_manifest(
            document,
            script_name=run.script_name,
            staged=staged,
            repository_id=checkout.repository_id,
            installation_id=checkout.installation_id,
            config_digest=manifest_config_digest(raw),
            policy=self._policy,
        )
        return build_repository_binding(manifest), document.scripts[run.script_name]

    async def _settle_result(
        self,
        claim: ClaimedRepoRun,
        checkout: RepoCheckout,
        spec: Any,
        result: ExecutableUnitRunResult,
    ) -> None:
        """Judge smoke assertions and record the run's terminal state + audit."""
        run = claim.run
        sandbox_result = result.sandbox_result
        exit_code = sandbox_result.returncode if sandbox_result is not None else 0
        stdout_text = (sandbox_result.stdout if sandbox_result is not None else "") or ""
        outcome = evaluate_smoke_assertions(spec, exit_code=exit_code, stdout_text=stdout_text)
        if not outcome.passed:
            fenced = await self._run_repository.fail(
                run.tenant_id,
                run.id,
                claim.generation,
                failure_code="smoke_assertion_failed",
                exit_code=exit_code,
                smoke_passed=False,
                workspace_id=run.workspace_id,
            )
            if fenced:
                await self._write_audit(
                    claim,
                    checkout,
                    status="failed",
                    reason_code="smoke_assertion_failed",
                    smoke_passed=False,
                )
            return
        output_json = json.dumps(result.output_data)
        if len(output_json.encode("utf-8")) > OUTPUT_PAYLOAD_CAP_BYTES:
            # Bounded by the persistence cap: the full payload is the script's
            # to publish elsewhere; the durable record stays a receipt.
            output_json = json.dumps({"output_truncated": True})
        fenced = await self._run_repository.finish(
            run.tenant_id,
            run.id,
            claim.generation,
            exit_code=exit_code,
            smoke_passed=True,
            output_payload_json=output_json,
            workspace_id=run.workspace_id,
        )
        if fenced:
            await self._write_audit(
                claim, checkout, status="completed", reason_code=None, smoke_passed=True
            )

    async def _write_audit(
        self,
        claim: ClaimedRepoRun,
        checkout: RepoCheckout | None,
        *,
        status: str,
        reason_code: str | None,
        smoke_passed: bool | None,
    ) -> None:
        """Write the ONE audit record every terminal repo-run state produces.

        ``execution_metadata`` is FLAT and uses only keys the audit capture
        vocabulary registers, so the repository provenance survives the
        capture boundary instead of being dropped as unknown content.
        """
        run = claim.run
        metadata: dict[str, Any] = {"checkout_id": run.checkout_id}
        if checkout is not None:
            metadata["repo_installation_id"] = str(checkout.installation_id)
            metadata["repo_repository_id"] = str(checkout.repository_id)
            if checkout.resolved_commit_sha:
                metadata["repo_commit_sha"] = checkout.resolved_commit_sha
            if checkout.config_digest:
                metadata["repo_config_digest"] = checkout.config_digest
            if checkout.tree_digest:
                metadata["repo_tree_digest"] = checkout.tree_digest
            if checkout.manifest_digest:
                metadata["repo_manifest_digest"] = checkout.manifest_digest
        if smoke_passed is not None:
            metadata["smoke_passed"] = smoke_passed
        if reason_code is not None:
            metadata["reason_code"] = reason_code
        await self._audit_repository.write(
            NodeAuditRecord(
                audit_id=f"{run.id}:repo-run:{claim.generation}",
                run_id=run.id,
                node_id=f"repo:{run.script_name}",
                graph_version_ref=f"repo-checkout:{run.checkout_id}",
                deployment_ref=self._deployment_ref,
                tenant_id=run.tenant_id,
                workspace_id=run.workspace_id,
                attempt=claim.generation,
                status=status,
                started_at=run.started_at or utc_now(),
                completed_at=utc_now(),
                execution_metadata=metadata,
            )
        )


__all__ = ["RepoRunWorker", "StagedPathMaterializer"]
