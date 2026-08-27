"""Async database-backed persistence for repo checkouts and repo runs (ZER-37).

Follows the GitHub/deployments recipe: per-call tenant+workspace scoping
through :class:`~zeroth.platform.storage.ScopedTable`, one
``@persistence_surface`` declaration per registered resource, and explicit
``@persistence_operation`` metadata on every public persistence method. Both
resources are workspace-scoped (like runs and deployments), so every call
builds either a :class:`ScopeContext` or a :class:`NullWorkspaceScopeContext`
from the caller-supplied ``tenant_id``/``workspace_id`` pair.

:class:`SQLiteRepoCheckoutRepository` additionally implements the
:class:`zeroth.integrations.github.checkout.CheckoutStateStore` protocol so
the checkout pipeline can record lifecycle transitions directly onto the
persisted row: ``record_state`` is the protocol's sync method, which schedules
the mapped async transition on the running loop (``flush_state_records``
awaits the backlog; recording is best-effort with no loop, exactly as the
protocol's "when anyone cares" contract allows).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from zeroth.integrations.github.models import CheckoutFailureCode
from zeroth.platform.primitives import utc_now
from zeroth.platform.storage import (
    SERVICE_SCOPE_REGISTRY,
    AsyncDatabase,
    NullWorkspaceScopeContext,
    ResourceOperation,
    ScopeContext,
    ScopedTable,
)
from zeroth.platform.storage.scoping import (
    named_isolation_probe,
    persistence_operation,
    persistence_surface,
)
from zeroth.service.repositories.repo_models import (
    OUTPUT_PAYLOAD_CAP_BYTES,
    RepoCheckout,
    RepoCheckoutState,
    RepoRun,
    RepoRunState,
    ensure_bounded_payload,
)

_DEFAULT_LEASE_SECONDS = 30.0
_CLAIM_CANDIDATE_LIMIT = 16

_PIPELINE_STATE_TO_CHECKOUT_STATE = {
    "resolving": RepoCheckoutState.VERIFYING,
    "fetching": RepoCheckoutState.FETCHING,
    "scanning": RepoCheckoutState.HARDENING,
    "materializing": RepoCheckoutState.HARDENING,
    "verifying": RepoCheckoutState.HARDENING,
    "ready": RepoCheckoutState.STAGED,
    "failed": RepoCheckoutState.FAILED,
}
"""How the checkout pipeline's ``record_state`` strings map onto row states."""


def _iso(value: datetime | None) -> str | None:
    """Serialize an optional datetime to the stored ISO-8601 TEXT form."""
    return value.isoformat() if value is not None else None


def _parse(value: object) -> datetime | None:
    """Parse an optional stored ISO-8601 TEXT column back to a datetime."""
    return datetime.fromisoformat(value) if isinstance(value, str) and value else None


def _tribool(value: object) -> bool | None:
    """Hydrate a nullable INTEGER boolean column."""
    return None if value is None else bool(value)


def _context(
    tenant_id: str | None, workspace_id: str | None
) -> ScopeContext | NullWorkspaceScopeContext:
    """Build the per-call scope, honoring the default-compat tenant convention."""
    tenant = tenant_id or "default"
    if workspace_id is None:
        if tenant == "default":
            return NullWorkspaceScopeContext.for_default_compatibility()
        return NullWorkspaceScopeContext(tenant_id=tenant)
    if tenant == "default":
        return ScopeContext.for_default_compatibility(workspace_id=workspace_id)
    return ScopeContext(tenant_id=tenant, workspace_id=workspace_id)


@dataclass(frozen=True, slots=True)
class ClaimedRepoRun:
    """A repo run paired with the generation that fences its completion."""

    run: RepoRun
    generation: int


