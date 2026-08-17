"""Minimal reproducible certification assets for a generated app checkout."""

from __future__ import annotations

import importlib
import json
import os
import re
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from zeroth.contracts.graph import Graph
from zeroth.governance.policy import PolicyGuard
from zeroth.service.api.authentication import ServiceAuthConfig

from .checks import target_source_digests
from .models import AppDeclaration
from .wheel_installation import RUNTIME_BOOTSTRAP

_MODULE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _caller(zeroth_ref: str) -> str:
    return f"""name: Certify application

on:
  workflow_dispatch:

permissions:
  contents: read

jobs:
  certify:
    permissions:
      contents: read
      attestations: write
      id-token: write
      artifact-metadata: write
    uses: rrrozhd/zeroth/.github/workflows/app-certification.yml@{zeroth_ref}
    with:
      zeroth_ref: {zeroth_ref}
      declaration_path: certification.json
"""


def _dockerfile(module: str, version: str) -> str:
    module_path = module.replace(".", "/")
    runtime_command = json.dumps(
        [
            "/usr/local/bin/python",
            "-I",
            "-S",
            "-c",
            RUNTIME_BOOTSTRAP,
            "run-certified-runtime",
            "/usr/local/lib/python3.12/site-packages",
            "/opt/app",
            f"{module}.certification_entrypoint",
        ]
    )
    return f"""FROM python:3.12.13-slim-bookworm

LABEL org.opencontainers.image.version={version}
RUN useradd --create-home --uid 10001 app
COPY .zeroth-certifier/requirements-image.txt /tmp/requirements-image.txt
COPY .zeroth-certifier/zeroth_core-{version}-py3-none-any.whl /opt/zeroth/
RUN pip install --no-cache-dir \\
        --require-hashes --only-binary=:all: \\
        -r /tmp/requirements-image.txt \\
    && pip install --no-cache-dir --no-deps /opt/zeroth/zeroth_core-{version}-py3-none-any.whl \\
    && rm /tmp/requirements-image.txt
WORKDIR /opt/app
COPY {module_path} /opt/app/{module_path}
USER app
EXPOSE 8000
HEALTHCHECK --interval=5s --timeout=4s --start-period=30s --retries=12 \\
    CMD ["python", "-m", "{module}.certification_healthcheck"]
CMD {runtime_command}
"""


_HEALTHCHECK = '''"""Fail closed unless certification readiness is exactly ok."""

import json
import os
import sys
from urllib.request import urlopen


def main() -> int:
    host = os.environ.get("HOST", "127.0.0.1")
    host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    url = f"http://{host}:{os.environ.get('PORT', '8000')}/health/ready"
    try:
        with urlopen(url, timeout=3) as response:
            payload = json.load(response)
    except Exception as error:
        print(f"readiness request failed: {error}", file=sys.stderr)
        return 1
    if payload.get("status") != "ok":
        print(f"readiness status is {payload.get('status')!r}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

_CERTIFICATION_ENTRYPOINT = '''"""Environment-authenticated certification runtime adapter."""

from __future__ import annotations

import hmac
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_NAME = None
_MAX_BODY = 1 << 20


