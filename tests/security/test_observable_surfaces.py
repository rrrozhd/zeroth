from __future__ import annotations

import sys
from copy import deepcopy
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


class _RecordingArtifactStore:
    """Transparent ArtifactStore observer used only by this evidence harness."""

    def __init__(self, inner, records: list[dict[str, object]], *, operation: str) -> None:  # noqa: ANN001
        self._inner = inner
        self._records = records
        self._operation = operation

    def _append(
        self,
        *,
        arguments: dict[str, object],
        data: bytes | None,
        metadata: dict[str, object],
        errors: list[object],
        status: str,
    ) -> None:
        self._records.append(
            {
                "operation": self._operation,
                "order": len(self._records),
                "arguments": arguments,
                "trace": [self._operation, status],
                "bytes": data,
                "metadata": metadata,
                "errors": errors,
                "status": status,
                "headers": {},
            }
        )

    async def store(self, key: str, data: bytes, content_type: str, ttl: int | None = None):  # noqa: ANN201
        reference = await self._inner.store(key, data, content_type, ttl=ttl)
        self._append(
            arguments={"key": key, "content-type": content_type, "ttl": ttl},
            data=None,
            metadata=reference.model_dump(mode="json"),
            errors=[],
            status="stored",
        )
        return reference

    async def retrieve(self, key: str) -> bytes:
        try:
            data = await self._inner.retrieve(key)
        except Exception as error:
            self._append(
                arguments={"key": key},
                data=None,
                metadata={},
                errors=[{"type": type(error).__name__, "message": str(error)}],
                status="not-found" if isinstance(error, ArtifactNotFoundError) else "error",
            )
            raise
        self._append(
            arguments={"key": key},
            data=data,
            metadata={},
            errors=[],
            status="retrieved",
        )
        return data

    async def list(self, run_id: str) -> object:
        try:
            method = self._inner.list
        except AttributeError:
            error = AttributeError("artifact store does not support list")
            self._append(
                arguments={"run-id": run_id},
                data=None,
                metadata={},
                errors=[{"type": "AttributeError", "message": str(error)}],
                status="unsupported",
            )
            raise error from None
        result = await method(run_id)
        self._append(
            arguments={"run-id": run_id},
            data=None,
            metadata={"result": result},
            errors=[],
            status="listed",
        )
        return result

    def __getattr__(self, name: str):  # noqa: ANN201
        return getattr(self._inner, name)


def _record_api_get(
    client: TestClient,
    records: list[dict[str, object]],
    *,
    operation: str,
    key: str,
    api_key: str,
    principal: str,
    correlation_id: str,
):  # noqa: ANN201
    response = client.get(
        f"/v1/artifacts/{key}",
        headers={**api_key_headers(api_key), "X-Correlation-ID": correlation_id},
    )
    records.append(
        {
            "operation": operation,
            "order": len(records),
            "arguments": {
                "key": key,
                "principal": principal,
                "correlation-id": correlation_id,
            },
            "trace": [operation, "response", str(response.status_code)],
            "bytes": response.content,
            "metadata": {
                "correlation-id": response.headers.get("x-correlation-id"),
            },
            "errors": [] if response.is_success else [response.json()],
            "status": response.status_code,
            "headers": dict(response.headers),
        }
    )
    return response


