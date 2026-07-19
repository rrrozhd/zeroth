"""WS-E: full-surface erasure, legal holds, and the retention audit log."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from tests.retention.conftest import make_audit_record
from zeroth.core.audit.verifier import _compute_pii_commitments, compute_chained_record
from zeroth.core.retention.erasure_service import LegalHoldError
from zeroth.platform.storage.json import to_json_value


async def _pii_present(database, ssn: str) -> dict[str, bool]:
    """Scan every plaintext surface for a raw PII string."""
    async with database.transaction() as connection:
        audits = await connection.fetch_all("SELECT record_json FROM node_audits", ())
        checkpoints = await connection.fetch_all("SELECT state_json FROM run_checkpoints", ())
        runs = await connection.fetch_all(
            "SELECT final_output, artifacts, metadata, error FROM runs", ()
        )
    return {
        "node_audits": any(ssn in (row["record_json"] or "") for row in audits),
        "run_checkpoints": any(ssn in (row["state_json"] or "") for row in checkpoints),
        "runs": any(ssn in "".join(str(row[c] or "") for c in row) for row in runs),
    }


async def test_full_surface_erasure(env) -> None:
    ssn = "999-88-7777"
    await env.seed_run("run-full", n_audits=3, artifact_key="run-full/n0/blob", ssn=ssn)

    # Pre-condition: PII is everywhere.
    before = await _pii_present(env.database, ssn)
    assert before == {"node_audits": True, "run_checkpoints": True, "runs": True}
    assert env.artifact_store.blobs.get("run-full/n0/blob") == b"pii"

    result = await env.service.erase_run("run-full", "rte")

    assert result.audits_erased == 3
    assert result.checkpoints_deleted >= 1
    assert result.run_redacted is True
    assert result.artifacts_deleted >= 1

    # No seeded PII string remains on ANY surface.
    after = await _pii_present(env.database, ssn)
    assert after == {"node_audits": False, "run_checkpoints": False, "runs": False}
    # Checkpoints deleted, run row kept (redacted), artifact gone.
    async with env.database.transaction() as connection:
        cp = await connection.fetch_all(
            "SELECT 1 FROM run_checkpoints WHERE run_id = ?", ("run-full",)
        )
        run_row = await connection.fetch_one(
            "SELECT run_id, artifacts, metadata FROM runs WHERE run_id = ?", ("run-full",)
        )
    assert cp == []
    assert run_row is not None  # continuity: row kept
    assert run_row["artifacts"] == "{}" and run_row["metadata"] == "{}"
    assert "run-full/n0/blob" not in env.artifact_store.blobs
    assert env.artifact_store.cleanup_calls == ["run-full"]


async def test_erasure_is_idempotent(env) -> None:
    await env.seed_run("run-idem", n_audits=2, ssn="111-11-1111")
    first = await env.service.erase_run("run-idem", "rte")
    assert first.audits_erased == 2
    # Second pass: everything already gone, no crash, no double-count.
    second = await env.service.erase_run("run-idem", "rte")
    assert second.audits_erased == 0
    assert second.checkpoints_deleted == 0


async def test_legal_hold_refuses_right_to_erasure(env) -> None:
    await env.seed_run("run-held", n_audits=2, ssn="222-22-2222")
    await env.hold_repo.place("default", run_id="run-held", reason="litigation")

    with pytest.raises(LegalHoldError):
        await env.service.erase_run("run-held", "rte")

    # PII untouched while held.
    present = await _pii_present(env.database, "222-22-2222")
    assert present["node_audits"] is True

    # Release, then erasure proceeds.
    holds = await env.hold_repo.list_for_tenant("default")
    await env.hold_repo.release(holds[0].hold_id)
    result = await env.service.erase_run("run-held", "rte")
    assert result.audits_erased == 2
    after = await _pii_present(env.database, "222-22-2222")
    assert after["node_audits"] is False


async def test_tenant_wide_hold_blocks_erasure(env) -> None:
    await env.seed_run("run-tw", tenant_id="default", n_audits=1, ssn="333-33-3333")
    await env.hold_repo.place("default", run_id=None, reason="tenant freeze")
    with pytest.raises(LegalHoldError):
        await env.service.erase_run("run-tw", "rte")


async def test_erasure_writes_retention_audit_log(env) -> None:
    await env.seed_run("run-log", n_audits=2, ssn="444-44-4444")
    await env.service.erase_run("run-log", "rte")

    entries = await env.log_repo.list_for_run("run-log")
    actions = [e["action"] for e in entries]
    assert "crypto_erase_audits" in actions
    assert "erase_checkpoints" in actions
    assert "redact_run" in actions
    assert "artifact_cleanup" in actions
    assert "erase_run_complete" in actions
    # Econ step recorded as skipped (no eraser wired).
    assert "econ_erase_skipped" in actions
    assert all(e["reason"] == "rte" for e in entries)


async def test_refused_hold_is_logged(env) -> None:
    await env.seed_run("run-refuse", n_audits=1, ssn="555-55-5555")
    await env.hold_repo.place("default", run_id="run-refuse")
    with pytest.raises(LegalHoldError):
        await env.service.erase_run("run-refuse", "rte")
    actions = [e["action"] for e in await env.log_repo.list_for_run("run-refuse")]
    assert actions == ["erasure_refused_legal_hold"]


class _FailOnceArtifactStore:
    def __init__(self, keys: list[str], *, fail_key: str) -> None:
        self.blobs = {key: key.encode() for key in keys}
        self.delete_calls: list[str] = []
        self._failed = False
        self._fail_key = fail_key

    async def cleanup_run(self, run_id: str, *, idempotency_key: str) -> int:
        return 0

    async def delete(self, key: str, *, idempotency_key: str) -> bool:
        self.delete_calls.append(key)
        if key == self._fail_key and not self._failed:
            self._failed = True
            raise RuntimeError("injected external cleanup failure")
        return self.blobs.pop(key, None) is not None


async def test_external_cleanup_retry_reloads_manifest_and_skips_completed(env) -> None:
    artifact_keys = [
        "run-retry/approval/key",
        "run-retry/checkpoint/key",
        "run-retry/condition/key",
        "run-retry/memory/key",
        "run-retry/snapshot/key",
        "run-retry/tool-arguments/key",
        "run-retry/tool-outcome/key",
        "run-retry/validation/key",
    ]
    store = _FailOnceArtifactStore(artifact_keys, fail_key="run-retry/validation/key")
    env.service._artifact_store = store
    await env.seed_run("run-retry", n_audits=0, ssn="909-90-9090")
    base_record = make_audit_record(
        audit_id="run-retry-a0",
        run_id="run-retry",
        artifact_key="run-retry/snapshot/key",
        ssn="909-90-9090",
    )
    record = base_record.__class__.model_validate(
        {
            **base_record.model_dump(mode="python"),
            "validation_results": {
                "nested": {
                    "artifact": {
                        "store": "filesystem",
                        "key": "run-retry/validation/key",
                        "content_type": "application/octet-stream",
                        "size": 3,
                    },
                    "join_key": "validation-join",
                }
            },
            "condition_results": [
                {
                    "payload": {
                        "store": "filesystem",
                        "key": "run-retry/condition/key",
                        "content_type": "application/octet-stream",
                        "size": 3,
                    },
                    "join_key": "condition-join",
                }
            ],
            "memory_interactions": [
                {
                    "memory_ref": "memory",
                    "connector_type": "kv",
                    "scope": "run",
                    "operation": "write",
                    "key": "record",
                    "value": {
                        "artifact": {
                            "store": "filesystem",
                            "key": "run-retry/memory/key",
                            "content_type": "application/octet-stream",
                            "size": 3,
                        },
                        "join_key": "memory-join",
                    },
                }
            ],
            "tool_calls": [
                {
                    "tool_ref": "tool",
                    "alias": "nested",
                    "arguments": {
                        "artifact": {
                            "store": "filesystem",
                            "key": "run-retry/tool-arguments/key",
                            "content_type": "application/octet-stream",
                            "size": 3,
                        },
                        "join_key": "tool-arguments-join",
                    },
                    "outcome": {
                        "artifact": {
                            "store": "filesystem",
                            "key": "run-retry/tool-outcome/key",
                            "content_type": "application/octet-stream",
                            "size": 3,
                        },
                        "join_key": "tool-outcome-join",
                    },
                }
            ],
            "approval_actions": [
                {
                    "approval_id": "approval",
                    "action": "approved",
                    "metadata": {
                        "artifact": {
                            "store": "filesystem",
                            "key": "run-retry/approval/key",
                            "content_type": "application/octet-stream",
                            "size": 3,
                        },
                        "join_key": "approval-join",
                        "duplicates": [
                            {
                                "store": "filesystem",
                                "key": "run-retry/validation/key",
                                "content_type": "application/octet-stream",
                                "size": 3,
                            },
                            {"join_key": "validation-join"},
                        ],
                    },
                }
            ],
            "execution_metadata": {"join_key": "metadata-join"},
        }
    )
    await env.audit_repo.write(record)
    # A second key exists only in the database checkpoint/run payload, proving
    # authorization harvests every database-resident surface before redaction.
    async with env.database.transaction() as connection:
        row = await connection.fetch_one(
            "SELECT checkpoint_id, state_json FROM run_checkpoints WHERE run_id = ?",
            ("run-retry",),
        )
        assert row is not None
        state = json.loads(row["state_json"])
        state["metadata"]["external"] = {
            "store": "filesystem",
            "key": "run-retry/checkpoint/key",
            "content_type": "application/octet-stream",
            "size": 3,
        }
        await connection.execute(
            "UPDATE run_checkpoints SET state_json = ? WHERE checkpoint_id = ?",
            (json.dumps(state), row["checkpoint_id"]),
        )

    first = await env.service.erase_run("run-retry", "rte")
    assert first.audits_erased == 1
    assert store.delete_calls == artifact_keys
    assert set(store.blobs) == {"run-retry/validation/key"}

    erased = (await env.audit_repo.list_by_run("run-retry"))[0]
    assert erased.validation_results == {}
    assert erased.condition_results == []
    assert erased.memory_interactions == []
    assert erased.tool_calls == []
    assert erased.approval_actions == []

    entries = await env.log_repo.list_for_run("run-retry")
    authorization = next(row for row in entries if row["action"] == "erasure_authorized")
    manifest = json.loads(authorization["detail"])
    artifact_operations = [
        operation for operation in manifest["operations"] if operation["kind"] == "artifact_key"
    ]
    assert [operation["artifact_key"] for operation in artifact_operations] == artifact_keys
    econ_operation = next(
        operation for operation in manifest["operations"] if operation["kind"] == "econ"
    )
    assert econ_operation["join_keys"] == ["metadata-join", "run-retry"]
    assert artifact_operations[0]["status"] == "pending"
    assert any(row["action"] == "external_cleanup_failed" for row in entries)

    retried = await env.service.retry_external_cleanup(authorization["log_id"])
    assert retried.artifacts_deleted == len(artifact_keys)
    assert store.delete_calls == [*artifact_keys, "run-retry/validation/key"]
    assert store.blobs == {}
    entries = await env.log_repo.list_for_run("run-retry")
    assert any(row["action"] == "external_cleanup_completed" for row in entries)


@pytest.mark.parametrize(
    "structured_update",
    [
        {"condition_results": [{"subject": "legacy pii"}]},
        {
            "approval_actions": [
                {
                    "approval_id": "legacy-approval",
                    "action": "approved",
                    "metadata": {"subject": "legacy pii"},
                }
            ]
        },
    ],
    ids=["condition-results", "approval-actions"],
)
async def test_legacy_erased_v2_structured_payload_blocks_reauthorization(
    env,
    structured_update: dict[str, object],
) -> None:
    """An old partial tombstone must not be mistaken for safely erased data."""
    await env.seed_run("run-partial-v2", n_audits=0, ssn="818-81-8181")
    base_record = make_audit_record(
        audit_id="run-partial-v2-a0",
        run_id="run-partial-v2",
        ssn="818-81-8181",
    )
    original = base_record.__class__.model_validate(
        {
            **base_record.model_dump(mode="python"),
            "digest_version": 2,
            **structured_update,
        }
    )
    prepared = original.model_copy(update={"pii_commitments": _compute_pii_commitments(original)})
    chained = compute_chained_record(prepared, None, None)
    partial_tombstone = chained.model_copy(
        update={
            "input_snapshot": {},
            "output_snapshot": {},
            "validation_results": {},
            "execution_metadata": {},
            "stdout": None,
            "stderr": None,
            "error": None,
            "tool_calls": [],
            "memory_interactions": [],
            "erased": True,
            "erased_at": datetime.now(UTC),
            "erasure_reason": "rte",
        }
    )
    async with env.database.transaction() as connection:
        await connection.execute(
            """
            INSERT INTO node_audits
                (audit_id, run_id, thread_id, node_id, graph_version_ref,
                 deployment_ref, tenant_id, workspace_id, created_at, record_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                partial_tombstone.audit_id,
                partial_tombstone.run_id,
                None,
                partial_tombstone.node_id,
                partial_tombstone.graph_version_ref,
                partial_tombstone.deployment_ref,
                partial_tombstone.tenant_id,
                None,
                datetime.now(UTC).isoformat(),
                to_json_value(partial_tombstone.model_dump(mode="json")),
            ),
        )
        audit_before = await connection.fetch_one(
            "SELECT record_json FROM node_audits WHERE audit_id = ?",
            (partial_tombstone.audit_id,),
        )

    with pytest.raises(ValueError, match="digest_version=2"):
        await env.service.erase_run("run-partial-v2", "rte")

    assert await env.log_repo.list_for_run("run-partial-v2") == []
    async with env.database.transaction() as connection:
        checkpoints = await connection.fetch_all(
            "SELECT checkpoint_id FROM run_checkpoints WHERE run_id = ?",
            ("run-partial-v2",),
        )
        run = await connection.fetch_one(
            "SELECT final_output FROM runs WHERE run_id = ?",
            ("run-partial-v2",),
        )
        audit_after = await connection.fetch_one(
            "SELECT record_json FROM node_audits WHERE audit_id = ?",
            (partial_tombstone.audit_id,),
        )
        coordination = await connection.fetch_one(
            "SELECT tenant_id FROM retention_coordination WHERE tenant_id = ?",
            ("default",),
        )
    assert checkpoints
    assert run is not None and run["final_output"] is not None
    assert audit_after == audit_before
    assert coordination is None
