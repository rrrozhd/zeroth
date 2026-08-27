"""Tests for admin run management endpoints."""

from __future__ import annotations

import contextlib

from fastapi.testclient import TestClient

from tests.service.helpers import (
    admin_headers,
    agent_graph,
    api_key_headers,
    deploy_service,
    operator_headers,
    scoped_auth_config,
)
from zeroth.contracts.graph.token_snapshot import TokenEngineSnapshotState
from zeroth.governance.identity import ServiceRole
from zeroth.runtime.orchestration.token_scheduler import initialize_token_snapshot
from zeroth.runtime.orchestration.token_snapshot_store import (
    TokenSnapshotConcurrencyError,
    TokenSnapshotWriteDisabledError,
)
from zeroth.runtime.runs import Run, RunFailureState, RunStatus
from zeroth.service.bootstrap import bootstrap_app
from zeroth.service.bootstrap.factory import bootstrap_scoped_app

DEPLOYMENT = "admin-test"


async def _make_service_and_app(sqlite_db, graph_id: str, deployment_ref: str):
    service, _ = await deploy_service(
        sqlite_db,
        agent_graph(graph_id=graph_id),
        deployment_ref=deployment_ref,
    )
    app = await bootstrap_app(sqlite_db, deployment_ref=service.deployment.deployment_ref)
    app.state.bootstrap = service
    return service, app


async def test_list_admin_runs_requires_auth(sqlite_db) -> None:
    """Listing is RUN_READ (read-only) since v0.4.10 — but never anonymous."""
    service, app = await _make_service_and_app(sqlite_db, "graph-admin-list", DEPLOYMENT + "-list")

    with TestClient(app) as client:
        r = client.get("/admin/runs")

    assert r.status_code == 401


async def test_list_admin_runs_returns_runs(sqlite_db) -> None:
    service, app = await _make_service_and_app(
        sqlite_db, "graph-admin-list2", DEPLOYMENT + "-list2"
    )

    with TestClient(app) as client:
        # Create a run.
        client.post(
            "/runs",
            json={"input_payload": {"value": 1}},
            headers=operator_headers(),
        )
        r = client.get("/admin/runs", headers=admin_headers())

    assert r.status_code == 200
    body = r.json()
    assert "runs" in body
    assert "total" in body
    assert body["total"] >= 1


async def test_list_admin_runs_is_tenant_wide_across_deployments(sqlite_db) -> None:
    service, app = await _make_service_and_app(
        sqlite_db, "graph-admin-tenant-history", DEPLOYMENT + "-tenant-history"
    )
    other = Run(
        graph_version_ref="other-graph@1",
        deployment_ref="other-deployment",
        tenant_id=service.deployment.tenant_id,
        status=RunStatus.COMPLETED,
        final_output={"ok": True},
    )
    await service.run_repository.create(other)

    with TestClient(app) as client:
        response = client.get("/admin/runs", headers=admin_headers())
        detail = client.get(f"/runs/{other.run_id}", headers=admin_headers())

    assert response.status_code == 200
    deployment_refs = {run["deployment_ref"] for run in response.json()["runs"]}
    assert "other-deployment" in deployment_refs
    assert detail.status_code == 200
    assert detail.json()["deployment_ref"] == "other-deployment"


async def test_list_admin_runs_rejects_out_of_bounds_limit(sqlite_db) -> None:
    """A02-12: limit/offset declare bounds instead of accepting anything an int fits."""
    service, app = await _make_service_and_app(
        sqlite_db, "graph-admin-bounds", DEPLOYMENT + "-bounds"
    )

    with TestClient(app) as client:
        too_high = client.get("/admin/runs?limit=1000000", headers=admin_headers())
        zero = client.get("/admin/runs?limit=0", headers=admin_headers())
        negative_offset = client.get("/admin/runs?offset=-1", headers=admin_headers())

    assert too_high.status_code == 422
    assert zero.status_code == 422
    assert negative_offset.status_code == 422


async def test_cancel_run_requires_admin_role(sqlite_db) -> None:
    service, app = await _make_service_and_app(
        sqlite_db,
        "graph-cancel-auth",
        DEPLOYMENT + "-cancel-auth",
    )

    with TestClient(app) as client:
        r1 = client.post(
            "/runs",
            json={"input_payload": {"value": 1}},
            headers=operator_headers(),
        )
        run_id = r1.json()["run_id"]
        r = client.post(f"/admin/runs/{run_id}/cancel", headers=operator_headers())

    assert r.status_code == 403


async def test_cancel_run_transitions_to_failed(sqlite_db) -> None:
    service, app = await _make_service_and_app(sqlite_db, "graph-cancel", DEPLOYMENT + "-cancel")

    with TestClient(app) as client:
        r1 = client.post(
            "/runs",
            json={"input_payload": {"value": 1}},
            headers=operator_headers(),
        )
        assert r1.status_code == 202
        run_id = r1.json()["run_id"]

        r = client.post(f"/admin/runs/{run_id}/cancel", headers=admin_headers())

    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "failed"


async def test_cancel_run_stops_the_active_worker_drive(sqlite_db, monkeypatch) -> None:
    service, app = await _make_service_and_app(
        sqlite_db,
        "graph-cancel-active-drive",
        DEPLOYMENT + "-cancel-active-drive",
    )
    interrupted: list[str] = []

    async def interrupt_active_run(run_id: str) -> None:
        interrupted.append(run_id)

    monkeypatch.setattr(
        service.worker,
        "interrupt_active_run",
        interrupt_active_run,
        raising=False,
    )

    with TestClient(app) as client:
        created = client.post(
            "/runs",
            json={"input_payload": {"value": 1}},
            headers=operator_headers(),
        )
        run_id = created.json()["run_id"]
        await service.run_repository.transition(run_id, RunStatus.RUNNING)

        response = client.post(f"/admin/runs/{run_id}/cancel", headers=admin_headers())

    assert response.status_code == 200
    assert interrupted == [run_id]
    audits = await service.audit_repository.list_by_run(run_id)
    control = [record for record in audits if record.node_id == "run.control.cancelled"]
    assert len(control) == 1
    assert control[0].actor is not None
    assert control[0].actor.subject == "admin-1"
    assert control[0].status == "completed"