@pytest.mark.asyncio()
async def test_recording_artifact_store_is_transparent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inner = TenantScopedArtifactStore(
        FilesystemArtifactStore(tmp_path / "transparent"), tenant_id="tenant-a"
    )
    records: list[dict[str, object]] = []
    recorder = _RecordingArtifactStore(inner, records, operation="probe")
    returned: list[object] = []
    original_store = inner.store

    async def capture_store(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        result = await original_store(*args, **kwargs)
        returned.append(result)
        return result

    monkeypatch.setattr(inner, "store", capture_store)
    key = generate_artifact_key("transparent-run", "node")
    reference = await recorder.store(key, b"transparent-bytes", "text/plain")

    assert reference is returned[0]
    assert records[0]["metadata"] == reference.model_dump(mode="json")
    assert await recorder.retrieve(key) == b"transparent-bytes"

    missing = generate_artifact_key("missing-run", "node")
    with pytest.raises(ArtifactNotFoundError) as direct_error:
        await inner.retrieve(missing)
    with pytest.raises(type(direct_error.value)) as recorded_error:
        await recorder.retrieve(missing)
    assert str(recorded_error.value) == str(direct_error.value)


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
        ("unknown", "safe-unknown", ServiceRole.OPERATOR, "tenant-unknown", None),
    )
    service, _ = await deploy_service(
        sqlite_db,
        agent_graph(graph_id="observable-surface-graph"),
        deployment_ref="observable-surface-deployment",
        auth_config=auth,
        tenant_id="tenant-a",
    )
    key = generate_artifact_key("observable-run", "node")
    payload = b"artifact-evidence-payload"
    records: list[dict[str, object]] = []
    backend = FilesystemArtifactStore(tmp_path / "artifacts")
    owner_store = TenantScopedArtifactStore(backend, tenant_id="tenant-a")
    foreign_store = TenantScopedArtifactStore(backend, tenant_id="tenant-b")
    unknown_store = TenantScopedArtifactStore(backend, tenant_id="tenant-unknown")

    artifact_ref = await _RecordingArtifactStore(
        owner_store, records, operation="owner-write"
    ).store(key, payload, "text/plain")
    await _RecordingArtifactStore(owner_store, records, operation="owner-retrieval").retrieve(key)
    with pytest.raises(AttributeError):
        await _RecordingArtifactStore(owner_store, records, operation="owner-list").list(
            "observable-run"
        )
    with pytest.raises(ArtifactNotFoundError) as foreign_read:
        await _RecordingArtifactStore(
            foreign_store, records, operation="foreign-retrieval"
        ).retrieve(key)
    with pytest.raises(AttributeError):
        await _RecordingArtifactStore(foreign_store, records, operation="foreign-list").list(
            "observable-run"
        )
    with pytest.raises(ArtifactNotFoundError):
        await _RecordingArtifactStore(
            unknown_store, records, operation="unknown-retrieval"
        ).retrieve(key)
    with pytest.raises(AttributeError):
        await _RecordingArtifactStore(unknown_store, records, operation="unknown-list").list(
            "observable-run"
        )

    service.artifact_store = _RecordingArtifactStore(
        owner_store, records, operation="owner-api-store-retrieval"
    )

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
        owner_api_read = _record_api_get(
            client,
            records,
            operation="owner-api-retrieval",
            key=key,
            api_key="safe-a",
            principal="tenant-a",
            correlation_id="artifact-owner-correlation",
        )
        foreign_api_read = _record_api_get(
            client,
            records,
            operation="foreign-api-retrieval",
            key=key,
            api_key="safe-b",
            principal="tenant-b",
            correlation_id="artifact-foreign-correlation",
        )
        unknown_api_read = _record_api_get(
            client,
            records,
            operation="unknown-api-retrieval",
            key=key,
            api_key="safe-unknown",
            principal="tenant-unknown",
            correlation_id="artifact-unknown-correlation",
        )
    assert invalid_auth.status_code == 401
    assert owner_api_read.status_code == 200
    assert foreign_api_read.status_code == 404
    assert unknown_api_read.status_code == 404

    return {
        "workload-environment": result.environment,
        "logs": {"stdout": result.stdout, "stderr": result.stderr},
        "errors": {
            "execution-error": str(timeout.value),
            "body": invalid_auth.content,
            "headers": dict(invalid_auth.headers),
        },
        "artifacts": {
            "key": key,
            "run-id": "observable-run",
            "payload": payload,
            "reference": artifact_ref.model_dump(mode="json"),
            "records": records,
        },
        "audit-payloads": audit.model_dump(mode="json"),
        "other-tenant": {
            "artifact-error": str(foreign_read.value),
            "api-body": foreign_api_read.content,
            "api-headers": dict(foreign_api_read.headers),
        },
    }


