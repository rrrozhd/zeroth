"""ZER-37 Task 7: the sidecar dispatch carries the staged tree end-to-end.

Pre-fix, ``_run_via_sidecar`` dropped the staged workdir entirely (the request
always said ``/workspace`` with no workspace attached) and shipped UNREWRITTEN
host paths in env and argv -- ``ZEROTH_OUTPUT_FILE``/``ZEROTH_INPUT_FILE``
pointed at a host tempdir that does not exist inside the sidecar's container.
These tests pin the staged dispatch (upload-before-execute ordering, workspace
tar packing, host->container path rewriting, capture write-back) and the
unstaged call's exact legacy request shape.
"""

from __future__ import annotations

import base64
import re
import tarfile
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest

from zeroth.integrations.execution.io import OutputExtractionError
from zeroth.integrations.execution.sandbox import (
    SandboxBackendMode,
    SandboxConfig,
    SandboxEnvironment,
    SandboxManager,
    SandboxPolicyViolationError,
    _pack_workspace_tar,
)
from zeroth.integrations.sandbox.models import (
    SidecarExecuteRequest,
    SidecarExecuteResponse,
)
from zeroth.integrations.sandbox.staging import validate_workspace_id
from zeroth.platform.primitives import OutboundDestinationError


def _response(**overrides: Any) -> SidecarExecuteResponse:
    base: dict[str, Any] = {
        "execution_id": "exec",
        "status": "completed",
        "returncode": 0,
        "stdout": "",
        "stderr": "",
        "duration_seconds": 0.1,
        "timed_out": False,
    }
    base.update(overrides)
    return SidecarExecuteResponse(**base)


class _FakeSidecarClient:
    """Records upload/execute calls in order and replays canned responses."""

    def __init__(
        self,
        *,
        response: SidecarExecuteResponse | None = None,
        upload_error: Exception | None = None,
    ) -> None:
        self.calls: list[tuple[str, str]] = []
        self.uploads: list[tuple[str, bytes]] = []
        self.requests: list[SidecarExecuteRequest] = []
        self._response = response or _response()
        self._upload_error = upload_error

    async def upload_workspace(self, workspace_id: str, tar_content: bytes) -> None:
        self.calls.append(("upload", workspace_id))
        if self._upload_error is not None:
            raise self._upload_error
        self.uploads.append((workspace_id, bytes(tar_content)))

    async def execute(self, request: SidecarExecuteRequest) -> SidecarExecuteResponse:
        self.calls.append(("execute", request.execution_id))
        self.requests.append(request)
        return self._response


def _manager(client: _FakeSidecarClient) -> SandboxManager:
    return SandboxManager(
        config=SandboxConfig(backend=SandboxBackendMode.SIDECAR),
        sidecar_client=client,
        base_env={},
    )


def _dispatch(
    client: _FakeSidecarClient,
    sandbox_root: Path,
    *,
    command: tuple[str, ...] = ("echo", "ok"),
    env: dict[str, str] | None = None,
    relative_cwd: object = None,
    read_only_paths: tuple[str, ...] = (),
    capture_output_file: str | None = None,
):
    return _manager(client)._run_via_sidecar(
        command=list(command),
        input_text=None,
        timeout_seconds=5.0,
        environment=SandboxEnvironment(cache_key="cache", variables=dict(env or {})),
        sandbox_root=sandbox_root,
        relative_cwd=relative_cwd,
        read_only_paths=read_only_paths,
        capture_output_file=capture_output_file,
    )


def _tar_members(payload: bytes) -> list[tarfile.TarInfo]:
    with tarfile.open(fileobj=BytesIO(payload), mode="r:") as archive:
        return archive.getmembers()


# --- upload-before-execute ordering ---