async def test_cancel_run_cascades_to_active_descendants(sqlite_db, monkeypatch) -> None:
    service, app = await _make_service_and_app(
        sqlite_db,
        "graph-cancel-descendants",
        DEPLOYMENT + "-cancel-descendants",
    )
    parent = Run(
        graph_version_ref=service.deployment.graph_version_ref,
        deployment_ref=service.deployment.deployment_ref,
        tenant_id=service.deployment.tenant_id,
        status=RunStatus.RUNNING,
    )
    completed_child = Run(
        graph_version_ref="child-completed@1",
        deployment_ref="child-completed",
        tenant_id=service.deployment.tenant_id,
        parent_run_id=parent.run_id,
        status=RunStatus.COMPLETED,
        final_output={"preserved": True},
    )
    active_child = Run(
        graph_version_ref="child-active@1",
        deployment_ref="child-active",
        tenant_id=service.deployment.tenant_id,
        parent_run_id=parent.run_id,
        status=RunStatus.RUNNING,
    )
    active_grandchild = Run(
        graph_version_ref="grandchild-active@1",
        deployment_ref="grandchild-active",
        tenant_id=service.deployment.tenant_id,
        parent_run_id=active_child.run_id,
        status=RunStatus.RUNNING,
    )
    for run in (parent, completed_child, active_child, active_grandchild):
        await service.run_repository.create(run)

    interrupted: list[str] = []

    async def interrupt_active_run(run_id: str) -> None:
        interrupted.append(run_id)

    monkeypatch.setattr(
        service.worker,
        "interrupt_active_run",
        interrupt_active_run,
        raising=False,
    )

    with TestClient(app) as client:
        response = client.post(
            f"/admin/runs/{parent.run_id}/cancel",
            headers=admin_headers(),
        )

    assert response.status_code == 200
    persisted_parent = await service.run_repository.get(parent.run_id)
    persisted_completed = await service.run_repository.get(completed_child.run_id)
    persisted_child = await service.run_repository.get(active_child.run_id)
    persisted_grandchild = await service.run_repository.get(active_grandchild.run_id)
    assert persisted_parent is not None
    assert persisted_completed is not None
    assert persisted_child is not None
    assert persisted_grandchild is not None
    assert persisted_parent.failure_state is not None
    assert persisted_parent.failure_state.reason == "operator_cancelled"
    assert persisted_completed.status is RunStatus.COMPLETED
    assert persisted_completed.final_output == {"preserved": True}
    assert persisted_child.status is RunStatus.FAILED
    assert persisted_child.failure_state is not None
    assert persisted_child.failure_state.reason == "operator_cancelled"
    assert persisted_grandchild.status is RunStatus.FAILED
    assert persisted_grandchild.failure_state is not None
    assert persisted_grandchild.failure_state.reason == "operator_cancelled"
    assert interrupted == [parent.run_id, active_child.run_id, active_grandchild.run_id]


async def test_repeated_cancel_reconciles_orphaned_active_descendant(sqlite_db) -> None:
    service, app = await _make_service_and_app(
        sqlite_db,
        "graph-cancel-reconcile-descendant",
        DEPLOYMENT + "-cancel-reconcile-descendant",
    )
    parent = Run(
        graph_version_ref=service.deployment.graph_version_ref,
        deployment_ref=service.deployment.deployment_ref,
        tenant_id=service.deployment.tenant_id,
        status=RunStatus.FAILED,
        failure_state=RunFailureState(
            reason="operator_cancelled",
            message="cancelled by admin",
        ),
    )
    child = Run(
        graph_version_ref="orphaned-child@1",
        deployment_ref="orphaned-child",
        tenant_id=service.deployment.tenant_id,
        parent_run_id=parent.run_id,
        status=RunStatus.RUNNING,
    )
    await service.run_repository.create(parent)
    await service.run_repository.create(child)

    with TestClient(app) as client:
        response = client.post(
            f"/admin/runs/{parent.run_id}/cancel",
            headers=admin_headers(),
        )

    persisted = await service.run_repository.get(child.run_id)
    assert response.status_code == 200
    assert persisted is not None
    assert persisted.status is RunStatus.FAILED
    assert persisted.failure_state is not None
    assert persisted.failure_state.reason == "operator_cancelled"


async def test_cancel_run_fences_existing_token_snapshot(sqlite_db) -> None:
    service, app = await _make_service_and_app(
        sqlite_db, "graph-cancel-token", DEPLOYMENT + "-cancel-token"
    )

    with TestClient(app) as client:
        created = client.post(
            "/runs", json={"input_payload": {"value": 1}}, headers=operator_headers()
        )
        run_id = created.json()["run_id"]
        snapshot = initialize_token_snapshot(run_id=run_id, root_node_id="agent-step", payload={})
        await service.run_repository.compare_and_swap_token_snapshot(
            run_id, expected_revision=None, snapshot=snapshot
        )

        response = client.post(f"/admin/runs/{run_id}/cancel", headers=admin_headers())

    persisted = await service.run_repository.get_token_snapshot(run_id)
    assert response.status_code == 200
    assert persisted is not None
    assert persisted.state is TokenEngineSnapshotState.CANCELLED


async def test_cancel_run_clears_active_lease(sqlite_db) -> None:
    service, app = await _make_service_and_app(
        sqlite_db,
        "graph-cancel-lease",
        DEPLOYMENT + "-cancel-lease",
    )

    with TestClient(app) as client:
        create_response = client.post(
            "/runs",
            json={"input_payload": {"value": 1}},
            headers=operator_headers(),
        )
        run_id = create_response.json()["run_id"]

        async with sqlite_db.transaction() as connection:
            await connection.execute(
                """
                UPDATE runs
                SET lease_worker_id = 'worker-1',
                    lease_acquired_at = '2026-03-28T00:00:00+00:00',
                    lease_expires_at = '2026-03-28T00:01:00+00:00'
                WHERE run_id = ?
                """,
                (run_id,),
            )

        response = client.post(f"/admin/runs/{run_id}/cancel", headers=admin_headers())

    assert response.status_code == 200
    async with sqlite_db.transaction() as connection:
        row = await connection.fetch_one(
            """
            SELECT lease_worker_id, lease_acquired_at, lease_expires_at
            FROM runs
            WHERE run_id = ?
            """,
            (run_id,),
        )
    assert row["lease_worker_id"] is None
    assert row["lease_acquired_at"] is None
    assert row["lease_expires_at"] is None


