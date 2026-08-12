"""Bounds on sidecar timeouts, subprocess waits and captured-output retention.

Three findings on the sandbox sidecar execution path:

* A07-2 -- ``SidecarExecuteRequest.timeout_seconds`` defaulted to ``None``, and
  ``None`` flowed straight into ``asyncio.wait_for``, where it means *wait
  forever*. A body that simply omitted the field bought an unbounded container.
* A07-6 -- every ``docker`` helper invocation (network create/rm, ``info``)
  awaited ``communicate()`` bare. The first of them runs *before* the container's
  own bounded wait, so a wedged daemon hung the request with nothing able to time
  it out.
* A07-7 -- captured stdout/stderr accumulated in ``_executions`` for the life of
  the process. The registry itself must stay -- it doubles as the permanent
  duplicate-execution guard pinned by ``tests/security/test_hostile_workloads.py``
  -- so it is the payload, not the identity, that has to age out.
"""

from __future__ import annotations

import asyncio
import math
import sys
import time
from typing import Any

import pytest

from zeroth.integrations.sandbox.executor import (
    DEFAULT_DOCKER_COMMAND_TIMEOUT_SECONDS,
    SidecarExecutor,
    _resolved_timeout,
)
from zeroth.integrations.sandbox.models import (
    DEFAULT_EXECUTION_TIMEOUT_SECONDS,
    SidecarExecuteRequest,
    SidecarExecuteResponse,
)

#: A child that outlives any deadline under test without ever returning.
_SLEEPER = "import time; time.sleep(30)"

#: Upper bound on how long a *bounded* helper invocation may take. Well above the
#: 0.1s deadline the tests configure and well below the child's own sleep, so the
#: assertion discriminates "the executor timed it out" from "the child finished".
_HANG_BUDGET_SECONDS = 5.0


def _body(**overrides: Any) -> dict[str, Any]:
    """Build a sidecar execute request body with the given overrides."""
    body: dict[str, Any] = {
        "execution_id": "exec-1",
        "image": "python:3.12-slim",
        "command": ["python", "-c", "print(1)"],
    }
    body.update(overrides)
    return body


def _install_spawn_recorder(
    monkeypatch: pytest.MonkeyPatch,
    spawned: list[asyncio.subprocess.Process],
    *,
    argv: tuple[str, ...] | None = None,
) -> None:
    """Record every spawned subprocess, optionally forcing the argv used.

    ``check_health`` builds its own argv (``<binary> info``), so a test that needs
    that probe to hang has to substitute the command rather than pass one in.
    """
    real_exec = asyncio.create_subprocess_exec

    async def _recording_exec(*args: str, **kwargs: Any) -> asyncio.subprocess.Process:
        proc = await real_exec(*(argv if argv is not None else args), **kwargs)
        spawned.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _recording_exec)


def _response(execution_id: str) -> SidecarExecuteResponse:
    """Build a terminal execution response carrying captured output."""
    return SidecarExecuteResponse(
        execution_id=execution_id,
        status="completed",
        returncode=0,
        stdout=f"stdout for {execution_id}",
        stderr=f"stderr for {execution_id}",
        duration_seconds=1.5,
    )


def _seed(executor: SidecarExecutor, count: int) -> list[str]:
    """Populate the execution registry with terminal responses, oldest first."""
    ids = [f"exec-{index}" for index in range(count)]
    for execution_id in ids:
        executor._executions[execution_id] = _response(execution_id)
    return ids


# --- A07-2: an absent timeout must mean a bound, not the absence of one -------


def test_absent_timeout_resolves_to_a_finite_positive_default() -> None:
    """A body that omits the field still reaches ``wait_for`` with a deadline.

    The model keeps ``timeout_seconds: float | None = None`` -- its constructor
    signature is pinned by the frozen protected-surface fixture -- so the bound
    is applied where the value is consumed.
    """
    request = SidecarExecuteRequest.model_validate(_body())
    resolved = _resolved_timeout(request.timeout_seconds)

    assert resolved == DEFAULT_EXECUTION_TIMEOUT_SECONDS
    assert 0 < resolved < math.inf


def test_explicit_none_timeout_resolves_to_the_default() -> None:
    """An explicit ``None`` is bounded rather than passed through as no bound."""
    request = SidecarExecuteRequest.model_validate(_body(timeout_seconds=None))

    assert request.timeout_seconds is None
    assert _resolved_timeout(request.timeout_seconds) == DEFAULT_EXECUTION_TIMEOUT_SECONDS


@pytest.mark.parametrize("value", [0, 0.0, -1.0, -0.001])
def test_non_positive_timeout_is_rejected(value: float) -> None:
    """Zero and negative deadlines are refused where the deadline is applied."""
    request = SidecarExecuteRequest.model_validate(_body(timeout_seconds=value))

    with pytest.raises(ValueError, match="timeout_seconds must be positive"):
        _resolved_timeout(request.timeout_seconds)


def test_explicit_positive_timeout_is_preserved() -> None:
    """A caller-supplied deadline survives validation and resolution unchanged."""
    request = SidecarExecuteRequest.model_validate(_body(timeout_seconds=12.5))

    assert _resolved_timeout(request.timeout_seconds) == 12.5


