"""Settlement follows the API lifecycle, including governed terminations."""

import asyncio
import time
from types import SimpleNamespace

import httpx
import pytest

from tests.load_release.workload_probe import Target, _settle_run
from zeroth.runtime.runs import Run, RunFailureState, RunStatus
from zeroth.service.api.run_api import _public_status


@pytest.mark.parametrize("reason", [None, "dead_letter", "policy_violation", "max_total_steps"])
async def test_every_public_failure_settles_as_failed(reason):
    run = Run(graph_version_ref="audit:v1", deployment_ref="audit", status=RunStatus.FAILED)
    if reason is not None:
        run.failure_state = RunFailureState(reason=reason)
    calls = []

    def respond(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"status": _public_status(run)})

    async with httpx.AsyncClient(
        base_url="https://audit.invalid",
        transport=httpx.MockTransport(respond),
    ) as client:
        target = Target(SimpleNamespace(secrets={"operator": "test-only"}), client)
        # A terminal response requires no polling; this bounds the regression
        # without waiting for the harness's twenty-second settlement deadline.
        try:
            result = await asyncio.wait_for(
                _settle_run(target, "sustained", 1, run.run_id, time.perf_counter()),
                0.1,
            )
        except TimeoutError:
            pytest.fail(f"public terminal status {_public_status(run)} was polled as live")
    assert [event["state"] for event in result] == ["failed"]
    assert calls == [f"/runs/{run.run_id}"]
