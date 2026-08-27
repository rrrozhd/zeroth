"""ZER-37 staging fields on the pinned sidecar models.

The three models' constructor signatures are pinned by the frozen
protected-surface fixture, so the new fields are hidden from the reported
signature via the ``__signature__`` exclusion idiom and recorded in
``tests/contracts/test_signature_exclusions.py``. These tests pin both halves:
the fields work, and the reported signature did not move.
"""

from __future__ import annotations

import inspect

import pytest
from pydantic import ValidationError

from zeroth.integrations.sandbox.models import (
    SidecarExecuteRequest,
    SidecarExecuteResponse,
    SidecarStatusResponse,
    validate_workspace_relative_path,
)

PINNED_REQUEST_PARAMETERS = {
    "execution_id",
    "image",
    "command",
    "input_text",
    "timeout_seconds",
    "environment",
    "working_directory",
    "cpu_cores",
    "memory_mb",
    "max_processes",
    "network_access",
}

PINNED_RESPONSE_PARAMETERS = {
    "execution_id",
    "status",
    "returncode",
    "stdout",
    "stderr",
    "duration_seconds",
    "timed_out",
    "stdout_truncated",
    "stderr_truncated",
}


def test_request_constructor_signature_is_unchanged() -> None:
    assert set(inspect.signature(SidecarExecuteRequest).parameters) == (
        PINNED_REQUEST_PARAMETERS
    )


@pytest.mark.parametrize("model", [SidecarExecuteResponse, SidecarStatusResponse])
def test_response_constructor_signatures_are_unchanged(model) -> None:
    assert set(inspect.signature(model).parameters) == PINNED_RESPONSE_PARAMETERS


def test_request_staging_fields_round_trip() -> None:
    request = SidecarExecuteRequest.model_validate(
        {
            "execution_id": "exec-1",
            "image": "python:3.12",
            "command": ["python", "main.py"],
            "workspace_id": "ws-1",
            "capture_output_file": "out/result.json",
            "read_only_paths": ["cfg", "data/reference"],
            "working_directory": "/workspace/pkg",
        }
    )

    replayed = SidecarExecuteRequest.model_validate_json(request.model_dump_json())

    assert replayed.workspace_id == "ws-1"
    assert replayed.capture_output_file == "out/result.json"
    assert replayed.read_only_paths == ["cfg", "data/reference"]


def test_request_without_staging_fields_keeps_defaults() -> None:
    request = SidecarExecuteRequest(execution_id="plain", image="python", command=["true"])

    assert request.workspace_id is None
    assert request.capture_output_file is None
    assert request.read_only_paths == []


@pytest.mark.parametrize("model", [SidecarExecuteResponse, SidecarStatusResponse])
def test_response_output_file_fields_round_trip(model) -> None:
    response = model.model_validate(
        {
            "execution_id": "exec-1",
            "status": "completed",
            "output_file_b64": "aGVsbG8=",
            "output_file_truncated": True,
        }
    )

    replayed = model.model_validate_json(response.model_dump_json())

    assert replayed.output_file_b64 == "aGVsbG8="
    assert replayed.output_file_truncated is True


@pytest.mark.parametrize(
    "path",
    ["../escape", "/absolute", "a\\b", "a\x00b", "a:b", "a//b", "a/./b", "a/../b", ""],
)
def test_capture_output_file_rejects_unclean_paths(path: str) -> None:
    # The validator's own message is a fixed template (pydantic's wrapper adds
    # its usual input_value display, as it already does for every pinned field).
    with pytest.raises(ValidationError, match="clean relative POSIX paths"):
        SidecarExecuteRequest(
            execution_id="e",
            image="python",
            command=["true"],
            workspace_id="ws",
            capture_output_file=path,
        )


def test_read_only_paths_reject_traversal_and_overlap() -> None:
    with pytest.raises(ValidationError):
        SidecarExecuteRequest(
            execution_id="e",
            image="python",
            command=["true"],
            workspace_id="ws",
            read_only_paths=["../cfg"],
        )
    with pytest.raises(ValidationError, match="overlap"):
        SidecarExecuteRequest(
            execution_id="e",
            image="python",
            command=["true"],
            workspace_id="ws",
            read_only_paths=["cfg", "cfg/sub"],
        )


def test_staging_fields_require_workspace_id() -> None:
    with pytest.raises(ValidationError, match="workspace_id"):
        SidecarExecuteRequest(
            execution_id="e",
            image="python",
            command=["true"],
            capture_output_file="out.json",
        )
    with pytest.raises(ValidationError, match="workspace_id"):
        SidecarExecuteRequest(
            execution_id="e", image="python", command=["true"], read_only_paths=["cfg"]
        )


def test_working_directory_must_sit_under_workspace_when_staged() -> None:
    accepted = SidecarExecuteRequest(
        execution_id="e",
        image="python",
        command=["true"],
        workspace_id="ws",
        working_directory="/workspace/pkg/sub",
    )
    assert accepted.working_directory == "/workspace/pkg/sub"

    for working_directory in ("/etc", "/workspace/../etc", "/workspaceevil", "/workspace/a:b"):
        with pytest.raises(ValidationError):
            SidecarExecuteRequest(
                execution_id="e",
                image="python",
                command=["true"],
                workspace_id="ws",
                working_directory=working_directory,
            )


def test_arbitrary_working_directory_stays_legal_without_a_workspace() -> None:
    """Pre-ZER-37 requests keep their semantics: no workspace, no new rules."""
    request = SidecarExecuteRequest(
        execution_id="e", image="python", command=["true"], working_directory="/app"
    )

    assert request.working_directory == "/app"


def test_the_lexical_validator_accepts_clean_relative_paths() -> None:
    assert validate_workspace_relative_path("out/result.json") == "out/result.json"
    assert validate_workspace_relative_path("single") == "single"
