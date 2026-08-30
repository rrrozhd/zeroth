"""Canonical import surface for the service authorization module."""

from __future__ import annotations


def test_authorization_publishes_its_whole_surface() -> None:
    """Every name the package is documented to export is still exported.

    This replaced a parity assertion comparing each name against the legacy
    republisher. ZER-25 removed that path, so the comparison would compare
    the module with itself; the surface it pinned is asserted directly.
    """
    import zeroth.service.api.authorization as canonical

    expected = {
        "Permission",
        "ROLE_PERMISSIONS",
        "require_deployment_scope",
        "require_permission",
        "require_resource_scope",
    }

    missing = sorted(name for name in expected if not hasattr(canonical, name))
    assert not missing, f"zeroth.service.api.authorization no longer publishes: {missing}"


def test_every_service_role_has_a_permission_set() -> None:
    from zeroth.governance.identity import ServiceRole
    from zeroth.service.api.authorization import ROLE_PERMISSIONS, Permission

    for role in ServiceRole:
        assert role in ROLE_PERMISSIONS, f"role {role} has no permission mapping"
        assert all(isinstance(p, Permission) for p in ROLE_PERMISSIONS[role])


def test_economic_administration_is_reserved_for_platform_admin() -> None:
    from zeroth.governance.identity import ServiceRole
    from zeroth.service.api.authorization import ROLE_PERMISSIONS, Permission

    assert Permission.ECON_ADMIN not in ROLE_PERMISSIONS[ServiceRole.ADMIN]
    assert Permission.ECON_ADMIN in ROLE_PERMISSIONS[ServiceRole.PLATFORM_ADMIN]
    assert ROLE_PERMISSIONS[ServiceRole.ADMIN] < ROLE_PERMISSIONS[ServiceRole.PLATFORM_ADMIN]


def test_evaluation_administration_is_reserved_for_platform_admin() -> None:
    """Campaign fault injection must never inherit ordinary tenant administration."""
    from zeroth.governance.identity import ServiceRole
    from zeroth.service.api.authorization import ROLE_PERMISSIONS, Permission

    for role in (ServiceRole.OPERATOR, ServiceRole.REVIEWER, ServiceRole.ADMIN):
        assert Permission.EVALUATION_ADMIN not in ROLE_PERMISSIONS[role]
    assert Permission.EVALUATION_ADMIN in ROLE_PERMISSIONS[ServiceRole.PLATFORM_ADMIN]


def test_repository_installation_claim_is_reserved_for_admin_roles() -> None:
    from zeroth.governance.identity import ServiceRole
    from zeroth.service.api.authorization import ROLE_PERMISSIONS, Permission

    assert Permission.REPOSITORY_ADMIN not in ROLE_PERMISSIONS[ServiceRole.OPERATOR]
    assert Permission.REPOSITORY_ADMIN not in ROLE_PERMISSIONS[ServiceRole.REVIEWER]
    assert Permission.REPOSITORY_ADMIN in ROLE_PERMISSIONS[ServiceRole.ADMIN]
    assert Permission.REPOSITORY_ADMIN in ROLE_PERMISSIONS[ServiceRole.PLATFORM_ADMIN]


def test_trusted_static_configuration_can_deliver_platform_admin() -> None:
    from zeroth.governance.identity import ServiceRole
    from zeroth.service.api.authentication import ServiceAuthConfig, ServiceAuthenticator

    config = ServiceAuthConfig.model_validate(
        {
            "api_keys": [
                {
                    "credential_id": "platform",
                    "secret": "trusted-secret",
                    "subject": "platform-operator",
                    "roles": ["platform_admin"],
                }
            ]
        }
    )

    principal = ServiceAuthenticator(config).authenticate_headers(
        {"X-API-Key": "trusted-secret"}
    )
    assert principal.roles == [ServiceRole.PLATFORM_ADMIN]


def test_certification_override_is_reserved_for_admin_roles() -> None:
    from zeroth.governance.identity import ServiceRole
    from zeroth.service.api.authorization import ROLE_PERMISSIONS, Permission

    assert Permission.CERTIFICATION_OVERRIDE not in ROLE_PERMISSIONS[ServiceRole.OPERATOR]
    assert Permission.CERTIFICATION_OVERRIDE not in ROLE_PERMISSIONS[ServiceRole.REVIEWER]
    assert Permission.CERTIFICATION_OVERRIDE in ROLE_PERMISSIONS[ServiceRole.ADMIN]
    assert Permission.CERTIFICATION_OVERRIDE in ROLE_PERMISSIONS[ServiceRole.PLATFORM_ADMIN]
