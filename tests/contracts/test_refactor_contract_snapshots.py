"""Semantic backend contract snapshots protected during the architecture refactor."""

from __future__ import annotations

import ast
import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from zeroth.contracts.graph.validation_errors import (
    GraphValidationError,
    GraphValidationReport,
    ValidationCode,
    ValidationIssue,
    ValidationSeverity,
)
from zeroth.contracts.registry import ContractNotFoundError, ContractRegistryError
from zeroth.governance.approvals import ApprovalRecord, ApprovalRepository
from zeroth.governance.audit import AuditRepository, NodeAuditRecord
from zeroth.governance.audit.capture_policy import AuditCapturePolicy
from zeroth.governance.policy import Capability, CapabilityDeniedError
from zeroth.governance.retention.audit_log_repository import RetentionAuditLogRepository
from zeroth.governance.retention.cleanup_manifest import (
    CleanupManifest,
    CleanupOperation,
    DatabaseErasureOutcome,
    operation_id,
)
from zeroth.governance.retention.cleanup_state_repository import CleanupStateRepository
from zeroth.integrations.persistence.runs import RunRepository, ThreadRepository
from zeroth.platform.storage import NullWorkspaceScopeContext
from zeroth.runtime.runs import Run, RunHistoryEntry, Thread, ThreadMemoryBinding
from zeroth.service.app import create_app
from zeroth.service.bootstrap import run_migrations

FIXTURES = Path(__file__).with_name("fixtures")
REPO_ROOT = Path(__file__).parents[2]
_OPENAPI_NOISE = {"description", "example", "examples", "operationId", "summary", "title"}
_HTTP_METHODS = {"delete", "get", "head", "options", "patch", "post", "put", "trace"}
_FIXED_TIME = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)


def _load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


def _normalize_openapi(value: Any, *, keys_are_contract: bool = False) -> Any:
    if isinstance(value, dict):
        return {
            key: _normalize_openapi(
                item,
                keys_are_contract=(
                    not keys_are_contract
                    and key in {"$defs", "definitions", "patternProperties", "properties"}
                ),
            )
            for key, item in sorted(value.items())
            if keys_are_contract or key not in _OPENAPI_NOISE
        }
    if isinstance(value, list):
        return [_normalize_openapi(item) for item in value]
    return value


def current_openapi_contract() -> dict[str, Any]:
    """Return behavior-bearing OpenAPI paths/components without prose churn."""
    schema = create_app(SimpleNamespace(regulus_client=None)).openapi()
    paths = {
        path: {
            key: value
            for key, value in definition.items()
            if key in _HTTP_METHODS or key == "parameters"
        }
        for path, definition in schema["paths"].items()
    }
    return _normalize_openapi(
        {
            "openapi": schema["openapi"],
            "paths": paths,
            "components": schema.get("components", {}),
        }
    )


def _migration_revisions() -> list[dict[str, str | None]]:
    revisions: dict[str, str | None] = {}
    versions = REPO_ROOT / "src" / "zeroth" / "service" / "_migrations" / "versions"
    for path in versions.glob("*.py"):
        tree = ast.parse(path.read_text())
        values: dict[str, str | None] = {}
        for node in tree.body:
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id in {"revision", "down_revision"}:
                values[target.id] = ast.literal_eval(node.value)
        if "revision" in values:
            revisions[str(values["revision"])] = values.get("down_revision")

    ordered: list[dict[str, str | None]] = []
    revision = next(key for key, parent in revisions.items() if parent is None)
    while revision:
        parent = revisions[revision]
        ordered.append({"revision": revision, "down_revision": parent})
        children = [
            key for key, candidate_parent in revisions.items() if candidate_parent == revision
        ]
        if len(children) > 1:
            raise AssertionError(f"branching Alembic history at {revision}: {children}")
        revision = children[0] if children else ""
    assert len(ordered) == len(revisions), "Alembic history is disconnected"
    return ordered


def _normalize_sql(sql: str | None) -> str | None:
    return None if sql is None else re.sub(r"\s+", " ", sql).strip()


