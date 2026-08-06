"""Application-level wiring of the verifying capability evidence provider (ZER-8 S8).

``tests/governance/test_capability_evidence_provider.py`` establishes that
``PersistedCapabilityEvidenceProvider`` recomputes ``tool_manifest_complete``
server-side and that a fresh signed attestation over a complete registered
inventory reaches ``ENFORCED``. What those tests cannot establish is whether any
of it is *reachable* from the gateway: they hand the reporter a provider they
built themselves, so they pass identically whether or not the deployed service
ever constructs one.

``CapabilityReporter``'s ``evidence_provider`` argument defaults to
``NoCapabilityEvidenceProvider``, whose ``evidence_for_run`` returns ``None`` --
and ``None`` evidence can never be ``ENFORCED``. So a factory that omits the
argument produces a gateway in which every attested run silently classifies as
``ADMISSION``, with every component test still green. These tests pin the
application fact instead.

* **R15** -- a fresh signed attestation over a matching complete inventory
  reaches ``ENFORCED`` through the reporter the *factory* built, not one the
  test assembled.
* **R10** -- the same attestation with nothing registered does not, so the
  positive control above is non-vacuous: it is the stored registration doing
  the work, not a provider that grants ``ENFORCED`` unconditionally.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tests.service.helpers import agent_graph, deploy_service
from zeroth.contracts.langgraph_gateway.models import (
    CompatibilityResult,
    CompatibilityStatus,
    GovernanceLevel,
)
from zeroth.governance.attestations import (
    InventoryRegistration,
    InventoryRegistrationRepository,
    RegisteredTool,
    RunAttestationPayload,
    RunAttestationRepository,
    recompute_inventory_fingerprint,
    sign_attestation,
)
from zeroth.governance.attestations.versions import ADAPTER_VERSION

CORRELATION = "corr-gateway-wiring"
REGISTERED_TOOLS = (
    RegisteredTool(name="search", fingerprint="fp-search"),
    RegisteredTool(name="send_email", fingerprint="fp-send-email"),
)
FINGERPRINT = recompute_inventory_fingerprint(REGISTERED_TOOLS)
"""The digest the server recomputes for :data:`REGISTERED_TOOLS`.

Derived, not a literal: a registration's fingerprint is computed from its
tool identities and cannot be declared, so a hand-written constant would
never match and would silently turn the ENFORCED positive control below
into a negative test.
"""
SIGNING_KEY_ID = "test"


class _FakeGatewayTransport:
    """The transport the gateway bootstrap builds, without any upstream client."""

    def __init__(self, _settings: object, _secret_provider: object) -> None:
        self.client = _MutableBaseUrl()

    async def aclose(self) -> None:
        """Nothing to close: this transport never opened a connection."""


class _MutableBaseUrl:
    """Stands in for the transport's httpx client, which the factory writes to."""

    def __init__(self) -> None:
        self.base_url: str | None = None


class _FakeCompatibilityDetector:
    """Returns the one bounded probe result without touching the network."""

    def __init__(self, _client: object, **_kwargs: Any) -> None:
        pass

    async def detect(self) -> CompatibilityResult:
        """Report a supported upstream so the gateway bootstrap continues."""
        return CompatibilityResult(
            tested_langgraph_versions=("1.2.9",),
            tested_agent_server_versions=("0.11.1",),
            detected_agent_server_version="0.11.1",
            status=CompatibilityStatus.SUPPORTED,
        )


class _InertHandler:
    """Accepts whatever the factory passes and does nothing with it."""

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass


