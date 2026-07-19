"""Validation of the payloads an edge can carry: conditions and mappings."""

from __future__ import annotations

from typing import TYPE_CHECKING

from zeroth.contracts.graph.validation.issues import append_issue
from zeroth.contracts.graph.validation.references import validate_ref_list
from zeroth.contracts.mappings import MappingValidationError
from zeroth.core.graph.validation_errors import (
    ValidationCode,
    ValidationIssue,
    ValidationSeverity,
)

if TYPE_CHECKING:
    from zeroth.contracts.mappings import MappingValidator
    from zeroth.core.graph.models import Edge


def validate_condition(
    graph_id: str,
    edge: Edge,
    issues: list[ValidationIssue],
) -> None:
    """Check that an edge's condition has a non-empty expression and valid operand refs."""
    condition = edge.condition
    assert condition is not None
    if not condition.expression.strip():
        append_issue(
            issues,
            severity=ValidationSeverity.ERROR,
            code=ValidationCode.INVALID_CONDITION,
            message="condition expression is required",
            graph_id=graph_id,
            edge_id=edge.edge_id,
            path=("edges", edge.edge_id, "condition", "expression"),
        )
    validate_ref_list(
        issues,
        graph_id=graph_id,
        edge_id=edge.edge_id,
        refs=condition.operand_refs,
        code=ValidationCode.INVALID_CONDITION,
        message="invalid condition operand reference",
        path=("edges", edge.edge_id, "condition", "operand_refs"),
    )


def validate_mapping(
    graph_id: str,
    edge: Edge,
    issues: list[ValidationIssue],
    *,
    mapping_validator: MappingValidator,
) -> None:
    """Validate an edge's data mapping using the mapping validator."""
    try:
        mapping_validator.validate(edge.mapping)  # type: ignore[arg-type]
    except MappingValidationError as exc:
        append_issue(
            issues,
            severity=ValidationSeverity.ERROR,
            code=ValidationCode.INVALID_MAPPING,
            message=str(exc),
            graph_id=graph_id,
            edge_id=edge.edge_id,
            path=("edges", edge.edge_id, "mapping"),
            details={"error": str(exc)},
        )
