"""Runnable gateway fixture and container release smoke probes."""

from __future__ import annotations

import json
import subprocess
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from langgraph_benchmark import CURRENT_RELEASE

ROOT = Path(__file__).resolve().parents[2]


class AgentServerFixtureHandler(BaseHTTPRequestHandler):
    """Minimal deterministic Agent Server surface for Compose release tests."""

    def _json(self, payload: Any) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - stdlib HTTP callback name
        if self.path == "/info":
            self._json({"langgraph_api_version": "0.11.1"})
            return
        if self.path == "/ok":
            self._json({"ok": True})
            return
        if self.path == "/openapi.json":
            self._json(_fixture_openapi())
            return
        if self.path.endswith("/release-slow"):
            time.sleep(2)
        self._json(
            {
                "fixture": "agent-server-0.11.1",
                "method": "GET",
                "path": self.path,
            }
        )

    def log_message(self, _format: str, *_args: Any) -> None:
        return


def _fixture_openapi() -> dict[str, Any]:
    fixture = ROOT / "tests/langgraph_gateway/fixtures/openapi-0.11.1.operations.json"
    operations = json.loads(fixture.read_text(encoding="utf-8"))["operations"]
    paths: dict[str, dict[str, dict[str, str]]] = {}
    for method, path, operation_id in operations:
        paths.setdefault(path, {})[method.lower()] = {"operationId": operation_id}
    return {"openapi": "3.1.0", "paths": paths}


def smoke(url: str, *, require_gateway: bool = False) -> None:
    """Fail unless dependency-aware readiness reports a service that can receive traffic."""
    with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310 - operator URL
        payload = json.load(response)
    if payload.get("status") not in {"ok", "degraded"} or not payload.get("checks"):
        raise RuntimeError(f"readiness failed: {payload!r}")
    gateway = payload.get("checks", {}).get("agent_server", {})
    if require_gateway and (payload.get("status") != "ok" or gateway.get("status") != "supported"):
        raise RuntimeError(f"gateway readiness failed: {payload!r}")


def gateway_smoke(url: str, api_key: str) -> None:
    """Make one authenticated request that must traverse the Agent Server gateway."""
    request = urllib.request.Request(url, headers={"X-API-Key": api_key})  # noqa: S310
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 - operator URL
        payload = json.load(response)
        correlation = response.headers.get("X-Correlation-ID")
    if payload.get("fixture") != "agent-server-0.11.1" or not correlation:
        raise RuntimeError(f"gateway smoke failed: {payload!r}")


def serve_mock_upstream(host: str, port: int) -> None:
    """Serve a deterministic test-only Agent Server compatibility fixture."""
    server = ThreadingHTTPServer((host, port), AgentServerFixtureHandler)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def resolved_image_evidence(references: list[str]) -> dict[str, Any]:
    """Record immutable local image IDs for the compatibility matrix."""
    images = []
    for reference in references:
        result = subprocess.run(
            ["docker", "image", "inspect", reference],
            check=True,
            capture_output=True,
            text=True,
        )
        inspected = json.loads(result.stdout)[0]
        images.append(
            {
                "reference": reference,
                "id": inspected["Id"],
                "repo_digests": inspected.get("RepoDigests") or [],
            }
        )
    return {"schema_version": 1, "release": CURRENT_RELEASE, "images": images}
