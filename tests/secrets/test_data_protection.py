from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from zeroth.core.agent_runtime.thread_store import RepositoryThreadResolver, RepositoryThreadStateStore
from zeroth.core.audit import AuditRepository
from zeroth.core.execution_units import EnvironmentVariable
from zeroth.core.graph import AgentNode, AgentNodeData, ExecutionSettings, Graph
from zeroth.core.orchestrator import RuntimeOrchestrator
from zeroth.core.runs import RunRepository, RunStatus, ThreadRepository
from zeroth.core.secrets import EnvSecretProvider, SecretResolver
from zeroth.core.service.bootstrap import run_migrations
from zeroth.core.storage import EncryptedField
from zeroth.core.storage.async_sqlite import AsyncSQLiteDatabase


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

    audit_repository = AuditRepository(database)
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
    assert audit.input_snapshot == {"value": "[REDACTED:API_KEY]"}
    assert audit.execution_metadata["secret"] == "[REDACTED:API_KEY]"

    async with database.transaction() as connection:
        row = await connection.fetch_one("SELECT record_json FROM node_audits", ())
    assert row is not None
    assert "super-secret" not in row["record_json"]
    await database.close()