def test_upload_completes_before_execute_and_workspace_id_is_valid(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("print('hi')", encoding="utf-8")
    client = _FakeSidecarClient()

    _dispatch(client, tmp_path)

    assert [kind for kind, _ in client.calls] == ["upload", "execute"]
    request = client.requests[0]
    assert request.workspace_id is not None
    # uuid4 hex passes the sidecar's docker-volume-safe charset rule.
    assert re.fullmatch(r"[0-9a-f]{32}", request.workspace_id)
    assert validate_workspace_id(request.workspace_id) == request.workspace_id
    # the id the tar was uploaded under is the id the request names
    assert client.uploads[0][0] == request.workspace_id


def test_upload_failure_prevents_execute(tmp_path: Path) -> None:
    client = _FakeSidecarClient(upload_error=RuntimeError("staging channel down"))

    with pytest.raises(RuntimeError, match="staging channel down"):
        _dispatch(client, tmp_path)

    assert [kind for kind, _ in client.calls] == ["upload"]
    assert client.requests == []


# --- working directory ---


@pytest.mark.parametrize(
    ("relative_cwd", "expected"),
    [
        (None, "/workspace"),
        (Path("."), "/workspace"),
        (Path("job"), "/workspace/job"),
        ("job/sub", "/workspace/job/sub"),
    ],
)
def test_working_directory_reflects_the_staged_relative_cwd(
    tmp_path: Path, relative_cwd: object, expected: str
) -> None:
    client = _FakeSidecarClient()

    _dispatch(client, tmp_path, relative_cwd=relative_cwd)

    assert client.requests[0].working_directory == expected


# --- host->container rewriting ---


def test_env_and_argv_host_paths_are_rewritten_to_container_form(tmp_path: Path) -> None:
    client = _FakeSidecarClient()
    env = {
        "ZEROTH_OUTPUT_FILE": str(tmp_path / "job" / "zeroth-output.json"),
        "ZEROTH_INPUT_FILE": str(tmp_path / "job" / "zeroth-input.json"),
        "UNRELATED": "/etc/hosts",
    }
    command = ("python", str(tmp_path / "main.py"), "--flag")

    _dispatch(client, tmp_path, command=command, env=env, relative_cwd=Path("job"))

    request = client.requests[0]
    assert request.environment["ZEROTH_OUTPUT_FILE"] == "/workspace/job/zeroth-output.json"
    assert request.environment["ZEROTH_INPUT_FILE"] == "/workspace/job/zeroth-input.json"
    assert request.environment["UNRELATED"] == "/etc/hosts"
    assert request.command == ["python", "/workspace/main.py", "--flag"]


# --- workspace tar packing ---


def test_workspace_tar_preserves_exec_bits_and_orders_members(tmp_path: Path) -> None:
    (tmp_path / "b.txt").write_text("data", encoding="utf-8")
    tool = tmp_path / "tool.sh"
    tool.write_text("#!/bin/sh\n", encoding="utf-8")
    tool.chmod(0o755)
    (tmp_path / "job").mkdir()
    (tmp_path / "job" / "a.txt").write_text("nested", encoding="utf-8")
    client = _FakeSidecarClient()

    _dispatch(client, tmp_path)

    members = _tar_members(client.uploads[0][1])
    # deterministic order: dirs then files at each level, sorted, parents first
    assert [member.name for member in members] == ["job", "b.txt", "tool.sh", "job/a.txt"]
    by_name = {member.name: member for member in members}
    assert by_name["job"].isdir()
    assert by_name["tool.sh"].isreg()
    assert by_name["tool.sh"].mode & 0o111, "exec bit must survive packing"
    assert not by_name["b.txt"].mode & 0o111
    assert by_name["job/a.txt"].size == len(b"nested")


def test_workspace_tar_packing_is_deterministic(tmp_path: Path) -> None:
    (tmp_path / "z.txt").write_text("z", encoding="utf-8")
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "dir").mkdir()
    (tmp_path / "dir" / "file").write_text("x", encoding="utf-8")

    assert _pack_workspace_tar(tmp_path) == _pack_workspace_tar(tmp_path)


