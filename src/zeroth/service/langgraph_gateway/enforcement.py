"""Persistent LangGraph tool decisions and server-verified run evidence."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from functools import wraps
from types import SimpleNamespace
from typing import Any, Protocol
from uuid import uuid4

from zeroth.contracts.langgraph_gateway.models import GovernanceLevel, RunCapabilityEvidence
from zeroth.governance.policy import PolicyDecision, PolicyGuard
from zeroth.integrations.langgraph._tool_types import (
    InventoryCoverage,
    SideEffectClass,
    ToolDecisionKind,
)
from zeroth.integrations.langgraph.enforcement_protocol import (
    ADAPTER_PROTOCOL_VERSION,
    ActionDescriptorV1,
    DecisionRequestV1,
    DecisionResponseV1,
    HeartbeatV1,
    InventoryEntryV1,
    InventoryRegistrationV1,
    RunAttestationV1,
    _canonical,
    inventory_fingerprint,
)
from zeroth.platform.observability.metrics import MetricsCollector
from zeroth.platform.signing import SigningKeyProvider
from zeroth.platform.storage.json import from_json_value
from zeroth.service.langgraph_gateway.context import ReservedContextClaims, ReservedContextCodec


class BudgetChecker(Protocol):
    """Represent budget checker state and behavior."""

    async def check_budget_status(self, tenant_id: str) -> object:
        """Check budget status."""
        ...


class EnforcementBoundaryError(RuntimeError):
    """Safe stable boundary failure."""

    def __init__(self, code: str, *, status_code: int = 400, retryable: bool = False) -> None:
        """Initialize the component with its validated dependencies."""
        self.code = code
        self.status_code = status_code
        self.retryable = retryable
        super().__init__("LangGraph enforcement request rejected")


_BOUNDARY_METRIC_CODES = {
    "zeroth.attestation_conflict": "attestation_conflict",
    "zeroth.idempotency_conflict": "idempotency_conflict",
    "zeroth.invalid_context": "invalid_context",
    "zeroth.invalid_inventory": "invalid_inventory",
    "zeroth.unknown_inventory": "unknown_inventory",
}

_UNEXPECTED_FAILURE_METRIC_CODES = {
    "register_inventory": "inventory_storage_failed",
    "decide": "decision_storage_failed",
    "attest_run": "attestation_storage_failed",
    "heartbeat": "heartbeat_storage_failed",
}


class _EnforcementBackendError(RuntimeError):
    """Represent enforcement backend error state and behavior."""

    def __init__(self, metric_code: str) -> None:
        """Initialize the component with its validated dependencies."""
        self.metric_code = metric_code


def _raise_unavailable(service: LangGraphEnforcementService, metric_code: str) -> None:
    """Implement the raise unavailable boundary for this component."""
    service.metrics.increment("zeroth_langgraph_enforcement_failures_total", {"code": metric_code})
    raise EnforcementBoundaryError(
        "zeroth.enforcement_unavailable", status_code=503, retryable=True
    ) from None


def _meter_boundary_errors(operation: Callable[..., Any]) -> Callable[..., Any]:
    """Implement the meter boundary errors boundary for this component."""

    @wraps(operation)
    async def counted(self: LangGraphEnforcementService, *args: Any, **kwargs: Any) -> Any:
        """Implement the counted boundary for this component."""
        try:
            return await operation(self, *args, **kwargs)
        except EnforcementBoundaryError as exc:
            self.metrics.increment(
                "zeroth_langgraph_enforcement_failures_total",
                {"code": _BOUNDARY_METRIC_CODES.get(exc.code, "unknown_boundary_error")},
            )
            raise
        except _EnforcementBackendError as exc:
            _raise_unavailable(self, exc.metric_code)
        except Exception:
            _raise_unavailable(self, _UNEXPECTED_FAILURE_METRIC_CODES[operation.__name__])

    return counted


class LangGraphEnforcementService:
    """Fail-closed decision and evidence boundary for one deployment."""

    def __init__(
        self,
        repository: LangGraphEnforcementRepository,
        *,
        codec: ReservedContextCodec,
        signer: SigningKeyProvider,
        policy_guard: PolicyGuard,
        budget_checker: BudgetChecker,
        metrics: MetricsCollector,
        deployment_ref: str,
        audience: str,
        expected_graph_version: str,
        policy_bindings: tuple[str, ...] = (),
        expected_adapter_version: str = ADAPTER_PROTOCOL_VERSION,
        expected_inventory_fingerprint: str | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        """Initialize the component with its validated dependencies."""
        self.repository = repository
        self.codec = codec
        self.signer = signer
        self.policy_guard = policy_guard
        self.budget_checker = budget_checker
        self.metrics = metrics
        self.deployment_ref = deployment_ref
        self.audience = audience
        self.expected_graph_version = expected_graph_version
        self.policy_bindings = policy_bindings
        self.expected_adapter_version = expected_adapter_version
        self.expected_inventory_fingerprint = expected_inventory_fingerprint
        self.now = now or (lambda: datetime.now(tz=UTC))
        self.deployment_evidence: RunCapabilityEvidence | None = None

    def _claims(
        self,
        token: str,
        *,
        tenant_id: str,
        principal_id: str,
        deployment_ref: str,
        policy_version: str | None = None,
        run_id: str | None = None,
    ) -> ReservedContextClaims:
        """Implement the claims boundary for this component."""
        try:
            claims = self.codec.decode(
                token, audience=self.audience, deployment_ref=self.deployment_ref
            )
            expected = (claims.tenant_id, claims.principal_id, claims.deployment_ref)
            if expected != (tenant_id, principal_id, deployment_ref):
                raise ValueError
            if policy_version is not None and claims.policy_version != policy_version:
                raise ValueError
            if run_id is not None and (claims.run_id or claims.correlation_id) != run_id:
                raise ValueError
            return claims
        except Exception:
            raise EnforcementBoundaryError("zeroth.invalid_context", status_code=401) from None

    @_meter_boundary_errors
    async def register_inventory(self, request: InventoryRegistrationV1) -> None:
        """Implement the register inventory boundary for this component."""
        self._claims(
            request.context_token,
            tenant_id=request.tenant_id,
            principal_id=request.principal_id,
            deployment_ref=request.deployment_ref,
        )
        if request.inventory_fingerprint != inventory_fingerprint(request.entries):
            raise EnforcementBoundaryError("zeroth.invalid_inventory")
        await self.repository.register_inventory(request)
        self.metrics.increment(
            "zeroth_langgraph_inventories_total", {"coverage": request.coverage.value}
        )

    @_meter_boundary_errors
    async def decide(self, request: DecisionRequestV1) -> DecisionResponseV1:
        """Implement the decide boundary for this component."""
        claims = self._claims(
            request.context_token,
            tenant_id=request.tenant_id,
            principal_id=request.principal_id,
            deployment_ref=request.deployment_ref,
            policy_version=request.policy_version,
            run_id=request.run_id,
        )
        if claims.correlation_id != request.correlation_id:
            raise EnforcementBoundaryError("zeroth.invalid_context", status_code=401)
        verdict, reason, policy_version = await self._evaluate(request, claims)
        action_hash = (
            "sha256:"
            + hashlib.sha256(
                _canonical(
                    {
                        "deployment_ref": request.deployment_ref,
                        "correlation_id": request.correlation_id,
                        "run_id": request.run_id,
                        "principal_id": request.principal_id,
                        "policy_version": request.policy_version,
                        "effective_policy_version": policy_version,
                        "decision": verdict.value,
                        "reason_code": reason,
                        "inventory_fingerprint": request.inventory_fingerprint,
                        "action": request.action.model_dump(mode="json"),
                    }
                )
            ).hexdigest()
        )
        decision_id = str(uuid4())
        response = DecisionResponseV1(
            decision_id=decision_id,
            idempotency_key=request.idempotency_key,
            decision=verdict,
            reason_code=reason,
            policy_version=policy_version,
            approval_ref=(decision_id if verdict is ToolDecisionKind.REQUIRE_APPROVAL else None),
        )
        stored = await self.repository.save_decision(
            request.idempotency_key,
            request.deployment_ref,
            action_hash,
            response,
        )
        self.metrics.increment(
            "zeroth_langgraph_decisions_total", {"decision": stored.decision.value}
        )
        return stored

    async def _evaluate(
        self, request: DecisionRequestV1, claims: ReservedContextClaims
    ) -> tuple[ToolDecisionKind, str, str]:
        """Implement the evaluate boundary for this component."""
        inventory = await self.repository.get_inventory(
            request.deployment_ref,
            self.expected_graph_version,
            self.expected_adapter_version,
            request.inventory_fingerprint,
        )
        entry = _matching_entry(inventory, request.action)
        if entry is None or request.action.side_effect is SideEffectClass.UNKNOWN:
            return ToolDecisionKind.DENY, "unknown_action", request.policy_version
        try:
            admission = self.policy_guard.evaluate_run_admission(
                SimpleNamespace(
                    tenant_id=request.tenant_id,
                    principal_id=request.principal_id,
                    roles=claims.roles,
                    deployment_ref=request.deployment_ref,
                    assistant_id=None,
                    input_classification=claims.content_classification or "unknown",
                    input_size_bytes=len(_canonical(request.action.arguments)),
                    policy_bindings=self.policy_bindings,
                )
            )
            if admission.policy_version != request.policy_version:
                return (
                    ToolDecisionKind.DENY,
                    "policy_version_mismatch",
                    admission.policy_version,
                )
            if not admission.allowed:
                return ToolDecisionKind.DENY, "policy_denied", admission.policy_version
            enforcement = self.policy_guard.evaluate(
                SimpleNamespace(policy_bindings=self.policy_bindings),
                SimpleNamespace(
                    policy_bindings=(), capability_bindings=request.action.capability_refs
                ),
                None,
                request.action.arguments,
            )
            if enforcement.decision is not PolicyDecision.ALLOW:
                return ToolDecisionKind.DENY, "capability_denied", admission.policy_version
            budget = await self.budget_checker.check_budget_status(request.tenant_id)
            if not getattr(budget, "allowed", False) or getattr(budget, "degraded", True):
                return ToolDecisionKind.DENY, "budget_denied", admission.policy_version
            if entry["requires_approval"] or (
                request.action.side_effect is SideEffectClass.SIDE_EFFECTING
                and enforcement.approval_required_for_side_effects
            ):
                if request.action.tool_call_id is None:
                    return (
                        ToolDecisionKind.DENY,
                        "approval_requires_tool_call_id",
                        admission.policy_version,
                    )
                return (
                    ToolDecisionKind.REQUIRE_APPROVAL,
                    "approval_required",
                    admission.policy_version,
                )
            return ToolDecisionKind.ALLOW, "allowed", admission.policy_version
        except Exception:
            self.metrics.increment(
                "zeroth_langgraph_enforcement_failures_total",
                {"code": "enforcement_unavailable"},
            )
            return ToolDecisionKind.DENY, "enforcement_unavailable", request.policy_version

    @_meter_boundary_errors
    async def attest_run(self, request: RunAttestationV1) -> RunCapabilityEvidence:
        """Implement the attest run boundary for this component."""
        claims = self._claims(
            request.context_token,
            tenant_id=request.tenant_id,
            principal_id=request.principal_id,
            deployment_ref=request.deployment_ref,
        )
        if claims.correlation_id != request.correlation_id:
            raise EnforcementBoundaryError("zeroth.invalid_context", status_code=401)
        if claims.run_id is None:
            raise EnforcementBoundaryError("zeroth.invalid_context", status_code=401)
        run_id = claims.run_id
        inventory = await self.repository.get_inventory(
            request.deployment_ref,
            request.graph_version,
            request.adapter_version,
            request.inventory_fingerprint,
        )
        complete = self._inventory_complete(inventory, request.inventory_fingerprint)
        versions_match = (
            request.graph_version == self.expected_graph_version
            and request.adapter_version == self.expected_adapter_version
        )
        level = GovernanceLevel.ADMISSION
        if versions_match:
            if request.claimed_level is GovernanceLevel.OBSERVED:
                level = GovernanceLevel.OBSERVED
            elif request.claimed_level is GovernanceLevel.ENFORCED:
                level = GovernanceLevel.ENFORCED if complete else GovernanceLevel.OBSERVED
        payload = {
            "tenant_id": request.tenant_id,
            "deployment_ref": request.deployment_ref,
            "correlation_id": request.correlation_id,
            "run_id": run_id,
            "graph_version": request.graph_version,
            "adapter_version": request.adapter_version,
            "inventory_fingerprint": request.inventory_fingerprint,
            "claimed_level": request.claimed_level.value,
            "governance_level": level.value,
            "tool_manifest_complete": complete,
            "observed_at": self.now().isoformat(),
        }
        try:
            signature = self.signer.sign(_canonical(payload))
            key_id = self.signer.key_id()
            algorithm = self.signer.algorithm()
        except Exception:
            raise _EnforcementBackendError("attestation_signing_failed") from None
        try:
            await self.repository.save_attestation(payload, signature, key_id, algorithm)
        except EnforcementBoundaryError:
            raise
        except Exception:
            raise _EnforcementBackendError("attestation_storage_failed") from None
        self.metrics.increment("zeroth_langgraph_attestations_total", {"level": level.value})
        return await StoredCapabilityEvidenceProvider(
            self.repository,
            self.signer,
            tenant_id=request.tenant_id,
            deployment_ref=request.deployment_ref,
        ).evidence_for_governance_run(run_id)  # type: ignore[return-value]

    @_meter_boundary_errors
    async def heartbeat(self, request: HeartbeatV1) -> None:
        """Implement the heartbeat boundary for this component."""
        self._claims(
            request.context_token,
            tenant_id=request.tenant_id,
            principal_id=request.principal_id,
            deployment_ref=request.deployment_ref,
        )
        coverage = await self.repository.heartbeat(request)
        if coverage is None:
            raise EnforcementBoundaryError("zeroth.unknown_inventory", status_code=404)
        complete = self._inventory_complete({"coverage": coverage}, request.inventory_fingerprint)
        versions_match = (
            request.graph_version == self.expected_graph_version
            and request.adapter_version == self.expected_adapter_version
        )
        level = GovernanceLevel.ADMISSION
        if versions_match:
            level = GovernanceLevel.ENFORCED if complete else GovernanceLevel.OBSERVED
        observed_at = self.now()
        evidence_payload = {
            "correlation_id": f"deployment:{request.deployment_ref}",
            "governance_level": level.value,
            "observed_at": observed_at.isoformat(),
            "graph_version": request.graph_version,
            "adapter_version": request.adapter_version,
            "inventory_fingerprint": request.inventory_fingerprint,
            "tool_manifest_complete": complete,
        }
        try:
            signature = self.signer.sign(_canonical(evidence_payload))
            signature_valid = (
                self.signer.verify(_canonical(evidence_payload), signature, self.signer.key_id())
                is True
            )
        except Exception:
            signature_valid = False
        self.deployment_evidence = RunCapabilityEvidence(
            **evidence_payload,
            signature_valid=signature_valid,
        )
        self.metrics.increment("zeroth_langgraph_heartbeats_total")

    def _inventory_complete(self, inventory: Mapping[str, Any] | None, fingerprint: str) -> bool:
        """Implement the inventory complete boundary for this component."""
        return bool(
            inventory
            and inventory["coverage"] == InventoryCoverage.COMPLETE.value
            and self.expected_inventory_fingerprint is not None
            and fingerprint == self.expected_inventory_fingerprint
        )


def _matching_entry(
    inventory: Mapping[str, Any] | None, action: ActionDescriptorV1
) -> dict[str, Any] | None:
    """Implement the matching entry boundary for this component."""
    if inventory is None:
        return None
    for entry in from_json_value(inventory["entries_json"]):
        if entry["name"] == action.name and entry["fingerprint"] == action.fingerprint:
            expected = ActionDescriptorV1(
                **(
                    entry
                    | {
                        "arguments": action.arguments,
                        "tool_call_id": action.tool_call_id,
                    }
                )
            )
            if expected == action:
                return entry
    return None


from zeroth.service.langgraph_gateway.enforcement_store import (  # noqa: E402  # isort: skip
    LangGraphEnforcementRepository,
    StoredCapabilityEvidenceProvider,
)


__all__ = [
    "ADAPTER_PROTOCOL_VERSION",
    "ActionDescriptorV1",
    "DecisionRequestV1",
    "DecisionResponseV1",
    "EnforcementBoundaryError",
    "HeartbeatV1",
    "InventoryEntryV1",
    "InventoryRegistrationV1",
    "LangGraphEnforcementRepository",
    "LangGraphEnforcementService",
    "RunAttestationV1",
    "StoredCapabilityEvidenceProvider",
    "inventory_fingerprint",
]
