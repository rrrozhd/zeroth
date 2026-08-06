from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from zeroth.contracts.langgraph_gateway.models import GovernanceLevel
from zeroth.econ.analytics.budget import BudgetCheckResult
from zeroth.governance.policy import PolicyDecision, PolicyGuard
from zeroth.governance.policy.registry import default_capability_registry
from zeroth.integrations.langgraph import InventoryCoverage, SideEffectClass, ToolDecisionKind
from zeroth.platform.observability.metrics import MetricsCollector
from zeroth.platform.signing import EnvHmacSigner
from zeroth.service.langgraph_gateway.context import ReservedContextClaims, ReservedContextCodec
from zeroth.service.langgraph_gateway.enforcement import (
    ActionDescriptorV1,
    DecisionRequestV1,
    DecisionResponseV1,
    EnforcementBoundaryError,
    HeartbeatV1,
    InventoryEntryV1,
    InventoryRegistrationV1,
    LangGraphEnforcementRepository,
    LangGraphEnforcementService,
    RunAttestationV1,
    inventory_fingerprint,
)

NOW = datetime(2026, 8, 4, 17, 0, tzinfo=UTC)
POLICY_VERSION = "sha256:4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945"


class Budget:
    def __init__(self, *, allowed: bool = True, degraded: bool = False, raises: bool = False):
        self.allowed = allowed
        self.degraded = degraded
        self.raises = raises

    async def check_budget_status(self, tenant_id: str) -> BudgetCheckResult:
        del tenant_id
        if self.raises:
            raise RuntimeError("private backend detail")
        return BudgetCheckResult(
            allowed=self.allowed,
            spend_usd=0,
            cap_usd=10,
            degraded=self.degraded,
            failure_mode="fail_closed" if self.degraded else "none",
        )


def _signer() -> EnvHmacSigner:
    return EnvHmacSigner(key_id="k1", keys={"k1": b"test-signing-key"})


def _token(
    codec: ReservedContextCodec,
    *,
    tenant: str = "tenant-a",
    principal: str = "user-1",
    deployment: str = "deployment-a",
    audience: str = "agent-server:a",
    correlation: str = "corr-1",
    run_id: str | None = "run-1",
    policy: str = POLICY_VERSION,
    content_classification: str | None = None,
) -> str:
    return codec.encode(
        ReservedContextClaims(
            tenant_id=tenant,
            principal_id=principal,
            roles=("operator",),
            deployment_ref=deployment,
            audience=audience,
            correlation_id=correlation,
            run_id=run_id,
            policy_version=policy,
            issued_at=100,
            expires_at=200,
            content_classification=content_classification,
        )
    )


def _entry(
    *,
    side_effect: SideEffectClass = SideEffectClass.READ_ONLY,
    requires_approval: bool = False,
) -> InventoryEntryV1:
    return InventoryEntryV1(
        name="lookup",
        fingerprint="sha256:tool",
        side_effect=side_effect,
        requires_approval=requires_approval,
    )


async def _service(sqlite_db, *, budget: Budget | None = None):
    signer = _signer()
    codec = ReservedContextCodec(signer, clock=lambda: 150)
    repository = LangGraphEnforcementRepository(sqlite_db)
    metrics = MetricsCollector()
    service = LangGraphEnforcementService(
        repository,
        codec=codec,
        signer=signer,
        policy_guard=PolicyGuard(capability_registry=default_capability_registry()),
        budget_checker=budget or Budget(),
        metrics=metrics,
        deployment_ref="deployment-a",
        audience="agent-server:a",
        expected_graph_version="graph-v1",
        expected_inventory_fingerprint=inventory_fingerprint((_entry(),)),
        now=lambda: NOW,
    )
    return service, repository, metrics, codec


