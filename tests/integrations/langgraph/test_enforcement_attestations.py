from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from zeroth.contracts.langgraph_gateway.models import GovernanceLevel
from zeroth.governance.langgraph_gateway.capabilities import CapabilityReporter
from zeroth.governance.policy import PolicyDecision, PolicyGuard
from zeroth.governance.policy.registry import default_capability_registry
from zeroth.integrations.langgraph import (
    InventoryCoverage,
    LangGraphGatewayClient,
    PolicyViolation,
    SideEffectClass,
    SQLiteApprovalRepository,
    ToolAction,
    ToolDecision,
    ToolDecisionKind,
    ToolGovernanceContext,
    ToolGovernanceError,
    ToolIdentity,
    ToolInventory,
    ToolInventoryEntry,
    attest_complete_inventory,
    govern_graph,
    govern_tools,
    record_tool_inventory,
)
from zeroth.platform.observability.metrics import MetricsCollector
from zeroth.platform.signing import EnvHmacSigner
from zeroth.platform.storage import NullWorkspaceScopeContext
from zeroth.service.langgraph_gateway.context import ReservedContextClaims, ReservedContextCodec
from zeroth.service.langgraph_gateway.enforcement import (
    ActionDescriptorV1,
    DecisionRequestV1,
    EnforcementBoundaryError,
    HeartbeatV1,
    InventoryEntryV1,
    InventoryRegistrationV1,
    LangGraphEnforcementRepository,
    LangGraphEnforcementService,
    RunAttestationV1,
    inventory_fingerprint,
)

NOW = datetime(2026, 8, 4, 18, 0, tzinfo=UTC)


class _Budget:
    async def check_budget_status(self, tenant_id: str) -> object:
        del tenant_id
        return type("BudgetResult", (), {"allowed": True, "degraded": False})()


def _signer() -> EnvHmacSigner:
    return EnvHmacSigner(key_id="k1", keys={"k1": b"adapter-signing-key"})


def _token(
    codec: ReservedContextCodec,
    correlation_id: str = "corr-1",
    *,
    principal_id: str = "user-1",
    policy_version: str = "policy-v1",
) -> str:
    return codec.encode(
        ReservedContextClaims(
            tenant_id="tenant-a",
            principal_id=principal_id,
            roles=("operator",),
            deployment_ref="deployment-a",
            audience="agent-server:a",
            correlation_id=correlation_id,
            run_id=correlation_id,
            policy_version=policy_version,
            issued_at=100,
            expires_at=200,
        )
    )


def _tool_inventory(coverage: InventoryCoverage) -> ToolInventory:
    return ToolInventory(
        entries=(
            ToolInventoryEntry(
                identity=ToolIdentity("lookup", "sha256:tool"),
                side_effect=SideEffectClass.READ_ONLY,
            ),
        ),
        coverage=coverage,
    )


def _entry() -> InventoryEntryV1:
    return InventoryEntryV1(
        name="lookup",
        fingerprint="sha256:tool",
        side_effect=SideEffectClass.READ_ONLY,
    )


async def _service(
    sqlite_db,
    *,
    expected_graph_version: str = "graph-v1",
    expected_inventory_fingerprint: str | None = inventory_fingerprint((_entry(),)),
):
    signer = _signer()
    codec = ReservedContextCodec(signer, clock=lambda: 150)
    repository = LangGraphEnforcementRepository(
        sqlite_db, NullWorkspaceScopeContext(tenant_id="tenant-a")
    )
    service = LangGraphEnforcementService(
        repository,
        codec=codec,
        signer=signer,
        policy_guard=PolicyGuard(capability_registry=default_capability_registry()),
        budget_checker=_Budget(),
        metrics=MetricsCollector(),
        deployment_ref="deployment-a",
        audience="agent-server:a",
        expected_graph_version=expected_graph_version,
        expected_inventory_fingerprint=expected_inventory_fingerprint,
        now=lambda: NOW,
    )
    return service, codec


