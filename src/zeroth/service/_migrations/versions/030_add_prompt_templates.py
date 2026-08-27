"""Persist tenant/workspace-scoped prompt template versions."""

from __future__ import annotations

from alembic import op

revision = "030"
down_revision = "029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE prompt_templates (
            tenant_id TEXT NOT NULL,
            workspace_id TEXT,
            workspace_scope TEXT NOT NULL,
            name TEXT NOT NULL,
            version INTEGER NOT NULL CHECK (version >= 1),
            template_str TEXT NOT NULL,
            variables_json TEXT NOT NULL,
            metadata_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            CHECK (
                (workspace_id IS NULL AND workspace_scope = 'null') OR
                (workspace_id IS NOT NULL AND workspace_scope = 'value:' || workspace_id)
            ),
            PRIMARY KEY (tenant_id, workspace_scope, name, version)
        )
    """)
    op.execute(
        "CREATE INDEX idx_prompt_templates_scope_name "
        "ON prompt_templates(tenant_id, workspace_scope, name, version)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE prompt_templates")
