"""Add ZER-8 tool-enforcement tables.

Revision ID: 016
Revises: 015
Create Date: 2026-07-30

Schema only, for the enforcement decision path introduced by ZER-8:

* ``decision_records`` -- one row per policy decision returned by the
  decision API. The idempotency key is scoped per tenant (``(tenant_id,
  idempotency_key)`` UNIQUE): the same key from a *different* tenant must
  create a distinct record, never collide. ``request_digest`` lets a
  replayed key be compared against the whole normalized request so that
  reusing a key for a different action can be rejected.
* ``tool_inventory_registrations`` -- what the SDK registered for a
  deployment (one row per registration event).
* ``run_attestations`` -- signed run-start attestations, unique per
  ``(tenant_id, correlation_id)``.
* ``enforcement_heartbeats`` -- deployment-level liveness, explicitly
  last-known (no upsert-in-place semantics at the schema layer).

Raw ``op.execute`` keeps the DDL portable across SQLite and Postgres,
matching the 001-015 convention. Booleans/JSON follow the same TEXT/INTEGER
portability rules used throughout this migration chain.
"""

from alembic import op

revision = "016"
down_revision = "015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the four enforcement tables and their supporting indexes."""
    # -- decision_records: one row per policy decision.
    op.execute("""
        CREATE TABLE IF NOT EXISTS decision_records (
            decision_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            idempotency_key TEXT NOT NULL,
            request_digest TEXT NOT NULL,
            decision_kind TEXT NOT NULL,
            reason_code TEXT NOT NULL,
            approval_ref TEXT,
            policy_version TEXT NOT NULL,
            deployment_ref TEXT NOT NULL,
            principal_id TEXT NOT NULL,
            action_name TEXT NOT NULL,
            action_fingerprint TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    op.create_index(
        "uq_decision_records_tenant_idempotency_key",
        "decision_records",
        ["tenant_id", "idempotency_key"],
        unique=True,
    )
    op.create_index(
        "idx_decision_records_tenant_created",
        "decision_records",
        ["tenant_id", "created_at"],
    )

    # -- tool_inventory_registrations: what the SDK registered for a
    #    deployment, one row per registration event.
    op.execute("""
        CREATE TABLE IF NOT EXISTS tool_inventory_registrations (
            registration_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            deployment_ref TEXT NOT NULL,
            graph_version TEXT NOT NULL,
            adapter_version TEXT NOT NULL,
            inventory_fingerprint TEXT NOT NULL,
            coverage TEXT NOT NULL,
            tool_count INTEGER NOT NULL,
            tools_json TEXT NOT NULL,
            registered_at TEXT NOT NULL
        )
    """)
    op.create_index(
        "idx_tool_inventory_registrations_tenant_deployment",
        "tool_inventory_registrations",
        ["tenant_id", "deployment_ref", "registered_at"],
    )

    # -- run_attestations: signed run-start attestations, unique per
    #    (tenant_id, correlation_id).
    op.execute("""
        CREATE TABLE IF NOT EXISTS run_attestations (
            attestation_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            correlation_id TEXT NOT NULL,
            deployment_ref TEXT NOT NULL,
            graph_version TEXT NOT NULL,
            adapter_version TEXT NOT NULL,
            inventory_fingerprint TEXT NOT NULL,
            inventory_coverage TEXT NOT NULL,
            tool_count INTEGER NOT NULL,
            claimed_level TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            digest TEXT NOT NULL,
            signature TEXT,
            signing_key_id TEXT,
            signing_algorithm TEXT,
            issued_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    op.create_index(
        "uq_run_attestations_tenant_correlation",
        "run_attestations",
        ["tenant_id", "correlation_id"],
        unique=True,
    )

    # -- enforcement_heartbeats: deployment-level liveness, explicitly
    #    last-known (append-only; no in-place upsert at the schema layer).
    op.execute("""
        CREATE TABLE IF NOT EXISTS enforcement_heartbeats (
            heartbeat_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            deployment_ref TEXT NOT NULL,
            graph_version TEXT,
            adapter_version TEXT,
            observed_at TEXT NOT NULL,
            reported_level TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)
    op.create_index(
        "idx_enforcement_heartbeats_tenant_deployment",
        "enforcement_heartbeats",
        ["tenant_id", "deployment_ref", "observed_at"],
    )


def downgrade() -> None:
    """Drop the four enforcement tables and their indexes."""
    op.drop_index(
        "idx_enforcement_heartbeats_tenant_deployment", table_name="enforcement_heartbeats"
    )
    op.execute("DROP TABLE IF EXISTS enforcement_heartbeats")

    op.drop_index("uq_run_attestations_tenant_correlation", table_name="run_attestations")
    op.execute("DROP TABLE IF EXISTS run_attestations")

    op.drop_index(
        "idx_tool_inventory_registrations_tenant_deployment",
        table_name="tool_inventory_registrations",
    )
    op.execute("DROP TABLE IF EXISTS tool_inventory_registrations")

    op.drop_index("idx_decision_records_tenant_created", table_name="decision_records")
    op.drop_index("uq_decision_records_tenant_idempotency_key", table_name="decision_records")
    op.execute("DROP TABLE IF EXISTS decision_records")
