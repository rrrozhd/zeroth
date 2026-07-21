"""Multi-agent orchestration runtime with lazy public collaborators.

Keeping the package surface lazy lets narrow protocol modules be imported by
persistence adapters without initializing the full service orchestration graph.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from zeroth.runtime.orchestration.audit_recorder import (
        RuntimeAuditRecorder as RuntimeAuditRecorder,
    )
    from zeroth.runtime.orchestration.dispatcher import NodeDispatcher as NodeDispatcher
    from zeroth.runtime.orchestration.driver import GraphDriver as GraphDriver
    from zeroth.runtime.orchestration.errors import (
        MemoryBindingResolutionError as MemoryBindingResolutionError,
    )
    from zeroth.runtime.orchestration.errors import (
        NodeDispatcherError as NodeDispatcherError,
    )
    from zeroth.runtime.orchestration.errors import (
        OrchestratorError as OrchestratorError,
    )
    from zeroth.runtime.orchestration.parallel_executor import (
        RuntimeParallelExecutor as RuntimeParallelExecutor,
    )
    from zeroth.runtime.orchestration.policy_gate import RuntimePolicyGate as RuntimePolicyGate
    from zeroth.runtime.orchestration.run_worker import RunWorker as RunWorker
    from zeroth.runtime.orchestration.token_snapshot_store import (
        TokenSnapshotConcurrencyError as TokenSnapshotConcurrencyError,
    )
    from zeroth.runtime.orchestration.token_snapshot_store import (
        TokenSnapshotCorruptionError as TokenSnapshotCorruptionError,
    )
    from zeroth.runtime.orchestration.token_snapshot_store import (
        TokenSnapshotStore as TokenSnapshotStore,
    )
    from zeroth.runtime.orchestration.token_snapshot_store import (
        TokenSnapshotTransitionError as TokenSnapshotTransitionError,
    )
    from zeroth.runtime.orchestration.token_snapshot_store import (
        TokenSnapshotWriteDisabledError as TokenSnapshotWriteDisabledError,
    )
    from zeroth.runtime.orchestration.tool_executor import (
        RuntimeToolExecutor as RuntimeToolExecutor,
    )

_EXPORTS = {
    "GraphDriver": ("driver", "GraphDriver"),
    "MemoryBindingResolutionError": ("errors", "MemoryBindingResolutionError"),
    "NodeDispatcher": ("dispatcher", "NodeDispatcher"),
    "NodeDispatcherError": ("errors", "NodeDispatcherError"),
    "OrchestratorError": ("errors", "OrchestratorError"),
    "RunWorker": ("run_worker", "RunWorker"),
    "RuntimeAuditRecorder": ("audit_recorder", "RuntimeAuditRecorder"),
    "RuntimeParallelExecutor": ("parallel_executor", "RuntimeParallelExecutor"),
    "RuntimePolicyGate": ("policy_gate", "RuntimePolicyGate"),
    "RuntimeToolExecutor": ("tool_executor", "RuntimeToolExecutor"),
    "TokenSnapshotConcurrencyError": (
        "token_snapshot_store",
        "TokenSnapshotConcurrencyError",
    ),
    "TokenSnapshotCorruptionError": (
        "token_snapshot_store",
        "TokenSnapshotCorruptionError",
    ),
    "TokenSnapshotStore": ("token_snapshot_store", "TokenSnapshotStore"),
    "TokenSnapshotTransitionError": (
        "token_snapshot_store",
        "TokenSnapshotTransitionError",
    ),
    "TokenSnapshotWriteDisabledError": (
        "token_snapshot_store",
        "TokenSnapshotWriteDisabledError",
    ),
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> object:
    target = _EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = target
    value = getattr(importlib.import_module(f"{__name__}.{module_name}"), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