async def _register(service, codec, *, tenant: str = "tenant-a", entry=None):
    entries = (entry or _entry(),)
    fingerprint = inventory_fingerprint(entries)
    await service.register_inventory(
        InventoryRegistrationV1(
            context_token=_token(codec, tenant=tenant),
            tenant_id=tenant,
            principal_id="user-1",
            deployment_ref="deployment-a",
            graph_version="graph-v1",
            coverage=InventoryCoverage.COMPLETE,
            entries=entries,
            inventory_fingerprint=fingerprint,
        )
    )
    return fingerprint


def _request(
    codec,
    fingerprint: str,
    *,
    tenant: str = "tenant-a",
    key: str = "key-1",
    action: ActionDescriptorV1 | None = None,
    token: str | None = None,
):
    return DecisionRequestV1(
        idempotency_key=key,
        context_token=token or _token(codec, tenant=tenant),
        tenant_id=tenant,
        principal_id="user-1",
        deployment_ref="deployment-a",
        correlation_id="corr-1",
        run_id="run-1",
        policy_version=POLICY_VERSION,
        inventory_fingerprint=fingerprint,
        action=action
        or ActionDescriptorV1(
            name="lookup", fingerprint="sha256:tool", side_effect=SideEffectClass.READ_ONLY
        ),
    )


def test_versioned_decision_contract() -> None:
    fields = set(DecisionRequestV1.model_fields)
    assert {
        "schema_version",
        "idempotency_key",
        "context_token",
        "tenant_id",
        "principal_id",
        "deployment_ref",
        "correlation_id",
        "run_id",
        "policy_version",
        "action",
    } <= fields
    with pytest.raises(ValidationError):
        DecisionRequestV1.model_validate({"schema_version": 2, "unexpected": True})


async def test_decision_reuses_governance_semantics(sqlite_db) -> None:
    service, _, _, codec = await _service(sqlite_db)
    fingerprint = await _register(service, codec)
    response = await service.decide(_request(codec, fingerprint))
    assert response.decision is ToolDecisionKind.ALLOW


async def test_changed_server_policy_version_fails_closed(sqlite_db) -> None:
    service, _, _, codec = await _service(sqlite_db)
    fingerprint = await _register(service, codec)
    request = _request(
        codec,
        fingerprint,
        key="key-policy-mismatch",
        token=_token(codec, policy="stale-policy-version"),
    ).model_copy(update={"policy_version": "stale-policy-version"})
    response = await service.decide(request)
    assert response.decision is ToolDecisionKind.DENY
    assert response.reason_code == "policy_version_mismatch"
    assert response.policy_version == POLICY_VERSION


async def test_policy_revision_change_cannot_replay_cached_allow(sqlite_db) -> None:
    service, _, _, codec = await _service(sqlite_db)
    fingerprint = await _register(service, codec)
    request = _request(codec, fingerprint, key="policy-revision")
    assert (await service.decide(request)).decision is ToolDecisionKind.ALLOW

    class RevisedGuard:
        def evaluate_run_admission(self, request):
            del request
            return SimpleNamespace(allowed=False, policy_version="policy-v2")

    service.policy_guard = RevisedGuard()
    with pytest.raises(EnforcementBoundaryError) as exc:
        await service.decide(request)
    assert exc.value.code == "zeroth.idempotency_conflict"


async def test_decision_admission_uses_signed_content_classification(sqlite_db) -> None:
    seen: list[str] = []

    class Guard:
        def evaluate_run_admission(self, request):
            seen.append(request.input_classification)
            return SimpleNamespace(allowed=False, policy_version="policy-v1")

    signer = _signer()
    codec = ReservedContextCodec(signer, clock=lambda: 150)
    repository = LangGraphEnforcementRepository(sqlite_db)
    service = LangGraphEnforcementService(
        repository,
        codec=codec,
        signer=signer,
        policy_guard=Guard(),
        budget_checker=Budget(),
        metrics=MetricsCollector(),
        deployment_ref="deployment-a",
        audience="agent-server:a",
        expected_graph_version="graph-v1",
        now=lambda: NOW,
    )
    fingerprint = await _register(service, codec)
    request = _request(
        codec,
        fingerprint,
        token=_token(codec, content_classification="restricted"),
    )
    response = await service.decide(request)
    assert response.decision is ToolDecisionKind.DENY
    assert seen == ["restricted"]


