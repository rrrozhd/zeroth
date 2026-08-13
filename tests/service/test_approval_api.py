from __future__ import annotations

import asyncio
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from tests.service.helpers import (
    CountingFinishRunner,
    approval_graph,
    approval_resume_graph,
    deploy_service,
    operator_headers,
    reviewer_headers,
    wait_for,
)
from zeroth.contracts.graph import GraphRepository
from zeroth.service.bootstrap import bootstrap_app

#: Statuses a run can still leave on its own. Anything else is terminal.
_NON_TERMINAL = frozenset({"pending", "running", "paused_for_approval"})


def _is_terminal(client, run_id: str) -> bool:
    """True once the run has settled, whatever it settled on."""
    status = client.get(f"/runs/{run_id}", headers=operator_headers()).json()["status"]
    return status not in _NON_TERMINAL


def _describe(client, run_id: str, phase: str):
    """What the run looked like when a wait gave up — see ZER21-AUD-001."""

    def describe() -> str:
        body = client.get(f"/runs/{run_id}", headers=operator_headers()).json()
        return (
            f"phase={phase!r} run {run_id} status={body.get('status')!r} "
            f"failure_state={body.get('failure_state')!r}"
        )

    return describe


def test_approval_wait_description_identifies_phase() -> None:
    client = Mock()
    client.get.return_value.json.return_value = {
        "status": "running",
        "failure_state": None,
    }

    assert "phase='approval_pause'" in _describe(client, "run-1", "approval_pause")()
    assert "phase='terminal_resume'" in _describe(client, "run-1", "terminal_resume")()


async def test_approval_api_queries_pending_approvals_by_id_run_thread_and_scope(sqlite_db) -> None:
    service, _ = await deploy_service(sqlite_db, approval_graph(graph_id="graph-approval-query"))
    app = await bootstrap_app(sqlite_db, deployment_ref=service.deployment.deployment_ref)
    app.state.bootstrap = service

    with TestClient(app) as client:
        create_response = client.post(
            "/runs",
            json={"input_payload": {"value": 7}},
            headers=operator_headers(),
        )
        run_id = create_response.json()["run_id"]
        wait_for(
            lambda: (
                client.get(f"/runs/{run_id}", headers=operator_headers()).json()["status"]
                == "paused_for_approval"
            ),
            describe=_describe(client, run_id, "approval_pause"),
        )

        paused_payload = client.get(f"/runs/{run_id}", headers=operator_headers()).json()
        approval_id = paused_payload["approval_paused_state"]["approval_id"]
        thread_id = paused_payload["thread_id"]

        all_response = client.get(
            f"/deployments/{service.deployment.deployment_ref}/approvals",
            headers=operator_headers(),
        )
        by_run_response = client.get(
            f"/deployments/{service.deployment.deployment_ref}/approvals",
            params={"run_id": run_id},
            headers=operator_headers(),
        )
        by_thread_response = client.get(
            f"/deployments/{service.deployment.deployment_ref}/approvals",
            params={"thread_id": thread_id},
            headers=operator_headers(),
        )
        by_id_response = client.get(
            f"/deployments/{service.deployment.deployment_ref}/approvals/{approval_id}",
            headers=operator_headers(),
        )
        query_by_id_response = client.get(
            f"/deployments/{service.deployment.deployment_ref}/approvals",
            params={"approval_id": approval_id},
            headers=operator_headers(),
        )

    assert all_response.status_code == 200
    assert [item["approval_id"] for item in all_response.json()] == [approval_id]
    assert [item["approval_id"] for item in by_run_response.json()] == [approval_id]
    assert [item["approval_id"] for item in by_thread_response.json()] == [approval_id]
    assert by_id_response.status_code == 200
    assert by_id_response.json()["approval_id"] == approval_id
    assert [item["approval_id"] for item in query_by_id_response.json()] == [approval_id]

    graph_repository = GraphRepository(sqlite_db)
    draft = await graph_repository.clone_published_to_draft(
        service.deployment.graph_id,
        service.deployment.graph_version,
    )
    await graph_repository.save(draft)
    published_v2 = await graph_repository.publish(draft.graph_id, draft.version)
    await service.deployment_service.deploy(
        service.deployment.deployment_ref,
        published_v2.graph_id,
        published_v2.version,
    )
    fresh_app = await bootstrap_app(
        sqlite_db,
        deployment_ref=service.deployment.deployment_ref,
        auth_config=service.auth_config,
    )

    with TestClient(fresh_app) as client:
        missing_response = client.get(
            f"/deployments/{service.deployment.deployment_ref}/approvals/{approval_id}",
            headers=operator_headers(),
        )

    assert missing_response.status_code == 404
    assert missing_response.json() == {"detail": "approval not found"}