# --- A07-6: docker helper invocations need a deadline of their own ------------


@pytest.mark.asyncio
async def test_run_cmd_bounds_a_hanging_docker_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """A helper command that never returns trips the docker-command deadline."""
    spawned: list[asyncio.subprocess.Process] = []
    _install_spawn_recorder(monkeypatch, spawned)
    executor = SidecarExecutor(docker_binary=sys.executable)
    executor._docker_command_timeout_seconds = 0.1

    started = time.perf_counter()
    with pytest.raises(TimeoutError):
        await executor._run_cmd(sys.executable, "-c", _SLEEPER)
    elapsed = time.perf_counter() - started

    assert elapsed < _HANG_BUDGET_SECONDS
    assert len(spawned) == 1


@pytest.mark.asyncio
async def test_run_cmd_reaps_the_child_it_gave_up_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """The timed-out child is killed and reaped, not left running."""
    spawned: list[asyncio.subprocess.Process] = []
    _install_spawn_recorder(monkeypatch, spawned)
    executor = SidecarExecutor(docker_binary=sys.executable)
    executor._docker_command_timeout_seconds = 0.1

    with pytest.raises(TimeoutError):
        await executor._run_cmd(sys.executable, "-c", _SLEEPER)

    assert len(spawned) == 1
    assert spawned[0].returncode is not None


@pytest.mark.asyncio
async def test_check_health_bounds_a_hanging_daemon_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """A wedged ``docker info`` reports unhealthy instead of hanging."""
    spawned: list[asyncio.subprocess.Process] = []
    _install_spawn_recorder(monkeypatch, spawned, argv=(sys.executable, "-c", _SLEEPER))
    executor = SidecarExecutor(docker_binary=sys.executable)
    executor._docker_command_timeout_seconds = 0.1

    started = time.perf_counter()
    healthy = await executor.check_health()
    elapsed = time.perf_counter() - started

    assert healthy is False
    assert elapsed < _HANG_BUDGET_SECONDS
    assert len(spawned) == 1
    assert spawned[0].returncode is not None


@pytest.mark.parametrize("value", [0, 0.0, -1.0])
def test_a_non_positive_execution_timeout_is_refused(value: float) -> None:
    """Zero or less is refused rather than read as immediate or infinite.

    The check lives in the resolver rather than on the model, because the
    request model's constructor signature is pinned by the frozen
    protected-surface fixture and a ``Field(gt=0)`` changes what it renders.
    """
    with pytest.raises(ValueError, match="timeout_seconds must be positive"):
        _resolved_timeout(value)


def test_default_docker_command_timeout_is_finite_and_positive() -> None:
    """The out-of-the-box executor carries a real deadline, not ``None``."""
    executor = SidecarExecutor()

    assert executor._docker_command_timeout_seconds == DEFAULT_DOCKER_COMMAND_TIMEOUT_SECONDS
    assert 0 < DEFAULT_DOCKER_COMMAND_TIMEOUT_SECONDS < math.inf


# --- A07-7: payloads age out, identities do not ------------------------------


def test_retirement_keeps_every_execution_id() -> None:
    """Retirement never drops a key: the registry is the replay guard."""
    executor = SidecarExecutor()
    executor._retained_payload_executions = 2
    ids = _seed(executor, 5)

    executor._retire_old_payloads()

    assert list(executor._executions) == ids


def test_retirement_blanks_the_oldest_payloads() -> None:
    """Everything past the retention window loses its captured output."""
    executor = SidecarExecutor()
    executor._retained_payload_executions = 2
    ids = _seed(executor, 5)

    executor._retire_old_payloads()

    for execution_id in ids[:3]:
        retired = executor._executions[execution_id]
        assert retired.stdout == ""
        assert retired.stderr == ""


def test_retirement_keeps_the_newest_payloads() -> None:
    """The newest ``retained_payload_executions`` entries keep their output."""
    executor = SidecarExecutor()
    executor._retained_payload_executions = 2
    ids = _seed(executor, 5)

    executor._retire_old_payloads()

    assert executor._executions[ids[0]].stdout == "", "nothing was retired at all"
    for execution_id in ids[3:]:
        kept = executor._executions[execution_id]
        assert kept.stdout == f"stdout for {execution_id}"
        assert kept.stderr == f"stderr for {execution_id}"


def test_retirement_preserves_terminal_status_and_returncode() -> None:
    """A retired entry still reports how its execution ended."""
    executor = SidecarExecutor()
    executor._retained_payload_executions = 2
    ids = _seed(executor, 5)

    executor._retire_old_payloads()

    for execution_id in ids[:3]:
        retired = executor._executions[execution_id]
        assert retired.stdout == "" and retired.stderr == "", "nothing was retired"
        assert retired.status == "completed"
        assert retired.returncode == 0
        assert retired.duration_seconds == 1.5


def test_retirement_is_a_no_op_below_the_retention_limit() -> None:
    """Nothing is retired while the registry fits inside the window."""
    executor = SidecarExecutor()
    ids = _seed(executor, 3)

    executor._retire_old_payloads()

    for execution_id in ids:
        kept = executor._executions[execution_id]
        assert kept.stdout == f"stdout for {execution_id}"