async def test_deployment_scoped_authentication(sqlite_db) -> None:
    service, repository, _, codec = await _service(sqlite_db)
    fingerprint = await _register(service, codec)
    request = _request(codec, fingerprint, token=_token(codec, deployment="deployment-b"))
    with pytest.raises(EnforcementBoundaryError, match="request rejected") as exc:
        await service.decide(request)
    assert exc.value.code == "zeroth.invalid_context"
    assert await repository.count_decisions() == 0


async def test_decision_is_bound_to_token_correlation(sqlite_db) -> None:
    service, repository, _, codec = await _service(sqlite_db)
    fingerprint = await _register(service, codec)
    request = _request(codec, fingerprint).model_copy(update={"correlation_id": "corr-2"})
    with pytest.raises(EnforcementBoundaryError) as exc:
        await service.decide(request)
    assert exc.value.code == "zeroth.invalid_context"
    assert await repository.count_decisions() == 0


async def test_decision_is_bound_to_signed_run_id(sqlite_db) -> None:
    service, repository, _, codec = await _service(sqlite_db)
    fingerprint = await _register(service, codec)
    request = _request(
        codec,
        fingerprint,
        token=_token(codec, run_id="run-signed"),
    ).model_copy(update={"run_id": "run-submitted"})
    with pytest.raises(EnforcementBoundaryError) as exc:
        await service.decide(request)
    assert exc.value.code == "zeroth.invalid_context"
    assert await repository.count_decisions() == 0

    unsigned_run = _request(
        codec,
        fingerprint,
        key="missing-signed-run",
        token=_token(codec, run_id=None),
    )
    with pytest.raises(EnforcementBoundaryError) as missing:
        await service.decide(unsigned_run)
    assert missing.value.code == "zeroth.invalid_context"
    assert await repository.count_decisions() == 0


async def test_safe_errors_fail_closed(sqlite_db) -> None:
    service, _, _, codec = await _service(sqlite_db)
    fingerprint = await _register(service, codec)
    with pytest.raises(EnforcementBoundaryError) as exc:
        await service.decide(_request(codec, fingerprint, token="malformed"))
    assert exc.value.code == "zeroth.invalid_context"
    assert "malformed" not in str(exc.value)


async def test_identical_idempotent_request_returns_same_decision(sqlite_db) -> None:
    service, _, _, codec = await _service(sqlite_db)
    fingerprint = await _register(service, codec)
    request = _request(codec, fingerprint)
    assert (await service.decide(request)).decision_id == (
        await service.decide(request)
    ).decision_id


async def test_approval_decision_carries_resumable_reference(sqlite_db) -> None:
    service, _, _, codec = await _service(sqlite_db)
    entry = _entry(requires_approval=True)
    fingerprint = await _register(service, codec, entry=entry)
    action = ActionDescriptorV1(
        name=entry.name,
        fingerprint=entry.fingerprint,
        side_effect=entry.side_effect,
        requires_approval=True,
        tool_call_id="call-1",
    )
    response = await service.decide(_request(codec, fingerprint, action=action))
    assert response.decision is ToolDecisionKind.REQUIRE_APPROVAL
    assert response.approval_ref == response.decision_id