async def _register(
    service: LangGraphEnforcementService,
    codec: ReservedContextCodec,
    coverage: InventoryCoverage,
) -> str:
    entries = (_entry(),)
    fingerprint = inventory_fingerprint(entries)
    await service.register_inventory(
        InventoryRegistrationV1(
            context_token=_token(codec),
            tenant_id="tenant-a",
            principal_id="user-1",
            deployment_ref="deployment-a",
            graph_version="graph-v1",
            coverage=coverage,
            entries=entries,
            inventory_fingerprint=fingerprint,
        )
    )
    return fingerprint


def _mock_client(
    coverage: InventoryCoverage,
    events: list[Any],
    *,
    decision: str = "allow",
    approval_ref: str | None = None,
    publish_token: bool = False,
) -> tuple[LangGraphGatewayClient, ReservedContextCodec]:
    codec = ReservedContextCodec(_signer(), clock=lambda: 150)

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        endpoint = request.url.path.rsplit("/", 1)[-1]
        events.append((endpoint, payload))
        if endpoint in {"inventories", "heartbeat"}:
            return httpx.Response(204)
        if endpoint == "decisions":
            return httpx.Response(
                200,
                json={
                    "schema_version": 1,
                    "decision_id": "decision-1",
                    "idempotency_key": payload["idempotency_key"],
                    "decision": decision,
                    "reason_code": (
                        "approval_required" if decision == "require_approval" else "allowed"
                    ),
                    "policy_version": "policy-v1",
                    "approval_ref": approval_ref,
                },
            )
        return httpx.Response(
            200,
            json={
                "correlation_id": payload["correlation_id"],
                "governance_level": payload["claimed_level"],
                "observed_at": NOW.isoformat(),
                "graph_version": payload["graph_version"],
                "adapter_version": payload["adapter_version"],
                "inventory_fingerprint": payload["inventory_fingerprint"],
                "signature_valid": True,
                "tool_manifest_complete": coverage is InventoryCoverage.COMPLETE,
            },
        )

    client = LangGraphGatewayClient(
        "https://zeroth.test",
        api_key="secret",
        tenant_id="tenant-a",
        principal_id="user-1",
        deployment_ref="deployment-a",
        policy_version="policy-v1",
        graph_version="graph-v1",
        inventory=_tool_inventory(coverage),
        heartbeat_interval_seconds=None,
        transport=httpx.MockTransport(handler),
        **({"token_provider": lambda: _token(codec)} if publish_token else {}),
    )
    return client, codec


def test_inventory_registration_contract() -> None:
    events: list[Any] = []
    client, codec = _mock_client(InventoryCoverage.COMPLETE, events)
    client.register_inventory(_token(codec))
    endpoint, payload = events[0]
    assert endpoint == "inventories"
    assert (payload["deployment_ref"], payload["graph_version"], payload["adapter_version"]) == (
        "deployment-a",
        "graph-v1",
        "2",
    )
    assert payload["coverage"] == "complete"
    assert payload["entries"][0]["fingerprint"] == "sha256:tool"
    assert payload["inventory_fingerprint"] == client.inventory_fingerprint


