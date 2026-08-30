"""Route-level proofs for the MCP server registry admin API.

This module exists because its absence let two defects ship: the discover
route's error path raised instead of answering, and a PUT that omitted ``env``
deleted every credential. Both are unreachable from unit tests of the helpers
and both are trivially visible from a request.
"""

from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient

from tests.service.helpers import (
    admin_headers,
    agent_graph,
    deploy_service,
    operator_headers,
    reviewer_headers,
)
from zeroth.service.bootstrap import bootstrap_app
from zeroth.platform.config.settings import SandboxSettings, get_settings

DEPLOYMENT = "mcp-registry-test"
SERVER = {
    "ref": "filesystem",
    "command": "npx",
    "args": ["-y", "@mcp/server-filesystem", "/srv/data"],
    "env": {"API_KEY": "s3cret"},
    "grants": ["filesystem_read", "process_spawn", "external_api_call"],
}
_MCP_IMAGE = "registry.example/zeroth-mcp@sha256:" + "c" * 64


async def _app(sqlite_db, suffix: str):
    service, _ = await deploy_service(
        sqlite_db,
        agent_graph(graph_id=f"graph-mcp-{suffix}"),
        deployment_ref=f"{DEPLOYMENT}-{suffix}",
    )
    app = await bootstrap_app(sqlite_db, deployment_ref=service.deployment.deployment_ref)
    app.state.bootstrap = service
    return app, service


async def test_create_and_list_never_echo_env_values(sqlite_db) -> None:
    """An MCP server's environment is credentials by convention."""
    app, _ = await _app(sqlite_db, "create")
    with TestClient(app) as client:
        created = client.post("/v1/mcp/servers", json=SERVER, headers=admin_headers())
        assert created.status_code == 201, created.text
        assert created.json()["env"] == {"API_KEY": "***"}
        assert "s3cret" not in created.text

        listed = client.get("/v1/mcp/servers", headers=admin_headers())
        assert listed.status_code == 200
        assert "s3cret" not in listed.text
        assert listed.json()[0]["grants"] == [
            "filesystem_read",
            "process_spawn",
            "external_api_call",
        ]


async def test_an_update_that_omits_env_keeps_the_credentials(sqlite_db) -> None:
    """The documented operation is "PUT to narrow grants".

    Because responses mask every value, a client round-tripping what it was
    shown has no real env to send. Treating that as an instruction to clear
    would delete the credentials the server needs to start.
    """
    app, service = await _app(sqlite_db, "update-keeps-env")
    with TestClient(app) as client:
        client.post("/v1/mcp/servers", json=SERVER, headers=admin_headers())
        narrowed = client.put(
            "/v1/mcp/servers/filesystem",
            json={"command": "npx", "args": SERVER["args"], "grants": ["filesystem_read"]},
            headers=admin_headers(),
        )
        assert narrowed.status_code == 200, narrowed.text
        assert narrowed.json()["grants"] == ["filesystem_read"]

    stored = await service.mcp_server_config_repository.get(
        "filesystem", tenant_id=service.deployment.tenant_id
    )
    assert stored is not None
    assert stored.env == {"API_KEY": "s3cret"}, "narrowing grants must not wipe credentials"


async def test_an_explicit_empty_env_still_clears(sqlite_db) -> None:
    """Omitted means keep; ``{}`` means the operator really said clear."""
    app, service = await _app(sqlite_db, "update-clears-env")
    with TestClient(app) as client:
        client.post("/v1/mcp/servers", json=SERVER, headers=admin_headers())
        r = client.put(
            "/v1/mcp/servers/filesystem",
            json={"command": "npx", "args": [], "env": {}, "grants": []},
            headers=admin_headers(),
        )
        assert r.status_code == 200, r.text

    stored = await service.mcp_server_config_repository.get(
        "filesystem", tenant_id=service.deployment.tenant_id
    )
    assert stored is not None
    assert stored.env == {}


