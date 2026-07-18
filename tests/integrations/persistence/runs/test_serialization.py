"""Characterization tests for run and thread row serialization.

``zeroth.integrations.persistence.runs.serialization`` owns the pure
translation between database rows and the canonical run domain models. It
holds no database reference and opens no transactions, so these tests pin the
conversion behaviour directly.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime

from zeroth.core.storage.json import to_json_value
from zeroth.integrations.persistence.runs import serialization
from zeroth.runtime.runs import (
    Run,
    RunConditionResult,
    RunHistoryEntry,
    RunStatus,
    Thread,
    ThreadMemoryBinding,
    ThreadStatus,
)


def test_package_imports_in_a_cold_interpreter() -> None:
    """The persistence package must import without ``zeroth.core`` warmed first.

    This runs in a subprocess on purpose. ``tests/conftest.py`` imports
    ``zeroth.core.service.bootstrap`` at collection time, so by the time any
    in-process test runs ``zeroth.core`` is already in ``sys.modules`` and a
    circular import between this package and ``zeroth.core`` is invisible.
    The extraction only works while ``zeroth.core.runs.__init__`` resolves the
    repositories lazily, and only a cold interpreter can prove that.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from zeroth.integrations.persistence.runs import serialization",
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, (
        f"zeroth.integrations.persistence.runs.serialization is not "
        f"cold-importable:\n{result.stderr}"
    )