@pytest.mark.parametrize("approval_source", ["inventory", "side_effect"])
async def test_approval_without_tool_call_identity_fails_closed(
    sqlite_db, approval_source: str
) -> None:
    service, _, _, codec = await _service(sqlite_db)
    entry = _entry(
        side_effect=(
            SideEffectClass.SIDE_EFFECTING
            if approval_source == "side_effect"
            else SideEffectClass.READ_ONLY
        ),
        requires_approval=approval_source == "inventory",
    )
    fingerprint = await _register(service, codec, entry=entry)
    if approval_source == "side_effect":

        class ApprovalPolicy:
            def evaluate_run_admission(self, request):
                del request
                return SimpleNamespace(allowed=True, policy_version=POLICY_VERSION)

            def evaluate(self, graph, node, run, arguments):
                del graph, node, run, arguments
                return SimpleNamespace(
                    decision=PolicyDecision.ALLOW,
                    approval_required_for_side_effects=True,
                )

        service.policy_guard = ApprovalPolicy()
    action = ActionDescriptorV1(
        name=entry.name,
        fingerprint=entry.fingerprint,
        side_effect=entry.side_effect,
        requires_approval=entry.requires_approval,
    )

    response = await service.decide(_request(codec, fingerprint, action=action))

    assert response.decision is ToolDecisionKind.DENY
    assert response.reason_code == "approval_requires_tool_call_id"
    assert response.approval_ref is None


async def test_distinct_approval_tool_calls_do_not_share_decision(sqlite_db) -> None:
    service, _, _, codec = await _service(sqlite_db)
    entry = _entry(requires_approval=True)
    fingerprint = await _register(service, codec, entry=entry)

    def request(key: str, call_id: str) -> DecisionRequestV1:
        return _request(
            codec,
            fingerprint,
            key=key,
            action=ActionDescriptorV1(
                name=entry.name,
                fingerprint=entry.fingerprint,
                side_effect=entry.side_effect,
                requires_approval=True,
                tool_call_id=call_id,
            ),
        )

    first_request = request("key-call-1", "call-1")
    first = await service.decide(first_request)
    second = await service.decide(request("key-call-2", "call-2"))
    retry = await service.decide(first_request)

    assert first.decision_id != second.decision_id
    assert first.approval_ref != second.approval_ref
    assert first.decision_id == retry.decision_id


async def test_capability_metadata_controls_server_decision(sqlite_db) -> None:
    service, _, _, codec = await _service(sqlite_db)
    entry = InventoryEntryV1(
        name="lookup",
        fingerprint="sha256:tool",
        side_effect=SideEffectClass.READ_ONLY,
        capability_refs=("network_read",),
    )
    fingerprint = await _register(service, codec, entry=entry)

    class CapabilityGuard:
        def evaluate_run_admission(self, request):
            del request
            return SimpleNamespace(allowed=True, policy_version=POLICY_VERSION)

        def evaluate(self, graph, node, run, arguments):
            del graph, run, arguments
            assert node.capability_bindings == ("network_read",)
            return SimpleNamespace(decision=PolicyDecision.DENY)

    service.policy_guard = CapabilityGuard()
    response = await service.decide(
        _request(
            codec,
            fingerprint,
            key="capability-denial",
            action=ActionDescriptorV1(
                name=entry.name,
                fingerprint=entry.fingerprint,
                side_effect=entry.side_effect,
                capability_refs=entry.capability_refs,
            ),
        )
    )
    assert response.decision is ToolDecisionKind.DENY
    assert response.reason_code == "capability_denied"


async def test_idempotent_insert_is_conflict_safe_before_read() -> None:
    events: list[tuple[str, str]] = []
    response = DecisionResponseV1(
        decision_id="decision-1",
        idempotency_key="key-1",
        decision=ToolDecisionKind.ALLOW,
        reason_code="allowed",
        policy_version="policy-v1",
    )

    class Connection:
        async def execute(self, sql, params=()):
            del params
            events.append(("execute", sql))

        async def fetch_one(self, sql, params=()):
            del params
            events.append(("fetch_one", sql))
            return {
                "deployment_ref": "deployment-a",
                "action_hash": "sha256:action",
                "response_json": response.model_dump_json(),
            }

    class Database:
        backend = "postgres"

        @asynccontextmanager
        async def transaction(self, *, write_lock=False):
            assert write_lock is True
            yield Connection()

    stored = await LangGraphEnforcementRepository(Database()).save_decision(
        "tenant-a",
        "key-1",
        "deployment-a",
        "sha256:action",
        response,
    )
    assert stored == response
    assert events[0][0] == "execute"
    assert "ON CONFLICT(tenant_id, idempotency_key) DO NOTHING" in events[0][1]
    assert events[1][0] == "fetch_one"