def test_deployment_client_derives_each_request_identity_from_current_token() -> None:
    events: list[Any] = []
    codec = ReservedContextCodec(_signer(), clock=lambda: 150)
    current = {"token": _token(codec, "corr-1")}

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        endpoint = request.url.path.rsplit("/", 1)[-1]
        events.append((endpoint, payload))
        if endpoint in {"inventories", "heartbeat"}:
            return httpx.Response(204)
        if endpoint == "attestations":
            return httpx.Response(
                200,
                json={
                    "correlation_id": payload["correlation_id"],
                    "governance_level": payload["claimed_level"],
                    "observed_at": NOW.isoformat(),
                    "graph_version": payload["graph_version"],
                    "adapter_version": payload["adapter_version"],
                    "inventory_fingerprint": payload["inventory_fingerprint"],
                    "signature_valid": True,
                    "tool_manifest_complete": True,
                },
            )
        return httpx.Response(
            200,
            json={
                "schema_version": 1,
                "decision_id": f"decision-{len(events)}",
                "idempotency_key": payload["idempotency_key"],
                "decision": "allow",
                "reason_code": "allowed",
                "policy_version": payload["policy_version"],
                "approval_ref": None,
            },
        )

    client = LangGraphGatewayClient(
        "https://zeroth.test",
        api_key="secret",
        tenant_id="tenant-a",
        principal_id="constructor-user",
        deployment_ref="deployment-a",
        policy_version="constructor-policy",
        graph_version="graph-v1",
        inventory=_tool_inventory(InventoryCoverage.COMPLETE),
        heartbeat_interval_seconds=None,
        transport=httpx.MockTransport(handler),
        token_provider=lambda: current["token"],
    )
    action = ToolAction(identity=ToolIdentity("lookup", "sha256:tool"), arguments={})

    for principal, policy, correlation in (
        ("user-1", "policy-v1", "corr-1"),
        ("user-2", "policy-v2", "corr-2"),
    ):
        token = _token(
            codec,
            correlation,
            principal_id=principal,
            policy_version=policy,
        )
        current["token"] = token
        client.register_inventory(token)
        client.attest_run(token, correlation)
        client.heartbeat(token)
        assert (
            client.decide(
                action,
                ToolGovernanceContext(
                    tenant_id="tenant-a",
                    principal_id=principal,
                    run_id=correlation,
                    correlation_id=correlation,
                ),
            ).kind
            is ToolDecisionKind.ALLOW
        )

    request_payloads = [payload for _, payload in events]
    assert [
        (payload["principal_id"], payload.get("policy_version")) for payload in request_payloads
    ] == [
        ("user-1", None),
        ("user-1", None),
        ("user-1", None),
        ("user-1", "policy-v1"),
        ("user-2", None),
        ("user-2", None),
        ("user-2", None),
        ("user-2", "policy-v2"),
    ]
    assert request_payloads[3]["idempotency_key"] != request_payloads[7]["idempotency_key"]
    client.close()


def test_complete_inventory_requires_independent_declared_manifest() -> None:
    recorded = _tool_inventory(InventoryCoverage.PARTIAL)
    declared = (
        ToolIdentity("lookup", "sha256:tool"),
        ToolIdentity("mutate", "sha256:missing"),
    )
    with pytest.raises(ToolGovernanceError):
        attest_complete_inventory(recorded, declared)


@pytest.mark.parametrize(
    "resolver_name",
    ("side_effect", "contract_ref", "capability_refs", "requires_approval"),
)
def test_metadata_resolver_failures_do_not_default_permissive(resolver_name: str) -> None:
    body_calls = 0

    def lookup(q: str) -> str:
        nonlocal body_calls
        body_calls += 1
        return q

    def unavailable(_tool: object) -> object:
        raise RuntimeError("metadata source unavailable")

    resolvers: dict[str, Any] = {
        "side_effect": lambda _tool: SideEffectClass.READ_ONLY,
        "contract_ref": lambda _tool: "contract:lookup",
        "capability_refs": lambda _tool: ("network_read",),
        "requires_approval": lambda _tool: True,
    }
    resolvers[resolver_name] = unavailable

    with pytest.raises(ToolGovernanceError, match="metadata resolver failed"):
        govern_tools([lookup], **resolvers)

    class AllowClient:
        calls = 0

        def decide(self, action: ToolAction, context: ToolGovernanceContext) -> ToolDecision:
            del action, context
            self.calls += 1
            return ToolDecision(kind=ToolDecisionKind.ALLOW, reason_code="allowed")

    client = AllowClient()
    fail_live = False

    def reviewed(_tool: object) -> object:
        return {
            "side_effect": SideEffectClass.READ_ONLY,
            "contract_ref": "contract:lookup",
            "capability_refs": ("network_read",),
            "requires_approval": True,
        }[resolver_name]

    def live(target: object) -> object:
        if fail_live:
            return unavailable(target)
        return reviewed(target)

    resolvers[resolver_name] = live
    [governed] = govern_tools(
        [lookup],
        context=ToolGovernanceContext("tenant-a", "user-1", "run-1"),
        client=client,
        **resolvers,
    )
    recorded = record_tool_inventory([governed])
    fail_live = True

    with pytest.raises(ToolGovernanceError, match="metadata resolver failed"):
        governed(q="cats")

    assert record_tool_inventory([governed]) == recorded
    assert client.calls == 0
    assert body_calls == 0


