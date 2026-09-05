"""Runnable gateway fixture and container release smoke probes."""

from __future__ import annotations

import hashlib
import json
import subprocess
import urllib.request
from pathlib import Path
from typing import Any

from release.langgraph.generated_evidence import LABEL_KEYS, PACKAGE_KEYS
from release.langgraph.langgraph_benchmark import CURRENT_RELEASE

ROOT = Path(__file__).resolve().parents[2]


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
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            status, correlation = response.status, response.headers.get("X-Correlation-ID")
    except urllib.error.HTTPError as error:
        # A real Agent Server answers an unknown assistant with 404. That is a
        # traversal, not a failure: what this smoke proves is that the request reached
        # an upstream through the gateway and came back correlated.
        status, correlation = error.code, error.headers.get("X-Correlation-ID")
    if not correlation:
        raise RuntimeError(f"gateway smoke failed: no correlation id on HTTP {status}")
    if status >= 500:
        raise RuntimeError(f"gateway smoke failed: upstream unreachable, HTTP {status}")


def serve_shell_agent_server(host: str, port: int) -> None:
    """Serve the shell application from the real Agent Server.

    What this replaced answered `/ok`, `/info` and `/openapi.json` from literals, and
    built its OpenAPI document out of the same fixture the gateway's fingerprint pin was
    derived from — so the compatibility gate compared the pin against its own answer key
    and reported "supported" with no Agent Server behind it. Running the real package
    means the fingerprint is computed over a surface nobody here authored, which is the
    only version of that check worth having.
    """
    from langgraph_api.cli import run_server

    run_server(
        host=host,
        port=port,
        reload=False,
        graphs={"shell": "release.langgraph.shell_graph:graph"},
        disable_persistence=True,
        open_browser=False,
        server_level="ERROR",
    )


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
    """The image's identity as the daemon reports it, never as a document claims it."""
    repo_digests = inspected.get("RepoDigests") or []
    if repo_digests and "@" in repo_digests[0]:
        return str(repo_digests[0]).split("@", 1)[1]
    return str(inspected["Id"])


def _bound_application_digest(inspected: dict[str, Any], sbom: Path, reference: str) -> str:
    """The daemon's digest for ``reference``, with the SBOM checked against it.

    The SBOM used to *supply* this value. Replacing it with a different digest
    consistently across ``image.spdx.json``, ``image-compatibility.json`` and
    ``image-packages.json`` -- leaving the daemon-sourced ``id`` untouched --
    still validated clean, because every check compared the SBOM with itself.
    The expected value now comes from ``docker image inspect``, and the SBOM is
    the thing being checked.
    """
    resolved = _resolved_digest(inspected)
    claimed = _spdx_digest(sbom, reference)
    if claimed != resolved:
        raise RuntimeError(
            f"SBOM describes {reference} as {claimed} but the image daemon reports "
            f"{resolved}; the SBOM does not describe the built image"
        )
    return resolved


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
                "digest": _bound_application_digest(inspected, sbom, reference)
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
        "release": packages["zeroth-core"],
        "image": {"reference": image, "digest": identity["digest"]},
        "packages": packages,
        "labels": selected_labels,
    }
