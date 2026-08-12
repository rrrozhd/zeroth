"""Scope contract version identity by tenant.

Revision ID: 023
Revises: 022
Create Date: 2026-08-11
"""

import sqlalchemy as sa
from alembic import op

revision = "023"
down_revision = "022"
branch_labels = None
depends_on = None

_LEGACY_COLUMNS = "contract_name, version, model_path, schema_json, metadata_json, created_at"


def _create_scoped_contract_versions() -> None:
    op.execute("""
        CREATE TABLE contract_versions (
            tenant_id TEXT NOT NULL,
            contract_name TEXT NOT NULL,
            version INTEGER NOT NULL,
            model_path TEXT NOT NULL,
            schema_json TEXT NOT NULL,
            metadata_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT contract_versions_scope_pkey
                PRIMARY KEY (tenant_id, contract_name, version)
        )
    """)


def upgrade() -> None:
    """Assign legacy rows to the reserved tenant and install scoped identity."""
    op.execute("ALTER TABLE contract_versions RENAME TO contract_versions_legacy_global")
    _create_scoped_contract_versions()
    op.execute(f"""
        INSERT INTO contract_versions (tenant_id, {_LEGACY_COLUMNS})
        SELECT 'default', {_LEGACY_COLUMNS}
        FROM contract_versions_legacy_global
    """)
    op.execute("DROP TABLE contract_versions_legacy_global")
    op.execute("""
        CREATE INDEX idx_contract_versions_tenant_name_version
            ON contract_versions(tenant_id, contract_name, version DESC)
    """)


def downgrade() -> None:
    """Restore global identity only when tenant-local identities do not collide."""
    duplicate = (
        op.get_bind()
        .execute(
            sa.text(
                """SELECT contract_name, version FROM contract_versions
                   GROUP BY contract_name, version HAVING COUNT(*) > 1
                   ORDER BY contract_name, version LIMIT 1"""
            )
        )
        .first()
    )
    if duplicate is not None:
        raise RuntimeError(
            "contract versions downgrade refused: duplicate global identity "
            f"{duplicate[0]!r}@{duplicate[1]}"
        )

    op.execute("ALTER TABLE contract_versions RENAME TO contract_versions_scoped_identity")
    op.execute("""
        CREATE TABLE contract_versions (
            contract_name TEXT NOT NULL,
            version INTEGER NOT NULL,
            model_path TEXT NOT NULL,
            schema_json TEXT NOT NULL,
            metadata_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(contract_name, version)
        )
    """)
    op.execute(f"""
        INSERT INTO contract_versions ({_LEGACY_COLUMNS})
        SELECT {_LEGACY_COLUMNS} FROM contract_versions_scoped_identity
    """)
    op.execute("DROP TABLE contract_versions_scoped_identity")
    op.execute("""
        CREATE INDEX idx_contract_versions_name_version
            ON contract_versions(contract_name, version DESC)
    """)
