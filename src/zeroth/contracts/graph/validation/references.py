"""Reference-shape rules and graph-level reference checks."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from zeroth.contracts.graph.validation.issues import append_issue
from zeroth.contracts.graph.validation_errors import (
    ValidationCode,
    ValidationIssue,
    ValidationSeverity,
)

if TYPE_CHECKING:
    from zeroth.contracts.graph.models import Graph


def is_ref_like(value: str) -> bool:
    """Return True if the string looks like a valid reference (non-empty, no spaces)."""
    text = value.strip()
    return bool(text) and not any(part.isspace() for part in text)


def all_unique(values: Iterable[str]) -> bool:
    """Return True if every string in the iterable is unique (no duplicates)."""
    seen: set[str] = set()
    for value in values:
        if value in seen:
            return False
        seen.add(value)
    return True


def require_ref(
    issues: list[ValidationIssue],
    *,
    graph_id: str,
    node_id: str,
    code: ValidationCode,
    message: str,
    value: str | None,
    path: tuple[str, ...],
) -> None:
    """Record an error if a required reference is missing or invalid."""
    if value is None or not is_ref_like(value):
        append_issue(
            issues,
            severity=ValidationSeverity.ERROR,
            code=code,
            message=message,
            graph_id=graph_id,
            node_id=node_id,
            path=path,
            details={"ref": value},
        )


def validate_ref_list(
    issues: list[ValidationIssue],
    *,
    graph_id: str,
    refs: list[str],
    code: ValidationCode,
    message: str,
    path: tuple[str, ...],
    node_id: str | None = None,
    edge_id: str | None = None,
) -> None:
    """Check each reference in a list and record an error for any invalid ones."""
    for index, ref in enumerate(refs):
        if not is_ref_like(ref):
            append_issue(
                issues,
                severity=ValidationSeverity.ERROR,
                code=code,
                message=message,
                graph_id=graph_id,
                node_id=node_id,
                edge_id=edge_id,
                path=path + (str(index),),
                details={"ref": ref},
            )


def validate_graph_refs(graph: Graph, issues: list[ValidationIssue]) -> None:
    """Check that graph-level policy references look valid."""
    for ref in graph.policy_bindings:
        if not is_ref_like(ref):
            append_issue(
                issues,
                severity=ValidationSeverity.ERROR,
                code=ValidationCode.INVALID_POLICY_REF,
                message=f"invalid policy reference: {ref!r}",
                graph_id=graph.graph_id,
                path=("policy_bindings",),
                details={"ref": ref},
            )
