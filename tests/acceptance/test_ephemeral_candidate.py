"""Prove the ephemeral candidate is the product, and that its evidence is real.

These are the assertions the mock candidate could never make. Every fact below is read
back over real HTTP from the real service: the approval-gated node's execution count
comes from the audit records the deployment published, not from a field a fixture was
written to return, and the post-restart reads hit a process that was actually stopped
and rebuilt against the same database file.

This is the foundation the deployed contract runs on. The contract retarget consumes
`EphemeralCandidate` as its `base_url` and lifecycle controller.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from tests.acceptance.ephemeral import FINISH_NODE, EphemeralCandidate
from tests.service.helpers import TEST_API_KEYS

_SETTLE_DEADLINE_SECONDS = 30.0


def _headers(role: str) -> dict[str, str]:
    return {"X-API-Key": TEST_API_KEYS[role]}


async def _wait_for_status(client: httpx.AsyncClient, run_id: str, expected: str) -> dict:
    """Poll a run to a status, reporting what was actually observed on timeout."""
    deadline = asyncio.get_running_loop().time() + _SETTLE_DEADLINE_SECONDS
    observed: object = None
    while asyncio.get_running_loop().time() < deadline:
        response = await client.get(f"/v1/runs/{run_id}", headers=_headers("operator"))
        response.raise_for_status()
        body = response.json()
        observed = body.get("status")
        if observed == expected:
            return body
        await asyncio.sleep(0.05)
    raise AssertionError(f"run {run_id} never reached {expected!r}; last observed {observed!r}")


async def _finish_node_executions(client: httpx.AsyncClient, run_id: str) -> int:
    """Count completed executions of the approval-gated node as the API reports them."""
    response = await client.get(f"/v1/runs/{run_id}/timeline", headers=_headers("reviewer"))
    response.raise_for_status()
    return sum(
        1
        for entry in response.json()["entries"]
        if entry.get("node_id") == FINISH_NODE and entry.get("status") == "completed"
    )


@pytest.fixture
async def candidate(tmp_path: Path):
    instance = EphemeralCandidate(tmp_path)
    await instance.provision()
    await instance.serve()
    try:
        yield instance
    finally:
        await instance.aclose()


async def test_the_candidate_serves_the_real_application(candidate: EphemeralCandidate) -> None:
    async with httpx.AsyncClient(base_url=candidate.base_url, timeout=10.0) as client:
        ready = await client.get("/health/ready")
        assert ready.status_code == 200
        assert ready.json()["checks"]["database"]["status"] == "ok"

        health = await client.get("/health", headers=_headers("operator"))
        assert health.status_code == 200
        assert health.json()["deployment_ref"] == candidate.deployment_ref

        # Real authentication, not a fixture branch on a header.
        assert (await client.get("/v1/runs")).status_code == 401


async def test_an_approval_gated_node_runs_zero_times_then_exactly_once(
    candidate: EphemeralCandidate,
) -> None:
    async with httpx.AsyncClient(base_url=candidate.base_url, timeout=10.0) as client:
        created = await client.post(
            "/v1/runs",
            json={"input_payload": {"value": 3}},
            headers=_headers("operator"),
        )
        assert created.status_code == 202
        run_id = created.json()["run_id"]

        paused = await _wait_for_status(client, run_id, "paused_for_approval")
        approval_id = paused["approval_paused_state"]["approval_id"]

        # The whole point of an approval gate: nothing downstream has run yet.
        assert await _finish_node_executions(client, run_id) == 0
        assert candidate.finish_runner.call_count == 0

        resolved = await client.post(
            f"/v1/deployments/{candidate.deployment_ref}/approvals/{approval_id}/resolve",
            json={"decision": "edit_and_approve", "edited_payload": {"value": 8}},
            headers=_headers("reviewer"),
        )
        assert resolved.status_code == 200

        completed = await _wait_for_status(client, run_id, "succeeded")
        assert completed["terminal_output"] == {"value": 9}
        assert await _finish_node_executions(client, run_id) == 1
        assert candidate.finish_runner.call_count == 1


async def test_run_approval_and_audit_evidence_survive_a_real_restart(
    candidate: EphemeralCandidate,
) -> None:
    async with httpx.AsyncClient(base_url=candidate.base_url, timeout=10.0) as client:
        created = await client.post(
            "/v1/runs",
            json={"input_payload": {"value": 3}},
            headers=_headers("operator"),
        )
        run_id = created.json()["run_id"]
        paused = await _wait_for_status(client, run_id, "paused_for_approval")
        approval_id = paused["approval_paused_state"]["approval_id"]
        await client.post(
            f"/v1/deployments/{candidate.deployment_ref}/approvals/{approval_id}/resolve",
            json={"decision": "approve"},
            headers=_headers("reviewer"),
        )
        await _wait_for_status(client, run_id, "succeeded")
        before = await _finish_node_executions(client, run_id)
        assert before == 1

    await candidate.restart()
    # A fresh process: anything still observable came out of the database.
    assert candidate.finish_runner.call_count == 0

    async with httpx.AsyncClient(base_url=candidate.base_url, timeout=10.0) as client:
        run = await client.get(f"/v1/runs/{run_id}", headers=_headers("operator"))
        assert run.status_code == 200
        assert run.json()["status"] == "succeeded"

        # The approval endpoints serve the pending queue, which this approval has
        # left. Its durable trace is the audit record the resolution wrote.
        timeline = await client.get(f"/v1/runs/{run_id}/timeline", headers=_headers("reviewer"))
        assert timeline.status_code == 200
        assert any(entry.get("status") == "approval_api" for entry in timeline.json()["entries"])

        # The node did not silently re-execute on the way back up.
        assert await _finish_node_executions(client, run_id) == 1


async def test_a_withdrawn_candidate_stops_answering(candidate: EphemeralCandidate) -> None:
    async with httpx.AsyncClient(base_url=candidate.base_url, timeout=10.0) as client:
        assert (await client.get("/health/ready")).status_code == 200

    await candidate.shutdown()

    async with httpx.AsyncClient(base_url=candidate.base_url, timeout=5.0) as client:
        with pytest.raises(httpx.HTTPError):
            await client.get("/health/ready")
