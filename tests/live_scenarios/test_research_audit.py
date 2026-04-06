from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from tests.service.helpers import (
    default_service_auth_config,
    operator_headers,
    reviewer_headers,
)
from zeroth.agent_runtime import ProviderResponse
from zeroth.agent_runtime.provider import CallableProviderAdapter
from zeroth.service.bootstrap import bootstrap_app

REPO_ROOT = Path(__file__).resolve().parents[2]


def _wait_for(
    client: TestClient,
    run_id: str,
    status: str,
    *,
    headers: dict[str, str] | None = None,
    timeout_seconds: float = 5.0,
) -> dict[str, Any]:
    import time

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        payload = client.get(f"/runs/{run_id}", headers=headers).json()
        if payload["status"] == status:
            return payload
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for run {run_id} to reach {status!r}")


class ToolThenContentProvider:
    def __init__(self, *, repo_path: Path, file_path: Path) -> None:
        self._repo_path = repo_path
        self._file_path = file_path
        self._last_input: dict[str, Any] | None = None

    async def ainvoke(self, request) -> ProviderResponse:  # noqa: ANN001
        has_tool_result = any(
            (
                getattr(message, "role", None) == "tool"
                or (isinstance(message, dict) and message.get("role") == "tool")
                or message.__class__.__name__ == "ToolMessage"
            )
            for message in request.messages
        )
        if not has_tool_result:
            self._last_input = dict(request.metadata["input_payload"])
            return ProviderResponse(
                content=None,
                tool_calls=[
                    {
                        "id": "tool-1",
                        "name": "repo_search",
                        "args": {
                            "query": "bootstrap_service",
                            "repo_path": str(self._repo_path),
                            "max_matches": 3,
                        },
                    },
                    {
                        "id": "tool-2",
                        "name": "read_file_excerpt",
                        "args": {
                            "path": str(self._file_path),
                            "start_line": 1,
                            "end_line": 40,
                        },
                    },
                ],
            )
        current = dict(self._last_input or {})
        return ProviderResponse(
            content={
                **current,
                "summary": "Collected repo evidence from the service bootstrap path.",
                "findings": ["bootstrap path should be checked for thread and policy wiring gaps"],
                "evidence": [
                    {
                        "kind": "repo_search",
                        "title": "bootstrap_service",
                        "location": "src/zeroth/service/bootstrap.py",
                        "snippet": "bootstrap_service(...)",
                    },
                    {
                        "kind": "file_excerpt",
                        "title": "bootstrap excerpt",
                        "location": "src/zeroth/service/bootstrap.py",
                        "snippet": "def bootstrap_service(",
                    },
                ],
                "sources": [
                    "src/zeroth/service/bootstrap.py",
                    "src/zeroth/orchestrator/runtime.py",
                ],
                "confidence": 0.62,
            }
        )


def _planner_provider(*, requires_research: bool = True):
    def provider(request) -> ProviderResponse:  # noqa: ANN001
        payload = dict(request.metadata["input_payload"])
        previous = request.metadata["thread_state"].get("output", {})
        run_count = int(previous.get("run_count", 0)) + 1
        return ProviderResponse(
            content={
                "question": payload["question"],
                "repo_path": payload.get("repo_path") or str(REPO_ROOT),
                "repo_query": "bootstrap_service",
                "file_path": str(REPO_ROOT / "src/zeroth/service/bootstrap.py"),
                "use_web": bool(payload.get("use_web", False)),
                "requires_research": requires_research,
                "requires_approval": bool(payload.get("force_approval", False)),
                "approval_reason": (
                    "force_approval requested" if payload.get("force_approval", False) else None
                ),
                "summary": "",
                "findings": [],
                "evidence": [],
                "sources": [],
                "confidence": 0.0,
                "run_count": run_count,
            }
        )

    return CallableProviderAdapter(provider)


def _review_provider():
    def provider(request) -> ProviderResponse:  # noqa: ANN001
        payload = dict(request.metadata["input_payload"])
        return ProviderResponse(
            content={
                **payload,
                "summary": payload.get("summary") or "Review complete.",
                "findings": payload.get("findings") or ["Potential bootstrap/runtime mismatch"],
                "confidence": 0.91,
                "requires_approval": bool(payload.get("requires_approval", False)),
                "approval_reason": payload.get("approval_reason"),
            }
        )

    return CallableProviderAdapter(provider)


def _final_provider():
    def provider(request) -> ProviderResponse:  # noqa: ANN001
        payload = dict(request.metadata["input_payload"])
        return ProviderResponse(
            content={
                "answer": f"Audit complete for: {payload['question']}",
                "summary": payload.get("summary") or "Audit complete.",
                "findings": payload.get("findings") or [],
                "confidence": payload.get("confidence", 0.0),
                "sources": payload.get("sources") or [],
                "approval_used": bool(payload.get("requires_approval", False)),
                "run_count": payload.get("run_count", 1),
            }
        )

    return CallableProviderAdapter(provider)


async def test_research_audit_bootstrap_and_api_flow(sqlite_db) -> None:
    from live_scenarios.research_audit.bootstrap import bootstrap_research_audit_service

    service = await bootstrap_research_audit_service(
        sqlite_db,
        provider_adapters={
            "plan": _planner_provider(requires_research=True),
            "research": ToolThenContentProvider(
                repo_path=REPO_ROOT,
                file_path=REPO_ROOT / "src/zeroth/service/bootstrap.py",
            ),
            "review": _review_provider(),
            "finalize": _final_provider(),
        },
        auth_config=default_service_auth_config(),
    )
    app = await bootstrap_app(
        sqlite_db,
        deployment_ref=service.deployment.deployment_ref,
        auth_config=default_service_auth_config(),
    )
    app.state.bootstrap = service

    with TestClient(app) as client:
        health = client.get("/health", headers=operator_headers())
        create = client.post(
            "/runs",
            json={"input_payload": {"question": "Find likely bootstrap bugs", "use_web": False}},
            headers=operator_headers(),
        )
        assert create.status_code == 202
        run_id = create.json()["run_id"]
        completed = _wait_for(client, run_id, "succeeded", headers=operator_headers())

    audits = await service.audit_repository.list_by_run(run_id)

    assert health.status_code == 200
    assert completed["terminal_output"]["approval_used"] is False
    assert completed["terminal_output"]["run_count"] == 1
    assert [
        entry.node_id for entry in (await service.run_repository.get(run_id)).execution_history
    ] == [
        "plan",
        "research",
        "normalize_evidence",
        "review",
        "finalize",
    ]
    research_audit = next(record for record in audits if record.node_id == "research")
    assert research_audit.execution_metadata["extra"]["tool_calls"]
    assert {
        record["tool"]["alias"]
        for record in research_audit.execution_metadata["extra"]["tool_calls"]
    } == {"repo_search", "read_file_excerpt"}


async def test_research_audit_approval_pause_and_resume(sqlite_db) -> None:
    from live_scenarios.research_audit.bootstrap import bootstrap_research_audit_service

    service = await bootstrap_research_audit_service(
        sqlite_db,
        provider_adapters={
            "plan": _planner_provider(requires_research=True),
            "research": ToolThenContentProvider(
                repo_path=REPO_ROOT,
                file_path=REPO_ROOT / "src/zeroth/service/bootstrap.py",
            ),
            "review": _review_provider(),
            "finalize": _final_provider(),
        },
        auth_config=default_service_auth_config(),
    )
    app = await bootstrap_app(
        sqlite_db,
        deployment_ref=service.deployment.deployment_ref,
        auth_config=default_service_auth_config(),
    )
    app.state.bootstrap = service

    with TestClient(app) as client:
        create = client.post(
            "/runs",
            json={
                "input_payload": {
                    "question": "Review and publish findings",
                    "use_web": False,
                    "force_approval": True,
                }
            },
            headers=operator_headers(),
        )
        assert create.status_code == 202
        run_id = create.json()["run_id"]
        paused = _wait_for(client, run_id, "paused_for_approval", headers=operator_headers())
        approval_id = paused["approval_paused_state"]["approval_id"]

        resolved = client.post(
            f"/deployments/{service.deployment.deployment_ref}/approvals/{approval_id}/resolve",
            json={"decision": "approve"},
            headers=reviewer_headers(),
        )
        completed = _wait_for(client, run_id, "succeeded", headers=operator_headers())

    assert resolved.status_code == 200
    assert completed["terminal_output"]["approval_used"] is True
    assert completed["terminal_output"]["answer"].startswith("Audit complete")


async def test_research_audit_thread_continuity_across_runs(sqlite_db) -> None:
    from live_scenarios.research_audit.bootstrap import bootstrap_research_audit_service

    service = await bootstrap_research_audit_service(
        sqlite_db,
        provider_adapters={
            "plan": _planner_provider(requires_research=False),
            "finalize": _final_provider(),
        },
        auth_config=default_service_auth_config(),
    )
    app = await bootstrap_app(
        sqlite_db,
        deployment_ref=service.deployment.deployment_ref,
        auth_config=default_service_auth_config(),
    )
    app.state.bootstrap = service

    with TestClient(app) as client:
        first = client.post(
            "/runs",
            json={"input_payload": {"question": "First pass"}},
            headers=operator_headers(),
        )
        assert first.status_code == 202
        first_run_id = first.json()["run_id"]
        thread_id = first.json()["thread_id"]
        first_done = _wait_for(client, first_run_id, "succeeded", headers=operator_headers())

        second = client.post(
            "/runs",
            json={"input_payload": {"question": "Second pass"}, "thread_id": thread_id},
            headers=operator_headers(),
        )
        assert second.status_code == 202
        second_run_id = second.json()["run_id"]
        second_done = _wait_for(client, second_run_id, "succeeded", headers=operator_headers())

    thread = await service.thread_repository.get(thread_id)

    assert first_done["terminal_output"]["run_count"] == 1
    assert second_done["terminal_output"]["run_count"] == 2
    assert thread is not None
    assert thread.run_ids == [first_run_id, second_run_id]
    assert thread.state_snapshot_refs


async def test_research_audit_strict_policy_mode_terminates_run(sqlite_db) -> None:
    from live_scenarios.research_audit.bootstrap import bootstrap_research_audit_service

    service = await bootstrap_research_audit_service(
        sqlite_db,
        provider_adapters={
            "plan": _planner_provider(requires_research=True),
            "research": ToolThenContentProvider(
                repo_path=REPO_ROOT,
                file_path=REPO_ROOT / "src/zeroth/service/bootstrap.py",
            ),
            "review": _review_provider(),
            "finalize": _final_provider(),
        },
        strict_policy=True,
        auth_config=default_service_auth_config(),
    )
    app = await bootstrap_app(
        sqlite_db,
        deployment_ref=service.deployment.deployment_ref,
        auth_config=default_service_auth_config(),
    )
    app.state.bootstrap = service

    with TestClient(app) as client:
        create = client.post(
            "/runs",
            json={"input_payload": {"question": "Find likely bootstrap bugs"}},
            headers=operator_headers(),
        )
        assert create.status_code == 202
        run_id = create.json()["run_id"]
        failed = _wait_for(
            client,
            run_id,
            "terminated_by_policy",
            headers=operator_headers(),
        )

    audits = await service.audit_repository.list_by_run(run_id)

    assert failed["failure_state"]["reason"] == "policy_violation"
    assert len(audits) == 1
    assert audits[0].status == "rejected"
