"""Owned Docker workloads must terminate before sandbox cleanup returns."""

from __future__ import annotations

import os
import subprocess
from io import BytesIO
from uuid import uuid4

import pytest

from zeroth.integrations.execution.sandbox import (
    DockerSandboxConfig,
    SandboxBackendMode,
    SandboxBackendUnavailableError,
    SandboxConfig,
    SandboxManager,
    SandboxStrictnessMode,
    SandboxTimeoutError,
)


class Process:
    def __init__(self, timeout: bool = False) -> None:
        self.stdout = BytesIO(b"partial")
        self.stderr = BytesIO()
        self.stdin = None
        self.returncode = 0
        self.timeout = timeout
        self.killed = False

    def wait(self, timeout: float | None = None) -> int:
        if self.timeout and not self.killed:
            raise subprocess.TimeoutExpired("docker start", timeout)
        return self.returncode

    def kill(self) -> None:
        self.killed = True


def manager_for(
    calls, *, timeout=False, create_failure=False, start_failure=False, cleanup_failure=False
):
    process = Process(timeout)

    def runner(command, **kwargs):
        calls.append((list(command), kwargs))
        failed = (command[1] == "create" and create_failure) or (
            command[1] == "rm" and cleanup_failure
        )
        return subprocess.CompletedProcess(command, int(failed), "python:3.12\n", "")

    def start(command, **kwargs):
        calls.append((list(command), kwargs))
        if start_failure:
            raise OSError("start unavailable")
        return process

    return SandboxManager(
        config=SandboxConfig(
            backend=SandboxBackendMode.DOCKER,
            strictness_mode=SandboxStrictnessMode.STRICT,
            docker=DockerSandboxConfig(container_name="provisioned-template"),
        ),
        command_runner=runner,
        process_factory=start,
        container_inspector=lambda _: True,
    ), process


def assert_owned_cleanup(calls):
    create = next(cmd for cmd, _ in calls if cmd[1] == "create")
    name = create[create.index("--name") + 1]
    assert name != "provisioned-template"
    assert name.startswith("zeroth-sandbox-run-")
    remove, options = next((cmd, options) for cmd, options in calls if cmd[1] == "rm")
    assert remove == ["docker", "rm", "--force", name]
    assert 0 < options["timeout"] <= 10
    return name


def test_timeout_removes_owned_container_after_killing_attached_client() -> None:
    calls = []
    manager, process = manager_for(calls, timeout=True)
    with pytest.raises(SandboxTimeoutError):
        manager.run(["sleep", "30"], timeout_seconds=1)
    assert process.killed
    name = assert_owned_cleanup(calls)
    assert next(cmd for cmd, _ in calls if cmd[1] == "start")[-1] == name


def test_success_removes_owned_container() -> None:
    calls = []
    manager, _ = manager_for(calls)
    assert manager.run(["true"]).returncode == 0
    assert_owned_cleanup(calls)


def test_attach_launch_failure_still_removes_created_container() -> None:
    calls = []
    manager, _ = manager_for(calls, start_failure=True)
    with pytest.raises(OSError, match="start unavailable"):
        manager.run(["true"])
    assert_owned_cleanup(calls)


def test_cleanup_failure_is_not_reported_as_success() -> None:
    calls = []
    manager, _ = manager_for(calls, cleanup_failure=True)
    with pytest.raises(SandboxBackendUnavailableError, match="cleanup"):
        manager.run(["true"])


def test_failed_creation_never_starts_or_removes_an_unowned_container() -> None:
    calls = []
    manager, _ = manager_for(calls, create_failure=True)
    with pytest.raises(SandboxBackendUnavailableError, match="creation"):
        manager.run(["true"])
    assert all(cmd[1] not in {"start", "rm"} for cmd, _ in calls)


def test_creation_consumes_deadline_and_cleans_up_before_start(monkeypatch) -> None:
    calls = []
    manager, _ = manager_for(calls)
    ticks = iter([0.0, 2.0])
    monkeypatch.setattr(
        "zeroth.integrations.execution.sandbox.time.perf_counter", lambda: next(ticks)
    )
    with pytest.raises(SandboxTimeoutError):
        manager.run(["true"], timeout_seconds=1)
    assert_owned_cleanup(calls)
    assert all(cmd[1] != "start" for cmd, _ in calls)


def test_timeout_cleanup_failure_reports_unavailable_backend() -> None:
    calls = []
    manager, process = manager_for(calls, timeout=True, cleanup_failure=True)
    with pytest.raises(SandboxBackendUnavailableError, match="cleanup"):
        manager.run(["sleep", "30"], timeout_seconds=1)
    assert process.killed
    assert_owned_cleanup(calls)


def test_real_docker_timeout_removes_started_workload() -> None:
    """The release gate opts in; a configured but unavailable daemon must fail."""
    image = os.environ.get("ZEROTH_TEST_DOCKER_IMAGE")
    if not image:
        pytest.skip("set ZEROTH_TEST_DOCKER_IMAGE to run the real Docker lifecycle check")
    token = uuid4().hex
    tag = f"zeroth-lifecycle-test:{token}"
    template = f"zeroth-lifecycle-template-{token}"

    def docker(*args: str) -> str:
        return subprocess.check_output(["docker", *args], text=True, timeout=30).strip()

    tagged = False
    try:
        docker("tag", image, tag)
        tagged = True
        template_id = docker("run", "-d", "--name", template, tag, "sleep", "120")
        manager = SandboxManager(
            config=SandboxConfig(
                backend=SandboxBackendMode.DOCKER,
                strictness_mode=SandboxStrictnessMode.STRICT,
                docker=DockerSandboxConfig(container_name=template),
            )
        )
        # Warm the execution path and verify attached stdin and nonzero status.
        result = manager.run(
            ["python", "-c", "import sys; print(sys.stdin.read()); sys.exit(7)"],
            input_text="roundtrip",
            timeout_seconds=10,
        )
        assert result.stdout.strip() == "roundtrip"
        assert result.returncode == 7
        with pytest.raises(SandboxTimeoutError) as caught:
            manager.run(
                ["python", "-c", "import time; print('started', flush=True); time.sleep(60)"],
                timeout_seconds=3,
            )
        # Positive control proves a workload started before the deadline expired.
        assert "started" in caught.value.stdout
        assert docker("ps", "-aq", "--no-trunc", "--filter", f"ancestor={tag}").split() == [
            template_id
        ]
        assert docker("inspect", "-f", "{{.State.Running}}", template) == "true"
    finally:
        if tagged:
            # Only remove containers whose exact configured image is our unique tag.
            for container_id in docker("ps", "-aq", "--filter", f"ancestor={tag}").split():
                if docker("inspect", "-f", "{{.Config.Image}}", container_id) == tag:
                    docker("rm", "--force", container_id)
            docker("image", "rm", tag)


@pytest.mark.parametrize("error_type", [KeyboardInterrupt, OSError])
def test_interrupted_wait_reaps_client_and_removes_workload(monkeypatch, error_type) -> None:
    calls = []
    manager, process = manager_for(calls)
    waits = []
    error = error_type("interrupted wait")

    def interrupted_wait(timeout=None):
        waits.append(timeout)
        if len(waits) == 1:
            raise error
        return 0

    monkeypatch.setattr(process, "wait", interrupted_wait)
    with pytest.raises(error_type) as caught:
        manager.run(["sleep", "30"])
    assert caught.value is error
    assert_owned_cleanup(calls)
    assert process.killed
    assert len(waits) == 2
