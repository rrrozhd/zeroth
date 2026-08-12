"""Bootstrap wires configured retention defaults into the policy repository."""

from __future__ import annotations

from tests.service.helpers import agent_graph, default_service_auth_config, deploy_service
from zeroth.service.bootstrap.factory import bootstrap_scoped_service


async def test_bootstrap_seeds_policy_repository_default_from_settings(
    sqlite_db, monkeypatch
) -> None:
    monkeypatch.setenv("ZEROTH_RETENTION__DEFAULT_AUDIT_TTL_SECONDS", "86400")
    monkeypatch.setenv("ZEROTH_RETENTION__DEFAULT_RUN_TTL_SECONDS", "172800")
    # The settings singleton caches env at first read; reset it for this test.
    import zeroth.platform.config.settings as settings_module

    monkeypatch.setattr(settings_module, "_settings_singleton", None)

    service, _ = await deploy_service(
        sqlite_db, agent_graph(graph_id="graph-ret-defaults"), tenant_id="tenant-ret-defaults"
    )
    bootstrap = await bootstrap_scoped_service(
        sqlite_db,
        deployment_ref=service.deployment.deployment_ref,
        tenant_id=service.deployment.tenant_id,
        auth_config=service.auth_config,
    )

    resolved = await bootstrap.retention_policy_repository.resolve()
    assert resolved.audit_ttl_seconds == 86400
    assert resolved.run_ttl_seconds == 172800
    # Environment-derived defaults must not be persisted as tenant rows.
    assert await bootstrap.retention_policy_repository.get() is None


async def test_bootstrap_retention_worker_uses_current_deployment_scope_and_defaults(
    sqlite_db, monkeypatch
) -> None:
    monkeypatch.setenv("ZEROTH_RETENTION__ENABLED", "true")
    monkeypatch.setenv("ZEROTH_RETENTION__DEFAULT_RUN_TTL_SECONDS", "172800")
    import zeroth.platform.config.settings as settings_module

    monkeypatch.setattr(settings_module, "_settings_singleton", None)
    service, _ = await deploy_service(
        sqlite_db, agent_graph(graph_id="graph-ret-worker"), tenant_id="tenant-ret-worker"
    )
    bootstrap = await bootstrap_scoped_service(
        sqlite_db,
        deployment_ref=service.deployment.deployment_ref,
        tenant_id=service.deployment.tenant_id,
        auth_config=service.auth_config,
    )

    worker = bootstrap.retention_worker
    assert worker is not None
    assert worker.policy_repository is bootstrap.retention_policy_repository
    assert worker.erasure_service._policies is bootstrap.retention_policy_repository
    assert worker.policy_repository.tenant_id == bootstrap.deployment.tenant_id
    assert (await worker.policy_repository.resolve()).run_ttl_seconds == 172800
