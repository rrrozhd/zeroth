"""Stable Regulus registry identities for Zeroth agent executions."""

from __future__ import annotations

import re
from hashlib import sha256

_RUNTIME_NAMESPACE_PREFIX = re.compile(r"^(?:branch:\d+:|subgraph:[^:]+:\d+:)+")
_BRANCH_PREFIX = re.compile(r"^(?:branch:\d+:)+")


def stable_runtime_node_id(node_id: str) -> str:
    """Strip ephemeral parallel-branch prefixes from a runtime node identity."""
    return _BRANCH_PREFIX.sub("", node_id)


def authored_node_id(node_id: str) -> str:
    """Return the authored node ID behind runtime branch/subgraph namespaces."""
    return _RUNTIME_NAMESPACE_PREFIX.sub("", node_id)


def capability_identity(tenant_id: str, deployment_ref: str, node_id: str) -> str:
    material = f"capability\0{tenant_id}\0{deployment_ref}\0{node_id}".encode()
    return f"zeroth-cap-{sha256(material).hexdigest()[:24]}"


def implementation_identity(capability_id: str, model_provider: str) -> str:
    material = f"implementation\0{capability_id}\0{model_provider}".encode()
    return f"zeroth-impl-{sha256(material).hexdigest()[:24]}"