def test_workspace_with_symlink_is_refused_before_any_sidecar_call(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (tmp_path / "link.txt").symlink_to(outside)
    client = _FakeSidecarClient()

    with pytest.raises(SandboxPolicyViolationError, match="non-regular"):
        _dispatch(client, tmp_path)

    assert client.calls == []


# --- capture flow ---


def test_capture_response_writes_the_file_under_the_sandbox_root(tmp_path: Path) -> None:
    payload = b'{"answer": "sidecar", "score": 2}'
    client = _FakeSidecarClient(
        response=_response(output_file_b64=base64.b64encode(payload).decode())
    )
    (tmp_path / "job").mkdir()

    _dispatch(
        client,
        tmp_path,
        relative_cwd=Path("job"),
        capture_output_file="job/zeroth-output.json",
    )

    assert (tmp_path / "job" / "zeroth-output.json").read_bytes() == payload
    assert client.requests[0].capture_output_file == "job/zeroth-output.json"


def test_capture_traversal_path_is_refused_by_confine_path(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    client = _FakeSidecarClient(
        response=_response(output_file_b64=base64.b64encode(b"{}").decode())
    )

    with pytest.raises(OutboundDestinationError):
        _dispatch(client, root, capture_output_file="../escape.json")

    # refused before any request left the process, and nothing landed outside
    assert client.calls == []
    assert not (tmp_path / "escape.json").exists()


def test_truncated_capture_raises_and_fabricates_no_file(tmp_path: Path) -> None:
    client = _FakeSidecarClient(
        response=_response(output_file_b64=None, output_file_truncated=True)
    )

    with pytest.raises(OutputExtractionError, match="byte cap"):
        _dispatch(client, tmp_path, capture_output_file="zeroth-output.json")

    assert not (tmp_path / "zeroth-output.json").exists()


# --- read-only paths pass-through ---


def test_read_only_paths_are_forwarded_on_the_request(tmp_path: Path) -> None:
    (tmp_path / "vendor").mkdir()
    client = _FakeSidecarClient()

    _dispatch(client, tmp_path, read_only_paths=("vendor",))

    assert client.requests[0].read_only_paths == ["vendor"]


# --- backward compatibility: the unstaged call keeps today's request shape ---


def test_dispatch_without_sandbox_root_matches_the_legacy_request_shape() -> None:
    client = _FakeSidecarClient()
    manager = _manager(client)
    host_env = {"ZEROTH_OUTPUT_FILE": "/tmp/host/zeroth-output.json"}

    manager._run_via_sidecar(
        command=["python", "-c", "pass"],
        input_text="stdin",
        timeout_seconds=5.0,
        environment=SandboxEnvironment(cache_key="cache", variables=dict(host_env)),
    )

    assert [kind for kind, _ in client.calls] == ["execute"]
    request = client.requests[0]
    expected = SidecarExecuteRequest(
        execution_id=request.execution_id,
        image="zeroth-sandbox",
        command=["python", "-c", "pass"],
        input_text="stdin",
        timeout_seconds=5.0,
        environment=dict(host_env),
    ).model_dump()
    assert request.model_dump() == expected
    # spelled out: no workspace, legacy working_directory, env NOT rewritten
    assert request.workspace_id is None
    assert request.working_directory == "/workspace"
    assert request.capture_output_file is None
    assert request.read_only_paths == []
    assert request.environment == host_env


# --- SandboxManager.run threads the staged tree it creates ---


def test_manager_run_threads_the_staged_workspace_to_the_sidecar() -> None:
    client = _FakeSidecarClient()
    manager = _manager(client)

    result = manager.run(["echo", "ok"], working_directory="job")

    assert result.backend == "sidecar"
    assert [kind for kind, _ in client.calls] == ["upload", "execute"]
    request = client.requests[0]
    assert request.workspace_id is not None
    assert request.working_directory == "/workspace/job"
    # the created workdir travels in the staged tree
    assert "job" in [member.name for member in _tar_members(client.uploads[0][1])]