async def test_cancel_run_404_for_unknown_run(sqlite_db) -> None:
    service, app = await _make_service_and_app(
        sqlite_db, "graph-cancel-404", DEPLOYMENT + "-cancel-404"
    )

    with TestClient(app) as client:
        r = client.post("/admin/runs/nonexistent-run/cancel", headers=admin_headers())

    assert r.status_code == 404


async def test_replay_run_requires_admin_role(sqlite_db) -> None:
    service, app = await _make_service_and_app(
        sqlite_db,
        "graph-replay-auth",
        DEPLOYMENT + "-replay-auth",
    )

    with TestClient(app) as client:
        r1 = client.post(
            "/runs",
            json={"input_payload": {"value": 1}},
            headers=operator_headers(),
        )
        run_id = r1.json()["run_id"]
        # Cancel first so it's FAILED.
        client.post(f"/admin/runs/{run_id}/cancel", headers=admin_headers())
        r = client.post(f"/admin/runs/{run_id}/replay", headers=operator_headers())

    assert r.status_code == 403


async def test_replay_dead_letter_run_requeues(sqlite_db) -> None:
    """A FAILED run can be replayed back to PENDING status."""
    service, app = await _make_service_and_app(sqlite_db, "graph-replay", DEPLOYMENT + "-replay")

    with TestClient(app) as client:
        # Create a run and cancel it to get it to FAILED.
        r1 = client.post(
            "/runs",
            json={"input_payload": {"value": 1}},
            headers=operator_headers(),
        )
        assert r1.status_code == 202
        run_id = r1.json()["run_id"]
        # Cancel — puts run in FAILED state.
        r_cancel = client.post(f"/admin/runs/{run_id}/cancel", headers=admin_headers())
        assert r_cancel.status_code == 200

        # Replay — should go back to queued/pending.
        r_replay = client.post(f"/admin/runs/{run_id}/replay", headers=admin_headers())

    assert r_replay.status_code == 200
    body = r_replay.json()
    assert body["status"] == "queued"


async def test_replay_non_failed_run_returns_conflict(sqlite_db) -> None:
    service, app = await _make_service_and_app(
        sqlite_db, "graph-replay-conflict", DEPLOYMENT + "-rc"
    )

    with TestClient(app) as client:
        r1 = client.post(
            "/runs",
            json={"input_payload": {"value": 1}},
            headers=operator_headers(),
        )
        run_id = r1.json()["run_id"]
        # Try to replay a PENDING/RUNNING run (not FAILED yet).
        r = client.post(f"/admin/runs/{run_id}/replay", headers=admin_headers())

    assert r.status_code == 409


async def test_list_admin_runs_filters_by_status(sqlite_db) -> None:
    service, app = await _make_service_and_app(
        sqlite_db, "graph-admin-filter", DEPLOYMENT + "-filter"
    )

    with TestClient(app) as client:
        r1 = client.post(
            "/runs",
            json={"input_payload": {"value": 1}},
            headers=operator_headers(),
        )
        run_id = r1.json()["run_id"]

        # Filter by "failed" should return empty until we cancel.
        r_empty = client.get("/admin/runs?status_filter=FAILED", headers=admin_headers())
        assert r_empty.status_code == 200
        assert r_empty.json()["total"] == 0

        # Cancel the run to get it into FAILED state.
        client.post(f"/admin/runs/{run_id}/cancel", headers=admin_headers())

        # Now filter should return the run.
        r_filtered = client.get("/admin/runs?status_filter=FAILED", headers=admin_headers())
        assert r_filtered.status_code == 200
        assert r_filtered.json()["total"] >= 1


async def test_admin_routes_hide_service_from_foreign_tenant_admin(sqlite_db) -> None:
    auth_config = scoped_auth_config(
        ("tenant-a-admin", "tenant-a-admin-key", ServiceRole.ADMIN, "tenant-a", None),
        ("tenant-b-admin", "tenant-b-admin-key", ServiceRole.ADMIN, "tenant-b", None),
        ("tenant-a-operator", "tenant-a-operator-key", ServiceRole.OPERATOR, "tenant-a", None),
    )
    service, _ = await deploy_service(
        sqlite_db,
        agent_graph(graph_id="graph-admin-scope"),
        deployment_ref=DEPLOYMENT + "-scope",
        auth_config=auth_config,
        tenant_id="tenant-a",
    )
    app = await bootstrap_scoped_app(
        sqlite_db,
        deployment_ref=service.deployment.deployment_ref,
        tenant_id=service.deployment.tenant_id,
        workspace_id=service.deployment.workspace_id,
        auth_config=auth_config,
    )
    app.state.bootstrap = service

    with TestClient(app) as client:
        create_response = client.post(
            "/runs",
            json={"input_payload": {"value": 1}},
            headers=api_key_headers("tenant-a-operator-key"),
        )
        run_id = create_response.json()["run_id"]
        response = client.post(
            f"/admin/runs/{run_id}/cancel",
            headers=api_key_headers("tenant-b-admin-key"),
        )

    assert response.status_code == 404


async def test_interrupt_run_returns_waiting_interrupt_status(sqlite_db) -> None:
    service, app = await _make_service_and_app(
        sqlite_db,
        "graph-admin-interrupt",
        DEPLOYMENT + "-interrupt",
    )
    service.worker = None

    with TestClient(app) as client:
        create_response = client.post(
            "/runs",
            json={"input_payload": {"value": 1}},
            headers=operator_headers(),
        )
        run_id = create_response.json()["run_id"]

        run = await service.run_repository.get(run_id)
        assert run is not None
        await service.run_repository.transition(run_id, RunStatus.RUNNING)

        response = client.post(f"/admin/runs/{run_id}/interrupt", headers=admin_headers())

    assert response.status_code == 200
    assert response.json()["status"] == "waiting_interrupt"


