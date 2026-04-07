"""Data models used throughout the audit system.

Defines the shapes of audit records, query filters, redaction rules,
and the timeline container. All models use Pydantic for validation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from zeroth.identity import ActorIdentity


def _utc_now() -> datetime:
    """Return the current time in UTC. Used as a default timestamp factory."""
    return datetime.now(UTC)


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
    """

    model_config = ConfigDict(extra="forbid")

    tool_ref: str
    alias: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    outcome: dict[str, Any] | None = None
    error: str | None = None


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
    occurred_at: datetime = Field(default_factory=_utc_now)


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
    tenant_id: str = "default"
    workspace_id: str | None = None
    attempt: int = 1
    status: str
    actor: ActorIdentity | None = None
    input_snapshot: dict[str, Any] = Field(default_factory=dict)
    output_snapshot: dict[str, Any] = Field(default_factory=dict)
    validation_results: dict[str, Any] = Field(default_factory=dict)
    execution_metadata: dict[str, Any] = Field(default_factory=dict)
    token_usage: TokenUsage | None = None
    cost_usd: float | None = None
    cost_event_id: str | None = None
    error: str | None = None
    condition_results: list[dict[str, Any]] = Field(default_factory=list)
    memory_interactions: list[MemoryAccessRecord] = Field(default_factory=list)
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    approval_actions: list[ApprovalActionRecord] = Field(default_factory=list)
    stdout: str | None = None
    stderr: str | None = None
    supersedes_audit_id: str | None = None
    previous_record_digest: str | None = None
    record_digest: str | None = None
    started_at: datetime = Field(default_factory=_utc_now)
    completed_at: datetime | None = None


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
