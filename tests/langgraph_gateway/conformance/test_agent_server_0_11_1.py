from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

pytest.importorskip("langgraph", reason="requires the gateway-conformance dependency group")

from .cases import CASES, CLAIMED_OPERATIONS, REQUIRED_OPERATION_GROUPS, ConformanceCase
from .graph import graph


pytestmark = pytest.mark.langgraph_conformance


def test_manifest_covers_the_complete_gateway_inventory() -> None:
    assert CASES
    assert {case.group for case in CASES} == REQUIRED_OPERATION_GROUPS
    assert len({case.name for case in CASES}) == len(CASES)
    case_operations = {
        (case.method, case.path_template)
        for case in CASES
        if case.group not in {"auth", "validation", "unsupported", "interrupt-resume"}
    }
    assert case_operations == CLAIMED_OPERATIONS
    projection_path = Path(__file__).parents[1] / "fixtures" / "openapi-0.11.1.operations.json"
    projection = json.loads(projection_path.read_text(encoding="utf-8"))
    projected_operations = {(method, path) for method, path, _ in projection["operations"]}
    # The document describes itself at /openapi.json, so that system route is not
    # represented as an OpenAPI operation in the pinned projection.
    projected_operations.add(("GET", "/openapi.json"))
    assert projected_operations >= CLAIMED_OPERATIONS


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_every_case_declares_the_release_gate_contract(case: ConformanceCase) -> None:
    assert case.method
    assert case.path_template
    assert callable(case.path)
    assert callable(case.request)
    assert case.expected_status
    assert case.gateway_expected_status
    assert case.expected_content_type
    assert callable(case.normalizer)
    assert case.governance
    assert callable(case.cleanup)


