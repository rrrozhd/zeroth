"""Initial schema: all tables consolidated from existing migrations.

Revision ID: 001
Revises: None
Create Date: 2026-04-06

This migration consolidates all table definitions from the existing
per-repository Migration dataclasses into a single Alembic migration.
DDL is dialect-compatible (TEXT for timestamps and IDs, INTEGER for numerics).
"""

from alembic import op

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create all tables for the Zeroth platform."""

    # -- schema_versions (backwards compat with existing migration system) --
    op.execute("""
        CREATE TABLE IF NOT EXISTS schema_versions (
            scope TEXT PRIMARY KEY,
            version INTEGER NOT NULL,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # -- graph_versions --
    op.execute("""
        CREATE TABLE IF NOT EXISTS graph_versions (
            graph_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            status TEXT NOT NULL,
            schema_version INTEGER NOT NULL,
            payload TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(graph_id, version)
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_graph_versions_graph_id_version
        ON graph_versions(graph_id, version DESC)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_graph_versions_status
        ON graph_versions(status)
    """)

    # -- runs --
    op.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY,
            checkpoint_id TEXT,
            parent_checkpoint_id TEXT,
            epoch INTEGER NOT NULL,
            workflow_name TEXT NOT NULL,
            status TEXT NOT NULL,
            current_step TEXT,
            completed_steps TEXT NOT NULL,
            artifacts TEXT NOT NULL,
            channels TEXT NOT NULL,
            pending_approval TEXT,
            pending_interrupt_id TEXT,
            started_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            error TEXT,
            metadata TEXT NOT NULL,
            graph_version_ref TEXT NOT NULL,
            deployment_ref TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            current_node_ids TEXT NOT NULL,
            pending_node_ids TEXT NOT NULL DEFAULT '[]',
            execution_history TEXT NOT NULL DEFAULT '[]',
            node_visit_counts TEXT NOT NULL DEFAULT '{}',
            condition_results TEXT NOT NULL DEFAULT '[]',
            audit_refs TEXT NOT NULL DEFAULT '[]',
            final_output TEXT,
            failure_state TEXT,
            tenant_id TEXT DEFAULT 'default',
            workspace_id TEXT,
            submitted_by TEXT,
            lease_worker_id TEXT,
            lease_acquired_at TEXT,
            lease_expires_at TEXT,
            failure_count INTEGER NOT NULL DEFAULT 0,
            recovery_checkpoint_id TEXT
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_runs_scope
            ON runs(tenant_id, workspace_id, deployment_ref, thread_id, run_id)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_runs_dispatch
            ON runs(deployment_ref, status, lease_expires_at)
    """)

    # -- run_checkpoints --
    op.execute("""
        CREATE TABLE IF NOT EXISTS run_checkpoints (
            checkpoint_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            checkpoint_order INTEGER NOT NULL,
            state_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    # -- threads --
    op.execute("""
        CREATE TABLE IF NOT EXISTS threads (
            thread_id TEXT PRIMARY KEY,
            graph_version_ref TEXT NOT NULL,
            deployment_ref TEXT NOT NULL,
            status TEXT NOT NULL,
            participating_agent_refs TEXT NOT NULL,
            state_snapshot_refs TEXT NOT NULL,
            checkpoint_refs TEXT NOT NULL,
            memory_bindings TEXT NOT NULL,
            run_ids TEXT NOT NULL,
            active_run_id TEXT,
            last_run_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            tenant_id TEXT DEFAULT 'default',
            workspace_id TEXT
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_threads_scope
            ON threads(tenant_id, workspace_id, deployment_ref, thread_id)
    """)

    # -- contract_versions --
    op.execute("""
        CREATE TABLE IF NOT EXISTS contract_versions (
            contract_name TEXT NOT NULL,
            version INTEGER NOT NULL,
            model_path TEXT NOT NULL,
            schema_json TEXT NOT NULL,
            metadata_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(contract_name, version)
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_contract_versions_name_version
        ON contract_versions(contract_name, version DESC)
    """)

    # -- deployment_versions --
    op.execute("""
        CREATE TABLE IF NOT EXISTS deployment_versions (
            deployment_id TEXT PRIMARY KEY,
            deployment_ref TEXT NOT NULL,
            version INTEGER NOT NULL,
            graph_id TEXT NOT NULL,
            graph_version INTEGER NOT NULL,
            graph_version_ref TEXT NOT NULL,
            serialized_graph TEXT NOT NULL,
            entry_input_contract_ref TEXT,
            entry_input_contract_version INTEGER,
            entry_output_contract_ref TEXT,
            entry_output_contract_version INTEGER,
            deployment_settings_snapshot TEXT NOT NULL,
            graph_snapshot_digest TEXT,
            contract_snapshot_digest TEXT,
            settings_snapshot_digest TEXT,
            attestation_digest TEXT,
            tenant_id TEXT DEFAULT 'default',
            workspace_id TEXT,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(deployment_ref, version)
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_deployment_versions_ref_version
            ON deployment_versions(deployment_ref, version DESC)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_deployment_versions_graph
            ON deployment_versions(graph_id, graph_version, created_at, deployment_id)
    """)

    # -- approvals --
    op.execute("""
        CREATE TABLE IF NOT EXISTS approvals (
            approval_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            thread_id TEXT,
            node_id TEXT NOT NULL,
            graph_version_ref TEXT NOT NULL,
            deployment_ref TEXT NOT NULL,
            tenant_id TEXT DEFAULT 'default',
            workspace_id TEXT,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            record_json TEXT NOT NULL
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_approvals_run_id
            ON approvals(run_id, created_at, approval_id)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_approvals_thread_id
            ON approvals(thread_id, created_at, approval_id)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_approvals_deployment_ref
            ON approvals(deployment_ref, created_at, approval_id)
    """)

    # -- node_audits --
    op.execute("""
        CREATE TABLE IF NOT EXISTS node_audits (
            audit_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            thread_id TEXT,
            node_id TEXT NOT NULL,
            graph_version_ref TEXT NOT NULL,
            deployment_ref TEXT NOT NULL,
            tenant_id TEXT DEFAULT 'default',
            workspace_id TEXT,
            created_at TEXT NOT NULL,
            record_json TEXT NOT NULL
        )
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_node_audits_run_id
            ON node_audits(run_id, created_at, audit_id)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_node_audits_thread_id
            ON node_audits(thread_id, created_at, audit_id)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_node_audits_node_id
            ON node_audits(node_id, created_at, audit_id)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_node_audits_graph_version_ref
            ON node_audits(graph_version_ref, created_at, audit_id)
    """)
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_node_audits_deployment_ref
            ON node_audits(deployment_ref, created_at, audit_id)
    """)

    # -- rate_limit_buckets --
    op.execute("""
        CREATE TABLE IF NOT EXISTS rate_limit_buckets (
            bucket_key TEXT PRIMARY KEY,
            token_count REAL NOT NULL,
            last_refill_at TEXT NOT NULL,
            capacity REAL NOT NULL DEFAULT 10.0,
            refill_rate REAL NOT NULL DEFAULT 1.0
        )
    """)

    # -- quota_counters --
    op.execute("""
        CREATE TABLE IF NOT EXISTS quota_counters (
            counter_key TEXT PRIMARY KEY,
            value INTEGER NOT NULL DEFAULT 0,
            window_start TEXT NOT NULL,
            window_seconds INTEGER NOT NULL DEFAULT 86400
        )
    """)


def downgrade() -> None:
    """Drop all tables in reverse dependency order."""
    op.execute("DROP TABLE IF EXISTS quota_counters")
    op.execute("DROP TABLE IF EXISTS rate_limit_buckets")
    op.execute("DROP TABLE IF EXISTS node_audits")
    op.execute("DROP TABLE IF EXISTS approvals")
    op.execute("DROP TABLE IF EXISTS deployment_versions")
    op.execute("DROP TABLE IF EXISTS contract_versions")
    op.execute("DROP TABLE IF EXISTS threads")
    op.execute("DROP TABLE IF EXISTS run_checkpoints")
    op.execute("DROP TABLE IF EXISTS runs")
    op.execute("DROP TABLE IF EXISTS graph_versions")
    op.execute("DROP TABLE IF EXISTS schema_versions")