@persistence_surface(
    "service.repo_checkouts",
    probe=named_isolation_probe("_drive_repo_checkouts"),
    non_persistence_public_methods=frozenset(
        {"for_scope", "record_state", "flush_state_records"}
    ),
)
class SQLiteRepoCheckoutRepository:
    """Persist repository checkout lifecycle rows and their attestations."""

    def __init__(self, database: AsyncDatabase):
        self._database: AsyncDatabase = database
        # Scope the sync CheckoutStateStore adapter records under; rebound by
        # for_scope(). The default records under the default-compat tenant.
        self._recording_scope: tuple[str | None, str | None] = (None, None)
        self._state_tasks: set[asyncio.Task[None]] = set()

    def _checkouts(self, tenant_id: str | None, workspace_id: str | None) -> ScopedTable:
        """Bind the checkouts table to one tenant+workspace scope."""
        return ScopedTable(
            self._database,
            SERVICE_SCOPE_REGISTRY,
            "service.repo_checkouts",
            _context(tenant_id, workspace_id),
        )

    # -- CRUD and lifecycle ------------------------------------------------------

    @persistence_operation(ResourceOperation.CREATE)
    async def create(self, checkout: RepoCheckout) -> RepoCheckout:
        """Insert one checkout row owned by the checkout's tenant and workspace."""
        row = await self._checkouts(checkout.tenant_id, checkout.workspace_id).insert(
            {
                "id": checkout.id,
                "repository_pk": checkout.repository_pk,
                "installation_id": checkout.installation_id,
                "repository_id": checkout.repository_id,
                "repository_full_name": checkout.repository_full_name,
                "requested_ref": checkout.requested_ref,
                "resolved_commit_sha": checkout.resolved_commit_sha,
                "git_tree_id": checkout.git_tree_id,
                "tree_digest": checkout.tree_digest,
                "config_digest": checkout.config_digest,
                "manifest_digest": checkout.manifest_digest,
                "script_name": checkout.script_name,
                "state": checkout.state.value,
                "failure_code": (
                    None if checkout.failure_code is None else checkout.failure_code.value
                ),
                "failure_detail": checkout.failure_detail,
                "staged_path": checkout.staged_path,
                "file_count": checkout.file_count,
                "size_bytes": checkout.size_bytes,
                "has_lfs_pointers": (
                    None if checkout.has_lfs_pointers is None else int(checkout.has_lfs_pointers)
                ),
                "cache_hit": int(checkout.cache_hit),
                "verified_at": _iso(checkout.verified_at),
                "expires_at": _iso(checkout.expires_at),
                "attestation_digest": checkout.attestation_digest,
                "attestation_signature": checkout.attestation_signature,
                "attestation_key_id": checkout.attestation_key_id,
                "attestation_algorithm": checkout.attestation_algorithm,
                "attestation_payload_json": checkout.attestation_payload_json,
                "created_at": checkout.created_at.isoformat(),
                "updated_at": checkout.updated_at.isoformat(),
            }
        )
        return self._row_to_checkout(row)

    @persistence_operation(ResourceOperation.READ)
    async def get(
        self, tenant_id: str | None, checkout_id: str, *, workspace_id: str | None = None
    ) -> RepoCheckout | None:
        """Load one checkout by id within the caller's scope, or ``None``."""
        row = await self._checkouts(tenant_id, workspace_id).select_one(
            where={"id": checkout_id}
        )
        return None if row is None else self._row_to_checkout(row)

    @persistence_operation(ResourceOperation.ENUMERATE)
    async def list_checkouts(
        self,
        tenant_id: str | None,
        *,
        workspace_id: str | None = None,
        state: RepoCheckoutState | None = None,
    ) -> list[RepoCheckout]:
        """Return the scope's checkouts, oldest first, optionally by state."""
        where = None if state is None else {"state": state.value}
        async with self._checkouts(tenant_id, workspace_id).transaction() as checkouts:
            rows = await checkouts.select(where=where, order_by=("created_at", "id"))
        return [self._row_to_checkout(row) for row in rows]

    @persistence_operation(ResourceOperation.UPDATE)
    async def transition_state(
        self,
        tenant_id: str | None,
        checkout_id: str,
        state: RepoCheckoutState,
        *,
        workspace_id: str | None = None,
        resolved_commit_sha: str | None = None,
        git_tree_id: str | None = None,
        tree_digest: str | None = None,
        config_digest: str | None = None,
        manifest_digest: str | None = None,
        script_name: str | None = None,
        staged_path: str | None = None,
        file_count: int | None = None,
        size_bytes: int | None = None,
        has_lfs_pointers: bool | None = None,
        cache_hit: bool | None = None,
        verified_at: datetime | None = None,
        expires_at: datetime | None = None,
    ) -> bool:
        """Transition one checkout's state, stamping ``updated_at``.

        Optional keyword fields are written only when supplied, so a
        transition never nulls an identity recorded by an earlier step.
        Returns whether a row in this scope matched.
        """
        values: dict[str, Any] = {"state": state.value, "updated_at": utc_now().isoformat()}
        optional: dict[str, Any] = {
            "resolved_commit_sha": resolved_commit_sha,
            "git_tree_id": git_tree_id,
            "tree_digest": tree_digest,
            "config_digest": config_digest,
            "manifest_digest": manifest_digest,
            "script_name": script_name,
            "staged_path": staged_path,
            "file_count": file_count,
            "size_bytes": size_bytes,
            "has_lfs_pointers": None if has_lfs_pointers is None else int(has_lfs_pointers),
            "cache_hit": None if cache_hit is None else int(cache_hit),
            "verified_at": _iso(verified_at),
            "expires_at": _iso(expires_at),
        }
        values.update({key: value for key, value in optional.items() if value is not None})
        async with self._checkouts(tenant_id, workspace_id).transaction(
            write_lock=True
        ) as checkouts:
            return await checkouts.update_if_matches(
                values, where={"id": checkout_id}, returning="id"
            )

    @persistence_operation(ResourceOperation.UPDATE)
    async def record_failure(
        self,
        tenant_id: str | None,
        checkout_id: str,
        *,
        code: CheckoutFailureCode,
        redacted_detail: str,
        workspace_id: str | None = None,
    ) -> bool:
        """Mark one checkout FAILED with its typed code and pre-redacted detail."""
        async with self._checkouts(tenant_id, workspace_id).transaction(
            write_lock=True
        ) as checkouts:
            return await checkouts.update_if_matches(
                {
                    "state": RepoCheckoutState.FAILED.value,
                    "failure_code": code.value,
                    "failure_detail": redacted_detail,
                    "updated_at": utc_now().isoformat(),
                },
                where={"id": checkout_id},
                returning="id",
            )

    @persistence_operation(ResourceOperation.UPDATE)
    async def record_attestation(
        self,
        tenant_id: str | None,
        checkout_id: str,
        *,
        digest: str,
        signature: str | None,
        key_id: str | None,
        algorithm: str | None,
        payload_json: str,
        workspace_id: str | None = None,
    ) -> bool:
        """Persist the attestation envelope onto one checkout row.

        An all-``None`` signature triple is the unsigned-legacy shape
        (:func:`zeroth.service.repositories.attestation.sign_checkout_attestation`).
        """
        async with self._checkouts(tenant_id, workspace_id).transaction(
            write_lock=True
        ) as checkouts:
            return await checkouts.update_if_matches(
                {
                    "attestation_digest": digest,
                    "attestation_signature": signature,
                    "attestation_key_id": key_id,
                    "attestation_algorithm": algorithm,
                    "attestation_payload_json": payload_json,
                    "updated_at": utc_now().isoformat(),
                },
                where={"id": checkout_id},
                returning="id",
            )

    @persistence_operation(ResourceOperation.ENUMERATE, ResourceOperation.UPDATE)
    async def expire_stale(
        self,
        tenant_id: str | None,
        *,
        workspace_id: str | None = None,
        now: datetime | None = None,
    ) -> list[str]:
        """Sweep STAGED checkouts past ``expires_at`` to EXPIRED; return their ids."""
        horizon = (now or utc_now()).isoformat()
        stamp = utc_now().isoformat()
        async with self._checkouts(tenant_id, workspace_id).transaction(
            write_lock=True
        ) as checkouts:
            rows = await checkouts.select(
                columns=("id",),
                where={"state": RepoCheckoutState.STAGED.value},
                where_lt={"expires_at": horizon},
                order_by=("expires_at", "id"),
            )
            for row in rows:
                await checkouts.update(
                    {"state": RepoCheckoutState.EXPIRED.value, "updated_at": stamp},
                    where={"id": row["id"], "state": RepoCheckoutState.STAGED.value},
                )
        return [str(row["id"]) for row in rows]

    # -- CheckoutStateStore protocol adapter -------------------------------------

    def for_scope(
        self, tenant_id: str | None, workspace_id: str | None = None
    ) -> SQLiteRepoCheckoutRepository:
        """Return a view whose sync ``record_state`` adapter records under one scope.

        The async persistence methods are scope-per-call and unaffected; only
        the :class:`CheckoutStateStore` channel -- whose protocol carries no
        tenant -- needs a binding. An unbound repository records under the
        default-compat tenant, matching the house scoping convention.
        """
        scoped = SQLiteRepoCheckoutRepository(self._database)
        scoped._recording_scope = (tenant_id, workspace_id)
        return scoped

    def record_state(self, checkout_id: str, state: str, **fields: object) -> None:
        """Record that ``checkout_id`` entered ``state`` with optional detail.

        Sync :class:`CheckoutStateStore` conformance: schedules the mapped
        async transition on the running loop. Best-effort with no running
        loop, per the protocol's "when anyone cares" contract; tests and
        orderly shutdown drain the backlog with :meth:`flush_state_records`.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # no loop: lifecycle recording is best-effort
            return
        task = loop.create_task(self._record_pipeline_state(checkout_id, state, dict(fields)))
        self._state_tasks.add(task)
        task.add_done_callback(self._state_tasks.discard)

    async def flush_state_records(self) -> None:
        """Await every state transition ``record_state`` has scheduled so far."""
        while self._state_tasks:
            await asyncio.gather(*tuple(self._state_tasks))

    async def _record_pipeline_state(
        self, checkout_id: str, state: str, fields: dict[str, object]
    ) -> None:
        """Map one pipeline state string onto the persisted row's lifecycle."""
        tenant_id, workspace_id = self._recording_scope
        mapped = _PIPELINE_STATE_TO_CHECKOUT_STATE.get(state)
        if mapped is None:
            return
        if mapped is RepoCheckoutState.FAILED:
            raw = fields.get("code")
            try:
                code = CheckoutFailureCode(str(raw))
            except ValueError:
                code = CheckoutFailureCode.GIT_ERROR
            await self.record_failure(
                tenant_id,
                checkout_id,
                code=code,
                redacted_detail="checkout pipeline reported failure",
                workspace_id=workspace_id,
            )
            return
        commit_sha = fields.get("commit_sha")
        tree_digest = fields.get("tree_digest")
        await self.transition_state(
            tenant_id,
            checkout_id,
            mapped,
            workspace_id=workspace_id,
            resolved_commit_sha=commit_sha if isinstance(commit_sha, str) else None,
            tree_digest=tree_digest if isinstance(tree_digest, str) else None,
        )

    # -- row hydration -----------------------------------------------------------

    def _row_to_checkout(self, row: dict[str, Any]) -> RepoCheckout:
        """Convert a database row to a :class:`RepoCheckout`."""
        created_at = _parse(row["created_at"])
        updated_at = _parse(row["updated_at"])
        if created_at is None or updated_at is None:  # pragma: no cover - NOT NULL
            raise ValueError("checkout row is missing its timestamps")
        failure_code = row["failure_code"]
        return RepoCheckout(
            id=str(row["id"]),
            tenant_id=str(row["tenant_id"]),
            workspace_id=None if row["workspace_id"] is None else str(row["workspace_id"]),
            repository_pk=str(row["repository_pk"]),
            installation_id=int(row["installation_id"]),
            repository_id=int(row["repository_id"]),
            repository_full_name=str(row["repository_full_name"]),
            requested_ref=str(row["requested_ref"]),
            resolved_commit_sha=row["resolved_commit_sha"],
            git_tree_id=row["git_tree_id"],
            tree_digest=row["tree_digest"],
            config_digest=row["config_digest"],
            manifest_digest=row["manifest_digest"],
            script_name=row["script_name"],
            state=RepoCheckoutState(str(row["state"])),
            failure_code=(
                None if failure_code is None else CheckoutFailureCode(str(failure_code))
            ),
            failure_detail=row["failure_detail"],
            staged_path=row["staged_path"],
            file_count=row["file_count"],
            size_bytes=row["size_bytes"],
            has_lfs_pointers=_tribool(row["has_lfs_pointers"]),
            cache_hit=bool(row["cache_hit"]),
            verified_at=_parse(row["verified_at"]),
            expires_at=_parse(row["expires_at"]),
            attestation_digest=row["attestation_digest"],
            attestation_signature=row["attestation_signature"],
            attestation_key_id=row["attestation_key_id"],
            attestation_algorithm=row["attestation_algorithm"],
            attestation_payload_json=row["attestation_payload_json"],
            created_at=created_at,
            updated_at=updated_at,
        )


