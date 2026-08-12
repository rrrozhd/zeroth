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

    def evidence(
        operation: str,
        *,
        trace: list[str],
        data: bytes | None,
        metadata: dict[str, object],
        errors: list[object],
        status: str | int,
        headers: dict[str, str],
    ) -> dict[str, object]:
        return {
            "operation": operation,
            "trace": trace,
            "bytes": data,
            "metadata": metadata,
            "errors": errors,
            "status": status,
            "headers": headers,
        }

    def direct_miss_evidence(
        operation: str, error: ArtifactNotFoundError, requested_key: str
    ) -> dict[str, object]:
        return evidence(
            operation,
            trace=["retrieve", "not-found"],
            data=None,
            metadata={"requested-key": requested_key},
            errors=[{"type": type(error).__name__, "message": str(error)}],
            status="not-found",
            headers={},
        )

    async def list_attempt(
        operation: str, store: TenantScopedArtifactStore, run_id: str
    ) -> dict[str, object]:
        try:
            await store.list(run_id)  # type: ignore[attr-defined]
        except AttributeError as error:
            return evidence(
                operation,
                trace=["list", "unsupported"],
                data=None,
                metadata={"requested-run": run_id},
                errors=[{"type": type(error).__name__, "message": "list unsupported"}],
                status="unsupported",
                headers={},
            )
        raise AssertionError("artifact store unexpectedly acquired a list API")

    def api_evidence(operation: str, response, requested_key: str) -> dict[str, object]:  # noqa: ANN001
        headers = {
            name: value
            for name, value in response.headers.items()
            if name.lower() != "x-correlation-id"
        }
        return evidence(
            operation,
            trace=["api", "retrieve", str(response.status_code)],
            data=response.content,
            metadata={
                "url": str(response.request.url).replace(requested_key, "<requested-key>"),
                "correlation-id-present": "x-correlation-id" in response.headers,
            },
            errors=[] if response.is_success else [response.json()],
            status=response.status_code,
            headers=headers,
        )

    artifact_evidence = {
        "owner-write": evidence(
            "owner-write",
            trace=["tenant-a", "store", "stored"],
            data=None,
            metadata=artifact_ref.model_dump(mode="json"),
            errors=[],
            status="stored",
            headers={},
        ),
        "owner-retrieval": evidence(
            "owner-retrieval",
            trace=["tenant-a", "retrieve", "retrieved"],
            data=owner_artifact,
            metadata={"requested-key": key},
            errors=[],
            status="retrieved",
            headers={},
        ),
        "owner-list": await list_attempt("owner-list", owner_store, "observable-run"),
        "foreign-retrieval": direct_miss_evidence("foreign-retrieval", foreign_read.value, key),
        "foreign-list": await list_attempt("foreign-list", foreign_store, "observable-run"),
        "unknown-retrieval": direct_miss_evidence(
            "unknown-retrieval", unknown_read.value, unknown_key
        ),
        "unknown-list": await list_attempt("unknown-list", foreign_store, "unknown-run"),
        "owner-api-retrieval": api_evidence("owner-api-retrieval", owner_api_read, key),
        "foreign-api-retrieval": api_evidence("foreign-api-retrieval", foreign_api_read, key),
        "unknown-api-retrieval": api_evidence(
            "unknown-api-retrieval", unknown_api_read, unknown_key
        ),
    }

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
        "owner-write",
        "owner-retrieval",
        "owner-list",
        "foreign-retrieval",
        "foreign-list",
        "unknown-retrieval",
        "unknown-list",
        "owner-api-retrieval",
        "foreign-api-retrieval",
        "unknown-api-retrieval",
    }
    evidence_fields = {
        "operation",
        "trace",
        "bytes",
        "metadata",
        "errors",
        "status",
        "headers",
    }
    for operation in artifacts:
        assert set(artifacts[operation]) == evidence_fields
        assert artifacts[operation]["operation"] == operation
    foreign_retrieval = artifacts["foreign-retrieval"]
    unknown_retrieval = artifacts["unknown-retrieval"]
    for field in evidence_fields - {"operation", "metadata", "errors", "trace"}:
        assert foreign_retrieval[field] == unknown_retrieval[field]
    assert foreign_retrieval["metadata"].keys() == unknown_retrieval["metadata"].keys()
    assert foreign_retrieval["errors"][0]["type"] == unknown_retrieval["errors"][0]["type"]
    assert foreign_retrieval["errors"][0]["message"].replace(
        foreign_retrieval["metadata"]["requested-key"], "<requested-key>"
    ) == unknown_retrieval["errors"][0]["message"].replace(
        unknown_retrieval["metadata"]["requested-key"], "<requested-key>"
    )
    for field in evidence_fields - {"operation", "metadata"}:
        assert artifacts["foreign-list"][field] == artifacts["unknown-list"][field]
    for field in evidence_fields - {"operation"}:
        assert (
            artifacts["foreign-api-retrieval"][field] == artifacts["unknown-api-retrieval"][field]
        )
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
