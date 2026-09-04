"""Translation between database rows and the run domain models.

Everything here is a pure function: no database reference, no transaction, no
mutation of its arguments. The row converters were methods on the low-level
store that never touched ``self``, which is why they belong outside it.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from zeroth.platform.storage.json import from_json_value, to_json_value
from zeroth.runtime.runs import (
    Run,
    RunStatus,
    Thread,
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


def _collection_value(raw: str | bytes | None, default: Any) -> Any:
    """Decode a collection, defaulting only null before domain validation."""
    value = from_json_value(raw)
    return default if value is None else value


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
        completed_steps=_collection_value(row["completed_steps"], []),
        artifacts=_collection_value(row["artifacts"], {}),
        channels=_collection_value(row["channels"], {}),
        pending_approval=from_json_value(row["pending_approval"]),
        pending_interrupt_id=row["pending_interrupt_id"],
        started_at=datetime.fromisoformat(row["started_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        error=row["error"],
        metadata=_collection_value(row["metadata"], {}),
        graph_version_ref=row["graph_version_ref"],
        deployment_ref=row["deployment_ref"],
        parent_run_id=row["parent_run_id"],
        tenant_id=row["tenant_id"] or "default",
        workspace_id=row["workspace_id"],
        submitted_by=from_json_value(row["submitted_by"]),
        thread_id=row["thread_id"],
        current_node_ids=_collection_value(row["current_node_ids"], []),
        pending_node_ids=_collection_value(row["pending_node_ids"], []),
        execution_history=_collection_value(row["execution_history"], []),
        node_visit_counts=_collection_value(row["node_visit_counts"], {}),
        condition_results=_collection_value(row["condition_results"], []),
        audit_refs=_collection_value(row["audit_refs"], []),
        final_output=from_json_value(row["final_output"]),
        failure_state=from_json_value(row["failure_state"]),
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
        participating_agent_refs=_collection_value(row["participating_agent_refs"], []),
        state_snapshot_refs=_collection_value(row["state_snapshot_refs"], []),
        checkpoint_refs=_collection_value(row["checkpoint_refs"], []),
        memory_bindings=_collection_value(row["memory_bindings"], []),
        run_ids=_collection_value(row["run_ids"], []),
        active_run_id=row["active_run_id"],
        last_run_id=row["last_run_id"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )
