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
    from zeroth.runtime.orchestration.token_joins import (
        FailureMode as FailureMode,
    )
    from zeroth.runtime.orchestration.token_joins import JoinReducer as JoinReducer
    from zeroth.runtime.orchestration.token_joins import (
        JoinReducerInput as JoinReducerInput,
    )
    from zeroth.runtime.orchestration.token_joins import (
        JoinReductionClaim as JoinReductionClaim,
    )
    from zeroth.runtime.orchestration.token_joins import (
        JoinReductionClaimChangedError as JoinReductionClaimChangedError,
    )
    from zeroth.runtime.orchestration.token_joins import (
        JoinReductionRecoveryError as JoinReductionRecoveryError,
    )
    from zeroth.runtime.orchestration.token_joins import (
        JoinReductionReleaseError as JoinReductionReleaseError,
    )
    from zeroth.runtime.orchestration.token_joins import (
        TokenJoinTransitionError as TokenJoinTransitionError,
    )
    from zeroth.runtime.orchestration.token_joins import (
        close_ready_join as close_ready_join,
    )
    from zeroth.runtime.orchestration.token_joins import (
        close_ready_join_with_cas as close_ready_join_with_cas,
    )
    from zeroth.runtime.orchestration.token_joins import (
        deliver_to_join as deliver_to_join,
    )
    from zeroth.runtime.orchestration.token_joins import (
        reclaim_abandoned_join_reduction_with_cas as reclaim_abandoned_join_reduction_with_cas,
    )
    from zeroth.runtime.orchestration.token_joins import (
        reduce_join_inputs as reduce_join_inputs,
    )
    from zeroth.runtime.orchestration.token_joins import (
        settle_join_without_delivery as settle_join_without_delivery,
    )
    from zeroth.runtime.orchestration.token_loops import LoopReducer as LoopReducer
    from zeroth.runtime.orchestration.token_loops import (
        LoopReductionClaim as LoopReductionClaim,
    )
    from zeroth.runtime.orchestration.token_loops import (
        TokenLoopTransitionError as TokenLoopTransitionError,
    )
    from zeroth.runtime.orchestration.token_loops import (
        close_ready_loop as close_ready_loop,
    )
    from zeroth.runtime.orchestration.token_loops import (
        close_ready_loop_with_cas as close_ready_loop_with_cas,
    )
    from zeroth.runtime.orchestration.token_loops import enter_loop as enter_loop
    from zeroth.runtime.orchestration.token_loops import (
        reclaim_abandoned_loop_reduction_with_cas as reclaim_abandoned_loop_reduction_with_cas,
    )
    from zeroth.runtime.orchestration.token_loops import (
        settle_loop_member as settle_loop_member,
    )
    from zeroth.runtime.orchestration.token_scheduler import (
        DispatchClaim as DispatchClaim,
    )
    from zeroth.runtime.orchestration.token_scheduler import (
        FanOutBranch as FanOutBranch,
    )
    from zeroth.runtime.orchestration.token_scheduler import (
        PostCommitEffect as PostCommitEffect,
    )
    from zeroth.runtime.orchestration.token_scheduler import (
        TokenPostCommitError as TokenPostCommitError,
    )
    from zeroth.runtime.orchestration.token_scheduler import (
        TokenSchedulerTransitionError as TokenSchedulerTransitionError,
    )
    from zeroth.runtime.orchestration.token_scheduler import (
        TokenTransition as TokenTransition,
    )
    from zeroth.runtime.orchestration.token_scheduler import (
        apply_token_transition as apply_token_transition,
    )
    from zeroth.runtime.orchestration.token_scheduler import (
        claim_next_token as claim_next_token,
    )
    from zeroth.runtime.orchestration.token_scheduler import (
        complete_dispatch as complete_dispatch,
    )
    from zeroth.runtime.orchestration.token_scheduler import (
        enqueue_dispatch as enqueue_dispatch,
    )
    from zeroth.runtime.orchestration.token_scheduler import (
        fail_dispatch as fail_dispatch,
    )
    from zeroth.runtime.orchestration.token_scheduler import (
        fan_out_dispatch as fan_out_dispatch,
    )
    from zeroth.runtime.orchestration.token_scheduler import (
        initialize_token_snapshot as initialize_token_snapshot,
    )
    from zeroth.runtime.orchestration.token_scheduler import (
        recover_dispatch as recover_dispatch,
    )
    from zeroth.runtime.orchestration.token_scheduler import (
        retry_dispatch as retry_dispatch,
    )
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
    "DispatchClaim": ("token_scheduler", "DispatchClaim"),
    "FanOutBranch": ("token_scheduler", "FanOutBranch"),
    "FailureMode": ("token_joins", "FailureMode"),
    "GraphDriver": ("driver", "GraphDriver"),
    "JoinReducer": ("token_joins", "JoinReducer"),
    "JoinReducerInput": ("token_joins", "JoinReducerInput"),
    "JoinReductionClaim": ("token_joins", "JoinReductionClaim"),
    "JoinReductionClaimChangedError": (
        "token_joins",
        "JoinReductionClaimChangedError",
    ),
    "JoinReductionRecoveryError": ("token_joins", "JoinReductionRecoveryError"),
    "JoinReductionReleaseError": ("token_joins", "JoinReductionReleaseError"),
    "LoopReducer": ("token_loops", "LoopReducer"),
    "LoopReductionClaim": ("token_loops", "LoopReductionClaim"),
    "MemoryBindingResolutionError": ("errors", "MemoryBindingResolutionError"),
    "NodeDispatcher": ("dispatcher", "NodeDispatcher"),
    "NodeDispatcherError": ("errors", "NodeDispatcherError"),
    "OrchestratorError": ("errors", "OrchestratorError"),
    "RunWorker": ("run_worker", "RunWorker"),
    "RuntimeAuditRecorder": ("audit_recorder", "RuntimeAuditRecorder"),
    "RuntimeParallelExecutor": ("parallel_executor", "RuntimeParallelExecutor"),
    "RuntimePolicyGate": ("policy_gate", "RuntimePolicyGate"),
    "RuntimeToolExecutor": ("tool_executor", "RuntimeToolExecutor"),
    "PostCommitEffect": ("token_scheduler", "PostCommitEffect"),
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
    "TokenSchedulerTransitionError": (
        "token_scheduler",
        "TokenSchedulerTransitionError",
    ),
    "TokenJoinTransitionError": ("token_joins", "TokenJoinTransitionError"),
    "TokenLoopTransitionError": ("token_loops", "TokenLoopTransitionError"),
    "TokenPostCommitError": ("token_scheduler", "TokenPostCommitError"),
    "TokenTransition": ("token_scheduler", "TokenTransition"),
    "apply_token_transition": ("token_scheduler", "apply_token_transition"),
    "claim_next_token": ("token_scheduler", "claim_next_token"),
    "close_ready_join": ("token_joins", "close_ready_join"),
    "close_ready_join_with_cas": ("token_joins", "close_ready_join_with_cas"),
    "close_ready_loop": ("token_loops", "close_ready_loop"),
    "close_ready_loop_with_cas": ("token_loops", "close_ready_loop_with_cas"),
    "complete_dispatch": ("token_scheduler", "complete_dispatch"),
    "enqueue_dispatch": ("token_scheduler", "enqueue_dispatch"),
    "enter_loop": ("token_loops", "enter_loop"),
    "deliver_to_join": ("token_joins", "deliver_to_join"),
    "fail_dispatch": ("token_scheduler", "fail_dispatch"),
    "fan_out_dispatch": ("token_scheduler", "fan_out_dispatch"),
    "initialize_token_snapshot": ("token_scheduler", "initialize_token_snapshot"),
    "recover_dispatch": ("token_scheduler", "recover_dispatch"),
    "reclaim_abandoned_join_reduction_with_cas": (
        "token_joins",
        "reclaim_abandoned_join_reduction_with_cas",
    ),
    "reclaim_abandoned_loop_reduction_with_cas": (
        "token_loops",
        "reclaim_abandoned_loop_reduction_with_cas",
    ),
    "reduce_join_inputs": ("token_joins", "reduce_join_inputs"),
    "retry_dispatch": ("token_scheduler", "retry_dispatch"),
    "settle_join_without_delivery": ("token_joins", "settle_join_without_delivery"),
    "settle_loop_member": ("token_loops", "settle_loop_member"),
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
