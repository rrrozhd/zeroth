"""Canonical import surface for the service authorization module."""

from __future__ import annotations


def test_authorization_is_the_same_surface_through_both_paths() -> None:
    from zeroth.core.service import authorization as legacy
    from zeroth.service.api import authorization as canonical

    assert canonical.Permission is legacy.Permission
    assert canonical.ROLE_PERMISSIONS is legacy.ROLE_PERMISSIONS
    assert canonical.require_permission is legacy.require_permission
    assert canonical.require_deployment_scope is legacy.require_deployment_scope
    assert canonical.require_resource_scope is legacy.require_resource_scope


def test_every_service_role_has_a_permission_set() -> None:
    from zeroth.governance.identity import ServiceRole
    from zeroth.service.api.authorization import ROLE_PERMISSIONS, Permission

    for role in ServiceRole:
        assert role in ROLE_PERMISSIONS, f"role {role} has no permission mapping"
        assert all(isinstance(p, Permission) for p in ROLE_PERMISSIONS[role])
