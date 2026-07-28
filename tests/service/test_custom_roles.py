"""Config-driven custom RBAC roles on the refactored service surface."""

from __future__ import annotations

import pytest

from zeroth.governance.identity import AuthenticatedPrincipal, AuthMethod
from zeroth.service.api.authentication import ServiceAuthConfig, ServiceAuthenticator
from zeroth.service.api.authorization import (
    BUILTIN_ROLE_PERMISSIONS,
    Permission,
    RoleRegistry,
)


def test_role_registry_preserves_all_current_builtins() -> None:
    registry = RoleRegistry()

    assert registry.known_roles() == frozenset(BUILTIN_ROLE_PERMISSIONS)
    assert Permission.ECON_ADMIN not in registry.permissions_for(["admin"])
    assert Permission.ECON_ADMIN in registry.permissions_for(["platform_admin"])


def test_custom_roles_union_grants_and_unknown_roles_fail_closed() -> None:
    registry = RoleRegistry.from_config({"auditor": ["audit:read", "run:read"]})

    assert registry.permissions_for(["auditor", "ghost"]) == {
        Permission.AUDIT_READ,
        Permission.RUN_READ,
    }


@pytest.mark.parametrize("builtin", ["operator", "reviewer", "admin", "platform_admin"])
def test_custom_role_cannot_shadow_builtin(builtin: str) -> None:
    with pytest.raises(ValueError, match="collides with a built-in role"):
        RoleRegistry.from_config({builtin: ["run:read"]})


def test_custom_role_rejects_unknown_permission() -> None:
    with pytest.raises(ValueError, match="unknown permission"):
        RoleRegistry.from_config({"auditor": ["not:a:permission"]})


def test_custom_role_flows_through_static_auth_and_environment() -> None:
    config = ServiceAuthConfig.from_env(
        {
            "ZEROTH_SERVICE_API_KEYS_JSON": (
                '[{"credential_id":"auditor","secret":"secret",'
                '"subject":"auditor-1","roles":["auditor"]}]'
            ),
            "ZEROTH_SERVICE_ROLES_JSON": '{"auditor":["audit:read"]}',
        }
    )

    principal = ServiceAuthenticator(config).authenticate_headers({"X-API-Key": "secret"})

    assert config.custom_roles == {"auditor": ["audit:read"]}
    assert principal.roles == ["auditor"]


def test_identity_accepts_custom_role_name() -> None:
    principal = AuthenticatedPrincipal(
        subject="custom-user",
        auth_method=AuthMethod.API_KEY,
        roles=["auditor"],
    )

    assert principal.to_actor().roles == ["auditor"]
