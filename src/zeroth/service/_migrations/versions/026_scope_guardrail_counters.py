"""Scope durable rate-limit and quota state by tenant."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "026"
down_revision = "025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE rate_limit_buckets RENAME TO rate_limit_buckets_legacy")
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "ALTER TABLE rate_limit_buckets_legacy "
            "RENAME CONSTRAINT rate_limit_buckets_pkey TO rate_limit_buckets_legacy_pkey"
        )
    op.execute("""CREATE TABLE rate_limit_buckets (
        tenant_id TEXT NOT NULL, bucket_key TEXT NOT NULL,
        token_count REAL NOT NULL, last_refill_at TEXT NOT NULL,
        capacity REAL NOT NULL DEFAULT 10.0, refill_rate REAL NOT NULL DEFAULT 1.0,
        PRIMARY KEY (tenant_id, bucket_key))""")
    op.execute("""INSERT INTO rate_limit_buckets
        SELECT 'default', bucket_key, token_count, last_refill_at, capacity, refill_rate
        FROM rate_limit_buckets_legacy""")
    op.execute("DROP TABLE rate_limit_buckets_legacy")

    op.execute("ALTER TABLE quota_counters RENAME TO quota_counters_legacy")
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "ALTER TABLE quota_counters_legacy "
            "RENAME CONSTRAINT quota_counters_pkey TO quota_counters_legacy_pkey"
        )
    op.execute("""CREATE TABLE quota_counters (
        tenant_id TEXT NOT NULL, counter_key TEXT NOT NULL,
        value INTEGER NOT NULL DEFAULT 0, window_start TEXT NOT NULL,
        window_seconds INTEGER NOT NULL DEFAULT 86400,
        PRIMARY KEY (tenant_id, counter_key))""")
    op.execute("""INSERT INTO quota_counters
        SELECT 'default', counter_key, value, window_start, window_seconds
        FROM quota_counters_legacy""")
    op.execute("DROP TABLE quota_counters_legacy")


def downgrade() -> None:
    bind = op.get_bind()
    for table in ("rate_limit_buckets", "quota_counters"):
        foreign = bind.execute(
            sa.text(f"SELECT tenant_id FROM {table} WHERE tenant_id <> 'default' LIMIT 1")
        ).first()
        if foreign is not None:
            raise RuntimeError(f"cannot downgrade {table}: non-default tenant rows would collide")

    op.execute("ALTER TABLE rate_limit_buckets RENAME TO rate_limit_buckets_scoped")
    if bind.dialect.name == "postgresql":
        op.execute(
            "ALTER TABLE rate_limit_buckets_scoped "
            "RENAME CONSTRAINT rate_limit_buckets_pkey TO rate_limit_buckets_scoped_pkey"
        )
    op.execute("""CREATE TABLE rate_limit_buckets (
        bucket_key TEXT PRIMARY KEY, token_count REAL NOT NULL,
        last_refill_at TEXT NOT NULL, capacity REAL NOT NULL DEFAULT 10.0,
        refill_rate REAL NOT NULL DEFAULT 1.0)""")
    op.execute("""INSERT INTO rate_limit_buckets
        SELECT bucket_key, token_count, last_refill_at, capacity, refill_rate
        FROM rate_limit_buckets_scoped""")
    op.execute("DROP TABLE rate_limit_buckets_scoped")

    op.execute("ALTER TABLE quota_counters RENAME TO quota_counters_scoped")
    if bind.dialect.name == "postgresql":
        op.execute(
            "ALTER TABLE quota_counters_scoped "
            "RENAME CONSTRAINT quota_counters_pkey TO quota_counters_scoped_pkey"
        )
    op.execute("""CREATE TABLE quota_counters (
        counter_key TEXT PRIMARY KEY, value INTEGER NOT NULL DEFAULT 0,
        window_start TEXT NOT NULL, window_seconds INTEGER NOT NULL DEFAULT 86400)""")
    op.execute("""INSERT INTO quota_counters
        SELECT counter_key, value, window_start, window_seconds FROM quota_counters_scoped""")
    op.execute("DROP TABLE quota_counters_scoped")
