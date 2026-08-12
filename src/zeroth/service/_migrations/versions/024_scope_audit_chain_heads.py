"""Partition audit chain coordination by tenant.

Revision ID: 024
Revises: 023
Create Date: 2026-08-11
"""

import sqlalchemy as sa
from alembic import op

revision = "024"
down_revision = "023"
branch_labels = None
depends_on = None

_AUDIT_COLUMNS = """
    audit_id, run_id, thread_id, node_id, graph_version_ref, deployment_ref,
    tenant_id, workspace_id, created_at, record_json, cost_usd, cost_event_id,
    chain_sequence
"""


def _create_scoped_audits() -> None:
    op.execute("""
        CREATE TABLE node_audits (
            audit_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            thread_id TEXT,
            node_id TEXT NOT NULL,
            graph_version_ref TEXT NOT NULL,
            deployment_ref TEXT NOT NULL,
            tenant_id TEXT NOT NULL,
            workspace_id TEXT,
            created_at TEXT NOT NULL,
            record_json TEXT NOT NULL,
            cost_usd REAL,
            cost_event_id TEXT,
            chain_sequence INTEGER
        )
    """)


def _create_scoped_heads() -> None:
    op.execute("""
        CREATE TABLE audit_chain_heads (
            tenant_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            head_digest TEXT,
            next_sequence INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL,
            CONSTRAINT audit_chain_heads_scope_pkey PRIMARY KEY (tenant_id, run_id)
        )
    """)


def _create_scoped_indexes() -> None:
    op.execute("""
        CREATE INDEX idx_node_audits_tenant_run
            ON node_audits(tenant_id, run_id, created_at, audit_id)
    """)
    op.execute("""
        CREATE UNIQUE INDEX uq_node_audits_tenant_run_chain_sequence
            ON node_audits(tenant_id, run_id, chain_sequence)
    """)
    op.execute("""
        CREATE INDEX idx_node_audits_thread_id
            ON node_audits(tenant_id, workspace_id, thread_id, created_at, audit_id)
    """)
    op.execute("""
        CREATE INDEX idx_node_audits_node_id
            ON node_audits(tenant_id, workspace_id, node_id, created_at, audit_id)
    """)
    op.execute("""
        CREATE INDEX idx_node_audits_graph_version_ref
            ON node_audits(tenant_id, workspace_id, graph_version_ref, created_at, audit_id)
    """)
    op.execute("""
        CREATE INDEX idx_node_audits_deployment_ref
            ON node_audits(tenant_id, workspace_id, deployment_ref, created_at, audit_id)
    """)
    op.execute("""
        CREATE INDEX idx_node_audits_tenant_created
            ON node_audits(tenant_id, created_at)
    """)


def _first_value(sql: str) -> object | None:
    return op.get_bind().execute(sa.text(sql)).scalar_one_or_none()


def upgrade() -> None:
    """Derive each legacy head owner from its child audits, refusing ambiguity."""
    blank_owner = _first_value(
        """SELECT audit_id FROM node_audits
           WHERE tenant_id IS NULL OR TRIM(tenant_id) = ''
           ORDER BY audit_id LIMIT 1"""
    )
    if blank_owner is not None:
        raise RuntimeError(f"audit scope migration refused: missing tenant owner {blank_owner!r}")

    mixed_run = _first_value(
        """SELECT run_id FROM node_audits
           GROUP BY run_id HAVING COUNT(DISTINCT tenant_id) > 1
           ORDER BY run_id LIMIT 1"""
    )
    if mixed_run is not None:
        raise RuntimeError(f"audit scope migration refused: mixed-tenant run_id {mixed_run!r}")

    missing_head_owner = _first_value(
        """SELECT h.run_id FROM audit_chain_heads h
           LEFT JOIN node_audits a ON a.run_id = h.run_id
           GROUP BY h.run_id HAVING COUNT(a.audit_id) = 0
           ORDER BY h.run_id LIMIT 1"""
    )
    if missing_head_owner is not None:
        raise RuntimeError(
            "audit scope migration refused: chain head has no child audit owner "
            f"{missing_head_owner!r}"
        )

    op.execute("ALTER TABLE node_audits RENAME TO node_audits_legacy_owner")
    _create_scoped_audits()
    op.execute(f"""
        INSERT INTO node_audits ({_AUDIT_COLUMNS})
        SELECT {_AUDIT_COLUMNS} FROM node_audits_legacy_owner
    """)

    op.execute("ALTER TABLE audit_chain_heads RENAME TO audit_chain_heads_legacy_owner")
    _create_scoped_heads()
    op.execute("""
        INSERT INTO audit_chain_heads (
            tenant_id, run_id, head_digest, next_sequence, updated_at
        )
        SELECT MIN(a.tenant_id), h.run_id, h.head_digest, h.next_sequence, h.updated_at
        FROM audit_chain_heads_legacy_owner h
        JOIN node_audits a ON a.run_id = h.run_id
        GROUP BY h.run_id, h.head_digest, h.next_sequence, h.updated_at
    """)
    op.execute("DROP TABLE audit_chain_heads_legacy_owner")
    op.execute("DROP TABLE node_audits_legacy_owner")
    _create_scoped_indexes()


