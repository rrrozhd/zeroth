"""Durable action-state transitions required by Zeroth Check faults."""

from __future__ import annotations

import sqlite3

import pytest

from zeroth.integrations.langgraph._action_lifecycle import (
    ActionExecutionState,
    SQLiteActionExecutionRepository,
)
from zeroth.integrations.langgraph._tool_errors import ToolGovernanceError
from zeroth.integrations.langgraph._tool_normalize import normalize_tool_action
from zeroth.integrations.langgraph._tool_types import ToolGovernanceContext


CONTEXT = ToolGovernanceContext(
    tenant_id="tenant-a",
    principal_id="principal-a",
    run_id="run-a",
    thread_id="thread-a",
)
ACTION = normalize_tool_action(
    name="charge",
    arguments={"amount": 42},
    context=CONTEXT,
    side_effect="side_effecting",
    tool_call_id="call-a",
)


@pytest.fixture
def repository(tmp_path):
    return SQLiteActionExecutionRepository(tmp_path / "actions.sqlite3")


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        ("complete", ActionExecutionState.COMPLETED),
        ("fail_pre_effect", ActionExecutionState.FAILED),
        ("mark_ambiguous", ActionExecutionState.AMBIGUOUS),
    ],
)
def test_in_flight_transition_table(repository, event, expected) -> None:
    claim = repository.begin_once(ACTION, CONTEXT)
    assert claim.may_execute
    if event == "complete":
        repository.complete(claim, {"receipt": "ok"})
    elif event == "fail_pre_effect":
        repository.fail_pre_effect(claim, RuntimeError("not dispatched"))
    else:
        repository.mark_ambiguous(claim, TimeoutError(), close_claim=True)
    assert repository.records()[0].state is expected


def test_duplicate_marks_ambiguous_but_original_claim_can_complete(repository) -> None:
    original = repository.begin_once(ACTION, CONTEXT)
    duplicate = repository.begin_once(ACTION, CONTEXT)

    assert duplicate.may_execute is False
    assert duplicate.record.state is ActionExecutionState.AMBIGUOUS
    assert duplicate.record.claim_open is True
    completed = repository.complete(original, {"receipt": "first"})

    assert completed.state is ActionExecutionState.COMPLETED
    assert repository.replay_or_raise(repository.begin_once(ACTION, CONTEXT).record) == {
        "receipt": "first"
    }


def test_closed_ambiguous_claim_refuses_late_completion_and_redelivery(repository) -> None:
    claim = repository.begin_once(ACTION, CONTEXT)
    repository.mark_ambiguous(claim, TimeoutError(), close_claim=True)

    with pytest.raises(ToolGovernanceError):
        repository.complete(claim, {"receipt": "late"})
    resumed = repository.begin_once(ACTION, CONTEXT)
    assert resumed.may_execute is False
    assert resumed.record.state is ActionExecutionState.AMBIGUOUS


def test_original_claim_can_confirm_pre_effect_failure_after_duplicate(repository) -> None:
    original = repository.begin_once(ACTION, CONTEXT)
    repository.begin_once(ACTION, CONTEXT)

    failed = repository.fail_pre_effect(original, RuntimeError("validation refused"))
    retry = repository.begin_once(ACTION, CONTEXT)

    assert failed.state is ActionExecutionState.FAILED
    assert retry.may_execute is True


def test_reconciliation_requires_authority_and_rejects_conflicts(repository) -> None:
    claim = repository.begin_once(ACTION, CONTEXT)
    ambiguous = repository.mark_ambiguous(claim, TimeoutError(), close_claim=True)

    with pytest.raises(ToolGovernanceError):
        repository.reconcile_completed(ambiguous.action_key, {"receipt": "r1"}, "")
    evidence = repository.reconcile_completed(
        ambiguous.action_key, {"receipt": "r1"}, "incident-42"
    )
    assert evidence.prior_state is ActionExecutionState.AMBIGUOUS
    assert evidence.new_state is ActionExecutionState.COMPLETED
    assert repository.reconciliations(ambiguous.action_key) == (evidence,)
    with pytest.raises(ToolGovernanceError):
        repository.reconcile_completed(
            ambiguous.action_key, {"receipt": "different"}, "incident-43"
        )


def test_reconciliation_can_assert_verified_no_effect(repository) -> None:
    claim = repository.begin_once(ACTION, CONTEXT)
    ambiguous = repository.mark_ambiguous(claim, RuntimeError(), close_claim=True)

    evidence = repository.reconcile_no_effect(ambiguous.action_key, "incident-44")

    assert evidence.new_state is ActionExecutionState.FAILED
    retry = repository.begin_once(ACTION, CONTEXT)
    assert retry.may_execute is True
    assert retry.record.attempts == 2


def test_non_json_completion_does_not_commit(repository) -> None:
    claim = repository.begin_once(ACTION, CONTEXT)
    with pytest.raises(ToolGovernanceError):
        repository.complete(claim, object())
    assert repository.records()[0].state is ActionExecutionState.IN_FLIGHT


def test_two_repository_instances_observe_closed_ambiguity(tmp_path) -> None:
    path = tmp_path / "shared.sqlite3"
    first = SQLiteActionExecutionRepository(path)
    second = SQLiteActionExecutionRepository(path)
    claim = first.begin_once(ACTION, CONTEXT)

    first.mark_ambiguous(claim, TimeoutError(), close_claim=True)
    observed = second.begin_once(ACTION, CONTEXT)

    assert observed.may_execute is False
    assert observed.record.state is ActionExecutionState.AMBIGUOUS


def test_existing_action_table_is_migrated_conservatively(tmp_path) -> None:
    path = tmp_path / "legacy-actions.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """CREATE TABLE langgraph_action_executions (
            action_key TEXT PRIMARY KEY,
            identity_json TEXT NOT NULL,
            state TEXT NOT NULL,
            result_json TEXT,
            result_available INTEGER NOT NULL DEFAULT 0,
            error_type TEXT,
            attempts INTEGER NOT NULL DEFAULT 1,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
            )"""
        )

    repository = SQLiteActionExecutionRepository(path)
    claim = repository.begin_once(ACTION, CONTEXT)

    assert claim.may_execute is True
    assert claim.record.claim_open is True
