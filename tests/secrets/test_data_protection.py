from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from zeroth.contracts.graph import AgentNode, AgentNodeData, ExecutionSettings, Graph
from zeroth.governance.audit import AuditRepository
from zeroth.governance.audit.capture_policy import CAPTURE_METADATA_KEY
from zeroth.governance.audit.capture_projection import canonicalize, digest
from zeroth.integrations.execution import EnvironmentVariable
from zeroth.integrations.persistence.runs import RunRepository, ThreadRepository
from zeroth.platform.secrets import EnvSecretProvider, SecretResolver
from zeroth.platform.storage import EncryptedField
from zeroth.platform.storage.async_sqlite import AsyncSQLiteDatabase
from zeroth.runtime.agents.thread_store import (
    RepositoryThreadResolver,
    RepositoryThreadStateStore,
)
from zeroth.runtime.orchestration import RuntimeOrchestrator
from zeroth.runtime.orchestration.audit_recorder import RuntimeAuditRecorder
from zeroth.runtime.runs import RunStatus
from zeroth.service.bootstrap.migrations import run_migrations


def _payload_digest(payload: dict[str, object]) -> str:
    """Digest a payload exactly as the capture boundary's projection does."""
    return digest(canonicalize(payload))


def test_encrypted_field_round_trips_plaintext() -> None:
    encrypted = EncryptedField(EncryptedField.generate_key())

    ciphertext = encrypted.encrypt("top-secret")

    assert ciphertext != "top-secret"
    assert encrypted.decrypt(ciphertext) == "top-secret"


async def test_checkpoints_do_not_persist_raw_secret_values(tmp_path: Path) -> None:
    db_path = str(tmp_path / "checkpoints.db")
    encryption_key = EncryptedField.generate_key()
    run_migrations(f"sqlite:///{db_path}")
    database = AsyncSQLiteDatabase(path=db_path, encryption_key=encryption_key)

    run_repository = RunRepository(database)
    thread_repository = ThreadRepository(database)
    resolver = RepositoryThreadResolver(thread_repository)
    created = await resolver.resolve(
        None,
        graph_version_ref="graph:v1",
        deployment_ref="deployment:v1",
        run_id="run-a",
    )
    store = RepositoryThreadStateStore(
        database,
        tenant_id="default",
        workspace_id=None,
        run_repository=run_repository,
        thread_repository=thread_repository,
    )

    checkpoint_id = await store.checkpoint(
        created.thread.thread_id,
        {"secret": "top-secret", "nested": {"token": "abc123"}},
    )

    async with database.transaction() as connection:
        row = await connection.fetch_one(
            "SELECT state_json FROM run_checkpoints WHERE checkpoint_id = ?",
            (checkpoint_id,),
        )
    assert row is not None
    assert "top-secret" not in row["state_json"]
    assert "abc123" not in row["state_json"]

    loaded = await store.load(created.thread.thread_id)
    assert loaded == {"secret": "top-secret", "nested": {"token": "abc123"}}
    await database.close()


async def test_audit_records_do_not_contain_raw_secret_values_at_rest(tmp_path: Path) -> None:
    db_path = str(tmp_path / "audit.db")
    encryption_key = EncryptedField.generate_key()
    run_migrations(f"sqlite:///{db_path}")
    database = AsyncSQLiteDatabase(path=db_path, encryption_key=encryption_key)

    audit_repository = AuditRepository.for_default_compatibility(database)
    run_repository = RunRepository(database)
    secret_resolver = SecretResolver(EnvSecretProvider({"API_KEY": "super-secret"}))
    secret_resolver.resolve_environment_variables(
        [EnvironmentVariable(name="API_KEY", secret_ref="API_KEY")]
    )

    class SecretEchoRunner:
        async def run(self, input_payload, **kwargs):  # noqa: ANN001, ANN201
            del kwargs
            return SimpleNamespace(
                output_data={"value": input_payload["value"]},
                audit_record={"secret": "super-secret"},
            )

    graph = Graph(
        graph_id="graph-secret-audit",
        name="secret-audit",
        entry_step="agent",
        execution_settings=ExecutionSettings(max_total_steps=3),
        nodes=[
            AgentNode(
                node_id="agent",
                graph_version_ref="graph-secret-audit:v1",
                input_contract_ref="contract://input",
                output_contract_ref="contract://output",
                agent=AgentNodeData(instruction="echo", model_provider="provider://demo"),
            )
        ],
        edges=[],
    )
    orchestrator = RuntimeOrchestrator(
        audit_repository=audit_repository,
        run_repository=run_repository,
        agent_runners={"agent": SecretEchoRunner()},  # type: ignore[arg-type]
        executable_unit_runner=SimpleNamespace(),  # type: ignore[arg-type]
        secret_resolver=secret_resolver,
    )

    run = await orchestrator.run_graph(graph, {"value": "super-secret"})

    assert run.status is RunStatus.COMPLETED
    audits = await audit_repository.list_by_run(run.run_id)
    audit = audits[0]
    # Capture empties the content channels, so the record no longer carries the
    # masked snapshot -- it carries no snapshot at all. Asserting only ``== {}``
    # would stop distinguishing "the producer masked the secret" from "the
    # producer's redactor is a no-op", which is exactly what
    # ``RuntimeAuditRecorder.redact`` becomes without a secret resolver. The
    # content-free stand-ins are what keep the assertion honest: the digest that
    # replaced the snapshot is the digest of the *redacted* payload.
    assert audit.input_snapshot == {}
    dropped = audit.execution_metadata[CAPTURE_METADATA_KEY]["dropped_fields"]
    assert dropped["input_snapshot"]["count"] == 1
    assert dropped["input_snapshot"]["sha256"] == _payload_digest({"value": "[REDACTED:API_KEY]"})
    assert dropped["input_snapshot"]["sha256"] != _payload_digest({"value": "super-secret"})

    async with database.transaction() as connection:
        row = await connection.fetch_one("SELECT record_json FROM node_audits", ())
    assert row is not None
    assert "super-secret" not in row["record_json"]
    await database.close()


