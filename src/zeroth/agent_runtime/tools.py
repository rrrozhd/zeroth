"""Tool attachment system for the agent runtime.

Tools are external functions that an agent can call during execution (like
searching a database or sending an email). This module handles declaring
which tools an agent is allowed to use, checking permissions, resolving
tool references, and building audit records of tool usage.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ToolAttachmentError(ValueError):
    """Base error for anything that goes wrong with tool attachments."""


class UndeclaredToolError(ToolAttachmentError):
    """Raised when an agent tries to use a tool it was not configured to use."""


class ToolPermissionError(ToolAttachmentError):
    """Raised when a tool needs permissions that the agent does not have."""


class ToolAttachmentManifest(BaseModel):
    """Describes a tool that an agent is allowed to use.

    Each manifest declares the tool's alias (short name), what code it
    points to, what permissions it needs, and whether it can cause
    side effects (like writing to a database).
    """

    model_config = ConfigDict(extra="forbid")

    alias: str
    executable_unit_ref: str
    permission_scope: tuple[str, ...] = Field(default_factory=tuple)
    timeout_override_seconds: float | None = Field(default=None, ge=0.0)
    side_effect_allowed: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate(self) -> ToolAttachmentManifest:
        """Clean up whitespace and check that required fields are not empty."""
        alias = self.alias.strip()
        executable_unit_ref = self.executable_unit_ref.strip()
        if not alias:
            raise ValueError("alias must not be empty")
        if not executable_unit_ref:
            raise ValueError("executable_unit_ref must not be empty")
        if any(not scope.strip() for scope in self.permission_scope):
            raise ValueError("permission_scope entries must not be empty")
        object.__setattr__(self, "alias", alias)
        object.__setattr__(self, "executable_unit_ref", executable_unit_ref)
        object.__setattr__(
            self,
            "permission_scope",
            tuple(scope.strip() for scope in self.permission_scope),
        )
        return self


class ToolAttachmentBinding(BaseModel):
    """A resolved, ready-to-use version of a tool attachment.

    Created from a ToolAttachmentManifest when the tool is actually
    needed at runtime. Contains the same information but represents
    a tool that has been looked up and is ready to execute.
    """

    model_config = ConfigDict(extra="forbid")

    alias: str
    executable_unit_ref: str
    permission_scope: tuple[str, ...] = Field(default_factory=tuple)
    timeout_override_seconds: float | None = Field(default=None, ge=0.0)
    side_effect_allowed: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_manifest(cls, manifest: ToolAttachmentManifest) -> ToolAttachmentBinding:
        """Create a binding from a manifest, copying all its fields."""
        return cls(
            alias=manifest.alias,
            executable_unit_ref=manifest.executable_unit_ref,
            permission_scope=manifest.permission_scope,
            timeout_override_seconds=manifest.timeout_override_seconds,
            side_effect_allowed=manifest.side_effect_allowed,
            metadata=dict(manifest.metadata),
        )


class ToolAttachmentRegistry:
    """A lookup table of all the tools an agent has declared.

    Tools are stored by their alias (short name). You can register new
    tools, look them up, and resolve them into bindings ready for use.
    """

    def __init__(self, attachments: Sequence[ToolAttachmentManifest] | None = None) -> None:
        self._attachments: dict[str, ToolAttachmentManifest] = {}
        for attachment in attachments or []:
            self.register(attachment)

    def register(self, manifest: ToolAttachmentManifest) -> ToolAttachmentManifest:
        """Add a tool to the registry. Raises if a different tool with the same alias exists."""
        existing = self._attachments.get(manifest.alias)
        if existing is not None and existing != manifest:
            raise ValueError(f"duplicate tool attachment alias: {manifest.alias}")
        self._attachments[manifest.alias] = manifest
        return manifest

    def get(self, alias: str) -> ToolAttachmentManifest:
        """Look up a tool by its alias. Raises KeyError if not found."""
        normalized = alias.strip()
        try:
            return self._attachments[normalized]
        except KeyError as exc:
            raise KeyError(normalized) from exc

    def has(self, alias: str) -> bool:
        """Check whether a tool with this alias is registered."""
        return alias.strip() in self._attachments

    def resolve(self, alias: str) -> ToolAttachmentBinding:
        """Look up a tool by alias and return a ready-to-use binding."""
        return ToolAttachmentBinding.from_manifest(self.get(alias))

    def resolve_many(self, aliases: Sequence[str]) -> list[ToolAttachmentBinding]:
        """Resolve multiple tool aliases into bindings at once."""
        return [self.resolve(alias) for alias in aliases]

    def declared_aliases(self) -> list[str]:
        """Return a sorted list of all registered tool aliases."""
        return sorted(self._attachments)


class ToolAttachmentBridge:
    """High-level helper for validating tool calls and building audit records.

    Sits between the agent runner and the tool registry. Checks that
    requested tools were declared, that the caller has the right
    permissions, and creates audit-friendly records of each tool call.
    """

    def __init__(self, registry: ToolAttachmentRegistry | None = None) -> None:
        self.registry = registry or ToolAttachmentRegistry()

    @classmethod
    def from_config(
        cls,
        attachments: Sequence[ToolAttachmentManifest],
    ) -> ToolAttachmentBridge:
        """Create a bridge with a registry pre-loaded from a list of manifests."""
        return cls(ToolAttachmentRegistry(attachments))

    def resolve_declared_tools(
        self,
        declared_tool_refs: Sequence[str],
    ) -> list[ToolAttachmentBinding]:
        """Resolve a list of declared tool aliases into bindings."""
        bindings: list[ToolAttachmentBinding] = []
        for alias in declared_tool_refs:
            bindings.append(self.registry.resolve(alias))
        return bindings

    def ensure_declared_tools(
        self,
        requested_tool_refs: Sequence[str],
        declared_tool_refs: Sequence[str],
    ) -> list[ToolAttachmentBinding]:
        """Verify that all requested tools were declared, then resolve them.

        Raises UndeclaredToolError if any requested tool is not in the
        declared list.
        """
        declared_aliases = {alias.strip() for alias in declared_tool_refs}
        requested_aliases = [alias.strip() for alias in requested_tool_refs]
        missing = [alias for alias in requested_aliases if alias not in declared_aliases]
        if missing:
            raise UndeclaredToolError(f"undeclared tool(s): {', '.join(missing)}")
        return [self.registry.resolve(alias) for alias in requested_aliases]

    def validate_permissions(
        self,
        binding: ToolAttachmentBinding | ToolAttachmentManifest,
        granted_permissions: Sequence[str] | None,
    ) -> None:
        """Check that the caller has all the permissions a tool requires.

        Raises ToolPermissionError if any required permission is missing.
        """
        required = set(binding.permission_scope)
        granted = {
            permission.strip() for permission in granted_permissions or [] if permission.strip()
        }
        missing = sorted(required - granted)
        if missing:
            raise ToolPermissionError(
                f"missing permissions for {binding.alias}: {', '.join(missing)}"
            )

    def build_resolution_audit(
        self,
        *,
        declared_tool_refs: Sequence[str],
        resolved_bindings: Sequence[ToolAttachmentBinding],
        requested_tool_refs: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Create an audit record showing which tools were declared, requested, and resolved."""
        declared_aliases = [alias.strip() for alias in declared_tool_refs]
        requested_aliases = [alias.strip() for alias in requested_tool_refs or declared_aliases]
        resolved_aliases = [binding.alias for binding in resolved_bindings]
        return {
            "declared_tools": declared_aliases,
            "requested_tools": requested_aliases,
            "resolved_tools": [self._binding_audit(binding) for binding in resolved_bindings],
            "missing_tools": [
                alias for alias in requested_aliases if alias not in resolved_aliases
            ],
        }

    def build_call_audit(
        self,
        *,
        binding: ToolAttachmentBinding | ToolAttachmentManifest,
        arguments: Mapping[str, Any],
        granted_permissions: Sequence[str] | None = None,
        outcome: Mapping[str, Any] | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        """Create an audit record for a single tool call, including its arguments and result."""
        binding_audit = self._binding_audit(binding)
        self.validate_permissions(binding, granted_permissions)
        record = {
            "tool": binding_audit,
            "arguments": dict(arguments),
            "outcome": dict(outcome or {}),
            "error": error,
        }
        return record

    def _binding_audit(
        self,
        binding: ToolAttachmentBinding | ToolAttachmentManifest,
    ) -> dict[str, Any]:
        """Convert a binding or manifest into a plain dictionary for audit logs."""
        return {
            "alias": binding.alias,
            "executable_unit_ref": binding.executable_unit_ref,
            "permission_scope": list(binding.permission_scope),
            "timeout_override_seconds": binding.timeout_override_seconds,
            "side_effect_allowed": binding.side_effect_allowed,
            "metadata": dict(binding.metadata),
        }


class ToolAttachmentAction(StrEnum):
    """The types of actions that can happen with a tool attachment.

    Used for categorizing events in audit logs and event routing.
    """

    DECLARE = "declare"
    RESOLVE = "resolve"
    CALL = "call"


def normalize_declared_tool_refs(declared_tool_refs: Sequence[str]) -> list[str]:
    """Normalize tool aliases into a stable, deduplicated order."""

    normalized: list[str] = []
    seen: set[str] = set()
    for alias in declared_tool_refs:
        cleaned = alias.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        normalized.append(cleaned)
    return normalized
