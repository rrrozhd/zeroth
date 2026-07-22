"""Persist one coherent, CAS-replaced token-engine snapshot per run."""

from alembic import op

revision = "013"
down_revision = "012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE token_engine_snapshots (
            run_id TEXT PRIMARY KEY REFERENCES runs(run_id) ON DELETE CASCADE,
            revision INTEGER NOT NULL CHECK (revision >= 0),
            schema_version INTEGER NOT NULL CHECK (schema_version >= 1),
            next_token_ordinal INTEGER NOT NULL CHECK (next_token_ordinal >= 0),
            snapshot_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE token_engine_snapshots")
