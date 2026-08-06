"""Synchronous fail-closed client for the Zeroth LangGraph enforcement API."""

from __future__ import annotations

import hashlib
import json
import math
import threading
from collections.abc import Callable
from contextlib import suppress
from typing import Any

import httpx

from zeroth.contracts.langgraph_gateway.models import GovernanceLevel, RunCapabilityEvidence
from zeroth.integrations.langgraph._correlation import (
    current_reserved_context_token,
    governance_run_id_from_token,
    request_identity_from_token,
)
from zeroth.integrations.langgraph._tool_types import (
    InventoryCoverage,
    ToolAction,
    ToolDecision,
    ToolDecisionKind,
    ToolGovernanceContext,
    ToolInventory,
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
    inventory_fingerprint,
)


class LangGraphGatewayError(RuntimeError):
    """Safe failure raised when run evidence cannot reach the gateway."""

    def __init__(self) -> None:
        super().__init__("LangGraph gateway enforcement is unavailable")


class LangGraphGatewayClient:
    """Register inventory, attest run start, and decide normalized tool calls."""

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str,
        tenant_id: str,
        principal_id: str,
        deployment_ref: str,
        policy_version: str,
        graph_version: str,
        inventory: ToolInventory,
        adapter_version: str = ADAPTER_PROTOCOL_VERSION,
        timeout: float = 5.0,
        heartbeat_interval_seconds: float | None = 30.0,
        transport: httpx.BaseTransport | None = None,
        token_provider: Callable[[], str | None] = current_reserved_context_token,
    ) -> None:
        if type(inventory) is not ToolInventory:
            raise TypeError("inventory must be a ToolInventory")
        if heartbeat_interval_seconds is not None and (
            not math.isfinite(heartbeat_interval_seconds) or heartbeat_interval_seconds <= 0
        ):
            raise ValueError("heartbeat_interval_seconds must be positive and finite")
        self.tenant_id = tenant_id
        self.principal_id = principal_id
        self.deployment_ref = deployment_ref
        self.policy_version = policy_version
        self.graph_version = graph_version
        self.adapter_version = adapter_version
        self.inventory = inventory
        self._token_provider = token_provider
        self._heartbeat_interval_seconds = heartbeat_interval_seconds
        self._heartbeat_token: str | None = None
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None
        self._entries = tuple(
            InventoryEntryV1(
                name=entry.identity.name,
                fingerprint=entry.identity.fingerprint,
                side_effect=entry.side_effect,
                contract_ref=entry.contract_ref,
                capability_refs=tuple(entry.capability_refs),
                requires_approval=entry.requires_approval,
            )
            for entry in inventory.entries
        )
        self.inventory_fingerprint = inventory_fingerprint(self._entries)
        self._client = httpx.Client(
            base_url=base_url.rstrip("/") + "/",
            headers={"X-API-Key": api_key},
            timeout=timeout,
            transport=transport,
        )

    def close(self) -> None:
        """Close the owned HTTP client."""
        self._heartbeat_stop.set()
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=1.0)
        self._client.close()

    def register_inventory(self, context_token: str) -> None:
        """Persist this exact graph/adapter inventory."""
        principal_id, _policy_version = self._request_identity(context_token)
        body = InventoryRegistrationV1(
            context_token=context_token,
            tenant_id=self.tenant_id,
            principal_id=principal_id,
            deployment_ref=self.deployment_ref,
            graph_version=self.graph_version,
            adapter_version=self.adapter_version,
            coverage=self.inventory.coverage,
            entries=self._entries,
            inventory_fingerprint=self.inventory_fingerprint,
        )
        self._post("inventories", body.model_dump(mode="json"), expected_status=204)

    def attest_run(
        self,
        context_token: str,
        correlation_id: str,
        *,
        claimed_level: GovernanceLevel | None = None,
    ) -> RunCapabilityEvidence:
        """Request the server-authoritative run-start attestation."""
        principal_id, _policy_version = self._request_identity(context_token)
        claim = claimed_level or (
            GovernanceLevel.ENFORCED
            if self.inventory.coverage is InventoryCoverage.COMPLETE
            else GovernanceLevel.OBSERVED
        )
        body = RunAttestationV1(
            context_token=context_token,
            tenant_id=self.tenant_id,
            principal_id=principal_id,
            deployment_ref=self.deployment_ref,
            correlation_id=correlation_id,
            graph_version=self.graph_version,
            adapter_version=self.adapter_version,
            inventory_fingerprint=self.inventory_fingerprint,
            claimed_level=claim,
        )
        payload = self._post("attestations", body.model_dump(mode="json"))
        return RunCapabilityEvidence.model_validate(payload)

    def start_run(self, context_token: str, correlation_id: str) -> RunCapabilityEvidence:
        """Register inventory and attest before the wrapped graph can execute."""
        self.register_inventory(context_token)
        evidence = self.attest_run(context_token, correlation_id)
        self._heartbeat_token = context_token
        self._start_heartbeat_loop()
        return evidence

    def _start_heartbeat_loop(self) -> None:
        if self._heartbeat_interval_seconds is None or self._heartbeat_thread is not None:
            return
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            name="zeroth-langgraph-heartbeat",
            daemon=True,
        )
        self._heartbeat_thread.start()

    def _heartbeat_loop(self) -> None:
        interval = self._heartbeat_interval_seconds
        if interval is None:
            return
        while not self._heartbeat_stop.wait(interval):
            token = self._heartbeat_token
            if token is None:
                continue
            with suppress(LangGraphGatewayError):
                self.heartbeat(token)

    def heartbeat(self, context_token: str) -> None:
        """Refresh last-known adapter inventory freshness without minting run evidence."""
        principal_id, _policy_version = self._request_identity(context_token)
        body = HeartbeatV1(
            context_token=context_token,
            tenant_id=self.tenant_id,
            principal_id=principal_id,
            deployment_ref=self.deployment_ref,
            graph_version=self.graph_version,
            adapter_version=self.adapter_version,
            inventory_fingerprint=self.inventory_fingerprint,
        )
        self._post("heartbeat", body.model_dump(mode="json"), expected_status=204)

    def decide(self, action: ToolAction, context: ToolGovernanceContext) -> ToolDecision:
        """Return the gateway verdict, denying every local or transport failure."""
        try:
            if context.tenant_id != self.tenant_id:
                raise ValueError
            token = self._token_provider()
            if not token:
                raise ValueError
            principal_id, policy_version = self._request_identity(token)
            if context.principal_id != principal_id:
                raise ValueError
            governance_run_id = governance_run_id_from_token(token)
            if not governance_run_id:
                raise ValueError
            body = DecisionRequestV1(
                idempotency_key=self._idempotency_key(
                    action, context, governance_run_id, principal_id, policy_version
                ),
                context_token=token,
                tenant_id=self.tenant_id,
                principal_id=principal_id,
                deployment_ref=self.deployment_ref,
                correlation_id=context.correlation_id or "",
                run_id=governance_run_id,
                policy_version=policy_version,
                inventory_fingerprint=self.inventory_fingerprint,
                action=ActionDescriptorV1(
                    name=action.identity.name,
                    fingerprint=action.identity.fingerprint,
                    tool_call_id=action.tool_call_id,
                    arguments=dict(action.arguments),
                    side_effect=action.side_effect,
                    contract_ref=action.contract_ref,
                    capability_refs=tuple(action.capability_refs),
                    requires_approval=action.requires_approval,
                ),
            )
            response = DecisionResponseV1.model_validate(
                self._post("decisions", body.model_dump(mode="json"))
            )
            return ToolDecision(
                kind=response.decision,
                reason_code=response.reason_code,
                approval_ref=response.approval_ref,
            )
        except Exception:
            return ToolDecision(
                kind=ToolDecisionKind.DENY,
                reason_code="policy_unavailable",
            )

    def _post(
        self, endpoint: str, payload: dict[str, Any], *, expected_status: int = 200
    ) -> dict[str, Any]:
        try:
            response = self._client.post(
                f"v1/langgraph/deployments/{self.deployment_ref}/{endpoint}", json=payload
            )
            if response.status_code != expected_status:
                raise ValueError
            return {} if expected_status == 204 else response.json()
        except Exception:
            raise LangGraphGatewayError() from None

    def _idempotency_key(
        self,
        action: ToolAction,
        context: ToolGovernanceContext,
        governance_run_id: str,
        principal_id: str,
        policy_version: str,
    ) -> str:
        payload = {
            "tenant_id": self.tenant_id,
            "principal_id": principal_id,
            "deployment_ref": self.deployment_ref,
            "policy_version": policy_version,
            "run_id": governance_run_id,
            "correlation_id": context.correlation_id,
            "thread_id": context.thread_id,
            "tool_call_id": action.tool_call_id,
            "tool": action.identity.name,
            "fingerprint": action.identity.fingerprint,
            "arguments": dict(action.arguments),
        }
        encoded = json.dumps(
            payload, allow_nan=False, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        return "sha256:" + hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _request_identity(context_token: str) -> tuple[str, str]:
        identity = request_identity_from_token(context_token)
        if identity is None:
            raise LangGraphGatewayError()
        return identity


__all__ = ["LangGraphGatewayClient", "LangGraphGatewayError"]
