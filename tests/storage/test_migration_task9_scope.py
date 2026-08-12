"""Migration 025 gives Task 9 repositories direct scope-local identities."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine, make_url

from tests.conftest import requires_docker


_TABLES = (
    "runs",
    "token_engine_snapshots",
    "webhook_subscriptions",
    "webhook_deliveries",
    "webhook_dead_letters",
    "retention_cleanup_operations",
)


def _config(database_url: str) -> Config:
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "src/zeroth/service/_migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _signature(engine: Engine) -> dict[str, dict[str, object]]:
    inspector = inspect(engine)
    return {
        table: {
            "columns": [
                (column["name"], column["nullable"], column["default"])
                for column in inspector.get_columns(table)
            ],
            "primary_key": inspector.get_pk_constraint(table)["constrained_columns"],
            "indexes": sorted(
                (index["name"], bool(index["unique"]), tuple(index["column_names"]))
                for index in inspector.get_indexes(table)
            ),
            "foreign_keys": sorted(
                (
                    tuple(foreign_key["constrained_columns"]),
                    foreign_key["referred_table"],
                    tuple(foreign_key["referred_columns"]),
                )
                for foreign_key in inspector.get_foreign_keys(table)
            ),
        }
        for table in _TABLES
    }


def _seed_024(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                """INSERT INTO runs (
                    run_id, epoch, workflow_name, status, completed_steps, artifacts,
                    channels, started_at, updated_at, metadata, graph_version_ref,
                    deployment_ref, thread_id, current_node_ids, tenant_id, workspace_id
                ) VALUES (
                    'shared-run', 0, 'workflow', 'completed', '[]', '{}', '{}',
                    '2026-08-12', '2026-08-12', '{}', 'graph:v1', 'deployment:v1',
                    'thread-a', '[]', 'tenant-a', 'workspace-a'
                )"""
            )
        )
        connection.execute(
            text(
                """INSERT INTO token_engine_snapshots (
                    run_id, revision, schema_version, next_token_ordinal,
                    snapshot_json, updated_at
                ) VALUES ('shared-run', 1, 1, 2, '{}', '2026-08-12')"""
            )
        )
        connection.execute(
            text(
                """INSERT INTO webhook_subscriptions (
                    subscription_id, deployment_ref, tenant_id, target_url, secret,
                    event_types, created_at, updated_at
                ) VALUES (
                    'shared-subscription', 'deployment:v1', 'tenant-a',
                    'https://example.test/hook', 'secret', '["run.completed"]',
                    '2026-08-12', '2026-08-12'
                )"""
            )
        )
        connection.execute(
            text(
                """INSERT INTO webhook_deliveries (
                    delivery_id, subscription_id, event_type, event_id, payload_json,
                    next_attempt_at, created_at, updated_at
                ) VALUES (
                    'shared-delivery', 'shared-subscription', 'run.completed',
                    'event-a', '{}', '2026-08-12', '2026-08-12', '2026-08-12'
                )"""
            )
        )
        connection.execute(
            text(
                """INSERT INTO webhook_dead_letters (
                    dead_letter_id, delivery_id, subscription_id, event_type, event_id,
                    payload_json, attempt_count, created_at, dead_lettered_at
                ) VALUES (
                    'shared-dead-letter', 'shared-delivery', 'shared-subscription',
                    'run.completed', 'event-a', '{}', 5, '2026-08-12', '2026-08-12'
                )"""
            )
        )
        connection.execute(
            text(
                """INSERT INTO retention_audit_log (
                    log_id, tenant_id, run_id, action, reason, created_at
                ) VALUES (
                    'authorization-a', 'tenant-a', 'shared-run',
                    'erasure_authorized', 'rte', '2026-08-12'
                )"""
            )
        )
        connection.execute(
            text(
                """INSERT INTO retention_cleanup_state (
                    authorization_log_id, tenant_id, run_id, reason, created_at, updated_at
                ) VALUES (
                    'authorization-a', 'tenant-a', 'shared-run', 'rte',
                    '2026-08-12', '2026-08-12'
                )"""
            )
        )
        connection.execute(
            text(
                """INSERT INTO retention_cleanup_operations (
                    authorization_log_id, operation_id, status, updated_at
                ) VALUES ('authorization-a', 'operation-a', 'pending', '2026-08-12')"""
            )
        )


def _assert_upgrade_and_collisions(database_url: str) -> dict[str, dict[str, object]]:
    config = _config(database_url)
    command.upgrade(config, "024")
    engine = create_engine(database_url)
    _seed_024(engine)
    engine.dispose()

    command.upgrade(config, "025")
    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        assert inspector.get_pk_constraint("runs")["constrained_columns"] == [
            "tenant_id",
            "workspace_scope",
            "run_id",
        ]
        assert inspector.get_pk_constraint("token_engine_snapshots")["constrained_columns"] == [
            "tenant_id",
            "workspace_scope",
            "run_id",
        ]
        assert inspector.get_pk_constraint("webhook_subscriptions")["constrained_columns"] == [
            "tenant_id",
            "subscription_id",
        ]
        with engine.begin() as connection:
            assert connection.execute(
                text(
                    """SELECT tenant_id, workspace_id, workspace_scope, run_id
                    FROM token_engine_snapshots"""
                )
            ).one() == ("tenant-a", "workspace-a", "value:workspace-a", "shared-run")
            assert (
                connection.execute(
                    text(
                        "SELECT tenant_id FROM webhook_deliveries WHERE delivery_id='shared-delivery'"
                    )
                ).scalar_one()
                == "tenant-a"
            )
            assert (
                connection.execute(
                    text(
                        "SELECT tenant_id FROM webhook_dead_letters "
                        "WHERE dead_letter_id='shared-dead-letter'"
                    )
                ).scalar_one()
                == "tenant-a"
            )
            assert (
                connection.execute(
                    text(
                        "SELECT tenant_id FROM retention_cleanup_operations "
                        "WHERE operation_id='operation-a'"
                    )
                ).scalar_one()
                == "tenant-a"
            )

            connection.execute(
                text(
                    """INSERT INTO runs (
                        tenant_id, workspace_id, workspace_scope, run_id, epoch,
                        workflow_name, status, completed_steps, artifacts, channels,
                        started_at, updated_at, metadata, graph_version_ref,
                        deployment_ref, thread_id, current_node_ids
                    ) VALUES (
                        'tenant-b', 'workspace-b', 'value:workspace-b', 'shared-run', 0,
                        'workflow', 'completed', '[]', '{}', '{}', '2026-08-12',
                        '2026-08-12', '{}', 'graph:v1', 'deployment:v1', 'thread-b', '[]'
                    )"""
                )
            )
            connection.execute(
                text(
                    """INSERT INTO token_engine_snapshots (
                        tenant_id, workspace_id, workspace_scope, run_id, revision,
                        schema_version, next_token_ordinal, snapshot_json, updated_at
                    ) VALUES (
                        'tenant-b', 'workspace-b', 'value:workspace-b', 'shared-run',
                        1, 1, 2, '{}', '2026-08-12'
                    )"""
                )
            )
            connection.execute(
                text(
                    """INSERT INTO webhook_subscriptions (
                        tenant_id, subscription_id, deployment_ref, target_url, secret,
                        event_types, created_at, updated_at
                    ) VALUES (
                        'tenant-b', 'shared-subscription', 'deployment:v1',
                        'https://example.test/hook', 'secret', '["run.completed"]',
                        '2026-08-12', '2026-08-12'
                    )"""
                )
            )
            connection.execute(
                text(
                    """INSERT INTO webhook_deliveries (
                        tenant_id, delivery_id, subscription_id, event_type, event_id,
                        payload_json, next_attempt_at, created_at, updated_at
                    ) VALUES (
                        'tenant-b', 'shared-delivery', 'shared-subscription',
                        'run.completed', 'event-b', '{}', '2026-08-12',
                        '2026-08-12', '2026-08-12'
                    )"""
                )
            )
            assert (
                connection.execute(
                    text("SELECT COUNT(*) FROM runs WHERE run_id='shared-run'")
                ).scalar_one()
                == 2
            )
            assert (
                connection.execute(
                    text("SELECT COUNT(*) FROM token_engine_snapshots WHERE run_id='shared-run'")
                ).scalar_one()
                == 2
            )
            assert (
                connection.execute(
                    text(
                        "SELECT COUNT(*) FROM webhook_subscriptions "
                        "WHERE subscription_id='shared-subscription'"
                    )
                ).scalar_one()
                == 2
            )
            assert (
                connection.execute(
                    text(
                        "SELECT COUNT(*) FROM webhook_deliveries WHERE delivery_id='shared-delivery'"
                    )
                ).scalar_one()
                == 2
            )
        return _signature(engine)
    finally:
        engine.dispose()


def test_upgrade_from_024_preserves_owners_and_allows_scope_local_collisions(
    tmp_path: Path,
) -> None:
    _assert_upgrade_and_collisions(f"sqlite:///{tmp_path / 'task9-upgrade.db'}")


def test_fresh_head_matches_upgraded_task9_schema(tmp_path: Path) -> None:
    upgraded_url = f"sqlite:///{tmp_path / 'task9-upgraded.db'}"
    upgraded_signature = _assert_upgrade_and_collisions(upgraded_url)
    fresh_url = f"sqlite:///{tmp_path / 'task9-fresh.db'}"
    command.upgrade(_config(fresh_url), "head")
    fresh_engine = create_engine(fresh_url)
    try:
        assert _signature(fresh_engine) == upgraded_signature
    finally:
        fresh_engine.dispose()


@requires_docker
def test_task9_scope_migration_runs_on_postgres(postgres_container) -> None:
    admin_url = make_url(postgres_container.get_connection_url()).set(
        drivername="postgresql+psycopg"
    )
    database_name = f"zeroth_task9_{uuid4().hex}"
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{database_name}"'))
    database_url = admin_url.set(database=database_name).render_as_string(hide_password=False)
    try:
        _assert_upgrade_and_collisions(database_url)
    finally:
        admin_engine.dispose()
        admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
        with admin_engine.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{database_name}" WITH (FORCE)'))
        admin_engine.dispose()