@pytest.mark.parametrize(
    ("decision", "edited_payload", "expected_status", "expected_output"),
    [
        ("approve", None, "succeeded", {"value": 4}),
        ("reject", None, "failed", None),
        ("edit_and_approve", {"value": 4}, "succeeded", {"value": 5}),
    ],
)
async def test_approval_api_resolves_all_decisions_and_resumes_when_appropriate(
    sqlite_db,
    decision,
    edited_payload,
    expected_status,
    expected_output,
) -> None:
    service, _ = await deploy_service(
        sqlite_db,
        approval_resume_graph(graph_id=f"graph-approval-{decision}"),
    )
    finish_runner = CountingFinishRunner()
    service.orchestrator.agent_runners["finish-step"] = finish_runner
    app = await bootstrap_app(sqlite_db, deployment_ref=service.deployment.deployment_ref)
    app.state.bootstrap = service

    with TestClient(app) as client:
        create_response = client.post(
            "/runs",
            json={"input_payload": {"value": 3}},
            headers=operator_headers(),
        )
        run_id = create_response.json()["run_id"]
        wait_for(
            lambda: (
                client.get(f"/runs/{run_id}", headers=operator_headers()).json()["status"]
                == "paused_for_approval"
            ),
            describe=_describe(client, run_id, "approval_pause"),
        )
        approval_id = client.get(
            f"/runs/{run_id}",
            headers=operator_headers(),
        ).json()["approval_paused_state"]["approval_id"]

        payload = {"decision": decision}
        if edited_payload is not None:
            payload["edited_payload"] = edited_payload

        response = client.post(
            f"/deployments/{service.deployment.deployment_ref}/approvals/{approval_id}/resolve",
            json=payload,
            headers=reviewer_headers(),
        )
        # Resolving hands the resume to the durable worker; the endpoint only waits for it
        # best-effort (~5 s) before returning whatever the run currently is. So a terminal
        # status in the resolve response is not guaranteed, and asserting one made this
        # test fail under load with a value mismatch rather than a timeout (ZER-21).
        #
        # Wait for the run to become TERMINAL -- not for the expected status. Waiting for
        # the expected value would never be satisfied by a wrong terminal outcome, so the
        # test would burn the whole deadline and then report a timeout instead of the
        # wrong status it actually observed.
        wait_for(
            lambda: _is_terminal(client, run_id),
            describe=_describe(client, run_id, "terminal_resume"),
        )
        run_payload = client.get(f"/runs/{run_id}", headers=operator_headers()).json()

    assert response.status_code == 200
    assert response.json()["approval"]["resolution"]["decision"] == decision
    assert response.json()["run"]["run_id"] == run_id
    assert run_payload["status"] == expected_status
    assert run_payload["terminal_output"] == expected_output
    if decision == "reject":
        assert finish_runner.call_count == 0
        assert run_payload["failure_state"]["reason"] == "approval_rejected"
    else:
        assert finish_runner.call_count == 1
        assert finish_runner.last_input == (edited_payload or {"value": 3})


