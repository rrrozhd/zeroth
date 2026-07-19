"""Run and thread models for persisted execution state.

These Pydantic models represent the data that gets saved to the database
when a workflow runs. They track which nodes have executed, what happened
at each step, and the overall status of the run.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Re-exported as zeroth.core.runs API: the conditions contracts own the
# evaluation-outcome vocabulary; the run surface republishes it.
from zeroth.contracts.conditions.models import RunConditionResult as RunConditionResult
from zeroth.contracts.governed import RunState
from zeroth.contracts.governed import RunStatus as RunStatus  # re-exported as zeroth.core.runs API
from zeroth.core.identity import ActorIdentity
from zeroth.platform.primitives import utc_now


def _new_id() -> str:
    """Generate a new random hex ID using UUID4."""
    return uuid4().hex


class ThreadStatus(StrEnum):
    """Lifecycle states for a thread.

    A thread starts as ACTIVE, moves to COMPLETED when all its work is done,
    and can be ARCHIVED when it's no longer needed but you want to keep it.
    """

    ACTIVE = "active"
    COMPLETED = "completed"
    ARCHIVED = "archived"


class RunHistoryEntry(BaseModel):
    """A record of one node's execution within a run.

    Each time a node runs (or retries), a new entry is added to the run's
    execution history. This captures what went in, what came out, and when
    it happened.
    """

    model_config = ConfigDict(extra="forbid")

    node_id: str
    status: str
    attempt: int = 1
    input_snapshot: dict[str, Any] = Field(default_factory=dict)
    output_snapshot: dict[str, Any] = Field(default_factory=dict)
    audit_ref: str | None = None
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    # Per-node cost (USD) promoted from the node's audit record. Lets
    # ``_sum_run_cost`` / ``_sum_audit_cost`` aggregate a run's spend from its
    # own history — the basis for the local per-run cost ceiling. ``None`` when
    # no cost estimator populated a cost for the node.
    cost_usd: float | None = None


class RunFailureState(BaseModel):
    """Details about why a run failed.

    When a run ends in an error, this stores the reason and any extra details
    so you can figure out what went wrong.
    """

    model_config = ConfigDict(extra="forbid")

    reason: str
    message: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class ThreadMemoryBinding(BaseModel):
    """A reference linking a thread to a memory instance.

    Threads can have memory (like a scratchpad) attached to them.
    This binding says which memory connector and instance to use.
    """

    model_config = ConfigDict(extra="forbid")

    connector_id: str
    instance_id: str
    scope: str | None = None


class Run(RunState):
    """A single execution of a graph (workflow).

    Extends the vendored RunState with Zeroth-specific fields like execution
    history, node visit counts, and condition results.  Each run belongs
    to a thread and tracks its progress from start to finish.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    run_id: str = Field(default_factory=_new_id)
    workflow_name: str = ""
    graph_version_ref: str
    deployment_ref: str
    tenant_id: str = "default"
    workspace_id: str | None = None
    parent_run_id: str | None = None
    submitted_by: ActorIdentity | None = None
    current_node_ids: list[str] = Field(default_factory=list)
    pending_node_ids: list[str] = Field(default_factory=list)
    execution_history: list[RunHistoryEntry] = Field(default_factory=list)
    node_visit_counts: dict[str, int] = Field(default_factory=dict)
    condition_results: list[RunConditionResult] = Field(default_factory=list)
    audit_refs: list[str] = Field(default_factory=list)
    final_output: dict[str, Any] | list[Any] | str | int | float | bool | None = None
    failure_state: RunFailureState | None = None

    @model_validator(mode="after")
    def _fill_governed_defaults(self) -> Run:
        """Fill in governed RunState base-class fields from Zeroth-specific fields.

        This keeps the two layers in sync automatically: for example, if you
        set current_node_ids, the base RunState current_step field gets updated too.
        """
        if not self.thread_id:
            self.thread_id = self.run_id
        if not self.workflow_name:
            self.workflow_name = self.deployment_ref or self.graph_version_ref
        if self.current_step is None and self.current_node_ids:
            self.current_step = self.current_node_ids[0]
        if not self.completed_steps and self.execution_history:
            self.completed_steps = [entry.node_id for entry in self.execution_history]
        if self.failure_state is not None and self.error is None:
            self.error = self.failure_state.message or self.failure_state.reason
        return self


class Thread(BaseModel):
    """A container that groups related runs together over time.

    Think of a thread like a conversation: each message exchange is a "run",
    and the thread ties them all together.  It also tracks which agents
    participated, any saved checkpoints, and attached memory.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    thread_id: str = Field(default_factory=_new_id)
    graph_version_ref: str
    deployment_ref: str
    tenant_id: str = "default"
    workspace_id: str | None = None
    status: ThreadStatus = ThreadStatus.ACTIVE
    participating_agent_refs: list[str] = Field(default_factory=list)
    state_snapshot_refs: list[str] = Field(default_factory=list)
    checkpoint_refs: list[str] = Field(default_factory=list)
    memory_bindings: list[ThreadMemoryBinding] = Field(default_factory=list)
    run_ids: list[str] = Field(default_factory=list)
    active_run_id: str | None = None
    last_run_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