def test_signed_run_start_attestation() -> None:
    events: list[Any] = []
    client, codec = _mock_client(InventoryCoverage.COMPLETE, events)
    action = ToolAction(
        identity=ToolIdentity("lookup", "sha256:tool"),
        arguments={"q": "cats"},
        side_effect=SideEffectClass.READ_ONLY,
    )
    context = ToolGovernanceContext("tenant-a", "user-1", "run-1", correlation_id="corr-1")

    class Graph:
        name = "graph-v1"

        def invoke(self, value: Any, config: Any = None) -> Any:
            del config
            events.append(("graph", client.decide(action, context)))
            return value

    result = govern_graph(Graph(), gateway_client=client).invoke(
        {"value": 1}, config={"configurable": {"_zeroth": _token(codec)}}
    )
    assert result == {"value": 1}
    assert [event[0] for event in events] == ["inventories", "attestations", "decisions", "graph"]
    assert events[1][1]["correlation_id"] == "corr-1"
    assert events[2][1]["context_token"] == _token(codec)
    assert events[2][1]["run_id"] == "corr-1"
    assert events[3][1].kind is ToolDecisionKind.ALLOW


def test_public_tool_metadata_reaches_gateway(tmp_path: Any) -> None:
    events: list[Any] = []
    codec = ReservedContextCodec(_signer(), clock=lambda: 150)
    inventory = ToolInventory(
        entries=(
            ToolInventoryEntry(
                identity=ToolIdentity("lookup", "sha256:tool"),
                side_effect=SideEffectClass.SIDE_EFFECTING,
                capability_refs=("capability:search",),
                requires_approval=True,
            ),
        ),
        coverage=InventoryCoverage.COMPLETE,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        endpoint = request.url.path.rsplit("/", 1)[-1]
        events.append((endpoint, payload))
        if endpoint == "inventories":
            return httpx.Response(204)
        approval = payload["action"]["requires_approval"]
        return httpx.Response(
            200,
            json={
                "schema_version": 1,
                "decision_id": "decision-approval" if approval else "decision-denied",
                "idempotency_key": payload["idempotency_key"],
                "decision": "require_approval" if approval else "deny",
                "reason_code": "approval_required" if approval else "capability_denied",
                "policy_version": "policy-v1",
                "approval_ref": "decision-approval" if approval else None,
            },
        )

    client = LangGraphGatewayClient(
        "https://zeroth.test",
        api_key="secret",
        tenant_id="tenant-a",
        principal_id="user-1",
        deployment_ref="deployment-a",
        policy_version="policy-v1",
        graph_version="graph-v1",
        inventory=inventory,
        heartbeat_interval_seconds=None,
        transport=httpx.MockTransport(handler),
        token_provider=lambda: _token(codec),
    )
    client.register_inventory(_token(codec))
    context = ToolGovernanceContext(
        "tenant-a", "user-1", "upstream-run", thread_id="thread-1", correlation_id="corr-1"
    )

    def lookup(q: str) -> str:
        return q

    common = {
        "context": context,
        "client": client,
        "side_effect": lambda _tool: SideEffectClass.SIDE_EFFECTING,
        "capability_refs": lambda _tool: ("capability:search",),
    }
    [denied] = govern_tools([lookup], **common)
    with pytest.raises(PolicyViolation):
        denied(q="cats")

    class PausedError(RuntimeError):
        pass

    [approval] = govern_tools(
        [lookup],
        **common,
        requires_approval=lambda _tool: True,
        interrupt=lambda _payload: (_ for _ in ()).throw(PausedError()),
        approval_lifecycle=SQLiteApprovalRepository(tmp_path / "approvals.sqlite3"),
    )
    with pytest.raises(PausedError):
        approval(q="cats")

    assert events[0][1]["entries"][0]["capability_refs"] == ["capability:search"]
    assert events[0][1]["entries"][0]["requires_approval"] is True
    assert events[1][1]["action"]["capability_refs"] == ["capability:search"]
    assert events[1][1]["action"]["requires_approval"] is False
    assert events[2][1]["action"]["capability_refs"] == ["capability:search"]
    assert events[2][1]["action"]["requires_approval"] is True


async def test_recorded_public_tool_metadata_matches_live_action(sqlite_db, tmp_path: Any) -> None:
    captured: list[ToolAction] = []

    class ApprovalClient:
        def decide(self, action: ToolAction, context: ToolGovernanceContext) -> ToolDecision:
            del context
            captured.append(action)
            return ToolDecision(
                kind=ToolDecisionKind.REQUIRE_APPROVAL,
                reason_code="approval_required",
                approval_ref="approval-1",
            )

    class PausedError(RuntimeError):
        pass

    def lookup(q: str) -> str:
        return q

    [governed] = govern_tools(
        [lookup],
        context=ToolGovernanceContext(
            "tenant-a", "user-1", "corr-1", thread_id="thread-1", correlation_id="corr-1"
        ),
        client=ApprovalClient(),
        side_effect=lambda _tool: SideEffectClass.SIDE_EFFECTING,
        contract_ref=lambda _tool: "contract:lookup",
        capability_refs=lambda _tool: ("network_read",),
        requires_approval=lambda _tool: True,
        interrupt=lambda _payload: (_ for _ in ()).throw(PausedError()),
        approval_lifecycle=SQLiteApprovalRepository(tmp_path / "approvals.sqlite3"),
    )
    recorded = record_tool_inventory([governed])
    inventory = attest_complete_inventory(recorded, [governed.zeroth_binding.identity])
    [recorded_entry] = inventory.entries
    entries = (
        InventoryEntryV1(
            name=recorded_entry.identity.name,
            fingerprint=recorded_entry.identity.fingerprint,
            side_effect=recorded_entry.side_effect,
            contract_ref=recorded_entry.contract_ref,
            capability_refs=tuple(recorded_entry.capability_refs),
            requires_approval=recorded_entry.requires_approval,
        ),
    )
    fingerprint = inventory_fingerprint(entries)
    service, codec = await _service(sqlite_db, expected_inventory_fingerprint=fingerprint)

    class AllowMetadataPolicy:
        def evaluate_run_admission(self, request):
            del request
            return SimpleNamespace(allowed=True, policy_version="policy-v1")

        def evaluate(self, graph, node, run, arguments):
            del graph, node, run, arguments
            return SimpleNamespace(
                decision=PolicyDecision.ALLOW,
                approval_required_for_side_effects=False,
            )

    service.policy_guard = AllowMetadataPolicy()
    await service.register_inventory(
        InventoryRegistrationV1(
            context_token=_token(codec),
            tenant_id="tenant-a",
            principal_id="user-1",
            deployment_ref="deployment-a",
            graph_version="graph-v1",
            coverage=inventory.coverage,
            entries=entries,
            inventory_fingerprint=fingerprint,
        )
    )
    evidence = await service.attest_run(
        RunAttestationV1(
            context_token=_token(codec),
            tenant_id="tenant-a",
            principal_id="user-1",
            deployment_ref="deployment-a",
            correlation_id="corr-1",
            graph_version="graph-v1",
            inventory_fingerprint=fingerprint,
            claimed_level=GovernanceLevel.ENFORCED,
        )
    )

    with pytest.raises(PausedError):
        governed(q="cats")
    [live_action] = captured
    response = await service.decide(
        DecisionRequestV1(
            idempotency_key="recorded-live-metadata",
            context_token=_token(codec),
            tenant_id="tenant-a",
            principal_id="user-1",
            deployment_ref="deployment-a",
            correlation_id="corr-1",
            run_id="corr-1",
            policy_version="policy-v1",
            inventory_fingerprint=fingerprint,
            action=ActionDescriptorV1(
                name=live_action.identity.name,
                fingerprint=live_action.identity.fingerprint,
                tool_call_id="call-recorded-metadata",
                arguments=dict(live_action.arguments),
                side_effect=live_action.side_effect,
                contract_ref=live_action.contract_ref,
                capability_refs=tuple(live_action.capability_refs),
                requires_approval=live_action.requires_approval,
            ),
        )
    )

    assert evidence.governance_level is GovernanceLevel.ENFORCED
    assert (
        recorded_entry.side_effect,
        recorded_entry.contract_ref,
        tuple(recorded_entry.capability_refs),
        recorded_entry.requires_approval,
    ) == (
        live_action.side_effect,
        live_action.contract_ref,
        tuple(live_action.capability_refs),
        live_action.requires_approval,
    )
    assert response.decision is ToolDecisionKind.REQUIRE_APPROVAL


def test_start_run_schedules_default_heartbeat() -> None:
    heartbeat_seen = threading.Event()
    codec = ReservedContextCodec(_signer(), clock=lambda: 150)

    def handler(request: httpx.Request) -> httpx.Response:
        endpoint = request.url.path.rsplit("/", 1)[-1]
        if endpoint == "heartbeat":
            heartbeat_seen.set()
            return httpx.Response(204)
        if endpoint == "inventories":
            return httpx.Response(204)
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "correlation_id": payload["correlation_id"],
                "governance_level": payload["claimed_level"],
                "observed_at": NOW.isoformat(),
                "graph_version": payload["graph_version"],
                "adapter_version": payload["adapter_version"],
                "inventory_fingerprint": payload["inventory_fingerprint"],
                "signature_valid": True,
                "tool_manifest_complete": True,
            },
        )

    client = LangGraphGatewayClient(
        "https://zeroth.test",
        api_key="secret",
        tenant_id="tenant-a",
        principal_id="user-1",
        deployment_ref="deployment-a",
        policy_version="policy-v1",
        graph_version="graph-v1",
        inventory=_tool_inventory(InventoryCoverage.COMPLETE),
        heartbeat_interval_seconds=0.01,
        transport=httpx.MockTransport(handler),
    )
    try:
        client.start_run(_token(codec), "corr-1")
        assert heartbeat_seen.wait(timeout=1.0)
    finally:
        client.close()


