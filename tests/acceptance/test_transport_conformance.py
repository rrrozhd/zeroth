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
from typing import Any

import pytest
import uvicorn
import httpx
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


async def _stream_fixture(websocket: WebSocket, path: str) -> None:
    """Answer a stream the way the contract under test expects it to be answered."""
    await websocket.accept()
    first = await websocket.receive_json()
    if "sequence" in path:
        # Echo the whole opening sequence back so a caller can prove every frame
        # reached the wire, in order, rather than only the first or only the last.
        second = await websocket.receive_json()
        frames = [first, second]
        for sequence, frame in enumerate(frames, start=1):
            await websocket.send_json(
                {"type": "event", "sequence": sequence, "event": frame.get("method")}
            )
        await websocket.close()
        return
    names = (
        ["metadata", "values"]
        if "gateway" in path
        else ["run.started", "node.completed", "run.completed"]
    )
    for sequence, name in enumerate(names, start=1):
        await websocket.send_json({"event": name, "sequence": sequence})
    await websocket.close()


def _fixture_app() -> FastAPI:
    app = FastAPI()

    @app.websocket("/{path:path}")
    async def websocket_fixture(websocket: WebSocket, path: str) -> None:
        await _stream_fixture(websocket, path)

    done_audit = [{"node_id": "finish-step", "status": "completed"}]
    # A dispatch table rather than a branch ladder: the mock's job is to answer the
    # contract's paths, and a table says which path gets which answer at a glance.
    exact: dict[str, Any] = {
        "/health": {
            "deployment_ref": "candidate",
            "langgraph_gateway": {
                "compatibility": {"status": "supported", "detected_agent_server": "0.11.1"}
            },
        },
        "/__acceptance/identity": {
            "candidate_digest": _CANDIDATE_DIGEST,
            "deployment_ref": "candidate",
        },
        "/regulus/health": {
            "status": "ok",
            "schema_revision": {
                "applied": "20260824_10",
                "head": "20260824_10",
                "state": "current",
            },
        },
        "/v1/deployments": {"deployment_ref": "candidate"},
        "/__acceptance/timeline/after": {"entries": done_audit},
        "/v1/retention/policy": {"enabled": True},
        "/__acceptance/ready-compat": {"checks": {"agent_server": {"status": "supported"}}},
    }
    by_prefix: dict[str, Any] = {
        "/__acceptance/runs/": {
            "status": "succeeded",
            "terminal_output": {"value": 9, "artifact": {"key": "acceptance-tenant-blob"}},
        },
        "/__acceptance/audit/": {"tenant_id": "acceptance-tenant", "causally_ordered": True},
        "/__acceptance/artifacts/blob/": {"bytes": "present"},
        "/__acceptance/artifacts/": {
            "artifact_id": "server-artifact-id",
            "system_produced": True,
        },
        "/__acceptance/durability/": {"audits": done_audit},
    }
    status_body: dict[str, tuple[int, Any]] = {
        "/__acceptance/executable-units/resolve": (
            422,
            {"error_code": "unresolved_project_artifact"},
        ),
        "/__acceptance/executable-units/run": (422, {"error_code": "unstaged_project_artifact"}),
        "/__acceptance/restart": (202, {}),
        "/__acceptance/retention/erase": (409, {"detail": "held"}),
    }

    def _gateway(assistant: str) -> tuple[int, Any]:
        if assistant.endswith("-deny"):
            return 403, {"code": "zeroth.policy_denied"}
        if assistant.endswith("-upstream-failure"):
            return 502, {"code": "zeroth.upstream_unavailable"}
        return 200, {"forwarded": True}

    def _workflow(route: str, method: str, namespace: str) -> Response | None:
        if not route.startswith("/api/studio/v1/workflows/server-workflow-id"):
            return None
        if method == "DELETE":
            return Response(status_code=204)
        if route.endswith("/publish"):
            return JSONResponse({"status": "published"})
        return JSONResponse({"id": "server-workflow-id", "name": f"{namespace}-workflow"})

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
    async def http_fixture(request: Request, path: str) -> Response:
        route = f"/{path}"
        role = request.headers.get("X-API-Key")
        namespace = request.headers.get("X-Acceptance-Namespace", "")
        # Echo the caller's id, as a correlated deployment does; inventing one here
        # would relabel the request rather than correlate it.
        correlation = {"X-Correlation-ID": request.headers.get("X-Correlation-ID", "")}

        if route == "/health/ready":
            if getattr(app.state, "draining", False):
                return JSONResponse({"status": "draining"}, status_code=503)
            return JSONResponse(
                {
                    "status": "ok",
                    "checks": {"database": {"status": "ok"}},
                    "schema_revision": {
                        "applied": "029",
                        "head": "029",
                        "state": "current",
                    },
                }
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
        workflow = _workflow(route, request.method, namespace)
        if workflow is not None:
            return workflow
        if route == "/__acceptance/runs":
            return JSONResponse(
                {
                    "run_id": "server-run-id",
                    "tenant_id": request.headers.get("X-Acceptance-Tenant", ""),
                    "namespace": namespace,
                },
                status_code=202,
                headers=correlation,
            )
        if route == "/__acceptance/timeline/before":
            return JSONResponse({"approval_id": f"{namespace}-approval", "entries": []})
        if route == "/__acceptance/gateway/http":
            status, body = _gateway((await request.json())["assistant_id"])
            return JSONResponse(body, status_code=status, headers=correlation)
        if route == "/__acceptance/shutdown":
            app.state.draining = True
            return JSONResponse({"draining": True}, status_code=202)
        if route.startswith("/__acceptance/approvals/") and route.endswith("/resolve"):
            return JSONResponse({"resolved": True})
        if route.startswith("/__acceptance/fixtures/") and request.method == "DELETE":
            return Response(status_code=204)
        if route in exact:
            return JSONResponse(exact[route])
        if route in status_body:
            status, body = status_body[route]
            return JSONResponse(body, status_code=status)
        for prefix, body in by_prefix.items():
            if route.startswith(prefix):
                return JSONResponse(body)
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

    # Name what failed. A bare status assertion makes every regression here look the
    # same, and this module exists to localize harness faults.
    failed = {
        item.name: item.detail
        for item in [*report.scenarios, *report.cleanup]
        if item.status is not ScenarioStatus.PASSED
    }
    assert not failed, failed
    assert report.status is ScenarioStatus.PASSED
    assert len(report.scenarios) == 17


@pytest.mark.asyncio
async def test_transport_fixture_does_not_invent_a_migrations_route(
    deployed_fixture: str,
) -> None:
    async with httpx.AsyncClient(base_url=deployed_fixture) as client:
        response = await client.get("/__acceptance/migrations")

    assert response.status_code == 404


async def test_the_transport_sends_every_frame_of_an_opening_sequence_in_order(
    deployed_fixture: str, tmp_path: Path
) -> None:
    """A stream protocol is a conversation, so every frame must reach the wire, in order.

    The shipped `gateway_websocket` scenario proves this against a real Agent Server,
    which is the stronger evidence — but only if the transport genuinely sends all the
    frames. This pins that on its own, so a change that sent just the first or just the
    last one fails here rather than surfacing as a confusing ordering mismatch layers up.
    """
    identity_path = tmp_path / "identity.json"
    identity_path.write_text(json.dumps(_IDENTITY), encoding="utf-8")
    config = AcceptanceConfig.model_validate(
        {
            "schema_version": 1,
            "base_url": deployed_fixture,
            "tenant_id": "acceptance-tenant",
            "deployment_ref": "candidate",
            "candidate_identity": str(identity_path),
            "credentials": {"operator": "OP", "reviewer": "REV", "admin": "ADM"},
        }
    ).resolve({"OP": "operator", "REV": "reviewer", "ADM": "admin"}, run_id="01234567")

    async with AcceptanceTransport(config) as transport:
        events = await transport.websocket_events(
            "operator",
            "/sequence",
            None,
            max_events=2,
            frames=[{"id": 1, "method": "first"}, {"id": 2, "method": "second"}],
        )

    assert [event["event"] for event in events] == ["first", "second"]
