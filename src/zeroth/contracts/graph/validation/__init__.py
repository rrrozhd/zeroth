"""Contract-owned graph validators.

Each module here checks one concern of a graph and appends
``ValidationIssue`` values to a caller-supplied list. They are deliberately
free of runtime, governance, and integration dependencies so the contracts
layer can own them, and ``ContractValidator`` composes them in the canonical
order.

Two rules cannot live here. Capability grants resolve refs against the
governance ``Capability`` enum, and parallel-config checks import the runtime
reducer registry. Both arrive from outside: capability rules through the
``CapabilityChecks`` seam, parallel checks in the public ``GraphValidator``,
which is assembled in ``zeroth.runtime.graph_validation``.
"""

from zeroth.contracts.graph.validation.capabilities import (
    CapabilityChecks,
    NullCapabilityChecks,
)
from zeroth.contracts.graph.validation.facade import ContractValidator

__all__ = [
    "CapabilityChecks",
    "ContractValidator",
    "NullCapabilityChecks",
]