@persistence_surface(
    "service.repo_runs",
    probe=named_isolation_probe("_drive_repo_runs"),
)
class SQLiteRepoRunRepository:
    """Persist repo runs and their lease-fenced worker claims."""

    def __init__(self, database: AsyncDatabase):
        self._database: AsyncDatabase = database

    def _runs(self, tenant_id: str | None, workspace_id: str | None) -> ScopedTable:
        """Bind the repo-runs table to one tenant+workspace scope."""
        return ScopedTable(
            self._database,
            SERVICE_SCOPE_REGISTRY,
            "service.repo_runs",
            _context(tenant_id, workspace_id),
        )

    @persistence_operation(ResourceOperation.CREATE)
    async def create(self, run: RepoRun) -> RepoRun:
        """Insert one PENDING repo run owned by the run's tenant and workspace.

        ``lease_expires_at`` doubles as the due horizon (the webhook
        ``next_attempt_at`` pattern); when the model leaves it ``None`` the row
        is stamped due at ``created_at``, i.e. claimable immediately.
        """
        due = run.lease_expires_at or run.created_at
        row = await self._runs(run.tenant_id, run.workspace_id).insert(
            {
                "id": run.id,
                "checkout_id": run.checkout_id,
                "script_name": run.script_name,
                "input_payload_json": run.input_payload_json,
                "state": run.state.value,
                "exit_code": run.exit_code,
                "failure_code": run.failure_code,
                "smoke_passed": None if run.smoke_passed is None else int(run.smoke_passed),
                "output_payload_json": run.output_payload_json,
                "claimed_by": run.claimed_by,
                "claim_generation": run.claim_generation,
                "claimed_at": _iso(run.claimed_at),
                "lease_expires_at": due.isoformat(),
                "created_at": run.created_at.isoformat(),
                "started_at": _iso(run.started_at),
                "finished_at": _iso(run.finished_at),
                "updated_at": run.updated_at.isoformat(),
            }
        )
        return self._row_to_run(row)

    @persistence_operation(ResourceOperation.READ)
    async def get(
        self, tenant_id: str | None, run_id: str, *, workspace_id: str | None = None
    ) -> RepoRun | None:
        """Load one repo run by id within the caller's scope, or ``None``."""
        row = await self._runs(tenant_id, workspace_id).select_one(where={"id": run_id})
        return None if row is None else self._row_to_run(row)

    @persistence_operation(ResourceOperation.ENUMERATE)
    async def list_runs(
        self,
        tenant_id: str | None,
        *,
        workspace_id: str | None = None,
        checkout_id: str | None = None,
    ) -> list[RepoRun]:
        """Return the scope's runs, oldest first, optionally for one checkout."""
        where = None if checkout_id is None else {"checkout_id": checkout_id}
        async with self._runs(tenant_id, workspace_id).transaction() as runs:
            rows = await runs.select(where=where, order_by=("created_at", "id"))
        return [self._row_to_run(row) for row in rows]

    @persistence_operation(
        ResourceOperation.ENUMERATE, ResourceOperation.READ, ResourceOperation.UPDATE
    )
    async def claim_pending(
        self,
        tenant_id: str | None,
        *,
        worker_id: str,
        workspace_id: str | None = None,
        lease_seconds: float = _DEFAULT_LEASE_SECONDS,
    ) -> ClaimedRepoRun | None:
        """Claim the oldest due run for this scope, lease-fenced by generation.

        Mirrors ``WebhookRepository.claim_pending_delivery``: within one
        transaction the chosen row is flipped to ``RUNNING`` and its
        ``lease_expires_at`` pushed ``lease_seconds`` into the future, and the
        compare-and-swap predicate includes all observed lease state
        (state, lease horizon, generation) so two concurrent claimants cannot
        both win the same generation. A claim covers two due cases:

        * ``PENDING`` -- never claimed, or its lease from a lost claim lapsed;
        * ``RUNNING`` -- its lease expired, i.e. a worker died mid-run.

        Returns the claimed run and its fencing generation, or ``None``.
        """
        now = utc_now()
        now_iso = now.isoformat()
        eligible = (RepoRunState.PENDING.value, RepoRunState.RUNNING.value)
        async with self._runs(tenant_id, workspace_id).transaction(write_lock=True) as runs:
            rows = await runs.select(
                where_lt={"lease_expires_at": now_iso},
                where_in={"state": eligible},
                order_by=("lease_expires_at", "created_at", "id"),
                limit=_CLAIM_CANDIDATE_LIMIT,
            )
            for row in rows:
                generation = int(row["claim_generation"]) + 1
                lease_until = now + timedelta(seconds=lease_seconds)
                started_at = row["started_at"] or now_iso
                claimed = await runs.update_if_matches(
                    {
                        "state": RepoRunState.RUNNING.value,
                        "claimed_by": worker_id,
                        "claimed_at": now_iso,
                        "lease_expires_at": lease_until.isoformat(),
                        "started_at": started_at,
                        "updated_at": now_iso,
                    },
                    where={
                        "id": row["id"],
                        "state": row["state"],
                        "lease_expires_at": row["lease_expires_at"],
                        "claim_generation": row["claim_generation"],
                    },
                    returning="id",
                    increment=("claim_generation",),
                )
                if claimed:
                    run = self._row_to_run(row).model_copy(
                        update={
                            "state": RepoRunState.RUNNING,
                            "claimed_by": worker_id,
                            "claimed_at": now,
                            "claim_generation": generation,
                            "lease_expires_at": lease_until,
                            "started_at": _parse(started_at),
                            "updated_at": now,
                        }
                    )
                    return ClaimedRepoRun(run, generation)
        return None

    @persistence_operation(ResourceOperation.UPDATE)
    async def finish(
        self,
        tenant_id: str | None,
        run_id: str,
        generation: int,
        *,
        exit_code: int,
        smoke_passed: bool | None = None,
        output_payload_json: str | None = None,
        workspace_id: str | None = None,
    ) -> bool:
        """Complete only the currently leased generation as SUCCEEDED."""
        ensure_bounded_payload(
            output_payload_json,
            cap=OUTPUT_PAYLOAD_CAP_BYTES,
            field_name="output_payload_json",
        )
        now_iso = utc_now().isoformat()
        async with self._runs(tenant_id, workspace_id).transaction(write_lock=True) as runs:
            return await runs.update_if_matches(
                {
                    "state": RepoRunState.SUCCEEDED.value,
                    "exit_code": exit_code,
                    "smoke_passed": None if smoke_passed is None else int(smoke_passed),
                    "output_payload_json": output_payload_json,
                    "finished_at": now_iso,
                    "updated_at": now_iso,
                },
                where={
                    "id": run_id,
                    "state": RepoRunState.RUNNING.value,
                    "claim_generation": generation,
                },
                returning="id",
            )

    @persistence_operation(ResourceOperation.UPDATE)
    async def fail(
        self,
        tenant_id: str | None,
        run_id: str,
        generation: int,
        *,
        failure_code: str,
        exit_code: int | None = None,
        smoke_passed: bool | None = None,
        workspace_id: str | None = None,
    ) -> bool:
        """Fail only the currently leased generation with a typed failure code."""
        now_iso = utc_now().isoformat()
        values: dict[str, Any] = {
            "state": RepoRunState.FAILED.value,
            "failure_code": failure_code,
            "finished_at": now_iso,
            "updated_at": now_iso,
        }
        if exit_code is not None:
            values["exit_code"] = exit_code
        if smoke_passed is not None:
            values["smoke_passed"] = int(smoke_passed)
        async with self._runs(tenant_id, workspace_id).transaction(write_lock=True) as runs:
            return await runs.update_if_matches(
                values,
                where={
                    "id": run_id,
                    "state": RepoRunState.RUNNING.value,
                    "claim_generation": generation,
                },
                returning="id",
            )

    @persistence_operation(ResourceOperation.UPDATE)
    async def cancel(
        self, tenant_id: str | None, run_id: str, *, workspace_id: str | None = None
    ) -> bool:
        """Cancel one run that is still PENDING (a claimed run must finish/fail)."""
        now_iso = utc_now().isoformat()
        async with self._runs(tenant_id, workspace_id).transaction(write_lock=True) as runs:
            return await runs.update_if_matches(
                {
                    "state": RepoRunState.CANCELLED.value,
                    "finished_at": now_iso,
                    "updated_at": now_iso,
                },
                where={"id": run_id, "state": RepoRunState.PENDING.value},
                returning="id",
            )

    # -- row hydration -----------------------------------------------------------

    def _row_to_run(self, row: dict[str, Any]) -> RepoRun:
        """Convert a database row to a :class:`RepoRun`."""
        created_at = _parse(row["created_at"])
        updated_at = _parse(row["updated_at"])
        if created_at is None or updated_at is None:  # pragma: no cover - NOT NULL
            raise ValueError("repo run row is missing its timestamps")
        return RepoRun(
            id=str(row["id"]),
            tenant_id=str(row["tenant_id"]),
            workspace_id=None if row["workspace_id"] is None else str(row["workspace_id"]),
            checkout_id=str(row["checkout_id"]),
            script_name=str(row["script_name"]),
            input_payload_json=row["input_payload_json"],
            state=RepoRunState(str(row["state"])),
            exit_code=row["exit_code"],
            failure_code=row["failure_code"],
            smoke_passed=_tribool(row["smoke_passed"]),
            output_payload_json=row["output_payload_json"],
            claimed_by=row["claimed_by"],
            claim_generation=int(row["claim_generation"]),
            claimed_at=_parse(row["claimed_at"]),
            lease_expires_at=_parse(row["lease_expires_at"]),
            created_at=created_at,
            started_at=_parse(row["started_at"]),
            finished_at=_parse(row["finished_at"]),
            updated_at=updated_at,
        )


__all__ = [
    "ClaimedRepoRun",
    "SQLiteRepoCheckoutRepository",
    "SQLiteRepoRunRepository",
]