async def test_inventory_registration_uses_atomic_upsert() -> None:
    statements: list[str] = []

    class Connection:
        async def execute(self, sql, params=()):
            del params
            statements.append(sql)

    class Database:
        backend = "postgres"

        @asynccontextmanager
        async def transaction(self, *, write_lock=False):
            assert write_lock is True
            yield Connection()

    entries = (_entry(),)
    request = InventoryRegistrationV1(
        context_token="unused",
        tenant_id="tenant-a",
        principal_id="user-1",
        deployment_ref="deployment-a",
        graph_version="graph-v1",
        coverage=InventoryCoverage.COMPLETE,
        entries=entries,
        inventory_fingerprint=inventory_fingerprint(entries),
    )
    await LangGraphEnforcementRepository(Database()).register_inventory(request)
    assert len(statements) == 1
    assert "ON CONFLICT" in statements[0]
    assert "DO UPDATE SET" in statements[0]


async def test_idempotency_key_conflict_is_rejected(sqlite_db) -> None:
    service, _, _, codec = await _service(sqlite_db)
    fingerprint = await _register(service, codec)
    await service.decide(_request(codec, fingerprint))
    changed = ActionDescriptorV1(
        name="lookup",
        fingerprint="sha256:tool",
        arguments={"q": "changed"},
        side_effect=SideEffectClass.READ_ONLY,
    )
    with pytest.raises(EnforcementBoundaryError) as exc:
        await service.decide(_request(codec, fingerprint, action=changed))
    assert exc.value.code == "zeroth.idempotency_conflict"


async def test_idempotency_cannot_reuse_allow_across_policy_or_principal(sqlite_db) -> None:
    service, _, _, codec = await _service(sqlite_db)
    fingerprint = await _register(service, codec)
    await service.decide(_request(codec, fingerprint))

    changed_policy = _request(codec, fingerprint).model_copy(
        update={
            "policy_version": "policy-v2",
            "context_token": _token(codec, policy="policy-v2"),
        }
    )
    with pytest.raises(EnforcementBoundaryError) as policy_conflict:
        await service.decide(changed_policy)
    assert policy_conflict.value.code == "zeroth.idempotency_conflict"

    changed_principal = _request(codec, fingerprint).model_copy(
        update={
            "principal_id": "user-2",
            "context_token": _token(codec, principal="user-2"),
        }
    )
    with pytest.raises(EnforcementBoundaryError) as principal_conflict:
        await service.decide(changed_principal)
    assert principal_conflict.value.code == "zeroth.idempotency_conflict"


async def test_rejected_credentials_persist_no_decision(sqlite_db) -> None:
    service, repository, _, codec = await _service(sqlite_db)
    fingerprint = await _register(service, codec)
    wrong_audience = _token(codec, audience="other-service")
    with pytest.raises(EnforcementBoundaryError):
        await service.decide(_request(codec, fingerprint, token=wrong_audience))
    assert await repository.count_decisions() == 0


async def test_dependency_outage_fails_closed(sqlite_db) -> None:
    service, _, _, codec = await _service(sqlite_db, budget=Budget(raises=True))
    fingerprint = await _register(service, codec)
    response = await service.decide(_request(codec, fingerprint))
    assert response.decision is ToolDecisionKind.DENY
    assert response.reason_code == "enforcement_unavailable"


