"""Legacy import path for the orchestrator package.

The ``RuntimeOrchestrator`` and its errors now live in
:mod:`zeroth.runtime.orchestration`. This package republishes exactly the names
it published before ZER-25 relocated them; import from the canonical location
instead (see docs/backend-import-migration.md).
"""

from zeroth.runtime.orchestration.errors import (
    NodeDispatcherError,
    OrchestratorError,
)
from zeroth.runtime.orchestration.orchestrator import RuntimeOrchestrator

__all__ = [
    "NodeDispatcherError",
    "OrchestratorError",
    "RuntimeOrchestrator",
]
