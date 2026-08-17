"""Minimal reproducible certification assets for a generated app checkout."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .models import AppDeclaration
from .wheel_installation import RUNTIME_BOOTSTRAP

_MODULE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*$")


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
            f"{module}.entrypoint",
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


_HEALTHCHECK = '''"""Fail closed unless Zeroth readiness is exactly ok."""

import json
import sys
from urllib.request import urlopen


def main() -> int:
    try:
        with urlopen("http://127.0.0.1:8000/health/ready", timeout=3) as response:
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


def scaffold_checkout(
    root: Path,
    *,
    app_name: str,
    module: str,
    zeroth_version: str,
    zeroth_ref: str,
) -> list[Path]:
    """Emit the declaration, caller, Dockerfile, and readiness probe once."""
    if _MODULE.fullmatch(module) is None:
        raise ValueError("module must be a dotted Python module path")
    if re.fullmatch(r"[0-9a-f]{40}", zeroth_ref) is None:
        raise ValueError("zeroth_ref must be a full lowercase Git commit SHA")
    declaration = AppDeclaration.model_validate(_declaration(app_name, module, zeroth_version))
    files = {
        root / "certification.json": json.dumps(
            declaration.model_dump(mode="json"), indent=2, sort_keys=True
        )
        + "\n",
        root / "certification.semantic.json": json.dumps(
            {
                "capabilities": {},
                "contracts": {},
                "graphs": [],
                "policies": {},
                "reducers": [],
                "schema_version": 1,
                "service_config": {},
                "target_sources": {},
                "zeroth_version": zeroth_version,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        root / "Dockerfile.certification": _dockerfile(module, zeroth_version),
        root / ".github/workflows/app-certification.yml": _caller(zeroth_ref),
        root / module.replace(".", "/") / "certification_healthcheck.py": _HEALTHCHECK,
        root / module.replace(".", "/") / "migrations.py": _MIGRATIONS,
    }
    existing = [path for path in files if path.exists() or path.is_symlink()]
    if existing:
        raise ValueError("refusing to overwrite scaffold assets: " + ", ".join(map(str, existing)))
    for path, content in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return list(files)
