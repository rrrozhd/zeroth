"""Add WS-D provenance signature columns to deployment_versions.

Revision ID: 006
Revises: 005
Create Date: 2026-07-11

Adds the keyed-signature columns that make deployment attestations
tamper-resistant (signed) rather than only tamper-evident (unkeyed digest):

* ``attestation_signature``      — hex signature over ``attestation_digest``
* ``attestation_signing_key_id`` — the key the signature was produced with
* ``attestation_algorithm``      — "HS256" | "Ed25519"

All nullable so pre-signing rows hydrate as unsigned-legacy. The audit chain
needs no DDL: its signature fields ride inside the existing ``node_audits``
``record_json`` TEXT blob.
"""

import sqlalchemy as sa
from alembic import op

revision = "006"
down_revision = "005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the three nullable signature columns to deployment_versions."""
    op.add_column(
        "deployment_versions",
        sa.Column("attestation_signature", sa.Text(), nullable=True),
    )
    op.add_column(
        "deployment_versions",
        sa.Column("attestation_signing_key_id", sa.Text(), nullable=True),
    )
    op.add_column(
        "deployment_versions",
        sa.Column("attestation_algorithm", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    """Drop the WS-D signature columns from deployment_versions."""
    op.drop_column("deployment_versions", "attestation_algorithm")
    op.drop_column("deployment_versions", "attestation_signing_key_id")
    op.drop_column("deployment_versions", "attestation_signature")