async def test_interrupt_run_stops_the_active_worker_drive(sqlite_db, monkeypatch) -> None:
    service, app = await _make_service_and_app(
        sqlite_db,
        "graph-admin-interrupt-drive",
        DEPLOYMENT + "-interrupt-drive",
    )

    interrupted: list[str] = []

    async def interrupt_active_run(run_id: str) -> None:
        interrupted.append(run_id)

    monkeypatch.setattr(
        service.worker,
        "interrupt_active_run",
        interrupt_active_run,
        raising=False,
    )

    with TestClient(app) as client:
        created = client.post(
            "/runs",
            json={"input_payload": {"value": 1}},
            headers=operator_headers(),
        )
        run_id = created.json()["run_id"]
        await service.run_repository.transition(run_id, RunStatus.RUNNING)

        response = client.post(f"/admin/runs/{run_id}/interrupt", headers=admin_headers())

    assert response.status_code == 200
    assert response.json()["status"] == "waiting_interrupt"
    assert interrupted == [run_id]


async def test_interrupt_run_pauses_existing_token_snapshot(sqlite_db) -> None:
    service, app = await _make_service_and_app(
        sqlite_db,
        "graph-admin-token-interrupt",
        DEPLOYMENT + "-token-interrupt",
    )
    service.worker = None

    with TestClient(app) as client:
        created = client.post(
            "/runs", json={"input_payload": {"value": 1}}, headers=operator_headers()
        )
        run_id = created.json()["run_id"]
        await service.run_repository.transition(run_id, RunStatus.RUNNING)
        snapshot = initialize_token_snapshot(run_id=run_id, root_node_id="agent-step", payload={})
        await service.run_repository.compare_and_swap_token_snapshot(
            run_id, expected_revision=None, snapshot=snapshot
        )

        response = client.post(f"/admin/runs/{run_id}/interrupt", headers=admin_headers())

    persisted = await service.run_repository.get_token_snapshot(run_id)
    assert response.status_code == 200
    assert persisted is not None
    assert persisted.state is TokenEngineSnapshotState.PAUSED


async def test_operator_can_list_runs(sqlite_db) -> None:
    """Run listing is read-only: RUN_READ suffices; mutations stay RUN_ADMIN."""
    from tests.service.helpers import operator_headers

    service, _ = await deploy_service(sqlite_db, agent_graph(graph_id="graph-admin-list-op"))
    app = await bootstrap_app(
        sqlite_db,
        deployment_ref=service.deployment.deployment_ref,
        auth_config=service.auth_config,
    )
    app.state.bootstrap = service

    with TestClient(app) as client:
        r = client.get("/admin/runs", headers=operator_headers())

    assert r.status_code == 200


class _FailOnMarkerConnection:
    """Wraps a real connection and refuses one statement, by substring."""

    def __init__(self, connection, marker: str) -> None:
        self._connection = connection
        self._marker = marker

    def _guard(self, sql: str) -> None:
        if self._marker in sql:
            raise RuntimeError(f"injected write failure on {self._marker!r}")

    async def execute(self, sql, params=()):  # noqa: ANN001
        self._guard(sql)
        return await self._connection.execute(sql, params)

    async def fetch_one(self, sql, params=()):  # noqa: ANN001
        self._guard(sql)
        return await self._connection.fetch_one(sql, params)

    async def fetch_all(self, sql, params=()):  # noqa: ANN001
        self._guard(sql)
        return await self._connection.fetch_all(sql, params)

    async def execute_script(self, sql):  # noqa: ANN001
        return await self._connection.execute_script(sql)


@contextlib.contextmanager
def _refusing_write(database, marker: str):
    """Make every statement containing ``marker`` raise, for the duration."""
    original = database.transaction

    @contextlib.asynccontextmanager
    async def patched(*, write_lock: bool = False):
        async with original(write_lock=write_lock) as connection:
            yield _FailOnMarkerConnection(connection, marker)

    database.transaction = patched
    try:
        yield
    finally:
        del database.transaction


async def _runs_row(database, run_id: str) -> dict:
    async with database.transaction() as conn:
        row = await conn.fetch_one(
            "SELECT status, error, failure_state, failure_count,"
            " lease_worker_id, lease_expires_at FROM runs WHERE run_id = ?",
            (run_id,),
        )
    assert row is not None
    return dict(row)


async def test_replay_that_fails_midway_leaves_the_run_wholly_unreset(sqlite_db) -> None:
    """A02-17: a replay is all-or-nothing, so a failed one is simply retryable.

    The reset used to be three separate writes -- clear the failure metadata,
    zero ``failure_count`` and the lease, then transition to PENDING. A failure
    (or a 409) at the last one left a run that was still FAILED but had lost the
    identity of its failure: no ``failure_state`` for the dead-letter view to
    match, no lease, and a zeroed retry count. Refusing the reset write must
    leave every one of those columns exactly as it found them.
    """
    service, app = await _make_service_and_app(
        sqlite_db, "graph-replay-atomic", DEPLOYMENT + "-replay-atomic"
    )
    with TestClient(app) as client:
        run_id = client.post(
            "/runs", json={"input_payload": {"value": 1}}, headers=operator_headers()
        ).json()["run_id"]
        assert (
            client.post(f"/admin/runs/{run_id}/cancel", headers=admin_headers()).status_code == 200
        )

    database = service.run_repository.database
    async with database.transaction() as conn:
        await conn.execute(
            "UPDATE runs SET failure_count = 3, lease_worker_id = 'w1',"
            " lease_expires_at = '2099-01-01T00:00:00+00:00' WHERE run_id = ?",
            (run_id,),
        )
    before = await _runs_row(database, run_id)
    assert before["status"] == RunStatus.FAILED.value
    assert before["failure_state"] is not None

    with _refusing_write(database, "failure_count = ?"):
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post(f"/admin/runs/{run_id}/replay", headers=admin_headers())
    assert response.status_code >= 500

    after = await _runs_row(database, run_id)
    assert after["status"] == RunStatus.FAILED.value
    assert after["failure_state"] is not None, "failure identity destroyed by a failed replay"
    assert after["error"] == before["error"]
    assert after["failure_count"] == 3
    assert after["lease_worker_id"] == "w1"
    assert after["lease_expires_at"] == "2099-01-01T00:00:00+00:00"


