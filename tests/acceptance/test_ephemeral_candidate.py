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
import json
from pathlib import Path

import httpx
import pytest

from release.acceptance.config import AcceptanceConfig
from release.acceptance.models import (
    REQUIRED_SCENARIOS,
    AcceptanceContract,
    ScenarioStatus,
)
from release.acceptance.runner import AcceptanceRunner
from release.acceptance.transport import AcceptanceTransport
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
async def governed_candidate(tmp_path: Path):
    """A candidate whose real gateway fronts a real Agent Server on the shell app."""
    instance = EphemeralCandidate(tmp_path, with_agent_server=True)
    await instance.provision()
    await instance.serve()
    try:
        yield instance
    finally:
        await instance.aclose()


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
        # The gated node emits a real artifact, so the terminal output carries its
        # reference rather than the bare value.
        assert completed["terminal_output"]["value"] == 9
        reference = completed["terminal_output"]["artifact"]
        assert reference and reference["key"].startswith(candidate.tenant_id)

        # And the bytes come back through the product's own retrieval route.
        fetched = await client.get(
            f"/v1/artifacts/{reference['key']}", headers=_headers("operator")
        )
        assert fetched.status_code == 200
        assert fetched.content == b"acceptance artifact"

        missing = await client.get(
            "/v1/artifacts/some-other-tenant-artifact", headers=_headers("operator")
        )
        assert missing.status_code == 404
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


# The two legs AC1 names are authoritative for different scenarios. Only what needs a
# live Agent Server behind the gateway belongs to the remote leg, against a real
# deployment, which is where release-gates.json binds `deployed-suite` to the candidate
# image. Everything else the ephemeral leg proves on every change — including
# compatibility, which a live shell Agent Server now settles here rather than remotely,
# and the executable-unit failure path, which needs no Agent Server at all.
#
# What is left needs more than an upstream: gateway_http's 403 is unreachable while the
# PolicyRegistry ships empty, and its 502 needs a transport failure a healthy server
# cannot produce. Those are product questions, not missing fixtures.
AGENT_SERVER_SCENARIOS = frozenset(
    {
        "gateway_websocket",
    }
)


async def test_the_product_contract_runs_against_the_ephemeral_candidate(
    governed_candidate: EphemeralCandidate, tmp_path: Path
) -> None:
    """Run the shipped contract, and pin exactly which scenarios this leg proves.

    Asserting only the overall status would let this pass while silently losing
    coverage. The partition below is exact, so a fourteenth failing scenario — or a
    gateway scenario that quietly stopped being attempted — breaks the test.
    """
    identity = tmp_path / "identity.json"
    identity.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "commit": "a" * 40,
                "package": {"version": "1", "artifacts": {}},
                "image": {"candidate": "sha256:" + "b" * 64},
            }
        ),
        encoding="utf-8",
    )
    config = AcceptanceConfig.model_validate(
        {
            "schema_version": 1,
            "base_url": governed_candidate.base_url,
            "tenant_id": governed_candidate.tenant_id,
            "deployment_ref": governed_candidate.deployment_ref,
            "candidate_identity": str(identity),
            "credentials": {"operator": "OP", "reviewer": "REV", "admin": "ADM"},
            "poll_deadline_seconds": 30,
        }
    ).resolve(
        {
            "OP": TEST_API_KEYS["operator"],
            "REV": TEST_API_KEYS["reviewer"],
            "ADM": TEST_API_KEYS["admin"],
        },
        run_id="ephemeral",
    )
    contract = AcceptanceContract.model_validate(
        json.loads(Path("release/acceptance/contracts/zeroth-v1.json").read_text(encoding="utf-8"))
    )

    async with AcceptanceTransport(config) as transport:
        report = await AcceptanceRunner(
            config, contract, transport, lifecycle=governed_candidate
        ).run()

    results = {item.name: item for item in report.scenarios}
    assert set(results) == set(REQUIRED_SCENARIOS), "every scenario must be attempted and recorded"

    failed = {name for name, item in results.items() if item.status is not ScenarioStatus.PASSED}
    regressed = {name: results[name].detail for name in sorted(failed - AGENT_SERVER_SCENARIOS)}
    # Both directions matter. A scenario that unexpectedly PASSES means the partition
    # is stale and this leg is now proving more than it claims — silently under-claiming
    # coverage is how the remote leg ends up carrying work it no longer needs to.
    newly_passing = sorted(set(AGENT_SERVER_SCENARIOS) - failed)
    assert not regressed, f"scenarios this leg is authoritative for must pass: {regressed}"
    assert not newly_passing, (
        f"these now pass and should leave AGENT_SERVER_SCENARIOS: {newly_passing}"
    )

    # Promotion evidence is bound to the governed_candidate regardless of which leg produced it.
    assert report.candidate_digest == config.candidate_digest
    assert report.image_identity == config.candidate_identity["image"]
    assert report.namespace.startswith(f"{report.tenant_id}-")
    # The tenant is only meaningful because the deployment echoed it back on the run
    # it actually created; see the `runs` scenario's tenant_id assertion.
    assert report.tenant_id == governed_candidate.tenant_id
