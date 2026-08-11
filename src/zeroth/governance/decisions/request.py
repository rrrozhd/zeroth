"""The versioned wire types one tool-enforcement decision is asked and answered in.

Every model here is ``frozen`` and ``extra="forbid"``. Frozen because a request
is hashed into :func:`request_digest` and then compared against a stored digest:
a model somebody mutated between those two points would be a decision recorded
against material that no longer exists. ``extra="forbid"`` because an unmodelled
field would ride along the wire, reach no gate, and -- being unmodelled -- fall
outside the digest as well, which is precisely the smuggling channel the digest
exists to close.

**The request carries no tool arguments, only their digest.** The decision layer
never needs the arguments themselves to decide, and holding them would make this
service a second place a credential pasted into a tool call comes to rest.
``arguments_digest`` still distinguishes two calls to one tool, which is all the
idempotency contract requires.

**What ``request_digest`` covers, and why the exclusion set is one line.** The
digest spans the whole normalized request *except* ``idempotency_key``. That
single exclusion is what makes a replay recognisable -- the key is the thing
being replayed -- and everything else is covered so that reusing a key for a
different action is *detectable* rather than silently re-answered. Under-covering
it is a security bug, not a fidelity one: a field outside the digest is a field
an attacker can vary while holding a key that already earned an allow. The
exclusion set is therefore exported and pinned by
``tests/governance/test_decision_service.py`` rather than left implicit.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict

from zeroth.platform.storage.json import to_json_value

SideEffect = Literal["read_only", "side_effecting", "unknown"]
"""How consequential a tool call is, as far as the classifier could tell.

``unknown`` is a real member rather than a placeholder: a tool nobody classified
must stay distinguishable from one positively known to be read-only, because the
service refuses the former and may admit the latter. The literal mirrors
``zeroth.integrations.langgraph._tool_types.SideEffectClass`` by value; it is
restated rather than imported because governance may not depend on integrations.
"""

DIGEST_EXCLUDED_FIELDS: frozenset[str] = frozenset({"idempotency_key"})
"""The only request fields :func:`request_digest` does not cover.

Deliberately exported. Widening it is how a field silently stops binding a
decision to the action it was made for, so the set is asserted in the test suite
instead of living as an inline literal nobody reviews.
"""


class DecisionKind(StrEnum):
    """The verdicts the decision service can return for one attempted call.

    The values match
    ``zeroth.integrations.langgraph._tool_types.ToolDecisionKind`` exactly, so
    the SDK-side client can map a response onto its own verdict without a
    translation table. ``REQUIRE_APPROVAL`` is a verdict of its own rather than
    a flavour of deny: a call waiting on a human has not been refused, and
    collapsing the two would lose the distinction an auditor needs.
    """

    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


class NormalizedAction(BaseModel):
    """One tool call, already normalized, as a policy is written against it.

    The name alone is not an identity -- whoever bound the tool chose it and can
    reuse it for a different callable between runs -- so ``fingerprint`` is what
    makes a decision reproducible.

    Attributes:
        name: The tool's bound name, as the framework reports it.
        fingerprint: A stable digest of the tool's identifying material.
        arguments_digest: A digest of the call arguments. The arguments
            themselves are never carried.
        contract_ref: The tool contract this call claims to satisfy, when one
            is declared.
        side_effect: The classification, defaulting to the refusable ``unknown``
            rather than to anything the service would admit.
        capability_refs: Capabilities the action requires.
        requires_approval: Whether the tool explicitly requires a human approval.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    fingerprint: str
    arguments_digest: str
    contract_ref: str | None = None
    side_effect: SideEffect = "unknown"
    capability_refs: tuple[str, ...] = ()
    requires_approval: bool = False


class DecisionRequest(BaseModel):
    """One versioned ask for a verdict, attributed to a tenant and principal.

    Attributes:
        schema_version: The wire version. Pinned to ``1``; a future version is a
            different literal and therefore a different digest, so a v2 request
            can never replay a v1 decision.
        tenant_id: The tenant the call belongs to and the decision is scoped to.
        principal_id: The principal the call acts as.
        deployment_ref: The deployment the call was made from.
        action: The normalized tool call.
        idempotency_key: The caller's replay token, scoped per tenant.
        policy_bindings: The policy bindings the caller declares.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    tenant_id: str
    principal_id: str
    deployment_ref: str
    action: NormalizedAction
    idempotency_key: str
    policy_bindings: tuple[str, ...] = ()


class DecisionVerdict(BaseModel):
    """A decided but not yet persisted outcome.

    Split from :class:`DecisionResponse` because a verdict is what the evaluator
    produces and the identity (``decision_id``, ``issued_at``) is what the store
    assigns. Keeping them apart is what lets the repository re-serve a stored
    decision rather than mint a second identity for a replay.

    Attributes:
        kind: The verdict.
        reason_code: The registered audit term the verdict is recorded under.
        policy_version: The policy revision the verdict was reached against.
        approval_ref: The approval the call waits on, when ``kind`` is
            :attr:`DecisionKind.REQUIRE_APPROVAL`.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: DecisionKind
    reason_code: str
    policy_version: str
    approval_ref: str | None = None


class DecisionResponse(BaseModel):
    """One versioned verdict as it was recorded and is re-served.

    Attributes:
        schema_version: The wire version, pinned to ``1``.
        decision_id: The stored decision's identity. A replay returns the
            original, so two calls sharing an id shared a decision.
        kind: The verdict.
        reason_code: The registered audit term the verdict is recorded under.
        approval_ref: The approval the call waits on, when applicable.
        policy_version: The policy revision the verdict was reached against.
        tenant_id: The tenant the decision is scoped to.
        issued_at: When the decision was first recorded -- not when it was
            re-served, so a replay does not look like a fresh evaluation.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    decision_id: str
    kind: DecisionKind
    reason_code: str
    approval_ref: str | None = None
    policy_version: str
    tenant_id: str
    issued_at: datetime


class StoredDecision(BaseModel):
    """A recorded decision together with the request digest it was made for.

    The digest travels with the response because the idempotency contract is
    decided by comparing it: a caller that reads a stored decision without also
    reading what it was decided *for* cannot tell a replay from a key reused for
    another action.

    Attributes:
        response: The recorded decision.
        request_digest: The digest of the request that produced it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    response: DecisionResponse
    request_digest: str


def request_digest(request: DecisionRequest) -> str:
    """Digest the whole normalized request except its idempotency key.

    Canonical JSON (sorted keys, no whitespace) via
    :func:`~zeroth.platform.storage.json.to_json_value`, so two structurally
    identical requests digest identically regardless of field order on the wire.

    Args:
        request: The normalized request to digest.

    Returns:
        The ``sha256:``-prefixed hex digest binding tenant, principal,
        deployment, schema version, declared bindings and every field of the
        action.
    """
    payload = request.model_dump(mode="json", exclude=set(DIGEST_EXCLUDED_FIELDS))
    canonical = to_json_value(payload).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


__all__ = [
    "DIGEST_EXCLUDED_FIELDS",
    "DecisionKind",
    "DecisionRequest",
    "DecisionResponse",
    "DecisionVerdict",
    "NormalizedAction",
    "SideEffect",
    "StoredDecision",
    "request_digest",
]