# ---------------------------------------------------------------------------
# F-10b -- a contended lifecycle CAS is a conflict, not a server error
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _always_losing_snapshot_cas(repository):
    """Make every token-snapshot CAS on *repository* lose, and count the losses.

    This is exactly what a sustained-contention window looks like to
    ``TokenLifecycleAdapter._apply``: it reloads and re-proposes on every
    attempt and never wins, so after its bounded budget it re-raises the last
    :class:`TokenSnapshotConcurrencyError`. The count is yielded because the
    routes have three ways to answer 200 *without ever reaching the manager*
    (no snapshot, an already-terminal run, a repository that is not a
    ``TokenSnapshotStore``) — a green assertion on the status code proves
    nothing unless the CAS was actually attempted.
    """
    attempts: list[str] = []

    async def _conflict(run_id, *, expected_revision, snapshot):  # noqa: ANN001
        del snapshot
        attempts.append(run_id)
        raise TokenSnapshotConcurrencyError(
            run_id,
            expected_revision=expected_revision,
            actual_revision=(expected_revision or 0) + 1,
        )

    repository.compare_and_swap_token_snapshot = _conflict
    try:
        yield attempts
    finally:
        del repository.compare_and_swap_token_snapshot


async def _contended_running_run(service):
    """Seed a RUNNING run with a token snapshot that no worker can claim.

    Deliberately not created through ``POST /runs``: the app's own bootstrap
    starts a durable ``RunWorker`` against the same database, and a PENDING run
    is claimed and driven out from under the assertions during the CAS budget's
    (sub-second, but real) backoff. RUNNING with a NULL lease matches neither
    ``claim_pending`` (wants PENDING) nor ``claim_orphaned`` (wants a non-NULL
    lease), so the only writer in the window is the route under test.
    """
    run = await service.run_repository.create(
        Run(
            graph_version_ref="g:v1",
            deployment_ref=service.deployment.deployment_ref,
            status=RunStatus.RUNNING,
        )
    )
    snapshot = initialize_token_snapshot(run_id=run.run_id, root_node_id="agent-step", payload={})
    await service.run_repository.compare_and_swap_token_snapshot(
        run.run_id, expected_revision=None, snapshot=snapshot
    )
    return run.run_id, await service.run_repository.get_token_snapshot(run.run_id)


async def test_cancel_under_snapshot_contention_conflicts_instead_of_500(sqlite_db) -> None:
    """F-10b: an operator cancel that loses its CAS budget is a 409, not a 500.

    ``TokenSnapshotConcurrencyError`` subclasses ``RuntimeError``, so the
    route's ``except ValueError`` never saw it and a purely retryable
    contention condition surfaced to the operator as an unhandled server
    error. Nothing is written on that path -- the lifecycle CAS runs before the
    repository transition -- so the response must say "conflict, try again",
    and neither the run's failure identity nor its snapshot may move.
    """
    service, app = await _make_service_and_app(
        sqlite_db, "graph-cancel-contended", DEPLOYMENT + "-cancel-contended"
    )
    run_id, before_snapshot = await _contended_running_run(service)

    with TestClient(app, raise_server_exceptions=False) as client:
        with _always_losing_snapshot_cas(service.run_repository) as attempts:
            response = client.post(f"/admin/runs/{run_id}/cancel", headers=admin_headers())

    after_run = await service.run_repository.get(run_id)
    after_snapshot = await service.run_repository.get_token_snapshot(run_id)

    assert attempts, "the contended CAS was never reached; this test proves nothing"
    assert response.status_code == 409, (
        f"a retryable contention condition surfaced as {response.status_code}"
    )
    assert after_run is not None
    assert after_run.status is not RunStatus.FAILED, "the 409 path wrote the terminal status"
    assert after_run.failure_state is None, "the 409 path wrote a failure identity"
    assert before_snapshot is not None and after_snapshot is not None
    assert after_snapshot.state is before_snapshot.state
    assert after_snapshot.state is not TokenEngineSnapshotState.CANCELLED
    assert after_snapshot.revision == before_snapshot.revision


async def test_interrupt_under_snapshot_contention_conflicts_instead_of_500(sqlite_db) -> None:
    """F-10b, the pause half: same boundary, same surfaced contract."""
    service, app = await _make_service_and_app(
        sqlite_db, "graph-interrupt-contended", DEPLOYMENT + "-interrupt-contended"
    )
    run_id, before_snapshot = await _contended_running_run(service)

    with TestClient(app, raise_server_exceptions=False) as client:
        with _always_losing_snapshot_cas(service.run_repository) as attempts:
            response = client.post(f"/admin/runs/{run_id}/interrupt", headers=admin_headers())

    after_run = await service.run_repository.get(run_id)
    after_snapshot = await service.run_repository.get_token_snapshot(run_id)

    assert attempts, "the contended CAS was never reached; this test proves nothing"
    assert response.status_code == 409, (
        f"a retryable contention condition surfaced as {response.status_code}"
    )
    assert after_run is not None
    assert after_run.status is RunStatus.RUNNING, "the 409 path wrote run state"
    assert before_snapshot is not None and after_snapshot is not None
    assert after_snapshot.state is before_snapshot.state
    assert after_snapshot.state is not TokenEngineSnapshotState.PAUSED
    assert after_snapshot.revision == before_snapshot.revision


# ---------------------------------------------------------------------------
# F-10c -- token state erased mid-request is a conflict, not a server error
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _snapshot_vanishing_after_first_read(repository):
    """Serve the real snapshot once, then nothing — the route's own TOCTOU window.

    Both routes ask ``get_token_snapshot`` whether a run owns token state, and
    only then does ``TokenLifecycleAdapter._apply`` reload it. Retention erasure
    (``ErasureService.erase_run`` deletes the snapshot and *keeps* the redacted
    run row) and ``delete_run`` both land in that gap, and the adapter answers a
    missing snapshot with ``KeyError`` — not a ``ValueError``, so it escaped the
    routes' handler as an unhandled 500.

    The ordering matters: a patch that returns ``None`` to *every* caller is
    vacuous, because the route's own existence check would then route around
    the lifecycle entirely and answer 200 without ever reaching the adapter.
    The call log is yielded so a test can prove the reload actually happened.
    """
    real = repository.get_token_snapshot
    reads: list[str] = []

    async def _vanishing(run_id: str):  # noqa: ANN202
        reads.append(run_id)
        return await real(run_id) if len(reads) == 1 else None

    repository.get_token_snapshot = _vanishing
    try:
        yield reads
    finally:
        del repository.get_token_snapshot


