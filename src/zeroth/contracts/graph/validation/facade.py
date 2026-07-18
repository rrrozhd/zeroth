"""Composition of the contract-owned validators."""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

from zeroth.contracts.graph.validation.capabilities import (
    CapabilityChecks,
    NullCapabilityChecks,
)
from zeroth.contracts.graph.validation.cycles import validate_cycles
from zeroth.contracts.graph.validation.edges import validate_edges
from zeroth.contracts.graph.validation.issues import append_issue
from zeroth.contracts.graph.validation.nodes import validate_entrypoint, validate_nodes
from zeroth.contracts.graph.validation.references import validate_graph_refs
from zeroth.contracts.graph.validation.tools import validate_tool_attachments
from zeroth.core.graph.validation_errors import (
    ValidationCode,
    ValidationIssue,
    ValidationSeverity,
)
from zeroth.core.mappings import MappingValidator

if TYPE_CHECKING:
    from zeroth.core.graph.models import Graph, Node


class ContractValidator:
    """Run every contract-owned check against a graph, in the canonical order.

    Synchronous and self-contained: it reads the graph and nothing else, so a
    library consumer can check graph structure without a runtime.

    Governance-owned capability rules are skipped unless ``capability_checks``
    is supplied. The public ``GraphValidator`` supplies them along with the
    execution-level checks it adds on top.
    """

    def __init__(
        self,
        mapping_validator: MappingValidator | None = None,
        capability_checks: CapabilityChecks | None = None,
    ):
        self._mapping_validator = mapping_validator or MappingValidator()
        self._capability_checks = capability_checks or NullCapabilityChecks()

    def validate(self, graph: Graph, issues: list[ValidationIssue]) -> None:
        """Append every contract-level issue found in the graph."""
        node_map: dict[str, Node] = {}
        edge_ids: set[str] = set()
        # Adjacency is built once and then reused by the cycle checks later in validation.
        adjacency: dict[str, list[str]] = defaultdict(list)

        if not graph.nodes:
            append_issue(
                issues,
                severity=ValidationSeverity.ERROR,
                code=ValidationCode.EMPTY_GRAPH,
                message="graph must contain at least one node",
                graph_id=graph.graph_id,
            )

        validate_graph_refs(graph, issues)
        validate_nodes(graph, node_map, issues, capability_checks=self._capability_checks)
        validate_entrypoint(graph, node_map, issues)
        validate_edges(
            graph,
            node_map,
            edge_ids,
            adjacency,
            issues,
            mapping_validator=self._mapping_validator,
        )
        validate_tool_attachments(
            graph,
            node_map,
            issues,
            capability_checks=self._capability_checks,
        )
        validate_cycles(graph, node_map, adjacency, issues)
