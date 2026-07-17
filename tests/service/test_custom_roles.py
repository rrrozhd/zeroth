"""Config-driven custom roles: registry semantics and end-to-end enforcement."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.service.helpers import agent_graph, api_key_headers, deploy_service
from zeroth.core.service.auth import ServiceAuthConfig, StaticApiKeyCredential
from zeroth.core.service.authorization import (
    BUILTIN_ROLE_PERMISSIONS,
    Permission,
    RoleRegistry,
)

# --- RoleRegistry unit semantics -------------------------------------------------


def test_builtin_roles_resolve_without_config() -> None:
    registry = RoleRegistry()
    assert registry.permissions_for(["admin"]) == set(Permission)
    assert Permission.AUDIT_READ in registry.permissions_for(["reviewer"])
    assert registry.known_roles() == frozenset(BUILTIN_ROLE_PERMISSIONS)


def test_unknown_role_is_fail_closed() -> None:
    # A typo or a revoked role grants nothing rather than escalating.
    assert RoleRegistry().permissions_for(["ghost"]) == set()


def test_custom_role_from_config_grants_named_permissions() -> None:
    registry = RoleRegistry.from_config({"auditor": ["audit:read", "run:read"]})
    assert registry.permissions_for(["auditor"]) == {
        Permission.AUDIT_READ,
        Permission.RUN_READ,
    }
    # Built-ins remain available alongside the custom role.
    assert registry.permissions_for(["admin"]) == set(Permission)


def test_custom_role_colliding_with_builtin_is_rejected() -> None:
    with pytest.raises(ValueError, match="collides with a built-in role"):
        RoleRegistry.from_config({"operator": ["run:read"]})


def test_custom_role_with_unknown_permission_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown permission"):
        RoleRegistry.from_config({"auditor": ["audit:read", "not:a:permission"]})


def test_multiple_roles_union_permissions() -> None:
    registry = RoleRegistry.from_config({"auditor": ["audit:read"]})
    granted = registry.permissions_for(["reviewer", "auditor"])
    # Reviewer already carries AUDIT_READ; the point is the union does not crash
    # on a mixed built-in + custom role list.
    assert Permission.AUDIT_READ in granted
    assert Permission.APPROVAL_RESOLVE in granted  # from reviewer


# --- End-to-end enforcement through a real route ---------------------------------


def _auth_config_with_auditor() -> ServiceAuthConfig:
    """An API key whose only role is a config-defined 'auditor' (audit:read)."""
    return ServiceAuthConfig(
        api_keys=[
            StaticApiKeyCredential(
                credential_id="auditor-key",
                secret="test-auditor-key",
                subject="auditor-1",
                roles=["auditor"],
                tenant_id="default",
                workspace_id=None,
            ),
        ],
        custom_roles={"auditor": ["audit:read"]},
    )


@pytest.mark.asyncio
async def test_custom_role_is_granted_its_permission(sqlite_db) -> None:
    service, _ = await deploy_service(
        sqlite_db,
        agent_graph(graph_id="graph-custom-role-allow"),
        deployment_ref="custom-role-allow",
        auth_config=_auth_config_with_auditor(),
    )
    from zeroth.core.service.bootstrap import bootstrap_app

    app = await bootstrap_app(sqlite_db, deployment_ref=service.deployment.deployment_ref)
    app.state.bootstrap = service

    with TestClient(app) as client:
        response = client.get(
            f"/deployments/{service.deployment.deployment_ref}/audits",
            headers=api_key_headers("test-auditor-key"),
        )

    # AUDIT_READ is granted -> the gate passes (200), not 403.
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_custom_role_is_denied_ungranted_permission(sqlite_db) -> None:
    service, _ = await deploy_service(
        sqlite_db,
        agent_graph(graph_id="graph-custom-role-deny"),
        deployment_ref="custom-role-deny",
        auth_config=_auth_config_with_auditor(),
    )
    from zeroth.core.service.bootstrap import bootstrap_app

    app = await bootstrap_app(sqlite_db, deployment_ref=service.deployment.deployment_ref)
    app.state.bootstrap = service

    with TestClient(app) as client:
        # METRICS_READ (admin-only) is not in the auditor role -> forbidden,
        # proving the custom role is scoped and not silently treated as admin.
        response = client.get("/metrics", headers=api_key_headers("test-auditor-key"))

    assert response.status_code == 403