async def test_cancel_when_token_state_vanishes_conflicts_instead_of_500(sqlite_db) -> None:
    """F-10c: a cancel whose token state is erased mid-request is a 409, not a 500.

    Nothing is written on this path — the adapter raises on its reload, before
    any CAS — so the run must keep both its status and its failure identity,
    and the operator must get a conflict they can act on rather than a server
    error. Re-issuing converges: the second request's existence check sees no
    snapshot, routes around the lifecycle and cancels the run outright.
    """
    service, app = await _make_service_and_app(
        sqlite_db, "graph-cancel-vanished", DEPLOYMENT + "-cancel-vanished"
    )
    run_id, before_snapshot = await _contended_running_run(service)
    assert before_snapshot is not None

    with TestClient(app, raise_server_exceptions=False) as client:
        with _snapshot_vanishing_after_first_read(service.run_repository) as reads:
            response = client.post(f"/admin/runs/{run_id}/cancel", headers=admin_headers())

    after_run = await service.run_repository.get(run_id)

    assert len(reads) >= 2, "the adapter's reload was never reached; this test proves nothing"
    assert response.status_code == 409, (
        f"an erased token snapshot surfaced as {response.status_code}"
    )
    detail = response.json()["detail"]
    assert "no longer has token state" in detail, detail
    assert "lost a concurrent token-state update" not in detail, (
        "the vanished snapshot is indistinguishable from CAS contention in a log"
    )
    assert after_run is not None
    assert after_run.status is not RunStatus.FAILED, "the 409 path wrote the terminal status"
    assert after_run.failure_state is None, "the 409 path wrote a failure identity"


async def test_interrupt_when_token_state_vanishes_conflicts_instead_of_500(sqlite_db) -> None:
    """F-10c, the pause half: same window, same surfaced contract."""
    service, app = await _make_service_and_app(
        sqlite_db, "graph-interrupt-vanished", DEPLOYMENT + "-interrupt-vanished"
    )
    run_id, before_snapshot = await _contended_running_run(service)
    assert before_snapshot is not None

    with TestClient(app, raise_server_exceptions=False) as client:
        with _snapshot_vanishing_after_first_read(service.run_repository) as reads:
            response = client.post(f"/admin/runs/{run_id}/interrupt", headers=admin_headers())

    after_run = await service.run_repository.get(run_id)

    assert len(reads) >= 2, "the adapter's reload was never reached; this test proves nothing"
    assert response.status_code == 409, (
        f"an erased token snapshot surfaced as {response.status_code}"
    )
    detail = response.json()["detail"]
    assert "no longer has token state" in detail, detail
    assert after_run is not None
    assert after_run.status is RunStatus.RUNNING, "the 409 path wrote run state"


async def test_the_three_lifecycle_conflicts_read_differently(sqlite_db) -> None:
    """One route, three 409s: an operator reading a log must tell them apart.

    "Not interruptible", "lost the CAS" and "token state is gone" want three
    different operator responses — accept it, retry the same call, re-issue and
    expect the run to be cancelled outright. They are the same status code by
    design, so the detail string is the only thing carrying that difference.
    """
    service, app = await _make_service_and_app(
        sqlite_db, "graph-conflict-vocab", DEPLOYMENT + "-conflict-vocab"
    )
    settled = await service.run_repository.create(
        Run(
            graph_version_ref="g:v1",
            deployment_ref=service.deployment.deployment_ref,
            status=RunStatus.COMPLETED,
        )
    )
    contended_id, _ = await _contended_running_run(service)
    vanished_id, _ = await _contended_running_run(service)

    with TestClient(app, raise_server_exceptions=False) as client:
        not_interruptible = client.post(
            f"/admin/runs/{settled.run_id}/interrupt", headers=admin_headers()
        )
        with _always_losing_snapshot_cas(service.run_repository) as attempts:
            contention = client.post(
                f"/admin/runs/{contended_id}/interrupt", headers=admin_headers()
            )
        with _snapshot_vanishing_after_first_read(service.run_repository) as reads:
            vanished = client.post(f"/admin/runs/{vanished_id}/interrupt", headers=admin_headers())

    assert attempts, "the contended CAS was never reached; this test proves nothing"
    assert len(reads) >= 2, "the adapter's reload was never reached; this test proves nothing"
    details = [
        not_interruptible.json()["detail"],
        contention.json()["detail"],
        vanished.json()["detail"],
    ]
    assert [r.status_code for r in (not_interruptible, contention, vanished)] == [409, 409, 409]
    assert len(set(details)) == 3, f"two conflicts read identically in a log: {details}"


async def test_cancel_converges_after_the_token_state_was_really_erased(sqlite_db) -> None:
    """The 409's advice must be true: re-issuing after erasure actually cancels.

    This is what makes 409 the right status rather than 404 or 410 — retention
    erasure deletes the token snapshot and *keeps* the redacted run row, so the
    run is neither missing nor permanently gone, and the very next request can
    still cancel it. Reproduced with the exact repository call
    ``ErasureService.erase_run`` makes (the plain erase, not the fenced
    variant), so the state under test is the one an operator's retry lands in.
    """
    service, app = await _make_service_and_app(
        sqlite_db, "graph-cancel-converge", DEPLOYMENT + "-cancel-converge"
    )
    run_id, before_snapshot = await _contended_running_run(service)
    assert before_snapshot is not None

    async with service.run_repository.database.transaction(write_lock=True) as conn:
        await service.run_repository.erase_token_snapshot_for_run_in_transaction(conn, run_id)
    assert await service.run_repository.get_token_snapshot(run_id) is None, (
        "erasure left token state behind; this test reproduces the wrong state"
    )

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(f"/admin/runs/{run_id}/cancel", headers=admin_headers())

    after_run = await service.run_repository.get(run_id)

    assert response.status_code == 200, (
        f"a re-issued cancel after erasure answered {response.status_code}; the 409's "
        "'re-issue the request' advice is a lie"
    )
    assert after_run is not None
    assert after_run.status is RunStatus.FAILED


