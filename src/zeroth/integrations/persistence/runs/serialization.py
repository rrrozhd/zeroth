"""Translation between database rows and the run domain models.

Everything here is a pure function: no database reference, no transaction, no
mutation of its arguments. The row converters were methods on the low-level
store that never touched ``self``, which is why they belong outside it.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from zeroth.platform.storage.json import load_typed_value, to_json_value
from zeroth.runtime.runs import (
    Run,
    RunConditionResult,
    RunHistoryEntry,
    RunStatus,
    Thread,
    ThreadMemoryBinding,
    ThreadStatus,
)


def dump_model(value: object | None) -> str | None:
    """Serialize a Pydantic model (or plain value) to a JSON string for storage."""
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return to_json_value(value)  # type: ignore[arg-type]
    return to_json_value(value)  # type: ignore[arg-type]


def dump_list(items: Sequence[object]) -> str:
    """Serialize a list of Pydantic models to a JSON string for storage."""
    return to_json_value([item.model_dump(mode="json") for item in items])  # type: ignore[attr-defined]


def row_to_run(row: dict[str, Any]) -> Run:
    """Convert a raw database row into a Run model."""
    return Run(
        run_id=row["run_id"],
        checkpoint_id=row["checkpoint_id"],
        parent_checkpoint_id=row["parent_checkpoint_id"],
        epoch=row["epoch"],
        workflow_name=row["workflow_name"],
        status=RunStatus(row["status"]),
        current_step=row["current_step"],
        completed_steps=load_typed_value(row["completed_steps"], list[str]) or [],
        artifacts=load_typed_value(row["artifacts"], dict[str, Any]) or {},
        channels=load_typed_value(row["channels"], dict[str, Any]) or {},
        pending_approval=load_typed_value(row["pending_approval"], Any),
        pending_interrupt_id=row["pending_interrupt_id"],
        started_at=datetime.fromisoformat(row["started_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        error=row["error"],
        metadata=load_typed_value(row["metadata"], dict[str, Any]) or {},
        graph_version_ref=row["graph_version_ref"],
        deployment_ref=row["deployment_ref"],
        parent_run_id=row["parent_run_id"],
        tenant_id=row["tenant_id"] or "default",
        workspace_id=row["workspace_id"],
        submitted_by=load_typed_value(row["submitted_by"], dict[str, Any]),
        thread_id=row["thread_id"],
        current_node_ids=load_typed_value(row["current_node_ids"], list[str]) or [],
        pending_node_ids=load_typed_value(row["pending_node_ids"], list[str]) or [],
        execution_history=(load_typed_value(row["execution_history"], list[RunHistoryEntry]) or []),
        node_visit_counts=load_typed_value(row["node_visit_counts"], dict[str, int]) or {},
        condition_results=(
            load_typed_value(row["condition_results"], list[RunConditionResult]) or []
        ),
        audit_refs=load_typed_value(row["audit_refs"], list[str]) or [],
        final_output=load_typed_value(row["final_output"], Any),
        failure_state=load_typed_value(row["failure_state"], dict[str, Any]),
    )


def row_to_thread(row: dict[str, Any]) -> Thread:
    """Convert a raw database row into a Thread model."""
    return Thread(
        thread_id=row["thread_id"],
        graph_version_ref=row["graph_version_ref"],
        deployment_ref=row["deployment_ref"],
        tenant_id=row["tenant_id"] or "default",
        workspace_id=row["workspace_id"],
        status=ThreadStatus(row["status"]),
        participating_agent_refs=(
            load_typed_value(row["participating_agent_refs"], list[str]) or []
        ),
        state_snapshot_refs=load_typed_value(row["state_snapshot_refs"], list[str]) or [],
        checkpoint_refs=load_typed_value(row["checkpoint_refs"], list[str]) or [],
        memory_bindings=(load_typed_value(row["memory_bindings"], list[ThreadMemoryBinding]) or []),
        run_ids=load_typed_value(row["run_ids"], list[str]) or [],
        active_run_id=row["active_run_id"],
        last_run_id=row["last_run_id"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )
