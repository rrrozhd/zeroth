"""Transport conformance: the runner drives a contract over real HTTP and WebSocket.

The server here is a stand-in and its responses are authored to satisfy the contract,
so **nothing in this module is evidence about Zeroth**. It exists to exercise the parts
of the harness a product candidate cannot: real socket framing, WebSocket event
ordering, captures threaded between steps, and cleanup of owned resources.

Evidence about the product lives in `test_ephemeral_candidate.py`, which runs against
the real service. Keep that division: the moment a claim about Zeroth is asserted here,
it is being asserted against responses this file wrote.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn
from fastapi import FastAPI, Request, Response, WebSocket
from fastapi.responses import JSONResponse

from release.acceptance.config import AcceptanceConfig
from release.acceptance.models import AcceptanceContract, ScenarioStatus
from release.acceptance.runner import AcceptanceRunner
from release.acceptance.transport import AcceptanceTransport
from release.gates.identity import identity_digest

_IDENTITY = {
    "schema_version": 1,
    "commit": "a" * 40,
    "package": {"version": "1", "artifacts": {}},
    "image": {"candidate": "sha256:" + "b" * 64},
}
_CANDIDATE_DIGEST = identity_digest(_IDENTITY)


def _fixture_app() -> FastAPI:
    app = FastAPI()

    @app.websocket("/{path:path}")
    async def websocket_fixture(websocket: WebSocket, path: str) -> None:
        await websocket.accept()
        await websocket.receive_json()
        if path.endswith("stream"):
            names = ["run.started", "node.completed", "run.completed"]
        else:
            case = path.rsplit("/", 1)[-1]
            names = {
                "allow": ["gateway.admitted", "gateway.forwarded", "gateway.completed"],
                "deny": ["gateway.admitted", "gateway.denied"],
                "approval": ["gateway.admitted", "gateway.approval_required"],
                "resume": ["gateway.resumed", "gateway.forwarded", "gateway.completed"],
                "upstream-failure": [
                    "gateway.admitted",
                    "gateway.forwarded",
                    "gateway.upstream_error",
                ],
            }[case]
        for sequence, name in enumerate(names, start=1):
            await websocket.send_json({"event": name, "sequence": sequence})
        await websocket.close()

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
    async def http_fixture(request: Request, path: str) -> Response:
        route = f"/{path}"
        role = request.headers.get("X-API-Key")
        correlation = {"X-Correlation-ID": f"corr-{path.replace('/', '-')}"}
        if route == "/health/ready":
            if getattr(app.state, "draining", False):
                return JSONResponse({"status": "draining"}, status_code=503)
            return JSONResponse({"status": "ok", "checks": {"database": {"status": "ok"}}})
        if route == "/health":
            return JSONResponse({"deployment_ref": "candidate"})
        if route == "/__acceptance/identity":
            return JSONResponse(
                {"candidate_digest": _CANDIDATE_DIGEST, "deployment_ref": "candidate"}
            )
        if route == "/v1/deployments" and role is None:
            return JSONResponse({"detail": "not authenticated"}, status_code=401)
        if route == "/api/studio/v1/workflows" and request.method == "POST":
            if role == "reviewer":
                return JSONResponse({"detail": "forbidden"}, status_code=403)
            payload = await request.json()
            return JSONResponse(
                {"id": "server-workflow-id", "name": payload["name"], "status": "draft"},
                status_code=201,
            )
        if route.startswith("/api/studio/v1/workflows/server-workflow-id"):
            if request.method == "DELETE":
                return Response(status_code=204)
            if route.endswith("/publish") and request.method == "POST":
                return JSONResponse({"status": "published"})
            namespace = request.headers["X-Acceptance-Namespace"]
            return JSONResponse({"id": "server-workflow-id", "name": f"{namespace}-workflow"})
        if route == "/__acceptance/migrations":
            return JSONResponse({"current": True})
        if route == "/v1/deployments":
            return JSONResponse({"deployment_ref": "candidate"})
        if route == "/__acceptance/runs":
            return JSONResponse(
                {
                    "run_id": "server-run-id",
                    "tenant_id": "acceptance-tenant",
                    "namespace": request.headers["X-Acceptance-Namespace"],
                },
                status_code=202,
                headers=correlation,
            )
        if route.startswith("/__acceptance/runs/"):
            return JSONResponse({"status": "succeeded"})
        if route == "/__acceptance/timeline/before":
            return JSONResponse(
                {
                    "approval_id": f"{request.headers['X-Acceptance-Namespace']}-approval",
                    "entries": [],
                }
            )
        if route == "/__acceptance/timeline/after":
            return JSONResponse({"entries": [{"node_id": "finish-step", "status": "completed"}]})
        if route.startswith("/__acceptance/approvals/acceptance-tenant-") and route.endswith(
            "/resolve"
        ):
            return JSONResponse({"resolved": True})
        if route.startswith("/__acceptance/audit/"):
            return JSONResponse({"tenant_id": "acceptance-tenant", "causally_ordered": True})
        if route.startswith("/__acceptance/artifacts/"):
            return JSONResponse({"artifact_id": "server-artifact-id", "system_produced": True})
        if route == "/v1/retention/policy":
            return JSONResponse({"enabled": True})
        if route == "/__acceptance/gateway/http":
            case = (await request.json())["case"]
            decisions = {
                "allow": ("allow", 200),
                "deny": ("deny", 403),
                "approval": ("approval_required", 202),
                "resume": ("resumed", 200),
                "upstream_failure": ("upstream_error", 502),
            }
            decision, status = decisions[case]
            return JSONResponse({"decision": decision}, status_code=status, headers=correlation)
        if route == "/__acceptance/compatibility":
            return JSONResponse({"status": "supported", "detected_agent_server": "0.11.1"})
        if route == "/__acceptance/executable-units/resolve":
            return JSONResponse({"error_code": "unresolved_project_artifact"}, status_code=422)
        if route == "/__acceptance/executable-units/run":
            return JSONResponse({"error_code": "unstaged_project_artifact"}, status_code=422)
        if route.startswith("/__acceptance/durability/"):
            return JSONResponse({"audits": [{"node_id": "finish-step", "status": "completed"}]})
        if route == "/__acceptance/restart":
            return JSONResponse({}, status_code=202)
        if route == "/__acceptance/shutdown":
            app.state.draining = True
            return JSONResponse({"draining": True}, status_code=202)
        if route.startswith("/__acceptance/fixtures/") and request.method == "DELETE":
            return Response(status_code=204)
        return JSONResponse({"detail": "not found"}, status_code=404)

    return app


@pytest.fixture
def deployed_fixture() -> Iterator[str]:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(_fixture_app(), log_level="error", lifespan="off"))
    thread = threading.Thread(
        target=lambda: asyncio.run(server.serve(sockets=[listener])), daemon=True
    )
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        raise RuntimeError("acceptance fixture did not start")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()


@pytest.mark.asyncio
async def test_the_runner_drives_a_whole_contract_over_real_sockets(
    deployed_fixture: str, tmp_path: Path
) -> None:
    identity_path = tmp_path / "identity.json"
    identity_path.write_text(
        json.dumps(
            {
                **_IDENTITY,
            }
        ),
        encoding="utf-8",
    )
    config = AcceptanceConfig.model_validate(
        {
            "schema_version": 1,
            "base_url": deployed_fixture,
            "tenant_id": "acceptance-tenant",
            "deployment_ref": "candidate",
            "candidate_identity": str(identity_path),
            "credentials": {"operator": "OP", "reviewer": "REV", "admin": "ADM"},
            "lifecycle": {
                "restart_url": "/__acceptance/restart",
                "shutdown_url": "/__acceptance/shutdown",
            },
        }
    ).resolve({"OP": "operator", "REV": "reviewer", "ADM": "admin"}, run_id="01234567")
    contract = AcceptanceContract.model_validate(
        json.loads(
            Path("release/acceptance/contracts/transport-conformance-v1.json").read_text(
                encoding="utf-8"
            )
        )
    )

    async with AcceptanceTransport(config) as transport:
        report = await AcceptanceRunner(config, contract, transport).run()

    assert report.status is ScenarioStatus.PASSED
    assert len(report.scenarios) == 18
    assert all(item.status is ScenarioStatus.PASSED for item in report.cleanup)