async def test_permanent_token_snapshot_errors_are_never_dressed_as_conflicts(sqlite_db) -> None:
    """A permanent snapshot failure must not be sold to the operator as retryable.

    ``TokenSnapshotWriteDisabledError`` (a durable erasure fence) and
    ``TokenSnapshotCorruptionError`` are ``RuntimeError`` siblings of the
    contention error, and both are permanent: a 409 would send an operator into
    a retry loop that cannot terminate. This is why the contention handler keys
    on the concrete class instead of on ``RuntimeError``, and why the vanished-
    snapshot handler keys on ``KeyError`` alone.

    Deliberately asserts *not 409* rather than ``== 500``: the point being
    pinned is the distinction, not a blessing of today's unhandled-error status
    as the right long-term answer for a fenced run.
    """
    service, app = await _make_service_and_app(
        sqlite_db, "graph-permanent-err", DEPLOYMENT + "-permanent-err"
    )
    run_id, _ = await _contended_running_run(service)
    attempts: list[str] = []

    async def _write_disabled(run_id, *, expected_revision, snapshot):  # noqa: ANN001
        del expected_revision, snapshot
        attempts.append(run_id)
        raise TokenSnapshotWriteDisabledError(run_id)

    service.run_repository.compare_and_swap_token_snapshot = _write_disabled
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post(f"/admin/runs/{run_id}/interrupt", headers=admin_headers())
    finally:
        del service.run_repository.compare_and_swap_token_snapshot

    assert attempts, "the fenced CAS was never reached; this test proves nothing"
    assert response.status_code != 409, (
        "a permanent erasure fence was rendered as a retryable conflict"
    )


# ---------------------------------------------------------------------------
# F-10d -- the run row itself deleted mid-command is 404, not a server error
# ---------------------------------------------------------------------------


async def _uncontended_running_run(service) -> str:
    """Seed a RUNNING run that owns no token state, so the routes read it twice.

    No snapshot on purpose: ``_token_interrupt_manager`` then answers ``None``
    and the lifecycle is skipped entirely, so the only two ``get`` calls in the
    request are the route's own 404 guard and the one ``transition`` makes.
    That is the window under test, uncontaminated by the adapter's reloads.

    RUNNING with a NULL lease for the reason ``_contended_running_run`` gives:
    it matches neither ``claim_pending`` nor ``claim_orphaned``, so the app's
    durable worker cannot drive the run out from under the assertions.
    """
    run = await service.run_repository.create(
        Run(
            graph_version_ref="g:v1",
            deployment_ref=service.deployment.deployment_ref,
            status=RunStatus.RUNNING,
        )
    )
    return run.run_id


