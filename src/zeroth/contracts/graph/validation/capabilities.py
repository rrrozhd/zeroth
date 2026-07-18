"""The seam between contract validation and governance-owned capability rules.

Capability grants resolve reference strings against the ``Capability`` enum,
which lives in the governance layer and cannot move: its module path is
embedded in signature strings pinned by the immutable legacy library surface.
The contracts layer may not import governance, so the rules arrive as an
injected collaborator instead.

Injection rather than a separate later pass is deliberate. Both rules fire
partway through their validator's issue sequence -- the MCP check inside a
node, the grant check at the end of each agent's tool block -- so running them
afterwards would reorder the report, which consumers read as a contract.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from zeroth.core.graph.models import AgentNode, Graph, Node
    from zeroth.core.graph.validation_errors import ValidationIssue


class CapabilityChecks(Protocol):
    """Capability rules supplied by the layer that composes the validators."""

    def validate_agent_capabilities(
        self,
        graph_id: str,
        node: AgentNode,
        issues: list[ValidationIssue],
    ) -> None:
        """Check an agent's own declared capability needs."""

    def validate_tool_grants(
        self,
        graph: Graph,
        node: AgentNode,
        node_map: dict[str, Node],
        issues: list[ValidationIssue],
    ) -> None:
        """Check that an agent's grant covers every attached tool's needs."""


class NullCapabilityChecks:
    """Contract-only validation: skips the governance-owned capability rules."""

    def validate_agent_capabilities(
        self,
        graph_id: str,
        node: AgentNode,
        issues: list[ValidationIssue],
    ) -> None:
        """Do nothing; the composing layer supplies the real implementation."""

    def validate_tool_grants(
        self,
        graph: Graph,
        node: AgentNode,
        node_map: dict[str, Node],
        issues: list[ValidationIssue],
    ) -> None:
        """Do nothing; the composing layer supplies the real implementation."""