async def test_enforcement_outage_cannot_replay_cached_allow(sqlite_db) -> None:
    service, _, metrics, codec = await _service(sqlite_db)
    fingerprint = await _register(service, codec)
    request = _request(codec, fingerprint, key="outage-after-allow")
    assert (await service.decide(request)).decision is ToolDecisionKind.ALLOW

    service.budget_checker = Budget(raises=True)
    with pytest.raises(EnforcementBoundaryError) as exc:
        await service.decide(request)
    assert exc.value.code == "zeroth.idempotency_conflict"
    assert (
        metrics.snapshot()["counters"][
            'zeroth_langgraph_enforcement_failures_total{code="enforcement_unavailable"}'
        ]
        == 1
    )


async def test_unknown_action_fails_closed(sqlite_db) -> None:
    service, _, _, codec = await _service(sqlite_db)
    fingerprint = await _register(service, codec)
    unknown = ActionDescriptorV1(name="missing", fingerprint="sha256:none")
    response = await service.decide(_request(codec, fingerprint, action=unknown))
    assert response.decision is ToolDecisionKind.DENY
    assert response.reason_code == "unknown_action"


async def test_decisions_and_idempotency_are_tenant_scoped(sqlite_db) -> None:
    service, repository, _, codec = await _service(sqlite_db)
    first_fingerprint = await _register(service, codec, tenant="tenant-a")
    second_fingerprint = await _register(service, codec, tenant="tenant-b")
    first = await service.decide(_request(codec, first_fingerprint, tenant="tenant-a"))
    second = await service.decide(_request(codec, second_fingerprint, tenant="tenant-b"))
    assert first.decision_id != second.decision_id
    assert await repository.count_decisions() == 2


async def test_decision_and_attestation_metrics(sqlite_db) -> None:
    service, _, metrics, codec = await _service(sqlite_db)
    fingerprint = await _register(service, codec)
    await service.decide(_request(codec, fingerprint))
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
    assert evidence.governance_level is GovernanceLevel.ENFORCED
    counters = metrics.snapshot()["counters"]
    assert any(key.startswith("zeroth_langgraph_decisions_total") for key in counters)
    assert any(key.startswith("zeroth_langgraph_attestations_total") for key in counters)


async def test_same_correlation_distinct_signed_runs_have_isolated_attestations(
    sqlite_db,
) -> None:
    service, repository, _, codec = await _service(sqlite_db)
    fingerprint = await _register(service, codec)

    async def attest(run_id: str):
        return await service.attest_run(
            RunAttestationV1(
                context_token=_token(codec, run_id=run_id),
                tenant_id="tenant-a",
                principal_id="user-1",
                deployment_ref="deployment-a",
                correlation_id="corr-1",
                graph_version="graph-v1",
                inventory_fingerprint=fingerprint,
                claimed_level=GovernanceLevel.ENFORCED,
            )
        )

    first = await attest("run-1")
    with pytest.warns(DeprecationWarning):
        sole_correlation = await repository.get_attestation(
            "tenant-a", "deployment-a", "corr-1"
        )
    second = await attest("run-2")

    assert first.run_id == "run-1"
    assert second.run_id == "run-2"
    assert sole_correlation is not None and sole_correlation["run_id"] == "run-1"
    assert (
        await repository.get_attestation_by_run_id("tenant-a", "deployment-a", "run-1")
    )[
        "correlation_id"
    ] == "corr-1"
    assert (
        await repository.get_attestation_by_run_id("tenant-a", "deployment-a", "run-2")
    )[
        "correlation_id"
    ] == "corr-1"
    with pytest.warns(DeprecationWarning):
        assert await repository.get_attestation("tenant-a", "deployment-a", "corr-1") is None

    with pytest.raises(EnforcementBoundaryError) as missing:
        await service.attest_run(
            RunAttestationV1(
                context_token=_token(codec, run_id=None),
                tenant_id="tenant-a",
                principal_id="user-1",
                deployment_ref="deployment-a",
                correlation_id="corr-1",
                graph_version="graph-v1",
                inventory_fingerprint=fingerprint,
            )
        )
    assert missing.value.code == "zeroth.invalid_context"