async def _gateway_bootstrap(sqlite_db: Any, monkeypatch: Any, graph_id: str) -> Any:
    """Bootstrap with the gateway on and the REAL ``CapabilityReporter`` in place.

    Every upstream is faked, but the reporter deliberately is not: it is the
    object under test. ``GatewayProxy`` and the websocket handler are inert
    because neither participates in classifying a run.
    """
    from zeroth.platform.config import LangGraphGatewaySettings, get_settings
    from zeroth.platform.signing import EnvHmacSigner
    from zeroth.service.bootstrap.factory import bootstrap_service

    service, _ = await deploy_service(
        sqlite_db, agent_graph(graph_id=graph_id), deployment_ref=graph_id
    )
    gateway_settings = LangGraphGatewaySettings(
        enabled=True,
        upstream_url="http://agent-server.test",
        upstream_audience="agent-server:test",
        deployment_ref=service.deployment.deployment_ref,
    )
    settings = get_settings().model_copy(update={"langgraph_gateway": gateway_settings})
    signer = EnvHmacSigner(key_id=SIGNING_KEY_ID, keys={SIGNING_KEY_ID: b"gateway-key"})

    async def fake_build_signer(_settings: object, _secret_provider: object) -> object:
        return signer

    factory = "zeroth.service.bootstrap.factory"
    monkeypatch.setattr(f"{factory}.get_settings", lambda: settings)
    monkeypatch.setattr(f"{factory}.build_signing_provider_async", fake_build_signer)
    monkeypatch.setattr(f"{factory}.HTTPGatewayTransport", _FakeGatewayTransport)
    monkeypatch.setattr(f"{factory}.CompatibilityDetector", _FakeCompatibilityDetector)
    monkeypatch.setattr(f"{factory}.GatewayProxy", _InertHandler)
    monkeypatch.setattr(f"{factory}.WebSocketGatewayHandler", _InertHandler)
    bootstrap = await bootstrap_service(sqlite_db, deployment_ref=service.deployment.deployment_ref)
    return bootstrap, service.deployment, signer


async def _seed_attested_run(
    sqlite_db: Any,
    deployment: Any,
    signer: Any,
    *,
    register_inventory: bool,
) -> None:
    """Store a genuinely fresh signed attestation for the deployed graph.

    The clock is real here -- the factory's reporter uses its own default
    ``now`` -- so the payload is issued now and expires well inside the run.
    """
    issued_at = datetime.now(tz=UTC)
    payload = RunAttestationPayload(
        correlation_id=CORRELATION,
        tenant_id=deployment.tenant_id,
        deployment_ref=deployment.deployment_ref,
        graph_version=deployment.graph_version_ref,
        adapter_version=ADAPTER_VERSION,
        inventory_fingerprint=FINGERPRINT,
        inventory_coverage="complete",
        tool_count=2,
        claimed_level="enforced",
        issued_at=issued_at,
        expires_at=issued_at + timedelta(minutes=10),
    )
    await RunAttestationRepository(sqlite_db).record(sign_attestation(payload, signer))
    if not register_inventory:
        return
    await InventoryRegistrationRepository(sqlite_db).register(
        InventoryRegistration(
            tenant_id=deployment.tenant_id,
            deployment_ref=deployment.deployment_ref,
            graph_version=deployment.graph_version_ref,
            adapter_version=ADAPTER_VERSION,
            coverage="complete",
            tools=REGISTERED_TOOLS,
            registered_at=issued_at - timedelta(seconds=30),
        )
    )


@pytest.mark.asyncio
async def test_the_gateway_reporter_enforces_an_attested_run_the_factory_wired(
    sqlite_db, monkeypatch
) -> None:
    """R15 end to end: ENFORCED is reachable through the factory-built reporter.

    This is the load-bearing assertion of the subtask. It fails against a
    factory that leaves ``evidence_provider`` unset, because the default
    provider returns no evidence and no evidence can be ``ENFORCED``.
    """
    bootstrap, deployment, signer = await _gateway_bootstrap(
        sqlite_db, monkeypatch, "gateway-evidence-wiring"
    )
    await _seed_attested_run(sqlite_db, deployment, signer, register_inventory=True)

    reporter = bootstrap.langgraph_gateway_capability_reporter
    assert reporter is not None

    assert await reporter.level_for_run(CORRELATION) is GovernanceLevel.ENFORCED


@pytest.mark.asyncio
async def test_the_gateway_reporter_refuses_enforced_without_a_registered_inventory(
    sqlite_db, monkeypatch
) -> None:
    """R10 end to end: the stored registration is what the positive control needs.

    Without this, the test above could pass for the wrong reason -- a provider
    that grants ``ENFORCED`` to any signed attestation would satisfy it just as
    well as one that checks the inventory it holds.
    """
    bootstrap, deployment, signer = await _gateway_bootstrap(
        sqlite_db, monkeypatch, "gateway-evidence-unregistered"
    )
    await _seed_attested_run(sqlite_db, deployment, signer, register_inventory=False)

    reporter = bootstrap.langgraph_gateway_capability_reporter
    assert reporter is not None

    assert await reporter.level_for_run(CORRELATION) is not GovernanceLevel.ENFORCED