def test_gateway_client_preserves_approval_reference() -> None:
    events: list[Any] = []
    client, codec = _mock_client(
        InventoryCoverage.COMPLETE,
        events,
        decision="require_approval",
        approval_ref="decision-1",
        publish_token=True,
    )
    decision = client.decide(
        ToolAction(
            identity=ToolIdentity("lookup", "sha256:tool"),
            arguments={},
            side_effect=SideEffectClass.READ_ONLY,
            tool_call_id="call-1",
        ),
        ToolGovernanceContext("tenant-a", "user-1", "run-1", correlation_id="corr-1"),
    )
    assert decision.kind is ToolDecisionKind.REQUIRE_APPROVAL
    assert decision.approval_ref == "decision-1"


def test_distinct_tool_calls_get_distinct_approval_identity() -> None:
    events: list[Any] = []
    codec = ReservedContextCodec(_signer(), clock=lambda: 150)

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        events.append(payload)
        decision_id = f"decision:{payload['idempotency_key']}"
        return httpx.Response(
            200,
            json={
                "schema_version": 1,
                "decision_id": decision_id,
                "idempotency_key": payload["idempotency_key"],
                "decision": "require_approval",
                "reason_code": "approval_required",
                "policy_version": "policy-v1",
                "approval_ref": decision_id,
            },
        )

    client = LangGraphGatewayClient(
        "https://zeroth.test",
        api_key="secret",
        tenant_id="tenant-a",
        principal_id="user-1",
        deployment_ref="deployment-a",
        policy_version="policy-v1",
        graph_version="graph-v1",
        inventory=_tool_inventory(InventoryCoverage.COMPLETE),
        heartbeat_interval_seconds=None,
        transport=httpx.MockTransport(handler),
        token_provider=lambda: _token(codec),
    )
    context = ToolGovernanceContext(
        "tenant-a", "user-1", "run-1", thread_id="thread-1", correlation_id="corr-1"
    )

    def decide(call_id: str) -> ToolDecision:
        return client.decide(
            ToolAction(
                identity=ToolIdentity("lookup", "sha256:tool"),
                arguments={"q": "cats"},
                side_effect=SideEffectClass.READ_ONLY,
                requires_approval=True,
                tool_call_id=call_id,
            ),
            context,
        )

    first = decide("call-1")
    second = decide("call-2")
    retry = decide("call-1")

    assert first.approval_ref != second.approval_ref
    assert first.approval_ref == retry.approval_ref
    assert events[0]["idempotency_key"] != events[1]["idempotency_key"]
    assert events[0]["idempotency_key"] == events[2]["idempotency_key"]
    assert [event["action"]["tool_call_id"] for event in events] == [
        "call-1",
        "call-2",
        "call-1",
    ]


