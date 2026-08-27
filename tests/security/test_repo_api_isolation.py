"""Byte-identical 404 parity for every id-bearing repository-unit route.

The ``test_api_surface_isolation`` discipline applied to ZER-37: tenant-a owns
the GitHub installation, checkout, and run rows; tenant-b credentials drive
each id-bearing route once with tenant-a's real id (foreign) and once with an
id that exists nowhere (unknown). The two answers must be byte-identical --
same status, same body -- so a cross-tenant caller cannot use the API as an
existence oracle. Owner reads are asserted 200 first, proving the parity comes
from scoping rather than a broken route.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.service.helpers import agent_graph, deploy_service
from tests.service.test_repo_api import attach_rig, make_api_rig
from tests.service.test_tenant_isolation import _headers, _scoped_auth_config
from zeroth.service.app import create_app


async def _repo_isolation_app(sqlite_db, tmp_path, suffix: str):
    service, deployment = await deploy_service(
        sqlite_db,
        agent_graph(graph_id=f"repo-isolation-{suffix}"),
        deployment_ref=f"repo-isolation-{suffix}",
        auth_config=_scoped_auth_config(),
        tenant_id="tenant-a",
    )
    rig = await make_api_rig(
        sqlite_db,
        tmp_path,
        service.audit_repository,
        tenant_id="tenant-a",
        deployment_ref=deployment.deployment_ref,
    )
    attach_rig(service, rig)
    return create_app(service), service, rig


def _assert_identical(foreign, unknown, status_code: int) -> None:
    assert foreign.status_code == unknown.status_code == status_code, (
        foreign.status_code,
        unknown.status_code,
        foreign.text,
        unknown.text,
    )
    assert foreign.content == unknown.content


async def test_repository_id_routes_foreign_matches_unknown(sqlite_db, tmp_path) -> None:
    app, _, rig = await _repo_isolation_app(sqlite_db, tmp_path, "repository")
    try:
        with TestClient(app) as client:
            # Owner sanity: the id is real and resolvable inside tenant-a.
            owned = client.post(
                f"/v1/repos/{rig.repo_id}/resolve-ref",
                json={"ref": "main"},
                headers=_headers("tenant-a-operator-key"),
            )
            assert owned.status_code == 200

            foreign = client.post(
                f"/v1/repos/{rig.repo_id}/resolve-ref",
                json={"ref": "main"},
                headers=_headers("tenant-b-operator-key"),
            )
            unknown = client.post(
                "/v1/repos/424242/resolve-ref",
                json={"ref": "main"},
                headers=_headers("tenant-b-operator-key"),
            )
            _assert_identical(foreign, unknown, 404)
            assert foreign.json() == {"detail": "repository not found"}

            foreign = client.post(
                f"/v1/repos/{rig.repo_id}/checkouts",
                json={"ref": "main"},
                headers=_headers("tenant-b-operator-key"),
            )
            unknown = client.post(
                "/v1/repos/424242/checkouts",
                json={"ref": "main"},
                headers=_headers("tenant-b-operator-key"),
            )
            _assert_identical(foreign, unknown, 404)
            assert foreign.json() == {"detail": "repository not found"}
    finally:
        await rig.aclose()


async def test_checkout_routes_foreign_id_matches_unknown(sqlite_db, tmp_path) -> None:
    app, _, rig = await _repo_isolation_app(sqlite_db, tmp_path, "checkout")
    try:
        checkout, report = await rig.service.create_checkout(
            "tenant-a", None, rig.repo_id, ref="main"
        )
        assert report is None

        with TestClient(app) as client:
            owned = client.get(
                f"/v1/repos/checkouts/{checkout.id}",
                headers=_headers("tenant-a-operator-key"),
            )
            assert owned.status_code == 200

            for path_suffix in ("", "/attestation"):
                foreign = client.get(
                    f"/v1/repos/checkouts/{checkout.id}{path_suffix}",
                    headers=_headers("tenant-b-operator-key"),
                )
                unknown = client.get(
                    f"/v1/repos/checkouts/unknown-checkout{path_suffix}",
                    headers=_headers("tenant-b-operator-key"),
                )
                _assert_identical(foreign, unknown, 404)
                assert foreign.json() == {"detail": "checkout not found"}

            # Run admission: absent-in-scope and unknown share one refusal.
            foreign = client.post(
                f"/v1/repos/checkouts/{checkout.id}/runs",
                json={"script": "train", "input_payload": {}},
                headers=_headers("tenant-b-operator-key"),
            )
            unknown = client.post(
                "/v1/repos/checkouts/unknown-checkout/runs",
                json={"script": "train", "input_payload": {}},
                headers=_headers("tenant-b-operator-key"),
            )
            _assert_identical(foreign, unknown, 409)
            assert foreign.json()["detail"]["code"] == "checkout_unavailable_error"
    finally:
        await rig.aclose()


async def test_run_routes_foreign_id_matches_unknown(sqlite_db, tmp_path) -> None:
    app, _, rig = await _repo_isolation_app(sqlite_db, tmp_path, "run")
    try:
        checkout, report = await rig.service.create_checkout(
            "tenant-a", None, rig.repo_id, ref="main"
        )
        assert report is None
        run = await rig.service.create_run(
            "tenant-a", None, checkout.id, script="train", input_payload={}
        )

        with TestClient(app) as client:
            owned = client.get(
                f"/v1/repos/runs/{run.id}", headers=_headers("tenant-a-operator-key")
            )
            assert owned.status_code == 200
            owned_evidence = client.get(
                f"/v1/repos/runs/{run.id}/evidence",
                headers=_headers("tenant-a-reviewer-key"),
            )
            assert owned_evidence.status_code == 200

            foreign = client.get(
                f"/v1/repos/runs/{run.id}", headers=_headers("tenant-b-operator-key")
            )
            unknown = client.get(
                "/v1/repos/runs/unknown-run", headers=_headers("tenant-b-operator-key")
            )
            _assert_identical(foreign, unknown, 404)
            assert foreign.json() == {"detail": "run not found"}

            foreign = client.get(
                f"/v1/repos/runs/{run.id}/evidence",
                headers=_headers("tenant-b-reviewer-key"),
            )
            unknown = client.get(
                "/v1/repos/runs/unknown-run/evidence",
                headers=_headers("tenant-b-reviewer-key"),
            )
            _assert_identical(foreign, unknown, 404)
            assert foreign.json() == {"detail": "run not found"}
    finally:
        await rig.aclose()


async def test_installation_enumeration_foreign_matches_unknown(sqlite_db, tmp_path) -> None:
    app, _, rig = await _repo_isolation_app(sqlite_db, tmp_path, "installation")
    try:
        with TestClient(app) as client:
            owned = client.get(
                "/v1/repos/installations", headers=_headers("tenant-a-operator-key")
            )
            assert owned.status_code == 200
            assert [item["installation_id"] for item in owned.json()] == [1]

            # A foreign tenant sees an empty tenant, not tenant-a's rows.
            listed = client.get(
                "/v1/repos/installations", headers=_headers("tenant-b-operator-key")
            )
            assert listed.status_code == 200
            assert listed.json() == []

            # Grants of a claimed-elsewhere installation and of an unknown one
            # answer byte-identically.
            foreign = client.get(
                "/v1/repos/installations/1/repositories",
                headers=_headers("tenant-b-operator-key"),
            )
            unknown = client.get(
                "/v1/repos/installations/999/repositories",
                headers=_headers("tenant-b-operator-key"),
            )
            _assert_identical(foreign, unknown, 200)
            assert foreign.json() == []
    finally:
        await rig.aclose()
