"""Validation of the payloads an edge can carry: conditions and mappings."""

from __future__ import annotations

from typing import TYPE_CHECKING

from zeroth.contracts.graph.limits import CONDITION_EXPRESSION_MAX_CHARS
from zeroth.contracts.graph.validation.issues import append_issue
from zeroth.contracts.graph.validation.references import validate_ref_list
from zeroth.contracts.graph.validation_errors import (
    ValidationCode,
    ValidationIssue,
    ValidationSeverity,
)
from zeroth.contracts.mappings import MappingValidationError
from zeroth.contracts.mappings.models import TransformMappingOperation

if TYPE_CHECKING:
    from zeroth.contracts.graph.models import Edge
    from zeroth.contracts.mappings import MappingValidator


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
    elif len(condition.expression) > CONDITION_EXPRESSION_MAX_CHARS:
        # A05-5: an expression is a single predicate the runtime evaluates on
        # every traversal of this edge. It had a non-empty check but no ceiling.
        append_issue(
            issues,
            severity=ValidationSeverity.ERROR,
            code=ValidationCode.INVALID_CONDITION,
            message=(
                f"condition expression exceeds the {CONDITION_EXPRESSION_MAX_CHARS} character limit"
            ),
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
    assert edge.mapping is not None
    for index, operation in enumerate(edge.mapping.operations):
        if (
            isinstance(operation, TransformMappingOperation)
            and len(operation.expression) > CONDITION_EXPRESSION_MAX_CHARS
        ):
            append_issue(
                issues,
                severity=ValidationSeverity.ERROR,
                code=ValidationCode.INVALID_MAPPING,
                message=(
                    "transform expression exceeds the "
                    f"{CONDITION_EXPRESSION_MAX_CHARS} character limit"
                ),
                graph_id=graph_id,
                edge_id=edge.edge_id,
                path=(
                    "edges",
                    edge.edge_id,
                    "mapping",
                    "operations",
                    str(index),
                    "expression",
                ),
            )
            return
    try:
        mapping_validator.validate(edge.mapping)
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
