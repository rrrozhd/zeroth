"""Admin control surface for run management and metrics.

Provides:
  GET  /admin/runs              -- list runs by status (admin only)
  POST /admin/runs/{id}/cancel  -- forcibly fail a run
  POST /admin/runs/{id}/replay  -- replay a dead-letter run
  POST /admin/runs/{id}/interrupt -- interrupt a running run
  GET  /metrics                 -- Prometheus-format metrics
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict

from zeroth.contracts.governed import RunStatus
from zeroth.governance.audit.readiness import signer_is_available
from zeroth.runtime.orchestration.interrupts import InterruptManager
from zeroth.runtime.orchestration.token_lifecycle import TokenLifecycleAdapter
from zeroth.runtime.orchestration.token_snapshot_store import (
    TokenSnapshotConcurrencyError,
    TokenSnapshotStore,
)
from zeroth.runtime.runs import RunFailureState
from zeroth.service.api.authorization import (
    Permission,
    require_deployment_scope,
    require_permission,
)
from zeroth.service.api.run_api import RunStatusResponse, _serialize_run
from zeroth.service.service_audit import ServiceAuditRecorder


class AdminRunListResponse(BaseModel):
    """Response for the admin run list endpoint."""

    model_config = ConfigDict(extra="forbid")

    runs: list[RunStatusResponse]
    total: int


def register_admin_routes(app: FastAPI | APIRouter) -> None:
    """Register admin and metrics routes on the service app."""

    @app.get("/metrics")
    async def get_metrics(request: Request) -> Any:
        from fastapi.responses import PlainTextResponse

        bootstrap = _bootstrap(request)
        await require_permission(request, Permission.METRICS_READ)
        await require_deployment_scope(request, bootstrap.deployment)
        metrics_collector = getattr(bootstrap, "metrics_collector", None)
        if metrics_collector is None:
            return PlainTextResponse("# no metrics collector configured\n")
        return PlainTextResponse(
            metrics_collector.render_prometheus_text(),
            media_type="text/plain; version=0.0.4",
        )

    @app.get("/admin/runs", response_model=AdminRunListResponse)
    async def list_admin_runs(
        request: Request,
        status_filter: str | None = None,
        limit: int = Query(default=50, ge=1, le=1000),
        offset: int = Query(default=0, ge=0, le=1_000_000),
    ) -> AdminRunListResponse:
        bootstrap = _bootstrap(request)
        # Listing is read-only and every individual run is already readable
        # via GET /runs/{run_id} under RUN_READ — gating the list at
        # RUN_ADMIN only hid the console's run history from operators.
        # Mutations below (cancel/replay/interrupt) stay RUN_ADMIN.
        await require_permission(request, Permission.RUN_READ)
        await require_deployment_scope(request, bootstrap.deployment)
        runs = await bootstrap.run_repository.list_runs_for_scope(
            status=status_filter,
            limit=limit,
            offset=offset,
        )
        return AdminRunListResponse(
            runs=[_serialize_run(r) for r in runs],
            total=len(runs),
        )

    @app.post("/admin/runs/{run_id}/cancel", response_model=RunStatusResponse)
    async def cancel_run(request: Request, run_id: str) -> RunStatusResponse:
        bootstrap = _bootstrap(request)
        principal = await require_permission(request, Permission.RUN_ADMIN)
        await require_deployment_scope(request, bootstrap.deployment)
        run = await bootstrap.run_repository.get(run_id)
        if run is None or run.deployment_ref != bootstrap.deployment.deployment_ref:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
        audit = _run_control_audit_recorder(bootstrap)
        try:
            audit.ensure_signing_available()
        except RuntimeError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="run control audit signing is unavailable",
            ) from exc
        if run.status in {RunStatus.COMPLETED, RunStatus.FAILED}:
            # Cancellation is idempotent, but an older process may have
            # persisted the parent failure before it was able to terminalize
            # subgraph rows. Reissuing the same operator command is the safe
            # reconciliation mechanism for that interrupted boundary.
            if (
                run.status is RunStatus.FAILED
                and run.failure_state is not None
                and run.failure_state.reason == "operator_cancelled"
            ):
                try:
                    await _interrupt_active_drive(bootstrap, run_id)
                    descendant_count = await _cancel_active_descendants(bootstrap, run_id)
                except (ValueError, TokenSnapshotConcurrencyError) as exc:
                    raise _run_conflict(exc) from exc
                await _clear_run_lease(bootstrap, run_id)
                await audit.record_run_control_event(
                    actor=principal.to_actor(),
                    run=run,
                    transition="cancel_reconciled",
                    descendant_count=descendant_count,
                )
            return _serialize_run(run)
        try:
            await _apply_token_lifecycle(bootstrap, run_id, InterruptManager.cancel_run)
            run = await bootstrap.run_repository.cancel(
                run_id,
                bootstrap.deployment.deployment_ref,
                failure_state=RunFailureState(
                    reason="operator_cancelled", message="cancelled by admin"
                ),
            )
            # Persist the parent cancellation before stopping the drive so the
            # worker's CancelledError path cannot relabel an operator command as
            # a worker failure. Once the drive has stopped, no new subgraph
            # child can be created, so the bounded descendant walk below closes
            # every active child row without racing fresh fan-out.
            await _interrupt_active_drive(bootstrap, run_id)
            descendant_count = await _cancel_active_descendants(bootstrap, run_id)
        except (ValueError, TokenSnapshotConcurrencyError) as exc:
            raise _run_conflict(exc) from exc
        except KeyError as exc:
            if await bootstrap.run_repository.get(run_id) is None:
                raise _run_row_gone(run_id) from exc
            raise
        # Clear lease so any worker won't resume it.
        lease_manager = getattr(bootstrap, "lease_manager", None)
        if lease_manager is not None:
            await lease_manager.clear_lease(run_id)
        await audit.record_run_control_event(
            actor=principal.to_actor(),
            run=run,
            transition="cancelled",
            descendant_count=descendant_count,
        )
        return _serialize_run(run)

    @app.post("/admin/runs/{run_id}/replay", response_model=RunStatusResponse)
    async def replay_run(request: Request, run_id: str) -> RunStatusResponse:
        """Replay a dead-letter or failed run by resetting it to PENDING."""
        return await _replay_failed_run(request, run_id)

    @app.post("/admin/runs/{run_id}/interrupt", response_model=RunStatusResponse)
    async def interrupt_run(request: Request, run_id: str) -> RunStatusResponse:
        """Interrupt a running run (transitions to WAITING_INTERRUPT)."""
        bootstrap = _bootstrap(request)
        await require_permission(request, Permission.RUN_ADMIN)
        await require_deployment_scope(request, bootstrap.deployment)
        run = await bootstrap.run_repository.get(run_id)
        if run is None or run.deployment_ref != bootstrap.deployment.deployment_ref:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
        if run.status != RunStatus.RUNNING:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="only running runs can be interrupted",
            )
        try:
            await _apply_token_lifecycle(bootstrap, run_id, InterruptManager.pause_run)
            run = await bootstrap.run_repository.interrupt(
                run_id,
                bootstrap.deployment.deployment_ref,
            )
        except (ValueError, TokenSnapshotConcurrencyError) as exc:
            raise _run_conflict(exc) from exc
        except KeyError as exc:
            if await bootstrap.run_repository.get(run_id) is None:
                raise _run_row_gone(run_id) from exc
            raise
        worker = getattr(bootstrap, "worker", None)
        interrupt_active_run = getattr(worker, "interrupt_active_run", None)
        if callable(interrupt_active_run):
            await interrupt_active_run(run_id)
        return _serialize_run(run)


def _run_conflict(exc: Exception) -> HTTPException:
    """Render a lost race on a run as the 409 these routes already speak.

    F-10b: ``TokenSnapshotConcurrencyError`` subclasses ``RuntimeError``, not
    ``ValueError``, so once the token-lifecycle CAS grew a bounded retry budget
    its exhaustion fell straight past ``except ValueError`` into an unhandled
    500. It belongs with the ``ValueError`` the repository transition raises:
    both mean *somebody else changed this run first, nothing was written,
    resubmit*. The lifecycle CAS runs before the transition, so an exhausted
    budget leaves no partial state -- the whole defect was the surfaced
    contract.

    503 was the alternative and it advertises retryability more loudly, but it
    would report a healthy service as unavailable to every load balancer,
    circuit breaker and error-rate SLO that keys on 5xx, over a condition
    confined to one contended run. The detail string carries the retry hint
    instead, and stays distinguishable from "this run is not interruptible".

    Keyed on the concrete class on purpose: the sibling
    ``TokenSnapshotCorruptionError`` and ``TokenSnapshotWriteDisabledError``
    are ``RuntimeError`` too, and both are *permanent* -- dressing either as a
    retryable conflict would be worse than the 500 it gets today.

    Args:
        exc: The conflict raised by the lifecycle adapter or the repository.

    Returns:
        The 409 to raise in the route's ``except`` clause.
    """
    if isinstance(exc, TokenSnapshotConcurrencyError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"run {exc.run_id} lost a concurrent token-state update and nothing "
                "was written; retry the request"
            ),
        )
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))


async def _interrupt_active_drive(bootstrap: Any, run_id: str) -> None:
    """Stop an in-process drive after its durable operator state is written."""
    worker = getattr(bootstrap, "worker", None)
    interrupt_active_run = getattr(worker, "interrupt_active_run", None)
    if callable(interrupt_active_run):
        await interrupt_active_run(run_id)


async def _clear_run_lease(bootstrap: Any, run_id: str) -> None:
    lease_manager = getattr(bootstrap, "lease_manager", None)
    if lease_manager is not None:
        await lease_manager.clear_lease(run_id)


def _run_control_audit_recorder(bootstrap: Any) -> ServiceAuditRecorder:
    repository = getattr(bootstrap, "audit_repository", None)
    deployment = getattr(bootstrap, "deployment", None)
    if repository is None or deployment is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="run control audit is unavailable",
        )
    return ServiceAuditRecorder(
        repository=repository,
        deployment=deployment,
        require_signed=signer_is_available(getattr(bootstrap, "signer", None)),
    )


async def _cancel_active_descendants(bootstrap: Any, root_run_id: str) -> int:
    """Terminalize every active descendant after the root drive has stopped.

    Subgraph children are durable runs in their own deployment identities. A
    parent-only cancellation used to leave those rows RUNNING indefinitely;
    after a restart no worker serving the parent was authorized to claim the
    child deployment rows. The repository is already tenant/workspace scoped,
    and lineage supplies the only traversal edge needed here.

    Completed descendants are evidence and remain immutable. Active descendants
    get the same token fence, failure identity, worker interruption, and lease
    cleanup as the root. The visited set makes corrupt cyclic lineage bounded.
    """
    repository = bootstrap.run_repository
    pending = [root_run_id]
    visited = {root_run_id}
    cancelled_count = 0
    while pending:
        parent_run_id = pending.pop(0)
        for child in await repository.list_child_runs(parent_run_id):
            if child.run_id in visited:
                continue
            visited.add(child.run_id)
            pending.append(child.run_id)
            if child.status in {RunStatus.COMPLETED, RunStatus.FAILED}:
                continue
            await _apply_token_lifecycle(bootstrap, child.run_id, InterruptManager.cancel_run)
            await _transition_run(
                bootstrap,
                child.run_id,
                RunStatus.FAILED,
                failure_state=RunFailureState(
                    reason="operator_cancelled",
                    message=f"cancelled with ancestor {root_run_id}",
                ),
            )
            await _interrupt_active_drive(bootstrap, child.run_id)
            await _clear_run_lease(bootstrap, child.run_id)
            cancelled_count += 1
    return cancelled_count


def _token_state_gone(run_id: str) -> HTTPException:
    """Render a token snapshot erased mid-request as its own 409.

    F-10c: the routes check whether a run owns token state and only then does
    ``TokenLifecycleAdapter._apply`` reload it. Retention erasure lands in that
    gap -- ``ErasureService.erase_run`` deletes the snapshot and *keeps* the
    redacted run row -- as does ``delete_run``. The adapter answers a missing
    snapshot with ``KeyError``, which is not a ``ValueError`` either, so this
    TOCTOU also reached the operator as an unhandled 500.

    409 rather than 404 or 410 because the run itself is usually still there:
    erasure removes token state and leaves the row, so 404 would deny a run the
    very next request can still read, and 410 would claim a permanence that is
    simply false. Nor is the advice hollow -- re-issuing *converges*, because
    the second request's existence check now sees no snapshot, routes around
    the lifecycle entirely and cancels or interrupts the run outright (or 404s
    honestly, if the whole run was deleted).

    The detail deliberately shares no phrase with either sibling conflict --
    CAS contention ("lost a concurrent token-state update", retry verbatim) or
    "only running runs can be interrupted" -- because all three are 409 and the
    string is the only thing that tells an operator which one they hit.

    Args:
        run_id: The run whose token state vanished mid-request.

    Returns:
        The 409 to raise in place of the adapter's ``KeyError``.
    """
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=(
            f"run {run_id} no longer has token state (erased or deleted) so no lifecycle "
            "command was applied and nothing was written; re-issue the request"
        ),
    )


async def _apply_token_lifecycle(
    bootstrap: Any,
    run_id: str,
    command: Callable[[InterruptManager, str], Awaitable[object]],
) -> None:
    """Drive one lifecycle command for a run that still owns token state.

    Module level for two reasons. It keeps the ``manager is not None`` branch
    out of ``register_admin_routes``, whose mccabe count the commit gate
    ratchets and which absorbs every branch of every route closure. And it
    scopes the ``KeyError`` catch to the lifecycle call alone: widening it to
    the routes' own ``except`` would swallow ``transition``'s ``KeyError`` --
    a *different* condition, the run row itself being gone, whose honest answer
    is 404 -- and any incidental ``KeyError`` from a bug below.

    Even so the class is far too common to trust on its own, so the missing
    snapshot is confirmed by re-reading it before the ``KeyError`` is dressed
    as a conflict; anything else propagates untouched and keeps its 500. This
    is the same discipline the contention fix applied when it keyed on
    ``TokenSnapshotConcurrencyError`` rather than on ``RuntimeError``.

    Args:
        bootstrap: The service bootstrap holding the run repository.
        run_id: The run to drive.
        command: The unbound ``InterruptManager`` coroutine to apply.

    Raises:
        HTTPException: 409 when the run's token state vanished mid-request.
    """
    manager = await _token_interrupt_manager(bootstrap, run_id)
    if manager is None:
        return
    try:
        await command(manager, run_id)
    except KeyError as exc:
        if await bootstrap.run_repository.get_token_snapshot(run_id) is not None:
            raise
        raise _token_state_gone(run_id) from exc


def _run_row_gone(run_id: str) -> HTTPException:
    """Render a run row deleted mid-command as the 404 it actually is.

    F-10d: ``RunRepository.transition`` re-reads the run before it writes and
    raises ``KeyError(run_id)`` when the row is gone -- not a ``ValueError``
    either, so the last unhandled failure on these routes reached the operator
    as a 500.

    404 rather than the 409 its two neighbours use, because this condition is
    *permanent* where theirs are transient. A contended CAS converges on a
    retry and an erased token snapshot converges on a retry; a deleted run row
    never comes back, and the very next request is answered by the route's own
    unknown-run guard. Dressing it as a conflict would advertise a retry that
    provably cannot succeed -- the same mistake, in the opposite direction, as
    dressing the permanent snapshot errors as retryable conflicts.

    Removal is ``RunRepository.delete``'s doing, or an out-of-band delete of the
    same row. Deliberately *not* retention: ``ErasureService`` redacts the run
    row in place and keeps it, which is exactly why the vanished *snapshot* two
    functions above answers 409 and this answers 404.

    The detail shares no phrase with the three conflicts or with the guard's own
    ``"run not found"``: an operator reading a log has to separate "the run you
    asked about isn't there" from "the run you were operating on was removed
    underneath the command", because only the second reports a concurrent
    deletion.

    Args:
        run_id: The run whose row vanished mid-request.

    Returns:
        The 404 to raise in place of the repository's ``KeyError``.
    """
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=(
            f"run {run_id} was deleted while the command was in flight, so nothing was "
            "written and re-issuing cannot succeed"
        ),
    )


async def _transition_run(
    bootstrap: Any,
    run_id: str,
    new_status: RunStatus,
    *,
    failure_state: RunFailureState | None = None,
) -> Any:
    """Drive one operator status transition, answering a deleted run row honestly.

    Module level for the two reasons ``_apply_token_lifecycle`` is. It keeps
    branches out of ``register_admin_routes``, whose mccabe count the commit
    gate ratchets and which absorbs every branch of every route closure. And it
    scopes the ``KeyError`` catch to the one call that raises this condition:
    widening it to the routes' own ``except`` would swallow the lifecycle's
    *token-snapshot* ``KeyError`` -- a transient condition whose honest answer
    is the 409 above -- along with any incidental ``KeyError`` from a bug below.

    ``KeyError`` is far too common a class to trust on its own, and unlike the
    contention fix there is no concrete class to key on, so the row's absence is
    confirmed by re-reading it before the 404 is issued. That re-read is not
    guarding against a stale read of a row that is really gone; it is the only
    way to separate ``transition``'s missing-row ``KeyError`` from a
    ``KeyError`` raised anywhere else inside the same call, which propagates
    untouched and keeps its 500.

    Args:
        bootstrap: The service bootstrap holding the run repository.
        run_id: The run to transition.
        new_status: The status to move the run to.
        failure_state: Failure identity to record alongside the transition.

    Returns:
        The persisted run as the repository stored it.

    Raises:
        HTTPException: 404 when the run's row was deleted mid-command.
    """
    try:
        return await bootstrap.run_repository.transition(
            run_id, new_status, failure_state=failure_state
        )
    except KeyError as exc:
        if await bootstrap.run_repository.get(run_id) is not None:
            raise
        raise _run_row_gone(run_id) from exc


async def _replay_failed_run(request: Request, run_id: str) -> RunStatusResponse:
    """Body of ``POST /admin/runs/{run_id}/replay``, kept out of the route closure.

    It lives at module level only so ``register_admin_routes`` stays under the
    mccabe ceiling the commit gate ratchets: every branch of every nested route
    handler counts toward the registering function. The logic below is byte-for-
    byte the handler's own and runs in the same place in the request.

    A02-17: the reset is one guarded statement, so it either happens whole
    or not at all. It used to be three writes -- clear the failure metadata,
    zero ``failure_count`` and the lease, then transition -- and a failure or
    a 409 at the last one left a run that was still FAILED but had lost the
    identity of its failure: no ``failure_state`` for the dead-letter view to
    match, no lease, and a retry count reset to zero.

    The repository owns the transaction and adds its trusted tenant/workspace
    predicates. FAILED plus the deployment ref form the remaining CAS, so a
    concurrent state change loses without touching any colliding foreign row.
    """
    bootstrap = _bootstrap(request)
    await require_permission(request, Permission.RUN_ADMIN)
    await require_deployment_scope(request, bootstrap.deployment)
    repository = bootstrap.run_repository
    deployment_ref = bootstrap.deployment.deployment_ref
    run = await repository.get(run_id)
    if run is None or run.deployment_ref != deployment_ref:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
    if run.status != RunStatus.FAILED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="only failed runs can be replayed",
        )
    if not await repository.replay_failed(run_id, deployment_ref):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="only failed runs can be replayed",
        )
    replayed = await repository.get(run_id)
    if replayed is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="run not found")
    return _serialize_run(replayed)


def _bootstrap(request: Request) -> Any:
    bootstrap = getattr(request.app.state, "bootstrap", None)
    if bootstrap is None:
        raise RuntimeError("service bootstrap is not configured")
    return bootstrap


async def _token_interrupt_manager(bootstrap: Any, run_id: str) -> InterruptManager | None:
    """Return lifecycle routing only for runs that already own token state."""
    repository = bootstrap.run_repository
    if not isinstance(repository, TokenSnapshotStore):
        return None
    if await repository.get_token_snapshot(run_id) is None:
        return None
    return InterruptManager(token_lifecycle=TokenLifecycleAdapter(repository))
