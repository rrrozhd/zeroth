"""The real bootstrap glue threads the self-auth provider when regulus is enabled.

Every other regulus test hand-wires the provider via a stub bootstrap; this one
runs the actual ``bootstrap_service`` path (regulus enabled) and asserts the
production wiring -- ``auth_config.api_keys[0].secret`` -> ``make_self_auth_headers_provider``
-> ``RegulusClient``/``BudgetEnforcer`` -- is in place, so cost events authenticate
rather than silently 401/fail-open.
"""

from __future__ import annotations

import pytest

from tests.service.helpers import agent_graph, deploy_service
from zeroth.platform.config.settings import get_settings


@pytest.mark.asyncio
async def test_bootstrap_threads_self_auth_provider_when_regulus_enabled(
    sqlite_db, monkeypatch
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings.regulus, "enabled", True)
    monkeypatch.setattr(settings.regulus, "per_run_cap_usd", 0.25)

    service, _ = await deploy_service(sqlite_db, agent_graph(graph_id="regulus-bootstrap-wiring"))
    try:
        # The real wiring ran: both self-call clients carry a headers provider.
        assert service.regulus_client is not None
        assert service.regulus_client._client.transport._headers_provider is not None
        assert service.regulus_client._client.transport._asgi_app is not None
        # base_url deliberately stays the operator-configured, RESOLVABLE value.
        # ASGITransport routes the cost-event POST by PATH, so _asgi_app above is
        # what proves in-process dispatch; the readiness probe HTTP-GETs this
        # base_url, so pointing it at the unroutable "regulus.internal" sentinel
        # would flip /health/ready to degraded. See bootstrap/factory.py.
        assert service.regulus_client.base_url == settings.regulus.base_url
        assert service.budget_enforcer is not None
        assert service.budget_enforcer._headers_provider is not None
        assert service.probe_instrumentation is not None
        assert service.orchestrator.cost_instrumentation is service.probe_instrumentation
        assert service.memory_resolver._embedding_call_hooks is not None
        dispatcher = service.orchestrator._node_dispatcher
        assert dispatcher.cost_instrumentation is service.probe_instrumentation
        assert dispatcher.per_run_cap_usd == settings.regulus.per_run_cap_usd

        # And the provider is built from the first configured Zeroth service key
        # (the X-API-Key that passes the gated /regulus mount). The econ Bearer is
        # added on top when the regulus extra is installed.
        headers = service.budget_enforcer._headers_provider()
        assert headers["X-API-Key"] == "test-operator-key"
    finally:
        if service.regulus_client is not None:
            service.regulus_client.stop()


@pytest.mark.asyncio
async def test_bootstrap_builds_cost_estimator_when_regulus_disabled(
    sqlite_db, monkeypatch
) -> None:
    """On by default: cost attribution is wired even with Regulus off.

    CostEstimator is local (litellm pricing), so it is always constructed — that is
    what makes cost_usd populate and the local econ lenses work out of the box. The
    Regulus event stream and the budget enforcer stay gated on regulus.enabled.
    """
    settings = get_settings()
    monkeypatch.setattr(settings.regulus, "enabled", False)

    service, _ = await deploy_service(sqlite_db, agent_graph(graph_id="cost-on-by-default"))
    try:
        assert service.cost_estimator is not None  # local litellm pricing, always on
        assert service.regulus_client is None  # event stream stays off
        assert service.budget_enforcer is None  # budget caps stay Regulus-gated
    finally:
        if service.regulus_client is not None:
            service.regulus_client.stop()
