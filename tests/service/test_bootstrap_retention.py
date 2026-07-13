"""Bootstrap wires configured retention defaults into the policy repository."""

from __future__ import annotations

from tests.service.helpers import agent_graph, default_service_auth_config, deploy_service
from zeroth.core.service.bootstrap import bootstrap_service


async def test_bootstrap_seeds_policy_repository_default_from_settings(
    sqlite_db, monkeypatch
) -> None:
    monkeypatch.setenv("ZEROTH_RETENTION__DEFAULT_AUDIT_TTL_SECONDS", "86400")
    monkeypatch.setenv("ZEROTH_RETENTION__DEFAULT_RUN_TTL_SECONDS", "172800")
    # The settings singleton caches env at first read; reset it for this test.
    import zeroth.core.config.settings as settings_module

    monkeypatch.setattr(settings_module, "_settings_singleton", None)

    service, _ = await deploy_service(sqlite_db, agent_graph(graph_id="graph-ret-defaults"))
    bootstrap = await bootstrap_service(
        sqlite_db,
        deployment_ref=service.deployment.deployment_ref,
        auth_config=service.auth_config,
    )

    resolved = await bootstrap.retention_policy_repository.resolve("tenant-unseen")
    assert resolved.audit_ttl_seconds == 86400
    assert resolved.run_ttl_seconds == 172800
    # Environment-derived defaults must not be persisted as tenant rows.
    assert await bootstrap.retention_policy_repository.get("tenant-unseen") is None