def current_database_schema(database_path: Path) -> dict[str, Any]:
    """Migrate a real SQLite database and return its semantic catalog."""
    run_migrations(f"sqlite:///{database_path}")
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        table_rows = connection.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        tables: dict[str, Any] = {}
        for table_row in table_rows:
            table = str(table_row["name"])
            quoted = table.replace('"', '""')
            columns = [
                {
                    "cid": row["cid"],
                    "name": row["name"],
                    "type": row["type"],
                    "not_null": bool(row["notnull"]),
                    "default": row["dflt_value"],
                    "primary_key_position": row["pk"],
                    "hidden": row["hidden"],
                }
                for row in connection.execute(f'PRAGMA table_xinfo("{quoted}")')
            ]
            indexes = []
            for row in connection.execute(f'PRAGMA index_list("{quoted}")'):
                index_name = str(row["name"])
                escaped_index = index_name.replace('"', '""')
                indexes.append(
                    {
                        "name": index_name,
                        "unique": bool(row["unique"]),
                        "origin": row["origin"],
                        "partial": bool(row["partial"]),
                        "columns": [
                            item["name"]
                            for item in connection.execute(f'PRAGMA index_info("{escaped_index}")')
                        ],
                    }
                )
            foreign_keys = [
                dict(row) for row in connection.execute(f'PRAGMA foreign_key_list("{quoted}")')
            ]
            tables[table] = {
                "columns": columns,
                "indexes": sorted(indexes, key=lambda item: item["name"]),
                "foreign_keys": sorted(foreign_keys, key=lambda item: (item["id"], item["seq"])),
                # SQLite exposes CHECK/UNIQUE/FK constraint clauses most faithfully
                # through sqlite_master; whitespace is removed as an unstable input.
                "constraint_definition": _normalize_sql(table_row["sql"]),
            }
        return {"alembic_revisions": _migration_revisions(), "tables": tables}
    finally:
        connection.close()


def test_openapi_paths_and_components_match_semantic_snapshot() -> None:
    assert current_openapi_contract() == _load("backend_openapi.json")


def test_openapi_normalization_preserves_property_names_that_match_metadata_keys() -> None:
    """Schema fields named like OpenAPI prose metadata remain contract-bearing."""
    schemas = current_openapi_contract()["components"]["schemas"]
    raw_schemas = create_app(SimpleNamespace(regulus_client=None)).openapi()["components"][
        "schemas"
    ]
    for schema_name, raw_schema in raw_schemas.items():
        assert set(schemas[schema_name].get("properties", {})) == set(
            raw_schema.get("properties", {})
        )
    assert "description" in schemas["CreateTemplateRequest"]["properties"]
    assert "summary" in schemas["ApprovalRecord"]["properties"]
    assert "description" not in schemas["CreateTemplateRequest"]
    assert "title" not in schemas["CreateTemplateRequest"]["properties"]["description"]


def test_alembic_order_and_actual_sqlite_schema_match_snapshot(tmp_path: Path) -> None:
    assert current_database_schema(tmp_path / "contract.db") == _load("database_schema.json")


async def test_persisted_run_thread_and_checkpoint_models_round_trip(sqlite_db) -> None:
    run_repository = RunRepository.for_default_compatibility(sqlite_db)
    thread_repository = ThreadRepository.for_default_compatibility(sqlite_db)
    run = Run(
        run_id="run-contract",
        thread_id="thread-contract",
        graph_version_ref="graph-contract@1",
        deployment_ref="deployment-contract",
        workflow_name="contract-workflow",
        current_node_ids=["start"],
        pending_node_ids=["finish"],
        completed_steps=["start"],
        execution_history=[
            RunHistoryEntry(
                node_id="start",
                status="completed",
                input_snapshot={"value": 1},
                output_snapshot={"value": 2},
                started_at=_FIXED_TIME,
                completed_at=_FIXED_TIME,
            )
        ],
        started_at=_FIXED_TIME,
        updated_at=_FIXED_TIME,
    )
    persisted_run = await run_repository.create(run)
    assert persisted_run.model_dump(mode="json") == run.model_dump(mode="json")

    checkpoint_id = await run_repository.write_checkpoint(run)
    checkpoint = await run_repository.get_checkpoint(checkpoint_id)
    assert checkpoint is not None
    assert checkpoint.model_dump(mode="json") == run.model_dump(mode="json")

    thread = Thread(
        thread_id="thread-separate-contract",
        graph_version_ref="graph-contract@1",
        deployment_ref="deployment-contract",
        participating_agent_refs=["agent-contract"],
        memory_bindings=[
            ThreadMemoryBinding(
                connector_id="memory-contract",
                instance_id="instance-contract",
                scope="thread",
            )
        ],
        run_ids=["run-contract"],
        active_run_id="run-contract",
        last_run_id="run-contract",
        created_at=_FIXED_TIME,
        updated_at=_FIXED_TIME,
    )
    persisted_thread = await thread_repository.create(thread)
    assert persisted_thread.model_dump(mode="json") == thread.model_dump(mode="json")