@pytest.fixture(scope="session")
def agent_server() -> Iterator[httpx.Client]:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    script = (
        "from langgraph_api.cli import run_server; "
        f"run_server(host='127.0.0.1', port={port}, reload=False, "
        "graphs={'conformance':'tests.langgraph_gateway.conformance.graph:graph'}, "
        "disable_persistence=True, open_browser=False, server_level='ERROR')"
    )
    environment = os.environ.copy()
    repository_root = Path(__file__).parents[3]
    environment.update(
        {
            "LANGSMITH_TRACING": "false",
            "LANGCHAIN_TRACING_V2": "false",
            "LANGGRAPH_CLOUD_LICENSE_KEY": "",
            "PYTHONPATH": os.pathsep.join(
                filter(None, (str(repository_root), environment.get("PYTHONPATH")))
            ),
        }
    )
    with tempfile.TemporaryDirectory(prefix="zeroth-agent-server-") as server_directory:
        process = subprocess.Popen(
            [sys.executable, "-c", script],
            cwd=server_directory,
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10)
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    pytest.fail(
                        f"Agent Server exited during startup with code {process.returncode}"
                    )
                try:
                    if client.get("/ok").status_code == 200:
                        break
                except httpx.TransportError:
                    time.sleep(0.05)
            else:
                pytest.fail("Agent Server did not become ready within 20 seconds")
            yield client
        finally:
            client.close()
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def _render(value: Any, context: dict[str, str]) -> Any:
    if isinstance(value, str):
        return value.format_map(context)
    if isinstance(value, dict):
        return {key: _render(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [_render(item, context) for item in value]
    return value


def _setup_case(client: httpx.Client, case: ConformanceCase) -> dict[str, str]:
    assistant = client.post(
        "/assistants",
        json={
            "graph_id": "conformance",
            "name": f"fixture-{case.name}",
            "metadata": {"pair": case.name},
        },
    )
    assistant.raise_for_status()
    thread = client.post(
        "/threads",
        json={"metadata": {"fixture": case.name}, "graph_id": "conformance"},
    )
    thread.raise_for_status()
    context = {
        "assistant_id": assistant.json()["assistant_id"],
        "thread_id": thread.json()["thread_id"],
    }
    needs_run = case.group in {
        "threads",
        "state",
        "history",
        "run-get",
        "run-join",
        "run-join-stream",
        "run-cancel",
        "event-stream",
    }
    if needs_run:
        mode = "cancel" if case.group == "run-cancel" else "echo"
        run = client.post(
            f"/threads/{context['thread_id']}/runs",
            json={
                "assistant_id": context["assistant_id"],
                "input": {"mode": mode, "text": "setup"},
                "stream_mode": ["custom", "values"],
            },
        )
        run.raise_for_status()
        context["run_id"] = run.json()["run_id"]
        if mode != "cancel":
            joined = client.get(f"/threads/{context['thread_id']}/runs/{context['run_id']}/join")
            joined.raise_for_status()
            state = client.get(f"/threads/{context['thread_id']}/state")
            state.raise_for_status()
            context["checkpoint_id"] = state.json()["checkpoint"]["checkpoint_id"]
    if case.name in {"native-resume", "protocol-input-respond"}:
        paused = client.post(
            f"/threads/{context['thread_id']}/runs/wait",
            json={
                "assistant_id": context["assistant_id"],
                "input": {"mode": "interrupt", "text": "approve"},
            },
        )
        paused.raise_for_status()
    return context


@pytest.mark.parametrize("case", CASES, ids=lambda case: f"direct-{case.name}")
def test_direct_agent_server_case(agent_server: httpx.Client, case: ConformanceCase) -> None:
    context = _setup_case(agent_server, case)
    request = _render(case.request(context), context)
    headers = {"authorization": "Bearer invalid-fixture-token"} if case.group == "auth" else {}
    if case.expected_content_type == "text/event-stream":
        with agent_server.stream(
            case.method,
            case.path(context),
            json=request,
            headers=headers,
            timeout=10,
        ) as response:
            assert response.status_code in case.expected_status, response.read()
            assert response.headers.get("content-type", "").startswith("text/event-stream")
            if case.group not in {"threads", "event-stream"}:
                for index, _line in enumerate(response.iter_lines()):
                    if index >= 6:
                        break
    else:
        response = agent_server.request(
            case.method,
            case.path(context),
            json=request,
            headers=headers,
        )
        assert response.status_code in case.expected_status, response.text
        if case.expected_content_type == "none":
            assert not response.content
        else:
            assert response.headers.get("content-type", "").startswith(case.expected_content_type)
        if response.content and case.expected_content_type == "application/json":
            case.normalizer(response.json())
    case.cleanup(agent_server, context)


def test_fixture_replays_tools_and_fails_closed_on_a_cassette_miss(
    agent_server: httpx.Client,
) -> None:
    assistant = agent_server.post("/assistants", json={"graph_id": "conformance"}).json()
    replay = agent_server.post(
        "/runs/wait",
        json={
            "assistant_id": assistant["assistant_id"],
            "input": {
                "mode": "tools",
                "tool_calls": [
                    {"name": "lookup_weather", "arguments": {"city": "Raleigh"}},
                    {"name": "lookup_policy", "arguments": {"policy_id": "policy-7"}},
                ],
            },
        },
    )
    replay.raise_for_status()
    assert [row["name"] for row in replay.json()["tool_sequence"]] == [
        "lookup_weather",
        "lookup_policy",
    ]

    with pytest.raises(AssertionError, match="cassette miss"):
        graph.invoke(
            {
                "mode": "tools",
                "tool_calls": [{"name": "real-network", "arguments": {}}],
            }
        )


def test_fixture_replays_model_and_fails_closed_on_a_cassette_miss() -> None:
    request = {
        "messages": [{"content": "fixture question", "role": "user"}],
        "model": "fixture-model-v1",
        "temperature": 0,
    }

    replay = graph.invoke({"mode": "model", "model_request": request})

    assert replay["model_response"] == {
        "content": "fixture response",
        "finish_reason": "stop",
        "role": "assistant",
    }
    with pytest.raises(AssertionError, match="model cassette miss"):
        graph.invoke(
            {
                "mode": "model",
                "model_request": {**request, "model": "unrecorded-model"},
            }
        )


def test_fixture_exposes_the_retry_attempt_sequence() -> None:
    result = graph.invoke({"mode": "retry", "retry_key": "direct-retry"})

    assert result["retry_attempts"] == [1, 2]


def test_fixture_custom_events_are_ordered_and_exception_is_predictable() -> None:
    events = list(graph.stream({"mode": "echo", "text": "ordered"}, stream_mode="custom"))
    assert [(event["kind"], event["sequence"]) for event in events] == [
        ("custom", 1),
        ("token", 2),
        ("custom", 3),
    ]
    with pytest.raises(RuntimeError, match="deterministic-fixture-error"):
        graph.invoke({"mode": "error"})