async def test_all_boundary_errors_increment_failure_metric_once(sqlite_db) -> None:
    service, _, metrics, codec = await _service(sqlite_db)
    entry = _entry()
    fingerprint = inventory_fingerprint((entry,))

    invalid_inventory = InventoryRegistrationV1(
        context_token=_token(codec),
        tenant_id="tenant-a",
        principal_id="user-1",
        deployment_ref="deployment-a",
        graph_version="graph-v1",
        coverage=InventoryCoverage.COMPLETE,
        entries=(entry,),
        inventory_fingerprint="sha256:invalid",
    )
    with pytest.raises(EnforcementBoundaryError) as invalid_inventory_error:
        await service.register_inventory(invalid_inventory)
    assert invalid_inventory_error.value.code == "zeroth.invalid_inventory"

    await _register(service, codec)
    with pytest.raises(EnforcementBoundaryError) as invalid_context_error:
        await service.decide(_request(codec, fingerprint, token="malformed"))
    assert invalid_context_error.value.code == "zeroth.invalid_context"

    request = _request(codec, fingerprint, key="metric-conflict")
    await service.decide(request)
    changed_action = request.action.model_copy(update={"arguments": {"q": "changed"}})
    with pytest.raises(EnforcementBoundaryError) as idempotency_error:
        await service.decide(request.model_copy(update={"action": changed_action}))
    assert idempotency_error.value.code == "zeroth.idempotency_conflict"

    attestation = RunAttestationV1(
        context_token=_token(codec),
        tenant_id="tenant-a",
        principal_id="user-1",
        deployment_ref="deployment-a",
        correlation_id="corr-1",
        graph_version="graph-v1",
        inventory_fingerprint=fingerprint,
        claimed_level=GovernanceLevel.OBSERVED,
    )
    await service.attest_run(attestation)
    with pytest.raises(EnforcementBoundaryError) as attestation_error:
        await service.attest_run(
            attestation.model_copy(update={"claimed_level": GovernanceLevel.ENFORCED})
        )
    assert attestation_error.value.code == "zeroth.attestation_conflict"

    with pytest.raises(EnforcementBoundaryError) as heartbeat_error:
        await service.heartbeat(
            HeartbeatV1(
                context_token=_token(codec),
                tenant_id="tenant-a",
                principal_id="user-1",
                deployment_ref="deployment-a",
                graph_version="graph-v1",
                inventory_fingerprint="sha256:missing",
            )
        )
    assert heartbeat_error.value.code == "zeroth.unknown_inventory"

    counters = metrics.snapshot()["counters"]
    assert {
        code: counters.get(f'zeroth_langgraph_enforcement_failures_total{{code="{code}"}}', 0)
        for code in (
            "attestation_conflict",
            "idempotency_conflict",
            "invalid_context",
            "invalid_inventory",
            "unknown_inventory",
        )
    } == {
        "attestation_conflict": 1,
        "idempotency_conflict": 1,
        "invalid_context": 1,
        "invalid_inventory": 1,
        "unknown_inventory": 1,
    }
    assert (
        counters.get(
            'zeroth_langgraph_enforcement_failures_total{code="attestation_storage_failed"}', 0
        )
        == 0
    )


