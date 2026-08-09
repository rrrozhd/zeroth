from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from release.security.scan import CredentialLeakScanner
from tests.service.helpers import (
    agent_graph,
    api_key_headers,
    deploy_service,
    scoped_auth_config,
)
from zeroth.governance.audit.models import NodeAuditRecord
from zeroth.governance.audit.repository import AuditRepository
from zeroth.governance.identity import ServiceRole
from zeroth.integrations.execution.sandbox import SandboxManager, SandboxTimeoutError
from zeroth.platform.artifacts.errors import ArtifactNotFoundError
from zeroth.platform.artifacts.models import generate_artifact_key
from zeroth.platform.artifacts.store import FilesystemArtifactStore
from zeroth.platform.artifacts.tenant_scoped import TenantScopedArtifactStore
from zeroth.service.app import create_app


CANARY = "github_pat_" + "A" * 82


@pytest.mark.asyncio()
async def test_credential_canary_is_absent_from_every_observable_surface(
    sqlite_db, tmp_path: Path
) -> None:
    manager = SandboxManager(base_env={"GITHUB_TOKEN": CANARY})
    result = manager.run(
        [
            sys.executable,
            "-c",
            "import os,sys; print(os.getenv('GITHUB_TOKEN')); "
            "print(os.getenv('GITHUB_TOKEN'), file=sys.stderr); sys.exit(7)",
        ]
    )
    assert result.returncode == 7

    with pytest.raises(SandboxTimeoutError) as timeout:
        manager.run(
            [sys.executable, "-c", "import time; time.sleep(1)"],
            timeout_seconds=0.01,
        )

    auth = scoped_auth_config(
        ("a", "safe-a", ServiceRole.OPERATOR, "tenant-a", None),
        ("b", "safe-b", ServiceRole.OPERATOR, "tenant-b", None),
    )
    service, _ = await deploy_service(
        sqlite_db,
        agent_graph(graph_id="observable-surface-graph"),
        deployment_ref="observable-surface-deployment",
        auth_config=auth,
        tenant_id="tenant-a",
    )
    key = generate_artifact_key("observable-run", "node")
    backend = FilesystemArtifactStore(tmp_path / "artifacts")
    owner_store = TenantScopedArtifactStore(backend, tenant_id="tenant-a")
    foreign_store = TenantScopedArtifactStore(backend, tenant_id="tenant-b")
    await owner_store.store(key, result.stdout.encode(), "text/plain")
    artifact = await owner_store.retrieve(key)
    with pytest.raises(ArtifactNotFoundError) as foreign_read:
        await foreign_store.retrieve(key)
    await service.artifact_store.store(key, result.stdout.encode(), "text/plain")

    repository = AuditRepository(sqlite_db)
    await repository.write(
        NodeAuditRecord(
            audit_id="observable-audit",
            run_id="observable-run",
            node_id="node",
            graph_version_ref="graph:v1",
            deployment_ref="observable-surface-deployment",
            tenant_id="tenant-a",
            status="failed",
            input_snapshot={"authorization": CANARY},
            stdout=result.stdout,
            stderr=result.stderr,
            error=f"provider rejected {CANARY}",
        )
    )
    audit = await repository.get("observable-audit")
    assert audit is not None

    with TestClient(create_app(service)) as client:
        invalid_auth = client.get(
            f"/v1/artifacts/{key}", headers=api_key_headers(CANARY)
        )
        foreign_api_read = client.get(
            f"/v1/artifacts/{key}", headers=api_key_headers("safe-b")
        )
    assert invalid_auth.status_code == 401
    assert foreign_api_read.status_code == 404

    observable = {
        "workload-environment": result.environment,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "execution-error": str(timeout.value),
        "artifact": artifact,
        "audit-payload": audit.model_dump(mode="json"),
        "api-response": {
            "body": invalid_auth.content,
            "headers": dict(invalid_auth.headers),
        },
        "tenant-b-read": {
            "artifact-error": str(foreign_read.value),
            "api-body": foreign_api_read.content,
            "api-headers": dict(foreign_api_read.headers),
        },
    }

    assert CredentialLeakScanner([CANARY]).scan(observable, surface="aggregate") == []
