"""Policy and capability models.

This module defines the data structures used by the policy system:
what capabilities exist, what a policy looks like, and what the result
of a policy check contains.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Capability(StrEnum):
    """A specific permission that a node might need to do its job.

    Each value represents one kind of action (like reading from the network
    or writing to the filesystem).  Policies use these to control what
    nodes are allowed to do.
    """

    NETWORK_READ = "network_read"
    NETWORK_WRITE = "network_write"
    FILESYSTEM_READ = "filesystem_read"
    FILESYSTEM_WRITE = "filesystem_write"
    SECRET_ACCESS = "secret_access"
    EXTERNAL_API_CALL = "external_api_call"
    PROCESS_SPAWN = "process_spawn"
    MEMORY_READ = "memory_read"
    MEMORY_WRITE = "memory_write"


class PolicyDecision(StrEnum):
    """The outcome of a policy evaluation: either allow or deny."""

    ALLOW = "allow"
    DENY = "deny"


class PolicyDefinition(BaseModel):
    """A named set of rules that controls what capabilities are allowed or denied.

    You create one of these to describe what a particular policy permits.
    For example, a "read-only" policy might allow NETWORK_READ but deny
    NETWORK_WRITE.  It can also control which secrets are visible and how
    strict the sandbox should be.
    """

    model_config = ConfigDict(extra="forbid")

    policy_id: str
    allowed_capabilities: list[Capability] = Field(default_factory=list)
    denied_capabilities: list[Capability] = Field(default_factory=list)
    allowed_secrets: list[str] = Field(default_factory=list)
    network_mode: str | None = None
    approval_required_for_side_effects: bool = False
    timeout_override_seconds: float | None = None
    sandbox_strictness_mode: str | None = None


class EnforcementResult(BaseModel):
    """The result of checking policies before a node runs.

    Contains the decision (allow/deny), an optional reason if denied,
    and the effective set of capabilities and constraints that apply
    to the node's execution.
    """

    model_config = ConfigDict(extra="forbid")

    decision: PolicyDecision
    reason: str | None = None
    effective_capabilities: set[Capability] = Field(default_factory=set)
    allowed_secrets: list[str] = Field(default_factory=list)
    network_mode: str | None = None
    approval_required_for_side_effects: bool = False
    timeout_override_seconds: float | None = None
    sandbox_strictness_mode: str | None = None
