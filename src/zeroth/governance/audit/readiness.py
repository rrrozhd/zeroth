"""Audit-signing readiness rules shared by deployment and operator surfaces."""

from __future__ import annotations

from zeroth.contracts.graph.models import ExecutableUnitNode, Graph
from zeroth.platform.signing import NullSigner

_CONSEQUENTIAL_CAPABILITIES = {
    "network_write",
    "filesystem_write",
    "secret_access",
    "external_api_call",
    "process_spawn",
    "memory_write",
}


def graph_has_consequential_actions(graph: Graph) -> bool:
    """Return whether a graph can cross a consequential external-effect boundary."""
    for node in graph.nodes:
        if _CONSEQUENTIAL_CAPABILITIES.intersection(node.capability_bindings):
            return True
        if isinstance(node, ExecutableUnitNode) and node.executable_unit.inline_source is None:
            return True
    return False


def signer_is_available(signer: object | None) -> bool:
    """A NullSigner is an explicit unsigned mode, not signing readiness."""
    return signer is not None and not isinstance(signer, NullSigner)


def signed_audit_required(graph: Graph, deployment_mode: str) -> bool:
    """Production always requires signing; local consequential graphs do too."""
    return deployment_mode == "production" or (
        deployment_mode == "local" and graph_has_consequential_actions(graph)
    )