def test_the_runtime_recorder_masks_registered_secrets_before_the_audit_write() -> None:
    """The producer-side redactor, covered on its own so unwiring it fails here.

    ``RuntimeAuditRecorder.redact`` is a pass-through when no secret resolver is
    injected -- the opposite posture from the capture boundary. The at-rest test
    above can only see the digest of whatever the producer handed over, so this
    is where "the resolver is actually wired to the redactor" is pinned.
    """
    resolver = SecretResolver(EnvSecretProvider({"API_KEY": "super-secret"}))
    resolver.resolve_environment_variables(
        [EnvironmentVariable(name="API_KEY", secret_ref="API_KEY")]
    )

    masked = RuntimeAuditRecorder(secret_resolver=resolver).redact({"value": "super-secret"})
    unwired = RuntimeAuditRecorder().redact({"value": "super-secret"})

    assert masked == {"value": "[REDACTED:API_KEY]"}
    assert unwired == {"value": "super-secret"}


async def test_failure_error_and_message_are_redacted(tmp_path: Path) -> None:
    # S6: a node exception whose text echoes a resolved secret must be redacted in
    # BOTH the persisted audit record's `error` column and the run's
    # failure_state.message (which the public run API returns verbatim).
    db_path = str(tmp_path / "audit_fail.db")
    encryption_key = EncryptedField.generate_key()
    run_migrations(f"sqlite:///{db_path}")
    database = AsyncSQLiteDatabase(path=db_path, encryption_key=encryption_key)

    audit_repository = AuditRepository.for_default_compatibility(database)
    run_repository = RunRepository(database)
    secret_resolver = SecretResolver(EnvSecretProvider({"API_KEY": "super-secret"}))
    secret_resolver.resolve_environment_variables(
        [EnvironmentVariable(name="API_KEY", secret_ref="API_KEY")]
    )

    class SecretLeakingRunner:
        async def run(self, input_payload, **kwargs):  # noqa: ANN001, ANN201
            del input_payload, kwargs
            # httpx/provider errors routinely interpolate the request URL/headers.
            raise RuntimeError("upstream 401: token=super-secret rejected")

    graph = Graph(
        graph_id="graph-secret-fail",
        name="secret-fail",
        entry_step="agent",
        execution_settings=ExecutionSettings(max_total_steps=3),
        nodes=[
            AgentNode(
                node_id="agent",
                graph_version_ref="graph-secret-fail:v1",
                input_contract_ref="contract://input",
                output_contract_ref="contract://output",
                agent=AgentNodeData(instruction="echo", model_provider="provider://demo"),
            )
        ],
        edges=[],
    )
    orchestrator = RuntimeOrchestrator(
        audit_repository=audit_repository,
        run_repository=run_repository,
        agent_runners={"agent": SecretLeakingRunner()},  # type: ignore[arg-type]
        executable_unit_runner=SimpleNamespace(),  # type: ignore[arg-type]
        secret_resolver=secret_resolver,
    )

    run = await orchestrator.run_graph(graph, {"value": "hello"})

    assert run.status is RunStatus.FAILED
    assert run.failure_state is not None
    # failure_state.message is redacted (this is what GET /runs/{id} returns).
    assert "super-secret" not in (run.failure_state.message or "")
    assert "[REDACTED:API_KEY]" in (run.failure_state.message or "")
    # The persisted audit record's `error` is redacted too.
    audits = await audit_repository.list_by_run(run.run_id)
    errored = [a for a in audits if a.error]
    assert errored
    assert all("super-secret" not in (a.error or "") for a in errored)
    async with database.transaction() as connection:
        row = await connection.fetch_one("SELECT record_json FROM node_audits", ())
    assert row is not None
    assert "super-secret" not in (row["record_json"] or "")
    await database.close()
