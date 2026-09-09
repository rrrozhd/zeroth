"""Add the tenant-scoped model-migration qualification registry.

Revision ID: 20260904_21
Revises: 20260902_20
Create Date: 2026-09-04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260904_21"
down_revision = "20260902_20"
branch_labels = None
depends_on = None

_TABLE = "model_migration_qualifications"


def _table_names() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if _TABLE in _table_names():
        return
    op.create_table(
        _TABLE,
        sa.Column("qualification_id", sa.String(192), primary_key=True),
        sa.Column("tenant_id", sa.String(128), primary_key=True),
        sa.Column("record_version", sa.String(32), nullable=False),
        sa.Column("scope_digest", sa.String(64), nullable=False),
        sa.Column("active_scope_key", sa.String(64), nullable=True),
        sa.Column("workload", sa.String(128), nullable=False),
        sa.Column("incumbent_model", sa.String(255), nullable=False),
        sa.Column("candidate_model", sa.String(255), nullable=False),
        sa.Column("policy_digest", sa.String(64), nullable=False),
        sa.Column("algorithm_version", sa.String(128), nullable=False),
        sa.Column("cluster_family_set", sa.JSON(), nullable=False),
        sa.Column("mean_probability_contract", sa.String(128), nullable=False),
        sa.Column("icc_upper_bound_exact", sa.String(128), nullable=False),
        sa.Column("cluster_size_vector", sa.JSON(), nullable=False),
        sa.Column("cluster_size_vector_sha256", sa.String(64), nullable=False),
        sa.Column("authorization_count_threshold", sa.Integer(), nullable=False),
        sa.Column("boundary_error_alpha_exact", sa.String(128), nullable=False),
        sa.Column("certificate_artifact_sha256", sa.String(64), nullable=False),
        sa.Column("family_confidence_set_method", sa.String(128), nullable=False),
        sa.Column("family_qualification_artifact_sha256", sa.String(64), nullable=False),
        sa.Column("icc_upper_confidence_method", sa.String(128), nullable=False),
        sa.Column("icc_qualification_artifact_sha256", sa.String(64), nullable=False),
        sa.Column("independent_unit_definition", sa.String(512), nullable=False),
        sa.Column("grouping_keys", sa.JSON(), nullable=False),
        sa.Column("source_window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_evidence_sha256", sa.String(64), nullable=False),
        sa.Column("issuer", sa.String(128), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("supersedes_qualification_id", sa.String(192), nullable=True),
        sa.Column("record_digest", sa.String(64), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by", sa.String(128), nullable=True),
        sa.Column("revocation_digest", sa.String(64), nullable=True),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_by", sa.String(128), nullable=True),
        sa.Column("superseded_by_qualification_id", sa.String(192), nullable=True),
    )
    op.create_index(
        "ix_model_migration_qualifications_tenant_scope",
        _TABLE,
        ["tenant_id", "scope_digest"],
    )
    op.create_index(
        "uq_model_migration_qualifications_active_scope",
        _TABLE,
        ["tenant_id", "active_scope_key"],
        unique=True,
    )
    op.create_index(
        "ix_model_migration_qualifications_tenant_workload",
        _TABLE,
        ["tenant_id", "workload"],
    )


def downgrade() -> None:
    if _TABLE in _table_names():
        op.drop_table(_TABLE)
