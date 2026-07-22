"""Versioned audit erasure schemas shared by writing, verification, and cleanup."""

from __future__ import annotations

from typing import Any

LATEST_DIGEST_VERSION = 3

_PII_COMMITMENT_FIELDS_V2: tuple[str, ...] = (
    "input_snapshot",
    "output_snapshot",
    "validation_results",
    "execution_metadata",
    "stdout",
    "stderr",
    "error",
    "tool_calls",
    "memory_interactions",
)

_PII_COMMITMENT_FIELDS_V3: tuple[str, ...] = (
    *_PII_COMMITMENT_FIELDS_V2,
    "condition_results",
    "approval_actions",
)

AUDIT_CLEANUP_PAYLOAD_FIELDS: tuple[str, ...] = (
    "input_snapshot",
    "output_snapshot",
    "validation_results",
    "execution_metadata",
    "condition_results",
    "memory_interactions",
    "tool_calls",
    "approval_actions",
)

ERASED_PII_VALUES: dict[str, Any] = {
    "input_snapshot": {},
    "output_snapshot": {},
    "validation_results": {},
    "execution_metadata": {},
    "stdout": None,
    "stderr": None,
    "error": None,
    "condition_results": [],
    "tool_calls": [],
    "memory_interactions": [],
    "approval_actions": [],
}


def pii_commitment_fields(digest_version: int) -> tuple[str, ...]:
    """Return the immutable PII field schema for a supported digest version."""
    if digest_version == 2:
        return _PII_COMMITMENT_FIELDS_V2
    if digest_version >= 3:
        return _PII_COMMITMENT_FIELDS_V3
    return ()