def test_legacy_repository_module_still_imports_in_a_cold_interpreter() -> None:
    """The legacy module must keep importing after the implementation moves.

    ``zeroth.core.runs.repository`` remains a protected import location, and
    the extracted package imports the run models back out of the runtime
    domain. Both directions have to resolve from a cold start or the shim has
    closed a cycle that the warm suite cannot see.
    """
    result = subprocess.run(
        [sys.executable, "-c", "import zeroth.core.runs.repository"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, (
        f"zeroth.core.runs.repository is not cold-importable:\n{result.stderr}"
    )


def _run_row(**overrides: object) -> dict[str, object]:
    """Build a raw ``runs`` row with every column the converter reads."""
    row: dict[str, object] = {
        "run_id": "run-1",
        "checkpoint_id": "checkpoint-1",
        "parent_checkpoint_id": None,
        "epoch": 3,
        "workflow_name": "demo",
        "status": RunStatus.RUNNING.value,
        "current_step": "node-a",
        "completed_steps": to_json_value(["node-a"]),
        "artifacts": to_json_value({"key": "value"}),
        "channels": to_json_value({"main": ["hello"]}),
        "pending_approval": None,
        "pending_interrupt_id": None,
        "started_at": datetime(2026, 7, 18, 12, 0, tzinfo=UTC).isoformat(),
        "updated_at": datetime(2026, 7, 18, 12, 30, tzinfo=UTC).isoformat(),
        "error": None,
        "metadata": to_json_value({"origin": "test"}),
        "graph_version_ref": "graph-1",
        "deployment_ref": "deployment-1",
        "tenant_id": "tenant-1",
        "workspace_id": "workspace-1",
        "submitted_by": None,
        "thread_id": "thread-1",
        "current_node_ids": to_json_value(["node-a"]),
        "pending_node_ids": to_json_value([]),
        "execution_history": to_json_value(
            [RunHistoryEntry(node_id="node-a", status="completed").model_dump(mode="json")]
        ),
        "node_visit_counts": to_json_value({"node-a": 1}),
        "condition_results": to_json_value(
            [
                RunConditionResult(
                    condition_id="cond-1",
                    selected_edge_id="node-b",
                    matched=True,
                ).model_dump(mode="json")
            ]
        ),
        "audit_refs": to_json_value(["audit-1"]),
        "final_output": None,
        "failure_state": None,
    }
    row.update(overrides)
    return row


def _thread_row(**overrides: object) -> dict[str, object]:
    """Build a raw ``threads`` row with every column the converter reads."""
    row: dict[str, object] = {
        "thread_id": "thread-1",
        "graph_version_ref": "graph-1",
        "deployment_ref": "deployment-1",
        "tenant_id": "tenant-1",
        "workspace_id": "workspace-1",
        "status": ThreadStatus.ACTIVE.value,
        "participating_agent_refs": to_json_value(["agent-1"]),
        "state_snapshot_refs": to_json_value(["checkpoint-1"]),
        "checkpoint_refs": to_json_value(["checkpoint-1"]),
        "memory_bindings": to_json_value(
            [
                ThreadMemoryBinding(connector_id="conn-1", instance_id="memory-1").model_dump(
                    mode="json"
                )
            ]
        ),
        "run_ids": to_json_value(["run-1"]),
        "active_run_id": "run-1",
        "last_run_id": "run-1",
        "created_at": datetime(2026, 7, 18, 12, 0, tzinfo=UTC).isoformat(),
        "updated_at": datetime(2026, 7, 18, 12, 30, tzinfo=UTC).isoformat(),
    }
    row.update(overrides)
    return row


def test_row_to_run_rebuilds_every_persisted_field() -> None:
    """A stored row converts back into the run it was written from."""
    run = serialization.row_to_run(_run_row())

    assert isinstance(run, Run)
    assert run.run_id == "run-1"
    assert run.status is RunStatus.RUNNING
    assert run.epoch == 3
    assert run.completed_steps == ["node-a"]
    assert run.artifacts == {"key": "value"}
    assert run.metadata == {"origin": "test"}
    assert run.thread_id == "thread-1"
    assert run.started_at == datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
    assert run.updated_at == datetime(2026, 7, 18, 12, 30, tzinfo=UTC)
    assert [entry.node_id for entry in run.execution_history] == ["node-a"]
    assert [result.condition_id for result in run.condition_results] == ["cond-1"]
    assert run.audit_refs == ["audit-1"]


def test_row_to_run_defaults_a_null_tenant_to_the_default_tenant() -> None:
    """Rows written before tenancy existed still load under the default tenant."""
    run = serialization.row_to_run(_run_row(tenant_id=None))

    assert run.tenant_id == "default"


def test_row_to_thread_rebuilds_every_persisted_field() -> None:
    """A stored thread row converts back into the thread it was written from."""
    thread = serialization.row_to_thread(_thread_row())

    assert isinstance(thread, Thread)
    assert thread.thread_id == "thread-1"
    assert thread.status is ThreadStatus.ACTIVE
    assert thread.participating_agent_refs == ["agent-1"]
    assert thread.state_snapshot_refs == ["checkpoint-1"]
    assert thread.checkpoint_refs == ["checkpoint-1"]
    assert [binding.instance_id for binding in thread.memory_bindings] == ["memory-1"]
    assert thread.run_ids == ["run-1"]
    assert thread.active_run_id == "run-1"
    assert thread.created_at == datetime(2026, 7, 18, 12, 0, tzinfo=UTC)


def test_row_to_thread_defaults_a_null_tenant_to_the_default_tenant() -> None:
    """Thread rows written before tenancy existed load under the default tenant."""
    thread = serialization.row_to_thread(_thread_row(tenant_id=None))

    assert thread.tenant_id == "default"


def test_dump_model_returns_none_for_a_missing_value() -> None:
    """Optional model columns stay NULL rather than serializing ``"null"``."""
    assert serialization.dump_model(None) is None


def test_dump_model_serializes_a_pydantic_model() -> None:
    """Model-valued columns serialize through the shared JSON encoder."""
    dumped = serialization.dump_model(
        ThreadMemoryBinding(connector_id="conn-1", instance_id="memory-1")
    )

    assert dumped is not None
    assert "memory-1" in dumped


def test_dump_list_serializes_each_model_in_json_mode() -> None:
    """List columns serialize their members in JSON mode, not Python mode."""
    dumped = serialization.dump_list([RunHistoryEntry(node_id="node-a", status="completed")])

    assert "node-a" in dumped
    assert dumped.startswith("[")
