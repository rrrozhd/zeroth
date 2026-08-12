"""Give Task 9 storage rows direct scope-local ownership identities."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "025"
down_revision = "024"
branch_labels = None
depends_on = None

_RUN_COLUMNS = """
    run_id, checkpoint_id, parent_checkpoint_id, epoch, workflow_name, status,
    current_step, completed_steps, artifacts, channels, pending_approval,
    pending_interrupt_id, started_at, updated_at, error, metadata,
    graph_version_ref, deployment_ref, thread_id, current_node_ids,
    pending_node_ids, execution_history, node_visit_counts, condition_results,
    audit_refs, final_output, failure_state, tenant_id, workspace_id, submitted_by,
    lease_worker_id, lease_acquired_at, lease_expires_at, failure_count,
    recovery_checkpoint_id, token_snapshot_write_disabled, lease_generation
"""


def _create_runs() -> None:
    op.execute("""
        CREATE TABLE runs (
            run_id TEXT NOT NULL, checkpoint_id TEXT, parent_checkpoint_id TEXT,
            epoch INTEGER NOT NULL, workflow_name TEXT NOT NULL, status TEXT NOT NULL,
            current_step TEXT, completed_steps TEXT NOT NULL, artifacts TEXT NOT NULL,
            channels TEXT NOT NULL, pending_approval TEXT, pending_interrupt_id TEXT,
            started_at TEXT NOT NULL, updated_at TEXT NOT NULL, error TEXT,
            metadata TEXT NOT NULL, graph_version_ref TEXT NOT NULL,
            deployment_ref TEXT NOT NULL, thread_id TEXT NOT NULL,
            current_node_ids TEXT NOT NULL, pending_node_ids TEXT NOT NULL DEFAULT '[]',
            execution_history TEXT NOT NULL DEFAULT '[]',
            node_visit_counts TEXT NOT NULL DEFAULT '{}',
            condition_results TEXT NOT NULL DEFAULT '[]', audit_refs TEXT NOT NULL DEFAULT '[]',
            final_output TEXT, failure_state TEXT, tenant_id TEXT NOT NULL,
            workspace_id TEXT, workspace_scope TEXT NOT NULL, submitted_by TEXT,
            lease_worker_id TEXT, lease_acquired_at TEXT, lease_expires_at TEXT,
            failure_count INTEGER NOT NULL DEFAULT 0, recovery_checkpoint_id TEXT,
            token_snapshot_write_disabled INTEGER NOT NULL DEFAULT 0,
            lease_generation INTEGER NOT NULL DEFAULT 0,
            CONSTRAINT runs_scope_pkey PRIMARY KEY (tenant_id, workspace_scope, run_id)
        )
    """)


def _create_token_snapshots() -> None:
    op.execute("""
        CREATE TABLE token_engine_snapshots (
            tenant_id TEXT NOT NULL, workspace_id TEXT, workspace_scope TEXT NOT NULL,
            run_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK (revision >= 0),
            schema_version INTEGER NOT NULL CHECK (schema_version >= 1),
            next_token_ordinal INTEGER NOT NULL CHECK (next_token_ordinal >= 0),
            snapshot_json TEXT NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY (tenant_id, workspace_scope, run_id),
            FOREIGN KEY (tenant_id, workspace_scope, run_id)
                REFERENCES runs (tenant_id, workspace_scope, run_id) ON DELETE CASCADE
        )
    """)


def _create_webhooks() -> None:
    op.execute("""
        CREATE TABLE webhook_subscriptions (
            subscription_id TEXT NOT NULL, deployment_ref TEXT NOT NULL,
            tenant_id TEXT NOT NULL, target_url TEXT NOT NULL, secret TEXT NOT NULL,
            event_types TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY (tenant_id, subscription_id)
        )
    """)
    op.execute("""
        CREATE TABLE webhook_deliveries (
            delivery_id TEXT NOT NULL, subscription_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
            event_type TEXT NOT NULL, event_id TEXT NOT NULL, payload_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', attempt_count INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 5, next_attempt_at TEXT NOT NULL,
            last_error TEXT, last_status_code INTEGER, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, PRIMARY KEY (tenant_id, delivery_id)
        )
    """)
    op.execute("""
        CREATE TABLE webhook_dead_letters (
            dead_letter_id TEXT NOT NULL, delivery_id TEXT NOT NULL,
            subscription_id TEXT NOT NULL, tenant_id TEXT NOT NULL, event_type TEXT NOT NULL,
            event_id TEXT NOT NULL, payload_json TEXT NOT NULL, attempt_count INTEGER NOT NULL,
            last_error TEXT, last_status_code INTEGER, created_at TEXT NOT NULL,
            dead_lettered_at TEXT NOT NULL, PRIMARY KEY (tenant_id, dead_letter_id)
        )
    """)


def upgrade() -> None:
    op.execute("ALTER TABLE token_engine_snapshots RENAME TO token_engine_snapshots_legacy")
    op.execute("ALTER TABLE runs RENAME TO runs_legacy")
    _create_runs()
    op.execute(f"""
        INSERT INTO runs ({_RUN_COLUMNS}, workspace_scope)
        SELECT {_RUN_COLUMNS}, CASE WHEN workspace_id IS NULL THEN 'null'
            ELSE 'value:' || workspace_id END FROM runs_legacy
    """)
    _create_token_snapshots()
    op.execute("""
        INSERT INTO token_engine_snapshots (
            tenant_id, workspace_id, workspace_scope, run_id, revision, schema_version,
            next_token_ordinal, snapshot_json, updated_at
        ) SELECT r.tenant_id, r.workspace_id, r.workspace_scope, s.run_id, s.revision,
                 s.schema_version, s.next_token_ordinal, s.snapshot_json, s.updated_at
          FROM token_engine_snapshots_legacy s JOIN runs r ON r.run_id = s.run_id
    """)
    op.execute("DROP TABLE token_engine_snapshots_legacy")
    op.execute("DROP TABLE runs_legacy")
    op.execute(
        "CREATE INDEX idx_runs_scope "
        "ON runs(tenant_id, workspace_id, deployment_ref, thread_id, run_id)"
    )
    op.execute(
        "CREATE INDEX idx_runs_dispatch "
        "ON runs(tenant_id, deployment_ref, status, lease_expires_at)"
    )

    op.execute("ALTER TABLE webhook_dead_letters RENAME TO webhook_dead_letters_legacy")
    op.execute("ALTER TABLE webhook_deliveries RENAME TO webhook_deliveries_legacy")
    op.execute("ALTER TABLE webhook_subscriptions RENAME TO webhook_subscriptions_legacy")
    _create_webhooks()
    op.execute("""
        INSERT INTO webhook_subscriptions
        SELECT subscription_id, deployment_ref, tenant_id, target_url, secret, event_types,
               active, created_at, updated_at FROM webhook_subscriptions_legacy
    """)
    op.execute("""
        INSERT INTO webhook_deliveries
        SELECT d.delivery_id, d.subscription_id, s.tenant_id, d.event_type, d.event_id,
               d.payload_json, d.status, d.attempt_count, d.max_attempts, d.next_attempt_at,
               d.last_error, d.last_status_code, d.created_at, d.updated_at
          FROM webhook_deliveries_legacy d
          JOIN webhook_subscriptions_legacy s ON s.subscription_id = d.subscription_id
    """)
    op.execute("""
        INSERT INTO webhook_dead_letters
        SELECT d.dead_letter_id, d.delivery_id, d.subscription_id, s.tenant_id, d.event_type,
               d.event_id, d.payload_json, d.attempt_count, d.last_error, d.last_status_code,
               d.created_at, d.dead_lettered_at FROM webhook_dead_letters_legacy d
          JOIN webhook_subscriptions_legacy s ON s.subscription_id = d.subscription_id
    """)
    op.execute("DROP TABLE webhook_dead_letters_legacy")
    op.execute("DROP TABLE webhook_deliveries_legacy")
    op.execute("DROP TABLE webhook_subscriptions_legacy")
    op.execute(
        "CREATE INDEX idx_webhook_subs_deployment "
        "ON webhook_subscriptions(tenant_id, deployment_ref, active)"
    )
    op.execute(
        "CREATE INDEX idx_webhook_del_pending "
        "ON webhook_deliveries(tenant_id, status, next_attempt_at)"
    )
    op.execute(
        "CREATE INDEX idx_webhook_dl_subscription "
        "ON webhook_dead_letters(tenant_id, subscription_id, dead_lettered_at DESC)"
    )

    op.add_column(
        "retention_cleanup_operations",
        sa.Column("tenant_id", sa.String(), nullable=True),
    )
    op.execute("""
        UPDATE retention_cleanup_operations SET tenant_id = (
            SELECT tenant_id FROM retention_cleanup_state s
            WHERE s.authorization_log_id = retention_cleanup_operations.authorization_log_id
        )
    """)
    op.execute(
        "ALTER TABLE retention_cleanup_operations RENAME TO retention_cleanup_operations_legacy"
    )
    op.execute("""
        CREATE TABLE retention_cleanup_operations_scoped (
            authorization_log_id TEXT NOT NULL
                REFERENCES retention_cleanup_state(authorization_log_id) ON DELETE CASCADE,
            operation_id TEXT NOT NULL,
            status TEXT NOT NULL
                CHECK (status IN ('pending', 'in_progress', 'completed', 'failed', 'skipped')),
            deleted_count INTEGER,
            error TEXT,
            revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
            updated_at TEXT NOT NULL,
            tenant_id VARCHAR NOT NULL,
            PRIMARY KEY (authorization_log_id, operation_id)
        )
    """)
    op.execute("""
        INSERT INTO retention_cleanup_operations_scoped (
            authorization_log_id, operation_id, status, deleted_count, error,
            revision, updated_at, tenant_id
        ) SELECT authorization_log_id, operation_id, status, deleted_count, error,
                 revision, updated_at, tenant_id
            FROM retention_cleanup_operations_legacy
    """)
    op.execute("DROP TABLE retention_cleanup_operations_legacy")
    op.execute(
        "ALTER TABLE retention_cleanup_operations_scoped RENAME TO retention_cleanup_operations"
    )


def downgrade() -> None:
    connection = op.get_bind()
    collision_queries = (
        ("runs", "run_id"),
        ("token_engine_snapshots", "run_id"),
        ("webhook_subscriptions", "subscription_id"),
        ("webhook_deliveries", "delivery_id"),
        ("webhook_dead_letters", "dead_letter_id"),
    )
    for table_name, identifier in collision_queries:
        row = connection.execute(
            sa.text(
                f"SELECT {identifier} FROM {table_name} GROUP BY {identifier} "
                f"HAVING COUNT(*) > 1 ORDER BY {identifier} LIMIT 1"
            )
        ).first()
        if row is not None:
            raise RuntimeError(
                "Task 9 scope downgrade refused: "
                f"{table_name}.{identifier} {row[0]!r} exists in multiple scopes"
            )

    op.execute("ALTER TABLE token_engine_snapshots RENAME TO token_engine_snapshots_scoped")
    op.execute("ALTER TABLE runs RENAME TO runs_scoped")
    op.execute("""
        CREATE TABLE runs (
            run_id TEXT PRIMARY KEY, checkpoint_id TEXT, parent_checkpoint_id TEXT,
            epoch INTEGER NOT NULL, workflow_name TEXT NOT NULL, status TEXT NOT NULL,
            current_step TEXT, completed_steps TEXT NOT NULL, artifacts TEXT NOT NULL,
            channels TEXT NOT NULL, pending_approval TEXT, pending_interrupt_id TEXT,
            started_at TEXT NOT NULL, updated_at TEXT NOT NULL, error TEXT,
            metadata TEXT NOT NULL, graph_version_ref TEXT NOT NULL,
            deployment_ref TEXT NOT NULL, thread_id TEXT NOT NULL,
            current_node_ids TEXT NOT NULL, pending_node_ids TEXT NOT NULL DEFAULT '[]',
            execution_history TEXT NOT NULL DEFAULT '[]',
            node_visit_counts TEXT NOT NULL DEFAULT '{}',
            condition_results TEXT NOT NULL DEFAULT '[]', audit_refs TEXT NOT NULL DEFAULT '[]',
            final_output TEXT, failure_state TEXT, tenant_id TEXT DEFAULT 'default',
            workspace_id TEXT, submitted_by TEXT, lease_worker_id TEXT,
            lease_acquired_at TEXT, lease_expires_at TEXT,
            failure_count INTEGER NOT NULL DEFAULT 0, recovery_checkpoint_id TEXT,
            token_snapshot_write_disabled INTEGER NOT NULL DEFAULT '0',
            lease_generation INTEGER NOT NULL DEFAULT 0
        )
    """)
    op.execute(f"INSERT INTO runs ({_RUN_COLUMNS}) SELECT {_RUN_COLUMNS} FROM runs_scoped")
    op.execute("""
        CREATE TABLE token_engine_snapshots (
            run_id TEXT PRIMARY KEY REFERENCES runs(run_id) ON DELETE CASCADE,
            revision INTEGER NOT NULL CHECK (revision >= 0),
            schema_version INTEGER NOT NULL CHECK (schema_version >= 1),
            next_token_ordinal INTEGER NOT NULL CHECK (next_token_ordinal >= 0),
            snapshot_json TEXT NOT NULL, updated_at TEXT NOT NULL
        )
    """)
    op.execute("""
        INSERT INTO token_engine_snapshots
        SELECT run_id, revision, schema_version, next_token_ordinal, snapshot_json, updated_at
        FROM token_engine_snapshots_scoped
    """)
    op.execute("DROP TABLE token_engine_snapshots_scoped")
    op.execute("DROP TABLE runs_scoped")
    op.execute(
        "CREATE INDEX idx_runs_scope "
        "ON runs(tenant_id, workspace_id, deployment_ref, thread_id, run_id)"
    )
    op.execute("CREATE INDEX idx_runs_dispatch ON runs(deployment_ref, status, lease_expires_at)")
    op.execute("CREATE INDEX ix_runs_pending_claim ON runs(deployment_ref, status, started_at)")

    op.execute("ALTER TABLE webhook_dead_letters RENAME TO webhook_dead_letters_scoped")
    op.execute("ALTER TABLE webhook_deliveries RENAME TO webhook_deliveries_scoped")
    op.execute("ALTER TABLE webhook_subscriptions RENAME TO webhook_subscriptions_scoped")
    op.execute("""
        CREATE TABLE webhook_subscriptions (
            subscription_id TEXT PRIMARY KEY, deployment_ref TEXT NOT NULL,
            tenant_id TEXT NOT NULL DEFAULT 'default', target_url TEXT NOT NULL,
            secret TEXT NOT NULL, event_types TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )
    """)
    op.execute("""
        INSERT INTO webhook_subscriptions
        SELECT subscription_id, deployment_ref, tenant_id, target_url, secret, event_types,
               active, created_at, updated_at FROM webhook_subscriptions_scoped
    """)
    op.execute("""
        CREATE TABLE webhook_deliveries (
            delivery_id TEXT PRIMARY KEY, subscription_id TEXT NOT NULL,
            event_type TEXT NOT NULL, event_id TEXT NOT NULL, payload_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', attempt_count INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 5, next_attempt_at TEXT NOT NULL,
            last_error TEXT, last_status_code INTEGER, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (subscription_id) REFERENCES webhook_subscriptions(subscription_id)
        )
    """)
    op.execute("""
        INSERT INTO webhook_deliveries
        SELECT delivery_id, subscription_id, event_type, event_id, payload_json, status,
               attempt_count, max_attempts, next_attempt_at, last_error, last_status_code,
               created_at, updated_at FROM webhook_deliveries_scoped
    """)
    op.execute("""
        CREATE TABLE webhook_dead_letters (
            dead_letter_id TEXT PRIMARY KEY, delivery_id TEXT NOT NULL,
            subscription_id TEXT NOT NULL, event_type TEXT NOT NULL, event_id TEXT NOT NULL,
            payload_json TEXT NOT NULL, attempt_count INTEGER NOT NULL, last_error TEXT,
            last_status_code INTEGER, created_at TEXT NOT NULL, dead_lettered_at TEXT NOT NULL
        )
    """)
    op.execute("""
        INSERT INTO webhook_dead_letters
        SELECT dead_letter_id, delivery_id, subscription_id, event_type, event_id,
               payload_json, attempt_count, last_error, last_status_code, created_at,
               dead_lettered_at FROM webhook_dead_letters_scoped
    """)
    op.execute("DROP TABLE webhook_dead_letters_scoped")
    op.execute("DROP TABLE webhook_deliveries_scoped")
    op.execute("DROP TABLE webhook_subscriptions_scoped")
    op.execute(
        "CREATE INDEX idx_webhook_subs_deployment ON webhook_subscriptions(deployment_ref, active)"
    )
    op.execute(
        "CREATE INDEX idx_webhook_del_pending ON webhook_deliveries(status, next_attempt_at)"
    )
    op.execute(
        "CREATE INDEX idx_webhook_dl_subscription "
        "ON webhook_dead_letters(subscription_id, dead_lettered_at DESC)"
    )
    op.execute(
        "ALTER TABLE retention_cleanup_operations RENAME TO retention_cleanup_operations_scoped"
    )
    op.execute("""
        CREATE TABLE retention_cleanup_operations (
            authorization_log_id TEXT NOT NULL
                REFERENCES retention_cleanup_state(authorization_log_id) ON DELETE CASCADE,
            operation_id TEXT NOT NULL,
            status TEXT NOT NULL
                CHECK (status IN ('pending', 'in_progress', 'completed', 'failed', 'skipped')),
            deleted_count INTEGER,
            error TEXT,
            revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
            updated_at TEXT NOT NULL,
            PRIMARY KEY (authorization_log_id, operation_id)
        )
    """)
    op.execute("""
        INSERT INTO retention_cleanup_operations (
            authorization_log_id, operation_id, status, deleted_count, error,
            revision, updated_at
        ) SELECT authorization_log_id, operation_id, status, deleted_count, error,
                 revision, updated_at
            FROM retention_cleanup_operations_scoped
    """)
    op.execute("DROP TABLE retention_cleanup_operations_scoped")
