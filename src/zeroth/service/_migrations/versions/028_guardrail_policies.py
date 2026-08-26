"""Add append-only scoped ingress-guardrail policy history."""

from __future__ import annotations

from alembic import op

revision = "028"
down_revision = "027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE guardrail_policy_revisions (
        tenant_id TEXT NOT NULL,
        revision_id TEXT NOT NULL,
        scope_type TEXT NOT NULL CHECK (scope_type IN ('tenant', 'deployment')),
        deployment_ref TEXT NOT NULL DEFAULT '',
        policy_json TEXT NOT NULL,
        changed_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (tenant_id, revision_id))""")
    op.execute("""CREATE INDEX idx_guardrail_policy_lookup
        ON guardrail_policy_revisions
        (tenant_id, scope_type, deployment_ref, created_at, revision_id)""")
    op.execute("""CREATE TABLE guardrail_admission_state (
        tenant_id TEXT NOT NULL,
        workspace_id TEXT,
        workspace_scope TEXT NOT NULL,
        deployment_ref TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (tenant_id, workspace_scope, deployment_ref),
        CHECK ((workspace_id IS NULL AND workspace_scope = 'null') OR
               (workspace_id IS NOT NULL AND workspace_scope = 'value:' || workspace_id)))""")


def downgrade() -> None:
    op.execute("DROP TABLE guardrail_admission_state")
    op.execute("DROP TABLE guardrail_policy_revisions")