async def test_persisted_audit_and_approval_models_round_trip(sqlite_db) -> None:
    run = Run(
        run_id="run-record-contract",
        thread_id="thread-record-contract",
        graph_version_ref="graph-contract@1",
        deployment_ref="deployment-contract",
        started_at=_FIXED_TIME,
        updated_at=_FIXED_TIME,
    )
    await RunRepository.for_default_compatibility(sqlite_db).create(run)

    audit = NodeAuditRecord(
        tenant_id="default",
        workspace_id=None,
        audit_id="audit-contract",
        run_id=run.run_id,
        thread_id=run.thread_id,
        node_id="node-contract",
        graph_version_ref=run.graph_version_ref,
        deployment_ref=run.deployment_ref,
        status="completed",
        input_snapshot={"secret": "normalized", "value": 1},
        output_snapshot={"value": 2},
        started_at=_FIXED_TIME,
        completed_at=_FIXED_TIME,
    )
    persisted_audit = await AuditRepository.for_default_compatibility(sqlite_db).write(audit)
    assert persisted_audit is not None
    # The durable write is the capture boundary, so what round-trips is exactly
    # what the capture policy produced -- not what the producer submitted. The
    # comparison stays byte-for-byte outside policy-keyed HMAC values: storage
    # adds nothing and drops nothing of its own beyond the chain fields excluded
    # below. Each policy owns a random key, so only valid digest values are
    # normalized while their keys, schemas, counts, and surrounding content stay
    # exact.
    expected = AuditCapturePolicy().apply(audit)
    hmac_value = re.compile(r'(?<="hmac_sha256": ")[0-9a-f]{64}(?=")')
    hmac_schema_key = re.compile(
        r'(?<=\[")[0-9a-f]{16}(?=", "(?:NoneType|bool|int|float|str|dict|list)"\])'
    )
    excluded = {"chain_sequence", "digest_version", "pii_commitments", "record_digest"}
    persisted_json = json.dumps(
        persisted_audit.model_dump(mode="json", exclude=excluded), sort_keys=True
    )
    expected_json = json.dumps(expected.model_dump(mode="json", exclude=excluded), sort_keys=True)

    def normalize_policy_keyed_values(rendered: str) -> Any:
        value = json.loads(
            hmac_schema_key.sub("<policy-keyed>", hmac_value.sub("<policy-keyed>", rendered))
        )

        def normalize_entries(item: Any) -> Any:
            if isinstance(item, dict):
                normalized = {key: normalize_entries(value) for key, value in item.items()}
                if isinstance(normalized.get("<entries>"), list):
                    normalized["<entries>"] = sorted(
                        normalized["<entries>"], key=lambda entry: json.dumps(entry, sort_keys=True)
                    )
                return normalized
            if isinstance(item, list):
                return [normalize_entries(value) for value in item]
            return item

        return normalize_entries(value)

    normalized_persisted = normalize_policy_keyed_values(persisted_json)
    normalized_expected = normalize_policy_keyed_values(expected_json)
    assert normalized_persisted == normalized_expected
    assert persisted_audit.input_snapshot == {}
    assert persisted_audit.chain_sequence == 1
    assert persisted_audit.digest_version == 3
    assert re.fullmatch(r"[0-9a-f]{64}", persisted_audit.record_digest or "")

    approval = ApprovalRecord(
        approval_id="approval-contract",
        run_id=run.run_id,
        thread_id=run.thread_id,
        node_id="node-contract",
        graph_version_ref=run.graph_version_ref,
        deployment_ref=run.deployment_ref,
        summary="Approve contract characterization",
        rationale="Protect persisted approval semantics",
        context_excerpt={"value": 1},
        created_at=_FIXED_TIME,
        updated_at=_FIXED_TIME,
    )
    persisted_approval = await ApprovalRepository(sqlite_db).write(approval)
    assert persisted_approval.model_dump(mode="json") == approval.model_dump(mode="json")


