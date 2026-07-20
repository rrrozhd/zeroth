"""Legacy import path for the subgraph node-data contract.

``SubgraphNodeData`` is authored graph vocabulary and lives in
:mod:`zeroth.contracts.graph.models`; this module republishes it for
compatibility.
"""

from zeroth.contracts.graph.models import SubgraphNodeData

__all__ = ["SubgraphNodeData"]
