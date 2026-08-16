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
from zeroth.runtime.runs import RunStatus
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


async def test_interrupt_run_pauses_existing_token_snapshot(sqlite_db) -> None:
    service, app = await _make_service_and_app(
        sqlite_db,
        "graph-admin-token-interrupt",
        DEPLOYMENT + "-token-interrupt",
    )

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