async def test_heartbeat_downgrades_but_never_upgrades(sqlite_db) -> None:
    events: list[Any] = []
    client, codec = _mock_client(InventoryCoverage.COMPLETE, events)
    client.heartbeat(_token(codec))
    service, server_codec = await _service(sqlite_db)
    fingerprint = await _register(service, server_codec, InventoryCoverage.COMPLETE)
    await service.heartbeat(
        HeartbeatV1(
            context_token=_token(server_codec),
            tenant_id="tenant-a",
            principal_id="user-1",
            deployment_ref="deployment-a",
            graph_version="graph-v1",
            inventory_fingerprint=fingerprint,
        )
    )
    heartbeat = service.deployment_evidence
    assert heartbeat is not None and heartbeat.signature_valid
    reporter = CapabilityReporter(
        now=lambda: NOW + timedelta(seconds=1),
        expected_adapter_version="2",
    )
    assert reporter.level_for_deployment(heartbeat) is GovernanceLevel.ENFORCED
    assert (
        await reporter.level_for_run("corr-1", deployment_evidence=heartbeat)
        is GovernanceLevel.ADMISSION
    )
    stale = CapabilityReporter(now=lambda: NOW + timedelta(seconds=91))
    assert stale.level_for_deployment(heartbeat) is GovernanceLevel.ADMISSION
    assert events[0][0] == "heartbeat"


