from __future__ import annotations

import http.client
import importlib
import ipaddress
import json
import threading
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType

import pytest


def _sink_module() -> ModuleType:
    try:
        return importlib.import_module("zeroth.eval.auth_rejection_sink")
    except ModuleNotFoundError:
        pytest.fail("the authentication-rejection sink module is missing")


@contextmanager
def _running_sink(
    evidence_path: Path, *, max_records: int = 100
) -> Iterator[tuple[object, ModuleType]]:
    sink = _sink_module()
    create_server = getattr(sink, "create_server", None)
    assert callable(create_server), "create_server is missing"
    server = create_server(evidence_path=evidence_path, max_records=max_records, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, sink
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _request(server: object, method: str, path: str, **kwargs: object) -> tuple[int, bytes]:
    host, port = server.server_address[:2]
    connection = http.client.HTTPConnection(host, port, timeout=2)
    try:
        connection.request(method, path, **kwargs)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def test_chat_completions_returns_static_401_and_records_no_secrets(tmp_path: Path) -> None:
    evidence_path = tmp_path / "auth-rejections.jsonl"
    body_secret = "body-secret-that-must-not-be-recorded"
    header_secret = "header-secret-that-must-not-be-recorded"
    query_secret = "query-secret-that-must-not-be-recorded"

    with _running_sink(evidence_path) as (server, sink):
        status, body = _request(
            server,
            "POST",
            f"/v1/chat/completions?api_key={query_secret}",
            body=json.dumps({"model": "ignored", "secret": body_secret}),
            headers={
                "Authorization": f"Bearer {header_secret}",
                "Content-Type": "application/json",
            },
        )

    assert status == 401
    assert body == sink.AUTHENTICATION_ERROR_BODY
    assert json.loads(body) == {
        "error": {
            "message": "Authentication rejected by controlled local evaluation sink.",
            "type": "authentication_error",
            "param": None,
            "code": "invalid_api_key",
        }
    }
    evidence_text = evidence_path.read_text(encoding="utf-8")
    assert json.loads(evidence_text) == {
        "request_count": 1,
        "path": "/v1/chat/completions",
        "method": "POST",
        "authorization_present": True,
    }
    assert body_secret not in evidence_text
    assert header_secret not in evidence_text
    assert query_secret not in evidence_text


def test_health_is_read_only_and_evidence_is_hard_capped(tmp_path: Path) -> None:
    evidence_path = tmp_path / "auth-rejections.jsonl"

    with _running_sink(evidence_path, max_records=2) as (server, _sink):
        for _ in range(3):
            assert _request(server, "POST", "/v1/chat/completions")[0] == 401

        status, body = _request(server, "GET", "/health")

    assert status == 200
    assert json.loads(body) == {
        "status": "ok",
        "request_count": 3,
        "recorded_count": 2,
        "max_records": 2,
        "at_capacity": True,
        "evidence_write_failed": False,
    }
    lines = evidence_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert [json.loads(line)["request_count"] for line in lines] == [1, 2]
    assert all(json.loads(line)["authorization_present"] is False for line in lines)


def test_server_is_loopback_only_and_other_routes_do_not_create_evidence(tmp_path: Path) -> None:
    evidence_path = tmp_path / "auth-rejections.jsonl"

    with _running_sink(evidence_path) as (server, _sink):
        assert ipaddress.ip_address(server.server_address[0]).is_loopback
        assert _request(server, "POST", "/not-openai", body="do-not-store-me")[0] == 404
        assert _request(server, "GET", "/v1/chat/completions")[0] == 404

    assert not evidence_path.exists()


def test_evidence_write_failure_does_not_change_the_401_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence_path = tmp_path / "auth-rejections.jsonl"

    with _running_sink(evidence_path) as (server, sink):

        def fail_open(*_args: object, **_kwargs: object) -> int:
            raise OSError("simulated evidence volume failure")

        monkeypatch.setattr(sink.os, "open", fail_open)
        status, body = _request(server, "POST", "/v1/chat/completions")
        health_status, health_body = _request(server, "GET", "/health")

    assert status == 401
    assert body == sink.AUTHENTICATION_ERROR_BODY
    assert health_status == 200
    assert json.loads(health_body) == {
        "status": "degraded",
        "request_count": 1,
        "recorded_count": 0,
        "max_records": 100,
        "at_capacity": False,
        "evidence_write_failed": True,
    }
    assert not evidence_path.exists()


def test_existing_evidence_is_validated_and_appended_without_rewriting(tmp_path: Path) -> None:
    evidence_path = tmp_path / "auth-rejections.jsonl"

    with _running_sink(evidence_path, max_records=3) as (server, _sink):
        assert _request(server, "POST", "/v1/chat/completions")[0] == 401
    original = evidence_path.read_bytes()

    with _running_sink(evidence_path, max_records=3) as (server, _sink):
        assert _request(server, "POST", "/v1/chat/completions")[0] == 401

    updated = evidence_path.read_bytes()
    assert updated.startswith(original)
    assert [json.loads(line)["request_count"] for line in updated.splitlines()] == [1, 2]


def test_cli_entrypoint_and_arguments_are_exposed() -> None:
    sink = _sink_module()
    parser = sink.build_parser()

    args = parser.parse_args(
        ["--port", "8123", "--evidence-file", "run.jsonl", "--max-records", "7"]
    )

    assert args.port == 8123
    assert args.evidence_file == Path("run.jsonl")
    assert args.max_records == 7
    assert callable(sink.main)
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    assert (
        project["project"]["scripts"]["zeroth-auth-rejection-sink"]
        == "zeroth.eval.auth_rejection_sink:main"
    )
