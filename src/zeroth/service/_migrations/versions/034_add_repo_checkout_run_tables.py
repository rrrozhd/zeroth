"""Add ZER-37 repo checkout and repo run tables.

Revision ID: 034
Revises: 033
Create Date: 2026-08-26

Schema only, for the service-layer repo-checkout/repo-run surface:

* ``repo_checkouts`` -- one row per staged repository checkout: source
  identities (installation, repository grant, requested ref), the pinned
  commit/tree identities the pipeline verified, lifecycle state
  (REQUESTED..FAILED), and the deployment-style attestation envelope
  (digest, keyed signature triple, payload JSON).
* ``repo_runs`` -- one script execution against a staged checkout, with the
  webhook-worker lease pattern under explicit names: ``claim_generation``
  fences completions and ``lease_expires_at`` doubles as the due horizon.

Both tables are tenant+workspace scoped (``workspace_id`` nullable, per the
runs/deployments convention). Raw ``op.execute`` keeps the DDL portable
across SQLite and Postgres, matching the 001-027 convention. Booleans follow
the INTEGER portability rule used throughout this migration chain.
"""

from alembic import op

revision = "034"
down_revision = "033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the repo checkout and repo run tables and their indexes."""
    op.execute("""
        CREATE TABLE IF NOT EXISTS repo_checkouts (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            workspace_id TEXT,
            repository_pk TEXT NOT NULL,
            installation_id INTEGER NOT NULL,
            repository_id INTEGER NOT NULL,
            repository_full_name TEXT NOT NULL,
            requested_ref TEXT NOT NULL,
            resolved_commit_sha TEXT,
            git_tree_id TEXT,
            tree_digest TEXT,
            config_digest TEXT,
            manifest_digest TEXT,
            script_name TEXT,
            state TEXT NOT NULL,
            failure_code TEXT,
            failure_detail TEXT,
            staged_path TEXT,
            file_count INTEGER,
            size_bytes INTEGER,
            has_lfs_pointers INTEGER,
            cache_hit INTEGER NOT NULL DEFAULT 0,
            verified_at TEXT,
            expires_at TEXT,
            attestation_digest TEXT,
            attestation_signature TEXT,
            attestation_key_id TEXT,
            attestation_algorithm TEXT,
            attestation_payload_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (repository_pk) REFERENCES github_repositories (id)
        )
    """)
    op.create_index(
        "idx_repo_checkouts_tenant_state",
        "repo_checkouts",
        ["tenant_id", "workspace_id", "state", "expires_at"],
    )
    op.create_index(
        "idx_repo_checkouts_tenant_created",
        "repo_checkouts",
        ["tenant_id", "workspace_id", "created_at"],
    )

    op.execute("""
        CREATE TABLE IF NOT EXISTS repo_runs (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            workspace_id TEXT,
            checkout_id TEXT NOT NULL,
            script_name TEXT NOT NULL,
            input_payload_json TEXT,
            state TEXT NOT NULL,
            exit_code INTEGER,
            failure_code TEXT,
            smoke_passed INTEGER,
            output_payload_json TEXT,
            claimed_by TEXT,
            claim_generation INTEGER NOT NULL DEFAULT 0,
            claimed_at TEXT,
            lease_expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (checkout_id) REFERENCES repo_checkouts (id)
        )
    """)
    op.create_index(
        "idx_repo_runs_tenant_state_due",
        "repo_runs",
        ["tenant_id", "workspace_id", "state", "lease_expires_at"],
    )
    op.create_index(
        "idx_repo_runs_tenant_checkout",
        "repo_runs",
        ["tenant_id", "workspace_id", "checkout_id", "created_at"],
    )


def downgrade() -> None:
    """Drop the repo checkout and repo run tables and their indexes."""
    op.drop_index("idx_repo_runs_tenant_checkout", table_name="repo_runs")
    op.drop_index("idx_repo_runs_tenant_state_due", table_name="repo_runs")
    op.execute("DROP TABLE IF EXISTS repo_runs")

    op.drop_index("idx_repo_checkouts_tenant_created", table_name="repo_checkouts")
    op.drop_index("idx_repo_checkouts_tenant_state", table_name="repo_checkouts")
    op.execute("DROP TABLE IF EXISTS repo_checkouts")
