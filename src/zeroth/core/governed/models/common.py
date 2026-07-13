from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Union


JSONValue = Union[Dict[str, Any], List[Any], str, int, float, bool, None]

END_STEP = "__END__"


class RunStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    WAITING_INTERRUPT = "WAITING_INTERRUPT"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class DeterminismMode(str, Enum):
    STRICT = "STRICT"
    RULE_BASED = "RULE_BASED"
    BOUNDED_ROUTING = "BOUNDED_ROUTING"


class EventType(str, Enum):
    RUN_STARTED = "run_started"
    CHECKPOINT_WRITTEN = "checkpoint_written"
    CHECKPOINT_RESTORED = "checkpoint_restored"
    STEP_ENTERED = "step_entered"
    POLICY_CHECKED = "policy_checked"
    POLICY_DENIED = "policy_denied"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_GRANTED = "approval_granted"
    APPROVAL_REJECTED = "approval_rejected"
    INTERRUPT_REQUESTED = "interrupt_requested"
    INTERRUPT_RESOLVED = "interrupt_resolved"
    INTERRUPT_EXPIRED = "interrupt_expired"
    INTERRUPT_REJECTED_EPOCH = "interrupt_rejected_epoch"
    TOOL_EXECUTION_STARTED = "tool_execution_started"
    TOOL_EXECUTION_COMPLETED = "tool_execution_completed"
    TOOL_EXECUTION_FAILED = "tool_execution_failed"
    AGENT_ENTERED = "agent_entered"
    AGENT_COMPLETED = "agent_completed"
    AGENT_FAILED = "agent_failed"
    AGENT_HANDOFF_PROPOSED = "agent_handoff_proposed"
    AGENT_HANDOFF_ACCEPTED = "agent_handoff_accepted"
    AGENT_HANDOFF_REJECTED = "agent_handoff_rejected"
    AGENT_TOOL_CALL_STARTED = "agent_tool_call_started"
    AGENT_TOOL_CALL_COMPLETED = "agent_tool_call_completed"
    AGENT_TOOL_CALL_FAILED = "agent_tool_call_failed"
    TRANSITION_CHOSEN = "transition_chosen"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"
    THREAD_CREATED = "thread_created"
    THREAD_ACTIVE = "thread_active"
    THREAD_INTERRUPTED = "thread_interrupted"
    THREAD_IDLE = "thread_idle"
    THREAD_ARCHIVED = "thread_archived"
    CAPABILITY_DENIED = "capability_denied"
    MEMORY_READ = "memory_read"
    MEMORY_WRITE = "memory_write"
    MEMORY_DELETE = "memory_delete"
    MEMORY_SEARCH = "memory_search"


def normalize_step_ref(value: str) -> str:
    """Normalize step ref."""
    if value.lower() == "end":
        return END_STEP
    return value