def test_partial_inventory_cannot_be_enforced() -> None:
    events: list[Any] = []
    client, codec = _mock_client(InventoryCoverage.PARTIAL, events)
    evidence = client.start_run(_token(codec), "corr-1")
    assert events[0][1]["coverage"] == "partial"
    assert events[1][1]["claimed_level"] == "observed"
    assert evidence.governance_level is GovernanceLevel.OBSERVED


async def test_attestation_without_complete_inventory_is_not_enforced(sqlite_db) -> None:
    service, codec = await _service(sqlite_db)
    fingerprint = await _register(service, codec, InventoryCoverage.PARTIAL)
    evidence = await service.attest_run(
        RunAttestationV1(
            context_token=_token(codec),
            tenant_id="tenant-a",
            principal_id="user-1",
            deployment_ref="deployment-a",
            correlation_id="corr-1",
            graph_version="graph-v1",
            inventory_fingerprint=fingerprint,
            claimed_level=GovernanceLevel.ENFORCED,
        )
    )
    assert evidence.governance_level is GovernanceLevel.OBSERVED
    assert evidence.run_id == "corr-1"
    assert evidence.tool_manifest_complete is False


async def test_client_complete_claim_cannot_bypass_server_manifest(sqlite_db) -> None:
    service, codec = await _service(sqlite_db, expected_inventory_fingerprint=None)
    fingerprint = await _register(service, codec, InventoryCoverage.COMPLETE)
    evidence = await service.attest_run(
        RunAttestationV1(
            context_token=_token(codec),
            tenant_id="tenant-a",
            principal_id="user-1",
            deployment_ref="deployment-a",
            correlation_id="corr-1",
            graph_version="graph-v1",
            inventory_fingerprint=fingerprint,
            claimed_level=GovernanceLevel.ENFORCED,
        )
    )
    assert evidence.governance_level is GovernanceLevel.OBSERVED
    assert evidence.tool_manifest_complete is False
    await service.heartbeat(
        HeartbeatV1(
            context_token=_token(codec),
            tenant_id="tenant-a",
            principal_id="user-1",
            deployment_ref="deployment-a",
            graph_version="graph-v1",
            inventory_fingerprint=fingerprint,
        )
    )
    assert service.deployment_evidence is not None
    assert service.deployment_evidence.governance_level is GovernanceLevel.OBSERVED
    assert service.deployment_evidence.tool_manifest_complete is False

    service.expected_inventory_fingerprint = "sha256:" + "0" * 64
    mismatch = await service.attest_run(
        RunAttestationV1(
            context_token=_token(codec, correlation_id="corr-2"),
            tenant_id="tenant-a",
            principal_id="user-1",
            deployment_ref="deployment-a",
            correlation_id="corr-2",
            graph_version="graph-v1",
            inventory_fingerprint=fingerprint,
            claimed_level=GovernanceLevel.ENFORCED,
        )
    )
    assert mismatch.governance_level is GovernanceLevel.OBSERVED
    assert mismatch.tool_manifest_complete is False