def _expected_artifact_records(artifacts: dict[str, object]) -> list[dict[str, object]]:
    key = artifacts["key"]
    run_id = artifacts["run-id"]
    payload = artifacts["payload"]
    reference = artifacts["reference"]
    not_found = [{"type": "ArtifactNotFoundError", "message": f"Artifact not found: {key}"}]
    list_unsupported = [
        {"type": "AttributeError", "message": "artifact store does not support list"}
    ]
    return [
        {
            "operation": "owner-write",
            "order": 0,
            "arguments": {"key": key, "content-type": "text/plain", "ttl": None},
            "trace": ["owner-write", "stored"],
            "bytes": None,
            "metadata": reference,
            "errors": [],
            "status": "stored",
            "headers": {},
        },
        {
            "operation": "owner-retrieval",
            "order": 1,
            "arguments": {"key": key},
            "trace": ["owner-retrieval", "retrieved"],
            "bytes": payload,
            "metadata": {},
            "errors": [],
            "status": "retrieved",
            "headers": {},
        },
        {
            "operation": "owner-list",
            "order": 2,
            "arguments": {"run-id": run_id},
            "trace": ["owner-list", "unsupported"],
            "bytes": None,
            "metadata": {},
            "errors": list_unsupported,
            "status": "unsupported",
            "headers": {},
        },
        {
            "operation": "foreign-retrieval",
            "order": 3,
            "arguments": {"key": key},
            "trace": ["foreign-retrieval", "not-found"],
            "bytes": None,
            "metadata": {},
            "errors": not_found,
            "status": "not-found",
            "headers": {},
        },
        {
            "operation": "foreign-list",
            "order": 4,
            "arguments": {"run-id": run_id},
            "trace": ["foreign-list", "unsupported"],
            "bytes": None,
            "metadata": {},
            "errors": list_unsupported,
            "status": "unsupported",
            "headers": {},
        },
        {
            "operation": "unknown-retrieval",
            "order": 5,
            "arguments": {"key": key},
            "trace": ["unknown-retrieval", "not-found"],
            "bytes": None,
            "metadata": {},
            "errors": not_found,
            "status": "not-found",
            "headers": {},
        },
        {
            "operation": "unknown-list",
            "order": 6,
            "arguments": {"run-id": run_id},
            "trace": ["unknown-list", "unsupported"],
            "bytes": None,
            "metadata": {},
            "errors": list_unsupported,
            "status": "unsupported",
            "headers": {},
        },
        {
            "operation": "owner-api-store-retrieval",
            "order": 7,
            "arguments": {"key": key},
            "trace": ["owner-api-store-retrieval", "retrieved"],
            "bytes": payload,
            "metadata": {},
            "errors": [],
            "status": "retrieved",
            "headers": {},
        },
        {
            "operation": "owner-api-retrieval",
            "order": 8,
            "arguments": {
                "key": key,
                "principal": "tenant-a",
                "correlation-id": "artifact-owner-correlation",
            },
            "trace": ["owner-api-retrieval", "response", "200"],
            "bytes": payload,
            "metadata": {"correlation-id": "artifact-owner-correlation"},
            "errors": [],
            "status": 200,
            "headers": {
                "content-length": str(len(payload)),
                "content-type": "application/octet-stream",
                "x-correlation-id": "artifact-owner-correlation",
            },
        },
        {
            "operation": "foreign-api-retrieval",
            "order": 9,
            "arguments": {
                "key": key,
                "principal": "tenant-b",
                "correlation-id": "artifact-foreign-correlation",
            },
            "trace": ["foreign-api-retrieval", "response", "404"],
            "bytes": b'{"detail":"deployment not found"}',
            "metadata": {"correlation-id": "artifact-foreign-correlation"},
            "errors": [{"detail": "deployment not found"}],
            "status": 404,
            "headers": {
                "content-length": "33",
                "content-type": "application/json",
                "x-correlation-id": "artifact-foreign-correlation",
            },
        },
        {
            "operation": "unknown-api-retrieval",
            "order": 10,
            "arguments": {
                "key": key,
                "principal": "tenant-unknown",
                "correlation-id": "artifact-unknown-correlation",
            },
            "trace": ["unknown-api-retrieval", "response", "404"],
            "bytes": b'{"detail":"deployment not found"}',
            "metadata": {"correlation-id": "artifact-unknown-correlation"},
            "errors": [{"detail": "deployment not found"}],
            "status": 404,
            "headers": {
                "content-length": "33",
                "content-type": "application/json",
                "x-correlation-id": "artifact-unknown-correlation",
            },
        },
    ]


def _assert_artifact_evidence(artifacts: dict[str, object]) -> None:
    assert set(artifacts) == {"key", "run-id", "payload", "reference", "records"}
    assert artifacts["payload"] != b"None\n"
    assert artifacts["records"] == _expected_artifact_records(artifacts)


def _normalized_isolation_record(record: dict[str, object]) -> dict[str, object]:
    normalized = deepcopy(record)
    operation = normalized["operation"]
    assert isinstance(operation, str)
    normalized["operation"] = operation.replace("foreign", "other").replace("unknown", "other")
    normalized["order"] = "<order>"
    trace = normalized["trace"]
    assert isinstance(trace, list)
    trace[0] = normalized["operation"]
    arguments = normalized["arguments"]
    assert isinstance(arguments, dict)
    if "principal" in arguments:
        arguments["principal"] = "<other-tenant>"
    if "correlation-id" in arguments:
        arguments["correlation-id"] = "<correlation-id>"
    metadata = normalized["metadata"]
    assert isinstance(metadata, dict)
    if "correlation-id" in metadata:
        metadata["correlation-id"] = "<correlation-id>"
    headers = normalized["headers"]
    assert isinstance(headers, dict)
    if "x-correlation-id" in headers:
        headers["x-correlation-id"] = "<correlation-id>"
    return normalized


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
    _assert_artifact_evidence(artifacts)
    records = artifacts["records"]
    assert isinstance(records, list)
    assert _normalized_isolation_record(records[3]) == _normalized_isolation_record(records[5])
    assert _normalized_isolation_record(records[4]) == _normalized_isolation_record(records[6])
    assert _normalized_isolation_record(records[9]) == _normalized_isolation_record(records[10])
    assert CredentialLeakScanner([CANARY]).scan(captured["artifacts"], surface="artifacts") == []


@pytest.mark.asyncio()
async def test_artifact_evidence_literal_assertion_kills_structural_mutations(
    sqlite_db, tmp_path: Path
) -> None:
    captured = await _capture_observable_surfaces(sqlite_db, tmp_path)
    artifacts = captured["artifacts"]
    assert isinstance(artifacts, dict)
    mutations = []
    for mutate in (
        lambda records: records[0].update(trace=[]),
        lambda records: records[1].update(bytes=b""),
        lambda records: records[1].update(status=0),
        lambda records: records[8]["metadata"].update({"correlation-id": False}),
        lambda records: records[9].update(errors=[]),
        lambda records: records[0].update(metadata={}),
    ):
        mutated = deepcopy(artifacts)
        mutate(mutated["records"])
        mutations.append(mutated)

    for mutated in mutations:
        with pytest.raises(AssertionError):
            _assert_artifact_evidence(mutated)


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