async def test_approval_api_duplicate_resolution_is_idempotent(sqlite_db) -> None:
    service, _ = await deploy_service(
        sqlite_db,
        approval_resume_graph(graph_id="graph-approval-idempotent"),
    )
    finish_runner = CountingFinishRunner()
    service.orchestrator.agent_runners["finish-step"] = finish_runner
    app = await bootstrap_app(sqlite_db, deployment_ref=service.deployment.deployment_ref)
    app.state.bootstrap = service

    with TestClient(app) as client:
        create_response = client.post(
            "/runs",
            json={"input_payload": {"value": 3}},
            headers=operator_headers(),
        )
        run_id = create_response.json()["run_id"]
        wait_for(
            lambda: (
                client.get(f"/runs/{run_id}", headers=operator_headers()).json()["status"]
                == "paused_for_approval"
            ),
            describe=_describe(client, run_id, "approval_pause"),
        )
        approval_id = client.get(
            f"/runs/{run_id}",
            headers=operator_headers(),
        ).json()["approval_paused_state"]["approval_id"]

        payload = {
            "decision": "edit_and_approve",
            "edited_payload": {"value": 8},
        }
        first_response = client.post(
            f"/deployments/{service.deployment.deployment_ref}/approvals/{approval_id}/resolve",
            json=payload,
            headers=reviewer_headers(),
        )
        second_response = client.post(
            f"/deployments/{service.deployment.deployment_ref}/approvals/{approval_id}/resolve",
            json=payload,
            headers=reviewer_headers(),
        )
        # Same reason as above: the resolve response carries a best-effort view of the
        # run, so settle on the observable terminal state before asserting on it.
        wait_for(
            lambda: _is_terminal(client, run_id),
            describe=_describe(client, run_id, "terminal_resume"),
        )
        run_payload = client.get(f"/runs/{run_id}", headers=operator_headers()).json()

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert first_response.json()["approval"] == second_response.json()["approval"]
    assert run_payload["status"] == "succeeded"
    assert run_payload["terminal_output"] == {"value": 9}
    assert finish_runner.call_count == 1


async def test_resolve_returns_a_best_effort_run_view_not_a_terminal_guarantee(sqlite_db) -> None:
    """Pin what the resolve endpoint actually promises about the run it returns.

    It hands the resume to the durable worker and waits only best-effort (~5 s) before
    returning whatever the run currently is, so the response may still carry a
    non-terminal status. Nothing pinned that contract, which is how the approval tests
    came to assert a terminal status straight out of the response and flake under load
    (ZER-21). Making a slow resume outlast that budget shows the real behaviour.
    """
    service, _ = await deploy_service(
        sqlite_db, approval_resume_graph(graph_id="graph-approval-contract")
    )

    class SlowFinishRunner(CountingFinishRunner):
        async def run(self, input_payload, *, thread_id=None, runtime_context=None):
            # Must await, not block: a blocking sleep stalls the loop the endpoint's own
            # poll runs on, which serialises everything and hides the behaviour.
            await asyncio.sleep(8.0)
            return await super().run(
                input_payload, thread_id=thread_id, runtime_context=runtime_context
            )

    service.orchestrator.agent_runners["finish-step"] = SlowFinishRunner()
    app = await bootstrap_app(sqlite_db, deployment_ref=service.deployment.deployment_ref)
    app.state.bootstrap = service

    with TestClient(app) as client:
        run_id = client.post(
            "/runs", json={"input_payload": {"value": 3}}, headers=operator_headers()
        ).json()["run_id"]
        wait_for(
            lambda: (
                client.get(f"/runs/{run_id}", headers=operator_headers()).json()["status"]
                == "paused_for_approval"
            ),
            describe=_describe(client, run_id, "approval_pause"),
        )
        approval_id = client.get(f"/runs/{run_id}", headers=operator_headers()).json()[
            "approval_paused_state"
        ]["approval_id"]

        response = client.post(
            f"/deployments/{service.deployment.deployment_ref}/approvals/{approval_id}/resolve",
            json={"decision": "approve"},
            headers=reviewer_headers(),
        )

        # The approval resolution itself IS guaranteed; the run's terminal state is not.
        assert response.status_code == 200
        assert response.json()["approval"]["resolution"]["decision"] == "approve"
        assert response.json()["run"]["status"] in _NON_TERMINAL

        wait_for(
            lambda: _is_terminal(client, run_id),
            describe=_describe(client, run_id, "terminal_resume"),
        )
        settled = client.get(f"/runs/{run_id}", headers=operator_headers()).json()

    assert settled["status"] == "succeeded"
    assert settled["terminal_output"] == {"value": 4}
