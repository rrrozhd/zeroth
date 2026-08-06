"""Governed system specifications.

The contract slice of the vendored governai bundle: the flow/step
specifications under :mod:`zeroth.contracts.governed.app` and the shared
state models under :mod:`zeroth.contracts.governed.models`.

Only the contract slice lives here. The rest of the vendored bundle went to the
domain that owns it: audit to :mod:`zeroth.governance.audit`, memory to
:mod:`zeroth.integrations.memory.governed`, tools to
:mod:`zeroth.runtime.agents.tooling`, and the interrupt and run stores to
:mod:`zeroth.runtime.orchestration`.
"""

from zeroth.contracts.governed.models.common import RunStatus
from zeroth.contracts.governed.models.memory import MemoryScope
from zeroth.contracts.governed.models.run_state import RunState

__all__ = ["MemoryScope", "RunState", "RunStatus"]
