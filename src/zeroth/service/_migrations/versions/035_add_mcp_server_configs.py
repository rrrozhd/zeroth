"""Add mcp_server_configs table for operator-registered MCP servers.

Revision ID: 035
Revises: 034
Create Date: 2026-08-26

Stores the stdio MCP servers an operator makes available to graphs. Graph
authors reference a server by ``ref`` and never write its ``command``/``args``/
``env`` -- that split is the whole point of the table. ``capability_bindings``
on a node are checked as a subset of this row's ``grants`` at publish, so the
row decides *which graphs may reference a server*.

It decides nothing about what the spawned process may then do. ``command``,
``args`` and ``env`` are used verbatim: there is no command allowlist, no image
or binary digest pin, no ``cwd``, no rlimits, no uid drop and no sandbox. A
principal holding ``MCP_ADMIN`` can therefore run arbitrary code as the service
user, which is why that permission is admin-tier and deliberately not held by
``OPERATOR``.

``args``, ``env`` and ``grants`` are JSON serialized to TEXT (the 001-005
convention, portable across SQLite and Postgres). ``tenant_id`` is included in
the CREATE rather than added later: ``env`` carries API keys, so the column is
load-bearing from the first row. As with ``memory_connector_configs``, ``ref``
is the PRIMARY KEY, so two tenants cannot register the same ref on one shared
DB -- the accepted single-tenant-per-deployment limitation described in 007.

``env`` is stored as plaintext JSON. That is exact parity with
``memory_connector_configs`` (005) and an accepted posture, not an oversight:
there is no envelope encryption and no ``SecretProvider`` indirection behind
this column. What makes it tolerable is that nothing reads it back out --
``mcp_server_api._mask_env`` replaces every value with ``***`` on every
response, and the discover route's 502 goes through ``safe_error_detail`` -- so
the values leave the row only to reach the spawned process. Anyone changing
that posture should change 005 with it.
"""

from __future__ import annotations

import os

import sqlalchemy as sa
from alembic import op

revision = "035"
down_revision = "034"
branch_labels = None
depends_on = None

_TABLE = "mcp_server_configs"

#: Set to ``1`` to let :func:`downgrade` drop a table that still holds
#: registrations. Named for the revision so a later migration can carry its own
#: escape hatch without inheriting this one's meaning. An Alembic ``-x``
#: argument would have been the more idiomatic channel, but
#: ``service.bootstrap.migrations`` builds ``Config()`` programmatically and
#: never sets ``cmd_opts``, so ``context.get_x_argument()`` is empty for
#: everyone running migrations through the wheel -- the flag would be
#: unreachable exactly where an operator needs it.
FORCE_DROP_ENV_VAR = "ZEROTH_MIGRATION_035_FORCE_DROP"


def upgrade() -> None:
    """Create the mcp_server_configs table."""
    op.execute("""
        CREATE TABLE IF NOT EXISTS mcp_server_configs (
            ref TEXT PRIMARY KEY,
            command TEXT NOT NULL,
            args TEXT NOT NULL,
            env TEXT NOT NULL,
            grants TEXT NOT NULL,
            tenant_id TEXT NOT NULL DEFAULT 'default',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)


def downgrade() -> None:
    """Drop the table -- refusing while it still holds registrations.

    A downgrade is the operator's rollback path after a bad release, and in
    this chain it is the only irreversible step: every other recent migration
    reshapes rows it can rebuild, while this one destroys the only copy of each
    server's ``command``/``args``/``env`` -- the last of which is credentials.
    An unguarded ``DROP TABLE`` therefore turns "roll back the release" into
    "silently lose every registration and every API key in it", with nothing on
    screen to say so.

    So it refuses first, the way 026 refuses its DDL when rows exist that the
    narrower schema could not hold. The difference is only in what counts as
    unsafe: 026 asks whether the rows would *collide*, this asks whether there
    are any rows at all, because none of them survive. Rolling back a
    deployment that never registered a server stays a no-op, which is the case
    a release rollback actually hits.

    ``ZEROTH_MIGRATION_035_FORCE_DROP=1`` is the escape hatch for an operator
    who has read the message and means it.
    """
    bind = op.get_bind()
    if not sa.inspect(bind).has_table(_TABLE):
        return
    registered = bind.execute(sa.text(f"SELECT ref FROM {_TABLE} LIMIT 1")).first()
    if registered is not None and os.environ.get(FORCE_DROP_ENV_VAR) != "1":
        raise RuntimeError(
            f"cannot downgrade {_TABLE}: it still holds MCP server registrations, "
            "whose command/args/env (API keys) exist nowhere else and would be "
            f"destroyed. Deregister them first, or set {FORCE_DROP_ENV_VAR}=1 to "
            "drop them anyway."
        )
    op.execute(f"DROP TABLE IF EXISTS {_TABLE}")
