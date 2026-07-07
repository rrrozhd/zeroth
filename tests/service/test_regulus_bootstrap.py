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
from zeroth.core.config.settings import get_settings


@pytest.mark.asyncio
async def test_bootstrap_threads_self_auth_provider_when_regulus_enabled(
    sqlite_db, monkeypatch
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings.regulus, "enabled", True)

    service, _ = await deploy_service(
        sqlite_db, agent_graph(graph_id="regulus-bootstrap-wiring")
    )
    try:
        # The real wiring ran: both self-call clients carry a headers provider.
        assert service.regulus_client is not None
        assert service.regulus_client._client.transport._headers_provider is not None
        assert service.budget_enforcer is not None
        assert service.budget_enforcer._headers_provider is not None

        # And the provider is built from the first configured Zeroth service key
        # (the X-API-Key that passes the gated /regulus mount). The econ Bearer is
        # added on top when the regulus extra is installed.
        headers = service.budget_enforcer._headers_provider()
        assert headers["X-API-Key"] == "test-operator-key"
    finally:
        if service.regulus_client is not None:
            service.regulus_client.stop()
