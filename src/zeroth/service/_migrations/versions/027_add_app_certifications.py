"""Add scoped app certification state and append-only audit events."""

from alembic import op

revision = "027"
down_revision = "026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the greenfield certification promotion boundary."""
    op.execute("""
        CREATE TABLE app_certifications (
            row_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            workspace_id TEXT,
            certification_id TEXT NOT NULL,
            receipt_json TEXT NOT NULL,
            receipt_digest TEXT NOT NULL,
            state TEXT NOT NULL CHECK (
                state IN ('buildable', 'test_deployable', 'certified', 'promoted', 'revoked')
            ),
            promotion_target_key TEXT,
            promoted_at TEXT,
            revoked_at TEXT,
            revocation_reason TEXT,
            override_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    op.execute("""
        CREATE UNIQUE INDEX uq_app_certifications_scoped_identity
        ON app_certifications (
            tenant_id, COALESCE(workspace_id, ''), certification_id
        )
    """)
    op.execute("""
        CREATE UNIQUE INDEX uq_app_certifications_promoted_target
        ON app_certifications (
            tenant_id, COALESCE(workspace_id, ''), promotion_target_key
        ) WHERE promotion_target_key IS NOT NULL
    """)
    op.execute("""
        CREATE TABLE app_certification_events (
            event_id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            workspace_id TEXT,
            certification_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            state TEXT NOT NULL CHECK (
                state IN ('buildable', 'test_deployable', 'certified', 'promoted', 'revoked')
            ),
            promotion_target_key TEXT,
            actor_id TEXT NOT NULL,
            reason TEXT,
            scopes_json TEXT NOT NULL,
            override_expires_at TEXT,
            created_at TEXT NOT NULL
        )
    """)
    op.execute("""
        CREATE INDEX idx_app_certification_events_timeline
        ON app_certification_events (
            tenant_id, workspace_id, certification_id, created_at
        )
    """)
    if op.get_bind().dialect.name == "postgresql":
        op.execute("""
            CREATE FUNCTION zeroth_reject_app_certification_event_mutation()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'app_certification_events is append-only';
            END;
            $$ LANGUAGE plpgsql
        """)
        op.execute("""
            CREATE TRIGGER app_certification_events_append_only
            BEFORE UPDATE OR DELETE ON app_certification_events
            FOR EACH ROW EXECUTE FUNCTION zeroth_reject_app_certification_event_mutation()
        """)
    else:
        op.execute("""
            CREATE TRIGGER app_certification_events_no_update
            BEFORE UPDATE ON app_certification_events
            BEGIN SELECT RAISE(ABORT, 'app_certification_events is append-only'); END
        """)
        op.execute("""
            CREATE TRIGGER app_certification_events_no_delete
            BEFORE DELETE ON app_certification_events
            BEGIN SELECT RAISE(ABORT, 'app_certification_events is append-only'); END
        """)


def downgrade() -> None:
    """Drop certification tables without legacy backfill."""
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER IF EXISTS app_certification_events_append_only "
            "ON app_certification_events"
        )
        op.execute(
            "DROP FUNCTION IF EXISTS zeroth_reject_app_certification_event_mutation()"
        )
    else:
        op.execute("DROP TRIGGER IF EXISTS app_certification_events_no_update")
        op.execute("DROP TRIGGER IF EXISTS app_certification_events_no_delete")
    op.execute("DROP TABLE IF EXISTS app_certification_events")
    op.execute("DROP TABLE IF EXISTS app_certifications")
