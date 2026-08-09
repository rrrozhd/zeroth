"""Runnable gateway fixture and container release smoke probes."""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from generated_evidence import LABEL_KEYS, PACKAGE_KEYS
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


def _inspect_image(reference: str) -> dict[str, Any]:
    result = subprocess.run(
        ["docker", "image", "inspect", reference],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)[0]


def _spdx_digest(path: Path, reference: str) -> str:
    value = json.loads(path.read_text(encoding="utf-8"))
    roots = [
        package
        for package in value.get("packages", [])
        if package.get("name") == reference and package.get("primaryPackagePurpose") == "CONTAINER"
    ]
    if len(roots) != 1 or not str(roots[0].get("versionInfo", "")).startswith("sha256:"):
        raise RuntimeError("SBOM does not identify the built image digest")
    return str(roots[0]["versionInfo"])


def _resolved_digest(inspected: dict[str, Any]) -> str:
    repo_digests = inspected.get("RepoDigests") or []
    if repo_digests and "@" in repo_digests[0]:
        return str(repo_digests[0]).split("@", 1)[1]
    return str(inspected["Id"])


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def resolved_image_evidence(references: list[str], *, sbom: Path, artifact: Path) -> dict[str, Any]:
    """Record immutable image and exported-artifact digests."""
    images = []
    for index, reference in enumerate(references):
        inspected = _inspect_image(reference)
        images.append(
            {
                "reference": reference,
                "id": inspected["Id"],
                "digest": _spdx_digest(sbom, reference)
                if index == 0
                else _resolved_digest(inspected),
                "repo_digests": inspected.get("RepoDigests") or [],
            }
        )
    return {
        "schema_version": 2,
        "release": CURRENT_RELEASE,
        "artifact": {"path": artifact.name, "digest": _file_digest(artifact)},
        "images": images,
    }


def _expected_packages(compatibility: dict[str, Any]) -> dict[str, str]:
    resolved = compatibility["resolved"]
    return {name: str(resolved[key]) for name, key in PACKAGE_KEYS.items()}


def _expected_labels(compatibility: dict[str, Any]) -> dict[str, str]:
    values = {
        **compatibility["resolved"],
        "adapter_version": compatibility["adapter_version"],
    }
    return {name: str(values[key]) for name, key in LABEL_KEYS.items()}


def installed_package_evidence(
    image: str, compatibility_path: Path, image_evidence_path: Path
) -> dict[str, Any]:
    """Inspect the built image and reject versions that drift from the matrix."""
    compatibility = json.loads(compatibility_path.read_text(encoding="utf-8"))
    image_evidence = json.loads(image_evidence_path.read_text(encoding="utf-8"))
    identity = next(item for item in image_evidence["images"] if item["reference"] == image)
    names = tuple(PACKAGE_KEYS)
    script = (
        "import json,importlib.metadata as m;"
        f"names={names!r};"
        "print(json.dumps({name:m.version(name) for name in names},sort_keys=True))"
    )
    result = subprocess.run(
        ["docker", "run", "--rm", "--entrypoint", "python", image, "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )
    packages = json.loads(result.stdout)
    labels = _inspect_image(image).get("Config", {}).get("Labels") or {}
    selected_labels = {name: labels.get(name) for name in LABEL_KEYS}
    if packages != _expected_packages(compatibility):
        raise RuntimeError("installed image packages do not match compatibility evidence")
    if selected_labels != _expected_labels(compatibility):
        raise RuntimeError("image labels do not match compatibility evidence")
    return {
        "schema_version": 1,
        "release": CURRENT_RELEASE,
        "image": {"reference": image, "digest": identity["digest"]},
        "packages": packages,
        "labels": selected_labels,
    }
