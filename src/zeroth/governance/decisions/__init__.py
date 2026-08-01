"""The policy decision service behind tool enforcement (ZER-8).

The SDK owns a synchronous ``ToolDecisionClient`` seam
(``zeroth.integrations.langgraph._tool_decisions``) whose default implementation
denies every call because no policy source is wired to it. This package is the
policy source: a versioned request/response pair, idempotent tenant-scoped
persistence, and a service that fails closed at every gate. The transport that
exposes it lives elsewhere -- nothing here opens a connection or knows a
protocol exists.
"""

from zeroth.governance.decisions.repository import (
    DecisionRepository,
    IdempotencyConflictError,
)
from zeroth.governance.decisions.request import (
    DIGEST_EXCLUDED_FIELDS,
    DecisionKind,
    DecisionRequest,
    DecisionResponse,
    DecisionVerdict,
    NormalizedAction,
    SideEffect,
    StoredDecision,
    request_digest,
)
from zeroth.governance.decisions.service import (
    ApprovalGate,
    NoApprovalRequired,
    ToolDecisionService,
)

__all__ = [
    "DIGEST_EXCLUDED_FIELDS",
    "ApprovalGate",
    "DecisionKind",
    "DecisionRepository",
    "DecisionRequest",
    "DecisionResponse",
    "DecisionVerdict",
    "IdempotencyConflictError",
    "NoApprovalRequired",
    "NormalizedAction",
    "SideEffect",
    "StoredDecision",
    "ToolDecisionService",
    "request_digest",
]