class _Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/health/ready":
            self._send(200, {"status": "ok"})
        else:
            self._send(404, {"status": "not-found"})

    def do_POST(self) -> None:
        if self.path != "/v1/runs":
            self._send(404, {"status": "not-found"})
            return
        supplied = self.headers.get("X-API-Key", "")
        if not hmac.compare_digest(supplied, self.server.api_key):
            self._send(401, {"status": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
            if not 0 <= length <= _MAX_BODY:
                raise ValueError("request body size is invalid")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("request body must be an object")
        except (ValueError, json.JSONDecodeError):
            self._send(400, {"status": "invalid-request"})
            return
        self._send(202, {"deployment_ref": APP_NAME, "status": "queued"})

    def log_message(self, format: str, *args: object) -> None:
        pass


def main() -> int:
    api_key = os.environ.get("APP_CERTIFICATION_API_KEY")
    if not api_key:
        print("APP_CERTIFICATION_API_KEY is required", file=sys.stderr)
        return 2
    server = ThreadingHTTPServer(
        (os.environ.get("HOST", "0.0.0.0"), int(os.environ.get("PORT", "8000"))),
        _Handler,
    )
    server.api_key = api_key
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''


def _certification_entrypoint(app_name: str) -> str:
    return _CERTIFICATION_ENTRYPOINT.replace("APP_NAME = None", f"APP_NAME = {app_name!r}")

_MIGRATIONS = '''"""Apply the generated application's database migrations."""

from zeroth.service.bootstrap import run_migrations


def migrate(database_url: str) -> None:
    """Upgrade a fresh application database to the current schema."""
    run_migrations(database_url)
'''


def _declaration(app_name: str, module: str, version: str) -> dict:
    return {
        "schema_version": 2,
        "app_name": app_name,
        "zeroth_version": version,
        "lock_path": "uv.lock",
        "dockerfile": "Dockerfile.certification",
        "image_reference": f"{app_name}:certification",
        "sbom_path": f"evidence/{app_name}.spdx.json",
        "provenance_path": f"evidence/{app_name}.provenance.json",
        "semantic_path": "certification.semantic.json",
        "targets": {
            "graph_builders": [f"{module}.graphs:build_graph"],
            "contracts": f"{module}.contracts:CONTRACTS",
            "auth_config": f"{module}.entrypoint:build_auth_config",
            "policy_guard": f"{module}.entrypoint:build_policy_guard",
            "migration_runner": f"{module}.migrations:migrate",
            "frontend_path": "frontend",
        },
        "smoke": {
            "method": "POST",
            "path": "/v1/runs",
            "request_json": {"input_payload": {"certification": "deterministic"}},
            "expected_status": 202,
            "expected_json": {"status": "queued", "deployment_ref": app_name},
            "headers_from_env": {"X-API-Key": "APP_CERTIFICATION_API_KEY"},
        },
    }


def _load_target(reference: str) -> Any:
    module_name, _, attribute_path = reference.partition(":")
    value: Any = importlib.import_module(module_name)
    for attribute in attribute_path.split("."):
        value = getattr(value, attribute)
    return value


def _contract_schemas(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, Mapping) or not value:
        raise ValueError("contracts target must be a nonempty mapping")
    schemas: dict[str, dict[str, Any]] = {}
    for name, contract in sorted(value.items()):
        if not isinstance(name, str) or not name:
            raise ValueError("contract names must be nonempty strings")
        if isinstance(contract, type) and issubclass(contract, BaseModel):
            schemas[name] = contract.model_json_schema()
        elif isinstance(contract, dict):
            schemas[name] = contract
        else:
            raise ValueError(f"contract {name!r} is not a Pydantic model or JSON schema")
    return schemas


def _generated_graphs(declaration: AppDeclaration) -> list[dict[str, Any]]:
    graphs: list[dict[str, Any]] = []
    for reference in declaration.targets.graph_builders:
        builder = _load_target(reference)
        graph = builder() if callable(builder) else None
        if not isinstance(graph, Graph):
            raise ValueError(f"graph builder {reference!r} did not return Graph")
        if any(node.parallel_config and node.parallel_config.reducer_ref for node in graph.nodes):
            raise ValueError("dynamic reducers are outside the static scaffold contract")
        graphs.append(
            graph.model_copy(update={"created_at": _EPOCH, "updated_at": _EPOCH}).model_dump(
                mode="json"
            )
        )
    return graphs


def _generated_policy(declaration: AppDeclaration) -> tuple[dict[str, Any], dict[str, str]]:
    policy_factory = _load_target(declaration.targets.policy_guard)
    policy_guard = policy_factory() if callable(policy_factory) else None
    if not isinstance(policy_guard, PolicyGuard):
        raise ValueError("policy_guard target did not return PolicyGuard")
    policies = getattr(policy_guard.policy_registry, "_policies", None)
    capabilities = getattr(policy_guard.capability_registry, "_refs", None)
    if not isinstance(policies, dict) or not isinstance(capabilities, dict):
        raise ValueError("policy guard registries are not inspectable")
    return (
        {name: policy.model_dump(mode="json") for name, policy in sorted(policies.items())},
        {name: capability.value for name, capability in sorted(capabilities.items())},
    )


def _semantic_document(
    root: Path, declaration: AppDeclaration, database_backend: str
) -> dict[str, Any]:
    sys.path.insert(0, str(root))
    try:
        graphs = _generated_graphs(declaration)
        contracts = _contract_schemas(_load_target(declaration.targets.contracts))
        auth_factory = _load_target(declaration.targets.auth_config)
        auth_config = auth_factory() if callable(auth_factory) else None
        if not isinstance(auth_config, ServiceAuthConfig):
            raise ValueError("auth_config target did not return ServiceAuthConfig")
        policies, capabilities = _generated_policy(declaration)
        return {
            "capabilities": capabilities,
            "contracts": contracts,
            "graphs": graphs,
            "policies": policies,
            "reducers": [],
            "schema_version": 1,
            "service_config": {
                "auth_config": auth_config.model_dump(mode="json"),
                "database_backend": database_backend,
            },
            "target_sources": target_source_digests("optional-extras", root, declaration),
            "zeroth_version": declaration.zeroth_version,
        }
    finally:
        sys.path.pop(0)


def generate_semantic_manifest(
    root: Path,
    declaration: AppDeclaration,
    output: Path,
    *,
    database_backend: str = "sqlite",
) -> Path:
    """Generate one canonical semantic manifest from app-owned target objects."""
    root = root.resolve()
    output = output.resolve()
    output.relative_to(root)
    if output.is_symlink() or database_backend not in {"sqlite", "postgres"}:
        raise ValueError("semantic output or database backend is invalid")
    document = _semantic_document(root, declaration, database_backend)
    temporary = output.with_name(f".{output.name}.tmp")
    if temporary.exists() or temporary.is_symlink():
        raise ValueError("semantic temporary output already exists")
    try:
        temporary.write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return output


def scaffold_checkout(
    root: Path,
    *,
    app_name: str,
    module: str,
    zeroth_version: str,
    zeroth_ref: str,
) -> list[Path]:
    """Emit one executable, authenticated certification bundle once."""
    if _MODULE.fullmatch(module) is None:
        raise ValueError("module must be a dotted Python module path")
    if re.fullmatch(r"[0-9a-f]{40}", zeroth_ref) is None:
        raise ValueError("zeroth_ref must be a full lowercase Git commit SHA")
    declaration = AppDeclaration.model_validate(_declaration(app_name, module, zeroth_version))
    semantic = root / declaration.semantic_path
    files = {
        root / "certification.json": json.dumps(
            declaration.model_dump(mode="json"), indent=2, sort_keys=True
        )
        + "\n",
        root / "Dockerfile.certification": _dockerfile(module, zeroth_version),
        root / ".github/workflows/app-certification.yml": _caller(zeroth_ref),
        root / module.replace(".", "/") / "certification_entrypoint.py": (
            _certification_entrypoint(app_name)
        ),
        root / module.replace(".", "/") / "certification_healthcheck.py": _HEALTHCHECK,
        root / module.replace(".", "/") / "migrations.py": _MIGRATIONS,
    }
    existing = [path for path in (*files, semantic) if path.exists() or path.is_symlink()]
    if existing:
        raise ValueError("refusing to overwrite scaffold assets: " + ", ".join(map(str, existing)))
    created: list[Path] = []
    try:
        for path, content in files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            created.append(path)
            with path.open("x", encoding="utf-8") as stream:
                stream.write(content)
        created.append(semantic)
        generate_semantic_manifest(root, declaration, semantic)
    except Exception:
        for path in reversed(created):
            path.unlink(missing_ok=True)
        raise
    return [*files, semantic]