@contextlib.contextmanager
def _run_row_deleted_between_the_two_reads(repository, run_id: str):
    """Delete the run after the route guard and inside its atomic command.

    Both routes read the run once to answer 404 for an unknown or foreign run,
    and the atomic cancel/interrupt command reads it again while writing.

    The delete is the real repository call, and ``transition`` is never patched,
    so the ``KeyError`` under test is raised by the production code path rather
    than staged by the test. Ordering is the whole point and it is why the read
    log is yielded: a delete that landed *before* the first read would be
    answered by the route's own guard with its own 404, and a green status code
    would prove nothing about the handler being pinned here. Reads for other
    runs pass through untouched so a background worker cannot shift the count.
    """
    real_get = repository.get
    real_delete = repository.delete
    real_cancel = repository.cancel
    real_interrupt = repository.interrupt
    reads: list[str] = []

    async def _vanishing(target: str):  # noqa: ANN202
        if target != run_id:
            return await real_get(target)
        reads.append(target)
        return await real_get(target)

    async def _vanishing_command(command, target: str, *args, **kwargs):  # noqa: ANN001, ANN202
        if target == run_id:
            reads.append(target)
            await real_delete(target)
        return await command(target, *args, **kwargs)

    async def _cancel(target: str, *args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        return await _vanishing_command(real_cancel, target, *args, **kwargs)

    async def _interrupt(target: str, *args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        return await _vanishing_command(real_interrupt, target, *args, **kwargs)

    repository.get = _vanishing
    repository.cancel = _cancel
    repository.interrupt = _interrupt
    try:
        yield reads
    finally:
        del repository.get
        del repository.cancel
        del repository.interrupt


async def test_cancel_when_the_run_row_vanishes_is_404_instead_of_500(sqlite_db) -> None:
    """F-10d: a cancel whose run row is deleted mid-command is a 404, not a 500.

    ``transition`` raises ``KeyError(run_id)`` when the row is gone, which is
    neither a ``ValueError`` nor a ``TokenSnapshotConcurrencyError``, so it fell
    past the route's handler and reached the operator as an unhandled server
    error. It is a different condition from a vanished *token snapshot*: the run
    is definitively gone, no retry can converge, and only 404 says so.

    The detail must not be the guard's ``"run not found"`` -- the two 404s
    answer different questions ("you asked about a run that isn't there" versus
    "the run you were operating on was removed underneath the command") -- and
    the read log proves the answer came from the transition, not the guard.
    """
    service, app = await _make_service_and_app(
        sqlite_db, "graph-cancel-row-gone", DEPLOYMENT + "-cancel-row-gone"
    )
    run_id = await _uncontended_running_run(service)

    with TestClient(app, raise_server_exceptions=False) as client:
        with _run_row_deleted_between_the_two_reads(service.run_repository, run_id) as reads:
            response = client.post(f"/admin/runs/{run_id}/cancel", headers=admin_headers())

    assert len(reads) >= 2, "the transition's own read was never reached; this test proves nothing"
    assert await service.run_repository.get(run_id) is None, (
        "the run row survived; this test reproduces the wrong state"
    )
    assert response.status_code == 404, (
        f"a run deleted mid-command surfaced as {response.status_code}"
    )
    detail = response.json()["detail"]
    assert "deleted while the command was in flight" in detail, detail
    assert detail != "run not found", (
        "the mid-command deletion is indistinguishable from an unknown run in a log"
    )


async def test_interrupt_when_the_run_row_vanishes_is_404_instead_of_500(sqlite_db) -> None:
    """F-10d, the pause half: same window, same surfaced contract."""
    service, app = await _make_service_and_app(
        sqlite_db, "graph-interrupt-row-gone", DEPLOYMENT + "-interrupt-row-gone"
    )
    run_id = await _uncontended_running_run(service)

    with TestClient(app, raise_server_exceptions=False) as client:
        with _run_row_deleted_between_the_two_reads(service.run_repository, run_id) as reads:
            response = client.post(f"/admin/runs/{run_id}/interrupt", headers=admin_headers())

    assert len(reads) >= 2, "the transition's own read was never reached; this test proves nothing"
    assert await service.run_repository.get(run_id) is None, (
        "the run row survived; this test reproduces the wrong state"
    )
    assert response.status_code == 404, (
        f"a run deleted mid-command surfaced as {response.status_code}"
    )
    detail = response.json()["detail"]
    assert "deleted while the command was in flight" in detail, detail
    assert detail != "run not found"


async def test_a_re_issued_command_after_the_row_was_deleted_stays_404(sqlite_db) -> None:
    """The 404's premise must be true: the deletion is permanent, so the retry 404s too.

    This is what makes 404 the right status here rather than the 409 its two
    neighbours use. A contended CAS converges on a retry and an erased token
    snapshot converges on a retry; a deleted run row converges on nothing,
    because the row never comes back. The re-issued command is answered by the
    route's own guard, so it carries the guard's detail, not the handler's.
    """
    service, app = await _make_service_and_app(
        sqlite_db, "graph-cancel-row-retry", DEPLOYMENT + "-cancel-row-retry"
    )
    run_id = await _uncontended_running_run(service)
    await service.run_repository.delete(run_id)
    assert await service.run_repository.get(run_id) is None, (
        "the delete left the row behind; this test reproduces the wrong state"
    )

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(f"/admin/runs/{run_id}/cancel", headers=admin_headers())

    assert response.status_code == 404
    assert response.json()["detail"] == "run not found"


async def test_a_foreign_key_error_from_the_transition_is_never_dressed_as_404(sqlite_db) -> None:
    """A ``KeyError`` raised with the run row still there must not read as a deletion.

    ``KeyError`` carries no information about what was missing, and unlike the
    contention fix there is no concrete class to key on -- ``transition`` raises
    the same builtin for a missing run row as any incidental lookup failure in a
    bug below it would. Re-reading the run before answering 404 is the only
    thing separating the two, so this pins the branch that keeps the second one
    a server error: telling an operator a run was deleted when it is sitting
    right there sends them looking for a concurrent deleter that never ran.

    Deliberately asserts *not 404* rather than ``== 500``, for the reason the
    permanent-snapshot-error test gives: the point is the distinction, not a
    blessing of today's unhandled-error status.
    """
    service, app = await _make_service_and_app(
        sqlite_db, "graph-foreign-keyerror", DEPLOYMENT + "-foreign-keyerror"
    )
    run_id = await _uncontended_running_run(service)
    attempts: list[str] = []

    async def _foreign_key_error(target, *args, **kwargs):  # noqa: ANN001, ANN002, ANN202
        del args, kwargs
        attempts.append(target)
        raise KeyError("an incidental lookup below the command")

    service.run_repository.cancel = _foreign_key_error
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post(f"/admin/runs/{run_id}/cancel", headers=admin_headers())
    finally:
        del service.run_repository.cancel

    assert attempts, "the transition was never reached; this test proves nothing"
    assert await service.run_repository.get(run_id) is not None, (
        "the run row is gone; this test reproduces the wrong state"
    )
    assert response.status_code != 404, (
        "an incidental KeyError was mislabelled as a concurrent deletion"
    )


async def test_the_four_operator_command_failures_read_differently(sqlite_db) -> None:
    """One route, four failures an operator must tell apart from a log line alone.

    "Not interruptible", "lost the CAS", "token state is gone" and "the run was
    deleted" want four different responses -- accept it, retry the same call,
    re-issue and expect the run cancelled outright, stop retrying entirely.
    Three of them share status 409 and the fourth is the same 404 the unknown-run
    guard already returns, so the detail string is the only thing carrying the
    difference in every case.
    """
    service, app = await _make_service_and_app(
        sqlite_db, "graph-conflict-vocab4", DEPLOYMENT + "-conflict-vocab4"
    )
    settled = await service.run_repository.create(
        Run(
            graph_version_ref="g:v1",
            deployment_ref=service.deployment.deployment_ref,
            status=RunStatus.COMPLETED,
        )
    )
    contended_id, _ = await _contended_running_run(service)
    vanished_id, _ = await _contended_running_run(service)
    deleted_id = await _uncontended_running_run(service)

    with TestClient(app, raise_server_exceptions=False) as client:
        not_interruptible = client.post(
            f"/admin/runs/{settled.run_id}/interrupt", headers=admin_headers()
        )
        with _always_losing_snapshot_cas(service.run_repository) as attempts:
            contention = client.post(
                f"/admin/runs/{contended_id}/interrupt", headers=admin_headers()
            )
        with _snapshot_vanishing_after_first_read(service.run_repository) as reads:
            vanished = client.post(f"/admin/runs/{vanished_id}/interrupt", headers=admin_headers())
        with _run_row_deleted_between_the_two_reads(service.run_repository, deleted_id) as rows:
            deleted = client.post(f"/admin/runs/{deleted_id}/interrupt", headers=admin_headers())

    assert attempts, "the contended CAS was never reached; this test proves nothing"
    assert len(reads) >= 2, "the adapter's reload was never reached; this test proves nothing"
    assert len(rows) >= 2, "the transition's own read was never reached; this test proves nothing"
    responses = (not_interruptible, contention, vanished, deleted)
    details = [response.json()["detail"] for response in responses]
    assert [response.status_code for response in responses] == [409, 409, 409, 404]
    assert len(set(details)) == 4, f"two operator failures read identically in a log: {details}"
    assert "run not found" not in details, (
        "the mid-command deletion collapsed into the unknown-run guard's answer"
    )
