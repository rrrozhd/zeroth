"""The gateway-backed :class:`ToolDecisionClient` (ZER-8).

ZER-6 defined the seam and left everything behind it open
(:mod:`zeroth.integrations.langgraph._tool_decisions`). This is what fills it:
a client that asks the service's ``/v1/enforcement/decisions`` route for the
verdict on one already-normalized tool call.

**It is deliberately not defensive.** ``resolve_tool_decision`` already converts
every way this client can fail -- a raise, a ``None``, an unrecognized verdict --
into a denial recorded as ``policy_unavailable``. So the one thing this module
must never do is *handle* a failure: a client that answered "allow" (or any
verdict at all) when it could not reach the policy source would be manufacturing
a decision nobody made, and the seam above would pass it on as genuine. Transport
errors and error statuses therefore propagate untouched. Failing closed is the
seam's job, and it can only do that job if this module tells it the truth.

**It blocks.** The seam is synchronous by construction (see that module's
docstring: ``on_tool_start`` cannot await), so a network-backed client bridges on
its own side, and here that means a blocking request on the calling thread. In
an async graph the call runs inside a coroutine and holds the event loop for the
duration -- which is precisely why the wait is bounded by
:data:`DEFAULT_DECISION_TIMEOUT_SECONDS` rather than left to the transport's
default, and why that bound is a constructor argument an operator can lower.

The arguments themselves never leave the process: the wire carries
``arguments_digest``, computed by the same
:func:`~zeroth.integrations.langgraph._tool_normalize.argument_fingerprint` the
rest of the integration uses, so a decision made here is reproducible against
one made anywhere else.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx

from zeroth.governance.decisions.request import DecisionResponse, NormalizedAction
from zeroth.governance.enforcement_wire import DecisionSubmission
from zeroth.integrations.langgraph._tool_normalize import argument_fingerprint
from zeroth.integrations.langgraph._tool_types import (
    ToolAction,
    ToolDecision,
    ToolDecisionKind,
    ToolGovernanceContext,
)

DEFAULT_DECISION_TIMEOUT_SECONDS = 5.0
"""How long a governed tool call may wait for its verdict.

Bounded rather than generous on purpose: this wait is on the graph's own thread,
so an unbounded one turns a slow decision service into a hung run. Exceeding it
raises, which the seam records as ``policy_unavailable`` -- a denial. The number
is therefore a statement about how long a run should stall before being refused,
not a network tuning knob.
"""

DECISION_PATH = "/v1/enforcement/decisions"


class HttpToolDecisionClient:
    """Ask the Zeroth service for the verdict on one tool call.

    The client owns no policy logic. It normalizes nothing (its input is
    already normalized), decides nothing, and repairs nothing: it serializes
    the action, sends it, and returns what came back.
    """

    def __init__(
        self,
        *,
        base_url: str,
        deployment_ref: str,
        api_key: str | None = None,
        timeout: float = DEFAULT_DECISION_TIMEOUT_SECONDS,
        policy_bindings: tuple[str, ...] = (),
        client: httpx.Client | None = None,
    ) -> None:
        """Bind the client to one service and deployment.

        Args:
            base_url: Origin of the Zeroth service, e.g. ``https://zeroth.internal``.
            deployment_ref: The deployment the calling graph runs as. The
                service scopes the decision to it and refuses one the caller's
                credential does not reach.
            api_key: Credential presented as ``X-API-Key``. The tenant and
                principal a decision is attributed to are taken by the service
                from this credential, never from the body -- a client cannot
                name the tenant it wants to be judged as.
            timeout: Seconds one decision may take, defaulting to
                :data:`DEFAULT_DECISION_TIMEOUT_SECONDS`.
            policy_bindings: Extra policy refs to evaluate this call against.
            client: A pre-built, pooled ``httpx.Client``. Supplying one is the
                normal production path: a client per decision would pay a fresh
                TCP and TLS handshake on every governed tool call.
        """
        self._deployment_ref = deployment_ref
        self._timeout = timeout
        self._policy_bindings = policy_bindings
        self._owns_client = client is None
        self._client = client if client is not None else httpx.Client(base_url=base_url)
        self._headers = {"X-API-Key": api_key} if api_key else {}

    def decide(self, action: ToolAction, context: ToolGovernanceContext) -> ToolDecision:
        """Return the service's verdict for *action*.

        Args:
            action: The normalized action to be judged.
            context: The governance context the call is attributed to. Only the
                run identity is read here; the tenant and principal a decision
                is recorded under come from the credential, server-side.

        Returns:
            The verdict, with ``require_approval`` preserved as its own kind.

        Raises:
            httpx.HTTPError: The service could not be reached, or answered with
                an error status. Never converted to a verdict here -- see the
                module docstring.
            ValueError: The response was not a decision. Also not converted:
                a body this client cannot understand is not an allow.
        """
        submission = DecisionSubmission(
            deployment_ref=self._deployment_ref,
            action=NormalizedAction(
                name=action.identity.name,
                fingerprint=action.identity.fingerprint,
                arguments_digest=argument_fingerprint(action.arguments),
                contract_ref=action.contract_ref,
                side_effect=action.side_effect.value,
            ),
            # One key per attempt, minted here rather than derived from the
            # action: a transport retry of *this* call reuses it and cannot
            # double-record, while an identical tool call made again later is a
            # new attempt that policy must be free to refuse. Deriving the key
            # from the action content would silently answer the second call
            # from the first call's verdict, past every rate and budget rule.
            idempotency_key=f"{context.run_id}:{uuid.uuid4()}",
            policy_bindings=self._policy_bindings,
        )
        response = self._client.post(
            DECISION_PATH,
            json=submission.model_dump(mode="json"),
            headers=self._headers,
            timeout=self._timeout,
        )
        response.raise_for_status()
        decided = DecisionResponse.model_validate(response.json())
        return ToolDecision(
            kind=ToolDecisionKind(decided.kind.value),
            reason_code=decided.reason_code,
            approval_ref=decided.approval_ref,
        )

    def close(self) -> None:
        """Close the transport, but only if this client opened it.

        A caller that supplied a pooled client is still its owner; closing it
        here would shut a connection pool other callers are using.
        """
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> HttpToolDecisionClient:
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()
