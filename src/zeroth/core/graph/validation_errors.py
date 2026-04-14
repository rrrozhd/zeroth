"""Data types for validation results, issue codes, and validation errors.

When the GraphValidator checks a graph, it produces a GraphValidationReport
containing ValidationIssue objects.  If there are blocking errors, you can
call raise_for_errors() to throw a GraphValidationError.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ValidationSeverity(StrEnum):
    """How serious a validation issue is.

    WARNING means something looks suspicious but won't block you.
    ERROR means the graph cannot be published or run until it is fixed.
    """

    WARNING = "warning"
    ERROR = "error"


class ValidationCode(StrEnum):
    """Machine-readable codes that identify what kind of problem was found.

    Each code maps to a specific type of check (e.g. missing entrypoint,
    duplicate node ID, unsafe cycle).  These are stable and safe to use
    in automated tooling.
    """

    EMPTY_GRAPH = "empty_graph"
    DUPLICATE_NODE_ID = "duplicate_node_id"
    DUPLICATE_EDGE_ID = "duplicate_edge_id"
    MISSING_ENTRYPOINT = "missing_entrypoint"
    UNKNOWN_ENTRYPOINT = "unknown_entrypoint"
    UNKNOWN_EDGE_SOURCE = "unknown_edge_source"
    UNKNOWN_EDGE_TARGET = "unknown_edge_target"
    INVALID_GRAPH_VERSION_REF = "invalid_graph_version_ref"
    MISSING_CONTRACT_REF = "missing_contract_ref"
    INVALID_CONTRACT_REF = "invalid_contract_ref"
    INVALID_NODE_ATTACHMENT = "invalid_node_attachment"
    INVALID_CONDITION = "invalid_condition"
    INVALID_MAPPING = "invalid_mapping"
    UNSAFE_CYCLE = "unsafe_cycle"
    INVALID_POLICY_REF = "invalid_policy_ref"
    INVALID_CAPABILITY_REF = "invalid_capability_ref"
    INVALID_OUTPUT_CONTRACT = "invalid_output_contract"
    INVALID_MERGE_STRATEGY = "invalid_merge_strategy"
    INVALID_REDUCER_REF = "invalid_reducer_ref"


class ValidationIssue(BaseModel):
    """A single problem found during graph validation.

    Contains the severity, a machine-readable code, a human-readable message,
    and location info (which graph, node, or edge has the problem).
    """

    model_config = ConfigDict(frozen=True)

    severity: ValidationSeverity
    code: ValidationCode
    message: str
    graph_id: str
    node_id: str | None = None
    edge_id: str | None = None
    path: tuple[str, ...] = Field(default_factory=tuple)
    details: dict[str, Any] = Field(default_factory=dict)


class GraphValidationReport(BaseModel):
    """The full validation result for a graph, containing all issues found.

    Use ``is_valid`` to quickly check if the graph passed, ``errors`` to get
    just the blocking problems, or ``raise_for_errors()`` to throw if invalid.
    """

    model_config = ConfigDict(frozen=True)

    graph_id: str
    issues: list[ValidationIssue] = Field(default_factory=list)

    @property
    def errors(self) -> list[ValidationIssue]:
        """Return only the ERROR-level issues (problems that must be fixed)."""
        return [issue for issue in self.issues if issue.severity is ValidationSeverity.ERROR]

    @property
    def warnings(self) -> list[ValidationIssue]:
        """Return only the WARNING-level issues (non-blocking concerns)."""
        return [issue for issue in self.issues if issue.severity is ValidationSeverity.WARNING]

    @property
    def is_valid(self) -> bool:
        """Return True if there are no errors (warnings are okay)."""
        return not self.errors

    def summary(self) -> dict[str, int]:
        """Return a compact severity summary."""
        return {
            "errors": len(self.errors),
            "warnings": len(self.warnings),
            "total": len(self.issues),
        }

    def raise_for_errors(self) -> None:
        """Raise when the report contains blocking issues."""
        if self.errors:
            raise GraphValidationError(self)


class GraphValidationError(ValueError):
    """Raised when a graph fails validation with one or more errors.

    The ``report`` attribute contains the full GraphValidationReport
    so you can inspect exactly what went wrong.
    """

    def __init__(self, report: GraphValidationReport):
        self.report = report
        summary = report.summary()
        super().__init__(
            f"graph {report.graph_id!r} failed validation: "
            f"{summary['errors']} error(s), {summary['warnings']} warning(s)"
        )
