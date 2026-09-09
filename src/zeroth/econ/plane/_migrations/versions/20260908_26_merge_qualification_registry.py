"""Merge qualification registry and execution evidence histories."""

revision = "20260908_26"
down_revision = ("20260907_25", "20260904_21")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Join the independently applied histories without changing data."""


def downgrade() -> None:
    """Return to the two parent revisions without changing data."""