async def test_persisted_cleanup_manifest_round_trip(sqlite_db) -> None:
    tenant_id = "tenant-contract"
    run_id = "run-cleanup-contract"
    artifact_operation = CleanupOperation(
        operation_id=operation_id(tenant_id, run_id, "artifact_prefix", run_id),
        kind="artifact_prefix",
        tenant_id=tenant_id,
        run_id=run_id,
    )
    econ_operation = CleanupOperation(
        operation_id=operation_id(tenant_id, run_id, "econ", run_id),
        kind="econ",
        tenant_id=tenant_id,
        run_id=run_id,
        join_keys=[run_id],
    )
    manifest = CleanupManifest(
        tenant_id=tenant_id,
        run_id=run_id,
        reason="manual",
        database_result=DatabaseErasureOutcome(
            audits_erased=2,
            checkpoints_deleted=1,
            run_redacted=True,
        ),
        operations=[artifact_operation, econ_operation],
    )
    scope_context = NullWorkspaceScopeContext(tenant_id=tenant_id)
    log_repository = RetentionAuditLogRepository(sqlite_db, scope_context)
    cleanup_repository = CleanupStateRepository(sqlite_db, scope_context)
    authorization_log_id = await log_repository.record(
        run_id=run_id,
        action="erasure_authorized",
        reason="manual",
        detail={"manifest": manifest.model_dump(mode="json")},
    )
    async with sqlite_db.transaction(write_lock=True) as connection:
        await cleanup_repository.initialize_in_transaction(
            connection,
            authorization_log_id=authorization_log_id,
            manifest=manifest,
        )
        state = await cleanup_repository.get_state_in_transaction(
            connection, authorization_log_id
        )
        operations = await cleanup_repository.list_operations_in_transaction(
            connection, authorization_log_id
        )

    assert state is not None
    assert (state.tenant_id, state.run_id, state.reason) == (
        manifest.tenant_id,
        manifest.run_id,
        manifest.reason,
    )
    assert sorted(
        (item.operation_id, item.status, item.deleted_count) for item in operations
    ) == sorted(
        (item.operation_id, item.status, item.deleted_count) for item in manifest.operations
    )
    log_row = await log_repository.get(authorization_log_id)
    assert log_row is not None
    stored_detail = json.loads(log_row["detail"])
    assert CleanupManifest.model_validate(stored_detail["manifest"]) == manifest


def test_representative_public_exception_types_and_semantics() -> None:
    missing = ContractNotFoundError("contract://missing@1")
    assert isinstance(missing, ContractRegistryError)
    assert str(missing) == "contract://missing@1"

    issue = ValidationIssue(
        severity=ValidationSeverity.ERROR,
        code=ValidationCode.EMPTY_GRAPH,
        message="graph requires at least one node",
        graph_id="graph-contract",
        path=("nodes",),
    )
    report = GraphValidationReport(graph_id="graph-contract", issues=[issue])
    graph_error = GraphValidationError(report)
    assert isinstance(graph_error, ValueError)
    assert graph_error.report is report
    assert str(graph_error) == "graph 'graph-contract' failed validation: 1 error(s), 0 warning(s)"

    denied = CapabilityDeniedError(
        node_id="agent-contract",
        required={Capability.NETWORK_READ, Capability.SECRET_ACCESS},
        granted={Capability.NETWORK_READ},
    )
    assert isinstance(denied, PermissionError)
    assert denied.missing == {Capability.SECRET_ACCESS}
    assert str(denied) == (
        "capability denied for node 'agent-contract': missing secret_access. "
        "required [network_read, secret_access]; granted [network_read]."
    )
