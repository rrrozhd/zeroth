"""Constructing and accumulating validation issues."""

from __future__ import annotations

from typing import Any

from zeroth.core.graph.validation_errors import (
    ValidationCode,
    ValidationIssue,
    ValidationSeverity,
)


def append_issue(
    issues: list[ValidationIssue],
    *,
    severity: ValidationSeverity,
    code: ValidationCode,
    message: str,
    graph_id: str,
    node_id: str | None = None,
    edge_id: str | None = None,
    path: tuple[str, ...] = (),
    details: dict[str, Any] | None = None,
) -> None:
    """Helper to create a ValidationIssue and add it to the issues list."""
    issues.append(
        ValidationIssue(
            severity=severity,
            code=code,
            message=message,
            graph_id=graph_id,
            node_id=node_id,
            edge_id=edge_id,
            path=path,
            details=dict(details or {}),
        )
    )