def downgrade() -> None:
    """Restore run-global coordination only when scoped identities do not collide."""
    duplicate_head = _first_value(
        """SELECT run_id FROM audit_chain_heads
           GROUP BY run_id HAVING COUNT(*) > 1 ORDER BY run_id LIMIT 1"""
    )
    if duplicate_head is not None:
        raise RuntimeError(
            f"audit scope downgrade refused: duplicate global run_id {duplicate_head!r}"
        )
    duplicate_sequence = _first_value(
        """SELECT run_id FROM node_audits
           WHERE chain_sequence IS NOT NULL
           GROUP BY run_id, chain_sequence HAVING COUNT(*) > 1
           ORDER BY run_id LIMIT 1"""
    )
    if duplicate_sequence is not None:
        raise RuntimeError(
            "audit scope downgrade refused: duplicate global run sequence for "
            f"{duplicate_sequence!r}"
        )

    op.execute("ALTER TABLE node_audits RENAME TO node_audits_scoped_owner")
    op.execute("""
        CREATE TABLE node_audits (
            audit_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            thread_id TEXT,
            node_id TEXT NOT NULL,
            graph_version_ref TEXT NOT NULL,
            deployment_ref TEXT NOT NULL,
            tenant_id TEXT DEFAULT 'default',
            workspace_id TEXT,
            created_at TEXT NOT NULL,
            record_json TEXT NOT NULL,
            cost_usd REAL,
            cost_event_id TEXT,
            chain_sequence INTEGER
        )
    """)
    op.execute(f"""
        INSERT INTO node_audits ({_AUDIT_COLUMNS})
        SELECT {_AUDIT_COLUMNS} FROM node_audits_scoped_owner
    """)
    op.execute("ALTER TABLE audit_chain_heads RENAME TO audit_chain_heads_scoped_owner")
    op.execute("""
        CREATE TABLE audit_chain_heads (
            run_id TEXT PRIMARY KEY,
            head_digest TEXT,
            next_sequence INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL
        )
    """)
    op.execute("""
        INSERT INTO audit_chain_heads (run_id, head_digest, next_sequence, updated_at)
        SELECT run_id, head_digest, next_sequence, updated_at
        FROM audit_chain_heads_scoped_owner
    """)
    op.execute("DROP TABLE audit_chain_heads_scoped_owner")
    op.execute("DROP TABLE node_audits_scoped_owner")
    op.execute("""
        CREATE UNIQUE INDEX uq_node_audits_run_chain_sequence
            ON node_audits(run_id, chain_sequence)
    """)
    op.execute("CREATE INDEX idx_node_audits_run_id ON node_audits(run_id, created_at, audit_id)")
    op.execute(
        "CREATE INDEX idx_node_audits_thread_id ON node_audits(thread_id, created_at, audit_id)"
    )
    op.execute("CREATE INDEX idx_node_audits_node_id ON node_audits(node_id, created_at, audit_id)")
    op.execute(
        "CREATE INDEX idx_node_audits_graph_version_ref "
        "ON node_audits(graph_version_ref, created_at, audit_id)"
    )
    op.execute(
        "CREATE INDEX idx_node_audits_deployment_ref "
        "ON node_audits(deployment_ref, created_at, audit_id)"
    )
    op.execute("CREATE INDEX idx_node_audits_tenant_created ON node_audits(tenant_id, created_at)")