async def test_discovery_refuses_unisolated_process_by_default(sqlite_db) -> None:
    app, _ = await _app(sqlite_db, "discover-isolation")
    with TestClient(app) as client:
        client.post("/v1/mcp/servers", json=SERVER, headers=admin_headers())
        with patch("zeroth.service.api.mcp_server_api.MCPClientManager.start") as start:
            response = client.get(
                "/v1/mcp/servers/filesystem/tools", headers=admin_headers()
            )
    assert response.status_code == 503
    start.assert_not_called()


async def test_discovery_uses_configured_isolation_profile(sqlite_db, monkeypatch) -> None:
    monkeypatch.setattr(
        get_settings(),
        "sandbox",
        SandboxSettings(
            mcp_isolation_image=_MCP_IMAGE,
            mcp_isolation_allowed_environment_keys=("API_KEY",),
        ),
    )
    app, _ = await _app(sqlite_db, "discover-isolated")
    observed = []

    async def start(manager):  # noqa: ANN001
        observed.extend(manager._configs)
        return []

    with TestClient(app) as client:
        client.post("/v1/mcp/servers", json=SERVER, headers=admin_headers())
        with patch("zeroth.service.api.mcp_server_api.MCPClientManager.start", new=start):
            response = client.get(
                "/v1/mcp/servers/filesystem/tools", headers=admin_headers()
            )

    assert response.status_code == 200, response.text
    assert observed[0].command == "docker"
    assert observed[0].args[-5:] == [
        _MCP_IMAGE,
        "npx",
        "-y",
        "@mcp/server-filesystem",
        "/srv/data",
    ]
    assert "s3cret" not in " ".join(observed[0].args)


async def test_discovery_failure_answers_502_rather_than_raising(sqlite_db, monkeypatch) -> None:
    """The one route that runs an operator-supplied process.

    Its error path used to call safe_error_detail with the wrong keyword, so
    every failure raised TypeError and surfaced as a 500 with a traceback --
    the sanitisation never ran at all.
    """
    monkeypatch.setattr(
        get_settings(),
        "sandbox",
        SandboxSettings(allow_unisolated_mcp_development=True),
    )
    app, _ = await _app(sqlite_db, "discover-fails")
    with TestClient(app, raise_server_exceptions=False) as client:
        client.post("/v1/mcp/servers", json=SERVER, headers=admin_headers())
        with patch(
            "zeroth.service.api.mcp_server_api.MCPClientManager.start",
            side_effect=RuntimeError("boom: /srv/data/secret.pem not readable"),
        ):
            r = client.get("/v1/mcp/servers/filesystem/tools", headers=admin_headers())
    assert r.status_code == 502, r.text
    assert "boom" not in r.text
    assert "secret.pem" not in r.text


async def test_unknown_ref_is_404_and_bad_ref_is_422(sqlite_db) -> None:
    app, _ = await _app(sqlite_db, "refs")
    with TestClient(app) as client:
        assert (
            client.get("/v1/mcp/servers/never-registered/tools", headers=admin_headers())
        ).status_code == 404
        assert (
            client.delete("/v1/mcp/servers/NOT A REF", headers=admin_headers())
        ).status_code == 422


async def test_a_duplicate_ref_is_409_not_a_500(sqlite_db) -> None:
    app, _ = await _app(sqlite_db, "duplicate")
    with TestClient(app) as client:
        assert client.post("/v1/mcp/servers", json=SERVER, headers=admin_headers()).status_code == 201
        again = client.post("/v1/mcp/servers", json=SERVER, headers=admin_headers())
    assert again.status_code == 409, again.text


async def test_the_registry_is_closed_to_the_role_that_authors_graphs(sqlite_db) -> None:
    """The whole security argument for MCP_ADMIN.

    OPERATOR holds WORKFLOW_ADMIN and CONNECTOR_ADMIN. If it could also write
    these rows, the registry would stop being the side of the capability check
    a graph author cannot edit.
    """
    app, _ = await _app(sqlite_db, "authz")
    with TestClient(app) as client:
        for headers in (operator_headers(), reviewer_headers()):
            assert client.post(
                "/v1/mcp/servers", json=SERVER, headers=headers
            ).status_code == 403
            assert client.get("/v1/mcp/servers", headers=headers).status_code == 403
        assert client.get("/v1/mcp/servers", headers=admin_headers()).status_code == 200
