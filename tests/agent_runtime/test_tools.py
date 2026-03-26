from __future__ import annotations

import pytest

from zeroth.agent_runtime.tools import (
    ToolAttachmentBridge,
    ToolAttachmentManifest,
    ToolAttachmentRegistry,
    ToolPermissionError,
    UndeclaredToolError,
    normalize_declared_tool_refs,
)


def _registry() -> ToolAttachmentRegistry:
    return ToolAttachmentRegistry(
        [
            ToolAttachmentManifest(
                alias="search",
                executable_unit_ref="eu://search",
                permission_scope=("net:query",),
                timeout_override_seconds=2.5,
                side_effect_allowed=False,
                metadata={"description": "search helper"},
            ),
            ToolAttachmentManifest(
                alias="notes",
                executable_unit_ref="eu://notes",
                permission_scope=("memory:write",),
                side_effect_allowed=True,
                metadata={"description": "write notes"},
            ),
        ]
    )


def test_tool_attachment_registry_resolves_aliases_and_normalizes_refs() -> None:
    registry = _registry()
    bridge = ToolAttachmentBridge(registry)

    binding = registry.resolve("search")
    audit = bridge.build_resolution_audit(
        declared_tool_refs=normalize_declared_tool_refs(["search", "notes", "search"]),
        resolved_bindings=[binding],
        requested_tool_refs=["search"],
    )

    assert binding.alias == "search"
    assert binding.executable_unit_ref == "eu://search"
    assert binding.timeout_override_seconds == 2.5
    assert audit["declared_tools"] == ["search", "notes"]
    assert audit["resolved_tools"][0]["side_effect_allowed"] is False


def test_tool_attachment_bridge_rejects_undeclared_tool_requests() -> None:
    registry = _registry()
    bridge = ToolAttachmentBridge(registry)

    with pytest.raises(UndeclaredToolError, match="undeclared tool"):
        bridge.ensure_declared_tools(
            requested_tool_refs=["search", "admin"],
            declared_tool_refs=["search"],
        )


def test_tool_attachment_bridge_enforces_permissions() -> None:
    registry = _registry()
    bridge = ToolAttachmentBridge(registry)
    binding = registry.resolve("notes")

    with pytest.raises(ToolPermissionError, match="missing permissions"):
        bridge.validate_permissions(binding, granted_permissions=["memory:read"])

    bridge.validate_permissions(binding, granted_permissions=["memory:write", "memory:read"])


def test_tool_attachment_audit_helpers_capture_timeout_and_side_effect_metadata() -> None:
    registry = _registry()
    bridge = ToolAttachmentBridge(registry)
    binding = registry.resolve("notes")

    resolution_audit = bridge.build_resolution_audit(
        declared_tool_refs=["notes"],
        resolved_bindings=[binding],
    )
    call_audit = bridge.build_call_audit(
        binding=binding,
        arguments={"topic": "daily-review"},
        granted_permissions=["memory:write"],
        outcome={"status": "ok"},
    )

    assert resolution_audit["resolved_tools"][0]["timeout_override_seconds"] is None
    assert resolution_audit["resolved_tools"][0]["side_effect_allowed"] is True
    assert call_audit["tool"]["alias"] == "notes"
    assert call_audit["tool"]["side_effect_allowed"] is True
    assert call_audit["arguments"] == {"topic": "daily-review"}
    assert call_audit["outcome"] == {"status": "ok"}

