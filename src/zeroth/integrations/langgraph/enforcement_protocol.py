"""The LangGraph enforcement wire protocol, shared by the adapter and the gateway.

These are the versioned DTOs the SDK-side client and the server-side gateway
exchange, plus the canonical digest that fingerprints an inventory. They live in
``integrations`` rather than ``contracts`` for a measured reason: the models name
``SideEffectClass``, ``ToolDecisionKind`` and ``InventoryCoverage`` from
:mod:`zeroth.integrations.langgraph._tool_types`, and ``contracts`` may import only
``platform``. Housing them in ``contracts`` would have replaced one forbidden edge
with a worse one.

Keeping them here is what removes temporary exception E2 outright rather than
relocating it: :mod:`zeroth.integrations.langgraph._gateway_client` now reaches
them without leaving its own domain, and the gateway service reaches them across
the ``service`` -> ``integrations`` edge the policy already permits. What that
drives to zero is the count of *forbidden* edges and exceptions, not every edge:
the service still depends on this module, permitted. The client side creates no
edge at all, because it never leaves its own domain.

Restating the three enums here instead -- the trick
:mod:`zeroth.governance.decisions.request` uses -- was rejected: governance
restates because it has no legal alternative, whereas integrations importing
integrations is ordinary. Restating would have split the identity of a type that
travels the wire between client and server.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from zeroth.contracts.langgraph_gateway.models import GovernanceLevel
from zeroth.integrations.langgraph._tool_types import (
    InventoryCoverage,
    SideEffectClass,
    ToolDecisionKind,
)

ADAPTER_PROTOCOL_VERSION = "1"


class ActionDescriptorV1(BaseModel):
    """Canonical policy input for one tool call."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    fingerprint: str = Field(min_length=1)
    tool_call_id: str | None = Field(
        default=None, min_length=1, max_length=256, pattern=r"^\S(?:.*\S)?$"
    )
    arguments: dict[str, Any] = Field(default_factory=dict)
    side_effect: SideEffectClass = SideEffectClass.UNKNOWN
    contract_ref: str | None = None
    capability_refs: tuple[str, ...] = ()
    requires_approval: bool = False
    identity_configuration: tuple[str, ...] = ()


class DecisionRequestV1(BaseModel):
    """Authenticated, versioned decision request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    idempotency_key: str = Field(min_length=1)
    context_token: str = Field(min_length=1, repr=False)
    tenant_id: str = Field(min_length=1)
    principal_id: str = Field(min_length=1)
    deployment_ref: str = Field(min_length=1)
    correlation_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)
    inventory_fingerprint: str = Field(min_length=1)
    action: ActionDescriptorV1


class DecisionResponseV1(BaseModel):
    """Stable idempotent decision result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    decision_id: str
    idempotency_key: str
    decision: ToolDecisionKind
    reason_code: str
    policy_version: str
    approval_ref: str | None = None


class InventoryEntryV1(BaseModel):
    """Server-registered identity and contract facts for one tool."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    fingerprint: str = Field(min_length=1)
    side_effect: SideEffectClass = SideEffectClass.UNKNOWN
    contract_ref: str | None = None
    capability_refs: tuple[str, ...] = ()
    requires_approval: bool = False
    identity_configuration: tuple[str, ...] = ()


class InventoryRegistrationV1(BaseModel):
    """Versioned tool-inventory registration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    context_token: str = Field(min_length=1, repr=False)
    tenant_id: str = Field(min_length=1)
    principal_id: str = Field(min_length=1)
    deployment_ref: str = Field(min_length=1)
    graph_version: str = Field(min_length=1)
    adapter_version: str = Field(default=ADAPTER_PROTOCOL_VERSION, min_length=1)
    coverage: InventoryCoverage
    entries: tuple[InventoryEntryV1, ...]
    inventory_fingerprint: str = Field(min_length=1)


class RunAttestationV1(BaseModel):
    """Run-start claim authenticated by reserved context and signed by Zeroth."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    context_token: str = Field(min_length=1, repr=False)
    tenant_id: str = Field(min_length=1)
    principal_id: str = Field(min_length=1)
    deployment_ref: str = Field(min_length=1)
    correlation_id: str = Field(min_length=1)
    graph_version: str = Field(min_length=1)
    adapter_version: str = Field(default=ADAPTER_PROTOCOL_VERSION, min_length=1)
    inventory_fingerprint: str = Field(min_length=1)
    claimed_level: GovernanceLevel = GovernanceLevel.OBSERVED


class HeartbeatV1(BaseModel):
    """Authenticated liveness signal for one registered adapter inventory."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    context_token: str = Field(min_length=1, repr=False)
    tenant_id: str = Field(min_length=1)
    principal_id: str = Field(min_length=1)
    deployment_ref: str = Field(min_length=1)
    graph_version: str = Field(min_length=1)
    adapter_version: str = Field(default=ADAPTER_PROTOCOL_VERSION, min_length=1)
    inventory_fingerprint: str = Field(min_length=1)


def inventory_fingerprint(entries: tuple[InventoryEntryV1, ...]) -> str:
    """Return the canonical digest for a complete ordered inventory."""
    payload = [entry.model_dump(mode="json") for entry in sorted(entries, key=lambda e: e.name)]
    return "sha256:" + hashlib.sha256(_canonical(payload)).hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, allow_nan=False, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


__all__ = [
    "ADAPTER_PROTOCOL_VERSION",
    "ActionDescriptorV1",
    "DecisionRequestV1",
    "DecisionResponseV1",
    "HeartbeatV1",
    "InventoryEntryV1",
    "InventoryRegistrationV1",
    "RunAttestationV1",
    "inventory_fingerprint",
]
