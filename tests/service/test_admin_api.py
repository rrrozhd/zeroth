"""Tests for admin run management endpoints."""
from __future__ import annotations

from fastapi.testclient import TestClient

from tests.service.helpers import (
    admin_headers,
    agent_graph,
    api_key_headers,
    deploy_service,
    operator_headers,
    scoped_auth_config,
)
from zeroth.identity import ServiceRole
from zeroth.runs import RunStatus
from zeroth.service.bootstrap import bootstrap_app

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


async def test_list_admin_runs_requires_admin_role(sqlite_db) -> None:
    service, app = await _make_service_and_app(sqlite_db, "graph-admin-list", DEPLOYMENT + "-list")

    with TestClient(app) as client:
        r = client.get("/admin/runs", headers=operator_headers())

    assert r.status_code == 403


async def test_list_admin_runs_returns_runs(sqlite_db) -> None:
    service, app = await _make_service_and_app(sqlite_db, "graph-admin-list2", DEPLOYMENT + "-list2")

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
    service, app = await _make_service_and_app(sqlite_db, "graph-cancel-404", DEPLOYMENT + "-cancel-404")

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
    service, app = await _make_service_and_app(sqlite_db, "graph-replay-conflict", DEPLOYMENT + "-rc")

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
    service, app = await _make_service_and_app(sqlite_db, "graph-admin-filter", DEPLOYMENT + "-filter")

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
    app = await bootstrap_app(
        sqlite_db,
        deployment_ref=service.deployment.deployment_ref,
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