@pytest.mark.parametrize(
    ("operation", "repository_method", "metric_code"),
    (
        ("register_inventory", "register_inventory", "inventory_storage_failed"),
        ("decide", "save_decision", "decision_storage_failed"),
        ("attest_run", "get_inventory", "attestation_storage_failed"),
        ("heartbeat", "heartbeat", "heartbeat_storage_failed"),
    ),
)
async def test_public_repository_failures_are_sanitized_and_metered_once(
    sqlite_db, monkeypatch, operation: str, repository_method: str, metric_code: str
) -> None:
    service, repository, metrics, codec = await _service(sqlite_db)
    entry = _entry()
    fingerprint = inventory_fingerprint((entry,))
    if operation != "register_inventory":
        await _register(service, codec)
    requests = {
        "register_inventory": InventoryRegistrationV1(
            context_token=_token(codec),
            tenant_id="tenant-a",
            principal_id="user-1",
            deployment_ref="deployment-a",
            graph_version="graph-v1",
            coverage=InventoryCoverage.COMPLETE,
            entries=(entry,),
            inventory_fingerprint=fingerprint,
        ),
        "decide": _request(codec, fingerprint, key="backend-failure"),
        "attest_run": RunAttestationV1(
            context_token=_token(codec),
            tenant_id="tenant-a",
            principal_id="user-1",
            deployment_ref="deployment-a",
            correlation_id="corr-1",
            graph_version="graph-v1",
            inventory_fingerprint=fingerprint,
        ),
        "heartbeat": HeartbeatV1(
            context_token=_token(codec),
            tenant_id="tenant-a",
            principal_id="user-1",
            deployment_ref="deployment-a",
            graph_version="graph-v1",
            inventory_fingerprint=fingerprint,
        ),
    }

    async def fail(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("private-db-host=secret")

    monkeypatch.setattr(repository, repository_method, fail)
    with pytest.raises(EnforcementBoundaryError) as caught:
        await getattr(service, operation)(requests[operation])

    assert caught.value.code == "zeroth.enforcement_unavailable"
    assert caught.value.status_code == 503
    assert caught.value.retryable is True
    assert "private-db-host" not in str(caught.value)
    counters = metrics.snapshot()["counters"]
    assert counters[f'zeroth_langgraph_enforcement_failures_total{{code="{metric_code}"}}'] == 1


async def test_enforcement_failures_increment_failure_metric(sqlite_db) -> None:
    service, repository, metrics, codec = await _service(sqlite_db, budget=Budget(raises=True))
    fingerprint = await _register(service, codec)
    response = await service.decide(_request(codec, fingerprint, key="metric-outage"))
    assert response.reason_code == "enforcement_unavailable"

    original_signer = service.signer

    class FailingSigner:
        def sign(self, payload):
            del payload
            raise RuntimeError("private signer detail")

        def key_id(self):
            return original_signer.key_id()

        def algorithm(self):
            return original_signer.algorithm()

    service.signer = FailingSigner()
    with pytest.raises(EnforcementBoundaryError) as signer_error:
        await service.attest_run(
            RunAttestationV1(
                context_token=_token(codec, correlation="corr-sign"),
                tenant_id="tenant-a",
                principal_id="user-1",
                deployment_ref="deployment-a",
                correlation_id="corr-sign",
                graph_version="graph-v1",
                inventory_fingerprint=fingerprint,
            )
        )
    assert signer_error.value.code == "zeroth.enforcement_unavailable"
    assert signer_error.value.retryable is True
    assert "private signer detail" not in str(signer_error.value)

    service.signer = original_signer
    original_save = repository.save_attestation

    async def fail_storage(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("private storage detail")

    repository.save_attestation = fail_storage
    with pytest.raises(EnforcementBoundaryError) as storage_error:
        await service.attest_run(
            RunAttestationV1(
                context_token=_token(codec, correlation="corr-store"),
                tenant_id="tenant-a",
                principal_id="user-1",
                deployment_ref="deployment-a",
                correlation_id="corr-store",
                graph_version="graph-v1",
                inventory_fingerprint=fingerprint,
            )
        )
    assert storage_error.value.code == "zeroth.enforcement_unavailable"
    assert storage_error.value.retryable is True
    assert "private storage detail" not in str(storage_error.value)
    repository.save_attestation = original_save

    counters = metrics.snapshot()["counters"]
    assert (
        counters['zeroth_langgraph_enforcement_failures_total{code="enforcement_unavailable"}'] == 1
    )
    assert (
        counters['zeroth_langgraph_enforcement_failures_total{code="attestation_signing_failed"}']
        == 1
    )
    assert (
        counters['zeroth_langgraph_enforcement_failures_total{code="attestation_storage_failed"}']
        == 1
    )
