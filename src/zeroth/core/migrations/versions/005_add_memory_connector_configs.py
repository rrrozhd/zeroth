"""Add memory_connector_configs table for runtime-managed connectors.

Revision ID: 005
Revises: 004
Create Date: 2026-07-07

Stores operator-authored memory connector configurations so connectors
created through the console (POST /v1/connectors) survive restarts and are
re-registered at bootstrap. ``params`` is a JSON object serialized to TEXT
(same convention as other tables; portable across SQLite and Postgres).
"""

from alembic import op

revision = "005"
down_revision = "004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the memory_connector_configs table."""
    op.execute("""
        CREATE TABLE IF NOT EXISTS memory_connector_configs (
            ref TEXT PRIMARY KEY,
            backend_type TEXT NOT NULL,
            params TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)


def downgrade() -> None:
    """Drop the memory_connector_configs table."""
    op.execute("DROP TABLE IF EXISTS memory_connector_configs")
