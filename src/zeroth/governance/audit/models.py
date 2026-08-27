"""Data models used throughout the audit system.

Defines the shapes of audit records, query filters, redaction rules,
and the timeline container. All models use Pydantic for validation.
"""

from __future__ import annotations

import inspect
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from zeroth.governance.identity import ActorIdentity
from zeroth.platform.measurement import MeasurementState
from zeroth.platform.primitives import utc_now


class AuditRedactionConfig(BaseModel):
    """Rules that control which parts of audit payloads get hidden or removed.

    Use this to protect sensitive data (like API keys or passwords) from
    appearing in audit logs. You can redact specific dictionary keys or
    omit entire nested paths.
    """

    model_config = ConfigDict(extra="forbid")

    redact_keys: set[str] = Field(default_factory=set)
    omit_paths: set[tuple[str, ...]] = Field(default_factory=set)


class ToolCallRecord(BaseModel):
    """A record of a single tool call made during a node execution.

    Captures which tool was called, what arguments were passed in,
    what the tool returned, and whether it produced an error.

    The ``operation_*`` fields carry the durable receipt outcome for guarded
    executable tools. MCP calls populate only the support and residual-risk
    marker because they bypass the operation boundary entirely.
    """

    model_config = ConfigDict(extra="forbid")

    tool_ref: str
    alias: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    outcome: dict[str, Any] | None = None
    error: str | None = None
    tool_call_id: str | None = None
    operation_key: str | None = None
    operation_target_ref: str | None = None
    operation_support: str | None = None
    operation_state: str | None = None
    operation_first_execution: bool | None = None
    operation_replay_suppressed: bool | None = None
    operation_reconciliation_required: bool | None = None
    operation_reconciliation_exhausted: bool | None = None
    operation_residual_duplicate_risk: bool | None = None


# ``ToolCallRecord``'s constructor signature is pinned by the protected surface
# fixtures, so the ZER-26 marker fields are hidden from the reported signature
# rather than recorded as a surface change — the same idiom ``ErasureResult``
# and ``ZerothSettings`` use. Both remain ordinary keyword arguments.
_tool_call_record_parameters = inspect.signature(ToolCallRecord).parameters
ToolCallRecord.__signature__ = inspect.signature(ToolCallRecord).replace(
    parameters=[
        parameter
        for name, parameter in _tool_call_record_parameters.items()
        if name
        not in {
            "operation_key",
            "tool_call_id",
            "operation_target_ref",
            "operation_support",
            "operation_state",
            "operation_first_execution",
            "operation_replay_suppressed",
            "operation_reconciliation_required",
            "operation_reconciliation_exhausted",
            "operation_residual_duplicate_risk",
        }
    ]
)


class MemoryAccessRecord(BaseModel):
    """A record of a single memory read or write during a node execution.

    Tracks which memory store was accessed, what operation was performed
    (e.g. read, write, delete), and the key/value involved.
    """

    model_config = ConfigDict(extra="forbid")

    memory_ref: str
    connector_type: str
    scope: str
    operation: str
    key: str
    value: Any | None = None


class ApprovalActionRecord(BaseModel):
    """A record of an approval-related action (e.g. requested, approved, denied).

    Used when a node requires human or system approval before proceeding.
    Tracks who took the action and when it happened.
    """

    model_config = ConfigDict(extra="forbid")

    approval_id: str
    action: str
    actor: ActorIdentity | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime = Field(default_factory=utc_now)


class TokenUsage(BaseModel):
    """Token consumption metrics from a single LLM provider call.

    Tracks input (prompt) tokens, output (completion) tokens, total tokens,
    and the model that produced them. Used by ProviderResponse and
    NodeAuditRecord for cost attribution and budget enforcement.
    """

    model_config = ConfigDict(extra="forbid")

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    model_name: str = ""


