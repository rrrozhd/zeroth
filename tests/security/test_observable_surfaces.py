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
from zeroth.platform.storage import NullWorkspaceScopeContext
from zeroth.service.app import create_app


CANARY = "github_pat_" + "A" * 82


async def _capture_observable_surfaces(sqlite_db, tmp_path: Path) -> dict[str, object]:
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
    artifact_ref = await owner_store.store(key, result.stdout.encode(), "text/plain")
    owner_artifact = await owner_store.retrieve(key)
    with pytest.raises(ArtifactNotFoundError) as foreign_read:
        await foreign_store.retrieve(key)
    unknown_key = generate_artifact_key("unknown-run", "node")
    with pytest.raises(ArtifactNotFoundError) as unknown_read:
        await foreign_store.retrieve(unknown_key)
    foreign_exists = await foreign_store.exists(key)
    unknown_exists = await foreign_store.exists(unknown_key)
    # ArtifactStore has no non-destructive list API. Its run cleanup performs
    # scope-local enumeration first; tenant-b owns no matching artifacts, so
    # these zero-result attempts safely exercise that observable.
    foreign_listed = await foreign_store.cleanup_run(
        "observable-run", idempotency_key="observable-foreign-list"
    )
    unknown_listed = await foreign_store.cleanup_run(
        "unknown-run", idempotency_key="observable-unknown-list"
    )
    assert await owner_store.exists(key)
    await service.artifact_store.store(key, result.stdout.encode(), "text/plain")

    repository = AuditRepository.scoped(sqlite_db, NullWorkspaceScopeContext(tenant_id="tenant-a"))
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
        invalid_auth = client.get(f"/v1/artifacts/{key}", headers=api_key_headers(CANARY))
        owner_api_read = client.get(f"/v1/artifacts/{key}", headers=api_key_headers("safe-a"))
        foreign_api_read = client.get(f"/v1/artifacts/{key}", headers=api_key_headers("safe-b"))
        unknown_api_read = client.get(
            f"/v1/artifacts/{unknown_key}", headers=api_key_headers("safe-b")
        )
    assert invalid_auth.status_code == 401
    assert owner_api_read.status_code == 200
    assert foreign_api_read.status_code == 404
    assert unknown_api_read.status_code == 404

    def direct_miss_evidence(error: ArtifactNotFoundError, requested_key: str) -> dict[str, object]:
        return {
            "bytes": None,
            "metadata": {"exists": False, "requested-key": requested_key},
            "error": {
                "type": type(error).__name__,
                "message": str(error),
            },
            "status": "not-found",
            "headers": {},
        }

    def api_evidence(response, requested_key: str) -> dict[str, object]:  # noqa: ANN001
        headers = {
            name: value
            for name, value in response.headers.items()
            if name.lower() != "x-correlation-id"
        }
        return {
            "bytes": response.content,
            "metadata": {
                "url": str(response.request.url).replace(requested_key, "<requested-key>"),
                "correlation-id-present": "x-correlation-id" in response.headers,
            },
            "error": None if response.is_success else response.json(),
            "status": response.status_code,
            "headers": headers,
        }

    artifact_evidence = {
        "operation-trace": [
            "tenant-a:store",
            "tenant-a:retrieve",
            "tenant-b:retrieve-owner-key",
            "tenant-b:exists-owner-key",
            "tenant-b:enumerate-via-cleanup_run:owner-run",
            "tenant-b:retrieve-unknown-key",
            "tenant-b:exists-unknown-key",
            "tenant-b:enumerate-via-cleanup_run:unknown-run",
            "api:tenant-a:retrieve-owner-key",
            "api:tenant-b:retrieve-owner-key",
            "api:tenant-b:retrieve-unknown-key",
        ],
        "owner-retrieval": {
            "bytes": owner_artifact,
            "metadata": artifact_ref.model_dump(mode="json"),
            "error": None,
            "status": "retrieved",
            "headers": {},
        },
        "foreign-retrieval": direct_miss_evidence(foreign_read.value, key),
        "unknown-retrieval": direct_miss_evidence(unknown_read.value, unknown_key),
        "foreign-list": {
            "bytes": None,
            "metadata": {"count": foreign_listed},
            "error": None,
            "status": "empty",
            "headers": {},
        },
        "unknown-list": {
            "bytes": None,
            "metadata": {"count": unknown_listed},
            "error": None,
            "status": "empty",
            "headers": {},
        },
        "owner-api-retrieval": api_evidence(owner_api_read, key),
        "foreign-api-retrieval": api_evidence(foreign_api_read, key),
        "unknown-api-retrieval": api_evidence(unknown_api_read, unknown_key),
    }
    artifact_evidence["foreign-retrieval"]["metadata"]["exists"] = foreign_exists
    artifact_evidence["unknown-retrieval"]["metadata"]["exists"] = unknown_exists

    return {
        "workload-environment": result.environment,
        "logs": {"stdout": result.stdout, "stderr": result.stderr},
        "errors": {
            "execution-error": str(timeout.value),
            "body": invalid_auth.content,
            "headers": dict(invalid_auth.headers),
        },
        "artifacts": artifact_evidence,
        "audit-payloads": audit.model_dump(mode="json"),
        "other-tenant": {
            "artifact-error": str(foreign_read.value),
            "api-body": foreign_api_read.content,
            "api-headers": dict(foreign_api_read.headers),
        },
    }


@pytest.mark.asyncio()
async def test_credential_canary_absent_from_workload_environment(
    sqlite_db, tmp_path: Path
) -> None:
    captured = await _capture_observable_surfaces(sqlite_db, tmp_path)
    assert (
        CredentialLeakScanner([CANARY]).scan(
            captured["workload-environment"], surface="workload-environment"
        )
        == []
    )


@pytest.mark.asyncio()
async def test_credential_canary_absent_from_logs(sqlite_db, tmp_path: Path) -> None:
    captured = await _capture_observable_surfaces(sqlite_db, tmp_path)
    assert CredentialLeakScanner([CANARY]).scan(captured["logs"], surface="logs") == []


@pytest.mark.asyncio()
async def test_credential_canary_absent_from_errors(sqlite_db, tmp_path: Path) -> None:
    captured = await _capture_observable_surfaces(sqlite_db, tmp_path)
    assert CredentialLeakScanner([CANARY]).scan(captured["errors"], surface="errors") == []


@pytest.mark.asyncio()
async def test_credential_canary_absent_from_artifacts(sqlite_db, tmp_path: Path) -> None:
    captured = await _capture_observable_surfaces(sqlite_db, tmp_path)
    artifacts = captured["artifacts"]
    assert isinstance(artifacts, dict)
    assert artifacts != captured["logs"]["stdout"]
    assert set(artifacts) == {
        "operation-trace",
        "owner-retrieval",
        "foreign-retrieval",
        "unknown-retrieval",
        "foreign-list",
        "unknown-list",
        "owner-api-retrieval",
        "foreign-api-retrieval",
        "unknown-api-retrieval",
    }
    evidence_fields = {"bytes", "metadata", "error", "status", "headers"}
    for operation in set(artifacts) - {"operation-trace"}:
        assert set(artifacts[operation]) == evidence_fields
    foreign_retrieval = artifacts["foreign-retrieval"]
    unknown_retrieval = artifacts["unknown-retrieval"]
    for field in evidence_fields - {"metadata", "error"}:
        assert foreign_retrieval[field] == unknown_retrieval[field]
    assert foreign_retrieval["metadata"].keys() == unknown_retrieval["metadata"].keys()
    assert foreign_retrieval["metadata"]["exists"] == unknown_retrieval["metadata"]["exists"]
    assert foreign_retrieval["error"]["type"] == unknown_retrieval["error"]["type"]
    assert foreign_retrieval["error"]["message"].replace(
        foreign_retrieval["metadata"]["requested-key"], "<requested-key>"
    ) == unknown_retrieval["error"]["message"].replace(
        unknown_retrieval["metadata"]["requested-key"], "<requested-key>"
    )
    assert artifacts["foreign-list"] == artifacts["unknown-list"]
    assert artifacts["foreign-api-retrieval"] == artifacts["unknown-api-retrieval"]
    assert artifacts["owner-retrieval"]["bytes"] == artifacts["owner-api-retrieval"]["bytes"]
    assert CredentialLeakScanner([CANARY]).scan(captured["artifacts"], surface="artifacts") == []


@pytest.mark.asyncio()
async def test_credential_canary_absent_from_audit_payloads(sqlite_db, tmp_path: Path) -> None:
    captured = await _capture_observable_surfaces(sqlite_db, tmp_path)
    assert (
        CredentialLeakScanner([CANARY]).scan(captured["audit-payloads"], surface="audit-payloads")
        == []
    )


@pytest.mark.asyncio()
async def test_credential_canary_absent_from_other_tenant(sqlite_db, tmp_path: Path) -> None:
    captured = await _capture_observable_surfaces(sqlite_db, tmp_path)
    assert (
        CredentialLeakScanner([CANARY]).scan(captured["other-tenant"], surface="other-tenant") == []
    )
