"""Record structured tool identities on an inventory registration.

Revision ID: 017
Revises: 016
Create Date: 2026-07-30

Migration 016 stored a registration's ``tools_json`` as bare *names*, and took
``inventory_fingerprint`` and ``tool_count`` verbatim from the caller. A
security audit showed that this let a client supply both sides of the
enforcement comparison: register ``{coverage: "complete", tools: [],
tool_count: 999, fingerprint: "X"}``, attest ``"X"``, and the verifying
provider -- comparing two strings the same client chose -- classified the run
``ENFORCED`` with no governed tools.

Two changes close it:

* ``tool_identities_json`` carries ``{"name", "fingerprint"}`` per tool, and
  the server recomputes the aggregate digest and the count from it
  (:mod:`zeroth.governance.attestations.inventory`). The column is nullable
  because SQLite cannot add a ``NOT NULL`` column to a populated table without
  a default, and a default here would fabricate an identity set.
* Rows written before this revision are downgraded to ``partial`` coverage.
  Their digest and count were client-certified under the rejected scheme, so
  continuing to honour their ``complete`` claim would carry the vulnerability
  forward across the upgrade. Nothing is deleted -- the history stays readable,
  it simply stops certifying completeness.

``tools_json`` is deliberately left in place and still written. It is NOT NULL
from 016, it remains useful for inspection, and it is never read back into a
registration.
"""

from alembic import op

revision = "017"
down_revision = "016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the identities column and fail existing registrations closed."""
    op.execute("ALTER TABLE tool_inventory_registrations ADD COLUMN tool_identities_json TEXT")
    # Fail-closed: a registration recorded under the client-certified scheme
    # must not keep asserting complete coverage once completeness is what
    # grants ENFORCED.
    op.execute(
        "UPDATE tool_inventory_registrations SET coverage = 'partial' "
        "WHERE tool_identities_json IS NULL"
    )


def downgrade() -> None:
    """Drop the identities column.

    The coverage downgrade is not reversed: the original values were not
    retained, and restoring a ``complete`` claim would be inventing one.
    """
    op.execute("ALTER TABLE tool_inventory_registrations DROP COLUMN tool_identities_json")