class NodeAuditRecord(BaseModel):
    """The main audit record for a single node execution.

    This is the core audit object. It captures everything that happened
    when a node ran: inputs, outputs, tool calls, memory accesses,
    approval actions, validation results, timing, and any errors.
    """

    model_config = ConfigDict(extra="forbid")

    audit_id: str
    run_id: str
    thread_id: str | None = None
    node_id: str
    node_version: int = 1
    graph_version_ref: str
    deployment_ref: str
    tenant_id: str
    workspace_id: str | None = None
    # First-class campaign correlation survives metadata-only capture. Provider
    # request identities deliberately remain outside this free-form model.
    campaign_id: str | None = None
    attempt: int = 1
    status: str
    actor: ActorIdentity | None = None
    input_snapshot: dict[str, Any] = Field(default_factory=dict)
    output_snapshot: dict[str, Any] = Field(default_factory=dict)
    validation_results: dict[str, Any] = Field(default_factory=dict)
    execution_metadata: dict[str, Any] = Field(default_factory=dict)
    token_usage: TokenUsage | None = None
    cost_usd: float | None = None
    estimated_cost_usd: float | None = None
    cost_measurement: MeasurementState | None = None
    cost_event_id: str | None = None
    error: str | None = None
    condition_results: list[dict[str, Any]] = Field(default_factory=list)
    memory_interactions: list[MemoryAccessRecord] = Field(default_factory=list)
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    approval_actions: list[ApprovalActionRecord] = Field(default_factory=list)
    stdout: str | None = None
    stderr: str | None = None
    supersedes_audit_id: str | None = None
    # Allocated transactionally from the per-run database coordination row.
    # Nullable keeps pre-migration audit payloads readable.
    chain_sequence: int | None = None
    previous_record_digest: str | None = None
    record_digest: str | None = None
    # WS-D keyed signature over ``record_digest``. Persisted inside record_json
    # (no DDL). EXCLUDED from the digest itself (see verifier._compute_record_digest)
    # so the digest is byte-identical whether or not a record is signed. Nullable
    # so legacy records verify as unsigned-legacy, not signed-invalid.
    record_signature: str | None = None
    signing_key_id: str | None = None
    signing_algorithm: str | None = None
    # WS-E retention/crypto-erasure. All five fields are EXCLUDED from the digest
    # (see verifier._DIGEST_EXCLUDED_FIELDS) so that: (a) pre-WS-E rows, which
    # lack these keys entirely, recompute byte-identically once pydantic fills
    # the defaults, and (b) crypto-erasure — which nulls the PII payload fields
    # but leaves ``pii_commitments`` and ``record_digest`` untouched — does not
    # change the digest, so the append-only hash-chain still verifies.
    #
    # ``digest_version``: 1 = legacy whole-payload SHA-256 (grandfathered,
    # un-erasable); 2 = original commitment digest; 3 = expanded commitments
    # covering condition and approval structured payloads as well.
    erased: bool = False
    erased_at: datetime | None = None
    erasure_reason: str | None = None  # ttl | rte | manual
    digest_version: int = 1
    pii_commitments: dict[str, str] | None = None
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None

    @model_validator(mode="after")
    def _ensure_completion_not_before_start(self) -> NodeAuditRecord:
        # completed_at is often set from now() at call time while started_at
        # falls back to its default factory a few microseconds later, which can
        # render as "completed before started". Clamp so the invariant holds.
        if self.completed_at is not None and self.completed_at < self.started_at:
            self.completed_at = self.started_at
        if self.cost_measurement is None:
            self.cost_measurement = (
                MeasurementState.MEASURED
                if self.cost_usd is not None
                else MeasurementState.ESTIMATED
                if self.estimated_cost_usd is not None
                else MeasurementState.UNMEASURED
            )
        return self


NodeAuditRecord.__signature__ = inspect.signature(NodeAuditRecord).replace(
    parameters=[
        parameter
        for name, parameter in inspect.signature(NodeAuditRecord).parameters.items()
        if name not in {"campaign_id", "estimated_cost_usd", "cost_measurement"}
    ]
)


class AuditQuery(BaseModel):
    """Filters for searching audit records.

    Set one or more fields to narrow down which audit records you want.
    Leave a field as None to not filter on it. For example, set run_id
    to retrieve all audit records from a specific run.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str | None = None
    thread_id: str | None = None
    node_id: str | None = None
    graph_version_ref: str | None = None
    deployment_ref: str | None = None
    # WS-B: filter audit records to a single tenant. None = no tenant filter.
    tenant_id: str | None = None
    workspace_id: str | None = None
    workspace_scoped: bool = False


class AuditTimeline(BaseModel):
    """A time-ordered list of audit records for a single run or scope.

    Think of this as a "replay log" -- it shows you exactly what happened
    and in what order, making it easy to trace through a run step by step.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str | None = None
    entries: list[NodeAuditRecord] = Field(default_factory=list)


class AuditContinuityReport(BaseModel):
    """Verification result for a run or deployment audit chain."""

    model_config = ConfigDict(extra="forbid")

    scope: str
    verified: bool
    record_count: int = 0
    failed_audit_id: str | None = None
    error: str | None = None
    # WS-D three-state signature result, independent of the ``verified`` digest
    # axis: True = every signed record's signature checks out; False = a signed
    # record failed verification; None = unsigned-legacy (no records were signed).
    signature_verified: bool | None = None
    # How many records in scope carried no signature (unsigned-legacy).
    unsigned_record_count: int = 0