async def test_run_attestation_cannot_be_upgraded_by_retry(sqlite_db) -> None:
    service, codec = await _service(sqlite_db)
    fingerprint = await _register(service, codec, InventoryCoverage.PARTIAL)
    request = RunAttestationV1(
        context_token=_token(codec),
        tenant_id="tenant-a",
        principal_id="user-1",
        deployment_ref="deployment-a",
        correlation_id="corr-1",
        graph_version="graph-v1",
        inventory_fingerprint=fingerprint,
        claimed_level=GovernanceLevel.ENFORCED,
    )
    first = await service.attest_run(request)
    await _register(service, codec, InventoryCoverage.COMPLETE)
    with pytest.raises(EnforcementBoundaryError) as exc:
        await service.attest_run(request)
    assert exc.value.code == "zeroth.attestation_conflict"
    assert first.governance_level is GovernanceLevel.OBSERVED


async def test_fingerprint_mismatch_prevents_enforcement(sqlite_db) -> None:
    service, codec = await _service(sqlite_db)
    await _register(service, codec, InventoryCoverage.COMPLETE)
    evidence = await service.attest_run(
        RunAttestationV1(
            context_token=_token(codec),
            tenant_id="tenant-a",
            principal_id="user-1",
            deployment_ref="deployment-a",
            correlation_id="corr-1",
            graph_version="graph-v1",
            inventory_fingerprint="sha256:mismatch",
            claimed_level=GovernanceLevel.ENFORCED,
        )
    )
    assert evidence.governance_level is GovernanceLevel.OBSERVED


async def test_mixed_versions_downgrade_enforcement(sqlite_db) -> None:
    service, codec = await _service(sqlite_db)
    entries = (_entry(),)
    fingerprint = inventory_fingerprint(entries)
    await service.register_inventory(
        InventoryRegistrationV1(
            context_token=_token(codec),
            tenant_id="tenant-a",
            principal_id="user-1",
            deployment_ref="deployment-a",
            graph_version="graph-v2",
            adapter_version="2",
            coverage=InventoryCoverage.COMPLETE,
            entries=entries,
            inventory_fingerprint=fingerprint,
        )
    )
    evidence = await service.attest_run(
        RunAttestationV1(
            context_token=_token(codec),
            tenant_id="tenant-a",
            principal_id="user-1",
            deployment_ref="deployment-a",
            correlation_id="corr-1",
            graph_version="graph-v2",
            adapter_version="2",
            inventory_fingerprint=fingerprint,
            claimed_level=GovernanceLevel.ENFORCED,
        )
    )
    assert evidence.governance_level is GovernanceLevel.ADMISSION


async def test_server_recomputes_authoritative_run_classification(sqlite_db) -> None:
    service, codec = await _service(sqlite_db)
    fingerprint = await _register(service, codec, InventoryCoverage.COMPLETE)
    lower = await service.attest_run(
        RunAttestationV1(
            context_token=_token(codec),
            tenant_id="tenant-a",
            principal_id="user-1",
            deployment_ref="deployment-a",
            correlation_id="corr-1",
            graph_version="graph-v1",
            inventory_fingerprint=fingerprint,
            claimed_level=GovernanceLevel.ADMISSION,
        )
    )
    assert lower.governance_level is GovernanceLevel.ADMISSION


def test_gateway_client_fails_closed_without_reserved_token() -> None:
    events: list[Any] = []
    client, _ = _mock_client(InventoryCoverage.COMPLETE, events)
    decision = client.decide(
        ToolAction(
            identity=ToolIdentity("lookup", "sha256:tool"),
            arguments={},
            side_effect=SideEffectClass.READ_ONLY,
        ),
        ToolGovernanceContext("tenant-a", "user-1", "run-1"),
    )
    assert decision.kind is ToolDecisionKind.DENY
    assert events == []
