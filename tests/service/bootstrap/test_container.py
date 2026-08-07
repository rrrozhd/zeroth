"""Canonical import surface and injection behavior of the bootstrap container."""

from __future__ import annotations

from types import SimpleNamespace


def test_container_is_the_same_object_through_both_paths() -> None:
    from zeroth.service.bootstrap import DeploymentBootstrapError as LegacyError
    from zeroth.service.bootstrap import ServiceBootstrap as LegacyContainer
    from zeroth.service.bootstrap.container import (
        DeploymentBootstrapError,
        ServiceBootstrap,
    )

    assert ServiceBootstrap is LegacyContainer
    assert DeploymentBootstrapError is LegacyError


def test_container_holds_injected_dependencies_by_identity() -> None:
    from zeroth.service.bootstrap.container import ServiceBootstrap

    dependencies = {
        "deployment_service": SimpleNamespace(),
        "deployment": SimpleNamespace(),
        "graph": SimpleNamespace(),
        "run_repository": SimpleNamespace(),
        "thread_repository": SimpleNamespace(),
        "approval_service": SimpleNamespace(),
        "audit_repository": SimpleNamespace(),
        "contract_registry": SimpleNamespace(),
        "orchestrator": SimpleNamespace(),
        "auth_config": SimpleNamespace(),
        "authenticator": SimpleNamespace(),
    }
    bootstrap = ServiceBootstrap(**dependencies)

    for field_name, dependency in dependencies.items():
        assert getattr(bootstrap, field_name) is dependency
    # Optional wiring stays unset until the factory provides it.
    assert bootstrap.worker is None
    assert bootstrap.regulus_client is None
    assert bootstrap.secret_provider is None
    assert bootstrap.retention_worker is None


def test_container_error_is_a_runtime_error() -> None:
    from zeroth.service.bootstrap.container import DeploymentBootstrapError

    assert issubclass(DeploymentBootstrapError, RuntimeError)
