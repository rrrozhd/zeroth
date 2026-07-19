"""Governed system specifications.

The contract slice of the vendored governai bundle: the flow/step
specifications under :mod:`zeroth.contracts.governed.app` and the shared
state models under :mod:`zeroth.contracts.governed.models`. The audit,
integrations, memory, runtime, and tools implementations remain under
:mod:`zeroth.core.governed` (see docs/governed-capability-disposition.md).
"""

from zeroth.contracts.governed.models.common import RunStatus
from zeroth.contracts.governed.models.run_state import RunState

__all__ = ["RunState", "RunStatus"]
